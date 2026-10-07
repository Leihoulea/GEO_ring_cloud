"""Prepare model tensors, train-only normalization, and deterministic patches."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

from common import DATA, configured_inputs, ensure_layout, load_config, read_epic, resolve_scene_rows, split_for_date, write_json, write_manifest


def channels(scene: str, epic: dict[str, np.ndarray], coarse: np.lib.npyio.NpzFile, geo: np.lib.npyio.NpzFile) -> tuple[dict[str, np.ndarray], np.ndarray]:
    valid = epic["valid"] & np.isfinite(coarse["coarse_cloud_fraction_up"])
    # `valid_mask` is stored once as both the Z-channel and the loss mask.
    common = {"epic_vza": epic["epic_vza"], "epic_sza": epic["epic_sza"], "land_ocean": np.isin(epic["surface_type"], [1, 4]).astype(np.float32)}
    c = {"coarse_cloud_fraction_up": coarse["coarse_cloud_fraction_up"], "coarse_valid_fraction_up": np.where(coarse["cell_id"] >= 0, coarse["valid_fraction_by_cell"][coarse["cell_id"]], np.nan)}
    g = {key: np.asarray(geo[key]) for key in geo.files if key.startswith("geo_")}
    return {**c, **g, **common}, valid


def main() -> None:
    ensure_layout(); cfg, _, _, manifest = configured_inputs(); cnn = load_config("cnn.yaml"); rows = resolve_scene_rows(); prepared=[]
    stat_values: dict[str, list[np.ndarray]] = {}
    for row in rows:
        split = split_for_date(row["scene_date"], cfg)
        output = DATA / "prepared" / f"{row['sample_id']}.npz"
        if output.exists():
            # Resume immutable scene tensors after a desktop-session timeout,
            # while still incorporating them in train-only normalization.
            try:
                with np.load(output, allow_pickle=False) as z:
                    valid = z["valid_mask"].astype(bool)
                    if split == "train":
                        for key in z.files:
                            if key in {"target", "valid_mask", "split"}:
                                continue
                            value = z[key]
                            finite = value[np.isfinite(value) & valid]
                            if finite.size:
                                stat_values.setdefault(key, []).append(finite.astype(np.float32))
                prepared.append(output)
                continue
            except (EOFError, OSError, ValueError, KeyError):
                # This is a generated, incomplete local temporary artifact,
                # never an input product. Remove only this exact file.
                output.unlink()
        epic = read_epic(Path(row["epic_clm_file"]), cfg)
        with np.load(DATA / "coarse50" / f"{row['sample_id']}_coarse50.npz", allow_pickle=False) as coarse, np.load(DATA / "geo_features" / f"{row['sample_id']}_geo_features.npz", allow_pickle=False) as geo:
            arrays, valid = channels(row["sample_id"], epic, coarse, geo)
            np.savez_compressed(output, target=epic["cloud"], valid_mask=valid.astype(np.uint8), split=np.array(split), **arrays)
            prepared.append(output)
            if split == "train":
                for key, value in arrays.items():
                    finite = value[np.isfinite(value) & valid]
                    if finite.size and key != "valid_mask": stat_values.setdefault(key, []).append(finite.astype(np.float32))
    stats = {}
    for key, values in stat_values.items():
        value = np.concatenate(values)
        stats[key] = {"mean": float(np.mean(value)), "std": float(np.std(value) or 1.0), "median": float(np.median(value)), "p02": float(np.percentile(value, 2)), "p98": float(np.percentile(value, 98)), "n_train_pixels": int(value.size)}
    stats_path = DATA / "prepared" / "normalization_stats.json"; write_json(stats_path, stats)
    patch_index=[]; size=int(cnn["patch_size"]); minimum=float(cnn["minimum_valid_earth_fraction"])
    for path in prepared:
        with np.load(path, allow_pickle=False) as z:
            if str(z["split"]) != "train": continue
            valid=z["valid_mask"].astype(bool); h,w=valid.shape
            for r in range(0, h-size+1, size):
                for c in range(0, w-size+1, size):
                    fraction=float(np.mean(valid[r:r+size,c:c+size]))
                    if fraction >= minimum: patch_index.append({"scene_file":str(path),"row":r,"col":c,"size":size,"valid_earth_fraction":fraction})
    from common import write_csv
    patch_path=DATA / "patches" / "train_patch_index.csv"; write_csv(patch_path,patch_index)
    write_manifest("07_prepare_training_samples", Path(__file__), [manifest], prepared + [stats_path,patch_path], {"patch_size":size,"minimum_valid_earth_fraction":minimum,"normalization_scope":"train dates only","patch_count":len(patch_index)})
    print(f"Prepared {len(prepared)} scenes and {len(patch_index)} train patches")


if __name__ == "__main__": sys.exit(main())
