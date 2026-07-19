from __future__ import annotations

import argparse
import calendar
import csv
import json
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import h5py
import netCDF4
import numpy as np
import requests

CORE_CODE_ROOT = Path(__file__).resolve().parents[1] / "geo_ring_cloud_stage1"
if str(CORE_CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_CODE_ROOT))

from geo_ring_cloud.paths import (  # noqa: E402
    DATA_CHECK_ROOT,
    EUMETSAT_CREDENTIALS_FILE,
    EXTERNAL_GEO_CLOUD_ROOT,
)

try:
    import eccodes  # type: ignore
except Exception:
    eccodes = None


OUT_DIR = DATA_CHECK_ROOT / "meteosat_priority_cloud_download"
CRED_FILE = EUMETSAT_CREDENTIALS_FILE
DEFAULT_PROXY = "http://127.0.0.1:7897"
SEARCH_URL = "https://api.eumetsat.int/data/search-products/1.0.0/os"
BROWSE_COLLECTION_URL = "https://api.eumetsat.int/data/browse/1.0.0/collections/{collection}"
DOWNLOAD_URL = "https://api.eumetsat.int/data/download/1.0.0/collections/{collection}/products/{product}"

TARGET_COLLECTIONS = [
    {
        "priority": "P0",
        "collection_id": "EO:EUM:DAT:0573",
        "title": "Geostationary Nowcasting Cloud Type - MSG - Indian Ocean",
        "expected": ["cloud_type"],
        "notes": "IODC nowcasting cloud type; phase-like class may be encoded in cloud type table.",
    },
    {
        "priority": "P0",
        "collection_id": "EO:EUM:DAT:0574",
        "title": "Geostationary Nowcasting Cloud Top Temperature and Height - MSG - Indian Ocean",
        "expected": ["cloud_top_height", "cloud_top_temperature"],
        "notes": "IODC nowcasting CTTH; CTP is checked but may not be present depending on product definition.",
    },
    {
        "priority": "P0",
        "collection_id": "EO:EUM:DAT:0575",
        "title": "Geostationary Nowcasting Cloud Microphysics - MSG - Indian Ocean",
        "expected": ["cloud_optical_thickness", "cloud_effective_radius"],
        "notes": "IODC nowcasting microphysics; phase is checked as optional.",
    },
    {
        "priority": "P0",
        "collection_id": "EO:EUM:DAT:MSG:OCA",
        "title": "Optimal Cloud Analysis - MSG - 0 degree",
        "expected": ["cloud_optical_thickness", "cloud_effective_radius"],
        "notes": "0 degree OCA sanity target.",
    },
    {
        "priority": "P0",
        "collection_id": "EO:EUM:DAT:MSG:OCA-IODC",
        "title": "Optimal Cloud Analysis - MSG - Indian Ocean",
        "expected": ["cloud_optical_thickness", "cloud_effective_radius"],
        "notes": "IODC OCA sanity target.",
    },
    {
        "priority": "P1",
        "collection_id": "EO:EUM:DAT:0572",
        "title": "Geostationary Nowcasting Cloud Mask - MSG - Indian Ocean",
        "expected": ["cloud_mask"],
        "notes": "IODC nowcasting cloud mask; lower priority because CLM already exists.",
    },
    {
        "priority": "P2",
        "collection_id": "EO:EUM:DAT:0059",
        "title": "Europe/North-Atlantic Cloud Top Temperature and Height",
        "expected": ["cloud_top_height", "cloud_top_temperature"],
        "notes": "Regional reference only; not a full-disk/IODC primary input.",
    },
    {
        "priority": "P2",
        "collection_id": "EO:EUM:DAT:0060",
        "title": "Europe/North-Atlantic Cloud Type",
        "expected": ["cloud_type"],
        "notes": "Regional reference only; not a full-disk/IODC primary input.",
    },
    {
        "priority": "P2",
        "collection_id": "EO:EUM:DAT:0061",
        "title": "Europe/North-Atlantic Cloud Mask",
        "expected": ["cloud_mask"],
        "notes": "Regional reference only; not a full-disk/IODC primary input.",
    },
]

PRIMARY_WINDOWS = [
    ("2024-03-12_0000_near", "2024-03-11T23:30:00Z", "2024-03-12T00:30:00Z"),
]
FALLBACK_WINDOWS = [
    ("2024-03-01_0000_near", "2024-02-29T23:30:00Z", "2024-03-01T00:30:00Z"),
    ("2024-03-12_0000_near", "2024-03-11T23:30:00Z", "2024-03-12T00:30:00Z"),
    ("2024-03-31_0000_near", "2024-03-30T23:30:00Z", "2024-03-31T00:30:00Z"),
]

VARIABLE_PATTERNS = {
    "cloud_mask": ["cloud mask", "cloud_mask", "cma", "clm", "mask"],
    "cloud_type": ["cloud type", "cloud_type", "ct ", "ct-", "ct_"],
    "cloud_phase": ["cloud phase", "phase", "ice/liquid", "liquid/ice"],
    "cloud_top_height": ["cloud top height", "cth", "ctth", "height", "ctoph"],
    "cloud_top_temperature": ["cloud top temperature", "ctt", "temperature", "ctot"],
    "cloud_top_pressure": ["cloud top pressure", "ctp", "pressure"],
    "cloud_optical_thickness": ["cloud optical thickness", "optical thickness", "optical depth", "cot", "cod"],
    "cloud_effective_radius": ["effective radius", "particle effective radius", "cloud effective radius", "cer", "cre"],
    "quality_flag": ["quality", "quality flag", "confidence", "status", "dqf", "flag"],
    "uncertainty": ["uncertainty", "error", "standard deviation"],
    "latitude": ["latitude", "lat"],
    "longitude": ["longitude", "lon"],
    "projection": ["projection", "grid mapping", "geostationary", "space_view"],
}

MAX_STATS_VALUES = 250_000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ensure_proxy_env() -> None:
    if not any(os.environ.get(k) for k in ["HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"]):
        os.environ["HTTPS_PROXY"] = DEFAULT_PROXY
        os.environ["https_proxy"] = DEFAULT_PROXY
        os.environ["HTTP_PROXY"] = DEFAULT_PROXY
        os.environ["http_proxy"] = DEFAULT_PROXY


def proxy_dict() -> dict[str, str]:
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or DEFAULT_PROXY
    return {"http": proxy, "https": proxy} if proxy else {}


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def read_credentials() -> tuple[str, str]:
    key = os.environ.get("EUMETSAT_CONSUMER_KEY", "").strip()
    secret = os.environ.get("EUMETSAT_CONSUMER_SECRET", "").strip()
    text = CRED_FILE.read_text(encoding="utf-8", errors="ignore") if CRED_FILE.exists() else ""
    for line in text.splitlines():
        if "=" in line:
            name, value = line.split("=", 1)
        elif ":" in line:
            name, value = line.split(":", 1)
        else:
            continue
        low = name.lower()
        if "consumer" in low and "key" in low:
            key = value.strip().strip("'\"")
        if "consumer" in low and "secret" in low:
            secret = value.strip().strip("'\"")
    if not key or not secret:
        raise RuntimeError("EUMETSAT credentials not found")
    return key, secret


def get_token() -> str:
    key, secret = read_credentials()
    last = ""
    for attempt in range(6):
        try:
            response = requests.post(
                "https://api.eumetsat.int/token",
                auth=(key, secret),
                data={"grant_type": "client_credentials"},
                timeout=60,
                proxies=proxy_dict(),
            )
            response.raise_for_status()
            token = response.json().get("access_token")
            if not token:
                raise RuntimeError("missing access_token")
            return str(token)
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(4 * (attempt + 1))
    raise RuntimeError(f"token_failed: {last}")


def api_get_json(url: str, token: str, params: dict[str, Any]) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"}
    last = ""
    for attempt in range(4):
        try:
            response = requests.get(url, headers=headers, params=params, timeout=80, proxies=proxy_dict())
            out = {"http_status": response.status_code, "url": response.url}
            if response.status_code == 404:
                out["error"] = response.text[:500]
                return out
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                payload.update(out)
                return payload
            return out | {"payload": payload}
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(3 * (attempt + 1))
    return {"http_status": "ERROR", "error": last, "url": url}


def browse_collection(collection_id: str, token: str) -> dict[str, Any]:
    url = BROWSE_COLLECTION_URL.format(collection=quote(collection_id, safe=""))
    return api_get_json(url, token, {"format": "json"})


def download_product(collection_id: str, product_id: str, token: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        return
    tmp = path.with_suffix(path.suffix + ".part")
    url = DOWNLOAD_URL.format(collection=quote(collection_id, safe=""), product=quote(product_id, safe=""))
    headers = {"Authorization": f"Bearer {token}"}
    last = ""
    for attempt in range(5):
        try:
            with requests.get(url, headers=headers, timeout=180, proxies=proxy_dict(), stream=True) as response:
                response.raise_for_status()
                with tmp.open("wb") as out:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            out.write(chunk)
            if tmp.stat().st_size <= 0:
                raise RuntimeError("downloaded_zero_bytes")
            tmp.replace(path)
            return
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            try:
                tmp.unlink()
            except OSError:
                pass
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"download_failed: {last}")


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
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def safe_json(value: Any, limit: int = 2000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return text[:limit]


def safe_name(value: str, limit: int = 180) -> str:
    return re.sub(r"[^A-Za-z0-9._+=-]+", "_", value).strip("_")[:limit] or "product"


def search_collection(collection_id: str, token: str, start: str, end: str, count: int = 20) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    params = {"format": "json", "pi": collection_id, "si": 0, "c": count, "dtstart": start, "dtend": end}
    payload = api_get_json(SEARCH_URL, token, params)
    features = payload.get("features", []) if isinstance(payload, dict) else []
    return features if isinstance(features, list) else [], payload


def feature_time(feature: dict[str, Any]) -> datetime | None:
    props = feature.get("properties", {}) if isinstance(feature, dict) else {}
    for key in ["date", "startDate", "sensingStart", "sensing_start"]:
        value = props.get(key) or feature.get(key)
        if isinstance(value, str):
            try:
                return parse_iso(value)
            except Exception:
                continue
    pid = str(feature.get("id", ""))
    m = re.search(r"(20\d{12})", pid)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def pick_nearest(features: list[dict[str, Any]], target: datetime) -> dict[str, Any] | None:
    if not features:
        return None
    def score(feature: dict[str, Any]) -> float:
        t = feature_time(feature)
        return abs((t - target).total_seconds()) if t else 1e18
    return sorted(features, key=score)[0]


def sample_availability(token: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    picked: dict[str, dict[str, Any]] = {}
    browse_by_id = {item["collection_id"]: browse_collection(item["collection_id"], token) for item in TARGET_COLLECTIONS}
    target = parse_iso("2024-03-12T00:00:00Z")
    for item in TARGET_COLLECTIONS:
        cid = item["collection_id"]
        browse = browse_by_id.get(cid, {})
        found = False
        for window_name, start, end in PRIMARY_WINDOWS + FALLBACK_WINDOWS:
            if found and window_name != "2024-03-12_0000_near":
                continue
            features, payload = search_collection(cid, token, start, end, 20)
            sample = pick_nearest(features, target)
            product_id = str(sample.get("id", "")) if sample else ""
            if sample and cid not in picked:
                picked[cid] = sample
                found = True
            rows.append(
                {
                    "priority": item["priority"],
                    "collection_id": cid,
                    "title": item["title"],
                    "window": window_name,
                    "dtstart": start,
                    "dtend": end,
                    "available": bool(features),
                    "feature_count": len(features),
                    "sample_product_id": product_id,
                    "sample_time": feature_time(sample).isoformat().replace("+00:00", "Z") if sample and feature_time(sample) else "",
                    "browse_http_status": browse.get("http_status", ""),
                    "browse_title": browse.get("datasetTitle", browse.get("title", "")),
                    "browse_error": browse.get("error", ""),
                    "http_status": payload.get("http_status", ""),
                    "query_url": payload.get("url", ""),
                    "error": payload.get("error", ""),
                    "notes": item["notes"],
                }
            )
            if found:
                break
    return rows, picked


def array_stats(data: Any) -> dict[str, Any]:
    out = {"min": "", "max": "", "mean": ""}
    try:
        arr = np.asarray(data)
        if arr.size == 0 or arr.dtype.kind in {"S", "U", "O"}:
            return out
        flat = arr.reshape(-1)
        if flat.size > MAX_STATS_VALUES:
            step = max(1, flat.size // MAX_STATS_VALUES)
            flat = flat[::step][:MAX_STATS_VALUES]
        flat = flat.astype("float64", copy=False)
        good = flat[np.isfinite(flat)]
        if good.size:
            out["min"] = float(np.nanmin(good))
            out["max"] = float(np.nanmax(good))
            out["mean"] = float(np.nanmean(good))
    except Exception as exc:
        out["stats_error"] = f"{type(exc).__name__}: {exc}"
    return out


def infer_flags(text: str) -> dict[str, bool]:
    low = text.lower()
    return {key: any(pattern in low for pattern in patterns) for key, patterns in VARIABLE_PATTERNS.items()}


def inspect_grib_bytes(data: bytes, entry: str, base: dict[str, Any]) -> list[dict[str, Any]]:
    if eccodes is None:
        return [{**base, "entry": entry, "format": "GRIB", "status": "ECCODES_UNAVAILABLE"}]
    rows = []
    with tempfile.NamedTemporaryFile(delete=False, suffix=".grb") as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        with tmp_path.open("rb") as fh:
            idx = 0
            while True:
                gid = eccodes.codes_grib_new_from_file(fh)
                if gid is None:
                    break
                idx += 1
                row = {**base, "entry": entry, "format": "GRIB", "message_index": idx, "status": "OK"}
                for key in ["shortName", "name", "paramId", "typeOfLevel", "level", "gridType", "Nx", "Ny", "units"]:
                    try:
                        row[key] = eccodes.codes_get(gid, key)
                    except Exception:
                        row[key] = ""
                row["shape"] = f"{row.get('Ny', '')}x{row.get('Nx', '')}".strip("x")
                row["variable_name"] = row.get("shortName", "")
                row["dtype"] = "GRIB_PACKED"
                try:
                    row.update(array_stats(eccodes.codes_get_values(gid)))
                except Exception as exc:
                    row["stats_error"] = f"{type(exc).__name__}: {exc}"
                eccodes.codes_release(gid)
                text = " ".join(str(row.get(k, "")) for k in ["variable_name", "shortName", "name", "paramId", "units", "gridType"])
                row.update({f"has_{k}": v for k, v in infer_flags(text).items()})
                rows.append(row)
    except Exception as exc:
        rows.append({**base, "entry": entry, "format": "GRIB", "status": f"GRIB_ERROR: {type(exc).__name__}: {exc}"})
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass
    return rows


def inspect_netcdf_path(path: Path, entry: str, base: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    ds = netCDF4.Dataset(path)
    try:
        rows.append({**base, "entry": entry, "format": "NetCDF", "variable_name": "__GLOBAL_ATTRS__", "attributes_json": safe_json({k: getattr(ds, k) for k in ds.ncattrs()}), "status": "OK"})
        for name, var in ds.variables.items():
            attrs = {k: getattr(var, k) for k in var.ncattrs()}
            row = {
                **base,
                "entry": entry,
                "format": "NetCDF",
                "variable_name": name,
                "shortName": "",
                "name": attrs.get("long_name", attrs.get("standard_name", "")),
                "paramId": "",
                "shape": "x".join(str(x) for x in getattr(var, "shape", ())),
                "dtype": str(getattr(var, "dtype", "")),
                "units": attrs.get("units", ""),
                "fill_value": attrs.get("_FillValue", attrs.get("missing_value", "")),
                "valid_range": attrs.get("valid_range", f"{attrs.get('valid_min', '')},{attrs.get('valid_max', '')}".strip(",")),
                "attributes_json": safe_json(attrs),
                "status": "OK",
            }
            try:
                row.update(array_stats(var[:]))
            except Exception as exc:
                row["stats_error"] = f"{type(exc).__name__}: {exc}"
            text = " ".join(str(row.get(k, "")) for k in ["variable_name", "name", "units", "attributes_json"])
            row.update({f"has_{k}": v for k, v in infer_flags(text).items()})
            rows.append(row)
    finally:
        ds.close()
    return rows


def inspect_hdf_path(path: Path, entry: str, base: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    with h5py.File(path, "r") as h5:
        rows.append({**base, "entry": entry, "format": "HDF5", "variable_name": "__GLOBAL_ATTRS__", "attributes_json": safe_json({k: h5.attrs.get(k) for k in h5.attrs.keys()}), "status": "OK"})
        def visitor(name: str, obj: Any) -> None:
            if not hasattr(obj, "shape") or not hasattr(obj, "dtype"):
                return
            attrs = {k: obj.attrs.get(k) for k in obj.attrs.keys()}
            row = {
                **base,
                "entry": entry,
                "format": "HDF5",
                "variable_name": name,
                "shortName": "",
                "name": attrs.get("long_name", attrs.get("description", "")),
                "paramId": "",
                "shape": "x".join(str(x) for x in obj.shape),
                "dtype": str(obj.dtype),
                "units": attrs.get("units", ""),
                "fill_value": attrs.get("_FillValue", attrs.get("FillValue", "")),
                "valid_range": attrs.get("valid_range", ""),
                "attributes_json": safe_json(attrs),
                "status": "OK",
            }
            try:
                row.update(array_stats(obj[...]))
            except Exception as exc:
                row["stats_error"] = f"{type(exc).__name__}: {exc}"
            text = " ".join(str(row.get(k, "")) for k in ["variable_name", "name", "units", "attributes_json"])
            row.update({f"has_{k}": v for k, v in infer_flags(text).items()})
            rows.append(row)
        h5.visititems(visitor)
    return rows


def inspect_file(path: Path, base: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                entries = zf.infolist()
                rows.append(
                    {
                        **base,
                        "entry": "__ZIP_ENTRIES__",
                        "format": "ZIP",
                        "variable_name": "__ZIP_ENTRIES__",
                        "shape": "",
                        "dtype": "",
                        "units": "",
                        "attributes_json": "; ".join(f"{i.filename}({i.file_size})" for i in entries),
                        "status": "OK",
                    }
                )
                for info in entries:
                    lname = info.filename.lower()
                    if not lname.endswith((".grb", ".grib", ".grib2", ".nc", ".nc4", ".h5", ".hdf", ".hdf5")):
                        continue
                    data = zf.read(info)
                    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(info.filename).suffix) as tmp:
                        tmp.write(data)
                        tmp_path = Path(tmp.name)
                    try:
                        if lname.endswith((".grb", ".grib", ".grib2")):
                            rows.extend(inspect_grib_bytes(data, info.filename, base))
                        elif lname.endswith((".nc", ".nc4")):
                            rows.extend(inspect_netcdf_path(tmp_path, info.filename, base))
                        else:
                            rows.extend(inspect_hdf_path(tmp_path, info.filename, base))
                    finally:
                        try:
                            tmp_path.unlink()
                        except OSError:
                            pass
            return rows
        suffix = path.suffix.lower()
        if suffix in {".grb", ".grib", ".grib2"}:
            return inspect_grib_bytes(path.read_bytes(), path.name, base)
        if suffix in {".nc", ".nc4"}:
            return inspect_netcdf_path(path, path.name, base)
        if suffix in {".h5", ".hdf", ".hdf5"}:
            return inspect_hdf_path(path, path.name, base)
        return [{**base, "entry": path.name, "format": "UNKNOWN", "status": "UNSUPPORTED_FORMAT"}]
    except Exception as exc:
        return [{**base, "entry": "", "format": "", "status": f"INSPECT_ERROR: {type(exc).__name__}: {exc}"}]


def aggregate_flags(rows: list[dict[str, Any]]) -> dict[str, bool]:
    out = {key: False for key in VARIABLE_PATTERNS}
    for row in rows:
        for key in out:
            value = row.get(f"has_{key}")
            out[key] = out[key] or value is True or str(value).lower() == "true"
    return out


def run_sample_phase(token: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sample_dir = OUT_DIR / "samples"
    availability, picked = sample_availability(token)
    write_csv(OUT_DIR / "meteosat_priority_collection_availability.csv", availability)

    inventory: list[dict[str, Any]] = []
    sample_status: list[dict[str, Any]] = []
    for item in TARGET_COLLECTIONS:
        cid = item["collection_id"]
        feature = picked.get(cid)
        if not feature:
            sample_status.append({"collection_id": cid, "sample_status": "NO_PRODUCT_FOUND"})
            continue
        product_id = str(feature.get("id", ""))
        local = sample_dir / item["priority"] / safe_name(cid) / (safe_name(product_id) + ".zip")
        base = {
            "priority": item["priority"],
            "collection_id": cid,
            "collection_title": item["title"],
            "product_id": product_id,
            "sample_path": str(local),
            "sample_time": feature_time(feature).isoformat().replace("+00:00", "Z") if feature_time(feature) else "",
        }
        try:
            existed = local.exists() and local.stat().st_size > 0
            download_product(cid, product_id, token, local)
            sample_rows = inspect_file(local, base)
            inventory.extend(sample_rows)
            flags = aggregate_flags(sample_rows)
            expected_ok = all(flags.get(x, False) for x in item["expected"])
            sample_status.append(
                {
                    **base,
                    "sample_status": "SKIPPED_EXISTING" if existed else "DOWNLOADED",
                    "inspect_status": "OK" if any(r.get("status") == "OK" for r in sample_rows) else "INSPECT_FAILED",
                    "expected_variables": ";".join(item["expected"]),
                    "expected_ok": expected_ok,
                    **{f"contains_{k}": v for k, v in flags.items()},
                }
            )
        except Exception as exc:
            sample_status.append({**base, "sample_status": f"FAILED: {type(exc).__name__}: {exc}", "expected_ok": False})
    write_csv(OUT_DIR / "meteosat_priority_sample_inventory.csv", inventory)
    write_csv(OUT_DIR / "meteosat_priority_sample_status.csv", sample_status)
    write_sample_report(availability, sample_status, inventory)


def boolish(value: Any) -> bool:
    return value is True or str(value).lower() == "true"


def write_sample_report(availability: list[dict[str, Any]], sample_status: list[dict[str, Any]], inventory: list[dict[str, Any]]) -> None:
    by_cid = {row["collection_id"]: row for row in sample_status}
    avail_count = defaultdict(int)
    for row in availability:
        if boolish(row.get("available")):
            avail_count[row["collection_id"]] += 1
    lines = [
        "# Meteosat Priority Cloud Product Sample Report",
        "",
        f"Generated: {utc_now()}",
        "",
        "This is phase 1 only: each collection is searched around March 2024 target times and at most one sample is downloaded/inspected.",
        "",
        "## Availability And Sample Gate",
        "",
        "| priority | collection | title | available_windows | sample_status | expected_ok | detected_variables |",
        "| --- | --- | --- | ---: | --- | --- | --- |",
    ]
    eligible = []
    for item in TARGET_COLLECTIONS:
        row = by_cid.get(item["collection_id"], {})
        detected = [k.replace("contains_", "") for k, v in row.items() if k.startswith("contains_") and boolish(v)]
        if boolish(row.get("expected_ok")) and item["priority"] in {"P0", "P1"}:
            eligible.append(item["collection_id"])
        lines.append(
            f"| {item['priority']} | `{item['collection_id']}` | {item['title']} | {avail_count[item['collection_id']]} | {row.get('sample_status','NO_PRODUCT_FOUND')} | {row.get('expected_ok','False')} | {'; '.join(detected)} |"
        )
    lines.extend(["", "## Required Answers", ""])
    answer_map = {
        "EO:EUM:DAT:0573": "0573 是否提供 cloud type / phase-like class",
        "EO:EUM:DAT:0574": "0574 是否提供 CTH / CTT / CTP",
        "EO:EUM:DAT:0575": "0575 是否提供 COT / CER / phase",
        "EO:EUM:DAT:MSG:OCA": "OCA 0 degree 是否提供 phase / CTP / COT / CER",
        "EO:EUM:DAT:MSG:OCA-IODC": "OCA-IODC 是否提供 phase / CTP / COT / CER",
    }
    for cid, question in answer_map.items():
        row = by_cid.get(cid, {})
        detected = [k.replace("contains_", "") for k, v in row.items() if k.startswith("contains_") and boolish(v)]
        status = row.get("sample_status", "NO_PRODUCT_FOUND")
        lines.append(f"- {question}: sample_status=`{status}`, expected_ok=`{row.get('expected_ok','False')}`, detected=`{'; '.join(detected) if detected else 'none'}`.")
    lines.extend(
        [
            f"- 适合批量下载 2024-03 的 collection: {', '.join('`'+x+'`' for x in eligible) if eligible else 'none'}。脚本只会把这些 P0/P1 且 expected_ok=True 的 collection 放入批量 manifest。",
            "- P2 regional collections are sample/local-reference only and are not included in the default full-month download manifest.",
            "",
            "## Inventory Preview",
            "",
            "| collection | entry | format | variable | shortName | name | paramId | shape | units | min | max |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | ---: | ---: |",
        ]
    )
    for row in inventory[:120]:
        lines.append(
            f"| `{row.get('collection_id','')}` | {str(row.get('entry',''))[:80]} | {row.get('format','')} | {row.get('variable_name','')} | {row.get('shortName','')} | {str(row.get('name',''))[:80]} | {row.get('paramId','')} | {row.get('shape','')} | {row.get('units','')} | {row.get('min','')} | {row.get('max','')} |"
        )
    (OUT_DIR / "meteosat_priority_sample_report.md").write_text("\n".join(lines), encoding="utf-8-sig")


def eligible_collections() -> list[dict[str, Any]]:
    rows = read_csv(OUT_DIR / "meteosat_priority_sample_status.csv")
    ok = {row["collection_id"] for row in rows if boolish(row.get("expected_ok"))}
    return [item for item in TARGET_COLLECTIONS if item["collection_id"] in ok and item["priority"] in {"P0", "P1"}]


def target_times_from_existing_meteosat() -> list[datetime]:
    roots = [
        EXTERNAL_GEO_CLOUD_ROOT / service / product
        for service in ("Meteosat-0deg", "Meteosat-IODC")
        for product in ("CLM", "CTH")
    ]
    times = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.zip"):
            m = re.search(r"(202403\d{8})", path.name)
            if not m:
                continue
            try:
                dt = datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                times.add(dt.replace(minute=0, second=0, microsecond=0))
            except Exception:
                pass
    if times:
        return sorted(times)
    out = []
    for day in range(1, calendar.monthrange(2024, 3)[1] + 1):
        for hour in range(24):
            out.append(datetime(2024, 3, day, hour, tzinfo=timezone.utc))
    return out


def build_manifest(token: str) -> list[dict[str, Any]]:
    manifest = []
    targets = target_times_from_existing_meteosat()
    for item in eligible_collections():
        cid = item["collection_id"]
        for target in targets:
            start = (target - timedelta(minutes=20)).isoformat().replace("+00:00", "Z")
            end = (target + timedelta(minutes=20)).isoformat().replace("+00:00", "Z")
            features, payload = search_collection(cid, token, start, end, 20)
            sample = pick_nearest(features, target)
            product_id = str(sample.get("id", "")) if sample else ""
            local = OUT_DIR / "downloads" / safe_name(cid) / target.strftime("%Y%m%d") / target.strftime("%H") / (safe_name(product_id or f"{safe_name(cid)}_{target:%Y%m%d%H}") + ".zip")
            manifest.append(
                {
                    "priority": item["priority"],
                    "collection_id": cid,
                    "title": item["title"],
                    "target_time_utc": target.isoformat().replace("+00:00", "Z"),
                    "search_start": start,
                    "search_end": end,
                    "product_id": product_id,
                    "product_time": feature_time(sample).isoformat().replace("+00:00", "Z") if sample and feature_time(sample) else "",
                    "feature_count": len(features),
                    "local_path": str(local),
                    "manifest_status": "FOUND" if product_id else "NOT_FOUND",
                    "http_status": payload.get("http_status", ""),
                    "note": payload.get("error", ""),
                }
            )
    write_csv(OUT_DIR / "meteosat_priority_download_manifest.csv", manifest)
    return manifest


def run_download_phase(token: str) -> None:
    sample_status = read_csv(OUT_DIR / "meteosat_priority_sample_status.csv")
    if not sample_status:
        raise RuntimeError("sample phase has not been run")
    manifest = build_manifest(token)
    status_rows = []
    for row in manifest:
        if row["manifest_status"] != "FOUND":
            status_rows.append({**row, "download_status": "NOT_FOUND"})
            continue
        path = Path(row["local_path"])
        if path.exists() and path.stat().st_size > 0:
            status_rows.append({**row, "download_status": "SKIPPED_EXISTING", "bytes": path.stat().st_size})
            continue
        try:
            download_product(row["collection_id"], row["product_id"], token, path)
            status_rows.append({**row, "download_status": "OK", "bytes": path.stat().st_size})
        except Exception as exc:
            status_rows.append({**row, "download_status": f"FAILED: {type(exc).__name__}: {exc}"})
    write_csv(OUT_DIR / "meteosat_priority_download_status.csv", status_rows)
    verify_downloads()


def verify_downloads() -> None:
    status_rows = read_csv(OUT_DIR / "meteosat_priority_download_status.csv")
    verification = []
    smoke = []
    by_collection: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in status_rows:
        by_collection[row["collection_id"]].append(row)
        path = Path(row.get("local_path", ""))
        if row.get("download_status") in {"OK", "SKIPPED_EXISTING"} and path.exists() and path.stat().st_size > 0:
            verification.append({**row, "verification_status": "OK", "verified_bytes": path.stat().st_size})
        else:
            verification.append({**row, "verification_status": row.get("download_status", "UNKNOWN")})
    for cid, rows in by_collection.items():
        good = [r for r in rows if r.get("download_status") in {"OK", "SKIPPED_EXISTING"} and Path(r.get("local_path", "")).exists()]
        if not good:
            smoke.append({"collection_id": cid, "smoke_status": "NO_FILES"})
            continue
        picks = [good[0], good[len(good) // 2], good[-1]] if len(good) > 2 else good
        for pick in picks:
            path = Path(pick["local_path"])
            inv = inspect_file(path, {"collection_id": cid, "product_id": pick.get("product_id", ""), "sample_path": str(path)})
            flags = aggregate_flags(inv)
            smoke.append({**pick, "smoke_status": "OK" if any(r.get("status") == "OK" for r in inv) else "INSPECT_FAILED", **{f"contains_{k}": v for k, v in flags.items()}})
    write_csv(OUT_DIR / "meteosat_priority_download_verification.csv", verification)
    write_csv(OUT_DIR / "meteosat_priority_variable_smoke_test.csv", smoke)
    write_download_report(verification, smoke)


def write_download_report(verification: list[dict[str, Any]], smoke: list[dict[str, Any]]) -> None:
    counts = defaultdict(lambda: defaultdict(int))
    for row in verification:
        counts[row["collection_id"]][row.get("verification_status", "")] += 1
    eligible = eligible_collections()
    lines = [
        "# Meteosat Priority Cloud Product Download Report",
        "",
        f"Generated: {utc_now()}",
        "",
        "Only collections that passed phase-1 expected-variable gates are included in the full-month manifest.",
        "",
        f"Eligible collections from phase 1: {', '.join('`'+x['collection_id']+'`' for x in eligible) if eligible else 'none'}.",
        "",
        "Batch download action: no full-month download was attempted because no P0/P1 collection passed sample validation." if not eligible else "Batch download action: full-month download attempted for eligible collections.",
        "",
        "## Verification Counts",
        "",
        "| collection | status | count |",
        "| --- | --- | ---: |",
    ]
    for cid, sub in sorted(counts.items()):
        for status, count in sorted(sub.items()):
            lines.append(f"| `{cid}` | {status} | {count} |")
    lines.extend(["", "## Smoke Test", "", "| collection | file | cloud_type | CTH | CTT | CTP | COT | CER | quality |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"])
    for row in smoke:
        lines.append(
            f"| `{row.get('collection_id','')}` | {Path(row.get('local_path', row.get('sample_path',''))).name} | {row.get('contains_cloud_type','')} | {row.get('contains_cloud_top_height','')} | {row.get('contains_cloud_top_temperature','')} | {row.get('contains_cloud_top_pressure','')} | {row.get('contains_cloud_optical_thickness','')} | {row.get('contains_cloud_effective_radius','')} | {row.get('contains_quality_flag','')} |"
        )
    (OUT_DIR / "meteosat_priority_download_report.md").write_text("\n".join(lines), encoding="utf-8-sig")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["sample", "download", "verify", "all"], default="sample")
    args = parser.parse_args()
    ensure_proxy_env()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    token = get_token() if args.phase in {"sample", "download", "all"} else ""
    if args.phase in {"sample", "all"}:
        run_sample_phase(token)
    if args.phase in {"download", "all"}:
        run_download_phase(token)
    if args.phase == "verify":
        verify_downloads()
    print(f"done phase={args.phase} out={OUT_DIR}")


if __name__ == "__main__":
    main()
