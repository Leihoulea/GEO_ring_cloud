"""Observation-consistent GEO structure + coarse EPIC anchor baseline."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from common import DATA, PREDICTIONS, REPORTS, configured_inputs, ensure_layout, resolve_scene_rows, split_for_date, utc_now, write_csv, write_manifest


def cell_mean(values: np.ndarray, valid: np.ndarray, cell: np.ndarray, n: int) -> np.ndarray:
    use = valid & np.isfinite(values) & (cell >= 0)
    count = np.bincount(cell[use], minlength=n); total=np.bincount(cell[use], weights=values[use], minlength=n)
    out=np.full(n,np.nan,dtype=np.float32); good=count>0; out[good]=total[good]/count[good]; return out


def main() -> None:
    ensure_layout(); cfg, _, _, manifest = configured_inputs(); rows=resolve_scene_rows(); outdir=PREDICTIONS/"deterministic"; outdir.mkdir(parents=True,exist_ok=True); summary=[]; outputs=[]
    for row in rows:
        with np.load(DATA/"prepared"/f"{row['sample_id']}.npz",allow_pickle=False) as prepared, np.load(DATA/"coarse50"/f"{row['sample_id']}_coarse50.npz",allow_pickle=False) as coarse:
            valid=prepared["valid_mask"].astype(bool) & np.isfinite(prepared["geo_cloud_probability"])
            cell=coarse["cell_id"]; target_coarse=coarse["cloud_fraction_by_cell"]; p=np.where(np.isfinite(prepared["geo_cloud_probability"]),prepared["geo_cloud_probability"],0.5).astype(np.float32)
            for _ in range(3):
                current=cell_mean(p,valid,cell,len(target_coarse)); correction=target_coarse-current; correction_up=np.zeros(p.shape,dtype=np.float32); okay=cell>=0; correction_up[okay]=np.nan_to_num(correction[cell[okay]],nan=0.0); p=np.clip(p+correction_up,0,1)
            closure=cell_mean(p,valid,cell,len(target_coarse))-target_coarse
            output=outdir/f"{row['sample_id']}.npz"; np.savez_compressed(output,prediction=p,valid_mask=valid.astype(np.uint8),cell_id=cell,closure_by_cell=closure); outputs.append(output)
            close=closure[np.isfinite(closure)]; summary.append({"scene_id":row["sample_id"],"split":split_for_date(row["scene_date"],cfg),"closure_mae":float(np.mean(np.abs(close))),"closure_rmse":float(np.sqrt(np.mean(close**2))),"prediction_mean":float(np.mean(p[valid]))})
    table=outdir/"deterministic_summary.csv";write_csv(table,summary);outputs.append(table)
    report=REPORTS/"06_deterministic_baseline.md";report.write_text("\n".join(["# 06 确定性基线","",f"生成时间：`{utc_now()}`","","- 初始预测为 GEO cloud probability。","- 每场景执行 3 次 `P ← clip(P + U(CF_EPIC50 − D(P)))`，其中 D 和 U 均为冻结的同一 metric-cell operator。","- 该基线不训练任何参数；GEO 提供空间纹理，coarse EPIC 只提供观测一致性的 coarse anchor。","" ]),encoding="utf-8");outputs.append(report)
    write_manifest("06_deterministic_baseline",Path(__file__),[manifest],outputs,{"iterations":3,"method":"GEO plus coarse-EPIC iterative correction"});print(f"Wrote deterministic predictions for {len(rows)} scenes")


if __name__=="__main__":sys.exit(main())
