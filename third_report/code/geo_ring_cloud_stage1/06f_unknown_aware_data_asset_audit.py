from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import math
import re
import sqlite3
import sys
import traceback
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import h5py
import netCDF4
import numpy as np
import pandas as pd
import yaml
from satpy.readers.ahi_hsd import AHIHSDFileHandler
from satpy.readers.seviri_l1b_native import NativeMSGFileHandler


WORK_ROOT = Path(r"D:\AAAresearch_paper\third_report")
CODE_ROOT = WORK_ROOT / "code" / "geo_ring_cloud_stage1"
STAGE_ROOT = Path(r"D:\AAAresearch_paper\geo_ring_cloud_stage1")
REPORT_DIR = STAGE_ROOT / "reports"
OUT_DIR = STAGE_ROOT / "data_asset_audit_06f"
EXPORT_DIR = OUT_DIR / "exports"
PARQUET_DIR = OUT_DIR / "parquet"

INPUT_DIRS = [
    Path(r"D:\AAAresearch_paper\data"),
    Path(r"D:\AAAresearch_paper\geo_geometry_check"),
    Path(r"D:\AAAresearch_paper\geo_ring_cloud_stage1\standardized_native"),
    Path(r"D:\AAAresearch_paper\geo_ring_cloud_stage1\reprojected_grid"),
    Path(r"D:\AAAresearch_paper\geo_ring_cloud_stage1\fused_best_source"),
]

SQLITE_PATH = OUT_DIR / "data_asset_audit.sqlite"
SUMMARY_JSON = OUT_DIR / "audit_summary.json"
REPORT_MD = REPORT_DIR / "06f_unknown_aware_data_asset_audit_report.md"

HIGH_UNKNOWNS_CSV = EXPORT_DIR / "high_priority_unknowns.csv"
HIGH_UNKNOWNS_RAW_CSV = EXPORT_DIR / "high_priority_unknowns_raw.csv"
CAPABILITY_CSV = EXPORT_DIR / "product_capability_matrix.csv"
BLOCKING_CSV = EXPORT_DIR / "blocking_issues.csv"
RECOMMEND_CSV = EXPORT_DIR / "recommendation_matrix.csv"

SUPPORTED_SUFFIXES = {
    ".nc",
    ".hdf",
    ".h5",
    ".hdf5",
    ".npz",
    ".json",
    ".csv",
    ".yaml",
    ".yml",
    ".txt",
    ".md",
    ".xml",
    ".nat",
    ".zip",
    ".bz2",
}

MAX_TEXT_CHARS = 2000
MAX_SAMPLE_POINTS = 4096
MAX_CATEGORY_UNIQUE = 256
MAX_ATTR_TEXT = 4000
MAX_ERROR_TEXT = 2000
COMMIT_EVERY = 25


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_module(module_path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


STAGE1_COMMON = load_module(CODE_ROOT / "stage1_common.py", "stage1_common_06f")


def safe_json(value: Any) -> str:
    def _default(obj: Any) -> Any:
        if isinstance(obj, (np.generic,)):
            return obj.item()
        if isinstance(obj, Path):
            return str(obj)
        if isinstance(obj, bytes):
            return obj.decode("utf-8", errors="ignore")
        if hasattr(obj, "tolist"):
            return obj.tolist()
        return str(obj)

    return json.dumps(value, ensure_ascii=False, default=_default)


def truncate_text(text: Any, limit: int = MAX_ATTR_TEXT) -> str:
    value = "" if text is None else str(text)
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def normalize_name(value: str) -> str:
    return STAGE1_COMMON.normalize_name(value)


def attr_to_python(value: Any) -> Any:
    return STAGE1_COMMON.attr_to_python(value)


def attrs_to_dict(attrs: Any) -> dict[str, Any]:
    return STAGE1_COMMON.attrs_to_dict(attrs)


def parse_time_from_any(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    ts = pd.to_datetime(text, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_lon(lon: float | np.ndarray) -> float | np.ndarray:
    return ((np.asarray(lon) + 180.0) % 360.0) - 180.0


def is_supported_file(path: Path) -> bool:
    suffixes = [s.lower() for s in path.suffixes]
    if not suffixes:
        return False
    if suffixes[-1] in SUPPORTED_SUFFIXES:
        return True
    if "".join(suffixes[-2:]) == ".dat.bz2":
        return True
    return False


def file_format(path: Path) -> str:
    suffixes = [s.lower() for s in path.suffixes]
    if "".join(suffixes[-2:]) == ".dat.bz2":
        return "HSD_BZ2"
    if suffixes[-1] in {".nc"}:
        return "NETCDF"
    if suffixes[-1] in {".hdf", ".h5", ".hdf5"}:
        return "HDF5"
    if suffixes[-1] == ".npz":
        return "NPZ"
    if suffixes[-1] == ".nat":
        return "NAT"
    if suffixes[-1] == ".zip":
        return "ZIP"
    if suffixes[-1] == ".json":
        return "JSON"
    if suffixes[-1] == ".csv":
        return "CSV"
    if suffixes[-1] in {".yaml", ".yml"}:
        return "YAML"
    if suffixes[-1] == ".xml":
        return "XML"
    if suffixes[-1] in {".txt", ".md"}:
        return "TEXT"
    return suffixes[-1].lstrip(".").upper()


def path_source_type(path: Path) -> str:
    p = str(path).lower()
    if "standardized_native" in p:
        return "standardized_native"
    if "reprojected_grid" in p:
        return "reprojected_grid"
    if "fused_best_source" in p:
        return "fused_best_source"
    if "geo_geometry_check" in p:
        return "geometry_sample"
    return "raw_data"


def parse_satellite_sensor_product(path: Path) -> dict[str, Any]:
    name = path.name
    full = str(path)
    sat = sensor = product = level = None

    if "FY4B" in name.upper() or "FY4B" in full.upper():
        sat = "FY4B"
        sensor = "AGRI"
        for prod in ["CLM", "CLP", "CLT", "CTH", "CTP", "CTT", "GEO", "FDI"]:
            if f"_{prod}-_" in name.upper() or f"_{prod}_" in name.upper() or f"-{prod}" in name.upper() or f"\\FY4B-{prod}" in full.upper():
                product = prod
                break
    elif "GOES-16" in full or "_G16_" in name.upper():
        sat = "GOES-16"
        sensor = "ABI"
        for prod in ["ACMF", "ACHAF", "ACHTF", "CTPF", "ACTPF", "CODF", "CPSF", "RADF"]:
            if prod in name.upper():
                product = prod
                break
    elif "GOES-18" in full or "_G18_" in name.upper():
        sat = "GOES-18"
        sensor = "ABI"
        for prod in ["ACMF", "ACHAF", "ACHTF", "CTPF", "ACTPF", "CODF", "CPSF", "RADF"]:
            if prod in name.upper():
                product = prod
                break
    elif "HIMAWARI-9" in full.upper() or "H09" in name.upper():
        sat = "Himawari-9"
        sensor = "AHI"
        for prod in ["CMSK", "CHGT", "FLDK"]:
            if prod in name.upper():
                product = prod
                break
        if product == "FLDK":
            product = "L1B_FLDK"
    elif "MSG2" in name.upper():
        sat = "Meteosat-IODC"
        sensor = "SEVIRI"
        product = "NATIVE"
    elif "MSG3" in name.upper():
        sat = "Meteosat-0deg"
        sensor = "SEVIRI"
        product = "NATIVE"
    elif "METEOSAT-0DEG" in full.upper():
        sat = "Meteosat-0deg"
        sensor = "SEVIRI"
    elif "METEOSAT-IODC" in full.upper():
        sat = "Meteosat-IODC"
        sensor = "SEVIRI"

    level_match = re.search(r"_L([12](?:\.\d+)?)", name.upper())
    if level_match:
        level = "L" + level_match.group(1)
    elif "L2" in name.upper():
        level = "L2"
    elif "L1B" in name.upper():
        level = "L1B"
    elif product == "NATIVE":
        level = "L1.5"

    # Standardized / reprojected / fused names
    if sat is None and path.suffix.lower() == ".npz":
        parts = path.stem.split("_")
        if parts:
            if parts[0].startswith("FY4B"):
                sat = "FY4B"
                sensor = "AGRI"
            elif parts[0].startswith("GOES-16"):
                sat = "GOES-16"
                sensor = "ABI"
            elif parts[0].startswith("GOES-18"):
                sat = "GOES-18"
                sensor = "ABI"
            elif parts[0].startswith("Himawari-9"):
                sat = "Himawari-9"
                sensor = "AHI"
            elif parts[0].startswith("Meteosat-0deg"):
                sat = "Meteosat-0deg"
                sensor = "SEVIRI"
            elif parts[0].startswith("Meteosat-IODC"):
                sat = "Meteosat-IODC"
                sensor = "SEVIRI"
        if len(parts) >= 2 and product is None:
            product = parts[1]
    return {
        "satellite": sat,
        "sensor": sensor,
        "product": product,
        "level": level,
    }


def parse_times_from_name(path: Path) -> dict[str, str | None]:
    name = path.name
    times = re.findall(r"(\d{14})", name)
    nominal = start = end = None
    if len(times) >= 1:
        nominal = parse_time_from_any(times[0])
    if len(times) >= 2:
        start = parse_time_from_any(times[0])
        end = parse_time_from_any(times[1])
    if len(times) >= 3:
        nominal = parse_time_from_any(times[0])
        start = parse_time_from_any(times[1])
        end = parse_time_from_any(times[2])

    goes = re.search(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})_e(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})", name)
    if goes:
        start = datetime.strptime(
            f"{goes.group(1)} {goes.group(2)} {goes.group(3)}:{goes.group(4)}:{goes.group(5)}",
            "%Y %j %H:%M:%S",
        ).replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = datetime.strptime(
            f"{goes.group(6)} {goes.group(7)} {goes.group(8)}:{goes.group(9)}:{goes.group(10)}",
            "%Y %j %H:%M:%S",
        ).replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        nominal = start

    return {"nominal_time": nominal, "start_time": start, "end_time": end}


def path_checksum(path: Path) -> str:
    token = f"{path}|{path.stat().st_size}|{int(path.stat().st_mtime)}"
    return hashlib.sha1(token.encode("utf-8")).hexdigest()


def sample_indices(length: int, max_points: int = 16) -> np.ndarray:
    if length <= max_points:
        return np.arange(length, dtype=int)
    return np.unique(np.linspace(0, length - 1, max_points, dtype=int))


def sample_numeric_array(arr: np.ndarray) -> tuple[np.ndarray, str, int]:
    a = np.asarray(arr)
    if a.ndim == 0:
        return a.reshape(1), "scalar", 1
    if a.size <= MAX_SAMPLE_POINTS:
        return a.reshape(-1), "full", int(a.size)
    if a.ndim == 1:
        idx = sample_indices(a.shape[0], max_points=min(MAX_SAMPLE_POINTS, 1024))
        return a[idx].reshape(-1), "indexed_1d", int(idx.size)
    slices = []
    sample_count = 1
    for dim in a.shape:
        count = min(8, dim)
        sample_count *= count
        slices.append(sample_indices(dim, count))
    mesh = np.ix_(*slices)
    sampled = a[mesh].reshape(-1)
    return sampled, f"mesh_{'x'.join(str(len(s)) for s in slices)}", int(sampled.size)


def profile_array(arr: np.ndarray, attrs: dict[str, Any] | None = None) -> dict[str, Any]:
    attrs = attrs or {}
    a = np.asarray(arr)
    sampled, sample_method, sample_size = sample_numeric_array(a)
    sampled = np.asarray(sampled)
    profile: dict[str, Any] = {
        "sample_method": sample_method,
        "sample_size": sample_size,
        "unique_count": None,
        "sample_values_json": safe_json([]),
        "suspected_fill_values_json": safe_json([]),
        "valid_range_json": safe_json([attrs.get("valid_min"), attrs.get("valid_max")]),
        "scale_factor": attrs.get("scale_factor"),
        "add_offset": attrs.get("add_offset"),
        "min": None,
        "max": None,
        "mean": None,
        "std": None,
        "finite_ratio": None,
        "nan_ratio": None,
    }
    if sampled.size == 0:
        return profile

    if sampled.dtype.kind in "iu":
        values, counts = np.unique(sampled, return_counts=True)
        if len(values) <= MAX_CATEGORY_UNIQUE:
            profile["unique_count"] = int(len(values))
            profile["sample_values_json"] = safe_json(
                [{"value": attr_to_python(v), "count": int(c)} for v, c in zip(values, counts)]
            )
        profile["min"] = float(values.min())
        profile["max"] = float(values.max())
        profile["mean"] = float(sampled.astype(np.float64).mean())
        profile["std"] = float(sampled.astype(np.float64).std())
        profile["finite_ratio"] = 1.0
        profile["nan_ratio"] = 0.0
    elif sampled.dtype.kind in "f":
        finite = np.isfinite(sampled)
        finite_vals = sampled[finite]
        profile["finite_ratio"] = float(finite.mean())
        profile["nan_ratio"] = float((~finite).mean())
        if finite_vals.size:
            profile["min"] = float(np.min(finite_vals))
            profile["max"] = float(np.max(finite_vals))
            profile["mean"] = float(np.mean(finite_vals))
            profile["std"] = float(np.std(finite_vals))
            if finite_vals.size <= MAX_CATEGORY_UNIQUE:
                vals, counts = np.unique(np.round(finite_vals, 6), return_counts=True)
                profile["unique_count"] = int(len(vals))
                profile["sample_values_json"] = safe_json(
                    [{"value": float(v), "count": int(c)} for v, c in zip(vals[:64], counts[:64])]
                )
            else:
                profile["sample_values_json"] = safe_json([float(v) for v in finite_vals[:32]])
        fill_candidates = []
        for key in ["_FillValue", "missing_value"]:
            if key in attrs:
                fill_candidates.append(attr_to_python(attrs[key]))
        profile["suspected_fill_values_json"] = safe_json(fill_candidates)
    else:
        values = [str(v) for v in sampled.reshape(-1)[:32]]
        profile["sample_values_json"] = safe_json(values)
    return profile


def semantic_classify(name: str, units: str, attrs: dict[str, Any], group_path: str = "") -> tuple[list[str], float, str]:
    n = normalize_name(name)
    u = normalize_name(units)
    labels: list[str] = []
    evidence: list[str] = []
    text_blob = " ".join(
        [
            name,
            group_path,
            str(attrs.get("long_name", "")),
            str(attrs.get("standard_name", "")),
            str(attrs.get("description", "")),
            str(attrs.get("grid_mapping_name", "")),
        ]
    ).lower()
    if any(k in n for k in ["cloudmask", "cloudmaskbinary", "acm", "bcm", "cbm", "clm", "cmsk"]) or "cloud mask" in text_blob:
        labels.append("cloud_mask_or_class")
        evidence.append("name/text matched cloud mask semantics")
    if any(k in n for k in ["cloudtype", "cloud_type", "clt", "ct", "phase", "clp", "actpf"]):
        labels.append("cloud_mask_or_class")
        evidence.append("name matched cloud type/phase semantics")
    if any(k in n for k in ["cth", "cldtophght", "cloudtopheight", "cloudtophght", "ctoph", "heightkm", "achaf", "ht"]) or "cloud top height" in text_blob:
        labels.append("cloud_physical")
        evidence.append("name/text matched cloud top height")
    if any(k in n for k in ["ctt", "cloudtoptemp", "cldtoptemp", "temperaturek", "achtf"]):
        labels.append("cloud_physical")
        evidence.append("name/text matched cloud top temperature")
    if any(k in n for k in ["ctp", "cloudtoppres", "cldtoppres", "pressurehpa", "ctpf"]):
        labels.append("cloud_physical")
        evidence.append("name/text matched cloud top pressure")
    if any(k in n for k in ["cod", "cot", "optdpth", "opticalthickness"]) or "optical depth" in text_blob:
        labels.append("cloud_physical")
        evidence.append("name/text matched cloud optical thickness")
    if any(k in n for k in ["cps", "cer", "effectiveradius"]):
        labels.append("cloud_physical")
        evidence.append("name/text matched cloud effective radius")
    if any(k in n for k in ["dqf", "quality", "flag", "status", "mask", "condition", "code"]):
        labels.append("quality_flag")
        evidence.append("name matched flag/quality pattern")
    if any(k in n for k in ["lat", "latitude", "lon", "longitude", "projectionx", "projectiony", "linenumber", "columnnumber"]):
        labels.append("coordinate")
        evidence.append("name matched coordinate pattern")
    if any(k in n for k in ["projection", "gridmapping", "cfac", "lfac", "coff", "loff", "sweep", "ssp"]):
        labels.append("navigation_projection")
        evidence.append("name/text matched projection/navigation pattern")
    if any(k in n for k in ["zenith", "azimuth", "glint", "vza", "vaa", "sza", "saa", "raa", "soz", "soa", "saz"]):
        labels.append("geometry_angle")
        evidence.append("name matched angle pattern")
    if any(k in n for k in ["time", "scan", "start", "end", "nominal", "observation"]):
        labels.append("time_scan")
        evidence.append("name matched time/scan pattern")
    if any(k in n for k in ["rad", "radiance", "bt", "brightnesstemperature", "tbb", "albedo", "nomchannel", "calchannel"]):
        labels.append("radiance_or_BT")
        evidence.append("name matched radiance/BT pattern")
    if any(k in n for k in ["algorithm", "processing", "retrieval"]):
        labels.append("algorithm_status")
        evidence.append("name/text matched algorithm status pattern")
    if any(k in n for k in ["calibration", "cal", "gain", "offset"]):
        labels.append("calibration_status")
        evidence.append("name/text matched calibration pattern")
    if any(k in n for k in ["source", "history", "institution", "project", "summary", "keyword", "metadata"]):
        labels.append("lineage_metadata")
        evidence.append("name/text matched lineage metadata pattern")
    if n == "obitype" or "observing type" in text_blob:
        labels.append("lineage_metadata")
        evidence.append("FY4B docs: OBIType = Observing Type metadata")
    if not labels:
        labels.append("unknown_candidate")
        evidence.append("no semantic rule matched")
    labels = sorted(set(labels))
    confidence = 0.9 if labels != ["unknown_candidate"] else 0.2
    return labels, confidence, "; ".join(evidence[:4])


def known_status_for(item_type: str, labels: list[str], profile: dict[str, Any], attrs: dict[str, Any], name: str) -> str:
    if item_type in {"metadata_item", "global_attribute_item", "group_attribute_item", "csv_column", "json_key", "yaml_key", "xml_element"}:
        return "unknown_metadata_only" if labels == ["unknown_candidate"] else "known_uninterpreted"
    if "reader_internal" in labels:
        return "reader_internal"
    if labels == ["unknown_candidate"]:
        if any(k in normalize_name(name) for k in ["dqf", "quality", "flag", "status", "mask", "code"]):
            return "unknown_flag_like"
        if profile.get("unique_count") is not None and profile["unique_count"] <= MAX_CATEGORY_UNIQUE:
            return "unknown_numeric"
        return "unknown_named"
    if "coordinate" in labels or "geometry_angle" in labels or "radiance_or_BT" in labels:
        return "known_interpreted"
    if "lineage_metadata" in labels:
        return "known_uninterpreted"
    if "quality_flag" in labels and not (attrs.get("flag_meanings") or attrs.get("flag_values") or attrs.get("flag_masks")):
        return "known_uninterpreted"
    return "known_interpreted"


def unknown_assessment(
    item_id: int,
    file_row: dict[str, Any],
    item_row: dict[str, Any],
    profile: dict[str, Any] | None,
    attrs: dict[str, Any],
) -> dict[str, Any] | None:
    known_status = item_row["known_status"]
    if known_status.startswith("known") and item_row["semantic_class"] != "unknown_candidate":
        return None

    risk = 0
    reasons: list[str] = []
    shape = json.loads(item_row["shape_json"]) if item_row["shape_json"] else []
    if len(shape) >= 2:
        risk += 1
        reasons.append("ndim>=2")
    if profile:
        uniq = profile.get("unique_count")
        if uniq is not None and uniq <= 32:
            risk += 2
            reasons.append("small_unique_count")
        sample_vals = item_row.get("dtype", "")
        if sample_vals and any(k in sample_vals for k in ["int", "uint"]):
            risk += 1
            reasons.append("integer_dtype")
    norm_name = item_row["normalized_name"]
    if any(k in norm_name for k in ["qa", "dqf", "quality", "flag", "status", "condition", "mask", "class", "type", "algorithm"]):
        risk += 2
        reasons.append("flag_like_name")
    units = normalize_name(item_row.get("units", ""))
    if any(k in units for k in ["degree", "percent", "pressure", "temperature", "height", "distance"]):
        risk += 1
        reasons.append("physical_units")
    if any(k in attrs for k in ["valid_range", "_FillValue", "missing_value", "description", "grid_mapping", "coordinates"]):
        risk += 1
        reasons.append("informative_attrs")

    possible_meaning = item_row["semantic_class"]
    if "quality_flag" in item_row["semantic_class"]:
        potential_use = "screening_or_rating"
    elif "geometry_angle" in item_row["semantic_class"]:
        potential_use = "07_stratification_or_geometry"
    elif "coordinate" in item_row["semantic_class"] or "navigation_projection" in item_row["semantic_class"]:
        potential_use = "reprojection_or_geometry"
    elif "cloud_physical" in item_row["semantic_class"] or "cloud_mask_or_class" in item_row["semantic_class"]:
        potential_use = "fusion_or_overlap"
    else:
        potential_use = "manual_review"

    blocks_07 = 1 if potential_use in {"reprojection_or_geometry", "fusion_or_overlap"} and risk >= 3 else 0
    affects_rating = 1 if potential_use in {"screening_or_rating", "07_stratification_or_geometry"} else 0
    priority = "HIGH" if blocks_07 or risk >= 4 else ("MEDIUM" if risk >= 2 else "LOW")
    return {
        "item_id": item_id,
        "priority": priority,
        "risk_score": risk,
        "why_flagged": "; ".join(reasons),
        "possible_meaning": possible_meaning,
        "potential_use": potential_use,
        "risk_if_ignored": "may hide geometry/quality/fusion-relevant information" if risk >= 3 else "diagnostic information may be underused",
        "recommended_manual_check": 1,
        "blocks_07": blocks_07,
        "affects_future_rating": affects_rating,
    }


def recommendations_for_item(
    item_id: int,
    item_row: dict[str, Any],
    unknown_row: dict[str, Any] | None,
) -> dict[str, Any]:
    labels = item_row["semantic_class"].split("|") if item_row["semantic_class"] else []
    use_now = use_later = do_not_use = 0
    use_for_fusion = use_for_rating = use_for_screening = use_for_07 = use_for_deep = 0
    confidence = item_row["semantic_confidence"]
    reason = "semantic classification"
    blocking = 0
    if "cloud_mask_or_class" in labels or "cloud_physical" in labels:
        use_now = 1
        use_for_fusion = 1
        use_for_07 = 1
    elif "geometry_angle" in labels or "coordinate" in labels or "navigation_projection" in labels or "time_scan" in labels:
        use_now = 1
        use_for_07 = 1
        use_for_screening = 1
    elif "quality_flag" in labels:
        use_now = 1
        use_for_screening = 1
        use_for_rating = 1
    elif "radiance_or_BT" in labels:
        use_later = 1
        use_for_deep = 1
    elif "lineage_metadata" in labels or "algorithm_status" in labels or "calibration_status" in labels:
        use_later = 1
    else:
        do_not_use = 1
        reason = "unknown or metadata-only item"
    if unknown_row is not None:
        if unknown_row["blocks_07"]:
            blocking = 1
            reason += "; unknown blocks_07"
        if unknown_row["priority"] == "HIGH":
            use_later = 1
            do_not_use = 0
    return {
        "item_id": item_id,
        "use_now": use_now,
        "use_later": use_later,
        "do_not_use": do_not_use,
        "use_for_fusion": use_for_fusion,
        "use_for_rating": use_for_rating,
        "use_for_screening": use_for_screening,
        "use_for_07_stratification": use_for_07,
        "use_for_future_deep_space_enhancement": use_for_deep,
        "reason": reason,
        "confidence": confidence,
        "blocking_issue": blocking,
    }


def flag_record_from_item(
    file_id: int,
    item_id: int,
    item_row: dict[str, Any],
    attrs: dict[str, Any],
    profile: dict[str, Any] | None,
) -> dict[str, Any] | None:
    name = item_row["name"]
    text_blob = " ".join([normalize_name(name), normalize_name(item_row["description"]), normalize_name(item_row["units"])])
    if not any(k in text_blob for k in ["dqf", "quality", "flag", "mask", "status", "code", "class", "type"]):
        return None
    flag_values = attrs.get("flag_values")
    flag_masks = attrs.get("flag_masks")
    flag_meanings = attrs.get("flag_meanings")
    fill_value = attrs.get("_FillValue", attrs.get("missing_value"))
    unique_count = profile.get("unique_count") if profile else None
    is_enum = 1 if flag_values or (unique_count is not None and unique_count <= 32) else 0
    is_bitfield = 1 if flag_masks else 0
    requires_manual = 1 if not (flag_meanings or flag_values or flag_masks) else 0
    return {
        "file_id": file_id,
        "item_id": item_id,
        "flag_values": truncate_text(safe_json(attr_to_python(flag_values)), 1000) if flag_values is not None else "",
        "flag_masks": truncate_text(safe_json(attr_to_python(flag_masks)), 1000) if flag_masks is not None else "",
        "flag_meanings": truncate_text(str(flag_meanings or ""), 1000),
        "bit_positions": "",
        "fill_value": truncate_text(str(fill_value), 200),
        "valid_range": truncate_text(safe_json([attrs.get("valid_min"), attrs.get("valid_max")]), 200),
        "is_enum": is_enum,
        "is_bitfield": is_bitfield,
        "valid_for_display": 1 if "cloud_mask_or_class" in item_row["semantic_class"] or "quality_flag" in item_row["semantic_class"] else 0,
        "valid_for_fusion": 1 if "cloud_mask_or_class" in item_row["semantic_class"] and requires_manual == 0 else 0,
        "valid_for_screening": 1 if "quality_flag" in item_row["semantic_class"] else 0,
        "valid_for_rating": 1 if "quality_flag" in item_row["semantic_class"] and requires_manual == 0 else 0,
        "valid_for_07_stratification": 1 if "geometry_angle" in item_row["semantic_class"] else 0,
        "requires_manual_interpretation": requires_manual,
    }


def geo_time_from_item(file_id: int, item_id: int, file_row: dict[str, Any], item_row: dict[str, Any], attrs: dict[str, Any]) -> dict[str, Any] | None:
    labels = item_row["semantic_class"].split("|") if item_row["semantic_class"] else []
    if not set(labels) & {"navigation_projection", "coordinate", "geometry_angle", "time_scan"}:
        return None
    return {
        "file_id": file_id,
        "item_id": item_id,
        "projection_name": truncate_text(str(attrs.get("grid_mapping_name", attrs.get("grid_mapping", ""))), 200),
        "grid_mapping": truncate_text(str(attrs.get("grid_mapping", "")), 200),
        "x_y_name": item_row["name"] if item_row["normalized_name"] in {"x", "y", "projectionx", "projectiony"} else "",
        "lat_lon_name": item_row["name"] if item_row["normalized_name"] in {"lat", "latitude", "lon", "longitude"} else "",
        "area_extent": "",
        "native_shape": item_row["shape_json"],
        "resolution": truncate_text(str(attrs.get("resolution", attrs.get("spatial_resolution", ""))), 100),
        "subpoint_lon": attrs.get("longitude_of_projection_origin") or attrs.get("sub_lon") or attrs.get("ssp_longitude"),
        "subpoint_lat": attrs.get("latitude_of_projection_origin") or attrs.get("ssp_latitude"),
        "satellite_height_radius": attrs.get("perspective_point_height") or attrs.get("distance_earth_center_to_satellite") or attrs.get("h"),
        "semi_major_axis": attrs.get("semi_major_axis") or attrs.get("a") or attrs.get("earth_equatorial_radius"),
        "semi_minor_axis": attrs.get("semi_minor_axis") or attrs.get("b") or attrs.get("earth_polar_radius"),
        "sweep_axis": attrs.get("sweep_angle_axis", ""),
        "cfac_lfac_coff_loff": safe_json(
            {
                "CFAC": attrs.get("CFAC"),
                "LFAC": attrs.get("LFAC"),
                "COFF": attrs.get("COFF"),
                "LOFF": attrs.get("LOFF"),
            }
        ),
        "segment_index_count": safe_json(
            {
                "segment_index": attrs.get("segment_index"),
                "segment_count": attrs.get("segment_count"),
                "full_disk_complete": attrs.get("full_disk_complete"),
            }
        ),
        "angle_kind": (
            "official_angle"
            if any(k in item_row["normalized_name"] for k in ["nomsatellitezenith", "nomsunzenith", "nomsunazimuth", "satellitezenith", "solarzenith"])
            else ("computed_angle" if "reprojected" in file_row["source_type"] or "fused" in file_row["source_type"] else "approximate_angle")
        ),
        "azimuth_convention_flag": "AZIMUTH_CONVENTION_UNKNOWN" if "azimuth" in item_row["normalized_name"] else "",
        "nominal_time": file_row["nominal_time"],
        "observation_start": file_row["start_time"],
        "observation_end": file_row["end_time"],
        "scan_start": parse_time_from_any(attrs.get("time_coverage_start")),
        "scan_end": parse_time_from_any(attrs.get("time_coverage_end")),
        "segment_time": parse_time_from_any(attrs.get("segment_time")),
        "scan_duration": "",
        "target_time_difference_minutes": "",
    }


def insert_many(conn: sqlite3.Connection, table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    conn.executemany(sql, [[row.get(c) for c in cols] for row in rows])


def setup_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE IF NOT EXISTS files (
            file_id INTEGER PRIMARY KEY,
            satellite TEXT,
            sensor TEXT,
            product TEXT,
            level TEXT,
            source_type TEXT,
            nominal_time TEXT,
            start_time TEXT,
            end_time TEXT,
            path TEXT UNIQUE,
            format TEXT,
            size_bytes INTEGER,
            reader TEXT,
            read_status TEXT,
            error_message TEXT,
            checksum_optional TEXT
        );
        CREATE TABLE IF NOT EXISTS items (
            item_id INTEGER PRIMARY KEY,
            file_id INTEGER,
            item_type TEXT,
            group_path TEXT,
            name TEXT,
            normalized_name TEXT,
            shape_json TEXT,
            dtype TEXT,
            dimensions_json TEXT,
            units TEXT,
            long_name TEXT,
            standard_name TEXT,
            description TEXT,
            semantic_class TEXT,
            semantic_confidence REAL,
            known_status TEXT,
            manual_review_priority TEXT,
            notes TEXT
        );
        CREATE TABLE IF NOT EXISTS attributes (
            attr_id INTEGER PRIMARY KEY,
            file_id INTEGER,
            item_id_nullable INTEGER,
            attr_scope TEXT,
            key TEXT,
            value_text TEXT,
            value_json TEXT,
            value_type TEXT
        );
        CREATE TABLE IF NOT EXISTS profiles (
            profile_id INTEGER PRIMARY KEY,
            item_id INTEGER,
            min REAL,
            max REAL,
            mean REAL,
            std REAL,
            finite_ratio REAL,
            nan_ratio REAL,
            unique_count INTEGER,
            sample_values_json TEXT,
            suspected_fill_values_json TEXT,
            valid_range_json TEXT,
            scale_factor TEXT,
            add_offset TEXT,
            sample_method TEXT,
            sample_size INTEGER
        );
        CREATE TABLE IF NOT EXISTS flags (
            flag_id INTEGER PRIMARY KEY,
            file_id INTEGER,
            item_id INTEGER,
            flag_values TEXT,
            flag_masks TEXT,
            flag_meanings TEXT,
            bit_positions TEXT,
            fill_value TEXT,
            valid_range TEXT,
            is_enum INTEGER,
            is_bitfield INTEGER,
            valid_for_display INTEGER,
            valid_for_fusion INTEGER,
            valid_for_screening INTEGER,
            valid_for_rating INTEGER,
            valid_for_07_stratification INTEGER,
            requires_manual_interpretation INTEGER
        );
        CREATE TABLE IF NOT EXISTS geo_time (
            geo_time_id INTEGER PRIMARY KEY,
            file_id INTEGER,
            item_id INTEGER,
            projection_name TEXT,
            grid_mapping TEXT,
            x_y_name TEXT,
            lat_lon_name TEXT,
            area_extent TEXT,
            native_shape TEXT,
            resolution TEXT,
            subpoint_lon TEXT,
            subpoint_lat TEXT,
            satellite_height_radius TEXT,
            semi_major_axis TEXT,
            semi_minor_axis TEXT,
            sweep_axis TEXT,
            cfac_lfac_coff_loff TEXT,
            segment_index_count TEXT,
            angle_kind TEXT,
            azimuth_convention_flag TEXT,
            nominal_time TEXT,
            observation_start TEXT,
            observation_end TEXT,
            scan_start TEXT,
            scan_end TEXT,
            segment_time TEXT,
            scan_duration TEXT,
            target_time_difference_minutes TEXT
        );
        CREATE TABLE IF NOT EXISTS unknowns (
            unknown_id INTEGER PRIMARY KEY,
            item_id INTEGER,
            priority TEXT,
            risk_score INTEGER,
            why_flagged TEXT,
            possible_meaning TEXT,
            potential_use TEXT,
            risk_if_ignored TEXT,
            recommended_manual_check INTEGER,
            blocks_07 INTEGER,
            affects_future_rating INTEGER
        );
        CREATE TABLE IF NOT EXISTS recommendations (
            recommendation_id INTEGER PRIMARY KEY,
            item_id INTEGER,
            use_now INTEGER,
            use_later INTEGER,
            do_not_use INTEGER,
            use_for_fusion INTEGER,
            use_for_rating INTEGER,
            use_for_screening INTEGER,
            use_for_07_stratification INTEGER,
            use_for_future_deep_space_enhancement INTEGER,
            reason TEXT,
            confidence REAL,
            blocking_issue INTEGER
        );
        CREATE TABLE IF NOT EXISTS audit_gates (
            gate_name TEXT PRIMARY KEY,
            gate_value TEXT,
            rationale TEXT
        );
        """
    )
    conn.commit()


@dataclass
class ItemBundle:
    item_row: dict[str, Any]
    attrs: dict[str, Any]
    profile: dict[str, Any] | None
    flag_row: dict[str, Any] | None
    geo_time_row: dict[str, Any] | None
    unknown_row: dict[str, Any] | None
    recommendation_row: dict[str, Any] | None


class Auditor:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.file_id = 0
        self.item_id = 0
        self.attr_id = 0
        self.profile_id = 0
        self.flag_id = 0
        self.geo_time_id = 0
        self.unknown_id = 0
        self.recommendation_id = 0
        self.file_rows: list[dict[str, Any]] = []
        self.item_rows: list[dict[str, Any]] = []
        self.attr_rows: list[dict[str, Any]] = []
        self.profile_rows: list[dict[str, Any]] = []
        self.flag_rows: list[dict[str, Any]] = []
        self.geo_time_rows: list[dict[str, Any]] = []
        self.unknown_rows: list[dict[str, Any]] = []
        self.recommendation_rows: list[dict[str, Any]] = []
        self.processed = 0

    def flush(self) -> None:
        insert_many(self.conn, "files", self.file_rows)
        insert_many(self.conn, "items", self.item_rows)
        insert_many(self.conn, "attributes", self.attr_rows)
        insert_many(self.conn, "profiles", self.profile_rows)
        insert_many(self.conn, "flags", self.flag_rows)
        insert_many(self.conn, "geo_time", self.geo_time_rows)
        insert_many(self.conn, "unknowns", self.unknown_rows)
        insert_many(self.conn, "recommendations", self.recommendation_rows)
        self.conn.commit()
        self.file_rows.clear()
        self.item_rows.clear()
        self.attr_rows.clear()
        self.profile_rows.clear()
        self.flag_rows.clear()
        self.geo_time_rows.clear()
        self.unknown_rows.clear()
        self.recommendation_rows.clear()

    def next_file_id(self) -> int:
        self.file_id += 1
        return self.file_id

    def next_item_id(self) -> int:
        self.item_id += 1
        return self.item_id

    def record_attr_rows(self, file_id: int, item_id: int | None, scope: str, attrs: dict[str, Any]) -> None:
        for key, value in attrs.items():
            self.attr_id += 1
            self.attr_rows.append(
                {
                    "attr_id": self.attr_id,
                    "file_id": file_id,
                    "item_id_nullable": item_id,
                    "attr_scope": scope,
                    "key": str(key),
                    "value_text": truncate_text(value),
                    "value_json": truncate_text(safe_json(attr_to_python(value))),
                    "value_type": type(value).__name__,
                }
            )

    def add_item_bundle(self, file_id: int, bundle: ItemBundle) -> None:
        item_row = dict(bundle.item_row)
        item_id = item_row["item_id"]
        self.item_rows.append(item_row)
        self.record_attr_rows(file_id, item_id, item_row["item_type"], bundle.attrs)
        if bundle.profile is not None:
            self.profile_id += 1
            row = dict(bundle.profile)
            row["profile_id"] = self.profile_id
            row["item_id"] = item_id
            self.profile_rows.append(row)
        if bundle.flag_row is not None:
            self.flag_id += 1
            row = dict(bundle.flag_row)
            row["flag_id"] = self.flag_id
            self.flag_rows.append(row)
        if bundle.geo_time_row is not None:
            self.geo_time_id += 1
            row = dict(bundle.geo_time_row)
            row["geo_time_id"] = self.geo_time_id
            self.geo_time_rows.append(row)
        if bundle.unknown_row is not None:
            self.unknown_id += 1
            row = dict(bundle.unknown_row)
            row["unknown_id"] = self.unknown_id
            self.unknown_rows.append(row)
        if bundle.recommendation_row is not None:
            self.recommendation_id += 1
            row = dict(bundle.recommendation_row)
            row["recommendation_id"] = self.recommendation_id
            self.recommendation_rows.append(row)

    def process(self, path: Path) -> None:
        file_id = self.next_file_id()
        meta = parse_satellite_sensor_product(path)
        time_meta = parse_times_from_name(path)
        file_row = {
            "file_id": file_id,
            "satellite": meta["satellite"],
            "sensor": meta["sensor"],
            "product": meta["product"],
            "level": meta["level"],
            "source_type": path_source_type(path),
            "nominal_time": time_meta["nominal_time"],
            "start_time": time_meta["start_time"],
            "end_time": time_meta["end_time"],
            "path": str(path),
            "format": file_format(path),
            "size_bytes": path.stat().st_size,
            "reader": "",
            "read_status": "UNREAD",
            "error_message": "",
            "checksum_optional": path_checksum(path),
        }
        try:
            self._process_impl(file_row, path)
        except Exception as exc:
            file_row["read_status"] = "ERROR"
            file_row["error_message"] = truncate_text(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}", MAX_ERROR_TEXT)
        self.file_rows.append(file_row)
        self.processed += 1
        if self.processed % COMMIT_EVERY == 0:
            self.flush()

    def _process_impl(self, file_row: dict[str, Any], path: Path) -> None:
        fmt = file_row["format"]
        if fmt == "NETCDF":
            file_row["reader"] = "netCDF4"
            self.read_netcdf(file_row, path)
        elif fmt == "HDF5":
            file_row["reader"] = "h5py"
            self.read_hdf5(file_row, path)
        elif fmt == "NPZ":
            file_row["reader"] = "numpy"
            self.read_npz(file_row, path)
        elif fmt == "NAT":
            file_row["reader"] = "satpy.seviri_l1b_native"
            self.read_nat(file_row, path)
        elif fmt == "HSD_BZ2":
            file_row["reader"] = "satpy.ahi_hsd"
            self.read_hsd(file_row, path)
        elif fmt == "ZIP":
            file_row["reader"] = "zipfile"
            self.read_zip(file_row, path)
        elif fmt == "JSON":
            file_row["reader"] = "json"
            self.read_json(file_row, path)
        elif fmt == "CSV":
            file_row["reader"] = "csv"
            self.read_csv(file_row, path)
        elif fmt == "YAML":
            file_row["reader"] = "yaml"
            self.read_yaml(file_row, path)
        elif fmt == "XML":
            file_row["reader"] = "ElementTree"
            self.read_xml(file_row, path)
        elif fmt == "TEXT":
            file_row["reader"] = "text"
            self.read_text(file_row, path)
        else:
            file_row["reader"] = "unsupported"
            file_row["read_status"] = "SKIPPED_UNSUPPORTED"
            file_row["error_message"] = "unsupported extension"

    def make_item_bundle(
        self,
        file_id: int,
        file_row: dict[str, Any],
        item_type: str,
        group_path: str,
        name: str,
        shape: list[int] | None,
        dtype: str,
        dimensions: list[str] | None,
        units: str,
        long_name: str,
        standard_name: str,
        description: str,
        attrs: dict[str, Any],
        profile: dict[str, Any] | None,
        notes: str = "",
    ) -> ItemBundle:
        item_id = self.next_item_id()
        labels, confidence, evidence = semantic_classify(name, units, attrs, group_path=group_path)
        item_row = {
            "item_id": item_id,
            "file_id": file_id,
            "item_type": item_type,
            "group_path": group_path,
            "name": name,
            "normalized_name": normalize_name(name),
            "shape_json": safe_json(shape or []),
            "dtype": dtype,
            "dimensions_json": safe_json(dimensions or []),
            "units": truncate_text(units, 200),
            "long_name": truncate_text(long_name, 300),
            "standard_name": truncate_text(standard_name, 300),
            "description": truncate_text(description, 500),
            "semantic_class": "|".join(labels),
            "semantic_confidence": confidence,
            "known_status": "",  # fill below
            "manual_review_priority": "LOW",
            "notes": truncate_text("; ".join(x for x in [evidence, notes] if x), 500),
        }
        item_row["known_status"] = known_status_for(item_type, labels, profile or {}, attrs, name)
        unknown_row = unknown_assessment(item_id, file_row, item_row, profile, attrs)
        if unknown_row is not None:
            item_row["manual_review_priority"] = unknown_row["priority"]
        else:
            item_row["manual_review_priority"] = "LOW" if item_row["known_status"] == "known_interpreted" else "MEDIUM"
        flag_row = flag_record_from_item(file_id, item_id, item_row, attrs, profile)
        geo_row = geo_time_from_item(file_id, item_id, file_row, item_row, attrs)
        rec_row = recommendations_for_item(item_id, item_row, unknown_row)
        return ItemBundle(item_row=item_row, attrs=attrs, profile=profile, flag_row=flag_row, geo_time_row=geo_row, unknown_row=unknown_row, recommendation_row=rec_row)

    def read_netcdf(self, file_row: dict[str, Any], path: Path) -> None:
        file_id = file_row["file_id"]
        ds = netCDF4.Dataset(path)
        try:
            global_attrs = {k: attr_to_python(getattr(ds, k)) for k in ds.ncattrs()}
            self.record_attr_rows(file_id, None, "global", global_attrs)
            for dim_name, dim in ds.dimensions.items():
                bundle = self.make_item_bundle(
                    file_id,
                    file_row,
                    "dimension",
                    "/",
                    dim_name,
                    [len(dim)] if not dim.isunlimited() else [],
                    "dimension",
                    [],
                    "",
                    "",
                    "",
                    "netCDF dimension",
                    {"isunlimited": dim.isunlimited()},
                    None,
                )
                self.add_item_bundle(file_id, bundle)

            def walk(group: Any, prefix: str = "/") -> None:
                group_attrs = attrs_to_dict(group.__dict__)
                if prefix != "/":
                    group_bundle = self.make_item_bundle(
                        file_id,
                        file_row,
                        "group",
                        prefix,
                        prefix.split("/")[-2] if prefix.endswith("/") else prefix.split("/")[-1],
                        [],
                        "group",
                        [],
                        "",
                        "",
                        "",
                        "netCDF group",
                        group_attrs,
                        None,
                    )
                    self.add_item_bundle(file_id, group_bundle)
                if group_attrs:
                    self.record_attr_rows(file_id, None, f"group:{prefix}", group_attrs)
                for name, var in group.variables.items():
                    attrs = {k: attr_to_python(getattr(var, k)) for k in var.ncattrs()}
                    shape = list(var.shape)
                    dims = list(var.dimensions)
                    profile = None
                    try:
                        sampled = var[()] if np.prod(shape, dtype=np.int64) <= MAX_SAMPLE_POINTS else var[tuple(slice(0, min(s, 8)) for s in shape)] if shape else var[()]
                        if np.ma.isMaskedArray(sampled):
                            sampled = sampled.filled(np.nan)
                        profile = profile_array(np.asarray(sampled), attrs)
                    except Exception:
                        profile = None
                    bundle = self.make_item_bundle(
                        file_id,
                        file_row,
                        "variable",
                        prefix,
                        name,
                        shape,
                        str(var.dtype),
                        dims,
                        str(attrs.get("units", "")),
                        str(attrs.get("long_name", "")),
                        str(attrs.get("standard_name", "")),
                        str(attrs.get("description", attrs.get("comment", ""))),
                        attrs,
                        profile,
                    )
                    self.add_item_bundle(file_id, bundle)
                for child_name, child in group.groups.items():
                    walk(child, prefix + child_name + "/")

            walk(ds)
            # useful file-level time updates
            for key in ["time_coverage_start", "time_coverage_end", "start_time", "end_time"]:
                if key in global_attrs:
                    parsed = parse_time_from_any(global_attrs[key])
                    if parsed:
                        if "start" in key and not file_row["start_time"]:
                            file_row["start_time"] = parsed
                        if "end" in key and not file_row["end_time"]:
                            file_row["end_time"] = parsed
            file_row["read_status"] = "OK"
        finally:
            ds.close()

    def read_hdf5(self, file_row: dict[str, Any], path: Path) -> None:
        file_id = file_row["file_id"]
        with h5py.File(path, "r") as hdf:
            self.record_attr_rows(file_id, None, "global", attrs_to_dict(hdf.attrs))

            def walk(group: h5py.Group, prefix: str = "/") -> None:
                if prefix != "/":
                    bundle = self.make_item_bundle(
                        file_id,
                        file_row,
                        "group",
                        prefix,
                        prefix.split("/")[-2] if prefix.endswith("/") else prefix.split("/")[-1],
                        [],
                        "group",
                        [],
                        "",
                        "",
                        "",
                        "hdf5 group",
                        attrs_to_dict(group.attrs),
                        None,
                    )
                    self.add_item_bundle(file_id, bundle)
                for name, obj in group.items():
                    obj_path = prefix + name if prefix.endswith("/") else prefix + "/" + name
                    if isinstance(obj, h5py.Group):
                        walk(obj, obj_path + "/")
                    elif isinstance(obj, h5py.Dataset):
                        attrs = attrs_to_dict(obj.attrs)
                        shape = list(obj.shape)
                        dims = [f"dim_{i}" for i in range(len(shape))]
                        profile = None
                        try:
                            sampled = obj[()] if obj.size <= MAX_SAMPLE_POINTS else obj[tuple(slice(0, min(s, 8)) for s in obj.shape)] if obj.shape else obj[()]
                            profile = profile_array(np.asarray(sampled), attrs)
                        except Exception:
                            profile = None
                        bundle = self.make_item_bundle(
                            file_id,
                            file_row,
                            "dataset",
                            prefix,
                            name,
                            shape,
                            str(obj.dtype),
                            dims,
                            str(attrs.get("units", "")),
                            str(attrs.get("long_name", "")),
                            str(attrs.get("standard_name", "")),
                            str(attrs.get("description", "")),
                            attrs,
                            profile,
                        )
                        self.add_item_bundle(file_id, bundle)

            walk(hdf)
        file_row["read_status"] = "OK"

    def read_npz(self, file_row: dict[str, Any], path: Path) -> None:
        file_id = file_row["file_id"]
        with np.load(path, allow_pickle=False) as npz:
            meta = {}
            if "metadata_json" in npz.files:
                try:
                    meta = json.loads(str(npz["metadata_json"]))
                except Exception:
                    meta = {}
            if meta:
                self.record_attr_rows(file_id, None, "npz_metadata_json", meta)
                for key, value in meta.items():
                    attrs = {}
                    profile = None
                    if isinstance(value, (dict, list)):
                        dtype = type(value).__name__
                        shape = [len(value)] if isinstance(value, list) else []
                        dims = []
                        units = ""
                        long_name = ""
                        standard_name = ""
                        description = "metadata_json item"
                    else:
                        dtype = type(value).__name__
                        shape = []
                        dims = []
                        units = ""
                        long_name = ""
                        standard_name = ""
                        description = "metadata_json scalar item"
                    bundle = self.make_item_bundle(
                        file_id,
                        file_row,
                        "metadata_item",
                        "/metadata_json",
                        key,
                        shape,
                        dtype,
                        dims,
                        units,
                        long_name,
                        standard_name,
                        description,
                        attrs,
                        profile,
                        notes=truncate_text(str(value), 300),
                    )
                    self.add_item_bundle(file_id, bundle)

            for name in npz.files:
                if name.endswith("_json") or name == "metadata_json":
                    continue
                arr = np.asarray(npz[name])
                attrs = {}
                if meta.get("reader_attrs") and isinstance(meta["reader_attrs"], dict):
                    attrs = meta["reader_attrs"].get(f"attrs_{name}", {}) or {}
                profile = profile_array(arr, attrs)
                bundle = self.make_item_bundle(
                    file_id,
                    file_row,
                    "npz_array",
                    "/",
                    name,
                    list(arr.shape),
                    str(arr.dtype),
                    [f"dim_{i}" for i in range(arr.ndim)],
                    str(attrs.get("units", "")),
                    str(attrs.get("long_name", "")),
                    str(attrs.get("standard_name", "")),
                    str(attrs.get("description", "")),
                    attrs,
                    profile,
                    notes=f"npz key {name}",
                )
                self.add_item_bundle(file_id, bundle)
        file_row["read_status"] = "OK"

    def read_nat(self, file_row: dict[str, Any], path: Path) -> None:
        file_id = file_row["file_id"]
        handler = NativeMSGFileHandler(str(path), {}, {})
        mda = handler.mda or {}
        header = handler.header or {}
        trailer = handler.trailer or {}
        file_row["satellite"] = file_row["satellite"] or infer_meteosat_service(
            str(mda.get("platform_name", "")),
            float(mda.get("projection_parameters", {}).get("ssp_longitude", 0.0)),
        )
        self.record_attr_rows(file_id, None, "native_mda", mda)
        self.record_attr_rows(file_id, None, "native_header", header)
        self.record_attr_rows(file_id, None, "native_trailer", trailer)
        for key, value in mda.items():
            item_type = "metadata_item"
            attrs = {}
            shape = []
            dtype = type(value).__name__
            notes = ""
            if isinstance(value, dict):
                notes = truncate_text(safe_json(value), 300)
            bundle = self.make_item_bundle(
                file_id,
                file_row,
                item_type,
                "/native_mda",
                key,
                shape,
                dtype,
                [],
                "",
                "",
                "",
                "satpy native metadata",
                attrs,
                None,
                notes=notes,
            )
            self.add_item_bundle(file_id, bundle)
        file_row["read_status"] = "OK"

    def read_hsd(self, file_row: dict[str, Any], path: Path) -> None:
        file_id = file_row["file_id"]
        seg_match = re.search(r"_S(\d{2})(\d{2})\.", path.name)
        seg = int(seg_match.group(1)) if seg_match else 1
        total = int(seg_match.group(2)) if seg_match else 10
        band_match = re.search(r"_(B\d{2})_", path.name.upper())
        band = band_match.group(1) if band_match else "B13"
        handler = AHIHSDFileHandler(str(path), {"segment": seg, "total_segments": total}, {"file_type": band})
        area = handler._get_area_def()
        proj_info = {k: attr_to_python(handler.proj_info[k]) for k in handler.proj_info.dtype.names}
        nav_info = {k: attr_to_python(handler.nav_info[k]) for k in handler.nav_info.dtype.names}
        meta = {
            "platform_name": handler.platform_name,
            "observation_area": handler.observation_area,
            "segment_index": seg,
            "segment_count": total,
            "proj_info": proj_info,
            "nav_info": nav_info,
            "area_id": area.area_id,
            "area_shape": list(area.shape),
            "proj_dict": area.proj_dict,
            "area_extent": [float(x) for x in area.area_extent],
            "full_disk_complete": total == 10,
        }
        self.record_attr_rows(file_id, None, "hsd_meta", meta)
        for key, value in meta.items():
            bundle = self.make_item_bundle(
                file_id,
                file_row,
                "metadata_item",
                "/hsd",
                key,
                [len(value)] if isinstance(value, list) else [],
                type(value).__name__,
                [],
                "",
                "",
                "",
                "HSD metadata item",
                {},
                None,
                notes=truncate_text(safe_json(value), 500),
            )
            self.add_item_bundle(file_id, bundle)
        file_row["read_status"] = "OK"

    def read_zip(self, file_row: dict[str, Any], path: Path) -> None:
        file_id = file_row["file_id"]
        with zipfile.ZipFile(path) as zf:
            entries = zf.infolist()
            bundle = self.make_item_bundle(
                file_id,
                file_row,
                "metadata_item",
                "/zip",
                "entry_count",
                [],
                "int",
                [],
                "",
                "",
                "",
                "zip entry count",
                {},
                None,
                notes=str(len(entries)),
            )
            self.add_item_bundle(file_id, bundle)
            for info in entries[:100]:
                attrs = {"compress_size": info.compress_size, "file_size": info.file_size}
                bundle = self.make_item_bundle(
                    file_id,
                    file_row,
                    "zip_entry",
                    "/zip",
                    info.filename,
                    [],
                    "zip_entry",
                    [],
                    "",
                    "",
                    "",
                    "zip entry",
                    attrs,
                    None,
                )
                self.add_item_bundle(file_id, bundle)
                if info.filename.lower().endswith(".xml"):
                    try:
                        root = ET.fromstring(zf.read(info.filename))
                        self.walk_xml_tree(file_row, file_id, root, f"/zip/{info.filename}")
                    except Exception:
                        pass
        file_row["read_status"] = "OK"

    def read_json(self, file_row: dict[str, Any], path: Path) -> None:
        file_id = file_row["file_id"]
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        self.walk_json_like(file_row, file_id, data, "/json", "json_key")
        file_row["read_status"] = "OK"

    def read_yaml(self, file_row: dict[str, Any], path: Path) -> None:
        file_id = file_row["file_id"]
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        self.walk_json_like(file_row, file_id, data, "/yaml", "yaml_key")
        file_row["read_status"] = "OK"

    def walk_json_like(self, file_row: dict[str, Any], file_id: int, data: Any, prefix: str, item_type: str) -> None:
        if isinstance(data, dict):
            for key, value in data.items():
                shape = [len(value)] if isinstance(value, list) else []
                dtype = type(value).__name__
                bundle = self.make_item_bundle(
                    file_id,
                    file_row,
                    item_type,
                    prefix,
                    str(key),
                    shape,
                    dtype,
                    [],
                    "",
                    "",
                    "",
                    f"{item_type} item",
                    {},
                    None,
                    notes=truncate_text(safe_json(value), 500) if not isinstance(value, (dict, list)) else "",
                )
                self.add_item_bundle(file_id, bundle)
                self.walk_json_like(file_row, file_id, value, prefix + "/" + str(key), item_type)
        elif isinstance(data, list):
            if data and isinstance(data[0], dict):
                for idx, value in enumerate(data[:16]):
                    self.walk_json_like(file_row, file_id, value, prefix + f"/[{idx}]", item_type)

    def read_csv(self, file_row: dict[str, Any], path: Path) -> None:
        file_id = file_row["file_id"]
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            fieldnames = reader.fieldnames or []
            rows = []
            for idx, row in enumerate(reader):
                if idx >= 32:
                    break
                rows.append(row)
        for col in fieldnames:
            values = [r.get(col) for r in rows if col in r]
            arr = np.asarray([v for v in values if v not in [None, ""]], dtype=object)
            profile = None
            numeric_vals: list[float] = []
            for v in values:
                try:
                    numeric_vals.append(float(v))
                except Exception:
                    pass
            if numeric_vals:
                profile = profile_array(np.asarray(numeric_vals, dtype=np.float32), {})
            bundle = self.make_item_bundle(
                file_id,
                file_row,
                "csv_column",
                "/csv",
                col,
                [len(values)],
                "column",
                ["row"],
                "",
                "",
                "",
                "csv column",
                {},
                profile,
                notes=truncate_text(safe_json(values[:8]), 300),
            )
            self.add_item_bundle(file_id, bundle)
        file_row["read_status"] = "OK"

    def read_text(self, file_row: dict[str, Any], path: Path) -> None:
        file_id = file_row["file_id"]
        text = path.read_text(encoding="utf-8-sig", errors="ignore")
        lines = text.splitlines()
        attrs = {"line_count": len(lines), "char_count": len(text)}
        bundle = self.make_item_bundle(
            file_id,
            file_row,
            "text_document",
            "/text",
            path.name,
            [len(lines)],
            "text",
            ["line"],
            "",
            "",
            "",
            "plain text or markdown file",
            attrs,
            None,
            notes=truncate_text(text[:MAX_TEXT_CHARS], 500),
        )
        self.add_item_bundle(file_id, bundle)
        if path.suffix.lower() == ".md":
            for line in lines:
                if line.startswith("#"):
                    bundle = self.make_item_bundle(
                        file_id,
                        file_row,
                        "markdown_heading",
                        "/text",
                        line.lstrip("# ").strip(),
                        [],
                        "heading",
                        [],
                        "",
                        "",
                        "",
                        "markdown heading",
                        {},
                        None,
                    )
                    self.add_item_bundle(file_id, bundle)
        file_row["read_status"] = "OK"

    def read_xml(self, file_row: dict[str, Any], path: Path) -> None:
        file_id = file_row["file_id"]
        root = ET.fromstring(path.read_text(encoding="utf-8-sig", errors="ignore"))
        self.walk_xml_tree(file_row, file_id, root, "/xml")
        file_row["read_status"] = "OK"

    def walk_xml_tree(self, file_row: dict[str, Any], file_id: int, root: ET.Element, prefix: str) -> None:
        bundle = self.make_item_bundle(
            file_id,
            file_row,
            "xml_element",
            prefix,
            root.tag,
            [],
            "xml",
            [],
            "",
            "",
            "",
            "xml element",
            root.attrib,
            None,
            notes=truncate_text((root.text or "").strip(), 300),
        )
        self.add_item_bundle(file_id, bundle)
        for child in list(root)[:200]:
            self.walk_xml_tree(file_row, file_id, child, prefix + "/" + root.tag)


def infer_meteosat_service(platform_name: str, ssp_lon: float) -> str:
    if abs(ssp_lon - 45.5) < 0.2:
        return "Meteosat-IODC"
    if abs(ssp_lon - 0.0) < 0.2:
        return "Meteosat-0deg"
    if "9" in platform_name:
        return "Meteosat-IODC"
    if "10" in platform_name:
        return "Meteosat-0deg"
    return platform_name or "Meteosat"


def discover_files() -> list[Path]:
    files: list[Path] = []
    for root in INPUT_DIRS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and is_supported_file(path):
                files.append(path)
    files.sort()
    return files


def write_gate_rows(conn: sqlite3.Connection) -> dict[str, str]:
    stats = {}
    files_df = pd.read_sql_query("SELECT read_status, COUNT(*) AS c FROM files GROUP BY read_status", conn)
    total_files = int(files_df["c"].sum()) if not files_df.empty else 0
    ok_files = int(files_df[files_df["read_status"] == "OK"]["c"].sum()) if not files_df.empty else 0
    error_files = int(files_df[files_df["read_status"] == "ERROR"]["c"].sum()) if not files_df.empty else 0

    unknown_df = pd.read_sql_query("SELECT priority, COUNT(*) AS c FROM unknowns GROUP BY priority", conn)
    high_unknowns = int(unknown_df[unknown_df["priority"] == "HIGH"]["c"].sum()) if not unknown_df.empty else 0
    blocking_unknowns = int(pd.read_sql_query("SELECT COUNT(*) AS c FROM unknowns WHERE blocks_07 = 1", conn).iloc[0]["c"])

    capability_df = pd.read_sql_query(
        """
        SELECT f.satellite, f.product,
               MAX(CASE WHEN i.semantic_class LIKE '%cloud_mask_or_class%' THEN 1 ELSE 0 END) AS has_cloud_class,
               MAX(CASE WHEN i.name IN ('cloud_top_height_km','HT','CldTopHght') OR i.semantic_class LIKE '%cloud_physical%' THEN 1 ELSE 0 END) AS has_cloud_physical,
               MAX(CASE WHEN i.semantic_class LIKE '%coordinate%' OR i.semantic_class LIKE '%navigation_projection%' THEN 1 ELSE 0 END) AS has_geo,
               MAX(CASE WHEN i.semantic_class LIKE '%geometry_angle%' THEN 1 ELSE 0 END) AS has_angle,
               MAX(CASE WHEN i.semantic_class LIKE '%quality_flag%' THEN 1 ELSE 0 END) AS has_quality
        FROM files f
        LEFT JOIN items i ON f.file_id = i.file_id
        GROUP BY f.satellite, f.product
        """,
        conn,
    )
    core_satellites = {"FY4B", "GOES-16", "GOES-18", "Himawari-9", "Meteosat-0deg", "Meteosat-IODC"}
    covered = {s for s in capability_df["satellite"].dropna().unique() if s in core_satellites}

    discovery_gate = "PASS" if total_files > 0 and ok_files >= max(1, int(total_files * 0.95)) else "FAIL"
    semantic_gate = "PASS" if blocking_unknowns == 0 and error_files == 0 else ("PARTIAL" if blocking_unknowns < 20 else "FAIL")
    fusion_gate = "PASS_WITH_WARNINGS"
    if len(covered) < 6 or blocking_unknowns > 20:
        fusion_gate = "FAIL"
    elif blocking_unknowns == 0 and error_files == 0:
        fusion_gate = "PASS"
    unknown_gate = "LOW" if blocking_unknowns == 0 else ("MEDIUM" if blocking_unknowns < 20 else "HIGH")

    gate_rows = [
        ("DISCOVERY_GATE", discovery_gate, f"ok_files={ok_files}; total_files={total_files}; error_files={error_files}"),
        ("SEMANTIC_GATE", semantic_gate, f"high_unknowns={high_unknowns}; blocking_unknowns={blocking_unknowns}; error_files={error_files}"),
        ("FUSION_READINESS_GATE", fusion_gate, f"covered_core_satellites={len(covered)}; blocking_unknowns={blocking_unknowns}"),
        ("UNKNOWN_RISK_GATE", unknown_gate, f"high_unknowns={high_unknowns}; blocking_unknowns={blocking_unknowns}"),
    ]
    conn.execute("DELETE FROM audit_gates")
    conn.executemany("INSERT INTO audit_gates (gate_name, gate_value, rationale) VALUES (?, ?, ?)", gate_rows)
    conn.commit()
    for name, value, _ in gate_rows:
        stats[name] = value
    return stats


def export_views(conn: sqlite3.Connection) -> None:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    high_unknowns_raw = pd.read_sql_query(
        """
        SELECT u.*, i.name, i.group_path, i.semantic_class, i.known_status, i.notes, f.path, f.satellite, f.product
        FROM unknowns u
        JOIN items i ON u.item_id = i.item_id
        JOIN files f ON i.file_id = f.file_id
        WHERE u.priority = 'HIGH' OR u.blocks_07 = 1
        ORDER BY u.blocks_07 DESC, u.risk_score DESC, f.satellite, f.product, i.name
        """,
        conn,
    )
    high_unknowns_raw.to_csv(HIGH_UNKNOWNS_RAW_CSV, index=False, encoding="utf-8-sig")
    if high_unknowns_raw.empty:
        high_unknowns = high_unknowns_raw.copy()
    else:
        grouped_rows: list[dict[str, Any]] = []
        group_cols = [
            "satellite",
            "product",
            "name",
            "semantic_class",
            "known_status",
            "possible_meaning",
            "potential_use",
            "blocks_07",
            "affects_future_rating",
        ]
        for keys, group in high_unknowns_raw.groupby(group_cols, dropna=False, sort=True):
            key_map = dict(zip(group_cols, keys))
            grouped_rows.append(
                {
                    **key_map,
                    "file_occurrences": int(len(group)),
                    "max_risk_score": int(group["risk_score"].max()),
                    "example_path": str(group["path"].iloc[0]),
                    "why_flagged": " | ".join(sorted({str(v) for v in group["why_flagged"].dropna() if str(v).strip()})),
                    "notes": " | ".join(sorted({str(v) for v in group["notes"].dropna() if str(v).strip()})[:3]),
                    "example_group_path": str(group["group_path"].iloc[0]) if "group_path" in group.columns else "",
                }
            )
        high_unknowns = pd.DataFrame(grouped_rows).sort_values(
            by=["blocks_07", "max_risk_score", "file_occurrences", "satellite", "product", "name"],
            ascending=[False, False, False, True, True, True],
        )
    high_unknowns.to_csv(HIGH_UNKNOWNS_CSV, index=False, encoding="utf-8-sig")

    capability = pd.read_sql_query(
        """
        SELECT f.satellite, f.product,
               MAX(CASE WHEN i.normalized_name IN ('cloudmask','cloudmaskbinary','bcm','acm','cbm','clm','cmsk') THEN 1 ELSE 0 END) AS has_cloud_mask_or_class,
               MAX(CASE WHEN i.normalized_name IN ('cloudtopheightkm','cldtophght','ht','cth') THEN 1 ELSE 0 END) AS has_cth_like,
               MAX(CASE WHEN i.normalized_name IN ('cloudtoptemperaturek','cldtoptemp','ctt') THEN 1 ELSE 0 END) AS has_ctt_like,
               MAX(CASE WHEN i.normalized_name IN ('cloudtoppressurehpa','cldtoppres','ctp') THEN 1 ELSE 0 END) AS has_ctp_like,
               MAX(CASE WHEN i.normalized_name IN ('cloudopticalthickness','cldoptdpth','cod','cot') THEN 1 ELSE 0 END) AS has_cot_like,
               MAX(CASE WHEN i.normalized_name IN ('cloudeffectiveradiusum','cer','cps') THEN 1 ELSE 0 END) AS has_cer_like,
               MAX(CASE WHEN i.semantic_class LIKE '%coordinate%' OR i.semantic_class LIKE '%navigation_projection%' THEN 1 ELSE 0 END) AS has_geometry,
               MAX(CASE WHEN i.semantic_class LIKE '%geometry_angle%' THEN 1 ELSE 0 END) AS has_angles,
               MAX(CASE WHEN i.semantic_class LIKE '%quality_flag%' THEN 1 ELSE 0 END) AS has_quality,
               COUNT(DISTINCT f.file_id) AS file_count
        FROM files f
        LEFT JOIN items i ON f.file_id = i.file_id
        GROUP BY f.satellite, f.product
        ORDER BY f.satellite, f.product
        """,
        conn,
    )
    capability.to_csv(CAPABILITY_CSV, index=False, encoding="utf-8-sig")

    blocking = pd.read_sql_query(
        """
        SELECT f.satellite, f.product, f.path, f.read_status, f.error_message,
               i.name, i.semantic_class, i.known_status,
               u.priority, u.risk_score, u.why_flagged, u.possible_meaning, u.blocks_07
        FROM files f
        LEFT JOIN items i ON f.file_id = i.file_id
        LEFT JOIN unknowns u ON i.item_id = u.item_id
        WHERE f.read_status = 'ERROR' OR (u.blocks_07 = 1)
        ORDER BY f.read_status DESC, u.risk_score DESC, f.satellite, f.product
        """,
        conn,
    )
    blocking.to_csv(BLOCKING_CSV, index=False, encoding="utf-8-sig")

    recommendation = pd.read_sql_query(
        """
        SELECT f.satellite, f.product, i.name, i.semantic_class, i.known_status,
               r.use_now, r.use_later, r.do_not_use, r.use_for_fusion, r.use_for_rating,
               r.use_for_screening, r.use_for_07_stratification, r.use_for_future_deep_space_enhancement,
               r.reason, r.confidence, r.blocking_issue
        FROM recommendations r
        JOIN items i ON r.item_id = i.item_id
        JOIN files f ON i.file_id = f.file_id
        WHERE r.use_for_fusion = 1 OR r.use_for_rating = 1 OR r.use_for_07_stratification = 1
           OR i.known_status LIKE 'unknown%'
        ORDER BY f.satellite, f.product, i.name
        """,
        conn,
    )
    recommendation.to_csv(RECOMMEND_CSV, index=False, encoding="utf-8-sig")


def maybe_write_parquet(conn: sqlite3.Connection) -> list[str]:
    outputs: list[str] = []
    try:
        import pyarrow  # noqa: F401
    except Exception:
        return outputs
    PARQUET_DIR.mkdir(parents=True, exist_ok=True)
    for table in ["files", "items", "unknowns", "recommendations"]:
        df = pd.read_sql_query(f"SELECT * FROM {table}", conn)
        path = PARQUET_DIR / f"{table}.parquet"
        df.to_parquet(path, index=False)
        outputs.append(str(path))
    return outputs


def build_summary(conn: sqlite3.Connection, gates: dict[str, str], parquet_paths: list[str]) -> dict[str, Any]:
    file_counts = pd.read_sql_query("SELECT read_status, COUNT(*) AS count FROM files GROUP BY read_status", conn)
    item_counts = pd.read_sql_query("SELECT item_type, COUNT(*) AS count FROM items GROUP BY item_type", conn)
    unknown_counts = pd.read_sql_query("SELECT priority, COUNT(*) AS count FROM unknowns GROUP BY priority", conn)
    top_blocking = pd.read_sql_query(
        """
        SELECT f.satellite, f.product, i.name, u.priority, u.risk_score, u.why_flagged
        FROM unknowns u
        JOIN items i ON u.item_id = i.item_id
        JOIN files f ON i.file_id = f.file_id
        WHERE u.blocks_07 = 1
        ORDER BY u.risk_score DESC
        LIMIT 20
        """,
        conn,
    )
    allow_07 = gates["DISCOVERY_GATE"] == "PASS" and gates["FUSION_READINESS_GATE"] != "FAIL" and gates["UNKNOWN_RISK_GATE"] != "HIGH"
    return {
        "run_time": utc_now(),
        "input_dirs": [str(p) for p in INPUT_DIRS],
        "output_paths": {
            "sqlite": str(SQLITE_PATH),
            "summary_json": str(SUMMARY_JSON),
            "report_md": str(REPORT_MD),
            "high_priority_unknowns_csv": str(HIGH_UNKNOWNS_CSV),
            "product_capability_matrix_csv": str(CAPABILITY_CSV),
            "blocking_issues_csv": str(BLOCKING_CSV),
            "recommendation_matrix_csv": str(RECOMMEND_CSV),
            "parquet": parquet_paths,
        },
        "file_counts": file_counts.to_dict(orient="records"),
        "item_counts": item_counts.to_dict(orient="records"),
        "unknown_counts_by_priority": unknown_counts.to_dict(orient="records"),
        "gate_status": gates,
        "top_blocking_issues": top_blocking.to_dict(orient="records"),
        "next_step_recommendation": "允许进入07并携带 warning" if allow_07 else "先处理 blocking unknowns/read failures，再决定是否进入07",
        "allow_enter_07": allow_07,
    }


def build_report(conn: sqlite3.Connection, summary: dict[str, Any]) -> str:
    total_files = int(pd.read_sql_query("SELECT COUNT(*) AS c FROM files", conn).iloc[0]["c"])
    ok_files = int(pd.read_sql_query("SELECT COUNT(*) AS c FROM files WHERE read_status = 'OK'", conn).iloc[0]["c"])
    err_files = int(pd.read_sql_query("SELECT COUNT(*) AS c FROM files WHERE read_status = 'ERROR'", conn).iloc[0]["c"])
    core_cap = pd.read_sql_query(
        """
        SELECT f.satellite, f.product,
               MAX(CASE WHEN i.normalized_name IN ('cloudmask','cloudmaskbinary','bcm','acm','cbm','clm','cmsk') THEN 1 ELSE 0 END) AS cloud_class,
               MAX(CASE WHEN i.normalized_name IN ('cloudtopheightkm','cldtophght','ht','cth') THEN 1 ELSE 0 END) AS cth_like,
               MAX(CASE WHEN i.normalized_name IN ('cloudtoptemperaturek','cldtoptemp','ctt') THEN 1 ELSE 0 END) AS ctt_like,
               MAX(CASE WHEN i.normalized_name IN ('cloudtoppressurehpa','cldtoppres','ctp') THEN 1 ELSE 0 END) AS ctp_like,
               MAX(CASE WHEN i.semantic_class LIKE '%geometry_angle%' THEN 1 ELSE 0 END) AS angles,
               MAX(CASE WHEN i.semantic_class LIKE '%coordinate%' OR i.semantic_class LIKE '%navigation_projection%' THEN 1 ELSE 0 END) AS geometry,
               MAX(CASE WHEN i.semantic_class LIKE '%quality_flag%' THEN 1 ELSE 0 END) AS quality
        FROM files f
        LEFT JOIN items i ON f.file_id = i.file_id
        GROUP BY f.satellite, f.product
        ORDER BY f.satellite, f.product
        """,
        conn,
    )
    underused = pd.read_sql_query(
        """
        SELECT f.satellite, f.product, i.name, i.semantic_class, i.known_status
        FROM items i
        JOIN files f ON i.file_id = f.file_id
        WHERE f.satellite IS NOT NULL
          AND i.item_type IN ('variable','dataset','npz_array')
          AND (
               i.semantic_class LIKE '%geometry_angle%'
           OR i.semantic_class LIKE '%navigation_projection%'
           OR i.semantic_class LIKE '%time_scan%'
           OR i.semantic_class LIKE '%quality_flag%'
          )
        ORDER BY f.satellite, f.product, i.name
        LIMIT 40
        """,
        conn,
    )
    high_unknowns = pd.read_sql_query(
        """
        SELECT f.satellite, f.product, i.name, u.risk_score, u.why_flagged, u.blocks_07, u.affects_future_rating
        FROM unknowns u
        JOIN items i ON u.item_id = i.item_id
        JOIN files f ON i.file_id = f.file_id
        WHERE u.priority = 'HIGH'
          AND f.satellite IS NOT NULL
        ORDER BY u.blocks_07 DESC, u.risk_score DESC, f.satellite, f.product, i.name
        LIMIT 30
        """,
        conn,
    )
    overlap_ready = pd.read_sql_query(
        """
        SELECT DISTINCT f.satellite, f.product, i.name
        FROM items i
        JOIN files f ON i.file_id = f.file_id
        WHERE f.satellite IS NOT NULL
          AND i.item_type IN ('variable','dataset','npz_array')
          AND i.name IN ('cloud_mask','cloud_top_height_km','cloud_top_temperature_K','cloud_top_pressure_hPa',
                         'cloud_phase','cloud_type','sensor_zenith_angle','solar_zenith_angle',
                         'valid_mask','quality_flag_raw','quality_flag_standard')
        ORDER BY f.satellite, f.product, i.name
        """,
        conn,
    )
    diagnostic_only = pd.read_sql_query(
        """
        SELECT DISTINCT f.satellite, f.product, i.name
        FROM recommendations r
        JOIN items i ON r.item_id = i.item_id
        JOIN files f ON i.file_id = f.file_id
        WHERE f.satellite IS NOT NULL
          AND i.item_type IN ('variable','dataset','npz_array')
          AND r.use_now = 0 AND r.use_later = 1 AND r.use_for_fusion = 0
        ORDER BY f.satellite, f.product, i.name
        LIMIT 40
        """,
        conn,
    )
    official_angle = pd.read_sql_query(
        """
        SELECT DISTINCT f.satellite, f.product, i.name
        FROM geo_time g
        JOIN items i ON g.item_id = i.item_id
        JOIN files f ON i.file_id = f.file_id
        WHERE f.satellite IS NOT NULL
          AND i.item_type IN ('variable','dataset','npz_array')
          AND g.angle_kind = 'official_angle'
        ORDER BY f.satellite, f.product, i.name
        """,
        conn,
    )
    computed_angle = pd.read_sql_query(
        """
        SELECT DISTINCT f.satellite, f.product, i.name
        FROM geo_time g
        JOIN items i ON g.item_id = i.item_id
        JOIN files f ON i.file_id = f.file_id
        WHERE f.satellite IS NOT NULL
          AND i.item_type IN ('variable','dataset','npz_array')
          AND g.angle_kind = 'computed_angle'
        ORDER BY f.satellite, f.product, i.name
        """,
        conn,
    )
    approx_angle = pd.read_sql_query(
        """
        SELECT DISTINCT f.satellite, f.product, i.name
        FROM geo_time g
        JOIN items i ON g.item_id = i.item_id
        JOIN files f ON i.file_id = f.file_id
        WHERE f.satellite IS NOT NULL
          AND i.item_type IN ('variable','dataset','npz_array')
          AND g.angle_kind = 'approximate_angle'
        ORDER BY f.satellite, f.product, i.name
        """,
        conn,
    )
    unclear_flags = pd.read_sql_query(
        """
        SELECT f.satellite, f.product, i.name, fl.requires_manual_interpretation
        FROM flags fl
        JOIN items i ON fl.item_id = i.item_id
        JOIN files f ON i.file_id = f.file_id
        WHERE f.satellite IS NOT NULL
          AND i.item_type IN ('variable','dataset','npz_array')
          AND fl.requires_manual_interpretation = 1
        ORDER BY f.satellite, f.product, i.name
        LIMIT 40
        """,
        conn,
    )
    warnings = []
    if summary["gate_status"]["SEMANTIC_GATE"] != "PASS":
        warnings.append("semantic unknowns 仍需人工确认")
    if summary["gate_status"]["UNKNOWN_RISK_GATE"] != "LOW":
        warnings.append("unknown risk 未降到 LOW")
    if summary["gate_status"]["FUSION_READINESS_GATE"] != "PASS":
        warnings.append("fusion readiness 需携带 warning")

    lines = [
        "# 06f Unknown-aware Data Asset Audit Report",
        "",
        f"- 生成时间 UTC: {summary['run_time']}",
        f"- 输入目录: {', '.join(summary['input_dirs'])}",
        "",
        "## 1. 扫描规模",
        "",
        f"- 共扫描文件: `{total_files}`",
        f"- 成功读取: `{ok_files}`",
        f"- 失败读取: `{err_files}`",
        "",
        "## 2. 各卫星/产品核心能力",
        "",
    ]
    for row in core_cap.itertuples():
        if not row.satellite:
            continue
        lines.append(
            f"- {row.satellite} / {row.product}: "
            f"cloud_class={int(row.cloud_class)}, cth_like={int(row.cth_like)}, ctt_like={int(row.ctt_like)}, "
            f"ctp_like={int(row.ctp_like)}, geometry={int(row.geometry)}, angles={int(row.angles)}, quality={int(row.quality)}"
        )
    lines.extend(["", "## 3. 当前数据里以前未充分利用的信息", ""])
    for row in underused.head(20).itertuples():
        lines.append(f"- {row.satellite} / {row.product} / {row.name}: {row.semantic_class} ({row.known_status})")
    lines.extend(["", "## 4. 高优先级未知项", ""])
    if high_unknowns.empty:
        lines.append("- 未发现 HIGH priority unknown。")
    else:
        for row in high_unknowns.itertuples():
            lines.append(f"- {row.satellite} / {row.product} / {row.name}: risk={row.risk_score}, blocks_07={row.blocks_07}, affects_rating={row.affects_future_rating}; {row.why_flagged}")
    lines.extend(["", "## 5. 哪些未知项阻塞 07", ""])
    blockers = high_unknowns[high_unknowns["blocks_07"] == 1] if not high_unknowns.empty else pd.DataFrame()
    if blockers.empty:
        lines.append("- 当前未发现明确 blocks_07 的高优先级未知项。")
    else:
        for row in blockers.itertuples():
            lines.append(f"- {row.satellite} / {row.product} / {row.name}: {row.why_flagged}")
    lines.extend(["", "## 6. 哪些未知项只影响后续 rating", ""])
    rating_only = high_unknowns[(high_unknowns["blocks_07"] == 0) & (high_unknowns["affects_future_rating"] == 1)] if not high_unknowns.empty else pd.DataFrame()
    if rating_only.empty:
        lines.append("- 未发现仅影响 rating 的 HIGH priority unknown。")
    else:
        for row in rating_only.itertuples():
            lines.append(f"- {row.satellite} / {row.product} / {row.name}: {row.why_flagged}")
    lines.extend(["", "## 7. 可直接用于 07 overlap validation 的变量", ""])
    for row in overlap_ready.head(40).itertuples():
        lines.append(f"- {row.satellite} / {row.product} / {row.name}")
    lines.extend(["", "## 8. 只能诊断、不能直接融合的变量", ""])
    for row in diagnostic_only.head(30).itertuples():
        lines.append(f"- {row.satellite} / {row.product} / {row.name}")
    lines.extend(["", "## 9. 仍语义不清的 flag / code table", ""])
    if unclear_flags.empty:
        lines.append("- 未发现需要人工解释的 flag/code table。")
    else:
        for row in unclear_flags.itertuples():
            lines.append(f"- {row.satellite} / {row.product} / {row.name}")
    lines.extend(["", "## 10. 官方角度 / 计算角度 / 近似角度", ""])
    lines.append("- official_angle:")
    for row in official_angle.head(20).itertuples():
        lines.append(f"  - {row.satellite} / {row.product} / {row.name}")
    lines.append("- computed_angle:")
    for row in computed_angle.head(20).itertuples():
        lines.append(f"  - {row.satellite} / {row.product} / {row.name}")
    lines.append("- approximate_angle:")
    for row in approx_angle.head(20).itertuples():
        lines.append(f"  - {row.satellite} / {row.product} / {row.name}")
    lines.extend(["", "## 11. 对深空相机云信息增强最有价值的信息", ""])
    lines.extend(
        [
            "- 几何角度场: sensor_zenith_angle / solar_zenith_angle / azimuth / glint / relative_azimuth。",
            "- 官方或准官方投影参数: SSP longitude, perspective/satellite height, semi-major/minor axis, sweep axis, CFAC/LFAC/COFF/LOFF。",
            "- 质量与算法状态层: DQF / quality_flag / status / confidence / code table。",
            "- 云微物理辅助层: cloud_optical_thickness / effective_radius / cloud phase/type。",
        ]
    )
    lines.extend(["", "## 12. 是否允许进入 07", ""])
    lines.append(f"- DISCOVERY_GATE: `{summary['gate_status']['DISCOVERY_GATE']}`")
    lines.append(f"- SEMANTIC_GATE: `{summary['gate_status']['SEMANTIC_GATE']}`")
    lines.append(f"- FUSION_READINESS_GATE: `{summary['gate_status']['FUSION_READINESS_GATE']}`")
    lines.append(f"- UNKNOWN_RISK_GATE: `{summary['gate_status']['UNKNOWN_RISK_GATE']}`")
    lines.append(f"- 是否允许进入 07: `{summary['allow_enter_07']}`")
    if warnings:
        lines.append(f"- 必带 warning: {', '.join(warnings)}")
    else:
        lines.append("- 必带 warning: none")
    return "\n".join(lines)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    if SQLITE_PATH.exists():
        SQLITE_PATH.unlink()

    conn = sqlite3.connect(SQLITE_PATH)
    summary: dict[str, Any] | None = None
    try:
        setup_db(conn)
        auditor = Auditor(conn)
        for path in discover_files():
            auditor.process(path)
        auditor.flush()
        gates = write_gate_rows(conn)
        export_views(conn)
        parquet_paths = maybe_write_parquet(conn)
        summary = build_summary(conn, gates, parquet_paths)
        SUMMARY_JSON.write_text(safe_json(summary), encoding="utf-8")
        REPORT_MD.write_text(build_report(conn, summary), encoding="utf-8")
    finally:
        conn.close()

    if summary is None:
        raise RuntimeError("06f audit did not produce summary")
    allow_07 = summary["allow_enter_07"]
    print(f"DISCOVERY_GATE={summary['gate_status']['DISCOVERY_GATE']}")
    print(f"SEMANTIC_GATE={summary['gate_status']['SEMANTIC_GATE']}")
    print(f"FUSION_READINESS_GATE={summary['gate_status']['FUSION_READINESS_GATE']}")
    print(f"UNKNOWN_RISK_GATE={summary['gate_status']['UNKNOWN_RISK_GATE']}")
    print(f"SQLite={SQLITE_PATH}")
    print(f"Report={REPORT_MD}")
    print(f"CSV={HIGH_UNKNOWNS_CSV}")
    print(f"CSV={CAPABILITY_CSV}")
    print(f"CSV={BLOCKING_CSV}")
    print(f"CSV={RECOMMEND_CSV}")
    print(f"AllowEnter07={allow_07}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
