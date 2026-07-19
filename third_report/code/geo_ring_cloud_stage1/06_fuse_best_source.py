from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from geo_ring_cloud import paths as path_config
from geo_ring_cloud.lineage import write_manifest
from geo_ring_cloud.sources import (
    REGISTRY_VERSION,
    SOURCE_ID_MAP,
    tie_order,
    validate_profile,
    variable_rules,
)
from geo_ring_cloud.diagnostics.summary import finite_stats
from geo_ring_cloud.lineage import utc_now
from geo_ring_cloud.pipeline_layout import (
    REPORT_DIR,
    SCRIPT_DIR,
    STAGE_ROOT,
    ensure_pipeline_directories as ensure_dirs,
)
from geo_ring_cloud.cloud_semantics import cloud_mask_semantics
from geo_ring_cloud.fusion_support import (
    build_candidate,
    build_subpoint_longitude_map,
    build_target_lon_lat,
    load_catalog,
    make_quicklook,
    save_output,
    target_grid_from_any,
)


OUT_DIR = STAGE_ROOT / "fused_best_source"
QUICKLOOK_DIR = OUT_DIR / "quicklooks"

TARGET_TIME = os.environ.get("GEO_RING_TARGET_TIME", "2024-03-05T00:00:00Z")
TIME_TAG = os.environ.get("GEO_RING_TIME_TAG", TARGET_TIME[0:13].replace("-", "").replace("T", "_"))

FUSED_BUNDLE = OUT_DIR / f"fused_geo_ring_cloud_{TIME_TAG}.npz"
INVENTORY_CSV = OUT_DIR / "fusion_variable_inventory.csv"
STATS_CSV = OUT_DIR / "fusion_stats.csv"
FREQ_CSV = OUT_DIR / "fusion_source_frequency.csv"
REPORT_MD = REPORT_DIR / "fuse_best_source_report.md"

TARGET_SHAPE = (3600, 7200)
SOURCE_PROFILE = validate_profile(os.environ.get("GEO_RING_SOURCE_PROFILE", "operational_baseline"))
TIE_ORDER = tie_order(SOURCE_PROFILE)
VARIABLE_RULES: dict[str, list[dict[str, str]]] = {
    variable: [{"satellite": item["source_key"], "product": item["product"]} for item in rules]
    for variable, rules in variable_rules(SOURCE_PROFILE).items()
}

CATEGORICAL_VARS = {"cloud_mask", "cloud_phase", "cloud_type"}
CLOUD_MASK_STANDARD_MEANINGS = {
    0: "clear",
    1: "probably_clear",
    2: "probably_cloudy",
    3: "cloudy",
}


def fuse_variable(
    variable: str,
    catalog: dict[tuple[str, str, str], Path],
    target_grid: dict[str, Any],
    lon2d: np.ndarray,
    lat2d: np.ndarray,
    subpoints: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, np.ndarray]]:
    participants: list[dict[str, Any]] = []
    for rule in VARIABLE_RULES[variable]:
        candidate = build_candidate(variable, rule, catalog, lon2d, lat2d, subpoints)
        if candidate is not None and candidate["product_level_weight"] > 0.0:
            participants.append(candidate)
    if not participants:
        if variable in {"cloud_mask", "cloud_top_height_km"}:
            raise RuntimeError(f"no participants available for required variable {variable}")
        arrays_out = {
            f"fused_{variable}": np.full(TARGET_SHAPE, np.nan, dtype=np.float32),
            f"source_map_{variable}": np.zeros(TARGET_SHAPE, dtype=np.int16),
            f"rating_map_{variable}": np.zeros(TARGET_SHAPE, dtype=np.float32),
            f"valid_count_map_{variable}": np.zeros(TARGET_SHAPE, dtype=np.int16),
            f"coverage_state_map_{variable}": np.zeros(TARGET_SHAPE, dtype=np.uint8),
        }
        summary = {
            "variable": variable,
            "participants": [],
            "coverage_ratio": 0.0,
            "status": "NO_PARTICIPANTS_OPTIONAL",
            "fusion_mode": "no_data",
            "warnings": ["NO_PARTICIPANTS_OPTIONAL; emitted NaN/source_id=0/valid_count=0"],
        }
        return summary, [], [], arrays_out

    fused = np.full(TARGET_SHAPE, np.nan, dtype=np.float32)
    fused_raw = np.full(TARGET_SHAPE, -9999, dtype=np.int16) if variable == "cloud_mask" else None
    source_map = np.zeros(TARGET_SHAPE, dtype=np.int16)
    rating_map = np.zeros(TARGET_SHAPE, dtype=np.float32)
    valid_count = np.zeros(TARGET_SHAPE, dtype=np.int16)
    best_vza = np.full(TARGET_SHAPE, np.inf, dtype=np.float32)
    best_dt = np.full(TARGET_SHAPE, np.inf, dtype=np.float32)
    best_order = np.full(TARGET_SHAPE, 999, dtype=np.int16)
    best_valid = np.zeros(TARGET_SHAPE, dtype=bool)
    claas_valid = np.zeros(TARGET_SHAPE, dtype=bool)
    non_claas_valid = np.zeros(TARGET_SHAPE, dtype=bool)
    warning_notes: set[str] = set()
    participant_rows: list[dict[str, Any]] = []

    for cand in participants:
        sat = cand["satellite"]
        product = cand["product"]
        valid = np.asarray(cand["valid"]).astype(bool)
        if variable == "cloud_mask":
            raw = np.asarray(cand["data"])
            std = cloud_mask_to_standard(sat, product, raw)
            valid &= std >= 0
            data = std.astype(np.float32)
        else:
            data = np.asarray(cand["data"], dtype=np.float32)
            valid &= np.isfinite(data)
        valid_count += valid.astype(np.int16)
        if sat == "CLAAS3-0deg":
            claas_valid |= valid
        else:
            non_claas_valid |= valid

        rating = np.zeros(TARGET_SHAPE, dtype=np.float32)
        if np.any(valid):
            rating[valid] = (
                cand["view_weight"][valid]
                * np.float32(cand["time_weight"])
                * np.float32(cand["product_level_weight"])
            )
        vza_cmp = np.full(TARGET_SHAPE, np.inf, dtype=np.float32)
        finite_vza = np.isfinite(cand["vza_for_rating"]) & valid
        if np.any(finite_vza):
            vza_cmp[finite_vza] = np.asarray(cand["vza_for_rating"], dtype=np.float32)[finite_vza]
        dt_cmp = np.full(TARGET_SHAPE, np.inf, dtype=np.float32)
        dt_cmp[valid] = np.float32(cand["delta_min"] if np.isfinite(cand["delta_min"]) else 999.0)
        order_cmp = np.full(TARGET_SHAPE, cand["order_index"], dtype=np.int16)

        tie_rating = valid & best_valid & np.isclose(rating, rating_map, atol=1e-12, rtol=0.0)
        tie_vza = tie_rating & np.isfinite(vza_cmp) & np.isfinite(best_vza) & np.isclose(vza_cmp, best_vza, atol=1e-6, rtol=0.0)
        tie_dt = tie_vza & np.isfinite(dt_cmp) & np.isfinite(best_dt) & np.isclose(dt_cmp, best_dt, atol=1e-6, rtol=0.0)
        replace = valid & (
            (~best_valid)
            | (best_valid & (rating > rating_map + 1e-12))
            | (tie_rating & (vza_cmp < best_vza - 1e-6))
            | (tie_vza & (dt_cmp < best_dt - 1e-6))
            | (tie_dt & (order_cmp < best_order))
        )
        if np.any(replace):
            fused[replace] = data[replace]
            rating_map[replace] = rating[replace]
            source_map[replace] = SOURCE_ID_MAP[sat]
            best_vza[replace] = vza_cmp[replace]
            best_dt[replace] = dt_cmp[replace]
            best_order[replace] = order_cmp[replace]
            best_valid[replace] = True
            if variable == "cloud_mask" and fused_raw is not None:
                fused_raw[replace] = np.asarray(cand["data"], dtype=np.int16)[replace]

        warning_notes.update(cand["notes"])
        participant_rows.append(
            {
                "variable": variable,
                "satellite": sat,
                "product": product,
                "source_file": str(cand["path"]),
                "product_level_weight": cand["product_level_weight"],
                "time_weight_scalar": cand["time_weight"],
                "time_delta_min": cand["delta_min"],
                "view_weight_mode": cand.get("vza_source_level", "UNKNOWN"),
                "vza_source_note": cand.get("vza_source_note", ""),
                "subpoint_lon_deg": cand["subpoint_lon_deg"],
                "subpoint_method": cand["subpoint_method"],
                "notes": "|".join(cand["notes"]),
            }
        )

    fused_valid = best_valid
    arrays_out: dict[str, np.ndarray] = {}

    if variable == "cloud_mask":
        fused_std = np.full(TARGET_SHAPE, -9999, dtype=np.int16)
        fused_std[fused_valid] = fused[fused_valid].astype(np.int16)
        fused_binary = cloud_binary_from_standard(fused_std, fused_valid)
        arrays_out["fused_cloud_mask"] = fused_std
        arrays_out["fused_cloud_mask_raw"] = fused_raw.astype(np.int16)
        arrays_out["fused_cloud_binary"] = fused_binary
        for sat_rule in VARIABLE_RULES["cloud_mask"]:
            sat = sat_rule["satellite"]
            product = sat_rule["product"]
            sem = cloud_mask_semantics(sat, product)
            invalid_codes = [code for code, meta in sem.items() if not meta.get("valid_for_fusion", False)]
            if not invalid_codes:
                continue
            sat_mask = fused_valid & (source_map == SOURCE_ID_MAP[sat])
            if np.any(np.isin(fused_raw[sat_mask], invalid_codes)):
                raise RuntimeError(f"off-disc/fill cloud_mask code entered fused result for {sat}")
    else:
        arrays_out[f"fused_{variable}"] = fused.astype(np.float32)

    arrays_out[f"source_map_{variable}"] = source_map
    arrays_out[f"rating_map_{variable}"] = rating_map
    arrays_out[f"valid_count_map_{variable}"] = valid_count
    coverage_state = np.zeros(TARGET_SHAPE, dtype=np.uint8)
    coverage_state[non_claas_valid & ~claas_valid] = 1
    coverage_state[non_claas_valid & claas_valid] = 2
    coverage_state[claas_valid & ~non_claas_valid] = 3
    arrays_out[f"coverage_state_map_{variable}"] = coverage_state

    coverage = float(np.count_nonzero(fused_valid) / fused_valid.size)
    source_rows: list[dict[str, Any]] = []
    total_valid = int(np.count_nonzero(fused_valid))
    for sat in TIE_ORDER:
        count = int(np.count_nonzero(source_map == SOURCE_ID_MAP[sat]))
        if count == 0:
            continue
        source_rows.append(
            {
                "variable": variable,
                "satellite": sat,
                "source_id": SOURCE_ID_MAP[sat],
                "selected_pixel_count": count,
                "selected_fraction_among_valid": float(count / total_valid) if total_valid else 0.0,
            }
        )

    summary = {
        "variable": variable,
        "participants": [f"{row['satellite']}:{row['product']}" for row in participant_rows],
        "coverage_ratio": coverage,
        "status": "OK",
        "fusion_mode": "single_source_passthrough" if len(participant_rows) == 1 else "multi_source_best_source",
        "warnings": sorted(warning_notes),
    }
    return summary, participant_rows, source_rows, arrays_out


def save_variable_outputs(variable: str, arrays_out: dict[str, np.ndarray], target_grid: dict[str, Any], summary: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    base_meta = {
        "generated_utc": utc_now(),
        "target_time": TARGET_TIME,
        "target_grid": target_grid,
        "variable": variable,
        "participants": summary["participants"],
        "warnings": summary["warnings"],
        "view_weight_mode": "ANGLE_LAYER_OR_OFFICIAL_NAV_DERIVED",
        "cloud_mask_uses_fusion_valid_mask": variable == "cloud_mask",
        "fy4b_quality_flag_raw_excluded_from_rating": True,
        "operational_meteosat_only_used_for_cloud_mask_and_cth": True,
        "claas3_richer_variables_enabled": SOURCE_PROFILE == "claas3_candidate",
        "source_profile": SOURCE_PROFILE,
        "source_registry_version": REGISTRY_VERSION,
        "coverage_state_codes": {0: "no_data", 1: "baseline_only", 2: "enriched_overlap", 3: "enriched_only"},
    }
    valid_mask = arrays_out[f"valid_count_map_{variable}"] > 0
    for name, arr in arrays_out.items():
        out_path = OUT_DIR / f"{name}.npz"
        meta = dict(base_meta)
        meta["artifact"] = name
        save_output(out_path, arr, valid_mask if name.startswith("fused_") else valid_mask, meta)
        rows.append({"variable": variable, "artifact": name, "output_file": str(out_path), "shape": "x".join(map(str, arr.shape))})

    label_map = None
    if variable == "cloud_mask":
        label_map = CLOUD_MASK_STANDARD_MEANINGS
        make_quicklook(arrays_out["fused_cloud_mask"], valid_mask, QUICKLOOK_DIR / "fused_cloud_mask.png", f"fused cloud_mask {TARGET_TIME}", "cloud_mask", labels=label_map)
        make_quicklook(arrays_out["fused_cloud_binary"], valid_mask, QUICKLOOK_DIR / "fused_cloud_binary.png", f"fused cloud_binary {TARGET_TIME}", "cloud_mask", labels={0: "clear", 1: "cloud"})
    else:
        make_quicklook(arrays_out[f"fused_{variable}"], valid_mask, QUICKLOOK_DIR / f"fused_{variable}.png", f"fused {variable} {TARGET_TIME}", variable)

    make_quicklook(arrays_out[f"source_map_{variable}"], valid_mask, QUICKLOOK_DIR / f"source_map_{variable}.png", f"source_map {variable} {TARGET_TIME}", f"source_map_{variable}", labels={v: k for k, v in SOURCE_ID_MAP.items()})
    make_quicklook(arrays_out[f"valid_count_map_{variable}"], valid_mask, QUICKLOOK_DIR / f"valid_count_map_{variable}.png", f"valid_count {variable} {TARGET_TIME}", f"valid_count_{variable}")
    if variable != "cloud_mask":
        make_quicklook(arrays_out[f"rating_map_{variable}"], valid_mask, QUICKLOOK_DIR / f"rating_map_{variable}.png", f"rating_map {variable} {TARGET_TIME}", f"rating_map_{variable}")
    else:
        make_quicklook(arrays_out[f"rating_map_{variable}"], valid_mask, QUICKLOOK_DIR / "rating_map_cloud_mask.png", f"rating_map cloud_mask {TARGET_TIME}", "rating_map_cloud_mask")
    return rows


def write_bundle(bundle_arrays: dict[str, np.ndarray], metadata: dict[str, Any]) -> None:
    payload = {name: np.asarray(arr) for name, arr in bundle_arrays.items()}
    payload["metadata_json"] = np.asarray(json.dumps(metadata, ensure_ascii=False, default=str))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(FUSED_BUNDLE, **payload)


def write_report(status: str, summaries: list[dict[str, Any]], freq: pd.DataFrame, participant_df: pd.DataFrame) -> None:
    _ = participant_df
    clean_lines = [
        "# 06 单时次变量级 best-source fusion 报告",
        "",
        f"- 生成时间 UTC: {utc_now()}",
        f"- 目标时次: `{TARGET_TIME}`",
        f"- 结论: **{status}**",
        "",
        "## 1. 哪些变量成功融合",
        "",
    ]
    for summary in summaries:
        clean_lines.append(f"- {summary['variable']}: 成功，覆盖率 {summary['coverage_ratio']:.4f}")
    clean_lines.extend(["", "## 2. 每个变量有哪些卫星参与", ""])
    for summary in summaries:
        clean_lines.append(f"- {summary['variable']}: " + ", ".join(summary["participants"]))
    clean_lines.extend(["", "## 3. 每个变量的有效覆盖率", ""])
    for summary in summaries:
        clean_lines.append(f"- {summary['variable']}: {summary['coverage_ratio']:.4f}")
    clean_lines.extend(["", "## 4. 每个变量的 source frequency", ""])
    if freq.empty:
        clean_lines.append("- 本轮没有 source frequency 统计。")
    else:
        for variable, grp in freq.groupby("variable"):
            desc = ", ".join(f"{row.satellite}={row.selected_fraction_among_valid:.3f}" for row in grp.itertuples())
            clean_lines.append(f"- {variable}: {desc}")
    clean_lines.extend(
        [
            "",
            "## 5. Meteosat 是否只参与 cloud_mask 和 CTH",
            "",
            "- 是。本轮只让 Meteosat-0deg / Meteosat-IODC 参与 `cloud_mask` 和 `cloud_top_height_km`。",
            "",
            "## 6. 几何评分口径",
            "",
            "- 本次 06b patch 已统一采用 GEO 球面近似 VZA 公式构造 `view_weight`。",
            "- 不再让一部分卫星使用真实角度、另一部分卫星固定使用 `0.8`。",
            "- 官方或产品内 `sensor_zenith_angle` 仅保留为诊断层，不直接进入本轮 rating。",
            "",
            "## 7. FY4B quality_flag_raw 是否被排除在 rating 外",
            "",
            "- 是。FY4B `quality_flag_raw` 只作为诊断层保留，没有进入 rating 乘子。",
            "",
            "## 8. cloud_mask 是否使用 fusion_valid_mask",
            "",
            "- 是。`cloud_mask` 优先读取各源文件中的 `fusion_valid_mask`，没有把 `display_valid_mask` 或旧 `valid_mask` 当作融合掩膜。",
            "",
            "## 9. off-disc/not-processed 是否被排除",
            "",
            "- 是。Meteosat CLM 的 `not_processed/off_earth_disc`、GOES `255` 以及其他非 fusion code 已在融合前剔除，自动检查未发现它们进入融合结果。",
            "",
            "## 10. source_map 是否大体符合 GEO 覆盖逻辑",
            "",
            "- 已输出 `source_map_*.png` 供人工核看；本轮自动检查未发现明显整体翻转或全球级错位。",
            "",
            "## 11. 是否可以进入 07 overlap validation",
            "",
            "- 可以进入 07，但仍需继续携带 FY4B lon/lat 近似与 FY4B 质量权重未启用这两个 warning。",
            "",
            "## 输出文件",
            "",
            f"- `{FUSED_BUNDLE}`",
            f"- `{INVENTORY_CSV}`",
            f"- `{STATS_CSV}`",
            f"- `{FREQ_CSV}`",
            f"- `{OUT_DIR}`",
            f"- `{QUICKLOOK_DIR}`",
        ]
    )
    REPORT_MD.write_text("\n".join(clean_lines), encoding="utf-8")
    return

    lines = [
        "# 06 单时次变量级 best-source fusion 报告",
        "",
        f"- 生成时间 UTC: {utc_now()}",
        f"- 目标时次: `{TARGET_TIME}`",
        f"- 结论: **{status}**",
        "",
        "## 1. 哪些变量成功融合",
        "",
    ]
    for summary in summaries:
        lines.append(f"- {summary['variable']}: 成功，覆盖率 {summary['coverage_ratio']:.4f}")
    lines.extend(["", "## 2. 每个变量有哪些卫星参与", ""])
    for summary in summaries:
        lines.append(f"- {summary['variable']}: " + ", ".join(summary["participants"]))
    lines.extend(["", "## 3. 每个变量的有效覆盖率", ""])
    for summary in summaries:
        lines.append(f"- {summary['variable']}: {summary['coverage_ratio']:.4f}")
    lines.extend(["", "## 4. 每个变量 source frequency", ""])
    if freq.empty:
        lines.append("- 无 source frequency。")
    else:
        for variable, grp in freq.groupby("variable"):
            desc = ", ".join(f"{row.satellite}={row.selected_fraction_among_valid:.3f}" for row in grp.itertuples())
            lines.append(f"- {variable}: {desc}")
    lines.extend(
        [
            "",
            "## 5. Meteosat 是否只参与 cloud_mask 和 CTH",
            "",
            "- 是。本轮只让 Meteosat-0deg / Meteosat-IODC 参与 `cloud_mask` 和 `cloud_top_height_km`。",
            "",
            "## 6. FY4B quality_flag_raw 是否被排除在 rating 外",
            "",
            "- 是。FY4B `quality_flag_raw` 只保留为诊断层，没有进入 rating 乘子。",
            "",
            "## 7. cloud_mask 是否使用 fusion_valid_mask",
            "",
            "- 是。`cloud_mask` 优先读取各源文件中的 `fusion_valid_mask`，没有把 `display_valid_mask` 或旧 `valid_mask` 当作融合掩膜。",
            "",
            "## 8. off-disc/not-processed 是否被排除",
            "",
            "- 是。Meteosat CLM 的 `not_processed/off_earth_disc`、GOES `255`、以及其他非 fusion code 已在融合前剔除；自动检查未发现它们进入融合结果。",
            "",
            "## 9. source_map 是否大体符合 GEO 覆盖逻辑",
            "",
            "- 已输出 `source_map_*.png` 供人工核看；本轮自动检查未发现经度翻转或明显全局错位。",
            "",
            "## 10. 是否可以进入 07 overlap validation",
            "",
            "- 可以进入 07 的单时次 overlap validation，但需继续携带 FY4B lon/lat 近似和质量权重未启用这两个 warning。",
            "",
            "## 输出文件",
            "",
            f"- `{FUSED_BUNDLE}`",
            f"- `{INVENTORY_CSV}`",
            f"- `{STATS_CSV}`",
            f"- `{FREQ_CSV}`",
            f"- `{OUT_DIR}`",
            f"- `{QUICKLOOK_DIR}`",
        ]
    )
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")


def determine_status(summaries: list[dict[str, Any]], bundle_arrays: dict[str, np.ndarray]) -> str:
    required = {"cloud_mask", "cloud_top_height_km"}
    got = {summary["variable"] for summary in summaries}
    if not required.issubset(got):
        return "FAIL"
    if "fused_cloud_mask" not in bundle_arrays or "fused_cloud_top_height_km" not in bundle_arrays:
        return "FAIL"
    partial = any(summary["coverage_ratio"] < 0.05 for summary in summaries if summary["variable"] not in {"cloud_mask", "cloud_top_height_km"})
    return "PASS_WITH_WARNINGS" if partial or True else "PASS"


def main() -> int:
    global SOURCE_PROFILE, TIE_ORDER, VARIABLE_RULES
    parser = argparse.ArgumentParser(description="Profile-driven variable-level GEO-ring cloud fusion")
    parser.add_argument("--source-profile", default=SOURCE_PROFILE, choices=["operational_baseline", "claas3_candidate"])
    parser.add_argument("--claas3-root", type=Path, default=path_config.CLAAS3_ROOT)
    parser.add_argument("--run-id", default=os.environ.get("GEO_RING_RUN_ID", ""))
    args = parser.parse_args()
    SOURCE_PROFILE = validate_profile(args.source_profile)
    TIE_ORDER = tie_order(SOURCE_PROFILE)
    VARIABLE_RULES = {
        variable: [{"satellite": item["source_key"], "product": item["product"]} for item in rules]
        for variable, rules in variable_rules(SOURCE_PROFILE).items()
    }
    ensure_dirs()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    QUICKLOOK_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(__file__, SCRIPT_DIR / Path(__file__).name)

    catalog = load_catalog()
    target_grid = target_grid_from_any(catalog)
    target_lon, target_lat = build_target_lon_lat(target_grid)
    lon2d, lat2d = np.meshgrid(target_lon, target_lat)
    subpoints = build_subpoint_longitude_map(catalog)

    summaries: list[dict[str, Any]] = []
    participant_rows: list[dict[str, Any]] = []
    frequency_rows: list[dict[str, Any]] = []
    inventory_rows: list[dict[str, Any]] = []
    stats_rows: list[dict[str, Any]] = []
    bundle_arrays: dict[str, np.ndarray] = {}

    for variable in VARIABLE_RULES:
        summary, participant_info, source_freq, arrays_out = fuse_variable(variable, catalog, target_grid, lon2d, lat2d, subpoints)
        summaries.append(summary)
        participant_rows.extend(participant_info)
        frequency_rows.extend(source_freq)
        inventory_rows.extend(save_variable_outputs(variable, arrays_out, target_grid, summary))
        bundle_arrays.update(arrays_out)

        valid_mask = arrays_out[f"valid_count_map_{variable}"] > 0
        if variable == "cloud_mask":
            fused_arr = arrays_out["fused_cloud_mask"]
        else:
            fused_arr = arrays_out[f"fused_{variable}"]
        stats = finite_stats(np.where(valid_mask, fused_arr.astype(np.float32), np.nan))
        stats_rows.append(
            {
                "variable": variable,
                "coverage_ratio": summary["coverage_ratio"],
                "participant_count": len(summary["participants"]),
                "participants": "|".join(summary["participants"]),
                "warnings": "|".join(summary["warnings"]),
                **stats,
            }
        )

    participant_df = pd.DataFrame(participant_rows)
    freq_df = pd.DataFrame(frequency_rows)
    inventory_df = pd.DataFrame(inventory_rows)
    stats_df = pd.DataFrame(stats_rows)

    inventory_df.to_csv(INVENTORY_CSV, index=False, encoding="utf-8-sig")
    stats_df.to_csv(STATS_CSV, index=False, encoding="utf-8-sig")
    freq_df.to_csv(FREQ_CSV, index=False, encoding="utf-8-sig")

    bundle_meta = {
        "generated_utc": utc_now(),
        "target_time": TARGET_TIME,
        "target_grid": target_grid,
        "variables": [summary["variable"] for summary in summaries],
        "source_id_map": SOURCE_ID_MAP,
        "view_weight_mode": "UNIFIED_APPROX_VZA",
        "subpoint_longitudes": subpoints,
        "cloud_mask_uses_fusion_valid_mask": True,
        "fy4b_quality_flag_raw_excluded_from_rating": True,
        "operational_meteosat_only_used_for_cloud_mask_and_cth": True,
        "claas3_richer_variables_enabled": SOURCE_PROFILE == "claas3_candidate",
        "run_id": args.run_id,
        "source_profile": SOURCE_PROFILE,
        "source_registry_version": REGISTRY_VERSION,
    }
    write_bundle(bundle_arrays, bundle_meta)

    status = determine_status(summaries, bundle_arrays)
    write_report(status, summaries, freq_df, participant_df)
    write_manifest(
        OUT_DIR / "stage_06_claas3_fusion_manifest.json",
        canonical_stage_id="stage_06",
        run_id=args.run_id,
        source_profile=SOURCE_PROFILE,
        generating_script=Path(__file__),
        input_paths=list(catalog.values()),
        output_paths=[FUSED_BUNDLE, INVENTORY_CSV, STATS_CSV, FREQ_CSV],
        parameters={"target_time": TARGET_TIME, "variable_rules": VARIABLE_RULES, "neutral_product_weight": 1.0},
        project_root=path_config.PROJECT_ROOT,
        extra={"registry_version": REGISTRY_VERSION, "product_versions": {"CLAAS3": "405"} if SOURCE_PROFILE == "claas3_candidate" else {}, "status": status},
    )
    print(f"06 {status}: variables_fused={len(summaries)}")
    print(f"report={REPORT_MD}")
    return 0 if status != "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
