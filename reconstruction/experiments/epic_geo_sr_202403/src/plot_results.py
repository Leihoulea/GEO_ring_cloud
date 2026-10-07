"""Render requested comparison figures once predictions and metric tables exist."""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from common import DATA, FIGURES, PREDICTIONS, configured_inputs, ensure_layout, resolve_scene_rows, split_for_date, write_manifest


def main() -> None:
    ensure_layout();cfg,_,_,manifest=configured_inputs(); scenes=[r for r in resolve_scene_rows() if split_for_date(r["scene_date"],cfg)=="test"]
    if not scenes:raise RuntimeError("No frozen test scene.")
    row=scenes[0];base=DATA/"prepared"/f"{row['sample_id']}.npz";methods=["deterministic","cnn_c","cnn_g","cnn_cg","cnn_cg_closure"];arrays=[]
    with np.load(base,allow_pickle=False) as z: target=z["target"];valid=z["valid_mask"].astype(bool)
    for method in methods:
        path=PREDICTIONS/method/f"{row['sample_id']}.npz" if method=="deterministic" else PREDICTIONS/method/"seed_42"/f"{row['sample_id']}.npz"
        if path.exists(): arrays.append((method,np.load(path,allow_pickle=False)["prediction"]))
    if not arrays:raise RuntimeError("No prediction exists; run deterministic baseline or CNN inference first.")
    fig,axes=plt.subplots(1,len(arrays)+1,figsize=(4*(len(arrays)+1),4));axes[0].imshow(np.where(valid,target,np.nan),vmin=0,vmax=1,cmap="viridis");axes[0].set_title("native EPIC target");axes[0].axis("off")
    for ax,(name,pred) in zip(axes[1:],arrays):ax.imshow(np.where(valid,pred,np.nan),vmin=0,vmax=1,cmap="viridis");ax.set_title(name);ax.axis("off")
    fig.tight_layout();out=FIGURES/"model_comparison"/f"{row['sample_id']}_comparison.png";out.parent.mkdir(parents=True,exist_ok=True);fig.savefig(out,dpi=180);plt.close(fig)
    write_manifest("plot_results",Path(__file__),[manifest],[out],{"scene_id":row["sample_id"],"methods":[name for name,_ in arrays]});print(out)


if __name__=="__main__":sys.exit(main())
