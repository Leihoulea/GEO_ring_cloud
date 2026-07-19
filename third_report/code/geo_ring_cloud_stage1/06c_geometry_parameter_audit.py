from __future__ import annotations

import math
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from geo_ring_cloud.geometry import (
    GeometryParams,
    build_target_lon_lat,
    ecef_vza_chunk,
    extract_target_grid,
    gather_geometry_params,
    load_npz_meta_arrays,
    normalize_lon,
    representative_native_path,
    representative_reprojected_path,
    spherical_vza_chunk,
)
from geo_ring_cloud.lineage import utc_now
from geo_ring_cloud.pipeline_layout import (
    REPORT_DIR,
    SCRIPT_DIR,
    STAGE_ROOT,
    ensure_pipeline_directories as ensure_dirs,
)


OUT_DIR = STAGE_ROOT / "geometry_audit_06c"

TARGET_TIME = "2024-03-05T00:00:00Z"

GEOMETRY_AUDIT_CSV = OUT_DIR / "satellite_geometry_parameter_audit.csv"
METHOD_COMPARISON_CSV = OUT_DIR / "vza_method_comparison.csv"
OFFICIAL_COMPARE_CSV = OUT_DIR / "official_vs_computed_vza_stats.csv"
REPORT_MD = REPORT_DIR / "06c_geometry_audit_report.md"

TARGET_SHAPE = (3600, 7200)
ROW_CHUNK = 120


def load_fusion_valid_mask(satellite: str) -> np.ndarray:
    product_map = {
        "FY4B": "CLM",
        "GOES-16": "ACMF",
        "GOES-18": "ACMF",
        "Himawari-9": "CMSK",
        "Meteosat-0deg": "CLM",
        "Meteosat-IODC": "CLM",
    }
    product = product_map[satellite]
    path = representative_reprojected_path(satellite, product, "cloud_mask")
    _, arrays = load_npz_meta_arrays(path)
    if "fusion_valid_mask" in arrays:
        return np.asarray(arrays["fusion_valid_mask"]).astype(bool)
    if "valid_mask" in arrays:
        return np.asarray(arrays["valid_mask"]).astype(bool)
    raise RuntimeError(f"valid mask missing in {path}")


def load_fy4b_official_sensor_zenith() -> tuple[np.ndarray, np.ndarray]:
    path = representative_reprojected_path("FY4B", "GEO", "sensor_zenith_angle")
    _, arrays = load_npz_meta_arrays(path)
    data = np.asarray(arrays["data"], dtype=np.float32)
    valid = np.asarray(arrays.get("valid_mask", np.isfinite(data))).astype(bool) & np.isfinite(data)
    return data, valid


def summarize_diff(diff: np.ndarray) -> dict[str, float]:
    finite = np.isfinite(diff)
    if not finite.any():
        return {
            "pixel_count": 0,
            "mean_signed_diff_deg": math.nan,
            "mean_abs_diff_deg": math.nan,
            "rmse_deg": math.nan,
            "p95_abs_diff_deg": math.nan,
            "max_abs_diff_deg": math.nan,
        }
    arr = diff[finite].astype(np.float64)
    abs_arr = np.abs(arr)
    return {
        "pixel_count": int(arr.size),
        "mean_signed_diff_deg": float(arr.mean()),
        "mean_abs_diff_deg": float(abs_arr.mean()),
        "rmse_deg": float(np.sqrt(np.mean(arr * arr))),
        "p95_abs_diff_deg": float(np.percentile(abs_arr, 95)),
        "max_abs_diff_deg": float(abs_arr.max()),
    }


def compute_vza_chunks(
    params: GeometryParams,
    lon_1d: np.ndarray,
    lat_1d: np.ndarray,
    valid_mask: np.ndarray,
) -> tuple[list[dict[str, Any]], dict[str, np.ndarray]]:
    diffs_current_vs_recommended: list[np.ndarray] = []
    diffs_recommended_vs_ecef: list[np.ndarray] = []
    outputs: dict[str, np.ndarray] = {}
    fy4b_spherical_full = np.full(TARGET_SHAPE, np.nan, dtype=np.float32) if params.satellite == "FY4B" else None
    fy4b_ecef_full = np.full(TARGET_SHAPE, np.nan, dtype=np.float32) if params.satellite == "FY4B" else None

    for y0 in range(0, lat_1d.size, ROW_CHUNK):
        y1 = min(lat_1d.size, y0 + ROW_CHUNK)
        rows = slice(y0, y1)
        mask_chunk = valid_mask[rows, :]
        if not np.any(mask_chunk):
            continue
        current_chunk = spherical_vza_chunk(
            lat_1d[rows],
            lon_1d,
            params.current_subpoint_lon_deg,
            params.current_earth_radius_m,
            params.current_earth_radius_m + params.current_height_above_ellipsoid_m,
        )
        recommended_spherical_chunk = spherical_vza_chunk(
            lat_1d[rows],
            lon_1d,
            params.recommended_subpoint_lon_deg,
            params.recommended_a_m,
            params.recommended_center_distance_m,
        )
        recommended_ecef_chunk = ecef_vza_chunk(
            lat_1d[rows],
            lon_1d,
            params.recommended_subpoint_lon_deg,
            params.recommended_a_m,
            params.recommended_b_m,
            params.recommended_center_distance_m,
        )
        good_current = mask_chunk & np.isfinite(current_chunk) & np.isfinite(recommended_spherical_chunk)
        if np.any(good_current):
            diffs_current_vs_recommended.append((recommended_spherical_chunk - current_chunk)[good_current].astype(np.float32))
        good_ecef = mask_chunk & np.isfinite(recommended_spherical_chunk) & np.isfinite(recommended_ecef_chunk)
        if np.any(good_ecef):
            diffs_recommended_vs_ecef.append((recommended_ecef_chunk - recommended_spherical_chunk)[good_ecef].astype(np.float32))
        if params.satellite == "FY4B":
            fy4b_spherical_full[rows, :] = recommended_spherical_chunk.astype(np.float32)
            fy4b_ecef_full[rows, :] = recommended_ecef_chunk.astype(np.float32)

    rows_out: list[dict[str, Any]] = []
    diff_current = np.concatenate(diffs_current_vs_recommended) if diffs_current_vs_recommended else np.asarray([], dtype=np.float32)
    diff_ecef = np.concatenate(diffs_recommended_vs_ecef) if diffs_recommended_vs_ecef else np.asarray([], dtype=np.float32)
    stats_current = summarize_diff(diff_current)
    stats_ecef = summarize_diff(diff_ecef)
    rows_out.append(
        {
            "satellite": params.satellite,
            "comparison": "recommended_spherical_minus_current06b_spherical",
            "domain": "cloud_mask_fusion_valid_mask",
            **stats_current,
        }
    )
    rows_out.append(
        {
            "satellite": params.satellite,
            "comparison": "recommended_ecef_minus_recommended_spherical",
            "domain": "cloud_mask_fusion_valid_mask",
            **stats_ecef,
        }
    )
    if params.satellite == "FY4B":
        outputs["recommended_spherical_full"] = fy4b_spherical_full
        outputs["recommended_ecef_full"] = fy4b_ecef_full
    return rows_out, outputs


def compare_official_fy4b(
    recommended_spherical: np.ndarray,
    recommended_ecef: np.ndarray,
) -> pd.DataFrame:
    official, official_valid = load_fy4b_official_sensor_zenith()
    rows: list[dict[str, Any]] = []
    for method_name, computed in [
        ("recommended_spherical", recommended_spherical),
        ("recommended_ecef", recommended_ecef),
    ]:
        mask = official_valid & np.isfinite(computed)
        diff = official.astype(np.float32) - computed.astype(np.float32)
        stats = summarize_diff(diff[mask].astype(np.float32) if np.any(mask) else np.asarray([], dtype=np.float32))
        rows.append(
            {
                "satellite": "FY4B",
                "comparison": f"official_minus_{method_name}",
                "domain": "reprojected_official_sensor_zenith_valid_mask",
                **stats,
            }
        )
    return pd.DataFrame(rows)


def rows_from_params(params_list: list[GeometryParams]) -> pd.DataFrame:
    rows = []
    for p in params_list:
        rows.append(
            {
                "satellite": p.satellite,
                "reference_product": p.reference_product,
                "source_file": p.source_file,
                "current_subpoint_lon_deg": p.current_subpoint_lon_deg,
                "current_subpoint_source": p.current_subpoint_source,
                "current_earth_radius_m": p.current_earth_radius_m,
                "current_earth_radius_source": p.current_earth_radius_source,
                "current_height_above_ellipsoid_m": p.current_height_above_ellipsoid_m,
                "current_height_source": p.current_height_source,
                "recommended_subpoint_lon_deg": p.recommended_subpoint_lon_deg,
                "recommended_subpoint_source": p.recommended_subpoint_source,
                "recommended_a_m": p.recommended_a_m,
                "recommended_a_source": p.recommended_a_source,
                "recommended_b_m": p.recommended_b_m,
                "recommended_b_source": p.recommended_b_source,
                "recommended_center_distance_m": p.recommended_center_distance_m,
                "recommended_center_distance_source": p.recommended_center_distance_source,
                "recommended_height_above_ellipsoid_m": p.recommended_height_above_ellipsoid_m,
                "recommended_height_source": p.recommended_height_source,
                "fallback_used": p.fallback_used,
                "notes": p.notes,
            }
        )
    return pd.DataFrame(rows)


def build_report(geometry_df: pd.DataFrame, method_df: pd.DataFrame, official_df: pd.DataFrame) -> str:
    ecef_rows = method_df[method_df["comparison"] == "recommended_ecef_minus_recommended_spherical"].copy()
    fy_rows = official_df.copy()
    all_small = False
    if not ecef_rows.empty:
        max_mae = float(ecef_rows["mean_abs_diff_deg"].max())
        max_p95 = float(ecef_rows["p95_abs_diff_deg"].max())
        max_max = float(ecef_rows["max_abs_diff_deg"].max())
        fy_mae = float(fy_rows["mean_abs_diff_deg"].max()) if not fy_rows.empty else math.nan
        fy_p95 = float(fy_rows["p95_abs_diff_deg"].max()) if not fy_rows.empty else math.nan
        all_small = (max_mae <= 0.05) and (max_p95 <= 0.15) and (max_max <= 0.35) and (not np.isfinite(fy_mae) or fy_mae <= 1.0) and (not np.isfinite(fy_p95) or fy_p95 <= 2.0)
    recommendation = "可以继续进入 07" if all_small else "暂不建议直接进入 07"

    lines = [
        "# 06c Geometry Audit Report",
        "",
        f"- 生成时间 UTC: {utc_now()}",
        f"- 目标时次: `{TARGET_TIME}`",
        f"- 结论: **{recommendation}**",
        "",
        "## 1. 每颗卫星几何参数来源",
        "",
    ]
    for row in geometry_df.itertuples():
        lines.append(
            f"- {row.satellite}: subpoint={row.recommended_subpoint_lon_deg:.3f} ({row.recommended_subpoint_source}); "
            f"height_above_ellipsoid={row.recommended_height_above_ellipsoid_m:.1f} m ({row.recommended_height_source}); "
            f"a={row.recommended_a_m:.1f} ({row.recommended_a_source}); "
            f"fallback_used={row.fallback_used}"
        )
    lines.extend(["", "## 2. 当前 06b 参数源改进幅度", ""])
    current_rows = method_df[method_df["comparison"] == "recommended_spherical_minus_current06b_spherical"]
    for row in current_rows.itertuples():
        lines.append(
            f"- {row.satellite}: mean_abs_diff={row.mean_abs_diff_deg:.4f} deg, "
            f"p95_abs_diff={row.p95_abs_diff_deg:.4f} deg, max_abs_diff={row.max_abs_diff_deg:.4f} deg"
        )
    lines.extend(["", "## 3. WGS84-ECEF 与 spherical 方法差异", ""])
    ecef_rows = method_df[method_df["comparison"] == "recommended_ecef_minus_recommended_spherical"]
    for row in ecef_rows.itertuples():
        lines.append(
            f"- {row.satellite}: mean_abs_diff={row.mean_abs_diff_deg:.4f} deg, "
            f"p95_abs_diff={row.p95_abs_diff_deg:.4f} deg, max_abs_diff={row.max_abs_diff_deg:.4f} deg"
        )
    lines.extend(["", "## 4. FY4B official sensor_zenith_angle 对照", ""])
    if official_df.empty:
        lines.append("- FY4B official 对照未生成。")
    else:
        for row in official_df.itertuples():
            lines.append(
                f"- {row.comparison}: mean_signed_diff={row.mean_signed_diff_deg:.4f} deg, "
                f"mean_abs_diff={row.mean_abs_diff_deg:.4f} deg, rmse={row.rmse_deg:.4f} deg, "
                f"p95_abs_diff={row.p95_abs_diff_deg:.4f} deg, max_abs_diff={row.max_abs_diff_deg:.4f} deg"
            )
    lines.extend(
        [
            "",
            "## 5. 审计判断",
            "",
            "- 只有当 spherical 与 ECEF/official 差异都足够小，才建议沿用当前 06b 结果进入 07。",
            "- 本次阈值采用：所有卫星 `recommended_ecef_minus_recommended_spherical` 的 `mean_abs_diff <= 0.05 deg`、`p95_abs_diff <= 0.15 deg`、`max_abs_diff <= 0.35 deg`，且 FY4B official 对照 `mean_abs_diff <= 1.0 deg`、`p95_abs_diff <= 2.0 deg`。",
            f"- 审计建议: **{recommendation}**",
            "",
            "## 6. 备注",
            "",
            "- FY4B 已优先改为从 L2/HDF 元数据读取 subpoint/height；文件名解析只作为 fallback。",
            "- GOES 已使用 goes_imager_projection 的 longitude_of_projection_origin、perspective_point_height、semi_major_axis、semi_minor_axis。",
            "- Himawari-9 未在现有标准化元数据或源 NetCDF 中发现明确星下点经度，因此本轮仍使用 140.7E fallback，并已明确标注。",
            "- Meteosat-0deg / Meteosat-IODC 未在现有标准化元数据或 ZIP XML 中发现明确星下点经度，因此本轮分别使用 0E / 45.5E 服务经度 fallback，并已明确标注。",
            "",
            "## 输出文件",
            "",
            f"- `{GEOMETRY_AUDIT_CSV}`",
            f"- `{METHOD_COMPARISON_CSV}`",
            f"- `{OFFICIAL_COMPARE_CSV}`",
            f"- `{REPORT_MD}`",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    ensure_dirs()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(__file__, SCRIPT_DIR / Path(__file__).name)

    params_list = gather_geometry_params()
    geometry_df = rows_from_params(params_list)
    geometry_df.to_csv(GEOMETRY_AUDIT_CSV, index=False, encoding="utf-8-sig")

    target_grid = extract_target_grid()
    lon_1d, lat_1d = build_target_lon_lat(target_grid)

    method_rows: list[dict[str, Any]] = []
    fy4b_outputs: dict[str, np.ndarray] = {}
    for params in params_list:
        valid_mask = load_fusion_valid_mask(params.satellite)
        rows, outputs = compute_vza_chunks(params, lon_1d, lat_1d, valid_mask)
        method_rows.extend(rows)
        if params.satellite == "FY4B":
            fy4b_outputs = outputs
    method_df = pd.DataFrame(method_rows)
    method_df.to_csv(METHOD_COMPARISON_CSV, index=False, encoding="utf-8-sig")

    official_df = compare_official_fy4b(
        fy4b_outputs["recommended_spherical_full"],
        fy4b_outputs["recommended_ecef_full"],
    )
    official_df.to_csv(OFFICIAL_COMPARE_CSV, index=False, encoding="utf-8-sig")

    REPORT_MD.write_text(build_report(geometry_df, method_df, official_df), encoding="utf-8")
    print(f"06c DONE: satellites={len(params_list)}")
    print(f"report={REPORT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
