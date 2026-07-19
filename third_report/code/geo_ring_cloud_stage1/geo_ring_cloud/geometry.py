"""Canonical GEO geometry parameters and view-zenith algorithms."""

from __future__ import annotations

import json
import math
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

import h5py
import netCDF4
import numpy as np

from . import fusion_support as F06
from .adapters.cloud_products import (
    find_himawari_r21_geometry_file,
    read_himawari_r21_geometry,
    read_mapping,
)
from .pipeline_layout import STAGE_ROOT


COMPONENT_ROLE = "geometry"

NATIVE_DIR = STAGE_ROOT / "standardized_native"
REPROJECT_DIR = STAGE_ROOT / "reprojected_grid"
TIME_TAG = "20240305_0000"
TARGET_TIME = "2024-03-05T00:00:00Z"
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
