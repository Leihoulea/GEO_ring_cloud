# -*- coding: utf-8 -*-
"""Joint interpretation for Stage 09d VIS and SEL diagnostics."""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Any

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from geo_ring_cloud import paths as path_config  # noqa: E402
from geo_ring_cloud.diagnostics.full_pixel_workflow import (  # noqa: E402
    md_table,
    read_csv,
    utc_now,
    write_csv,
    write_run_manifest,
)


DEFAULT_VIS = path_config.RUNS_ROOT / "stage09d_geo_visible_controlled_metrics_202403"
DEFAULT_SEL = path_config.RUNS_ROOT / "stage09d_source_selection_sensitivity_202403"
DEFAULT_OUT = path_config.RUNS_ROOT / "stage09d_vis_sel_joint_interpretation_202403"


def sf(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return math.nan


def pick(rows: list[dict[str, Any]], **keys: str) -> dict[str, Any]:
    for row in rows:
        ok = True
        for key, value in keys.items():
            if row.get(key) != value:
                ok = False
                break
        if ok:
            return row
    return {}


def build_joint(vis_dir: Path, sel_dir: Path, out: Path) -> Path:
    (out / "reports").mkdir(parents=True, exist_ok=True)
    (out / "tables").mkdir(parents=True, exist_ok=True)
    vis_policy = read_csv(vis_dir / "02_policy_metrics" / "stage_09d_vis_policy_metrics_by_mask.csv")
    vis_gap = read_csv(vis_dir / "05_meteosat_focus" / "stage_09d_vis_meteosat_vs_goes_eastasia_gap.csv")
    sel_regret = read_csv(sel_dir / "03_selection_regret" / "stage_09d_sel_selection_regret_summary.csv")
    sel_ge4 = read_csv(sel_dir / "04_valid_count_ge4_focus" / "stage_09d_sel_valid_count_ge4_diagnostics.csv")
    sel_ge4_source = read_csv(sel_dir / "04_valid_count_ge4_focus" / "stage_09d_sel_valid_count_ge4_source_comparison.csv")
    rows = []
    for policy in ["A_inclusive_binary", "B_high_confidence_only", "C_uncertainty_aware_3class"]:
        base = pick(vis_policy, policy=policy, mask_name="VIS-0_baseline_current")
        vis3 = pick(vis_policy, policy=policy, mask_name="VIS-3_lat60_visible")
        clean = pick(vis_policy, policy=policy, mask_name="VIS-5_clean_core")
        all_valid = pick(sel_regret, policy=policy, pixel_group="ALL_VALID")
        ge4 = pick(sel_regret, policy=policy, pixel_group="valid_source_count_ge4")
        rows.append(
            {
                "policy": policy,
                "VIS0_agreement": sf(base.get("agreement_weighted")),
                "VIS3_agreement": sf(vis3.get("agreement_weighted")),
                "VIS3_delta": sf(vis3.get("agreement_weighted")) - sf(base.get("agreement_weighted")),
                "VIS5_clean_core_agreement": sf(clean.get("agreement_weighted")),
                "SEL_ALL_VALID_regret": sf(all_valid.get("selection_regret_agreement_weighted")),
                "SEL_valid_count_ge4_regret": sf(ge4.get("selection_regret_agreement_weighted")),
            }
        )
    summary_csv = out / "tables" / "stage_09d_vis_sel_joint_summary.csv"
    write_csv(summary_csv, rows)
    report = build_report(rows, vis_gap, sel_ge4, sel_ge4_source)
    report_path = out / "reports" / "stage_09d_vis_sel_joint_report_cn.md"
    report_path.write_text(report, encoding="utf-8")
    write_run_manifest(
        out / "reports" / "stage_09d_vis_sel_joint_manifest.json",
        canonical_stage_id=STAGE_ID,
        script_path=Path(__file__).resolve(),
        input_paths=[
            vis_dir / "02_policy_metrics" / "stage_09d_vis_policy_metrics_by_mask.csv",
            vis_dir / "05_meteosat_focus" / "stage_09d_vis_meteosat_vs_goes_eastasia_gap.csv",
            sel_dir / "03_selection_regret" / "stage_09d_sel_selection_regret_summary.csv",
            sel_dir / "04_valid_count_ge4_focus" / "stage_09d_sel_valid_count_ge4_diagnostics.csv",
        ],
        output_paths=[summary_csv, report_path],
        filters=["No new computation; joint interpretation of VIS and SEL outputs."],
        unit_conversions=[],
        row_counts={"joint_summary_rows": len(rows)},
        warnings=[],
    )
    return report_path


def build_report(rows: list[dict[str, Any]], vis_gap: list[dict[str, Any]], sel_ge4: list[dict[str, Any]], sel_ge4_source: list[dict[str, Any]]) -> str:
    a = next((r for r in rows if r["policy"] == "A_inclusive_binary"), {})
    lines = [
        "# Stage 09D VIS + SEL joint interpretation",
        "",
        f"- Generated UTC: `{utc_now()}`",
        "- Scope: joint interpretation only; no new pixel calculation; no fusion v2.",
        "- EPIC remains an independent diagnostic reference, not absolute truth.",
        "",
        "## Joint Summary",
        md_table(rows, ["policy", "VIS0_agreement", "VIS3_agreement", "VIS3_delta", "VIS5_clean_core_agreement", "SEL_ALL_VALID_regret", "SEL_valid_count_ge4_regret"], 10),
        "",
        "## Required Answers",
        f"1. Baseline GEO-valid exclusion: compare VIS-0/VIS-1 in the VIS report. Policy A VIS-0 agreement is `{sf(a.get('VIS0_agreement')):.3f}`.",
        f"2. GEO-visible/reliable masks: Policy A VIS-3 agreement is `{sf(a.get('VIS3_agreement')):.3f}`, delta `{sf(a.get('VIS3_delta')):.3f}`.",
        "3. High latitude/low geometry explains part of mismatch if VIS deltas are positive, but it does not automatically remove source-family gaps.",
        "4. Source-selection sensitivity explains valid_source_count>=4 if SEL regret remains high in that group.",
        f"5. Policy A ALL_VALID diagnostic regret is `{sf(a.get('SEL_ALL_VALID_regret')):.3f}`; valid_count>=4 regret is `{sf(a.get('SEL_valid_count_ge4_regret')):.3f}`.",
        "6. VIS explains comparison-domain effects; SEL explains hard selected-source behavior in multi-source overlap regions. Their relative importance is shown by VIS deltas vs SEL regret.",
        "7. VIS overall deltas are suitable for main text if robust; SEL oracle/regret should be main text only as diagnostic mechanism, with details in supplement.",
        "8. Semantic mapping delta validation is still needed if Meteosat gaps remain after VIS controls.",
        "9. Controlled source-selection rerun may be justified as a next diagnostic stage, but not as automatic production modification.",
        "",
        "## Meteosat Gap Rows",
        md_table(vis_gap, ["policy", "mask_name", "meteosat_agreement", "goes_agreement", "east_asia_agreement", "best_non_meteosat_minus_meteosat"], 30),
        "",
        "## valid_count>=4 SEL Rows",
        md_table(sel_ge4, ["policy", "pixel_group", "n_valid_total", "current_selected_agreement_weighted", "best_available_agreement_weighted", "selection_regret_agreement_weighted"], 20),
        "",
        "## valid_count>=4 Source Comparison",
        md_table(sel_ge4_source, ["policy", "source_name", "n_valid_total", "agreement_weighted", "f1_cloud_weighted", "iou_cloud_weighted"], 30),
    ]
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage 09d VIS/SEL joint report.")
    parser.add_argument("--vis-dir", default=str(DEFAULT_VIS))
    parser.add_argument("--sel-dir", default=str(DEFAULT_SEL))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUT))
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    print(build_joint(Path(args.vis_dir), Path(args.sel_dir), Path(args.output_dir)))
