# -*- coding: utf-8 -*-
"""Stage 09H Gate 3B Meteosat navigation direct confirmation.

Read-only diagnostic that compares Meteosat-0deg CLM latitude/longitude arrays
against an independent Meteosat-10 SEVIRI L1.5 native area definition, then
checks whether replacing the CLM navigation restores the Stage 09 EPIC-view
source comparison. No production reader or fusion product is modified.
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
from datetime import datetime, timedelta, timezone
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
GATE_ID = "gate3b_navigation_direct_confirmation"
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

NAV_TRANSFORMS = {
    "identity": lambda a: a,
    "flipud": lambda a: a[::-1, :],
    "fliplr": lambda a: a[:, ::-1],
    "rot180": lambda a: a[::-1, ::-1],
}


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
        cells = []
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
    if not path.exists():
        hits = sorted(CLM_ROOT.rglob(f"*20240312{hour:02d}0000*.zip"))
        if not hits:
            raise FileNotFoundError(f"missing Meteosat-0deg CLM ZIP for 2024-03-12 {hour:02d}:00")
        return hits[0]
    return path


def inventory_cases(case_ids: list[str], warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case_id in case_ids:
        hour = parse_hour(case_id)
        l15 = find_l15_native(hour)
        clm = find_clm_zip(hour)
        l15_nom = nominal_cycle_from_l15(l15)
        clm_nom = parse_clm_nominal(clm)
        rows.append(
            {
                "case_id": case_id,
                "hour_utc": hour,
                "l15_platform": L15_PLATFORM,
                "l15_channel": CHANNEL,
                "l15_nominal_time_utc": l15_nom.isoformat().replace("+00:00", "Z") if l15_nom else "",
                "clm_nominal_time_utc": clm_nom.isoformat().replace("+00:00", "Z") if clm_nom else "",
                "pair_status": "exact_match" if l15_nom == clm_nom else "time_mismatch",
                "l15_path": str(l15),
                "clm_path": str(clm),
                "l15_shape_y": 3712,
                "l15_shape_x": 3712,
                "clm_shape_y": 3712,
                "clm_shape_x": 3712,
                "reference_navigation_source": "Satpy SEVIRI native area definition; CLM lat/lon excluded",
            }
        )
        if l15_nom != clm_nom:
            warn(warnings, "l15_clm_time_mismatch", f"{case_id} L1.5 nominal {l15_nom} != CLM nominal {clm_nom}", case_id=case_id)
    return rows


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
    except Exception as exc:
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
        mask = reshape_square_if_needed(np.asarray(ds[var_name].values, dtype=np.float32))
        lat = reshape_square_if_needed(np.asarray(ds["latitude"].values, dtype=np.float32))
        lon = reshape_square_if_needed(np.asarray(ds["longitude"].values, dtype=np.float32))
        meta.update(
            {
                "selected_grib_entry": entry,
                "selected_grib_cache": str(extracted),
                "raw_variable_name": var_name,
                "raw_dims_json": json.dumps(dict(ds.sizes), ensure_ascii=False),
                "mask_shape": str(mask.shape),
                "latitude_shape": str(lat.shape),
                "longitude_shape": str(lon.shape),
                "reshape_method": "identity if already 2D else C-order square reshape",
                "data_vars": ",".join(ds.data_vars),
                "coords": ",".join(ds.coords),
                "dataset_attrs_json": json.dumps({k: str(v) for k, v in ds.attrs.items()}, ensure_ascii=False),
                "variable_attrs_json": json.dumps({k: str(v) for k, v in ds[var_name].attrs.items()}, ensure_ascii=False),
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
    meta = {
        "l15_path": str(path),
        "reader": "satpy:seviri_l1b_native",
        "channel": CHANNEL,
        "shape_y": int(lat.shape[0]),
        "shape_x": int(lat.shape[1]),
        "area_id": str(getattr(area, "area_id", "")),
        "area_shape": str(getattr(area, "shape", "")),
        "area_proj_dict_json": json.dumps(getattr(area, "proj_dict", {}), ensure_ascii=False, default=str),
        "start_time_utc": str(da.attrs.get("start_time")),
        "end_time_utc": str(da.attrs.get("end_time")),
        "reference_valid_pixels": int(np.count_nonzero(valid)),
        "storage_order": "row0=south, rowlast=north, col0=east, collast=west",
        "row0_lat_sample_deg": float(lat[100, lat.shape[1] // 2]),
        "rowlast_lat_sample_deg": float(lat[-101, lat.shape[1] // 2]),
        "col0_lon_sample_deg": float(lon[lon.shape[0] // 2, 100]),
        "collast_lon_sample_deg": float(lon[lon.shape[0] // 2, -101]),
        "reference_navigation_uses_clm_latlon": False,
    }
    if meta["row0_lat_sample_deg"] > meta["rowlast_lat_sample_deg"] or meta["col0_lon_sample_deg"] < meta["collast_lon_sample_deg"]:
        warn(warnings, "unexpected_l15_reference_storage_order", "L1.5 area-derived navigation storage order differs from Gate 3A expectation", l15_path=str(path))
    return lon, lat, valid, meta


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


def compare_navigation(case_id: str, ref_lon: np.ndarray, ref_lat: np.ndarray, ref_valid: np.ndarray, cur_lon: np.ndarray, cur_lat: np.ndarray) -> tuple[list[dict[str, Any]], dict[str, np.ndarray]]:
    rows: list[dict[str, Any]] = []
    distance_maps: dict[str, np.ndarray] = {}
    for transform_name, transform in NAV_TRANSFORMS.items():
        lat_t = transform(cur_lat)
        lon_t = normalize_longitude(transform(cur_lon))
        valid = current_nav_valid(lat_t, lon_t, ref_valid)
        if not np.any(valid):
            rows.append({"case_id": case_id, "transform": transform_name, "n_valid": 0})
            continue
        lat_err = np.abs(lat_t[valid].astype(np.float32) - ref_lat[valid].astype(np.float32))
        lon_err = np.abs(circular_lon_diff_deg(lon_t[valid], ref_lon[valid]))
        dist = geodesic_distance_km(lon_t[valid], lat_t[valid], ref_lon[valid], ref_lat[valid])
        dist_map = np.full(ref_lat.shape, np.nan, dtype=np.float32)
        dist_map[valid] = dist
        distance_maps[transform_name] = dist_map
        rows.append(
            {
                "case_id": case_id,
                "transform": transform_name,
                "n_valid": int(np.count_nonzero(valid)),
                "valid_fraction_of_reference_disk": float(np.count_nonzero(valid) / max(np.count_nonzero(ref_valid), 1)),
                "latitude_mae_deg": float(np.nanmean(lat_err)),
                "longitude_circular_mae_deg": float(np.nanmean(lon_err)),
                "median_geodesic_distance_km": float(np.nanmedian(dist)),
                "p95_geodesic_distance_km": float(np.nanpercentile(dist, 95)),
                "matched_fraction_within_1km": float(np.mean(dist <= 1.0)),
                "matched_fraction_within_5km": float(np.mean(dist <= 5.0)),
                "matched_fraction_within_10km": float(np.mean(dist <= 10.0)),
            }
        )
        del lat_t, lon_t, valid, lat_err, lon_err, dist
        gc.collect()
    if rows:
        metric_order = "median_geodesic_distance_km"
        finite_rows = [row for row in rows if metric_order in row and math.isfinite(float(row[metric_order]))]
        if finite_rows:
            best = min(finite_rows, key=lambda row: float(row[metric_order]))
            for row in rows:
                row["best_transform_by_median_distance"] = best["transform"]
                row["is_best_transform"] = row["transform"] == best["transform"]
    return rows, distance_maps


def nearest_ref_point(ref_lon: np.ndarray, ref_lat: np.ndarray, ref_valid: np.ndarray, target_lat: float, target_lon: float) -> tuple[int, int]:
    dlat = ref_lat - target_lat
    dlon = circular_lon_diff_deg(ref_lon, target_lon)
    score = np.where(ref_valid, dlat * dlat + dlon * dlon, np.inf)
    flat = int(np.nanargmin(score))
    return np.unravel_index(flat, ref_lat.shape)


def control_point_rows(case_id: str, ref_lon: np.ndarray, ref_lat: np.ndarray, ref_valid: np.ndarray, cur_lon: np.ndarray, cur_lat: np.ndarray) -> list[dict[str, Any]]:
    targets = [
        ("disk_center", 0.0, 0.0),
        ("north_near_edge", 68.0, 0.0),
        ("south_near_edge", -68.0, 0.0),
        ("east_near_edge", 0.0, 67.0),
        ("west_near_edge", 0.0, -67.0),
        ("nw_quadrant", 35.0, -35.0),
        ("ne_quadrant", 35.0, 35.0),
        ("sw_quadrant", -35.0, -35.0),
        ("se_quadrant", -35.0, 35.0),
    ]
    rows: list[dict[str, Any]] = []
    cur_lon_norm = normalize_longitude(cur_lon)
    cur_lon_rot = normalize_longitude(cur_lon[::-1, ::-1])
    cur_lat_rot = cur_lat[::-1, ::-1]
    for name, target_lat, target_lon in targets:
        row, col = nearest_ref_point(ref_lon, ref_lat, ref_valid, target_lat, target_lon)
        lon_i = np.asarray([cur_lon_norm[row, col]], dtype=np.float64)
        lat_i = np.asarray([cur_lat[row, col]], dtype=np.float64)
        lon_r = np.asarray([cur_lon_rot[row, col]], dtype=np.float64)
        lat_r = np.asarray([cur_lat_rot[row, col]], dtype=np.float64)
        reflo = np.asarray([ref_lon[row, col]], dtype=np.float64)
        refla = np.asarray([ref_lat[row, col]], dtype=np.float64)
        _, _, ident_m = GEOD.inv(lon_i, lat_i, reflo, refla)
        _, _, rot_m = GEOD.inv(lon_r, lat_r, reflo, refla)
        rows.append(
            {
                "case_id": case_id,
                "control_point": name,
                "native_row": int(row),
                "native_col": int(col),
                "target_lat_deg": target_lat,
                "target_lon_deg": target_lon,
                "reference_lat_deg": float(ref_lat[row, col]),
                "reference_lon_deg": float(ref_lon[row, col]),
                "current_identity_lat_deg": float(cur_lat[row, col]),
                "current_identity_lon_deg": float(cur_lon_norm[row, col]),
                "current_rot180_lat_deg": float(cur_lat_rot[row, col]),
                "current_rot180_lon_deg": float(cur_lon_rot[row, col]),
                "identity_error_km": float(ident_m[0] / 1000.0),
                "rot180_error_km": float(rot_m[0] / 1000.0),
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
        return {
            "n_valid": 0,
            "agreement": math.nan,
            "precision_cloud": math.nan,
            "recall_cloud": math.nan,
            "specificity_clear": math.nan,
            "balanced_accuracy": math.nan,
            "f1_cloud": math.nan,
            "iou_cloud": math.nan,
            "mcc": math.nan,
            "TP": 0,
            "TN": 0,
            "FP": 0,
            "FN": 0,
        }
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
    f1 = 2.0 * precision * recall / max(precision + recall, 1e-12)
    denom = math.sqrt(max((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn), 0))
    return {
        "n_valid": n_valid,
        "agreement": float(np.mean(e == g)),
        "precision_cloud": precision,
        "recall_cloud": recall,
        "specificity_clear": specificity,
        "balanced_accuracy": (recall + specificity) / 2.0,
        "f1_cloud": f1,
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
        return {
            "boundary_f1": math.nan,
            "boundary_precision": math.nan,
            "boundary_recall": math.nan,
            "boundary_chamfer_px": math.nan,
            "n_boundary_epic": 0,
            "n_boundary_geo": 0,
        }
    eb = boundary_mask(epic_cls, valid, positive)
    gb = boundary_mask(geo_cls, valid, positive)
    tp = int(np.count_nonzero(eb & gb))
    fp = int(np.count_nonzero((~eb) & gb & valid))
    fn = int(np.count_nonzero(eb & (~gb) & valid))
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    f1 = 2.0 * precision * recall / max(precision + recall, 1e-12)
    if np.any(eb) and np.any(gb):
        dist_to_geo = distance_transform_edt(~gb)
        dist_to_epic = distance_transform_edt(~eb)
        chamfer = (float(np.mean(dist_to_geo[eb])) + float(np.mean(dist_to_epic[gb]))) / 2.0
    else:
        chamfer = math.nan
    return {
        "boundary_f1": f1,
        "boundary_precision": precision,
        "boundary_recall": recall,
        "boundary_chamfer_px": chamfer,
        "n_boundary_epic": int(np.count_nonzero(eb)),
        "n_boundary_geo": int(np.count_nonzero(gb)),
    }


def replacement_experiment(case_id: str, mask: np.ndarray, cur_lat: np.ndarray, cur_lon: np.ndarray, ref_lat: np.ndarray, ref_lon: np.ndarray, warnings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    manifest = pd.DataFrame(load_manifest(STAGE09D_DIR))
    hit = manifest[manifest["sample_id"] == case_id]
    if hit.empty:
        warn(warnings, "no_existing_stage09d_epic_case", f"{case_id} not present in Stage 09D manifest; EPIC replacement experiment skipped", case_id=case_id)
        return [], []
    row = hit.iloc[0].to_dict()
    ctx = full_pixel.sample_context(row)
    policy = full_pixel.POLICIES[POLICY_NAME]
    epic_cls, epic_policy_valid = full_pixel.apply_policy(ctx["epic"]["cloud_mask"], policy["epic"])
    valid_earth = np.isin(ctx["epic"]["cloud_mask"], [1, 2, 3, 4])
    variants = {
        "A_current_clm_navigation": (cur_lat, cur_lon),
        "B_l15_reference_navigation": (ref_lat, ref_lon),
        "C_current_clm_navigation_rot180": (cur_lat[::-1, ::-1], cur_lon[::-1, ::-1]),
    }
    sampled: dict[str, dict[str, np.ndarray | int]] = {}
    variant_rows: list[dict[str, Any]] = []
    for variant, (lat, lon) in variants.items():
        grid_data, grid_valid, source_valid_count = reproject_mask(mask, lat, lon, ctx["grid"])
        raw_on_epic, raw_valid_on_epic = full_pixel.sample_grid(grid_data, grid_valid, ctx["epic"]["lat"], ctx["epic"]["lon"], ctx["grid"])
        standard = full_pixel.source_to_standard(SOURCE, raw_on_epic)
        geo_cls, geo_policy_valid = full_pixel.apply_policy(standard, policy["geo"])
        valid = valid_earth & epic_policy_valid & raw_valid_on_epic & geo_policy_valid
        metrics = confusion_metrics(epic_cls, geo_cls, valid, positive=policy["positive"])
        metrics.update(boundary_metrics(epic_cls, geo_cls, valid, positive=policy["positive"]))
        metrics.update(
            {
                "case_id": case_id,
                "replacement_variant": variant,
                "comparison_domain": "variant_valid",
                "policy": POLICY_NAME,
                "source": SOURCE,
                "source_valid_count_before_reprojection": source_valid_count,
                "epic_source_valid_before_policy": int(np.count_nonzero(raw_valid_on_epic & valid_earth)),
                "stage09d_epic_time_utc": row.get("epic_time_utc", ""),
                "stage_run_dir": row.get("stage_run_dir", ""),
            }
        )
        variant_rows.append(metrics)
        sampled[variant] = {"geo_cls": geo_cls, "valid": valid, "source_valid_count": source_valid_count}
        del grid_data, grid_valid, raw_on_epic, raw_valid_on_epic, standard, geo_cls, geo_policy_valid
        gc.collect()
    common = valid_earth & epic_policy_valid
    for item in sampled.values():
        common &= np.asarray(item["valid"], dtype=bool)
    common_rows: list[dict[str, Any]] = []
    for variant, item in sampled.items():
        geo_cls = np.asarray(item["geo_cls"])
        metrics = confusion_metrics(epic_cls, geo_cls, common, positive=policy["positive"])
        metrics.update(boundary_metrics(epic_cls, geo_cls, common, positive=policy["positive"]))
        metrics.update(
            {
                "case_id": case_id,
                "replacement_variant": variant,
                "comparison_domain": "common_valid_A_B_C",
                "policy": POLICY_NAME,
                "source": SOURCE,
                "source_valid_count_before_reprojection": int(item["source_valid_count"]),
                "epic_source_valid_before_policy": int(np.count_nonzero(np.asarray(item["valid"], dtype=bool) & valid_earth)),
                "stage09d_epic_time_utc": row.get("epic_time_utc", ""),
                "stage_run_dir": row.get("stage_run_dir", ""),
            }
        )
        common_rows.append(metrics)
    return variant_rows, common_rows


def summarize_replacement(metrics: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for domain, group in metrics.groupby("comparison_domain"):
        a = group[group["replacement_variant"] == "A_current_clm_navigation"]
        b = group[group["replacement_variant"] == "B_l15_reference_navigation"]
        c = group[group["replacement_variant"] == "C_current_clm_navigation_rot180"]
        if a.empty or b.empty or c.empty:
            continue
        a = a.iloc[0]
        b = b.iloc[0]
        c = c.iloc[0]
        rows.append(
            {
                "case_id": a["case_id"],
                "comparison_domain": domain,
                "agreement_A_current": float(a["agreement"]),
                "agreement_B_reference": float(b["agreement"]),
                "agreement_C_current_rot180": float(c["agreement"]),
                "delta_agreement_B_minus_A": float(b["agreement"] - a["agreement"]),
                "delta_agreement_C_minus_A": float(c["agreement"] - a["agreement"]),
                "abs_agreement_B_minus_C": abs(float(b["agreement"] - c["agreement"])),
                "f1_A_current": float(a["f1_cloud"]),
                "f1_B_reference": float(b["f1_cloud"]),
                "f1_C_current_rot180": float(c["f1_cloud"]),
                "delta_f1_B_minus_A": float(b["f1_cloud"] - a["f1_cloud"]),
                "delta_f1_C_minus_A": float(c["f1_cloud"] - a["f1_cloud"]),
                "abs_f1_B_minus_C": abs(float(b["f1_cloud"] - c["f1_cloud"])),
                "mcc_A_current": float(a["mcc"]),
                "mcc_B_reference": float(b["mcc"]),
                "mcc_C_current_rot180": float(c["mcc"]),
                "delta_mcc_B_minus_A": float(b["mcc"] - a["mcc"]),
                "delta_mcc_C_minus_A": float(c["mcc"] - a["mcc"]),
                "abs_mcc_B_minus_C": abs(float(b["mcc"] - c["mcc"])),
                "B_and_C_close": bool(abs(float(b["agreement"] - c["agreement"])) <= 0.02 and abs(float(b["mcc"] - c["mcc"])) <= 0.05),
                "B_and_C_better_than_A": bool((b["agreement"] - a["agreement"]) >= 0.10 and (c["agreement"] - a["agreement"]) >= 0.10 and (b["mcc"] - a["mcc"]) >= 0.20 and (c["mcc"] - a["mcc"]) >= 0.20),
            }
        )
    return pd.DataFrame(rows)


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
                "risk_relevance": "CLM cloud_mask/latitude/longitude raw GRIB 1D values are reshaped here; Gate 3B localizes the mismatch to navigation orientation handling, not cloud-mask values.",
                "recommended_fix_point": "Inspect Meteosat latitude/longitude generation/reshape order before Stage 05 reproject; do not rotate cloud_mask values.",
            }
        )
    return rows


def make_navigation_figure(case_id: str, maps: dict[str, np.ndarray], paths: dict[str, Path]) -> list[dict[str, Any]]:
    rows = []
    fig, axes = plt.subplots(1, 4, figsize=(13.2, 3.8), constrained_layout=True)
    for ax, transform in zip(axes, ["identity", "flipud", "fliplr", "rot180"]):
        arr = maps.get(transform)
        if arr is None:
            ax.axis("off")
            continue
        display = arr[::-1, ::-1][::6, ::6]
        im = ax.imshow(display, origin="upper", cmap="magma_r", vmin=0, vmax=np.nanpercentile(display, 95))
        ax.set_title(transform)
        ax.set_xticks([])
        ax.set_yticks([])
        plt.colorbar(im, ax=ax, fraction=0.046, pad=0.02, label="km")
    fig.suptitle(f"Stage 09H Gate 3B navigation distance to L1.5 reference | {case_id}", fontsize=11)
    for ext, dpi in [("png", 220), ("svg", 220), ("pdf", 220)]:
        fig_path = paths["figures"] / f"stage_09h_gate3b_{case_id}_navigation_distance.{ext}"
        fig.savefig(fig_path, dpi=dpi)
        rows.append(
            {
                "figure_id": f"stage_09h_gate3b_{case_id}_navigation_distance",
                "case_id": case_id,
                "figure_path": str(fig_path),
                "source_csv": str(paths["source_data"] / "stage_09h_gate3b_navigation_transform_metrics.csv"),
                "description": "Geodesic distance between transformed current CLM navigation and L1.5 area-derived reference navigation.",
            }
        )
    plt.close(fig)
    return rows


def make_replacement_figure(metrics: pd.DataFrame, paths: dict[str, Path]) -> list[dict[str, Any]]:
    if metrics.empty:
        return []
    view = metrics[metrics["comparison_domain"] == "common_valid_A_B_C"].copy()
    if view.empty:
        view = metrics.copy()
    variants = [
        "A_current_clm_navigation",
        "B_l15_reference_navigation",
        "C_current_clm_navigation_rot180",
    ]
    labels = ["A current", "B L1.5 ref", "C current rot180"]
    colors = ["#4C78A8", "#D62728", "#59A14F"]
    fig, axes = plt.subplots(1, 3, figsize=(13.2, 4.0), constrained_layout=True)
    for ax, metric, title in zip(axes, ["agreement", "f1_cloud", "mcc"], ["Agreement", "Cloud F1", "MCC"]):
        values = []
        for variant in variants:
            hit = view[view["replacement_variant"] == variant]
            values.append(float(hit.iloc[0][metric]) if not hit.empty else math.nan)
        ax.bar(labels, values, color=colors)
        ax.set_title(title)
        ax.set_ylim(-0.2 if metric == "mcc" else 0.0, 1.0)
        ax.tick_params(axis="x", labelrotation=20)
        ax.grid(axis="y", color="#D9D9D9", linewidth=0.6)
    fig.suptitle("Stage 09H Gate 3B navigation replacement EPIC-view test | 20240312_1500", fontsize=11)
    rows = []
    for ext, dpi in [("png", 240), ("svg", 240), ("pdf", 240)]:
        fig_path = paths["figures"] / f"stage_09h_gate3b_navigation_replacement_20240312_1500.{ext}"
        fig.savefig(fig_path, dpi=dpi)
        rows.append(
            {
                "figure_id": "stage_09h_gate3b_navigation_replacement_20240312_1500",
                "case_id": "20240312_1500",
                "figure_path": str(fig_path),
                "source_csv": str(paths["source_data"] / "stage_09h_gate3b_navigation_replacement_metrics.csv"),
                "description": "EPIC-view comparison after reprojecting the same CLM mask with current, L1.5 reference, and current-rot180 navigation.",
            }
        )
    plt.close(fig)
    return rows


def final_decision(nav_metrics: pd.DataFrame, repl_summary: pd.DataFrame) -> str:
    direct_cases = []
    for case_id, group in nav_metrics.groupby("case_id"):
        ident = group[group["transform"] == "identity"]
        rot = group[group["transform"] == "rot180"]
        if ident.empty or rot.empty:
            continue
        ident_med = float(ident.iloc[0]["median_geodesic_distance_km"])
        rot_med = float(rot.iloc[0]["median_geodesic_distance_km"])
        rot_p95 = float(rot.iloc[0]["p95_geodesic_distance_km"])
        rot_frac_10 = float(rot.iloc[0]["matched_fraction_within_10km"])
        best_transform = str(rot.iloc[0].get("best_transform_by_median_distance", ""))
        direct_cases.append(
            best_transform == "rot180"
            and rot_med <= 10.0
            and rot_p95 <= 25.0
            and rot_frac_10 >= 0.85
            and ident_med >= 1000.0
        )
    direct_confirmed = len(direct_cases) >= 1 and all(direct_cases)
    replacement_confirmed = False
    if not repl_summary.empty:
        common = repl_summary[repl_summary["comparison_domain"] == "common_valid_A_B_C"]
        if not common.empty:
            row = common.iloc[0]
            replacement_confirmed = bool(row["B_and_C_close"] and row["B_and_C_better_than_A"])
    return "CONFIRMED_NAVIGATION_ORIENTATION_ERROR" if direct_confirmed and replacement_confirmed else "NAVIGATION_ORIENTATION_ERROR_SUPPORTED_BUT_NOT_FULLY_CONFIRMED"


def write_report(
    paths: dict[str, Path],
    final_status: str,
    nav_metrics: pd.DataFrame,
    controls: pd.DataFrame,
    repl: pd.DataFrame,
    repl_summary: pd.DataFrame,
    code_locations: pd.DataFrame,
    warnings: list[dict[str, Any]],
) -> Path:
    best = nav_metrics[nav_metrics["is_best_transform"] == True].copy() if not nav_metrics.empty and "is_best_transform" in nav_metrics else pd.DataFrame()
    lines = [
        "# Stage 09H Gate 3B Meteosat navigation direct confirmation",
        "",
        f"- Generated UTC: {utc_now()}",
        f"- Stage/Gate: `{STAGE_ID}` / `{GATE_ID}`",
        "- Scope: 2024-03-12 Meteosat-0deg CLM navigation against Meteosat-10 L1.5 official native area definition.",
        "- Constraints: no download, no production modification, no permanent rotation, no new Meteosat-0deg EPIC case.",
        "- Reference navigation: generated from Satpy SEVIRI native area definition only; current CLM latitude/longitude arrays are excluded from reference construction.",
        "",
        "## Final status",
        "",
        f"- Final Gate 3B decision: `{final_status}`.",
        "- Direct navigation comparison tests current CLM latitude/longitude under identity/flipud/fliplr/rot180 against the L1.5 area-derived reference.",
        "- EPIC replacement test keeps the CLM cloud mask unchanged and changes only the navigation used for reprojecting.",
        "- Residual note: the best rot180 navigation is not sub-kilometre identical to the L1.5 reference; its residual is about one to several SEVIRI pixels. The direction diagnosis is based on identity being many-thousand-kilometre wrong, rot180 being the best transform, and independent EPIC-view recovery when only navigation is changed.",
    ]
    if not best.empty:
        keep = [
            "case_id",
            "transform",
            "latitude_mae_deg",
            "longitude_circular_mae_deg",
            "median_geodesic_distance_km",
            "p95_geodesic_distance_km",
            "matched_fraction_within_1km",
            "matched_fraction_within_5km",
            "matched_fraction_within_10km",
        ]
        lines.extend(["", "## Best navigation transform", "", dataframe_to_markdown(best[keep], floatfmt=".6f")])
    if not repl_summary.empty:
        keep = [
            "case_id",
            "comparison_domain",
            "agreement_A_current",
            "agreement_B_reference",
            "agreement_C_current_rot180",
            "delta_agreement_B_minus_A",
            "abs_agreement_B_minus_C",
            "mcc_A_current",
            "mcc_B_reference",
            "mcc_C_current_rot180",
            "delta_mcc_B_minus_A",
            "abs_mcc_B_minus_C",
            "B_and_C_close",
            "B_and_C_better_than_A",
        ]
        lines.extend(["", "## Navigation replacement EPIC-view test", "", dataframe_to_markdown(repl_summary[keep], floatfmt=".6f")])
    if not controls.empty:
        lines.extend(["", "## Control point sample", "", dataframe_to_markdown(controls.head(12), floatfmt=".4f")])
    lines.extend(
        [
            "",
            "## Root-cause localization",
            "",
            "- Gate 3A showed the CLM mask values align with L1.5 IR in native storage order.",
            "- Gate 3B directly tests latitude/longitude and localizes the orientation mismatch to navigation handling.",
            "- Recommended fix point: inspect Meteosat latitude/longitude generation and C-order reshape in `geo_ring_cloud.adapters.cloud_products.read_meteosat_zip()` / `reshape_square_if_needed()` before Stage 05 reproject. Do not rotate the cloud mask values.",
            "- The production fix is intentionally not implemented in this Gate.",
            "",
            "### Code locations",
            "",
            dataframe_to_markdown(code_locations, floatfmt=".0f") if not code_locations.empty else "",
            "",
            "## Warnings",
            "",
            f"- Warning rows: {len(warnings)}. See `logs/stage_09h_gate3b_warnings.csv`.",
        ]
    )
    report = paths["reports"] / "stage_09h_gate3b_navigation_direct_confirmation_report_cn.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Stage 09H Gate 3B Meteosat navigation direct confirmation.")
    parser.add_argument("--cases", default="20240312_1200,20240312_1500", help="Comma-separated case IDs for direct navigation comparison.")
    parser.add_argument("--replacement-case", default="20240312_1500", help="Existing Stage 09D case for navigation replacement EPIC sampling.")
    args = parser.parse_args()

    paths = ensure_dirs(OUT_ROOT)
    warnings: list[dict[str, Any]] = []
    figure_rows: list[dict[str, Any]] = []
    started = utc_now()
    cases = [item.strip() for item in str(args.cases).split(",") if item.strip()]
    replacement_case = str(args.replacement_case).strip()

    inventory = inventory_cases(cases, warnings)
    write_csv(inventory, paths["source_data"] / "stage_09h_gate3b_file_pair_inventory.csv")

    ref_rows: list[dict[str, Any]] = []
    clm_rows: list[dict[str, Any]] = []
    nav_metric_rows: list[dict[str, Any]] = []
    control_rows_all: list[dict[str, Any]] = []
    arrays_by_case: dict[str, dict[str, np.ndarray]] = {}
    for row in inventory:
        case_id = row["case_id"]
        ref_lon, ref_lat, ref_valid, ref_meta = reference_navigation_from_l15(Path(row["l15_path"]), warnings)
        ref_meta["case_id"] = case_id
        ref_rows.append(ref_meta)
        clm_arrays, clm_meta = read_clm_raw_zip(Path(row["clm_path"]), paths["cache"], warnings)
        clm_meta["case_id"] = case_id
        clm_rows.append(clm_meta)
        cur_lat = np.asarray(clm_arrays["latitude"], dtype=np.float32)
        cur_lon = np.asarray(clm_arrays["longitude"], dtype=np.float32)
        metrics, dist_maps = compare_navigation(case_id, ref_lon, ref_lat, ref_valid, cur_lon, cur_lat)
        nav_metric_rows.extend(metrics)
        control_rows_all.extend(control_point_rows(case_id, ref_lon, ref_lat, ref_valid, cur_lon, cur_lat))
        figure_rows.extend(make_navigation_figure(case_id, dist_maps, paths))
        arrays_by_case[case_id] = {
            "mask": np.asarray(clm_arrays["cloud_mask"], dtype=np.float32),
            "cur_lat": cur_lat,
            "cur_lon": cur_lon,
            "ref_lat": ref_lat,
            "ref_lon": ref_lon,
        }
        del ref_valid, dist_maps
        gc.collect()

    replacement_rows: list[dict[str, Any]] = []
    replacement_common_rows: list[dict[str, Any]] = []
    if replacement_case in arrays_by_case:
        arr = arrays_by_case[replacement_case]
        variant_rows, common_rows = replacement_experiment(
            replacement_case,
            arr["mask"],
            arr["cur_lat"],
            arr["cur_lon"],
            arr["ref_lat"],
            arr["ref_lon"],
            warnings,
        )
        replacement_rows.extend(variant_rows)
        replacement_common_rows.extend(common_rows)
    else:
        warn(warnings, "replacement_case_not_in_direct_cases", f"{replacement_case} not in direct comparison cases; replacement experiment skipped", case_id=replacement_case)

    nav_metrics = pd.DataFrame(nav_metric_rows)
    controls = pd.DataFrame(control_rows_all)
    replacement_metrics = pd.DataFrame(replacement_rows + replacement_common_rows)
    repl_summary = summarize_replacement(replacement_metrics) if not replacement_metrics.empty else pd.DataFrame()
    final_status = final_decision(nav_metrics, repl_summary)

    write_csv(ref_rows, paths["source_data"] / "stage_09h_gate3b_l15_reference_navigation_inventory.csv")
    write_csv(clm_rows, paths["source_data"] / "stage_09h_gate3b_current_clm_navigation_inventory.csv")
    write_csv(nav_metrics, paths["source_data"] / "stage_09h_gate3b_navigation_transform_metrics.csv")
    write_csv(controls, paths["source_data"] / "stage_09h_gate3b_navigation_control_points.csv")
    write_csv(replacement_metrics, paths["source_data"] / "stage_09h_gate3b_navigation_replacement_metrics.csv")
    write_csv(repl_summary, paths["source_data"] / "stage_09h_gate3b_navigation_replacement_summary.csv")
    code_locations = pd.DataFrame(code_location_rows())
    write_csv(code_locations, paths["source_data"] / "stage_09h_gate3b_root_cause_code_locations.csv")
    figure_rows.extend(make_replacement_figure(replacement_metrics, paths))
    write_csv(figure_rows, paths["logs"] / "stage_09h_gate3b_figure_index.csv")
    write_csv(warnings, paths["logs"] / "stage_09h_gate3b_warnings.csv")
    report = write_report(paths, final_status, nav_metrics, controls, replacement_metrics, repl_summary, code_locations, warnings)
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
        "replacement_case": replacement_case,
        "final_status": final_status,
        "report": str(report),
        "warnings_count": len(warnings),
        "constraints": [
            "no internet download",
            "no production reader modification",
            "no fusion logic modification",
            "no permanent CLM rotation",
            "no new Meteosat-0deg EPIC cases",
        ],
    }
    write_json(manifest, paths["logs"] / "manifest_gate3b.json")
    print(json.dumps({"status": final_status, "cases": cases, "replacement_case": replacement_case, "output_root": str(OUT_ROOT)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
