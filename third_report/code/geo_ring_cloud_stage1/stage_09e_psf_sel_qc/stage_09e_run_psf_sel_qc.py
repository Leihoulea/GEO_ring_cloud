# -*- coding: utf-8 -*-
"""Stage 09E PSF-like EPIC-view and SEL-QC diagnostics.

This is diagnostic-only code.  It reads existing Stage 09D March 2024 products,
does not rerun Stage 05/06, does not modify fused production logic, and does
not generate a new fused product.
"""
from __future__ import annotations

import argparse
import math
import sys
import traceback
from collections import defaultdict
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import ndimage

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from geo_ring_cloud import paths as path_config  # noqa: E402
from geo_ring_cloud.diagnostics import full_pixel as d09d  # noqa: E402
from geo_ring_cloud.diagnostics.full_pixel_workflow import (  # noqa: E402
    SOURCE_ID_TO_NAME,
    SOURCE_NAME_TO_ID,
    bool_series,
    describe_valid_context,
    ensure_dirs,
    load_manifest,
    md_table,
    metric_row,
    read_csv,
    scene_boundary,
    selected_source_array,
    source_samples,
    summarize_metric_rows,
    utc_now,
    weighted_mean,
    write_csv,
    write_json,
    write_run_manifest,
)

STAGE_ID = "stage_09e"
DEFAULT_STAGE09D_DIR = path_config.RUNS_ROOT / "stage09d_full_pixel_diagnostics_202403"
DEFAULT_PSF_OUT = path_config.RUNS_ROOT / "stage_09e_psf_aware_epic_view_202403"
DEFAULT_SELQC_OUT = path_config.RUNS_ROOT / "stage_09e_sel_qc_common_valid_202403"
POLICIES = ["A_inclusive_binary", "B_high_confidence_only", "C_uncertainty_aware_3class"]
PAIR_LIST = [
    ("FY4B", "Himawari-9"),
    ("FY4B", "Meteosat-IODC"),
    ("Himawari-9", "Meteosat-IODC"),
    ("Meteosat-0deg", "GOES-16"),
    ("Meteosat-0deg", "Meteosat-IODC"),
    ("GOES-16", "GOES-18"),
]
SOURCE_SETS = {
    "FY4B_and_IODC": ["FY4B", "Meteosat-IODC"],
    "Himawari_and_IODC": ["Himawari-9", "Meteosat-IODC"],
    "Meteosat0deg_and_GOES16": ["Meteosat-0deg", "GOES-16"],
    "valid_count_ge4_all_available": ["FY4B", "Himawari-9", "Meteosat-0deg", "Meteosat-IODC"],
}


def sf(value: Any, default: float = math.nan) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def fmt(value: Any) -> str:
    x = sf(value)
    return f"{x:.3f}" if math.isfinite(x) else "NA"


def first_row(rows: list[dict[str, Any]], **keys: str) -> dict[str, Any]:
    for row in rows:
        if all(str(row.get(k, "")) == str(v) for k, v in keys.items()):
            return row
    return {}


def safe_label(name: str) -> str:
    return name.replace("-", "").replace(" ", "").replace("_", "")


def summarize_custom_rows(rows: list[dict[str, Any]], group_fields: list[str], metrics: list[str], weight: str) -> list[dict[str, Any]]:
    buckets: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[tuple(row.get(f, "") for f in group_fields)].append(row)
    out = []
    for key, vals in sorted(buckets.items(), key=lambda kv: kv[0]):
        item = {field: value for field, value in zip(group_fields, key)}
        item["row_count"] = len(vals)
        item["sample_count"] = len({v.get("sample_id", "") for v in vals if v.get("sample_id", "")})
        item[f"{weight}_total"] = sum(sf(v.get(weight), 0.0) for v in vals)
        for metric in metrics:
            num = 0.0
            den = 0.0
            raw = []
            for v in vals:
                x = sf(v.get(metric))
                w = sf(v.get(weight), 0.0)
                if math.isfinite(x):
                    raw.append(x)
                if math.isfinite(x) and math.isfinite(w) and w > 0:
                    num += x * w
                    den += w
            item[f"{metric}_weighted"] = num / den if den else math.nan
            item[f"{metric}_mean"] = sum(raw) / len(raw) if raw else math.nan
        out.append(item)
    return out


def kernel_definitions(kernel_set: str) -> list[dict[str, Any]]:
    rows = [
        {"kernel_name": "K0_nearest", "kernel_type": "nearest", "radius_cell": 0, "sigma_cell": "", "is_official_epic_psf": False},
        {"kernel_name": "K1_box_3x3", "kernel_type": "box", "radius_cell": 1, "sigma_cell": "", "is_official_epic_psf": False},
        {"kernel_name": "K2_box_5x5", "kernel_type": "box", "radius_cell": 2, "sigma_cell": "", "is_official_epic_psf": False},
        {"kernel_name": "K3_box_7x7", "kernel_type": "box", "radius_cell": 3, "sigma_cell": "", "is_official_epic_psf": False},
        {"kernel_name": "K4_gaussian_sigma_0p75_cell_radius_3", "kernel_type": "gaussian", "radius_cell": 3, "sigma_cell": 0.75, "is_official_epic_psf": False},
        {"kernel_name": "K5_gaussian_sigma_1p25_cell_radius_5", "kernel_type": "gaussian", "radius_cell": 5, "sigma_cell": 1.25, "is_official_epic_psf": False},
        {"kernel_name": "K6_gaussian_sigma_1p75_cell_radius_7", "kernel_type": "gaussian", "radius_cell": 7, "sigma_cell": 1.75, "is_official_epic_psf": False},
        {"kernel_name": "K7_gaussian_sigma_2p50_cell_radius_9", "kernel_type": "gaussian", "radius_cell": 9, "sigma_cell": 2.50, "is_official_epic_psf": False},
    ]
    if kernel_set == "pilot":
        keep = {"K0_nearest", "K1_box_3x3", "K3_box_7x7", "K5_gaussian_sigma_1p25_cell_radius_5"}
        rows = [r for r in rows if r["kernel_name"] in keep]
    return rows


def make_kernel(row: dict[str, Any]) -> np.ndarray | None:
    if row["kernel_type"] == "nearest":
        return None
    radius = int(row["radius_cell"])
    size = 2 * radius + 1
    if row["kernel_type"] == "box":
        k = np.ones((size, size), dtype=np.float32)
    else:
        sigma = float(row["sigma_cell"])
        y, x = np.mgrid[-radius : radius + 1, -radius : radius + 1]
        k = np.exp(-0.5 * ((x * x + y * y) / (sigma * sigma))).astype(np.float32)
    k /= np.sum(k)
    return k


def convolved_fraction(binary_grid: np.ndarray, valid_grid: np.ndarray, kernel: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    valid_f = valid_grid.astype(np.float32)
    num = ndimage.convolve(binary_grid.astype(np.float32) * valid_f, kernel, mode="constant", cval=0.0)
    den = ndimage.convolve(valid_f, kernel, mode="constant", cval=0.0)
    frac = np.full(binary_grid.shape, np.nan, dtype=np.float32)
    ok = den > 0
    frac[ok] = num[ok] / den[ok]
    return frac, den


def sample_grid_fast(grid_arr: np.ndarray, valid_grid: np.ndarray, ctx: dict[str, Any], fill: float = np.nan) -> tuple[np.ndarray, np.ndarray]:
    out, valid = d09d.sample_grid(grid_arr, valid_grid, ctx["epic"]["lat"], ctx["epic"]["lon"], ctx["grid"], fill=fill)
    return out, valid


def epic_policy(policy_name: str, ctx: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    policy = d09d.POLICIES[policy_name]
    epic_cls, epic_pv = d09d.apply_policy(ctx["epic"]["cloud_mask"], policy["epic"])
    valid_earth = np.isin(ctx["epic"]["cloud_mask"], [1, 2, 3, 4])
    return epic_cls, epic_pv & valid_earth


def selected_source_class(ctx: dict[str, Any], source_cls: dict[str, np.ndarray], source_valid: dict[str, np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    selected_cls = np.full(ctx["epic"]["lat"].shape, -1, dtype=np.int16)
    selected_valid = np.zeros(ctx["epic"]["lat"].shape, dtype=bool)
    for source, sid in SOURCE_NAME_TO_ID.items():
        if source not in source_cls:
            continue
        m = ctx["selected_source"] == sid
        selected_cls[m] = source_cls[source][m]
        selected_valid |= m & source_valid[source]
    return selected_cls, selected_valid


def pixel_groups(ctx: dict[str, Any], scene: dict[str, np.ndarray], source_valid: dict[str, np.ndarray], base: np.ndarray) -> dict[str, np.ndarray]:
    groups = {
        "ALL_VALID": base,
        "valid_source_count_1": base & (ctx["valid_count"] == 1),
        "valid_source_count_ge4": base & (ctx["valid_count"] >= 4),
        "selected_MeteosatIODC": base & (ctx["selected_source"] == SOURCE_NAME_TO_ID["Meteosat-IODC"]),
        "selected_Meteosat0deg": base & (ctx["selected_source"] == SOURCE_NAME_TO_ID["Meteosat-0deg"]),
        "non_boundary": base & (scene["boundary_class"] == "non_boundary"),
        "boundary_or_broken_cloud": base & ((scene["boundary_class"] != "non_boundary") | (scene["scene_type"] == "broken_cloud")),
    }
    if "FY4B" in source_valid and "Meteosat-IODC" in source_valid:
        groups["FY4B_and_IODC_both_available"] = base & source_valid["FY4B"] & source_valid["Meteosat-IODC"]
    if "Himawari-9" in source_valid and "Meteosat-IODC" in source_valid:
        groups["Himawari_and_IODC_both_available"] = base & source_valid["Himawari-9"] & source_valid["Meteosat-IODC"]
    if "Meteosat-0deg" in source_valid and "GOES-16" in source_valid:
        groups["Meteosat0deg_and_GOES16_both_available"] = base & source_valid["Meteosat-0deg"] & source_valid["GOES-16"]
    return groups


def source_policy_samples(ctx: dict[str, Any], policy_name: str) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], list[str]]:
    source_std, source_valid_raw, warnings = source_samples(ctx)
    out_cls: dict[str, np.ndarray] = {}
    out_valid: dict[str, np.ndarray] = {}
    for source, std in source_std.items():
        cls, pv = d09d.apply_policy(std, d09d.POLICIES[policy_name]["geo"])
        out_cls[source] = cls
        out_valid[source] = source_valid_raw[source] & pv
    return out_cls, out_valid, warnings


def psf_fused_for_sample(row: dict[str, Any], kernels: list[dict[str, Any]], min_valid_weight: float) -> dict[str, Any]:
    ctx = d09d.sample_context(row)
    scene = scene_boundary(ctx, "A_inclusive_binary")
    out_rows: list[dict[str, Any]] = []
    boundary_rows: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    grid_valid = ctx["fused_valid"].astype(bool)
    kernel_cache = {k["kernel_name"]: make_kernel(k) for k in kernels}

    for policy_name in POLICIES:
        epic_cls, epic_valid = epic_policy(policy_name, ctx)
        geo_cls_grid, geo_pv_grid = d09d.apply_policy(ctx["fused_data"], d09d.POLICIES[policy_name]["geo"])
        valid_grid = grid_valid & geo_pv_grid
        for kdef in kernels:
            kname = kdef["kernel_name"]
            thresholds = [0.25, 0.50, 0.75] if policy_name != "C_uncertainty_aware_3class" else [math.nan]
            if kdef["kernel_type"] == "nearest":
                sampled = ctx["fused_on_epic"].astype(np.float32)
                sampled_valid = ctx["fused_on_valid"].astype(bool)
                psf_fraction = np.full(sampled.shape, np.nan, dtype=np.float32)
                if policy_name != "C_uncertainty_aware_3class":
                    cls_nearest, pv_nearest = d09d.apply_policy(sampled, d09d.POLICIES[policy_name]["geo"])
                    psf_fraction[pv_nearest] = (cls_nearest[pv_nearest] == d09d.POLICIES[policy_name]["positive"]).astype(np.float32)
            elif policy_name == "C_uncertainty_aware_3class":
                class_fracs = []
                class_valids = []
                for label in [0, 1, 2]:
                    frac_grid, den_grid = convolved_fraction(geo_cls_grid == label, valid_grid, kernel_cache[kname])
                    frac_on, den_on = sample_grid_fast(frac_grid, den_grid >= min_valid_weight, ctx)
                    class_fracs.append(frac_on)
                    class_valids.append(den_on)
                stack = np.stack(class_fracs, axis=0)
                stack_filled = np.where(np.isfinite(stack), stack, -1)
                sampled = np.argmax(stack_filled, axis=0).astype(np.float32)
                sampled_valid = np.all(np.stack(class_valids, axis=0), axis=0) & (np.max(stack_filled, axis=0) >= 0)
                psf_fraction = stack[2]
            else:
                cloud_grid = geo_cls_grid == d09d.POLICIES[policy_name]["positive"]
                frac_grid, den_grid = convolved_fraction(cloud_grid, valid_grid, kernel_cache[kname])
                psf_fraction, sampled_valid = sample_grid_fast(frac_grid, den_grid >= min_valid_weight, ctx)
                sampled = psf_fraction

            for threshold in thresholds:
                if policy_name == "C_uncertainty_aware_3class":
                    if kdef["kernel_type"] == "nearest":
                        geo_cls, pv = d09d.apply_policy(sampled, d09d.POLICIES[policy_name]["geo"])
                        valid = epic_valid & sampled_valid & pv
                    else:
                        safe_sampled = np.where(sampled_valid, sampled, -1)
                        geo_cls = safe_sampled.astype(np.int16)
                        valid = epic_valid & sampled_valid
                    threshold_label = "argmax"
                elif kdef["kernel_type"] == "nearest":
                    geo_cls, pv = d09d.apply_policy(sampled, d09d.POLICIES[policy_name]["geo"])
                    valid = epic_valid & sampled_valid & pv
                    threshold_label = f"{threshold:.2f}"
                else:
                    geo_cls = np.where(psf_fraction >= threshold, d09d.POLICIES[policy_name]["positive"], 0).astype(np.int16)
                    valid = epic_valid & sampled_valid & np.isfinite(psf_fraction)
                    threshold_label = f"{threshold:.2f}"
                metrics = metric_row(epic_cls, geo_cls, valid, policy_name)
                if policy_name != "C_uncertainty_aware_3class" and int(metrics.get("n_valid", 0)) > 0:
                    ep_cloud = (epic_cls == d09d.POLICIES[policy_name]["positive"]).astype(np.float32)
                    frac = psf_fraction[valid]
                    metrics["brier_score_fraction"] = float(np.mean((frac - ep_cloud[valid]) ** 2))
                    metrics["mae_cloud_fraction"] = float(np.mean(np.abs(frac - ep_cloud[valid])))
                item = {
                    **metrics,
                    "sample_id": row["sample_id"],
                    "kernel_name": kname,
                    "policy": policy_name,
                    "threshold": threshold_label,
                    "candidate_group": row.get("candidate_group", ""),
                    "dominant_source": row.get("dominant_source", ""),
                }
                item.update(describe_valid_context(ctx, valid, scene))
                out_rows.append(item)
                for group_name, gmask in {
                    "non_boundary": scene["boundary_class"] == "non_boundary",
                    "boundary_or_broken_cloud": (scene["boundary_class"] != "non_boundary") | (scene["scene_type"] == "broken_cloud"),
                    "homogeneous_clear": scene["scene_type"] == "homogeneous_clear",
                    "homogeneous_cloud": scene["scene_type"] == "homogeneous_cloud",
                    "broken_cloud": scene["scene_type"] == "broken_cloud",
                }.items():
                    gv = valid & gmask
                    gm = metric_row(epic_cls, geo_cls, gv, policy_name)
                    boundary_rows.append({**gm, "sample_id": row["sample_id"], "kernel_name": kname, "policy": policy_name, "threshold": threshold_label, "boundary_scene": group_name, "candidate_group": row.get("candidate_group", ""), "dominant_source": row.get("dominant_source", "")})
    return {"metric_rows": out_rows, "boundary_rows": boundary_rows, "warnings": warnings}


def run_psf(args: argparse.Namespace, manifest: list[dict[str, Any]]) -> Path:
    out = Path(args.psf_output_dir)
    ensure_dirs(out, ["00_kernel_definitions", "01_fused_psf_metrics", "02_group_psf_metrics", "05_boundary_scene_psf", "06_meteosat_gap_psf", "08_figures", "reports", "logs"])
    kernels = kernel_definitions(args.kernel_set)
    kernel_csv = out / "00_kernel_definitions" / "stage_09e_kernel_definitions.csv"
    write_csv(kernel_csv, kernels)
    metric_rows: list[dict[str, Any]] = []
    boundary_rows: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for idx, row in enumerate(manifest, 1):
        print(f"[stage_09e_psf] {idx}/{len(manifest)} {row['sample_id']}", flush=True)
        try:
            res = psf_fused_for_sample(row, kernels, args.min_valid_weight)
            metric_rows.extend(res["metric_rows"])
            boundary_rows.extend(res["boundary_rows"])
            warnings.extend(res["warnings"])
        except Exception as exc:
            warnings.append({"sample_id": row.get("sample_id", ""), "module": "psf_fused", "warning": f"{type(exc).__name__}:{exc}", "traceback": traceback.format_exc()})
    per_sample_csv = out / "01_fused_psf_metrics" / "stage_09e_fused_psf_metrics_by_sample.csv"
    write_csv(per_sample_csv, metric_rows)
    by_kernel = summarize_metric_rows(metric_rows, ["kernel_name", "policy", "threshold"])
    by_kernel_csv = out / "01_fused_psf_metrics" / "stage_09e_fused_psf_metrics_by_kernel.csv"
    write_csv(by_kernel_csv, by_kernel)
    by_group = summarize_metric_rows(metric_rows, ["candidate_group", "kernel_name", "policy", "threshold"])
    by_group_csv = out / "02_group_psf_metrics" / "stage_09e_psf_metrics_by_group.csv"
    write_csv(by_group_csv, by_group)
    boundary_csv = out / "05_boundary_scene_psf" / "stage_09e_psf_metrics_by_boundary_scene.csv"
    boundary_sum = summarize_metric_rows(boundary_rows, ["boundary_scene", "kernel_name", "policy", "threshold"])
    write_csv(boundary_csv, boundary_sum)
    gap_rows = meteosat_gap_rows(by_group)
    gap_csv = out / "06_meteosat_gap_psf" / "stage_09e_psf_meteosat_gap_by_kernel.csv"
    write_csv(gap_csv, gap_rows)
    plot_index = make_psf_figures(out, by_kernel, boundary_sum, gap_rows, by_kernel_csv, boundary_csv, gap_csv)
    plot_index_csv = out / "08_figures" / "stage_09e_psf_plot_index.csv"
    write_csv(plot_index_csv, plot_index)
    warnings_csv = out / "logs" / "stage_09e_psf_warnings.csv"
    write_csv(warnings_csv, warnings)
    report = write_psf_report(out, by_kernel, boundary_sum, gap_rows, warnings, [kernel_csv, per_sample_csv, by_kernel_csv, by_group_csv, boundary_csv, gap_csv, plot_index_csv])
    write_run_manifest(
        out / "logs" / "stage_09e_psf_manifest.json",
        canonical_stage_id=STAGE_ID,
        script_path=Path(__file__),
        input_paths=[Path(args.stage09d_dir)],
        output_paths=[kernel_csv, per_sample_csv, by_kernel_csv, by_group_csv, boundary_csv, gap_csv, plot_index_csv, report],
        filters=["existing Stage 09D March 2024 samples only", f"kernel_set={args.kernel_set}", f"min_valid_weight={args.min_valid_weight}"],
        unit_conversions=[],
        row_counts={"samples": len(manifest), "metric_rows": len(metric_rows), "boundary_rows": len(boundary_rows), "warnings": len(warnings)},
        warnings=warnings,
    )
    return report


def meteosat_gap_rows(group_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str], dict[str, float]] = defaultdict(dict)
    for row in group_rows:
        cg = str(row.get("candidate_group", "")).upper()
        key = (row.get("kernel_name", ""), row.get("policy", ""), row.get("threshold", ""))
        val = sf(row.get("agreement_weighted"))
        if "METEOSAT" in cg:
            buckets[key]["meteosat_agreement"] = val
        elif "GOES" in cg:
            buckets[key]["goes_agreement"] = val
        elif "EAST" in cg or "FY4B" in cg or "HIMAWARI" in cg:
            buckets[key]["east_asia_agreement"] = val
    rows = []
    nearest_gap: dict[tuple[str, str], float] = {}
    for key, vals in buckets.items():
        best_non = max([v for k, v in vals.items() if k in {"goes_agreement", "east_asia_agreement"} and math.isfinite(v)], default=math.nan)
        gap = best_non - vals.get("meteosat_agreement", math.nan) if math.isfinite(best_non) else math.nan
        if key[0] == "K0_nearest":
            nearest_gap[(key[1], key[2])] = gap
        rows.append({"kernel_name": key[0], "policy": key[1], "threshold": key[2], **vals, "best_non_meteosat_minus_meteosat": gap})
    for row in rows:
        row["delta_gap_vs_nearest"] = sf(row.get("best_non_meteosat_minus_meteosat")) - nearest_gap.get((row["policy"], row["threshold"]), math.nan)
    return rows


def make_psf_figures(out: Path, by_kernel: list[dict[str, Any]], boundary: list[dict[str, Any]], gap: list[dict[str, Any]], kernel_csv: Path, boundary_csv: Path, gap_csv: Path) -> list[dict[str, str]]:
    idx: list[dict[str, str]] = []
    figdir = out / "08_figures"
    df = pd.DataFrame(by_kernel)
    main = df[(df["threshold"].astype(str).isin(["0.50", "argmax"]))].copy()
    if not main.empty:
        for col in ["agreement_weighted", "f1_cloud_weighted", "iou_cloud_weighted"]:
            main[col] = pd.to_numeric(main[col], errors="coerce")
        nearest = main[main["kernel_name"] == "K0_nearest"].set_index("policy")["agreement_weighted"].to_dict()
        main["delta_agreement_vs_nearest"] = main.apply(lambda r: r["agreement_weighted"] - nearest.get(r["policy"], np.nan), axis=1)
        kernel_order = ["K0_nearest", "K1_box_3x3", "K3_box_7x7", "K5_gaussian_sigma_1p25_cell_radius_5"]
        short = {
            "K0_nearest": "nearest",
            "K1_box_3x3": "box 3x3",
            "K3_box_7x7": "box 7x7",
            "K5_gaussian_sigma_1p25_cell_radius_5": "gauss 1.25",
        }
        policy_label = {
            "A_inclusive_binary": "Policy A",
            "B_high_confidence_only": "Policy B",
            "C_uncertainty_aware_3class": "Policy C",
        }
        fig, axes = plt.subplots(2, 1, figsize=(9.5, 7.2), constrained_layout=True, sharex=True)
        colors = {"A_inclusive_binary": "#386fa4", "B_high_confidence_only": "#2a9d8f", "C_uncertainty_aware_3class": "#e76f51"}
        for policy, grp in main.groupby("policy"):
            grp = grp.set_index("kernel_name").reindex(kernel_order)
            x = np.arange(len(kernel_order))
            axes[0].plot(x, grp["agreement_weighted"], marker="o", linewidth=2.0, color=colors.get(policy), label=policy_label.get(policy, policy))
            axes[1].plot(x, grp["delta_agreement_vs_nearest"], marker="o", linewidth=2.0, color=colors.get(policy), label=policy_label.get(policy, policy))
        axes[0].set_ylabel("agreement")
        axes[0].set_title("Stage 09E PSF-like fused agreement")
        axes[0].grid(axis="y", alpha=0.25)
        axes[0].legend(ncol=3, fontsize=9)
        axes[1].axhline(0, color="#555555", linewidth=0.8)
        axes[1].set_ylabel("delta vs nearest")
        axes[1].set_xticks(np.arange(len(kernel_order)))
        axes[1].set_xticklabels([short[k] for k in kernel_order])
        axes[1].grid(axis="y", alpha=0.25)
        p = figdir / "fig01_stage_09e_psf_agreement_by_kernel_policy.png"
        fig.savefig(p, dpi=170)
        plt.close(fig)
        idx.append({"plot_path": str(p), "source_csv": str(kernel_csv), "description": "PSF-like fused agreement and delta by kernel/policy", "created_time_utc": utc_now()})
    bdf = pd.DataFrame(boundary)
    if not bdf.empty:
        b = bdf[(bdf["policy"] == "A_inclusive_binary") & (bdf["threshold"].astype(str) == "0.50")].copy()
        if not b.empty:
            b["agreement_weighted"] = pd.to_numeric(b["agreement_weighted"], errors="coerce")
            nearest = b[b["kernel_name"] == "K0_nearest"].set_index("boundary_scene")["agreement_weighted"].to_dict()
            b["delta_vs_nearest"] = b.apply(lambda r: r["agreement_weighted"] - nearest.get(r["boundary_scene"], np.nan), axis=1)
            b = b[b["kernel_name"] != "K0_nearest"]
            scene_order = ["boundary_or_broken_cloud", "non_boundary", "homogeneous_clear", "homogeneous_cloud"]
            b = b[b["boundary_scene"].isin(scene_order)]
            pivot = b.pivot(index="kernel_name", columns="boundary_scene", values="delta_vs_nearest").reindex(["K1_box_3x3", "K3_box_7x7", "K5_gaussian_sigma_1p25_cell_radius_5"])
            fig, ax = plt.subplots(figsize=(10, 5.2), constrained_layout=True)
            pivot.plot(kind="bar", ax=ax, color=["#e76f51", "#386fa4", "#8ab17d", "#2a9d8f"])
            ax.axhline(0, color="#555555", linewidth=0.8)
            ax.set_ylabel("agreement delta vs nearest")
            ax.set_title("Stage 09E PSF improvement by boundary/scene, Policy A")
            ax.set_xticklabels(["box 3x3", "box 7x7", "gauss 1.25"], rotation=0)
            ax.grid(axis="y", alpha=0.25)
            ax.legend(title="scene", fontsize=8)
            p = figdir / "fig02_stage_09e_psf_boundary_scene_agreement.png"
            fig.savefig(p, dpi=170)
            plt.close(fig)
            idx.append({"plot_path": str(p), "source_csv": str(boundary_csv), "description": "Boundary/scene PSF delta vs nearest", "created_time_utc": utc_now()})
    gdf = pd.DataFrame(gap)
    if not gdf.empty:
        g = gdf[(gdf["policy"] == "A_inclusive_binary") & (gdf["threshold"].astype(str) == "0.50")].copy()
        if not g.empty:
            order = ["K0_nearest", "K1_box_3x3", "K3_box_7x7", "K5_gaussian_sigma_1p25_cell_radius_5"]
            g = g.set_index("kernel_name").reindex(order).reset_index()
            labels = ["nearest", "box 3x3", "box 7x7", "gauss 1.25"]
            for col in ["meteosat_agreement", "goes_agreement", "east_asia_agreement", "best_non_meteosat_minus_meteosat", "delta_gap_vs_nearest"]:
                g[col] = pd.to_numeric(g[col], errors="coerce")
            fig, axes = plt.subplots(2, 1, figsize=(9.5, 7.0), constrained_layout=True, sharex=True)
            x = np.arange(len(g))
            axes[0].plot(x, g["meteosat_agreement"], marker="o", label="Meteosat", color="#e76f51")
            axes[0].plot(x, g["goes_agreement"], marker="o", label="GOES", color="#386fa4")
            axes[0].plot(x, g["east_asia_agreement"], marker="o", label="East Asia", color="#2a9d8f")
            axes[0].set_ylabel("agreement")
            axes[0].set_title("Stage 09E regional agreement by PSF kernel, Policy A")
            axes[0].grid(axis="y", alpha=0.25)
            axes[0].legend(ncol=3, fontsize=9)
            axes[1].bar(x, g["best_non_meteosat_minus_meteosat"], color="#7a5195", label="gap")
            axes[1].plot(x, g["delta_gap_vs_nearest"], marker="o", color="#e76f51", label="delta gap vs nearest")
            axes[1].axhline(0, color="#555555", linewidth=0.8)
            axes[1].set_ylabel("gap / delta")
            axes[1].set_xticks(x)
            axes[1].set_xticklabels(labels)
            axes[1].grid(axis="y", alpha=0.25)
            axes[1].legend(fontsize=9)
            p = figdir / "fig03_stage_09e_psf_meteosat_gap.png"
            fig.savefig(p, dpi=170)
            plt.close(fig)
            idx.append({"plot_path": str(p), "source_csv": str(gap_csv), "description": "Regional agreement and Meteosat gap by kernel", "created_time_utc": utc_now()})
    return idx


def write_psf_report(out: Path, by_kernel: list[dict[str, Any]], boundary: list[dict[str, Any]], gap: list[dict[str, Any]], warnings: list[dict[str, Any]], outputs: list[Path]) -> Path:
    main = [r for r in by_kernel if r.get("threshold") in {"0.50", "argmax"}]
    nearest = {r["policy"]: sf(r.get("agreement_weighted")) for r in main if r.get("kernel_name") == "K0_nearest"}
    for r in main:
        r["delta_agreement_vs_nearest"] = sf(r.get("agreement_weighted")) - nearest.get(r.get("policy"), math.nan)
    best = sorted(main, key=lambda r: sf(r.get("delta_agreement_vs_nearest"), -999), reverse=True)[:8]
    best_a = max([r for r in main if r.get("policy") == "A_inclusive_binary"], key=lambda r: sf(r.get("delta_agreement_vs_nearest"), -999), default={})
    best_b = max([r for r in main if r.get("policy") == "B_high_confidence_only"], key=lambda r: sf(r.get("delta_agreement_vs_nearest"), -999), default={})
    best_c = max([r for r in main if r.get("policy") == "C_uncertainty_aware_3class"], key=lambda r: sf(r.get("delta_agreement_vs_nearest"), -999), default={})
    boundary_k0 = first_row(boundary, boundary_scene="boundary_or_broken_cloud", kernel_name="K0_nearest", policy="A_inclusive_binary", threshold="0.50")
    boundary_best = max(
        [r for r in boundary if r.get("boundary_scene") == "boundary_or_broken_cloud" and r.get("policy") == "A_inclusive_binary" and r.get("threshold") == "0.50"],
        key=lambda r: sf(r.get("agreement_weighted"), -999),
        default={},
    )
    non_boundary_k0 = first_row(boundary, boundary_scene="non_boundary", kernel_name="K0_nearest", policy="A_inclusive_binary", threshold="0.50")
    non_boundary_best = max(
        [r for r in boundary if r.get("boundary_scene") == "non_boundary" and r.get("policy") == "A_inclusive_binary" and r.get("threshold") == "0.50"],
        key=lambda r: sf(r.get("agreement_weighted"), -999),
        default={},
    )
    gap_k0 = first_row(gap, kernel_name="K0_nearest", policy="A_inclusive_binary", threshold="0.50")
    gap_best_kernel = first_row(gap, kernel_name=best_a.get("kernel_name", ""), policy="A_inclusive_binary", threshold="0.50")
    lines = [
        "# Stage 09E PSF-like EPIC-view fused cloud-mask diagnostics",
        "",
        f"- Generated UTC: `{utc_now()}`",
        "- Scope: existing Stage 09D March 2024 samples only; no Stage 05/06 rerun; no fusion v2.",
        "- Units: agreement/F1/IoU/Brier/MAE/cloud fractions are unitless fractions; kernel radius/sigma are in 0.05-degree grid cells.",
        "- Important limitation: this is a PSF-like footprint-aware sensitivity experiment using normalized box/Gaussian kernels. It is not an official reproduction of the EPIC L2 Composite PSF convolution.",
        "",
        "## Direct Answers",
        f"1. Policy A 最大 PSF-like agreement 提升为 `{fmt(best_a.get('delta_agreement_vs_nearest'))}`，来自 `{best_a.get('kernel_name', 'NA')}`：nearest `{fmt(nearest.get('A_inclusive_binary'))}` -> `{fmt(best_a.get('agreement_weighted'))}`。",
        f"2. Policy B 最大提升为 `{fmt(best_b.get('delta_agreement_vs_nearest'))}`，Policy C 最大提升为 `{fmt(best_c.get('delta_agreement_vs_nearest'))}`。三种 policy 都是约 0.01 量级，不支持“空间代表性单独解释 60%-80% agreement”的说法。",
        "3. 3x3/7x7 box 与 Gaussian sigma=1.25 的方向一致，但本次 pilot 中 7x7 box 提升最大；这仍是 PSF-like sensitivity，不是 official EPIC PSF reproduction。",
        f"4. 改善主要集中在 boundary/broken-cloud：Policy A boundary/broken-cloud `{fmt(boundary_k0.get('agreement_weighted'))}` -> `{fmt(boundary_best.get('agreement_weighted'))}`，而 non-boundary `{fmt(non_boundary_k0.get('agreement_weighted'))}` -> `{fmt(non_boundary_best.get('agreement_weighted'))}`，几乎不变。",
        f"5. Meteosat gap 没有缩小，反而从 `{fmt(gap_k0.get('best_non_meteosat_minus_meteosat'))}` 到 `{fmt(gap_best_kernel.get('best_non_meteosat_minus_meteosat'))}`；因此 Meteosat 低 agreement 不能主要归因于 footprint/PSF 空间代表性。",
        "6. 结论：PSF-like aggregation 能解释一小部分云边界/碎云 mismatch，但当前主要矛盾仍更可能在 source/product semantics、source selection 和区域/source-family 差异。",
        "",
        "## Top Kernel Deltas",
        md_table(best, ["kernel_name", "policy", "threshold", "agreement_weighted", "delta_agreement_vs_nearest", "n_valid_total"], 20),
        "",
        "## Boundary / Scene Contrast",
        md_table([r for r in boundary if r.get("policy") == "A_inclusive_binary" and r.get("threshold") == "0.50"], ["boundary_scene", "kernel_name", "agreement_weighted", "n_valid_total"], 40),
        "",
        "## Meteosat Gap",
        md_table([r for r in gap if r.get("policy") == "A_inclusive_binary" and r.get("threshold") == "0.50"], ["kernel_name", "meteosat_agreement", "goes_agreement", "east_asia_agreement", "best_non_meteosat_minus_meteosat", "delta_gap_vs_nearest"], 40),
        "",
        "## Quality Control",
        f"- Warning rows: `{len(warnings)}`.",
        "- Missing variables or failed samples are warning rows and do not stop the whole run.",
        "",
        "## Traceability",
    ]
    lines.extend(f"- `{p}`" for p in outputs)
    path = out / "reports" / "stage_09e_psf_aware_epic_view_report_cn.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_selqc(args: argparse.Namespace, manifest: list[dict[str, Any]]) -> Path:
    out = Path(args.selqc_output_dir)
    ensure_dirs(out, ["00_definition_audit", "01_valid_count_consistency", "02_oracle_granularity", "03_common_valid_comparison", "04_figures", "reports", "logs"])
    consistency_rows: list[dict[str, Any]] = []
    oracle_rows: list[dict[str, Any]] = []
    common_rows: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    definition_rows = [
        {"field": "best_available_agreement", "original_stage09d_definition": "sample + pixel_group level oracle: choose source with best aggregate metric within that row", "stage09e_qc_status": "not pixel-level oracle"},
        {"field": "selected_is_best_fraction", "original_stage09d_definition": "pixelwise: selected is counted best when selected is correct or no available source is correct", "stage09e_qc_status": "valid but should not be mixed with group-level regret without explanation"},
        {"field": "valid_source_count", "original_stage09d_definition": "Stage 06 fused valid-count map sampled nearest to EPIC pixels", "stage09e_qc_status": "checked against policy-specific available source count"},
    ]
    write_csv(out / "00_definition_audit" / "stage_09e_selqc_selected_is_best_definition_audit.csv", definition_rows)
    for idx, row in enumerate(manifest, 1):
        print(f"[stage_09e_selqc] {idx}/{len(manifest)} {row['sample_id']}", flush=True)
        try:
            ctx = d09d.sample_context(row)
            scene = scene_boundary(ctx, "A_inclusive_binary")
            for policy_name in POLICIES:
                epic_cls, epic_valid = epic_policy(policy_name, ctx)
                source_cls, source_valid, source_warnings = source_policy_samples(ctx, policy_name)
                warnings.extend({"sample_id": row["sample_id"], "module": "selqc_source_load", "warning": w} for w in source_warnings)
                available_count = np.zeros(ctx["epic"]["lat"].shape, dtype=np.int16)
                for valid in source_valid.values():
                    available_count += (valid & epic_valid).astype(np.int16)
                selected_cls, selected_valid = selected_source_class(ctx, source_cls, source_valid)
                base = epic_valid & selected_valid
                groups = pixel_groups(ctx, scene, source_valid, base)
                for bin_name, mask in {
                    "ALL_VALID": base,
                    "valid_source_count_1": base & (ctx["valid_count"] == 1),
                    "valid_source_count_ge4": base & (ctx["valid_count"] >= 4),
                }.items():
                    n = int(np.count_nonzero(mask))
                    if n == 0:
                        continue
                    diff = available_count.astype(np.float32) - ctx["valid_count"].astype(np.float32)
                    consistency_rows.append(
                        {
                            "sample_id": row["sample_id"],
                            "policy": policy_name,
                            "pixel_group": bin_name,
                            "n_valid": n,
                            "stage06_valid_source_count_mean": float(np.nanmean(ctx["valid_count"][mask])),
                            "available_source_count_recomputed_mean": float(np.nanmean(available_count[mask])),
                            "equal_count_fraction": float(np.mean(diff[mask] == 0)),
                            "available_gt_stage06_fraction": float(np.mean(diff[mask] > 0)),
                            "available_lt_stage06_fraction": float(np.mean(diff[mask] < 0)),
                        }
                    )
                for group_name, group_mask in groups.items():
                    valid_current = group_mask & selected_valid
                    if not np.any(valid_current):
                        continue
                    current = metric_row(epic_cls, selected_cls, valid_current, policy_name)
                    any_correct = np.zeros(valid_current.shape, dtype=bool)
                    for source, cls in source_cls.items():
                        any_correct |= valid_current & source_valid[source] & (cls == epic_cls)
                    pixel_oracle_agreement = int(np.count_nonzero(any_correct & valid_current)) / int(np.count_nonzero(valid_current))
                    source_metrics = []
                    for source, cls in source_cls.items():
                        valid = group_mask & source_valid[source] & epic_valid
                        if np.any(valid):
                            sm = metric_row(epic_cls, cls, valid, policy_name)
                            sm["source_name"] = source
                            source_metrics.append(sm)
                    best = max(source_metrics, key=lambda r: sf(r.get("agreement"), -1)) if source_metrics else {}
                    oracle_rows.append(
                        {
                            "sample_id": row["sample_id"],
                            "policy": policy_name,
                            "pixel_group": group_name,
                            "n_valid": current.get("n_valid"),
                            "current_selected_agreement": current.get("agreement"),
                            "sample_group_level_best_source": best.get("source_name", ""),
                            "sample_group_level_best_agreement": best.get("agreement", math.nan),
                            "pixel_level_oracle_agreement": pixel_oracle_agreement,
                            "sample_group_regret": sf(best.get("agreement")) - sf(current.get("agreement")),
                            "pixel_oracle_regret": pixel_oracle_agreement - sf(current.get("agreement")),
                        }
                    )
                for set_name, sources in SOURCE_SETS.items():
                    if not all(s in source_cls for s in sources):
                        continue
                    for group_name in ["ALL_VALID", "valid_source_count_ge4", "selected_MeteosatIODC", "FY4B_and_IODC_both_available", "Himawari_and_IODC_both_available", "Meteosat0deg_and_GOES16_both_available"]:
                        if group_name not in groups:
                            continue
                        common = groups[group_name] & epic_valid
                        for source in sources:
                            common &= source_valid[source]
                        if not np.any(common):
                            continue
                        for source in sources:
                            m = metric_row(epic_cls, source_cls[source], common, policy_name)
                            common_rows.append({**m, "sample_id": row["sample_id"], "policy": policy_name, "pixel_group": group_name, "source_set": set_name, "source_name": source, "n_common_valid": m.get("n_valid")})
        except Exception as exc:
            warnings.append({"sample_id": row.get("sample_id", ""), "module": "selqc", "warning": f"{type(exc).__name__}:{exc}", "traceback": traceback.format_exc()})
    consistency_csv = out / "01_valid_count_consistency" / "stage_09e_selqc_valid_count_consistency.csv"
    write_csv(consistency_csv, consistency_rows)
    consistency_summary_csv = out / "01_valid_count_consistency" / "stage_09e_selqc_valid_count_consistency_summary.csv"
    write_csv(
        consistency_summary_csv,
        summarize_custom_rows(
            consistency_rows,
            ["policy", "pixel_group"],
            [
                "stage06_valid_source_count_mean",
                "available_source_count_recomputed_mean",
                "equal_count_fraction",
                "available_gt_stage06_fraction",
                "available_lt_stage06_fraction",
            ],
            "n_valid",
        ),
    )
    oracle_csv = out / "02_oracle_granularity" / "stage_09e_selqc_oracle_granularity_comparison.csv"
    write_csv(oracle_csv, oracle_rows)
    oracle_summary_csv = out / "02_oracle_granularity" / "stage_09e_selqc_oracle_granularity_summary.csv"
    write_csv(
        oracle_summary_csv,
        summarize_custom_rows(
            oracle_rows,
            ["policy", "pixel_group"],
            [
                "current_selected_agreement",
                "sample_group_level_best_agreement",
                "pixel_level_oracle_agreement",
                "sample_group_regret",
                "pixel_oracle_regret",
            ],
            "n_valid",
        ),
    )
    common_csv = out / "03_common_valid_comparison" / "stage_09e_selqc_common_valid_source_comparison.csv"
    write_csv(common_csv, common_rows)
    common_summary_csv = out / "03_common_valid_comparison" / "stage_09e_selqc_common_valid_source_comparison_summary.csv"
    write_csv(common_summary_csv, summarize_metric_rows(common_rows, ["policy", "pixel_group", "source_set", "source_name"], weight="n_common_valid"))
    warnings_csv = out / "logs" / "stage_09e_selqc_warnings.csv"
    write_csv(warnings_csv, warnings)
    plot_index = make_selqc_figures(out, consistency_summary_csv, oracle_summary_csv, common_summary_csv)
    plot_index_csv = out / "04_figures" / "stage_09e_selqc_plot_index.csv"
    write_csv(plot_index_csv, plot_index)
    report = write_selqc_report(out, consistency_summary_csv, oracle_summary_csv, common_summary_csv, warnings, [consistency_csv, consistency_summary_csv, oracle_csv, oracle_summary_csv, common_csv, common_summary_csv, plot_index_csv])
    write_run_manifest(
        out / "logs" / "stage_09e_selqc_manifest.json",
        canonical_stage_id=STAGE_ID,
        script_path=Path(__file__),
        input_paths=[Path(args.stage09d_dir)],
        output_paths=[consistency_csv, consistency_summary_csv, oracle_csv, oracle_summary_csv, common_csv, common_summary_csv, plot_index_csv, report],
        filters=["existing Stage 09D March 2024 samples only", "policy-specific source validity", "same-pixel common-valid comparison"],
        unit_conversions=[],
        row_counts={"samples": len(manifest), "consistency_rows": len(consistency_rows), "oracle_rows": len(oracle_rows), "common_rows": len(common_rows), "warnings": len(warnings)},
        warnings=warnings,
    )
    return report


def make_selqc_figures(out: Path, consistency_summary_csv: Path, oracle_summary_csv: Path, common_summary_csv: Path) -> list[dict[str, str]]:
    idx: list[dict[str, str]] = []
    c = pd.read_csv(consistency_summary_csv)
    if not c.empty and "equal_count_fraction_weighted" in c:
        c = c.copy()
        c["mismatch_fraction"] = 1 - pd.to_numeric(c["equal_count_fraction_weighted"], errors="coerce")
        c["available_gt_stage06_fraction_weighted"] = pd.to_numeric(c["available_gt_stage06_fraction_weighted"], errors="coerce")
        c["available_lt_stage06_fraction_weighted"] = pd.to_numeric(c["available_lt_stage06_fraction_weighted"], errors="coerce")
        group_order = ["ALL_VALID", "valid_source_count_1", "valid_source_count_ge4"]
        policy_order = ["A_inclusive_binary", "B_high_confidence_only", "C_uncertainty_aware_3class"]
        policy_short = {"A_inclusive_binary": "Policy A", "B_high_confidence_only": "Policy B", "C_uncertainty_aware_3class": "Policy C"}
        colors = {"A_inclusive_binary": "#386fa4", "B_high_confidence_only": "#e76f51", "C_uncertainty_aware_3class": "#2a9d8f"}
        fig, ax = plt.subplots(figsize=(9.5, 5.2), constrained_layout=True)
        x = np.arange(len(group_order))
        width = 0.24
        floor = 1e-7
        for i, policy in enumerate(policy_order):
            sub = c[c["policy"] == policy].set_index("pixel_group").reindex(group_order)
            vals = sub["mismatch_fraction"].clip(lower=floor)
            ax.bar(x + (i - 1) * width, vals, width=width, label=policy_short[policy], color=colors[policy])
            for xpos, raw in zip(x + (i - 1) * width, sub["mismatch_fraction"]):
                if pd.notna(raw) and raw > 0.001:
                    ax.text(xpos, raw * 1.15, f"{raw:.3f}", ha="center", va="bottom", fontsize=8)
        ax.set_yscale("log")
        ax.set_ylabel("1 - equal_count_fraction (log scale)")
        ax.set_title("Stage 09E valid-source-count mismatch fraction by policy")
        ax.set_xticks(x)
        ax.set_xticklabels(["All valid", "valid count = 1", "valid count >= 4"])
        ax.grid(axis="y", alpha=0.25, which="both")
        ax.legend(ncol=3, fontsize=9)
        p = out / "04_figures" / "fig01_stage_09e_selqc_valid_count_consistency.png"
        fig.savefig(p, dpi=170)
        plt.close(fig)
        idx.append({"plot_path": str(p), "source_csv": str(consistency_summary_csv), "description": "valid_source_count mismatch fraction by policy", "created_time_utc": utc_now()})

        fig, ax = plt.subplots(figsize=(10.5, 5.2), constrained_layout=True)
        labels = []
        gt_vals = []
        lt_vals = []
        for policy in policy_order:
            sub = c[c["policy"] == policy].set_index("pixel_group").reindex(group_order)
            for group in group_order:
                labels.append(f"{policy_short[policy]}\n{group.replace('valid_source_count_', 'vc=')}")
                gt_vals.append(float(sub.loc[group, "available_gt_stage06_fraction_weighted"]))
                lt_vals.append(float(sub.loc[group, "available_lt_stage06_fraction_weighted"]))
        x2 = np.arange(len(labels))
        ax.bar(x2, gt_vals, color="#2a9d8f", label="available > Stage06 count")
        ax.bar(x2, lt_vals, bottom=gt_vals, color="#e76f51", label="available < Stage06 count")
        ax.set_ylabel("fraction")
        ax.set_title("Stage 09E valid-source-count mismatch direction")
        ax.set_xticks(x2)
        ax.set_xticklabels(labels, rotation=35, ha="right", fontsize=8)
        ax.set_ylim(0, 1)
        ax.grid(axis="y", alpha=0.25)
        ax.legend(fontsize=9)
        p = out / "04_figures" / "fig01b_stage_09e_selqc_valid_count_mismatch_direction.png"
        fig.savefig(p, dpi=170)
        plt.close(fig)
        idx.append({"plot_path": str(p), "source_csv": str(consistency_summary_csv), "description": "valid_source_count mismatch direction by policy", "created_time_utc": utc_now()})
    o = pd.read_csv(oracle_summary_csv)
    if not o.empty and "sample_group_regret_weighted" in o:
        groups = ["ALL_VALID", "valid_source_count_1", "valid_source_count_ge4", "selected_MeteosatIODC"]
        a = o[(o["policy"] == "A_inclusive_binary") & (o["pixel_group"].isin(groups))].copy()
        a = a.set_index("pixel_group").reindex(groups).reset_index()
        for col in ["current_selected_agreement_weighted", "sample_group_level_best_agreement_weighted", "pixel_level_oracle_agreement_weighted", "sample_group_regret_weighted", "pixel_oracle_regret_weighted"]:
            a[col] = pd.to_numeric(a[col], errors="coerce")
        labels = ["All valid", "valid count = 1", "valid count >= 4", "selected IODC"]
        x = np.arange(len(a))
        fig, axes = plt.subplots(2, 1, figsize=(10.0, 7.2), constrained_layout=True, sharex=True)
        width = 0.25
        axes[0].bar(x - width, a["current_selected_agreement_weighted"], width=width, label="current selected", color="#386fa4")
        axes[0].bar(x, a["sample_group_level_best_agreement_weighted"], width=width, label="sample-group oracle", color="#e76f51")
        axes[0].bar(x + width, a["pixel_level_oracle_agreement_weighted"], width=width, label="pixel oracle", color="#2a9d8f")
        axes[0].set_ylabel("agreement")
        axes[0].set_title("Stage 09E oracle granularity, Policy A")
        axes[0].grid(axis="y", alpha=0.25)
        axes[0].legend(ncol=3, fontsize=9)
        axes[1].bar(x - width / 2, a["sample_group_regret_weighted"], width=width, label="sample-group regret", color="#e76f51")
        axes[1].bar(x + width / 2, a["pixel_oracle_regret_weighted"], width=width, label="pixel-oracle regret", color="#2a9d8f")
        axes[1].axhline(0, color="#555555", linewidth=0.8)
        axes[1].set_ylabel("agreement regret")
        axes[1].set_xticks(x)
        axes[1].set_xticklabels(labels)
        axes[1].grid(axis="y", alpha=0.25)
        axes[1].legend(ncol=2, fontsize=9)
        p = out / "04_figures" / "fig02_stage_09e_selqc_oracle_granularity.png"
        fig.savefig(p, dpi=170)
        plt.close(fig)
        idx.append({"plot_path": str(p), "source_csv": str(oracle_summary_csv), "description": "oracle granularity comparison", "created_time_utc": utc_now()})
    s = pd.read_csv(common_summary_csv)
    if not s.empty and "agreement_weighted" in s:
        key_rows = [
            ("valid_source_count_ge4", "valid_count_ge4_all_available", ["FY4B", "Meteosat-IODC"], "vc>=4: FY4B vs IODC"),
            ("selected_MeteosatIODC", "FY4B_and_IODC", ["FY4B", "Meteosat-IODC"], "selected IODC: FY4B vs IODC"),
            ("ALL_VALID", "Meteosat0deg_and_GOES16", ["GOES-16", "Meteosat-0deg"], "All valid: GOES16 vs Met0"),
            ("ALL_VALID", "Himawari_and_IODC", ["Himawari-9", "Meteosat-IODC"], "All valid: Himawari vs IODC"),
        ]
        rows = []
        for pixel_group, source_set, sources, label in key_rows:
            sub = s[(s["policy"] == "A_inclusive_binary") & (s["pixel_group"] == pixel_group) & (s["source_set"] == source_set)]
            if sub.empty:
                continue
            row = {"comparison": label}
            for source in sources:
                hit = sub[sub["source_name"] == source]
                row[source] = pd.to_numeric(hit["agreement_weighted"], errors="coerce").iloc[0] if not hit.empty else np.nan
            row["sources"] = sources
            rows.append(row)
        if rows:
            fig, ax = plt.subplots(figsize=(10.0, 5.4), constrained_layout=True)
            y = np.arange(len(rows))
            color_map = {"FY4B": "#2a9d8f", "Meteosat-IODC": "#e76f51", "GOES-16": "#386fa4", "Meteosat-0deg": "#7a5195", "Himawari-9": "#8ab17d"}
            for i, row in enumerate(rows):
                vals = [(src, row.get(src, np.nan)) for src in row["sources"]]
                finite = [(src, val) for src, val in vals if pd.notna(val)]
                if len(finite) == 2:
                    ax.plot([finite[0][1], finite[1][1]], [i, i], color="#999999", linewidth=1.3, zorder=1)
                for src, val in finite:
                    ax.scatter(val, i, s=90, color=color_map.get(src, "#386fa4"), label=src, zorder=2)
                    xoff = 0.010 if val < 0.72 else -0.010
                    ha = "left" if val < 0.72 else "right"
                    ax.text(
                        val + xoff,
                        i,
                        f"{src} {val:.3f}",
                        va="center",
                        ha=ha,
                        fontsize=8,
                        bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.82, "pad": 1.0},
                    )
            ax.set_yticks(y)
            ax.set_yticklabels([r["comparison"] for r in rows])
            ax.set_xlim(0.3, 0.85)
            ax.set_xlabel("agreement")
            ax.set_title("Stage 09E common-valid same-pixel source contrasts, Policy A")
            ax.grid(axis="x", alpha=0.25)
            handles, labels = ax.get_legend_handles_labels()
            unique = dict(zip(labels, handles))
            ax.legend(unique.values(), unique.keys(), ncol=5, fontsize=8, loc="lower center", bbox_to_anchor=(0.5, -0.18))
            p = out / "04_figures" / "fig03_stage_09e_selqc_common_valid_source_comparison.png"
            fig.savefig(p, dpi=170)
            plt.close(fig)
            idx.append({"plot_path": str(p), "source_csv": str(common_summary_csv), "description": "common-valid same-pixel source contrasts", "created_time_utc": utc_now()})
    return idx


def write_selqc_report(out: Path, consistency_summary_csv: Path, oracle_summary_csv: Path, common_summary_csv: Path, warnings: list[dict[str, Any]], outputs: list[Path]) -> Path:
    consistency = read_csv(consistency_summary_csv)
    oracle = read_csv(oracle_summary_csv)
    common = read_csv(common_summary_csv)
    vc1_a = first_row(consistency, policy="A_inclusive_binary", pixel_group="valid_source_count_1")
    vc4_a = first_row(consistency, policy="A_inclusive_binary", pixel_group="valid_source_count_ge4")
    vc4_b = first_row(consistency, policy="B_high_confidence_only", pixel_group="valid_source_count_ge4")
    all_a = first_row(oracle, policy="A_inclusive_binary", pixel_group="ALL_VALID")
    iodc_a = first_row(oracle, policy="A_inclusive_binary", pixel_group="selected_MeteosatIODC")
    vc4_oracle_a = first_row(oracle, policy="A_inclusive_binary", pixel_group="valid_source_count_ge4")
    vc4_fy4b_a = first_row(common, policy="A_inclusive_binary", pixel_group="valid_source_count_ge4", source_set="valid_count_ge4_all_available", source_name="FY4B")
    vc4_iodc_a = first_row(common, policy="A_inclusive_binary", pixel_group="valid_source_count_ge4", source_set="valid_count_ge4_all_available", source_name="Meteosat-IODC")
    iodc_fy4b_a = first_row(common, policy="A_inclusive_binary", pixel_group="selected_MeteosatIODC", source_set="FY4B_and_IODC", source_name="FY4B")
    iodc_iodc_a = first_row(common, policy="A_inclusive_binary", pixel_group="selected_MeteosatIODC", source_set="FY4B_and_IODC", source_name="Meteosat-IODC")
    lines = [
        "# Stage 09E SEL-QC common-valid report",
        "",
        f"- Generated UTC: `{utc_now()}`",
        "- Scope: existing 53 Stage 09D March 2024 samples only; diagnostic QC, no production fusion change.",
        "- Units: agreement/F1/IoU/regret/fractions are unitless fractions.",
        "",
        "## Direct Answers",
        "1. 原 Stage 09D SEL 的 best_available 是 sample + pixel_group level aggregate oracle，不是逐像元 oracle；因此它可以在 `valid_source_count=1` 里产生非零 group-level regret。",
        f"2. 对 Policy A 的 `valid_source_count=1`，Stage 06 valid_count 与 recomputed available count 几乎同口径：equal fraction `{fmt(vc1_a.get('equal_count_fraction_weighted'))}`，pixel-level oracle regret 约为 `{fmt(first_row(oracle, policy='A_inclusive_binary', pixel_group='valid_source_count_1').get('pixel_oracle_regret_weighted'))}`，接近 0；原非零 regret 主要来自 aggregate oracle 粒度，而不是逐像元唯一源仍可替代。",
        f"3. 对 Policy B 的 `valid_source_count_ge4`，available count 明显低于 Stage 06 count：equal fraction `{fmt(vc4_b.get('equal_count_fraction_weighted'))}`，available_lt_stage06 `{fmt(vc4_b.get('available_lt_stage06_fraction_weighted'))}`；这是 policy-specific 有效像元集合变化造成的口径差异。",
        f"4. ALL_VALID Policy A group-level regret 为 `{fmt(all_a.get('sample_group_regret_weighted'))}`，selected_MeteosatIODC 为 `{fmt(iodc_a.get('sample_group_regret_weighted'))}`，valid_source_count_ge4 为 `{fmt(vc4_oracle_a.get('sample_group_regret_weighted'))}`，说明 IODC 与多源重叠区 selection regret 仍高。",
        f"5. common-valid same-pixel 下，valid_count_ge4 仍是 FY4B 高于 IODC：FY4B agreement `{fmt(vc4_fy4b_a.get('agreement_weighted'))}` vs IODC `{fmt(vc4_iodc_a.get('agreement_weighted'))}`。",
        f"6. selected_MeteosatIODC 的 common-valid FY4B vs IODC 对比也仍显示 FY4B 更高：FY4B `{fmt(iodc_fy4b_a.get('agreement_weighted'))}` vs IODC `{fmt(iodc_iodc_a.get('agreement_weighted'))}`。",
        "7. 论文主文优先引用 common-valid same-pixel 和 oracle-granularity QC 后的结论；原 SEL group-level oracle、selected_is_best_fraction 可放补充材料，并必须解释粒度。",
        "",
        "## valid_source_count Consistency",
        md_table(consistency, ["policy", "pixel_group", "n_valid_total", "stage06_valid_source_count_mean_weighted", "available_source_count_recomputed_mean_weighted", "equal_count_fraction_weighted", "available_gt_stage06_fraction_weighted", "available_lt_stage06_fraction_weighted"], 40),
        "",
        "## Oracle Granularity",
        md_table(oracle, ["policy", "pixel_group", "n_valid_total", "current_selected_agreement_weighted", "sample_group_level_best_agreement_weighted", "pixel_level_oracle_agreement_weighted", "sample_group_regret_weighted", "pixel_oracle_regret_weighted"], 50),
        "",
        "## Common-valid Same-pixel Source Comparison",
        md_table(common, ["policy", "pixel_group", "source_set", "source_name", "n_common_valid_total", "agreement_weighted", "f1_cloud_weighted", "iou_cloud_weighted", "cloud_fraction_bias_weighted"], 80),
        "",
        "## Quality Control",
        f"- Warning rows: `{len(warnings)}`.",
        "- Missing source variables are recorded as warnings and skipped where needed.",
        "",
        "## Traceability",
    ]
    lines.extend(f"- `{p}`" for p in outputs)
    path = out / "reports" / "stage_09e_sel_qc_common_valid_report_cn.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage09d-dir", default=str(DEFAULT_STAGE09D_DIR))
    ap.add_argument("--psf-output-dir", default=str(DEFAULT_PSF_OUT))
    ap.add_argument("--selqc-output-dir", default=str(DEFAULT_SELQC_OUT))
    ap.add_argument("--only", choices=["all", "selqc", "psf_fused", "report", "figures"], default="all")
    ap.add_argument("--kernel-set", choices=["pilot", "full"], default="pilot")
    ap.add_argument("--min-valid-weight", type=float, default=0.50)
    ap.add_argument("--max-samples", type=int, default=0)
    return ap.parse_args()


def run_report_only(args: argparse.Namespace) -> list[Path]:
    psf = Path(args.psf_output_dir)
    sel = Path(args.selqc_output_dir)
    psf_by_kernel = read_csv(psf / "01_fused_psf_metrics" / "stage_09e_fused_psf_metrics_by_kernel.csv")
    psf_boundary = read_csv(psf / "05_boundary_scene_psf" / "stage_09e_psf_metrics_by_boundary_scene.csv")
    psf_gap = read_csv(psf / "06_meteosat_gap_psf" / "stage_09e_psf_meteosat_gap_by_kernel.csv")
    psf_warnings = read_csv(psf / "logs" / "stage_09e_psf_warnings.csv")
    psf_plot_index = make_psf_figures(
        psf,
        psf_by_kernel,
        psf_boundary,
        psf_gap,
        psf / "01_fused_psf_metrics" / "stage_09e_fused_psf_metrics_by_kernel.csv",
        psf / "05_boundary_scene_psf" / "stage_09e_psf_metrics_by_boundary_scene.csv",
        psf / "06_meteosat_gap_psf" / "stage_09e_psf_meteosat_gap_by_kernel.csv",
    )
    write_csv(psf / "08_figures" / "stage_09e_psf_plot_index.csv", psf_plot_index)
    sel_plot_index = make_selqc_figures(
        sel,
        sel / "01_valid_count_consistency" / "stage_09e_selqc_valid_count_consistency_summary.csv",
        sel / "02_oracle_granularity" / "stage_09e_selqc_oracle_granularity_summary.csv",
        sel / "03_common_valid_comparison" / "stage_09e_selqc_common_valid_source_comparison_summary.csv",
    )
    write_csv(sel / "04_figures" / "stage_09e_selqc_plot_index.csv", sel_plot_index)
    if args.only == "figures":
        return [psf / "08_figures" / "stage_09e_psf_plot_index.csv", sel / "04_figures" / "stage_09e_selqc_plot_index.csv"]
    psf_report = write_psf_report(
        psf,
        psf_by_kernel,
        psf_boundary,
        psf_gap,
        psf_warnings,
        [
            psf / "00_kernel_definitions" / "stage_09e_kernel_definitions.csv",
            psf / "01_fused_psf_metrics" / "stage_09e_fused_psf_metrics_by_sample.csv",
            psf / "01_fused_psf_metrics" / "stage_09e_fused_psf_metrics_by_kernel.csv",
            psf / "02_group_psf_metrics" / "stage_09e_psf_metrics_by_group.csv",
            psf / "05_boundary_scene_psf" / "stage_09e_psf_metrics_by_boundary_scene.csv",
            psf / "06_meteosat_gap_psf" / "stage_09e_psf_meteosat_gap_by_kernel.csv",
            psf / "08_figures" / "stage_09e_psf_plot_index.csv",
        ],
    )
    sel_report = write_selqc_report(
        sel,
        sel / "01_valid_count_consistency" / "stage_09e_selqc_valid_count_consistency_summary.csv",
        sel / "02_oracle_granularity" / "stage_09e_selqc_oracle_granularity_summary.csv",
        sel / "03_common_valid_comparison" / "stage_09e_selqc_common_valid_source_comparison_summary.csv",
        read_csv(sel / "logs" / "stage_09e_selqc_warnings.csv"),
        [
            sel / "01_valid_count_consistency" / "stage_09e_selqc_valid_count_consistency.csv",
            sel / "01_valid_count_consistency" / "stage_09e_selqc_valid_count_consistency_summary.csv",
            sel / "02_oracle_granularity" / "stage_09e_selqc_oracle_granularity_comparison.csv",
            sel / "02_oracle_granularity" / "stage_09e_selqc_oracle_granularity_summary.csv",
            sel / "03_common_valid_comparison" / "stage_09e_selqc_common_valid_source_comparison.csv",
            sel / "03_common_valid_comparison" / "stage_09e_selqc_common_valid_source_comparison_summary.csv",
            sel / "04_figures" / "stage_09e_selqc_plot_index.csv",
        ],
    )
    return [sel_report, psf_report]


def main() -> None:
    args = parse_args()
    if args.only == "report":
        print("\n".join(str(p) for p in run_report_only(args)), flush=True)
        return
    manifest = [r for r in load_manifest(Path(args.stage09d_dir)) if bool_series(r.get("can_run_sampling"))]
    if args.max_samples:
        manifest = manifest[: args.max_samples]
    reports = []
    if args.only in {"all", "selqc"}:
        reports.append(run_selqc(args, manifest))
    if args.only in {"all", "psf_fused"}:
        reports.append(run_psf(args, manifest))
    print("\n".join(str(p) for p in reports), flush=True)


if __name__ == "__main__":
    main()
