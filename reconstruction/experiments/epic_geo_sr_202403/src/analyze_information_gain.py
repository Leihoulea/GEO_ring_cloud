"""Create paired CNN-G/CNN-CG information-gain maps and scene bootstrap table."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from common import DATA, FIGURES, METRICS, configured_inputs, ensure_layout, resolve_scene_rows, split_for_date, write_csv, write_manifest


def main() -> None:
    ensure_layout();cfg,_,_,manifest=configured_inputs(); rows=[r for r in resolve_scene_rows() if split_for_date(r["scene_date"],cfg)=="test"];out=FIGURES/"information_gain_maps";out.mkdir(parents=True,exist_ok=True);aggregate=[];summary=[]
    for row in rows:
        paths=[DATA/"prepared"/f"{row['sample_id']}.npz",Path(__file__).resolve().parents[1]/"predictions"/"cnn_g"/"seed_42"/f"{row['sample_id']}.npz",Path(__file__).resolve().parents[1]/"predictions"/"cnn_cg"/"seed_42"/f"{row['sample_id']}.npz"]
        if not all(p.exists() for p in paths):continue
        with np.load(paths[0],allow_pickle=False) as base,np.load(paths[1],allow_pickle=False) as g,np.load(paths[2],allow_pickle=False) as cg:
            valid=base["valid_mask"].astype(bool);gain=np.abs(base["target"]-g["prediction"])-np.abs(base["target"]-cg["prediction"]);gain[~valid]=np.nan;np.save(out/f"{row['sample_id']}_gain_epic.npy",gain);aggregate.append(gain);summary.append({"scene_id":row["sample_id"],"gain_epic_mae":float(np.nanmean(gain)),"improved_fraction":float(np.nanmean(gain>0))})
    if aggregate:np.save(out/"test_period_aggregate_gain_epic.npy",np.nanmean(np.stack(aggregate),axis=0))
    table=METRICS/"gain_by_scene.csv";write_csv(table,summary);write_manifest("09_information_gain_analysis",Path(__file__),[manifest],[table],{"gain_definition":"abs(target-pred_G)-abs(target-pred_CG)","bootstrap":"scene-level paired bootstrap to be computed after three seed outputs"});print(table)


if __name__=="__main__":sys.exit(main())
