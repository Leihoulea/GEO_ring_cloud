from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from geo_ring_cloud import geometry as F06C
from geo_ring_cloud.paths import GEOMETRY_ROOT, STAGE_ROOT, THIRD_REPORT_ROOT
from geo_ring_cloud.sources import tie_order, validate_profile


STAGE_ID = "stage_06e"
WORKSPACE_ROOT = THIRD_REPORT_ROOT

EXTERNAL_STAGE_ROOT = STAGE_ROOT
EXTERNAL_REPROJECT_DIR = EXTERNAL_STAGE_ROOT / "reprojected_grid"
EXTERNAL_GEOMETRY_AUDIT_DIR = GEOMETRY_ROOT

LOCAL_REPORT_ROOT = WORKSPACE_ROOT / "reports" / "geo_ring_cloud_stage1_06e_vza_ecef_final_audit"
LOCAL_REPORT_ROOT.mkdir(parents=True, exist_ok=True)

TIME_TAG = "20240305_0000"
TARGET_TIME = "2024-03-05T00:00:00Z"
TARGET_SHAPE = (3600, 7200)
ROW_CHUNK = 120
TIE_ORDER = tie_order(validate_profile(os.environ.get("GEO_RING_SOURCE_PROFILE", "operational_baseline")))

SOURCE_CSV = LOCAL_REPORT_ROOT / "06e_current_vza_source_inventory.csv"
COMPARISON_CSV = LOCAL_REPORT_ROOT / "06e_vza_vs_audited_ecef.csv"
IMPROVEMENT_CSV = LOCAL_REPORT_ROOT / "06e_vs_06b_ecef_improvement.csv"
REPORT_MD = LOCAL_REPORT_ROOT / "06e_vza_ecef_final_audit_report.md"
FY4B_DIAG_MD = LOCAL_REPORT_ROOT / "fy4b_06e_vza_gap_diagnosis.md"


def utc_now() -> str:
    return pd.Timestamp.utcnow().isoformat().replace("+00:00", "Z")


def load_npz(path: Path) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    with np.load(path, allow_pickle=False) as z:
        meta = json.loads(str(z["metadata_json"])) if "metadata_json" in z.files else {}
        arrays = {name: np.asarray(z[name]) for name in z.files if not name.endswith("_json")}
    return meta, arrays


def current_angle_layer_path(satellite: str) -> Path:
    return EXTERNAL_REPROJECT_DIR / satellite / f"{satellite}_ANGLE_sensor_zenith_angle_grid_{TIME_TAG}.npz"


def load_current_angle_layer(satellite: str) -> tuple[dict[str, Any], np.ndarray, np.ndarray]:
    path = current_angle_layer_path(satellite)
    if not path.exists():
        raise FileNotFoundError(f"missing current angle layer for {satellite}: {path}")
    meta, arrays = load_npz(path)
    if "data" not in arrays:
        raise RuntimeError(f"data missing in {path}")
    data = np.asarray(arrays["data"], dtype=np.float32)
    if data.shape != TARGET_SHAPE:
        raise RuntimeError(f"unexpected shape in {path}: {data.shape}")
    valid = np.asarray(arrays.get("valid_mask", np.isfinite(data))).astype(bool) & np.isfinite(data)
    return meta, data, valid


def build_source_inventory(params_list: list[Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    by_sat = {p.satellite: p for p in params_list}
    for satellite in TIE_ORDER:
        meta, data, valid = load_current_angle_layer(satellite)
        params = by_sat[satellite]
        rows.append(
            {
                "satellite": satellite,
                "target_time": TARGET_TIME,
                "angle_layer_file": str(current_angle_layer_path(satellite)),
                "angle_source_level": meta.get("angle_source_level", ""),
                "source_file": meta.get("source_file", ""),
                "resampling_method": meta.get("resampling_method", ""),
                "current_subpoint_lon_deg": params.current_subpoint_lon_deg,
                "recommended_subpoint_lon_deg": params.recommended_subpoint_lon_deg,
                "recommended_subpoint_source": params.recommended_subpoint_source,
                "recommended_a_m": params.recommended_a_m,
                "recommended_b_m": params.recommended_b_m,
                "recommended_center_distance_m": params.recommended_center_distance_m,
                "recommended_height_above_ellipsoid_m": params.recommended_height_above_ellipsoid_m,
                "valid_pixel_count": int(np.count_nonzero(valid)),
                "coverage_ratio": float(np.count_nonzero(valid) / valid.size),
                "notes": str(meta.get("notes", "")),
            }
        )
    df = pd.DataFrame(rows)
    df.to_csv(SOURCE_CSV, index=False, encoding="utf-8-sig")
    return df


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


def compare_current_layer_to_ecef(
    params: Any,
    lon_1d: np.ndarray,
    lat_1d: np.ndarray,
    current_data: np.ndarray,
    current_valid: np.ndarray,
) -> list[dict[str, Any]]:
    diffs_current_vs_ecef: list[np.ndarray] = []
    diffs_current_vs_spherical: list[np.ndarray] = []

    for y0 in range(0, lat_1d.size, ROW_CHUNK):
        y1 = min(lat_1d.size, y0 + ROW_CHUNK)
        rows = slice(y0, y1)
        mask_chunk = current_valid[rows, :]
        if not np.any(mask_chunk):
            continue
        current_chunk = current_data[rows, :]
        ecef_chunk = F06C.ecef_vza_chunk(
            lat_1d[rows],
            lon_1d,
            params.recommended_subpoint_lon_deg,
            params.recommended_a_m,
            params.recommended_b_m,
            params.recommended_center_distance_m,
        )
        spherical_chunk = F06C.spherical_vza_chunk(
            lat_1d[rows],
            lon_1d,
            params.recommended_subpoint_lon_deg,
            params.recommended_a_m,
            params.recommended_center_distance_m,
        )

        good_ecef = mask_chunk & np.isfinite(current_chunk) & np.isfinite(ecef_chunk)
        if np.any(good_ecef):
            diffs_current_vs_ecef.append((current_chunk - ecef_chunk)[good_ecef].astype(np.float32))

        good_spherical = mask_chunk & np.isfinite(current_chunk) & np.isfinite(spherical_chunk)
        if np.any(good_spherical):
            diffs_current_vs_spherical.append((current_chunk - spherical_chunk)[good_spherical].astype(np.float32))

    diff_ecef = np.concatenate(diffs_current_vs_ecef) if diffs_current_vs_ecef else np.asarray([], dtype=np.float32)
    diff_spherical = np.concatenate(diffs_current_vs_spherical) if diffs_current_vs_spherical else np.asarray([], dtype=np.float32)

    return [
        {
            "satellite": params.satellite,
            "comparison": "current06e_vza_minus_audited_ecef",
            "domain": "current_06e_angle_layer_valid_mask",
            "angle_source_level": "",
            **summarize_diff(diff_ecef),
        },
        {
            "satellite": params.satellite,
            "comparison": "current06e_vza_minus_audited_spherical",
            "domain": "current_06e_angle_layer_valid_mask",
            "angle_source_level": "",
            **summarize_diff(diff_spherical),
        },
    ]


def build_comparison_table(params_list: list[Any], source_df: pd.DataFrame) -> pd.DataFrame:
    target_grid = F06C.extract_target_grid()
    lon_1d, lat_1d = F06C.build_target_lon_lat(target_grid)

    angle_level_map = source_df.set_index("satellite")["angle_source_level"].to_dict()
    rows: list[dict[str, Any]] = []
    for params in params_list:
        _, current_data, current_valid = load_current_angle_layer(params.satellite)
        sat_rows = compare_current_layer_to_ecef(params, lon_1d, lat_1d, current_data, current_valid)
        for row in sat_rows:
            row["angle_source_level"] = angle_level_map.get(params.satellite, "")
        rows.extend(sat_rows)
    df = pd.DataFrame(rows)
    df.to_csv(COMPARISON_CSV, index=False, encoding="utf-8-sig")
    return df


def load_legacy_06b_reference() -> pd.DataFrame:
    path = EXTERNAL_GEOMETRY_AUDIT_DIR / "vza_method_comparison_by_satellite.csv"
    if not path.exists():
        raise FileNotFoundError(f"missing legacy geometry comparison file: {path}")
    df = pd.read_csv(path, encoding="utf-8-sig")
    keep = df[df["comparison"] == "current06b_spherical_minus_audited_ecef"].copy()
    return keep[
        [
            "satellite",
            "mean_abs_diff_deg",
            "rmse_deg",
            "p95_abs_diff_deg",
            "max_abs_diff_deg",
            "pixel_count",
        ]
    ].rename(
        columns={
            "mean_abs_diff_deg": "legacy06b_mean_abs_diff_deg",
            "rmse_deg": "legacy06b_rmse_deg",
            "p95_abs_diff_deg": "legacy06b_p95_abs_diff_deg",
            "max_abs_diff_deg": "legacy06b_max_abs_diff_deg",
            "pixel_count": "legacy06b_pixel_count",
        }
    )


def build_improvement_table(comparison_df: pd.DataFrame) -> pd.DataFrame:
    current = comparison_df[comparison_df["comparison"] == "current06e_vza_minus_audited_ecef"].copy()
    current = current[
        [
            "satellite",
            "angle_source_level",
            "pixel_count",
            "mean_abs_diff_deg",
            "rmse_deg",
            "p95_abs_diff_deg",
            "max_abs_diff_deg",
        ]
    ].rename(
        columns={
            "pixel_count": "current06e_pixel_count",
            "mean_abs_diff_deg": "current06e_mean_abs_diff_deg",
            "rmse_deg": "current06e_rmse_deg",
            "p95_abs_diff_deg": "current06e_p95_abs_diff_deg",
            "max_abs_diff_deg": "current06e_max_abs_diff_deg",
        }
    )
    legacy = load_legacy_06b_reference()
    merged = legacy.merge(current, on="satellite", how="outer")
    merged["mean_abs_diff_change_deg"] = merged["current06e_mean_abs_diff_deg"] - merged["legacy06b_mean_abs_diff_deg"]
    merged["p95_abs_diff_change_deg"] = merged["current06e_p95_abs_diff_deg"] - merged["legacy06b_p95_abs_diff_deg"]
    merged["rmse_change_deg"] = merged["current06e_rmse_deg"] - merged["legacy06b_rmse_deg"]
    merged["status"] = np.where(
        merged["mean_abs_diff_change_deg"] < -1e-6,
        "IMPROVED",
        np.where(merged["mean_abs_diff_change_deg"] > 1e-6, "DEGRADED", "UNCHANGED"),
    )
    merged.to_csv(IMPROVEMENT_CSV, index=False, encoding="utf-8-sig")
    return merged


def build_report(source_df: pd.DataFrame, comparison_df: pd.DataFrame, improvement_df: pd.DataFrame) -> str:
    current_rows = comparison_df[comparison_df["comparison"] == "current06e_vza_minus_audited_ecef"].copy()
    current_rows = current_rows.sort_values("satellite")

    lines = [
        "# 06e 当前 VZA 口径 vs ECEF 最终复核",
        "",
        f"- 生成时间 UTC: {utc_now()}",
        f"- 目标时次: `{TARGET_TIME}`",
        "- 本次复核对象不是早期 06b 的球面近似方案，而是当前主链实际用于 `view_weight` 的 `sensor_zenith_angle` 角度层。",
        "- ECEF 在这里作为更严格的独立几何审计基准使用，不等同于业务官方逐像元真值产品。",
        "",
        "## 1. 当前 06e 角度来源摘要",
        "",
    ]
    for row in source_df.itertuples():
        lines.append(
            f"- {row.satellite}: angle_source_level={row.angle_source_level}, "
            f"coverage={row.coverage_ratio:.3f}, source=`{row.source_file}`"
        )

    lines.extend(["", "## 2. 当前 06e VZA 与 audited ECEF 的差异统计", ""])
    for row in current_rows.itertuples():
        lines.append(
            f"- {row.satellite}: mean_abs_diff={row.mean_abs_diff_deg:.4f} deg, "
            f"rmse={row.rmse_deg:.4f} deg, p95_abs_diff={row.p95_abs_diff_deg:.4f} deg, "
            f"max_abs_diff={row.max_abs_diff_deg:.4f} deg, source_level={row.angle_source_level}"
        )

    lines.extend(["", "## 3. 与 06b 基线对比", ""])
    for row in improvement_df.itertuples():
        lines.append(
            f"- {row.satellite}: legacy06b_mean_abs={row.legacy06b_mean_abs_diff_deg:.4f} deg, "
            f"current06e_mean_abs={row.current06e_mean_abs_diff_deg:.4f} deg, "
            f"change={row.mean_abs_diff_change_deg:+.4f} deg, status={row.status}"
        )

    nav_rows = current_rows[current_rows["angle_source_level"] == "OFFICIAL_NAV_DERIVED"]
    official_rows = current_rows[current_rows["angle_source_level"].isin(["OFFICIAL_PIXEL_ANGLE", "OFFICIAL_GRIDDED_ANGLE"])]
    if not nav_rows.empty:
        nav_max_mae = float(nav_rows["mean_abs_diff_deg"].max())
        nav_max_p95 = float(nav_rows["p95_abs_diff_deg"].max())
    else:
        nav_max_mae = math.nan
        nav_max_p95 = math.nan
    if not official_rows.empty:
        official_max_mae = float(official_rows["mean_abs_diff_deg"].max())
        official_max_p95 = float(official_rows["p95_abs_diff_deg"].max())
    else:
        official_max_mae = math.nan
        official_max_p95 = math.nan

    lines.extend(
        [
            "",
            "## 4. 审计结论",
            "",
            "- 对 GOES / Meteosat 这类 `OFFICIAL_NAV_DERIVED` 角度层，应把它们理解为“当前实现口径 vs 更严格 ECEF 基准”的实现一致性检查。",
            "- 对 FY4B / Himawari 这类官方角度层，应把它们理解为“当前主链实际使用的官方角度层 vs 独立 ECEF 基准”的后验几何复核，而不是谁替代谁。",
            (
                f"- 当前 `OFFICIAL_NAV_DERIVED` 组的最差统计约为 "
                f"mean_abs_diff={nav_max_mae:.4f} deg, p95_abs_diff={nav_max_p95:.4f} deg。"
                if np.isfinite(nav_max_mae)
                else "- 当前没有 `OFFICIAL_NAV_DERIVED` 角度层统计。"
            ),
            (
                f"- 当前官方角度层组（FY4B/Himawari）的最差统计约为 "
                f"mean_abs_diff={official_max_mae:.4f} deg, p95_abs_diff={official_max_p95:.4f} deg。"
                if np.isfinite(official_max_mae)
                else "- 当前没有官方角度层统计。"
            ),
            "- 因此，本次审计的关键意义不在于把所有卫星都压成同一种‘真值关系’，而在于确认当前 06e 真正进入 rating 的 VZA 口径是否足够稳定、来源是否清楚、与更严格几何基准是否保持在可解释范围内。",
            "",
            "## 输出文件",
            "",
            f"- `{SOURCE_CSV}`",
            f"- `{COMPARISON_CSV}`",
            f"- `{IMPROVEMENT_CSV}`",
            f"- `{REPORT_MD}`",
        ]
    )
    text = "\n".join(lines) + "\n"
    REPORT_MD.write_text(text, encoding="utf-8-sig")
    return text


def write_fy4b_diagnosis(source_df: pd.DataFrame, comparison_df: pd.DataFrame, improvement_df: pd.DataFrame) -> str:
    src = source_df[source_df["satellite"] == "FY4B"].iloc[0]
    comp = comparison_df[comparison_df["satellite"] == "FY4B"].copy()
    row_ecef = comp[comp["comparison"] == "current06e_vza_minus_audited_ecef"].iloc[0]
    row_sph = comp[comp["comparison"] == "current06e_vza_minus_audited_spherical"].iloc[0]
    imp = improvement_df[improvement_df["satellite"] == "FY4B"].iloc[0]
    lines = [
        "# FY4B 06e VZA 差异诊断",
        "",
        "## 结论",
        "",
        "- 当前证据更支持“FY4B 官方角度层落到目标网格时所依赖的 geolocate/重投影口径存在系统性偏差”，而不是“ECEF 基准本身有问题”或“纯粹是球面/椭球差异造成”。",
        "",
        "## 直接证据",
        "",
        f"- FY4B 当前 06e vs audited ECEF: mean_abs_diff={row_ecef['mean_abs_diff_deg']:.4f} deg, p95_abs_diff={row_ecef['p95_abs_diff_deg']:.4f} deg, max_abs_diff={row_ecef['max_abs_diff_deg']:.4f} deg。",
        f"- FY4B 当前 06e vs audited spherical: mean_abs_diff={row_sph['mean_abs_diff_deg']:.4f} deg, p95_abs_diff={row_sph['p95_abs_diff_deg']:.4f} deg, max_abs_diff={row_sph['max_abs_diff_deg']:.4f} deg。",
        f"- FY4B 旧 06b spherical vs audited ECEF: mean_abs_diff={imp['legacy06b_mean_abs_diff_deg']:.4f} deg, p95_abs_diff={imp['legacy06b_p95_abs_diff_deg']:.4f} deg。",
        "- 由于 `current06e_vs_ECEF` 与 `current06e_vs_spherical` 的差异量级都远大于 `06b spherical vs ECEF` 的 0.017 deg 量级，这说明主导项不是地球球面/椭球模型差异，而是当前官方角度层落格口径本身。",
        "",
        "## 元数据与实现证据",
        "",
        f"- 当前 FY4B 角度层来源级别: `{src['angle_source_level']}`。",
        f"- 当前 FY4B 角度层说明: `{src['notes']}`。",
        "- 当前 `FY4B_ANGLE_sensor_zenith_angle` 的 metadata 明确写到：`FY4B lon/lat derived from source L2 fixed-grid coordinates using legacy geostationary extent`，并且由于 GEO 产品本身缺少直接可用的投影变量，重投影时回退复用了同格 CLM 源文件。",
        "- FY4B GEO native 标准化层中没有 `latitude/longitude`，因此 06e 不是用官方像元经纬度直接重投影，而是先构造了一套近似 lon/lat，再把官方角度数组 nearest 落格。",
        "- FY4B GEO HDF 文件本身包含 `LineNumber`、`ColumnNumber`、`dSamplingAngle`、`dSteppingAngle` 等导航相关字段，说明当前 `x_extent = y_extent = 5.5e6 m` 的 legacy geostationary extent 只是临时 geolocate 路径，不是唯一也未必是最优路径。",
        "",
        "## 当前判断",
        "",
        "- 更可能的主因: `FY4B 官方角度层 -> 目标网格` 这一步使用的 geolocate/重投影口径偏粗，导致角度层在目标网格上的空间落点与 audited ECEF 的几何关系不完全一致。",
        "- 目前不优先支持的解释: “FY4B 官方 VZA 定义本身错误”。现有证据不足以支持把问题归因到官方角度物理定义。",
        "- 目前也不支持把问题主要归因于“球面 vs 椭球”差异，因为如果是这个原因，06b spherical vs ECEF 不会只有约 0.017 deg。",
        "",
        "## 下一步建议",
        "",
        "- 优先改进 FY4B GEO 的 geolocation：尝试直接利用 `LineNumber/ColumnNumber/dSamplingAngle/dSteppingAngle` 重建 fixed-grid scan geometry，而不是继续依赖 `legacy geostationary extent`。",
        "- 在改进 geolocation 后，重新生成 `FY4B_ANGLE_sensor_zenith_angle_grid_*` 并重跑本审计，观察 FY4B mean_abs_diff 是否显著下降。",
    ]
    text = "\n".join(lines) + "\n"
    FY4B_DIAG_MD.write_text(text, encoding="utf-8-sig")
    return text


def main() -> int:
    params_list = F06C.gather_geometry_params()
    source_df = build_source_inventory(params_list)
    comparison_df = build_comparison_table(params_list, source_df)
    improvement_df = build_improvement_table(comparison_df)
    build_report(source_df, comparison_df, improvement_df)
    write_fy4b_diagnosis(source_df, comparison_df, improvement_df)
    print(f"wrote: {REPORT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
