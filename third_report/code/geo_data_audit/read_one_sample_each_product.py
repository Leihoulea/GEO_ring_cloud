from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import shutil
import struct
import sys
import tempfile
import traceback
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

try:
    import h5py
except Exception as exc:  # pragma: no cover
    h5py = exc

try:
    import netCDF4
except Exception as exc:  # pragma: no cover
    netCDF4 = exc

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except Exception as exc:  # pragma: no cover
    plt = exc


OUT_DIR = Path(r"D:\AAAresearch_paper\data_check_report")
SCRIPT_DIR = Path(__file__).resolve().parent
MAPPING_PATH = OUT_DIR / "manual_variable_mapping_by_product.yaml"
PARSED_METADATA = OUT_DIR / "parsed_file_metadata.csv"
QUICKLOOK_DIR = OUT_DIR / "quicklooks"
DATA_ROOTS_EXTRA = [
    Path(r"D:\AAAresearch_paper\data\FY4B-GEO"),
    Path(r"D:\AAAresearch_paper\data\FY4B-CTP"),
    Path(r"D:\AAAresearch_paper\data\FY4B_Data"),
    Path(r"D:\AAAresearch_paper\second_report\satellitedata\FY4B_Data"),
]
MAX_STATS_VALUES = 300_000


def log(message: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = []
        for row in rows:
            for key in row:
                if key not in fields:
                    fields.append(key)
        if not fields:
            fields = ["empty"]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def write_md(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8-sig")


def load_mapping() -> dict[str, dict[str, list[str]]]:
    return yaml.safe_load(MAPPING_PATH.read_text(encoding="utf-8-sig"))


def copy_mapping_to_out() -> None:
    source = SCRIPT_DIR / "manual_variable_mapping_by_product.yaml"
    if source.exists() and source.resolve() != MAPPING_PATH.resolve():
        shutil.copyfile(source, MAPPING_PATH)


def product_key(family: str, product: str) -> str:
    if family == "FY4B":
        return f"FY4B_{product}"
    if family == "GOES":
        return f"GOES_{product}"
    if family == "Himawari":
        return f"Himawari_{product}"
    if family == "Meteosat":
        return f"Meteosat_{product}"
    return f"{family}_{product}"


def select_samples() -> list[dict[str, Any]]:
    df = pd.read_csv(PARSED_METADATA, encoding="utf-8-sig")
    df = df[df["file_size"].fillna(0).astype("int64") > 0].copy()
    df = df[df["satellite_family"].isin(["FY4B", "GOES", "Himawari", "Meteosat"])]
    samples = []
    for (family, satellite, product), group in df.groupby(["satellite_family", "satellite", "product"], dropna=False):
        if not product or str(product) == "nan":
            continue
        group = group.sort_values(["parse_status", "nominal_time", "file_path"], na_position="last")
        pick = group.iloc[min(len(group) // 2, len(group) - 1)].to_dict()
        pick["sample_reason"] = "middle_time_from_parsed_metadata"
        samples.append(pick)

    # Explicitly hunt FY4B GEO/CTP/CLP/COT/CER in user-provided FY4B locations even if first audit did not parse them.
    existing_keys = {(s.get("satellite_family"), s.get("product")) for s in samples}
    for root in DATA_ROOTS_EXTRA:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            name = path.name.upper()
            product = None
            for token in ["GEO", "CTP", "CLP", "COT", "CER", "FDI"]:
                if f"_{token}" in name or f"-{token}" in name or token in path.parent.name.upper():
                    product = token
                    break
            if product and ("FY4B", product) not in existing_keys:
                samples.append(
                    {
                        "satellite_family": "FY4B",
                        "satellite": "FY4B",
                        "product": product,
                        "file_path": str(path),
                        "file_size": path.stat().st_size,
                        "nominal_time": "",
                        "parse_status": "extra_search",
                        "sample_reason": f"extra_search_in_{root}",
                    }
                )
                existing_keys.add(("FY4B", product))
    return samples


def all_dataset_names_hdf(path: Path) -> dict[str, Any]:
    datasets = {}
    with h5py.File(path, "r") as h5:
        def visitor(name, obj):
            if hasattr(obj, "shape") and hasattr(obj, "dtype"):
                datasets[name] = obj
        h5.visititems(visitor)
    return datasets


def inspect_hdf(sample: dict, mapping: dict) -> tuple[list[dict], list[dict], list[dict], str | None]:
    path = Path(sample["file_path"])
    variable_rows, stats_rows, anomaly_rows = [], [], []
    quicklook = None
    with h5py.File(path, "r") as h5:
        datasets = []
        h5.visititems(lambda name, obj: datasets.append((name, obj)) if hasattr(obj, "shape") and hasattr(obj, "dtype") else None)
        mapped = resolve_mapping(sample, [name for name, _ in datasets], mapping)
        quick_var = choose_quicklook_variable(datasets, mapped)
        for name, obj in datasets:
            std = mapped.get(name, "unknown")
            attrs = {str(k): attr_to_str(v) for k, v in obj.attrs.items()}
            variable_rows.append(variable_record(sample, name, std, obj.shape, obj.dtype, attrs, "hdf5"))
            if np.issubdtype(obj.dtype, np.number) and obj.shape:
                arr = sample_array_hdf(obj)
                stats_rows.append(stats_record(sample, name, std, arr, attrs))
            if quick_var and name == quick_var[0] and quicklook is None:
                quicklook = make_quicklook(sample, name, sample_array_hdf(obj), "hdf5")
        expected_missing(sample, mapped.values(), mapping, anomaly_rows)
    return variable_rows, stats_rows, anomaly_rows, quicklook


def inspect_netcdf(sample: dict, mapping: dict) -> tuple[list[dict], list[dict], list[dict], str | None]:
    path = Path(sample["file_path"])
    variable_rows, stats_rows, anomaly_rows = [], [], []
    quicklook = None
    with netCDF4.Dataset(path, "r") as ds:
        try:
            ds.set_auto_mask(False)
            ds.set_auto_scale(False)
        except Exception:
            pass
        names = list(ds.variables.keys())
        mapped = resolve_mapping(sample, names, mapping)
        quick_var = choose_quicklook_variable([(name, ds.variables[name]) for name in names], mapped)
        for name in names:
            var = ds.variables[name]
            attrs = {k: attr_to_str(getattr(var, k)) for k in var.ncattrs()}
            std = mapped.get(name, "unknown")
            variable_rows.append(variable_record(sample, name, std, getattr(var, "shape", ()), getattr(var, "dtype", ""), attrs, "netcdf"))
            if np.issubdtype(np.dtype(var.dtype), np.number) and getattr(var, "shape", ()):
                arr = sample_array_nc(var)
                stats_rows.append(stats_record(sample, name, std, arr, attrs))
            if quick_var and name == quick_var[0] and quicklook is None:
                quicklook = make_quicklook(sample, name, sample_array_nc(var), "netcdf")
        expected_missing(sample, mapped.values(), mapping, anomaly_rows)
    return variable_rows, stats_rows, anomaly_rows, quicklook


def inspect_meteosat_zip(sample: dict, mapping: dict) -> tuple[list[dict], list[dict], list[dict], list[dict], str | None]:
    path = Path(sample["file_path"])
    variable_rows, stats_rows, anomaly_rows, grib_rows = [], [], [], []
    quicklook = None
    with zipfile.ZipFile(path) as zf:
        bad = zf.testzip()
        names = zf.namelist()
        grib_names = [n for n in names if n.lower().endswith((".grb", ".grib", ".grib2"))]
        variable_rows.append(variable_record(sample, "ZIP_ENTRIES", "metadata_manifest_entries", (len(names),), "zip", {"bad_member": bad or "", "entries": ";".join(names)}, "zip"))
        if bad:
            anomaly_rows.append(anomaly_record(sample, "ERROR", "zip_bad_member", bad))
        if not grib_names:
            anomaly_rows.append(anomaly_record(sample, "ERROR", "meteosat_no_grib_inside_zip", ";".join(names)))
        for grib_name in grib_names:
            data = zf.read(grib_name)
            records = parse_grib_messages(sample, grib_name, data)
            grib_rows.extend(records)
            for rec in records:
                variable_rows.append(
                    variable_record(
                        sample,
                        grib_name,
                        infer_meteosat_std(sample, rec),
                        (rec.get("number_of_data_points", ""),),
                        f"GRIB edition {rec.get('edition')}",
                        rec,
                        "grib_metadata",
                    )
                )
            if grib_decoder_available():
                dec_vars, dec_stats, dec_anomalies, dec_grib, dec_quicklook = decode_grib_member(sample, grib_name, data, records, mapping)
                variable_rows.extend(dec_vars)
                stats_rows.extend(dec_stats)
                anomaly_rows.extend(dec_anomalies)
                grib_rows.extend(dec_grib)
                quicklook = quicklook or dec_quicklook
            else:
                anomaly_rows.append(anomaly_record(sample, "WARN", "grib_value_decode_not_available", "cfgrib/eccodes/pygrib/wgrib2 not available; parsed GRIB sections but did not decode packed grid values"))
        expected = mapping.get(product_key(sample["satellite_family"], sample["product"]), {})
        for std in expected:
            if std in {"projection"}:
                continue
            if not any(row.get("standard_variable") == std for row in variable_rows):
                severity = "INFO" if std in {"cloud_top_temperature", "cloud_top_pressure"} and sample.get("product") == "CTH" else "WARN"
                anomaly_rows.append(anomaly_record(sample, severity, "mapped_standard_variable_not_found", std))
    return variable_rows, stats_rows, anomaly_rows, grib_rows, quicklook


def grib_decoder_available() -> bool:
    for mod in ["cfgrib", "eccodes", "pygrib"]:
        try:
            __import__(mod)
            return True
        except Exception:
            pass
    for exe in ["wgrib2", "grib_ls", "grib_dump"]:
        if shutil.which(exe):
            return True
    return False


def decode_grib_member(sample: dict, member_name: str, data: bytes, records: list[dict], mapping: dict) -> tuple[list[dict], list[dict], list[dict], list[dict], str | None]:
    variable_rows, stats_rows, anomaly_rows, grib_rows = [], [], [], []
    quicklook = None
    try:
        import cfgrib
    except Exception as exc:
        return [], [], [anomaly_record(sample, "WARN", "grib_value_decode_not_available", f"{type(exc).__name__}: {exc}")], [], None

    with tempfile.NamedTemporaryFile(suffix=".grib", delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        dss = cfgrib.open_datasets(str(tmp_path), indexpath="")
        if not dss:
            anomaly_rows.append(anomaly_record(sample, "ERROR", "grib_decode_no_dataset", member_name))
            return variable_rows, stats_rows, anomaly_rows, grib_rows, quicklook
        for ds_index, ds in enumerate(dss):
            try:
                grid_shape = grib_grid_shape(records, ds)
                for var_name in ds.data_vars:
                    da = ds[var_name]
                    attrs = {str(k): attr_to_str(v) for k, v in da.attrs.items()}
                    std = infer_meteosat_decoded_std(sample, var_name, attrs, mapping)
                    arr = np.asarray(da.values)
                    arr_for_grid = reshape_grib_array(arr, grid_shape)
                    shape = arr_for_grid.shape if arr_for_grid is not arr else da.shape
                    attrs["cfgrib_dims"] = dict(da.sizes)
                    attrs["decoded_grid_shape"] = shape
                    attrs["grib_member"] = member_name
                    attrs["grib_dataset_index"] = ds_index
                    variable_rows.append(variable_record(sample, var_name, std, shape, da.dtype, attrs, "cfgrib"))
                    if is_numeric_dtype(da.dtype) and arr.size:
                        stats_arr = downsample_array(arr_for_grid)
                        stats_rows.append(stats_record(sample, var_name, std, stats_arr, attrs))
                    if quicklook is None and arr_for_grid.ndim >= 2 and is_numeric_dtype(da.dtype):
                        quicklook = make_quicklook(sample, var_name, arr_for_grid, "cfgrib")
                    grib_rows.append(decoded_grib_record(sample, member_name, records, ds_index, var_name, std, arr_for_grid, da, attrs))
            finally:
                ds.close()
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        anomaly_rows.append(anomaly_record(sample, "ERROR", "grib_decode_failed", f"{member_name}: {detail}"))
    finally:
        tmp_path.unlink(missing_ok=True)
    return variable_rows, stats_rows, anomaly_rows, grib_rows, quicklook


def grib_grid_shape(records: list[dict], ds: Any) -> tuple[int, int] | None:
    for rec in records:
        try:
            nx = int(rec.get("nx") or 0)
            ny = int(rec.get("ny") or 0)
            if nx > 0 and ny > 0:
                return ny, nx
        except Exception:
            pass
    sizes = dict(getattr(ds, "sizes", {}))
    y = sizes.get("y") or sizes.get("latitude")
    x = sizes.get("x") or sizes.get("longitude")
    if y and x:
        return int(y), int(x)
    return None


def reshape_grib_array(arr: np.ndarray, grid_shape: tuple[int, int] | None) -> np.ndarray:
    arr = np.asarray(arr)
    if grid_shape and arr.ndim == 1 and arr.size == int(grid_shape[0]) * int(grid_shape[1]):
        return arr.reshape(grid_shape)
    return arr


def downsample_array(arr: np.ndarray) -> np.ndarray:
    arr = np.asarray(arr)
    if not arr.shape:
        return arr
    slices = downsample_slices(tuple(arr.shape))
    return arr[tuple(slices)]


def infer_meteosat_decoded_std(sample: dict, var_name: str, attrs: dict[str, Any], mapping: dict) -> str:
    search_text = normalize_name(" ".join([var_name, attrs.get("GRIB_shortName", ""), attrs.get("long_name", ""), attrs.get("GRIB_name", "")]))
    product = str(sample.get("product", "")).upper()
    if "quality" in search_text or search_text.endswith("qi") or "qi" in normalize_name(var_name):
        return "quality_flag"
    if product == "CLM" and ("cloudmask" in search_text or var_name == "p260537"):
        return "cloud_mask"
    if product == "CTH":
        if "cloudtopheight" in search_text or normalize_name(var_name) == "ctoph":
            return "cloud_top_height"
        if "cloudtoptemperature" in search_text or normalize_name(var_name) in {"ctt", "cttoph"}:
            return "cloud_top_temperature"
        if "cloudtoppressure" in search_text or normalize_name(var_name) in {"ctp", "ctopp"}:
            return "cloud_top_pressure"
    resolved = resolve_mapping(sample, [var_name], mapping)
    return resolved.get(var_name, "unknown")


def decoded_grib_record(sample: dict, member_name: str, records: list[dict], ds_index: int, var_name: str, std: str, arr: np.ndarray, da: Any, attrs: dict[str, Any]) -> dict[str, Any]:
    base = dict(records[0]) if records else {}
    finite = np.isfinite(arr.astype("float64", copy=False)) if is_numeric_dtype(arr.dtype) else np.ones(arr.shape, dtype=bool)
    row = {
        **base,
        "satellite_family": sample["satellite_family"],
        "satellite": sample["satellite"],
        "product": sample["product"],
        "zip_file": sample["file_path"],
        "grib_member": member_name,
        "decode_engine": "cfgrib/eccodes",
        "decode_status": "OK",
        "decoded_dataset_index": ds_index,
        "decoded_variable": var_name,
        "decoded_standard_variable": std,
        "decoded_shape": str(tuple(arr.shape)),
        "decoded_dtype": str(da.dtype),
        "decoded_units": attrs.get("units", ""),
        "decoded_long_name": attrs.get("long_name", attrs.get("GRIB_name", "")),
        "decoded_short_name": attrs.get("GRIB_shortName", ""),
        "decoded_min": "",
        "decoded_max": "",
    }
    if is_numeric_dtype(arr.dtype) and arr.size and finite.any():
        vals = arr[finite]
        row["decoded_min"] = float(np.nanmin(vals))
        row["decoded_max"] = float(np.nanmax(vals))
    return row


def parse_grib_messages(sample: dict, member_name: str, data: bytes) -> list[dict]:
    rows = []
    pos = 0
    msg_index = 0
    while True:
        idx = data.find(b"GRIB", pos)
        if idx < 0 or idx + 16 > len(data):
            break
        discipline = data[idx + 6] if idx + 6 < len(data) else ""
        edition = data[idx + 7] if idx + 7 < len(data) else ""
        if edition == 2:
            total_length = int.from_bytes(data[idx + 8 : idx + 16], "big", signed=False)
        elif edition == 1:
            total_length = int.from_bytes(data[idx + 4 : idx + 7], "big", signed=False)
        else:
            total_length = 0
        if total_length <= 0 or idx + total_length > len(data):
            total_length = len(data) - idx
        payload = data[idx : idx + total_length]
        msg_index += 1
        rec = {
            "satellite_family": sample["satellite_family"],
            "satellite": sample["satellite"],
            "product": sample["product"],
            "zip_file": sample["file_path"],
            "grib_member": member_name,
            "message_index": msg_index,
            "edition": edition,
            "discipline": discipline,
            "total_length": total_length,
            "sections": "",
            "grid_template": "",
            "product_template": "",
            "data_representation_template": "",
            "number_of_data_points": "",
            "nx": "",
            "ny": "",
            "grid_metadata_note": "",
        }
        if edition == 2:
            rec.update(parse_grib2_sections(payload))
        rows.append(rec)
        pos = idx + total_length
    if not rows:
        rows.append({"satellite_family": sample["satellite_family"], "satellite": sample["satellite"], "product": sample["product"], "zip_file": sample["file_path"], "grib_member": member_name, "message_index": 0, "edition": "", "discipline": "", "total_length": len(data), "sections": "NO_GRIB_MAGIC_FOUND", "grid_metadata_note": "Could not find GRIB marker"})
    return rows


def parse_grib2_sections(payload: bytes) -> dict[str, Any]:
    pos = 16
    sections = []
    out: dict[str, Any] = {}
    while pos + 5 <= len(payload):
        if payload[pos : pos + 4] == b"7777":
            break
        sec_len = int.from_bytes(payload[pos : pos + 4], "big", signed=False)
        sec_no = payload[pos + 4]
        if sec_len < 5 or pos + sec_len > len(payload):
            sections.append(f"{sec_no}:bad_len_{sec_len}")
            break
        sec = payload[pos : pos + sec_len]
        sections.append(str(sec_no))
        if sec_no == 3 and sec_len >= 20:
            out.update(parse_grib2_grid_section(sec))
        elif sec_no == 4 and sec_len >= 11:
            out["product_template"] = int.from_bytes(sec[7:9], "big", signed=False)
            # discipline/category/parameter live in product definition template for most templates.
            if sec_len >= 13:
                out["parameter_category"] = sec[9]
                out["parameter_number"] = sec[10]
        elif sec_no == 5 and sec_len >= 12:
            out["number_of_data_points_section5"] = int.from_bytes(sec[5:9], "big", signed=False)
            out["data_representation_template"] = int.from_bytes(sec[9:11], "big", signed=False)
        pos += sec_len
    out["sections"] = ",".join(sections)
    return out


def parse_grib2_grid_section(sec: bytes) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        out["number_of_data_points"] = int.from_bytes(sec[6:10], "big", signed=False)
        out["grid_template"] = int.from_bytes(sec[12:14], "big", signed=False)
        template = out["grid_template"]
        body = sec[14:]
        if template == 0 and len(body) >= 34:  # regular lat/lon
            out["nx"] = int.from_bytes(body[6:10], "big", signed=False)
            out["ny"] = int.from_bytes(body[10:14], "big", signed=False)
            out["grid_metadata_note"] = "GRIB2 template 3.0 regular lat/lon"
        elif template == 90 and len(body) >= 40:  # space view perspective / geostationary
            # Template 3.90 offsets vary by centre; parse defensively.
            values = [int.from_bytes(body[i : i + 4], "big", signed=False) for i in range(0, min(len(body) - 3, 64), 4)]
            candidates = [v for v in values if 100 <= v <= 20000]
            if len(candidates) >= 2:
                out["nx"] = candidates[0]
                out["ny"] = candidates[1]
            out["grid_metadata_note"] = f"GRIB2 template 3.90 space-view/geostationary; raw_4byte_values={values[:12]}"
        else:
            out["grid_metadata_note"] = f"GRIB2 grid template {template}; manual decoder may be needed"
    except Exception as exc:
        out["grid_metadata_note"] = f"grid parse failed: {type(exc).__name__}: {exc}"
    return out


def infer_meteosat_std(sample: dict, rec: dict) -> str:
    product = str(sample.get("product", "")).upper()
    if product == "CLM":
        return "cloud_mask"
    if product == "CTH":
        return "cloud_top_height"
    return "unknown"


def resolve_mapping(sample: dict, variable_names: list[str], mapping: dict) -> dict[str, str]:
    key = product_key(sample["satellite_family"], sample["product"])
    product_map = mapping.get(key, {})
    resolved = {}
    normalized = {normalize_name(v): v for v in variable_names}
    for std, candidates in product_map.items():
        for cand in candidates or []:
            cand_norm = normalize_name(str(cand))
            for var_norm, original in normalized.items():
                if not cand_norm:
                    continue
                if len(cand_norm) <= 2:
                    matched = cand_norm == var_norm
                else:
                    matched = cand_norm == var_norm or cand_norm in var_norm or var_norm in cand_norm
                if matched and original not in resolved:
                    resolved[original] = std
    return resolved


def normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def variable_record(sample: dict, name: str, std: str, shape: Any, dtype: Any, attrs: dict[str, Any], reader: str) -> dict[str, Any]:
    return {
        "satellite_family": sample.get("satellite_family", ""),
        "satellite": sample.get("satellite", ""),
        "product": sample.get("product", ""),
        "product_key": product_key(sample.get("satellite_family", ""), sample.get("product", "")),
        "file_path": sample.get("file_path", ""),
        "nominal_time": sample.get("nominal_time", ""),
        "reader": reader,
        "variable": name,
        "standard_variable": std,
        "shape": str(tuple(shape) if isinstance(shape, (tuple, list)) else shape),
        "dtype": str(dtype),
        "units": attrs.get("units", attrs.get("unit", "")) if attrs else "",
        "attrs_json": json.dumps(attrs, ensure_ascii=False, default=str)[:2000],
    }


def stats_record(sample: dict, name: str, std: str, arr: np.ndarray, attrs: dict[str, Any]) -> dict[str, Any]:
    arr = np.asarray(arr)
    raw_shape = tuple(arr.shape)
    arr_float = arr.astype("float64", copy=False) if np.issubdtype(arr.dtype, np.number) else arr
    fill_values = []
    for k in ["_FillValue", "FillValue", "missing_value", "InvalidValue"]:
        if k in attrs:
            v = attrs[k]
            if isinstance(v, str):
                try:
                    v = float(v.strip("[]"))
                except Exception:
                    pass
            fill_values.append(v[0] if isinstance(v, (list, tuple)) and v else v)
    mask = np.zeros(arr.shape, dtype=bool)
    for fv in fill_values:
        try:
            mask |= arr == fv
        except Exception:
            pass
    finite = np.isfinite(arr_float) if np.issubdtype(arr_float.dtype, np.number) else np.ones(arr.shape, dtype=bool)
    valid = arr_float[finite & ~mask] if np.issubdtype(arr_float.dtype, np.number) else np.asarray([])
    total = int(arr.size)
    row = {
        "satellite_family": sample.get("satellite_family", ""),
        "satellite": sample.get("satellite", ""),
        "product": sample.get("product", ""),
        "file_path": sample.get("file_path", ""),
        "variable": name,
        "standard_variable": std,
        "shape": str(raw_shape),
        "dtype": str(arr.dtype),
        "sample_count": total,
        "fill_ratio": round(float(mask.sum() / total), 6) if total else 0,
        "invalid_ratio": round(float((~finite).sum() / total), 6) if total else 0,
        "min": "",
        "max": "",
        "mean": "",
        "p01": "",
        "p50": "",
        "p99": "",
    }
    if valid.size:
        row.update(
            {
                "min": float(np.nanmin(valid)),
                "max": float(np.nanmax(valid)),
                "mean": float(np.nanmean(valid)),
                "p01": float(np.nanpercentile(valid, 1)),
                "p50": float(np.nanpercentile(valid, 50)),
                "p99": float(np.nanpercentile(valid, 99)),
            }
        )
    return row


def sample_array_hdf(obj) -> np.ndarray:
    shape = tuple(obj.shape)
    if not shape:
        return np.asarray(obj[()])
    slices = downsample_slices(shape)
    return np.asarray(obj[tuple(slices)])


def sample_array_nc(var) -> np.ndarray:
    shape = tuple(var.shape)
    if not shape:
        return np.asarray(var[:])
    slices = downsample_slices(shape)
    return np.asarray(var[tuple(slices)])


def downsample_slices(shape: tuple[int, ...]) -> list[slice]:
    if not shape:
        return []
    total = math.prod(max(1, int(x)) for x in shape)
    if total <= MAX_STATS_VALUES:
        return [slice(None)] * len(shape)
    factor = (total / MAX_STATS_VALUES) ** (1 / len(shape))
    return [slice(None, None, max(1, int(math.ceil(factor)))) for _ in shape]


def choose_quicklook_variable(datasets: list[tuple[str, Any]], mapped: dict[str, str]) -> tuple[str, str] | None:
    priority = [
        "cloud_mask",
        "cloud_top_height",
        "cloud_top_temperature",
        "cloud_top_pressure",
        "cloud_phase",
        "cloud_probability",
        "radiance",
        "reflectance",
    ]
    for std in priority:
        for name, obj in datasets:
            if mapped.get(name) == std and len(getattr(obj, "shape", ())) >= 2 and is_numeric_dtype(getattr(obj, "dtype", "")):
                return name, std
    for name, obj in datasets:
        if len(getattr(obj, "shape", ())) >= 2 and is_numeric_dtype(getattr(obj, "dtype", "")):
            return name, mapped.get(name, "unknown")
    return None


def is_numeric_dtype(dtype: Any) -> bool:
    try:
        return np.issubdtype(np.dtype(dtype), np.number)
    except Exception:
        return False


def make_quicklook(sample: dict, var_name: str, arr: np.ndarray, reader: str) -> str | None:
    if isinstance(plt, Exception):
        return None
    arr = np.asarray(arr)
    while arr.ndim > 2:
        arr = arr[0]
    if arr.ndim != 2 or arr.size == 0:
        return None
    try:
        a = arr.astype("float64", copy=False)
        finite = np.isfinite(a)
        if not finite.any():
            return None
        vmin, vmax = np.nanpercentile(a[finite], [2, 98])
        QUICKLOOK_DIR.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{sample.get('satellite')}_{sample.get('product')}_{var_name}")[:180]
        out = QUICKLOOK_DIR / f"{safe}.png"
        fig, ax = plt.subplots(figsize=(6, 5), dpi=120)
        im = ax.imshow(a, origin="upper", cmap="viridis", vmin=vmin, vmax=vmax)
        ax.set_title(f"{sample.get('satellite')} {sample.get('product')} {var_name}", fontsize=9)
        ax.set_xticks([])
        ax.set_yticks([])
        fig.colorbar(im, ax=ax, shrink=0.7)
        fig.tight_layout()
        fig.savefig(out)
        plt.close(fig)
        return str(out)
    except Exception:
        return None


def attr_to_str(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    if hasattr(value, "tolist"):
        value = value.tolist()
    return str(value)


def expected_missing(sample: dict, present_stds: Any, mapping: dict, anomaly_rows: list[dict]) -> None:
    key = product_key(sample["satellite_family"], sample["product"])
    expected = set(mapping.get(key, {}).keys())
    present = set(present_stds)
    for std in sorted(expected - present):
        if std == "projection":
            continue
        anomaly_rows.append(anomaly_record(sample, "WARN", "mapped_standard_variable_not_found", std))


def anomaly_record(sample: dict, severity: str, issue: str, detail: str) -> dict[str, Any]:
    return {
        "satellite_family": sample.get("satellite_family", ""),
        "satellite": sample.get("satellite", ""),
        "product": sample.get("product", ""),
        "file_path": sample.get("file_path", ""),
        "severity": severity,
        "issue": issue,
        "detail": detail,
    }


def inspect_sample(sample: dict, mapping: dict) -> tuple[list[dict], list[dict], list[dict], list[dict], dict]:
    path = Path(sample["file_path"])
    suffix = "".join(path.suffixes).lower()
    result = {
        "satellite_family": sample.get("satellite_family", ""),
        "satellite": sample.get("satellite", ""),
        "product": sample.get("product", ""),
        "file_path": str(path),
        "reader": "",
        "quicklook_path": "",
        "status": "OK",
        "message": "",
    }
    try:
        if sample.get("satellite_family") == "Meteosat" and ".zip" in suffix:
            vars_, stats, anomalies, grib, quicklook = inspect_meteosat_zip(sample, mapping)
            result["reader"] = "zip_grib_metadata"
        elif any(ext in suffix for ext in [".hdf", ".h5"]) and not isinstance(h5py, Exception):
            vars_, stats, anomalies, quicklook = inspect_hdf(sample, mapping)
            grib = []
            result["reader"] = "hdf5"
        elif ".nc" in suffix and not isinstance(netCDF4, Exception):
            vars_, stats, anomalies, quicklook = inspect_netcdf(sample, mapping)
            grib = []
            result["reader"] = "netcdf"
        else:
            vars_, stats, grib = [], [], []
            anomalies = [anomaly_record(sample, "ERROR", "unsupported_file_or_missing_library", suffix)]
            quicklook = None
            result["reader"] = "unsupported"
            result["status"] = "ERROR"
        result["quicklook_path"] = quicklook or ""
        if any(a.get("severity") == "ERROR" for a in anomalies):
            result["status"] = "ERROR"
        elif any(a.get("severity") == "WARN" for a in anomalies):
            result["status"] = "WARN"
        result["message"] = f"variables={len(vars_)} stats={len(stats)} anomalies={len(anomalies)}"
        return vars_, stats, anomalies, grib, result
    except Exception as exc:
        result["status"] = "ERROR"
        result["message"] = f"{type(exc).__name__}: {exc}"
        return [], [], [anomaly_record(sample, "ERROR", "sample_read_failed", result["message"])], [], result


def priority_confirmation_report(samples: list[dict], results: list[dict], grib_rows: list[dict], anomalies: list[dict]) -> str:
    products_present = {(s.get("satellite_family"), s.get("product")) for s in samples}
    lines = [
        "# Priority Products To Confirm Or Supplement",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "## Must Confirm / Supplement",
        "- FY4B GEO 4000M and CTP: searched in `D:\\AAAresearch_paper\\data\\FY4B-GEO`, `D:\\AAAresearch_paper\\data\\FY4B-CTP`, `D:\\AAAresearch_paper\\data\\FY4B_Data`, and `D:\\AAAresearch_paper\\second_report\\satellitedata\\FY4B_Data`; see sample table for whether files were found and readable.",
        "- FY4B CTP: required for v1 cloud-top pressure; sample is included only if found locally.",
        "- FY4B CLP/cloud phase: required/strongly useful for phase; sample is included only if found locally.",
        "- GOES ACTPF/CTPF/CTTF: not present in current `E:\\GEO_Cloud_2024` sample set; should be downloaded if phase/pressure/temperature must be independent products rather than ACHAF bundled fields.",
        "- Meteosat GRIB: ZIP members are now read, GRIB sections are parsed, and packed-grid values are decoded with `cfgrib/eccodes` when available.",
        "",
        "## Product Presence In This Sample Run",
    ]
    for fam, prod in sorted(products_present):
        lines.append(f"- {fam}_{prod}")
    lines.append("")
    lines.append("## Meteosat GRIB Findings")
    if grib_rows:
        for row in grib_rows[:20]:
            lines.append(
                f"- {Path(row.get('zip_file','')).name} member `{row.get('grib_member')}` "
                f"edition={row.get('edition')} discipline={row.get('discipline')} "
                f"grid_template={row.get('grid_template')} nx={row.get('nx')} ny={row.get('ny')} "
                f"points={row.get('number_of_data_points') or row.get('number_of_data_points_section5')} "
                f"note={row.get('grid_metadata_note')}"
            )
    else:
        lines.append("- No GRIB rows parsed.")
    lines.append("")
    lines.append("## Blocking Anomalies")
    for a in anomalies:
        if a.get("severity") == "ERROR" or "grib" in a.get("issue", "").lower():
            lines.append(f"- {a.get('satellite')} {a.get('product')} {a.get('severity')} {a.get('issue')}: {a.get('detail')}")
    return "\n".join(lines)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    QUICKLOOK_DIR.mkdir(parents=True, exist_ok=True)
    copy_mapping_to_out()
    mapping = load_mapping()
    log("Selecting one sample per satellite/product")
    samples = select_samples()
    write_csv(OUT_DIR / "one_sample_each_product_selection.csv", samples)
    all_vars: list[dict] = []
    all_stats: list[dict] = []
    all_anomalies: list[dict] = []
    all_grib: list[dict] = []
    results: list[dict] = []
    for sample in samples:
        log(f"Reading {sample.get('satellite_family')} {sample.get('satellite')} {sample.get('product')}: {sample.get('file_path')}")
        vars_, stats, anomalies, grib, result = inspect_sample(sample, mapping)
        all_vars.extend(vars_)
        all_stats.extend(stats)
        all_anomalies.extend(anomalies)
        all_grib.extend(grib)
        results.append(result)

    write_csv(OUT_DIR / "one_sample_each_product_read_summary.csv", results)
    write_csv(OUT_DIR / "one_sample_each_product_variables.csv", all_vars)
    write_csv(OUT_DIR / "one_sample_each_product_variable_statistics.csv", all_stats)
    write_csv(OUT_DIR / "one_sample_each_product_internal_anomalies.csv", all_anomalies)
    write_csv(OUT_DIR / "meteosat_grib_deep_check.csv", all_grib)
    write_md(OUT_DIR / "priority_products_to_confirm.md", priority_confirmation_report(samples, results, all_grib, all_anomalies))

    ok = sum(1 for r in results if r["status"] == "OK")
    warn = sum(1 for r in results if r["status"] == "WARN")
    err = sum(1 for r in results if r["status"] == "ERROR")
    lines = [
        "# One Sample Each Product Read Report",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"- samples: {len(results)}",
        f"- OK: {ok}",
        f"- WARN: {warn}",
        f"- ERROR: {err}",
        f"- quicklooks: `{QUICKLOOK_DIR}`",
        "",
        "## Key Notes",
        "- NetCDF/HDF products were read as real arrays; statistics and quicklook PNGs were generated where a 2D numeric variable exists.",
        "- Meteosat ZIP files were opened and internal `.grb` members were read as GRIB byte streams; GRIB sections/grid metadata and decoded cfgrib variables were written to `meteosat_grib_deep_check.csv`.",
        "- Meteosat packed data values are decoded with `cfgrib/eccodes` into real cloud-mask/cloud-height arrays where those variables exist.",
        "- See `manual_variable_mapping_by_product.yaml` for explicit product-to-variable mappings.",
        "",
        "## Sample Results",
    ]
    for r in results:
        lines.append(f"- {r['satellite']} {r['product']}: {r['status']} via {r['reader']} - {r['message']} quicklook={r['quicklook_path'] or 'none'}")
    write_md(OUT_DIR / "one_sample_each_product_read_report.md", "\n".join(lines))
    log(f"Finished. samples={len(results)} OK={ok} WARN={warn} ERROR={err}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise
