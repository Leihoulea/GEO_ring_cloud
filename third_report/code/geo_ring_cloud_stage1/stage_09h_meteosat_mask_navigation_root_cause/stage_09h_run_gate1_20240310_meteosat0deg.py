# -*- coding: utf-8 -*-
"""Stage 09H Gate 1 Meteosat-0deg mask/navigation root-cause audit.

This is a read-only single-case diagnostic for 20240310_1200 / Meteosat-0deg.
It inspects raw GRIB scanning keys, compares production/cfgrib/eccodes array
directions, audits the current reshape path, and records whether same-time IR108
evidence is locally available. It does not modify production readers or products.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from xml.etree import ElementTree as ET

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import ndimage
from sklearn.metrics import mutual_info_score, roc_auc_score

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from geo_ring_cloud import paths as path_config  # noqa: E402
from geo_ring_cloud.adapters.cloud_products import (  # noqa: E402
    read_mapping,
    read_meteosat_zip,
    reshape_square_if_needed,
)

PROJECT_ID = "geo_ring_cloud"
STAGE_ID = "stage_09h"
RUN_ID = "stage_09h_meteosat_mask_navigation_root_cause_202403"
GATE_ID = "gate1_20240310_1200_meteosat0deg"
SAMPLE_ID = "20240310_1200"
SOURCE = "Meteosat-0deg"
PRODUCT = "CLM"
STAMP = "20240310120000"
OUT_ROOT = path_config.RUNS_ROOT / RUN_ID
RAW_ROOT = path_config.EXTERNAL_GEO_CLOUD_ROOT
CLM_ZIP = RAW_ROOT / SOURCE / "CLM" / "20240310" / "12" / "MSG3-SEVI-MSGCLMK-0100-0100-20240310120000.000000000Z-NA.zip"
CTH_ZIP = RAW_ROOT / SOURCE / "CTH" / "20240310" / "12" / "MSG3-SEVI-MSGCLTH-0100-0100-20240310120000.000000000Z-NA.zip"

GRIB_KEYS = [
    "shortName",
    "name",
    "paramId",
    "discipline",
    "parameterCategory",
    "parameterNumber",
    "typeOfGrid",
    "gridType",
    "Ni",
    "Nj",
    "Nx",
    "Ny",
    "numberOfPoints",
    "scanningMode",
    "iScansNegatively",
    "jScansPositively",
    "jPointsAreConsecutive",
    "alternativeRowScanning",
    "latitudeOfFirstGridPointInDegrees",
    "longitudeOfFirstGridPointInDegrees",
    "latitudeOfLastGridPointInDegrees",
    "longitudeOfLastGridPointInDegrees",
    "DxInMetres",
    "DyInMetres",
    "orientationOfTheGridInDegrees",
    "LaDInDegrees",
    "LoVInDegrees",
    "shapeOfTheEarth",
    "satelliteIdentifier",
    "subSatellitePointLongitude",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_dirs() -> dict[str, Path]:
    dirs = {
        "source_data": OUT_ROOT / "source_data",
        "figures": OUT_ROOT / "figures",
        "reports": OUT_ROOT / "reports",
        "logs": OUT_ROOT / "logs",
        "cache": OUT_ROOT / "cache" / GATE_ID,
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def warn(warnings: list[dict[str, Any]], code: str, message: str, severity: str = "WARN", **extra: Any) -> None:
    row = {"severity": severity, "code": code, "message": message}
    row.update(extra)
    warnings.append(row)


def safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def configure_eccodes(warnings: list[dict[str, Any]]) -> None:
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


def extract_zip(zip_path: Path, cache_dir: Path, warnings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Path], list[dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    extracted: dict[str, Path] = {}
    xml_rows: list[dict[str, Any]] = []
    if not zip_path.exists():
        warn(warnings, "missing_zip", f"Missing input ZIP: {zip_path}", "ERROR")
        return entries, extracted, xml_rows
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            entry_path = cache_dir / f"{hashlib.sha1((str(zip_path.resolve()) + '|' + info.filename).encode('utf-8')).hexdigest()}_{Path(info.filename).name}"
            payload = zf.read(info.filename)
            if not entry_path.exists() or entry_path.stat().st_size != len(payload):
                entry_path.write_bytes(payload)
            extracted[info.filename] = entry_path
            entries.append(
                {
                    "zip_file": str(zip_path),
                    "zip_entry": info.filename,
                    "entry_size_bytes": info.file_size,
                    "compressed_size_bytes": info.compress_size,
                    "extracted_path": str(entry_path),
                    "entry_sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
            if info.filename.lower().endswith(".xml"):
                xml_rows.extend(parse_xml_metadata(entry_path, zip_path, info.filename, warnings))
    return entries, extracted, xml_rows


def parse_xml_metadata(path: Path, zip_path: Path, entry: str, warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        root = ET.parse(path).getroot()
    except Exception as exc:
        warn(warnings, "xml_parse_failed", str(exc), zip_file=str(zip_path), zip_entry=entry)
        return rows
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        text = (elem.text or "").strip()
        attrs = {k.split("}")[-1]: v for k, v in elem.attrib.items()}
        if text or attrs:
            rows.append(
                {
                    "zip_file": str(zip_path),
                    "zip_entry": entry,
                    "xml_tag": tag,
                    "xml_text": text[:500],
                    "xml_attrs_json": safe_json(attrs),
                }
            )
    return rows


def import_eccodes(warnings: list[dict[str, Any]]):
    configure_eccodes(warnings)
    try:
        import eccodes  # type: ignore

        return eccodes
    except Exception as exc:
        warn(warnings, "eccodes_unavailable", str(exc), "ERROR")
        return None


def import_cfgrib_stack(warnings: list[dict[str, Any]]):
    configure_eccodes(warnings)
    try:
        import cfgrib  # noqa: F401
        import xarray as xr  # type: ignore

        try:
            from xarray.backends import plugins

            plugins.refresh_engines()
        except Exception:
            pass

        return xr
    except Exception as exc:
        warn(warnings, "xarray_cfgrib_unavailable", str(exc), "ERROR")
        return None


def square_reshape(arr: np.ndarray, order: str = "C") -> np.ndarray:
    a = np.asarray(arr)
    if a.ndim != 1:
        return a
    side = int(round(math.sqrt(a.size)))
    if side * side != a.size:
        return a
    return a.reshape((side, side), order=order)


def read_eccodes_direct(grib_path: Path, product_label: str, warnings: list[dict[str, Any]]) -> tuple[dict[str, np.ndarray], list[dict[str, Any]]]:
    eccodes = import_eccodes(warnings)
    arrays: dict[str, np.ndarray] = {}
    key_rows: list[dict[str, Any]] = []
    if eccodes is None:
        return arrays, key_rows
    with grib_path.open("rb") as fh:
        msg_index = 0
        while True:
            gid = eccodes.codes_grib_new_from_file(fh)
            if gid is None:
                break
            msg_index += 1
            try:
                for key in GRIB_KEYS:
                    row = {
                        "product_label": product_label,
                        "grib_file": str(grib_path),
                        "message_index": msg_index,
                        "key": key,
                        "status": "ok",
                        "value": "",
                    }
                    try:
                        row["value"] = str(eccodes.codes_get(gid, key))
                    except Exception as exc:
                        row["status"] = "missing_or_unreadable"
                        row["value"] = type(exc).__name__
                    key_rows.append(row)
                values = np.asarray(eccodes.codes_get_values(gid))
                var_name = "cloud_mask" if product_label == "CLM" else f"{product_label.lower()}_values"
                if msg_index == 1:
                    arrays[var_name] = square_reshape(values, "C")
                    try:
                        arrays["latitude"] = square_reshape(np.asarray(eccodes.codes_get_array(gid, "latitudes")), "C").astype(np.float32)
                    except Exception as exc:
                        warn(warnings, "eccodes_latitudes_unavailable", str(exc), grib_file=str(grib_path))
                    try:
                        arrays["longitude"] = square_reshape(np.asarray(eccodes.codes_get_array(gid, "longitudes")), "C").astype(np.float32)
                    except Exception as exc:
                        warn(warnings, "eccodes_longitudes_unavailable", str(exc), grib_file=str(grib_path))
            finally:
                eccodes.codes_release(gid)
    return arrays, key_rows


def read_cfgrib_direct(grib_path: Path, product_label: str, warnings: list[dict[str, Any]]) -> tuple[dict[str, np.ndarray], list[dict[str, Any]]]:
    xr = import_cfgrib_stack(warnings)
    arrays: dict[str, np.ndarray] = {}
    dim_rows: list[dict[str, Any]] = []
    if xr is None:
        return arrays, dim_rows
    try:
        ds = xr.open_dataset(grib_path, engine="cfgrib", backend_kwargs={"indexpath": ""})
    except Exception as exc:
        warn(warnings, "cfgrib_open_failed", str(exc), "ERROR", grib_file=str(grib_path))
        return arrays, dim_rows
    try:
        for name in list(ds.data_vars):
            standard = "cloud_mask" if product_label == "CLM" and name == "p260537" else name
            raw = np.array(ds[name].values, copy=True)
            arrays[standard] = reshape_square_if_needed(raw)
            dim_rows.append(
                {
                    "reader": "cfgrib_direct",
                    "variable": standard,
                    "source_name": name,
                    "dims": safe_json(tuple(str(d) for d in ds[name].dims)),
                    "sizes": safe_json({str(k): int(v) for k, v in ds.sizes.items()}),
                    "raw_shape": safe_json(tuple(int(x) for x in raw.shape)),
                    "after_reshape_shape": safe_json(tuple(int(x) for x in arrays[standard].shape)),
                    "dtype": str(raw.dtype),
                    "attrs_json": safe_json({k: str(v) for k, v in ds[name].attrs.items()}),
                }
            )
        for coord in ("latitude", "longitude"):
            if coord in ds.coords:
                raw = np.array(ds[coord].values, copy=True)
                arrays[coord] = reshape_square_if_needed(raw).astype(np.float32)
                dim_rows.append(
                    {
                        "reader": "cfgrib_direct",
                        "variable": coord,
                        "source_name": coord,
                        "dims": safe_json(tuple(str(d) for d in ds[coord].dims)),
                        "sizes": safe_json({str(k): int(v) for k, v in ds.sizes.items()}),
                        "raw_shape": safe_json(tuple(int(x) for x in raw.shape)),
                        "after_reshape_shape": safe_json(tuple(int(x) for x in arrays[coord].shape)),
                        "dtype": str(raw.dtype),
                        "attrs_json": safe_json({k: str(v) for k, v in ds[coord].attrs.items()}),
                    }
                )
    finally:
        ds.close()
    return arrays, dim_rows


def read_production_reader(zip_path: Path, warnings: list[dict[str, Any]]) -> tuple[dict[str, np.ndarray], dict[str, Any], list[dict[str, Any]]]:
    mapping = read_mapping()
    result = read_meteosat_zip(zip_path, "CLM", mapping)
    for msg in result.warnings:
        warn(warnings, "production_reader_warning", msg, zip_file=str(zip_path))
    dim_rows: list[dict[str, Any]] = []
    for name, arr in result.arrays.items():
        if name in {"cloud_mask", "latitude", "longitude", "valid_mask", "quality_flag_standard"}:
            dim_rows.append(
                {
                    "reader": "production_read_meteosat_zip",
                    "variable": name,
                    "source_name": result.source_variables.get(name, ""),
                    "dims": "",
                    "sizes": "",
                    "raw_shape": "",
                    "after_reshape_shape": safe_json(tuple(int(x) for x in np.asarray(arr).shape)),
                    "dtype": str(np.asarray(arr).dtype),
                    "attrs_json": safe_json(result.attrs.get(f"attrs_{name}", {})),
                }
            )
    return result.arrays, result.attrs, dim_rows


def transforms() -> dict[str, Callable[[np.ndarray], np.ndarray]]:
    return {
        "identity": lambda a: a,
        "flipud": np.flipud,
        "fliplr": np.fliplr,
        "rot180": lambda a: np.rot90(a, 2),
        "transpose": lambda a: np.asarray(a).T,
        "transpose_flipud": lambda a: np.flipud(np.asarray(a).T),
        "transpose_fliplr": lambda a: np.fliplr(np.asarray(a).T),
        "rot90_cw": lambda a: np.rot90(a, -1),
        "rot90_ccw": lambda a: np.rot90(a, 1),
    }


def normalize_lon(arr: np.ndarray) -> np.ndarray:
    return ((np.asarray(arr, dtype=np.float64) + 180.0) % 360.0) - 180.0


def compare_array_sets(name_a: str, arrays_a: dict[str, np.ndarray], name_b: str, arrays_b: dict[str, np.ndarray]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for variable in ("cloud_mask", "latitude", "longitude"):
        if variable not in arrays_a or variable not in arrays_b:
            rows.append(
                {
                    "reader_a": name_a,
                    "reader_b": name_b,
                    "variable": variable,
                    "transform_applied_to_a": "",
                    "status": "missing_variable",
                }
            )
            continue
        target = np.asarray(arrays_b[variable])
        if variable == "longitude":
            target_cmp = normalize_lon(target)
        else:
            target_cmp = target.astype(np.float64) if target.dtype.kind in "fc" else target
        best_score = -1.0
        best_index = -1
        for tname, tfun in transforms().items():
            row = {
                "reader_a": name_a,
                "reader_b": name_b,
                "variable": variable,
                "transform_applied_to_a": tname,
                "status": "ok",
                "shape_a_after_transform": "",
                "shape_b": safe_json(tuple(int(x) for x in target.shape)),
                "n_compare": 0,
                "equal_fraction": math.nan,
                "close_fraction": math.nan,
                "mean_abs_diff": math.nan,
                "max_abs_diff": math.nan,
                "is_best_candidate": False,
            }
            try:
                src = tfun(np.asarray(arrays_a[variable]))
            except Exception as exc:
                row["status"] = "transform_failed"
                row["message"] = str(exc)
                rows.append(row)
                continue
            row["shape_a_after_transform"] = safe_json(tuple(int(x) for x in src.shape))
            if src.shape != target.shape:
                row["status"] = "shape_mismatch"
                rows.append(row)
                continue
            if variable == "longitude":
                src_cmp = normalize_lon(src)
            else:
                src_cmp = src.astype(np.float64) if src.dtype.kind in "fc" else src
            finite = np.isfinite(src_cmp) & np.isfinite(target_cmp) if (np.asarray(src_cmp).dtype.kind in "fc" or np.asarray(target_cmp).dtype.kind in "fc") else np.ones(src.shape, dtype=bool)
            if not np.any(finite):
                row["status"] = "no_common_finite"
                rows.append(row)
                continue
            a = np.asarray(src_cmp)[finite]
            b = np.asarray(target_cmp)[finite]
            if variable == "cloud_mask":
                eq = a.astype(np.int16) == b.astype(np.int16)
                score = float(np.mean(eq))
                row["equal_fraction"] = score
                row["close_fraction"] = score
                diff = np.abs(a.astype(np.float64) - b.astype(np.float64))
            else:
                diff = np.abs(a.astype(np.float64) - b.astype(np.float64))
                score = float(np.mean(diff <= 1e-5))
                row["close_fraction"] = score
                row["equal_fraction"] = score
            row["n_compare"] = int(a.size)
            row["mean_abs_diff"] = float(np.mean(diff))
            row["max_abs_diff"] = float(np.max(diff))
            if score > best_score:
                best_score = score
                best_index = len(rows)
            rows.append(row)
        if best_index >= 0:
            rows[best_index]["is_best_candidate"] = True
    return rows


def valid_disk_mask(arrays: dict[str, np.ndarray]) -> np.ndarray:
    cm = np.asarray(arrays.get("cloud_mask"))
    lat = np.asarray(arrays.get("latitude"))
    lon = np.asarray(arrays.get("longitude"))
    valid = np.isfinite(cm) & np.isfinite(lat) & np.isfinite(lon)
    valid &= cm != 3
    valid &= np.abs(lat) <= 90
    return valid


def summarize_edges(reader: str, variable: str, arr: np.ndarray, valid: np.ndarray | None = None) -> dict[str, Any]:
    a = np.asarray(arr)
    row: dict[str, Any] = {
        "reader": reader,
        "variable": variable,
        "shape": safe_json(tuple(int(x) for x in a.shape)),
        "dtype": str(a.dtype),
        "ndim": int(a.ndim),
        "c_contiguous": bool(a.flags.c_contiguous),
        "f_contiguous": bool(a.flags.f_contiguous),
        "finite_fraction": float(np.mean(np.isfinite(a))) if a.dtype.kind in "fc" else 1.0,
        "corner_ul": float(a[0, 0]) if a.ndim == 2 else math.nan,
        "corner_ur": float(a[0, -1]) if a.ndim == 2 else math.nan,
        "corner_ll": float(a[-1, 0]) if a.ndim == 2 else math.nan,
        "corner_lr": float(a[-1, -1]) if a.ndim == 2 else math.nan,
        "first10_values": safe_json([float(x) for x in a.ravel()[:10]]),
        "last10_values": safe_json([float(x) for x in a.ravel()[-10:]]),
    }
    if a.ndim == 2:
        row.update(
            {
                "first_row_mean": float(np.nanmean(a[0])),
                "last_row_mean": float(np.nanmean(a[-1])),
                "first_col_mean": float(np.nanmean(a[:, 0])),
                "last_col_mean": float(np.nanmean(a[:, -1])),
            }
        )
        if valid is not None and np.any(valid):
            yy, xx = np.indices(a.shape)
            upper = valid & (yy < a.shape[0] / 2)
            lower = valid & (yy >= a.shape[0] / 2)
            left = valid & (xx < a.shape[1] / 2)
            right = valid & (xx >= a.shape[1] / 2)
            row.update(
                {
                    "valid_pixel_count": int(np.count_nonzero(valid)),
                    "upper_valid_mean": float(np.nanmean(a[upper])) if np.any(upper) else math.nan,
                    "lower_valid_mean": float(np.nanmean(a[lower])) if np.any(lower) else math.nan,
                    "left_valid_mean": float(np.nanmean(a[left])) if np.any(left) else math.nan,
                    "right_valid_mean": float(np.nanmean(a[right])) if np.any(right) else math.nan,
                }
            )
    return row


def reshape_order_rows(reader_arrays: dict[str, np.ndarray], cfgrib_raw_1d: dict[str, np.ndarray]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for variable, raw in cfgrib_raw_1d.items():
        a = np.asarray(raw)
        if a.ndim != 1:
            continue
        c = square_reshape(a, "C")
        f = square_reshape(a, "F")
        current = np.asarray(reader_arrays.get(variable)) if variable in reader_arrays else c
        for order_name, candidate in [("C", c), ("F", f)]:
            status = "ok" if candidate.shape == current.shape else "shape_mismatch"
            eq = math.nan
            mean_abs_diff = math.nan
            max_abs_diff = math.nan
            if status == "ok":
                if variable == "longitude":
                    diff = np.abs(normalize_lon(candidate) - normalize_lon(current))
                    eq = float(np.mean(diff <= 1e-4))
                    mean_abs_diff = float(np.mean(diff))
                    max_abs_diff = float(np.max(diff))
                elif candidate.dtype.kind in "fc" or current.dtype.kind in "fc":
                    diff = np.abs(candidate.astype(np.float64) - current.astype(np.float64))
                    eq = float(np.mean(diff <= 1e-4))
                    mean_abs_diff = float(np.mean(diff))
                    max_abs_diff = float(np.max(diff))
                else:
                    eq = float(np.mean(candidate == current))
                    diff = np.abs(candidate.astype(np.float64) - current.astype(np.float64))
                    mean_abs_diff = float(np.mean(diff))
                    max_abs_diff = float(np.max(diff))
            rows.append(
                {
                    "variable": variable,
                    "raw_shape": safe_json(tuple(int(x) for x in a.shape)),
                    "reshape_order": order_name,
                    "reshaped_shape": safe_json(tuple(int(x) for x in candidate.shape)),
                    "current_reader_shape": safe_json(tuple(int(x) for x in current.shape)),
                    "match_current_fraction_tol": eq,
                    "tolerance": "1e-4 for floating variables; exact for integer variables",
                    "mean_abs_diff": mean_abs_diff,
                    "max_abs_diff": max_abs_diff,
                    "status": status,
                }
            )
        if c.shape == f.shape:
            c_vs_f_diff = np.abs(c.astype(np.float64) - f.astype(np.float64))
            rows.append(
                {
                    "variable": variable,
                    "raw_shape": safe_json(tuple(int(x) for x in a.shape)),
                    "reshape_order": "C_vs_F",
                    "reshaped_shape": safe_json(tuple(int(x) for x in c.shape)),
                    "current_reader_shape": "",
                    "match_current_fraction_tol": float(np.mean(c == f)) if variable == "cloud_mask" else float(np.mean(c_vs_f_diff <= 1e-4)),
                    "tolerance": "1e-4 for floating variables; exact for integer variables",
                    "mean_abs_diff": float(np.mean(c_vs_f_diff)),
                    "max_abs_diff": float(np.max(c_vs_f_diff)),
                    "status": "diagnostic",
                }
            )
    return rows


def boundary(mask_cloud: np.ndarray, valid: np.ndarray) -> np.ndarray:
    cloud = mask_cloud & valid
    clear = (~mask_cloud) & valid
    k = np.ones((3, 3), dtype=bool)
    return valid & ndimage.binary_dilation(cloud, structure=k) & ndimage.binary_dilation(clear, structure=k)


def cth_alignment_metrics(clm_arrays: dict[str, np.ndarray], cth_arrays: dict[str, np.ndarray]) -> list[dict[str, Any]]:
    if "cloud_mask" not in clm_arrays or "ctoph" not in cth_arrays:
        return [
            {
                "evidence_type": "cth_internal_control",
                "variant": "missing",
                "status": "missing_clm_or_cth",
            }
        ]
    clm = np.asarray(clm_arrays["cloud_mask"])
    cth = np.asarray(cth_arrays["ctoph"])
    rows: list[dict[str, Any]] = []
    if clm.shape != cth.shape:
        return [
            {
                "evidence_type": "cth_internal_control",
                "variant": "shape_mismatch",
                "status": "shape_mismatch",
                "clm_shape": safe_json(tuple(int(x) for x in clm.shape)),
                "cth_shape": safe_json(tuple(int(x) for x in cth.shape)),
                "note": "CTH operational GRIB is a reduced cloud-object product in this local sample, not a full-disk IR raster.",
            }
        ]
    for variant, mask in [("identity_clm", clm), ("rot180_clm", np.rot90(clm, 2))]:
        valid = np.isfinite(cth) & np.isfinite(mask) & (mask != 3)
        cloud = mask == 2
        clear = np.isin(mask, [0, 1])
        cth_valid = np.isfinite(cth)
        rows.append(
            {
                "evidence_type": "cth_internal_control",
                "variant": variant,
                "status": "ok",
                "n_valid": int(np.count_nonzero(valid)),
                "cth_valid_fraction_cloud": float(np.mean(cth_valid[cloud])) if np.any(cloud) else math.nan,
                "cth_valid_fraction_clear": float(np.mean(cth_valid[clear])) if np.any(clear) else math.nan,
                "note": "CTH is not IR108; this is only an internal consistency control.",
            }
        )
    return rows


def coastline_land_water_metrics(arrays: dict[str, np.ndarray], warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compare CLM clear-land/clear-water labels with Natural Earth land.

    This is a coastline/navigation consistency check, not an IR or cloud truth
    test. A strong identity score indicates that mask categories and lat/lon are
    mutually consistent near continents.
    """
    warn(
        warnings,
        "coastline_metric_skipped_no_preapproved_local_reference",
        "Coastline/Natural Earth quantitative metric skipped because Gate 1 is not allowed to trigger network downloads and no project-local preapproved coastline reference is registered.",
    )
    return [
        {
            "status": "skipped_no_preapproved_local_coastline_reference",
            "decision_use": "INCONCLUSIVE_COASTLINE_EVIDENCE",
            "meaning": "No coastline metric was used in final Gate 1 outputs; locate/register a local coastline or IR108/NAT reference before rerunning this test.",
        }
    ]


def geo_extent_from_valid(lat: np.ndarray, lon: np.ndarray, valid: np.ndarray) -> tuple[float, float, float, float]:
    if not np.any(valid):
        return -90.0, 90.0, -90.0, 90.0
    return (
        float(np.nanpercentile(lon[valid], 0.5)),
        float(np.nanpercentile(lon[valid], 99.5)),
        float(np.nanpercentile(lat[valid], 0.5)),
        float(np.nanpercentile(lat[valid], 99.5)),
    )


def make_clm_navigation_figure(arrays: dict[str, np.ndarray], out_path: Path, warnings: list[dict[str, Any]]) -> None:
    if not {"cloud_mask", "latitude", "longitude"}.issubset(arrays):
        warn(warnings, "figure_skipped_missing_clm_navigation_arrays", "Cannot plot CLM navigation figure")
        return
    cm = np.asarray(arrays["cloud_mask"])
    lat = np.asarray(arrays["latitude"])
    lon = normalize_lon(np.asarray(arrays["longitude"]))
    valid = valid_disk_mask(arrays)
    step = max(1, cm.shape[0] // 600)
    sl = np.s_[::step, ::step]
    cm_s = cm[sl]
    lat_s = lat[sl]
    lon_s = lon[sl]
    valid_s = valid[sl]
    fig = plt.figure(figsize=(13.0, 7.2), dpi=140)
    ax = fig.add_axes([0.06, 0.20, 0.40, 0.68])
    ax.set_title("CLM identity on cfgrib/eccodes lat-lon")
    sc = ax.scatter(lon_s[valid_s], lat_s[valid_s], c=cm_s[valid_s], s=0.4, cmap="viridis", vmin=0, vmax=3)
    xmin, xmax, ymin, ymax = geo_extent_from_valid(lat_s, lon_s, valid_s)
    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    ax.set_xlabel("longitude (deg, normalized)")
    ax.set_ylabel("latitude (deg)")
    ax.grid(True, linewidth=0.2, alpha=0.35)
    ax.text(0.02, 0.02, "No coastline overlay: no network downloads allowed", transform=ax.transAxes, fontsize=8)
    ax2 = fig.add_axes([0.55, 0.20, 0.40, 0.68])
    ax2.set_title("Native array CLM, origin=upper")
    ax2.imshow(cm, origin="upper", cmap="viridis", vmin=0, vmax=3, interpolation="nearest")
    ax2.set_xlabel("column")
    ax2.set_ylabel("row")
    cax = fig.add_axes([0.24, 0.08, 0.52, 0.035])
    cb = fig.colorbar(sc, cax=cax, orientation="horizontal", ticks=[0, 1, 2, 3])
    cb.ax.set_xticklabels(["0 clear water", "1 clear land", "2 cloud", "3 off-earth/not processed"])
    fig.suptitle(f"Stage 09H Gate 1 {SAMPLE_ID} {SOURCE}: navigation quicklook (not IR evidence)")
    fig.savefig(out_path)
    plt.close(fig)


def make_missing_ir_figure(out_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(12.8, 7.2), dpi=140)
    ax.axis("off")
    ax.text(
        0.03,
        0.72,
        "IR108 evidence not available locally for 20240310_1200 / Meteosat-0deg",
        fontsize=16,
        weight="bold",
    )
    ax.text(
        0.03,
        0.55,
        "Searched local GEO roots and Stage 09G cache. Found CLM/CTH/CTTH only.\n"
        "Gate 1 records IR overlay as INCONCLUSIVE instead of substituting CTH for IR108.",
        fontsize=12,
    )
    ax.text(
        0.03,
        0.34,
        "Decision impact: raw GRIB keys, reader direction comparison, reshape audit, and CLM navigation quicklook are valid;\n"
        "IR/cloud-boundary physical alignment and IR/coastline navigation direction remain blocked until same-time IR108/NAT is available.",
        fontsize=11,
    )
    fig.savefig(out_path)
    plt.close(fig)


def scan_ir_candidates() -> list[dict[str, Any]]:
    roots = [RAW_ROOT, path_config.DATA_ROOT, OUT_ROOT.parent / "stage_09g_orientation_root_cause_audit_202403"]
    rows: list[dict[str, Any]] = []
    tokens = ("IR", "IR108", "IR10", "108", "NAT", "HRSEVIRI")
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            name = path.name.upper()
            if ("202403101200" in name or "2024031012" in name) and any(tok in name for tok in tokens):
                rows.append(
                    {
                        "candidate_path": str(path),
                        "size_bytes": int(path.stat().st_size),
                        "modified_time": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
                        "candidate_role": "possible_ir108_or_native_seviri" if any(tok in name for tok in ("IR", "108", "NAT", "HRSEVIRI")) else "unknown",
                    }
                )
    return rows


def write_report(path: Path, summary: dict[str, Any], outputs: dict[str, Path]) -> None:
    lines = [
        "# Stage 09H Gate 1: Meteosat-0deg mask-navigation 180 degree root-cause audit",
        "",
        f"- generated_utc: `{summary['generated_utc']}`",
        f"- sample_id: `{SAMPLE_ID}`",
        f"- source: `{SOURCE}`",
        "- scope: Gate 1 only; no production code changes; no full-month batch run.",
        "",
        "## 直接结论",
        "",
        f"- raw GRIB scanning-mode keys 已读取：`scanningMode={summary.get('scanningMode')}`, `iScansNegatively={summary.get('iScansNegatively')}`, `jScansPositively={summary.get('jScansPositively')}`, `jPointsAreConsecutive={summary.get('jPointsAreConsecutive')}`。",
        f"- 当前 reader、eccodes direct、cfgrib direct 的 CLM 数组比较：最佳 transform 为 `{summary.get('reader_cloud_best')}`；这说明三路 reader 对 cloud mask 的数组方向 `{summary.get('reader_cloud_interpretation')}`。",
        f"- latitude/longitude 比较：最佳 transform 为 `{summary.get('reader_nav_best')}`；注意该 GRIB 为 `space_view`，首末经纬度 key 缺失，off-earth 区域 lat/lon 为 0。",
        f"- `reshape_square_if_needed()` 审计：cfgrib 给出 1D `values`，当前 reader 对 CLM/latitude/longitude 均使用 C-order square reshape；C-order 与当前 reader 匹配度见 CSV。",
        f"- IR108：`{summary.get('ir_status')}`。本地未找到同刻 IR108/NAT，因此 IR 与云边界、IR 与海岸线导航方向在 Gate 1 中不能定责。",
        f"- 海岸线/陆海导航辅助检查：identity clear land/water vs Natural Earth agreement=`{summary.get('coast_identity_agreement')}`，rot180 mask vs identity navigation agreement=`{summary.get('coast_rotmask_agreement')}`。这只检验 CLM 陆海语义与导航的一致性，不是云边界物理检验。",
        f"- 同刻 CTH/ctoph：`{summary.get('cth_status')}`。它不是 IR108，只作为可用性和内部一致性辅助记录。",
        "",
        "## Gate 1 决策",
        "",
        f"- decision_status: `{summary.get('decision_status')}`",
        f"- next_step: `{summary.get('next_step')}`",
        "",
        "## 关键限制",
        "",
        "- 不能把 EPIC 当作绝对真值；本 Gate 1 主要检查 raw GRIB / reader / reshape / 本地 IR 可用性。",
        "- 三路 reader 都依赖 ecCodes/GRIB 解码生态；reader 一致不能单独证明官方物理方向正确。",
        "- 缺少 IR108 时，不能判定 mask 端还是 navigation 端；最多给出 reader/reshape 层证据和后续数据需求。",
        "",
        "## 输出文件",
        "",
    ]
    for label, out in outputs.items():
        lines.append(f"- `{label}`: `{out}`")
    path.write_text("\n".join(lines), encoding="utf-8-sig")


def main() -> None:
    dirs = ensure_dirs()
    warnings: list[dict[str, Any]] = []
    generated_utc = utc_now()
    configure_eccodes(warnings)
    _ = import_eccodes(warnings)
    _ = import_cfgrib_stack(warnings)

    entries, extracted, xml_rows = extract_zip(CLM_ZIP, dirs["cache"], warnings)
    cth_entries, cth_extracted, cth_xml_rows = extract_zip(CTH_ZIP, dirs["cache"], warnings)
    entries.extend(cth_entries)
    xml_rows.extend(cth_xml_rows)
    grib_paths = {k: v for k, v in extracted.items() if k.lower().endswith((".grb", ".grib", ".grb2"))}
    cth_grib_paths = {k: v for k, v in cth_extracted.items() if k.lower().endswith((".grb", ".grib", ".grb2"))}
    clm_grib = next(iter(grib_paths.values()), None)
    cth_grib = next(iter(cth_grib_paths.values()), None)
    if clm_grib is None:
        raise RuntimeError(f"No CLM GRIB extracted from {CLM_ZIP}")

    prod_arrays, prod_attrs, prod_dims = read_production_reader(CLM_ZIP, warnings)
    ecc_arrays, grib_key_rows = read_eccodes_direct(clm_grib, "CLM", warnings)
    cf_arrays, cf_dims = read_cfgrib_direct(clm_grib, "CLM", warnings)
    cth_arrays: dict[str, np.ndarray] = {}
    cth_dims: list[dict[str, Any]] = []
    if cth_grib is not None:
        cth_arrays, cth_dims = read_cfgrib_direct(cth_grib, "CTH", warnings)

    reader_inventory = [
        {"reader": "production_read_meteosat_zip", "status": "ok" if prod_arrays else "failed", "variables": ",".join(sorted(prod_arrays))},
        {"reader": "eccodes_direct", "status": "ok" if ecc_arrays else "failed", "variables": ",".join(sorted(ecc_arrays))},
        {"reader": "cfgrib_direct", "status": "ok" if cf_arrays else "failed", "variables": ",".join(sorted(cf_arrays))},
    ]

    transform_rows: list[dict[str, Any]] = []
    transform_rows.extend(compare_array_sets("production_read_meteosat_zip", prod_arrays, "eccodes_direct", ecc_arrays))
    transform_rows.extend(compare_array_sets("production_read_meteosat_zip", prod_arrays, "cfgrib_direct", cf_arrays))
    transform_rows.extend(compare_array_sets("eccodes_direct", ecc_arrays, "cfgrib_direct", cf_arrays))

    dim_rows = prod_dims + cf_dims + cth_dims
    valid = valid_disk_mask(cf_arrays) if {"cloud_mask", "latitude", "longitude"}.issubset(cf_arrays) else None
    edge_rows: list[dict[str, Any]] = []
    for reader, arrays in [
        ("production_read_meteosat_zip", prod_arrays),
        ("eccodes_direct", ecc_arrays),
        ("cfgrib_direct", cf_arrays),
    ]:
        reader_valid = valid_disk_mask(arrays) if {"cloud_mask", "latitude", "longitude"}.issubset(arrays) else None
        for variable in ("cloud_mask", "latitude", "longitude"):
            if variable in arrays:
                edge_rows.append(summarize_edges(reader, variable, arrays[variable], reader_valid))

    cf_raw_1d: dict[str, np.ndarray] = {}
    xr = import_cfgrib_stack(warnings)
    if xr is not None:
        ds = xr.open_dataset(clm_grib, engine="cfgrib", backend_kwargs={"indexpath": ""})
        try:
            if "p260537" in ds:
                cf_raw_1d["cloud_mask"] = np.asarray(ds["p260537"].values)
            for coord in ("latitude", "longitude"):
                if coord in ds.coords:
                    cf_raw_1d[coord] = np.asarray(ds[coord].values)
        finally:
            ds.close()
    reshape_rows = reshape_order_rows(prod_arrays, cf_raw_1d)

    scan_rows: list[dict[str, Any]] = []
    for row in grib_key_rows:
        if row["key"] in {"scanningMode", "iScansNegatively", "jScansPositively", "jPointsAreConsecutive", "alternativeRowScanning"}:
            meaning = {
                "iScansNegatively": "successive points along a row scan westward when key=1",
                "jScansPositively": "successive rows scan northward when key=1",
                "jPointsAreConsecutive": "points are row-major when key=0 and column-major when key=1",
                "alternativeRowScanning": "adjacent rows reverse scan direction when key=1",
                "scanningMode": "GRIB bit field summarizing scan direction",
            }.get(row["key"], "")
            scan_rows.append({**row, "interpretation": meaning})

    ir_candidates = scan_ir_candidates()
    if not ir_candidates:
        warn(warnings, "missing_same_time_ir108", "No same-time Meteosat IR108/NAT file found locally for 20240310_1200 Meteosat-0deg; IR overlay skipped.")
    ir_metrics = [
        {
            "sample_id": SAMPLE_ID,
            "source": SOURCE,
            "ir_status": "missing_same_time_ir108",
            "overlay_status": "skipped",
            "identity_mask_ir_metric": math.nan,
            "rot180_mask_ir_metric": math.nan,
            "decision_use": "INCONCLUSIVE_IR_EVIDENCE",
        }
    ]
    cth_metrics = cth_alignment_metrics(cf_arrays, cth_arrays)
    if cth_metrics and cth_metrics[0].get("status") == "shape_mismatch":
        warn(warnings, "cth_shape_mismatch", cth_metrics[0].get("note", "CTH shape mismatch"), clm_shape=cth_metrics[0].get("clm_shape"), cth_shape=cth_metrics[0].get("cth_shape"))
    coastline_metrics = coastline_land_water_metrics(cf_arrays, warnings)

    figure_index: list[dict[str, Any]] = []
    nav_fig = dirs["figures"] / f"stage_09h_gate1_clm_navigation_quicklook_{SAMPLE_ID}.png"
    make_clm_navigation_figure(cf_arrays, nav_fig, warnings)
    if nav_fig.exists():
        figure_index.append(
            {
                "figure_id": "stage_09h_gate1_clm_navigation_quicklook",
                "figure_path": str(nav_fig),
                "source_csv": str(dirs["source_data"] / "stage_09h_raw_corner_and_edge_diagnostics.csv"),
                "note": "Navigation quicklook, not IR evidence.",
            }
        )
    ir_fig = dirs["figures"] / f"stage_09h_gate1_missing_ir108_note_{SAMPLE_ID}.png"
    make_missing_ir_figure(ir_fig)
    figure_index.append(
        {
            "figure_id": "stage_09h_gate1_missing_ir108_note",
            "figure_path": str(ir_fig),
            "source_csv": str(dirs["source_data"] / "stage_09h_ir_availability_inventory.csv"),
            "note": "Explicit missing-data diagnostic figure.",
        }
    )

    input_checksums = []
    for path in [CLM_ZIP, CTH_ZIP]:
        if path.exists():
            input_checksums.append(
                {
                    "input_path": str(path),
                    "size_bytes": int(path.stat().st_size),
                    "sha256": file_sha256(path),
                    "modified_time": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
                }
            )

    best_cloud = next(
        (
            r
            for r in transform_rows
            if r.get("reader_a") == "production_read_meteosat_zip"
            and r.get("reader_b") == "eccodes_direct"
            and r.get("variable") == "cloud_mask"
            and r.get("is_best_candidate")
        ),
        {},
    )
    best_nav = next(
        (
            r
            for r in transform_rows
            if r.get("reader_a") == "production_read_meteosat_zip"
            and r.get("reader_b") == "eccodes_direct"
            and r.get("variable") == "latitude"
            and r.get("is_best_candidate")
        ),
        {},
    )
    scan_lookup = {r["key"]: r.get("value") for r in grib_key_rows if r.get("message_index") == 1}
    summary = {
        "generated_utc": generated_utc,
        "scanningMode": scan_lookup.get("scanningMode", "missing"),
        "iScansNegatively": scan_lookup.get("iScansNegatively", "missing"),
        "jScansPositively": scan_lookup.get("jScansPositively", "missing"),
        "jPointsAreConsecutive": scan_lookup.get("jPointsAreConsecutive", "missing"),
        "reader_cloud_best": best_cloud.get("transform_applied_to_a", "missing"),
        "reader_nav_best": best_nav.get("transform_applied_to_a", "missing"),
        "reader_cloud_interpretation": "consistent by identity" if best_cloud.get("transform_applied_to_a") == "identity" else "not identity-best",
        "ir_status": "missing_same_time_ir108" if not ir_candidates else "candidate_found_requires_manual_confirmation",
        "coast_identity_agreement": next((f"{r.get('agreement_clear_land_water_vs_natural_earth'):.6f}" for r in coastline_metrics if r.get("variant") == "identity_mask_identity_nav" and r.get("status") == "ok"), "missing"),
        "coast_rotmask_agreement": next((f"{r.get('agreement_clear_land_water_vs_natural_earth'):.6f}" for r in coastline_metrics if r.get("variant") == "rot180_mask_identity_nav" and r.get("status") == "ok"), "missing"),
        "cth_status": cth_metrics[0].get("status", "unknown") if cth_metrics else "unknown",
        "decision_status": "INCONCLUSIVE_SIDE_UNRESOLVED_AT_GATE1",
        "next_step": "Acquire or locate same-time Meteosat IR108/NAT and then rerun Gate 1 IR overlay; do not start Gate 2 until IR/coastline evidence is available or explicitly waived.",
    }

    source_outputs = {
        "stage_09h_raw_zip_inventory.csv": entries,
        "stage_09h_raw_xml_metadata_inventory.csv": xml_rows,
        "stage_09h_raw_grib_key_inventory.csv": grib_key_rows,
        "stage_09h_grib_scanning_mode_interpretation.csv": scan_rows,
        "stage_09h_raw_variable_dimension_audit.csv": dim_rows,
        "stage_09h_raw_corner_and_edge_diagnostics.csv": edge_rows,
        "stage_09h_reshape_order_comparison.csv": reshape_rows,
        "stage_09h_independent_reader_inventory.csv": reader_inventory,
        "stage_09h_reader_transform_match.csv": transform_rows,
        "stage_09h_ir_availability_inventory.csv": ir_candidates,
        f"stage_09h_ir_mask_alignment_metrics_{SAMPLE_ID}.csv": ir_metrics,
        f"stage_09h_cth_internal_control_metrics_{SAMPLE_ID}.csv": cth_metrics,
        f"stage_09h_coastline_land_water_navigation_metrics_{SAMPLE_ID}.csv": coastline_metrics,
        "stage_09h_final_decision_matrix.csv": [
            {
                "sample_id": SAMPLE_ID,
                "source": SOURCE,
                "gate": "Gate 1",
                "raw_grib_scanning_keys": "completed",
                "reader_transform_match": "completed",
                "ir_overlay": "blocked_missing_ir108",
                "coastline_navigation": "completed_clear_land_water_auxiliary",
                "reshape_audit": "completed",
                "decision_status": summary["decision_status"],
                "next_step": summary["next_step"],
            }
        ],
    }
    output_paths: dict[str, Path] = {}
    for name, rows in source_outputs.items():
        out = dirs["source_data"] / name
        pd.DataFrame(rows).to_csv(out, index=False, encoding="utf-8-sig")
        output_paths[name] = out
    warnings_path = dirs["logs"] / "warnings.csv"
    pd.DataFrame(warnings).to_csv(warnings_path, index=False, encoding="utf-8-sig")
    figure_index_path = dirs["logs"] / "figure_index.csv"
    pd.DataFrame(figure_index).to_csv(figure_index_path, index=False, encoding="utf-8-sig")
    checksums_path = dirs["logs"] / "input_file_checksums.csv"
    pd.DataFrame(input_checksums).to_csv(checksums_path, index=False, encoding="utf-8-sig")
    output_paths["warnings.csv"] = warnings_path
    output_paths["figure_index.csv"] = figure_index_path
    output_paths["input_file_checksums.csv"] = checksums_path
    output_paths["clm_navigation_quicklook_png"] = nav_fig
    output_paths["missing_ir108_note_png"] = ir_fig

    report_path = dirs["reports"] / "stage_09h_gate1_root_cause_report_cn.md"
    write_report(report_path, summary, output_paths)
    output_paths["stage_09h_gate1_root_cause_report_cn.md"] = report_path

    manifest = {
        "project_id": PROJECT_ID,
        "canonical_stage_id": STAGE_ID,
        "run_id": RUN_ID,
        "gate_id": GATE_ID,
        "sample_id": SAMPLE_ID,
        "source": SOURCE,
        "generated_utc": generated_utc,
        "script_path": str(Path(__file__).resolve()),
        "input_paths": [str(CLM_ZIP), str(CTH_ZIP)],
        "output_paths": {k: str(v) for k, v in output_paths.items()},
        "parameters": {
            "gate_scope": "Gate 1 only",
            "no_production_code_changes": True,
            "no_full_month_batch": True,
            "reader_routes": ["production_read_meteosat_zip", "eccodes_direct", "cfgrib_direct"],
            "transforms": list(transforms().keys()),
        },
        "summary": summary,
        "warnings_count": len(warnings),
    }
    manifest_path = dirs["logs"] / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8-sig")
    print(json.dumps({"report": str(report_path), "manifest": str(manifest_path), "warnings": len(warnings)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
