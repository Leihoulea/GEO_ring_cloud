# -*- coding: utf-8 -*-
"""Stage 09H Gate 4A Meteosat navigation exact mapping and fix design.

Read-only diagnostic. This gate does not modify the Meteosat production reader,
does not rotate cloud_mask values, and does not rerun a full March batch.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import inspect
import json
import math
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from pyproj import Geod
from scipy.ndimage import distance_transform_edt

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from geo_ring_cloud import paths as path_config  # noqa: E402
from geo_ring_cloud.adapters import cloud_products  # noqa: E402
from geo_ring_cloud.adapters.cloud_products import reshape_square_if_needed  # noqa: E402
from geo_ring_cloud.cloud_semantics import cloud_mask_masks  # noqa: E402
from geo_ring_cloud.diagnostics import full_pixel  # noqa: E402
from geo_ring_cloud.diagnostics.full_pixel_workflow import load_manifest  # noqa: E402
from geo_ring_cloud.reprojection import build_tree, normalize_longitude, query_reproject  # noqa: E402

PROJECT_ID = "geo_ring_cloud"
STAGE_ID = "stage_09h"
RUN_ID = "stage_09h_meteosat_mask_navigation_root_cause_202403"
GATE_ID = "gate4a_navigation_exact_mapping_fix_design"
SOURCE = "Meteosat-0deg"
PRODUCT = "CLM"
L15_PLATFORM = "Meteosat-10"
CHANNEL = "IR_108"
POLICY_NAME = "A_inclusive_binary"
OUT_ROOT = path_config.RUNS_ROOT / RUN_ID
DATA_ROOT = path_config.THIRD_REPORT_ROOT / "Satellite_Data_20240312"
L15_ROOT = DATA_ROOT / L15_PLATFORM
CLM_ROOT = path_config.EXTERNAL_GEO_CLOUD_ROOT / SOURCE / "CLM" / "20240312"
STAGE09D_DIR = path_config.RUNS_ROOT / "stage09d_full_pixel_diagnostics_202403"
GEOD = Geod(a=6378169.0, rf=295.488065897014)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_dirs(root: Path) -> dict[str, Path]:
    dirs = {
        "source_data": root / "source_data",
        "figures": root / "figures",
        "reports": root / "reports",
        "logs": root / "logs",
        "cache": root / "cache" / GATE_ID,
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def write_csv(rows: list[dict[str, Any]] | pd.DataFrame, path: Path) -> Path:
    df = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def write_json(obj: Any, path: Path) -> Path:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str), encoding="utf-8-sig")
    return path


def dataframe_to_markdown(df: pd.DataFrame, floatfmt: str = ".4f") -> str:
    if df.empty:
        return ""
    cols = [str(col) for col in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for _, row in df.iterrows():
        cells: list[str] = []
        for col in df.columns:
            value = row[col]
            if isinstance(value, (float, np.floating)):
                cells.append("" if not np.isfinite(value) else format(float(value), floatfmt))
            else:
                cells.append(str(value))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def warn(warnings: list[dict[str, Any]], code: str, message: str, severity: str = "WARN", **extra: Any) -> None:
    row = {
        "timestamp_utc": utc_now(),
        "stage_id": STAGE_ID,
        "gate_id": GATE_ID,
        "severity": severity,
        "warning_code": code,
        "message": message,
    }
    row.update(extra)
    warnings.append(row)


def parse_hour(value: str) -> int:
    return int(str(value).split("_")[-1][:2])


def parse_l15_time(path: Path) -> datetime | None:
    match = re.search(r"-(\d{8})(\d{6})\.\d+Z-NA(?:\.nat)?$", path.name)
    if not match:
        return None
    return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def nominal_cycle_from_l15(path: Path) -> datetime | None:
    ts = parse_l15_time(path)
    if ts is None:
        return None
    return ts.replace(minute=0, second=0, microsecond=0)


def parse_clm_nominal(path: Path) -> datetime | None:
    match = re.search(r"-(\d{8})(\d{6})\.\d+Z-NA\.zip$", path.name)
    if not match:
        return None
    return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def find_l15_native(hour: int) -> Path:
    hits = sorted(L15_ROOT.rglob(f"*20240312{hour:02d}*.nat"))
    hits = [path for path in hits if path.parent.name == path.stem]
    if not hits:
        raise FileNotFoundError(f"missing {L15_PLATFORM} L1.5 native file for 2024-03-12 {hour:02d}:00")
    return hits[0]


def find_clm_zip(hour: int) -> Path:
    path = CLM_ROOT / f"{hour:02d}" / f"MSG3-SEVI-MSGCLMK-0100-0100-20240312{hour:02d}0000.000000000Z-NA.zip"
    if path.exists():
        return path
    hits = sorted(CLM_ROOT.rglob(f"*20240312{hour:02d}0000*.zip"))
    if not hits:
        raise FileNotFoundError(f"missing Meteosat-0deg CLM ZIP for 2024-03-12 {hour:02d}:00")
    return hits[0]


def configure_eccodes_library(warnings: list[dict[str, Any]]) -> None:
    if sys.platform != "win32":
        return
    dll = Path(sys.prefix) / "Library" / "bin" / "eccodes.dll"
    if not dll.exists():
        warn(warnings, "eccodes_dll_missing", f"ecCodes DLL not found at {dll}")
        return
    try:
        import findlibs

        original_find = findlibs.find

        def patched_find(name: str) -> str | None:
            if name == "eccodes":
                return str(dll)
            return original_find(name)

        findlibs.find = patched_find
    except Exception as exc:  # pragma: no cover - environment-specific
        warn(warnings, "eccodes_findlibs_patch_failed", str(exc))


def read_clm_raw_zip(path: Path, cache_dir: Path, warnings: list[dict[str, Any]]) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    configure_eccodes_library(warnings)
    import xarray as xr

    meta: dict[str, Any] = {"source_path": str(path), "reader": "zip+xarray_cfgrib_raw_values+C_order_reshape"}
    with zipfile.ZipFile(path) as zf:
        entries = zf.namelist()
        grib_entries = [entry for entry in entries if entry.lower().endswith((".grb", ".grib", ".grb2", ".bin"))]
        if not grib_entries:
            raise RuntimeError(f"No GRIB entry found in {path}")
        entry = grib_entries[0]
        payload = zf.read(entry)
    cache_key = hashlib.sha1(f"{path.resolve()}|{entry}".encode("utf-8")).hexdigest()
    extracted = cache_dir / f"{cache_key}{Path(entry).suffix or '.grb'}"
    if not extracted.exists() or extracted.stat().st_size != len(payload):
        extracted.write_bytes(payload)
    ds = xr.open_dataset(extracted, engine="cfgrib", backend_kwargs={"indexpath": ""})
    try:
        var_name = "p260537" if "p260537" in ds.data_vars else list(ds.data_vars)[0]
        mask_raw = np.asarray(ds[var_name].values, dtype=np.float32)
        lat_raw = np.asarray(ds["latitude"].values, dtype=np.float32)
        lon_raw = np.asarray(ds["longitude"].values, dtype=np.float32)
        mask = reshape_square_if_needed(mask_raw)
        lat = reshape_square_if_needed(lat_raw)
        lon = reshape_square_if_needed(lon_raw)
        attrs = {f"ds.{k}": str(v) for k, v in ds.attrs.items()}
        attrs.update({f"var.{k}": str(v) for k, v in ds[var_name].attrs.items()})
        meta.update(
            {
                "selected_grib_entry": entry,
                "selected_grib_cache": str(extracted),
                "raw_variable_name": var_name,
                "raw_dims_json": json.dumps(dict(ds.sizes), ensure_ascii=False),
                "mask_raw_shape": str(mask_raw.shape),
                "latitude_raw_shape": str(lat_raw.shape),
                "longitude_raw_shape": str(lon_raw.shape),
                "mask_shape": str(mask.shape),
                "latitude_shape": str(lat.shape),
                "longitude_shape": str(lon.shape),
                "reshape_method": "identity if already 2D else C-order square reshape",
                "data_vars": ",".join(ds.data_vars),
                "coords": ",".join(ds.coords),
                "dataset_attrs_json": json.dumps({k: str(v) for k, v in ds.attrs.items()}, ensure_ascii=False),
                "variable_attrs_json": json.dumps({k: str(v) for k, v in ds[var_name].attrs.items()}, ensure_ascii=False),
                "navigation_relevant_attrs_json": json.dumps({k: v for k, v in attrs.items() if is_navigation_attr_key(k)}, ensure_ascii=False),
            }
        )
    finally:
        ds.close()
    return {"cloud_mask": mask, "latitude": lat, "longitude": lon}, meta


def reference_navigation_from_l15(path: Path, warnings: list[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    from satpy import Scene

    scene = Scene(filenames=[str(path)], reader="seviri_l1b_native")
    scene.load([CHANNEL], calibration="brightness_temperature")
    da = scene[CHANNEL].reset_coords(drop=True)
    area = da.attrs["area"]
    lon, lat = area.get_lonlats()
    lon = normalize_longitude(np.asarray(lon, dtype=np.float32))
    lat = np.asarray(lat, dtype=np.float32)
    valid = np.isfinite(lon) & np.isfinite(lat) & (lat >= -90.0) & (lat <= 90.0)
    lon[~valid] = np.nan
    lat[~valid] = np.nan
    proj_dict = getattr(area, "proj_dict", {})
    area_extent = getattr(area, "area_extent", None)
    meta = {
        "l15_path": str(path),
        "reader": "satpy:seviri_l1b_native",
        "channel": CHANNEL,
        "shape_y": int(lat.shape[0]),
        "shape_x": int(lat.shape[1]),
        "area_id": str(getattr(area, "area_id", "")),
        "area_shape": str(getattr(area, "shape", "")),
        "area_extent_json": json.dumps(area_extent, ensure_ascii=False, default=str),
        "area_proj_dict_json": json.dumps(proj_dict, ensure_ascii=False, default=str),
        "projection_a_m": proj_dict.get("a", ""),
        "projection_b_m": proj_dict.get("b", ""),
        "projection_rf": proj_dict.get("rf", ""),
        "projection_h_m": proj_dict.get("h", ""),
        "projection_lon_0_deg": proj_dict.get("lon_0", ""),
        "projection_sweep": proj_dict.get("sweep", ""),
        "start_time_utc": str(da.attrs.get("start_time")),
        "end_time_utc": str(da.attrs.get("end_time")),
        "reference_valid_pixels": int(np.count_nonzero(valid)),
        "storage_order": "row0=south, rowlast=north, col0=east, collast=west",
        "pixel_center_convention": "Satpy/pyresample area pixel centers from SEVIRI native area definition",
        "reference_navigation_uses_clm_latlon": False,
    }
    return lon, lat, valid, meta


def is_navigation_attr_key(key: str) -> bool:
    lower = key.lower()
    tokens = [
        "grid",
        "earth",
        "axis",
        "satellite",
        "height",
        "longitude",
        "latitude",
        "scanning",
        "scan",
        "nx",
        "ny",
        "dx",
        "dy",
        "nr",
        "orientation",
        "projection",
        "shape",
    ]
    return any(token in lower for token in tokens)


def circular_lon_diff_deg(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return ((np.asarray(a, dtype=np.float32) - np.asarray(b, dtype=np.float32) + 180.0) % 360.0) - 180.0


def geodesic_distance_km(lon_a: np.ndarray, lat_a: np.ndarray, lon_b: np.ndarray, lat_b: np.ndarray, chunk: int = 1_000_000) -> np.ndarray:
    n = lon_a.size
    out = np.empty(n, dtype=np.float32)
    for start in range(0, n, chunk):
        stop = min(start + chunk, n)
        _, _, dist_m = GEOD.inv(
            lon_a[start:stop].astype(np.float64),
            lat_a[start:stop].astype(np.float64),
            lon_b[start:stop].astype(np.float64),
            lat_b[start:stop].astype(np.float64),
        )
        out[start:stop] = (np.asarray(dist_m, dtype=np.float64) / 1000.0).astype(np.float32)
    return out


def current_nav_valid(lat: np.ndarray, lon: np.ndarray, ref_valid: np.ndarray) -> np.ndarray:
    lon_norm = normalize_longitude(lon)
    finite = np.isfinite(lat) & np.isfinite(lon_norm) & (lat >= -90.0) & (lat <= 90.0)
    not_zero_fill = ~(np.isclose(lat, 0.0) & np.isclose(lon, 0.0))
    return ref_valid & finite & not_zero_fill


def shifted_after_rot180(arr: np.ndarray, dr: int, dc: int, fill: float = np.nan) -> np.ndarray:
    """Return arr[::-1, ::-1] shifted without wrap.

    Output[target_r, target_c] = rot[target_r + dr, target_c + dc].
    Thus the original source index is N - 1 - (target + shift).
    """
    rot = arr[::-1, ::-1]
    out = np.full(rot.shape, fill, dtype=rot.dtype)
    nrow, ncol = rot.shape
    src_r0 = max(0, dr)
    src_r1 = min(nrow, nrow + dr)
    dst_r0 = max(0, -dr)
    dst_r1 = dst_r0 + (src_r1 - src_r0)
    src_c0 = max(0, dc)
    src_c1 = min(ncol, ncol + dc)
    dst_c0 = max(0, -dc)
    dst_c1 = dst_c0 + (src_c1 - src_c0)
    if src_r1 > src_r0 and src_c1 > src_c0:
        out[dst_r0:dst_r1, dst_c0:dst_c1] = rot[src_r0:src_r1, src_c0:src_c1]
    return out


def shift_metrics(
    case_id: str,
    ref_lon: np.ndarray,
    ref_lat: np.ndarray,
    ref_valid: np.ndarray,
    cur_lon: np.ndarray,
    cur_lat: np.ndarray,
    dr: int,
    dc: int,
) -> tuple[dict[str, Any], np.ndarray]:
    lat_t = shifted_after_rot180(cur_lat, dr, dc)
    lon_t = normalize_longitude(shifted_after_rot180(cur_lon, dr, dc))
    valid = current_nav_valid(lat_t, lon_t, ref_valid)
    dist_map = np.full(ref_lat.shape, np.nan, dtype=np.float32)
    row: dict[str, Any] = {
        "case_id": case_id,
        "base_transform": "rot180",
        "dr_after_rot180": int(dr),
        "dc_after_rot180": int(dc),
        "shift_convention": "output[target]=rot180_current[target+shift]; no wrap",
        "source_row_formula": f"N - 1 - (target_row + {dr})",
        "source_col_formula": f"N - 1 - (target_col + {dc})",
        "special_mapping_label": special_shift_label(dr, dc),
        "n_valid": int(np.count_nonzero(valid)),
        "valid_fraction_of_reference_disk": float(np.count_nonzero(valid) / max(np.count_nonzero(ref_valid), 1)),
    }
    if not np.any(valid):
        return row, dist_map
    lat_err = np.abs(lat_t[valid].astype(np.float32) - ref_lat[valid].astype(np.float32))
    lon_err = np.abs(circular_lon_diff_deg(lon_t[valid], ref_lon[valid]))
    dist = geodesic_distance_km(lon_t[valid], lat_t[valid], ref_lon[valid], ref_lat[valid])
    dist_map[valid] = dist
    row.update(
        {
            "latitude_mae_deg": float(np.nanmean(lat_err)),
            "longitude_circular_mae_deg": float(np.nanmean(lon_err)),
            "median_geodesic_error_km": float(np.nanmedian(dist)),
            "p95_geodesic_error_km": float(np.nanpercentile(dist, 95)),
            "mean_geodesic_error_km": float(np.nanmean(dist)),
            "matched_fraction_within_0p5km": float(np.mean(dist <= 0.5)),
            "matched_fraction_within_1km": float(np.mean(dist <= 1.0)),
            "matched_fraction_within_3km": float(np.mean(dist <= 3.0)),
            "matched_fraction_within_5km": float(np.mean(dist <= 5.0)),
            "matched_fraction_within_10km": float(np.mean(dist <= 10.0)),
        }
    )
    del lat_t, lon_t, valid, lat_err, lon_err, dist
    gc.collect()
    return row, dist_map


def special_shift_label(dr: int, dc: int) -> str:
    if dr == 0 and dc == 0:
        return "ordinary_N_minus_1_index"
    if dr == 1 and dc == 1:
        return "requested_N_minus_2_index"
    return "integer_shift_candidate"


def search_shifts(case_id: str, ref_lon: np.ndarray, ref_lat: np.ndarray, ref_valid: np.ndarray, cur_lon: np.ndarray, cur_lat: np.ndarray) -> tuple[list[dict[str, Any]], dict[str, np.ndarray]]:
    rows: list[dict[str, Any]] = []
    maps: dict[str, np.ndarray] = {}
    for dr in range(-3, 4):
        for dc in range(-3, 4):
            row, dist_map = shift_metrics(case_id, ref_lon, ref_lat, ref_valid, cur_lon, cur_lat, dr, dc)
            rows.append(row)
            if (dr, dc) in [(0, 0), (1, 1)]:
                maps[f"dr{dr:+d}_dc{dc:+d}"] = dist_map
            del dist_map
            gc.collect()
    finite = [row for row in rows if math.isfinite(float(row.get("median_geodesic_error_km", math.nan)))]
    if finite:
        best = min(finite, key=lambda row: (float(row["median_geodesic_error_km"]), float(row["p95_geodesic_error_km"])))
        for row in rows:
            row["best_dr_after_rot180"] = int(best["dr_after_rot180"])
            row["best_dc_after_rot180"] = int(best["dc_after_rot180"])
            row["is_best_shift"] = row["dr_after_rot180"] == best["dr_after_rot180"] and row["dc_after_rot180"] == best["dc_after_rot180"]
        _, best_map = shift_metrics(case_id, ref_lon, ref_lat, ref_valid, cur_lon, cur_lat, int(best["dr_after_rot180"]), int(best["dc_after_rot180"]))
        maps["best"] = best_map
    return rows, maps


def native_pixel_radius(ref_valid: np.ndarray) -> np.ndarray:
    rows, cols = np.indices(ref_valid.shape, dtype=np.float32)
    rr = rows[ref_valid]
    cc = cols[ref_valid]
    center_r = float(np.nanmean(rr))
    center_c = float(np.nanmean(cc))
    radius = np.sqrt((rows - center_r) ** 2 + (cols - center_c) ** 2)
    radius_norm = radius / float(np.nanmax(radius[ref_valid]))
    radius_norm[~ref_valid] = np.nan
    return radius_norm.astype(np.float32)


def radial_profile_rows(case_id: str, dist_map: np.ndarray, ref_valid: np.ndarray) -> list[dict[str, Any]]:
    radius = native_pixel_radius(ref_valid)
    rows: list[dict[str, Any]] = []
    edges = np.array([0.0, 0.2, 0.4, 0.6, 0.75, 0.85, 0.92, 0.97, 1.01], dtype=np.float32)
    for lo, hi in zip(edges[:-1], edges[1:]):
        valid = ref_valid & np.isfinite(dist_map) & np.isfinite(radius) & (radius >= lo) & (radius < hi)
        values = dist_map[valid]
        rows.append(
            {
                "case_id": case_id,
                "radius_bin": f"{lo:.2f}-{hi:.2f}",
                "radius_norm_min": float(lo),
                "radius_norm_max": float(hi),
                "n_valid": int(values.size),
                "median_geodesic_error_km": float(np.nanmedian(values)) if values.size else math.nan,
                "mean_geodesic_error_km": float(np.nanmean(values)) if values.size else math.nan,
                "p95_geodesic_error_km": float(np.nanpercentile(values, 95)) if values.size else math.nan,
                "within_1km_fraction": float(np.mean(values <= 1.0)) if values.size else math.nan,
                "within_5km_fraction": float(np.mean(values <= 5.0)) if values.size else math.nan,
                "within_10km_fraction": float(np.mean(values <= 10.0)) if values.size else math.nan,
            }
        )
    return rows


def target_lon_lat(grid: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    lon = np.linspace(float(grid["lon_centers_first_last"][0]), float(grid["lon_centers_first_last"][1]), int(grid["lon_size"]), dtype=np.float64)
    lat = np.linspace(float(grid["lat_centers_first_last"][0]), float(grid["lat_centers_first_last"][1]), int(grid["lat_size"]), dtype=np.float64)
    return lon, lat


def reproject_mask(mask: np.ndarray, lat: np.ndarray, lon: np.ndarray, grid: dict[str, Any]) -> tuple[np.ndarray, np.ndarray, int]:
    _, fusion_valid, _ = cloud_mask_masks(SOURCE, PRODUCT, mask)
    lon_norm = normalize_longitude(lon)
    finite_nav = np.isfinite(lat) & np.isfinite(lon_norm) & (lat >= -90.0) & (lat <= 90.0)
    zero_fill = np.isclose(lat, 0.0) & np.isclose(lon, 0.0)
    source_valid = fusion_valid & finite_nav & (~zero_fill)
    tree, src_y, src_x, _ = build_tree(lon_norm, lat.astype(np.float32), source_valid)
    target_lon, target_lat = target_lon_lat(grid)
    data_grid, valid_grid_u8 = query_reproject(
        tree,
        src_y,
        src_x,
        mask.astype(np.int16, copy=False),
        target_lon,
        target_lat,
        np.dtype(np.int16),
        -9999,
    )
    return data_grid, valid_grid_u8.astype(bool), int(np.count_nonzero(source_valid))


def confusion_metrics(epic_cls: np.ndarray, geo_cls: np.ndarray, valid: np.ndarray, positive: int = 1) -> dict[str, Any]:
    n_valid = int(np.count_nonzero(valid))
    if n_valid == 0:
        return {"n_valid": 0, "agreement": math.nan, "balanced_accuracy": math.nan, "f1_cloud": math.nan, "iou_cloud": math.nan, "mcc": math.nan, "TP": 0, "TN": 0, "FP": 0, "FN": 0}
    e = epic_cls[valid]
    g = geo_cls[valid]
    ep = e == positive
    gp = g == positive
    tp = int(np.count_nonzero(ep & gp))
    tn = int(np.count_nonzero((~ep) & (~gp)))
    fp = int(np.count_nonzero((~ep) & gp))
    fn = int(np.count_nonzero(ep & (~gp)))
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    specificity = tn / max(tn + fp, 1)
    denom = math.sqrt(max((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn), 0))
    return {
        "n_valid": n_valid,
        "agreement": float(np.mean(e == g)),
        "precision_cloud": precision,
        "recall_cloud": recall,
        "specificity_clear": specificity,
        "balanced_accuracy": (recall + specificity) / 2.0,
        "f1_cloud": 2.0 * precision * recall / max(precision + recall, 1e-12),
        "iou_cloud": tp / max(tp + fp + fn, 1),
        "mcc": ((tp * tn) - (fp * fn)) / denom if denom else math.nan,
        "TP": tp,
        "TN": tn,
        "FP": fp,
        "FN": fn,
        "cloud_fraction_epic": float(np.mean(ep)),
        "cloud_fraction_geo": float(np.mean(gp)),
        "cloud_fraction_bias_geo_minus_epic": float(np.mean(gp) - np.mean(ep)),
    }


def boundary_mask(classes: np.ndarray, valid: np.ndarray, positive: int = 1) -> np.ndarray:
    cloud = (classes == positive) & valid
    padded_cloud = np.pad(cloud.astype(np.int16), 1, mode="edge")
    padded_valid = np.pad(valid.astype(np.int16), 1, mode="constant")
    count = np.zeros(classes.shape, dtype=np.int16)
    valid_count = np.zeros(classes.shape, dtype=np.int16)
    for dy in range(3):
        for dx in range(3):
            count += padded_cloud[dy : dy + classes.shape[0], dx : dx + classes.shape[1]]
            valid_count += padded_valid[dy : dy + classes.shape[0], dx : dx + classes.shape[1]]
    return valid & (valid_count > 0) & (count > 0) & (count < valid_count)


def boundary_metrics(epic_cls: np.ndarray, geo_cls: np.ndarray, valid: np.ndarray, positive: int = 1) -> dict[str, Any]:
    if int(np.count_nonzero(valid)) == 0:
        return {"boundary_f1": math.nan, "boundary_precision": math.nan, "boundary_recall": math.nan, "boundary_chamfer_px": math.nan, "n_boundary_epic": 0, "n_boundary_geo": 0}
    eb = boundary_mask(epic_cls, valid, positive)
    gb = boundary_mask(geo_cls, valid, positive)
    tp = int(np.count_nonzero(eb & gb))
    fp = int(np.count_nonzero((~eb) & gb & valid))
    fn = int(np.count_nonzero(eb & (~gb) & valid))
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    if np.any(eb) and np.any(gb):
        dist_to_geo = distance_transform_edt(~gb)
        dist_to_epic = distance_transform_edt(~eb)
        chamfer = (float(np.mean(dist_to_geo[eb])) + float(np.mean(dist_to_epic[gb]))) / 2.0
    else:
        chamfer = math.nan
    return {
        "boundary_f1": 2.0 * precision * recall / max(precision + recall, 1e-12),
        "boundary_precision": precision,
        "boundary_recall": recall,
        "boundary_chamfer_px": chamfer,
        "n_boundary_epic": int(np.count_nonzero(eb)),
        "n_boundary_geo": int(np.count_nonzero(gb)),
    }


def candidate_fix_experiment(
    case_id: str,
    mask: np.ndarray,
    cur_lat: np.ndarray,
    cur_lon: np.ndarray,
    ref_lat: np.ndarray,
    ref_lon: np.ndarray,
    best_dr: int,
    best_dc: int,
    warnings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    manifest = pd.DataFrame(load_manifest(STAGE09D_DIR))
    hit = manifest[manifest["sample_id"] == case_id]
    if hit.empty:
        warn(warnings, "no_existing_stage09d_epic_case", f"{case_id} not present in Stage 09D manifest; candidate fix EPIC experiment skipped", case_id=case_id)
        return []
    row = hit.iloc[0].to_dict()
    ctx = full_pixel.sample_context(row)
    policy = full_pixel.POLICIES[POLICY_NAME]
    epic_cls, epic_policy_valid = full_pixel.apply_policy(ctx["epic"]["cloud_mask"], policy["epic"])
    valid_earth = np.isin(ctx["epic"]["cloud_mask"], [1, 2, 3, 4])
    variants = {
        "A_current_navigation": (cur_lat, cur_lon, "current cfgrib latitude/longitude"),
        "B_simple_rot180_navigation": (cur_lat[::-1, ::-1], cur_lon[::-1, ::-1], "current latitude/longitude rot180 only"),
        "C_rot180_best_integer_shift_navigation": (
            shifted_after_rot180(cur_lat, best_dr, best_dc),
            shifted_after_rot180(cur_lon, best_dr, best_dc),
            f"current latitude/longitude rot180 plus dr={best_dr}, dc={best_dc}; no wrap",
        ),
        "D_official_area_derived_navigation": (ref_lat, ref_lon, "Satpy SEVIRI L1.5 area-derived navigation"),
    }
    sampled: dict[str, dict[str, np.ndarray | int]] = {}
    rows: list[dict[str, Any]] = []
    for variant, (lat, lon, note) in variants.items():
        grid_data, grid_valid, source_valid_count = reproject_mask(mask, np.asarray(lat, dtype=np.float32), np.asarray(lon, dtype=np.float32), ctx["grid"])
        raw_on_epic, raw_valid_on_epic = full_pixel.sample_grid(grid_data, grid_valid, ctx["epic"]["lat"], ctx["epic"]["lon"], ctx["grid"])
        standard = full_pixel.source_to_standard(SOURCE, raw_on_epic)
        geo_cls, geo_policy_valid = full_pixel.apply_policy(standard, policy["geo"])
        valid = valid_earth & epic_policy_valid & raw_valid_on_epic & geo_policy_valid
        metrics = confusion_metrics(epic_cls, geo_cls, valid, positive=policy["positive"])
        metrics.update(boundary_metrics(epic_cls, geo_cls, valid, positive=policy["positive"]))
        metrics.update(
            {
                "case_id": case_id,
                "candidate_variant": variant,
                "comparison_domain": "variant_valid",
                "policy": POLICY_NAME,
                "source": SOURCE,
                "navigation_note": note,
                "cloud_mask_transform": "identity_cloud_mask_not_rotated",
                "source_valid_count_before_reprojection": source_valid_count,
                "epic_source_valid_before_policy": int(np.count_nonzero(raw_valid_on_epic & valid_earth)),
                "stage09d_epic_time_utc": row.get("epic_time_utc", ""),
                "stage_run_dir": row.get("stage_run_dir", ""),
            }
        )
        rows.append(metrics)
        sampled[variant] = {"geo_cls": geo_cls, "valid": valid, "source_valid_count": source_valid_count}
        del grid_data, grid_valid, raw_on_epic, raw_valid_on_epic, standard, geo_cls, geo_policy_valid
        gc.collect()
    common = valid_earth & epic_policy_valid
    for item in sampled.values():
        common &= np.asarray(item["valid"], dtype=bool)
    for variant, item in sampled.items():
        geo_cls = np.asarray(item["geo_cls"])
        metrics = confusion_metrics(epic_cls, geo_cls, common, positive=policy["positive"])
        metrics.update(boundary_metrics(epic_cls, geo_cls, common, positive=policy["positive"]))
        metrics.update(
            {
                "case_id": case_id,
                "candidate_variant": variant,
                "comparison_domain": "common_valid_all_candidates",
                "policy": POLICY_NAME,
                "source": SOURCE,
                "navigation_note": variants[variant][2],
                "cloud_mask_transform": "identity_cloud_mask_not_rotated",
                "source_valid_count_before_reprojection": int(item["source_valid_count"]),
                "epic_source_valid_before_policy": int(np.count_nonzero(np.asarray(item["valid"], dtype=bool) & valid_earth)),
                "stage09d_epic_time_utc": row.get("epic_time_utc", ""),
                "stage_run_dir": row.get("stage_run_dir", ""),
            }
        )
        rows.append(metrics)
    return rows


def projection_parameter_rows(case_id: str, ref_meta: dict[str, Any], clm_meta: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, value in ref_meta.items():
        if key in {
            "area_id",
            "area_shape",
            "area_extent_json",
            "area_proj_dict_json",
            "projection_a_m",
            "projection_b_m",
            "projection_rf",
            "projection_h_m",
            "projection_lon_0_deg",
            "projection_sweep",
            "shape_y",
            "shape_x",
            "storage_order",
            "pixel_center_convention",
        }:
            rows.append({"case_id": case_id, "source": "L1.5_reference_area", "parameter": key, "value": value})
    rows.extend(
        [
            {"case_id": case_id, "source": "current_clm_cfgrib", "parameter": "mask_raw_shape", "value": clm_meta.get("mask_raw_shape", "")},
            {"case_id": case_id, "source": "current_clm_cfgrib", "parameter": "latitude_raw_shape", "value": clm_meta.get("latitude_raw_shape", "")},
            {"case_id": case_id, "source": "current_clm_cfgrib", "parameter": "longitude_raw_shape", "value": clm_meta.get("longitude_raw_shape", "")},
            {"case_id": case_id, "source": "current_clm_cfgrib", "parameter": "reshape_method", "value": clm_meta.get("reshape_method", "")},
        ]
    )
    try:
        attrs = json.loads(str(clm_meta.get("navigation_relevant_attrs_json", "{}")))
    except json.JSONDecodeError:
        attrs = {}
    for key, value in sorted(attrs.items()):
        rows.append({"case_id": case_id, "source": "current_clm_cfgrib_attrs", "parameter": key, "value": value})
    return rows


def exact_mapping_rows(shift_df: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case_id, group in shift_df.groupby("case_id"):
        best = group[group["is_best_shift"] == True]
        ordinary = group[(group["dr_after_rot180"] == 0) & (group["dc_after_rot180"] == 0)]
        requested = group[(group["dr_after_rot180"] == 1) & (group["dc_after_rot180"] == 1)]
        for label, frame in [("best", best), ("ordinary_N_minus_1_index", ordinary), ("requested_N_minus_2_index", requested)]:
            if frame.empty:
                continue
            row = frame.iloc[0].to_dict()
            rows.append(
                {
                    "case_id": case_id,
                    "mapping_role": label,
                    "dr_after_rot180": int(row["dr_after_rot180"]),
                    "dc_after_rot180": int(row["dc_after_rot180"]),
                    "source_row_formula": row["source_row_formula"],
                    "source_col_formula": row["source_col_formula"],
                    "median_geodesic_error_km": row.get("median_geodesic_error_km", math.nan),
                    "p95_geodesic_error_km": row.get("p95_geodesic_error_km", math.nan),
                    "matched_fraction_within_0p5km": row.get("matched_fraction_within_0p5km", math.nan),
                    "matched_fraction_within_1km": row.get("matched_fraction_within_1km", math.nan),
                    "matched_fraction_within_5km": row.get("matched_fraction_within_5km", math.nan),
                    "matched_fraction_within_10km": row.get("matched_fraction_within_10km", math.nan),
                    "valid_fraction_of_reference_disk": row.get("valid_fraction_of_reference_disk", math.nan),
                }
            )
    return rows


def residual_classification(shift_df: pd.DataFrame, radial_df: pd.DataFrame) -> str:
    if shift_df.empty:
        return "INCONCLUSIVE_no_shift_metrics"
    best = shift_df[shift_df["is_best_shift"] == True].copy()
    ordinary = shift_df[(shift_df["dr_after_rot180"] == 0) & (shift_df["dc_after_rot180"] == 0)].copy()
    if best.empty or ordinary.empty:
        return "INCONCLUSIVE_missing_best_or_ordinary"
    median_gain = float(ordinary["median_geodesic_error_km"].median() - best["median_geodesic_error_km"].median())
    p95_gain = float(ordinary["p95_geodesic_error_km"].median() - best["p95_geodesic_error_km"].median())
    best_median = float(best["median_geodesic_error_km"].median())
    best_p95 = float(best["p95_geodesic_error_km"].median())
    if best_median <= 1.0 and best_p95 <= 3.0 and median_gain >= 2.0:
        return "FIXED_INTEGER_PIXEL_OFFSET_DOMINANT"
    if median_gain >= 1.0 or p95_gain >= 3.0:
        return "INTEGER_SHIFT_PARTIAL_WITH_RESIDUAL"
    if not radial_df.empty:
        inner = radial_df[radial_df["radius_norm_max"] <= 0.4]["median_geodesic_error_km"].median()
        limb = radial_df[radial_df["radius_norm_min"] >= 0.85]["median_geodesic_error_km"].median()
        if math.isfinite(float(inner)) and math.isfinite(float(limb)) and limb > max(inner * 2.0, inner + 3.0):
            return "SUBPIXEL_OR_PROJECTION_RESIDUAL_WITH_LIMB_AMPLIFICATION"
    if best_median <= 6.0 and best_p95 <= 18.0:
        return "SUBPIXEL_GRID_CENTER_OR_PROJECTION_PARAMETER_RESIDUAL"
    return "PROJECTION_PARAMETER_OR_NONLINEAR_RESIDUAL_REQUIRES_DEEPER_AUDIT"


def fix_scope_rows() -> list[dict[str, Any]]:
    return [
        {
            "scope_id": "eligible_after_independent_audit",
            "satellite_family": "Meteosat-0deg",
            "product": "CLM",
            "file_pattern": "MSG3-SEVI-MSGCLMK-0100-0100-*.zip",
            "native_shape": "3712x3712",
            "audited_cases": "20240312_1200;20240312_1500",
            "proposed_navigation_action": "regenerate latitude/longitude from official SEVIRI native area definition; keep cloud_mask identity",
            "auto_apply": "no_until_production_patch_and_tests",
            "double_rotation_guard": "before fix, assert current CLM navigation identity is not already close to official area-derived navigation and/or record navigation_orientation_status",
        },
        {
            "scope_id": "requires_separate_audit",
            "satellite_family": "Meteosat-IODC",
            "product": "CLM",
            "file_pattern": "IODC CLM files",
            "native_shape": "not_assumed",
            "audited_cases": "",
            "proposed_navigation_action": "do not inherit Meteosat-0deg transform automatically",
            "auto_apply": "no",
            "double_rotation_guard": "run product/grid-specific Gate audit first",
        },
        {
            "scope_id": "requires_separate_audit",
            "satellite_family": "Meteosat-0deg",
            "product": "CTH/CTT/CTP/other",
            "file_pattern": "non-CLM product files",
            "native_shape": "not_assumed",
            "audited_cases": "",
            "proposed_navigation_action": "do not apply CLM navigation fix automatically",
            "auto_apply": "no",
            "double_rotation_guard": "each product must audit storage order and navigation source",
        },
    ]


def regression_spec_rows() -> list[dict[str, Any]]:
    return [
        {
            "test_id": "test_meteosat0deg_clm_mask_not_rotated",
            "purpose": "Guard against accidental cloud_mask rotation.",
            "input_case": "20240312_1200 CLM",
            "expected": "cloud_mask array bytes/shape remain identity relative to raw cfgrib values after reader fix.",
        },
        {
            "test_id": "test_meteosat0deg_clm_navigation_matches_l15_area",
            "purpose": "Confirm repaired lat/lon match independent official SEVIRI area navigation.",
            "input_case": "20240312_1200 and 20240312_1500",
            "expected": "median geodesic error <= 1 km or documented residual threshold from Gate 4A; identity wrongness no longer present.",
        },
        {
            "test_id": "test_meteosat0deg_clm_no_double_rotation",
            "purpose": "Prevent applying rot180 or regenerated navigation twice.",
            "input_case": "already-fixed synthetic/navigation fixture",
            "expected": "fix gate detects navigation_orientation_status or low identity error and skips rotation/regeneration.",
        },
        {
            "test_id": "test_stage05_reproject_uses_fixed_navigation_only",
            "purpose": "Ensure Stage 05 consumes repaired navigation while preserving mask semantics.",
            "input_case": "20240312_1500 CLM",
            "expected": "EPIC-view source comparison matches official-area candidate metrics from Gate 4A within tolerance.",
        },
    ]


def code_location_rows() -> list[dict[str, Any]]:
    rows = []
    for name, obj in [
        ("read_meteosat_zip", cloud_products.read_meteosat_zip),
        ("reshape_square_if_needed", cloud_products.reshape_square_if_needed),
    ]:
        try:
            source_file = inspect.getsourcefile(obj) or ""
            line_no = inspect.getsourcelines(obj)[1]
        except Exception:
            source_file = ""
            line_no = -1
        rows.append(
            {
                "function": name,
                "source_file": source_file,
                "first_line": line_no,
                "risk_relevance": "Meteosat CLM cloud_mask/latitude/longitude raw GRIB arrays are read and reshaped here before Stage 05 reproject.",
                "recommended_fix_point": "Design fix in reader/navigation generation path only; cloud_mask must remain identity.",
            }
        )
    return rows


def make_residual_figure(case_id: str, dist_map: np.ndarray, paths: dict[str, Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    display = dist_map[::-1, ::-1][::4, ::4]
    source_rows: list[dict[str, Any]] = []
    for row in range(display.shape[0]):
        values = display[row, :]
        cols = np.where(np.isfinite(values))[0]
        for col in cols:
            source_rows.append(
                {
                    "case_id": case_id,
                    "display_row_north_up": int(row),
                    "display_col_east_right": int(col),
                    "decimation_factor": 4,
                    "geodesic_error_km": float(values[col]),
                }
            )
    source_csv = paths["source_data"] / f"stage_09h_gate4a_{case_id}_best_shift_residual_map_source.csv"
    write_csv(source_rows, source_csv)
    vmax = float(np.nanpercentile(display, 95)) if np.any(np.isfinite(display)) else 10.0
    fig, ax = plt.subplots(figsize=(9.6, 5.4), constrained_layout=True)
    im = ax.imshow(display, origin="upper", cmap="magma_r", vmin=0, vmax=vmax)
    ax.set_title(f"Stage 09H Gate 4A best-shift residual | {case_id}")
    ax.set_xticks([])
    ax.set_yticks([])
    ax.text(0.01, 0.98, "North up / East right display only", transform=ax.transAxes, va="top", ha="left", fontsize=8, color="white")
    plt.colorbar(im, ax=ax, fraction=0.025, pad=0.015, label="geodesic error (km)")
    for ext, dpi in [("png", 240), ("svg", 240), ("pdf", 240)]:
        fig_path = paths["figures"] / f"stage_09h_gate4a_{case_id}_best_shift_residual.{ext}"
        fig.savefig(fig_path, dpi=dpi)
        rows.append(
            {
                "figure_id": f"stage_09h_gate4a_{case_id}_best_shift_residual",
                "case_id": case_id,
                "figure_path": str(fig_path),
                "source_csv": str(source_csv),
                "description": "Full-disk geodesic residual after the best rot180+integer-shift navigation mapping; visual display is decimated only.",
            }
        )
    plt.close(fig)
    return rows


def make_candidate_figure(metrics: pd.DataFrame, paths: dict[str, Path]) -> list[dict[str, Any]]:
    if metrics.empty:
        return []
    view = metrics[metrics["comparison_domain"] == "common_valid_all_candidates"].copy()
    if view.empty:
        view = metrics.copy()
    order = ["A_current_navigation", "B_simple_rot180_navigation", "C_rot180_best_integer_shift_navigation", "D_official_area_derived_navigation"]
    labels = ["current", "rot180", "rot180+shift", "official area"]
    colors = ["#4C78A8", "#59A14F", "#F28E2B", "#D62728"]
    fig, axes = plt.subplots(1, 4, figsize=(12.8, 3.6), constrained_layout=True)
    for ax, metric, title in zip(axes, ["agreement", "f1_cloud", "iou_cloud", "mcc"], ["Agreement", "Cloud F1", "Cloud IoU", "MCC"]):
        values = []
        for variant in order:
            hit = view[view["candidate_variant"] == variant]
            values.append(float(hit.iloc[0][metric]) if not hit.empty else math.nan)
        ax.bar(labels, values, color=colors)
        ax.set_title(title)
        ax.set_ylim(-0.2 if metric == "mcc" else 0.0, 1.0)
        ax.grid(axis="y", color="#D9D9D9", linewidth=0.6)
        ax.tick_params(axis="x", labelrotation=25, labelsize=8)
    fig.suptitle("Stage 09H Gate 4A candidate navigation fix test | 20240312_1500", fontsize=10)
    rows: list[dict[str, Any]] = []
    for ext, dpi in [("png", 240), ("svg", 240), ("pdf", 240)]:
        fig_path = paths["figures"] / f"stage_09h_gate4a_candidate_fix_20240312_1500.{ext}"
        fig.savefig(fig_path, dpi=dpi)
        rows.append(
            {
                "figure_id": "stage_09h_gate4a_candidate_fix_20240312_1500",
                "case_id": "20240312_1500",
                "figure_path": str(fig_path),
                "source_csv": str(paths["source_data"] / "stage_09h_gate4a_candidate_fix_comparison.csv"),
                "description": "EPIC-view comparison for current, simple rot180, rot180+best-shift, and official-area navigation. Cloud mask is unchanged.",
            }
        )
    plt.close(fig)
    return rows


def write_regression_spec(paths: dict[str, Path], rows: list[dict[str, Any]]) -> Path:
    lines = [
        "# Stage 09H Gate 4A regression test specification",
        "",
        "这些测试是生产修复设计的回归保护，不在 Gate 4A 修改生产 reader。",
        "",
        dataframe_to_markdown(pd.DataFrame(rows), floatfmt=".4f"),
        "",
        "约束：每一种 Meteosat 产品和网格必须单独审计；不得把 Meteosat-0deg CLM 的结论自动套用到 IODC、CTH、CTT、CTP 或其他文件族。",
    ]
    path = paths["reports"] / "stage_09h_gate4a_regression_test_specification.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return path


def write_report(
    paths: dict[str, Path],
    shift_df: pd.DataFrame,
    mapping_df: pd.DataFrame,
    radial_df: pd.DataFrame,
    parameter_df: pd.DataFrame,
    candidate_df: pd.DataFrame,
    scope_df: pd.DataFrame,
    code_df: pd.DataFrame,
    residual_status: str,
    warnings: list[dict[str, Any]],
) -> Path:
    best = shift_df[shift_df["is_best_shift"] == True].copy() if not shift_df.empty and "is_best_shift" in shift_df else pd.DataFrame()
    common_best = "INCONCLUSIVE"
    if not best.empty:
        pairs = sorted({(int(r.dr_after_rot180), int(r.dc_after_rot180)) for r in best.itertuples()})
        common_best = str(pairs[0]) if len(pairs) == 1 else "not_consistent:" + str(pairs)
    recommendation = "B_official_area_derived_navigation"
    if residual_status == "FIXED_INTEGER_PIXEL_OFFSET_DOMINANT":
        recommendation_note = "整数偏移几乎解释残差，但生产修复仍建议优先用官方 area definition 生成导航，以避免依赖显示方向和 reshape 假设。"
    else:
        recommendation_note = "整数 shift 没有完全消除残差，说明剩余误差更像亚像元中心、投影参数或 limb 放大问题；生产修复应优先重新生成官方 navigation，而不是硬编码 rot180+shift。"
    lines = [
        "# Stage 09H Gate 4A Meteosat navigation exact mapping and production-fix design",
        "",
        f"- Generated UTC: {utc_now()}",
        f"- Stage/Gate: `{STAGE_ID}` / `{GATE_ID}`",
        "- Scope: Meteosat-0deg CLM, 20240312_1200 as main case and 20240312_1500 as repeat/EPIC existing case.",
        "- Hard constraints: cloud_mask is never rotated; production `read_meteosat_zip()` is not modified; old standardized/native results are not overwritten; no full-month rerun.",
        "- Shift convention: first apply current-navigation rot180, then evaluate no-wrap integer shifts where `output[target]=rot180_current[target+shift]`.",
        "",
        "## Final Gate 4A decision",
        "",
        f"- Best shift consistency: `{common_best}`.",
        f"- Residual structure classification: `{residual_status}`.",
        f"- Recommended production-fix design: `{recommendation}`.",
        f"- Reason: {recommendation_note}",
        "- Important EPIC-view caveat: simple rot180 can score slightly higher against EPIC cloud mask than official-area navigation in the 20240312_1500 candidate test. This is not used as the navigation truth criterion, because EPIC cloud comparison also includes cloud-mask semantic, sampling, time, boundary, and product differences. The exact-navigation decision is anchored to the independent L1.5 official native area definition.",
        "- Scope guard: only Meteosat-0deg CLM 3712x3712 file family is eligible after this audit; IODC and non-CLM products require separate audits.",
        "",
        "## Exact mapping result",
        "",
        dataframe_to_markdown(mapping_df, floatfmt=".6f") if not mapping_df.empty else "",
    ]
    if not best.empty:
        keep = [
            "case_id",
            "dr_after_rot180",
            "dc_after_rot180",
            "median_geodesic_error_km",
            "p95_geodesic_error_km",
            "matched_fraction_within_0p5km",
            "matched_fraction_within_1km",
            "matched_fraction_within_5km",
            "matched_fraction_within_10km",
            "valid_fraction_of_reference_disk",
        ]
        lines.extend(["", "## Best shift metrics", "", dataframe_to_markdown(best[keep], floatfmt=".6f")])
    if not candidate_df.empty:
        keep = [
            "case_id",
            "candidate_variant",
            "comparison_domain",
            "agreement",
            "balanced_accuracy",
            "f1_cloud",
            "iou_cloud",
            "mcc",
            "boundary_f1",
            "boundary_chamfer_px",
            "n_valid",
        ]
        lines.extend(["", "## Candidate fix EPIC-view comparison", "", dataframe_to_markdown(candidate_df[keep], floatfmt=".6f")])
    if not radial_df.empty:
        lines.extend(["", "## Residual radial profile", "", dataframe_to_markdown(radial_df.head(16), floatfmt=".6f")])
    lines.extend(
        [
            "",
            "## Projection parameter comparison",
            "",
            "完整参数见 `source_data/stage_09h_gate4a_projection_parameter_comparison.csv`。下面列出前若干行：",
            "",
            dataframe_to_markdown(parameter_df.head(24), floatfmt=".4f") if not parameter_df.empty else "",
            "",
            "## Production fix scope",
            "",
            dataframe_to_markdown(scope_df, floatfmt=".4f") if not scope_df.empty else "",
            "",
            "## Code locations",
            "",
            dataframe_to_markdown(code_df, floatfmt=".0f") if not code_df.empty else "",
            "",
            "## Output files",
            "",
            "- `source_data/stage_09h_gate4a_shift_sensitivity.csv`",
            "- `source_data/stage_09h_gate4a_exact_navigation_mapping.csv`",
            "- `source_data/stage_09h_gate4a_projection_parameter_comparison.csv`",
            "- `source_data/stage_09h_gate4a_residual_radial_profile.csv`",
            "- `source_data/stage_09h_gate4a_candidate_fix_comparison.csv`",
            "- `source_data/stage_09h_gate4a_fix_scope_conditions.csv`",
            "- `reports/stage_09h_gate4a_regression_test_specification.md`",
            "",
            "## Warnings",
            "",
            f"- Warning rows: {len(warnings)}. See `logs/stage_09h_gate4a_warnings.csv`.",
        ]
    )
    path = paths["reports"] / "stage_09h_gate4a_production_fix_design_report_cn.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Stage 09H Gate 4A navigation exact mapping and fix design.")
    parser.add_argument("--cases", default="20240312_1200,20240312_1500", help="Comma-separated direct navigation cases.")
    parser.add_argument("--epic-case", default="20240312_1500", help="Existing Stage 09D case for EPIC-view candidate fix comparison.")
    args = parser.parse_args()

    paths = ensure_dirs(OUT_ROOT)
    started = utc_now()
    warnings: list[dict[str, Any]] = []
    figure_rows: list[dict[str, Any]] = []
    cases = [item.strip() for item in str(args.cases).split(",") if item.strip()]
    epic_case = str(args.epic_case).strip()

    inventory_rows: list[dict[str, Any]] = []
    shift_rows: list[dict[str, Any]] = []
    radial_rows: list[dict[str, Any]] = []
    parameter_rows_all: list[dict[str, Any]] = []
    arrays_by_case: dict[str, dict[str, np.ndarray]] = {}

    for case_id in cases:
        hour = parse_hour(case_id)
        l15_path = find_l15_native(hour)
        clm_path = find_clm_zip(hour)
        l15_nom = nominal_cycle_from_l15(l15_path)
        clm_nom = parse_clm_nominal(clm_path)
        inventory_rows.append(
            {
                "case_id": case_id,
                "hour_utc": hour,
                "l15_platform": L15_PLATFORM,
                "l15_channel": CHANNEL,
                "l15_nominal_time_utc": l15_nom.isoformat().replace("+00:00", "Z") if l15_nom else "",
                "clm_nominal_time_utc": clm_nom.isoformat().replace("+00:00", "Z") if clm_nom else "",
                "pair_status": "exact_match" if l15_nom == clm_nom else "time_mismatch",
                "l15_path": str(l15_path),
                "clm_path": str(clm_path),
                "reference_navigation_source": "Satpy SEVIRI native area definition; current CLM lat/lon excluded",
            }
        )
        ref_lon, ref_lat, ref_valid, ref_meta = reference_navigation_from_l15(l15_path, warnings)
        clm_arrays, clm_meta = read_clm_raw_zip(clm_path, paths["cache"], warnings)
        parameter_rows_all.extend(projection_parameter_rows(case_id, ref_meta, clm_meta))
        cur_lat = np.asarray(clm_arrays["latitude"], dtype=np.float32)
        cur_lon = np.asarray(clm_arrays["longitude"], dtype=np.float32)
        rows, dist_maps = search_shifts(case_id, ref_lon, ref_lat, ref_valid, cur_lon, cur_lat)
        shift_rows.extend(rows)
        if "best" in dist_maps:
            radial_rows.extend(radial_profile_rows(case_id, dist_maps["best"], ref_valid))
            figure_rows.extend(make_residual_figure(case_id, dist_maps["best"], paths))
        arrays_by_case[case_id] = {
            "mask": np.asarray(clm_arrays["cloud_mask"], dtype=np.float32),
            "cur_lat": cur_lat,
            "cur_lon": cur_lon,
            "ref_lat": ref_lat,
            "ref_lon": ref_lon,
        }
        del ref_valid, dist_maps, clm_arrays
        gc.collect()

    inventory_df = pd.DataFrame(inventory_rows)
    shift_df = pd.DataFrame(shift_rows)
    mapping_df = pd.DataFrame(exact_mapping_rows(shift_df))
    radial_df = pd.DataFrame(radial_rows)
    parameter_df = pd.DataFrame(parameter_rows_all)

    candidate_rows: list[dict[str, Any]] = []
    if epic_case in arrays_by_case:
        best_hit = shift_df[(shift_df["case_id"] == epic_case) & (shift_df["is_best_shift"] == True)]
        if best_hit.empty:
            warn(warnings, "missing_best_shift_for_epic_case", f"No best shift found for {epic_case}; candidate fix comparison skipped.", case_id=epic_case)
        else:
            best = best_hit.iloc[0]
            arr = arrays_by_case[epic_case]
            candidate_rows.extend(
                candidate_fix_experiment(
                    epic_case,
                    arr["mask"],
                    arr["cur_lat"],
                    arr["cur_lon"],
                    arr["ref_lat"],
                    arr["ref_lon"],
                    int(best["dr_after_rot180"]),
                    int(best["dc_after_rot180"]),
                    warnings,
                )
            )
    else:
        warn(warnings, "epic_case_not_in_direct_cases", f"{epic_case} is not in direct cases; candidate fix comparison skipped.", case_id=epic_case)
    candidate_df = pd.DataFrame(candidate_rows)

    scope_df = pd.DataFrame(fix_scope_rows())
    regression_rows = regression_spec_rows()
    regression_spec_path = write_regression_spec(paths, regression_rows)
    code_df = pd.DataFrame(code_location_rows())
    residual_status = residual_classification(shift_df, radial_df)

    figure_rows.extend(make_candidate_figure(candidate_df, paths))

    write_csv(inventory_df, paths["source_data"] / "stage_09h_gate4a_file_pair_inventory.csv")
    write_csv(shift_df, paths["source_data"] / "stage_09h_gate4a_shift_sensitivity.csv")
    write_csv(mapping_df, paths["source_data"] / "stage_09h_gate4a_exact_navigation_mapping.csv")
    write_csv(parameter_df, paths["source_data"] / "stage_09h_gate4a_projection_parameter_comparison.csv")
    write_csv(radial_df, paths["source_data"] / "stage_09h_gate4a_residual_radial_profile.csv")
    write_csv(candidate_df, paths["source_data"] / "stage_09h_gate4a_candidate_fix_comparison.csv")
    write_csv(scope_df, paths["source_data"] / "stage_09h_gate4a_fix_scope_conditions.csv")
    write_csv(regression_rows, paths["source_data"] / "stage_09h_gate4a_regression_test_specification.csv")
    write_csv(code_df, paths["source_data"] / "stage_09h_gate4a_root_cause_code_locations.csv")
    write_csv(figure_rows, paths["logs"] / "stage_09h_gate4a_figure_index.csv")
    write_csv(warnings, paths["logs"] / "stage_09h_gate4a_warnings.csv")

    report = write_report(paths, shift_df, mapping_df, radial_df, parameter_df, candidate_df, scope_df, code_df, residual_status, warnings)
    manifest = {
        "project_id": PROJECT_ID,
        "canonical_stage_id": STAGE_ID,
        "gate_id": GATE_ID,
        "run_id": RUN_ID,
        "started_utc": started,
        "finished_utc": utc_now(),
        "script_path": str(Path(__file__).resolve()),
        "output_root": str(OUT_ROOT),
        "direct_navigation_cases": cases,
        "epic_candidate_case": epic_case,
        "residual_status": residual_status,
        "report": str(report),
        "regression_test_specification": str(regression_spec_path),
        "warnings_count": len(warnings),
        "constraints": [
            "no internet download",
            "no production reader modification",
            "no fusion logic modification",
            "cloud_mask identity only",
            "no overwrite of previous standardized native products",
            "no full-month rerun",
        ],
    }
    write_json(manifest, paths["logs"] / "manifest_gate4a.json")
    print(json.dumps({"status": residual_status, "cases": cases, "epic_case": epic_case, "output_root": str(OUT_ROOT)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
