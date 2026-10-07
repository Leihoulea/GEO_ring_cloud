"""Run saved CNN checkpoints on complete frozen scenes, never test patches only."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from common import DATA, PREDICTIONS, ROOT, configured_inputs, resolve_scene_rows, split_for_date
from cnn_utils import load_scene, predict_tiled, require_torch
from common import load_config


def main() -> None:
    ap=argparse.ArgumentParser();ap.add_argument("model",choices=["cnn_c","cnn_g","cnn_cg","cnn_cg_closure"]);ap.add_argument("--seed",type=int,default=42);args=ap.parse_args()
    torch=require_torch();from cnn_model import build_cnn
    checkpoint=torch.load(ROOT/"checkpoints"/args.model/f"seed_{args.seed}.pt",map_location="cpu",weights_only=True);names=checkpoint["input_channels"];model=build_cnn(len(names));model.load_state_dict(checkpoint["state_dict"]);model.eval();stats=json.loads((DATA/"prepared"/"normalization_stats.json").read_text());cfg,_,_,_=configured_inputs();cnn=load_config("cnn.yaml");out=PREDICTIONS/args.model/f"seed_{args.seed}";out.mkdir(parents=True,exist_ok=True)
    for row in resolve_scene_rows():
        if split_for_date(row["scene_date"],cfg) not in {"validation","test"}:continue
        x,target,valid,coarse,cell,_=load_scene(DATA/"prepared"/f"{row['sample_id']}.npz",names,stats)
        pred=predict_tiled(model,x,"cpu",int(cnn["validation_tile_size"]),int(cnn["validation_halo_pixels"]))
        np.savez_compressed(out/f"{row['sample_id']}.npz",prediction=pred,target=target,valid_mask=valid.astype(np.uint8),coarse_up=coarse,cell_id=cell)
    print(out)


if __name__=="__main__":main()
