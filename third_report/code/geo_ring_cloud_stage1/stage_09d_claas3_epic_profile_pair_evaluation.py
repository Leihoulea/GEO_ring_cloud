from __future__ import annotations

import argparse
import csv
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from scipy.ndimage import uniform_filter

import path_config
from geo_ring_cloud_lineage import write_manifest


SCRIPT_DIR = Path(__file__).resolve().parent
STAGE_ID = "stage_09d"
DOMAINS = ("global_common", "replacement_active", "unchanged_control")
AGGREGATIONS = ("nearest", "box_7x7")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_stage08c() -> Any:
    path = SCRIPT_DIR / "08c_epic_cloud_mask_semantic_sensitivity.py"
    spec = importlib.util.spec_from_file_location("stage08c_pair_support", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def profile_root(matrix: dict[str, Any], profile: str) -> Path:
    return Path(next(row["profile_root"] for row in matrix["profile_runs"] if row["source_profile"] == profile))


def box_binary(values: np.ndarray, valid: np.ndarray, radius: int = 3) -> tuple[np.ndarray, np.ndarray]:
    size = 2 * radius + 1
    numerator = uniform_filter(np.where(valid, values, 0.0).astype(np.float32), size=size, mode="constant", cval=0.0)
    denominator = uniform_filter(valid.astype(np.float32), size=size, mode="constant", cval=0.0)
    out_valid = denominator >= 0.5
    fraction = np.zeros(values.shape, dtype=np.float32)
    fraction[out_valid] = numerator[out_valid] / denominator[out_valid]
    return (fraction >= 0.5).astype(np.int16), out_valid


def profile_samples(root: Path, policy: dict[str, Any], epic_lat: np.ndarray, epic_lon: np.ndarray, support: Any) -> dict[str, Any]:
    grid = json.loads((root / "reprojected_grid" / "target_grid_definition.json").read_text(encoding="utf-8"))
    mask, mask_valid = support.load_npz_array(root / "fused_best_source" / "fused_cloud_mask.npz")
    source, source_valid = support.load_npz_array(root / "fused_best_source" / "source_map_cloud_mask.npz")
    source_sample = support.sample_grid_to_points(source, source_valid, epic_lat, epic_lon, grid)
    nearest_raw, nearest_valid = support.sample_grid_to_points(mask, mask_valid, epic_lat, epic_lon, grid)
    nearest_class, nearest_policy_valid = support.apply_policy(nearest_raw, policy["geo"])
    grid_class, grid_policy_valid = support.apply_policy(mask, policy["geo"])
    box_class, box_valid = box_binary(grid_class, mask_valid & grid_policy_valid)
    box_sample = support.sample_grid_to_points(box_class, box_valid, epic_lat, epic_lon, grid)
    return {
        "nearest": (nearest_class, nearest_valid & nearest_policy_valid),
        "box_7x7": box_sample,
        "source": source_sample,
        "source_grid": (source, source_valid),
        "grid": grid,
    }


def comparison_domains(common: np.ndarray, base_source: np.ndarray, base_source_valid: np.ndarray, cand_source: np.ndarray, cand_source_valid: np.ndarray, stable_control: np.ndarray | None = None) -> dict[str, np.ndarray]:
    source_common = base_source_valid & cand_source_valid & np.isfinite(base_source) & np.isfinite(cand_source)
    base_id = np.zeros(base_source.shape, dtype=np.int16)
    cand_id = np.zeros(cand_source.shape, dtype=np.int16)
    base_id[source_common] = np.rint(base_source[source_common]).astype(np.int16)
    cand_id[source_common] = np.rint(cand_source[source_common]).astype(np.int16)
    unchanged = common & source_common & (base_id == cand_id) & ~np.isin(base_id, [0, 5, 7])
    if stable_control is not None:
        unchanged &= stable_control
    return {
        "global_common": common,
        "replacement_active": common & source_common & (base_id == 5) & (cand_id == 7),
        "unchanged_control": unchanged,
    }


def stable_source_neighborhood(sampled: dict[str, dict[str, Any]], lat: np.ndarray, lon: np.ndarray, support: Any, radius: int = 3) -> np.ndarray:
    base_source, base_valid = sampled["operational_baseline"]["source_grid"]
    cand_source, cand_valid = sampled["claas3_candidate"]["source_grid"]
    source_common = base_valid & cand_valid & np.isfinite(base_source) & np.isfinite(cand_source)
    base_id = np.zeros(base_source.shape, dtype=np.int16)
    cand_id = np.zeros(cand_source.shape, dtype=np.int16)
    base_id[source_common] = np.rint(base_source[source_common]).astype(np.int16)
    cand_id[source_common] = np.rint(cand_source[source_common]).astype(np.int16)
    source_changed = ~source_common | (base_id != cand_id)
    changed_fraction = uniform_filter(source_changed.astype(np.float32), size=2 * radius + 1, mode="constant", cval=1.0)
    stable_grid = changed_fraction <= 1e-8
    stable_sample, stable_valid = support.sample_grid_to_points(stable_grid.astype(np.int16), np.ones(stable_grid.shape, dtype=bool), lat, lon, sampled["operational_baseline"]["grid"])
    return stable_valid & (stable_sample == 1)


def sample_metrics(matrix_path: Path, support: Any) -> list[dict[str, Any]]:
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    common = matrix["common_inputs"]
    tag = common["time_tag"]
    epic, lat, lon, _ = support.read_epic_l2(Path(common["epic_l2"]), None, None, None)
    earth = np.isfinite(epic) & np.isfinite(lat) & np.isfinite(lon)
    rows: list[dict[str, Any]] = []
    for policy_name in ("A_inclusive_binary", "B_high_confidence_only"):
        policy = support.EPIC_POLICIES[policy_name]
        epic_class, epic_policy_valid = support.apply_policy(epic, policy["epic"])
        sampled = {
            profile: profile_samples(profile_root(matrix, profile), policy, lat, lon, support)
            for profile in ("operational_baseline", "claas3_candidate")
        }
        base_source, base_source_valid = sampled["operational_baseline"]["source"]
        cand_source, cand_source_valid = sampled["claas3_candidate"]["source"]
        stable_box_control = stable_source_neighborhood(sampled, lat, lon, support)
        for aggregation in AGGREGATIONS:
            base_class, base_valid = sampled["operational_baseline"][aggregation]
            cand_class, cand_valid = sampled["claas3_candidate"][aggregation]
            common_domain = earth & epic_policy_valid & base_valid & cand_valid
            domains = comparison_domains(common_domain, base_source, base_source_valid, cand_source, cand_source_valid, stable_box_control if aggregation == "box_7x7" else None)
            for domain_name in DOMAINS:
                domain = domains[domain_name]
                base = support.binary_metrics(epic_class, base_class, domain)
                cand = support.binary_metrics(epic_class, cand_class, domain)
                rows.append({
                    "run_id": matrix["run_id"],
                    "time_tag": tag,
                    "target_time": common["target_time"],
                    "epic_l2": common["epic_l2"],
                    "policy": policy_name,
                    "aggregation": aggregation,
                    "domain": domain_name,
                    "common_n": int(np.count_nonzero(domain)),
                    "baseline_f1": base.get("f1", float("nan")),
                    "candidate_f1": cand.get("f1", float("nan")),
                    "f1_delta_candidate_minus_baseline": cand.get("f1", float("nan")) - base.get("f1", float("nan")),
                    "baseline_precision": base.get("precision", float("nan")),
                    "candidate_precision": cand.get("precision", float("nan")),
                    "precision_delta_candidate_minus_baseline": cand.get("precision", float("nan")) - base.get("precision", float("nan")),
                    "baseline_recall": base.get("recall", float("nan")),
                    "candidate_recall": cand.get("recall", float("nan")),
                    "recall_delta_candidate_minus_baseline": cand.get("recall", float("nan")) - base.get("recall", float("nan")),
                    "baseline_agreement": base.get("agreement", float("nan")),
                    "candidate_agreement": cand.get("agreement", float("nan")),
                    "agreement_delta_candidate_minus_baseline": cand.get("agreement", float("nan")) - base.get("agreement", float("nan")),
                    "baseline_coverage": float(np.count_nonzero(earth & base_valid) / max(np.count_nonzero(earth), 1)),
                    "candidate_coverage": float(np.count_nonzero(earth & cand_valid) / max(np.count_nonzero(earth), 1)),
                    "coverage_delta_candidate_minus_baseline": float((np.count_nonzero(earth & cand_valid) - np.count_nonzero(earth & base_valid)) / max(np.count_nonzero(earth), 1)),
                    "baseline_only_pixel_count": int(np.count_nonzero(earth & base_valid & ~cand_valid)),
                    "candidate_only_pixel_count": int(np.count_nonzero(earth & cand_valid & ~base_valid)),
                })
    return rows


def bootstrap_ci(values: np.ndarray, seed: int = 73, draws: int = 4000) -> tuple[float, float]:
    if values.size < 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    means = np.mean(values[rng.integers(0, values.size, size=(draws, values.size))], axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 09d common-domain EPIC cloud-mask A/B evaluation")
    parser.add_argument("--matrix-manifest", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    support = load_stage08c()
    rows = [row for manifest in args.matrix_manifest for row in sample_metrics(manifest, support)]
    summary_rows: list[dict[str, Any]] = []
    for policy in ("A_inclusive_binary", "B_high_confidence_only"):
        for aggregation in AGGREGATIONS:
            for domain in DOMAINS:
                policy_rows = [row for row in rows if row["policy"] == policy and row["aggregation"] == aggregation and row["domain"] == domain]
                values = np.asarray([row["f1_delta_candidate_minus_baseline"] for row in policy_rows], dtype=float)
                values = values[np.isfinite(values)]
                lo, hi = bootstrap_ci(values)
                coverage_delta = float(np.mean([row["coverage_delta_candidate_minus_baseline"] for row in policy_rows])) if policy_rows else float("nan")
                if len(values) < 5:
                    decision = "unresolved"
                elif domain == "unchanged_control":
                    decision = "control_pass" if float(np.max(np.abs(values))) <= 1e-12 else "control_mismatch"
                elif lo >= -0.01 and coverage_delta >= -0.01 and float(np.min(values)) >= -0.03:
                    decision = "prefer_claas3" if float(np.mean(values)) > 0 else "conditional"
                elif float(np.max(values)) <= -0.03 or coverage_delta < -0.01:
                    decision = "prefer_operational"
                else:
                    decision = "conditional"
                summary_rows.append({
                    "policy": policy,
                    "aggregation": aggregation,
                    "domain": domain,
                    "sample_block_count": len(values),
                    "macro_mean_f1_delta": float(np.mean(values)) if values.size else float("nan"),
                    "bootstrap_95ci_lower": lo,
                    "bootstrap_95ci_upper": hi,
                    "mean_coverage_delta": coverage_delta,
                    "worst_sample_f1_delta": float(np.min(values)) if values.size else float("nan"),
                    "decision": decision,
                })
    sample_path = args.output_dir / "stage_09d_claas3_epic_common_domain_samples.csv"
    summary_path = args.output_dir / "stage_09d_claas3_epic_profile_pair_summary.csv"
    write_csv(sample_path, rows)
    write_csv(summary_path, summary_rows)
    def summary_row(policy: str, domain: str) -> dict[str, Any]:
        return next(
            row for row in summary_rows
            if row["policy"] == policy and row["aggregation"] == "box_7x7" and row["domain"] == domain
        )

    mask_report_rows = [
        ("Policy A", summary_row("A_inclusive_binary", "replacement_active")),
        ("Policy B", summary_row("B_high_confidence_only", "replacement_active")),
    ]
    control_rows = [
        summary_row("A_inclusive_binary", "unchanged_control"),
        summary_row("B_high_confidence_only", "unchanged_control"),
    ]
    report = args.output_dir / "stage_09d_claas3_epic_profile_pair_report.md"
    report.write_text(
        "\n".join([
            "# Stage 09d CLAAS-3 与 EPIC 云掩膜双轨评估",
            "",
            f"- 生成时间（UTC）：{utc_now()}",
            "- 主要证据域：`replacement_active`，即 baseline source ID 5 且 candidate source ID 7。",
            "- 两轨使用相同 EPIC 像元、共同有效域与 `box_7x7` 近似 FOV 聚合。",
            *[
                f"- {label}：CLAAS3-candidate 减 baseline 的宏平均 F1 为 {row['macro_mean_f1_delta']:+.4f}，"
                f"时次块 bootstrap 95% CI [{row['bootstrap_95ci_lower']:+.4f}, {row['bootstrap_95ci_upper']:+.4f}]，"
                f"五时次最差差值 {row['worst_sample_f1_delta']:+.4f}，判定 `{row['decision']}`。"
                for label, row in mask_report_rows
            ],
            f"- 未替换控制域：Policy A/B 判定分别为 `{control_rows[0]['decision']}` / `{control_rows[1]['decision']}`，F1 差值均为 {control_rows[0]['macro_mean_f1_delta']:+.1f}。",
            "- nearest 结果保留在 CSV 中作为聚合敏感性分析；正文判断以 `box_7x7` 为主。",
            "- EPIC 仅作云掩膜相对诊断，不裁决 CTP/CTT/CPH/COT/CER/CWP，也不构成生产切换依据。",
            "- 这是五时次 preliminary pilot；门槛为 F1 差值 95% CI 下界不低于 -0.01、单时次稳定退化不超过 0.03、覆盖率下降不超过 1 个百分点。",
        ]),
        encoding="utf-8",
    )
    write_manifest(
        args.output_dir / "stage_09d_claas3_epic_profile_pair_manifest.json",
        canonical_stage_id="stage_09d",
        run_id="profile_pair_batch",
        source_profile="profile_pair",
        generating_script=Path(__file__),
        input_paths=args.matrix_manifest,
        output_paths=[sample_path, summary_path, report],
        parameters={"bootstrap_draws": 4000, "sample_block_bootstrap": True, "common_domain": True, "domains": DOMAINS, "aggregations": AGGREGATIONS},
        project_root=path_config.PROJECT_ROOT,
        extra={"product_versions": {"CLAAS3": "405"}},
    )
    print(f"stage_09d OK: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
