"""Reusable Stage 06 fusion inputs, geometry weights, and candidate construction."""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any

import matplotlib
import netCDF4
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, ListedColormap

from .adapters.cloud_products import (
    find_himawari_r21_geometry_file,
    read_himawari_r21_geometry,
    read_mapping,
)
from .cloud_semantics import reproject_mask_for_use
from .pipeline_layout import STAGE_ROOT
from .sources import SOURCE_BY_KEY, SOURCE_ID_MAP, tie_order, validate_profile, variable_rules


COMPONENT_ROLE = "fusion_support"

REPROJECT_DIR = STAGE_ROOT / "reprojected_grid"
NATIVE_DIR = STAGE_ROOT / "standardized_native"
TARGET_TIME = os.environ.get("GEO_RING_TARGET_TIME", "2024-03-05T00:00:00Z")
TIME_TAG = os.environ.get("GEO_RING_TIME_TAG", TARGET_TIME[0:13].replace("-", "").replace("T", "_"))
TARGET_SHAPE = (3600, 7200)
SOURCE_PROFILE = validate_profile(os.environ.get("GEO_RING_SOURCE_PROFILE", "operational_baseline"))
TIE_ORDER = tie_order(SOURCE_PROFILE)
VARIABLE_RULES: dict[str, list[dict[str, str]]] = {
    variable: [{"satellite": item["source_key"], "product": item["product"]} for item in rules]
    for variable, rules in variable_rules(SOURCE_PROFILE).items()
}
CATEGORICAL_VARS = {"cloud_mask", "cloud_phase", "cloud_type"}
EARTH_RADIUS_KM = 6378.137
GEO_ALTITUDE_KM = 35786.023
SATELLITE_RADIUS_KM = EARTH_RADIUS_KM + GEO_ALTITUDE_KM

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
