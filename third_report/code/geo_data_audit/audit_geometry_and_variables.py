from __future__ import annotations

import csv
import json
import math
import os
import re
import tempfile
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import h5py
import netCDF4
import numpy as np
import pandas as pd
import yaml


BASE_REPORT_DIR = Path(r"D:\AAAresearch_paper\data_check_report")
OUT_DIR = BASE_REPORT_DIR / "geometry_variable_audit"
PARSED_METADATA = BASE_REPORT_DIR / "parsed_file_metadata.csv"
MAPPING_YAML = BASE_REPORT_DIR / "manual_variable_mapping_by_product.yaml"
DATA_ROOTS = [Path(r"D:\AAAresearch_paper\data"), Path(r"E:\GEO_Cloud_2024")]
SUPPORTED_SUFFIXES = {".nc", ".hdf", ".h5", ".zip", ".grb", ".grib", ".grib2", ".nat", ".xml"}
MAX_STATS_VALUES = 250_000

GEOMETRY_VARIABLES = [
    "latitude",
    "longitude",
    "projection_x",
    "projection_y",
    "geostationary_projection",
    "satellite_longitude",
    "satellite_height",
    "sweep_axis",
    "earth_radius",
    "observation_time",
    "start_time",
    "end_time",
    "scan_time",
    "solar_zenith_angle",
    "solar_azimuth_angle",
    "sensor_zenith_angle",
    "sensor_azimuth_angle",
    "relative_azimuth_angle",
    "glint_angle",
    "day_night_flag",
    "terminator_flag",
]

QUALITY_CAPS = [
    "QA",
    "DQF",
    "quality_flag",
    "confidence",
    "retrieval_quality",
    "processing_flag",
    "valid_pixel_flag",
    "cloud_confidence",
    "fill_value",
    "algorithm_status_flag",
]

CLOUD_VARIABLES = [
    "cloud_mask",
    "cloud_probability",
    "cloud_top_height",
    "quality_flag",
    "cloud_top_temperature",
    "cloud_top_pressure",
    "cloud_phase",
    "cloud_type",
    "cloud_optical_thickness",
    "cloud_effective_radius",
    "cloud_water_path",
    "ice_water_path",
    "uncertainty",
]

STATUS_DIRECT = "AVAILABLE_DIRECT"
STATUS_DERIVED = "AVAILABLE_DERIVED"
STATUS_MISSING = "MISSING_NEED_DOWNLOAD"
STATUS_UNKNOWN = "UNKNOWN_NEED_MANUAL_CHECK"


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


def safe_json(value: Any, limit: int = 2200) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return text[:limit]


def attr_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    if isinstance(value, np.generic):
        return value.item()
    if hasattr(value, "tolist"):
        return value.tolist()
    return value


def norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def load_mapping() -> dict[str, Any]:
    if MAPPING_YAML.exists():
        return yaml.safe_load(MAPPING_YAML.read_text(encoding="utf-8-sig")) or {}
    return {}


def inventory_files() -> pd.DataFrame:
    if PARSED_METADATA.exists():
        df = pd.read_csv(PARSED_METADATA, encoding="utf-8-sig", low_memory=False)
        df = df[df["file_path"].astype(str).map(lambda p: Path(p).suffix.lower() in SUPPORTED_SUFFIXES)].copy()
        df = df[df["file_path"].astype(str).map(lambda p: Path(p).exists())]
    else:
        df = pd.DataFrame()
    known = set(df["file_path"].astype(str)) if not df.empty else set()
    extra = []
    for root in DATA_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            if str(path) in known:
                continue
            meta = parse_path(path)
            meta.update({"file_path": str(path), "file_size": path.stat().st_size, "suffix": path.suffix.lower(), "parse_status": "script_scan"})
            extra.append(meta)
    if extra:
        df = pd.concat([df, pd.DataFrame(extra)], ignore_index=True)
    if df.empty:
        return df
    for col in ["satellite_family", "satellite", "sensor", "product", "level", "nominal_time", "start_time", "end_time", "spatial_resolution", "version", "projection", "platform_detail"]:
        if col not in df:
            df[col] = ""
    df["file_size"] = df.get("file_size", df.get("size_bytes", 0)).fillna(0).astype("int64")
    df["product"] = df.apply(lambda r: r["product"] if str(r["product"]) not in {"", "nan", "None"} else parse_path(Path(str(r["file_path"]))).get("product", ""), axis=1)
    df["satellite_family"] = df.apply(lambda r: r["satellite_family"] if str(r["satellite_family"]) not in {"", "nan", "None", "UNKNOWN"} else parse_path(Path(str(r["file_path"]))).get("satellite_family", "UNKNOWN"), axis=1)
    df["satellite"] = df.apply(lambda r: r["satellite"] if str(r["satellite"]) not in {"", "nan", "None"} else parse_path(Path(str(r["file_path"]))).get("satellite", ""), axis=1)
    return df


def parse_path(path: Path) -> dict[str, str]:
    text = str(path)
    name = path.name
    upper = text.upper()
    meta = {"satellite_family": "UNKNOWN", "satellite": "", "sensor": "", "product": "", "level": "", "nominal_time": ""}
    if "FY4B" in upper or "FY4A" in upper:
        sat = "FY4B" if "FY4B" in upper else "FY4A"
        meta.update({"satellite_family": "FY4B" if sat == "FY4B" else "FY4A", "satellite": sat, "sensor": "AGRI"})
        for prod in ["FDI", "GEO", "CLM", "CLT", "CTH", "CTT", "CTP", "CLP", "COT", "CER"]:
            if f"_{prod}" in upper or f"-{prod}" in upper or path.parent.name.upper().endswith(prod):
                meta["product"] = prod
                break
    elif "GOES-16" in upper or "_G16" in upper or "G16_" in upper:
        meta.update({"satellite_family": "GOES", "satellite": "GOES-16", "sensor": "ABI"})
    elif "GOES-18" in upper or "_G18" in upper or "G18_" in upper:
        meta.update({"satellite_family": "GOES", "satellite": "GOES-18", "sensor": "ABI"})
    elif "HIMAWARI" in upper or "AHI-" in upper or "_H09" in upper or "_H08" in upper:
        sat = "Himawari-9" if "H09" in upper or "HIMAWARI-9" in upper else "Himawari-8"
        meta.update({"satellite_family": "Himawari", "satellite": sat, "sensor": "AHI"})
    elif "METEOSAT" in upper or "MSG" in upper or "SEVI" in upper:
        sat = "Meteosat-IODC" if "IODC" in upper or "MSG2" in upper else "Meteosat-0deg"
        meta.update({"satellite_family": "Meteosat", "satellite": sat, "sensor": "SEVIRI"})
    if meta["satellite_family"] == "GOES":
        m = re.search(r"ABI-L[12][A-Z]?-(\w+?)-", name)
        if m:
            meta["product"] = m.group(1).replace("M6", "").replace("F", "F")
        for prod in ["ACMF", "ACHAF", "ACTPF", "ACHTF", "CTPF", "CTTF", "CODF", "CPSF", "RadF"]:
            if prod.upper() in upper:
                meta["product"] = prod
    elif meta["satellite_family"] == "Himawari":
        for prod in ["CMSK", "CHGT", "HSD", "COT", "CER", "CLP", "CLT"]:
            if prod in upper:
                meta["product"] = prod
    elif meta["satellite_family"] == "Meteosat":
        for prod, tokens in {"CLM": ["MSGCLMK", "\\CLM\\"], "CTH": ["MSGCLTH", "\\CTH\\"], "CTTH": ["CTTH"], "CMA": ["CMA"], "CT": ["\\CT\\", "MSGCT"], "OCA": ["OCA"], "CMIC": ["CMIC"], "L15": [".NAT", "NATIVE"]}.items():
            if any(tok in upper for tok in tokens):
                meta["product"] = prod
                break
    if not meta["product"]:
        meta["product"] = path.parent.name
    m = re.search(r"(20\d{6,12})", name)
    if m:
        meta["nominal_time"] = m.group(1)
    return meta


def choose_samples(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    good = df[df["satellite_family"].isin(["FY4B", "GOES", "Himawari", "Meteosat"])].copy()
    good = good[good["product"].astype(str).str.len() > 0]
    for _, group in good.groupby(["satellite_family", "satellite", "product"], dropna=False):
        group = group.sort_values(["nominal_time", "file_path"], na_position="last").reset_index(drop=True)
        if len(group) <= 3:
            picks = list(range(len(group)))
        else:
            picks = [0, len(group) // 2, len(group) - 1]
        for idx in picks:
            row = group.iloc[idx].to_dict()
            row["sample_position"] = "first" if idx == 0 else "last" if idx == len(group) - 1 else "middle"
            row["product_file_count"] = len(group)
            rows.append(row)
    return pd.DataFrame(rows)


def standard_guess(name: str, attrs: dict[str, Any], mapping_key: str = "") -> str:
    n = norm(name)
    text = norm(" ".join([name, str(attrs.get("long_name", "")), str(attrs.get("description", "")), str(attrs.get("GRIB_shortName", "")), str(attrs.get("GRIB_name", ""))]))
    if "nominalsatellitesubpointlon" in n:
        return "satellite_longitude"
    if "nominalsatelliteheight" in n:
        return "satellite_height"
    if "nominalsatellitesubpointlat" in n or "geospatiallatlonextent" in n:
        return "unknown"
    if any(token in n for token in ["zenithangle", "azimuthangle", "anglebounds", "localzenith", "solarzenith"]):
        return "unknown"
    exact = {
        "x": "projection_x",
        "y": "projection_y",
        "latitude": "latitude",
        "lat": "latitude",
        "longitude": "longitude",
        "lon": "longitude",
        "goesimagerprojection": "geostationary_projection",
        "dqf": "quality_flag",
        "qa": "quality_flag",
        "ctoph": "cloud_top_height",
        "ctophqi": "quality_flag",
        "p260537": "cloud_mask",
        "cldtophght": "cloud_top_height",
        "cldtoptemp": "cloud_top_temperature",
        "cldtoppres": "cloud_top_pressure",
        "cldoptdpth": "cloud_optical_thickness",
        "cldeffrad": "cloud_effective_radius",
        "cloudprobability": "cloud_probability",
        "cloudprobabilities": "cloud_probability",
        "cloudmask": "cloud_mask",
        "cloudmaskbinary": "cloud_mask",
        "bcm": "cloud_mask",
        "acm": "cloud_mask",
        "ht": "cloud_top_height",
        "temp": "cloud_top_temperature",
        "pres": "cloud_top_pressure",
        "phase": "cloud_phase",
    }
    if n in exact:
        return exact[n]
    rules = [
        ("cloud_top_height", ["cloudtopheight", "cth"]),
        ("cloud_top_temperature", ["cloudtoptemperature", "ctt"]),
        ("cloud_top_pressure", ["cloudtoppressure", "ctp"]),
        ("cloud_optical_thickness", ["cloudoptical", "opticaldepth", "cot", "cod"]),
        ("cloud_effective_radius", ["effectiveradius", "particle", "cer", "cps"]),
        ("cloud_phase", ["cloudphase", "phase"]),
        ("cloud_type", ["cloudtype", "clt"]),
        ("cloud_mask", ["cloudmask", "clear", "clm"]),
        ("quality_flag", ["quality", "dqf", "flag", "confidence"]),
        ("latitude", ["latitude"]),
        ("longitude", ["longitude"]),
        ("observation_time", ["timebounds", "time"]),
    ]
    for std, tokens in rules:
        if any(t in text for t in tokens):
            return std
    return "unknown"


def sampled_stats(arr: np.ndarray, attrs: dict[str, Any]) -> dict[str, Any]:
    out = {"sample_min": "", "sample_max": "", "sample_mean": "", "fill_ratio": "", "invalid_ratio": ""}
    arr = np.asarray(arr)
    if arr.size == 0 or not np.issubdtype(arr.dtype, np.number):
        return out
    sample = downsample(arr)
    values = sample.astype("float64", copy=False)
    finite = np.isfinite(values)
    fill = np.zeros(values.shape, dtype=bool)
    fill_candidates = []
    for key in ["_FillValue", "missing_value", "fill_value", "InvalidValue"]:
        if key in attrs:
            fill_candidates.append(attrs[key])
    for fv in fill_candidates:
        try:
            fv0 = np.asarray(fv).ravel()[0]
            fill |= values == float(fv0)
        except Exception:
            pass
    valid = values[finite & ~fill]
    out["invalid_ratio"] = round(float((~finite).sum() / values.size), 6) if values.size else ""
    out["fill_ratio"] = round(float(fill.sum() / values.size), 6) if values.size else ""
    if valid.size:
        out["sample_min"] = float(np.nanmin(valid))
        out["sample_max"] = float(np.nanmax(valid))
        out["sample_mean"] = float(np.nanmean(valid))
    return out


def downsample(arr: np.ndarray) -> np.ndarray:
    if arr.size <= MAX_STATS_VALUES or arr.ndim == 0:
        return arr
    factor = (arr.size / MAX_STATS_VALUES) ** (1 / arr.ndim)
    return arr[tuple(slice(None, None, max(1, int(math.ceil(factor)))) for _ in arr.shape)]


def common_row(file_meta: dict[str, Any], reader: str, group_path: str, name: str, shape: Any, dtype: Any, attrs: dict[str, Any]) -> dict[str, Any]:
    return {
        "satellite_family": file_meta.get("satellite_family", ""),
        "satellite": file_meta.get("satellite", ""),
        "sensor": file_meta.get("sensor", ""),
        "product": file_meta.get("product", ""),
        "sample_position": file_meta.get("sample_position", ""),
        "product_file_count": file_meta.get("product_file_count", ""),
        "file_path": file_meta.get("file_path", ""),
        "reader": reader,
        "group_path": group_path,
        "variable_name": name,
        "standard_guess": standard_guess(name, attrs),
        "shape": str(tuple(shape) if isinstance(shape, (tuple, list)) else shape),
        "dtype": str(dtype),
        "units": attrs.get("units", attrs.get("unit", "")),
        "long_name": attrs.get("long_name", attrs.get("description", attrs.get("GRIB_name", ""))),
        "valid_min": attrs.get("valid_min", attrs.get("valid_range", "")),
        "valid_max": attrs.get("valid_max", ""),
        "fill_value": attrs.get("_FillValue", attrs.get("missing_value", attrs.get("fill_value", ""))),
        "scale_factor": attrs.get("scale_factor", ""),
        "add_offset": attrs.get("add_offset", ""),
        "coordinates": attrs.get("coordinates", ""),
        "grid_mapping": attrs.get("grid_mapping", ""),
        "projection_metadata": "",
        "time_metadata": "",
        "quality_flag_metadata": "",
        "attrs_json": safe_json(attrs),
    }


def inspect_file(file_meta: dict[str, Any]) -> list[dict[str, Any]]:
    path = Path(str(file_meta["file_path"]))
    suffixes = "".join(path.suffixes).lower()
    meta_rows = file_metadata_rows(file_meta)
    if ".zip" in suffixes and file_meta.get("satellite_family") == "Meteosat":
        return meta_rows + inspect_meteosat_zip(file_meta)
    if path.suffix.lower() in {".grb", ".grib", ".grib2"}:
        return meta_rows + inspect_grib_path(file_meta, path)
    if ".nc" in suffixes:
        return meta_rows + inspect_netcdf(file_meta, path)
    if path.suffix.lower() in {".hdf", ".h5"}:
        return meta_rows + inspect_hdf(file_meta, path)
    if path.suffix.lower() == ".xml":
        return meta_rows + inspect_xml(file_meta, path)
    return meta_rows + [common_row(file_meta, "unsupported", "", path.name, (), path.suffix, {"description": "unsupported or native binary not opened"})]


def file_metadata_rows(file_meta: dict[str, Any]) -> list[dict[str, Any]]:
    time_meta = {k: file_meta.get(k, "") for k in ["nominal_time", "start_time", "end_time"] if str(file_meta.get(k, "")) not in {"", "nan", "None"}}
    if not time_meta:
        return []
    row = common_row(file_meta, "file_metadata", "/", "FILE_TIME_METADATA", (), "metadata", {"description": "filename/metadata time fields", **time_meta})
    row["standard_guess"] = "observation_time"
    row["time_metadata"] = safe_json(time_meta)
    return [row]


def inspect_netcdf(file_meta: dict[str, Any], path: Path) -> list[dict[str, Any]]:
    rows = []
    with netCDF4.Dataset(path, "r") as ds:
        try:
            ds.set_auto_mask(False)
        except Exception:
            pass
        global_attrs = {name: attr_value(getattr(ds, name)) for name in ds.ncattrs()}
        for name, var in ds.variables.items():
            attrs = {a: attr_value(getattr(var, a)) for a in var.ncattrs()}
            row = common_row(file_meta, "netcdf", getattr(var, "group", lambda: ds)().path if hasattr(var, "group") else "/", name, getattr(var, "shape", ()), getattr(var, "dtype", ""), attrs)
            row["projection_metadata"] = safe_json({k: v for k, v in attrs.items() if "projection" in k.lower() or "perspective" in k.lower() or "longitude" in k.lower() or "sweep" in k.lower()})
            row["time_metadata"] = safe_json({k: v for k, v in {**global_attrs, **attrs}.items() if "time" in k.lower() or "date" in k.lower()})
            row["quality_flag_metadata"] = safe_json({k: v for k, v in attrs.items() if "flag" in k.lower() or "quality" in k.lower() or "confidence" in k.lower()})
            try:
                if getattr(var, "shape", ()):
                    row.update(sampled_stats(np.asarray(var[tuple(slice(None, None, max(1, math.ceil(s / 7000))) for s in var.shape)]), attrs))
                else:
                    row.update(sampled_stats(np.asarray(var[()]), attrs))
            except Exception as exc:
                row["stats_error"] = f"{type(exc).__name__}: {exc}"
            rows.append(row)
    return rows


def inspect_hdf(file_meta: dict[str, Any], path: Path) -> list[dict[str, Any]]:
    rows = []
    with h5py.File(path, "r") as h5:
        def visitor(name: str, obj: Any) -> None:
            if hasattr(obj, "shape") and hasattr(obj, "dtype"):
                attrs = {str(k): attr_value(v) for k, v in obj.attrs.items()}
                row = common_row(file_meta, "hdf5", str(Path(name).parent), Path(name).name, obj.shape, obj.dtype, attrs)
                row["projection_metadata"] = safe_json({k: v for k, v in attrs.items() if any(t in k.lower() for t in ["projection", "longitude", "satellite", "earth", "geos"])})
                row["time_metadata"] = safe_json({k: v for k, v in attrs.items() if "time" in k.lower() or "date" in k.lower()})
                row["quality_flag_metadata"] = safe_json({k: v for k, v in attrs.items() if "flag" in k.lower() or "quality" in k.lower() or "confidence" in k.lower()})
                try:
                    row.update(sampled_stats(np.asarray(obj[tuple(slice(None, None, max(1, math.ceil(s / 7000))) for s in obj.shape)] if obj.shape else obj[()]), attrs))
                except Exception as exc:
                    row["stats_error"] = f"{type(exc).__name__}: {exc}"
                rows.append(row)
        h5.visititems(visitor)
    return rows


def inspect_xml(file_meta: dict[str, Any], path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    attrs = {"description": "xml/manifest", "text_head": text[:2000]}
    return [common_row(file_meta, "xml", "/", path.name, (len(text),), "text/xml", attrs)]


def inspect_meteosat_zip(file_meta: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    path = Path(str(file_meta["file_path"]))
    with zipfile.ZipFile(path) as zf:
        rows.append(common_row(file_meta, "zip", "/", "ZIP_ENTRIES", (len(zf.namelist()),), "zip", {"entries": ";".join(zf.namelist())}))
        for name in zf.namelist():
            if name.lower().endswith((".xml", ".manifest")):
                attrs = {"description": "zip xml/manifest", "text_head": zf.read(name).decode("utf-8", errors="ignore")[:2000]}
                rows.append(common_row(file_meta, "zip_xml", "/", name, (len(attrs["text_head"]),), "text/xml", attrs))
            if name.lower().endswith((".grb", ".grib", ".grib2")):
                data = zf.read(name)
                with tempfile.NamedTemporaryFile(suffix=".grib", delete=False) as tmp:
                    tmp.write(data)
                    tmp_path = Path(tmp.name)
                try:
                    grib_meta = parse_grib_header(file_meta, name, data)
                    rows.extend(inspect_grib_path(file_meta, tmp_path, member_name=name, grib_meta=grib_meta))
                finally:
                    tmp_path.unlink(missing_ok=True)
    return rows


def inspect_grib_path(file_meta: dict[str, Any], path: Path, member_name: str = "", grib_meta: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    rows = []
    try:
        import cfgrib
        dss = cfgrib.open_datasets(str(path), indexpath="")
        for ds_index, ds in enumerate(dss):
            try:
                shape_hint = grib_shape(grib_meta or {}, ds)
                for coord in ds.coords:
                    da = ds[coord]
                    attrs = {str(k): attr_value(v) for k, v in da.attrs.items()}
                    arr_shape = tuple(da.shape)
                    row = common_row(file_meta, "cfgrib_coord", member_name, coord, arr_shape, da.dtype, attrs)
                    row["projection_metadata"] = safe_json(grib_meta or {})
                    rows.append(row)
                for name in ds.data_vars:
                    da = ds[name]
                    attrs = {str(k): attr_value(v) for k, v in da.attrs.items()}
                    arr = np.asarray(da.values)
                    if shape_hint and arr.ndim == 1 and arr.size == shape_hint[0] * shape_hint[1]:
                        arr = arr.reshape(shape_hint)
                    row = common_row(file_meta, "cfgrib", member_name, name, arr.shape, arr.dtype, attrs)
                    row["projection_metadata"] = safe_json({**(grib_meta or {}), **{k: v for k, v in attrs.items() if k.startswith("GRIB_")}})
                    row["quality_flag_metadata"] = safe_json({k: v for k, v in attrs.items() if "quality" in k.lower() or "flag" in k.lower()})
                    row.update(sampled_stats(arr, attrs))
                    rows.append(row)
            finally:
                ds.close()
    except Exception as exc:
        rows.append(common_row(file_meta, "grib_error", member_name, member_name or path.name, (), "grib", {"description": f"{type(exc).__name__}: {exc}"}))
    return rows


def parse_grib_header(file_meta: dict[str, Any], member: str, data: bytes) -> dict[str, Any]:
    idx = data.find(b"GRIB")
    if idx < 0 or idx + 16 > len(data):
        return {"grib_member": member, "error": "NO_GRIB_MAGIC"}
    edition = data[idx + 7]
    out = {"grib_member": member, "edition": edition, "discipline": data[idx + 6]}
    if edition != 2:
        return out
    total = int.from_bytes(data[idx + 8 : idx + 16], "big", signed=False)
    payload = data[idx : idx + total]
    pos = 16
    while pos + 5 <= len(payload):
        if payload[pos : pos + 4] == b"7777":
            break
        sec_len = int.from_bytes(payload[pos : pos + 4], "big", signed=False)
        sec_no = payload[pos + 4]
        sec = payload[pos : pos + sec_len]
        if sec_no == 3 and sec_len >= 20:
            out["number_of_data_points"] = int.from_bytes(sec[6:10], "big", signed=False)
            out["grid_template"] = int.from_bytes(sec[12:14], "big", signed=False)
            if out["grid_template"] == 90:
                body = sec[14:]
                vals = [int.from_bytes(body[i : i + 4], "big", signed=False) for i in range(0, min(len(body) - 3, 64), 4)]
                cand = [v for v in vals if 100 <= v <= 20000]
                if len(cand) >= 2:
                    out["nx"], out["ny"] = cand[0], cand[1]
                out["grid_metadata_note"] = f"GRIB2 template 3.90 geostationary; raw_4byte_values={vals[:12]}"
        pos += sec_len
    return out


def grib_shape(meta: dict[str, Any], ds: Any) -> tuple[int, int] | None:
    try:
        nx, ny = int(meta.get("nx") or 0), int(meta.get("ny") or 0)
        if nx and ny:
            return ny, nx
    except Exception:
        pass
    sizes = dict(getattr(ds, "sizes", {}))
    if "values" in sizes:
        root = int(math.sqrt(sizes["values"]))
        if root * root == sizes["values"]:
            return root, root
    return None


def summarize_capabilities(inv: pd.DataFrame, files: pd.DataFrame) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    geom_rows, quality_rows, cloud_rows = [], [], []
    for (family, sat), group in inv.groupby(["satellite_family", "satellite"], dropna=False):
        stds = set(group["standard_guess"].dropna().astype(str))
        names = set(group["variable_name"].dropna().astype(str))
        attrs_text = " ".join(group[["variable_name", "long_name", "units", "projection_metadata", "quality_flag_metadata"]].fillna("").astype(str).agg(" ".join, axis=1).tolist()).lower()
        products = sorted(set(group["product"].dropna().astype(str)))
        geom_status = geometry_statuses(family, sat, group, stds, names, attrs_text, products)
        for var in GEOMETRY_VARIABLES:
            geom_rows.append({"satellite_family": family, "satellite": sat, "variable": var, **geom_status[var], "evidence_products": ",".join(products)})
        q_status = quality_statuses(group, attrs_text)
        for cap in QUALITY_CAPS:
            quality_rows.append({"satellite_family": family, "satellite": sat, "quality_capability": cap, **q_status[cap], "evidence_products": ",".join(products)})
        c_status = cloud_statuses(family, sat, stds, products)
        for var in CLOUD_VARIABLES:
            cloud_rows.append({"satellite_family": family, "satellite": sat, "cloud_variable": var, **c_status[var], "evidence_products": ",".join(products)})
    return geom_rows, quality_rows, cloud_rows


def status(status: str, evidence: str, note: str = "") -> dict[str, str]:
    return {"status": status, "evidence": evidence, "note": note}


def has_standard(group: pd.DataFrame, standard: str) -> bool:
    return bool((group["standard_guess"].astype(str) == standard).any())


def has_array_standard(group: pd.DataFrame, standard: str) -> bool:
    sub = group[group["standard_guess"].astype(str) == standard]
    for shape_text in sub["shape"].fillna("").astype(str):
        dims = [int(x) for x in re.findall(r"\d+", shape_text)]
        if len(dims) >= 1 and math.prod(dims) > 1:
            return True
    return False


def geometry_statuses(family: str, sat: str, group: pd.DataFrame, stds: set[str], names: set[str], text: str, products: list[str]) -> dict[str, dict[str, str]]:
    out = {v: status(STATUS_MISSING, "", "") for v in GEOMETRY_VARIABLES}
    for v in ["projection_x", "projection_y", "geostationary_projection", "observation_time"]:
        if has_standard(group, v):
            out[v] = status(STATUS_DIRECT, v)
    for v in ["latitude", "longitude"]:
        if has_array_standard(group, v):
            out[v] = status(STATUS_DIRECT, v)
    if has_standard(group, "satellite_height"):
        out["satellite_height"] = status(STATUS_DIRECT, "satellite_height variable/metadata")
    if has_standard(group, "satellite_longitude"):
        out["satellite_longitude"] = status(STATUS_DIRECT, "satellite_longitude variable/metadata")
    if any(t in text for t in ["perspective_point_height", "nominal_satellite_height", "satellite_height", "heightofsatellite"]):
        out["satellite_height"] = status(STATUS_DIRECT, "projection metadata")
    if any(t in text for t in ["longitude_of_projection_origin", "nominal_satellite_subpoint_lon", "satellitelongitude"]):
        out["satellite_longitude"] = status(STATUS_DIRECT, "projection metadata")
    if "sweep_angle_axis" in text or "sweep" in text:
        out["sweep_axis"] = status(STATUS_DIRECT, "projection metadata")
    if "semi_major_axis" in text or "earth_radius" in text or "earthmajoraxis" in text:
        out["earth_radius"] = status(STATUS_DIRECT, "projection metadata")
    if out["latitude"]["status"] == STATUS_MISSING and out["projection_x"]["status"] == STATUS_DIRECT and out["projection_y"]["status"] == STATUS_DIRECT and out["geostationary_projection"]["status"] == STATUS_DIRECT:
        out["latitude"] = status(STATUS_DERIVED, "projection_x/projection_y/geostationary_projection", "can derive with pyproj/satpy")
        out["longitude"] = status(STATUS_DERIVED, "projection_x/projection_y/geostationary_projection", "can derive with pyproj/satpy")
    if family == "Meteosat" and ("CLM" in products or "CTH" in products):
        if out["latitude"]["status"] == STATUS_MISSING:
            out["latitude"] = status(STATUS_DERIVED, "cfgrib latitude coordinate or GRIB template 90")
            out["longitude"] = status(STATUS_DERIVED, "cfgrib longitude coordinate or GRIB template 90")
        out["satellite_longitude"] = status(STATUS_DERIVED, "Meteosat service/GRIB area", "0deg/IODC needs explicit confirmation for production")
        out["satellite_height"] = status(STATUS_DERIVED, "GRIB template 90", "derive from template metadata if needed")
    if family == "FY4B" and "GEO" not in products and out["latitude"]["status"] == STATUS_MISSING:
        out["latitude"] = status(STATUS_MISSING, "", "FY4B GEO/navigation file not found in sampled products")
        out["longitude"] = status(STATUS_MISSING, "", "FY4B GEO/navigation file not found in sampled products")
    has_latlon_time = out["latitude"]["status"] in {STATUS_DIRECT, STATUS_DERIVED} and out["longitude"]["status"] in {STATUS_DIRECT, STATUS_DERIVED} and out["observation_time"]["status"] in {STATUS_DIRECT, STATUS_DERIVED}
    has_satpos = out["satellite_longitude"]["status"] in {STATUS_DIRECT, STATUS_DERIVED} or out["geostationary_projection"]["status"] in {STATUS_DIRECT, STATUS_DERIVED}
    if has_latlon_time:
        for v in ["solar_zenith_angle", "solar_azimuth_angle", "day_night_flag", "terminator_flag"]:
            out[v] = status(STATUS_DERIVED, "latitude/longitude/observation_time", "solar geometry can be computed")
    if has_latlon_time and has_satpos:
        for v in ["sensor_zenith_angle", "sensor_azimuth_angle"]:
            out[v] = status(STATUS_DERIVED, "lat/lon + satellite position/projection", "view geometry can be computed")
    if out["solar_azimuth_angle"]["status"] == STATUS_DERIVED and out["sensor_azimuth_angle"]["status"] == STATUS_DERIVED:
        out["relative_azimuth_angle"] = status(STATUS_DERIVED, "SAZ + VAZ")
    if out["solar_zenith_angle"]["status"] == STATUS_DERIVED and out["sensor_zenith_angle"]["status"] == STATUS_DERIVED and out["relative_azimuth_angle"]["status"] == STATUS_DERIVED:
        out["glint_angle"] = status(STATUS_DERIVED, "SZA + VZA + RAA")
    return out


def quality_statuses(group: pd.DataFrame, text: str) -> dict[str, dict[str, str]]:
    out = {q: status(STATUS_MISSING, "", "") for q in QUALITY_CAPS}
    names = " ".join(group["variable_name"].fillna("").astype(str)).lower()
    if "dqf" in names:
        out["DQF"] = status(STATUS_DIRECT, "DQF variable")
        out["quality_flag"] = status(STATUS_DIRECT, "DQF variable", "map to 0-3 after product-specific code-table review")
    if "qa" in names or "quality" in names:
        out["QA"] = status(STATUS_DIRECT, "QA/quality variable")
        out["quality_flag"] = status(STATUS_DIRECT, "QA/quality variable")
    if "confidence" in names or "confidence" in text:
        out["confidence"] = status(STATUS_DIRECT, "confidence metadata/variable")
    if "retrieval" in text and "quality" in text:
        out["retrieval_quality"] = status(STATUS_DIRECT, "retrieval quality metadata")
    if "processing" in text and "flag" in text:
        out["processing_flag"] = status(STATUS_DIRECT, "processing flag metadata")
    if "valid" in text and "flag" in text:
        out["valid_pixel_flag"] = status(STATUS_DIRECT, "valid flag metadata")
    if "cloud" in text and "confidence" in text:
        out["cloud_confidence"] = status(STATUS_DIRECT, "cloud confidence")
    if "_fillvalue" in text or "missing_value" in text or any(str(v) not in {"", "nan"} for v in group["fill_value"].fillna("").astype(str)):
        out["fill_value"] = status(STATUS_DIRECT, "fill/missing value metadata")
    if "algorithm" in text and "flag" in text:
        out["algorithm_status_flag"] = status(STATUS_DIRECT, "algorithm status metadata")
    return out


def cloud_statuses(family: str, sat: str, stds: set[str], products: list[str]) -> dict[str, dict[str, str]]:
    out = {v: status(STATUS_MISSING, "", "") for v in CLOUD_VARIABLES}
    for v in CLOUD_VARIABLES:
        if v in stds:
            out[v] = status(STATUS_DIRECT, v)
    if "quality_flag" not in stds and any(p in products for p in ["CLM", "CTH", "ACMF", "ACHAF", "CMSK", "CHGT"]):
        out["quality_flag"] = status(STATUS_UNKNOWN, "", "quality may be encoded as code-table/fill values; manual product review needed")
    if family == "GOES":
        for v, prod in [("cloud_top_temperature", "ACHTF"), ("cloud_top_pressure", "CTPF"), ("cloud_phase", "ACTPF"), ("cloud_optical_thickness", "CODF"), ("cloud_effective_radius", "CPSF")]:
            if v not in stds:
                out[v] = status(STATUS_MISSING, prod, f"download/confirm GOES {prod}")
    if family == "Meteosat":
        for v, prod in [("cloud_top_temperature", "CTTH"), ("cloud_top_pressure", "CTTH"), ("cloud_phase", "CT"), ("cloud_optical_thickness", "OCA/CMIC"), ("cloud_effective_radius", "OCA/CMIC")]:
            if v not in stds:
                out[v] = status(STATUS_MISSING, prod, f"download/confirm Meteosat {prod}")
    if family == "FY4B":
        for v, prod in [("cloud_top_pressure", "CTP"), ("cloud_phase", "CLP"), ("cloud_optical_thickness", "COT"), ("cloud_effective_radius", "CER")]:
            if v not in stds:
                out[v] = status(STATUS_MISSING, prod, f"download/confirm FY4B {prod}")
    return out


def derived_geometry_tests(geom_rows: list[dict[str, Any]], inv: pd.DataFrame) -> list[dict[str, Any]]:
    by_sat = defaultdict(dict)
    for row in geom_rows:
        by_sat[row["satellite"]][row["variable"]] = row
    tests = []
    for sat, caps in by_sat.items():
        for target in ["latitude_longitude", "VZA", "VAZ", "SZA", "SAZ", "RAA", "glint_angle", "day_night_terminator_flag"]:
            ok, inputs, reason, need = test_target(target, caps)
            tests.append({"satellite": sat, "target": target, "can_compute": ok, "inputs_used": inputs, "failure_reason": reason, "needs": need})
    return tests


def test_target(target: str, caps: dict[str, Any]) -> tuple[str, str, str, str]:
    def good(v: str) -> bool:
        return caps.get(v, {}).get("status") in {STATUS_DIRECT, STATUS_DERIVED}
    if target == "latitude_longitude":
        if good("latitude") and good("longitude"):
            return "YES", caps["latitude"].get("evidence", ""), "", ""
        return "NO", "", "missing lat/lon and insufficient projection metadata", "lat/lon or projection x/y + geostationary projection"
    if target in {"SZA", "SAZ", "day_night_terminator_flag"}:
        if good("latitude") and good("longitude") and good("observation_time"):
            return "YES", "lat/lon + observation_time", "", ""
        return "NO", "", "solar geometry needs lat/lon/time", "lat/lon + observation_time"
    if target in {"VZA", "VAZ"}:
        if good("latitude") and good("longitude") and (good("satellite_longitude") or good("geostationary_projection")):
            return "YES", "lat/lon + satellite position/projection", "", ""
        return "NO", "", "view geometry needs lat/lon and satellite position/projection", "lat/lon + satellite_longitude/height or projection"
    if target == "RAA":
        if good("relative_azimuth_angle"):
            return "YES", "SAZ + VAZ", "", ""
        return "NO", "", "needs both solar and view azimuth", "SZA/SAZ + VZA/VAZ inputs"
    if target == "glint_angle":
        if good("glint_angle"):
            return "YES", "SZA + VZA + RAA", "", ""
        return "NO", "", "needs solar/view zenith and relative azimuth", "SZA/VZA/RAA"
    return "UNKNOWN", "", "unimplemented", ""


def recommended_downloads(geom_rows: list[dict[str, Any]], cloud_rows: list[dict[str, Any]], quality_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    cloud = pd.DataFrame(cloud_rows)
    geom = pd.DataFrame(geom_rows)
    quality = pd.DataFrame(quality_rows)
    for sat in sorted(set(cloud["satellite"])):
        c = cloud[cloud["satellite"] == sat].set_index("cloud_variable")
        g = geom[geom["satellite"] == sat].set_index("variable")
        q = quality[quality["satellite"] == sat].set_index("quality_capability")
        def cstat(v): return c.loc[v, "status"] if v in c.index else STATUS_MISSING
        def gstat(v): return g.loc[v, "status"] if v in g.index else STATUS_MISSING
        if cstat("cloud_mask") == STATUS_MISSING:
            rows.append(rec(sat, 0, "cloud_mask product", "v0 requires cloud mask"))
        if cstat("cloud_top_height") == STATUS_MISSING:
            rows.append(rec(sat, 0, "cloud_top_height / CTH product", "v0 requires CTH"))
        if gstat("latitude") == STATUS_MISSING or gstat("longitude") == STATUS_MISSING:
            rows.append(rec(sat, 0, "navigation/GEO/latlon or projection metadata", "v0 reprojection requires geolocation"))
        if gstat("solar_zenith_angle") == STATUS_MISSING or gstat("sensor_zenith_angle") == STATUS_MISSING:
            rows.append(rec(sat, 1, "geometry angles or enough inputs to compute VZA/SZA/RAA", "rating needs geometry"))
        if q.loc["quality_flag", "status"] == STATUS_MISSING if "quality_flag" in q.index else True:
            rows.append(rec(sat, 1, "QA/DQF/quality flag", "rating needs reliability flag"))
        for v, item in [("cloud_top_temperature", "CTT/ACHTF/CTTH"), ("cloud_top_pressure", "CTP/CTPF/CTTH"), ("cloud_phase", "CLP/ACTPF/CT")]:
            if cstat(v) == STATUS_MISSING:
                rows.append(rec(sat, 2, item, f"v1 requires {v}"))
        for v, item in [("cloud_optical_thickness", "COT/CODF/OCA/CMIC"), ("cloud_effective_radius", "CER/CPSF/OCA/CMIC")]:
            if cstat(v) == STATUS_MISSING:
                rows.append(rec(sat, 3, item, f"v2 microphysics requires {v}"))
    return rows


def rec(sat: str, priority: int, item: str, reason: str) -> dict[str, Any]:
    return {"satellite": sat, "priority": priority, "recommended_item": item, "reason": reason}


def md_table(rows: list[dict[str, Any]], columns: list[str], limit: int = 200) -> str:
    out = ["|" + "|".join(columns) + "|", "|" + "|".join(["---"] * len(columns)) + "|"]
    for row in rows[:limit]:
        out.append("|" + "|".join(str(row.get(c, "")).replace("|", "/") for c in columns) + "|")
    return "\n".join(out)


def make_reports(inv_rows: list[dict[str, Any]], geom_rows: list[dict[str, Any]], quality_rows: list[dict[str, Any]], cloud_rows: list[dict[str, Any]], test_rows: list[dict[str, Any]], rec_rows: list[dict[str, Any]]) -> None:
    write_md(OUT_DIR / "product_variable_inventory_full.md", "# Product Variable Inventory\n\n" + md_table(inv_rows, ["satellite", "product", "sample_position", "variable_name", "standard_guess", "shape", "dtype", "units", "long_name"], 300))
    write_md(OUT_DIR / "geometry_capability_report.md", "# Geometry Capability Report\n\n" + md_table(geom_rows, ["satellite", "variable", "status", "evidence", "note"], 500))
    write_md(OUT_DIR / "quality_flag_mapping_recommendation.md", quality_report(quality_rows))
    write_md(OUT_DIR / "cloud_variable_capability_report.md", "# Cloud Variable Capability Report\n\n" + md_table(cloud_rows, ["satellite", "cloud_variable", "status", "evidence", "note"], 500))
    write_md(OUT_DIR / "derived_geometry_test_report.md", "# Derived Geometry Test Report\n\n" + md_table(test_rows, ["satellite", "target", "can_compute", "inputs_used", "failure_reason", "needs"], 500))
    write_md(OUT_DIR / "recommended_minimal_download_list.md", download_report(rec_rows))
    write_md(OUT_DIR / "geometry_variable_audit_report.md", final_report(inv_rows, geom_rows, quality_rows, cloud_rows, test_rows, rec_rows))


def quality_report(rows: list[dict[str, Any]]) -> str:
    lines = ["# Quality Flag Capability And Mapping Recommendation", ""]
    lines.append(md_table(rows, ["satellite", "quality_capability", "status", "evidence", "note"], 500))
    lines.extend(["", "## Recommendation", "- Map product-native QA/DQF code tables to 0 invalid, 1 low, 2 medium, 3 high only after per-product code-table confirmation.", "- Treat fill/missing values as invalid before applying confidence levels.", "- GOES DQF and Himawari PackedQualityFlags/DQF are direct quality inputs; Meteosat CTH `ctophqi` is a direct height-quality indicator; Meteosat CLM lacks a separate decoded QA flag in current samples."])
    return "\n".join(lines)


def download_report(rows: list[dict[str, Any]]) -> str:
    lines = ["# Recommended Minimal Download List", "", "Priority 0 blocks v0 cloud_mask + CTH/reprojection; Priority 1 blocks rating geometry/QA; Priority 2 blocks v1 cloud-top triplet/phase; Priority 3 blocks v2 microphysics.", ""]
    lines.append(md_table(sorted(rows, key=lambda r: (r["priority"], r["satellite"], r["recommended_item"])), ["priority", "satellite", "recommended_item", "reason"], 500))
    return "\n".join(lines)


def final_report(inv_rows: list[dict[str, Any]], geom_rows: list[dict[str, Any]], quality_rows: list[dict[str, Any]], cloud_rows: list[dict[str, Any]], test_rows: list[dict[str, Any]], rec_rows: list[dict[str, Any]]) -> str:
    inv = pd.DataFrame(inv_rows)
    geom = pd.DataFrame(geom_rows)
    cloud = pd.DataFrame(cloud_rows)
    tests = pd.DataFrame(test_rows)
    sats = sorted(set(inv["satellite"].dropna().astype(str)))
    lines = ["# Geometry And Variable Audit Report", "", f"Generated: {datetime.now().isoformat(timespec='seconds')}", "", "No downloads, no mosaicking, no reprojection, no source data modification were performed.", ""]
    lines.append("## 1. Current Variables")
    for sat in sats:
        sub = inv[inv["satellite"] == sat]
        stds = sorted(set(x for x in sub["standard_guess"].astype(str) if x and x != "unknown"))
        products = sorted(set(sub["product"].astype(str)))
        lines.append(f"- {sat}: products={', '.join(products)}; standard variables={', '.join(stds) if stds else 'none confidently guessed'}")
    lines.append("")
    lines.append("## 2-4. Geometry Direct / Derived / Missing")
    for sat in sats:
        sub = geom[geom["satellite"] == sat]
        direct = sorted(sub[sub["status"] == STATUS_DIRECT]["variable"].tolist())
        derived = sorted(sub[sub["status"] == STATUS_DERIVED]["variable"].tolist())
        missing = sorted(sub[sub["status"] == STATUS_MISSING]["variable"].tolist())
        lines.append(f"- {sat}: direct={', '.join(direct) or 'none'}; derived={', '.join(derived) or 'none'}; must-download/confirm={', '.join(missing) or 'none'}")
    lines.append("")
    lines.append("## 5. Minimum Reprojection Geometry")
    for sat in sats:
        sub = tests[(tests["satellite"] == sat) & (tests["target"] == "latitude_longitude")]
        can = sub.iloc[0]["can_compute"] if len(sub) else "NO"
        lines.append(f"- {sat}: {'has' if can == 'YES' else 'does not yet have'} minimum lat/lon capability for reprojection ({can}).")
    lines.append("")
    lines.append("## 6. Khlopenkov-Style Rating Minimum")
    for sat in sats:
        sub = tests[tests["satellite"] == sat]
        ok = all((sub[sub["target"] == t]["can_compute"].iloc[0] == "YES") if len(sub[sub["target"] == t]) else False for t in ["SZA", "VZA", "RAA"])
        q = any(r["satellite"] == sat and r["quality_capability"] == "quality_flag" and r["status"] in {STATUS_DIRECT, STATUS_DERIVED, STATUS_UNKNOWN} for r in quality_rows)
        lines.append(f"- {sat}: rating minimum {'likely available' if ok and q else 'not complete'}; geometry_ok={ok}; quality_flag_or_candidate={q}.")
    lines.append("")
    lines.append("## 7-8. Can v0 Reprojection Start?")
    v0 = []
    temporary = []
    for sat in sats:
        csub = cloud[cloud["satellite"] == sat].set_index("cloud_variable")
        has_mask = "cloud_mask" in csub.index and csub.loc["cloud_mask", "status"] == STATUS_DIRECT
        has_cth = "cloud_top_height" in csub.index and csub.loc["cloud_top_height", "status"] == STATUS_DIRECT
        latlon = tests[(tests["satellite"] == sat) & (tests["target"] == "latitude_longitude")]
        geo_ok = len(latlon) and latlon.iloc[0]["can_compute"] == "YES"
        if has_mask and has_cth and geo_ok:
            v0.append(sat)
        if sat.startswith("Meteosat"):
            temporary.append(f"{sat}: CLM 3712x3712 and CTH 1237x1237 require explicit native-grid-to-target resampling policy.")
        if sat == "FY4B":
            temporary.append("FY4B: requires matching GEO/navigation convention confirmation before production reprojection.")
    lines.append(f"- Can start v0 reprojection prototype for: {', '.join(v0) if v0 else 'none'}")
    lines.append("- Temporary assumptions: " + " ".join(temporary))
    lines.append("")
    lines.append("## 9-10. Minimal Downloads And Named Products")
    lines.append("- See `recommended_minimal_download_list.csv/md` for the ranked list.")
    lines.append("- FY4B GEO: needed if no direct/derivable FY4B lat/lon is confirmed for the selected L2 grid.")
    lines.append("- GOES CTP/CTT/ACTPF: still needed for v1 cloud-top pressure/temperature/phase unless ACHAF bundled variables are confirmed sufficient.")
    lines.append("- Meteosat CTTH/OCA/CMIC/CT: CTTH is needed for CTT/CTP; OCA/CMIC for optical/microphysical variables; CT for cloud type/phase.")
    lines.append("")
    lines.append("## Key Output Files")
    for name in ["product_variable_inventory_full.csv", "geometry_capability_matrix.csv", "quality_flag_capability_matrix.csv", "cloud_variable_capability_matrix.csv", "derived_geometry_test_results.csv", "recommended_minimal_download_list.csv"]:
        lines.append(f"- `{OUT_DIR / name}`")
    return "\n".join(lines)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log("Scanning existing file inventory")
    files = inventory_files()
    write_csv(OUT_DIR / "scanned_file_groups.csv", files.to_dict("records"))
    samples = choose_samples(files)
    write_csv(OUT_DIR / "sampled_files.csv", samples.to_dict("records"))
    log(f"Inspecting {len(samples)} sampled files across {samples.groupby(['satellite_family','satellite','product']).ngroups if len(samples) else 0} product groups")
    inv_rows: list[dict[str, Any]] = []
    for i, row in enumerate(samples.to_dict("records"), 1):
        log(f"{i}/{len(samples)} {row.get('satellite')} {row.get('product')} {row.get('sample_position')}: {row.get('file_path')}")
        try:
            inv_rows.extend(inspect_file(row))
        except Exception as exc:
            inv_rows.append(common_row(row, "read_error", "/", Path(str(row.get("file_path", ""))).name, (), "error", {"description": f"{type(exc).__name__}: {exc}"}))
    write_csv(OUT_DIR / "product_variable_inventory_full.csv", inv_rows)
    inv_df = pd.DataFrame(inv_rows)
    geom_rows, quality_rows, cloud_rows = summarize_capabilities(inv_df, files)
    test_rows = derived_geometry_tests(geom_rows, inv_df)
    rec_rows = recommended_downloads(geom_rows, cloud_rows, quality_rows)
    write_csv(OUT_DIR / "geometry_capability_matrix.csv", geom_rows)
    write_csv(OUT_DIR / "quality_flag_capability_matrix.csv", quality_rows)
    write_csv(OUT_DIR / "cloud_variable_capability_matrix.csv", cloud_rows)
    write_csv(OUT_DIR / "derived_geometry_test_results.csv", test_rows)
    write_csv(OUT_DIR / "recommended_minimal_download_list.csv", sorted(rec_rows, key=lambda r: (r["priority"], r["satellite"], r["recommended_item"])))
    make_reports(inv_rows, geom_rows, quality_rows, cloud_rows, test_rows, rec_rows)
    log(f"Finished. Output: {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
