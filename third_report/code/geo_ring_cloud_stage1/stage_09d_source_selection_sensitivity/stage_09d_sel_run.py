# -*- coding: utf-8 -*-
"""Stage 09d SEL: source-selection sensitivity diagnostics.

Diagnostic only.  Best-available sources are EPIC-referenced retrospective
diagnostic quantities and are not production rules.
"""
from __future__ import annotations

import argparse
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from geo_ring_cloud import paths as path_config  # noqa: E402
from geo_ring_cloud.diagnostics import full_pixel as d09d  # noqa: E402
from geo_ring_cloud.diagnostics.full_pixel_workflow import (  # noqa: E402
    ABS_LAT_BINS,
    DEFAULT_STAGE09D_DIR,
    EPIC_VZA_BINS,
    SOURCE_ID_TO_NAME,
    SOURCE_NAME_TO_ID,
    SZA_BINS,
    VALID_COUNT_BINS,
    add_plot,
    bar_plot,
    base_valid_mask,
    bool_series,
    classify,
    describe_valid_context,
    ensure_dirs,
    grouped_bar,
    heatmap,
    load_manifest,
    md_table,
    metric_row,
    nanmean,
    read_csv,
    scene_boundary,
    selected_source_array,
    source_samples,
    summarize_metric_rows,
    utc_now,
    weighted_mean,
    write_csv,
    write_run_manifest,
)


DEFAULT_OUT = path_config.RUNS_ROOT / "stage09d_source_selection_sensitivity_202403"
POLICY_ORDER = ["A_inclusive_binary", "B_high_confidence_only", "C_uncertainty_aware_3class"]


def safe_source_label(name: str) -> str:
    return name.replace("-", "").replace(" ", "").replace("_", "")


def pixel_groups(ctx: dict[str, Any], scene: dict[str, np.ndarray], source_valid: dict[str, np.ndarray], base: np.ndarray) -> dict[str, np.ndarray]:
    lat_abs = np.abs(ctx["epic"]["lat"])
    selected = ctx["selected_source"]
    groups: dict[str, np.ndarray] = {
        "ALL_VALID": base,
        "valid_source_count_1": base & (ctx["valid_count"] == 1),
        "valid_source_count_2": base & (ctx["valid_count"] == 2),
        "valid_source_count_3": base & (ctx["valid_count"] == 3),
        "valid_source_count_ge4": base & (ctx["valid_count"] >= 4),
        "non_boundary": base & (scene["boundary_class"] == "non_boundary"),
        "boundary_or_broken_cloud": base & ((scene["boundary_class"] != "non_boundary") | (scene["scene_type"] == "broken_cloud")),
        "abs_lat_lt60": base & (lat_abs < 60),
        "abs_lat_60_70": base & (lat_abs >= 60) & (lat_abs < 70),
        "abs_lat_70_80": base & (lat_abs >= 70) & (lat_abs < 80),
        "abs_lat_ge80": base & (lat_abs >= 80),
    }
    for sid, name in SOURCE_ID_TO_NAME.items():
        groups[f"selected_{safe_source_label(name)}"] = base & (selected == sid)
    iodc = source_valid.get("Meteosat-IODC", np.zeros(base.shape, dtype=bool))
    fy4b = source_valid.get("FY4B", np.zeros(base.shape, dtype=bool))
    him = source_valid.get("Himawari-9", np.zeros(base.shape, dtype=bool))
    met0 = source_valid.get("Meteosat-0deg", np.zeros(base.shape, dtype=bool))
    goes16 = source_valid.get("GOES-16", np.zeros(base.shape, dtype=bool))
    groups["selected_MeteosatIODC_and_valid_count_ge4"] = groups["selected_MeteosatIODC"] & (ctx["valid_count"] >= 4)
    groups["MeteosatIODC_available"] = base & iodc
    groups["MeteosatIODC_selected"] = groups["selected_MeteosatIODC"]
    groups["MeteosatIODC_available_but_not_selected"] = base & iodc & (selected != SOURCE_NAME_TO_ID["Meteosat-IODC"])
    groups["FY4B_and_IODC_both_available"] = base & fy4b & iodc
    groups["Himawari_and_IODC_both_available"] = base & him & iodc
    groups["Meteosat0deg_and_GOES16_both_available"] = base & met0 & goes16
    return groups


def source_policy(source_cls: dict[str, np.ndarray], source_valid: dict[str, np.ndarray], source: str, policy_name: str) -> tuple[np.ndarray, np.ndarray]:
    cls, pv = d09d.apply_policy(source_cls[source], d09d.POLICIES[policy_name]["geo"])
    return cls, source_valid[source] & pv


def current_source_distribution(ctx: dict[str, Any], valid: np.ndarray) -> tuple[str, str, float]:
    n = int(np.count_nonzero(valid))
    if n == 0:
        return "none", "{}", math.nan
    counts = Counter()
    for sid in SOURCE_ID_TO_NAME:
        c = int(np.count_nonzero(valid & (ctx["selected_source"] == sid)))
        if c:
            counts[SOURCE_ID_TO_NAME[sid]] = c
    mode = counts.most_common(1)[0][0] if counts else "none"
    frac = counts.most_common(1)[0][1] / n if counts else math.nan
    return mode, ";".join(f"{k}:{v}" for k, v in sorted(counts.items())), frac


def selected_source_class(ctx: dict[str, Any], source_classes_by_policy: dict[str, dict[str, np.ndarray]], policy_name: str) -> tuple[np.ndarray, np.ndarray]:
    selected_cls = np.full(ctx["epic"]["lat"].shape, -1, dtype=np.int16)
    selected_valid = np.zeros(ctx["epic"]["lat"].shape, dtype=bool)
    for source, sid in SOURCE_NAME_TO_ID.items():
        cls = source_classes_by_policy[policy_name].get(source)
        if cls is None:
            continue
        m = ctx["selected_source"] == sid
        selected_cls[m] = cls[m]
        selected_valid |= m
    return selected_cls, selected_valid


def sample_sel(row: dict[str, Any]) -> dict[str, Any]:
    ctx = d09d.sample_context(row)
    scene = scene_boundary(ctx, "A_inclusive_binary")
    source_std, source_valid_raw, source_warnings = source_samples(ctx)
    selected_names = selected_source_array(ctx["selected_source"])
    current_rows: list[dict[str, Any]] = []
    available_rows: list[dict[str, Any]] = []
    rank_rows: list[dict[str, Any]] = []
    regret_rows: list[dict[str, Any]] = []
    counter_rows: list[dict[str, Any]] = []
    case_rows: list[dict[str, Any]] = []
    warnings = [{"sample_id": row["sample_id"], "module": "sel_source_load", "warning": w} for w in source_warnings]

    for policy_name in POLICY_ORDER:
        base, epic_cls, fused_cls = base_valid_mask(ctx, policy_name)
        source_cls_by_policy: dict[str, np.ndarray] = {}
        source_valid_by_policy: dict[str, np.ndarray] = {}
        for source in source_std:
            cls, valid = source_policy(source_std, source_valid_raw, source, policy_name)
            source_cls_by_policy[source] = cls
            source_valid_by_policy[source] = valid
        groups = pixel_groups(ctx, scene, source_valid_by_policy, base)
        selected_cls, selected_valid = selected_source_class(ctx, {policy_name: source_cls_by_policy}, policy_name)
        for group_name, group_mask in groups.items():
            valid_current = group_mask & selected_valid
            n_current = int(np.count_nonzero(valid_current))
            if n_current == 0:
                continue
            current = metric_row(epic_cls, selected_cls, valid_current, policy_name)
            mode, dist, mode_frac = current_source_distribution(ctx, valid_current)
            current.update(
                {
                    "sample_id": row["sample_id"],
                    "pixel_group": group_name,
                    "policy": policy_name,
                    "current_selected_source_mode": mode,
                    "current_selected_source_distribution": dist,
                    "current_selected_source_mode_fraction": mode_frac,
                    "candidate_group": row.get("candidate_group", ""),
                    "dominant_source": row.get("dominant_source", ""),
                }
            )
            current.update(describe_valid_context(ctx, valid_current, scene))
            current_rows.append(current)

            source_metrics = []
            for source, cls in source_cls_by_policy.items():
                same_valid = group_mask & source_valid_by_policy[source]
                n_same = int(np.count_nonzero(same_valid))
                if n_same == 0:
                    continue
                sm = metric_row(epic_cls, cls, same_valid, policy_name)
                sm.update(
                    {
                        "sample_id": row["sample_id"],
                        "pixel_group": group_name,
                        "policy": policy_name,
                        "source_name": source,
                        "n_valid_same_pixels": sm.get("n_valid"),
                        "candidate_group": row.get("candidate_group", ""),
                        "dominant_source": row.get("dominant_source", ""),
                    }
                )
                sm.update(describe_valid_context(ctx, same_valid, scene))
                available_rows.append(sm)
                source_metrics.append(sm)
            if not source_metrics:
                continue
            regret = regret_from_metrics(ctx, epic_cls, source_cls_by_policy, source_valid_by_policy, selected_cls, valid_current, group_name, policy_name, current, source_metrics, scene, row)
            regret_rows.append(regret)
            rank_rows.append({k: regret.get(k) for k in ["sample_id", "pixel_group", "policy", "n_valid", "num_available_sources_mean", "current_selected_source_mode", "current_selected_agreement_rank", "current_selected_f1_rank", "current_selected_iou_rank", "selected_is_best_fraction"]})
            counter_rows.extend(counterfactual_rows(ctx, epic_cls, source_cls_by_policy, source_valid_by_policy, selected_cls, group_mask, policy_name, group_name, row, current, source_metrics))
            if group_name in {"valid_source_count_ge4", "MeteosatIODC_selected", "selected_MeteosatIODC_and_valid_count_ge4", "ALL_VALID"}:
                case_rows.append(
                    {
                        "sample_id": row["sample_id"],
                        "pixel_group": group_name,
                        "policy": policy_name,
                        "n_valid": regret.get("n_valid"),
                        "selection_regret_agreement": regret.get("selection_regret_agreement"),
                        "current_selected_source_mode": mode,
                        "best_available_source_by_agreement": regret.get("best_available_source_by_agreement"),
                        "candidate_group": row.get("candidate_group", ""),
                    }
                )
    return {
        "current_rows": current_rows,
        "available_rows": available_rows,
        "rank_rows": rank_rows,
        "regret_rows": regret_rows,
        "counter_rows": counter_rows,
        "case_rows": case_rows,
        "warnings": warnings,
    }


def rank_of_source(source_metrics: list[dict[str, Any]], selected_mode: str, metric: str) -> int | float:
    ranked = sorted(source_metrics, key=lambda r: float(r.get(metric, math.nan)), reverse=True)
    for idx, row in enumerate(ranked, 1):
        if row.get("source_name") == selected_mode:
            return idx
    return math.nan


def regret_from_metrics(
    ctx: dict[str, Any],
    epic_cls: np.ndarray,
    source_cls: dict[str, np.ndarray],
    source_valid: dict[str, np.ndarray],
    selected_cls: np.ndarray,
    valid_current: np.ndarray,
    group_name: str,
    policy_name: str,
    current: dict[str, Any],
    source_metrics: list[dict[str, Any]],
    scene: dict[str, np.ndarray],
    row: dict[str, Any],
) -> dict[str, Any]:
    mode, dist, mode_frac = current_source_distribution(ctx, valid_current)
    best_ag = max(source_metrics, key=lambda r: float(r.get("agreement", -1)))
    best_f1 = max(source_metrics, key=lambda r: float(r.get("f1_cloud", -1)))
    best_iou = max(source_metrics, key=lambda r: float(r.get("iou_cloud", -1)))
    selected_is_best = pixelwise_selected_is_best(epic_cls, source_cls, source_valid, selected_cls, valid_current)
    num_avail = np.zeros(valid_current.shape, dtype=np.int16)
    for source in source_valid:
        num_avail += (source_valid[source] & valid_current).astype(np.int16)
    out = {
        "sample_id": row["sample_id"],
        "pixel_group": group_name,
        "policy": policy_name,
        "n_valid": current.get("n_valid"),
        "num_available_sources_mean": nanmean(num_avail.astype(np.float32), valid_current),
        "current_selected_source_mode": mode,
        "current_selected_source_distribution": dist,
        "current_selected_source_mode_fraction": mode_frac,
        "current_selected_agreement": current.get("agreement"),
        "best_available_source_by_agreement": best_ag.get("source_name"),
        "best_available_agreement": best_ag.get("agreement"),
        "selection_regret_agreement": best_ag.get("agreement") - current.get("agreement"),
        "current_selected_f1": current.get("f1_cloud"),
        "best_available_source_by_f1": best_f1.get("source_name"),
        "best_available_f1": best_f1.get("f1_cloud"),
        "selection_regret_f1": best_f1.get("f1_cloud") - current.get("f1_cloud"),
        "current_selected_iou": current.get("iou_cloud"),
        "best_available_source_by_iou": best_iou.get("source_name"),
        "best_available_iou": best_iou.get("iou_cloud"),
        "selection_regret_iou": best_iou.get("iou_cloud") - current.get("iou_cloud"),
        "current_selected_agreement_rank": rank_of_source(source_metrics, mode, "agreement"),
        "current_selected_f1_rank": rank_of_source(source_metrics, mode, "f1_cloud"),
        "current_selected_iou_rank": rank_of_source(source_metrics, mode, "iou_cloud"),
        "selected_is_best_fraction": selected_is_best,
        "candidate_group": row.get("candidate_group", ""),
        "dominant_source": row.get("dominant_source", ""),
    }
    out.update(describe_valid_context(ctx, valid_current, scene))
    return out


def pixelwise_selected_is_best(epic_cls: np.ndarray, source_cls: dict[str, np.ndarray], source_valid: dict[str, np.ndarray], selected_cls: np.ndarray, valid: np.ndarray) -> float:
    n = int(np.count_nonzero(valid))
    if n == 0:
        return math.nan
    any_correct = np.zeros(valid.shape, dtype=bool)
    for source, cls in source_cls.items():
        any_correct |= valid & source_valid[source] & (cls == epic_cls)
    selected_correct = valid & (selected_cls == epic_cls)
    selected_best = selected_correct | (valid & (~any_correct))
    return int(np.count_nonzero(selected_best & valid)) / n


def counterfactual_rows(
    ctx: dict[str, Any],
    epic_cls: np.ndarray,
    source_cls: dict[str, np.ndarray],
    source_valid: dict[str, np.ndarray],
    selected_cls: np.ndarray,
    group_mask: np.ndarray,
    policy_name: str,
    group_name: str,
    row: dict[str, Any],
    current: dict[str, Any],
    source_metrics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    strategies = ["current_selected", "always_FY4B_if_available", "always_Himawari_if_available", "always_GOES16_if_available", "always_MeteosatIODC_if_available", "best_available_oracle"]
    for strategy in strategies:
        if strategy == "current_selected":
            rows.append({"sample_id": row["sample_id"], "pixel_group": group_name, "policy": policy_name, "strategy": strategy, "n_valid": current.get("n_valid"), "agreement": current.get("agreement"), "f1_cloud": current.get("f1_cloud"), "iou_cloud": current.get("iou_cloud")})
            continue
        if strategy == "best_available_oracle":
            best = max(source_metrics, key=lambda r: float(r.get("agreement", -1)))
            rows.append({"sample_id": row["sample_id"], "pixel_group": group_name, "policy": policy_name, "strategy": strategy, "source_name": best.get("source_name"), "n_valid": best.get("n_valid"), "agreement": best.get("agreement"), "f1_cloud": best.get("f1_cloud"), "iou_cloud": best.get("iou_cloud")})
            continue
        src = {
            "always_FY4B_if_available": "FY4B",
            "always_Himawari_if_available": "Himawari-9",
            "always_GOES16_if_available": "GOES-16",
            "always_MeteosatIODC_if_available": "Meteosat-IODC",
        }[strategy]
        if src not in source_cls:
            continue
        valid = group_mask & source_valid[src]
        if not np.any(valid):
            continue
        m = metric_row(epic_cls, source_cls[src], valid, policy_name)
        rows.append({"sample_id": row["sample_id"], "pixel_group": group_name, "policy": policy_name, "strategy": strategy, "source_name": src, **m})
    return rows


def run(args: argparse.Namespace) -> Path:
    stage09d_dir = Path(args.stage09d_dir)
    out = Path(args.output_dir)
    ensure_dirs(
        out,
        [
            "00_pixel_group_manifest",
            "01_current_selected_performance",
            "02_available_source_comparison",
            "03_selection_regret",
            "04_valid_count_ge4_focus",
            "05_meteosat_iodc_selected_focus",
            "06_case_maps",
            "07_figures",
            "reports",
            "logs",
        ],
    )
    group_defs = pixel_group_definitions()
    write_csv(out / "00_pixel_group_manifest" / "stage_09d_sel_pixel_group_definitions.csv", group_defs)
    manifest = [r for r in load_manifest(stage09d_dir) if bool_series(r.get("can_run_source_pair"))]
    if args.max_samples:
        manifest = manifest[: args.max_samples]
    current_rows: list[dict[str, Any]] = []
    available_rows: list[dict[str, Any]] = []
    rank_rows: list[dict[str, Any]] = []
    regret_rows: list[dict[str, Any]] = []
    counter_rows: list[dict[str, Any]] = []
    case_rows: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for idx, row in enumerate(manifest, 1):
        print(f"[stage_09d_sel] {idx}/{len(manifest)} {row['sample_id']}", flush=True)
        try:
            result = sample_sel(row)
            current_rows.extend(result["current_rows"])
            available_rows.extend(result["available_rows"])
            rank_rows.extend(result["rank_rows"])
            regret_rows.extend(result["regret_rows"])
            counter_rows.extend(result["counter_rows"])
            case_rows.extend(result["case_rows"])
            warnings.extend(result["warnings"])
        except Exception as exc:
            import traceback

            warnings.append({"sample_id": row.get("sample_id", ""), "module": "sample_sel", "warning": str(exc), "traceback": traceback.format_exc()})

    current_csv = out / "01_current_selected_performance" / "stage_09d_sel_current_selected_metrics.csv"
    available_csv = out / "02_available_source_comparison" / "stage_09d_sel_available_source_metrics_same_pixels.csv"
    rank_csv = out / "02_available_source_comparison" / "stage_09d_sel_source_rank_by_pixel_group.csv"
    regret_csv = out / "03_selection_regret" / "stage_09d_sel_selection_regret_summary.csv"
    counter_csv = out / "03_selection_regret" / "stage_09d_sel_counterfactual_diagnostic_summary.csv"
    write_csv(current_csv, current_rows)
    write_csv(available_csv, available_rows)
    write_csv(rank_csv, rank_rows)
    write_csv(regret_csv, regret_rows)
    write_csv(counter_csv, counter_rows)
    write_csv(out / "logs" / "stage_09d_sel_warnings.csv", warnings)

    ge4_diag, ge4_source = focus_valid_count_ge4(regret_rows, available_rows)
    iodc_diag, iodc_source = focus_iodc(regret_rows, available_rows)
    ge4_diag_csv = out / "04_valid_count_ge4_focus" / "stage_09d_sel_valid_count_ge4_diagnostics.csv"
    ge4_source_csv = out / "04_valid_count_ge4_focus" / "stage_09d_sel_valid_count_ge4_source_comparison.csv"
    iodc_diag_csv = out / "05_meteosat_iodc_selected_focus" / "stage_09d_sel_iodc_selected_diagnostics.csv"
    iodc_source_csv = out / "05_meteosat_iodc_selected_focus" / "stage_09d_sel_iodc_selected_available_source_comparison.csv"
    write_csv(ge4_diag_csv, ge4_diag)
    write_csv(ge4_source_csv, ge4_source)
    write_csv(iodc_diag_csv, iodc_diag)
    write_csv(iodc_source_csv, iodc_source)
    delta_csv = out / "reports" / "stage_09d_sel_delta_vs_stage09d_baseline.csv"
    write_csv(delta_csv, build_delta_rows(regret_rows))
    case_inventory_csv = out / "06_case_maps" / "stage_09d_sel_case_map_inventory.csv"
    write_csv(case_inventory_csv, case_rows)
    make_case_maps(out, manifest, case_rows)
    plot_index = make_figures(out, regret_rows, available_rows, ge4_source, iodc_diag)
    plot_index_csv = out / "07_figures" / "stage_09d_sel_plot_index.csv"
    write_csv(plot_index_csv, plot_index)
    report_path = out / "reports" / "stage_09d_source_selection_sensitivity_report_cn.md"
    report_path.write_text(build_report(regret_rows, ge4_diag, ge4_source, iodc_diag, warnings), encoding="utf-8")
    write_run_manifest(
        out / "logs" / "stage_09d_sel_manifest.json",
        canonical_stage_id=STAGE_ID,
        script_path=Path(__file__).resolve(),
        input_paths=[stage09d_dir / "00_sample_manifest" / "stage09d_53_sample_manifest.csv"],
        output_paths=[current_csv, available_csv, rank_csv, regret_csv, counter_csv, ge4_diag_csv, ge4_source_csv, iodc_diag_csv, iodc_source_csv, plot_index_csv, report_path],
        filters=[r["definition"] for r in group_defs],
        unit_conversions=[
            {"source_variable": "latitude", "source_unit": "degree", "target_unit": "degree", "formula": "none", "affected_rows": len(regret_rows)},
            {"source_variable": "cloud metrics", "source_unit": "fraction", "target_unit": "fraction", "formula": "none", "affected_rows": len(current_rows)},
        ],
        row_counts={
            "samples": len(manifest),
            "current_rows": len(current_rows),
            "available_rows": len(available_rows),
            "rank_rows": len(rank_rows),
            "regret_rows": len(regret_rows),
            "counterfactual_rows": len(counter_rows),
            "warnings": len(warnings),
        },
        warnings=warnings,
    )
    return report_path


def pixel_group_definitions() -> list[dict[str, str]]:
    names = [
        ("ALL_VALID", "Stage 09d policy-valid fused comparison pixels."),
        ("valid_source_count_1", "ALL_VALID and valid_source_count == 1."),
        ("valid_source_count_2", "ALL_VALID and valid_source_count == 2."),
        ("valid_source_count_3", "ALL_VALID and valid_source_count == 3."),
        ("valid_source_count_ge4", "ALL_VALID and valid_source_count >= 4."),
        ("selected_GOES16", "ALL_VALID and fused selected source is GOES-16."),
        ("selected_GOES18", "ALL_VALID and fused selected source is GOES-18."),
        ("selected_FY4B", "ALL_VALID and fused selected source is FY4B."),
        ("selected_Himawari9", "ALL_VALID and fused selected source is Himawari-9."),
        ("selected_Meteosat0deg", "ALL_VALID and fused selected source is Meteosat-0deg."),
        ("selected_MeteosatIODC", "ALL_VALID and fused selected source is Meteosat-IODC."),
        ("selected_MeteosatIODC_and_valid_count_ge4", "ALL_VALID, selected Meteosat-IODC, and valid_source_count >= 4."),
        ("MeteosatIODC_available", "Meteosat-IODC prefusion source is available."),
        ("MeteosatIODC_selected", "Current fused selected source is Meteosat-IODC."),
        ("MeteosatIODC_available_but_not_selected", "IODC available but not selected."),
        ("FY4B_and_IODC_both_available", "FY4B and Meteosat-IODC both available."),
        ("Himawari_and_IODC_both_available", "Himawari-9 and Meteosat-IODC both available."),
        ("Meteosat0deg_and_GOES16_both_available", "Meteosat-0deg and GOES-16 both available."),
        ("non_boundary", "Cloud boundary class is non_boundary."),
        ("boundary_or_broken_cloud", "Near cloud boundary or local broken-cloud scene."),
        ("abs_lat_lt60", "abs(latitude) < 60 degrees."),
        ("abs_lat_60_70", "60 <= abs(latitude) < 70 degrees."),
        ("abs_lat_70_80", "70 <= abs(latitude) < 80 degrees."),
        ("abs_lat_ge80", "abs(latitude) >= 80 degrees."),
    ]
    return [{"pixel_group": n, "definition": d, "units": "boolean mask over EPIC pixels"} for n, d in names]


def focus_valid_count_ge4(regret_rows: list[dict[str, Any]], available_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ge4 = [r for r in regret_rows if r.get("pixel_group") == "valid_source_count_ge4"]
    source = [r for r in available_rows if r.get("pixel_group") == "valid_source_count_ge4"]
    diag = summarize_metric_rows(ge4, ["policy", "pixel_group"], "n_valid")
    src_summary = summarize_metric_rows(source, ["policy", "source_name"], "n_valid")
    for row in diag:
        row["current_selected_source_distribution"] = mode_text([r.get("current_selected_source_distribution", "") for r in ge4 if r.get("policy") == row.get("policy")])
    return diag, src_summary


def focus_iodc(regret_rows: list[dict[str, Any]], available_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    iodc = [r for r in regret_rows if r.get("pixel_group") in {"selected_MeteosatIODC", "MeteosatIODC_selected", "selected_MeteosatIODC_and_valid_count_ge4"}]
    src = [r for r in available_rows if r.get("pixel_group") in {"selected_MeteosatIODC", "MeteosatIODC_selected", "selected_MeteosatIODC_and_valid_count_ge4"}]
    return summarize_metric_rows(iodc, ["policy", "pixel_group"], "n_valid"), summarize_metric_rows(src, ["policy", "pixel_group", "source_name"], "n_valid")


def mode_text(values: list[str]) -> str:
    counts = Counter(v for v in values if v)
    return counts.most_common(1)[0][0] if counts else ""


def build_delta_rows(regret_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return summarize_metric_rows(regret_rows, ["policy", "pixel_group"], "n_valid")


def make_figures(out: Path, regret_rows: list[dict[str, Any]], available_rows: list[dict[str, Any]], ge4_source: list[dict[str, Any]], iodc_diag: list[dict[str, Any]]) -> list[dict[str, str]]:
    plot_index: list[dict[str, str]] = []
    fig_dir = out / "07_figures"
    regret_csv = out / "03_selection_regret" / "stage_09d_sel_selection_regret_summary.csv"
    available_csv = out / "02_available_source_comparison" / "stage_09d_sel_available_source_metrics_same_pixels.csv"
    ge4_csv = out / "04_valid_count_ge4_focus" / "stage_09d_sel_valid_count_ge4_source_comparison.csv"
    iodc_csv = out / "05_meteosat_iodc_selected_focus" / "stage_09d_sel_iodc_selected_diagnostics.csv"
    reg = pd.DataFrame(regret_rows)
    if not reg.empty:
        sub = reg[(reg["policy"] == "A_inclusive_binary") & reg["pixel_group"].isin(["ALL_VALID", "valid_source_count_2", "valid_source_count_3", "valid_source_count_ge4", "MeteosatIODC_selected", "non_boundary", "boundary_or_broken_cloud"])]
        bar_plot(fig_dir / "fig01_current_selected_vs_best_available_by_group.png", sub["pixel_group"].tolist(), pd.to_numeric(sub["selection_regret_agreement"], errors="coerce").tolist(), "Selection regret by pixel group (Policy A)", "best-current agreement", regret_csv, plot_index)
        vsc = sub[sub["pixel_group"].str.contains("valid_source_count", na=False)]
        if not vsc.empty:
            bar_plot(fig_dir / "fig02_selection_regret_by_valid_source_count.png", vsc["pixel_group"].tolist(), pd.to_numeric(vsc["selection_regret_agreement"], errors="coerce").tolist(), "Selection regret by valid source count", "best-current agreement", regret_csv, plot_index)
        iodc = reg[(reg["policy"] == "A_inclusive_binary") & reg["pixel_group"].str.contains("IODC", na=False)]
        if not iodc.empty:
            bar_plot(fig_dir / "fig05_iodc_selected_source_regret.png", iodc["pixel_group"].tolist(), pd.to_numeric(iodc["selection_regret_agreement"], errors="coerce").tolist(), "IODC-related selection regret", "best-current agreement", regret_csv, plot_index)
        rank = reg[reg["policy"] == "A_inclusive_binary"].pivot_table(index="pixel_group", values="current_selected_agreement_rank", aggfunc="mean")
        if not rank.empty:
            heatmap(fig_dir / "fig06_source_rank_heatmap_by_pixel_group.png", rank, "Current selected agreement rank by group", regret_csv, plot_index, 1, 6)
        bg = reg[(reg["policy"] == "A_inclusive_binary") & reg["pixel_group"].isin(["non_boundary", "boundary_or_broken_cloud", "abs_lat_lt60", "abs_lat_60_70", "abs_lat_70_80", "abs_lat_ge80"])]
        if not bg.empty:
            bar_plot(fig_dir / "fig07_selection_regret_by_boundary_geometry.png", bg["pixel_group"].tolist(), pd.to_numeric(bg["selection_regret_agreement"], errors="coerce").tolist(), "Selection regret by boundary/geometry", "best-current agreement", regret_csv, plot_index)
    ge4 = pd.DataFrame(ge4_source)
    if not ge4.empty:
        sub = ge4[ge4["policy"] == "A_inclusive_binary"].copy()
        bar_plot(fig_dir / "fig03_valid_count_ge4_available_source_agreement.png", sub["source_name"].tolist(), pd.to_numeric(sub["agreement_weighted"], errors="coerce").tolist(), "valid_count>=4 available source agreement", "agreement", ge4_csv, plot_index, (0, 1))
    avail = pd.DataFrame(available_rows)
    if not avail.empty:
        sub = avail[(avail["policy"] == "A_inclusive_binary") & (avail["pixel_group"].str.contains("valid_source_count", na=False))]
        if not sub.empty:
            dist = sub.groupby(["pixel_group", "source_name"], as_index=False)["n_valid_same_pixels"].sum()
            mat = dist.pivot(index="pixel_group", columns="source_name", values="n_valid_same_pixels").fillna(0)
            heatmap(fig_dir / "fig04_selected_source_distribution_by_valid_count.png", mat, "Available source pixels by valid-count group", available_csv, plot_index)
    return plot_index


def make_case_maps(out: Path, manifest: list[dict[str, Any]], case_rows: list[dict[str, Any]]) -> None:
    # Keep maps lightweight: choose top cases by sample-level regret inventory and
    # render existing Stage 09d error-atlas style fields only.
    if not case_rows:
        return
    selected = []
    seen = set()
    for row in sorted(case_rows, key=lambda r: float(r.get("selection_regret_agreement") or -1), reverse=True):
        tag = row.get("sample_id")
        if tag and tag not in seen:
            selected.append(row)
            seen.add(tag)
        if len(selected) >= 6:
            break
    manifest_by_id = {r["sample_id"]: r for r in manifest}
    plot_index = []
    inv = []
    for row in selected:
        tag = row["sample_id"]
        if tag not in manifest_by_id:
            continue
        try:
            ctx = d09d.sample_context(manifest_by_id[tag])
            scene = scene_boundary(ctx, "A_inclusive_binary")
            epic_cls, ev, geo_cls, gv = d09d.apply_policy(ctx["epic"]["cloud_mask"], d09d.POLICIES["A_inclusive_binary"]["epic"])[0], None, None, None
            geo_cls, geo_pv = d09d.apply_policy(ctx["fused_on_epic"], d09d.POLICIES["A_inclusive_binary"]["geo"])
            valid = ctx["fused_on_valid"] & geo_pv
            mismatch = np.full(epic_cls.shape, np.nan, dtype=np.float32)
            mismatch[valid & (epic_cls == geo_cls)] = 0
            mismatch[valid & (epic_cls != geo_cls)] = 1
            arrays = [epic_cls, geo_cls, ctx["selected_source"], ctx["valid_count"], mismatch, scene["boundary_bool"].astype(float), np.abs(ctx["epic"]["lat"])]
            titles = ["EPIC", "current fused", "selected source", "valid source count", "current wrong", "boundary", "abs lat"]
            fig, axes = plt.subplots(2, 4, figsize=(13, 7), constrained_layout=True)
            stride = max(1, epic_cls.shape[0] // 512)
            for ax, arr, title in zip(axes.ravel(), arrays, titles):
                im = ax.imshow(arr[::stride, ::stride], interpolation="nearest")
                ax.set_title(title)
                ax.axis("off")
                fig.colorbar(im, ax=ax, fraction=0.035)
            for ax in axes.ravel()[len(arrays) :]:
                ax.axis("off")
            fig.suptitle(f"{tag} {row.get('pixel_group')} regret={row.get('selection_regret_agreement')}")
            case_dir = out / "06_case_maps" / f"stage_09d_sel_case_{tag}"
            case_dir.mkdir(parents=True, exist_ok=True)
            png = case_dir / "stage_09d_sel_case_panel.png"
            fig.savefig(png, dpi=150)
            plt.close(fig)
            csv_path = case_dir / "stage_09d_sel_case_summary.csv"
            write_csv(csv_path, [row])
            inv.append({"sample_id": tag, "case_panel": str(png), "source_csv": str(csv_path), "pixel_group": row.get("pixel_group"), "selection_regret_agreement": row.get("selection_regret_agreement")})
            plot_index.append({"plot_path": str(png), "source_csv": str(csv_path), "description": "Stage 09d SEL representative case map", "created_time_utc": utc_now()})
        except Exception as exc:
            write_csv(out / "06_case_maps" / f"stage_09d_sel_case_{tag}_warning.csv", [{"sample_id": tag, "warning": str(exc)}])
    write_csv(out / "06_case_maps" / "stage_09d_sel_case_map_inventory.csv", inv)
    write_csv(out / "06_case_maps" / "stage_09d_sel_case_map_plot_index.csv", plot_index)


def build_report(regret_rows: list[dict[str, Any]], ge4_diag: list[dict[str, Any]], ge4_source: list[dict[str, Any]], iodc_diag: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> str:
    reg = summarize_metric_rows(regret_rows, ["policy", "pixel_group"], "n_valid")
    policy_a = [r for r in reg if r.get("policy") == "A_inclusive_binary"]
    lines = [
        "# Stage 09D-SEL source-selection sensitivity report",
        "",
        f"- Generated UTC: `{utc_now()}`",
        "- Scope: existing 53 Stage 09D March 2024 samples only; no Stage 05/06 rerun; no fusion v2.",
        "- Best-available source is an EPIC-referenced retrospective diagnostic oracle, not a production rule.",
        "- Units: agreement/F1/IoU/cloud fractions are unitless fractions; latitude/VZA/SZA are degrees.",
        "",
        "## Policy A Regret Summary",
        md_table(policy_a, ["pixel_group", "n_valid_total", "current_selected_agreement_weighted", "best_available_agreement_weighted", "selection_regret_agreement_weighted", "selected_is_best_fraction_weighted"], 40),
        "",
        "## valid_source_count>=4 Focus",
        md_table(ge4_diag, ["policy", "pixel_group", "n_valid_total", "current_selected_agreement_weighted", "best_available_agreement_weighted", "selection_regret_agreement_weighted", "current_selected_source_distribution"], 10),
        "",
        "## valid_source_count>=4 Source Comparison",
        md_table(ge4_source, ["policy", "source_name", "n_valid_total", "agreement_weighted", "f1_cloud_weighted", "iou_cloud_weighted"], 30),
        "",
        "## IODC Selected Focus",
        md_table(iodc_diag, ["policy", "pixel_group", "n_valid_total", "current_selected_agreement_weighted", "best_available_agreement_weighted", "selection_regret_agreement_weighted"], 30),
        "",
        "## Required Answers",
    ]
    required = [
        "ALL_VALID 中 current selected 与 best available diagnostic source 的差距见 selection_regret_summary。",
        "valid_source_count>=4 是否 100% 或近乎 100% 为 Meteosat-IODC 见 valid_count_ge4 diagnostics 中的 source distribution。",
        "valid_source_count>=4 同像元各 source 指标见 source comparison。",
        "current selected source 排名见 source_rank_by_pixel_group。",
        "selected source 是否经常不是 agreement 最高 source 见 selected_is_best_fraction。",
        "selection regret 是否集中在 IODC 见 IODC selected focus。",
        "selection regret 是否集中在高纬、boundary/broken cloud、valid_count>=4 见 boundary/geometry regret 图表。",
        "排除高纬或 boundary 后 regret 是否仍存在见 abs_lat_lt60 与 non_boundary groups。",
        "Meteosat-dominant low agreement 的解释需要与 VIS 联合判断。",
        "controlled source-selection rerun 若需要，应作为下一阶段诊断，不能自动改生产。",
        "本阶段不能作为 fusion v2，因为 best_available 使用 EPIC 事后参照，实时生产不可用。",
    ]
    lines.extend(f"{i}. {text}" for i, text in enumerate(required, 1))
    lines.extend(
        [
            "",
            "## Quality Control",
            f"- Warning rows: `{len(warnings)}`",
            "- Missing prefusion/source variables are recorded in `logs/stage_09d_sel_warnings.csv` and skipped per group.",
            "- Raw products are immutable; all outputs are derived CSV/PNG/Markdown with manifests.",
            "",
            "## Traceability",
            "- Every PNG is indexed in `07_figures/stage_09d_sel_plot_index.csv` or the case-map plot index.",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage 09d source-selection sensitivity diagnostics.")
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
