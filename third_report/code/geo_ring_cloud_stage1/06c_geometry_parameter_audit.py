from __future__ import annotations

import importlib.util
import json
import math
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

import h5py
import netCDF4
import numpy as np
import pandas as pd

from geo_ring_cloud.pipeline_support import (
    REPORT_DIR,
    SCRIPT_DIR,
    ensure_dirs,
    find_himawari_r21_geometry_file,
    read_himawari_r21_geometry,
    read_mapping,
    utc_now,
)


STAGE_ROOT = Path(r"D:\AAAresearch_paper\geo_ring_cloud_stage1")
NATIVE_DIR = STAGE_ROOT / "standardized_native"
REPROJECT_DIR = STAGE_ROOT / "reprojected_grid"
OUT_DIR = STAGE_ROOT / "geometry_audit_06c"

TIME_TAG = "20240305_0000"
TARGET_TIME = "2024-03-05T00:00:00Z"

GEOMETRY_AUDIT_CSV = OUT_DIR / "satellite_geometry_parameter_audit.csv"
METHOD_COMPARISON_CSV = OUT_DIR / "vza_method_comparison.csv"
OFFICIAL_COMPARE_CSV = OUT_DIR / "official_vs_computed_vza_stats.csv"
REPORT_MD = REPORT_DIR / "06c_geometry_audit_report.md"

TARGET_SHAPE = (3600, 7200)
CURRENT_EARTH_RADIUS_M = 6378137.0
CURRENT_HEIGHT_ABOVE_ELLIPSOID_M = 35786023.0
DEFAULT_A_M = 6378137.0
DEFAULT_B_M = 6356752.31414
DEFAULT_CENTER_DISTANCE_M = 42164000.0
DEFAULT_HEIGHT_ABOVE_ELLIPSOID_M = DEFAULT_CENTER_DISTANCE_M - DEFAULT_A_M
HIMAWARI_FALLBACK_LON_DEG = 140.7
METEOSAT_0_FALLBACK_LON_DEG = 0.0
METEOSAT_IODC_FALLBACK_LON_DEG = 45.5
ROW_CHUNK = 120


def load_f06_module():
    path = SCRIPT_DIR / "06_fuse_best_source.py"
    spec = importlib.util.spec_from_file_location("stage1_f06", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load 06 module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


F06 = load_f06_module()


@dataclass
class GeometryParams:
    satellite: str
    reference_product: str
    source_file: str
    current_subpoint_lon_deg: float
    current_subpoint_source: str
    current_earth_radius_m: float
    current_earth_radius_source: str
    current_height_above_ellipsoid_m: float
    current_height_source: str
    recommended_subpoint_lon_deg: float
    recommended_subpoint_source: str
    recommended_a_m: float
    recommended_a_source: str
    recommended_b_m: float
    recommended_b_source: str
    recommended_center_distance_m: float
    recommended_center_distance_source: str
    recommended_height_above_ellipsoid_m: float
    recommended_height_source: str
    fallback_used: bool
    notes: str


def normalize_lon(lon: float | np.ndarray) -> float | np.ndarray:
    return ((np.asarray(lon) + 180.0) % 360.0) - 180.0


def build_target_lon_lat(target_grid: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    lon = target_grid["lon_min"] + target_grid["resolution_degree"] / 2.0 + np.arange(target_grid["lon_size"], dtype=np.float64) * target_grid["resolution_degree"]
    lat = target_grid["lat_min"] + target_grid["resolution_degree"] / 2.0 + np.arange(target_grid["lat_size"], dtype=np.float64) * target_grid["resolution_degree"]
    return lon, lat


def representative_native_path(satellite: str, product: str) -> Path:
    return NATIVE_DIR / f"{satellite}_{product}_{TIME_TAG}_native_cloud_v0.npz"


def representative_reprojected_path(satellite: str, product: str, variable: str) -> Path:
    return REPROJECT_DIR / satellite / f"{satellite}_{product}_{variable}_grid_{TIME_TAG}.npz"


def load_npz_meta_arrays(path: Path) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    with np.load(path, allow_pickle=False) as z:
        meta = json.loads(str(z["metadata_json"]))
        arrays = {name: np.asarray(z[name]) for name in z.files if not name.endswith("_json")}
    return meta, arrays


def extract_target_grid() -> dict[str, Any]:
    catalog = F06.load_catalog()
    return F06.target_grid_from_any(catalog)


def parse_collection_from_zip_xml(zip_path: Path) -> tuple[str | None, list[str]]:
    notes: list[str] = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                if not name.lower().endswith(".xml"):
                    continue
                root = ET.fromstring(zf.read(name))
                for elem in root.iter():
                    tag = elem.tag.lower()
                    text = (elem.text or "").strip()
                    if tag.endswith("parentidentifier") and text:
                        notes.append(f"{name}:parentIdentifier={text}")
                        return text, notes
                    if tag.endswith("identifier") and text.startswith("EO:EUM:DAT:"):
                        notes.append(f"{name}:identifier={text}")
                        return text, notes
    except Exception as exc:
        notes.append(f"zip_xml_read_failed:{type(exc).__name__}")
    return None, notes


def read_fy4b_params() -> GeometryParams:
    native_path = representative_native_path("FY4B", "CLM")
    geo_native_path = representative_native_path("FY4B", "GEO")
    meta, _ = load_npz_meta_arrays(native_path)
    geo_meta, _ = load_npz_meta_arrays(geo_native_path)
    l2_source = Path(str(meta["source_file"]))
    geo_source = Path(str(geo_meta["source_file"]))
    notes: list[str] = []

    recommended_lon = math.nan
    recommended_center = math.nan
    recommended_height = math.nan
    rec_lon_source = "missing"
    rec_center_source = "missing"
    rec_height_source = "missing"
    if l2_source.exists():
        with netCDF4.Dataset(l2_source) as ds:
            if "nominal_satellite_subpoint_lon" in ds.variables:
                recommended_lon = float(np.asarray(ds.variables["nominal_satellite_subpoint_lon"][:]).ravel()[0])
                rec_lon_source = "FY4B L2 source variable nominal_satellite_subpoint_lon via standardized metadata source_file"
            if "nominal_satellite_height" in ds.variables:
                recommended_center = float(np.asarray(ds.variables["nominal_satellite_height"][:]).ravel()[0])
                rec_center_source = "FY4B L2 source variable nominal_satellite_height via standardized metadata source_file"
                recommended_height = recommended_center - DEFAULT_A_M
                rec_height_source = "derived from FY4B L2 nominal_satellite_height minus semimajor_axis"
    if not np.isfinite(recommended_lon) or not np.isfinite(recommended_center):
        with h5py.File(geo_source, "r") as hdf:
            if not np.isfinite(recommended_lon) and "NOMCenterLon" in hdf.attrs:
                recommended_lon = float(np.asarray(hdf.attrs["NOMCenterLon"]).ravel()[0])
                rec_lon_source = "FY4B GEO HDF global_NOMCenterLon fallback"
            if not np.isfinite(recommended_center) and "NOMSatHeight" in hdf.attrs:
                recommended_center = float(np.asarray(hdf.attrs["NOMSatHeight"]).ravel()[0])
                rec_center_source = "FY4B GEO HDF global_NOMSatHeight fallback"
                recommended_height = recommended_center - float(np.asarray(hdf.attrs.get("Semimajor axis of ellipsoid", [DEFAULT_A_M])).ravel()[0])
                rec_height_source = "derived from FY4B GEO HDF NOMSatHeight minus Semimajor axis of ellipsoid"
    current_lon = float(F06.build_subpoint_longitude_map(F06.load_catalog())["FY4B"]["subpoint_lon_deg"])
    current_lon_source = "current 06b infer_subpoint_longitude (filename parse fallback path)"
    current_radius = float(F06.EARTH_RADIUS_KM * 1000.0)
    current_height = float(F06.GEO_ALTITUDE_KM * 1000.0)

    with h5py.File(geo_source, "r") as hdf:
        a = float(np.asarray(hdf.attrs.get("Semimajor axis of ellipsoid", [DEFAULT_A_M])).ravel()[0])
        b = float(np.asarray(hdf.attrs.get("Semiminor axis of ellipsoid", [DEFAULT_B_M])).ravel()[0])
    notes.append(f"FY4B GEO HDF semimajor={a:g} semiminor={b:g}")
    if not np.isfinite(recommended_lon):
        source_name = str(meta["source_file"])
        match = re.search(r"_N_DISK_(\d{4})([EW])_", source_name)
        if not match:
            raise RuntimeError("FY4B fallback filename parse failed")
        recommended_lon = float(match.group(1)) / 10.0
        if match.group(2) == "W":
            recommended_lon = -recommended_lon
        rec_lon_source = "FY4B filename parse fallback"
        notes.append("FY4B recommended subpoint longitude fell back to filename parse")
    if not np.isfinite(recommended_center):
        recommended_center = DEFAULT_CENTER_DISTANCE_M
        rec_center_source = "nominal geostationary fallback center distance"
        recommended_height = DEFAULT_HEIGHT_ABOVE_ELLIPSOID_M
        rec_height_source = "nominal geostationary fallback height above ellipsoid"
        notes.append("FY4B recommended height fell back to nominal geostationary constants")
    return GeometryParams(
        satellite="FY4B",
        reference_product="CLM/GEO",
        source_file=str(l2_source),
        current_subpoint_lon_deg=float(normalize_lon(current_lon)),
        current_subpoint_source=current_lon_source,
        current_earth_radius_m=current_radius,
        current_earth_radius_source="current 06b global constant EARTH_RADIUS_KM",
        current_height_above_ellipsoid_m=current_height,
        current_height_source="current 06b global constant GEO_ALTITUDE_KM",
        recommended_subpoint_lon_deg=float(normalize_lon(recommended_lon)),
        recommended_subpoint_source=rec_lon_source,
        recommended_a_m=a,
        recommended_a_source="FY4B GEO HDF global attribute Semimajor axis of ellipsoid",
        recommended_b_m=b,
        recommended_b_source="FY4B GEO HDF global attribute Semiminor axis of ellipsoid",
        recommended_center_distance_m=recommended_center,
        recommended_center_distance_source=rec_center_source,
        recommended_height_above_ellipsoid_m=recommended_height,
        recommended_height_source=rec_height_source,
        fallback_used=("fallback" in rec_lon_source.lower()) or ("fallback" in rec_height_source.lower()) or ("fallback" in rec_center_source.lower()),
        notes=" | ".join(notes),
    )


def read_goes_params(satellite: str) -> GeometryParams:
    native_path = representative_native_path(satellite, "ACMF")
    meta, _ = load_npz_meta_arrays(native_path)
    attrs = meta["reader_attrs"]["geostationary_projection_attrs"]
    lon0 = float(attrs["longitude_of_projection_origin"])
    h = float(attrs["perspective_point_height"])
    a = float(attrs.get("semi_major_axis", DEFAULT_A_M))
    b = float(attrs.get("semi_minor_axis", DEFAULT_B_M))
    current_lon = float(F06.build_subpoint_longitude_map(F06.load_catalog())[satellite]["subpoint_lon_deg"])
    return GeometryParams(
        satellite=satellite,
        reference_product="ACMF",
        source_file=str(meta["source_file"]),
        current_subpoint_lon_deg=float(normalize_lon(current_lon)),
        current_subpoint_source="current 06b GOES projection metadata path",
        current_earth_radius_m=float(F06.EARTH_RADIUS_KM * 1000.0),
        current_earth_radius_source="current 06b global constant EARTH_RADIUS_KM",
        current_height_above_ellipsoid_m=float(F06.GEO_ALTITUDE_KM * 1000.0),
        current_height_source="current 06b global constant GEO_ALTITUDE_KM",
        recommended_subpoint_lon_deg=float(normalize_lon(lon0)),
        recommended_subpoint_source="GOES goes_imager_projection.longitude_of_projection_origin from standardized native reader_attrs",
        recommended_a_m=a,
        recommended_a_source="GOES goes_imager_projection.semi_major_axis from standardized native reader_attrs",
        recommended_b_m=b,
        recommended_b_source="GOES goes_imager_projection.semi_minor_axis from standardized native reader_attrs",
        recommended_center_distance_m=a + h,
        recommended_center_distance_source="GOES semimajor_axis + perspective_point_height",
        recommended_height_above_ellipsoid_m=h,
        recommended_height_source="GOES goes_imager_projection.perspective_point_height from standardized native reader_attrs",
        fallback_used=False,
        notes=f"orbital_slot={meta['reader_attrs'].get('global_orbital_slot', 'unknown')}; sweep={attrs.get('sweep_angle_axis', 'x')}",
    )


def find_source_attr_case_insensitive(ds: netCDF4.Dataset, keys: list[str]) -> tuple[Any | None, str | None]:
    attrs = {name.lower(): name for name in ds.ncattrs()}
    for key in keys:
        if key.lower() in attrs:
            actual = attrs[key.lower()]
            return getattr(ds, actual), actual
    return None, None


def read_himawari_params() -> GeometryParams:
    native_path = representative_native_path("Himawari-9", "CMSK")
    meta, _ = load_npz_meta_arrays(native_path)
    source = Path(str(meta["source_file"]))
    current_lon = float(F06.build_subpoint_longitude_map(F06.load_catalog())["Himawari-9"]["subpoint_lon_deg"])
    recommended_lon = math.nan
    lon_source = "missing"
    recommended_a_m = DEFAULT_A_M
    recommended_a_source = "fallback WGS84 semimajor axis"
    recommended_b_m = DEFAULT_B_M
    recommended_b_source = "fallback WGS84 semiminor axis"
    recommended_center_distance_m = DEFAULT_CENTER_DISTANCE_M
    recommended_center_distance_source = "fallback nominal geostationary center distance"
    recommended_height_above_ellipsoid_m = DEFAULT_HEIGHT_ABOVE_ELLIPSOID_M
    recommended_height_source = "fallback nominal geostationary height above ellipsoid"
    fallback_used = True
    notes: list[str] = []
    aux_path, aux_info = find_himawari_r21_geometry_file(str(meta.get("nominal_time", TARGET_TIME)))
    if aux_path is not None:
        aux = read_himawari_r21_geometry(aux_path, read_mapping())
        gp = np.asarray(aux.attrs.get("r21_geometry_parameters", []), dtype=np.float64)
        if gp.size >= 8:
            recommended_lon = float(gp[0])
            lon_source = f"Himawari R21_FLDK geometry_parameters[0]=sub_lon from {aux_path.name}"
            recommended_center_distance_m = float(gp[5]) * 1000.0
            recommended_center_distance_source = f"Himawari R21_FLDK geometry_parameters[5]=Rs from {aux_path.name}"
            recommended_a_m = float(gp[6]) * 1000.0
            recommended_a_source = f"Himawari R21_FLDK geometry_parameters[6]=req from {aux_path.name}"
            recommended_b_m = float(gp[7]) * 1000.0
            recommended_b_source = f"Himawari R21_FLDK geometry_parameters[7]=rpol from {aux_path.name}"
            recommended_height_above_ellipsoid_m = recommended_center_distance_m - recommended_a_m
            recommended_height_source = "derived as Rs - req from Himawari R21_FLDK geometry_parameters"
            fallback_used = False
            notes.append(
                f"Himawari R21_FLDK auxiliary geometry selected: {aux_path.name}; "
                f"selected_time={aux_info.get('selected_time')}; dt_minutes={aux_info.get('dt_minutes')}"
            )
    if source.exists():
        with netCDF4.Dataset(source) as ds:
            value, attr_name = find_source_attr_case_insensitive(
                ds,
                [
                    "nominal_satellite_subpoint_lon",
                    "satellite_subpoint_lon",
                    "subsatellite_longitude",
                    "sub_satellite_longitude",
                    "orbital_slot_longitude",
                    "longitude_of_projection_origin",
                ],
            )
            if value is not None:
                recommended_lon = float(np.asarray(value).ravel()[0])
                lon_source = f"Himawari source NetCDF global attribute {attr_name}"
            else:
                notes.append("Himawari source NetCDF and standardized metadata did not expose explicit subpoint longitude")
    if not np.isfinite(recommended_lon):
        recommended_lon = HIMAWARI_FALLBACK_LON_DEG
        lon_source = "fallback fixed Himawari-9 operational slot 140.7E"
        fallback_used = True
    return GeometryParams(
        satellite="Himawari-9",
        reference_product="CMSK",
        source_file=str(source),
        current_subpoint_lon_deg=float(normalize_lon(current_lon)),
        current_subpoint_source="current 06b estimated_from_native_latlon_center_pixel",
        current_earth_radius_m=float(F06.EARTH_RADIUS_KM * 1000.0),
        current_earth_radius_source="current 06b global constant EARTH_RADIUS_KM",
        current_height_above_ellipsoid_m=float(F06.GEO_ALTITUDE_KM * 1000.0),
        current_height_source="current 06b global constant GEO_ALTITUDE_KM",
        recommended_subpoint_lon_deg=float(normalize_lon(recommended_lon)),
        recommended_subpoint_source=lon_source,
        recommended_a_m=recommended_a_m,
        recommended_a_source=recommended_a_source,
        recommended_b_m=recommended_b_m,
        recommended_b_source=recommended_b_source,
        recommended_center_distance_m=recommended_center_distance_m,
        recommended_center_distance_source=recommended_center_distance_source,
        recommended_height_above_ellipsoid_m=recommended_height_above_ellipsoid_m,
        recommended_height_source=recommended_height_source,
        fallback_used=fallback_used,
        notes=" | ".join(notes),
    )


def read_meteosat_params(satellite: str, fallback_lon_deg: float) -> GeometryParams:
    native_path = representative_native_path(satellite, "CLM")
    meta, _ = load_npz_meta_arrays(native_path)
    source = Path(str(meta["source_file"]))
    current_lon = float(F06.build_subpoint_longitude_map(F06.load_catalog())[satellite]["subpoint_lon_deg"])
    collection_id, xml_notes = parse_collection_from_zip_xml(source)
    recommended_lon = fallback_lon_deg
    lon_source = f"fallback service longitude {fallback_lon_deg:g}E from Meteosat service designation"
    notes = list(xml_notes)
    if satellite == "Meteosat-0deg":
        notes.append("Meteosat-0deg standardized metadata and ZIP XML do not expose explicit subpoint longitude; using 0E service longitude fallback")
    else:
        notes.append("Meteosat-IODC standardized metadata and ZIP XML do not expose explicit subpoint longitude; using 45.5E service longitude fallback")
    if collection_id:
        notes.append(f"collection_id={collection_id}")
    return GeometryParams(
        satellite=satellite,
        reference_product="CLM",
        source_file=str(source),
        current_subpoint_lon_deg=float(normalize_lon(current_lon)),
        current_subpoint_source="current 06b estimated_from_native_latlon_center_pixel",
        current_earth_radius_m=float(F06.EARTH_RADIUS_KM * 1000.0),
        current_earth_radius_source="current 06b global constant EARTH_RADIUS_KM",
        current_height_above_ellipsoid_m=float(F06.GEO_ALTITUDE_KM * 1000.0),
        current_height_source="current 06b global constant GEO_ALTITUDE_KM",
        recommended_subpoint_lon_deg=float(normalize_lon(recommended_lon)),
        recommended_subpoint_source=lon_source,
        recommended_a_m=DEFAULT_A_M,
        recommended_a_source="fallback WGS84 semimajor axis",
        recommended_b_m=DEFAULT_B_M,
        recommended_b_source="fallback WGS84 semiminor axis",
        recommended_center_distance_m=DEFAULT_CENTER_DISTANCE_M,
        recommended_center_distance_source="fallback nominal geostationary center distance",
        recommended_height_above_ellipsoid_m=DEFAULT_HEIGHT_ABOVE_ELLIPSOID_M,
        recommended_height_source="fallback nominal geostationary height above ellipsoid",
        fallback_used=True,
        notes=" | ".join(notes),
    )


def gather_geometry_params() -> list[GeometryParams]:
    rows = [
        read_fy4b_params(),
        read_goes_params("GOES-16"),
        read_goes_params("GOES-18"),
        read_himawari_params(),
        read_meteosat_params("Meteosat-0deg", METEOSAT_0_FALLBACK_LON_DEG),
        read_meteosat_params("Meteosat-IODC", METEOSAT_IODC_FALLBACK_LON_DEG),
    ]
    return rows


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


def spherical_vza_chunk(
    lat_1d_deg: np.ndarray,
    lon_1d_deg: np.ndarray,
    subpoint_lon_deg: float,
    earth_radius_m: float,
    center_distance_m: float,
) -> np.ndarray:
    lat_rad = np.deg2rad(lat_1d_deg[:, None])
    dlon = np.deg2rad(normalize_lon(lon_1d_deg[None, :] - subpoint_lon_deg))
    cos_psi = np.cos(lat_rad) * np.cos(dlon)
    visible = cos_psi > (earth_radius_m / center_distance_m)
    numer = center_distance_m * cos_psi - earth_radius_m
    denom = np.sqrt(
        center_distance_m * center_distance_m
        + earth_radius_m * earth_radius_m
        - 2.0 * center_distance_m * earth_radius_m * cos_psi
    )
    cos_vza = np.clip(numer / denom, -1.0, 1.0)
    out = np.full((lat_1d_deg.size, lon_1d_deg.size), np.nan, dtype=np.float32)
    out[visible] = np.rad2deg(np.arccos(cos_vza[visible])).astype(np.float32)
    return out


def ecef_vza_chunk(
    lat_1d_deg: np.ndarray,
    lon_1d_deg: np.ndarray,
    subpoint_lon_deg: float,
    a_m: float,
    b_m: float,
    center_distance_m: float,
) -> np.ndarray:
    lat_rad = np.deg2rad(lat_1d_deg[:, None])
    lon_rad = np.deg2rad(lon_1d_deg[None, :])
    e2 = 1.0 - (b_m * b_m) / (a_m * a_m)
    sin_lat = np.sin(lat_rad)
    cos_lat = np.cos(lat_rad)
    cos_lon = np.cos(lon_rad)
    sin_lon = np.sin(lon_rad)
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
