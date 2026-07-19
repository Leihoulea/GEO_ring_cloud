from __future__ import annotations

import importlib.util
import json
import math
import re
import shutil
from pathlib import Path
from typing import Any

import matplotlib
import netCDF4
import numpy as np
import pandas as pd

matplotlib.use("Agg")

from geo_ring_cloud.lineage import utc_now
from geo_ring_cloud.pipeline_layout import REPORT_DIR, SCRIPT_DIR, ensure_pipeline_directories as ensure_dirs


STAGE_ROOT = Path(r"D:\AAAresearch_paper\geo_ring_cloud_stage1")
NATIVE_DIR = STAGE_ROOT / "standardized_native"
FUSED_DIR = STAGE_ROOT / "fused_best_source"
OUT_DIR = STAGE_ROOT / "source_selection_diagnostics"
QUICKLOOK_DIR = OUT_DIR / "quicklooks"

TARGET_TIME = "2024-03-05T00:00:00Z"
TIME_TAG = "20240305_0000"

REPORT_MD = REPORT_DIR / "source_selection_diagnostics_report.md"
SELECTED_VS_MIN_VZA_CSV = OUT_DIR / "selected_vs_min_vza_agreement.csv"
GEOMETRY_DRIVER_CSV = OUT_DIR / "geometry_driver_summary.csv"
RATING_MARGIN_CSV = OUT_DIR / "rating_margin_summary.csv"
BOUNDARY_MARGIN_CSV = OUT_DIR / "boundary_margin_summary.csv"
UNIFIED_CHANGE_CSV = OUT_DIR / "unified_vza_change_summary.csv"
TRANSITION_CSV = OUT_DIR / "unified_vza_transition_matrix.csv"
SUBPOINT_CSV = OUT_DIR / "satellite_subpoint_longitude_summary.csv"
LEGACY_CHANGE_CSV = OUT_DIR / "legacy_mixed_change_summary.csv"
LEGACY_TRANSITION_CSV = OUT_DIR / "legacy_mixed_transition_matrix.csv"

EARTH_RADIUS_KM = 6378.137
GEO_ALTITUDE_KM = 35786.023
SATELLITE_RADIUS_KM = EARTH_RADIUS_KM + GEO_ALTITUDE_KM
APPROX_WEIGHT = 0.8
APPROX_EQUIVALENT_VZA_DEG = float(np.rad2deg(np.arccos(np.clip((APPROX_WEIGHT - 0.2) / 0.8, -1.0, 1.0))))


def load_f06_module():
    path = SCRIPT_DIR / "06_fuse_best_source.py"
    spec = importlib.util.spec_from_file_location("stage1_f06", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load 06 module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


F06 = load_f06_module()


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
    if satellite == "FY4B":
        sample = catalog.get((satellite, "CLM", "cloud_mask")) or catalog.get((satellite, "CTH", "cloud_top_height_km"))
        if sample is None:
            raise RuntimeError("FY4B sample missing")
        meta, _ = F06.load_npz_payload(sample)
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
        meta, _ = F06.load_npz_payload(sample)
        source_file = Path(str(meta.get("source_file", "")))
        with netCDF4.Dataset(source_file) as ds:
            proj = ds.variables["goes_imager_projection"]
            lon0 = float(getattr(proj, "longitude_of_projection_origin"))
        return float(normalize_lon(lon0)), "read_from_goes_imager_projection"

    if satellite.startswith("Himawari"):
        native = NATIVE_DIR / f"{satellite}_CMSK_{TIME_TAG}_native_cloud_v0.npz"
        if not native.exists():
            native = NATIVE_DIR / f"{satellite}_CHGT_{TIME_TAG}_native_cloud_v0.npz"
        if not native.exists():
            raise RuntimeError(f"Himawari native file missing for {satellite}")
        return infer_subpoint_from_native_latlon(native), "estimated_from_native_latlon_center_pixel"

    if satellite.startswith("Meteosat"):
        native = NATIVE_DIR / f"{satellite}_CLM_{TIME_TAG}_native_cloud_v0.npz"
        if not native.exists():
            raise RuntimeError(f"Meteosat CLM native file missing for {satellite}")
        return infer_subpoint_from_native_latlon(native), "estimated_from_native_latlon_center_pixel"

    raise RuntimeError(f"unsupported satellite {satellite}")


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


def view_weight_from_vza(vza: np.ndarray, valid: np.ndarray) -> np.ndarray:
    out = np.zeros(valid.shape, dtype=np.float32)
    finite = np.isfinite(vza) & valid
    if np.any(finite):
        out[finite] = np.clip(0.2 + 0.8 * np.cos(np.deg2rad(vza[finite])), 0.2, 1.0).astype(np.float32)
    return out


def select_by_rating(
    candidates: list[dict[str, Any]],
    mode: str,
    valid_key: str,
    rating_key: str,
    vza_key: str,
) -> dict[str, np.ndarray]:
    shape = F06.TARGET_SHAPE
    best_rating = np.full(shape, -np.inf, dtype=np.float32)
    second_rating = np.full(shape, -np.inf, dtype=np.float32)
    best_vza = np.full(shape, np.inf, dtype=np.float32)
    best_dt = np.full(shape, np.inf, dtype=np.float32)
    best_order = np.full(shape, 999, dtype=np.int16)
    source_map = np.zeros(shape, dtype=np.int16)
    best_valid = np.zeros(shape, dtype=bool)

    for cand in candidates:
        valid = np.asarray(cand[valid_key]).astype(bool)
        rating = np.asarray(cand[rating_key], dtype=np.float32)
        eff_vza = np.asarray(cand[vza_key], dtype=np.float32)
        dt = np.full(shape, np.float32(cand["delta_min"] if np.isfinite(cand["delta_min"]) else 999.0), dtype=np.float32)
        order = np.full(shape, cand["order_index"], dtype=np.int16)
        prev_best_rating = best_rating.copy()

        tie_rating = valid & best_valid & np.isclose(rating, best_rating, atol=1e-12, rtol=0.0)
        tie_vza = tie_rating & np.isfinite(eff_vza) & np.isfinite(best_vza) & np.isclose(eff_vza, best_vza, atol=1e-6, rtol=0.0)
        tie_dt = tie_vza & np.isfinite(dt) & np.isfinite(best_dt) & np.isclose(dt, best_dt, atol=1e-6, rtol=0.0)
        replace = valid & (
            (~best_valid)
            | (best_valid & (rating > best_rating + 1e-12))
            | (tie_rating & (eff_vza < best_vza - 1e-6))
            | (tie_vza & (dt < best_dt - 1e-6))
            | (tie_dt & (order < best_order))
        )

        if np.any(replace):
            best_rating[replace] = rating[replace]
            best_vza[replace] = eff_vza[replace]
            best_dt[replace] = dt[replace]
            best_order[replace] = order[replace]
            source_map[replace] = F06.SOURCE_ID_MAP[cand["satellite"]]
            best_valid[replace] = True

        higher = valid & (rating > prev_best_rating)
        if np.any(higher):
            second_rating[higher] = prev_best_rating[higher]
            best_rating[higher] = rating[higher]

        equal = valid & ~higher & np.isclose(rating, prev_best_rating, atol=1e-12, rtol=0.0)
        if np.any(equal):
            second_rating[equal] = np.maximum(second_rating[equal], prev_best_rating[equal])

        better_second = valid & ~higher & ~equal & (rating > second_rating)
        if np.any(better_second):
            second_rating[better_second] = rating[better_second]

    second_rating[~np.isfinite(second_rating)] = np.nan
    best_rating[~best_valid] = np.nan
    best_vza[~best_valid] = np.nan
    margin = best_rating - second_rating
    margin[(~best_valid) | (~np.isfinite(second_rating))] = np.nan
    return {
        "source_map": source_map,
        "best_rating": best_rating,
        "second_rating": second_rating,
        "best_vza": best_vza,
        "valid": best_valid.astype(np.uint8),
        "margin": margin.astype(np.float32),
    }


def select_min_vza_source(candidates: list[dict[str, Any]], valid_key: str, vza_key: str) -> np.ndarray:
    shape = F06.TARGET_SHAPE
    best_vza = np.full(shape, np.inf, dtype=np.float32)
    best_dt = np.full(shape, np.inf, dtype=np.float32)
    best_order = np.full(shape, 999, dtype=np.int16)
    source_map = np.zeros(shape, dtype=np.int16)
    valid_any = np.zeros(shape, dtype=bool)
    for cand in candidates:
        valid = np.asarray(cand[valid_key]).astype(bool)
        eff_vza = np.asarray(cand[vza_key], dtype=np.float32)
        dt = np.full(shape, np.float32(cand["delta_min"] if np.isfinite(cand["delta_min"]) else 999.0), dtype=np.float32)
        order = np.full(shape, cand["order_index"], dtype=np.int16)
        replace = valid & (
            (~valid_any)
            | (eff_vza < best_vza - 1e-6)
            | (np.isclose(eff_vza, best_vza, atol=1e-6, rtol=0.0) & (dt < best_dt - 1e-6))
            | (
                np.isclose(eff_vza, best_vza, atol=1e-6, rtol=0.0)
                & np.isclose(dt, best_dt, atol=1e-6, rtol=0.0)
                & (order < best_order)
            )
        )
        if np.any(replace):
            best_vza[replace] = eff_vza[replace]
            best_dt[replace] = dt[replace]
            best_order[replace] = order[replace]
            source_map[replace] = F06.SOURCE_ID_MAP[cand["satellite"]]
            valid_any[replace] = True
    return source_map


def boundary_mask(source_map: np.ndarray, valid_mask: np.ndarray) -> np.ndarray:
    src = np.asarray(source_map)
    valid = np.asarray(valid_mask).astype(bool)
    out = np.zeros(src.shape, dtype=bool)
    out[1:, :] |= valid[1:, :] & valid[:-1, :] & (src[1:, :] != src[:-1, :])
    out[:-1, :] |= valid[:-1, :] & valid[1:, :] & (src[:-1, :] != src[1:, :])
    out[:, 1:] |= valid[:, 1:] & valid[:, :-1] & (src[:, 1:] != src[:, :-1])
    out[:, :-1] |= valid[:, :-1] & valid[:, 1:] & (src[:, :-1] != src[:, 1:])
    return out


def build_candidates(variable: str, catalog: dict[tuple[str, str, str], Path], lon2d: np.ndarray, lat2d: np.ndarray, subpoints: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for rule in F06.VARIABLE_RULES[variable]:
        sat = rule["satellite"]
        product = rule["product"]
        path = catalog.get((sat, product, variable))
        if path is None:
            continue
        meta, arrays = F06.load_npz_payload(path)
        data = np.asarray(arrays["data"])
        if data.shape != F06.TARGET_SHAPE:
            continue
        valid_native = F06.candidate_valid_mask(variable, arrays)
        time_w, _, delta_min = F06.time_weight(meta)

        direct_result = F06.load_sensor_zenith(catalog, sat, product)
        direct_vza, direct_note = direct_result[0], direct_result[1]
        direct_source_level = direct_result[2] if len(direct_result) > 2 else ""
        has_direct_vza = direct_vza is not None
        if has_direct_vza:
            legacy_vza = np.asarray(direct_vza, dtype=np.float32)
            legacy_view_weight, _ = F06.view_weight_from_vza(direct_vza, valid_native)
        else:
            legacy_vza = np.full(F06.TARGET_SHAPE, np.float32(APPROX_EQUIVALENT_VZA_DEG), dtype=np.float32)
            legacy_view_weight = np.zeros(F06.TARGET_SHAPE, dtype=np.float32)
            legacy_view_weight[valid_native] = APPROX_WEIGHT

        sat_lon = subpoints[sat]["subpoint_lon_deg"]
        unified_vza = approximate_geostationary_vza(lat2d, lon2d, sat_lon)
        valid_unified = valid_native & np.isfinite(unified_vza)
        unified_view_weight = view_weight_from_vza(unified_vza, valid_unified)

        plw = F06.product_level_weight(variable, sat, product)
        legacy_rating = legacy_view_weight * np.float32(time_w) * np.float32(plw)
        unified_rating = unified_view_weight * np.float32(time_w) * np.float32(plw)

        candidates.append(
            {
                "satellite": sat,
                "product": product,
                "variable": variable,
                "path": path,
                "meta": meta,
                "data": data,
                "delta_min": delta_min,
                "order_index": F06.TIE_ORDER.index(sat),
                "has_direct_vza": has_direct_vza,
                "direct_note": direct_note or "",
                "direct_source_level": direct_source_level or "",
                "valid_legacy": valid_native,
                "legacy_vza": legacy_vza,
                "legacy_rating": legacy_rating,
                "valid_current": valid_unified,
                "current_vza": unified_vza,
                "current_rating": unified_rating,
                "valid_unified": valid_unified,
                "unified_vza": unified_vza,
                "unified_rating": unified_rating,
                "subpoint_lon_deg": sat_lon,
            }
        )
    return candidates


def save_npz(path: Path, data: np.ndarray, valid_mask: np.ndarray, metadata: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        data=np.asarray(data),
        valid_mask=np.asarray(valid_mask, dtype=np.uint8),
        metadata_json=np.asarray(json.dumps(metadata, ensure_ascii=False, default=str)),
    )


def run_variable(variable: str, catalog: dict[tuple[str, str, str], Path], target_grid: dict[str, Any], lon2d: np.ndarray, lat2d: np.ndarray, subpoints: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    current_source_npz = FUSED_DIR / f"source_map_{variable}.npz"
    current_rating_npz = FUSED_DIR / f"rating_map_{variable}.npz"
    if not current_source_npz.exists() or not current_rating_npz.exists():
        raise RuntimeError(f"missing fused outputs for {variable}")

    current_source_saved = np.asarray(np.load(current_source_npz, allow_pickle=False)["data"], dtype=np.int16)
    current_rating_saved = np.asarray(np.load(current_rating_npz, allow_pickle=False)["data"], dtype=np.float32)

    candidates = build_candidates(variable, catalog, lon2d, lat2d, subpoints)
    if not candidates:
        raise RuntimeError(f"no diagnostic candidates for {variable}")

    current_diag = select_by_rating(candidates, "current", "valid_current", "current_rating", "current_vza")
    legacy_diag = select_by_rating(candidates, "legacy", "valid_legacy", "legacy_rating", "legacy_vza")
    unified_diag = select_by_rating(candidates, "unified", "valid_unified", "unified_rating", "unified_vza")
    min_vza_source = select_min_vza_source(candidates, "valid_current", "current_vza")

    valid_current = current_diag["valid"].astype(bool)
    valid_unified = unified_diag["valid"].astype(bool)
    current_agree_saved = valid_current & (current_diag["source_map"] == current_source_saved)
    rating_diff = np.abs(current_diag["best_rating"] - current_rating_saved)
    rating_diff[~valid_current] = np.nan

    agreement_rows = [
        {
            "variable": variable,
            "current_vs_saved_source_agreement": float(np.count_nonzero(current_agree_saved) / np.count_nonzero(valid_current)) if np.count_nonzero(valid_current) else np.nan,
            "current_vs_saved_rating_max_abs_diff": float(np.nanmax(rating_diff)) if np.isfinite(rating_diff).any() else np.nan,
            "selected_vs_min_current_vza_agreement": float(np.count_nonzero(valid_current & (current_source_saved == min_vza_source)) / np.count_nonzero(valid_current)) if np.count_nonzero(valid_current) else np.nan,
            "valid_pixels": int(np.count_nonzero(valid_current)),
        }
    ]

    selected_direct = np.zeros(F06.TARGET_SHAPE, dtype=bool)
    for cand in candidates:
        if cand["has_direct_vza"]:
            selected_direct |= valid_current & (current_source_saved == F06.SOURCE_ID_MAP[cand["satellite"]])
    legacy_overlap = valid_current & legacy_diag["valid"].astype(bool)
    legacy_mixed_changed = legacy_overlap & (current_source_saved != legacy_diag["source_map"])

    geometry_rows = [
        {
            "variable": variable,
            "selected_has_native_direct_vza_fraction": float(np.count_nonzero(selected_direct) / np.count_nonzero(valid_current)) if np.count_nonzero(valid_current) else np.nan,
            "selected_without_native_direct_vza_fraction": float(np.count_nonzero(valid_current & ~selected_direct) / np.count_nonzero(valid_current)) if np.count_nonzero(valid_current) else np.nan,
            "legacy_mixed_diff_fraction": float(np.count_nonzero(legacy_mixed_changed) / np.count_nonzero(legacy_overlap)) if np.count_nonzero(legacy_overlap) else np.nan,
            "legacy_mixed_same_fraction": float(np.count_nonzero(legacy_overlap & ~legacy_mixed_changed) / np.count_nonzero(legacy_overlap)) if np.count_nonzero(legacy_overlap) else np.nan,
            "selected_direct_fraction": float(np.count_nonzero(selected_direct) / np.count_nonzero(valid_current)) if np.count_nonzero(valid_current) else np.nan,
            "selected_approx_fraction": float(np.count_nonzero(valid_current & ~selected_direct) / np.count_nonzero(valid_current)) if np.count_nonzero(valid_current) else np.nan,
            "real_vza_decisive_fraction": float(np.count_nonzero(legacy_mixed_changed) / np.count_nonzero(legacy_overlap)) if np.count_nonzero(legacy_overlap) else np.nan,
            "approx_or_tie_fraction": float(np.count_nonzero(legacy_overlap & ~legacy_mixed_changed) / np.count_nonzero(legacy_overlap)) if np.count_nonzero(legacy_overlap) else np.nan,
        }
    ]

    margin = current_diag["margin"]
    boundary = boundary_mask(current_source_saved, valid_current)
    interior = valid_current & ~boundary
    margin_rows = [
        {
            "variable": variable,
            "best_rating_mean": float(np.nanmean(current_diag["best_rating"])) if np.isfinite(current_diag["best_rating"]).any() else np.nan,
            "second_best_rating_mean": float(np.nanmean(current_diag["second_rating"])) if np.isfinite(current_diag["second_rating"]).any() else np.nan,
            "margin_mean": float(np.nanmean(margin)) if np.isfinite(margin).any() else np.nan,
            "margin_median": float(np.nanmedian(margin)) if np.isfinite(margin).any() else np.nan,
            "margin_p05": float(np.nanpercentile(margin, 5)) if np.isfinite(margin).any() else np.nan,
            "margin_p25": float(np.nanpercentile(margin, 25)) if np.isfinite(margin).any() else np.nan,
        }
    ]
    boundary_rows = [
        {
            "variable": variable,
            "boundary_fraction": float(np.count_nonzero(boundary) / np.count_nonzero(valid_current)) if np.count_nonzero(valid_current) else np.nan,
            "boundary_margin_mean": float(np.nanmean(margin[boundary])) if np.count_nonzero(boundary & np.isfinite(margin)) else np.nan,
            "interior_margin_mean": float(np.nanmean(margin[interior])) if np.count_nonzero(interior & np.isfinite(margin)) else np.nan,
            "boundary_small_margin_le_0p01": float(np.count_nonzero(boundary & np.isfinite(margin) & (margin <= 0.01)) / np.count_nonzero(boundary)) if np.count_nonzero(boundary) else np.nan,
            "boundary_small_margin_le_0p05": float(np.count_nonzero(boundary & np.isfinite(margin) & (margin <= 0.05)) / np.count_nonzero(boundary)) if np.count_nonzero(boundary) else np.nan,
            "boundary_small_margin_le_0p10": float(np.count_nonzero(boundary & np.isfinite(margin) & (margin <= 0.10)) / np.count_nonzero(boundary)) if np.count_nonzero(boundary) else np.nan,
        }
    ]

    unified_change = valid_current & valid_unified & (current_source_saved != unified_diag["source_map"])
    legacy_rows = [
        {
            "variable": variable,
            "current_coverage_ratio": float(np.count_nonzero(valid_current) / valid_current.size),
            "legacy_coverage_ratio": float(np.count_nonzero(legacy_diag["valid"]) / legacy_diag["valid"].size),
            "changed_fraction_on_overlap_valid": float(np.count_nonzero(legacy_mixed_changed) / np.count_nonzero(legacy_overlap)) if np.count_nonzero(legacy_overlap) else np.nan,
            "changed_fraction_on_current_valid": float(np.count_nonzero(legacy_mixed_changed) / np.count_nonzero(valid_current)) if np.count_nonzero(valid_current) else np.nan,
        }
    ]
    unified_rows = [
        {
            "variable": variable,
            "current_coverage_ratio": float(np.count_nonzero(valid_current) / valid_current.size),
            "unified_coverage_ratio": float(np.count_nonzero(valid_unified) / valid_unified.size),
            "changed_fraction_on_current_valid": float(np.count_nonzero(unified_change) / np.count_nonzero(valid_current)) if np.count_nonzero(valid_current) else np.nan,
            "changed_fraction_on_overlap_valid": float(np.count_nonzero(unified_change) / np.count_nonzero(valid_current & valid_unified)) if np.count_nonzero(valid_current & valid_unified) else np.nan,
        }
    ]

    transition_rows: list[dict[str, Any]] = []
    if np.count_nonzero(valid_current & valid_unified):
        pairs = np.column_stack([current_source_saved[valid_current & valid_unified], unified_diag["source_map"][valid_current & valid_unified]])
        unique_pairs, counts = np.unique(pairs, axis=0, return_counts=True)
        for pair, count in zip(unique_pairs, counts):
            transition_rows.append(
                {
                    "variable": variable,
                    "from_source_id": int(pair[0]),
                    "from_satellite": next((k for k, v in F06.SOURCE_ID_MAP.items() if v == int(pair[0])), ""),
                    "to_source_id": int(pair[1]),
                    "to_satellite": next((k for k, v in F06.SOURCE_ID_MAP.items() if v == int(pair[1])), ""),
                    "pixel_count": int(count),
                }
            )
    legacy_transition_rows: list[dict[str, Any]] = []
    if np.count_nonzero(legacy_overlap):
        legacy_pairs = np.column_stack([legacy_diag["source_map"][legacy_overlap], current_source_saved[legacy_overlap]])
        unique_pairs, counts = np.unique(legacy_pairs, axis=0, return_counts=True)
        for pair, count in zip(unique_pairs, counts):
            legacy_transition_rows.append(
                {
                    "variable": variable,
                    "from_source_id": int(pair[0]),
                    "from_satellite": next((k for k, v in F06.SOURCE_ID_MAP.items() if v == int(pair[0])), ""),
                    "to_source_id": int(pair[1]),
                    "to_satellite": next((k for k, v in F06.SOURCE_ID_MAP.items() if v == int(pair[1])), ""),
                    "pixel_count": int(count),
                }
            )

    driver_map = np.full(F06.TARGET_SHAPE, np.nan, dtype=np.float32)
    driver_map[valid_current & ~selected_direct] = 1.0
    driver_map[selected_direct] = 2.0
    driver_map[legacy_mixed_changed] = 3.0

    meta = {
        "generated_utc": utc_now(),
        "target_time": TARGET_TIME,
        "variable": variable,
        "target_grid": target_grid,
    }
    save_npz(OUT_DIR / f"min_current_vza_source_map_{variable}.npz", min_vza_source.astype(np.int16), valid_current, {**meta, "artifact": "min_current_vza_source_map"})
    save_npz(OUT_DIR / f"second_best_rating_{variable}.npz", current_diag["second_rating"].astype(np.float32), valid_current, {**meta, "artifact": "second_best_rating"})
    save_npz(OUT_DIR / f"rating_margin_{variable}.npz", margin.astype(np.float32), valid_current, {**meta, "artifact": "rating_margin"})
    save_npz(OUT_DIR / f"boundary_mask_{variable}.npz", boundary.astype(np.uint8), valid_current, {**meta, "artifact": "boundary_mask"})
    save_npz(OUT_DIR / f"geometry_driver_{variable}.npz", driver_map.astype(np.float32), valid_current, {**meta, "artifact": "geometry_driver"})
    save_npz(OUT_DIR / f"unified_vza_source_map_{variable}.npz", unified_diag["source_map"].astype(np.int16), valid_unified, {**meta, "artifact": "unified_vza_source_map"})
    save_npz(OUT_DIR / f"unified_vza_changed_mask_{variable}.npz", unified_change.astype(np.uint8), valid_current & valid_unified, {**meta, "artifact": "unified_vza_changed_mask"})

    labels = {v: k for k, v in F06.SOURCE_ID_MAP.items()}
    F06.make_quicklook(min_vza_source.astype(np.int16), valid_current, QUICKLOOK_DIR / f"min_current_vza_source_map_{variable}.png", f"min-current-vza source_map {variable} {TARGET_TIME}", f"source_map_{variable}", labels=labels)
    F06.make_quicklook(margin.astype(np.float32), valid_current & np.isfinite(margin), QUICKLOOK_DIR / f"rating_margin_{variable}.png", f"rating margin {variable} {TARGET_TIME}", f"rating_margin_{variable}")
    F06.make_quicklook(boundary.astype(np.int16), valid_current, QUICKLOOK_DIR / f"boundary_mask_{variable}.png", f"boundary mask {variable} {TARGET_TIME}", f"valid_count_{variable}", labels={0: "interior", 1: "boundary"})
    F06.make_quicklook(driver_map.astype(np.float32), valid_current, QUICKLOOK_DIR / f"geometry_driver_{variable}.png", f"geometry driver {variable} {TARGET_TIME}", f"source_map_{variable}", labels={1: "no_native_direct_vza", 2: "native_direct_vza_available", 3: "changed_vs_legacy_mixed"})
    F06.make_quicklook(unified_diag["source_map"].astype(np.int16), valid_unified, QUICKLOOK_DIR / f"unified_vza_source_map_{variable}.png", f"unified-vza source_map {variable} {TARGET_TIME}", f"source_map_{variable}", labels=labels)
    F06.make_quicklook(unified_change.astype(np.int16), valid_current & valid_unified, QUICKLOOK_DIR / f"unified_vza_changed_mask_{variable}.png", f"unified-vza changed mask {variable} {TARGET_TIME}", f"valid_count_{variable}", labels={0: "same", 1: "changed"})

    return agreement_rows, geometry_rows, margin_rows + boundary_rows, transition_rows, unified_rows, legacy_rows, legacy_transition_rows


def write_report(
    agreement_df: pd.DataFrame,
    geometry_df: pd.DataFrame,
    margin_df: pd.DataFrame,
    boundary_df: pd.DataFrame,
    unified_df: pd.DataFrame,
    transition_df: pd.DataFrame,
    subpoint_df: pd.DataFrame,
    legacy_df: pd.DataFrame | None = None,
    legacy_transition_df: pd.DataFrame | None = None,
) -> None:
    clean_lines = [
        "# 06.5 source-selection diagnostics 报告",
        "",
        f"- 生成时间 UTC: {utc_now()}",
        f"- 目标时次: `{TARGET_TIME}`",
        "- 本步骤不改变 06 的融合结果，只解释 source map 为什么这样。",
        "",
        "## 1. selected source 是否基本等于 min-VZA source",
        "",
    ]
    for row in agreement_df.itertuples():
        clean_lines.append(
            f"- {row.variable}: current_vs_saved_source_agreement={row.current_vs_saved_source_agreement:.4f}, "
            f"selected_vs_min_current_vza_agreement={row.selected_vs_min_current_vza_agreement:.4f}"
        )
    clean_lines.extend(["", "## 2. 哪些区域由真实几何诊断支持，哪些区域没有 native direct VZA", ""])
    for row in geometry_df.itertuples():
        clean_lines.append(
            f"- {row.variable}: selected_has_native_direct_vza_fraction={row.selected_has_native_direct_vza_fraction:.4f}, "
            f"selected_without_native_direct_vza_fraction={row.selected_without_native_direct_vza_fraction:.4f}, "
            f"legacy_mixed_diff_fraction={row.legacy_mixed_diff_fraction:.4f}"
        )
    clean_lines.extend(["", "## 3. best rating 与 second-best rating 的差值", ""])
    for row in margin_df.itertuples():
        clean_lines.append(
            f"- {row.variable}: margin_mean={row.margin_mean:.4f}, margin_median={row.margin_median:.4f}, "
            f"margin_p05={row.margin_p05:.4f}, margin_p25={row.margin_p25:.4f}"
        )
    clean_lines.extend(["", "## 4. source 边界是否主要出现在小 rating margin 区域", ""])
    for row in boundary_df.itertuples():
        clean_lines.append(
            f"- {row.variable}: boundary_fraction={row.boundary_fraction:.4f}, "
            f"boundary_margin_mean={row.boundary_margin_mean:.4f}, interior_margin_mean={row.interior_margin_mean:.4f}, "
            f"boundary_small_margin<=0.05={row.boundary_small_margin_le_0p05:.4f}"
        )
    clean_lines.extend(["", "## 5. 如果所有卫星都用统一近似 VZA，source map 是否还会改变", ""])
    for row in unified_df.itertuples():
        clean_lines.append(
            f"- {row.variable}: changed_fraction_on_current_valid={row.changed_fraction_on_current_valid:.4f}, "
            f"changed_fraction_on_overlap_valid={row.changed_fraction_on_overlap_valid:.4f}"
        )
    if legacy_df is not None and not legacy_df.empty:
        clean_lines.extend(["", "## 5.5 与旧 mixed-geometry 方案相比，06b patch 实际改动了多少 source selection", ""])
        for row in legacy_df.itertuples():
            clean_lines.append(
                f"- {row.variable}: changed_fraction_on_current_valid={row.changed_fraction_on_current_valid:.4f}, "
                f"changed_fraction_on_overlap_valid={row.changed_fraction_on_overlap_valid:.4f}"
            )
    clean_lines.extend(["", "## 星下点经度假设", ""])
    for row in subpoint_df.itertuples():
        clean_lines.append(f"- {row.satellite}: subpoint_lon_deg={row.subpoint_lon_deg:.3f}, method={row.method}")
    if legacy_transition_df is not None and not legacy_transition_df.empty:
        clean_lines.extend(["", "## 与旧 mixed-geometry 相比最显著的 source 转换", ""])
        top = legacy_transition_df.sort_values("pixel_count", ascending=False).head(20)
        for row in top.itertuples():
            clean_lines.append(f"- {row.variable}: {row.from_satellite} -> {row.to_satellite}, pixels={row.pixel_count}")
    clean_lines.extend(
        [
            "",
            "## 输出文件",
            "",
            f"- `{SELECTED_VS_MIN_VZA_CSV}`",
            f"- `{GEOMETRY_DRIVER_CSV}`",
            f"- `{RATING_MARGIN_CSV}`",
            f"- `{BOUNDARY_MARGIN_CSV}`",
            f"- `{UNIFIED_CHANGE_CSV}`",
            f"- `{TRANSITION_CSV}`",
            f"- `{LEGACY_CHANGE_CSV}`",
            f"- `{LEGACY_TRANSITION_CSV}`",
            f"- `{SUBPOINT_CSV}`",
            f"- `{OUT_DIR}`",
            f"- `{QUICKLOOK_DIR}`",
        ]
    )
    REPORT_MD.write_text("\n".join(clean_lines), encoding="utf-8")
    return

    lines = [
        "# 06.5 source-selection diagnostics 报告",
        "",
        f"- 生成时间 UTC: {utc_now()}",
        f"- 目标时次: `{TARGET_TIME}`",
        "- 本步骤不改变 06 的融合结果，只解释 source map 为何如此。",
        "",
        "## 1. selected source 是否基本等于 min-VZA source",
        "",
    ]
    for row in agreement_df.itertuples():
        lines.append(
            f"- {row.variable}: current_vs_saved_source_agreement={row.current_vs_saved_source_agreement:.4f}, "
            f"selected_vs_min_current_vza_agreement={row.selected_vs_min_current_vza_agreement:.4f}"
        )
    lines.extend(["", "## 2. 哪些区域由真实 VZA 决定，哪些由 VIEW_WEIGHT_APPROX=0.8 决定", ""])
    for row in geometry_df.itertuples():
        lines.append(
            f"- {row.variable}: selected_direct_fraction={row.selected_direct_fraction:.4f}, "
            f"selected_approx_fraction={row.selected_approx_fraction:.4f}, "
            f"real_vza_decisive_fraction={row.real_vza_decisive_fraction:.4f}"
        )
    lines.extend(["", "## 3. best rating 与 second-best rating 的差值", ""])
    for row in margin_df.itertuples():
        lines.append(
            f"- {row.variable}: margin_mean={row.margin_mean:.4f}, margin_median={row.margin_median:.4f}, "
            f"margin_p05={row.margin_p05:.4f}, margin_p25={row.margin_p25:.4f}"
        )
    lines.extend(["", "## 4. source 边界是否主要出现在小 rating margin 区域", ""])
    for row in boundary_df.itertuples():
        lines.append(
            f"- {row.variable}: boundary_fraction={row.boundary_fraction:.4f}, "
            f"boundary_margin_mean={row.boundary_margin_mean:.4f}, interior_margin_mean={row.interior_margin_mean:.4f}, "
            f"boundary_small_margin<=0.05={row.boundary_small_margin_le_0p05:.4f}"
        )
    lines.extend(["", "## 5. 若所有卫星都使用统一几何公式估算 VZA，source map 是否明显改变", ""])
    for row in unified_df.itertuples():
        lines.append(
            f"- {row.variable}: changed_fraction_on_current_valid={row.changed_fraction_on_current_valid:.4f}, "
            f"changed_fraction_on_overlap_valid={row.changed_fraction_on_overlap_valid:.4f}"
        )
    lines.extend(["", "## 星下点经度假设", ""])
    for row in subpoint_df.itertuples():
        lines.append(f"- {row.satellite}: subpoint_lon_deg={row.subpoint_lon_deg:.3f}, method={row.method}")
    if not transition_df.empty:
        lines.extend(["", "## unified VZA 变化最显著的 source 转换", ""])
        top = transition_df.sort_values("pixel_count", ascending=False).head(20)
        for row in top.itertuples():
            lines.append(f"- {row.variable}: {row.from_satellite} -> {row.to_satellite}, pixels={row.pixel_count}")
    lines.extend(
        [
            "",
            "## 输出文件",
            "",
            f"- `{SELECTED_VS_MIN_VZA_CSV}`",
            f"- `{GEOMETRY_DRIVER_CSV}`",
            f"- `{RATING_MARGIN_CSV}`",
            f"- `{BOUNDARY_MARGIN_CSV}`",
            f"- `{UNIFIED_CHANGE_CSV}`",
            f"- `{TRANSITION_CSV}`",
            f"- `{SUBPOINT_CSV}`",
            f"- `{OUT_DIR}`",
            f"- `{QUICKLOOK_DIR}`",
        ]
    )
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ensure_dirs()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    QUICKLOOK_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(__file__, SCRIPT_DIR / Path(__file__).name)

    catalog = F06.load_catalog()
    target_grid = F06.target_grid_from_any(catalog)
    target_lon, target_lat = build_target_lon_lat(target_grid)
    lon2d, lat2d = np.meshgrid(target_lon, target_lat)

    subpoint_rows = []
    subpoints: dict[str, dict[str, Any]] = {}
    for satellite in F06.TIE_ORDER:
        lon0, method = infer_subpoint_longitude(satellite, catalog)
        subpoints[satellite] = {"subpoint_lon_deg": lon0, "method": method}
        subpoint_rows.append({"satellite": satellite, "subpoint_lon_deg": lon0, "method": method})
    subpoint_df = pd.DataFrame(subpoint_rows)
    subpoint_df.to_csv(SUBPOINT_CSV, index=False, encoding="utf-8-sig")

    agreement_rows: list[dict[str, Any]] = []
    geometry_rows: list[dict[str, Any]] = []
    margin_rows_all: list[dict[str, Any]] = []
    boundary_rows_all: list[dict[str, Any]] = []
    transition_rows: list[dict[str, Any]] = []
    unified_rows: list[dict[str, Any]] = []
    legacy_rows_all: list[dict[str, Any]] = []
    legacy_transition_rows_all: list[dict[str, Any]] = []

    for variable in F06.VARIABLE_RULES:
        agreement, geometry, margin_boundary, transitions, unified_rows_var, legacy_rows_var, legacy_transitions = run_variable(variable, catalog, target_grid, lon2d, lat2d, subpoints)
        agreement_rows.extend(agreement)
        geometry_rows.extend(geometry)
        margin_rows_all.append(margin_boundary[0])
        boundary_rows_all.append(margin_boundary[1])
        transition_rows.extend(transitions)
        unified_rows.extend(unified_rows_var)
        legacy_rows_all.extend(legacy_rows_var)
        legacy_transition_rows_all.extend(legacy_transitions)

    agreement_df = pd.DataFrame(agreement_rows)
    geometry_df = pd.DataFrame(geometry_rows)
    margin_df = pd.DataFrame(margin_rows_all)
    boundary_df = pd.DataFrame(boundary_rows_all)
    unified_df = pd.DataFrame(unified_rows)
    transition_df = pd.DataFrame(transition_rows)
    legacy_df = pd.DataFrame(legacy_rows_all)
    legacy_transition_df = pd.DataFrame(legacy_transition_rows_all)

    agreement_df.to_csv(SELECTED_VS_MIN_VZA_CSV, index=False, encoding="utf-8-sig")
    geometry_df.to_csv(GEOMETRY_DRIVER_CSV, index=False, encoding="utf-8-sig")
    margin_df.to_csv(RATING_MARGIN_CSV, index=False, encoding="utf-8-sig")
    boundary_df.to_csv(BOUNDARY_MARGIN_CSV, index=False, encoding="utf-8-sig")
    unified_df.to_csv(UNIFIED_CHANGE_CSV, index=False, encoding="utf-8-sig")
    transition_df.to_csv(TRANSITION_CSV, index=False, encoding="utf-8-sig")
    legacy_df.to_csv(LEGACY_CHANGE_CSV, index=False, encoding="utf-8-sig")
    legacy_transition_df.to_csv(LEGACY_TRANSITION_CSV, index=False, encoding="utf-8-sig")

    write_report(agreement_df, geometry_df, margin_df, boundary_df, unified_df, transition_df, subpoint_df, legacy_df, legacy_transition_df)
    print(f"06.5 DONE: variables={len(F06.VARIABLE_RULES)}")
    print(f"report={REPORT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
