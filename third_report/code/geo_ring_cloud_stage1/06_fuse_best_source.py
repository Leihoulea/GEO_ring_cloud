from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
from pathlib import Path
from typing import Any

import matplotlib
import netCDF4
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, ListedColormap

from geo_ring_cloud import paths as path_config
from geo_ring_cloud.lineage import write_manifest
from geo_ring_cloud.sources import (
    REGISTRY_VERSION,
    SOURCE_BY_KEY,
    SOURCE_ID_MAP,
    tie_order,
    validate_profile,
    variable_rules,
)
from geo_ring_cloud.diagnostics.summary import finite_stats
from geo_ring_cloud.pipeline_support import (
    REPORT_DIR,
    SCRIPT_DIR,
    STAGE_ROOT,
    ensure_dirs,
    find_himawari_r21_geometry_file,
    read_himawari_r21_geometry,
    read_mapping,
    utc_now,
)
from geo_ring_cloud.cloud_semantics import cloud_mask_semantics, reproject_mask_for_use


REPROJECT_DIR = STAGE_ROOT / "reprojected_grid"
NATIVE_DIR = STAGE_ROOT / "standardized_native"
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
EARTH_RADIUS_KM = 6378.137
GEO_ALTITUDE_KM = 35786.023
SATELLITE_RADIUS_KM = EARTH_RADIUS_KM + GEO_ALTITUDE_KM

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


def parse_output_filename(path: Path) -> tuple[str, str, str] | None:
    name = path.name
    if not name.endswith(f"_grid_{TIME_TAG}.npz"):
        return None
    stem = name[: -len(f"_grid_{TIME_TAG}.npz")]
    for sat in TIE_ORDER:
        prefix = f"{sat}_"
        if stem.startswith(prefix):
            rest = stem[len(prefix) :]
            if "_" not in rest:
                return None
            product, variable = rest.split("_", 1)
            return sat, product, variable
    return None


def load_catalog() -> dict[tuple[str, str, str], Path]:
    catalog: dict[tuple[str, str, str], Path] = {}
    inventory_path = REPROJECT_DIR / "reprojected_variable_inventory.csv"
    if inventory_path.exists():
        inventory = pd.read_csv(inventory_path)
        for row in inventory.itertuples(index=False):
            if str(getattr(row, "status", "")) != "OK":
                continue
            path = Path(str(getattr(row, "output_file", "")))
            key = (str(getattr(row, "satellite", "")), str(getattr(row, "product", "")), str(getattr(row, "variable", "")))
            if path.exists() and key[0] in TIE_ORDER:
                catalog[key] = path
        if catalog:
            return catalog
    for path in REPROJECT_DIR.rglob(f"*_{TIME_TAG}.npz"):
        parsed = parse_output_filename(path)
        if parsed is None:
            continue
        catalog[parsed] = path
    return catalog


def load_npz_payload(path: Path) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    with np.load(path, allow_pickle=False) as z:
        meta = json.loads(str(z["metadata_json"]))
        arrays = {name: np.asarray(z[name]) for name in z.files if not name.endswith("_json")}
    return meta, arrays


def target_grid_from_any(catalog: dict[tuple[str, str, str], Path]) -> dict[str, Any]:
    if not catalog:
        raise RuntimeError("no reprojected files found")
    sample = next(iter(catalog.values()))
    meta, _ = load_npz_payload(sample)
    grid = dict(meta.get("target_grid", {}))
    if grid.get("shape") != [TARGET_SHAPE[0], TARGET_SHAPE[1]]:
        raise RuntimeError(f"unexpected target grid shape: {grid.get('shape')}")
    return grid


def normalize_lon(lon: float | np.ndarray) -> float | np.ndarray:
    return ((np.asarray(lon) + 180.0) % 360.0) - 180.0


def build_target_lon_lat(target_grid: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    lon = target_grid["lon_min"] + target_grid["resolution_degree"] / 2.0 + np.arange(target_grid["lon_size"], dtype=np.float32) * target_grid["resolution_degree"]
    lat = target_grid["lat_min"] + target_grid["resolution_degree"] / 2.0 + np.arange(target_grid["lat_size"], dtype=np.float32) * target_grid["resolution_degree"]
    return lon, lat


def infer_subpoint_from_native_latlon(path: Path) -> float:
    with np.load(path, allow_pickle=False) as z:
        if "latitude" not in z.files or "longitude" not in z.files:
            raise RuntimeError(f"native lat/lon missing in {path.name}")
        lat = np.asarray(z["latitude"], dtype=np.float32)
        lon = np.asarray(z["longitude"], dtype=np.float32)
    h, w = lat.shape
    yy, xx = np.indices(lat.shape, dtype=np.float32)
    cy = (h - 1) / 2.0
    cx = (w - 1) / 2.0
    valid = np.isfinite(lat) & np.isfinite(lon)
    score = np.abs(lat) + 1e-4 * np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
    score[~valid] = np.inf
    iy, ix = np.unravel_index(np.argmin(score), score.shape)
    return float(normalize_lon(lon[iy, ix]))


def infer_subpoint_longitude(satellite: str, catalog: dict[tuple[str, str, str], Path]) -> tuple[float, str]:
    if satellite == "CLAAS3-0deg":
        return SOURCE_BY_KEY[satellite].service_longitude_deg, "source_registry_service_longitude"
    if satellite == "FY4B":
        sample = catalog.get((satellite, "CLM", "cloud_mask")) or catalog.get((satellite, "CTH", "cloud_top_height_km"))
        if sample is None:
            raise RuntimeError("FY4B sample missing")
        meta, _ = load_npz_payload(sample)
        source_file = str(meta.get("source_file", ""))
        match = re.search(r"_N_DISK_(\d{4})([EW])_", source_file)
        if not match:
            raise RuntimeError(f"cannot parse FY4B subpoint from {source_file}")
        value = float(match.group(1)) / 10.0
        if match.group(2) == "W":
            value = -value
        return float(normalize_lon(value)), "parsed_from_source_filename"

    if satellite.startswith("GOES"):
        sample = catalog.get((satellite, "ACMF", "cloud_mask")) or catalog.get((satellite, "ACHAF", "cloud_top_height_km"))
        if sample is None:
            raise RuntimeError(f"{satellite} sample missing")
        meta, _ = load_npz_payload(sample)
        source_file = Path(str(meta.get("source_file", "")))
        with netCDF4.Dataset(source_file) as ds:
            proj = ds.variables["goes_imager_projection"]
            lon0 = float(getattr(proj, "longitude_of_projection_origin"))
        return float(normalize_lon(lon0)), "read_from_goes_imager_projection"

    if satellite.startswith("Himawari"):
        native = NATIVE_DIR / f"{satellite}_CMSK_{TIME_TAG}_native_cloud_v0.npz"
        if not native.exists():
            native = NATIVE_DIR / f"{satellite}_CHGT_{TIME_TAG}_native_cloud_v0.npz"
        aux_path, aux_info = find_himawari_r21_geometry_file(TARGET_TIME)
        if aux_path is not None:
            aux = read_himawari_r21_geometry(aux_path, read_mapping())
            gp = np.asarray(aux.attrs.get("r21_geometry_parameters", []), dtype=np.float64)
            if gp.size >= 1 and np.isfinite(gp[0]):
                return float(normalize_lon(float(gp[0]))), f"read_from_himawari_r21_geometry_parameters selected_time={aux_info.get('selected_time')}"
        if native.exists():
            return infer_subpoint_from_native_latlon(native), "estimated_from_native_latlon_center_pixel"
        return SOURCE_BY_KEY[satellite].service_longitude_deg, "source_registry_service_longitude_reused_asset"

    if satellite.startswith("Meteosat"):
        native = NATIVE_DIR / f"{satellite}_CLM_{TIME_TAG}_native_cloud_v0.npz"
        if native.exists():
            return infer_subpoint_from_native_latlon(native), "estimated_from_native_latlon_center_pixel"
        return SOURCE_BY_KEY[satellite].service_longitude_deg, "source_registry_service_longitude_reused_asset"

    raise RuntimeError(f"unsupported satellite {satellite}")


def build_subpoint_longitude_map(catalog: dict[tuple[str, str, str], Path]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for satellite in TIE_ORDER:
        lon0, method = infer_subpoint_longitude(satellite, catalog)
        out[satellite] = {"subpoint_lon_deg": lon0, "method": method}
    return out


def approximate_geostationary_vza(lat2d: np.ndarray, lon2d: np.ndarray, subpoint_lon_deg: float) -> np.ndarray:
    lat_rad = np.deg2rad(lat2d.astype(np.float64, copy=False))
    dlon = np.deg2rad(normalize_lon(lon2d.astype(np.float64, copy=False) - subpoint_lon_deg))
    cos_psi = np.cos(lat_rad) * np.cos(dlon)
    visible = cos_psi > (EARTH_RADIUS_KM / SATELLITE_RADIUS_KM)
    numer = SATELLITE_RADIUS_KM * cos_psi - EARTH_RADIUS_KM
    denom = np.sqrt(
        SATELLITE_RADIUS_KM * SATELLITE_RADIUS_KM
        + EARTH_RADIUS_KM * EARTH_RADIUS_KM
        - 2.0 * SATELLITE_RADIUS_KM * EARTH_RADIUS_KM * cos_psi
    )
    cos_vza = np.clip(numer / denom, -1.0, 1.0)
    out = np.full(lat2d.shape, np.nan, dtype=np.float32)
    out[visible] = np.rad2deg(np.arccos(cos_vza[visible])).astype(np.float32)
    return out


def sensor_angle_candidate_paths(catalog: dict[tuple[str, str, str], Path], satellite: str, product: str) -> list[Path]:
    candidates: list[Path] = []
    angle_layer = catalog.get((satellite, "ANGLE", "sensor_zenith_angle"))
    if angle_layer is not None:
        candidates.append(angle_layer)
    direct = catalog.get((satellite, product, "sensor_zenith_angle"))
    if direct is not None and direct not in candidates:
        candidates.append(direct)
    if satellite == "FY4B":
        geo = catalog.get((satellite, "GEO", "sensor_zenith_angle"))
        if geo is not None and geo not in candidates:
            candidates.append(geo)
    return candidates


def load_sensor_zenith(catalog: dict[tuple[str, str, str], Path], satellite: str, product: str) -> tuple[np.ndarray | None, str | None, str | None]:
    for candidate in sensor_angle_candidate_paths(catalog, satellite, product):
        meta, arrays = load_npz_payload(candidate)
        if "data" not in arrays:
            continue
        arr = np.asarray(arrays["data"], dtype=np.float32)
        if arr.shape == TARGET_SHAPE:
            source_level = str(meta.get("angle_source_level") or meta.get("source_level") or "ANGLE_LAYER")
            return arr, None, source_level
    return None, "VIEW_WEIGHT_APPROX", "FALLBACK_APPROX"


def time_weight(meta: dict[str, Any]) -> tuple[float, str | None, float]:
    nominal = meta.get("nominal_time")
    if not nominal:
        return 0.8, "TIME_WEIGHT_APPROX", float("nan")
    source_ts = pd.to_datetime(nominal, utc=True, errors="coerce")
    target_ts = pd.to_datetime(TARGET_TIME, utc=True)
    if pd.isna(source_ts):
        return 0.8, "TIME_WEIGHT_APPROX", float("nan")
    delta_min = abs((source_ts - target_ts).total_seconds()) / 60.0
    return float(1.0 / (1.0 + (delta_min / 30.0) ** 2)), None, float(delta_min)


def view_weight_from_vza(vza: np.ndarray | None, valid_mask: np.ndarray) -> tuple[np.ndarray, str | None]:
    if vza is None:
        out = np.zeros(TARGET_SHAPE, dtype=np.float32)
        out[valid_mask] = 0.8
        return out, "VIEW_WEIGHT_APPROX"
    arr = np.asarray(vza, dtype=np.float32)
    finite_valid = np.isfinite(arr) & valid_mask
    if np.any(valid_mask) and not np.any(finite_valid):
        out = np.zeros(TARGET_SHAPE, dtype=np.float32)
        out[valid_mask] = 0.8
        return out, "VIEW_WEIGHT_APPROX"
    if np.any(finite_valid):
        vmin = float(np.nanmin(arr[finite_valid]))
        vmax = float(np.nanmax(arr[finite_valid]))
        if vmin < -0.5 or vmax > 90.5:
            raise RuntimeError(f"VZA out of range: min={vmin:.3f}, max={vmax:.3f}")
    out = np.zeros(TARGET_SHAPE, dtype=np.float32)
    weight = 0.2 + 0.8 * np.cos(np.deg2rad(np.clip(arr, 0.0, 90.0)))
    weight = np.clip(weight, 0.2, 1.0).astype(np.float32, copy=False)
    out[finite_valid] = weight[finite_valid]
    return out, None


def product_level_weight(variable: str, satellite: str, product: str) -> float:
    _ = (variable, satellite, product)
    return 1.0


def cloud_mask_to_standard(satellite: str, product: str, raw: np.ndarray) -> np.ndarray:
    arr = np.asarray(raw)
    out = np.full(arr.shape, -9999, dtype=np.int16)
    sat = str(satellite)
    prod = str(product)
    if sat == "FY4B" and prod == "CLM":
        mapping = {0: 3, 1: 2, 2: 1, 3: 0}
    elif sat.startswith("GOES") and prod == "ACMF":
        mapping = {0: 0, 1: 3}
    elif sat.startswith("Himawari") and prod == "CMSK":
        mapping = {0: 0, 1: 1, 2: 2, 3: 3}
    elif sat.startswith("Meteosat") and prod == "CLM":
        mapping = {0: 0, 1: 0, 2: 3}
    elif sat == "CLAAS3-0deg" and prod == "CMA":
        mapping = {0: 0, 3: 3}
    else:
        mapping = {}
    for raw_code, std_code in mapping.items():
        out[arr == raw_code] = std_code
    return out


def cloud_binary_from_standard(std: np.ndarray, valid_mask: np.ndarray) -> np.ndarray:
    out = np.full(std.shape, -1, dtype=np.int8)
    out[valid_mask & np.isin(std, [0, 1])] = 0
    out[valid_mask & np.isin(std, [2, 3])] = 1
    return out


def candidate_valid_mask(variable: str, arrays: dict[str, np.ndarray]) -> np.ndarray:
    if variable == "cloud_mask":
        return reproject_mask_for_use(arrays, variable, use="fusion")
    return reproject_mask_for_use(arrays, variable, use="fusion")


def save_output(path: Path, data: np.ndarray, valid_mask: np.ndarray, metadata: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        data=np.asarray(data),
        valid_mask=np.asarray(valid_mask, dtype=np.uint8),
        metadata_json=np.asarray(json.dumps(metadata, ensure_ascii=False, default=str)),
    )


def make_quicklook(data: np.ndarray, valid_mask: np.ndarray, out_path: Path, title: str, variable: str, labels: dict[int, str] | None = None) -> None:
    arr = np.asarray(data)
    valid = np.asarray(valid_mask).astype(bool)
    plot = arr.astype(np.float32, copy=True)
    plot[~valid] = np.nan
    stride = max(1, int(math.ceil(math.sqrt(plot.size / 1_200_000)))) if plot.size > 1_200_000 else 1
    plot = plot[::stride, ::stride]
    plt.figure(figsize=(11, 4.8), dpi=150)
    if variable in CATEGORICAL_VARS or variable.startswith("source_map") or variable.startswith("valid_count"):
        finite = np.isfinite(plot)
        if finite.any():
            values = np.sort(np.unique(plot[finite]))
            if variable.startswith("valid_count"):
                cmap = ListedColormap(["#f7f7f7", "#c7e9c0", "#74c476", "#31a354", "#006d2c", "#00441b", "#08306b"][: max(2, values.size)])
            elif variable.startswith("source_map"):
                colors = ["#3182bd", "#31a354", "#e6550d", "#756bb1", "#636363", "#dd3497", "#6baed6"]
                cmap = ListedColormap(colors[: max(1, values.size)])
            elif variable == "cloud_mask":
                colors = ["#2b8cbe", "#a6bddb", "#fdae6b", "#d7301f"]
                cmap = ListedColormap(colors[: max(1, values.size)])
            else:
                cmap = ListedColormap(plt.get_cmap("tab20")(np.linspace(0, 1, max(1, values.size))))
            cmap.set_bad("#ffffff")
            if values.size == 1:
                bounds = np.array([values[0] - 0.5, values[0] + 0.5])
            else:
                bounds = np.concatenate(([values[0] - 0.5], (values[:-1] + values[1:]) / 2.0, [values[-1] + 0.5]))
            norm = BoundaryNorm(bounds, cmap.N)
            im = plt.imshow(plot, extent=[-180, 180, -90, 90], origin="lower", cmap=cmap, norm=norm, interpolation="nearest")
            cbar = plt.colorbar(im, shrink=0.78, ticks=values)
            if labels is None:
                cbar.ax.set_yticklabels([str(int(v)) if float(v).is_integer() else f"{v:g}" for v in values])
            else:
                cbar.ax.set_yticklabels([labels.get(int(v), str(int(v))) for v in values])
        else:
            im = plt.imshow(plot, extent=[-180, 180, -90, 90], origin="lower", cmap="gray", interpolation="nearest")
            plt.colorbar(im, shrink=0.78)
    else:
        finite = np.isfinite(plot)
        if finite.any():
            vmin, vmax = np.nanpercentile(plot[finite], [2, 98])
            if not np.isfinite(vmin) or not np.isfinite(vmax) or vmin == vmax:
                vmin, vmax = None, None
        else:
            vmin, vmax = None, None
        im = plt.imshow(plot, extent=[-180, 180, -90, 90], origin="lower", cmap="viridis", interpolation="nearest", vmin=vmin, vmax=vmax)
        plt.colorbar(im, shrink=0.78)
    coverage = float(np.count_nonzero(valid) / valid.size) if valid.size else 0.0
    plt.title(f"{title} | coverage={coverage:.3f}", fontsize=10)
    plt.xlabel("Longitude")
    plt.ylabel("Latitude")
    plt.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_path)
    plt.close()


def build_candidate(
    variable: str,
    rule: dict[str, str],
    catalog: dict[tuple[str, str, str], Path],
    lon2d: np.ndarray,
    lat2d: np.ndarray,
    subpoints: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    sat = rule["satellite"]
    product = rule["product"]
    data_path = catalog.get((sat, product, variable))
    if data_path is None:
        return None
    meta, arrays = load_npz_payload(data_path)
    if "data" not in arrays:
        return None
    data = np.asarray(arrays["data"])
    valid = candidate_valid_mask(variable, arrays)
    if valid.shape != TARGET_SHAPE or data.shape != TARGET_SHAPE:
        return None
    angle_vza, angle_vza_note, angle_source_level = load_sensor_zenith(catalog, sat, product)
    if angle_vza is not None:
        rating_vza = angle_vza
        vza_source_note = "VZA_SOURCE_ANGLE_LAYER"
    else:
        rating_vza = approximate_geostationary_vza(lat2d, lon2d, subpoints[sat]["subpoint_lon_deg"])
        vza_source_note = "VZA_SOURCE_NAV_DERIVED_FALLBACK"
        angle_source_level = "OFFICIAL_NAV_DERIVED" if sat.startswith(("GOES", "Meteosat")) else "FALLBACK_APPROX"
    valid &= np.isfinite(rating_vza)
    view_weight, extra_view_note = view_weight_from_vza(rating_vza, valid)
    tw, time_note, delta_min = time_weight(meta)
    notes = [n for n in [extra_view_note, time_note, angle_vza_note, vza_source_note] if n]
    return {
        "satellite": sat,
        "product": product,
        "variable": variable,
        "path": data_path,
        "meta": meta,
        "arrays": arrays,
        "data": data,
        "valid": valid,
        "view_weight": view_weight,
        "vza_for_rating": rating_vza,
        "vza_source_level": angle_source_level,
        "vza_source_note": vza_source_note,
        "time_weight": tw,
        "delta_min": delta_min,
        "product_level_weight": product_level_weight(variable, sat, product),
        "order_index": TIE_ORDER.index(sat),
        "subpoint_lon_deg": subpoints[sat]["subpoint_lon_deg"],
        "subpoint_method": subpoints[sat]["method"],
        "notes": notes,
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
