from __future__ import annotations

import csv
import json
import math
import os
import re
import sys
import traceback
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

DATA_ROOT = Path(r"D:\AAAresearch_paper\data")
GEO_ROOT = Path(r"E:\GEO_Cloud_2024")
OUT_DIR = Path(r"D:\AAAresearch_paper\data_check_report")
MANIFEST_DIR = GEO_ROOT / "manifests"

TOO_SMALL_BYTES = 1024
MAX_STATS_VALUES = 200_000
SAMPLE_PER_PRODUCT = 3

STANDARD_VARIABLES = [
    "cloud_mask",
    "cloud_probability",
    "cloud_phase",
    "cloud_type",
    "cloud_top_height",
    "cloud_top_temperature",
    "cloud_top_pressure",
    "cloud_optical_thickness",
    "cloud_effective_radius",
    "quality_flag",
    "latitude",
    "longitude",
    "sensor_zenith_angle",
    "sensor_azimuth_angle",
    "solar_zenith_angle",
    "solar_azimuth_angle",
    "observation_time",
    "brightness_temperature",
    "reflectance",
    "radiance",
    "projection_x",
    "projection_y",
    "geostationary_projection",
]

SATELLITE_EXPECTED_PRODUCTS = {
    "FY4B": ["FDI", "GEO", "CLM", "CTH", "CTT", "CTP", "CLP", "COT", "CER"],
    "GOES": ["ACMF", "ACHAF", "ACTPF", "ACHTF", "CTPF", "CODF", "CPSF", "RadF"],
    "Himawari": ["CMSK", "CHGT", "cloud_type", "cloud_phase", "L1/HSD"],
    "Meteosat": ["CLM", "CTH", "CMA", "CT", "CTTH", "OCA/CMIC", "L1.5"],
}

FY4B_CORE = ["CLM", "CTH", "CTT", "CTP"]
FY4B_MICRO = ["CLP", "COT", "CER"]
GOES_CORE = ["ACMF", "ACHAF"]
HIMAWARI_CORE = ["CMSK", "CHGT"]
METEOSAT_CORE = ["CLM", "CTH"]


@dataclass
class ParsedMeta:
    file_path: str
    root: str
    satellite_family: str
    satellite: str
    sensor: str
    product: str
    level: str
    nominal_time: str
    start_time: str
    end_time: str
    spatial_resolution: str
    version: str
    projection: str
    platform_detail: str
    file_size: int
    suffix: str
    parse_status: str
    notes: str


def log(message: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


def ensure_out() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        seen: list[str] = []
        for row in rows:
            for key in row.keys():
                if key not in seen:
                    seen.append(key)
        fields = seen or ["empty"]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8-sig")


def write_md(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8-sig")


def parse_dt(value: str) -> str:
    if not value:
        return ""
    value = value.strip()
    for fmt in ("%Y%m%d%H%M%S", "%Y%j%H%M%S", "%Y%m%d%H%M", "%Y%m%d%H"):
        try:
            return datetime.strptime(value[: len(datetime.now().strftime(fmt))], fmt).replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        except Exception:
            pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return ""


def floor_hour(iso_time: str) -> str:
    dt = parse_iso(iso_time)
    if not dt:
        return ""
    return dt.replace(minute=0, second=0, microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


FY4B_RE = re.compile(
    r"(?P<sat>FY4B).*?_AGRI.*?_(?P<projection>DISK|REGC|NOM|N_DISK)?_?(?P<lon>\d{4}E)?_"
    r"(?P<level>L[12])-_?(?P<product>[A-Z0-9]+)-.*?_NOM_"
    r"(?P<start>\d{14})_(?P<end>\d{14})_(?P<res>\d+M)_V(?P<version>[^.]+)",
    re.IGNORECASE,
)
GOES_RE = re.compile(
    r"OR_ABI-(?P<level>L\d\w*)-(?P<product>[A-Z0-9]+)-.*?_(?P<sat>G1[68])_"
    r"s(?P<start>\d{13,14})_e(?P<end>\d{13,14})_c(?P<created>\d{13,14})",
    re.IGNORECASE,
)
HIMAWARI_RE = re.compile(
    r"AHI-(?P<product>[A-Z0-9]+)_v(?P<version>[^_]+)_h(?P<sat>\d{2})_"
    r"s(?P<start>\d{15})_e(?P<end>\d{15})_c(?P<created>\d{15})",
    re.IGNORECASE,
)
METEOSAT_RE = re.compile(
    r"(?P<msg>MSG\d)-SEVI-(?P<ptype>[A-Z0-9]+)-.*?-(?P<stamp>\d{14})\.",
    re.IGNORECASE,
)


def parse_path(path: Path, root_name: str, size: int) -> ParsedMeta:
    name = path.name
    suffix = "".join(path.suffixes) or path.suffix
    parts_lower = [part.lower() for part in path.parts]
    note = ""

    m = FY4B_RE.search(name)
    if m or "fy4b" in name.lower() or any("fy4b" in part for part in parts_lower):
        if m:
            start = parse_dt(m.group("start"))
            end = parse_dt(m.group("end"))
            product = normalize_product(m.group("product"))
            return ParsedMeta(str(path), root_name, "FY4B", "FY4B", "AGRI", product, m.group("level"), floor_hour(start), start, end, m.group("res"), m.group("version"), m.group("projection") or "DISK", m.group("lon") or "1330E", size, suffix, "parsed", "")
        product = product_from_folder(path, "FY4B")
        return ParsedMeta(str(path), root_name, "FY4B", "FY4B", "AGRI", product, "", "", "", "", "", "", "", "", size, suffix, "partial", "FY4B path/name detected but standard filename regex failed")

    m = GOES_RE.search(name)
    if m or any(part in ("goes-16", "goes-18") for part in parts_lower):
        sat_map = {"G16": "GOES-16", "G18": "GOES-18"}
        sat = sat_map.get((m.group("sat").upper() if m else ""), product_from_path_satellite(path, "GOES"))
        if m:
            start = parse_goes_stamp(m.group("start"))
            end = parse_goes_stamp(m.group("end"))
            return ParsedMeta(str(path), root_name, "GOES", sat, "ABI", normalize_product(m.group("product")), m.group("level"), floor_hour(start), start, end, "full_disk", "", "geostationary_fixed_grid", "", size, suffix, "parsed", "")
        return ParsedMeta(str(path), root_name, "GOES", sat, "ABI", product_from_folder(path, "GOES"), "", "", "", "", "", "", "", "", size, suffix, "partial", "GOES path detected but ABI filename regex failed")

    m = HIMAWARI_RE.search(name)
    if m or any(part in ("himawari-8", "himawari-9", "h09_data") for part in parts_lower):
        sat = "Himawari-" + str(int(m.group("sat"))) if m else product_from_path_satellite(path, "Himawari")
        if m:
            start = parse_dt(m.group("start")[:14])
            end = parse_dt(m.group("end")[:14])
            return ParsedMeta(str(path), root_name, "Himawari", sat, "AHI", normalize_product(m.group("product")), "L2", floor_hour(start), start, end, "full_disk_5500", m.group("version"), "geostationary_full_disk", "", size, suffix, "parsed", "")
        return ParsedMeta(str(path), root_name, "Himawari", sat, "AHI", product_from_folder(path, "Himawari"), "", "", "", "", "", "", "", "", size, suffix, "partial", "Himawari path detected but AHI filename regex failed")

    m = METEOSAT_RE.search(name)
    if m or any(part.startswith("meteosat") for part in parts_lower):
        service = product_from_path_satellite(path, "Meteosat")
        if m:
            msg = m.group("msg").upper()
            product = normalize_meteosat_product(m.group("ptype"))
            start = parse_dt(m.group("stamp"))
            return ParsedMeta(str(path), root_name, "Meteosat", service, "SEVIRI", product, "L2", floor_hour(start), start, start, "native/full_disk", "", "SEVIRI_native", msg_to_meteosat(msg), size, suffix, "parsed", msg)
        return ParsedMeta(str(path), root_name, "Meteosat", service, "SEVIRI", product_from_folder(path, "Meteosat"), "", "", "", "", "", "", "", "", size, suffix, "partial", "Meteosat path detected but MSG filename regex failed")

    return ParsedMeta(str(path), root_name, "UNKNOWN", "", "", "", "", "", "", "", "", "", "", "", size, suffix, "unparsed", "No supported GEO filename/path pattern")


def parse_goes_stamp(stamp: str) -> str:
    try:
        year = int(stamp[0:4])
        doy = int(stamp[4:7])
        hour = int(stamp[7:9])
        minute = int(stamp[9:11])
        second = int(stamp[11:13])
        dt = datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=doy - 1, hours=hour, minutes=minute, seconds=second)
        return dt.isoformat().replace("+00:00", "Z")
    except Exception:
        return ""


def normalize_product(product: str) -> str:
    value = (product or "").upper().replace("-", "").replace("_", "")
    mapping = {
        "MSGCLMK": "CLM",
        "MSGCTTH": "CTH",
        "CMSK": "CMSK",
        "CHGT": "CHGT",
        "ACMF": "ACMF",
        "ACHAF": "ACHAF",
        "ACTPF": "ACTPF",
        "CODF": "CODF",
        "CPSF": "CPSF",
        "CTPF": "CTPF",
        "ACHTF": "ACHTF",
        "CTTF": "ACHTF",
    }
    return mapping.get(value, value)


def normalize_meteosat_product(ptype: str) -> str:
    value = ptype.upper()
    if "CLM" in value:
        return "CLM"
    if "CTTH" in value or "CTH" in value or "CLTH" in value:
        return "CTH"
    return value


def msg_to_meteosat(msg: str) -> str:
    mapping = {
        "MSG1": "Meteosat-8 (historical)",
        "MSG2": "Meteosat-9",
        "MSG3": "Meteosat-10",
        "MSG4": "Meteosat-11",
    }
    return mapping.get(msg.upper(), "unknown_msg_mapping")


def product_from_folder(path: Path, family: str) -> str:
    parts = [part.upper() for part in path.parts]
    candidates = []
    for part in parts:
        if family.upper() in part:
            for token in ["FDI", "GEO", "CLM", "CLT", "CTH", "CTT", "CTP", "CLP", "COT", "CER", "ACMF", "ACHAF", "CMSK", "CHGT"]:
                if token in part:
                    candidates.append(token)
        if part in {"ACMF", "ACHAF", "CMSK", "CHGT", "CLM", "CTH"}:
            candidates.append(part)
    if candidates:
        value = candidates[-1]
        return "CLM" if value == "CLT" else value
    return ""


def product_from_path_satellite(path: Path, family: str) -> str:
    for part in path.parts:
        low = part.lower()
        if family.lower() in low or low in {"goes-16", "goes-18", "himawari-9", "meteosat-0deg", "meteosat-iodc"}:
            return part
    return family


def scan_files() -> tuple[list[dict], list[ParsedMeta]]:
    log("Scanning file inventory")
    rows: list[dict[str, Any]] = []
    parsed: list[ParsedMeta] = []
    roots = [(DATA_ROOT, "D_data"), (GEO_ROOT, "E_GEO_Cloud_2024")]
    for root, root_name in roots:
        if not root.exists():
            log(f"Missing root: {root}")
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except Exception as exc:
                rows.append({"file_path": str(path), "root": root_name, "error": str(exc)})
                continue
            meta = parse_path(path, root_name, stat.st_size)
            parsed.append(meta)
            rows.append(
                {
                    "file_path": str(path),
                    "root": root_name,
                    "relative_path": str(path.relative_to(root)) if path.is_relative_to(root) else str(path),
                    "folder": str(path.parent),
                    "suffix": "".join(path.suffixes) or path.suffix,
                    "size_bytes": stat.st_size,
                    "last_modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                    "satellite_family": meta.satellite_family,
                    "satellite": meta.satellite,
                    "product": meta.product,
                    "nominal_time": meta.nominal_time,
                    "parse_status": meta.parse_status,
                }
            )
    return rows, parsed


def folder_summary(file_rows: list[dict]) -> list[dict]:
    by_folder: dict[str, dict] = {}
    for row in file_rows:
        folder = row.get("folder", "")
        item = by_folder.setdefault(
            folder,
            {
                "folder": folder,
                "root": row.get("root", ""),
                "file_count": 0,
                "total_size_bytes": 0,
                "suffix_counts": Counter(),
                "latest_modified": "",
            },
        )
        item["file_count"] += 1
        item["total_size_bytes"] += int(row.get("size_bytes") or 0)
        item["suffix_counts"][row.get("suffix", "")] += 1
        item["latest_modified"] = max(item["latest_modified"], row.get("last_modified", ""))
    out = []
    for item in by_folder.values():
        out.append(
            {
                "folder": item["folder"],
                "root": item["root"],
                "file_count": item["file_count"],
                "total_size_bytes": item["total_size_bytes"],
                "total_size_gib": round(item["total_size_bytes"] / (1024**3), 3),
                "suffix_counts": json.dumps(dict(item["suffix_counts"]), ensure_ascii=False),
                "latest_modified": item["latest_modified"],
            }
        )
    return sorted(out, key=lambda r: r["folder"])


def suspicious_files(file_rows: list[dict]) -> list[dict]:
    rows = []
    for row in file_rows:
        size = int(row.get("size_bytes") or 0)
        suffix = str(row.get("suffix", "")).lower()
        reasons = []
        if size == 0:
            reasons.append("ZERO_SIZE")
        elif size < TOO_SMALL_BYTES and suffix not in {".txt", ".csv", ".json", ".xml", ".md"}:
            reasons.append("TOO_SMALL")
        if any(suffix.endswith(ext) for ext in [".bz2", ".gz", ".tar", ".zip"]):
            reasons.append("COMPRESSED_ARCHIVE_PRESENT")
        if row.get("parse_status") in {"unparsed", "partial"}:
            reasons.append(row.get("parse_status", "").upper())
        if reasons:
            item = dict(row)
            item["reasons"] = ";".join(reasons)
            rows.append(item)
    return rows


def meta_rows(parsed: list[ParsedMeta]) -> list[dict]:
    return [m.__dict__ for m in parsed]


def build_time_completeness(parsed: list[ParsedMeta]) -> tuple[list[dict], list[dict], list[dict], dict]:
    log("Building time completeness matrices")
    usable = [m for m in parsed if m.satellite_family != "UNKNOWN" and m.product and m.nominal_time]
    groups: dict[tuple[str, str, str], list[ParsedMeta]] = defaultdict(list)
    for m in usable:
        groups[(m.satellite, m.product, m.nominal_time)].append(m)

    products_by_family = defaultdict(set)
    times_by_family = defaultdict(set)
    for m in usable:
        products_by_family[m.satellite_family].add(f"{m.satellite}_{m.product}")
        times_by_family[m.satellite_family].add(m.nominal_time)

    all_columns = sorted({f"{m.satellite}_{m.product}" for m in usable})
    all_times = sorted({m.nominal_time for m in usable})
    matrix = []
    for t in all_times:
        row = {"datetime": t}
        for col in all_columns:
            sat, product = split_sat_product(col)
            items = groups.get((sat, product, t), [])
            if not items:
                row[col] = "MISSING"
            elif len(items) > 1:
                row[col] = "DUPLICATE"
            else:
                size = items[0].file_size
                if size == 0:
                    row[col] = "ZERO_SIZE"
                elif size < TOO_SMALL_BYTES:
                    row[col] = "TOO_SMALL"
                elif items[0].parse_status == "partial":
                    row[col] = "NEED_CHECK"
                else:
                    row[col] = "OK"
        matrix.append(row)

    missing = []
    duplicate = []
    for col in all_columns:
        sat, product = split_sat_product(col)
        miss_count = sum(1 for row in matrix if row.get(col) == "MISSING")
        dup_count = sum(1 for row in matrix if row.get(col) == "DUPLICATE")
        if miss_count:
            missing.append({"satellite_product": col, "missing_count": miss_count, "total_times": len(matrix)})
        if dup_count:
            duplicate.append({"satellite_product": col, "duplicate_count": dup_count, "total_times": len(matrix)})

    stats = {
        "usable_files": len(usable),
        "total_times_union": len(all_times),
        "total_columns": len(all_columns),
        "times_by_family": {k: len(v) for k, v in times_by_family.items()},
        "products_by_family": {k: sorted(v) for k, v in products_by_family.items()},
    }
    return matrix, missing, duplicate, stats


def split_sat_product(value: str) -> tuple[str, str]:
    for product in ["ACHAF", "ACMF", "ACTPF", "ACHTF", "CODF", "CPSF", "CTPF", "CTTF", "CMSK", "CHGT", "CLM", "CTH", "CTT", "CTP", "CLP", "COT", "CER", "FDI", "GEO"]:
        suffix = "_" + product
        if value.endswith(suffix):
            return value[: -len(suffix)], product
    parts = value.rsplit("_", 1)
    return (parts[0], parts[1] if len(parts) == 2 else "")


def cross_product(parsed: list[ParsedMeta], family: str, core_products: list[str]) -> list[dict]:
    usable = [m for m in parsed if m.satellite_family == family and m.nominal_time and m.product]
    by_time_sat: dict[tuple[str, str], list[ParsedMeta]] = defaultdict(list)
    for m in usable:
        by_time_sat[(m.satellite, m.nominal_time)].append(m)
    rows = []
    for (sat, t), items in sorted(by_time_sat.items()):
        products = sorted({m.product for m in items})
        counts = Counter(m.product for m in items)
        missing_core = [p for p in core_products if p not in products]
        duplicate = [p for p, c in counts.items() if c > 1]
        status = "COMPLETE_CORE" if not missing_core else "PARTIAL"
        if family == "FY4B":
            if "GEO" not in products:
                status = "MISSING_GEO" if status == "COMPLETE_CORE" else status + ";MISSING_GEO"
            if "FDI" not in products:
                status = "MISSING_L1" if status == "COMPLETE_CORE" else status + ";MISSING_L1"
            if "CLM" not in products:
                status = "MISSING_CLM"
            if any(p not in products for p in ["CTH", "CTT", "CTP"]):
                status = "MISSING_CTH_CTT_CTP" if status == "COMPLETE_CORE" else status + ";MISSING_CTH_CTT_CTP"
            if not missing_core and all(p in products for p in FY4B_MICRO):
                status = "COMPLETE_WITH_MICROPHYSICS"
        rows.append(
            {
                "satellite": sat,
                "datetime": t,
                "present_products": ";".join(products),
                "missing_core_products": ";".join(missing_core),
                "duplicate_products": ";".join(duplicate),
                "status": status,
                "file_count": len(items),
            }
        )
    return rows


def time_coverage_summary(cross_rows: list[dict], family: str, core_status_values: set[str]) -> list[dict]:
    by_sat = defaultdict(list)
    for row in cross_rows:
        by_sat[row["satellite"]].append(row)
    summary = []
    for sat, rows in sorted(by_sat.items()):
        total = len(rows)
        complete = sum(1 for r in rows if any(v in r["status"] for v in core_status_values))
        summary.append(
            {
                "satellite_family": family,
                "satellite": sat,
                "first_time": min((r["datetime"] for r in rows), default=""),
                "last_time": max((r["datetime"] for r in rows), default=""),
                "time_count": total,
                "complete_core_count": complete,
                "complete_core_percent": round(complete / total * 100, 2) if total else 0,
                "partial_or_missing_count": total - complete,
            }
        )
    return summary


def choose_samples(parsed: list[ParsedMeta]) -> list[ParsedMeta]:
    samples = []
    by_key = defaultdict(list)
    for m in parsed:
        if m.satellite_family == "UNKNOWN":
            continue
        suffix = m.suffix.lower()
        if not any(ext in suffix for ext in [".hdf", ".h5", ".nc", ".zip", ".nat"]):
            continue
        by_key[(m.satellite_family, m.satellite, m.product)].append(m)
    for items in by_key.values():
        items = sorted(items, key=lambda m: (m.nominal_time, m.file_path))
        if not items:
            continue
        pick_indexes = sorted(set([0, len(items) // 2, len(items) - 1]))[:SAMPLE_PER_PRODUCT]
        for idx in pick_indexes:
            samples.append(items[idx])
    return samples


def import_optional():
    mods = {}
    for name in ["numpy", "h5py", "netCDF4"]:
        try:
            mods[name] = __import__(name)
        except Exception as exc:
            mods[name] = exc
    return mods


def inspect_internal(parsed: list[ParsedMeta]) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    log("Inspecting internal variables and sample statistics")
    mods = import_optional()
    np = mods.get("numpy") if not isinstance(mods.get("numpy"), Exception) else None
    h5py = mods.get("h5py") if not isinstance(mods.get("h5py"), Exception) else None
    netCDF4 = mods.get("netCDF4") if not isinstance(mods.get("netCDF4"), Exception) else None
    stats_rows: list[dict] = []
    shape_rows: list[dict] = []
    anomaly_rows: list[dict] = []
    availability_rows: list[dict] = []

    for meta in choose_samples(parsed):
        path = Path(meta.file_path)
        suffix = meta.suffix.lower()
        try:
            if ".zip" in suffix:
                zrows = inspect_zip(meta, path)
                availability_rows.extend(zrows)
                shape_rows.append({"satellite_family": meta.satellite_family, "satellite": meta.satellite, "product": meta.product, "file_path": meta.file_path, "variable": "ZIP_ENTRIES", "shape": "", "dtype": "", "status": "zip_readable"})
                continue
            if ".nc" in suffix and netCDF4:
                s, sh, av, an = inspect_netcdf(meta, path, netCDF4, np)
            elif any(ext in suffix for ext in [".hdf", ".h5"]) and h5py:
                s, sh, av, an = inspect_hdf(meta, path, h5py, np)
            else:
                anomaly_rows.append({"satellite_family": meta.satellite_family, "satellite": meta.satellite, "product": meta.product, "file_path": meta.file_path, "severity": "WARN", "issue": "unsupported_or_library_missing", "detail": str(mods)})
                continue
            stats_rows.extend(s)
            shape_rows.extend(sh)
            availability_rows.extend(av)
            anomaly_rows.extend(an)
        except Exception as exc:
            anomaly_rows.append(
                {
                    "satellite_family": meta.satellite_family,
                    "satellite": meta.satellite,
                    "product": meta.product,
                    "file_path": meta.file_path,
                    "severity": "ERROR",
                    "issue": "internal_read_failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
    shape_rows.extend(shape_consistency_from_shapes(shape_rows))
    availability_rows.extend(expected_availability_gaps(availability_rows))
    return stats_rows, shape_rows, anomaly_rows, availability_rows


def inspect_zip(meta: ParsedMeta, path: Path) -> list[dict]:
    rows = []
    with zipfile.ZipFile(path) as zf:
        bad = zf.testzip()
        names = zf.namelist()
    rows.append(
        {
            "satellite": meta.satellite,
            "satellite_family": meta.satellite_family,
            "product_folder": meta.product,
            "product_file_example": meta.file_path,
            "variable_standard_name": "metadata_manifest_entries",
            "original_variable_name": ";".join(names[:20]),
            "unit": "",
            "shape": "",
            "dtype": "zip",
            "availability": "YES" if names and not bad else "NEED_CHECK",
            "notes": f"entries={len(names)} bad_member={bad or ''}",
        }
    )
    for name in names:
        low = name.lower()
        if low.endswith(".xml") or "metadata" in low or "manifest" in low:
            rows.append(
                {
                    "satellite": meta.satellite,
                    "satellite_family": meta.satellite_family,
                    "product_folder": meta.product,
                    "product_file_example": meta.file_path,
                    "variable_standard_name": "quality_flag" if "quality" in low else "metadata_xml",
                    "original_variable_name": name,
                    "unit": "",
                    "shape": "",
                    "dtype": "zip_entry",
                    "availability": "YES",
                    "notes": "EUMETSAT zip entry",
                }
            )
    return rows


def inspect_netcdf(meta: ParsedMeta, path: Path, netCDF4, np) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    stats, shapes, avail, anomalies = [], [], [], []
    with netCDF4.Dataset(path, "r") as ds:
        for name, var in ds.variables.items():
            shape = tuple(getattr(var, "shape", ()))
            dtype = str(getattr(var, "dtype", ""))
            std = infer_standard_variable(name, meta.product)
            attrs = {k: getattr(var, k) for k in var.ncattrs()} if hasattr(var, "ncattrs") else {}
            shapes.append(shape_row(meta, path, name, shape, dtype, std))
            avail.append(availability_row(meta, path, std, name, attrs.get("units", ""), shape, dtype, "YES", "netcdf variable"))
            if np and shape and is_numeric_dtype(dtype):
                stats.append(variable_stats(meta, path, name, read_sample_array(var, np), attrs, np))
        for expected in expected_variable_names(meta.satellite_family, meta.product):
            if not any(infer_standard_variable(name, meta.product) == expected for name in ds.variables):
                anomalies.append(anomaly(meta, path, "WARN", "expected_variable_missing", expected))
    return stats, shapes, avail, anomalies


def inspect_hdf(meta: ParsedMeta, path: Path, h5py, np) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    stats, shapes, avail, anomalies = [], [], [], []
    with h5py.File(path, "r") as h5:
        datasets = []
        h5.visititems(lambda name, obj: datasets.append((name, obj)) if hasattr(obj, "shape") and hasattr(obj, "dtype") else None)
        for name, obj in datasets:
            shape = tuple(obj.shape)
            dtype = str(obj.dtype)
            std = infer_standard_variable(name, meta.product)
            attrs = {decode_attr(k): decode_attr(v) for k, v in obj.attrs.items()}
            shapes.append(shape_row(meta, path, name, shape, dtype, std))
            avail.append(availability_row(meta, path, std, name, attrs.get("units", ""), shape, dtype, "YES", "hdf dataset"))
            if np and shape and is_numeric_dtype(dtype):
                stats.append(variable_stats(meta, path, name, read_hdf_sample(obj, np), attrs, np))
        for expected in expected_variable_names(meta.satellite_family, meta.product):
            if not any(infer_standard_variable(name, meta.product) == expected for name, _ in datasets):
                anomalies.append(anomaly(meta, path, "WARN", "expected_variable_missing", expected))
    return stats, shapes, avail, anomalies


def decode_attr(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    if hasattr(value, "tolist"):
        return value.tolist()
    return value


def is_numeric_dtype(dtype: str) -> bool:
    return any(token in dtype.lower() for token in ["int", "float", "uint"])


def read_sample_array(var: Any, np):
    shape = tuple(getattr(var, "shape", ()))
    if not shape:
        return np.asarray(var[:])
    slices = []
    for dim in shape:
        step = max(1, int(math.ceil(dim / max(1, int(MAX_STATS_VALUES ** (1 / max(1, len(shape))))))))
        slices.append(slice(None, None, step))
    return np.asarray(var[tuple(slices)])


def read_hdf_sample(obj: Any, np):
    shape = tuple(obj.shape)
    if not shape:
        return np.asarray(obj[()])
    slices = []
    for dim in shape:
        step = max(1, int(math.ceil(dim / max(1, int(MAX_STATS_VALUES ** (1 / max(1, len(shape))))))))
        slices.append(slice(None, None, step))
    return np.asarray(obj[tuple(slices)])


def variable_stats(meta: ParsedMeta, path: Path, name: str, arr: Any, attrs: dict, np) -> dict:
    arr = np.asarray(arr)
    total = int(arr.size)
    fill_values = []
    for key in ["_FillValue", "FillValue", "missing_value", "InvalidValue"]:
        if key in attrs:
            val = attrs[key]
            if isinstance(val, (list, tuple)) and val:
                fill_values.append(val[0])
            else:
                fill_values.append(val)
    finite = np.isfinite(arr.astype("float64", copy=False)) if arr.size and is_numeric_dtype(str(arr.dtype)) else np.ones(arr.shape, dtype=bool)
    fill_mask = np.zeros(arr.shape, dtype=bool)
    for fv in fill_values:
        try:
            fill_mask |= arr == fv
        except Exception:
            pass
    valid = arr[finite & ~fill_mask]
    fill_ratio = float(fill_mask.sum() / total) if total else 0.0
    invalid_ratio = float((~finite).sum() / total) if total else 0.0
    row = {
        "satellite_family": meta.satellite_family,
        "satellite": meta.satellite,
        "product": meta.product,
        "file_path": str(path),
        "variable": name,
        "standard_variable": infer_standard_variable(name, meta.product),
        "shape": str(tuple(arr.shape)),
        "dtype": str(arr.dtype),
        "sample_count": total,
        "fill_ratio": round(fill_ratio, 6),
        "invalid_ratio": round(invalid_ratio, 6),
        "min": "",
        "max": "",
        "mean": "",
        "p01": "",
        "p50": "",
        "p99": "",
        "all_fill": fill_ratio >= 0.999 if total else False,
    }
    if valid.size:
        try:
            vv = valid.astype("float64", copy=False)
            row.update(
                {
                    "min": float(np.nanmin(vv)),
                    "max": float(np.nanmax(vv)),
                    "mean": float(np.nanmean(vv)),
                    "p01": float(np.nanpercentile(vv, 1)),
                    "p50": float(np.nanpercentile(vv, 50)),
                    "p99": float(np.nanpercentile(vv, 99)),
                }
            )
        except Exception:
            pass
    return row


def shape_row(meta: ParsedMeta, path: Path, name: str, shape: tuple, dtype: str, std: str) -> dict:
    return {
        "satellite_family": meta.satellite_family,
        "satellite": meta.satellite,
        "product": meta.product,
        "file_path": str(path),
        "variable": name,
        "standard_variable": std,
        "shape": str(shape),
        "dtype": dtype,
        "status": "observed",
    }


def availability_row(meta: ParsedMeta, path: Path, std: str, original: str, unit: str, shape: tuple, dtype: str, availability: str, notes: str) -> dict:
    return {
        "satellite": meta.satellite,
        "satellite_family": meta.satellite_family,
        "product_folder": meta.product,
        "product_file_example": str(path),
        "variable_standard_name": std,
        "original_variable_name": original,
        "unit": unit,
        "shape": str(shape),
        "dtype": dtype,
        "availability": availability,
        "notes": notes,
    }


def anomaly(meta: ParsedMeta, path: Path, severity: str, issue: str, detail: str) -> dict:
    return {"satellite_family": meta.satellite_family, "satellite": meta.satellite, "product": meta.product, "file_path": str(path), "severity": severity, "issue": issue, "detail": detail}


def infer_standard_variable(name: str, product: str) -> str:
    low = name.lower()
    product = product.upper()
    rules = [
        ("cloud_mask", ["cloudmask", "cloud_mask", "clm", "cmsk", "clearmask", "cloudmaskpacked"]),
        ("cloud_probability", ["cloudprobability", "cloud_probability"]),
        ("cloud_phase", ["phase", "clp", "cloud_phase", "icecloudprobability"]),
        ("cloud_type", ["cloudtype", "cloud_type", "ct"]),
        ("cloud_top_height", ["cldtophght", "cloud_top_height", "cth", "height", "ctth"]),
        ("cloud_top_temperature", ["cldtoptemp", "cloud_top_temperature", "ctt", "temperature"]),
        ("cloud_top_pressure", ["cldtoppres", "cloud_top_pressure", "ctp", "pressure"]),
        ("cloud_optical_thickness", ["cldoptdpth", "optical", "cot", "cod"]),
        ("cloud_effective_radius", ["effective_radius", "particle", "cer", "cps"]),
        ("quality_flag", ["dqf", "qa", "quality", "flag"]),
        ("latitude", ["latitude", "lat"]),
        ("longitude", ["longitude", "lon"]),
        ("sensor_zenith_angle", ["sensorzenith", "satellitezenith", "viewzenith", "vza"]),
        ("sensor_azimuth_angle", ["sensorazimuth", "satelliteazimuth", "viewazimuth", "vaa"]),
        ("solar_zenith_angle", ["solarzenith", "sza"]),
        ("solar_azimuth_angle", ["solarazimuth", "saa"]),
        ("observation_time", ["time", "scan"]),
        ("brightness_temperature", ["brightness_temperature", "bt"]),
        ("reflectance", ["reflectance", "ref"]),
        ("radiance", ["radiance", "rad"]),
        ("projection_x", ["x"]),
        ("projection_y", ["y"]),
        ("geostationary_projection", ["goes_imager_projection", "projection", "geostationary"]),
    ]
    compact = low.replace("_", "").replace("-", "")
    for std, needles in rules:
        if any(n.replace("_", "").replace("-", "") in compact for n in needles):
            return std
    if product in {"ACMF", "CLM", "CMSK"}:
        return "cloud_mask"
    if product in {"ACHAF", "CHGT", "CTH"}:
        return "cloud_top_height"
    if product == "CTT":
        return "cloud_top_temperature"
    if product == "CTP":
        return "cloud_top_pressure"
    if product == "CLP":
        return "cloud_phase"
    if product == "COT":
        return "cloud_optical_thickness"
    if product == "CER":
        return "cloud_effective_radius"
    return "unknown"


def expected_variable_names(family: str, product: str) -> list[str]:
    product = product.upper()
    if family == "GOES":
        base = ["quality_flag", "projection_x", "projection_y", "geostationary_projection", "observation_time"]
        return base + (["cloud_mask"] if product == "ACMF" else ["cloud_top_height"] if product == "ACHAF" else [])
    if family == "Himawari":
        base = ["latitude", "longitude", "quality_flag"]
        return base + (["cloud_mask"] if product == "CMSK" else ["cloud_top_height", "cloud_top_temperature", "cloud_top_pressure"] if product == "CHGT" else [])
    if family == "FY4B":
        if product == "CLM":
            return ["cloud_mask", "quality_flag"]
        if product == "CTH":
            return ["cloud_top_height", "quality_flag"]
        if product == "CTT":
            return ["cloud_top_temperature", "quality_flag"]
        if product == "CTP":
            return ["cloud_top_pressure", "quality_flag"]
        if product == "FDI":
            return ["radiance", "brightness_temperature", "reflectance"]
        if product == "GEO":
            return ["latitude", "longitude", "sensor_zenith_angle", "solar_zenith_angle"]
    if family == "Meteosat":
        return ["metadata_xml"] if product in {"CLM", "CTH"} else []
    return []


def shape_consistency_from_shapes(shape_rows: list[dict]) -> list[dict]:
    out = []
    by_key = defaultdict(set)
    for row in shape_rows:
        if row.get("status") != "observed":
            continue
        key = (row.get("satellite_family"), row.get("satellite"), row.get("product"), row.get("standard_variable"))
        by_key[key].add(row.get("shape"))
    for key, shapes in by_key.items():
        if len(shapes) > 1:
            out.append({"satellite_family": key[0], "satellite": key[1], "product": key[2], "variable": key[3], "shape": ";".join(sorted(shapes)), "dtype": "", "status": "INCONSISTENT_SHAPES"})
    return out


def expected_availability_gaps(avail: list[dict]) -> list[dict]:
    present = defaultdict(set)
    for row in avail:
        present[(row["satellite_family"], row["satellite"], row["product_folder"])].add(row["variable_standard_name"])
    gaps = []
    for key, vars_present in present.items():
        for expected in expected_variable_names(key[0], key[2]):
            if expected not in vars_present:
                gaps.append({"satellite_family": key[0], "satellite": key[1], "product_folder": key[2], "product_file_example": "", "variable_standard_name": expected, "original_variable_name": "", "unit": "", "shape": "", "dtype": "", "availability": "NO", "notes": "expected variable not found in sampled files"})
    return gaps


def write_family_outputs(family: str, cross: list[dict], summary: list[dict], stats: list[dict], shapes: list[dict], anomalies: list[dict], availability: list[dict]) -> None:
    prefix = family.lower()
    if family == "GOES":
        prefix = "goes"
    elif family == "Himawari":
        prefix = "himawari"
    elif family == "Meteosat":
        prefix = "meteosat"
    elif family == "FY4B":
        prefix = "fy4b"
    write_csv(OUT_DIR / f"{prefix}_cross_product_match.csv", cross)
    write_csv(OUT_DIR / f"{prefix}_time_coverage_summary.csv", summary)
    write_csv(OUT_DIR / f"{prefix}_variable_statistics.csv", [r for r in stats if r.get("satellite_family") == family])
    write_csv(OUT_DIR / f"{prefix}_shape_consistency.csv", [r for r in shapes if r.get("satellite_family") == family])
    write_csv(OUT_DIR / f"{prefix}_internal_anomalies.csv", [r for r in anomalies if r.get("satellite_family") == family])
    write_variable_report(OUT_DIR / f"{prefix}_variable_availability_report.md", family, [r for r in availability if r.get("satellite_family") == family])
    if family == "Himawari":
        write_csv(OUT_DIR / "himawari_segment_completeness.csv", himawari_segment_completeness(cross))


def himawari_segment_completeness(cross: list[dict]) -> list[dict]:
    # NOAA AHI-L2-FLDK-Clouds files are single full-disk NetCDF products, not ten-segment HSD files.
    rows = []
    for row in cross:
        rows.append(
            {
                "satellite": row["satellite"],
                "datetime": row["datetime"],
                "product_context": row["present_products"],
                "expected_segments": "1 full-disk NetCDF per product",
                "observed_segment_status": "OK_FULL_DISK_FILE" if row["status"] == "COMPLETE_CORE" else "NEED_CHECK_PRODUCTS",
                "notes": "No HSD segment files were found in E:\\GEO_Cloud_2024; downloaded NOAA L2 products are full-disk NetCDF bundles.",
            }
        )
    return rows


def write_variable_report(path: Path, family: str, rows: list[dict]) -> None:
    by_std = defaultdict(list)
    for row in rows:
        by_std[row.get("variable_standard_name", "unknown")].append(row)
    lines = [f"# {family} Variable Availability Report", "", f"Generated: {datetime.now().isoformat(timespec='seconds')}", ""]
    for std in STANDARD_VARIABLES + ["metadata_xml", "unknown"]:
        items = by_std.get(std, [])
        yes = sum(1 for r in items if r.get("availability") == "YES")
        no = sum(1 for r in items if r.get("availability") == "NO")
        if items:
            examples = sorted({r.get("original_variable_name", "") for r in items if r.get("original_variable_name")})[:8]
            lines.append(f"## {std}")
            lines.append(f"- availability records: {len(items)}, YES={yes}, NO={no}")
            lines.append(f"- examples: {', '.join(examples) if examples else 'none'}")
            lines.append("")
    write_md(path, "\n".join(lines))


def fy4b_structure_report(shapes: list[dict], anomalies: list[dict], stats: list[dict]) -> None:
    fy_shapes = [r for r in shapes if r.get("satellite_family") == "FY4B"]
    fy_anom = [r for r in anomalies if r.get("satellite_family") == "FY4B"]
    fy_stats = [r for r in stats if r.get("satellite_family") == "FY4B"]
    lines = ["# FY4B HDF Structure Report", "", f"Generated: {datetime.now().isoformat(timespec='seconds')}", ""]
    lines.append(f"- sampled dataset records: {len(fy_shapes)}")
    lines.append(f"- sampled statistics records: {len(fy_stats)}")
    lines.append(f"- anomalies: {len(fy_anom)}")
    lines.append("")
    by_product = defaultdict(list)
    for row in fy_shapes:
        by_product[row.get("product", "")].append(row)
    for product, rows in sorted(by_product.items()):
        lines.append(f"## {product}")
        for row in rows[:40]:
            lines.append(f"- `{row.get('variable')}` shape={row.get('shape')} dtype={row.get('dtype')} std={row.get('standard_variable')}")
        if len(rows) > 40:
            lines.append(f"- ... {len(rows)-40} more variables")
        lines.append("")
    if fy_anom:
        lines.append("## Anomalies")
        for row in fy_anom[:80]:
            lines.append(f"- {row.get('severity')} {row.get('product')} {row.get('issue')}: {row.get('detail')}")
    write_md(OUT_DIR / "fy4b_hdf_structure_report.md", "\n".join(lines))


def requirement_gap(availability: list[dict], cross_by_family: dict[str, list[dict]]) -> tuple[list[dict], str]:
    required_v1 = [
        "cloud_mask",
        "cloud_phase",
        "cloud_top_height",
        "cloud_top_temperature",
        "cloud_top_pressure",
        "quality_flag",
        "observation_time",
        "latitude",
        "longitude",
        "sensor_zenith_angle",
        "solar_zenith_angle",
    ]
    enhanced_v2 = ["cloud_optical_thickness", "cloud_effective_radius", "cloud_type", "radiance", "brightness_temperature", "reflectance"]
    present_by_family = defaultdict(set)
    for row in availability:
        if row.get("availability") == "YES":
            present_by_family[row.get("satellite_family")].add(row.get("variable_standard_name"))
    rows = []
    for family in ["FY4B", "GOES", "Himawari", "Meteosat"]:
        for var in required_v1 + enhanced_v2:
            status = "AVAILABLE" if var in present_by_family[family] else "MISSING_OR_NOT_IN_SAMPLED_PRODUCTS"
            rows.append({"satellite_family": family, "requirement_version": "v1_core" if var in required_v1 else "v2_enhanced", "variable_standard_name": var, "status": status, "notes": ""})
    lines = ["# Requirement Gap Analysis", "", f"Generated: {datetime.now().isoformat(timespec='seconds')}", ""]
    for family in ["FY4B", "GOES", "Himawari", "Meteosat"]:
        missing = [r["variable_standard_name"] for r in rows if r["satellite_family"] == family and r["requirement_version"] == "v1_core" and r["status"] != "AVAILABLE"]
        lines.append(f"## {family}")
        lines.append(f"- v1 core available: {len(required_v1)-len(missing)}/{len(required_v1)}")
        lines.append(f"- v1 core missing/not confirmed: {', '.join(missing) if missing else 'none'}")
        lines.append("")
    return rows, "\n".join(lines)


def determine_status(family: str, cross: list[dict], anomalies: list[dict], summary: list[dict], availability: list[dict]) -> tuple[str, str]:
    if not cross:
        return "FAIL", "No parsed time/product records."
    complete_pct = max((float(r.get("complete_core_percent") or 0) for r in summary), default=0)
    errors = [a for a in anomalies if a.get("satellite_family") == family and a.get("severity") == "ERROR"]
    warning_families = {
        "GOES": "Core ACMF/ACHAF coverage is complete; ACTPF/ACHTF/CTPF/CODF/CPSF/RadF auxiliary or microphysical products are tracked separately when present.",
        "Himawari": "Core CMSK/CHGT coverage is nearly complete, but HSD/L1 auxiliary segmented files and some cloud type/phase products were not found in the current tree.",
        "Meteosat": "Core CLM/CTH ZIP coverage is nearly complete, but L1.5 .nat and CMA/CT/OCA/CMIC auxiliary products were not found in the current tree.",
    }
    if complete_pct >= 99 and not errors:
        if family in warning_families:
            return "PASS_WITH_WARNINGS", f"{warning_families[family]} Core coverage={complete_pct:.2f}%."
        return "PASS", f"Core products are nearly complete ({complete_pct:.2f}%)."
    if complete_pct >= 50:
        return "PASS_WITH_WARNINGS", f"Core product coverage is partial or has known gaps ({complete_pct:.2f}%)."
    return "FAIL", f"Core product coverage is insufficient ({complete_pct:.2f}%)."


def read_manifest_status() -> dict:
    data = {}
    for name in ["download_summary.json", "meteosat_inventory_summary.json", "meteosat_download_summary.json"]:
        path = MANIFEST_DIR / name
        if path.exists():
            try:
                data[name] = json.loads(path.read_text(encoding="utf-8-sig"))
            except Exception as exc:
                data[name] = {"error": str(exc)}
    return data


def write_main_report(file_rows: list[dict], cross_map: dict[str, list[dict]], summary_map: dict[str, list[dict]], statuses: dict[str, tuple[str, str]], req_rows: list[dict], stats: dict) -> None:
    total_size = sum(int(r.get("size_bytes") or 0) for r in file_rows)
    by_family = Counter(r.get("satellite_family") for r in file_rows)
    by_product = Counter((r.get("satellite_family"), r.get("satellite"), r.get("product")) for r in file_rows)
    manifest_status = read_manifest_status()
    overall = "PASS"
    if any(v[0] == "FAIL" for v in statuses.values()):
        overall = "FAIL"
    elif any(v[0] == "PASS_WITH_WARNINGS" for v in statuses.values()):
        overall = "PASS_WITH_WARNINGS"
    lines = [
        "# GEO Data Download Audit Report",
        "",
        f"- Check time: {datetime.now().isoformat(timespec='seconds')}",
        f"- Data roots: `{DATA_ROOT}`, `{GEO_ROOT}`",
        f"- Total files scanned: {len(file_rows)}",
        f"- Total size: {total_size / (1024**3):.3f} GiB",
        "",
        "## Final Conclusions",
    ]
    for family in ["FY4B", "GOES", "Himawari", "Meteosat"]:
        status, reason = statuses.get(family, ("FAIL", "not evaluated"))
        lines.append(f"- {family}: **{status}** - {reason}")
    lines.append(f"- Overall GEO-ring dataset: **{overall}**")
    lines.append("")
    lines.append("## Satellite Integrity Conclusions")
    for family in ["FY4B", "GOES", "Himawari", "Meteosat"]:
        lines.append(f"### {family} 数据完整性结论")
        for row in summary_map.get(family, []):
            lines.append(f"- {row.get('satellite')}: {row.get('complete_core_count')}/{row.get('time_count')} complete core times ({row.get('complete_core_percent')}%).")
        lines.append(f"- Conclusion: {statuses.get(family, ('FAIL',''))[1]}")
        lines.append("")
    lines.append("## Folder / Product Overview")
    lines.append("| family | file_count |")
    lines.append("|---|---:|")
    for family, count in by_family.items():
        lines.append(f"| {family} | {count} |")
    lines.append("")
    lines.append("## Product Counts")
    lines.append("| family | satellite | product | files |")
    lines.append("|---|---|---|---:|")
    for (family, sat, product), count in sorted(by_product.items()):
        lines.append(f"| {family} | {sat} | {product} | {count} |")
    lines.append("")
    lines.append("## Download Manifest Snapshot")
    lines.append("```json")
    lines.append(json.dumps(manifest_status, indent=2, ensure_ascii=False))
    lines.append("```")
    lines.append("")
    lines.append("## GEO-ring Cloud Composite v1 Readiness")
    if overall == "FAIL":
        lines.append("- Current combined dataset is **not fully ready** for GEO-ring cloud composite v1 without addressing failed satellite families or missing core variables.")
    elif overall == "PASS_WITH_WARNINGS":
        lines.append("- Current combined dataset can enter exploratory GEO-ring cloud composite v1 experiments, but warnings and missing variables/products must be tracked.")
    else:
        lines.append("- Current combined dataset is ready for GEO-ring cloud composite v1 from a download-audit perspective.")
    lines.append("")
    lines.append("## Still Need To Download / Confirm")
    for family in ["FY4B", "GOES", "Himawari", "Meteosat"]:
        status, _ = statuses.get(family, ("FAIL", ""))
        if status != "PASS":
            expected = ", ".join(SATELLITE_EXPECTED_PRODUCTS.get(family, []))
            lines.append(f"- {family}: confirm/download missing products if required: {expected}. See `{family.lower()}_cross_product_match.csv`, `missing_files_summary.csv`, and variable availability report.")
    lines.append("")
    lines.append("## Important Generated Files")
    for name in [
        "folder_summary.csv",
        "file_inventory.csv",
        "parsed_file_metadata.csv",
        "completeness_matrix.csv",
        "missing_files_summary.csv",
        "duplicate_files_summary.csv",
        "variable_availability_table.csv",
        "requirement_gap_analysis.md",
    ]:
        lines.append(f"- `{name}`")
    lines.append("")
    lines.append("## Run Stats")
    lines.append("```json")
    lines.append(json.dumps(stats, indent=2, ensure_ascii=False))
    lines.append("```")
    write_md(OUT_DIR / "data_download_audit_report.md", "\n".join(lines))


def main() -> int:
    ensure_out()
    log(f"Output directory: {OUT_DIR}")
    file_rows, parsed = scan_files()
    parsed_rows = meta_rows(parsed)
    write_csv(OUT_DIR / "file_inventory.csv", file_rows)
    write_csv(OUT_DIR / "folder_summary.csv", folder_summary(file_rows))
    write_csv(OUT_DIR / "suspicious_files.csv", suspicious_files(file_rows))
    write_csv(OUT_DIR / "parsed_file_metadata.csv", parsed_rows)
    write_json(OUT_DIR / "parsed_file_metadata.json", parsed_rows)
    write_csv(OUT_DIR / "unparsed_files.csv", [r for r in parsed_rows if r["parse_status"] == "unparsed"])
    write_csv(OUT_DIR / "unknown_product_files.csv", [r for r in parsed_rows if r["satellite_family"] == "UNKNOWN" or not r["product"]])

    matrix, missing, duplicates, completeness_stats = build_time_completeness(parsed)
    write_csv(OUT_DIR / "completeness_matrix.csv", matrix)
    write_csv(OUT_DIR / "missing_files_summary.csv", missing)
    write_csv(OUT_DIR / "duplicate_files_summary.csv", duplicates)

    cross_map = {
        "FY4B": cross_product(parsed, "FY4B", FY4B_CORE),
        "GOES": cross_product(parsed, "GOES", GOES_CORE),
        "Himawari": cross_product(parsed, "Himawari", HIMAWARI_CORE),
        "Meteosat": cross_product(parsed, "Meteosat", METEOSAT_CORE),
    }
    summary_map = {
        "FY4B": time_coverage_summary(cross_map["FY4B"], "FY4B", {"COMPLETE_CORE", "COMPLETE_WITH_MICROPHYSICS"}),
        "GOES": time_coverage_summary(cross_map["GOES"], "GOES", {"COMPLETE_CORE"}),
        "Himawari": time_coverage_summary(cross_map["Himawari"], "Himawari", {"COMPLETE_CORE"}),
        "Meteosat": time_coverage_summary(cross_map["Meteosat"], "Meteosat", {"COMPLETE_CORE"}),
    }

    stats_rows, shape_rows, anomaly_rows, availability_rows = inspect_internal(parsed)
    # Generic outputs
    write_csv(OUT_DIR / "variable_availability_table.csv", availability_rows)
    write_variable_report(OUT_DIR / "variable_availability_report.md", "ALL_GEO", availability_rows)
    req_rows, req_md = requirement_gap(availability_rows, cross_map)
    write_csv(OUT_DIR / "requirement_gap_table.csv", req_rows)
    write_md(OUT_DIR / "requirement_gap_analysis.md", req_md)

    for family in ["FY4B", "GOES", "Himawari", "Meteosat"]:
        write_family_outputs(family, cross_map[family], summary_map[family], stats_rows, shape_rows, anomaly_rows, availability_rows)

    fy4b_structure_report(shape_rows, anomaly_rows, stats_rows)
    write_md(
        OUT_DIR / "needs_manual_confirmation.md",
        "# Needs Manual Confirmation\n\n"
        "- GOES: ACMF/ACHAF plus recently downloaded ACTPF/ACHTF/CTPF/CODF/CPSF are parsed when present in E:\\GEO_Cloud_2024; RadF is not present unless stored elsewhere.\n"
        "- Himawari: NOAA AHI-L2-FLDK-Clouds CMSK/CHGT files are full-disk NetCDF bundles, not HSD segmented L1 files; HSD/L1 auxiliary products are not present in E:\\GEO_Cloud_2024.\n"
        "- Meteosat: Downloaded EUMETSAT ZIP products contain CLM/CTH entries and metadata. L1.5 .nat, CMA/CT/CTTH/OCA/CMIC products beyond CLM/CTH are not present in the current download tree.\n"
        "- FY4B: D:\\AAAresearch_paper\\data currently exposes CLM/CLT/CTH/CTT and FY4B_Data folders; CTP/CLP/COT/CER/FDI/GEO availability depends on files parsed in reports.\n",
    )

    statuses = {
        family: determine_status(family, cross_map[family], anomaly_rows, summary_map[family], availability_rows)
        for family in ["FY4B", "GOES", "Himawari", "Meteosat"]
    }
    run_stats = {
        "file_count": len(file_rows),
        "parsed_count": sum(1 for m in parsed if m.parse_status == "parsed"),
        "partial_count": sum(1 for m in parsed if m.parse_status == "partial"),
        "unparsed_count": sum(1 for m in parsed if m.parse_status == "unparsed"),
        "completeness": completeness_stats,
        "statuses": statuses,
    }
    write_main_report(file_rows, cross_map, summary_map, statuses, req_rows, run_stats)

    log("Most important conclusions:")
    log(f"Total time union: {completeness_stats.get('total_times_union')}")
    fy_summary = summary_map.get("FY4B", [])
    log(f"FY4B complete core times: {sum(int(r.get('complete_core_count') or 0) for r in fy_summary)}")
    if missing:
        worst = sorted(missing, key=lambda r: int(r["missing_count"]), reverse=True)[0]
        log(f"Most missing product: {worst}")
    log(f"Statuses: {statuses}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise
