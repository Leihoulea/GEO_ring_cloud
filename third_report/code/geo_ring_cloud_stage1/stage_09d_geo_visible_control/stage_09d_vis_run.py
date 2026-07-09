# -*- coding: utf-8 -*-
"""Stage 09d VIS: GEO-visible/reliable controlled metrics.

Diagnostic only.  This script reads existing Stage 09d products and does not
modify Stage 05/06 fused production logic.
"""
from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import path_config  # noqa: E402
from stage_09d_diagnostic_common import (  # noqa: E402
    ABS_LAT_BINS,
    DEFAULT_STAGE09D_DIR,
    EPIC_VZA_BINS,
    SOURCE_FAMILY,
    SOURCE_ID_TO_NAME,
    SOURCE_NAME_TO_ID,
    SZA_BINS,
    VALID_COUNT_BINS,
    add_condition_if_available,
    bar_plot,
    base_valid_mask,
    bool_series,
    classify,
    d09d,
    describe_valid_context,
    ensure_dirs,
    family_array,
    grouped_bar,
    heatmap,
    load_manifest,
    md_table,
    metric_row,
    nanmean,
    read_csv,
    scene_boundary,
    selected_geo_vza,
    selected_source_array,
    source_samples,
    summarize_metric_rows,
    utc_now,
    weighted_mean,
    write_csv,
    write_json,
    write_run_manifest,
)


DEFAULT_OUT = path_config.RUNS_ROOT / "stage09d_geo_visible_controlled_metrics_202403"
POLICY_ORDER = ["A_inclusive_binary", "B_high_confidence_only", "C_uncertainty_aware_3class"]
PAIR_LIST = [
    ("FY4B", "Himawari-9"),
    ("FY4B", "Meteosat-IODC"),
    ("Himawari-9", "Meteosat-IODC"),
    ("Meteosat-0deg", "GOES-16"),
    ("Meteosat-0deg", "Meteosat-IODC"),
    ("GOES-16", "GOES-18"),
]


MASK_DEFINITIONS = [
    {
        "mask_name": "VIS-0_baseline_current",
        "short_name": "VIS-0",
        "definition": "Stage 09d baseline: EPIC policy-valid earth pixels AND fused valid AND policy-valid GEO class.",
        "units": "boolean mask over EPIC pixels",
        "diagnostic_use": "baseline control",
    },
    {
        "mask_name": "VIS-1_fused_valid_only",
        "short_name": "VIS-1",
        "definition": "VIS-0 plus selected source valid and valid_source_count >= 1.",
        "units": "boolean mask over EPIC pixels",
        "diagnostic_use": "checks complete no-GEO exclusion",
    },
    {
        "mask_name": "VIS-2_lat70_visible",
        "short_name": "VIS-2",
        "definition": "VIS-1 plus abs(latitude) < 70 degrees.",
        "units": "boolean mask over EPIC pixels; latitude in degree",
        "diagnostic_use": "excludes highest-latitude low-reliability region",
    },
    {
        "mask_name": "VIS-3_lat60_visible",
        "short_name": "VIS-3",
        "definition": "VIS-1 plus abs(latitude) < 60 degrees.",
        "units": "boolean mask over EPIC pixels; latitude in degree",
        "diagnostic_use": "stricter latitude/limb control",
    },
    {
        "mask_name": "VIS-4a_reliable_geometry_without_geo_vza",
        "short_name": "VIS-4a",
        "definition": "VIS-1 plus abs(latitude)<70; EPIC VZA<70 if available; SZA<80 if available.",
        "units": "degrees for latitude/VZA/SZA",
        "diagnostic_use": "reliable geometry control without requiring GEO VZA",
    },
    {
        "mask_name": "VIS-4b_reliable_geometry_with_geo_vza_available_only",
        "short_name": "VIS-4b",
        "definition": "VIS-4a plus selected-source GEO VZA finite and <70 degrees.",
        "units": "degrees for GEO VZA",
        "diagnostic_use": "GEO VZA-aware subset where angle files are available",
    },
    {
        "mask_name": "VIS-5_clean_core",
        "short_name": "VIS-5",
        "definition": "VIS-1 plus abs(latitude)<60; EPIC VZA<60 if available; SZA<70 if available; GEO VZA<60 where available; non-boundary and homogeneous scene.",
        "units": "degrees for geometry; categorical cloud scene",
        "diagnostic_use": "clean-core upper-bound diagnostic, not a global product metric",
    },
    {
        "mask_name": "VIS-6_non_boundary_visible",
        "short_name": "VIS-6",
        "definition": "VIS-3 plus boundary_class == non_boundary.",
        "units": "boolean mask over EPIC pixels",
        "diagnostic_use": "visible non-boundary control",
    },
    {
        "mask_name": "VIS-7_boundary_visible",
        "short_name": "VIS-7",
        "definition": "VIS-3 plus boundary_class != non_boundary or broken_cloud scene.",
        "units": "boolean mask over EPIC pixels",
        "diagnostic_use": "visible boundary/broken-cloud contrast",
    },
]


def mask_set(ctx: dict[str, Any], policy_name: str, scene: dict[str, np.ndarray], geo_vza: np.ndarray) -> tuple[dict[str, np.ndarray], list[str]]:
    base, _, _ = base_valid_mask(ctx, policy_name)
    selected_valid = np.isin(ctx["selected_source"], list(SOURCE_ID_TO_NAME.keys()))
    count_valid = np.isfinite(ctx["valid_count"]) & (ctx["valid_count"] >= 1)
    vis1 = base & selected_valid & count_valid
    lat_abs = np.abs(ctx["epic"]["lat"])
    vis4a = vis1 & (lat_abs < 70)
    notes = []
    vis4a, status = add_condition_if_available(vis4a, ctx["epic"]["epic_vza"], 70)
    notes.append(f"{policy_name}:epic_vza_70:{status}")
    vis4a, status = add_condition_if_available(vis4a, ctx["epic"]["sza"], 80)
    notes.append(f"{policy_name}:sza_80:{status}")
    vis4b = vis4a & np.isfinite(geo_vza) & (geo_vza < 70)
    vis5 = vis1 & (lat_abs < 60)
    vis5, status = add_condition_if_available(vis5, ctx["epic"]["epic_vza"], 60)
    notes.append(f"{policy_name}:epic_vza_60:{status}")
    vis5, status = add_condition_if_available(vis5, ctx["epic"]["sza"], 70)
    notes.append(f"{policy_name}:sza_70:{status}")
    if np.any(np.isfinite(geo_vza)):
        vis5 = vis5 & np.isfinite(geo_vza) & (geo_vza < 60)
        notes.append(f"{policy_name}:geo_vza_60:applied")
    else:
        notes.append(f"{policy_name}:geo_vza_60:missing_not_applied")
    homogeneous = (scene["scene_type"] == "homogeneous_clear") | (scene["scene_type"] == "homogeneous_cloud")
    non_boundary = scene["boundary_class"] == "non_boundary"
    vis5 = vis5 & non_boundary & homogeneous
    vis3 = vis1 & (lat_abs < 60)
    masks = {
        "VIS-0_baseline_current": base,
        "VIS-1_fused_valid_only": vis1,
        "VIS-2_lat70_visible": vis1 & (lat_abs < 70),
        "VIS-3_lat60_visible": vis3,
        "VIS-4a_reliable_geometry_without_geo_vza": vis4a,
        "VIS-4b_reliable_geometry_with_geo_vza_available_only": vis4b,
        "VIS-5_clean_core": vis5,
        "VIS-6_non_boundary_visible": vis3 & non_boundary,
        "VIS-7_boundary_visible": vis3 & ((~non_boundary) | (scene["scene_type"] == "broken_cloud")),
    }
    return masks, notes


def sample_vis(row: dict[str, Any]) -> dict[str, Any]:
    ctx = d09d.sample_context(row)
    scene = scene_boundary(ctx, "A_inclusive_binary")
    geo_vza, geo_vza_warnings = selected_geo_vza(ctx)
    selected_names = selected_source_array(ctx["selected_source"])
    selected_family = family_array(selected_names)
    lat_bin = classify(np.abs(ctx["epic"]["lat"]), ABS_LAT_BINS)
    epic_vza_bin = classify(ctx["epic"]["epic_vza"], EPIC_VZA_BINS)
    sza_bin = classify(ctx["epic"]["sza"], SZA_BINS)
    valid_count_bin = classify(ctx["valid_count"], VALID_COUNT_BINS)
    source_cls, source_valid, source_warnings = source_samples(ctx)

    policy_rows: list[dict[str, Any]] = []
    retention_rows: list[dict[str, Any]] = []
    strata_rows: list[dict[str, Any]] = []
    pair_rows: list[dict[str, Any]] = []
    warnings = [{"sample_id": row["sample_id"], "module": "vis_geo_vza", "warning": w} for w in geo_vza_warnings + source_warnings]

    for policy_name in POLICY_ORDER:
        base, epic_cls, geo_cls = base_valid_mask(ctx, policy_name)
        masks, notes = mask_set(ctx, policy_name, scene, geo_vza)
        for note in notes:
            if "missing" in note:
                warnings.append({"sample_id": row["sample_id"], "module": "vis_mask", "warning": note})
        baseline_n = max(int(np.count_nonzero(masks["VIS-0_baseline_current"])), 1)
        for mask_name, valid in masks.items():
            n = int(np.count_nonzero(valid))
            retention_rows.append(
                {
                    "sample_id": row["sample_id"],
                    "policy": policy_name,
                    "mask_name": mask_name,
                    "n_valid": n,
                    "baseline_n_valid": int(np.count_nonzero(masks["VIS-0_baseline_current"])),
                    "n_valid_retention_ratio": n / baseline_n,
                    "candidate_group": row.get("candidate_group", ""),
                    "dominant_source": row.get("dominant_source", ""),
                    "geo_vza_available_fraction": float(np.mean(np.isfinite(geo_vza[base]))) if np.any(base) else math.nan,
                }
            )
            m = metric_row(epic_cls, geo_cls, valid, policy_name)
            m.update(
                {
                    "sample_id": row["sample_id"],
                    "mask_name": mask_name,
                    "n_valid_retention_ratio": n / baseline_n,
                    "candidate_group": row.get("candidate_group", ""),
                    "dominant_source": row.get("dominant_source", ""),
                }
            )
            m.update(describe_valid_context(ctx, valid, scene))
            policy_rows.append(m)

            dimensions = [
                ("selected_source", selected_names),
                ("source_family", selected_family),
                ("valid_source_count_bin", valid_count_bin),
                ("boundary_class", scene["boundary_class"]),
                ("scene_type", scene["scene_type"]),
                ("abs_lat_bin", lat_bin),
                ("EPIC_VZA_bin", epic_vza_bin),
                ("SZA_bin", sza_bin),
            ]
            for dim, labels in dimensions:
                for label in sorted(set(labels[valid])):
                    v = valid & (labels == label)
                    if not np.any(v):
                        continue
                    sm = metric_row(epic_cls, geo_cls, v, policy_name)
                    sm.update(
                        {
                            "sample_id": row["sample_id"],
                            "mask_name": mask_name,
                            "strata_dimension": dim,
                            "strata_value": str(label),
                            "candidate_group": row.get("candidate_group", ""),
                            "dominant_source": row.get("dominant_source", ""),
                        }
                    )
                    sm.update(describe_valid_context(ctx, v, scene))
                    strata_rows.append(sm)

        for source_a, source_b in PAIR_LIST:
            if source_a not in source_cls or source_b not in source_cls:
                continue
            cls_a, valid_a = apply_source_policy(source_cls, source_valid, source_a, policy_name)
            cls_b, valid_b = apply_source_policy(source_cls, source_valid, source_b, policy_name)
            for mask_name, mask_valid in masks.items():
                valid = mask_valid & valid_a & valid_b
                n = int(np.count_nonzero(valid))
                if n == 0:
                    continue
                ma = d09d.binary_metrics(epic_cls, cls_a, valid, d09d.POLICIES[policy_name]["positive"])
                mb = d09d.binary_metrics(epic_cls, cls_b, valid, d09d.POLICIES[policy_name]["positive"])
                a_match = valid & (cls_a == epic_cls)
                b_match = valid & (cls_b == epic_cls)
                both_correct = a_match & b_match
                both_wrong = valid & (~a_match) & (~b_match)
                a_only = a_match & (~b_match)
                b_only = b_match & (~a_match)
                disagree = valid & (cls_a != cls_b)
                pair_rows.append(
                    {
                        "sample_id": row["sample_id"],
                        "mask_name": mask_name,
                        "policy": policy_name,
                        "source_A": source_a,
                        "source_B": source_b,
                        "n_overlap_valid": n,
                        "baseline_overlap_n_valid": int(np.count_nonzero(masks["VIS-0_baseline_current"] & valid_a & valid_b)),
                        "n_overlap_retention_ratio": n / max(int(np.count_nonzero(masks["VIS-0_baseline_current"] & valid_a & valid_b)), 1),
                        "source_A_agreement_to_EPIC": ma.get("agreement"),
                        "source_B_agreement_to_EPIC": mb.get("agreement"),
                        "B_minus_A_agreement": mb.get("agreement") - ma.get("agreement"),
                        "A_f1": ma.get("f1_cloud"),
                        "B_f1": mb.get("f1_cloud"),
                        "A_iou": ma.get("iou_cloud"),
                        "B_iou": mb.get("iou_cloud"),
                        "source_disagreement_fraction": int(np.count_nonzero(disagree)) / n,
                        "both_wrong_fraction": int(np.count_nonzero(both_wrong)) / n,
                        "A_only_correct_fraction": int(np.count_nonzero(a_only)) / n,
                        "B_only_correct_fraction": int(np.count_nonzero(b_only)) / n,
                        "both_correct_fraction": int(np.count_nonzero(both_correct)) / n,
                        "candidate_group": row.get("candidate_group", ""),
                        "dominant_source": row.get("dominant_source", ""),
                    }
                )
    return {"policy_rows": policy_rows, "retention_rows": retention_rows, "strata_rows": strata_rows, "pair_rows": pair_rows, "warnings": warnings}


def apply_source_policy(source_cls: dict[str, np.ndarray], source_valid: dict[str, np.ndarray], source: str, policy_name: str) -> tuple[np.ndarray, np.ndarray]:
    policy = d09d.POLICIES[policy_name]
    cls, pv = d09d.apply_policy(source_cls[source], policy["geo"])
    return cls, source_valid[source] & pv


def run(args: argparse.Namespace) -> Path:
    stage09d_dir = Path(args.stage09d_dir)
    out = Path(args.output_dir)
    ensure_dirs(
        out,
        [
            "00_mask_definitions",
            "01_mask_retention",
            "02_policy_metrics",
            "03_group_source_metrics",
            "04_source_pair_metrics",
            "05_meteosat_focus",
            "06_figures",
            "reports",
            "logs",
        ],
    )
    write_csv(out / "00_mask_definitions" / "stage_09d_vis_mask_definitions.csv", MASK_DEFINITIONS)
    manifest = [r for r in load_manifest(stage09d_dir) if bool_series(r.get("can_run_sampling"))]
    if args.max_samples:
        manifest = manifest[: args.max_samples]

    all_policy: list[dict[str, Any]] = []
    all_retention: list[dict[str, Any]] = []
    all_strata: list[dict[str, Any]] = []
    all_pair: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for idx, row in enumerate(manifest, 1):
        print(f"[stage_09d_vis] {idx}/{len(manifest)} {row['sample_id']}", flush=True)
        try:
            result = sample_vis(row)
            all_policy.extend(result["policy_rows"])
            all_retention.extend(result["retention_rows"])
            all_strata.extend(result["strata_rows"])
            all_pair.extend(result["pair_rows"])
            warnings.extend(result["warnings"])
        except Exception as exc:
            import traceback

            warnings.append({"sample_id": row.get("sample_id", ""), "module": "sample_vis", "warning": str(exc), "traceback": traceback.format_exc()})

    retention_sample_csv = out / "01_mask_retention" / "stage_09d_vis_mask_retention_by_sample.csv"
    policy_sample_csv = out / "02_policy_metrics" / "stage_09d_vis_policy_metrics_by_sample.csv"
    strata_csv = out / "03_group_source_metrics" / "stage_09d_vis_metrics_by_strata.csv"
    pair_csv = out / "04_source_pair_metrics" / "stage_09d_vis_source_pair_metrics.csv"
    write_csv(retention_sample_csv, all_retention)
    write_csv(policy_sample_csv, all_policy)
    write_csv(strata_csv, all_strata)
    write_csv(pair_csv, all_pair)
    write_csv(out / "logs" / "stage_09d_vis_warnings.csv", warnings)

    retention_group = summarize_metric_rows(all_retention, ["policy", "mask_name", "candidate_group"])
    policy_mask = summarize_metric_rows(all_policy, ["policy", "mask_name"])
    group_metrics = summarize_metric_rows(all_policy, ["policy", "mask_name", "candidate_group"])
    selected_metrics = summarize_metric_rows([r for r in all_strata if r.get("strata_dimension") == "selected_source"], ["policy", "mask_name", "strata_value"])
    count_metrics = summarize_metric_rows([r for r in all_strata if r.get("strata_dimension") == "valid_source_count_bin"], ["policy", "mask_name", "strata_value"])
    write_csv(out / "01_mask_retention" / "stage_09d_vis_mask_retention_by_group.csv", retention_group)
    write_csv(out / "02_policy_metrics" / "stage_09d_vis_policy_metrics_by_mask.csv", policy_mask)
    write_csv(out / "03_group_source_metrics" / "stage_09d_vis_metrics_by_group.csv", group_metrics)
    write_csv(out / "03_group_source_metrics" / "stage_09d_vis_metrics_by_selected_source.csv", selected_metrics)
    write_csv(out / "03_group_source_metrics" / "stage_09d_vis_metrics_by_valid_source_count.csv", count_metrics)

    meteosat_focus = [r for r in group_metrics if "METEOSAT" in str(r.get("candidate_group", "")) or "Meteosat" in str(r.get("dominant_source", ""))]
    write_csv(out / "05_meteosat_focus" / "stage_09d_vis_meteosat_focus_summary.csv", meteosat_focus)
    gap_rows = build_gap_rows(group_metrics)
    write_csv(out / "05_meteosat_focus" / "stage_09d_vis_meteosat_vs_goes_eastasia_gap.csv", gap_rows)
    baseline_delta = build_delta_rows(policy_mask)
    write_csv(out / "reports" / "stage_09d_vis_delta_vs_baseline.csv", baseline_delta)

    plot_index = make_figures(out, policy_mask, all_retention, group_metrics, gap_rows, count_metrics, all_pair)
    plot_index_csv = out / "06_figures" / "stage_09d_vis_plot_index.csv"
    write_csv(plot_index_csv, plot_index)
    report = build_report(out, policy_mask, baseline_delta, gap_rows, all_retention, warnings)
    report_path = out / "reports" / "stage_09d_geo_visible_controlled_metrics_report_cn.md"
    report_path.write_text(report, encoding="utf-8")
    write_run_manifest(
        out / "logs" / "stage_09d_vis_manifest.json",
        script_path=Path(__file__).resolve(),
        input_paths=[stage09d_dir / "00_sample_manifest" / "stage09d_53_sample_manifest.csv"],
        output_paths=[
            retention_sample_csv,
            policy_sample_csv,
            strata_csv,
            pair_csv,
            plot_index_csv,
            report_path,
        ],
        filters=[r["definition"] for r in MASK_DEFINITIONS],
        unit_conversions=[
            {"source_variable": "latitude", "source_unit": "degree", "target_unit": "degree", "formula": "none", "affected_rows": len(all_policy)},
            {"source_variable": "VZA/SZA", "source_unit": "degree", "target_unit": "degree", "formula": "none", "affected_rows": len(all_policy)},
        ],
        row_counts={
            "samples": len(manifest),
            "policy_rows": len(all_policy),
            "retention_rows": len(all_retention),
            "strata_rows": len(all_strata),
            "pair_rows": len(all_pair),
            "warnings": len(warnings),
        },
        warnings=warnings,
    )
    return report_path


def build_gap_rows(group_metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in group_metrics:
        by_key[(row["policy"], row["mask_name"])][str(row.get("candidate_group", ""))] = row
    rows = []
    for (policy, mask_name), groups in sorted(by_key.items()):
        met = groups.get("METEOSAT_DOMINANT_CONTROL")
        goes = groups.get("GOES_DOMINANT_CONTROL")
        east = groups.get("EAST_ASIA_FY4B_HIMAWARI_PRIORITY")
        mixed = groups.get("MIXED_OR_BOUNDARY")
        best_ref = max(
            [safe_gap_value(goes), safe_gap_value(east), safe_gap_value(mixed)],
            default=math.nan,
        )
        rows.append(
            {
                "policy": policy,
                "mask_name": mask_name,
                "meteosat_agreement": safe_gap_value(met),
                "goes_agreement": safe_gap_value(goes),
                "east_asia_agreement": safe_gap_value(east),
                "mixed_boundary_agreement": safe_gap_value(mixed),
                "best_non_meteosat_agreement": best_ref,
                "best_non_meteosat_minus_meteosat": best_ref - safe_gap_value(met) if math.isfinite(best_ref) and met else math.nan,
            }
        )
    return rows


def safe_gap_value(row: dict[str, Any] | None) -> float:
    if not row:
        return math.nan
    return float(row.get("agreement_weighted", math.nan))


def build_delta_rows(policy_mask: list[dict[str, Any]]) -> list[dict[str, Any]]:
    baseline: dict[str, dict[str, Any]] = {}
    for row in policy_mask:
        if row.get("mask_name") == "VIS-0_baseline_current":
            baseline[row["policy"]] = row
    rows = []
    for row in policy_mask:
        b = baseline.get(row["policy"], {})
        ag = row.get("agreement_weighted", math.nan)
        bag = b.get("agreement_weighted", math.nan)
        rows.append(
            {
                "policy": row["policy"],
                "mask_name": row["mask_name"],
                "agreement_weighted": ag,
                "baseline_agreement_weighted": bag,
                "delta_agreement_vs_VIS0": ag - bag if math.isfinite(float(ag)) and math.isfinite(float(bag)) else math.nan,
                "n_valid_total": row.get("n_valid_total", ""),
                "retention_vs_VIS0": row.get("n_valid_total", 0) / max(float(b.get("n_valid_total", 0) or 0), 1.0),
            }
        )
    return rows


def make_figures(out: Path, policy_mask: list[dict[str, Any]], retention_rows: list[dict[str, Any]], group_metrics: list[dict[str, Any]], gap_rows: list[dict[str, Any]], count_metrics: list[dict[str, Any]], pair_rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    plot_index: list[dict[str, str]] = []
    fig_dir = out / "06_figures"
    policy_csv = out / "02_policy_metrics" / "stage_09d_vis_policy_metrics_by_mask.csv"
    policy_df = pd.DataFrame(policy_mask)
    if not policy_df.empty:
        piv = policy_df.pivot(index="mask_name", columns="policy", values="agreement_weighted").reset_index()
        grouped_bar(fig_dir / "fig01_policy_agreement_by_mask.png", piv, "mask_name", [c for c in piv.columns if c != "mask_name"], "Policy agreement by VIS mask", "agreement", policy_csv, plot_index, (0, 1))
    ret_df = pd.DataFrame(retention_rows)
    ret_csv = out / "01_mask_retention" / "stage_09d_vis_mask_retention_by_sample.csv"
    if not ret_df.empty:
        ret_df["n_valid_retention_ratio"] = pd.to_numeric(ret_df["n_valid_retention_ratio"], errors="coerce")
        rsum = ret_df.groupby(["policy", "mask_name"], as_index=False)["n_valid_retention_ratio"].mean()
        piv = rsum.pivot(index="mask_name", columns="policy", values="n_valid_retention_ratio").reset_index()
        grouped_bar(fig_dir / "fig02_n_valid_retention_by_mask.png", piv, "mask_name", [c for c in piv.columns if c != "mask_name"], "Mean retained pixel ratio by VIS mask", "retention ratio", ret_csv, plot_index, (0, 1))
    group_df = pd.DataFrame(group_metrics)
    group_csv = out / "03_group_source_metrics" / "stage_09d_vis_metrics_by_group.csv"
    if not group_df.empty:
        sub = group_df[(group_df["policy"] == "A_inclusive_binary") & group_df["mask_name"].isin(["VIS-0_baseline_current", "VIS-2_lat70_visible", "VIS-3_lat60_visible", "VIS-5_clean_core"])].copy()
        sub["label"] = sub["mask_name"] + "|" + sub["candidate_group"]
        bar_plot(fig_dir / "fig03_group_agreement_by_mask.png", sub["label"].tolist(), pd.to_numeric(sub["agreement_weighted"], errors="coerce").tolist(), "Policy A group agreement by VIS mask", "agreement", group_csv, plot_index, (0, 1))
    gap_df = pd.DataFrame(gap_rows)
    gap_csv = out / "05_meteosat_focus" / "stage_09d_vis_meteosat_vs_goes_eastasia_gap.csv"
    if not gap_df.empty:
        sub = gap_df[gap_df["policy"] == "A_inclusive_binary"].copy()
        bar_plot(fig_dir / "fig04_meteosat_vs_goes_eastasia_gap_by_mask.png", sub["mask_name"].tolist(), pd.to_numeric(sub["best_non_meteosat_minus_meteosat"], errors="coerce").tolist(), "Meteosat gap vs best non-Meteosat by mask", "agreement gap", gap_csv, plot_index)
    count_df = pd.DataFrame(count_metrics)
    count_csv = out / "03_group_source_metrics" / "stage_09d_vis_metrics_by_valid_source_count.csv"
    if not count_df.empty:
        sub = count_df[(count_df["policy"] == "A_inclusive_binary") & (count_df["mask_name"] == "VIS-1_fused_valid_only")].copy()
        bar_plot(fig_dir / "fig05_valid_source_count_by_mask.png", sub["strata_value"].tolist(), pd.to_numeric(sub["agreement_weighted"], errors="coerce").tolist(), "Policy A agreement by valid source count under VIS-1", "agreement", count_csv, plot_index, (0, 1))
    pair_df = pd.DataFrame(pair_rows)
    pair_csv = out / "04_source_pair_metrics" / "stage_09d_vis_source_pair_metrics.csv"
    if not pair_df.empty:
        sub = pair_df[(pair_df["policy"] == "A_inclusive_binary") & (pair_df["mask_name"] == "VIS-3_lat60_visible")].copy()
        if not sub.empty:
            summary = sub.groupby(["source_A", "source_B"], as_index=False)["source_disagreement_fraction"].mean()
            summary["pair"] = summary["source_A"] + " vs " + summary["source_B"]
            bar_plot(fig_dir / "fig06_source_pair_disagreement_by_mask.png", summary["pair"].tolist(), summary["source_disagreement_fraction"].tolist(), "Source-pair disagreement under VIS-3", "disagreement fraction", pair_csv, plot_index, (0, 1))
    strata_csv = out / "03_group_source_metrics" / "stage_09d_vis_metrics_by_strata.csv"
    boundary_rows = read_csv(strata_csv)
    boundary_df = pd.DataFrame([r for r in boundary_rows if r.get("policy") == "A_inclusive_binary" and r.get("strata_dimension") in {"boundary_class", "scene_type"} and r.get("mask_name") == "VIS-3_lat60_visible"])
    if not boundary_df.empty:
        boundary_df["label"] = boundary_df["strata_dimension"] + ":" + boundary_df["strata_value"]
        bar_plot(fig_dir / "fig07_boundary_vs_nonboundary_under_visible_mask.png", boundary_df["label"].tolist(), pd.to_numeric(boundary_df["agreement"], errors="coerce").tolist(), "Boundary/scene agreement under VIS-3", "agreement", strata_csv, plot_index, (0, 1))
    return plot_index


def build_report(out: Path, policy_mask: list[dict[str, Any]], delta_rows: list[dict[str, Any]], gap_rows: list[dict[str, Any]], retention_rows: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> str:
    pm = {(r["policy"], r["mask_name"]): r for r in policy_mask}
    lines = [
        "# Stage 09D-VIS GEO-visible controlled metrics report",
        "",
        f"- Generated UTC: `{utc_now()}`",
        "- Scope: existing 53 Stage 09D March 2024 samples only; no Stage 05/06 rerun; no fusion v2.",
        "- Units: latitude/VZA/SZA are degrees; agreement/F1/IoU/retention/cloud fractions are unitless fractions.",
        "- EPIC is an independent diagnostic reference, not absolute truth.",
        "",
        "## Key Mask Agreement",
        md_table(
            [
                {
                    "policy": p,
                    "VIS-0": pm.get((p, "VIS-0_baseline_current"), {}).get("agreement_weighted", math.nan),
                    "VIS-1": pm.get((p, "VIS-1_fused_valid_only"), {}).get("agreement_weighted", math.nan),
                    "VIS-2": pm.get((p, "VIS-2_lat70_visible"), {}).get("agreement_weighted", math.nan),
                    "VIS-3": pm.get((p, "VIS-3_lat60_visible"), {}).get("agreement_weighted", math.nan),
                    "VIS-4a": pm.get((p, "VIS-4a_reliable_geometry_without_geo_vza"), {}).get("agreement_weighted", math.nan),
                    "VIS-4b": pm.get((p, "VIS-4b_reliable_geometry_with_geo_vza_available_only"), {}).get("agreement_weighted", math.nan),
                    "VIS-5": pm.get((p, "VIS-5_clean_core"), {}).get("agreement_weighted", math.nan),
                }
                for p in POLICY_ORDER
            ],
            ["policy", "VIS-0", "VIS-1", "VIS-2", "VIS-3", "VIS-4a", "VIS-4b", "VIS-5"],
        ),
        "",
        "## Required Answers",
    ]
    for idx, question in enumerate(
        [
            "VIS-1 与 VIS-0 是否几乎一致？",
            "排除 abs(lat)>=70 后，A/B/C agreement 提高多少？",
            "排除 abs(lat)>=60 后，A/B/C agreement 提高多少？",
            "reliable_geometry mask 是否显著提高 agreement？",
            "clean_core mask 的 agreement 上限是多少？retention ratio 多少？",
            "VIS-5 如果 retained pixels 很少，只能作为上限诊断。",
            "Meteosat-dominant 与 GOES/East Asia 的差距在 VIS-2/VIS-3/VIS-4 后是否缩小？",
            "如果差距仍在，Meteosat low agreement 不能简单归因于 GEO 不可见区。",
            "source-pair disagreement 在 GEO-visible controlled masks 下是否仍然存在？",
            "valid_source_count>=4 区域低 agreement 是否在排除高纬后仍存在？",
        ],
        1,
    ):
        lines.append(f"{idx}. {question} 见 `stage_09d_vis_delta_vs_baseline.csv`、`stage_09d_vis_meteosat_vs_goes_eastasia_gap.csv`、source-pair 和 valid-source-count 汇总。")
    lines.extend(
        [
            "",
            "## Delta vs Baseline",
            md_table(delta_rows, ["policy", "mask_name", "agreement_weighted", "delta_agreement_vs_VIS0", "retention_vs_VIS0"], 40),
            "",
            "## Meteosat Gap",
            md_table(gap_rows, ["policy", "mask_name", "meteosat_agreement", "goes_agreement", "east_asia_agreement", "best_non_meteosat_minus_meteosat"], 40),
            "",
            "## Quality Control / Missing Variables",
            f"- Warning rows: `{len(warnings)}`",
            "- GEO VZA masks are partial where selected-source GEO VZA files are missing; missing variables are recorded in `logs/stage_09d_vis_warnings.csv` and the run manifest.",
            "- No unit conversions were applied; angular variables remain in degrees and all fractions are unitless.",
            "",
            "## Traceability",
            "- Every PNG is indexed in `06_figures/stage_09d_vis_plot_index.csv` with a source CSV.",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage 09d VIS controlled diagnostic metrics.")
    parser.add_argument("--stage09d-dir", default=str(DEFAULT_STAGE09D_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUT))
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--cpu-target", type=float, default=0.70)
    parser.add_argument("--memory-target", type=float, default=0.70)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--max-samples", type=int, default=0)
    return parser.parse_args()


if __name__ == "__main__":
    path = run(parse_args())
    print(path)
