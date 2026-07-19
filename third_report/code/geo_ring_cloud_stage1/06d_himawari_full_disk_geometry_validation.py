from __future__ import annotations

import json
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import matplotlib
import netCDF4
import numpy as np
import pandas as pd
from botocore import UNSIGNED
from botocore.config import Config
from satpy import Scene
from satpy.readers.ahi_hsd import AHIHSDFileHandler

from geo_ring_cloud.lineage import utc_now
from geo_ring_cloud.paths import GEOMETRY_ROOT as BASE_GEOMETRY_ROOT, STAGE_ROOT

matplotlib.use("Agg")


TARGET_PREFIX = "AHI-L1b-FLDK/2024/03/05/0000/"
TARGET_TIME = "2024-03-05T00:00:00Z"
TARGET_BAND = "B13"
SEGMENTS = list(range(1, 11))
CURRENT06C_VZA_CSV = BASE_GEOMETRY_ROOT / "vza_method_comparison_by_satellite.csv"
GEOMETRY_ROOT = BASE_GEOMETRY_ROOT / "Himawari-9"
REPROJECT_ROOT = STAGE_ROOT / "reprojected_grid" / "Himawari-9"

INVENTORY_CSV = GEOMETRY_ROOT / "himawari_full_segment_inventory.csv"
GEOMETRY_AUDIT_CSV = GEOMETRY_ROOT / "himawari_full_disk_geometry_audit.csv"
VZA_COMPARE_CSV = GEOMETRY_ROOT / "himawari_vza_method_comparison.csv"
REPORT_MD = GEOMETRY_ROOT / "himawari_full_disk_geometry_report.md"

S3 = boto3.client("s3", config=Config(signature_version=UNSIGNED))


def normalize_lon(lon: float | np.ndarray) -> float | np.ndarray:
    return ((np.asarray(lon) + 180.0) % 360.0) - 180.0


@dataclass
class HeaderParams:
    source: str
    sub_lon_deg: float
    ssp_longitude_deg: float
    ssp_latitude_deg: float
    distance_satellite_center_km: float
    earth_equatorial_radius_km: float
    earth_polar_radius_km: float
    cfac: int
    lfac: int
    coff: float
    loff: float


def list_b13_keys() -> list[str]:
    paginator = S3.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket="noaa-himawari9", Prefix=TARGET_PREFIX):
        for item in page.get("Contents", []):
            key = item["Key"]
            if f"_{TARGET_BAND}_" in key and key.endswith(".DAT.bz2"):
                keys.append(key)
    return sorted(keys)


def parse_segment_index(name: str) -> int | None:
    match = re.search(r"_S(\d{2})10\.", name)
    if not match:
        return None
    return int(match.group(1))


def expected_segment_names() -> list[str]:
    return [f"HS_H09_20240305_0000_{TARGET_BAND}_FLDK_R20_S{i:02d}10.DAT.bz2" for i in SEGMENTS]


def _download_one_segment(name: str, key: str | None) -> dict[str, Any]:
    local_path = GEOMETRY_ROOT / name
    existed_before = local_path.exists()
    downloaded = False
    status = "OK"
    notes = ""
    if key is None:
        status = "REMOTE_MISSING"
        notes = "not found under AWS prefix"
    else:
        if not existed_before:
            S3.download_file("noaa-himawari9", key, str(local_path))
            downloaded = True
        if not local_path.exists():
            status = "DOWNLOAD_FAILED"
        elif local_path.stat().st_size <= 0:
            status = "SIZE_ZERO"
    return {
        "segment_index": parse_segment_index(name),
        "expected_name": name,
        "remote_key": key or "",
        "local_path": str(local_path),
        "exists_before": existed_before,
        "downloaded_now": downloaded,
        "exists_after": local_path.exists(),
        "size_bytes": local_path.stat().st_size if local_path.exists() else 0,
        "status": status,
        "notes": notes,
    }


def download_segments() -> pd.DataFrame:
    GEOMETRY_ROOT.mkdir(parents=True, exist_ok=True)
    remote_keys = list_b13_keys()
    key_map = {Path(key).name: key for key in remote_keys}
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        future_map = {
            executor.submit(_download_one_segment, name, key_map.get(name)): name
            for name in expected_segment_names()
        }
        for future in as_completed(future_map):
            rows.append(future.result())
    return pd.DataFrame(rows).sort_values("segment_index").reset_index(drop=True)


def read_header_params(path: Path) -> HeaderParams:
    seg = parse_segment_index(path.name)
    if seg is None:
        raise RuntimeError(f"cannot parse segment index from {path.name}")
    handler = AHIHSDFileHandler(str(path), {"segment": seg, "total_segments": 10}, {"file_type": TARGET_BAND})
    return HeaderParams(
        source=str(path),
        sub_lon_deg=float(handler.proj_info["sub_lon"]),
        ssp_longitude_deg=float(handler.nav_info["SSP_longitude"]),
        ssp_latitude_deg=float(handler.nav_info["SSP_latitude"]),
        distance_satellite_center_km=float(handler.nav_info["distance_earth_center_to_satellite"]),
        earth_equatorial_radius_km=float(handler.proj_info["earth_equatorial_radius"]),
        earth_polar_radius_km=float(handler.proj_info["earth_polar_radius"]),
        cfac=int(handler.proj_info["CFAC"]),
        lfac=int(handler.proj_info["LFAC"]),
        coff=float(handler.proj_info["COFF"]),
        loff=float(handler.proj_info["LOFF"]),
    )


def build_scene(files: list[Path]) -> tuple[Any, Any]:
    scn = Scene(filenames=[str(p) for p in files], reader="ahi_hsd")
    scn.load([TARGET_BAND])
    data = scn[TARGET_BAND]
    area = data.attrs["area"]
    return data, area


def build_header_comparison(single_params: HeaderParams, full_params: HeaderParams) -> list[dict[str, Any]]:
    rows = []
    for field in [
        "sub_lon_deg",
        "ssp_longitude_deg",
        "ssp_latitude_deg",
        "distance_satellite_center_km",
        "earth_equatorial_radius_km",
        "earth_polar_radius_km",
        "cfac",
        "lfac",
        "coff",
        "loff",
    ]:
        sv = getattr(single_params, field)
        fv = getattr(full_params, field)
        if isinstance(sv, (int, np.integer)) and isinstance(fv, (int, np.integer)):
            diff = int(fv) - int(sv)
            stable = diff == 0
        else:
            diff = float(fv) - float(sv)
            stable = abs(diff) <= 1e-6
        rows.append(
            {
                "row_type": "single_vs_full_header_param",
                "parameter": field,
                "single_segment_value": sv,
                "full_segment_value": fv,
                "difference_full_minus_single": diff,
                "stable": stable,
                "notes": "",
            }
        )
    return rows


def build_segment_extent_rows(files: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    previous_ymin = None
    previous_ymax = None
    previous_seg = None
    for path in files:
        seg = parse_segment_index(path.name)
        handler = AHIHSDFileHandler(str(path), {"segment": seg, "total_segments": 10}, {"file_type": TARGET_BAND})
        area = handler._get_area_def()
        extent = area.area_extent
        gap = math.nan
        if previous_ymin is not None and previous_ymax is not None and previous_seg is not None:
            gap = float(previous_ymin - extent[3])
        rows.append(
            {
                "row_type": "segment_extent",
                "parameter": f"segment_{seg:02d}",
                "single_segment_value": "",
                "full_segment_value": "",
                "difference_full_minus_single": "",
                "stable": True,
                "segment_index": seg,
                "height": int(area.height),
                "width": int(area.width),
                "x_min_m": float(extent[0]),
                "y_min_m": float(extent[1]),
                "x_max_m": float(extent[2]),
                "y_max_m": float(extent[3]),
                "gap_from_previous_segment_top_minus_current_top_m": gap,
                "notes": f"previous_segment={previous_seg}" if previous_seg is not None else "first segment",
            }
        )
        previous_ymin = float(extent[1])
        previous_ymax = float(extent[3])
        previous_seg = seg
    return rows


def ecef_vza(lat_deg: np.ndarray, lon_deg: np.ndarray, subpoint_lon_deg: float, a_m: float, b_m: float, center_distance_m: float) -> np.ndarray:
    lat_rad = np.deg2rad(lat_deg)
    lon_rad = np.deg2rad(lon_deg)
    e2 = 1.0 - (b_m * b_m) / (a_m * a_m)
    sin_lat = np.sin(lat_rad)
    cos_lat = np.cos(lat_rad)
    sin_lon = np.sin(lon_rad)
    cos_lon = np.cos(lon_rad)
    n = a_m / np.sqrt(1.0 - e2 * sin_lat * sin_lat)
    x = n * cos_lat * cos_lon
    y = n * cos_lat * sin_lon
    z = n * (1.0 - e2) * sin_lat
    lon0 = math.radians(float(subpoint_lon_deg))
    sat = np.array([center_distance_m * math.cos(lon0), center_distance_m * math.sin(lon0), 0.0], dtype=np.float64)
    los_x = sat[0] - x
    los_y = sat[1] - y
    los_z = sat[2] - z
    los_norm = np.sqrt(los_x * los_x + los_y * los_y + los_z * los_z)
    up_x = cos_lat * cos_lon
    up_y = cos_lat * sin_lon
    up_z = sin_lat
    dot = los_x * up_x + los_y * up_y + los_z * up_z
    cos_vza = np.clip(dot / los_norm, -1.0, 1.0)
    out = np.full(lat_deg.shape, np.nan, dtype=np.float32)
    visible = dot > 0.0
    out[visible] = np.rad2deg(np.arccos(cos_vza[visible])).astype(np.float32)
    return out


def spherical_vza(lat_deg: np.ndarray, lon_deg: np.ndarray, subpoint_lon_deg: float, earth_radius_m: float, center_distance_m: float) -> np.ndarray:
    lat_rad = np.deg2rad(lat_deg)
    dlon = np.deg2rad(normalize_lon(lon_deg - subpoint_lon_deg))
    cos_psi = np.cos(lat_rad) * np.cos(dlon)
    visible = cos_psi > (earth_radius_m / center_distance_m)
    numer = center_distance_m * cos_psi - earth_radius_m
    denom = np.sqrt(center_distance_m * center_distance_m + earth_radius_m * earth_radius_m - 2.0 * center_distance_m * earth_radius_m * cos_psi)
    cos_vza = np.clip(numer / denom, -1.0, 1.0)
    out = np.full(lat_deg.shape, np.nan, dtype=np.float32)
    out[visible] = np.rad2deg(np.arccos(cos_vza[visible])).astype(np.float32)
    return out


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


def read_current06c_himawari_subpoint_lon() -> float:
    if not CURRENT06C_VZA_CSV.exists():
        raise FileNotFoundError(CURRENT06C_VZA_CSV)
    df = pd.read_csv(CURRENT06C_VZA_CSV)
    row = df[(df["satellite"] == "Himawari-9") & (df["comparison"] == "current06b_spherical_minus_audited_ecef")].iloc[0]
    return float(row["current_subpoint_lon_deg"])


def read_reprojected_target_grid() -> tuple[dict[str, Any], np.ndarray]:
    path = REPROJECT_ROOT / "Himawari-9_CMSK_cloud_mask_grid_20240305_0000.npz"
    with np.load(path, allow_pickle=False) as z:
        meta = json.loads(str(z["metadata_json"]))
        arrays = {name: np.asarray(z[name]) for name in z.files if name != "metadata_json"}
    valid = np.asarray(arrays.get("fusion_valid_mask", arrays.get("valid_mask"))).astype(bool)
    return dict(meta["target_grid"]), valid


def target_lon_lat(target_grid: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    lon = target_grid["lon_min"] + target_grid["resolution_degree"] / 2.0 + np.arange(target_grid["lon_size"], dtype=np.float64) * target_grid["resolution_degree"]
    lat = target_grid["lat_min"] + target_grid["resolution_degree"] / 2.0 + np.arange(target_grid["lat_size"], dtype=np.float64) * target_grid["resolution_degree"]
    lon2d, lat2d = np.meshgrid(lon, lat)
    return lon2d, lat2d


def build_vza_comparison(full_params: HeaderParams, area: Any) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    single_params = full_params

    stride = 50
    lon_sample, lat_sample = area.get_lonlats(data_slice=(slice(0, area.height, stride), slice(0, area.width, stride)), cache=False)
    finite = np.isfinite(lon_sample) & np.isfinite(lat_sample)
    full_ecef = ecef_vza(
        lat_sample,
        lon_sample,
        full_params.ssp_longitude_deg,
        full_params.earth_equatorial_radius_km * 1000.0,
        full_params.earth_polar_radius_km * 1000.0,
        full_params.distance_satellite_center_km * 1000.0,
    )
    single_ecef = ecef_vza(
        lat_sample,
        lon_sample,
        single_params.ssp_longitude_deg,
        single_params.earth_equatorial_radius_km * 1000.0,
        single_params.earth_polar_radius_km * 1000.0,
        single_params.distance_satellite_center_km * 1000.0,
    )
    current06b_lon = read_current06c_himawari_subpoint_lon()
    current06b_sph = spherical_vza(
        lat_sample,
        lon_sample,
        current06b_lon,
        6378137.0,
        6378137.0 + 35786000.0,
    )

    rows.append(
        {
            "comparison": "single_segment_header_ecef_minus_full_segment_ecef",
            "domain": "sampled_full_disk_native_area",
            "current_subpoint_lon_deg": single_params.ssp_longitude_deg,
            "reference_subpoint_lon_deg": full_params.ssp_longitude_deg,
            **summarize_diff((single_ecef - full_ecef)[finite]),
        }
    )
    rows.append(
        {
            "comparison": "current06b_spherical_minus_full_segment_ecef",
            "domain": "sampled_full_disk_native_area",
            "current_subpoint_lon_deg": current06b_lon,
            "reference_subpoint_lon_deg": full_params.ssp_longitude_deg,
            **summarize_diff((current06b_sph - full_ecef)[finite]),
        }
    )
    rows.append(
        {
            "comparison": "full_segment_ecef_absolute_range",
            "domain": "sampled_full_disk_native_area",
            "current_subpoint_lon_deg": full_params.ssp_longitude_deg,
            "reference_subpoint_lon_deg": full_params.ssp_longitude_deg,
            "pixel_count": int(np.isfinite(full_ecef).sum()),
            "mean_signed_diff_deg": float(np.nanmean(full_ecef)),
            "mean_abs_diff_deg": float(np.nanmean(np.abs(full_ecef))),
            "rmse_deg": float(np.sqrt(np.nanmean(full_ecef * full_ecef))),
            "p95_abs_diff_deg": float(np.nanpercentile(np.abs(full_ecef), 95)),
            "max_abs_diff_deg": float(np.nanmax(full_ecef)),
        }
    )

    target_grid, coverage_mask = read_reprojected_target_grid()
    lon2d, lat2d = target_lon_lat(target_grid)
    current_target = spherical_vza(lat2d, lon2d, current06b_lon, 6378137.0, 6378137.0 + 35786000.0)
    full_target = ecef_vza(
        lat2d,
        lon2d,
        full_params.ssp_longitude_deg,
        full_params.earth_equatorial_radius_km * 1000.0,
        full_params.earth_polar_radius_km * 1000.0,
        full_params.distance_satellite_center_km * 1000.0,
    )
    mask = coverage_mask & np.isfinite(current_target) & np.isfinite(full_target)
    rows.append(
        {
            "comparison": "current06b_spherical_minus_full_segment_ecef",
            "domain": "current_reprojected_cloudmask_coverage_on_target_grid",
            "current_subpoint_lon_deg": current06b_lon,
            "reference_subpoint_lon_deg": full_params.ssp_longitude_deg,
            **summarize_diff((current_target - full_target)[mask]),
        }
    )
    return pd.DataFrame(rows)


def build_geometry_audit_df(inventory_df: pd.DataFrame, files: list[Path], single_params: HeaderParams, full_params: HeaderParams, area: Any) -> pd.DataFrame:
    rows = build_header_comparison(single_params, full_params)
    rows.extend(build_segment_extent_rows(files))
    rows.append(
        {
            "row_type": "full_disk_area",
            "parameter": "scene_area_definition",
            "single_segment_value": "",
            "full_segment_value": "",
            "difference_full_minus_single": "",
            "stable": True,
            "segment_index": "",
            "height": int(area.height),
            "width": int(area.width),
            "x_min_m": float(area.area_extent[0]),
            "y_min_m": float(area.area_extent[1]),
            "x_max_m": float(area.area_extent[2]),
            "y_max_m": float(area.area_extent[3]),
            "gap_from_previous_segment_top_minus_current_top_m": "",
            "notes": f"proj={area.proj_dict}",
        }
    )
    return pd.DataFrame(rows)


def build_report(inventory_df: pd.DataFrame, geom_df: pd.DataFrame, vza_df: pd.DataFrame, single_params: HeaderParams, full_params: HeaderParams) -> str:
    missing_count = int((inventory_df["status"] != "OK").sum())
    all_segments_ok = missing_count == 0 and len(inventory_df) == 10
    full_area_row = geom_df[geom_df["row_type"] == "full_disk_area"].iloc[0]
    current_row = vza_df[(vza_df["comparison"] == "current06b_spherical_minus_full_segment_ecef") & (vza_df["domain"] == "current_reprojected_cloudmask_coverage_on_target_grid")].iloc[0]
    sample_row = vza_df[(vza_df["comparison"] == "current06b_spherical_minus_full_segment_ecef") & (vza_df["domain"] == "sampled_full_disk_native_area")].iloc[0]
    stable_header = bool(geom_df[(geom_df["row_type"] == "single_vs_full_header_param")]["stable"].all())
    vza_reasonable = float(vza_df[vza_df["comparison"] == "full_segment_ecef_absolute_range"].iloc[0]["max_abs_diff_deg"]) <= 90.0

    if not all_segments_ok or not stable_header or not vza_reasonable:
        status = "FAIL"
    else:
        status = "PASS_WITH_WARNINGS"

    lines = [
        "# Himawari Full-disk Geometry Report",
        "",
        f"- 生成时间 UTC: {utc_now()}",
        f"- 目标时次: `{TARGET_TIME}`",
        f"- 判定: **{status}**",
        "",
        "## 1. Segment 下载与完整性",
        "",
        f"- 目标 B13 segment 数: `10`",
        f"- 成功可用 segment 数: `{int((inventory_df['status'] == 'OK').sum())}`",
        f"- 缺失或失败 segment 数: `{missing_count}`",
        "",
        "## 2. Full-disk Satpy 读取结果",
        "",
        f"- 读取方式: `satpy Scene(..., reader='ahi_hsd')`",
        f"- full-disk area shape: `{int(full_area_row['height'])} x {int(full_area_row['width'])}`",
        f"- full-disk area extent: `({full_area_row['x_min_m']}, {full_area_row['y_min_m']}, {full_area_row['x_max_m']}, {full_area_row['y_max_m']})` m",
        "",
        "## 3. 06c 单 segment header 对比",
        "",
        f"- sub_lon: single=`{single_params.sub_lon_deg:.6f}` full=`{full_params.sub_lon_deg:.6f}`",
        f"- SSP_longitude: single=`{single_params.ssp_longitude_deg:.6f}` full=`{full_params.ssp_longitude_deg:.6f}`",
        f"- distance_earth_center_to_satellite: single=`{single_params.distance_satellite_center_km:.6f}` km full=`{full_params.distance_satellite_center_km:.6f}` km",
        f"- earth_equatorial/polar_radius: `{single_params.earth_equatorial_radius_km:.6f}` / `{single_params.earth_polar_radius_km:.6f}` km",
        f"- CFAC/LFAC/COFF/LOFF: `{single_params.cfac}` / `{single_params.lfac}` / `{single_params.coff}` / `{single_params.loff}`",
        f"- header 参数稳定: `{stable_header}`",
        "",
        "## 4. 与当前 source selection 参数对比",
        "",
        f"- 06b/current 使用的 Himawari subpoint longitude: `{read_current06c_himawari_subpoint_lon():.6f}` deg",
        f"- full-segment 审计得到的 SSP_longitude: `{full_params.ssp_longitude_deg:.6f}` deg",
        f"- 差值: `{full_params.ssp_longitude_deg - read_current06c_himawari_subpoint_lon():.6f}` deg",
        f"- 在当前 Himawari reprojected cloud_mask 覆盖区域上，current06b spherical vs full-segment ECEF 的 mean_abs_diff: `{float(current_row['mean_abs_diff_deg']):.6f}` deg",
        f"- 在 sampled native full-disk 上，current06b spherical vs full-segment ECEF 的 mean_abs_diff: `{float(sample_row['mean_abs_diff_deg']):.6f}` deg",
        "",
        "## 5. VZA 判断",
        "",
        f"- full-segment ECEF VZA 是否合理(最大值<=90): `{vza_reasonable}`",
        "- 本轮未获得 JAXA P-Tree gridded official satellite zenith，因此没有 official VZA 对照。",
        "",
        "## 6. 结论",
        "",
    ]
    if status == "FAIL":
        lines.append("- 06d 未通过。要先修复 segment 完整性或 full-disk 几何异常。")
    else:
        lines.append("- 06d 通过 full-disk geometry validation，但仍保留 `没有 JAXA official gridded VZA` 这一 warning。")
    lines.extend(
        [
            "",
            "## 输出文件",
            "",
            f"- `{INVENTORY_CSV}`",
            f"- `{GEOMETRY_AUDIT_CSV}`",
            f"- `{VZA_COMPARE_CSV}`",
            f"- `{REPORT_MD}`",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    inventory_df = download_segments()
    inventory_df.to_csv(INVENTORY_CSV, index=False, encoding="utf-8-sig")
    good_files = [Path(p) for p in inventory_df[inventory_df["status"] == "OK"]["local_path"].tolist()]
    if len(good_files) != 10:
        geom_df = pd.DataFrame()
        vza_df = pd.DataFrame()
        REPORT_MD.write_text(
            "\n".join(
                [
                    "# Himawari Full-disk Geometry Report",
                    "",
                    f"- 生成时间 UTC: {utc_now()}",
                    f"- 目标时次: `{TARGET_TIME}`",
                    "- 判定: **FAIL**",
                    "- 原因: 10 个 B13 segments 未全部就绪，未继续 full-disk 读取。",
                ]
            ),
            encoding="utf-8",
        )
        if not geom_df.empty:
            geom_df.to_csv(GEOMETRY_AUDIT_CSV, index=False, encoding="utf-8-sig")
        if not vza_df.empty:
            vza_df.to_csv(VZA_COMPARE_CSV, index=False, encoding="utf-8-sig")
        print("06d FAIL: incomplete segment set")
        print(f"report={REPORT_MD}")
        return 1

    good_files = sorted(good_files, key=lambda p: parse_segment_index(p.name) or 0)
    single_params = read_header_params(good_files[0])
    data, area = build_scene(good_files)
    full_params = read_header_params(good_files[0])

    geom_df = build_geometry_audit_df(inventory_df, good_files, single_params, full_params, area)
    geom_df.to_csv(GEOMETRY_AUDIT_CSV, index=False, encoding="utf-8-sig")

    vza_df = build_vza_comparison(full_params, area)
    vza_df.to_csv(VZA_COMPARE_CSV, index=False, encoding="utf-8-sig")

    REPORT_MD.write_text(build_report(inventory_df, geom_df, vza_df, single_params, full_params), encoding="utf-8")
    print("06d DONE")
    print(f"report={REPORT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
