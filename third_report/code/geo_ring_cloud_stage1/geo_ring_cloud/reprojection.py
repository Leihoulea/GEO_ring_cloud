"""Reusable native geolocation and nearest-neighbor reprojection APIs."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import netCDF4
import numpy as np
from pyproj import CRS, Transformer
from scipy.spatial import cKDTree

from .pipeline_layout import STAGE_ROOT


COMPONENT_ROLE = "reprojection"

NATIVE_DIR = STAGE_ROOT / "standardized_native"
TARGET_TIME = os.environ.get("GEO_RING_TARGET_TIME", "2024-03-05T00:00:00Z")
TIME_TAG = os.environ.get("GEO_RING_TIME_TAG", TARGET_TIME[0:13].replace("-", "").replace("T", "_"))
GRID_RES = 0.05
LAT_MIN, LAT_MAX = -90.0, 90.0
LON_MIN, LON_MAX = -180.0, 180.0
MAX_DISTANCE_DEG = 0.15
SUSPECT_CODES = {
    "cloud_mask": {-128, 126, 127, 255},
    "cloud_type": {-128, 126, 127, 255},
    "cloud_phase": {-128, 126, 127, 255},
    "quality_flag_raw": {-128, 126, 127, 255, 32767, 65535},
    "quality_flag_standard": {-128, 126, 127, 255},
    "valid_mask": {-128, 126, 127, 255},
}

def make_target_grid() -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    lon_size = int(round((LON_MAX - LON_MIN) / GRID_RES))
    lat_size = int(round((LAT_MAX - LAT_MIN) / GRID_RES))
    lon = LON_MIN + GRID_RES / 2.0 + np.arange(lon_size, dtype=np.float64) * GRID_RES
    lat = LAT_MIN + GRID_RES / 2.0 + np.arange(lat_size, dtype=np.float64) * GRID_RES
    grid = {
        "crs": "EPSG:4326 WGS84 lon-lat",
        "lon_min": LON_MIN,
        "lon_max": LON_MAX,
        "lat_min": LAT_MIN,
        "lat_max": LAT_MAX,
        "resolution_degree": GRID_RES,
        "lon_size": int(lon_size),
        "lat_size": int(lat_size),
        "lon_centers_first_last": [float(lon[0]), float(lon[-1])],
        "lat_centers_first_last": [float(lat[0]), float(lat[-1])],
        "shape": [int(lat.size), int(lon.size)],
        "longitude_convention": "-180_to_180",
    }
    return lon, lat, grid

def load_npz(path: Path) -> dict[str, Any]:
    with np.load(path, allow_pickle=False) as z:
        meta = json.loads(str(z["metadata_json"]))
        avail = json.loads(str(z["variable_availability_json"]))
        arrays = {name: np.asarray(z[name]) for name in z.files if not name.endswith("_json") and name != "variable_availability"}
    return {"meta": meta, "availability": avail, "arrays": arrays, "path": path}

def available(bundle: dict[str, Any], variable: str) -> bool:
    return bool(bundle["availability"].get(f"has_{variable}", False)) and variable in bundle["arrays"]

def normalize_longitude(lon: np.ndarray) -> np.ndarray:
    values = np.asarray(lon, dtype=np.float32)
    out = np.full(values.shape, np.nan, dtype=np.float32)
    finite = np.isfinite(values)
    out[finite] = ((values[finite] + 180.0) % 360.0) - 180.0
    out[finite & np.isclose(out, -180.0)] = 180.0
    return out

def valid_for_variable(variable: str, arr: np.ndarray) -> np.ndarray:
    a = np.asarray(arr)
    if a.ndim < 2:
        return np.zeros(a.shape, dtype=bool)
    if a.dtype.kind == "f":
        mask = np.isfinite(a)
        for code in SUSPECT_CODES.get(variable, set()):
            mask &= ~np.isclose(a, float(code))
        return mask
    if a.dtype.kind in "iu":
        mask = np.ones(a.shape, dtype=bool)
        for code in SUSPECT_CODES.get(variable, set()):
            mask &= a != code
        return mask
    return np.ones(a.shape, dtype=bool)

def read_fy4b_lonlat_from_source(bundle: dict[str, Any], shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray, list[str]]:
    source = Path(str(bundle["meta"].get("source_file", "")))
    notes = ["FY4B lon/lat derived from source L2 fixed-grid coordinates using legacy geostationary extent; GEO angles are not used as lon/lat."]
    if (not source.exists()) or source.suffix.lower() not in {".nc", ".nc4"}:
        clm_npz = NATIVE_DIR / f"FY4B_CLM_{TIME_TAG}_native_cloud_v0.npz"
        if not clm_npz.exists():
            raise RuntimeError(f"FY4B projection source unavailable; original={source}")
        clm_bundle = load_npz(clm_npz)
        source = Path(str(clm_bundle["meta"].get("source_file", "")))
        notes.append(f"FY4B product source lacks projection variables; reused CLM same-grid source {source.name}")
    if not source.exists():
        raise RuntimeError(f"FY4B source file missing: {source}")
    with netCDF4.Dataset(source) as ds:
        sub_lon = float(np.asarray(ds.variables["nominal_satellite_subpoint_lon"][:]).ravel()[0])
        sat_height_center = float(np.asarray(ds.variables["nominal_satellite_height"][:]).ravel()[0])
    a = 6378137.0
    b = 6356752.31414
    h = sat_height_center - a
    # FY4B L2 stores x/y as pixel indices. Use the same 4-km full disk approximation
    # used by the earlier L1 standardized builder for first-pass geolocation.
    ny, nx = shape
    x_extent = 5.5e6
    y_extent = 5.5e6
    px = (-x_extent + (np.arange(nx, dtype=np.float64) + 0.5) * (2 * x_extent / nx)).astype(np.float64)
    py = (y_extent - (np.arange(ny, dtype=np.float64) + 0.5) * (2 * y_extent / ny)).astype(np.float64)
    xx, yy = np.meshgrid(px, py)
    geos = CRS.from_proj4(f"+proj=geos +h={h} +lon_0={sub_lon} +a={a} +b={b} +units=m +sweep=x +no_defs")
    geo = CRS.from_proj4("+proj=longlat +datum=WGS84 +no_defs")
    lon, lat = Transformer.from_crs(geos, geo, always_xy=True).transform(xx, yy)
    lon = normalize_longitude(lon)
    lat = np.asarray(lat, dtype=np.float32)
    bad = ~np.isfinite(lon) | ~np.isfinite(lat) | (lat < -90) | (lat > 90)
    lon[bad] = np.nan
    lat[bad] = np.nan
    return lon, lat, notes

def goes_lonlat(bundle: dict[str, Any], shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray, list[str]]:
    arrays = bundle["arrays"]
    meta = bundle["meta"]
    if not available(bundle, "projection_x") or not available(bundle, "projection_y"):
        raise RuntimeError("GOES projection_x/projection_y unavailable")
    x = np.asarray(arrays["projection_x"], dtype=np.float64)
    y = np.asarray(arrays["projection_y"], dtype=np.float64)
    if x.size != shape[1] or y.size != shape[0]:
        raise RuntimeError(f"GOES x/y size {x.size}/{y.size} does not match data shape {shape}")
    attrs = meta.get("reader_attrs", {}).get("geostationary_projection_attrs", {})
    h = float(attrs["perspective_point_height"])
    lon0 = float(attrs["longitude_of_projection_origin"])
    a = float(attrs.get("semi_major_axis", 6378137.0))
    b = float(attrs.get("semi_minor_axis", 6356752.31414))
    sweep = str(attrs.get("sweep_angle_axis", "x"))
    xx, yy = np.meshgrid(x * h, y * h)
    geos = CRS.from_proj4(f"+proj=geos +h={h} +lon_0={lon0} +a={a} +b={b} +units=m +sweep={sweep} +no_defs")
    lon, lat = Transformer.from_crs(geos, CRS.from_proj4("+proj=longlat +datum=WGS84 +no_defs"), always_xy=True).transform(xx, yy)
    lon = normalize_longitude(lon)
    lat = np.asarray(lat, dtype=np.float32)
    bad = ~np.isfinite(lon) | ~np.isfinite(lat) | (lat < -90) | (lat > 90)
    lon[bad] = np.nan
    lat[bad] = np.nan
    return lon, lat, ["GOES lon/lat derived from projection_x/projection_y and goes_imager_projection; x/y are scan angles multiplied by perspective_point_height."]

def claas3_lonlat(bundle: dict[str, Any], shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray, list[str]]:
    arrays = bundle["arrays"]
    meta = bundle["meta"]
    x = np.asarray(arrays.get("projection_x"), dtype=np.float64)
    y = np.asarray(arrays.get("projection_y"), dtype=np.float64)
    if x.size != shape[1] or y.size != shape[0]:
        raise RuntimeError(f"CLAAS-3 x/y size {x.size}/{y.size} does not match data shape {shape}")
    attrs = meta.get("geostationary_projection_attrs", {})
    required = {"perspective_point_height", "longitude_of_projection_origin"}
    if not required.issubset(attrs):
        raise RuntimeError("CLAAS-3 CF geostationary projection attributes are incomplete")
    h = float(attrs["perspective_point_height"])
    lon0 = float(attrs["longitude_of_projection_origin"])
    a = float(attrs.get("semi_major_axis", 6378137.0))
    b = float(attrs.get("semi_minor_axis", 6356752.31414))
    sweep = str(attrs.get("sweep_angle_axis", "y"))
    xx, yy = np.meshgrid(x * h, y * h)
    geos = CRS.from_proj4(f"+proj=geos +h={h} +lon_0={lon0} +a={a} +b={b} +units=m +sweep={sweep} +no_defs")
    geo = CRS.from_proj4("+proj=longlat +datum=WGS84 +no_defs")
    lon, lat = Transformer.from_crs(geos, geo, always_xy=True).transform(xx, yy)
    lon = normalize_longitude(lon)
    lat = np.asarray(lat, dtype=np.float32)
    bad = ~np.isfinite(lon) | ~np.isfinite(lat) | (lat < -90) | (lat > 90)
    lon[bad] = np.nan
    lat[bad] = np.nan
    if abs(float(np.nanmedian(lon))) > 1.0:
        raise RuntimeError(f"CLAAS-3 projection center failed 0-degree check: median longitude={np.nanmedian(lon):.3f}")
    return lon, lat, ["CLAAS-3 lon/lat derived from CF projection plus x/y scan angles; no array-index geolocation was used."]

def native_lonlat(bundle: dict[str, Any], variable_shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray, list[str]]:
    if not available(bundle, "latitude") or not available(bundle, "longitude"):
        raise RuntimeError("latitude/longitude unavailable")
    lat = np.asarray(bundle["arrays"]["latitude"], dtype=np.float32)
    lon = np.asarray(bundle["arrays"]["longitude"], dtype=np.float32)
    if lat.shape != variable_shape or lon.shape != variable_shape:
        raise RuntimeError(f"lat/lon shape {lat.shape}/{lon.shape} does not match variable shape {variable_shape}")
    sat = str(bundle["meta"].get("satellite_group", ""))
    notes = ["native latitude/longitude used"]
    if sat.startswith("Meteosat"):
        old_min = float(np.nanmin(lon))
        old_max = float(np.nanmax(lon))
        lon = normalize_longitude(lon)
        notes.append(f"Meteosat longitude normalized from {old_min:g}..{old_max:g} to -180..180")
    return lon, lat, notes

def geolocate(bundle: dict[str, Any], variable_shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray, list[str]]:
    sat = str(bundle["meta"].get("satellite_group", ""))
    if sat == "FY4B":
        if available(bundle, "latitude") and available(bundle, "longitude"):
            return native_lonlat(bundle, variable_shape)
        return read_fy4b_lonlat_from_source(bundle, variable_shape)
    if sat.startswith("GOES"):
        return goes_lonlat(bundle, variable_shape)
    if sat == "CLAAS3-0deg":
        return claas3_lonlat(bundle, variable_shape)
    return native_lonlat(bundle, variable_shape)

def build_tree(lon: np.ndarray, lat: np.ndarray, source_valid: np.ndarray) -> tuple[cKDTree, np.ndarray, np.ndarray, list[str]]:
    finite = np.isfinite(lon) & np.isfinite(lat) & source_valid
    notes: list[str] = []
    stride = 1
    if finite.size > 18_000_000:
        # Prototype memory guard. Still nearest-neighbor, but with a documented 2x source stride
        # for very dense 2-km products.
        stride = 2
        notes.append("source geolocation decimated by stride=2 for prototype memory control; nearest-neighbor only")
        finite_s = finite[::stride, ::stride]
        lon_s = lon[::stride, ::stride]
        lat_s = lat[::stride, ::stride]
    else:
        finite_s = finite
        lon_s = lon
        lat_s = lat
    yy, xx = np.nonzero(finite_s)
    if yy.size == 0:
        raise RuntimeError("no finite valid geolocation points")
    points = np.column_stack([lon_s[yy, xx].astype(np.float64), lat_s[yy, xx].astype(np.float64)])
    tree = cKDTree(points)
    src_y = yy.astype(np.int32) * stride
    src_x = xx.astype(np.int32) * stride
    return tree, src_y, src_x, notes

def query_reproject(
    tree: cKDTree,
    src_y: np.ndarray,
    src_x: np.ndarray,
    data: np.ndarray,
    target_lon: np.ndarray,
    target_lat: np.ndarray,
    out_dtype: np.dtype,
    fill_value: float | int,
) -> tuple[np.ndarray, np.ndarray]:
    out = np.full((target_lat.size, target_lon.size), fill_value, dtype=out_dtype)
    out_valid = np.zeros(out.shape, dtype=np.uint8)
    chunk_rows = 160
    for start in range(0, target_lat.size, chunk_rows):
        stop = min(target_lat.size, start + chunk_rows)
        lat_chunk = target_lat[start:stop]
        lon2d, lat2d = np.meshgrid(target_lon, lat_chunk)
        pts = np.column_stack([lon2d.ravel().astype(np.float64), lat2d.ravel().astype(np.float64)])
        dist, idx = tree.query(pts, k=1, distance_upper_bound=MAX_DISTANCE_DEG, workers=-1)
        ok = np.isfinite(dist) & (idx < src_y.size)
        if np.any(ok):
            flat_out = out[start:stop].reshape(-1)
            flat_valid = out_valid[start:stop].reshape(-1)
            sy = src_y[idx[ok]]
            sx = src_x[idx[ok]]
            flat_out[np.nonzero(ok)[0]] = data[sy, sx].astype(out_dtype, copy=False)
            flat_valid[np.nonzero(ok)[0]] = 1
    return out, out_valid
