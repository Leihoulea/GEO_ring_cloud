from __future__ import annotations

import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import h5py
import matplotlib
import netCDF4
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from satpy.readers.ahi_hsd import AHIHSDFileHandler
from satpy.readers.seviri_l1b_native import NativeMSGFileHandler

from geo_ring_cloud import fusion_support as F06
from geo_ring_cloud.lineage import utc_now
from geo_ring_cloud.paths import GEOMETRY_ROOT, STAGE_ROOT
from geo_ring_cloud.pipeline_layout import REPORT_DIR as REPORT_ROOT

STAGE_ID = "stage_06c"

REPROJECT_ROOT = STAGE_ROOT / "reprojected_grid"
STANDARDIZED_ROOT = STAGE_ROOT / "standardized_native"

TIME_TAG = "20240305_0000"
TARGET_TIME = "2024-03-05T00:00:00Z"

FILE_INVENTORY_CSV = GEOMETRY_ROOT / "geometry_sample_file_inventory.csv"
GOES_AUDIT_CSV = GEOMETRY_ROOT / "goes_geometry_metadata_audit.csv"
GOES_L1_L2_CSV = GEOMETRY_ROOT / "goes_l1b_vs_l2_projection_check.csv"
HIMAWARI_AUDIT_CSV = GEOMETRY_ROOT / "himawari_geometry_metadata_audit.csv"
HIMAWARI_REPORT_MD = GEOMETRY_ROOT / "himawari_header_parse_report.md"
METEOSAT_AUDIT_CSV = GEOMETRY_ROOT / "meteosat_geometry_metadata_audit.csv"
METEOSAT_REPORT_MD = GEOMETRY_ROOT / "meteosat_reader_parse_report.md"
VZA_COMPARISON_CSV = GEOMETRY_ROOT / "vza_method_comparison_by_satellite.csv"
VZA_REPORT_MD = GEOMETRY_ROOT / "vza_method_comparison_report.md"
VZA_QUICKLOOK_DIR = GEOMETRY_ROOT / "vza_difference_quicklooks"

ROW_CHUNK = 120


def normalize_lon(lon: float | np.ndarray) -> float | np.ndarray:
    return ((np.asarray(lon) + 180.0) % 360.0) - 180.0


@dataclass
class GeometryParameters:
    satellite: str
    service: str
    subpoint_lon_deg: float
    subpoint_source: str
    semi_major_axis_m: float
    semi_major_axis_source: str
    semi_minor_axis_m: float
    semi_minor_axis_source: str
    center_distance_m: float
    center_distance_source: str
    height_above_ellipsoid_m: float
    height_source: str
    notes: str


def load_npz(path: Path) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    with np.load(path, allow_pickle=False) as z:
        metadata = json.loads(str(z["metadata_json"]))
        arrays = {name: np.asarray(z[name]) for name in z.files if name != "metadata_json"}
    return metadata, arrays


def find_first_existing(paths: list[Path]) -> Path:
    for path in paths:
        if path.exists():
            return path
    raise FileNotFoundError(f"none of the candidate paths exist: {paths}")


def standardized_path(satellite: str, product: str) -> Path:
    return STANDARDIZED_ROOT / f"{satellite}_{product}_{TIME_TAG}_native_cloud_v0.npz"


def reprojected_path(satellite: str, product: str, variable: str) -> Path:
    return REPROJECT_ROOT / satellite / f"{satellite}_{product}_{variable}_grid_{TIME_TAG}.npz"


def parse_himawari_segment_info(path: Path) -> tuple[int, int]:
    match = re.search(r"_S(\d{2})(\d{2})\.", path.name)
    if not match:
        return 1, 10
    return int(match.group(1)), int(match.group(2))


def read_target_grid() -> dict[str, Any]:
    sample = reprojected_path("GOES-16", "ACMF", "cloud_mask")
    meta, _ = load_npz(sample)
    return dict(meta["target_grid"])


def build_target_lon_lat(target_grid: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    lon = target_grid["lon_min"] + target_grid["resolution_degree"] / 2.0 + np.arange(target_grid["lon_size"], dtype=np.float64) * target_grid["resolution_degree"]
    lat = target_grid["lat_min"] + target_grid["resolution_degree"] / 2.0 + np.arange(target_grid["lat_size"], dtype=np.float64) * target_grid["resolution_degree"]
    return lon, lat


def read_geometry_manifest() -> pd.DataFrame:
    path = GEOMETRY_ROOT / "geometry_sample_download_manifest.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_csv(path)


def build_file_inventory() -> pd.DataFrame:
    manifest = read_geometry_manifest()
    rows: list[dict[str, Any]] = []

    for row in manifest.itertuples():
        local_path = Path(str(row.local_files))
        suffix = "".join(local_path.suffixes).lower()
        if row.satellite.startswith("GOES"):
            detected_type = "GOES ABI-L1b NetCDF"
            reader = "netCDF4.Dataset"
        elif row.satellite.startswith("Himawari"):
            detected_type = "Himawari HSD segment"
            reader = "satpy.readers.ahi_hsd.AHIHSDFileHandler"
        else:
            detected_type = "unknown"
            reader = "unknown"
        rows.append(
            {
                "satellite": row.satellite,
                "service": row.satellite,
                "source_group": "geometry_sample_download_manifest",
                "local_path": str(local_path),
                "exists": local_path.exists(),
                "format_hint": suffix,
                "detected_file_type": detected_type,
                "reader_selected": reader,
                "notes": getattr(row, "notes", ""),
            }
        )

    fy4b_std = standardized_path("FY4B", "GEO")
    if fy4b_std.exists():
        fy_meta, _ = load_npz(fy4b_std)
        fy_source = Path(str(fy_meta["source_file"]))
        rows.append(
            {
                "satellite": "FY4B",
                "service": "FY4B",
                "source_group": "standardized_native_source",
                "local_path": str(fy_source),
                "exists": fy_source.exists(),
                "format_hint": "".join(fy_source.suffixes).lower(),
                "detected_file_type": "FY4B GEO HDF",
                "reader_selected": "h5py.File",
                "notes": "source file referenced by FY4B GEO standardized native NPZ",
            }
        )

    for nat_path in sorted(GEOMETRY_ROOT.rglob("*.nat")):
        name = nat_path.name
        service = "unknown"
        if name.startswith("MSG2-"):
            service = "Meteosat-IODC candidate"
        elif name.startswith("MSG3-"):
            service = "Meteosat-0deg candidate"
        rows.append(
            {
                "satellite": "Meteosat",
                "service": service,
                "source_group": "auto_discovered_native_samples",
                "local_path": str(nat_path),
                "exists": nat_path.exists(),
                "format_hint": "".join(nat_path.suffixes).lower(),
                "detected_file_type": "Meteosat SEVIRI native .nat",
                "reader_selected": "satpy.readers.seviri_l1b_native.NativeMSGFileHandler",
                "notes": nat_path.parent.name,
            }
        )

    df = pd.DataFrame(rows).sort_values(["satellite", "service", "local_path"]).reset_index(drop=True)
    return df


def read_goes_sample_metadata(sample_path: Path) -> dict[str, Any]:
    with netCDF4.Dataset(sample_path) as ds:
        proj = ds.variables["goes_imager_projection"]
        x = np.asarray(ds.variables["x"][:], dtype=np.float64)
        y = np.asarray(ds.variables["y"][:], dtype=np.float64)
        return {
            "longitude_of_projection_origin": float(getattr(proj, "longitude_of_projection_origin")),
            "perspective_point_height": float(getattr(proj, "perspective_point_height")),
            "semi_major_axis": float(getattr(proj, "semi_major_axis")),
            "semi_minor_axis": float(getattr(proj, "semi_minor_axis")),
            "sweep_angle_axis": str(getattr(proj, "sweep_angle_axis")),
            "grid_mapping_name": str(getattr(proj, "grid_mapping_name", "")),
            "x_min": float(np.nanmin(x)),
            "x_max": float(np.nanmax(x)),
            "y_min": float(np.nanmin(y)),
            "y_max": float(np.nanmax(y)),
            "x_size": int(x.size),
            "y_size": int(y.size),
            "x_units": str(getattr(ds.variables["x"], "units", "")),
            "y_units": str(getattr(ds.variables["y"], "units", "")),
        }


def build_goes_audits(manifest: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    sample_rows = []
    compare_rows = []
    for satellite in ["GOES-16", "GOES-18"]:
        row = manifest[manifest["satellite"] == satellite].iloc[0]
        sample_path = Path(str(row["local_files"]))
        l1 = read_goes_sample_metadata(sample_path)

        std_meta, _ = load_npz(standardized_path(satellite, "ACMF"))
        proj_attrs = dict(std_meta["reader_attrs"]["geostationary_projection_attrs"])
        l2_source = Path(str(std_meta["source_file"]))
        l2_xmin = math.nan
        l2_xmax = math.nan
        l2_ymin = math.nan
        l2_ymax = math.nan
        l2_xsize = math.nan
        l2_ysize = math.nan
        l2_xunits = ""
        l2_yunits = ""
        if l2_source.exists():
            with netCDF4.Dataset(l2_source) as ds:
                x = np.asarray(ds.variables["x"][:], dtype=np.float64)
                y = np.asarray(ds.variables["y"][:], dtype=np.float64)
                l2_xmin = float(np.nanmin(x))
                l2_xmax = float(np.nanmax(x))
                l2_ymin = float(np.nanmin(y))
                l2_ymax = float(np.nanmax(y))
                l2_xsize = int(x.size)
                l2_ysize = int(y.size)
                l2_xunits = str(getattr(ds.variables["x"], "units", ""))
                l2_yunits = str(getattr(ds.variables["y"], "units", ""))

        sample_rows.append(
            {
                "satellite": satellite,
                "sample_path": str(sample_path),
                "l2_source_file": str(l2_source),
                **l1,
            }
        )

        for field in [
            "longitude_of_projection_origin",
            "perspective_point_height",
            "semi_major_axis",
            "semi_minor_axis",
            "sweep_angle_axis",
        ]:
            l1_val = l1[field]
            l2_val = proj_attrs.get(field)
            if isinstance(l1_val, float):
                diff = float(l1_val - float(l2_val))
                consistent = abs(diff) <= 1e-6
            else:
                diff = math.nan
                consistent = str(l1_val) == str(l2_val)
            compare_rows.append(
                {
                    "satellite": satellite,
                    "field": field,
                    "l1b_value": l1_val,
                    "l2_value": l2_val,
                    "difference": diff,
                    "consistent": bool(consistent),
                }
            )
        for field, l1_val, l2_val in [
            ("x_range_min", l1["x_min"], l2_xmin),
            ("x_range_max", l1["x_max"], l2_xmax),
            ("y_range_min", l1["y_min"], l2_ymin),
            ("y_range_max", l1["y_max"], l2_ymax),
            ("x_size", l1["x_size"], l2_xsize),
            ("y_size", l1["y_size"], l2_ysize),
            ("x_units", l1["x_units"], l2_xunits),
            ("y_units", l1["y_units"], l2_yunits),
        ]:
            if isinstance(l1_val, (int, float)) and isinstance(l2_val, (int, float)) and not isinstance(l1_val, bool) and not isinstance(l2_val, bool):
                diff = float(l1_val - l2_val)
                consistent = abs(diff) <= 1e-6
            else:
                diff = math.nan
                consistent = str(l1_val) == str(l2_val)
            compare_rows.append(
                {
                    "satellite": satellite,
                    "field": field,
                    "l1b_value": l1_val,
                    "l2_value": l2_val,
                    "difference": diff,
                    "consistent": bool(consistent),
                }
            )

    return pd.DataFrame(sample_rows), pd.DataFrame(compare_rows)


def build_himawari_audit(manifest: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    row = manifest[manifest["satellite"] == "Himawari-9"].iloc[0]
    sample_path = Path(str(row["local_files"]))
    seg, seg_total = parse_himawari_segment_info(sample_path)
    handler = AHIHSDFileHandler(str(sample_path), {"segment": seg, "total_segments": seg_total}, {"file_type": "B13"})
    proj = handler.proj_info
    nav = handler.nav_info
    rec = {
        "satellite": "Himawari-9",
        "sample_path": str(sample_path),
        "platform_name": str(handler.platform_name),
        "observation_area": str(handler.observation_area),
        "segment_index": seg,
        "segment_total": seg_total,
        "sub_lon_deg": float(proj["sub_lon"]),
        "ssp_longitude_deg": float(nav["SSP_longitude"]),
        "ssp_latitude_deg": float(nav["SSP_latitude"]),
        "nadir_longitude_deg": float(nav["nadir_longitude"]),
        "nadir_latitude_deg": float(nav["nadir_latitude"]),
        "distance_from_earth_center_km": float(proj["distance_from_earth_center"]),
        "distance_earth_center_to_satellite_km": float(nav["distance_earth_center_to_satellite"]),
        "earth_equatorial_radius_km": float(proj["earth_equatorial_radius"]),
        "earth_polar_radius_km": float(proj["earth_polar_radius"]),
        "CFAC": int(proj["CFAC"]),
        "LFAC": int(proj["LFAC"]),
        "COFF": float(proj["COFF"]),
        "LOFF": float(proj["LOFF"]),
        "header_sufficient_for_projection_parameters": True,
        "header_sufficient_for_full_disk_segment_completeness_check": False,
        "notes": "single HSD segment header is sufficient for projection/subpoint/height metadata but not for full-disk segment completeness audit",
    }
    report = "\n".join(
        [
            "# Himawari Header Parse Report",
            "",
            f"- 生成时间 UTC: {utc_now()}",
            f"- 样本文件: `{sample_path}`",
            "- 读取方式: `satpy.readers.ahi_hsd.AHIHSDFileHandler`",
            "- 结论: 单个 HSD segment header 足以提取几何导航参数，不足以验证 full disk 全 segment 完整性。",
            "",
            "## 关键参数",
            "",
            f"- sub_lon: `{rec['sub_lon_deg']:.6f}` deg",
            f"- SSP_longitude: `{rec['ssp_longitude_deg']:.6f}` deg",
            f"- SSP_latitude: `{rec['ssp_latitude_deg']:.6f}` deg",
            f"- distance_earth_center_to_satellite: `{rec['distance_earth_center_to_satellite_km']:.6f}` km",
            f"- earth_equatorial_radius: `{rec['earth_equatorial_radius_km']:.6f}` km",
            f"- earth_polar_radius: `{rec['earth_polar_radius_km']:.6f}` km",
            f"- CFAC/LFAC: `{rec['CFAC']}` / `{rec['LFAC']}`",
            f"- COFF/LOFF: `{rec['COFF']}` / `{rec['LOFF']}`",
            "",
            "## 判断",
            "",
            "- 当前样本足以支撑 06c 的 Himawari 几何参数审计和近似/严格 VZA 对比。",
            "- 如果后续要验证 full disk segment 完整性、逐 segment 拼接方向或重建原始 HSD 图像，仍需要 S0110-S1010 完整 segments，或改用 JAXA P-Tree 更完整样本。",
            "",
        ]
    )
    return pd.DataFrame([rec]), report


def infer_meteosat_service(platform_name: str, ssp_lon: float) -> str:
    if abs(ssp_lon - 45.5) < 0.2:
        return "Meteosat-IODC"
    if abs(ssp_lon - 0.0) < 0.2:
        return "Meteosat-0deg"
    return platform_name


def build_meteosat_audit() -> tuple[pd.DataFrame, str]:
    rows: list[dict[str, Any]] = []
    lines = [
        "# Meteosat Reader Parse Report",
        "",
        f"- 生成时间 UTC: {utc_now()}",
        "- 读取方式按文件格式自动选择。本轮发现的样本均为 SEVIRI native `.nat`，因此实际使用 `satpy.readers.seviri_l1b_native.NativeMSGFileHandler`。",
        "",
        "## 样本读取结果",
        "",
    ]
    for nat_path in sorted(GEOMETRY_ROOT.rglob("*.nat")):
        handler = NativeMSGFileHandler(str(nat_path), {}, {})
        proj = dict(handler.mda.get("projection_parameters", {}))
        platform_name = str(handler.mda.get("platform_name", ""))
        ssp_lon = float(proj.get("ssp_longitude"))
        service = infer_meteosat_service(platform_name, ssp_lon)
        a = float(proj.get("a"))
        b = float(proj.get("b"))
        h = float(proj.get("h"))
        row = {
            "service": service,
            "platform_name": platform_name,
            "sample_path": str(nat_path),
            "reader_selected": "seviri_l1b_native",
            "ssp_longitude_deg": ssp_lon,
            "semi_major_axis_m": a,
            "semi_minor_axis_m": b,
            "satellite_height_above_ellipsoid_m": h,
            "center_distance_m": a + h,
            "number_of_lines": int(handler.mda.get("number_of_lines")),
            "number_of_columns": int(handler.mda.get("number_of_columns")),
            "is_full_disk": bool(handler.mda.get("is_full_disk")),
            "available_channels": ",".join([k for k, v in dict(handler.mda.get("available_channels", {})).items() if v]),
            "channel_count": len(handler.mda.get("channel_list", [])),
            "line_column_orientation_status": "metadata_available_native_shape_only",
            "area_extent_status": "native_projection_parameters_available",
            "notes": nat_path.parent.name,
        }
        rows.append(row)
        lines.extend(
            [
                f"- `{nat_path.name}` -> `{service}` / `{platform_name}`",
                f"  - ssp_longitude: `{ssp_lon}` deg",
                f"  - a/b/h: `{a}` / `{b}` / `{h}` m",
                f"  - shape: `{row['number_of_lines']} x {row['number_of_columns']}`",
                f"  - is_full_disk: `{row['is_full_disk']}`",
            ]
        )
    lines.extend(
        [
            "",
            "## 判断",
            "",
            "- 本地 Meteosat native reader 可稳定读取 MSG3 0deg 和 MSG2 IODC 样本的投影参数。",
            "- 当前结果足以为 06c 提供 SSP longitude、a/b、satellite height、native shape。",
            "- 本轮没有生成完整 full-disk lat/lon 场；06c 只做参数级与 VZA 方法级审计。",
            "",
        ]
    )
    return pd.DataFrame(rows), "\n".join(lines)


def current_06b_geometry_params() -> dict[str, dict[str, float]]:
    catalog = F06.load_catalog()
    lon_map = F06.build_subpoint_longitude_map(catalog)
    earth_radius_m = float(F06.EARTH_RADIUS_KM * 1000.0)
    center_distance_m = earth_radius_m + float(F06.GEO_ALTITUDE_KM * 1000.0)
    out = {}
    for satellite, meta in lon_map.items():
        out[satellite] = {
            "subpoint_lon_deg": float(normalize_lon(meta["subpoint_lon_deg"])),
            "semi_major_axis_m": earth_radius_m,
            "semi_minor_axis_m": earth_radius_m,
            "center_distance_m": center_distance_m,
            "height_above_ellipsoid_m": center_distance_m - earth_radius_m,
        }
    return out


def fy4b_audited_params() -> GeometryParameters:
    geo_meta, _ = load_npz(standardized_path("FY4B", "GEO"))
    clm_meta, _ = load_npz(standardized_path("FY4B", "CLM"))
    geo_source = Path(str(geo_meta["source_file"]))
    clm_source = Path(str(clm_meta["source_file"]))

    lon = math.nan
    center = math.nan
    lon_source = "missing"
    center_source = "missing"
    with netCDF4.Dataset(clm_source) as ds:
        if "nominal_satellite_subpoint_lon" in ds.variables:
            lon = float(np.asarray(ds.variables["nominal_satellite_subpoint_lon"][:]).ravel()[0])
            lon_source = "FY4B L2 nominal_satellite_subpoint_lon"
        if "nominal_satellite_height" in ds.variables:
            center = float(np.asarray(ds.variables["nominal_satellite_height"][:]).ravel()[0])
            center_source = "FY4B L2 nominal_satellite_height"

    with h5py.File(geo_source, "r") as hdf:
        attrs = {str(k): hdf.attrs[k] for k in hdf.attrs.keys()}
        a = float(np.asarray(attrs["Semimajor axis of ellipsoid"]).ravel()[0])
        b = float(np.asarray(attrs["Semiminor axis of ellipsoid"]).ravel()[0])
        if not np.isfinite(lon):
            lon = float(np.asarray(attrs["NOMCenterLon"]).ravel()[0])
            lon_source = "FY4B GEO HDF NOMCenterLon fallback"
        if not np.isfinite(center):
            center = float(np.asarray(attrs["NOMSatHeight"]).ravel()[0])
            center_source = "FY4B GEO HDF NOMSatHeight fallback"

    return GeometryParameters(
        satellite="FY4B",
        service="FY4B",
        subpoint_lon_deg=float(normalize_lon(lon)),
        subpoint_source=lon_source,
        semi_major_axis_m=a,
        semi_major_axis_source="FY4B GEO HDF Semimajor axis of ellipsoid",
        semi_minor_axis_m=b,
        semi_minor_axis_source="FY4B GEO HDF Semiminor axis of ellipsoid",
        center_distance_m=center,
        center_distance_source=center_source,
        height_above_ellipsoid_m=center - a,
        height_source="derived from center_distance - semi_major_axis",
        notes=f"geo_source={geo_source}",
    )


def goes_audited_params(goes_df: pd.DataFrame, satellite: str) -> GeometryParameters:
    row = goes_df[goes_df["satellite"] == satellite].iloc[0]
    a = float(row["semi_major_axis"])
    h = float(row["perspective_point_height"])
    return GeometryParameters(
        satellite=satellite,
        service=satellite,
        subpoint_lon_deg=float(normalize_lon(float(row["longitude_of_projection_origin"]))),
        subpoint_source="GOES L1b goes_imager_projection.longitude_of_projection_origin",
        semi_major_axis_m=a,
        semi_major_axis_source="GOES L1b goes_imager_projection.semi_major_axis",
        semi_minor_axis_m=float(row["semi_minor_axis"]),
        semi_minor_axis_source="GOES L1b goes_imager_projection.semi_minor_axis",
        center_distance_m=a + h,
        center_distance_source="GOES L1b semi_major_axis + perspective_point_height",
        height_above_ellipsoid_m=h,
        height_source="GOES L1b goes_imager_projection.perspective_point_height",
        notes=f"sweep_angle_axis={row['sweep_angle_axis']}",
    )


def himawari_audited_params(him_df: pd.DataFrame) -> GeometryParameters:
    row = him_df.iloc[0]
    a = float(row["earth_equatorial_radius_km"]) * 1000.0
    b = float(row["earth_polar_radius_km"]) * 1000.0
    center = float(row["distance_earth_center_to_satellite_km"]) * 1000.0
    return GeometryParameters(
        satellite="Himawari-9",
        service="Himawari-9",
        subpoint_lon_deg=float(normalize_lon(float(row["ssp_longitude_deg"]))),
        subpoint_source="Himawari HSD nav_info.SSP_longitude",
        semi_major_axis_m=a,
        semi_major_axis_source="Himawari HSD proj_info.earth_equatorial_radius",
        semi_minor_axis_m=b,
        semi_minor_axis_source="Himawari HSD proj_info.earth_polar_radius",
        center_distance_m=center,
        center_distance_source="Himawari HSD nav_info.distance_earth_center_to_satellite",
        height_above_ellipsoid_m=center - a,
        height_source="derived from center_distance - equatorial_radius",
        notes="single-segment header metadata",
    )


def meteosat_audited_params(meteo_df: pd.DataFrame, service: str) -> GeometryParameters:
    subset = meteo_df[meteo_df["service"] == service].sort_values("sample_path")
    row = subset.iloc[0]
    a = float(row["semi_major_axis_m"])
    center = float(row["center_distance_m"])
    return GeometryParameters(
        satellite=service,
        service=service,
        subpoint_lon_deg=float(normalize_lon(float(row["ssp_longitude_deg"]))),
        subpoint_source="Meteosat native mda.projection_parameters.ssp_longitude",
        semi_major_axis_m=a,
        semi_major_axis_source="Meteosat native mda.projection_parameters.a",
        semi_minor_axis_m=float(row["semi_minor_axis_m"]),
        semi_minor_axis_source="Meteosat native mda.projection_parameters.b",
        center_distance_m=center,
        center_distance_source="Meteosat native a + h",
        height_above_ellipsoid_m=float(row["satellite_height_above_ellipsoid_m"]),
        height_source="Meteosat native mda.projection_parameters.h",
        notes=f"platform_name={row['platform_name']}",
    )


def all_audited_parameters(goes_df: pd.DataFrame, him_df: pd.DataFrame, meteo_df: pd.DataFrame) -> list[GeometryParameters]:
    return [
        fy4b_audited_params(),
        goes_audited_params(goes_df, "GOES-16"),
        goes_audited_params(goes_df, "GOES-18"),
        himawari_audited_params(him_df),
        meteosat_audited_params(meteo_df, "Meteosat-0deg"),
        meteosat_audited_params(meteo_df, "Meteosat-IODC"),
    ]


def load_reprojected_mask(satellite: str) -> np.ndarray:
    product_map = {
        "FY4B": "CLM",
        "GOES-16": "ACMF",
        "GOES-18": "ACMF",
        "Himawari-9": "CMSK",
        "Meteosat-0deg": "CLM",
        "Meteosat-IODC": "CLM",
    }
    path = reprojected_path(satellite, product_map[satellite], "cloud_mask")
    _, arrays = load_npz(path)
    if "fusion_valid_mask" in arrays:
        return np.asarray(arrays["fusion_valid_mask"]).astype(bool)
    if "valid_mask" in arrays:
        return np.asarray(arrays["valid_mask"]).astype(bool)
    data = np.asarray(arrays["data"])
    return np.isfinite(data)


def load_fy4b_official_vza() -> tuple[np.ndarray, np.ndarray]:
    geo_path = reprojected_path("FY4B", "GEO", "sensor_zenith_angle")
    _, arrays = load_npz(geo_path)
    data = np.asarray(arrays["data"], dtype=np.float32)
    valid = np.asarray(arrays.get("valid_mask", np.isfinite(data))).astype(bool) & np.isfinite(data)
    return data, valid


def spherical_vza_chunk(lat_1d_deg: np.ndarray, lon_1d_deg: np.ndarray, subpoint_lon_deg: float, earth_radius_m: float, center_distance_m: float) -> np.ndarray:
    lat_rad = np.deg2rad(lat_1d_deg[:, None])
    dlon = np.deg2rad(normalize_lon(lon_1d_deg[None, :] - subpoint_lon_deg))
    cos_psi = np.cos(lat_rad) * np.cos(dlon)
    visible = cos_psi > (earth_radius_m / center_distance_m)
    numer = center_distance_m * cos_psi - earth_radius_m
    denom = np.sqrt(center_distance_m * center_distance_m + earth_radius_m * earth_radius_m - 2.0 * center_distance_m * earth_radius_m * cos_psi)
    cos_vza = np.clip(numer / denom, -1.0, 1.0)
    out = np.full((lat_1d_deg.size, lon_1d_deg.size), np.nan, dtype=np.float32)
    out[visible] = np.rad2deg(np.arccos(cos_vza[visible])).astype(np.float32)
    return out


def ecef_vza_chunk(lat_1d_deg: np.ndarray, lon_1d_deg: np.ndarray, subpoint_lon_deg: float, a_m: float, b_m: float, center_distance_m: float) -> np.ndarray:
    lat_rad = np.deg2rad(lat_1d_deg[:, None])
    lon_rad = np.deg2rad(lon_1d_deg[None, :])
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
    visible = dot > 0.0
    out = np.full((lat_1d_deg.size, lon_1d_deg.size), np.nan, dtype=np.float32)
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


def save_difference_quicklook(diff: np.ndarray, valid_mask: np.ndarray, out_path: Path, title: str) -> None:
    arr = np.array(diff, dtype=np.float32, copy=True)
    arr[~valid_mask] = np.nan
    if not np.isfinite(arr).any():
        return
    vmax = float(np.nanpercentile(np.abs(arr[np.isfinite(arr)]), 99))
    vmax = max(vmax, 0.05)
    plt.figure(figsize=(14, 7))
    plt.imshow(arr, cmap="coolwarm", vmin=-vmax, vmax=vmax, origin="lower")
    plt.colorbar(label="deg")
    plt.title(title)
    plt.tight_layout()
    plt.savefig(out_path, dpi=180)
    plt.close()


def build_vza_comparison(audited_params: list[GeometryParameters]) -> pd.DataFrame:
    current_params = current_06b_geometry_params()
    target_grid = read_target_grid()
    lon_1d, lat_1d = build_target_lon_lat(target_grid)
    rows: list[dict[str, Any]] = []

    fy4b_current_full = None
    fy4b_ecef_full = None

    for audited in audited_params:
        valid_mask = load_reprojected_mask(audited.satellite)
        current = current_params[audited.satellite]
        current_vs_ecef_pieces: list[np.ndarray] = []
        audited_spherical_vs_ecef_pieces: list[np.ndarray] = []
        current_full = np.full(valid_mask.shape, np.nan, dtype=np.float32) if audited.satellite == "FY4B" else None
        ecef_full = np.full(valid_mask.shape, np.nan, dtype=np.float32) if audited.satellite == "FY4B" else None
        diff_full = np.full(valid_mask.shape, np.nan, dtype=np.float32)

        for y0 in range(0, lat_1d.size, ROW_CHUNK):
            y1 = min(lat_1d.size, y0 + ROW_CHUNK)
            rows_slice = slice(y0, y1)
            mask_chunk = valid_mask[rows_slice, :]
            if not np.any(mask_chunk):
                continue

            current_chunk = spherical_vza_chunk(
                lat_1d[rows_slice],
                lon_1d,
                current["subpoint_lon_deg"],
                current["semi_major_axis_m"],
                current["center_distance_m"],
            )
            audited_spherical_chunk = spherical_vza_chunk(
                lat_1d[rows_slice],
                lon_1d,
                audited.subpoint_lon_deg,
                audited.semi_major_axis_m,
                audited.center_distance_m,
            )
            ecef_chunk = ecef_vza_chunk(
                lat_1d[rows_slice],
                lon_1d,
                audited.subpoint_lon_deg,
                audited.semi_major_axis_m,
                audited.semi_minor_axis_m,
                audited.center_distance_m,
            )

            good_current = mask_chunk & np.isfinite(current_chunk) & np.isfinite(ecef_chunk)
            if np.any(good_current):
                piece = (current_chunk - ecef_chunk)[good_current].astype(np.float32)
                current_vs_ecef_pieces.append(piece)
                diff_tmp = np.full(mask_chunk.shape, np.nan, dtype=np.float32)
                diff_tmp[good_current] = (current_chunk - ecef_chunk)[good_current].astype(np.float32)
                diff_full[rows_slice, :] = np.where(np.isfinite(diff_tmp), diff_tmp, diff_full[rows_slice, :])

            good_audited = mask_chunk & np.isfinite(audited_spherical_chunk) & np.isfinite(ecef_chunk)
            if np.any(good_audited):
                audited_spherical_vs_ecef_pieces.append((audited_spherical_chunk - ecef_chunk)[good_audited].astype(np.float32))

            if audited.satellite == "FY4B":
                current_full[rows_slice, :] = current_chunk
                ecef_full[rows_slice, :] = ecef_chunk

        stats_current = summarize_diff(np.concatenate(current_vs_ecef_pieces) if current_vs_ecef_pieces else np.asarray([], dtype=np.float32))
        stats_audited = summarize_diff(np.concatenate(audited_spherical_vs_ecef_pieces) if audited_spherical_vs_ecef_pieces else np.asarray([], dtype=np.float32))
        rows.append(
            {
                "satellite": audited.satellite,
                "comparison": "current06b_spherical_minus_audited_ecef",
                "current_subpoint_lon_deg": current["subpoint_lon_deg"],
                "audited_subpoint_lon_deg": audited.subpoint_lon_deg,
                "audited_subpoint_source": audited.subpoint_source,
                **stats_current,
            }
        )
        rows.append(
            {
                "satellite": audited.satellite,
                "comparison": "audited_spherical_minus_audited_ecef",
                "current_subpoint_lon_deg": current["subpoint_lon_deg"],
                "audited_subpoint_lon_deg": audited.subpoint_lon_deg,
                "audited_subpoint_source": audited.subpoint_source,
                **stats_audited,
            }
        )
        save_difference_quicklook(
            diff_full,
            valid_mask & np.isfinite(diff_full),
            VZA_QUICKLOOK_DIR / f"{audited.satellite}_current06b_minus_audited_ecef.png",
            f"{audited.satellite}: current06b spherical - audited ECEF VZA",
        )
        if audited.satellite == "FY4B":
            fy4b_current_full = current_full
            fy4b_ecef_full = ecef_full

    if fy4b_current_full is not None and fy4b_ecef_full is not None:
        official, official_valid = load_fy4b_official_vza()
        for name, computed in [
            ("official_minus_current06b_spherical", fy4b_current_full),
            ("official_minus_audited_ecef", fy4b_ecef_full),
        ]:
            mask = official_valid & np.isfinite(computed)
            diff = official.astype(np.float32) - computed.astype(np.float32)
            rows.append(
                {
                    "satellite": "FY4B",
                    "comparison": name,
                    "current_subpoint_lon_deg": current_params["FY4B"]["subpoint_lon_deg"],
                    "audited_subpoint_lon_deg": next(p.subpoint_lon_deg for p in audited_params if p.satellite == "FY4B"),
                    "audited_subpoint_source": next(p.subpoint_source for p in audited_params if p.satellite == "FY4B"),
                    **summarize_diff(diff[mask].astype(np.float32) if np.any(mask) else np.asarray([], dtype=np.float32)),
                }
            )
            save_difference_quicklook(
                diff,
                mask,
                VZA_QUICKLOOK_DIR / f"FY4B_{name}.png",
                f"FY4B: {name}",
            )

    return pd.DataFrame(rows)


def build_vza_report(
    goes_df: pd.DataFrame,
    goes_compare_df: pd.DataFrame,
    him_df: pd.DataFrame,
    meteo_df: pd.DataFrame,
    audited_params: list[GeometryParameters],
    vza_df: pd.DataFrame,
) -> str:
    parameter_lines = []
    for p in audited_params:
        parameter_lines.append(
            f"- {p.satellite}: SSP/subpoint={p.subpoint_lon_deg:.6f} deg ({p.subpoint_source}); "
            f"a={p.semi_major_axis_m:.3f} m; b={p.semi_minor_axis_m:.3f} m; "
            f"center_distance={p.center_distance_m:.3f} m; height={p.height_above_ellipsoid_m:.3f} m"
        )

    current06b_rows = vza_df[vza_df["comparison"] == "current06b_spherical_minus_audited_ecef"]
    audited_rows = vza_df[vza_df["comparison"] == "audited_spherical_minus_audited_ecef"]
    fy4b_rows = vza_df[vza_df["comparison"].str.startswith("official_minus_", na=False)]

    consistent_goes = bool(goes_compare_df["consistent"].all())
    meteo_services = set(meteo_df["service"].tolist())
    meteo_ok = {"Meteosat-0deg", "Meteosat-IODC"}.issubset(meteo_services)
    fy4b_ok = False
    if not fy4b_rows.empty:
        row = fy4b_rows[fy4b_rows["comparison"] == "official_minus_audited_ecef"]
        if not row.empty:
            fy4b_ok = float(row.iloc[0]["mean_abs_diff_deg"]) <= 1.0 and float(row.iloc[0]["p95_abs_diff_deg"]) <= 2.0

    him_warning = not bool(him_df.iloc[0]["header_sufficient_for_full_disk_segment_completeness_check"])
    meteo_warning = True
    max_audited_mae = float(audited_rows["mean_abs_diff_deg"].max()) if not audited_rows.empty else math.inf
    max_audited_p95 = float(audited_rows["p95_abs_diff_deg"].max()) if not audited_rows.empty else math.inf

    if not consistent_goes or not meteo_ok or not fy4b_ok:
        status = "FAIL"
    elif him_warning or meteo_warning or max_audited_mae > 0.05 or max_audited_p95 > 0.15:
        status = "PASS_WITH_WARNINGS"
    else:
        status = "PASS"

    lines = [
        "# 06c Multi-satellite Geometry Metadata Audit Report",
        "",
        f"- 生成时间 UTC: {utc_now()}",
        f"- 目标时次: `{TARGET_TIME}`",
        f"- 判定: **{status}**",
        "",
        "## 1. 样本与参数来源",
        "",
    ]
    lines.extend(parameter_lines)
    lines.extend(
        [
            "",
            "## 2. GOES L1b 与 L2 投影一致性",
            "",
            f"- GOES L1b/L2 投影字段全部一致: `{consistent_goes}`",
            f"- GOES-16 L1b 样本: `{goes_df[goes_df['satellite'] == 'GOES-16'].iloc[0]['sample_path']}`",
            f"- GOES-18 L1b 样本: `{goes_df[goes_df['satellite'] == 'GOES-18'].iloc[0]['sample_path']}`",
            "",
            "## 3. Himawari HSD header 结论",
            "",
            f"- 单个 segment header 足以读取 sub_lon/SSP longitude/CFAC/LFAC/COFF/LOFF: `{bool(him_df.iloc[0]['header_sufficient_for_projection_parameters'])}`",
            f"- 单个 segment 不足以检查 full disk 全 segment 完整性: `{not bool(him_df.iloc[0]['header_sufficient_for_full_disk_segment_completeness_check'])}`",
            "",
            "## 4. Meteosat native reader 结论",
            "",
            f"- 已识别到的服务: `{', '.join(sorted(meteo_services))}`",
            "- MSG3 native 样本对应 Meteosat-10 / 0deg，SSP longitude=0.0。",
            "- MSG2 native 样本对应 Meteosat-9 / IODC，SSP longitude=45.5。",
            "",
            "## 5. VZA 方法对比",
            "",
        ]
    )
    for row in current06b_rows.itertuples():
        lines.append(
            f"- {row.satellite}: current06b spherical - audited ECEF, mean_abs={row.mean_abs_diff_deg:.6f} deg, "
            f"p95_abs={row.p95_abs_diff_deg:.6f} deg, max_abs={row.max_abs_diff_deg:.6f} deg"
        )
    lines.extend(["", "## 6. FY4B official VZA 对照", ""])
    if fy4b_rows.empty:
        lines.append("- FY4B official VZA 对照未生成。")
    else:
        for row in fy4b_rows.itertuples():
            lines.append(
                f"- {row.comparison}: mean_abs={row.mean_abs_diff_deg:.6f} deg, "
                f"p95_abs={row.p95_abs_diff_deg:.6f} deg, max_abs={row.max_abs_diff_deg:.6f} deg"
            )
    lines.extend(
        [
            "",
            "## 7. 判定解释",
            "",
            f"- GOES projection metadata 成功: `{consistent_goes}`",
            f"- Meteosat 0deg / IODC metadata 成功: `{meteo_ok}`",
            f"- FY4B official VZA 与 audited ECEF 对比在可接受范围内: `{fy4b_ok}`",
            "- GOES/FY4B/Meteosat 都已具备 WGS84/ECEF VZA 计算所需参数。",
            "- Himawari 当前已能从单个 HSD header 提取核心几何参数，但没有验证 full disk 全 segments 完整性。",
            "",
            "## 8. 结论",
            "",
        ]
    )
    if status == "FAIL":
        lines.append("- 当前结果不建议直接把 06b 的几何结论当作稳定基线。先修复 FAIL 项。")
    elif status == "PASS_WITH_WARNINGS":
        lines.append("- 可以把 06c 结果作为 07 的几何基线，但需要携带 Himawari segment 完整性未验证这一 warning。")
    else:
        lines.append("- 可以把 06c 结果作为 07 的几何基线。")
    lines.extend(
        [
            "",
            "## 输出文件",
            "",
            f"- `{FILE_INVENTORY_CSV}`",
            f"- `{GOES_AUDIT_CSV}`",
            f"- `{GOES_L1_L2_CSV}`",
            f"- `{HIMAWARI_AUDIT_CSV}`",
            f"- `{HIMAWARI_REPORT_MD}`",
            f"- `{METEOSAT_AUDIT_CSV}`",
            f"- `{METEOSAT_REPORT_MD}`",
            f"- `{VZA_COMPARISON_CSV}`",
            f"- `{VZA_REPORT_MD}`",
            f"- `{VZA_QUICKLOOK_DIR}`",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    GEOMETRY_ROOT.mkdir(parents=True, exist_ok=True)
    VZA_QUICKLOOK_DIR.mkdir(parents=True, exist_ok=True)

    inventory_df = build_file_inventory()
    inventory_df.to_csv(FILE_INVENTORY_CSV, index=False, encoding="utf-8-sig")

    manifest_df = read_geometry_manifest()
    goes_df, goes_compare_df = build_goes_audits(manifest_df)
    goes_df.to_csv(GOES_AUDIT_CSV, index=False, encoding="utf-8-sig")
    goes_compare_df.to_csv(GOES_L1_L2_CSV, index=False, encoding="utf-8-sig")

    him_df, him_report = build_himawari_audit(manifest_df)
    him_df.to_csv(HIMAWARI_AUDIT_CSV, index=False, encoding="utf-8-sig")
    HIMAWARI_REPORT_MD.write_text(him_report, encoding="utf-8")

    meteo_df, meteo_report = build_meteosat_audit()
    meteo_df.to_csv(METEOSAT_AUDIT_CSV, index=False, encoding="utf-8-sig")
    METEOSAT_REPORT_MD.write_text(meteo_report, encoding="utf-8")

    audited_params = all_audited_parameters(goes_df, him_df, meteo_df)
    vza_df = build_vza_comparison(audited_params)
    vza_df.to_csv(VZA_COMPARISON_CSV, index=False, encoding="utf-8-sig")

    report = build_vza_report(goes_df, goes_compare_df, him_df, meteo_df, audited_params, vza_df)
    VZA_REPORT_MD.write_text(report, encoding="utf-8")

    print("06c multi-satellite geometry metadata audit done")
    print(f"report={VZA_REPORT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
