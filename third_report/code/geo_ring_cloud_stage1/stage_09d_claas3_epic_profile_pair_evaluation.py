from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
from scipy.ndimage import uniform_filter

import path_config
from geo_ring_cloud.diagnostics.epic_pair import (
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
    paired_classification_metrics,
    prefusion_samples,
    read_csv,
    sample_fused_aux,
    sample_fused_profile,
    selected_geo_vza,
    source_domains,
    source_stability_samples,
    utc_now,
    write_csv,
)
from geo_ring_cloud_lineage import write_manifest


SCRIPT_DIR = Path(__file__).resolve().parent
STAGE_ID = "stage_09d"
FUSED_NAME = "stage_09d_claas3_aligned_per_time_fused.csv"
PAIR_NAME = "stage_09d_claas3_aligned_per_time_source_pair.csv"


def comparison_domains(common: np.ndarray, base_source: np.ndarray, base_source_valid: np.ndarray, cand_source: np.ndarray, cand_source_valid: np.ndarray) -> dict[str, np.ndarray]:
    """Backward-compatible name for the shared paired-domain contract."""
    return source_domains(common, base_source, base_source_valid, cand_source, cand_source_valid)


def box_binary(values: np.ndarray, valid: np.ndarray, window: int = 7, min_support: float = 0.5) -> tuple[np.ndarray, np.ndarray]:
    """Legacy-compatible rectangular cloud-majority helper used by tests."""
    support = uniform_filter(valid.astype(np.float32), size=window, mode="constant", cval=0.0)
    cloud = uniform_filter(((values == 1) & valid).astype(np.float32), size=window, mode="constant", cval=0.0)
    out_valid = support >= min_support
    out = np.zeros(values.shape, dtype=np.int8)
    out[out_valid] = (cloud[out_valid] / support[out_valid] >= 0.5).astype(np.int8)
    return out, out_valid


def _profile_state(context: dict[str, Any], variable: str) -> dict[str, Any]:
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


def _coverage(valid: np.ndarray, universe: np.ndarray) -> float:
    denominator = int(np.count_nonzero(universe))
    return float(np.count_nonzero(valid & universe) / denominator) if denominator else math.nan


def _cache_samples(cache: dict[str, np.ndarray], prefix: str, samples: dict[str, tuple[np.ndarray, np.ndarray]]) -> None:
    for aggregation, (values, valid) in samples.items():
        cache[f"{prefix}__{aggregation}__values"] = values
        cache[f"{prefix}__{aggregation}__valid"] = valid


def _fused_rows(context: dict[str, Any], state: dict[str, Any], cache: dict[str, np.ndarray]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    stable_base = source_stability_samples(context, "operational_baseline", "cloud_mask")
    stable_cand = source_stability_samples(context, "claas3_candidate", "cloud_mask")
    for policy_name, policy in POLICIES.items():
        reference, reference_valid = apply_policy(context["epic"]["cloud_mask"], policy["epic"])
        baseline = sample_fused_profile(context, "operational_baseline", "cloud_mask", policy_name)
        candidate = sample_fused_profile(context, "claas3_candidate", "cloud_mask", policy_name)
        if policy_name in {"A_inclusive_binary", "B_high_confidence_only"}:
            _cache_samples(cache, f"fused__operational_baseline__{policy_name}", baseline)
            _cache_samples(cache, f"fused__claas3_candidate__{policy_name}", candidate)
        for aggregation in AGGREGATIONS:
            base_values, base_valid = baseline[aggregation]
            cand_values, cand_valid = candidate[aggregation]
            common = reference_valid & base_valid & cand_valid
            domains = source_domains(common, state["base_source"], state["base_source_valid"], state["cand_source"], state["cand_source_valid"])
            domains["replacement_active"] &= stable_base[aggregation] & stable_cand[aggregation]
            domains["unchanged_control"] &= stable_base[aggregation] & stable_cand[aggregation]
            universe = reference_valid
            for domain_name, domain in domains.items():
                for stratum_name, stratum in state["strata"].items():
                    use = domain & stratum
                    metrics = paired_classification_metrics(reference, base_values, cand_values, use, policy["classes"], policy["cloud_class"])
                    if int(metrics.get("common_n", 0)) == 0 and stratum_name != "all":
                        continue
                    row = common_metadata(
                        context,
                        diagnostic_type="fused_profile",
                        source_A="operational_baseline",
                        source_B="claas3_candidate",
                        source_A_stream="operational_meteosat_fusion_profile",
                        source_B_stream="claas3_candidate_fusion_profile",
                        policy=policy_name,
                        aggregation=aggregation,
                        aggregation_description="approximate rectangular FOV aggregation" if aggregation.startswith("box_") else "nearest grid-cell sampling",
                        domain=domain_name,
                        stratum=stratum_name,
                    )
                    row.update(metrics)
                    row["status"] = "eligible_per_time" if int(metrics.get("common_n", 0)) > 0 else "unresolved_sparse"
                    stratum_universe = universe & stratum
                    row["A_coverage"] = _coverage(base_valid, stratum_universe)
                    row["B_coverage"] = _coverage(cand_valid, stratum_universe)
                    row["B_minus_A_coverage"] = row["B_coverage"] - row["A_coverage"]
                    rows.append(row)
    return rows


def _pair_rows(context: dict[str, Any], strata: dict[str, np.ndarray], cache: dict[str, np.ndarray]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for policy_name, policy in POLICIES.items():
        reference, reference_valid = apply_policy(context["epic"]["cloud_mask"], policy["epic"])
        samples = prefusion_samples(context, "cloud_mask", policy_name)
        if policy_name in {"A_inclusive_binary", "B_high_confidence_only"}:
            for source, source_samples in samples.items():
                _cache_samples(cache, f"prefusion__{source}__{policy_name}", source_samples)
        for source_a, source_b in PRIMARY_PAIRS:
            for aggregation in AGGREGATIONS:
                a_values, a_valid = samples[source_a][aggregation]
                b_values, b_valid = samples[source_b][aggregation]
                common = reference_valid & a_valid & b_valid
                for stratum_name, stratum in strata.items():
                    metrics = paired_classification_metrics(reference, a_values, b_values, common & stratum, policy["classes"], policy["cloud_class"])
                    if int(metrics.get("common_n", 0)) == 0 and stratum_name != "all":
                        continue
                    row = common_metadata(
                        context,
                        diagnostic_type="prefusion_source_pair",
                        source_A=source_a,
                        source_B=source_b,
                        source_A_stream=SOURCE_STREAM[source_a],
                        source_B_stream=SOURCE_STREAM[source_b],
                        policy=policy_name,
                        aggregation=aggregation,
                        aggregation_description="approximate rectangular FOV aggregation" if aggregation.startswith("box_") else "nearest grid-cell sampling",
                        domain="prefusion_common",
                        stratum=stratum_name,
                    )
                    row.update(metrics)
                    row["status"] = "eligible_per_time" if int(metrics.get("common_n", 0)) > 0 else "unresolved_sparse"
                    rows.append(row)
    return rows


def evaluate_matrix(matrix_path: Path, output_dir: Path, diagnostic_cache: Path | None = None) -> tuple[Path, Path, Path]:
    context = build_context(matrix_path)
    state = _profile_state(context, "cloud_mask")
    cache_arrays: dict[str, np.ndarray] = {}
    fused_rows = _fused_rows(context, state, cache_arrays)
    pair_rows = _pair_rows(context, state["strata"], cache_arrays)
    output_dir.mkdir(parents=True, exist_ok=True)
    fused_path = output_dir / FUSED_NAME
    pair_path = output_dir / PAIR_NAME
    write_csv(fused_path, fused_rows)
    write_csv(pair_path, pair_rows)
    if diagnostic_cache:
        diagnostic_cache.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(diagnostic_cache, **cache_arrays)
    key_inputs = [
        context["epic_path"],
        context["profiles"]["operational_baseline"] / "fused_best_source" / "fused_cloud_mask.npz",
        context["profiles"]["claas3_candidate"] / "fused_best_source" / "fused_cloud_mask.npz",
        context["profiles"]["operational_baseline"] / "reprojected_grid" / "reprojected_variable_inventory.csv",
        context["profiles"]["claas3_candidate"] / "reprojected_grid" / "reprojected_variable_inventory.csv",
    ]
    compact = output_dir / "stage_09d_claas3_aligned_compact_diagnostic_manifest.json"
    compact.write_text(json.dumps({
        "generated_utc": utc_now(),
        "run_id": context["run_id"],
        "time_tag": context["time_tag"],
        "fixed_scene_reference": "EPIC Policy-A morphology",
        "fused_rows": len(fused_rows),
        "source_pair_rows": len(pair_rows),
        "inputs": [{"path": str(path), "size_bytes": path.stat().st_size, "sha256": file_sha256(path)} for path in key_inputs],
        "outputs": [{"path": str(path), "size_bytes": path.stat().st_size, "sha256": file_sha256(path)} for path in (fused_path, pair_path)],
        "diagnostic_cache": {"path": str(diagnostic_cache), "size_bytes": diagnostic_cache.stat().st_size, "sha256": file_sha256(diagnostic_cache)} if diagnostic_cache else None,
        "status": "PASS" if fused_rows and pair_rows else "FAIL",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return fused_path, pair_path, compact


def _number(row: dict[str, str], key: str) -> float:
    try:
        return float(row.get(key, "nan"))
    except (TypeError, ValueError):
        return math.nan


def _finite_mean(values: list[float] | np.ndarray) -> float:
    array = np.asarray(values, dtype=np.float64)
    finite = array[np.isfinite(array)]
    return float(np.mean(finite)) if finite.size else math.nan


def _pooled_binary(group: list[dict[str, str]], prefix: str) -> dict[str, float]:
    tp = sum(_number(row, f"{prefix}_cloud_tp") for row in group)
    fp = sum(_number(row, f"{prefix}_cloud_fp") for row in group)
    fn = sum(_number(row, f"{prefix}_cloud_fn") for row in group)
    precision = tp / (tp + fp) if tp + fp else math.nan
    recall = tp / (tp + fn) if tp + fn else math.nan
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else math.nan
    return {"precision": precision, "recall": recall, "f1": f1}


def summarize(input_root: Path, output_dir: Path, phase: str, max_delta: float, draws: int, seed: int) -> list[Path]:
    all_rows: list[dict[str, str]] = []
    for path in sorted(input_root.rglob(FUSED_NAME)) + sorted(input_root.rglob(PAIR_NAME)):
        for row in read_csv(path):
            row["input_table"] = path.name
            all_rows.append(row)
    if not all_rows:
        raise RuntimeError(f"no Stage 09d aligned per-time tables under {input_root}")
    for row in all_rows:
        row["primary_time_domain"] = str(_number(row, "time_delta_min") <= max_delta)
    key_fields = ("diagnostic_type", "source_A", "source_B", "source_A_stream", "source_B_stream", "policy", "aggregation", "domain", "stratum")
    groups: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    sensitivity: list[dict[str, str]] = []
    for row in all_rows:
        if row["primary_time_domain"] == "True":
            groups[tuple(row.get(key, "") for key in key_fields)].append(row)
        else:
            sensitivity.append(row)
    minimum_blocks = 5 if phase == "pilot" else 10
    summaries: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    for index, (key, rows) in enumerate(sorted(groups.items())):
        eligible = [row for row in rows if _number(row, "common_n") >= 1000]
        total_n = int(sum(_number(row, "common_n") for row in eligible))
        policy = key[5]
        metric = "macro_f1" if policy == "C_uncertainty_aware_3class" else "f1"
        delta_key = f"B_minus_A_{metric}"
        deltas = np.array([_number(row, delta_key) for row in eligible], dtype=np.float64)
        ci_low, ci_high = block_bootstrap(deltas, seed + index, draws)
        coverage_delta = _finite_mean([_number(row, "B_minus_A_coverage") for row in eligible]) if eligible and key[0] == "fused_profile" else math.nan
        status = "eligible" if len(eligible) >= minimum_blocks and total_n >= 50000 else "unresolved_sparse"
        summary = {field: value for field, value in zip(key_fields, key)}
        summary.update({
            "phase": phase,
            "n_time_blocks": len(eligible),
            "common_n": total_n,
            "status": status,
            "decision_metric": metric,
            "equal_time_macro_A": _finite_mean([_number(row, f"A_{metric}") for row in eligible]),
            "equal_time_macro_B": _finite_mean([_number(row, f"B_{metric}") for row in eligible]),
            "equal_time_macro_B_minus_A": _finite_mean(deltas),
            "bootstrap_ci95_low": ci_low,
            "bootstrap_ci95_high": ci_high,
            "mean_coverage_B_minus_A": coverage_delta,
        })
        if policy != "C_uncertainty_aware_3class":
            pooled_a = _pooled_binary(eligible, "A")
            pooled_b = _pooled_binary(eligible, "B")
            summary.update({f"pooled_A_{name}": value for name, value in pooled_a.items()})
            summary.update({f"pooled_B_{name}": value for name, value in pooled_b.items()})
        summaries.append(summary)
        decision = "unresolved_sparse"
        if status == "eligible":
            if ci_low > 0 and (math.isnan(coverage_delta) or coverage_delta >= -0.01):
                decision = "prefer_claas3" if key[1] in {"operational_baseline", "Meteosat-0deg"} and key[2] in {"claas3_candidate", "CLAAS3-0deg"} else "prefer_source_B"
            elif ci_high < 0:
                decision = "prefer_operational" if key[1] in {"operational_baseline", "Meteosat-0deg"} else "prefer_source_A"
            elif ci_low >= -0.01:
                decision = "conditional"
            else:
                decision = "unresolved"
        decisions.append({**{field: value for field, value in zip(key_fields, key)}, "n_time_blocks": len(eligible), "common_n": total_n, "ci95_low": ci_low, "ci95_high": ci_high, "decision": decision})
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "stage_09d_claas3_aligned_bootstrap_summary.csv"
    decision_path = output_dir / "stage_09d_claas3_aligned_state_decision.csv"
    sensitivity_path = output_dir / "stage_09d_claas3_aligned_time_offset_sensitivity.csv"
    write_csv(summary_path, summaries)
    write_csv(decision_path, decisions)
    write_csv(sensitivity_path, sensitivity)
    report_path = output_dir / "stage_09d_claas3_aligned_report_zh.md"
    eligible_count = sum(row["status"] == "eligible" for row in summaries)
    report_path.write_text(
        "# Stage 09d CLAAS-3 对齐测评\n\n"
        f"- 阶段：{phase}\n- 主时间门限：≤{max_delta:g} min\n- bootstrap：{draws} 次，以整时次为块\n"
        f"- 可裁决状态：{eligible_count}/{len(summaries)}；其余保留为 unresolved_sparse。\n"
        "- 主场景分类固定来自 EPIC Policy-A morphology；box-7×7 仅是 approximate rectangular FOV aggregation，不是官方 EPIC PSF。\n"
        "- 结果表示 EPIC-relative 一致性及产品间差异，不代表绝对物理准确性。\n",
        encoding="utf-8",
    )
    write_manifest(
        output_dir / "stage_09d_claas3_aligned_lineage_manifest.json",
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
    parser = argparse.ArgumentParser(description="Stage 09d processing-stream-aware CLAAS-3 cloud-mask evaluation")
    parser.add_argument("--matrix-manifest", type=Path, action="append")
    parser.add_argument("--input-root", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=("pilot", "march"), default="pilot")
    parser.add_argument("--max-primary-time-delta-min", type=float, default=10.0)
    parser.add_argument("--bootstrap-draws", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20240309)
    parser.add_argument("--diagnostic-cache", type=Path)
    args = parser.parse_args()
    if args.matrix_manifest:
        outputs: list[Path] = []
        for matrix in args.matrix_manifest:
            sample_dir = args.output_dir / matrix.parent.name if len(args.matrix_manifest) > 1 else args.output_dir
            outputs.extend(evaluate_matrix(matrix, sample_dir, args.diagnostic_cache))
        write_manifest(
            args.output_dir / "stage_09d_claas3_aligned_lineage_manifest.json",
            canonical_stage_id=STAGE_ID,
            generating_script=Path(__file__),
            input_paths=args.matrix_manifest,
            output_paths=outputs,
            parameters={"phase": args.phase, "max_primary_time_delta_min": args.max_primary_time_delta_min, "scene_reference": "epic", "policies": list(POLICIES), "aggregations": list(AGGREGATIONS)},
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
