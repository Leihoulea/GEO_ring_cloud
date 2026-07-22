# -*- coding: utf-8 -*-
"""Stage 09H Gate 2 Meteosat-0deg multi-case systematicity audit.

This read-only diagnostic repeats source-level Meteosat-0deg native transform
tests across several March 2024 EPIC-matched cases.  Every variant is
reprojected from native Meteosat arrays to the Stage 05 lon-lat grid and then
sampled onto EPIC pixels.  The script does not modify production fusion logic.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.ndimage import distance_transform_edt

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from geo_ring_cloud import paths as path_config  # noqa: E402
from geo_ring_cloud.cloud_semantics import cloud_mask_masks  # noqa: E402
from geo_ring_cloud.diagnostics import full_pixel  # noqa: E402
from geo_ring_cloud.diagnostics.full_pixel_workflow import load_manifest  # noqa: E402
from geo_ring_cloud.reprojection import build_tree, normalize_longitude, query_reproject  # noqa: E402

PROJECT_ID = "geo_ring_cloud"
STAGE_ID = "stage_09h"
RUN_ID = "stage_09h_meteosat_mask_navigation_root_cause_202403"
GATE_ID = "gate2_meteosat0deg_systematicity"
SOURCE = "Meteosat-0deg"
PRODUCT = "CLM"
POLICY_NAME = "A_inclusive_binary"
STAGE09D_DIR = path_config.RUNS_ROOT / "stage09d_full_pixel_diagnostics_202403"
OUT_ROOT = path_config.RUNS_ROOT / RUN_ID


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_dirs(root: Path) -> dict[str, Path]:
    dirs = {
        "source_data": root / "source_data",
        "reports": root / "reports",
        "logs": root / "logs",
        "figures": root / "figures",
    }
    for directory in dirs.values():
        directory.mkdir(parents=True, exist_ok=True)
    return dirs


def read_npz_arrays(path: Path) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    with np.load(path, allow_pickle=False) as payload:
        arrays = {
            key: np.asarray(payload[key])
            for key in payload.files
            if not key.endswith("_json") and key != "variable_availability"
        }
        meta = json.loads(str(payload["metadata_json"])) if "metadata_json" in payload.files else {}
    return arrays, meta


def target_lon_lat(grid: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    lon = np.linspace(
        float(grid["lon_centers_first_last"][0]),
        float(grid["lon_centers_first_last"][1]),
        int(grid["lon_size"]),
        dtype=np.float64,
    )
    lat = np.linspace(
        float(grid["lat_centers_first_last"][0]),
        float(grid["lat_centers_first_last"][1]),
        int(grid["lat_size"]),
        dtype=np.float64,
    )
    return lon, lat


def find_native_path(row: dict[str, Any]) -> Path:
    inv_path = Path(row["stage_run_dir"]) / "standardized_native" / "standardized_native_inventory.csv"
    inv = pd.read_csv(inv_path)
    hit = inv[(inv["satellite_group"] == SOURCE) & (inv["product"] == PRODUCT)]
    if hit.empty:
        raise RuntimeError(f"missing {SOURCE} {PRODUCT} native row in {inv_path}")
    return Path(str(hit.iloc[0]["npz_file"]))


def epic_center_lon(epic: dict[str, np.ndarray]) -> float:
    lat = np.asarray(epic["lat"], dtype=np.float32)
    lon = ((np.asarray(epic["lon"], dtype=np.float32) + 180.0) % 360.0) - 180.0
    cloud = np.asarray(epic["cloud_mask"])
    valid = np.isfinite(lat) & np.isfinite(lon) & np.isin(cloud, [1, 2, 3, 4])
    if not np.any(valid):
        return math.nan
    near_equator = valid & (np.abs(lat) <= 5.0)
    use = near_equator if np.count_nonzero(near_equator) >= 100 else valid
    # Circular mean avoids the -180/180 seam.
    radians = np.deg2rad(lon[use].astype(np.float64))
    mean_angle = math.atan2(float(np.mean(np.sin(radians))), float(np.mean(np.cos(radians))))
    return ((math.degrees(mean_angle) + 180.0) % 360.0) - 180.0


def confusion_metrics(epic_cls: np.ndarray, geo_cls: np.ndarray, valid: np.ndarray, positive: int = 1) -> dict[str, Any]:
    n_valid = int(np.count_nonzero(valid))
    if n_valid == 0:
        return {
            "n_valid": 0,
            "agreement": math.nan,
            "balanced_accuracy": math.nan,
            "precision_cloud": math.nan,
            "recall_cloud": math.nan,
            "specificity_clear": math.nan,
            "f1_cloud": math.nan,
            "iou_cloud": math.nan,
            "mcc": math.nan,
            "TP": 0,
            "TN": 0,
            "FP": 0,
            "FN": 0,
            "cloud_fraction_epic": math.nan,
            "cloud_fraction_geo": math.nan,
            "cloud_fraction_bias_geo_minus_epic": math.nan,
        }
    e = epic_cls[valid]
    g = geo_cls[valid]
    ep = e == positive
    gp = g == positive
    tp = int(np.count_nonzero(ep & gp))
    tn = int(np.count_nonzero((~ep) & (~gp)))
    fp = int(np.count_nonzero((~ep) & gp))
    fn = int(np.count_nonzero(ep & (~gp)))
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    specificity = tn / max(tn + fp, 1)
    f1 = 2.0 * precision * recall / max(precision + recall, 1e-12)
    denom = math.sqrt(max((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn), 0))
    mcc = ((tp * tn) - (fp * fn)) / denom if denom else math.nan
    return {
        "n_valid": n_valid,
        "agreement": float(np.mean(e == g)),
        "balanced_accuracy": (recall + specificity) / 2.0,
        "precision_cloud": precision,
        "recall_cloud": recall,
        "specificity_clear": specificity,
        "f1_cloud": f1,
        "iou_cloud": tp / max(tp + fp + fn, 1),
        "mcc": mcc,
        "TP": tp,
        "TN": tn,
        "FP": fp,
        "FN": fn,
        "cloud_fraction_epic": float(np.mean(ep)),
        "cloud_fraction_geo": float(np.mean(gp)),
        "cloud_fraction_bias_geo_minus_epic": float(np.mean(gp) - np.mean(ep)),
    }


def boundary_mask(classes: np.ndarray, valid: np.ndarray, positive: int = 1) -> np.ndarray:
    cloud = (classes == positive) & valid
    padded_cloud = np.pad(cloud.astype(np.int16), 1, mode="edge")
    padded_valid = np.pad(valid.astype(np.int16), 1, mode="constant")
    count = np.zeros(classes.shape, dtype=np.int16)
    valid_count = np.zeros(classes.shape, dtype=np.int16)
    for dy in range(3):
        for dx in range(3):
            count += padded_cloud[dy : dy + classes.shape[0], dx : dx + classes.shape[1]]
            valid_count += padded_valid[dy : dy + classes.shape[0], dx : dx + classes.shape[1]]
    return valid & (valid_count > 0) & (count > 0) & (count < valid_count)


def boundary_metrics(epic_cls: np.ndarray, geo_cls: np.ndarray, valid: np.ndarray, positive: int = 1) -> dict[str, Any]:
    if int(np.count_nonzero(valid)) == 0:
        return {
            "boundary_f1": math.nan,
            "boundary_precision": math.nan,
            "boundary_recall": math.nan,
            "boundary_chamfer_px": math.nan,
            "n_boundary_epic": 0,
            "n_boundary_geo": 0,
        }
    eb = boundary_mask(epic_cls, valid, positive)
    gb = boundary_mask(geo_cls, valid, positive)
    tp = int(np.count_nonzero(eb & gb))
    fp = int(np.count_nonzero((~eb) & gb & valid))
    fn = int(np.count_nonzero(eb & (~gb) & valid))
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    f1 = 2.0 * precision * recall / max(precision + recall, 1e-12)
    if np.any(eb) and np.any(gb):
        dist_to_geo = distance_transform_edt(~gb)
        dist_to_epic = distance_transform_edt(~eb)
        chamfer = (float(np.mean(dist_to_geo[eb])) + float(np.mean(dist_to_epic[gb]))) / 2.0
    else:
        chamfer = math.nan
    return {
        "boundary_f1": f1,
        "boundary_precision": precision,
        "boundary_recall": recall,
        "boundary_chamfer_px": chamfer,
        "n_boundary_epic": int(np.count_nonzero(eb)),
        "n_boundary_geo": int(np.count_nonzero(gb)),
    }


def metric_bundle(epic_cls: np.ndarray, geo_cls: np.ndarray, valid: np.ndarray) -> dict[str, Any]:
    out = confusion_metrics(epic_cls, geo_cls, valid)
    out.update(boundary_metrics(epic_cls, geo_cls, valid))
    return out


def variant_arrays(mask: np.ndarray, lat: np.ndarray, lon: np.ndarray) -> list[tuple[str, str, str, np.ndarray, np.ndarray, np.ndarray]]:
    return [
        ("identity", "identity", "control", mask, lat, lon),
        (
            "rotate_mask_only",
            "cloud_mask[::-1, ::-1]; latitude/longitude unchanged",
            "A_relative_180_candidate",
            mask[::-1, ::-1],
            lat,
            lon,
        ),
        (
            "rotate_navigation_only",
            "cloud_mask unchanged; latitude/longitude[::-1, ::-1]",
            "B_relative_180_candidate",
            mask,
            lat[::-1, ::-1],
            lon[::-1, ::-1],
        ),
        (
            "rotate_both",
            "cloud_mask/latitude/longitude all [::-1, ::-1]",
            "C_storage_order_control",
            mask[::-1, ::-1],
            lat[::-1, ::-1],
            lon[::-1, ::-1],
        ),
        (
            "flipud_mask_only",
            "cloud_mask[::-1, :]; latitude/longitude unchanged",
            "single_axis_mask_control",
            mask[::-1, :],
            lat,
            lon,
        ),
        (
            "fliplr_mask_only",
            "cloud_mask[:, ::-1]; latitude/longitude unchanged",
            "single_axis_mask_control",
            mask[:, ::-1],
            lat,
            lon,
        ),
    ]


def reproject_variant(mask: np.ndarray, lat: np.ndarray, lon: np.ndarray, grid: dict[str, Any]) -> tuple[np.ndarray, np.ndarray, int]:
    _, fusion_valid, _ = cloud_mask_masks(SOURCE, PRODUCT, mask)
    finite_nav = np.isfinite(lat) & np.isfinite(lon) & (lat >= -90.0) & (lat <= 90.0)
    source_valid = fusion_valid & finite_nav
    tree, src_y, src_x, _ = build_tree(normalize_longitude(lon), lat.astype(np.float32), source_valid)
    target_lon, target_lat = target_lon_lat(grid)
    data_grid, valid_grid_u8 = query_reproject(
        tree,
        src_y,
        src_x,
        mask.astype(np.int16, copy=False),
        target_lon,
        target_lat,
        np.dtype(np.int16),
        -9999,
    )
    return data_grid, valid_grid_u8.astype(bool), int(np.count_nonzero(source_valid))


def load_source_agreement_table() -> pd.DataFrame:
    path = STAGE09D_DIR / "01_source_pair_recompute" / "stage09d_source_by_source_metrics.csv"
    df = pd.read_csv(path)
    df = df[(df["policy"] == POLICY_NAME) & (df["source_name"] == SOURCE) & df["agreement"].notna()].copy()
    if df.empty:
        raise RuntimeError(f"no {SOURCE} rows found in {path}")
    manifest = pd.DataFrame(load_manifest(STAGE09D_DIR))
    keep_cols = [
        "sample_id",
        "epic_time_utc",
        "nearest_georing_time_utc",
        "time_diff_min",
        "candidate_group",
        "dominant_source",
        "stage_run_dir",
        "epic_file",
    ]
    return df.merge(manifest[keep_cols], on="sample_id", how="left")


def add_case(
    candidate_rows: list[pd.Series],
    selected: list[pd.Series],
    seen: set[str],
    seen_dates: set[str],
    prefer_unique_date: bool,
    reason: str,
) -> bool:
    for row in candidate_rows:
        sample_id = str(row["sample_id"])
        date = sample_id[:8]
        if sample_id in seen:
            continue
        if prefer_unique_date and date in seen_dates:
            continue
        row = row.copy()
        row["selection_reason"] = reason
        selected.append(row)
        seen.add(sample_id)
        seen_dates.add(date)
        return True
    return False


def select_cases(metrics: pd.DataFrame, target_success_cases: int, max_candidate_cases: int) -> pd.DataFrame:
    ranked = metrics.sort_values("agreement").reset_index(drop=True)
    selected: list[pd.Series] = []
    seen: set[str] = set()
    seen_dates: set[str] = set()
    anchors = ["20240310_1200", "20240316_0800"]
    for sample_id in anchors:
        rows = [row for _, row in ranked[ranked["sample_id"] == sample_id].iterrows()]
        add_case(rows, selected, seen, seen_dates, prefer_unique_date=False, reason=f"forced_anchor_{sample_id}")
    # Order the first quantile picks as low / middle / high so a five-case run
    # cannot accidentally stop before including the high-agreement stratum.
    quantiles = [
        (0.0, "low_stage09d_agreement_quantile"),
        (0.5, "middle_stage09d_agreement_quantile"),
        (1.0, "high_stage09d_agreement_quantile"),
        (0.25, "lower_middle_stage09d_agreement_quantile"),
        (0.75, "upper_middle_stage09d_agreement_quantile"),
    ]
    for q, reason in quantiles:
        idx = int(round(q * (len(ranked) - 1)))
        window = ranked.iloc[max(0, idx - 3) : min(len(ranked), idx + 4)]
        rows = [row for _, row in window.iloc[(window["agreement"] - ranked.iloc[idx]["agreement"]).abs().argsort()].iterrows()]
        add_case(rows, selected, seen, seen_dates, prefer_unique_date=True, reason=reason)
    if len(selected) < max_candidate_cases:
        rows = [row for _, row in ranked.iterrows()]
        add_case(rows, selected, seen, seen_dates, prefer_unique_date=False, reason="fallback_ranked_fill")
    out = pd.DataFrame(selected).head(max(max_candidate_cases, target_success_cases))
    if out.empty:
        raise RuntimeError("case selection returned no rows")
    return out


def evaluate_case(row: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    ctx = full_pixel.sample_context(row)
    policy = full_pixel.POLICIES[POLICY_NAME]
    epic_cls, epic_policy_valid = full_pixel.apply_policy(ctx["epic"]["cloud_mask"], policy["epic"])
    valid_earth = np.isin(ctx["epic"]["cloud_mask"], [1, 2, 3, 4])
    native_path = find_native_path(row)
    native_arrays, native_meta = read_npz_arrays(native_path)
    raw_mask = np.asarray(native_arrays["cloud_mask"])
    raw_lat = np.asarray(native_arrays["latitude"], dtype=np.float32)
    raw_lon = np.asarray(native_arrays["longitude"], dtype=np.float32)
    if raw_mask.shape != raw_lat.shape or raw_mask.shape != raw_lon.shape:
        raise RuntimeError(f"native shape mismatch mask={raw_mask.shape} lat={raw_lat.shape} lon={raw_lon.shape}")

    sampled: dict[str, dict[str, np.ndarray | int]] = {}
    variant_rows: list[dict[str, Any]] = []
    for variant_name, transform, role, mask, lat, lon in variant_arrays(raw_mask, raw_lat, raw_lon):
        grid_data, grid_valid, native_valid_count = reproject_variant(mask, lat, lon, ctx["grid"])
        raw_on_epic, raw_valid_on_epic = full_pixel.sample_grid(
            grid_data,
            grid_valid,
            ctx["epic"]["lat"],
            ctx["epic"]["lon"],
            ctx["grid"],
        )
        standard = full_pixel.source_to_standard(SOURCE, raw_on_epic)
        geo_cls, geo_policy_valid = full_pixel.apply_policy(standard, policy["geo"])
        valid = valid_earth & epic_policy_valid & raw_valid_on_epic & geo_policy_valid
        sampled[variant_name] = {"geo_cls": geo_cls, "valid": valid, "native_valid_count": native_valid_count}
        metrics = metric_bundle(epic_cls, geo_cls, valid)
        metrics.update(
            {
                "sample_id": row["sample_id"],
                "epic_time_utc": row.get("epic_time_utc"),
                "time_diff_min": row.get("time_diff_min"),
                "candidate_group": row.get("candidate_group"),
                "dominant_source": row.get("dominant_source"),
                "epic_center_lon_deg": epic_center_lon(ctx["epic"]),
                "policy": POLICY_NAME,
                "source_name": SOURCE,
                "comparison_domain": "variant_valid_Meteosat0deg_source_range",
                "native_variant": variant_name,
                "variant_role": role,
                "native_transform": transform,
                "native_path": str(native_path),
                "native_valid_count_before_reprojection": native_valid_count,
                "epic_source_valid_before_policy": int(np.count_nonzero(raw_valid_on_epic & valid_earth)),
                "common_support_fraction": 1.0,
            }
        )
        variant_rows.append(metrics)

    common = valid_earth & epic_policy_valid
    for item in sampled.values():
        common &= np.asarray(item["valid"], dtype=bool)
    common_n = int(np.count_nonzero(common))
    common_rows: list[dict[str, Any]] = []
    identity_variant_n = int(np.count_nonzero(np.asarray(sampled["identity"]["valid"], dtype=bool)))
    for variant_name, transform, role, _, _, _ in variant_arrays(raw_mask, raw_lat, raw_lon):
        geo_cls = np.asarray(sampled[variant_name]["geo_cls"])
        variant_valid = np.asarray(sampled[variant_name]["valid"], dtype=bool)
        metrics = metric_bundle(epic_cls, geo_cls, common)
        metrics.update(
            {
                "sample_id": row["sample_id"],
                "epic_time_utc": row.get("epic_time_utc"),
                "time_diff_min": row.get("time_diff_min"),
                "candidate_group": row.get("candidate_group"),
                "dominant_source": row.get("dominant_source"),
                "epic_center_lon_deg": epic_center_lon(ctx["epic"]),
                "policy": POLICY_NAME,
                "source_name": SOURCE,
                "comparison_domain": "common_valid_all_native_variants",
                "native_variant": variant_name,
                "variant_role": role,
                "native_transform": transform,
                "native_path": str(native_path),
                "native_valid_count_before_reprojection": int(sampled[variant_name]["native_valid_count"]),
                "epic_source_valid_before_policy": int(np.count_nonzero(variant_valid & valid_earth)),
                "common_support_fraction": common_n / max(int(np.count_nonzero(variant_valid)), 1),
                "common_support_fraction_of_identity": common_n / max(identity_variant_n, 1),
            }
        )
        common_rows.append(metrics)

    case_meta = {
        "sample_id": row["sample_id"],
        "epic_time_utc": row.get("epic_time_utc"),
        "time_diff_min": row.get("time_diff_min"),
        "candidate_group": row.get("candidate_group"),
        "dominant_source": row.get("dominant_source"),
        "epic_center_lon_deg": epic_center_lon(ctx["epic"]),
        "native_path": str(native_path),
        "native_source_file": str(native_meta.get("source_file", "")),
        "native_shape": "x".join(str(v) for v in raw_mask.shape),
    }
    return pd.DataFrame(variant_rows), pd.DataFrame(common_rows), case_meta


def summarize_deltas(metrics: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows: list[dict[str, Any]] = []
    domains = sorted(metrics["comparison_domain"].unique())
    metric_cols = ["agreement", "balanced_accuracy", "f1_cloud", "iou_cloud", "mcc", "boundary_f1", "boundary_chamfer_px"]
    for domain in domains:
        sub = metrics[metrics["comparison_domain"] == domain]
        for sample_id, group in sub.groupby("sample_id"):
            base_rows = group[group["native_variant"] == "identity"]
            if base_rows.empty:
                continue
            base = base_rows.iloc[0]
            for _, row in group.iterrows():
                if row["native_variant"] == "identity":
                    continue
                out = {
                    "sample_id": sample_id,
                    "comparison_domain": domain,
                    "native_variant": row["native_variant"],
                    "variant_role": row["variant_role"],
                    "n_valid_identity": int(base["n_valid"]),
                    "n_valid_variant": int(row["n_valid"]),
                    "common_support_fraction": float(row.get("common_support_fraction", math.nan)),
                }
                for col in metric_cols:
                    out[f"{col}_identity"] = float(base[col]) if pd.notna(base[col]) else math.nan
                    out[f"{col}_variant"] = float(row[col]) if pd.notna(row[col]) else math.nan
                    sign = -1.0 if col == "boundary_chamfer_px" else 1.0
                    out[f"delta_{col}_positive_means_better"] = sign * (
                        out[f"{col}_variant"] - out[f"{col}_identity"]
                    )
                rows.append(out)
    delta = pd.DataFrame(rows)
    decision_rows: list[dict[str, Any]] = []
    common_delta = delta[delta["comparison_domain"] == "common_valid_all_native_variants"]
    for sample_id, group in common_delta.groupby("sample_id"):
        def get_variant(name: str) -> pd.Series | None:
            hit = group[group["native_variant"] == name]
            return None if hit.empty else hit.iloc[0]

        a = get_variant("rotate_mask_only")
        b = get_variant("rotate_navigation_only")
        c = get_variant("rotate_both")
        if a is None or b is None or c is None:
            continue
        a_clear = bool(a["delta_agreement_positive_means_better"] >= 0.10 and a["delta_mcc_positive_means_better"] >= 0.20)
        b_clear = bool(b["delta_agreement_positive_means_better"] >= 0.10 and b["delta_mcc_positive_means_better"] >= 0.20)
        c_close = bool(
            abs(c["delta_agreement_positive_means_better"]) <= 0.015
            and abs(c["delta_mcc_positive_means_better"]) <= 0.05
        )
        decision_rows.append(
            {
                "sample_id": sample_id,
                "comparison_domain": "common_valid_all_native_variants",
                "rotate_mask_only_clear_improvement": a_clear,
                "rotate_navigation_only_clear_improvement": b_clear,
                "rotate_both_close_to_identity": c_close,
                "pattern_A_and_B_improve_C_identity_like": bool(a_clear and b_clear and c_close),
                "threshold_delta_agreement_clear": 0.10,
                "threshold_delta_mcc_clear": 0.20,
                "threshold_abs_delta_agreement_C_close": 0.015,
                "threshold_abs_delta_mcc_C_close": 0.05,
                "delta_agreement_A": float(a["delta_agreement_positive_means_better"]),
                "delta_agreement_B": float(b["delta_agreement_positive_means_better"]),
                "delta_agreement_C": float(c["delta_agreement_positive_means_better"]),
                "delta_mcc_A": float(a["delta_mcc_positive_means_better"]),
                "delta_mcc_B": float(b["delta_mcc_positive_means_better"]),
                "delta_mcc_C": float(c["delta_mcc_positive_means_better"]),
                "delta_boundary_chamfer_A_positive_means_better": float(a["delta_boundary_chamfer_px_positive_means_better"]),
                "delta_boundary_chamfer_B_positive_means_better": float(b["delta_boundary_chamfer_px_positive_means_better"]),
                "delta_boundary_chamfer_C_positive_means_better": float(c["delta_boundary_chamfer_px_positive_means_better"]),
            }
        )
    return delta, pd.DataFrame(decision_rows)


def write_figures(delta: pd.DataFrame, metrics: pd.DataFrame, dirs: dict[str, Path]) -> pd.DataFrame:
    figure_rows: list[dict[str, Any]] = []
    plot_delta = delta[
        (delta["comparison_domain"] == "common_valid_all_native_variants")
        & delta["native_variant"].isin(["rotate_mask_only", "rotate_navigation_only", "rotate_both", "flipud_mask_only", "fliplr_mask_only"])
    ].copy()
    if not plot_delta.empty:
        source_csv = dirs["source_data"] / "stage_09h_gate2_figure_delta_source.csv"
        plot_delta.to_csv(source_csv, index=False, encoding="utf-8-sig")
        fig, ax = plt.subplots(figsize=(10.8, 5.8), constrained_layout=True)
        colors = {
            "rotate_mask_only": "#d55e00",
            "rotate_navigation_only": "#0072b2",
            "rotate_both": "#666666",
            "flipud_mask_only": "#cc79a7",
            "fliplr_mask_only": "#009e73",
        }
        pivot = plot_delta.pivot(index="sample_id", columns="native_variant", values="delta_agreement_positive_means_better")
        x = np.arange(len(pivot.index))
        width = 0.15
        for offset, variant in enumerate(colors):
            if variant not in pivot.columns:
                continue
            ax.bar(x + (offset - 2) * width, pivot[variant].values, width=width, label=variant, color=colors[variant])
        ax.axhline(0.0, color="black", linewidth=0.8)
        ax.axhline(0.10, color="#d55e00", linewidth=0.8, linestyle="--", label="clear threshold +0.10")
        ax.set_xticks(x)
        ax.set_xticklabels(pivot.index, rotation=35, ha="right")
        ax.set_ylabel("Delta agreement vs identity")
        ax.set_title("Stage 09H Gate 2: common-valid transform systematicity")
        ax.legend(ncols=3, fontsize=8)
        stem = dirs["figures"] / "stage_09h_gate2_delta_agreement_by_case"
        for ext, dpi in [(".png", 220), (".pdf", None), (".svg", None)]:
            fig.savefig(stem.with_suffix(ext), dpi=dpi)
        plt.close(fig)
        figure_rows.append(
            {
                "figure_id": "stage_09h_gate2_delta_agreement_by_case",
                "title": "Delta agreement by native transform variant",
                "source_csv": str(source_csv),
                "png": str(stem.with_suffix(".png")),
                "pdf": str(stem.with_suffix(".pdf")),
                "svg": str(stem.with_suffix(".svg")),
            }
        )

    plot_metrics = metrics[
        (metrics["comparison_domain"] == "common_valid_all_native_variants")
        & metrics["native_variant"].isin(["identity", "rotate_mask_only", "rotate_navigation_only", "rotate_both"])
    ].copy()
    if not plot_metrics.empty:
        source_csv = dirs["source_data"] / "stage_09h_gate2_figure_mcc_source.csv"
        plot_metrics.to_csv(source_csv, index=False, encoding="utf-8-sig")
        fig, ax = plt.subplots(figsize=(10.8, 5.8), constrained_layout=True)
        colors = {
            "identity": "#333333",
            "rotate_mask_only": "#d55e00",
            "rotate_navigation_only": "#0072b2",
            "rotate_both": "#999999",
        }
        pivot = plot_metrics.pivot(index="sample_id", columns="native_variant", values="mcc")
        x = np.arange(len(pivot.index))
        width = 0.18
        for offset, variant in enumerate(colors):
            if variant not in pivot.columns:
                continue
            ax.bar(x + (offset - 1.5) * width, pivot[variant].values, width=width, label=variant, color=colors[variant])
        ax.axhline(0.0, color="black", linewidth=0.8)
        ax.set_xticks(x)
        ax.set_xticklabels(pivot.index, rotation=35, ha="right")
        ax.set_ylabel("MCC")
        ax.set_title("Stage 09H Gate 2: MCC on common support")
        ax.legend(ncols=4, fontsize=8)
        stem = dirs["figures"] / "stage_09h_gate2_mcc_by_case"
        for ext, dpi in [(".png", 220), (".pdf", None), (".svg", None)]:
            fig.savefig(stem.with_suffix(ext), dpi=dpi)
        plt.close(fig)
        figure_rows.append(
            {
                "figure_id": "stage_09h_gate2_mcc_by_case",
                "title": "MCC by native transform variant",
                "source_csv": str(source_csv),
                "png": str(stem.with_suffix(".png")),
                "pdf": str(stem.with_suffix(".pdf")),
                "svg": str(stem.with_suffix(".svg")),
            }
        )
    return pd.DataFrame(figure_rows)


def fmt(value: Any, digits: int = 3) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)) or pd.isna(value):
        return "NA"
    if isinstance(value, (int, np.integer)):
        return str(int(value))
    return f"{float(value):.{digits}f}"


def markdown_table(df: pd.DataFrame, cols: list[str], digits: int = 3) -> str:
    if df.empty:
        return "_无可用记录。_"
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join(["---"] * len(cols)) + " |"]
    for _, row in df[cols].iterrows():
        lines.append("| " + " | ".join(fmt(row[col], digits) if isinstance(row[col], (float, int, np.floating, np.integer)) or pd.isna(row[col]) else str(row[col]) for col in cols) + " |")
    return "\n".join(lines)


def write_report(
    report_path: Path,
    selected: pd.DataFrame,
    metrics: pd.DataFrame,
    delta: pd.DataFrame,
    decisions: pd.DataFrame,
    outputs: dict[str, str],
    warnings: list[dict[str, Any]],
) -> None:
    common = metrics[metrics["comparison_domain"] == "common_valid_all_native_variants"].copy()
    variant = metrics[metrics["comparison_domain"] == "variant_valid_Meteosat0deg_source_range"].copy()
    key_cols = ["sample_id", "native_variant", "n_valid", "agreement", "balanced_accuracy", "f1_cloud", "iou_cloud", "mcc", "boundary_f1", "boundary_chamfer_px", "common_support_fraction"]
    common_key = common[common["native_variant"].isin(["identity", "rotate_mask_only", "rotate_navigation_only", "rotate_both"])][key_cols]
    support = variant[variant["native_variant"].isin(["identity", "rotate_mask_only", "rotate_navigation_only", "rotate_both", "flipud_mask_only", "fliplr_mask_only"])][
        ["sample_id", "native_variant", "n_valid", "agreement", "mcc", "boundary_chamfer_px"]
    ]
    n_decision = int(len(decisions))
    n_pattern = int(decisions["pattern_A_and_B_improve_C_identity_like"].sum()) if not decisions.empty else 0
    pattern_fraction = n_pattern / max(n_decision, 1)
    verdict = (
        "SYSTEMATIC_RELATIVE_180_PATTERN_HIGH_RISK"
        if n_decision and pattern_fraction >= 0.8
        else "MIXED_OR_NOT_STABLE_ENOUGH_FOR_SYSTEMATICITY"
    )
    lines = [
        "# Stage 09H Gate 2 Meteosat-0deg 多 case 系统性复现实验报告",
        "",
        f"- project_id: `{PROJECT_ID}`",
        f"- canonical_stage_id: `{STAGE_ID}`",
        f"- gate_id: `{GATE_ID}`",
        f"- policy: `{POLICY_NAME}`",
        f"- source: `{SOURCE}`",
        f"- generated_utc: `{utc_now()}`",
        "",
        "## 结论边界",
        "",
        "本批实验只判断一个现象是否跨 case 稳定：在 Meteosat-0deg native 层，如果只对 `cloud_mask` 做 180°旋转，或只对 `latitude/longitude` 做 180°旋转，是否都明显优于 identity，同时 `cloud_mask` 与 `latitude/longitude` 一起旋转是否又接近 identity。",
        "",
        "这只能证明“云掩膜值与导航坐标之间存在相对 180°方向风险”是否具有系统性；它不能单独判定错误在 mask 端还是 navigation 端，也不能把 EPIC 当绝对真值。",
        "",
        f"Gate 2 判定: **{verdict}**。在 common-valid 口径下，满足 A/B 明显优于 identity 且 C 接近 identity 的 case 为 `{n_pattern}/{n_decision}`。",
        "",
        "## 样本选择",
        "",
        "样本来自 Stage 09D `stage09d_source_by_source_metrics.csv` 中 `Meteosat-0deg`、`Policy A_inclusive_binary` 的已有 source-level 指标。选择原则是覆盖低、中、高 agreement，并强制包含前面肉眼怀疑/单 case 实验关注的 `20240310_1200` 与 `20240316_0800`；脚本遇到缺失文件会写 warning 并跳到候选列表下一项。",
        "",
        markdown_table(
            selected,
            ["sample_id", "selection_reason", "stage09d_agreement", "stage09d_n_valid", "epic_time_utc", "epic_center_lon_deg", "candidate_group", "dominant_source"],
        ),
        "",
        "## 指标口径",
        "",
        "- `variant_valid_Meteosat0deg_source_range`: 每个 variant 使用自己重投影后在 EPIC 上有效的 Meteosat-0deg 像元范围。这个口径保留了真实 support 变化。",
        "- `common_valid_all_native_variants`: 六个 variant 同时有效的公共像元范围。这个口径用于公平比较 identity 与变换，不让有效范围差异本身制造提升。",
        "- `common_support_fraction`: 在 common-valid 行里，定义为公共有效像元数 / 该 variant 自己有效像元数。数值越低，说明公平比较只保留了越小的一部分 support。",
        "- `boundary_chamfer_px`: EPIC 云边界与 GEO 云边界之间的平均像元距离，单位是 EPIC 数组像元。越小越好。",
        "",
        "## Common-valid 核心结果",
        "",
        markdown_table(common_key, key_cols),
        "",
        "## Variant-valid support 与主指标",
        "",
        markdown_table(support, ["sample_id", "native_variant", "n_valid", "agreement", "mcc", "boundary_chamfer_px"]),
        "",
        "## 系统性判定表",
        "",
        markdown_table(
            decisions,
            [
                "sample_id",
                "rotate_mask_only_clear_improvement",
                "rotate_navigation_only_clear_improvement",
                "rotate_both_close_to_identity",
                "pattern_A_and_B_improve_C_identity_like",
                "delta_agreement_A",
                "delta_agreement_B",
                "delta_agreement_C",
                "delta_mcc_A",
                "delta_mcc_B",
                "delta_mcc_C",
            ],
        ),
        "",
        "## Warning 与限制",
        "",
        f"- warning rows: `{len(warnings)}`，详见 `warnings_gate2.csv`。",
        "- 本实验没有等待或使用 IR108，因此不判断 IR/海岸线导航方向。",
        "- 本实验使用 EPIC 作为独立诊断参照，不把 EPIC 当绝对真值。A/B 同时改善只说明相对方向关系可疑，不能单独归因到 mask 或 navigation。",
        "- 本实验没有修改 fusion 生产逻辑，也没有生成新的 fused product。",
        "",
        "## 输出",
        "",
    ]
    for name, path in outputs.items():
        lines.append(f"- `{name}`: `{path}`")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-success-cases", type=int, default=5)
    parser.add_argument("--max-candidate-cases", type=int, default=8)
    args = parser.parse_args()

    dirs = ensure_dirs(OUT_ROOT)
    warnings: list[dict[str, Any]] = []
    stage09d_metrics = load_source_agreement_table()
    selected_candidates = select_cases(stage09d_metrics, args.target_success_cases, args.max_candidate_cases)
    candidate_rows: list[dict[str, Any]] = []
    for rank, (_, row) in enumerate(selected_candidates.iterrows(), start=1):
        reason = str(row.get("selection_reason", "anchor_or_quantile_candidate"))
        if row["sample_id"] == "20240310_1200":
            reason = "anchor_previous_gate_case"
        elif row["sample_id"] == "20240316_0800":
            reason = "suspect_visual_case"
        candidate_rows.append(
            {
                "sample_id": row["sample_id"],
                "selection_rank": rank,
                "selection_reason": reason,
                "stage09d_agreement": row["agreement"],
                "stage09d_n_valid": int(row["n_valid"]),
                "stage09d_balanced_accuracy": row["balanced_accuracy"],
                "stage09d_f1_cloud": row["f1_cloud"],
                "stage09d_iou_cloud": row["iou_cloud"],
                "epic_time_utc": row.get("epic_time_utc"),
                "time_diff_min": row.get("time_diff_min"),
                "candidate_group": row.get("candidate_group"),
                "dominant_source": row.get("dominant_source"),
                "stage_run_dir": row.get("stage_run_dir"),
                "epic_file": row.get("epic_file"),
            }
        )

    all_metric_frames: list[pd.DataFrame] = []
    case_meta_rows: list[dict[str, Any]] = []
    selected_success_rows: list[dict[str, Any]] = []
    for row in candidate_rows:
        if len(selected_success_rows) >= args.target_success_cases:
            break
        try:
            variant_metrics, common_metrics, case_meta = evaluate_case(row)
        except Exception as exc:  # noqa: BLE001
            warnings.append(
                {
                    "sample_id": row["sample_id"],
                    "stage": "gate2_case_evaluation",
                    "warning": type(exc).__name__,
                    "message": str(exc),
                }
            )
            continue
        all_metric_frames.extend([variant_metrics, common_metrics])
        row.update(case_meta)
        selected_success_rows.append(row)
        case_meta_rows.append(case_meta)

    if len(selected_success_rows) < args.target_success_cases:
        warnings.append(
            {
                "sample_id": "",
                "stage": "gate2_case_selection",
                "warning": "insufficient_successful_cases",
                "message": f"successful={len(selected_success_rows)} target={args.target_success_cases}",
            }
        )
    if not all_metric_frames:
        raise RuntimeError("no successful case metrics were produced")

    selected = pd.DataFrame(selected_success_rows)
    metrics = pd.concat(all_metric_frames, ignore_index=True)
    delta, decisions = summarize_deltas(metrics)
    figures = write_figures(delta, metrics, dirs)

    selected_path = dirs["source_data"] / "stage_09h_gate2_selected_meteosat0deg_cases.csv"
    metrics_path = dirs["source_data"] / "stage_09h_gate2_case_level_native_transform_metrics.csv"
    delta_path = dirs["source_data"] / "stage_09h_gate2_case_level_delta_summary.csv"
    decision_path = dirs["source_data"] / "stage_09h_gate2_systematicity_decision.csv"
    case_meta_path = dirs["source_data"] / "stage_09h_gate2_case_native_lineage.csv"
    warnings_path = dirs["logs"] / "warnings_gate2.csv"
    figure_index_path = dirs["logs"] / "figure_index_gate2.csv"
    manifest_path = dirs["logs"] / "manifest_gate2.json"
    report_path = dirs["reports"] / "stage_09h_gate2_meteosat0deg_systematicity_report_cn.md"

    selected.to_csv(selected_path, index=False, encoding="utf-8-sig")
    metrics.to_csv(metrics_path, index=False, encoding="utf-8-sig")
    delta.to_csv(delta_path, index=False, encoding="utf-8-sig")
    decisions.to_csv(decision_path, index=False, encoding="utf-8-sig")
    pd.DataFrame(case_meta_rows).to_csv(case_meta_path, index=False, encoding="utf-8-sig")
    pd.DataFrame(
        warnings,
        columns=["sample_id", "stage", "warning", "message"],
    ).to_csv(warnings_path, index=False, encoding="utf-8-sig")
    figures.to_csv(figure_index_path, index=False, encoding="utf-8-sig")

    outputs = {
        "selected_cases_csv": str(selected_path),
        "metrics_csv": str(metrics_path),
        "delta_csv": str(delta_path),
        "decision_csv": str(decision_path),
        "case_native_lineage_csv": str(case_meta_path),
        "warnings_csv": str(warnings_path),
        "figure_index_csv": str(figure_index_path),
        "manifest_json": str(manifest_path),
        "report_md": str(report_path),
    }
    manifest = {
        "project_id": PROJECT_ID,
        "canonical_stage_id": STAGE_ID,
        "gate_id": GATE_ID,
        "generated_utc": utc_now(),
        "script_path": str(Path(__file__).resolve()),
        "input_paths": {
            "stage09d_dir": str(STAGE09D_DIR),
            "source_metrics_csv": str(STAGE09D_DIR / "01_source_pair_recompute" / "stage09d_source_by_source_metrics.csv"),
            "sample_manifest": str(STAGE09D_DIR / "00_sample_manifest" / "stage09d_53_sample_manifest.csv"),
        },
        "parameters": {
            "source": SOURCE,
            "product": PRODUCT,
            "policy": POLICY_NAME,
            "target_success_cases": args.target_success_cases,
            "max_candidate_cases": args.max_candidate_cases,
            "variants": [item[0] for item in variant_arrays(np.zeros((1, 1), dtype=np.int16), np.zeros((1, 1), dtype=np.float32), np.zeros((1, 1), dtype=np.float32))],
            "decision_thresholds": {
                "clear_delta_agreement": 0.10,
                "clear_delta_mcc": 0.20,
                "rotate_both_abs_delta_agreement_close": 0.015,
                "rotate_both_abs_delta_mcc_close": 0.05,
            },
        },
        "row_counts": {
            "selected_cases": int(len(selected)),
            "metrics_rows": int(len(metrics)),
            "delta_rows": int(len(delta)),
            "decision_rows": int(len(decisions)),
            "warning_rows": int(len(warnings)),
            "figure_rows": int(len(figures)),
        },
        "outputs": outputs,
        "notes": [
            "Read-only diagnostic; no fusion production logic modified.",
            "EPIC is used as an independent diagnostic reference, not absolute truth.",
            "Gate 2 judges multi-case systematicity only and does not attribute the wrong side.",
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8-sig")
    write_report(report_path, selected, metrics, delta, decisions, outputs, warnings)
    print(json.dumps({"ok": True, "outputs": outputs, "row_counts": manifest["row_counts"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
