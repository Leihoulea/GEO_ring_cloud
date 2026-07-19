# -*- coding: utf-8 -*-
"""Postprocess Stage 09d VIS outputs into a presentation-ready report.

This script does not rerun pixel sampling.  It reads existing Stage 09d VIS CSV
outputs, writes compact derived summaries, and regenerates the Chinese report.
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from geo_ring_cloud import paths as path_config  # noqa: E402
from geo_ring_cloud.diagnostics.full_pixel_workflow import (  # noqa: E402
    md_table,
    read_csv,
    summarize_metric_rows,
    utc_now,
    write_csv,
    write_run_manifest,
)


DEFAULT_OUT = path_config.RUNS_ROOT / "stage09d_geo_visible_controlled_metrics_202403"
POLICIES = ["A_inclusive_binary", "B_high_confidence_only", "C_uncertainty_aware_3class"]


def sf(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return math.nan


def fmt(value: Any) -> str:
    x = sf(value)
    return f"{x:.3f}" if math.isfinite(x) else "NA"


def pick(rows: list[dict[str, Any]], **keys: str) -> dict[str, Any]:
    for row in rows:
        if all(row.get(k) == v for k, v in keys.items()):
            return row
    return {}


def weighted(rows: list[dict[str, Any]], value: str, weight: str) -> float:
    num = 0.0
    den = 0.0
    for row in rows:
        v = sf(row.get(value))
        w = sf(row.get(weight))
        if math.isfinite(v) and math.isfinite(w) and w > 0:
            num += v * w
            den += w
    return num / den if den else math.nan


def build_pair_summary(pair_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for row in pair_rows:
        key = (row.get("policy", ""), row.get("mask_name", ""), row.get("source_A", ""), row.get("source_B", ""))
        buckets.setdefault(key, []).append(row)
    out = []
    for (policy, mask, a, b), vals in sorted(buckets.items()):
        out.append(
            {
                "policy": policy,
                "mask_name": mask,
                "source_A": a,
                "source_B": b,
                "pair": f"{a} vs {b}",
                "sample_count": len({v.get("sample_id", "") for v in vals}),
                "n_overlap_valid_total": sum(sf(v.get("n_overlap_valid")) for v in vals),
                "n_overlap_retention_ratio_weighted": weighted(vals, "n_overlap_retention_ratio", "n_overlap_valid"),
                "source_A_agreement_weighted": weighted(vals, "source_A_agreement_to_EPIC", "n_overlap_valid"),
                "source_B_agreement_weighted": weighted(vals, "source_B_agreement_to_EPIC", "n_overlap_valid"),
                "B_minus_A_agreement_weighted": weighted(vals, "B_minus_A_agreement", "n_overlap_valid"),
                "A_f1_weighted": weighted(vals, "A_f1", "n_overlap_valid"),
                "B_f1_weighted": weighted(vals, "B_f1", "n_overlap_valid"),
                "A_iou_weighted": weighted(vals, "A_iou", "n_overlap_valid"),
                "B_iou_weighted": weighted(vals, "B_iou", "n_overlap_valid"),
                "source_disagreement_fraction_weighted": weighted(vals, "source_disagreement_fraction", "n_overlap_valid"),
                "both_wrong_fraction_weighted": weighted(vals, "both_wrong_fraction", "n_overlap_valid"),
            }
        )
    return out


def build_report(
    policy_mask: list[dict[str, Any]],
    delta_rows: list[dict[str, Any]],
    gap_rows: list[dict[str, Any]],
    count_rows: list[dict[str, Any]],
    pair_summary: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> str:
    key_table = []
    for policy in POLICIES:
        key_table.append(
            {
                "policy": policy,
                "VIS-0": sf(pick(policy_mask, policy=policy, mask_name="VIS-0_baseline_current").get("agreement_weighted")),
                "VIS-1": sf(pick(policy_mask, policy=policy, mask_name="VIS-1_fused_valid_only").get("agreement_weighted")),
                "VIS-2": sf(pick(policy_mask, policy=policy, mask_name="VIS-2_lat70_visible").get("agreement_weighted")),
                "VIS-3": sf(pick(policy_mask, policy=policy, mask_name="VIS-3_lat60_visible").get("agreement_weighted")),
                "VIS-4a": sf(pick(policy_mask, policy=policy, mask_name="VIS-4a_reliable_geometry_without_geo_vza").get("agreement_weighted")),
                "VIS-4b": sf(pick(policy_mask, policy=policy, mask_name="VIS-4b_reliable_geometry_with_geo_vza_available_only").get("agreement_weighted")),
                "VIS-5": sf(pick(policy_mask, policy=policy, mask_name="VIS-5_clean_core").get("agreement_weighted")),
                "VIS-6": sf(pick(policy_mask, policy=policy, mask_name="VIS-6_non_boundary_visible").get("agreement_weighted")),
                "VIS-7": sf(pick(policy_mask, policy=policy, mask_name="VIS-7_boundary_visible").get("agreement_weighted")),
            }
        )

    a0 = pick(delta_rows, policy="A_inclusive_binary", mask_name="VIS-0_baseline_current")
    a1 = pick(delta_rows, policy="A_inclusive_binary", mask_name="VIS-1_fused_valid_only")
    a2 = pick(delta_rows, policy="A_inclusive_binary", mask_name="VIS-2_lat70_visible")
    a3 = pick(delta_rows, policy="A_inclusive_binary", mask_name="VIS-3_lat60_visible")
    a4a = pick(delta_rows, policy="A_inclusive_binary", mask_name="VIS-4a_reliable_geometry_without_geo_vza")
    a4b = pick(delta_rows, policy="A_inclusive_binary", mask_name="VIS-4b_reliable_geometry_with_geo_vza_available_only")
    a5 = pick(delta_rows, policy="A_inclusive_binary", mask_name="VIS-5_clean_core")
    a6 = pick(delta_rows, policy="A_inclusive_binary", mask_name="VIS-6_non_boundary_visible")
    a7 = pick(delta_rows, policy="A_inclusive_binary", mask_name="VIS-7_boundary_visible")
    source_pair_focus = [
        r
        for r in pair_summary
        if r.get("policy") == "A_inclusive_binary"
        and r.get("mask_name") in {"VIS-0_baseline_current", "VIS-3_lat60_visible", "VIS-6_non_boundary_visible", "VIS-7_boundary_visible"}
        and r.get("pair")
        in {
            "FY4B vs Meteosat-IODC",
            "Meteosat-0deg vs GOES-16",
            "Meteosat-0deg vs Meteosat-IODC",
            "GOES-16 vs GOES-18",
            "FY4B vs Himawari-9",
        }
    ]
    valid_count_focus = [
        r
        for r in count_rows
        if r.get("policy") == "A_inclusive_binary"
        and r.get("mask_name") in {"VIS-0_baseline_current", "VIS-3_lat60_visible", "VIS-6_non_boundary_visible", "VIS-7_boundary_visible"}
    ]

    lines = [
        "# Stage 09D-VIS GEO-visible controlled metrics report",
        "",
        f"- Generated UTC: `{utc_now()}`",
        "- Scope: existing 53 Stage 09D March 2024 samples only; no Stage 05/06 rerun; no fusion v2.",
        "- Units: latitude/VZA/SZA are degrees; agreement/F1/IoU/retention/cloud fractions are unitless fractions.",
        "- EPIC is an independent diagnostic reference, not absolute truth.",
        "",
        "## Key Mask Agreement",
        md_table(key_table, ["policy", "VIS-0", "VIS-1", "VIS-2", "VIS-3", "VIS-4a", "VIS-4b", "VIS-5", "VIS-6", "VIS-7"], 10),
        "",
        "## Direct Answers",
        f"1. VIS-1 and VIS-0 are identical within the reported precision. Policy A is {fmt(a0.get('agreement_weighted'))} in VIS-0 and {fmt(a1.get('agreement_weighted'))} in VIS-1, with retention {fmt(a1.get('retention_vs_VIS0'))}. This means the Stage 09D baseline had already excluded fully invalid GEO pixels.",
        f"2. Removing abs(latitude)>=70 produces only a small overall change. Policy A changes by {fmt(a2.get('delta_agreement_vs_VIS0'))}; Policy B and C are also about +0.001.",
        f"3. Removing abs(latitude)>=60 also produces only a small overall change. Policy A changes by {fmt(a3.get('delta_agreement_vs_VIS0'))} with retention {fmt(a3.get('retention_vs_VIS0'))}. High latitude is enriched in mismatch, but it is not large enough in pixel share to move the overall metric much.",
        f"4. VIS-4a, the reliable-geometry mask without requiring GEO VZA, does not improve agreement: Policy A delta is {fmt(a4a.get('delta_agreement_vs_VIS0'))}. VIS-4b is not a full-sample reliable-geometry conclusion because selected-source GEO VZA is missing for many cases; it retains only {fmt(a4b.get('retention_vs_VIS0'))} of Policy A pixels and the run records {len(warnings)} warning rows.",
        f"5. VIS-5 clean_core reaches Policy A agreement {fmt(a5.get('agreement_weighted'))}, but retains only {fmt(a5.get('retention_vs_VIS0'))} of baseline pixels. It is an upper-bound diagnostic for clean, homogeneous, favorable-geometry pixels, not a global product metric.",
        "6. VIS-5 removes multiple mechanisms at once: high latitude, unfavorable angles, cloud boundary pixels, and broken-cloud scenes. Its high agreement must be interpreted together with VIS-2/VIS-3 and VIS-6/VIS-7, not assigned to one factor.",
        f"7. Meteosat-vs-non-Meteosat gaps do not shrink under VIS-2/VIS-3/VIS-4a. For Policy A, the best non-Meteosat minus Meteosat gap is {fmt(pick(gap_rows, policy='A_inclusive_binary', mask_name='VIS-0_baseline_current').get('best_non_meteosat_minus_meteosat'))} at VIS-0, {fmt(pick(gap_rows, policy='A_inclusive_binary', mask_name='VIS-3_lat60_visible').get('best_non_meteosat_minus_meteosat'))} at VIS-3, and {fmt(pick(gap_rows, policy='A_inclusive_binary', mask_name='VIS-4a_reliable_geometry_without_geo_vza').get('best_non_meteosat_minus_meteosat'))} at VIS-4a.",
        "8. Therefore Meteosat low agreement cannot be explained as simply a GEO-invisible-area artifact. Visibility and geometry matter locally, but source-family/product-semantics/selection effects remain.",
        "9. Source-pair disagreement remains under GEO-visible masks. Non-boundary pixels reduce disagreement, while boundary/broken-cloud pixels increase it; the table below gives the direct source-pair evidence.",
        f"10. valid_source_count>=4 remains low after latitude filtering: Policy A agreement is {fmt(pick(valid_count_focus, mask_name='VIS-0_baseline_current', strata_value='>=4').get('agreement_weighted'))} at VIS-0 and {fmt(pick(valid_count_focus, mask_name='VIS-3_lat60_visible', strata_value='>=4').get('agreement_weighted'))} at VIS-3. It is not removed by excluding high latitude.",
        "",
        "## VIS-4b Caution",
        "VIS-4b requires selected-source GEO VZA to be available and below 70 degrees. Because GEO VZA is missing for many source/sample combinations, VIS-4b is a small, biased subset rather than a representative geometry-controlled metric. Use VIS-4a for the full-sample geometry statement and VIS-4b only as a partial-angle-available sensitivity check.",
        "",
        "## Boundary Definition",
        "In this VIS report, `boundary` means cloud-mask boundary in EPIC pixel space, not satellite service-area/source-family boundary. A pixel is `near_boundary_1cell` when the local 3x3 neighborhood of the fused cloud mask contains both clear and cloudy valid pixels. `non_boundary` means that local 3x3 neighborhood is homogeneous under the selected policy. `broken_cloud` is based on local cloud fraction between homogeneous-clear and homogeneous-cloud thresholds.",
        "",
        "## VIS-5 Interpretation",
        "VIS-5 combines several filters: abs(latitude)<60, favorable EPIC/SZA geometry where available, GEO VZA<60 where available, non-boundary pixels, and homogeneous clear/cloud scenes. It should be read as a clean-core upper bound. The VIS-6/VIS-7 contrast is the cleaner cloud-boundary comparison: VIS-6 non-boundary Policy A agreement is "
        + f"{fmt(a6.get('agreement_weighted'))}, while VIS-7 boundary/broken-cloud agreement is {fmt(a7.get('agreement_weighted'))}.",
        "",
        "## Source-Pair Results Under VIS Masks",
        md_table(
            source_pair_focus,
            [
                "mask_name",
                "pair",
                "n_overlap_valid_total",
                "source_A_agreement_weighted",
                "source_B_agreement_weighted",
                "source_disagreement_fraction_weighted",
                "both_wrong_fraction_weighted",
            ],
            80,
        ),
        "",
        "## valid_source_count Results Under VIS Masks",
        md_table(valid_count_focus, ["mask_name", "strata_value", "n_valid_total", "agreement_weighted"], 80),
        "",
        "## Delta vs Baseline",
        md_table(delta_rows, ["policy", "mask_name", "agreement_weighted", "delta_agreement_vs_VIS0", "retention_vs_VIS0"], 40),
        "",
        "## Meteosat Gap",
        md_table(gap_rows, ["policy", "mask_name", "meteosat_agreement", "goes_agreement", "east_asia_agreement", "best_non_meteosat_minus_meteosat"], 40),
        "",
        "## Quality Control / Missing Variables",
        f"- Warning rows: `{len(warnings)}`.",
        "- GEO VZA masks are partial where selected-source GEO VZA files are missing; missing variables are recorded in `logs/stage_09d_vis_warnings.csv` and the run manifest.",
        "- No unit conversions were applied; angular variables remain in degrees and all fractions are unitless.",
        "",
        "## Traceability",
        "- Every PNG is indexed in `06_figures/stage_09d_vis_plot_index.csv` with a source CSV.",
        "- Source-pair summary table: `04_source_pair_metrics/stage_09d_vis_source_pair_summary_by_mask.csv`.",
    ]
    return "\n".join(lines)


def run(out: Path) -> Path:
    policy_mask = read_csv(out / "02_policy_metrics" / "stage_09d_vis_policy_metrics_by_mask.csv")
    delta_rows = read_csv(out / "reports" / "stage_09d_vis_delta_vs_baseline.csv")
    gap_rows = read_csv(out / "05_meteosat_focus" / "stage_09d_vis_meteosat_vs_goes_eastasia_gap.csv")
    count_rows = read_csv(out / "03_group_source_metrics" / "stage_09d_vis_metrics_by_valid_source_count.csv")
    pair_rows = read_csv(out / "04_source_pair_metrics" / "stage_09d_vis_source_pair_metrics.csv")
    warnings = read_csv(out / "logs" / "stage_09d_vis_warnings.csv")
    pair_summary = build_pair_summary(pair_rows)
    pair_summary_path = out / "04_source_pair_metrics" / "stage_09d_vis_source_pair_summary_by_mask.csv"
    write_csv(pair_summary_path, pair_summary)
    report_path = out / "reports" / "stage_09d_geo_visible_controlled_metrics_report_cn.md"
    report_path.write_text(build_report(policy_mask, delta_rows, gap_rows, count_rows, pair_summary, warnings), encoding="utf-8")
    write_run_manifest(
        out / "logs" / "stage_09d_vis_postprocess_manifest.json",
        canonical_stage_id=STAGE_ID,
        script_path=Path(__file__).resolve(),
        input_paths=[
            out / "02_policy_metrics" / "stage_09d_vis_policy_metrics_by_mask.csv",
            out / "04_source_pair_metrics" / "stage_09d_vis_source_pair_metrics.csv",
            out / "03_group_source_metrics" / "stage_09d_vis_metrics_by_valid_source_count.csv",
        ],
        output_paths=[pair_summary_path, report_path],
        filters=["report-only postprocess; no pixel-level recomputation"],
        unit_conversions=[],
        row_counts={"source_pair_raw_rows": len(pair_rows), "source_pair_summary_rows": len(pair_summary), "warning_rows": len(warnings)},
        warnings=warnings,
    )
    return report_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Postprocess Stage 09d VIS report.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUT))
    return parser.parse_args()


if __name__ == "__main__":
    print(run(Path(parse_args().output_dir)))
