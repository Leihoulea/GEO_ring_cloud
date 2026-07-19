# -*- coding: utf-8 -*-
"""Fast post-processing for existing Stage 09d SEL raw outputs."""
from __future__ import annotations

import argparse
import math
import sys
from collections import Counter
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
)


DEFAULT_OUT = path_config.RUNS_ROOT / "stage09d_source_selection_sensitivity_202403"


def sf(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return math.nan


def distribution_sum(rows: list[dict[str, Any]]) -> str:
    counts: Counter[str] = Counter()
    for row in rows:
        text = str(row.get("current_selected_source_distribution", ""))
        for part in text.split(";"):
            if not part or ":" not in part:
                continue
            name, value = part.rsplit(":", 1)
            try:
                counts[name] += int(float(value))
            except Exception:
                pass
    total = sum(counts.values())
    return ";".join(f"{k}:{v}:{v / total:.6f}" for k, v in sorted(counts.items())) if total else ""


def run(out: Path) -> Path:
    raw_path = out / "03_selection_regret" / "stage_09d_sel_selection_regret_summary.csv"
    rows = read_csv(raw_path)
    raw_copy = out / "03_selection_regret" / "stage_09d_sel_selection_regret_by_sample.csv"
    write_csv(raw_copy, rows)
    summary = summarize_metric_rows(rows, ["policy", "pixel_group"], "n_valid")
    for row in summary:
        matching = [r for r in rows if r.get("policy") == row.get("policy") and r.get("pixel_group") == row.get("pixel_group")]
        row["current_selected_source_distribution_aggregated"] = distribution_sum(matching)
    write_csv(raw_path, summary)

    available = read_csv(out / "02_available_source_comparison" / "stage_09d_sel_available_source_metrics_same_pixels.csv")
    ge4_raw = [r for r in rows if r.get("pixel_group") == "valid_source_count_ge4"]
    ge4_source = [r for r in available if r.get("pixel_group") == "valid_source_count_ge4"]
    ge4_diag = summarize_metric_rows(ge4_raw, ["policy", "pixel_group"], "n_valid")
    for row in ge4_diag:
        matching = [r for r in ge4_raw if r.get("policy") == row.get("policy")]
        row["current_selected_source_distribution"] = distribution_sum(matching)
    ge4_source_summary = summarize_metric_rows(ge4_source, ["policy", "source_name"], "n_valid")
    write_csv(out / "04_valid_count_ge4_focus" / "stage_09d_sel_valid_count_ge4_diagnostics.csv", ge4_diag)
    write_csv(out / "04_valid_count_ge4_focus" / "stage_09d_sel_valid_count_ge4_source_comparison.csv", ge4_source_summary)

    iodc_raw = [r for r in rows if r.get("pixel_group") in {"selected_MeteosatIODC", "MeteosatIODC_selected", "selected_MeteosatIODC_and_valid_count_ge4"}]
    iodc_source = [r for r in available if r.get("pixel_group") in {"selected_MeteosatIODC", "MeteosatIODC_selected", "selected_MeteosatIODC_and_valid_count_ge4"}]
    write_csv(out / "05_meteosat_iodc_selected_focus" / "stage_09d_sel_iodc_selected_diagnostics.csv", summarize_metric_rows(iodc_raw, ["policy", "pixel_group"], "n_valid"))
    write_csv(out / "05_meteosat_iodc_selected_focus" / "stage_09d_sel_iodc_selected_available_source_comparison.csv", summarize_metric_rows(iodc_source, ["policy", "pixel_group", "source_name"], "n_valid"))

    report = build_report(summary, ge4_diag, ge4_source_summary, summarize_metric_rows(iodc_raw, ["policy", "pixel_group"], "n_valid"))
    report_path = out / "reports" / "stage_09d_source_selection_sensitivity_report_cn.md"
    report_path.write_text(report, encoding="utf-8")
    return report_path


def build_report(summary: list[dict[str, Any]], ge4_diag: list[dict[str, Any]], ge4_source: list[dict[str, Any]], iodc_diag: list[dict[str, Any]]) -> str:
    policy_a = [r for r in summary if r.get("policy") == "A_inclusive_binary"]
    return "\n".join(
        [
            "# Stage 09D-SEL source-selection sensitivity report",
            "",
            f"- Generated UTC: `{utc_now()}`",
            "- Scope: existing 53 Stage 09D March 2024 samples only; no Stage 05/06 rerun; no fusion v2.",
            "- Best-available source is an EPIC-referenced retrospective diagnostic oracle, not a production rule.",
            "- Units: agreement/F1/IoU/cloud fractions are unitless fractions; latitude/VZA/SZA are degrees.",
            "",
            "## Policy A Regret Summary",
            md_table(policy_a, ["pixel_group", "n_valid_total", "current_selected_agreement_weighted", "best_available_agreement_weighted", "selection_regret_agreement_weighted", "selected_is_best_fraction_weighted", "current_selected_source_distribution_aggregated"], 40),
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
            "## Quality Control",
            "- Raw by-sample regret rows are preserved as `03_selection_regret/stage_09d_sel_selection_regret_by_sample.csv`.",
            "- Summary rows are written to `03_selection_regret/stage_09d_sel_selection_regret_summary.csv`.",
        ]
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Postprocess Stage 09d SEL outputs.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUT))
    return parser.parse_args()


if __name__ == "__main__":
    print(run(Path(parse_args().output_dir)))
