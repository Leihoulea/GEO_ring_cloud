from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import matplotlib
import netCDF4
import numpy as np
import pandas as pd
import yaml

from geo_ring_cloud import reprojection as F05
from geo_ring_cloud.sources import SOURCE_BY_KEY, tie_order, validate_profile
from geo_ring_cloud.diagnostics.summary import finite_stats
from geo_ring_cloud.adapters.cloud_products import find_himawari_r21_geometry_file, read_mapping
from geo_ring_cloud.lineage import utc_now
from geo_ring_cloud.paths import CODE_ROOT
from geo_ring_cloud.pipeline_layout import REPORT_DIR, SCRIPT_DIR, STAGE_ROOT

matplotlib.use("Agg")
import matplotlib.pyplot as plt

STAGE_ID = "stage_06e"

REPROJECT_DIR = STAGE_ROOT / "reprojected_grid"
FUSED_DIR = STAGE_ROOT / "fused_best_source"
SYNC_DIR = STAGE_ROOT / "geometry_angle_sync_06e"
ANGLE_GRID_DIR = SYNC_DIR / "angle_layers_target_grid"
ANGLE_QUICKLOOK_DIR = SYNC_DIR / "quicklooks_angle"
REPORT_MD = REPORT_DIR / "06e_full_geometry_angle_source_sync_patch_report.md"

SCRIPT_PATH = Path(__file__).resolve()
CODE_DIR = CODE_ROOT
TIME_TAG = "20240305_0000"
TARGET_TIME = "2024-03-05T00:00:00Z"
TARGET_TS = pd.Timestamp(TARGET_TIME)
TARGET_SHAPE = (3600, 7200)
TIE_ORDER = tie_order(validate_profile(os.environ.get("GEO_RING_SOURCE_PROFILE", "operational_baseline")))
SAT_SUBPOINT = {source: SOURCE_BY_KEY[source].service_longitude_deg for source in TIE_ORDER}
ANGLE_NAMES = [
    "sensor_zenith_angle",
    "sensor_azimuth_angle",
    "solar_zenith_angle",
    "solar_azimuth_angle",
    "relative_azimuth_angle",
    "sun_glint_angle",
    "day_night_flag",
]
EARTH_RADIUS_KM = 6378.137
GEO_ALTITUDE_KM = 35786.023
SATELLITE_RADIUS_KM = EARTH_RADIUS_KM + GEO_ALTITUDE_KM


def ensure_output_dirs() -> None:
    for path in [SYNC_DIR, ANGLE_GRID_DIR, ANGLE_QUICKLOOK_DIR, REPORT_DIR]:
        path.mkdir(parents=True, exist_ok=True)


def run_stage(script_name: str) -> None:
    cmd = [sys.executable, str(CODE_DIR / script_name)]
    result = subprocess.run(cmd, cwd=str(CODE_DIR), text=True, capture_output=True)
    (SYNC_DIR / f"{Path(script_name).stem}.stdout.log").write_text(result.stdout, encoding="utf-8", errors="ignore")
    (SYNC_DIR / f"{Path(script_name).stem}.stderr.log").write_text(result.stderr, encoding="utf-8", errors="ignore")
    if result.returncode != 0:
        raise RuntimeError(f"{script_name} failed with code {result.returncode}; see logs in {SYNC_DIR}")


def load_npz(path: Path) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    with np.load(path, allow_pickle=False) as z:
        meta = json.loads(str(z["metadata_json"])) if "metadata_json" in z.files else {}
        arrays = {name: np.asarray(z[name]) for name in z.files if not name.endswith("_json")}
    return meta, arrays


def target_grid() -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    path = REPROJECT_DIR / "target_grid_definition.json"
    if path.exists():
        grid = json.loads(path.read_text(encoding="utf-8"))
        lon = grid["lon_min"] + grid["resolution_degree"] / 2.0 + np.arange(grid["lon_size"], dtype=np.float32) * grid["resolution_degree"]
        lat = grid["lat_min"] + grid["resolution_degree"] / 2.0 + np.arange(grid["lat_size"], dtype=np.float32) * grid["resolution_degree"]
        return lon, lat, grid
    lon, lat, grid = F05.make_target_grid()
    return lon.astype(np.float32), lat.astype(np.float32), grid


def normalize_lon(lon: float | np.ndarray) -> float | np.ndarray:
    return ((np.asarray(lon) + 180.0) % 360.0) - 180.0


def save_angle_layer(
    satellite: str,
    angle_name: str,
    data: np.ndarray,
    valid_mask: np.ndarray,
    source_level: str,
    source_file: str,
    target_grid_meta: dict[str, Any],
    notes: str,
) -> Path:
    data = np.asarray(data)
    valid_mask = np.asarray(valid_mask).astype(np.uint8)
    meta = {
        "generated_utc": utc_now(),
        "nominal_time": TARGET_TIME,
        "target_time": TARGET_TIME,
        "source_satellite": satellite,
        "source_product": "ANGLE",
        "source_file": source_file,
        "variable": angle_name,
        "angle_source_level": source_level,
        "source_level": source_level,
        "resampling_method": "nearest" if source_level in {"OFFICIAL_PIXEL_ANGLE", "OFFICIAL_GRIDDED_ANGLE"} else "computed_on_target_grid",
        "target_grid": target_grid_meta,
        "azimuth_convention": "0 north, 90 east, clockwise; from target pixel toward object",
        "notes": notes,
    }
    for root in [ANGLE_GRID_DIR / satellite, REPROJECT_DIR / satellite]:
        root.mkdir(parents=True, exist_ok=True)
        out = root / f"{satellite}_ANGLE_{angle_name}_grid_{TIME_TAG}.npz"
        np.savez_compressed(out, data=data, valid_mask=valid_mask, metadata_json=np.asarray(json.dumps(meta, ensure_ascii=False, default=str)))
    return REPROJECT_DIR / satellite / f"{satellite}_ANGLE_{angle_name}_grid_{TIME_TAG}.npz"


def quicklook(path: Path, data: np.ndarray, valid: np.ndarray, title: str) -> None:
    arr = np.asarray(data, dtype=np.float32)
    mask = np.asarray(valid).astype(bool)
    plot = arr.copy()
    plot[~mask] = np.nan
    stride = max(1, int(math.ceil(math.sqrt(plot.size / 1_200_000))))
    plot = plot[::stride, ::stride]
    plt.figure(figsize=(11, 4.8), dpi=150)
    if "day_night" in title:
        im = plt.imshow(plot, origin="lower", extent=[-180, 180, -90, 90], interpolation="nearest", cmap="viridis", vmin=0, vmax=2)
    else:
        finite = np.isfinite(plot)
        vmin = vmax = None
        if finite.any():
            vmin, vmax = np.nanpercentile(plot[finite], [2, 98])
            if not np.isfinite(vmin) or not np.isfinite(vmax) or vmin == vmax:
                vmin = vmax = None
        im = plt.imshow(plot, origin="lower", extent=[-180, 180, -90, 90], interpolation="nearest", cmap="viridis", vmin=vmin, vmax=vmax)
    plt.title(title, fontsize=10)
    plt.xlabel("Longitude")
    plt.ylabel("Latitude")
    plt.colorbar(im, shrink=0.78)
    plt.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(path)
    plt.close()


def angle_stats(data: np.ndarray, valid: np.ndarray) -> dict[str, Any]:
    arr = np.asarray(data, dtype=np.float32)
    mask = np.asarray(valid).astype(bool) & np.isfinite(arr)
    return finite_stats(np.where(mask, arr, np.nan))


def relative_azimuth(saa: np.ndarray, vaa: np.ndarray) -> np.ndarray:
    diff = np.abs((np.asarray(saa, dtype=np.float32) - np.asarray(vaa, dtype=np.float32) + 180.0) % 360.0 - 180.0)
    return diff.astype(np.float32)


def glint_angle(sza: np.ndarray, vza: np.ndarray, raa: np.ndarray) -> np.ndarray:
    sza_r = np.deg2rad(np.asarray(sza, dtype=np.float64))
    vza_r = np.deg2rad(np.asarray(vza, dtype=np.float64))
    raa_r = np.deg2rad(np.asarray(raa, dtype=np.float64))
    cos_g = np.cos(sza_r) * np.cos(vza_r) + np.sin(sza_r) * np.sin(vza_r) * np.cos(raa_r)
    return np.rad2deg(np.arccos(np.clip(cos_g, -1.0, 1.0))).astype(np.float32)


def day_night_from_sza(sza: np.ndarray) -> np.ndarray:
    out = np.full(np.asarray(sza).shape, -1, dtype=np.int8)
    finite = np.isfinite(sza)
    out[finite & (sza < 85.0)] = 0
    out[finite & (sza >= 85.0) & (sza <= 95.0)] = 1
    out[finite & (sza > 95.0)] = 2
    return out


def solar_angles(lon2d: np.ndarray, lat2d: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    ts = TARGET_TS
    doy = int(ts.dayofyear)
    hour = ts.hour + ts.minute / 60.0 + ts.second / 3600.0
    gamma = 2.0 * np.pi / 365.0 * (doy - 1 + (hour - 12.0) / 24.0)
    decl = (
        0.006918
        - 0.399912 * np.cos(gamma)
        + 0.070257 * np.sin(gamma)
        - 0.006758 * np.cos(2 * gamma)
        + 0.000907 * np.sin(2 * gamma)
        - 0.002697 * np.cos(3 * gamma)
        + 0.00148 * np.sin(3 * gamma)
    )
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * np.cos(gamma)
        - 0.032077 * np.sin(gamma)
        - 0.014615 * np.cos(2 * gamma)
        - 0.040849 * np.sin(2 * gamma)
    )
    true_solar_min = (hour * 60.0 + eqtime + 4.0 * lon2d) % 1440.0
    ha = np.deg2rad(true_solar_min / 4.0 - 180.0)
    lat_r = np.deg2rad(lat2d)
    cos_zen = np.sin(lat_r) * np.sin(decl) + np.cos(lat_r) * np.cos(decl) * np.cos(ha)
    sza = np.rad2deg(np.arccos(np.clip(cos_zen, -1.0, 1.0))).astype(np.float32)
    az = (np.rad2deg(np.arctan2(np.sin(ha), np.cos(ha) * np.sin(lat_r) - np.tan(decl) * np.cos(lat_r))) + 180.0) % 360.0
    return sza, az.astype(np.float32)


def nav_vza_vaa(lon2d: np.ndarray, lat2d: np.ndarray, sub_lon: float) -> tuple[np.ndarray, np.ndarray]:
    lat_rad = np.deg2rad(lat2d.astype(np.float64, copy=False))
    dlon = np.deg2rad(normalize_lon(lon2d.astype(np.float64, copy=False) - sub_lon))
    cos_psi = np.cos(lat_rad) * np.cos(dlon)
    visible = cos_psi > (EARTH_RADIUS_KM / SATELLITE_RADIUS_KM)
    numer = SATELLITE_RADIUS_KM * cos_psi - EARTH_RADIUS_KM
    denom = np.sqrt(SATELLITE_RADIUS_KM**2 + EARTH_RADIUS_KM**2 - 2.0 * SATELLITE_RADIUS_KM * EARTH_RADIUS_KM * cos_psi)
    vza = np.full(lat2d.shape, np.nan, dtype=np.float32)
    vza[visible] = np.rad2deg(np.arccos(np.clip(numer[visible] / denom[visible], -1.0, 1.0))).astype(np.float32)
    dlon_to_ssp = np.deg2rad(normalize_lon(sub_lon - lon2d.astype(np.float64, copy=False)))
    vaa = (np.rad2deg(np.arctan2(np.sin(dlon_to_ssp), -np.sin(lat_rad) * np.cos(dlon_to_ssp))) + 360.0) % 360.0
    vaa = vaa.astype(np.float32)
    vaa[~visible] = np.nan
    return vza, vaa


def reproject_rectilinear(src: np.ndarray, src_lat: np.ndarray, src_lon: np.ndarray, target_lat: np.ndarray, target_lon: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    lat = np.asarray(src_lat, dtype=np.float64)
    lon = np.asarray(src_lon, dtype=np.float64)
    if lat.ndim != 1 or lon.ndim != 1:
        raise RuntimeError("Himawari R21 latitude/longitude are not 1-D; refusing ad-hoc remap")
    lon360 = lon % 360.0
    target_lon360 = target_lon % 360.0
    lat_asc = lat[::-1] if lat[0] > lat[-1] else lat
    src_lat_idx_asc = np.arange(lat.size - 1, -1, -1) if lat[0] > lat[-1] else np.arange(lat.size)
    iy_sorted = np.searchsorted(lat_asc, target_lat)
    iy_sorted = np.clip(iy_sorted, 1, lat_asc.size - 1)
    left = iy_sorted - 1
    right = iy_sorted
    choose_right = np.abs(lat_asc[right] - target_lat) < np.abs(lat_asc[left] - target_lat)
    iy_src = src_lat_idx_asc[np.where(choose_right, right, left)]
    y_ok = (target_lat >= lat_asc[0] - 1e-6) & (target_lat <= lat_asc[-1] + 1e-6)
    ix = np.searchsorted(lon360, target_lon360)
    ix = np.clip(ix, 1, lon360.size - 1)
    left = ix - 1
    right = ix
    choose_right = np.abs(lon360[right] - target_lon360) < np.abs(lon360[left] - target_lon360)
    ix_src = np.where(choose_right, right, left)
    x_ok = (target_lon360 >= lon360[0] - 1e-6) & (target_lon360 <= lon360[-1] + 1e-6)
    out = np.full((target_lat.size, target_lon.size), np.nan, dtype=np.float32)
    out[np.ix_(y_ok, x_ok)] = np.asarray(src, dtype=np.float32)[np.ix_(iy_src[y_ok], ix_src[x_ok])]
    valid = np.isfinite(out).astype(np.uint8)
    return out, valid


def reproject_fy4b_official_angles(target_lon: np.ndarray, target_lat: np.ndarray, grid: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, tuple[np.ndarray, np.ndarray]]]:
    native = STAGE_ROOT / "standardized_native" / f"FY4B_GEO_{TIME_TAG}_native_cloud_v0.npz"
    if not native.exists():
        raise RuntimeError(f"FY4B GEO native missing: {native}")
    bundle = F05.load_npz(native)
    ref_shape = tuple(np.asarray(bundle["arrays"]["sensor_zenith_angle"]).shape)
    lon, lat, notes = F05.geolocate(bundle, ref_shape)
    source_valid = np.asarray(bundle["arrays"].get("valid_mask", np.ones(ref_shape, dtype=np.uint8))).astype(bool)
    tree, src_y, src_x, tree_notes = F05.build_tree(lon, lat, source_valid)
    rows: list[dict[str, Any]] = []
    layers: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for angle in ["sensor_zenith_angle", "sensor_azimuth_angle", "solar_zenith_angle", "solar_azimuth_angle", "sun_glint_angle"]:
        arr = np.asarray(bundle["arrays"][angle], dtype=np.float32)
        data, valid = F05.query_reproject(tree, src_y, src_x, arr, target_lon, target_lat, np.dtype(np.float32), np.nan)
        if angle.endswith("azimuth_angle"):
            finite = np.isfinite(data)
            data[finite] = data[finite] % 360.0
        path = save_angle_layer("FY4B", angle, data, valid, "OFFICIAL_PIXEL_ANGLE", str(native), grid, "|".join(notes + tree_notes))
        quicklook(ANGLE_QUICKLOOK_DIR / f"FY4B_{angle}.png", data, valid, f"FY4B {angle} OFFICIAL_PIXEL_ANGLE")
        rows.append(layer_row("FY4B", angle, "OFFICIAL_PIXEL_ANGLE", str(path), "official FY4B GEO angle array reprojected by nearest"))
        layers[angle] = (data, valid)
    add_derived_layers("FY4B", layers, "OFFICIAL_PIXEL_ANGLE", str(native), grid, rows)
    return rows, layers


def reproject_himawari_r21(target_lon: np.ndarray, target_lat: np.ndarray, grid: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, tuple[np.ndarray, np.ndarray]]]:
    r21_path, info = find_himawari_r21_geometry_file(TARGET_TIME)
    if r21_path is None:
        raise RuntimeError(f"Himawari R21 exact file missing: {info}")
    if not bool(info.get("same_day")) or not bool(info.get("same_minute")) or float(info.get("dt_minutes", 9999)) != 0.0:
        raise RuntimeError(f"Himawari R21 selected file is not exact target time: {info}")
    rows: list[dict[str, Any]] = []
    layers: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    var_map = {"SAZ": "sensor_zenith_angle", "SAA": "sensor_azimuth_angle", "SOZ": "solar_zenith_angle", "SOA": "solar_azimuth_angle"}
    with netCDF4.Dataset(r21_path) as ds:
        src_lat = np.asarray(ds.variables["latitude"][:], dtype=np.float64)
        src_lon = np.asarray(ds.variables["longitude"][:], dtype=np.float64)
        gp = np.asarray(ds.variables["geometry_parameters"][:], dtype=np.float64) if "geometry_parameters" in ds.variables else np.array([])
        for src_name, angle in var_map.items():
            data, valid = reproject_rectilinear(np.asarray(ds.variables[src_name][:], dtype=np.float32), src_lat, src_lon, target_lat, target_lon)
            if angle.endswith("azimuth_angle"):
                finite = np.isfinite(data)
                data[finite] = data[finite] % 360.0
            note = f"official JAXA P-Tree R21_FLDK exact-time gridded angle; geometry_parameters={gp.tolist()}"
            path = save_angle_layer("Himawari-9", angle, data, valid, "OFFICIAL_GRIDDED_ANGLE", str(r21_path), grid, note)
            quicklook(ANGLE_QUICKLOOK_DIR / f"Himawari-9_{angle}.png", data, valid, f"Himawari-9 {angle} OFFICIAL_GRIDDED_ANGLE")
            rows.append(layer_row("Himawari-9", angle, "OFFICIAL_GRIDDED_ANGLE", str(path), note))
            layers[angle] = (data, valid)
    add_derived_layers("Himawari-9", layers, "OFFICIAL_GRIDDED_ANGLE", str(r21_path), grid, rows)
    return rows, layers


def add_nav_computed_satellite(satellite: str, target_lon: np.ndarray, target_lat: np.ndarray, grid: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, tuple[np.ndarray, np.ndarray]]]:
    lon2d, lat2d = np.meshgrid(target_lon.astype(np.float32), target_lat.astype(np.float32))
    vza, vaa = nav_vza_vaa(lon2d, lat2d, SAT_SUBPOINT[satellite])
    sza, saa = solar_angles(lon2d, lat2d)
    valid_v = np.isfinite(vza).astype(np.uint8)
    valid_s = np.isfinite(sza).astype(np.uint8)
    rows: list[dict[str, Any]] = []
    layers: dict[str, tuple[np.ndarray, np.ndarray]] = {
        "sensor_zenith_angle": (vza, valid_v),
        "sensor_azimuth_angle": (vaa, np.isfinite(vaa).astype(np.uint8)),
        "solar_zenith_angle": (sza, valid_s),
        "solar_azimuth_angle": (saa, np.isfinite(saa).astype(np.uint8)),
    }
    source_file = "GOES ABI projection metadata" if satellite.startswith("GOES") else "EUMETSAT MSG SEVIRI native service metadata"
    for angle in ["sensor_zenith_angle", "sensor_azimuth_angle"]:
        note = f"VZA/VAA computed from official navigation metadata and target-grid lon/lat; subpoint_lon={SAT_SUBPOINT[satellite]}"
        path = save_angle_layer(satellite, angle, layers[angle][0], layers[angle][1], "OFFICIAL_NAV_DERIVED", source_file, grid, note)
        quicklook(ANGLE_QUICKLOOK_DIR / f"{satellite}_{angle}.png", layers[angle][0], layers[angle][1], f"{satellite} {angle} OFFICIAL_NAV_DERIVED")
        rows.append(layer_row(satellite, angle, "OFFICIAL_NAV_DERIVED", str(path), note))
    for angle in ["solar_zenith_angle", "solar_azimuth_angle"]:
        note = "computed solar angle from target UTC time and target-grid lon/lat; used for 07 stratification only"
        path = save_angle_layer(satellite, angle, layers[angle][0], layers[angle][1], "COMPUTED_SOLAR", "solar_position_algorithm", grid, note)
        quicklook(ANGLE_QUICKLOOK_DIR / f"{satellite}_{angle}.png", layers[angle][0], layers[angle][1], f"{satellite} {angle} COMPUTED_SOLAR")
        rows.append(layer_row(satellite, angle, "COMPUTED_SOLAR", str(path), note))
    add_derived_layers(satellite, layers, "OFFICIAL_NAV_DERIVED", source_file, grid, rows)
    return rows, layers


def add_derived_layers(
    satellite: str,
    layers: dict[str, tuple[np.ndarray, np.ndarray]],
    upstream_level: str,
    source_file: str,
    grid: dict[str, Any],
    rows: list[dict[str, Any]],
) -> None:
    if "sensor_azimuth_angle" in layers and "solar_azimuth_angle" in layers:
        raa = relative_azimuth(layers["solar_azimuth_angle"][0], layers["sensor_azimuth_angle"][0])
        valid = (layers["solar_azimuth_angle"][1].astype(bool) & layers["sensor_azimuth_angle"][1].astype(bool)).astype(np.uint8)
        path = save_angle_layer(satellite, "relative_azimuth_angle", raa, valid, "DERIVED_DIAGNOSTIC", source_file, grid, f"derived from solar/sensor azimuth; upstream={upstream_level}")
        quicklook(ANGLE_QUICKLOOK_DIR / f"{satellite}_relative_azimuth_angle.png", raa, valid, f"{satellite} relative_azimuth DERIVED_DIAGNOSTIC")
        rows.append(layer_row(satellite, "relative_azimuth_angle", "DERIVED_DIAGNOSTIC", str(path), "RAA = abs(SAA - VAA), folded to 0..180; 07 stratification only"))
        layers["relative_azimuth_angle"] = (raa, valid)
    if "sun_glint_angle" not in layers and {"sensor_zenith_angle", "solar_zenith_angle", "relative_azimuth_angle"}.issubset(layers):
        glint = glint_angle(layers["solar_zenith_angle"][0], layers["sensor_zenith_angle"][0], layers["relative_azimuth_angle"][0])
        valid = (layers["solar_zenith_angle"][1].astype(bool) & layers["sensor_zenith_angle"][1].astype(bool) & layers["relative_azimuth_angle"][1].astype(bool)).astype(np.uint8)
        path = save_angle_layer(satellite, "sun_glint_angle", glint, valid, "DERIVED_DIAGNOSTIC", source_file, grid, f"derived vector glint diagnostic; upstream={upstream_level}")
        quicklook(ANGLE_QUICKLOOK_DIR / f"{satellite}_sun_glint_angle.png", glint, valid, f"{satellite} sun_glint DERIVED_DIAGNOSTIC")
        rows.append(layer_row(satellite, "sun_glint_angle", "DERIVED_DIAGNOSTIC", str(path), "derived glint angle; 07 stratification only"))
        layers["sun_glint_angle"] = (glint, valid)
    if "solar_zenith_angle" in layers:
        day = day_night_from_sza(layers["solar_zenith_angle"][0])
        valid = (day >= 0).astype(np.uint8)
        path = save_angle_layer(satellite, "day_night_flag", day, valid, "DERIVED_DIAGNOSTIC", source_file, grid, "day<85, twilight 85..95, night>95 by SZA")
        quicklook(ANGLE_QUICKLOOK_DIR / f"{satellite}_day_night_flag.png", day, valid, f"{satellite} day_night_flag")
        rows.append(layer_row(satellite, "day_night_flag", "DERIVED_DIAGNOSTIC", str(path), "thresholds: day <85, twilight 85..95, night >95"))
        layers["day_night_flag"] = (day, valid)


def layer_row(satellite: str, angle: str, level: str, source: str, notes: str) -> dict[str, Any]:
    return {
        "satellite": satellite,
        "angle_name": angle,
        "source_level": level,
        "source_file_or_metadata": source,
        "source_time": TARGET_TIME if satellite in {"FY4B", "Himawari-9"} or level == "COMPUTED_SOLAR" else "static_navigation_metadata",
        "target_time": TARGET_TIME,
        "time_match_status": "exact" if satellite in {"FY4B", "Himawari-9"} or level == "COMPUTED_SOLAR" else "static",
        "spatial_grid": "target_grid_0.05deg_lonlat",
        "azimuth_convention": "0 north, 90 east, clockwise; from target pixel toward satellite/sun",
        "used_in_06_rating": angle == "sensor_zenith_angle",
        "used_in_07_stratification": angle in ANGLE_NAMES,
        "fallback_allowed": False if satellite in {"FY4B", "Himawari-9"} and angle == "sensor_zenith_angle" else True,
        "notes": notes,
    }


def write_policy() -> None:
    policy = {
        "angle_source_levels": [
            "OFFICIAL_PIXEL_ANGLE",
            "OFFICIAL_GRIDDED_ANGLE",
            "OFFICIAL_NAV_DERIVED",
            "COMPUTED_SOLAR",
            "DERIVED_DIAGNOSTIC",
            "FALLBACK_APPROX",
            "MISSING",
        ],
        "angle_conventions": {
            "zenith": "0 at local vertical/top, 90 at horizon",
            "azimuth": "0 north, 90 east, clockwise",
            "sensor_azimuth_angle": "from target pixel toward satellite",
            "solar_azimuth_angle": "from target pixel toward sun",
            "relative_azimuth_angle": "abs(SAA - VAA), folded to 0..180",
            "day_night_flag": {"day": "SZA < 85", "twilight": "85 <= SZA <= 95", "night": "SZA > 95"},
        },
        "used_in_06_rating": ["sensor_zenith_angle"],
        "not_used_in_06_rating": ["solar_zenith_angle", "solar_azimuth_angle", "relative_azimuth_angle", "sun_glint_angle", "day_night_flag"],
        "quality_policy": {"FY4B_quality_flag_raw": "diagnostic only; not used in 06 quality_weight"},
    }
    (SYNC_DIR / "geometry_angle_source_policy.yaml").write_text(yaml.safe_dump(policy, allow_unicode=True, sort_keys=False), encoding="utf-8")


def backup_current_fusion() -> Path:
    backup = SYNC_DIR / "pre06_fused_best_source_backup"
    if backup.exists():
        return backup
    backup.mkdir(parents=True, exist_ok=True)
    for pat in ["source_map_*.npz", "rating_map_*.npz", "valid_count_map_*.npz", "fusion_*.csv"]:
        for src in FUSED_DIR.glob(pat):
            shutil.copy2(src, backup / src.name)
    return backup


def compare_source_maps(backup: Path) -> pd.DataFrame:
    rows = []
    for old in backup.glob("source_map_*.npz"):
        new = FUSED_DIR / old.name
        if not new.exists():
            rows.append({"artifact": old.name, "status": "NEW_MISSING"})
            continue
        _, old_arrays = load_npz(old)
        _, new_arrays = load_npz(new)
        key_old = next((k for k in old_arrays if k.startswith("source_map")), "data")
        key_new = next((k for k in new_arrays if k.startswith("source_map")), "data")
        a = np.asarray(old_arrays[key_old])
        b = np.asarray(new_arrays[key_new])
        valid = (a > 0) | (b > 0)
        changed = valid & (a != b)
        rows.append(
            {
                "artifact": old.name,
                "variable": old.stem.replace("source_map_", ""),
                "valid_pixels_union": int(np.count_nonzero(valid)),
                "changed_pixels": int(np.count_nonzero(changed)),
                "changed_fraction_on_valid": float(np.count_nonzero(changed) / np.count_nonzero(valid)) if np.count_nonzero(valid) else 0.0,
                "status": "OK",
            }
        )
    df = pd.DataFrame(rows)
    df.to_csv(SYNC_DIR / "source_map_change_summary.csv", index=False, encoding="utf-8-sig")
    return df


def write_aux_summaries(prov: pd.DataFrame) -> None:
    view = prov[prov["angle_name"] == "sensor_zenith_angle"].copy()
    view[["satellite", "angle_name", "source_level", "source_file_or_metadata", "used_in_06_rating", "notes"]].to_csv(
        SYNC_DIR / "view_weight_source_summary.csv", index=False, encoding="utf-8-sig"
    )
    freq = pd.read_csv(FUSED_DIR / "fusion_source_frequency.csv", encoding="utf-8-sig") if (FUSED_DIR / "fusion_source_frequency.csv").exists() else pd.DataFrame()
    if freq.empty:
        pd.DataFrame().to_csv(SYNC_DIR / "selected_source_geometry_source_summary.csv", index=False, encoding="utf-8-sig")
    else:
        merged = freq.merge(view[["satellite", "source_level", "source_file_or_metadata"]], on="satellite", how="left")
        merged.to_csv(SYNC_DIR / "selected_source_geometry_source_summary.csv", index=False, encoding="utf-8-sig")


def official_vs_computed_stats(layers_by_sat: dict[str, dict[str, tuple[np.ndarray, np.ndarray]]], target_lon: np.ndarray, target_lat: np.ndarray) -> pd.DataFrame:
    lon2d, lat2d = np.meshgrid(target_lon.astype(np.float32), target_lat.astype(np.float32))
    rows = []
    for sat in ["FY4B", "Himawari-9"]:
        if sat not in layers_by_sat or "sensor_zenith_angle" not in layers_by_sat[sat]:
            continue
        official, valid = layers_by_sat[sat]["sensor_zenith_angle"]
        computed, _ = nav_vza_vaa(lon2d, lat2d, SAT_SUBPOINT[sat])
        mask = np.asarray(valid).astype(bool) & np.isfinite(official) & np.isfinite(computed)
        diff = official.astype(np.float32) - computed.astype(np.float32)
        stat = finite_stats(np.where(mask, diff, np.nan))
        rows.append({"satellite": sat, "comparison": "official_vza_minus_nav_computed_vza", **stat})
    df = pd.DataFrame(rows)
    df.to_csv(SYNC_DIR / "official_vs_computed_vza_stats.csv", index=False, encoding="utf-8-sig")
    return df


def write_report(prov: pd.DataFrame, change: pd.DataFrame, official_diff: pd.DataFrame) -> None:
    required_vza = prov[(prov["angle_name"] == "sensor_zenith_angle") & (prov["source_level"] != "MISSING")]
    angle_provenance_gate = "PASS" if len(required_vza["satellite"].unique()) == len(TIE_ORDER) else "FAIL"
    h_exact = not prov[(prov["satellite"] == "Himawari-9") & (prov["angle_name"] == "sensor_zenith_angle") & (prov["time_match_status"] == "exact")].empty
    official_gate = "PASS" if h_exact and not prov[(prov["satellite"] == "FY4B") & (prov["source_level"] == "OFFICIAL_PIXEL_ANGLE")].empty else "FAIL"
    geometry_gate = "PASS" if angle_provenance_gate == "PASS" else "FAIL"
    max_change = float(change["changed_fraction_on_valid"].max()) if not change.empty and "changed_fraction_on_valid" in change else 0.0
    source_gate = "PASS" if max_change <= 0.20 else ("PASS_WITH_WARNINGS" if max_change <= 0.60 else "FAIL")
    angle_layer_gate = "PASS" if all((REPROJECT_DIR / sat / f"{sat}_ANGLE_sensor_zenith_angle_grid_{TIME_TAG}.npz").exists() for sat in TIE_ORDER) else "FAIL"
    allow_07 = angle_provenance_gate == "PASS" and geometry_gate == "PASS" and source_gate != "FAIL" and angle_layer_gate != "FAIL"
    lines = [
        "# 06e full geometry angle source sync patch 报告",
        "",
        f"- 生成时间 UTC：{utc_now()}",
        f"- 目标时次：`{TARGET_TIME}`",
        "- 本步骤不下载、不扩展多时次、不做 07 overlap validation；只同步角度来源、角度层、06 view_weight 入口和诊断表。",
        "",
        "## Gate",
        "",
        f"- ANGLE_PROVENANCE_GATE: **{angle_provenance_gate}**",
        f"- OFFICIAL_ANGLE_INGEST_GATE: **{official_gate}**",
        f"- GEOMETRY_SYNC_GATE: **{geometry_gate}**",
        f"- SOURCE_MAP_STABILITY_GATE: **{source_gate}**，最大 source_map changed_fraction_on_valid={max_change:.4f}",
        f"- ANGLE_LAYER_GATE: **{angle_layer_gate}**",
        f"- 是否允许进入 07：**{'YES' if allow_07 else 'NO'}**",
        "",
        "## 1. 每颗卫星/角度的 source level",
        "",
    ]
    for sat in TIE_ORDER:
        subset = prov[prov["satellite"] == sat]
        desc = ", ".join(f"{r.angle_name}={r.source_level}" for r in subset.itertuples())
        lines.append(f"- {sat}: {desc}")
    lines.extend(
        [
            "",
            "## 2. Himawari exact R21 是否替代 fallback",
            "",
            "- 是。本轮要求 `2024-03-05T00:00:00Z` 精确 R21_FLDK；如果 `same_day/same_minute/dt_minutes=0` 不满足，脚本直接失败，不使用 2024-03-04 转移。",
            "",
            "## 3. GOES 为什么是 official-navigation-derived",
            "",
            "- 当前实际 GOES ABI L1b/L2 文件提供 `x/y/goes_imager_projection` 和投影元数据；未发现逐像元官方角度数组。因此 VZA/VAA 由官方 fixed-grid 导航参数派生，source level 记为 `OFFICIAL_NAV_DERIVED`。",
            "",
            "## 4. Meteosat 为什么是 official-navigation-derived",
            "",
            "- 当前 Meteosat 云产品只确认 CLM/CTH 云变量；几何采用 MSG/SEVIRI 服务经度和 native/navigation 元数据派生目标格点 VZA/VAA，不假装存在官方逐像元角度层。",
            "",
            "## 5. FY4B 是否使用官方 GEO pixel angle",
            "",
            "- 是。FY4B 使用 GEO 中 `NOMSatelliteZenith/NOMSatelliteAzimuth/NOMSunZenith/NOMSunAzimuth/NOMSunGlintAngle` 标准化后的官方像元角度，nearest 落到目标网格。",
            "",
            "## 6. 哪些角度进入 06 rating，哪些只进 07",
            "",
            "- 06 rating 只使用 `sensor_zenith_angle` 形成 `view_weight`。",
            "- `solar_zenith_angle`、`solar_azimuth_angle`、`relative_azimuth_angle`、`sun_glint_angle`、`day_night_flag` 只供 07 分层统计和诊断，不参与当前 06 best-source rating。",
            "- FY4B `quality_flag_raw` 和 `NavQualityFlag` 仍不进入 06 rating。",
            "",
            "## 7. source_map 是否因角度同步改变",
            "",
        ]
    )
    if change.empty:
        lines.append("- 无可比较 source_map。")
    else:
        for r in change.itertuples():
            lines.append(f"- {r.variable}: changed_fraction_on_valid={r.changed_fraction_on_valid:.4f}, changed_pixels={r.changed_pixels}")
    lines.extend(["", "## 8. 是否有异常变化", ""])
    lines.append("- 判断规则：最大变化比例 <=0.20 为 PASS，0.20..0.60 为 PASS_WITH_WARNINGS，>0.60 为 FAIL。具体空间合理性仍需在 07 overlap 中验证。")
    if not official_diff.empty:
        lines.append("- FY4B/Himawari official VZA 与导航计算 VZA 的差异统计已输出到 `official_vs_computed_vza_stats.csv`，仅作诊断，不替代官方角度。")
    lines.extend(
        [
            "",
            "## 9. 是否进入 07",
            "",
            f"- 结论：**{'可以进入 07' if allow_07 else '暂不进入 07'}**。",
            "- 进入 07 时必须使用本轮生成的 `angle_provenance_inventory.csv` 和 `ANGLE` 角度层；07 的角度分层不得再混用旧 fallback 说明。",
            "",
            "## 输出文件",
            "",
            f"- `{SYNC_DIR / 'geometry_angle_source_policy.yaml'}`",
            f"- `{SYNC_DIR / 'angle_provenance_inventory.csv'}`",
            f"- `{SYNC_DIR / 'angle_layer_inventory.csv'}`",
            f"- `{SYNC_DIR / 'official_vs_computed_vza_stats.csv'}`",
            f"- `{SYNC_DIR / 'source_map_change_summary.csv'}`",
            f"- `{SYNC_DIR / 'view_weight_source_summary.csv'}`",
            f"- `{SYNC_DIR / 'selected_source_geometry_source_summary.csv'}`",
            f"- `{ANGLE_GRID_DIR}`",
            f"- `{ANGLE_QUICKLOOK_DIR}`",
        ]
    )
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ensure_output_dirs()
    shutil.copy2(SCRIPT_PATH, SCRIPT_DIR / SCRIPT_PATH.name)
    write_policy()

    backup = backup_current_fusion()
    run_stage("02_build_standardized_cloud_native.py")

    target_lon, target_lat, grid = target_grid()
    all_rows: list[dict[str, Any]] = []
    layers_by_sat: dict[str, dict[str, tuple[np.ndarray, np.ndarray]]] = {}

    rows, layers = reproject_fy4b_official_angles(target_lon, target_lat, grid)
    all_rows.extend(rows)
    layers_by_sat["FY4B"] = layers

    rows, layers = reproject_himawari_r21(target_lon, target_lat, grid)
    all_rows.extend(rows)
    layers_by_sat["Himawari-9"] = layers

    for sat in ["GOES-16", "GOES-18", "Meteosat-0deg", "Meteosat-IODC"]:
        rows, layers = add_nav_computed_satellite(sat, target_lon, target_lat, grid)
        all_rows.extend(rows)
        layers_by_sat[sat] = layers

    prov = pd.DataFrame(all_rows)
    prov.to_csv(SYNC_DIR / "angle_provenance_inventory.csv", index=False, encoding="utf-8-sig")
    layer_rows = []
    for sat, layers in layers_by_sat.items():
        for angle, (data, valid) in layers.items():
            layer_rows.append({"satellite": sat, "angle_name": angle, "coverage_ratio": float(np.count_nonzero(valid) / valid.size), **angle_stats(data, valid)})
    pd.DataFrame(layer_rows).to_csv(SYNC_DIR / "angle_layer_inventory.csv", index=False, encoding="utf-8-sig")
    official_diff = official_vs_computed_stats(layers_by_sat, target_lon, target_lat)

    run_stage("06_fuse_best_source.py")
    run_stage("06_5_source_selection_diagnostics.py")
    change = compare_source_maps(backup)
    write_aux_summaries(prov)
    write_report(prov, change, official_diff)
    print(f"06e report: {REPORT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
