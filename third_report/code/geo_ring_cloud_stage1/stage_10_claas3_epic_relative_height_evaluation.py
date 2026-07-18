from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

import path_config
from geo_ring_cloud_epic_pair_diagnostics import (
    AGGREGATIONS,
    POLICIES,
    PRIMARY_PAIRS,
    SOURCE_STREAM,
    add_profile_strata,
    add_visibility_strata,
    apply_policy,
    block_bootstrap,
    build_context,
    common_metadata,
    file_sha256,
    height_class,
    paired_height_metrics,
    prefusion_samples,
    read_csv,
    sample_fused_aux,
    sample_fused_profile,
    selected_geo_vza,
    utc_now,
    write_csv,
)
from geo_ring_cloud_lineage import write_manifest


SCRIPT_DIR = Path(__file__).resolve().parent
STAGE_ID = "stage_10"
FUSED_NAME = "stage_10_claas3_aligned_per_time_fused.csv"
PAIR_NAME = "stage_10_claas3_aligned_per_time_source_pair.csv"
CTH_POLICIES = ("A_inclusive_binary", "B_high_confidence_only")
BANDS = ("A_band", "B_band")


def _profile_state(context: dict[str, Any]) -> dict[str, Any]:
    variable = "cloud_top_height_km"
    base_source, base_source_valid = sample_fused_aux(context, "operational_baseline", f"source_map_{variable}")
    cand_source, cand_source_valid = sample_fused_aux(context, "claas3_candidate", f"source_map_{variable}")
    base_count, base_count_valid = sample_fused_aux(context, "operational_baseline", f"valid_count_map_{variable}")
    cand_count, cand_count_valid = sample_fused_aux(context, "claas3_candidate", f"valid_count_map_{variable}")
    base_source_valid &= base_count_valid
    cand_source_valid &= cand_count_valid
    base_vza = selected_geo_vza(context, "operational_baseline", base_source, base_source_valid)
    cand_vza = selected_geo_vza(context, "claas3_candidate", cand_source, cand_source_valid)
    strata = add_profile_strata(context["base_strata"], base_source, cand_source, base_count, cand_count, base_vza, cand_vza)
    strata = add_visibility_strata(strata, context["epic"], context["morphology"], base_source_valid, cand_source_valid, base_count, cand_count, base_vza, cand_vza)
    return {
        "base_source": base_source,
        "base_source_valid": base_source_valid,
        "cand_source": cand_source,
        "cand_source_valid": cand_source_valid,
        "base_count": base_count,
        "cand_count": cand_count,
        "base_vza": base_vza,
        "cand_vza": cand_vza,
        "strata": strata,
    }


def _height_strata(base: dict[str, np.ndarray], epic_height: np.ndarray, source_a: np.ndarray, source_b: np.ndarray) -> dict[str, np.ndarray]:
    result = dict(base)
    for prefix, values in (("EPIC_height", epic_height), ("source_A_height", source_a), ("source_B_height", source_b)):
        classes = height_class(values)
        result[f"{prefix}:low_0-3km"] = classes == 0
        result[f"{prefix}:mid_3-7km"] = classes == 1
        result[f"{prefix}:high_ge7km"] = classes == 2
    return result


def _cth_domains(
    common: np.ndarray,
    cloud_common: np.ndarray,
    epic_cloud: np.ndarray,
    geo_a_cloud: np.ndarray,
    geo_b_cloud: np.ndarray,
    epic_height: np.ndarray,
    source_a: np.ndarray,
    source_b: np.ndarray,
    context: dict[str, Any],
    clean_geometry: np.ndarray,
) -> dict[str, np.ndarray]:
    both_geo_cloud = (geo_a_cloud == 1) & (geo_b_cloud == 1)
    both_geo_clear = (geo_a_cloud == 0) & (geo_b_cloud == 0)
    epic_is_cloud = epic_cloud == 1
    epic_is_clear = epic_cloud == 0
    cloud_domain = common & cloud_common
    d1 = cloud_domain & epic_is_cloud & both_geo_cloud
    boundary_or_broken = context["morphology"]["boundary"] | (context["morphology"]["scene"] == 1)
    clean_scene = (context["morphology"]["scene"] == 2) & ~context["morphology"]["boundary"] & clean_geometry
    return {
        "D0_common_valid_cth": common,
        "D1_both_cloud": d1,
        "D3_EPIC_cloud_GEO_not_cloud": cloud_domain & epic_is_cloud & both_geo_clear,
        "D4_GEO_cloud_EPIC_not_cloud": cloud_domain & epic_is_clear & both_geo_cloud,
        "D5_clean_core_cloud": d1 & clean_scene,
        "D6_boundary_or_broken_cloud": d1 & boundary_or_broken,
        "D7_high_cloud": d1 & ((epic_height >= 7) | (source_a >= 7) | (source_b >= 7)),
    }


def _cached_samples(cache: Any, prefix: str) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    return {aggregation: (np.asarray(cache[f"{prefix}__{aggregation}__values"]), np.asarray(cache[f"{prefix}__{aggregation}__valid"], dtype=bool)) for aggregation in AGGREGATIONS}


def _mask_samples(context: dict[str, Any], policy_name: str, cache: Any | None = None) -> tuple[dict[str, tuple[np.ndarray, np.ndarray]], dict[str, tuple[np.ndarray, np.ndarray]]]:
    if cache is not None:
        return (
            _cached_samples(cache, f"fused__operational_baseline__{policy_name}"),
            _cached_samples(cache, f"fused__claas3_candidate__{policy_name}"),
        )
    return (
        sample_fused_profile(context, "operational_baseline", "cloud_mask", policy_name),
        sample_fused_profile(context, "claas3_candidate", "cloud_mask", policy_name),
    )


def _fused_rows(context: dict[str, Any], state: dict[str, Any], cache: Any | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    baseline_cth = sample_fused_profile(context, "operational_baseline", "cloud_top_height_km")
    candidate_cth = sample_fused_profile(context, "claas3_candidate", "cloud_top_height_km")
    for policy_name in CTH_POLICIES:
        policy = POLICIES[policy_name]
        epic_cloud, epic_cloud_valid = apply_policy(context["epic"]["cloud_mask"], policy["epic"])
        base_masks, cand_masks = _mask_samples(context, policy_name, cache)
        for aggregation in AGGREGATIONS:
            base_height, base_height_valid = baseline_cth[aggregation]
            cand_height, cand_height_valid = candidate_cth[aggregation]
            base_cloud, base_cloud_valid = base_masks[aggregation]
            cand_cloud, cand_cloud_valid = cand_masks[aggregation]
            cloud_common = epic_cloud_valid & base_cloud_valid & cand_cloud_valid
            clean_geometry = state["strata"].get("visibility:clean_geometry", np.zeros(epic_cloud.shape, dtype=bool))
            for band in BANDS:
                epic_height = context["epic"][band]
                epic_height_valid = context["epic"][f"{band}_valid"]
                common = epic_height_valid & base_height_valid & cand_height_valid
                domains = _cth_domains(common, cloud_common, epic_cloud, base_cloud, cand_cloud, epic_height, base_height, cand_height, context, clean_geometry)
                strata = _height_strata(state["strata"], epic_height, base_height, cand_height)
                for domain_name, domain in domains.items():
                    for stratum_name, stratum in strata.items():
                        metrics = paired_height_metrics(epic_height, base_height, cand_height, domain & stratum)
                        if int(metrics.get("common_n", 0)) == 0 and stratum_name != "all":
                            continue
                        row = common_metadata(
                            context,
                            diagnostic_type="fused_profile",
                            source_A="operational_baseline",
                            source_B="claas3_candidate",
                            source_A_stream="operational_meteosat_fusion_profile",
                            source_B_stream="claas3_candidate_fusion_profile",
                            band=band,
                            policy=policy_name,
                            aggregation=aggregation,
                            aggregation_description="approximate rectangular FOV aggregation" if aggregation.startswith("box_") else "nearest grid-cell sampling",
                            domain=domain_name,
                            stratum=stratum_name,
                        )
                        row.update(metrics)
                        row["status"] = "eligible_per_time" if int(metrics.get("common_n", 0)) > 0 else "unresolved_sparse"
                        rows.append(row)
    return rows


def _pair_rows(context: dict[str, Any], base_strata: dict[str, np.ndarray], cache: Any | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cth_samples = prefusion_samples(context, "cloud_top_height_km")
    for policy_name in CTH_POLICIES:
        policy = POLICIES[policy_name]
        epic_cloud, epic_cloud_valid = apply_policy(context["epic"]["cloud_mask"], policy["epic"])
        mask_samples = {source: _cached_samples(cache, f"prefusion__{source}__{policy_name}") for source in {item for pair in PRIMARY_PAIRS for item in pair}} if cache is not None else prefusion_samples(context, "cloud_mask", policy_name)
        for source_a, source_b in PRIMARY_PAIRS:
            for aggregation in AGGREGATIONS:
                a_height, a_height_valid = cth_samples[source_a][aggregation]
                b_height, b_height_valid = cth_samples[source_b][aggregation]
                a_cloud, a_cloud_valid = mask_samples[source_a][aggregation]
                b_cloud, b_cloud_valid = mask_samples[source_b][aggregation]
                clean_geometry = base_strata.get("visibility:clean_geometry", np.zeros(a_cloud.shape, dtype=bool))
                for band in BANDS:
                    epic_height = context["epic"][band]
                    common = context["epic"][f"{band}_valid"] & a_height_valid & b_height_valid
                    cloud_common = epic_cloud_valid & a_cloud_valid & b_cloud_valid
                    domains = _cth_domains(common, cloud_common, epic_cloud, a_cloud, b_cloud, epic_height, a_height, b_height, context, clean_geometry)
                    strata = _height_strata(base_strata, epic_height, a_height, b_height)
                    for domain_name, domain in domains.items():
                        for stratum_name, stratum in strata.items():
                            metrics = paired_height_metrics(epic_height, a_height, b_height, domain & stratum)
                            if int(metrics.get("common_n", 0)) == 0 and stratum_name != "all":
                                continue
                            row = common_metadata(
                                context,
                                diagnostic_type="prefusion_source_pair",
                                source_A=source_a,
                                source_B=source_b,
                                source_A_stream=SOURCE_STREAM[source_a],
                                source_B_stream=SOURCE_STREAM[source_b],
                                band=band,
                                policy=policy_name,
                                aggregation=aggregation,
                                aggregation_description="approximate rectangular FOV aggregation" if aggregation.startswith("box_") else "nearest grid-cell sampling",
                                domain=domain_name,
                                stratum=stratum_name,
                            )
                            row.update(metrics)
                            row["status"] = "eligible_per_time" if int(metrics.get("common_n", 0)) > 0 else "unresolved_sparse"
                            rows.append(row)
    return rows


def evaluate_matrix(matrix_path: Path, output_dir: Path, diagnostic_cache: Path | None = None) -> tuple[Path, Path, Path]:
    context = build_context(matrix_path)
    state = _profile_state(context)
    cache = np.load(diagnostic_cache, allow_pickle=False) if diagnostic_cache and diagnostic_cache.exists() else None
    try:
        fused_rows = _fused_rows(context, state, cache)
        pair_rows = _pair_rows(context, state["strata"], cache)
    finally:
        if cache is not None:
            cache.close()
    output_dir.mkdir(parents=True, exist_ok=True)
    fused_path = output_dir / FUSED_NAME
    pair_path = output_dir / PAIR_NAME
    write_csv(fused_path, fused_rows)
    write_csv(pair_path, pair_rows)
    key_inputs = [
        context["epic_path"],
        context["profiles"]["operational_baseline"] / "fused_best_source" / "fused_cloud_top_height_km.npz",
        context["profiles"]["claas3_candidate"] / "fused_best_source" / "fused_cloud_top_height_km.npz",
        context["profiles"]["operational_baseline"] / "reprojected_grid" / "reprojected_variable_inventory.csv",
        context["profiles"]["claas3_candidate"] / "reprojected_grid" / "reprojected_variable_inventory.csv",
    ]
    compact = output_dir / "stage_10_claas3_aligned_compact_diagnostic_manifest.json"
    compact.write_text(json.dumps({
        "generated_utc": utc_now(),
        "run_id": context["run_id"],
        "time_tag": context["time_tag"],
        "fixed_scene_reference": "EPIC Policy-A morphology",
        "cloud_mask_diagnostic_cache": str(diagnostic_cache) if diagnostic_cache else "",
        "height_interpretation": "EPIC-relative difference; EPIC effective height is not CTH truth",
        "fused_rows": len(fused_rows),
        "source_pair_rows": len(pair_rows),
        "inputs": [{"path": str(path), "size_bytes": path.stat().st_size, "sha256": file_sha256(path)} for path in key_inputs],
        "outputs": [{"path": str(path), "size_bytes": path.stat().st_size, "sha256": file_sha256(path)} for path in (fused_path, pair_path)],
        "status": "PASS" if fused_rows and pair_rows else "FAIL",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return fused_path, pair_path, compact


def _number(row: dict[str, str], key: str) -> float:
    try:
        return float(row.get(key, "nan"))
    except (TypeError, ValueError):
        return math.nan


def summarize(input_root: Path, output_dir: Path, phase: str, max_delta: float, draws: int, seed: int) -> list[Path]:
    all_rows: list[dict[str, str]] = []
    for path in sorted(input_root.rglob(FUSED_NAME)) + sorted(input_root.rglob(PAIR_NAME)):
        for row in read_csv(path):
            row["input_table"] = path.name
            all_rows.append(row)
    if not all_rows:
        raise RuntimeError(f"no Stage 10 aligned per-time tables under {input_root}")
    key_fields = ("diagnostic_type", "source_A", "source_B", "source_A_stream", "source_B_stream", "band", "policy", "aggregation", "domain", "stratum")
    groups: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    sensitivity: list[dict[str, str]] = []
    for row in all_rows:
        if _number(row, "time_delta_min") <= max_delta:
            groups[tuple(row.get(key, "") for key in key_fields)].append(row)
        else:
            sensitivity.append(row)
    minimum_blocks = 5 if phase == "pilot" else 10
    summaries: list[dict[str, Any]] = []
    for index, (key, rows) in enumerate(sorted(groups.items())):
        eligible = [row for row in rows if _number(row, "common_n") >= 1000]
        total_n = int(sum(_number(row, "common_n") for row in eligible))
        deltas = np.array([_number(row, "B_minus_A_mae_km") for row in eligible], dtype=np.float64)
        ci_low, ci_high = block_bootstrap(deltas, seed + index, draws)
        status = "eligible" if len(eligible) >= minimum_blocks and total_n >= 50000 else "unresolved_sparse"
        sum_n = float(total_n)
        summary = {field: value for field, value in zip(key_fields, key)}
        summary.update({
            "phase": phase,
            "n_time_blocks": len(eligible),
            "common_n": total_n,
            "status": status,
            "equal_time_macro_A_mae_km": float(np.nanmean([_number(row, "A_mae_km") for row in eligible])) if eligible else math.nan,
            "equal_time_macro_B_mae_km": float(np.nanmean([_number(row, "B_mae_km") for row in eligible])) if eligible else math.nan,
            "equal_time_macro_B_minus_A_mae_km": float(np.nanmean(deltas)) if deltas.size else math.nan,
            "bootstrap_ci95_low_km": ci_low,
            "bootstrap_ci95_high_km": ci_high,
            "pooled_A_bias_km": sum(_number(row, "A_sum_error_km") for row in eligible) / sum_n if sum_n else math.nan,
            "pooled_B_bias_km": sum(_number(row, "B_sum_error_km") for row in eligible) / sum_n if sum_n else math.nan,
            "pooled_A_mae_km": sum(_number(row, "A_sum_abs_error_km") for row in eligible) / sum_n if sum_n else math.nan,
            "pooled_B_mae_km": sum(_number(row, "B_sum_abs_error_km") for row in eligible) / sum_n if sum_n else math.nan,
            "pooled_A_rmse_km": math.sqrt(sum(_number(row, "A_sum_squared_error_km2") for row in eligible) / sum_n) if sum_n else math.nan,
            "pooled_B_rmse_km": math.sqrt(sum(_number(row, "B_sum_squared_error_km2") for row in eligible) / sum_n) if sum_n else math.nan,
        })
        summaries.append(summary)
    coupled: dict[tuple[str, ...], dict[str, dict[str, Any]]] = defaultdict(dict)
    coupled_fields = ("diagnostic_type", "source_A", "source_B", "source_A_stream", "source_B_stream", "policy", "aggregation", "domain", "stratum")
    for row in summaries:
        coupled[tuple(str(row[field]) for field in coupled_fields)][str(row["band"])] = row
    decisions: list[dict[str, Any]] = []
    for key, bands in sorted(coupled.items()):
        a = bands.get("A_band")
        b = bands.get("B_band")
        decision = "unresolved"
        if not a or not b or a["status"] != "eligible" or b["status"] != "eligible":
            decision = "unresolved_sparse"
        elif a["bootstrap_ci95_high_km"] < 0 and b["bootstrap_ci95_high_km"] < 0:
            decision = "prefer_claas3" if key[1] in {"operational_baseline", "Meteosat-0deg"} and key[2] in {"claas3_candidate", "CLAAS3-0deg"} else "prefer_source_B"
        elif a["bootstrap_ci95_low_km"] > 0 and b["bootstrap_ci95_low_km"] > 0:
            decision = "prefer_operational" if key[1] in {"operational_baseline", "Meteosat-0deg"} else "prefer_source_A"
        else:
            a_mean = a["equal_time_macro_B_minus_A_mae_km"]
            b_mean = b["equal_time_macro_B_minus_A_mae_km"]
            decision = "unresolved_band_conflict" if a_mean * b_mean < 0 else "conditional"
        decisions.append({**{field: value for field, value in zip(coupled_fields, key)}, "A_band_n_time_blocks": a["n_time_blocks"] if a else 0, "B_band_n_time_blocks": b["n_time_blocks"] if b else 0, "A_band_ci_low_km": a["bootstrap_ci95_low_km"] if a else math.nan, "A_band_ci_high_km": a["bootstrap_ci95_high_km"] if a else math.nan, "B_band_ci_low_km": b["bootstrap_ci95_low_km"] if b else math.nan, "B_band_ci_high_km": b["bootstrap_ci95_high_km"] if b else math.nan, "decision": decision})
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "stage_10_claas3_aligned_bootstrap_summary.csv"
    decision_path = output_dir / "stage_10_claas3_aligned_state_decision.csv"
    sensitivity_path = output_dir / "stage_10_claas3_aligned_time_offset_sensitivity.csv"
    write_csv(summary_path, summaries)
    write_csv(decision_path, decisions)
    write_csv(sensitivity_path, sensitivity)
    report_path = output_dir / "stage_10_claas3_aligned_report_zh.md"
    report_path.write_text(
        "# Stage 10 CLAAS-3 对齐测评\n\n"
        f"- 阶段：{phase}\n- 主时间门限：≤{max_delta:g} min\n- bootstrap：{draws} 次，以整时次为块。\n"
        "- 主空间判断为 box-7×7 approximate rectangular FOV aggregation；同时保留 nearest、3×3、5×5 敏感性。\n"
        "- 所有高度量均为 EPIC-relative difference。EPIC A/B effective cloud height 不是云顶高度真值。\n"
        "- A/B band 方向冲突时固定为 unresolved_band_conflict，不自动切换生产源。\n",
        encoding="utf-8",
    )
    write_manifest(
        output_dir / "stage_10_claas3_aligned_lineage_manifest.json",
        canonical_stage_id=STAGE_ID,
        generating_script=Path(__file__),
        input_paths=[input_root],
        output_paths=[summary_path, decision_path, sensitivity_path, report_path],
        parameters={"phase": phase, "max_primary_time_delta_min": max_delta, "bootstrap_draws": draws, "seed": seed, "scene_reference": "epic"},
        project_root=path_config.PROJECT_ROOT,
        run_id=output_dir.name,
        source_profile="profile_pair",
    )
    return [summary_path, decision_path, sensitivity_path, report_path]


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 10 processing-stream-aware CLAAS-3 EPIC-relative height evaluation")
    parser.add_argument("--matrix-manifest", type=Path, action="append")
    parser.add_argument("--input-root", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=("pilot", "march"), default="pilot")
    parser.add_argument("--max-primary-time-delta-min", type=float, default=10.0)
    parser.add_argument("--bootstrap-draws", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20240310)
    parser.add_argument("--diagnostic-cache", type=Path)
    args = parser.parse_args()
    if args.matrix_manifest:
        outputs: list[Path] = []
        for matrix in args.matrix_manifest:
            sample_dir = args.output_dir / matrix.parent.name if len(args.matrix_manifest) > 1 else args.output_dir
            outputs.extend(evaluate_matrix(matrix, sample_dir, args.diagnostic_cache))
        write_manifest(
            args.output_dir / "stage_10_claas3_aligned_lineage_manifest.json",
            canonical_stage_id=STAGE_ID,
            generating_script=Path(__file__),
            input_paths=args.matrix_manifest,
            output_paths=outputs,
            parameters={"phase": args.phase, "max_primary_time_delta_min": args.max_primary_time_delta_min, "scene_reference": "epic", "policies": list(CTH_POLICIES), "aggregations": list(AGGREGATIONS), "bands": list(BANDS)},
            project_root=path_config.PROJECT_ROOT,
            run_id=args.output_dir.name,
            source_profile="profile_pair",
        )
    elif args.input_root:
        summarize(args.input_root, args.output_dir, args.phase, args.max_primary_time_delta_min, args.bootstrap_draws, args.seed)
    else:
        parser.error("one of --matrix-manifest or --input-root is required")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
