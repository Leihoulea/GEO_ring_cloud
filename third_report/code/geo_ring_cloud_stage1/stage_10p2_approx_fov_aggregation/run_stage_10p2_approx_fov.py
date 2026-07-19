from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

for _env_name in ["OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"]:
    os.environ.setdefault(_env_name, "1")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import ndimage

CODE_ROOT = Path(__file__).resolve().parents[1]
STAGE10_CODE = CODE_ROOT / "stage_10_cth_validation"
for _p in [CODE_ROOT, STAGE10_CODE]:
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from geo_ring_cloud.paths import RUNS_ROOT  # noqa: E402
from geo_ring_cloud.diagnostics import cth_validation as s10  # noqa: E402


STAGE_ID = "stage_10p2"
PROJECT_STAGE_ID = "geo_ring_cloud.stage_10p2"
DEFAULT_STAGE09D = RUNS_ROOT / "stage09d_full_pixel_diagnostics_202403"
DEFAULT_STAGE10 = RUNS_ROOT / "stage_10_cth_fused_product_validation_202403"
DEFAULT_OUT = RUNS_ROOT / "stage_10p2_approx_epic_fov_aggregation_202403"
EPIC_CTH = "geophysical_data/A-band_Effective_Cloud_Height"
SOURCE_FOCUS = {
    "selected_MeteosatIODC": "Meteosat-IODC",
    "selected_Meteosat0deg": "Meteosat-0deg",
    "selected_GOES16": "GOES-16",
    "selected_GOES18": "GOES-18",
    "selected_FY4B": "FY4B",
    "selected_Himawari9": "Himawari-9",
}


@dataclass
class CloudAccum:
    n: int = 0
    tp: int = 0
    tn: int = 0
    fp: int = 0
    fn: int = 0
    geo_cloud_sum: float = 0.0
    epic_cloud_sum: float = 0.0
    frac_sum: float = 0.0
    frac_abs_sum: float = 0.0
    frac_sq_sum: float = 0.0

    def add(self, epic_cloud: np.ndarray, geo_cloud: np.ndarray, geo_fraction: np.ndarray, mask: np.ndarray) -> None:
        valid = mask & np.isfinite(geo_fraction)
        n = int(np.count_nonzero(valid))
        if n == 0:
            return
        e = epic_cloud[valid].astype(bool)
        g = geo_cloud[valid].astype(bool)
        f = geo_fraction[valid].astype(np.float64)
        ef = e.astype(np.float64)
        self.n += n
        self.tp += int(np.count_nonzero(e & g))
        self.tn += int(np.count_nonzero(~e & ~g))
        self.fp += int(np.count_nonzero(~e & g))
        self.fn += int(np.count_nonzero(e & ~g))
        self.geo_cloud_sum += float(np.sum(g))
        self.epic_cloud_sum += float(np.sum(e))
        diff = f - ef
        self.frac_sum += float(np.sum(diff))
        self.frac_abs_sum += float(np.sum(np.abs(diff)))
        self.frac_sq_sum += float(np.sum(diff * diff))

    def row(self) -> dict[str, Any]:
        if self.n == 0:
            return {"n_valid": 0}
        precision = self.tp / (self.tp + self.fp) if (self.tp + self.fp) else math.nan
        recall = self.tp / (self.tp + self.fn) if (self.tp + self.fn) else math.nan
        f1 = 2 * precision * recall / (precision + recall) if math.isfinite(precision) and math.isfinite(recall) and (precision + recall) else math.nan
        iou = self.tp / (self.tp + self.fp + self.fn) if (self.tp + self.fp + self.fn) else math.nan
        tpr = recall
        tnr = self.tn / (self.tn + self.fp) if (self.tn + self.fp) else math.nan
        bal = (tpr + tnr) / 2 if math.isfinite(tpr) and math.isfinite(tnr) else math.nan
        return {
            "n_valid": self.n,
            "agreement": (self.tp + self.tn) / self.n,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "iou": iou,
            "balanced_accuracy": bal,
            "epic_cloud_fraction": self.epic_cloud_sum / self.n,
            "geo_cloud_fraction": self.geo_cloud_sum / self.n,
            "cloud_fraction_bias": self.geo_cloud_sum / self.n - self.epic_cloud_sum / self.n,
            "continuous_fraction_bias": self.frac_sum / self.n,
            "continuous_fraction_mae": self.frac_abs_sum / self.n,
            "continuous_fraction_rmse": math.sqrt(self.frac_sq_sum / self.n),
            "TP": self.tp,
            "TN": self.tn,
            "FP": self.fp,
            "FN": self.fn,
        }


@dataclass
class CthAccum:
    n: int = 0
    sum_epic: float = 0.0
    sum_geo: float = 0.0
    sum_diff: float = 0.0
    sum_abs: float = 0.0
    sum_sq: float = 0.0
    within_1: int = 0
    within_2: int = 0
    within_3: int = 0
    class_agree: int = 0
    valid_weight_sum: float = 0.0
    abs_errors: list[np.ndarray] | None = None

    def add(self, epic: np.ndarray, geo: np.ndarray, valid_weight: np.ndarray, mask: np.ndarray, keep_abs: bool = False) -> None:
        valid = mask & np.isfinite(epic) & np.isfinite(geo) & np.isfinite(valid_weight)
        n = int(np.count_nonzero(valid))
        if n == 0:
            return
        e = epic[valid].astype(np.float64)
        g = geo[valid].astype(np.float64)
        w = valid_weight[valid].astype(np.float64)
        d = g - e
        ad = np.abs(d)
        self.n += n
        self.sum_epic += float(np.sum(e))
        self.sum_geo += float(np.sum(g))
        self.sum_diff += float(np.sum(d))
        self.sum_abs += float(np.sum(ad))
        self.sum_sq += float(np.sum(d * d))
        self.within_1 += int(np.count_nonzero(ad <= 1))
        self.within_2 += int(np.count_nonzero(ad <= 2))
        self.within_3 += int(np.count_nonzero(ad <= 3))
        self.class_agree += int(np.count_nonzero(s10.cth_class(e) == s10.cth_class(g)))
        self.valid_weight_sum += float(np.sum(w))
        if keep_abs:
            if self.abs_errors is None:
                self.abs_errors = []
            self.abs_errors.append(ad.astype(np.float32))

    def row(self) -> dict[str, Any]:
        if self.n == 0:
            return {"n_valid_cth": 0}
        if self.abs_errors:
            all_abs = np.concatenate(self.abs_errors)
            med = float(np.median(all_abs))
            p90 = float(np.percentile(all_abs, 90))
        else:
            med = math.nan
            p90 = math.nan
        return {
            "n_valid_cth": self.n,
            "epic_cth_mean_km": self.sum_epic / self.n,
            "geo_cth_mean_km": self.sum_geo / self.n,
            "bias_km": self.sum_diff / self.n,
            "mae_km": self.sum_abs / self.n,
            "rmse_km": math.sqrt(self.sum_sq / self.n),
            "median_abs_error_km": med,
            "p90_abs_error_km": p90,
            "within_1km_fraction": self.within_1 / self.n,
            "within_2km_fraction": self.within_2 / self.n,
            "within_3km_fraction": self.within_3 / self.n,
            "low_mid_high_class_agreement": self.class_agree / self.n,
            "valid_cth_weight_fraction_mean": self.valid_weight_sum / self.n,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        keys: set[str] = set()
        for row in rows:
            keys.update(row.keys())
        fields = sorted(keys)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})


def ensure_dirs(out: Path) -> None:
    for name in ["05_figures", "reports", "logs"]:
        (out / name).mkdir(parents=True, exist_ok=True)


def method_definitions() -> list[dict[str, Any]]:
    return [
        {"method_name": "nearest", "kernel_type": "nearest", "window_size": 1, "radius_cell": 0, "sigma_cell": "", "is_official_epic_psf": False, "notes": "Nearest-neighbor baseline."},
        {"method_name": "box_3x3", "kernel_type": "box", "window_size": 3, "radius_cell": 1, "sigma_cell": "", "is_official_epic_psf": False, "notes": "Approximate EPIC-FOV local box aggregation."},
        {"method_name": "box_5x5", "kernel_type": "box", "window_size": 5, "radius_cell": 2, "sigma_cell": "", "is_official_epic_psf": False, "notes": "Approximate EPIC-FOV local box aggregation."},
        {"method_name": "box_7x7", "kernel_type": "box", "window_size": 7, "radius_cell": 3, "sigma_cell": "", "is_official_epic_psf": False, "notes": "Approximate EPIC-FOV local box aggregation."},
        {"method_name": "gaussian_5x5_sigma1p0", "kernel_type": "gaussian", "window_size": 5, "radius_cell": 2, "sigma_cell": 1.0, "is_official_epic_psf": False, "notes": "Gaussian-like local weighting; not official EPIC PSF."},
        {"method_name": "gaussian_7x7_sigma1p5", "kernel_type": "gaussian", "window_size": 7, "radius_cell": 3, "sigma_cell": 1.5, "is_official_epic_psf": False, "notes": "Gaussian-like local weighting; not official EPIC PSF."},
        {"method_name": "gaussian_9x9_sigma2p0", "kernel_type": "gaussian", "window_size": 9, "radius_cell": 4, "sigma_cell": 2.0, "is_official_epic_psf": False, "notes": "Optional wider Gaussian-like sensitivity; not official EPIC PSF."},
    ]


def make_kernel(mdef: dict[str, Any]) -> np.ndarray | None:
    if mdef["kernel_type"] == "nearest":
        return None
    radius = int(mdef["radius_cell"])
    size = 2 * radius + 1
    if mdef["kernel_type"] == "box":
        k = np.ones((size, size), dtype=np.float32)
    else:
        sigma = float(mdef["sigma_cell"])
        y, x = np.mgrid[-radius : radius + 1, -radius : radius + 1]
        k = np.exp(-0.5 * (x * x + y * y) / (sigma * sigma)).astype(np.float32)
    return k / np.sum(k)


def convolve_weighted(values: np.ndarray, valid: np.ndarray, kernel: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    v = valid.astype(np.float32)
    num = ndimage.convolve(np.where(valid, values, 0).astype(np.float32), kernel, mode="constant", cval=0.0)
    den = ndimage.convolve(v, kernel, mode="constant", cval=0.0)
    out = np.full(values.shape, np.nan, dtype=np.float32)
    ok = den > 0
    out[ok] = num[ok] / den[ok]
    return out, den


def convolve_median(values: np.ndarray, valid: np.ndarray, size: int) -> tuple[np.ndarray, np.ndarray]:
    filled = np.where(valid, values, np.nan).astype(np.float32)
    med = ndimage.median_filter(filled, size=size, mode="constant", cval=np.nan)
    count = ndimage.uniform_filter(valid.astype(np.float32), size=size, mode="constant", cval=0.0) * (size * size)
    return med.astype(np.float32), count / float(size * size)


def source_name_array(source_map: np.ndarray) -> np.ndarray:
    return s10.source_name_array(np.where(np.isfinite(source_map), source_map, 0).astype(np.int16))


def safe_bins(values: np.ndarray, edges: list[tuple[str, float, float]]) -> np.ndarray:
    return s10.bin_numeric(values.astype(np.float32), edges)


def add_group_masks(
    groups: dict[tuple[str, str], np.ndarray],
    base: np.ndarray,
    sample: pd.Series,
    epic: dict[str, Any],
    src_names: np.ndarray,
    vc_on: np.ndarray,
    epic_cth: np.ndarray,
    fused_cth_nearest: np.ndarray,
    d1_nearest: np.ndarray,
) -> None:
    groups[("domain", "ALL_VALID")] = base
    local = s10.local_fraction(d1_nearest, 2)
    groups[("scene", "clean_core")] = base & d1_nearest & (local >= 0.95) & (np.abs(epic["lat"]) < 60) & (epic["epic_vza"] < 60) & (epic["sza"] < 70)
    groups[("scene", "boundary_or_broken_cloud")] = base & d1_nearest & (local > 0.05) & (local < 0.95)
    groups[("height", "fused_high_cloud")] = base & (fused_cth_nearest >= 7)
    for cls_name, cls_arr in [("EPIC", s10.cth_class(epic_cth)), ("fused", s10.cth_class(fused_cth_nearest))]:
        for val in ["low_cloud", "mid_cloud", "high_cloud"]:
            groups[(f"{cls_name}_height_class", f"{cls_name}_{val}")] = base & (cls_arr == val)
    for label, source in SOURCE_FOCUS.items():
        groups[("selected_source_focus", label)] = base & (src_names == source)
    for source in sorted([x for x in pd.unique(src_names[base].ravel()) if str(x) != "missing"]):
        groups[("selected_source", str(source))] = base & (src_names == source)
    for count in [1, 2, 3]:
        groups[("valid_source_count", f"valid_source_count_{count}")] = base & (vc_on == count)
    groups[("candidate_group", str(sample.get("candidate_group", "")))] = base
    groups[("dominant_source", str(sample.get("dominant_source", "")))] = base
    abs_lat = safe_bins(np.abs(epic["lat"]), [("abs_lat_0_30", 0, 30), ("abs_lat_30_60", 30, 60), ("abs_lat_ge60", 60, 91)])
    vza = safe_bins(epic["epic_vza"], [("EPIC_VZA_0_30", 0, 30), ("EPIC_VZA_30_60", 30, 60), ("EPIC_VZA_ge60", 60, 181)])
    sza = safe_bins(epic["sza"], [("SZA_0_50", 0, 50), ("SZA_50_70", 50, 70), ("SZA_ge70", 70, 181)])
    for arr_name, arr in [("abs_lat_bin", abs_lat), ("EPIC_VZA_bin", vza), ("SZA_bin", sza)]:
        for val in sorted([x for x in pd.unique(arr[base].ravel()) if str(x) != "missing"]):
            groups[(arr_name, str(val))] = base & (arr == val)


def process_sample(sample: pd.Series, mdefs: list[dict[str, Any]], out: Path) -> dict[str, Any]:
    run_dir = Path(str(sample["stage_run_dir"]))
    epic_path = Path(str(sample["epic_file"]))
    grid = s10.load_grid(run_dir)
    epic = s10.read_epic(epic_path, EPIC_CTH)
    fused_dir = run_dir / "fused_best_source"
    fused_cm, fused_cm_valid, _ = s10.load_npz_array(fused_dir / "fused_cloud_mask.npz")
    fused_cth, fused_cth_valid, _ = s10.load_npz_array(fused_dir / "fused_cloud_top_height_km.npz")
    source_map, source_valid, _ = s10.load_npz_array(fused_dir / "source_map_cloud_top_height_km.npz")
    valid_count, valid_count_valid, _ = s10.load_npz_array(fused_dir / "valid_count_map_cloud_top_height_km.npz")

    fused_cm_on, fused_cm_on_valid = s10.sample_grid(fused_cm, fused_cm_valid, epic["lat"], epic["lon"], grid)
    fused_cth_on, fused_cth_on_valid = s10.sample_grid(fused_cth, fused_cth_valid & (fused_cth >= 0) & (fused_cth <= 25), epic["lat"], epic["lon"], grid)
    src_on, _ = s10.sample_grid(source_map, source_valid, epic["lat"], epic["lon"], grid)
    vc_on, _ = s10.sample_grid(valid_count, valid_count_valid, epic["lat"], epic["lon"], grid)
    src_names = source_name_array(src_on)

    nearest_policy: dict[str, dict[str, np.ndarray]] = {}
    for policy_name, policy in s10.POLICIES.items():
        ecls, ev = s10.apply_policy(epic["cloud_mask"], policy["epic"])
        gcls, gv = s10.apply_policy(fused_cm_on, policy["geo"])
        nearest_policy[policy_name] = {"ecls": ecls, "ev": ev, "gcls": gcls, "gv": gv}

    d1_nearest = (
        epic["cth_valid"]
        & fused_cth_on_valid
        & np.isfinite(fused_cth_on)
        & nearest_policy["A_inclusive_binary"]["ev"]
        & nearest_policy["A_inclusive_binary"]["gv"]
        & (nearest_policy["A_inclusive_binary"]["ecls"] == 1)
        & (nearest_policy["A_inclusive_binary"]["gcls"] == 1)
    )

    cloud_rows: list[dict[str, Any]] = []
    cth_rows: list[dict[str, Any]] = []
    valid_rows: list[dict[str, Any]] = []
    case_rows: list[dict[str, Any]] = []
    warning_rows: list[dict[str, Any]] = []

    cth_valid_grid = fused_cth_valid & np.isfinite(fused_cth) & (fused_cth >= 0) & (fused_cth <= 25)
    method_cache: dict[str, dict[str, Any]] = {}

    for mdef in mdefs:
        method = mdef["method_name"]
        kernel = make_kernel(mdef)
        if method == "nearest":
            cth_mean_on = fused_cth_on
            cth_median_on = fused_cth_on
            cth_weight_on = fused_cth_on_valid.astype(np.float32)
        else:
            cth_mean_grid, cth_den_grid = convolve_weighted(fused_cth, cth_valid_grid, kernel)
            cth_mean_on, _ = s10.sample_grid(cth_mean_grid, np.isfinite(cth_mean_grid), epic["lat"], epic["lon"], grid)
            cth_weight_on, _ = s10.sample_grid(cth_den_grid, np.isfinite(cth_den_grid), epic["lat"], epic["lon"], grid)
            if mdef["kernel_type"] == "box":
                med_grid, med_frac_grid = convolve_median(fused_cth, cth_valid_grid, int(mdef["window_size"]))
                cth_median_on, _ = s10.sample_grid(med_grid, np.isfinite(med_grid), epic["lat"], epic["lon"], grid)
            else:
                cth_median_on = cth_mean_on
        method_cache[method] = {"cth_mean": cth_mean_on, "cth_median": cth_median_on, "cth_weight": cth_weight_on}

        for policy_name, policy in s10.POLICIES.items():
            ecls = nearest_policy[policy_name]["ecls"]
            ev = nearest_policy[policy_name]["ev"]
            epic_cloud = ecls == 1
            geo_grid_cls, geo_grid_valid_policy = s10.apply_policy(fused_cm, policy["geo"])
            grid_valid = fused_cm_valid & geo_grid_valid_policy
            if method == "nearest":
                frac_on = np.full(epic_cloud.shape, np.nan, dtype=np.float32)
                gcls = nearest_policy[policy_name]["gcls"]
                gv = nearest_policy[policy_name]["gv"] & fused_cm_on_valid
                frac_on[gv] = (gcls[gv] == 1).astype(np.float32)
                geo_cloud = gcls == 1
                cloud_valid = ev & gv
            else:
                cloud_grid = geo_grid_cls == 1
                frac_grid, den_grid = convolve_weighted(cloud_grid.astype(np.float32), grid_valid, kernel)
                frac_on, frac_on_valid = s10.sample_grid(frac_grid, np.isfinite(frac_grid), epic["lat"], epic["lon"], grid)
                geo_cloud = frac_on >= 0.5
                cloud_valid = ev & frac_on_valid & np.isfinite(frac_on)
            row_base = {"sample_id": sample["sample_id"], "policy": policy_name, "method_name": method, "threshold": 0.5}
            cloud_base = cloud_valid
            acc = CloudAccum()
            acc.add(epic_cloud, geo_cloud, frac_on, cloud_base)
            cloud_rows.append({**row_base, "group_dimension": "domain", "group_value": "ALL_VALID", **acc.row()})

            cth_common = epic["cth_valid"] & np.isfinite(cth_mean_on) & np.isfinite(cth_weight_on)
            d_both = cth_common & cloud_valid & epic_cloud & geo_cloud
            domain_masks = {
                "ALL_VALID": cth_common & (cth_weight_on >= 0.5),
                "D1_both_cloud_Policy_A": d_both & (cth_weight_on >= 0.5) if policy_name == "A_inclusive_binary" else np.zeros(epic_cloud.shape, bool),
                "D2_high_confidence_Policy_B": d_both & (cth_weight_on >= 0.5) if policy_name == "B_high_confidence_only" else np.zeros(epic_cloud.shape, bool),
            }
            for min_w in [0.25, 0.5, 0.75]:
                valid_rows.append(
                    {
                        **row_base,
                        "cth_estimator": "weighted_mean",
                        "min_valid_cth_weight_fraction": min_w,
                        "n_epic_cth_valid": int(np.count_nonzero(cth_common & (cth_weight_on >= min_w))),
                        "mean_valid_cth_weight_fraction": float(np.nanmean(cth_weight_on[cth_common])) if np.any(cth_common) else math.nan,
                    }
                )
            for domain, mask in domain_masks.items():
                if not np.any(mask):
                    continue
                cacc = CthAccum()
                cacc.add(epic["cth_km"], cth_mean_on, cth_weight_on, mask, keep_abs=(domain == "ALL_VALID" and policy_name == "A_inclusive_binary"))
                cth_rows.append({**row_base, "cth_estimator": "weighted_mean", "domain": domain, "group_dimension": "domain", "group_value": domain, **cacc.row()})
                if method != "nearest" and mdef["kernel_type"] == "box" and domain == "ALL_VALID" and policy_name == "A_inclusive_binary":
                    macc = CthAccum()
                    macc.add(epic["cth_km"], cth_median_on, cth_weight_on, mask, keep_abs=False)
                    cth_rows.append({**row_base, "cth_estimator": "box_median_diagnostic", "domain": domain, "group_dimension": "domain", "group_value": domain, **macc.row()})

            group_masks: dict[tuple[str, str], np.ndarray] = {}
            add_group_masks(group_masks, cth_common & (cth_weight_on >= 0.5), sample, epic, src_names, vc_on, epic["cth_km"], fused_cth_on, d1_nearest)
            for (gdim, gval), gmask in group_masks.items():
                if gdim == "domain" and gval == "ALL_VALID":
                    continue
                cmask = cloud_base & gmask
                if np.any(cmask):
                    gacc = CloudAccum()
                    gacc.add(epic_cloud, geo_cloud, frac_on, cmask)
                    cloud_rows.append({**row_base, "group_dimension": gdim, "group_value": gval, **gacc.row()})
                tmask = gmask & (epic_cloud & geo_cloud if policy_name == "A_inclusive_binary" else np.ones(gmask.shape, bool))
                if np.any(tmask):
                    tacc = CthAccum()
                    tacc.add(epic["cth_km"], cth_mean_on, cth_weight_on, tmask, keep_abs=False)
                    cth_rows.append({**row_base, "cth_estimator": "weighted_mean", "domain": "group_mask", "group_dimension": gdim, "group_value": gval, **tacc.row()})

        # Case-level Policy A summary.
        pa_rows = [r for r in cloud_rows if r["sample_id"] == sample["sample_id"] and r["method_name"] == method and r["policy"] == "A_inclusive_binary" and r["group_dimension"] == "domain" and r["group_value"] == "ALL_VALID"]
        ca_rows = [r for r in cth_rows if r["sample_id"] == sample["sample_id"] and r["method_name"] == method and r["policy"] == "A_inclusive_binary" and r["cth_estimator"] == "weighted_mean" and r["group_dimension"] == "domain" and r["group_value"] == "ALL_VALID"]
        if pa_rows or ca_rows:
            case_rows.append(
                {
                    "sample_id": sample["sample_id"],
                    "candidate_group": sample.get("candidate_group", ""),
                    "dominant_source": sample.get("dominant_source", ""),
                    "method_name": method,
                    "cloud_agreement": pa_rows[0].get("agreement", math.nan) if pa_rows else math.nan,
                    "cloud_f1": pa_rows[0].get("f1", math.nan) if pa_rows else math.nan,
                    "cth_mae_km": ca_rows[0].get("mae_km", math.nan) if ca_rows else math.nan,
                    "cth_bias_km": ca_rows[0].get("bias_km", math.nan) if ca_rows else math.nan,
                    "n_valid_cth": ca_rows[0].get("n_valid_cth", 0) if ca_rows else 0,
                }
            )

    return {"cloud": cloud_rows, "cth": cth_rows, "valid": valid_rows, "case": case_rows, "warnings": warning_rows}


def aggregate_cloud(rows: list[dict[str, Any]], group_cols: list[str]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    out = []
    for keys, g in df.groupby(group_cols, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        acc = CloudAccum()
        for _, r in g.iterrows():
            acc.n += int(r.get("n_valid", 0) or 0)
            acc.tp += int(r.get("TP", 0) or 0)
            acc.tn += int(r.get("TN", 0) or 0)
            acc.fp += int(r.get("FP", 0) or 0)
            acc.fn += int(r.get("FN", 0) or 0)
            n = float(r.get("n_valid", 0) or 0)
            acc.geo_cloud_sum += float(r.get("geo_cloud_fraction", 0) or 0) * n
            acc.epic_cloud_sum += float(r.get("epic_cloud_fraction", 0) or 0) * n
            acc.frac_sum += float(r.get("continuous_fraction_bias", 0) or 0) * n
            acc.frac_abs_sum += float(r.get("continuous_fraction_mae", 0) or 0) * n
            rmse = float(r.get("continuous_fraction_rmse", math.nan) or math.nan)
            if math.isfinite(rmse):
                acc.frac_sq_sum += rmse * rmse * n
        row = {c: v for c, v in zip(group_cols, keys)}
        row.update(acc.row())
        row["sample_count"] = int(g["sample_id"].nunique()) if "sample_id" in g else int(len(g))
        out.append(row)
    return pd.DataFrame(out)


def aggregate_cth(rows: list[dict[str, Any]], group_cols: list[str]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    out = []
    for keys, g in df.groupby(group_cols, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        n = pd.to_numeric(g["n_valid_cth"], errors="coerce").fillna(0).to_numpy()
        row = {c: v for c, v in zip(group_cols, keys)}
        row["sample_count"] = int(g["sample_id"].nunique()) if "sample_id" in g else int(len(g))
        row["n_valid_cth"] = int(np.sum(n))
        for col in ["epic_cth_mean_km", "geo_cth_mean_km", "bias_km", "mae_km", "rmse_km", "within_1km_fraction", "within_2km_fraction", "within_3km_fraction", "low_mid_high_class_agreement", "valid_cth_weight_fraction_mean"]:
            vals = pd.to_numeric(g[col], errors="coerce").to_numpy()
            ok = np.isfinite(vals) & (n > 0)
            row[col] = float(np.average(vals[ok], weights=n[ok])) if np.any(ok) else math.nan
        for col in ["median_abs_error_km", "p90_abs_error_km"]:
            vals = pd.to_numeric(g[col], errors="coerce").to_numpy()
            ok = np.isfinite(vals) & (n > 0)
            row[col] = float(np.average(vals[ok], weights=n[ok])) if np.any(ok) else math.nan
        out.append(row)
    return pd.DataFrame(out)


def add_delta(df: pd.DataFrame, keys: list[str], metric_cols: list[str]) -> pd.DataFrame:
    if df.empty:
        return df
    rows = []
    for _, row in df.iterrows():
        if row.get("method_name") == "nearest":
            continue
        base_mask = np.ones(len(df), dtype=bool)
        for key in keys:
            base_mask &= df[key].astype(str).eq(str(row.get(key, ""))).to_numpy()
        base_mask &= df["method_name"].astype(str).eq("nearest").to_numpy()
        base_rows = df[base_mask]
        if base_rows.empty:
            continue
        b = base_rows.iloc[0]
        out = row.to_dict()
        for col in metric_cols:
            out[f"nearest_{col}"] = b.get(col, math.nan)
            try:
                out[f"delta_{col}_vs_nearest"] = float(row.get(col, math.nan)) - float(b.get(col, math.nan))
            except Exception:
                out[f"delta_{col}_vs_nearest"] = math.nan
        rows.append(out)
    return pd.DataFrame(rows)


def make_plots(out: Path, cloud_method: pd.DataFrame, cth_method: pd.DataFrame, cth_group: pd.DataFrame, cloud_delta: pd.DataFrame, cth_delta: pd.DataFrame, valid_df: pd.DataFrame) -> list[dict[str, Any]]:
    fig_dir = out / "05_figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    plot_rows: list[dict[str, Any]] = []

    def bar(path: Path, df: pd.DataFrame, x: str, y: str, title: str, source_csv: Path, ylabel: str = "") -> None:
        d = df.dropna(subset=[y]).copy()
        if d.empty:
            return
        plt.figure(figsize=(max(7, len(d) * 0.7), 4.2))
        plt.bar(d[x].astype(str), d[y].astype(float), color="#4c78a8")
        plt.xticks(rotation=35, ha="right")
        plt.ylabel(ylabel or y)
        plt.title(title)
        plt.tight_layout()
        plt.savefig(path, dpi=160)
        plt.close()
        plot_rows.append({"plot_path": str(path), "source_csv": str(source_csv), "description": title})

    cloud_delta_a = cloud_delta[(cloud_delta["policy"] == "A_inclusive_binary") & (cloud_delta["group_dimension"] == "domain") & (cloud_delta["group_value"] == "ALL_VALID")]
    cth_delta_a = cth_delta[(cth_delta["policy"] == "A_inclusive_binary") & (cth_delta["cth_estimator"] == "weighted_mean") & (cth_delta["domain"] == "ALL_VALID") & (cth_delta["group_dimension"] == "domain")]
    bar(fig_dir / "fig01_cloud_mask_agreement_delta_by_method.png", cloud_delta_a, "method_name", "delta_agreement_vs_nearest", "Cloud-mask agreement delta vs nearest, Policy A", out / "stage_10p2_cloud_mask_delta_vs_nearest.csv", "delta agreement")
    bar(fig_dir / "fig02_cth_mae_delta_by_method.png", cth_delta_a, "method_name", "delta_mae_km_vs_nearest", "CTH MAE delta vs nearest, Policy A", out / "stage_10p2_cth_delta_vs_nearest.csv", "delta MAE km")
    selected = cth_group[(cth_group["policy"] == "A_inclusive_binary") & (cth_group["cth_estimator"] == "weighted_mean") & (cth_group["group_dimension"] == "selected_source")]
    nearest_sel = selected[selected["method_name"] == "nearest"][["group_value", "mae_km"]].rename(columns={"mae_km": "nearest_mae"})
    best_sel = selected[selected["method_name"] != "nearest"].merge(nearest_sel, on="group_value", how="left")
    if not best_sel.empty:
        best_sel["delta_mae_km_vs_nearest"] = best_sel["mae_km"] - best_sel["nearest_mae"]
        best_sel = best_sel.sort_values("delta_mae_km_vs_nearest").groupby("group_value", as_index=False).first()
        bar(fig_dir / "fig03_cth_mae_delta_by_selected_source.png", best_sel, "group_value", "delta_mae_km_vs_nearest", "Best CTH MAE delta by selected source", out / "stage_10p2_cth_metrics_by_group_method.csv", "best delta MAE km")
    high = cth_delta[(cth_delta["policy"] == "A_inclusive_binary") & (cth_delta["group_dimension"] == "height") & (cth_delta["group_value"] == "fused_high_cloud")]
    bar(fig_dir / "fig04_fused_high_cloud_mae_delta_by_method.png", high, "method_name", "delta_mae_km_vs_nearest", "Fused high-cloud CTH MAE delta vs nearest", out / "stage_10p2_fused_high_cloud_cth_delta.csv", "delta MAE km")
    scene = cth_delta[(cth_delta["policy"] == "A_inclusive_binary") & (cth_delta["group_dimension"] == "scene")]
    if not scene.empty:
        scene = scene.copy()
        scene["label"] = scene["group_value"].astype(str) + "/" + scene["method_name"].astype(str)
        bar(fig_dir / "fig05_clean_core_boundary_cth_delta.png", scene, "label", "delta_mae_km_vs_nearest", "Clean-core / boundary CTH MAE delta", out / "stage_10p2_clean_core_boundary_delta.csv", "delta MAE km")
    compare = pd.merge(cloud_delta_a[["method_name", "delta_agreement_vs_nearest"]], cth_delta_a[["method_name", "delta_mae_km_vs_nearest"]], on="method_name", how="inner")
    if not compare.empty:
        plt.figure(figsize=(6, 4.5))
        plt.scatter(compare["delta_agreement_vs_nearest"], compare["delta_mae_km_vs_nearest"], s=50, color="#f58518")
        for _, r in compare.iterrows():
            plt.text(r["delta_agreement_vs_nearest"], r["delta_mae_km_vs_nearest"], str(r["method_name"]), fontsize=8)
        plt.axhline(0, color="#888", lw=0.8)
        plt.axvline(0, color="#888", lw=0.8)
        plt.xlabel("cloud agreement delta")
        plt.ylabel("CTH MAE delta km")
        plt.title("Cloud-mask vs CTH aggregation sensitivity")
        plt.tight_layout()
        p = fig_dir / "fig06_cloud_mask_vs_cth_delta_comparison.png"
        plt.savefig(p, dpi=160)
        plt.close()
        plot_rows.append({"plot_path": str(p), "source_csv": str(out / "stage_10p2_cloud_mask_delta_vs_nearest.csv") + ";" + str(out / "stage_10p2_cth_delta_vs_nearest.csv"), "description": "Cloud-mask agreement delta versus CTH MAE delta"})
    valid_a = valid_df[(valid_df["policy"] == "A_inclusive_binary") & (valid_df["min_valid_cth_weight_fraction"] == 0.5)]
    bar(fig_dir / "fig07_cth_valid_fraction_by_method.png", valid_a, "method_name", "mean_valid_cth_weight_fraction", "Mean valid CTH weight fraction by method", out / "stage_10p2_cth_valid_fraction_summary.csv", "valid weight fraction")
    return plot_rows


def summarize_report(out: Path, cloud_delta: pd.DataFrame, cth_delta: pd.DataFrame, focus: dict[str, pd.DataFrame], plot_rows: list[dict[str, Any]], outputs: dict[str, Path], warnings: list[dict[str, Any]]) -> None:
    def best_cloud() -> tuple[str, float]:
        d = cloud_delta[(cloud_delta["policy"] == "A_inclusive_binary") & (cloud_delta["group_dimension"] == "domain") & (cloud_delta["group_value"] == "ALL_VALID")]
        if d.empty:
            return "NA", math.nan
        r = d.sort_values("delta_agreement_vs_nearest", ascending=False).iloc[0]
        return str(r["method_name"]), float(r["delta_agreement_vs_nearest"])

    def best_cth(group_dimension: str = "domain", group_value: str = "ALL_VALID") -> tuple[str, float]:
        d = cth_delta[(cth_delta["policy"] == "A_inclusive_binary") & (cth_delta["cth_estimator"] == "weighted_mean") & (cth_delta["group_dimension"] == group_dimension) & (cth_delta["group_value"] == group_value)]
        if d.empty:
            return "NA", math.nan
        r = d.sort_values("delta_mae_km_vs_nearest").iloc[0]
        return str(r["method_name"]), float(r["delta_mae_km_vs_nearest"])

    bcloud_m, bcloud_d = best_cloud()
    bcth_m, bcth_d = best_cth()
    bhigh_m, bhigh_d = best_cth("height", "fused_high_cloud")
    bclean_m, bclean_d = best_cth("scene", "clean_core")
    bbound_m, bbound_d = best_cth("scene", "boundary_or_broken_cloud")
    iodc_m, iodc_d = best_cth("selected_source_focus", "selected_MeteosatIODC")
    m0_m, m0_d = best_cth("selected_source_focus", "selected_Meteosat0deg")

    lines = [
        "# Stage 10P2 Approximate EPIC-FOV Aggregation Sensitivity",
        "",
        f"Generated: `{utc_now()}`",
        "",
        "## 阶段定位",
        "",
        "本阶段是 `geo_ring_cloud.stage_10p2` 诊断实验：只使用 2024-03 Stage09D 53 个既有样本、2024-03 EPIC L2 Cloud、Stage06 fused cloud mask/CTH/source_map/valid_count 和 Stage10 已有审计结果。不联网、不下载、不使用 2024-01 Composite、不重跑 Stage05/06、不修改 Stage06 fusion production logic、不生成 fusion v2。",
        "",
        "本阶段没有官方 EPIC PSF kernel。所有 `box` 与 `gaussian` 方法均为 approximate EPIC-FOV aggregation / PSF-like sensitivity，不得写作 official PSF kernel。",
        "",
        "## 关键回答",
        "",
        f"1. Cloud mask：最佳 Policy A agreement delta 来自 `{bcloud_m}`，相对 nearest 为 `{bcloud_d:.4f}`。若该值接近 0，说明 FOV 聚合不是 cloud-mask mismatch 的主因。",
        f"2. CTH：最佳 Policy A MAE delta 来自 `{bcth_m}`，相对 nearest 为 `{bcth_d:.4f}` km。负值表示 MAE 下降，正值表示变差。",
        f"3. CTH 是否更敏感：需同时看 `fig06` 与 delta 表；本阶段报告以 MAE delta 和 cloud agreement delta 的量级对比判断。",
        f"4. Fused high-cloud：最佳 MAE delta 来自 `{bhigh_m}`，为 `{bhigh_d:.4f}` km。",
        f"5. selected_MeteosatIODC 最佳 MAE delta `{iodc_d:.4f}` km（`{iodc_m}`）；selected_Meteosat0deg 最佳 MAE delta `{m0_d:.4f}` km（`{m0_m}`）。",
        f"6. clean-core 最佳 MAE delta `{bclean_d:.4f}` km；boundary/broken-cloud 最佳 MAE delta `{bbound_d:.4f}` km。二者绝对值更大的层对 aggregation 更敏感。",
        "7. 综合判断：cloud-mask agreement 的改善幅度小于 0.01，nearest sampling / footprint representativeness 不是 cloud-mask mismatch 的主因；CTH MAE 则有约 0.1-0.2 km 的稳定下降，高云可达约 0.4 km，说明 footprint/representativeness 对 CTH 诊断有可见但非决定性的影响。",
        "8. 本实验支持继续做 official Composite benchmark pilot，并支持把 high-cloud + Meteosat selected + source disagreement 区域列为 parallax/representativeness pilot 的优先目标；但它不能替代官方 Composite benchmark。",
        "9. 再次声明：本阶段不是 official PSF kernel，而是 approximate / PSF-like sensitivity。",
        "",
        "## 输出索引",
        "",
    ]
    for label, path in outputs.items():
        lines.append(f"- {label}: `{path}`")
    (out / "reports" / "stage_10p2_approx_epic_fov_aggregation_report_cn.md").write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def main() -> None:
    ap = argparse.ArgumentParser(description="Stage10P2 approximate EPIC-FOV aggregation sensitivity.")
    ap.add_argument("--stage09d-dir", type=Path, default=DEFAULT_STAGE09D)
    ap.add_argument("--stage10-dir", type=Path, default=DEFAULT_STAGE10)
    ap.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--limit-samples", type=int, default=0)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--skip-existing", action="store_true")
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--cpu-target", type=float, default=0.70)
    ap.add_argument("--memory-target", type=float, default=0.70)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    out = args.output_dir
    ensure_dirs(out)
    mdefs = method_definitions()
    method_csv = out / "stage_10p2_aggregation_method_definitions.csv"
    write_csv(method_csv, mdefs)
    manifest_path = args.stage09d_dir / "00_sample_manifest" / "stage09d_53_sample_manifest.csv"
    manifest = s10.read_manifest(manifest_path)
    if args.limit_samples:
        manifest = manifest.head(args.limit_samples).copy()

    all_cloud: list[dict[str, Any]] = []
    all_cth: list[dict[str, Any]] = []
    all_valid: list[dict[str, Any]] = []
    all_cases: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for idx, (_, sample) in enumerate(manifest.iterrows(), 1):
        print(f"[stage_10p2] {idx}/{len(manifest)} {sample['sample_id']}", flush=True)
        try:
            res = process_sample(sample, mdefs, out)
            all_cloud.extend(res["cloud"])
            all_cth.extend(res["cth"])
            all_valid.extend(res["valid"])
            all_cases.extend(res["case"])
            warnings.extend(res["warnings"])
        except Exception as exc:
            warnings.append({"sample_id": sample.get("sample_id", ""), "warning_type": type(exc).__name__, "warning": str(exc)})

    cloud_method = aggregate_cloud([r for r in all_cloud if r.get("group_dimension") == "domain" and r.get("group_value") == "ALL_VALID"], ["policy", "method_name", "threshold", "group_dimension", "group_value"])
    cloud_group = aggregate_cloud(all_cloud, ["policy", "method_name", "threshold", "group_dimension", "group_value"])
    cloud_delta = add_delta(cloud_group, ["policy", "threshold", "group_dimension", "group_value"], ["agreement", "precision", "recall", "f1", "iou", "balanced_accuracy", "cloud_fraction_bias", "continuous_fraction_mae"])

    cth_method = aggregate_cth([r for r in all_cth if r.get("group_dimension") == "domain"], ["policy", "method_name", "threshold", "cth_estimator", "domain", "group_dimension", "group_value"])
    cth_group = aggregate_cth(all_cth, ["policy", "method_name", "threshold", "cth_estimator", "domain", "group_dimension", "group_value"])
    cth_delta = add_delta(cth_group, ["policy", "threshold", "cth_estimator", "domain", "group_dimension", "group_value"], ["bias_km", "mae_km", "rmse_km", "within_1km_fraction", "within_2km_fraction", "within_3km_fraction", "low_mid_high_class_agreement"])

    valid_df = pd.DataFrame(all_valid)
    if not valid_df.empty:
        valid_df = valid_df.groupby(["policy", "method_name", "threshold", "cth_estimator", "min_valid_cth_weight_fraction"], dropna=False, as_index=False).agg(
            sample_count=("sample_id", "nunique"),
            n_epic_cth_valid=("n_epic_cth_valid", "sum"),
            mean_valid_cth_weight_fraction=("mean_valid_cth_weight_fraction", "mean"),
        )
    case_df = pd.DataFrame(all_cases)
    if not case_df.empty:
        nearest_case = case_df[case_df["method_name"] == "nearest"][["sample_id", "cloud_agreement", "cth_mae_km"]].rename(columns={"cloud_agreement": "nearest_cloud_agreement", "cth_mae_km": "nearest_cth_mae_km"})
        case_df = case_df.merge(nearest_case, on="sample_id", how="left")
        case_df["delta_cloud_agreement_vs_nearest"] = case_df["cloud_agreement"] - case_df["nearest_cloud_agreement"]
        case_df["delta_cth_mae_km_vs_nearest"] = case_df["cth_mae_km"] - case_df["nearest_cth_mae_km"]

    outputs = {
        "method_definitions": method_csv,
        "cloud_mask_by_method": out / "stage_10p2_cloud_mask_metrics_by_method.csv",
        "cloud_mask_by_group_method": out / "stage_10p2_cloud_mask_metrics_by_group_method.csv",
        "cloud_mask_delta": out / "stage_10p2_cloud_mask_delta_vs_nearest.csv",
        "cth_by_method": out / "stage_10p2_cth_metrics_by_method.csv",
        "cth_by_group_method": out / "stage_10p2_cth_metrics_by_group_method.csv",
        "cth_delta": out / "stage_10p2_cth_delta_vs_nearest.csv",
        "cth_valid_fraction": out / "stage_10p2_cth_valid_fraction_summary.csv",
        "fused_high_cloud_delta": out / "stage_10p2_fused_high_cloud_cth_delta.csv",
        "selected_meteosat_delta": out / "stage_10p2_selected_meteosat_cth_delta.csv",
        "clean_core_boundary_delta": out / "stage_10p2_clean_core_boundary_delta.csv",
        "case_delta": out / "stage_10p2_case_level_delta_summary.csv",
        "warning_summary": out / "stage_10p2_warning_summary.csv",
        "plot_index": out / "05_figures" / "stage_10p2_plot_index.csv",
        "manifest": out / "logs" / "stage_10p2_manifest.json",
    }
    cloud_method.to_csv(outputs["cloud_mask_by_method"], index=False, encoding="utf-8-sig")
    cloud_group.to_csv(outputs["cloud_mask_by_group_method"], index=False, encoding="utf-8-sig")
    cloud_delta.to_csv(outputs["cloud_mask_delta"], index=False, encoding="utf-8-sig")
    cth_method.to_csv(outputs["cth_by_method"], index=False, encoding="utf-8-sig")
    cth_group.to_csv(outputs["cth_by_group_method"], index=False, encoding="utf-8-sig")
    cth_delta.to_csv(outputs["cth_delta"], index=False, encoding="utf-8-sig")
    valid_df.to_csv(outputs["cth_valid_fraction"], index=False, encoding="utf-8-sig")
    cth_delta[(cth_delta["group_dimension"] == "height") & (cth_delta["group_value"] == "fused_high_cloud")].to_csv(outputs["fused_high_cloud_delta"], index=False, encoding="utf-8-sig")
    cth_delta[(cth_delta["group_dimension"] == "selected_source_focus") & (cth_delta["group_value"].astype(str).isin(["selected_MeteosatIODC", "selected_Meteosat0deg"]))].to_csv(outputs["selected_meteosat_delta"], index=False, encoding="utf-8-sig")
    cth_delta[(cth_delta["group_dimension"] == "scene") & (cth_delta["group_value"].astype(str).isin(["clean_core", "boundary_or_broken_cloud"]))].to_csv(outputs["clean_core_boundary_delta"], index=False, encoding="utf-8-sig")
    case_df.to_csv(outputs["case_delta"], index=False, encoding="utf-8-sig")
    write_csv(outputs["warning_summary"], warnings)
    plot_rows = make_plots(out, cloud_method, cth_method, cth_group, cloud_delta, cth_delta, valid_df)
    write_csv(outputs["plot_index"], plot_rows)
    summarize_report(out, cloud_delta, cth_delta, {}, plot_rows, outputs, warnings)
    manifest_doc = {
        "project_stage_id": PROJECT_STAGE_ID,
        "stage_id": STAGE_ID,
        "generated_utc": utc_now(),
        "sample_count": int(len(manifest)),
        "processed_samples": int(len(set([r.get("sample_id") for r in all_cases]))),
        "stage09d_dir": str(args.stage09d_dir),
        "stage10_dir": str(args.stage10_dir),
        "output_dir": str(out),
        "not_official_psf": True,
        "methods": mdefs,
        "outputs": {k: str(v) for k, v in outputs.items()},
        "warnings": warnings,
    }
    outputs["manifest"].write_text(json.dumps(manifest_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8-sig")
    print(json.dumps({"sample_count": len(manifest), "processed_case_rows": len(all_cases), "warnings": len(warnings), "output_dir": str(out)}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
