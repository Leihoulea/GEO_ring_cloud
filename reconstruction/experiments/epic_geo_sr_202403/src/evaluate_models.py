"""Compute complete-scene probability, classification, structure and closure metrics."""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np
from scipy import ndimage
from skimage.metrics import structural_similarity

from common import DATA, METRICS, PREDICTIONS, configured_inputs, ensure_layout, resolve_scene_rows, split_for_date, write_csv, write_manifest


SSIM_WINDOW_SIZE = 11


def masked_ssim(pred: np.ndarray, target: np.ndarray, valid: np.ndarray, window_size: int = SSIM_WINDOW_SIZE) -> tuple[float, int, float]:
    """Return disk-aware SSIM using only local windows wholly inside ``valid``.

    ``structural_similarity`` has no mask argument.  We therefore use harmless
    common fill values only to make the call finite, then retain its local SSIM
    values solely at centres whose full odd-sized window lies in the valid
    cloud-mask domain.  This prevents the circular EPIC disk boundary from
    affecting the reported spatial score.
    """
    if window_size < 3 or window_size % 2 == 0:
        raise ValueError("SSIM window size must be odd and at least 3")
    good = valid & np.isfinite(pred) & np.isfinite(target)
    if not np.any(good):
        return np.nan, 0, np.nan
    yy, xx = np.where(good)
    r0, r1 = yy.min(), yy.max() + 1
    c0, c1 = xx.min(), xx.max() + 1
    local_good = good[r0:r1, c0:c1]
    # The fill never affects retained centres: they are eroded by the complete
    # SSIM support window below.
    local_pred = np.where(local_good, pred[r0:r1, c0:c1], 0.0)
    local_target = np.where(local_good, target[r0:r1, c0:c1], 0.0)
    _, ssim_map = structural_similarity(
        local_pred, local_target, data_range=1.0, win_size=window_size, full=True
    )
    support = np.ones((window_size, window_size), dtype=bool)
    centres = ndimage.binary_erosion(local_good, structure=support, border_value=0)
    n_centres = int(np.sum(centres))
    fraction = float(n_centres / np.sum(local_good))
    return (float(np.mean(ssim_map[centres])) if n_centres else np.nan, n_centres, fraction)


def metrics(pred: np.ndarray,target: np.ndarray,valid: np.ndarray,coarse: np.ndarray,cell: np.ndarray) -> dict[str,float]:
    p,t=pred[valid],target[valid]; eps=1e-6; hard=p>=0.5; truth=t>=0.5; tp=np.sum(hard&truth);tn=np.sum(~hard&~truth);fp=np.sum(hard&~truth);fn=np.sum(~hard&truth)
    # Spatial residuals remain native-grid arrays; p/t above are flattened only
    # for probability and classification metrics.
    rtrue=target-coarse; rpred=pred-coarse; rv=valid&np.isfinite(rtrue)&np.isfinite(rpred); grad=lambda a:np.hypot(ndimage.sobel(a,0),ndimage.sobel(a,1)); gr=grad(rtrue);gp=grad(rpred); gv=rv&np.isfinite(gr)&np.isfinite(gp)
    count=np.bincount(cell[valid],minlength=int(cell.max())+1);summ=np.bincount(cell[valid],weights=pred[valid],minlength=len(count));cp=np.full(len(count),np.nan);cp[count>0]=summ[count>0]/count[count>0]; ct=np.bincount(cell[valid],weights=target[valid],minlength=len(count));ct[count>0]=ct[count>0]/count[count>0]; close=cp-ct
    corr=lambda a,b: float(np.corrcoef(a,b)[0,1]) if len(a)>2 and np.std(a)>0 and np.std(b)>0 else np.nan
    pred_edge=(ndimage.binary_dilation(pred>=0.5)^ndimage.binary_erosion(pred>=0.5)) & valid
    true_edge=(ndimage.binary_dilation(target>=0.5)^ndimage.binary_erosion(target>=0.5)) & valid
    edge_hit=np.sum(pred_edge & ndimage.binary_dilation(true_edge)); edge_precision=edge_hit/np.sum(pred_edge) if np.any(pred_edge) else np.nan
    edge_recall=np.sum(true_edge & ndimage.binary_dilation(pred_edge))/np.sum(true_edge) if np.any(true_edge) else np.nan
    boundary_f1=2*edge_precision*edge_recall/(edge_precision+edge_recall) if np.isfinite(edge_precision) and np.isfinite(edge_recall) and edge_precision+edge_recall else np.nan
    ssim,ssim_n,ssim_fraction=masked_ssim(pred,target,valid)
    return {"n":int(len(p)),"Brier":float(np.mean((p-t)**2)),"BCE":float(-np.mean(t*np.log(np.clip(p,eps,1-eps))+(1-t)*np.log(np.clip(1-p,eps,1-eps)))),"Accuracy":float((tp+tn)/len(p)),"Precision":float(tp/(tp+fp)) if tp+fp else np.nan,"Recall":float(tp/(tp+fn)) if tp+fn else np.nan,"F1":float(2*tp/(2*tp+fp+fn)) if 2*tp+fp+fn else np.nan,"CSI":float(tp/(tp+fp+fn)) if tp+fp+fn else np.nan,"POD":float(tp/(tp+fn)) if tp+fn else np.nan,"FAR":float(fp/(tp+fp)) if tp+fp else np.nan,"SSIM":ssim,"SSIM_n_valid_centres":ssim_n,"SSIM_valid_centre_fraction":ssim_fraction,"boundary_F1":float(boundary_f1),"highpass_RMSE":float(np.sqrt(np.mean((rpred[rv]-rtrue[rv])**2))),"highpass_corr":corr(rpred[rv],rtrue[rv]),"gradient_corr":corr(gp[gv],gr[gv]),"closure_MAE":float(np.nanmean(np.abs(close))),"closure_RMSE":float(np.sqrt(np.nanmean(close**2))),"closure_bias":float(np.nanmean(close))}


def main() -> None:
    ensure_layout();cfg,_,_,manifest=configured_inputs(); cnn=__import__("yaml").safe_load((Path(__file__).resolve().parents[1]/"config"/"cnn.yaml").read_text()); rows=resolve_scene_rows(); methods=["deterministic","cnn_c","cnn_g","cnn_cg","cnn_cg_closure"];all_rows=[]
    for method in methods:
        seeds=["deterministic"] if method=="deterministic" else [str(s) for s in cnn["seeds"]]
        for seed in seeds:
            for row in rows:
                if split_for_date(row["scene_date"],cfg)!="test":continue
                path=PREDICTIONS/method/f"{row['sample_id']}.npz" if method=="deterministic" else PREDICTIONS/method/f"seed_{seed}"/f"{row['sample_id']}.npz"
                if not path.exists():continue
                with np.load(path,allow_pickle=False) as z:
                    pred=z["prediction"];target=z["target"] if "target" in z.files else np.load(DATA/"prepared"/f"{row['sample_id']}.npz",allow_pickle=False)["target"];valid=z["valid_mask"].astype(bool);coarse=z["coarse_up"] if "coarse_up" in z.files else np.load(DATA/"coarse50"/f"{row['sample_id']}_coarse50.npz",allow_pickle=False)["coarse_cloud_fraction_up"];cell=z["cell_id"]
                all_rows.append({"model":method,"seed":seed,"scene_id":row["sample_id"],**metrics(pred,target,valid,coarse,cell)})
    scene=METRICS/"scene_metrics.csv";write_csv(scene,all_rows)
    summary=[]
    for method in sorted({r["model"] for r in all_rows}):
        group=[r for r in all_rows if r["model"]==method]; per_seed=[]
        for seed in sorted({r["seed"] for r in group}):
            seed_rows=[r for r in group if r["seed"]==seed]
            per_seed.append({key:float(np.nanmean([r[key] for r in seed_rows])) for key in seed_rows[0] if key not in {"model","seed","scene_id","n"}})
        values={key:float(np.nanmean([x[key] for x in per_seed])) for key in per_seed[0]}
        values.update({f"{key}_seed_std":float(np.nanstd([x[key] for x in per_seed],ddof=0)) for key in per_seed[0]})
        summary.append({"model":method,"input_groups":{"cnn_c":"C+Z","cnn_g":"G+Z","cnn_cg":"C+G+Z","cnn_cg_closure":"C+G+Z","deterministic":"G+coarse anchor"}[method],"seed_count":len(per_seed),**values})
    table=METRICS/"model_comparison.csv";write_csv(table,summary);write_manifest("08_cnn_ablation_results",Path(__file__),[manifest],[scene,table],{"evaluation":"complete test scenes","threshold":0.5,"ssim":{"type":"masked local SSIM","window_size_native_pixels":SSIM_WINDOW_SIZE,"aggregation":"mean over centres with complete valid support window"}});print(table)


if __name__=="__main__":sys.exit(main())
