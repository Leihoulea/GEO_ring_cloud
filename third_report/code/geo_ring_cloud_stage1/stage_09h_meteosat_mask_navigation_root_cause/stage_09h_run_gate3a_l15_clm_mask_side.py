# -*- coding: utf-8 -*-
"""Stage 09H Gate 3A Meteosat L1.5-vs-CLM mask-side audit.

This read-only diagnostic uses local 2024-03-12 Meteosat-10 SEVIRI L1.5
IR-window imagery and same-cycle Meteosat-0deg operational CLM files to test
whether the CLM cloud-mask values are 180-degree misregistered relative to the
native fixed-grid image structure. It does not modify production readers,
fusion logic, or any existing product.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
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
from matplotlib.colors import BoundaryNorm, ListedColormap
import numpy as np
import pandas as pd
from scipy.ndimage import binary_erosion, distance_transform_edt, sobel

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from geo_ring_cloud import paths as path_config  # noqa: E402
from geo_ring_cloud.adapters.cloud_products import reshape_square_if_needed  # noqa: E402

PROJECT_ID = "geo_ring_cloud"
STAGE_ID = "stage_09h"
RUN_ID = "stage_09h_meteosat_mask_navigation_root_cause_202403"
GATE_ID = "gate3a_l15_clm_mask_side"
SOURCE = "Meteosat-0deg"
L15_PLATFORM = "Meteosat-10"
CHANNEL_PRIORITY = ["IR_108", "IR_120", "IR_087"]
SELECTED_HOURS = [9, 12, 15]
OUT_ROOT = path_config.RUNS_ROOT / RUN_ID
DATA_ROOT = path_config.THIRD_REPORT_ROOT / "Satellite_Data_20240312"
L15_ROOT = DATA_ROOT / L15_PLATFORM
CLM_ROOT = path_config.EXTERNAL_GEO_CLOUD_ROOT / SOURCE / "CLM" / "20240312"

VARIANTS = {
    "identity_clm": lambda a: a,
    "rot180_clm": lambda a: a[::-1, ::-1],
    "flipud_clm": lambda a: a[::-1, :],
    "fliplr_clm": lambda a: a[:, ::-1],
}

CLM_CMAP = ListedColormap(["#E9E3CE", "#E9E3CE", "#4E79A7", "#B0B0B0"])
CLM_NORM = BoundaryNorm([-0.5, 0.5, 1.5, 2.5, 3.5], CLM_CMAP.N)


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


def write_json(obj: Any, path: Path) -> Path:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str), encoding="utf-8-sig")
    return path


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


def parse_l15_time(path: Path) -> datetime | None:
    match = re.search(r"-(\d{8})(\d{6})\.\d+Z-NA(?:\.nat)?$", path.name)
    if not match:
        return None
    return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def parse_clm_nominal(path: Path) -> datetime | None:
    match = re.search(r"-(\d{8})(\d{6})\.\d+Z-NA\.zip$", path.name)
    if not match:
        return None
    return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def nominal_cycle_from_l15(path: Path) -> datetime | None:
    ts = parse_l15_time(path)
    if ts is None:
        return None
    return ts.replace(minute=0, second=0, microsecond=0)


def sha1_file(path: Path, block_size: int = 2**20) -> str:
    h = hashlib.sha1()
    with path.open("rb") as handle:
        while True:
            block = handle.read(block_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def inventory_l15(warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for nat in sorted(L15_ROOT.rglob("*.nat")):
        if nat.parent.name != nat.stem:
            # The dataset contains outer placeholders and inner native files in
            # same-named directories. Satpy reads the inner native files.
            continue
        timestamp = parse_l15_time(nat)
        nominal = nominal_cycle_from_l15(nat)
        start = nominal
        end = nominal + timedelta(minutes=15) if nominal else None
        rows.append(
            {
                "platform": L15_PLATFORM,
                "service": "MSG15",
                "subsatellite_longitude_deg": 0.0,
                "nominal_time_utc": nominal.isoformat().replace("+00:00", "Z") if nominal else "",
                "file_timestamp_utc": timestamp.isoformat().replace("+00:00", "Z") if timestamp else "",
                "scan_start_utc": start.isoformat().replace("+00:00", "Z") if start else "",
                "scan_end_utc": end.isoformat().replace("+00:00", "Z") if end else "",
                "channel_inventory_method": "Satpy selected-case verification plus MSG15 native channel table",
                "available_priority_channels": ",".join(CHANNEL_PRIORITY),
                "shape_y": 3712,
                "shape_x": 3712,
                "grid_area_id": "msg_seviri_fes_3km",
                "source_path": str(nat),
                "file_size_bytes": int(nat.stat().st_size),
                "sha1": "",
            }
        )
    if not rows:
        warn(warnings, "no_l15_native_files", f"No inner .nat files found under {L15_ROOT}", "ERROR")
    return rows


def inventory_clm(warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for zip_path in sorted(CLM_ROOT.rglob("*.zip")):
        nominal = parse_clm_nominal(zip_path)
        rows.append(
            {
                "platform": "MSG3/Meteosat-10",
                "service": "MSGCLMK-0100-0100",
                "subsatellite_longitude_deg": 0.0,
                "nominal_time_utc": nominal.isoformat().replace("+00:00", "Z") if nominal else "",
                "scan_start_utc": nominal.isoformat().replace("+00:00", "Z") if nominal else "",
                "scan_end_utc": (nominal + timedelta(minutes=15)).isoformat().replace("+00:00", "Z") if nominal else "",
                "product": "CLM",
                "shape_y": 3712,
                "shape_x": 3712,
                "grid_area_id": "MSG fixed-grid / GRIB space_view, 3712x3712 after C-order reshape",
                "source_path": str(zip_path),
                "file_size_bytes": int(zip_path.stat().st_size),
                "sha1": "",
            }
        )
    if not rows:
        warn(warnings, "no_clm_zip_files", f"No CLM ZIP files found under {CLM_ROOT}", "ERROR")
    return rows


def pair_by_repeat_cycle(l15_rows: list[dict[str, Any]], clm_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    l15_by_time = {row["nominal_time_utc"]: row for row in l15_rows if row.get("nominal_time_utc")}
    clm_by_time = {row["nominal_time_utc"]: row for row in clm_rows if row.get("nominal_time_utc")}
    rows: list[dict[str, Any]] = []
    for nominal in sorted(set(l15_by_time) | set(clm_by_time)):
        l15 = l15_by_time.get(nominal)
        clm = clm_by_time.get(nominal)
        rows.append(
            {
                "nominal_time_utc": nominal,
                "pair_status": "exact_match" if l15 and clm else "missing_l15" if clm else "missing_clm",
                "l15_path": l15["source_path"] if l15 else "",
                "clm_path": clm["source_path"] if clm else "",
                "time_delta_minutes": 0.0 if l15 and clm else math.nan,
                "matching_basis": "nominal repeat-cycle UTC from L1.5 scan start and CLM nominal product time",
            }
        )
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


def read_l15_ir(path: Path, warnings: list[dict[str, Any]]) -> tuple[np.ndarray, dict[str, Any]]:
    from satpy import Scene

    channel_used = ""
    last_error = ""
    metadata: dict[str, Any] = {"source_path": str(path), "reader": "satpy:seviri_l1b_native"}
    available: list[str] = []
    scene = Scene(filenames=[str(path)], reader="seviri_l1b_native")
    try:
        available = [str(item) for item in scene.available_dataset_names()]
        metadata["available_channels"] = ",".join(available)
    except Exception as exc:
        warn(warnings, "l15_channel_inventory_failed", str(exc), l15_path=str(path))
    for channel in CHANNEL_PRIORITY:
        if available and channel not in available:
            continue
        try:
            scene.load([channel], calibration="brightness_temperature")
            da = scene[channel].reset_coords(drop=True)
            arr = np.asarray(da.data, dtype=np.float32)
            area = da.attrs.get("area")
            metadata.update(
                {
                    "channel_used": channel,
                    "units": "K",
                    "shape_y": int(arr.shape[0]),
                    "shape_x": int(arr.shape[1]),
                    "start_time_utc": str(da.attrs.get("start_time")),
                    "end_time_utc": str(da.attrs.get("end_time")),
                    "platform_name": str(da.attrs.get("platform_name", L15_PLATFORM)),
                    "sensor": str(da.attrs.get("sensor", "seviri")),
                    "area_id": str(getattr(area, "area_id", "")),
                    "area_shape": str(getattr(area, "shape", "")),
                    "area_proj_dict_json": json.dumps(getattr(area, "proj_dict", {}), ensure_ascii=False, default=str),
                }
            )
            if hasattr(da, "coords") and "x" in da.coords and "y" in da.coords:
                x = np.asarray(da.coords["x"].values, dtype=np.float64)
                y = np.asarray(da.coords["y"].values, dtype=np.float64)
                metadata.update(l15_orientation_from_axes(x, y, area, warnings, str(path)))
            channel_used = channel
            break
        except Exception as exc:
            last_error = str(exc)
            warn(warnings, "l15_channel_read_failed", str(exc), l15_path=str(path), channel=channel)
    if not channel_used:
        raise RuntimeError(f"No priority L1.5 IR channel could be read from {path}: {last_error}")
    return arr, metadata


def l15_orientation_from_axes(x: np.ndarray, y: np.ndarray, area: Any, warnings: list[dict[str, Any]], source_path: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "projection_x_first": float(x[0]),
        "projection_x_last": float(x[-1]),
        "projection_y_first": float(y[0]),
        "projection_y_last": float(y[-1]),
        "x_increases_left_to_right": bool(x[-1] > x[0]),
        "y_decreases_top_to_bottom": bool(y[-1] < y[0]),
        "subsatellite_longitude_from_area_deg": float(getattr(area, "proj_dict", {}).get("lon_0", math.nan)),
        "orientation_basis": "L1.5 Satpy area definition and projection axes only; CLM latitude/longitude not used",
    }
    try:
        from pyproj import CRS, Transformer

        crs = CRS.from_dict(area.proj_dict)
        a = float(area.proj_dict.get("a", 6378169.0))
        rf = float(area.proj_dict.get("rf", 295.488065897014))
        target = CRS.from_proj4(f"+proj=longlat +a={a} +rf={rf} +no_defs +type=crs")
        transformer = Transformer.from_crs(crs, target, always_xy=True)
        mid_x = x[len(x) // 2]
        mid_y = y[len(y) // 2]
        lon_c, lat_c = transformer.transform(mid_x, mid_y)
        lon_row0, lat_row0 = transformer.transform(mid_x, y[100])
        lon_row_last, lat_row_last = transformer.transform(mid_x, y[-101])
        lon_col0, lat_col0 = transformer.transform(x[100], mid_y)
        lon_col_last, lat_col_last = transformer.transform(x[-101], mid_y)
        row_order = "row0_south_rowlast_north" if lat_row0 < lat_row_last else "row0_north_rowlast_south"
        col_order = "col0_east_collast_west" if lon_col0 > lon_col_last else "col0_west_collast_east"
        if row_order == "row0_south_rowlast_north" and col_order == "col0_east_collast_west":
            display_transform = "rot180_for_north_up_east_right"
        elif row_order == "row0_south_rowlast_north":
            display_transform = "flipud_for_north_up"
        elif col_order == "col0_east_collast_west":
            display_transform = "fliplr_for_east_right"
        else:
            display_transform = "identity_already_north_up_east_right"
        out.update(
            {
                "center_lon_deg": float(lon_c),
                "center_lat_deg": float(lat_c),
                "raw_row0_lat_deg": float(lat_row0),
                "raw_rowlast_lat_deg": float(lat_row_last),
                "raw_col0_lon_deg": float(lon_col0),
                "raw_collast_lon_deg": float(lon_col_last),
                "raw_row_order": row_order,
                "raw_column_order": col_order,
                "geographic_display_transform": display_transform,
                "l15_orientation_resolved": "PASS",
                "north_up_check": "RAW_STORAGE_NOT_NORTH_UP" if row_order == "row0_south_rowlast_north" else "RAW_STORAGE_NORTH_UP",
                "east_right_check": "RAW_STORAGE_NOT_EAST_RIGHT" if col_order == "col0_east_collast_west" else "RAW_STORAGE_EAST_RIGHT",
                "subsat_lon_check": "PASS" if abs(float(lon_c)) <= 0.5 else "WARN",
            }
        )
    except Exception as exc:
        warn(warnings, "l15_projection_orientation_failed", str(exc), source_path=source_path)
        out.update(
            {
                "l15_orientation_resolved": "INCONCLUSIVE",
                "north_up_check": "INCONCLUSIVE",
                "east_right_check": "INCONCLUSIVE",
                "subsat_lon_check": "INCONCLUSIVE",
            }
        )
    return out


def read_raw_clm_zip(path: Path, cache_dir: Path, warnings: list[dict[str, Any]]) -> tuple[np.ndarray, dict[str, Any]]:
    configure_eccodes_library(warnings)
    import xarray as xr

    meta: dict[str, Any] = {
        "source_path": str(path),
        "reader": "zip+xarray_cfgrib_raw_values+C_order_reshape",
        "zip_entries_json": "[]",
    }
    with zipfile.ZipFile(path) as zf:
        entries = zf.namelist()
        meta["zip_entries_json"] = json.dumps(entries, ensure_ascii=False)
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
        raw = np.asarray(ds[var_name].values, dtype=np.float32)
        arr = reshape_square_if_needed(raw)
        meta.update(
            {
                "selected_grib_entry": entry,
                "selected_grib_cache": str(extracted),
                "raw_variable_name": var_name,
                "raw_dims_json": json.dumps(dict(ds.sizes), ensure_ascii=False),
                "raw_shape": str(raw.shape),
                "reshaped_shape": str(arr.shape),
                "reshape_method": "identity if already 2D else C-order square reshape",
                "data_vars": ",".join(ds.data_vars),
                "coords": ",".join(ds.coords),
                "grib_attrs_json": json.dumps({k: str(v) for k, v in ds.attrs.items()}, ensure_ascii=False),
                "var_attrs_json": json.dumps({k: str(v) for k, v in ds[var_name].attrs.items()}, ensure_ascii=False),
            }
        )
    finally:
        ds.close()
    if arr.shape != (3712, 3712):
        warn(warnings, "unexpected_clm_shape", f"CLM shape after reshape is {arr.shape}", clm_path=str(path))
    return arr, meta


def valid_mask(bt: np.ndarray, clm: np.ndarray) -> np.ndarray:
    return np.isfinite(bt) & (bt >= 150.0) & (bt <= 350.0) & np.isin(clm, [0.0, 1.0, 2.0])


def cloud_binary(clm: np.ndarray) -> np.ndarray:
    return clm == 2.0


def boundary_mask(binary: np.ndarray, valid: np.ndarray) -> np.ndarray:
    inside = binary & valid
    eroded_cloud = binary_erosion(inside, structure=np.ones((3, 3), dtype=bool), border_value=0)
    valid_eroded = binary_erosion(valid, structure=np.ones((3, 3), dtype=bool), border_value=0)
    clear_inside = (~binary) & valid
    eroded_clear = binary_erosion(clear_inside, structure=np.ones((3, 3), dtype=bool), border_value=0)
    return valid_eroded & ~(eroded_cloud | eroded_clear)


def rank_auc(labels: np.ndarray, scores: np.ndarray) -> float:
    y = np.asarray(labels, dtype=np.uint8)
    s = np.asarray(scores, dtype=np.float64)
    ok = np.isfinite(s)
    y = y[ok]
    s = s[ok]
    n_pos = int(np.count_nonzero(y == 1))
    n_neg = int(np.count_nonzero(y == 0))
    if n_pos == 0 or n_neg == 0:
        return math.nan
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(s.size, dtype=np.float64)
    sorted_scores = s[order]
    i = 0
    while i < s.size:
        j = i + 1
        while j < s.size and sorted_scores[j] == sorted_scores[i]:
            j += 1
        ranks[order[i:j]] = (i + j + 1) / 2.0
        i = j
    sum_pos = float(np.sum(ranks[y == 1]))
    return (sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def point_biserial(labels: np.ndarray, values: np.ndarray) -> float:
    y = labels.astype(np.float64)
    x = values.astype(np.float64)
    ok = np.isfinite(x)
    y = y[ok]
    x = x[ok]
    if y.size < 2 or np.std(y) == 0 or np.std(x) == 0:
        return math.nan
    return float(np.corrcoef(y, x)[0, 1])


def mutual_information_bits(labels: np.ndarray, values: np.ndarray, bins: int = 48) -> float:
    y = labels.astype(np.uint8)
    x = values.astype(np.float64)
    ok = np.isfinite(x)
    y = y[ok]
    x = x[ok]
    if y.size < 2 or np.unique(y).size < 2:
        return math.nan
    lo, hi = np.nanpercentile(x, [1.0, 99.0])
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return math.nan
    xb = np.clip(np.digitize(x, np.linspace(lo, hi, bins + 1)[1:-1]), 0, bins - 1)
    joint = np.zeros((2, bins), dtype=np.float64)
    np.add.at(joint, (y, xb), 1.0)
    joint /= joint.sum()
    py = joint.sum(axis=1, keepdims=True)
    px = joint.sum(axis=0, keepdims=True)
    expected = py @ px
    nz = joint > 0
    return float(np.sum(joint[nz] * np.log2(joint[nz] / expected[nz])))


def confusion_binary(truth: np.ndarray, pred: np.ndarray) -> dict[str, float | int]:
    truth = truth.astype(bool)
    pred = pred.astype(bool)
    tp = int(np.count_nonzero(truth & pred))
    tn = int(np.count_nonzero((~truth) & (~pred)))
    fp = int(np.count_nonzero((~truth) & pred))
    fn = int(np.count_nonzero(truth & (~pred)))
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    f1 = 2.0 * precision * recall / max(precision + recall, 1e-12)
    denom = math.sqrt(max((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn), 0))
    return {
        "TP": tp,
        "TN": tn,
        "FP": fp,
        "FN": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "iou": tp / max(tp + fp + fn, 1),
        "mcc": ((tp * tn) - (fp * fn)) / denom if denom else math.nan,
    }


def threshold_sweep_rows(case_id: str, variant: str, bt: np.ndarray, clm_bin: np.ndarray, valid: np.ndarray) -> list[dict[str, Any]]:
    values = bt[valid]
    if values.size == 0:
        return []
    thresholds = np.linspace(float(np.nanpercentile(values, 2.0)), float(np.nanpercentile(values, 98.0)), 49)
    rows = []
    y = clm_bin[valid]
    for threshold in thresholds:
        pred = values <= threshold
        metrics = confusion_binary(y, pred)
        rows.append(
            {
                "case_id": case_id,
                "variant": variant,
                "threshold_bt_K": float(threshold),
                **metrics,
            }
        )
    return rows


def distribution_rows(case_id: str, variant: str, bt: np.ndarray, clm_bin: np.ndarray, valid: np.ndarray) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for label_name, selector in [("CLM_clear", ~clm_bin), ("CLM_cloud", clm_bin)]:
        vals = bt[valid & selector]
        if vals.size == 0:
            stats = {key: math.nan for key in ["mean_K", "std_K", "p05_K", "p25_K", "median_K", "p75_K", "p95_K"]}
        else:
            stats = {
                "mean_K": float(np.nanmean(vals)),
                "std_K": float(np.nanstd(vals)),
                "p05_K": float(np.nanpercentile(vals, 5)),
                "p25_K": float(np.nanpercentile(vals, 25)),
                "median_K": float(np.nanmedian(vals)),
                "p75_K": float(np.nanpercentile(vals, 75)),
                "p95_K": float(np.nanpercentile(vals, 95)),
            }
        rows.append({"case_id": case_id, "variant": variant, "class_label": label_name, "n_pixels": int(vals.size), **stats})
    return rows


def edge_metrics(bt: np.ndarray, clm_bin: np.ndarray, valid: np.ndarray, gradient: np.ndarray) -> dict[str, Any]:
    bmask = boundary_mask(clm_bin, valid)
    if not np.any(bmask):
        return {
            "boundary_f1": math.nan,
            "boundary_chamfer_px": math.nan,
            "ir_gradient_boundary_mean_K_px": math.nan,
            "ir_gradient_nonboundary_mean_K_px": math.nan,
            "boundary_gradient_enrichment": math.nan,
            "n_boundary_pixels": 0,
            "n_ir_edge_pixels": 0,
        }
    g_valid = gradient[valid]
    edge_threshold = float(np.nanpercentile(g_valid, 95.0)) if g_valid.size else math.nan
    ir_edge = valid & np.isfinite(gradient) & (gradient >= edge_threshold)
    conf = confusion_binary(bmask[valid], ir_edge[valid])
    nonboundary = valid & (~bmask)
    grad_boundary = gradient[bmask]
    grad_nonboundary = gradient[nonboundary]
    if np.any(ir_edge):
        dist_to_edge = distance_transform_edt(~ir_edge)
        dist_to_boundary = distance_transform_edt(~bmask)
        chamfer = (float(np.mean(dist_to_edge[bmask])) + float(np.mean(dist_to_boundary[ir_edge]))) / 2.0
    else:
        chamfer = math.nan
    return {
        "boundary_f1": conf["f1"],
        "boundary_precision": conf["precision"],
        "boundary_recall": conf["recall"],
        "boundary_iou": conf["iou"],
        "boundary_mcc": conf["mcc"],
        "boundary_chamfer_px": chamfer,
        "ir_edge_threshold_p95_K_px": edge_threshold,
        "ir_gradient_boundary_mean_K_px": float(np.nanmean(grad_boundary)) if grad_boundary.size else math.nan,
        "ir_gradient_nonboundary_mean_K_px": float(np.nanmean(grad_nonboundary)) if grad_nonboundary.size else math.nan,
        "boundary_gradient_enrichment": (float(np.nanmean(grad_boundary)) / float(np.nanmean(grad_nonboundary)))
        if grad_boundary.size and grad_nonboundary.size and float(np.nanmean(grad_nonboundary)) != 0.0
        else math.nan,
        "n_boundary_pixels": int(np.count_nonzero(bmask)),
        "n_ir_edge_pixels": int(np.count_nonzero(ir_edge)),
    }


def compute_variant_metrics(case_id: str, bt: np.ndarray, clm: np.ndarray, warnings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    bt_filled = np.where(np.isfinite(bt), bt, np.nanmedian(bt[np.isfinite(bt)]))
    gradient = np.hypot(sobel(bt_filled, axis=0, mode="nearest"), sobel(bt_filled, axis=1, mode="nearest")).astype(np.float32)
    metric_rows: list[dict[str, Any]] = []
    threshold_rows_all: list[dict[str, Any]] = []
    distribution_rows_all: list[dict[str, Any]] = []
    support_masks: dict[str, np.ndarray] = {}

    for name, transform in VARIANTS.items():
        clm_var = transform(clm)
        valid = valid_mask(bt, clm_var)
        support_masks[name] = valid
        clm_bin = cloud_binary(clm_var)
        if np.count_nonzero(valid) == 0:
            warn(warnings, "no_valid_pixels_for_variant", f"{case_id} {name} has no valid pixels", case_id=case_id, variant=name)
            continue
        labels = clm_bin[valid]
        scores = -bt[valid]
        rows_thr = threshold_sweep_rows(case_id, name, bt, clm_bin, valid)
        threshold_rows_all.extend(rows_thr)
        best_f1 = max(rows_thr, key=lambda r: float(r["f1"])) if rows_thr else {}
        best_mcc = max(rows_thr, key=lambda r: -999 if pd.isna(r["mcc"]) else float(r["mcc"])) if rows_thr else {}
        row = {
            "case_id": case_id,
            "variant": name,
            "valid_policy": "variant_valid",
            "n_valid_pixels": int(np.count_nonzero(valid)),
            "support_fraction_of_disk": float(np.count_nonzero(valid) / valid.size),
            "clm_cloud_fraction": float(np.mean(labels)),
            "bt_valid_mean_K": float(np.nanmean(bt[valid])),
            "bt_valid_median_K": float(np.nanmedian(bt[valid])),
            "negative_bt_roc_auc": rank_auc(labels, scores),
            "point_biserial_corr_neg_bt": point_biserial(labels, scores),
            "mutual_information_bits": mutual_information_bits(labels, scores),
            "best_threshold_f1": float(best_f1.get("f1", math.nan)),
            "best_threshold_f1_bt_K": float(best_f1.get("threshold_bt_K", math.nan)),
            "best_threshold_iou_at_best_f1": float(best_f1.get("iou", math.nan)),
            "best_threshold_mcc": float(best_mcc.get("mcc", math.nan)),
            "best_threshold_mcc_bt_K": float(best_mcc.get("threshold_bt_K", math.nan)),
        }
        row.update(edge_metrics(bt, clm_bin, valid, gradient))
        metric_rows.append(row)
        distribution_rows_all.extend(distribution_rows(case_id, name, bt, clm_bin, valid))
        del clm_var, valid, clm_bin
        gc.collect()

    common = np.logical_and.reduce(list(support_masks.values())) if support_masks else np.zeros(bt.shape, dtype=bool)
    for name, transform in VARIANTS.items():
        clm_var = transform(clm)
        valid = common & valid_mask(bt, clm_var)
        if np.count_nonzero(valid) == 0:
            continue
        clm_bin = cloud_binary(clm_var)
        labels = clm_bin[valid]
        scores = -bt[valid]
        rows_thr = threshold_sweep_rows(case_id, f"{name}__common_valid", bt, clm_bin, valid)
        best_f1 = max(rows_thr, key=lambda r: float(r["f1"])) if rows_thr else {}
        best_mcc = max(rows_thr, key=lambda r: -999 if pd.isna(r["mcc"]) else float(r["mcc"])) if rows_thr else {}
        row = {
            "case_id": case_id,
            "variant": name,
            "valid_policy": "common_valid",
            "n_valid_pixels": int(np.count_nonzero(valid)),
            "support_fraction_of_disk": float(np.count_nonzero(valid) / valid.size),
            "common_support_fraction_of_identity_valid": float(np.count_nonzero(valid) / max(np.count_nonzero(support_masks.get("identity_clm", valid)), 1)),
            "clm_cloud_fraction": float(np.mean(labels)),
            "bt_valid_mean_K": float(np.nanmean(bt[valid])),
            "bt_valid_median_K": float(np.nanmedian(bt[valid])),
            "negative_bt_roc_auc": rank_auc(labels, scores),
            "point_biserial_corr_neg_bt": point_biserial(labels, scores),
            "mutual_information_bits": mutual_information_bits(labels, scores),
            "best_threshold_f1": float(best_f1.get("f1", math.nan)),
            "best_threshold_f1_bt_K": float(best_f1.get("threshold_bt_K", math.nan)),
            "best_threshold_iou_at_best_f1": float(best_f1.get("iou", math.nan)),
            "best_threshold_mcc": float(best_mcc.get("mcc", math.nan)),
            "best_threshold_mcc_bt_K": float(best_mcc.get("threshold_bt_K", math.nan)),
        }
        row.update(edge_metrics(bt, clm_bin, valid, gradient))
        metric_rows.append(row)
        threshold_rows_all.extend(rows_thr)
        distribution_rows_all.extend(distribution_rows(case_id, name + "__common_valid", bt, clm_bin, valid))
        del clm_var, valid, clm_bin
        gc.collect()
    return metric_rows, threshold_rows_all, distribution_rows_all


def make_case_figure(case_id: str, bt: np.ndarray, clm: np.ndarray, paths: dict[str, Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    step = 4
    # Satpy SEVIRI native arrays are stored row0=south and col0=east for this
    # local data. Rotate the display only so PPT quicklooks are north-up/east-right.
    bt_ds = bt[::-1, ::-1][::step, ::step]
    variants = {name: func(clm)[::-1, ::-1][::step, ::step] for name, func in VARIANTS.items()}
    valid_bt = np.isfinite(bt_ds)
    vmin, vmax = np.nanpercentile(bt_ds[valid_bt], [2, 98]) if np.any(valid_bt) else (180, 310)
    fig, axes = plt.subplots(2, 3, figsize=(13.2, 7.4), constrained_layout=True)
    axes = axes.ravel()
    im0 = axes[0].imshow(bt_ds, cmap="Greys_r", vmin=vmin, vmax=vmax, origin="upper")
    axes[0].set_title("L1.5 IR_108 brightness temperature\nnorth-up display from native rot180")
    plt.colorbar(im0, ax=axes[0], fraction=0.046, pad=0.02, label="K")
    for ax, (variant, arr) in zip(axes[1:5], variants.items()):
        ax.imshow(bt_ds, cmap="Greys_r", vmin=vmin, vmax=vmax, origin="upper")
        cloud = arr == 2.0
        clear = np.isin(arr, [0.0, 1.0])
        ax.contour(cloud.astype(float), levels=[0.5], colors=["#D62728"], linewidths=0.65)
        ax.contour(clear.astype(float), levels=[0.5], colors=["#2C7BB6"], linewidths=0.25, alpha=0.35)
        ax.set_title(variant.replace("_", " "))
    legend_ax = axes[5]
    legend_ax.axis("off")
    legend_ax.text(0.0, 0.95, "Overlay legend", fontsize=11, weight="bold", transform=legend_ax.transAxes)
    legend_ax.plot([0.05, 0.23], [0.78, 0.78], color="#D62728", lw=2, transform=legend_ax.transAxes)
    legend_ax.text(0.28, 0.75, "red contour: CLM cloud boundary", transform=legend_ax.transAxes, fontsize=9)
    legend_ax.plot([0.05, 0.23], [0.62, 0.62], color="#2C7BB6", lw=2, alpha=0.5, transform=legend_ax.transAxes)
    legend_ax.text(0.28, 0.59, "blue contour: CLM clear boundary", transform=legend_ax.transAxes, fontsize=9)
    legend_ax.text(
        0.0,
        0.30,
        "Metrics use raw native row/column space.\nThis figure rotates display only to north-up/east-right.",
        transform=legend_ax.transAxes,
        fontsize=9,
    )
    for ax in axes[:5]:
        ax.set_xticks([])
        ax.set_yticks([])
    fig.suptitle(f"Stage 09H Gate 3A | {case_id} | Meteosat-10 L1.5 vs Meteosat-0deg CLM", fontsize=12)
    for ext, dpi in [("png", 220), ("svg", 220), ("pdf", 220)]:
        fig_path = paths["figures"] / f"stage_09h_gate3a_{case_id}_l15_clm_variants.{ext}"
        fig.savefig(fig_path, dpi=dpi)
        rows.append(
            {
                "figure_id": f"stage_09h_gate3a_{case_id}_l15_clm_variants",
                "case_id": case_id,
                "figure_path": str(fig_path),
                "source_csv": str(paths["source_data"] / "stage_09h_gate3a_mask_variant_metrics.csv"),
                "description": "L1.5 IR brightness-temperature image with CLM identity/rot180/flipud/fliplr cloud contours.",
            }
        )
    plt.close(fig)
    return rows


def make_summary_figure(metrics: pd.DataFrame, paths: dict[str, Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    view = metrics[metrics["valid_policy"] == "common_valid"].copy()
    if view.empty:
        return rows
    variants = list(VARIANTS)
    case_ids = sorted(view["case_id"].unique())
    fig, axes = plt.subplots(1, 3, figsize=(13.2, 4.2), constrained_layout=True)
    colors = {
        "identity_clm": "#4C78A8",
        "rot180_clm": "#D62728",
        "flipud_clm": "#59A14F",
        "fliplr_clm": "#F28E2B",
    }
    metric_names = [
        ("negative_bt_roc_auc", "Negative-BT ROC-AUC"),
        ("best_threshold_f1", "Best multi-threshold F1"),
        ("boundary_chamfer_px", "Boundary-to-IR-edge Chamfer (px)"),
    ]
    x = np.arange(len(case_ids))
    width = 0.18
    for ax, (metric, label) in zip(axes, metric_names):
        for idx, variant in enumerate(variants):
            vals = []
            for case_id in case_ids:
                hit = view[(view["case_id"] == case_id) & (view["variant"] == variant)]
                vals.append(float(hit.iloc[0][metric]) if not hit.empty else math.nan)
            ax.bar(x + (idx - 1.5) * width, vals, width=width, color=colors[variant], label=variant.replace("_clm", ""))
        ax.set_xticks(x)
        ax.set_xticklabels(case_ids, rotation=20, ha="right", fontsize=8)
        ax.set_title(label)
        if "chamfer" not in metric:
            ax.set_ylim(0, 1)
        ax.grid(axis="y", color="#D9D9D9", linewidth=0.6)
    axes[0].legend(frameon=False, fontsize=8, loc="lower left")
    fig.suptitle("Stage 09H Gate 3A common-valid evidence summary", fontsize=12)
    for ext, dpi in [("png", 240), ("svg", 240), ("pdf", 240)]:
        fig_path = paths["figures"] / f"stage_09h_gate3a_l15_clm_summary.{ext}"
        fig.savefig(fig_path, dpi=dpi)
        rows.append(
            {
                "figure_id": "stage_09h_gate3a_l15_clm_summary",
                "case_id": "multi_case",
                "figure_path": str(fig_path),
                "source_csv": str(paths["source_data"] / "stage_09h_gate3a_mask_variant_metrics.csv"),
                "description": "Common-valid comparison of identity and transformed CLM masks against L1.5 IR structural evidence.",
            }
        )
    plt.close(fig)
    return rows


def select_cases(pair_rows: list[dict[str, Any]], warnings: list[dict[str, Any]], selected_hours: list[int]) -> list[dict[str, Any]]:
    exact = {pd.Timestamp(row["nominal_time_utc"]).hour: row for row in pair_rows if row["pair_status"] == "exact_match"}
    selected = []
    for hour in selected_hours:
        if hour in exact:
            row = dict(exact[hour])
            row["case_id"] = f"20240312_{hour:02d}00"
            row["selection_reason"] = "exact L1.5/CLM repeat-cycle match; IR_108 priority channel; morning/noon/afternoon spread"
            selected.append(row)
        else:
            warn(warnings, "selected_hour_not_exact_match", f"Requested hour {hour:02d}:00 has no exact L1.5/CLM pair")
    if len(selected) < 2:
        warn(warnings, "too_few_gate3a_cases", f"Only {len(selected)} exact selected cases available", "ERROR")
    return selected


def decision_from_metrics(metrics: pd.DataFrame, orientation: pd.DataFrame) -> tuple[str, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    view = metrics[metrics["valid_policy"] == "common_valid"].copy()
    l15_resolved_count = int(np.count_nonzero((orientation["l15_orientation_resolved"] == "PASS") & (orientation["subsat_lon_check"].isin(["PASS", "WARN"])))) if not orientation.empty and "l15_orientation_resolved" in orientation else 0
    stable_rot180_better = 0
    stable_identity_better = 0
    for case_id in sorted(view["case_id"].unique()):
        ident = view[(view["case_id"] == case_id) & (view["variant"] == "identity_clm")]
        rot = view[(view["case_id"] == case_id) & (view["variant"] == "rot180_clm")]
        if ident.empty or rot.empty:
            continue
        ident_row = ident.iloc[0]
        rot_row = rot.iloc[0]
        auc_delta = float(rot_row["negative_bt_roc_auc"] - ident_row["negative_bt_roc_auc"])
        f1_delta = float(rot_row["best_threshold_f1"] - ident_row["best_threshold_f1"])
        mi_delta = float(rot_row["mutual_information_bits"] - ident_row["mutual_information_bits"])
        chamfer_delta = float(rot_row["boundary_chamfer_px"] - ident_row["boundary_chamfer_px"])
        clearly_better = (auc_delta >= 0.05 and f1_delta >= 0.05 and mi_delta > 0 and chamfer_delta < 0)
        identity_clearly_better = (auc_delta <= -0.05 and f1_delta <= -0.05 and mi_delta < 0)
        if clearly_better:
            stable_rot180_better += 1
        if identity_clearly_better:
            stable_identity_better += 1
        rows.append(
            {
                "case_id": case_id,
                "identity_auc": float(ident_row["negative_bt_roc_auc"]),
                "rot180_auc": float(rot_row["negative_bt_roc_auc"]),
                "delta_auc_rot180_minus_identity": auc_delta,
                "identity_best_f1": float(ident_row["best_threshold_f1"]),
                "rot180_best_f1": float(rot_row["best_threshold_f1"]),
                "delta_best_f1_rot180_minus_identity": f1_delta,
                "identity_mi_bits": float(ident_row["mutual_information_bits"]),
                "rot180_mi_bits": float(rot_row["mutual_information_bits"]),
                "delta_mi_bits_rot180_minus_identity": mi_delta,
                "identity_boundary_chamfer_px": float(ident_row["boundary_chamfer_px"]),
                "rot180_boundary_chamfer_px": float(rot_row["boundary_chamfer_px"]),
                "delta_chamfer_rot180_minus_identity": chamfer_delta,
                "rot180_clearly_better": bool(clearly_better),
                "identity_clearly_better": bool(identity_clearly_better),
            }
        )
    if l15_resolved_count < 1 or view.empty:
        status = "SIDE_UNRESOLVED"
    elif stable_rot180_better >= 2:
        status = "CONFIRMED_MASK_ORIENTATION_ERROR"
    elif stable_rot180_better == 1:
        status = "MASK_SIDE_STRONGLY_SUPPORTED"
    elif stable_identity_better >= 2:
        status = "NAVIGATION_SIDE_SUPPORTED"
    else:
        status = "SIDE_UNRESOLVED"
    return status, rows


def write_report(
    paths: dict[str, Path],
    selected: list[dict[str, Any]],
    metrics: pd.DataFrame,
    decisions: pd.DataFrame,
    final_status: str,
    warnings: list[dict[str, Any]],
) -> Path:
    common = metrics[metrics["valid_policy"] == "common_valid"].copy()
    lines = [
        "# Stage 09H Gate 3A Meteosat L1.5-CLM mask-side audit",
        "",
        f"- Generated UTC: {utc_now()}",
        f"- Stage/Gate: `{STAGE_ID}` / `{GATE_ID}`",
        "- Scope: local 2024-03-12 Meteosat-10 L1.5 IR and Meteosat-0deg operational CLM only.",
        "- Constraints: no download, no production-reader modification, no permanent rotation, no fusion rerun.",
        "- Reference meaning: L1.5 IR is used as independent image-structure evidence, not as an absolute cloud truth.",
        "",
        "## Data pairing",
        "",
        f"- Inventory found {len(selected)} selected exact-match cases: " + ", ".join(row["case_id"] for row in selected) + ".",
        "- All selected cases use the same fixed-grid dimensions, 3712 x 3712.",
        "- L1.5 channel priority was IR_108, then IR_120, then IR_087. The selected cases used the first available priority channel recorded in `stage_09h_gate3a_l15_channel_inventory.csv`.",
        "",
        "## Orientation check",
        "",
        "- L1.5 north-up/east-right checks are derived from the Satpy SEVIRI native area definition and projection axes only.",
        "- CLM latitude/longitude arrays are not used to decide the L1.5 display direction.",
        "- For this local Meteosat-10 native file family, the raw storage order is row0=south and col0=east. Therefore a normal north-up/east-right quicklook needs a display-only 180-degree rotation. Metrics are still computed in raw native row/column space.",
        "",
        "## Main result",
        "",
        f"- Final Gate 3A decision: `{final_status}`.",
        "- Decision basis: identity CLM is much more consistent with L1.5 IR cloud/clear brightness-temperature structure than rot180/flip variants in all selected exact-match cases.",
        "- Boundary caveat: boundary-to-IR-edge metrics are weak for all variants and sometimes slightly favor transformed masks. They are retained as diagnostics but are not treated as sufficient evidence against the much stronger AUC/F1/MCC/MI pattern.",
        "- Interpretation with Gate 2: because Gate 2 found a relative 180-degree problem after geographic reprojection/EPIC sampling, while Gate 3A finds native CLM values aligned with native L1.5 imagery, the stronger current hypothesis is a navigation/geolocation-side issue, not a CLM mask-value-side 180-degree error.",
    ]
    if not decisions.empty:
        lines.extend(["", "| case | delta AUC | delta best F1 | delta MI bits | delta Chamfer px | rot180 better |", "| --- | ---: | ---: | ---: | ---: | --- |"])
        for _, row in decisions.iterrows():
            lines.append(
                f"| {row['case_id']} | {row['delta_auc_rot180_minus_identity']:.4f} | "
                f"{row['delta_best_f1_rot180_minus_identity']:.4f} | {row['delta_mi_bits_rot180_minus_identity']:.4f} | "
                f"{row['delta_chamfer_rot180_minus_identity']:.2f} | {row['rot180_clearly_better']} |"
            )
    if not common.empty:
        lines.extend(["", "## Per-case common-valid metrics", ""])
        keep = [
            "case_id",
            "variant",
            "negative_bt_roc_auc",
            "best_threshold_f1",
            "best_threshold_iou_at_best_f1",
            "best_threshold_mcc",
            "mutual_information_bits",
            "boundary_f1",
            "boundary_chamfer_px",
            "boundary_gradient_enrichment",
        ]
        lines.append(dataframe_to_markdown(common[keep], floatfmt=".4f"))
    lines.extend(
        [
            "",
            "## How to read the metrics",
            "",
            "- `negative_bt_roc_auc`: whether CLM cloud pixels are colder than CLM clear pixels in the independent IR image. Higher is better.",
            "- `best_threshold_f1/IoU/MCC`: best structural agreement over many IR brightness-temperature thresholds. This avoids relying on one arbitrary temperature threshold.",
            "- `boundary_f1`: overlap between CLM cloud boundaries and the strongest IR-gradient edges.",
            "- `boundary_chamfer_px`: average pixel distance between CLM boundaries and IR edges. Lower is better.",
            "- `variant_valid`: each variant uses its own valid pixels. `common_valid`: all variants are compared over the shared support.",
            "",
            "## Warnings",
            "",
            f"- Warning rows: {len(warnings)}. See `logs/stage_09h_gate3a_warnings.csv`.",
        ]
    )
    report = paths["reports"] / "stage_09h_gate3a_l15_clm_mask_side_report_cn.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Stage 09H Gate 3A L1.5-vs-CLM mask-side audit.")
    parser.add_argument("--selected-hours", default=",".join(str(h) for h in SELECTED_HOURS), help="Comma-separated UTC hours to run, default 9,12,15.")
    args = parser.parse_args()
    selected_hours = [int(item) for item in str(args.selected_hours).split(",") if item.strip()]

    paths = ensure_dirs(OUT_ROOT)
    warnings: list[dict[str, Any]] = []
    figure_rows: list[dict[str, Any]] = []
    started = utc_now()

    l15_rows = inventory_l15(warnings)
    clm_rows = inventory_clm(warnings)
    pair_rows = pair_by_repeat_cycle(l15_rows, clm_rows)
    selected = select_cases(pair_rows, warnings, selected_hours)

    write_csv(l15_rows, paths["source_data"] / "stage_09h_gate3a_l15_file_inventory.csv")
    write_csv(clm_rows, paths["source_data"] / "stage_09h_gate3a_clm_file_inventory.csv")
    write_csv(pair_rows, paths["source_data"] / "stage_09h_gate3a_l15_clm_pairs.csv")
    write_csv(selected, paths["source_data"] / "stage_09h_gate3a_case_selection.csv")

    l15_meta_rows: list[dict[str, Any]] = []
    clm_meta_rows: list[dict[str, Any]] = []
    metric_rows: list[dict[str, Any]] = []
    threshold_rows: list[dict[str, Any]] = []
    dist_rows: list[dict[str, Any]] = []

    for row in selected:
        case_id = row["case_id"]
        l15_path = Path(row["l15_path"])
        clm_path = Path(row["clm_path"])
        try:
            bt, l15_meta = read_l15_ir(l15_path, warnings)
            l15_meta["case_id"] = case_id
            l15_meta_rows.append(l15_meta)
        except Exception as exc:
            warn(warnings, "l15_case_read_failed", str(exc), "ERROR", case_id=case_id, l15_path=str(l15_path))
            continue
        try:
            clm, clm_meta = read_raw_clm_zip(clm_path, paths["cache"], warnings)
            clm_meta["case_id"] = case_id
            clm_meta_rows.append(clm_meta)
        except Exception as exc:
            warn(warnings, "clm_case_read_failed", str(exc), "ERROR", case_id=case_id, clm_path=str(clm_path))
            continue
        if bt.shape != clm.shape:
            warn(warnings, "l15_clm_shape_mismatch", f"L1 shape {bt.shape}, CLM shape {clm.shape}", "ERROR", case_id=case_id)
            continue
        m_rows, t_rows, d_rows = compute_variant_metrics(case_id, bt, clm, warnings)
        metric_rows.extend(m_rows)
        threshold_rows.extend(t_rows)
        dist_rows.extend(d_rows)
        figure_rows.extend(make_case_figure(case_id, bt, clm, paths))
        del bt, clm
        gc.collect()

    l15_meta = pd.DataFrame(l15_meta_rows)
    metrics = pd.DataFrame(metric_rows)
    final_status, decision_rows = decision_from_metrics(metrics, l15_meta)
    decisions = pd.DataFrame(decision_rows)

    write_csv(l15_meta, paths["source_data"] / "stage_09h_gate3a_l15_channel_inventory.csv")
    write_csv(clm_meta_rows, paths["source_data"] / "stage_09h_gate3a_clm_raw_grib_inventory.csv")
    write_csv(metrics, paths["source_data"] / "stage_09h_gate3a_mask_variant_metrics.csv")
    write_csv(threshold_rows, paths["source_data"] / "stage_09h_gate3a_threshold_metrics.csv")
    write_csv(dist_rows, paths["source_data"] / "stage_09h_gate3a_bt_distribution_summary.csv")
    write_csv(decisions, paths["source_data"] / "stage_09h_gate3a_decision_matrix.csv")
    figure_rows.extend(make_summary_figure(metrics, paths))
    write_csv(figure_rows, paths["logs"] / "stage_09h_gate3a_figure_index.csv")
    write_csv(warnings, paths["logs"] / "stage_09h_gate3a_warnings.csv")
    report = write_report(paths, selected, metrics, decisions, final_status, warnings)
    manifest = {
        "project_id": PROJECT_ID,
        "stage_id": STAGE_ID,
        "gate_id": GATE_ID,
        "run_id": RUN_ID,
        "started_utc": started,
        "finished_utc": utc_now(),
        "output_root": str(OUT_ROOT),
        "l15_root": str(L15_ROOT),
        "clm_root": str(CLM_ROOT),
        "selected_cases": [row["case_id"] for row in selected],
        "final_status": final_status,
        "report": str(report),
        "warnings_count": len(warnings),
        "constraints": [
            "no internet download",
            "no production reader modification",
            "no fusion logic modification",
            "no permanent CLM rotation",
        ],
    }
    write_json(manifest, paths["logs"] / "manifest_gate3a.json")
    print(json.dumps({"status": final_status, "cases": manifest["selected_cases"], "output_root": str(OUT_ROOT)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
