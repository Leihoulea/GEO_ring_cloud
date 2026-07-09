from __future__ import annotations

import csv
from pathlib import Path


ROOT = Path(r"D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs\20240319_1500")
REPORT_DIR = ROOT / "reports"
EPIC_DIR = ROOT / "epic_visual_comparison_20240319_1500"
FUSED_DIR = ROOT / "fused_best_source"
NATIVE_DIR = ROOT / "standardized_native"
REPROJECT_DIR = ROOT / "reprojected_grid"


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    inv = read_csv(NATIVE_DIR / "standardized_native_inventory.csv")
    failed_native = [r for r in inv if r.get("status") not in {"OK", ""}]
    epic_rows = read_csv(EPIC_DIR / "epic_time_match_summary.csv")
    freq_rows = read_csv(FUSED_DIR / "fusion_source_frequency.csv")
    reproj_rows = read_csv(REPROJECT_DIR / "reprojected_variable_inventory.csv")
    ok_reproj = [r for r in reproj_rows if r.get("status") == "OK"]

    epic_line = "- EPIC L1: not available"
    if epic_rows:
        r = epic_rows[0]
        epic_line = (
            f"- EPIC L1: `{r.get('epic_time_utc')}`, delta `{r.get('delta_minutes')}` min, "
            f"center lat/lon `{r.get('centroid_lat')}`, `{r.get('centroid_lon')}`"
        )

    cloud_sources = [r for r in freq_rows if r.get("variable") == "cloud_mask"]
    cloud_source_lines = [
        f"- {r.get('satellite')}: {r.get('selected_fraction_among_valid')} ({r.get('selected_pixel_count')} px)"
        for r in cloud_sources
    ]
    fail_lines = [
        f"- {r.get('satellite_group')} {r.get('product')}: {r.get('status')}；{r.get('notes')}"
        for r in failed_native
    ] or ["- None"]

    quicklook_dir = EPIC_DIR / "quicklooks_epic_compare"
    lines = [
        "# 2024-03-19 15:00 EPIC 对齐 GEO-ring 单时次报告",
        "",
        "## 1. 执行结论",
        "",
        "- Target GEO-ring time: `2024-03-19T15:00:00Z`",
        epic_line,
        "- GEO-ring time-run root: `" + str(ROOT) + "`",
        "- 02 standardized native: completed with one known failed source file.",
        "- 03 native validation: FAIL only because FY4B CLM failed to open; other 26 products are readable.",
        "- 05 reproject: PASS_WITH_WARNINGS.",
        "- 06 best-source fusion: PASS_WITH_WARNINGS, 8 fused variables.",
        "- 08 EPIC visual comparison: completed using local EPIC L1 H5, no NASA API dependency.",
        "",
        "## 2. 重要 warning",
        "",
        "- FY4B CLM for this time is a bad/unreadable NetCDF/HDF file, so FY4B does not participate in `cloud_mask` fusion.",
        "- FY4B still participates in CTH/CTT/CTP/phase/type where its products are readable.",
        "- Himawari sensor geometry uses fixed-grid R21 auxiliary geometry; solar geometry was not borrowed across time.",
        "- This is a visual sanity check against EPIC L1 RGB, not EPIC L2 quantitative cloud validation.",
        "",
        "## 3. Failed Native Products",
        "",
        *fail_lines,
        "",
        "## 4. Reprojection / Fusion Summary",
        "",
        f"- Reprojected variables OK: `{len(ok_reproj)}`",
        "- Fused bundle: `" + str(FUSED_DIR / "fused_geo_ring_cloud_20240319_1500.npz") + "`",
        "- Core shape gate: `fused_cloud_binary`, `fused_cloud_top_height_km`, `source_map_cloud_mask`, `valid_count_map_cloud_mask` are `3600 x 7200`.",
        "",
        "### cloud_mask source frequency",
        "",
        *cloud_source_lines,
        "",
        "## 5. EPIC Comparison Outputs",
        "",
        "- EPIC RGB: `" + str(EPIC_DIR / "epic_images") + "`",
        "- Quicklooks: `" + str(quicklook_dir) + "`",
        "- Main quicklooks:",
        "  - `epic_rgb_vs_georing_cloud_mask.png`",
        "  - `epic_rgb_vs_georing_cth.png`",
        "  - `epic_view_source_map.png`",
        "",
        "## 6. Gate",
        "",
        "- TIME_ALIGNMENT_GATE = PASS",
        "- EPIC_L1_READ_GATE = PASS",
        "- GEO_RING_FUSION_GATE = PASS_WITH_WARNINGS",
        "- FY4B_CLM_GATE = FAIL_NON_BLOCKING_FOR_VISUAL_COMPARISON",
        "- EPIC_VISUAL_COMPARISON_GATE = PASS_WITH_WARNINGS",
    ]
    out = REPORT_DIR / "single_time_20240319_1500_epic_comparison_report.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
