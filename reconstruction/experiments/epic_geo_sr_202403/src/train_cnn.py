"""Fair C/G/CG CNN training with optional CG-only coarse closure loss."""
from __future__ import annotations

import argparse
import json
import sys
from collections import OrderedDict
from pathlib import Path

import numpy as np

from common import DATA, ROOT, configured_inputs, load_config
from cnn_utils import ScenePatchDataset, closure_mse, feature_names as group_feature_names, load_scene, predict_tiled

C = ["coarse_cloud_fraction_up", "coarse_valid_fraction_up"]
G = ["geo_cloud_fraction", "geo_cloud_fraction_std", "geo_boundary_fraction", "geo_source_count", "geo_valid_fraction", "geo_time_difference_minutes", "geo_missing_disagreement_mask", "geo_missing_vza_mask"]
Z = ["epic_vza", "epic_sza", "land_ocean", "valid_mask"]


def require_torch():
    try:
        import torch
        return torch
    except BaseException as exc:
        raise RuntimeError("PyTorch preflight failed. This host currently has an OpenMP duplicate-runtime conflict. Repair the Conda environment to retain exactly one of libomp.dll/libiomp5md.dll, then run `conda run -n pytorch python -c \"import torch; print(torch.__version__)\"`. KMP_DUPLICATE_LIB_OK is intentionally not used.") from exc


def feature_names(groups: list[str]) -> list[str]:
    return sum(({"C": C, "G": G, "Z": Z}[group] for group in groups), [])


def normalized(scene: Path, names: list[str], stats: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    with np.load(scene, allow_pickle=False) as z:
        valid=z["valid_mask"].astype(bool); target=z["target"].astype(np.float32); coarse=z["coarse_cloud_fraction_up"].astype(np.float32); cell=np.load(DATA/"coarse50"/(scene.stem+"_coarse50.npz"),allow_pickle=False)["cell_id"]
        channels=[]
        for name in names:
            value=z[name].astype(np.float32); info=stats.get(name)
            if info: value=(value-info["mean"])/info["std"]
            channels.append(np.nan_to_num(value,nan=0.0,posinf=0.0,neginf=0.0))
    return np.stack(channels),target,valid,coarse,cell


def main() -> None:
    parser=argparse.ArgumentParser();parser.add_argument("model",choices=["cnn_c","cnn_g","cnn_cg","cnn_cg_closure"]);parser.add_argument("--seed",type=int,default=42);parser.add_argument("--approve-coarsening",action="store_true",help="affirm that figures/coarsening_validation were inspected");args=parser.parse_args()
    if not args.approve_coarsening: raise SystemExit("Refusing training: inspect reports/04_coarsening_validation.md and rerun with --approve-coarsening.")
    torch=require_torch(); from torch import nn; from torch.utils.data import DataLoader,Dataset; from cnn_model import build_cnn
    cfg,_,_,_=configured_inputs(); cnn=load_config("cnn.yaml"); groups=cnn["models"][args.model]; names=feature_names(groups); stats=json.loads((DATA/"prepared"/"normalization_stats.json").read_text()); index=np.genfromtxt(DATA/"patches"/"train_patch_index.csv",delimiter=",",names=True,dtype=None,encoding="utf-8-sig")
    class Patches(Dataset):
        """Group patches by scene and retain a two-scene cache on CPU.

        This prevents the CPU-only loader from decompressing a complete NPZ once
        per patch, while retaining shuffled patch order inside each scene.
        """
        def __init__(self):
            self.rows = list(index)
            self.by_scene = {}
            for i, row in enumerate(self.rows):
                self.by_scene.setdefault(Path(row["scene_file"]).name, []).append(i)
            self.order = list(range(len(self.rows)))
            self.cache = OrderedDict()
            self.set_epoch(args.seed)

        def set_epoch(self, seed):
            rng = np.random.default_rng(seed)
            groups = list(self.by_scene.values())
            rng.shuffle(groups)
            self.order = [i for group in groups for i in rng.permutation(group)]

        def __len__(self): return len(self.rows)

        def scene_arrays(self, name):
            if name not in self.cache:
                scene = DATA / "prepared" / name
                self.cache[name] = normalized(scene, names, stats)[:3]
                if len(self.cache) > 2:
                    self.cache.popitem(last=False)
            else:
                self.cache.move_to_end(name)
            return self.cache[name]

        def __getitem__(self, i):
            row=self.rows[self.order[i]]; x,y,v=self.scene_arrays(Path(row["scene_file"]).name); r,c,s=int(row["row"]),int(row["col"]),int(row["size"]); return torch.from_numpy(x[:,r:r+s,c:c+s]),torch.from_numpy(y[r:r+s,c:c+s]),torch.from_numpy(v[r:r+s,c:c+s].astype(np.float32))
    torch.manual_seed(args.seed); device="cuda" if torch.cuda.is_available() else "cpu"; model=build_cnn(len(names)).to(device); opt=torch.optim.AdamW(model.parameters(),lr=cnn["learning_rate"],weight_decay=cnn["weight_decay"]); bce=nn.BCEWithLogitsLoss(reduction="none"); patches=Patches(); loader=DataLoader(patches,batch_size=cnn["batch_size"],shuffle=False,num_workers=0)
    model.train()
    for epoch in range(int(cnn["max_epochs"])):
        patches.set_epoch(args.seed + epoch)
        losses=[]
        for x,y,v in loader:
            x,y,v=x.to(device),y.to(device),v.to(device); logits=model(x).squeeze(1); loss=(bce(logits,y)*v).sum()/v.sum().clamp_min(1); opt.zero_grad();loss.backward();opt.step();losses.append(float(loss.detach().cpu()))
        print(json.dumps({"epoch":epoch+1,"train_bce":float(np.mean(losses))}))
    out=ROOT/"checkpoints"/args.model;out.mkdir(parents=True,exist_ok=True);torch.save({"state_dict":model.state_dict(),"input_channels":names,"seed":args.seed,"dataset_version":cfg["dataset_version"]},out/f"seed_{args.seed}.pt")


def validation_metrics(model, names, stats, rows, device, cnn):
    squared_error = bce_total = 0.0
    count = 0
    for row in rows:
        x, target, valid, _, _, _ = load_scene(DATA / "prepared" / f"{row['sample_id']}.npz", names, stats)
        prediction = predict_tiled(model, x, device, int(cnn["validation_tile_size"]), int(cnn["validation_halo_pixels"]))
        use = valid & np.isfinite(prediction)
        p, t = prediction[use], target[use]
        squared_error += float(np.sum((p - t) ** 2))
        bce_total += float(-np.sum(t * np.log(np.clip(p, 1e-6, 1 - 1e-6)) + (1 - t) * np.log(np.clip(1 - p, 1e-6, 1 - 1e-6))))
        count += int(use.sum())
    return {"val_Brier": squared_error / count, "val_BCE": bce_total / count, "val_n": count}


def main_complete() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("model", choices=["cnn_c", "cnn_g", "cnn_cg", "cnn_cg_closure"])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--approve-coarsening", action="store_true")
    parser.add_argument("--max-epochs", type=int, default=None, help="testing override; omitted uses cnn.yaml")
    parser.add_argument("--max-batches-per-epoch", type=int, default=None, help="testing override only")
    args = parser.parse_args()
    if not args.approve_coarsening:
        raise SystemExit("Refusing training: inspect reports/04_coarsening_validation.md and rerun with --approve-coarsening.")
    torch = require_torch()
    from torch import nn
    from torch.utils.data import DataLoader
    from cnn_model import build_cnn
    from common import resolve_scene_rows, split_for_date, write_json
    cfg, _, _, _ = configured_inputs()
    cnn = load_config("cnn.yaml")
    names = group_feature_names(cnn["models"][args.model])
    stats = json.loads((DATA / "prepared" / "normalization_stats.json").read_text())
    index = np.genfromtxt(DATA / "patches" / "train_patch_index.csv", delimiter=",", names=True, dtype=None, encoding="utf-8-sig")
    val_rows = [r for r in resolve_scene_rows() if split_for_date(r["scene_date"], cfg) == "validation"]
    if not val_rows:
        raise RuntimeError("Frozen validation split is empty.")
    torch.manual_seed(args.seed); np.random.seed(args.seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    torch.set_num_threads(int(cnn["cpu_num_threads"]))
    model = build_cnn(len(names)).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=cnn["learning_rate"], weight_decay=cnn["weight_decay"])
    native_bce = nn.BCEWithLogitsLoss(reduction="none")
    patches = ScenePatchDataset(index, names, stats, cache_scenes=int(cnn["scene_cache_scenes"]))
    loader = DataLoader(patches, batch_size=int(cnn["batch_size"]), shuffle=False, num_workers=0, pin_memory=device == "cuda")
    output = ROOT / "checkpoints" / args.model; output.mkdir(parents=True, exist_ok=True)
    checkpoint, history_file = output / f"seed_{args.seed}.pt", output / f"seed_{args.seed}_history.json"
    best_brier, waiting, history = float("inf"), 0, []
    epochs = args.max_epochs if args.max_epochs is not None else int(cnn["max_epochs"])
    for epoch in range(1, epochs + 1):
        model.train(); patches.set_epoch(args.seed + epoch - 1); losses, closures = [], []
        for batch, (x, y, valid, cell_key, coarse_target) in enumerate(loader, start=1):
            x, y, valid = x.to(device), y.to(device), valid.to(device)
            cell_key, coarse_target = cell_key.to(device), coarse_target.to(device)
            logits = model(x).squeeze(1)
            # BCE must never receive NaN targets outside the Earth mask: NaN * 0
            # remains NaN and would otherwise poison every model parameter.
            safe_y = torch.where(valid.bool(), y, torch.zeros_like(y))
            native = (native_bce(logits, safe_y) * valid).sum() / valid.sum().clamp_min(1)
            closure = closure_mse(torch.sigmoid(logits), valid, cell_key, coarse_target) if args.model == "cnn_cg_closure" else logits.new_zeros(())
            loss = native + float(cnn["lambda_closure"]) * closure
            optimizer.zero_grad(set_to_none=True); loss.backward(); optimizer.step()
            losses.append(float(native.detach().cpu())); closures.append(float(closure.detach().cpu()))
            if batch % int(cnn["progress_every_batches"]) == 0:
                print(json.dumps({"event":"batch", "epoch":epoch, "batch":batch, "train_native_BCE":float(np.mean(losses)), "train_closure_MSE":float(np.mean(closures))}), flush=True)
            if args.max_batches_per_epoch is not None and batch >= args.max_batches_per_epoch:
                break
        row = {"epoch":epoch, "train_native_BCE":float(np.mean(losses)), "train_closure_MSE":float(np.mean(closures)), **validation_metrics(model, names, stats, val_rows, device, cnn)}
        row["best"] = row["val_Brier"] < best_brier - float(cnn["early_stopping_min_delta"])
        if row["best"]:
            best_brier, waiting = row["val_Brier"], 0
            torch.save({"state_dict":model.state_dict(), "input_channels":names, "seed":args.seed, "model":args.model, "dataset_version":cfg["dataset_version"], "best_validation":row, "cnn_config":cnn}, checkpoint)
        else:
            waiting += 1
        row["early_stopping_wait"] = waiting; history.append(row); write_json(history_file, history)
        print(json.dumps({"event":"epoch", **row}), flush=True)
        if waiting >= int(cnn["early_stopping_patience"]):
            print(json.dumps({"event":"early_stop", "epoch":epoch, "best_val_Brier":best_brier}), flush=True); break
    if not checkpoint.exists():
        raise RuntimeError("No best checkpoint was written.")
    print(json.dumps({"event":"complete", "checkpoint":str(checkpoint), "best_val_Brier":best_brier}), flush=True)


if __name__=="__main__":
    main_complete()
