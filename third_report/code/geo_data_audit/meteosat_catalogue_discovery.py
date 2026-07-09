from __future__ import annotations

import csv
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import requests

try:
    import eccodes  # type: ignore
except Exception:
    eccodes = None

try:
    import eumdac  # type: ignore
except Exception:
    eumdac = None

try:
    import netCDF4  # type: ignore
except Exception:
    netCDF4 = None


OUT_DIR = Path(r"D:\AAAresearch_paper\data_check_report\meteosat_catalogue_discovery")
CRED_FILE = Path(r"D:\AAAresearch_paper\third_report\eumetsat_dataservices_API.txt")
DEFAULT_PROXY = "http://127.0.0.1:7897"

QUERY_DATES = [
    ("2024-03-01", "2024-03-01T00:00:00Z", "2024-03-01T23:59:59Z"),
    ("2024-03-12", "2024-03-12T00:00:00Z", "2024-03-12T23:59:59Z"),
    ("2024-03-31", "2024-03-31T00:00:00Z", "2024-03-31T23:59:59Z"),
]

KEYWORDS = [
    "Meteosat",
    "MSG",
    "SEVIRI",
    "cloud",
    "cloud analysis",
    "cloud type",
    "cloud top",
    "CTTH",
    "CLA",
    "OCA",
    "CMIC",
    "microphysics",
    "optical thickness",
    "effective radius",
    "phase",
    "IODC",
    "0 degree",
    "NWC SAF",
    "CM SAF",
]

SEED_IDS = [
    "EO:EUM:DAT:MSG:CLA",
    "EO:EUM:DAT:MSG:CLA-IODC",
    "EO:EUM:DAT:MSG:OCA",
    "EO:EUM:DAT:MSG:OCA-IODC",
    "EO:EUM:DAT:0059",
    "EO:EUM:DAT:0574",
    "EO:EUM:DAT:0617",
    "EO:EUM:DAT:0820",
]

VARIABLE_TERMS = {
    "CTT": ["cloud top temperature", "ctt", "temperature"],
    "CTP": ["cloud top pressure", "ctp", "pressure"],
    "cloud_type": ["cloud type"],
    "cloud_phase": ["phase"],
    "COT": ["cloud optical thickness", "optical thickness", "optical depth"],
    "CER": ["cloud effective radius", "effective radius", "particle size"],
    "quality": ["quality", "flag", "confidence", "status"],
}


def read_credentials() -> tuple[str, str]:
    text = CRED_FILE.read_text(encoding="utf-8", errors="ignore")
    key = os.environ.get("EUMETSAT_CONSUMER_KEY", "")
    secret = os.environ.get("EUMETSAT_CONSUMER_SECRET", "")
    for line in text.splitlines():
        if "=" in line:
            name, value = line.split("=", 1)
        elif ":" in line:
            name, value = line.split(":", 1)
        else:
            continue
        low = name.lower()
        if "consumer" in low and "key" in low:
            key = value.strip().strip("\"'")
        if "consumer" in low and "secret" in low:
            secret = value.strip().strip("\"'")
    if not key or not secret:
        raise RuntimeError("EUMETSAT consumer key/secret not found")
    return key, secret


def ensure_proxy_env() -> None:
    if not any(os.environ.get(k) for k in ["HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"]):
        os.environ["HTTPS_PROXY"] = DEFAULT_PROXY
        os.environ["https_proxy"] = DEFAULT_PROXY
        os.environ["HTTP_PROXY"] = DEFAULT_PROXY
        os.environ["http_proxy"] = DEFAULT_PROXY


def proxy_dict() -> dict[str, str]:
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or DEFAULT_PROXY
    return {"http": proxy, "https": proxy} if proxy else {}


def get_bearer_token() -> str:
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
            value = response.json().get("access_token")
            if not value:
                raise RuntimeError("missing access_token")
            return str(value)
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"token failed after retries: {last}")


def api_get_json(url: str, bearer: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    last = ""
    headers = {"Authorization": f"Bearer {bearer}"}
    for attempt in range(4):
        try:
            response = requests.get(url, headers=headers, params=params, timeout=75, proxies=proxy_dict())
            if response.status_code == 404:
                return {"__status__": 404, "__error__": response.text[:300], "__url__": response.url}
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict):
                data["__status__"] = response.status_code
                data["__url__"] = response.url
            return data
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(3 * (attempt + 1))
    return {"__status__": "ERROR", "__error__": last, "__url__": url}


def api_download_product(collection_id: str, product_id: str, bearer: str, path: Path) -> None:
    url = f"https://api.eumetsat.int/data/download/1.0.0/collections/{quote(collection_id, safe='')}/products/{quote(product_id, safe='')}"
    headers = {"Authorization": f"Bearer {bearer}"}
    with requests.get(url, headers=headers, timeout=180, proxies=proxy_dict(), stream=True) as response:
        response.raise_for_status()
        with path.open("wb") as out:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    out.write(chunk)


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            return str(value)
    return str(value)


def collection_id(collection: Any) -> str:
    return str(getattr(collection, "_id", "") or getattr(collection, "id", "") or getattr(collection, "collection_id", ""))


def metadata_value(meta: dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        if key in meta and meta[key] not in (None, ""):
            return safe_text(meta[key])
    low = {str(k).lower(): v for k, v in meta.items()}
    for key in keys:
        if key.lower() in low and low[key.lower()] not in (None, ""):
            return safe_text(low[key.lower()])
    return ""


def relevance(collection: Any) -> tuple[int, list[str], str]:
    cid = collection_id(collection)
    title = safe_text(getattr(collection, "title", ""))
    abstract = safe_text(getattr(collection, "abstract", ""))
    product_type = safe_text(getattr(collection, "product_type", ""))
    try:
        meta = getattr(collection, "metadata", {}) or {}
    except Exception:
        meta = {}
    blob = " ".join([cid, title, abstract, product_type, safe_text(meta)]).lower()
    hits: list[str] = []
    score = 0
    for kw in KEYWORDS:
        if kw.lower() in blob:
            hits.append(kw)
            score += 1
    if any(x in blob for x in ["cloud", "cla", "oca", "cmic", "ctth", "cloud top", "cloud type"]):
        score += 2
    if any(x in blob for x in ["meteosat", "msg", "seviri"]):
        score += 2
    if any(x in blob for x in ["software", "toolbox", "training", "course"]):
        score -= 3
    if any(x in blob for x in ["climate data record", "cdr", "fundamental climate data record"]):
        score -= 1
    return max(score, 0), hits, blob


def expected_variables(blob: str) -> str:
    out = []
    for label, terms in VARIABLE_TERMS.items():
        if any(t in blob for t in terms):
            out.append(label)
    return ";".join(out)


def classify_candidate(row: dict[str, Any]) -> str:
    blob = " ".join(str(row.get(k, "")) for k in row).lower()
    labels = []
    if any(x in blob for x in ["software", "toolbox", "training", "algorithm package"]):
        labels.append("software-only")
    if any(x in blob for x in ["climate data record", "cdr", "climate monitoring", "cm saf"]):
        labels.append("CDR/SAF")
    if any(x in blob for x in ["regional", "europe", "africa", "iodc", "indian ocean"]):
        labels.append("regional_or_service_area")
    if not labels:
        labels.append("candidate_data_collection")
    return ";".join(labels)


def discover_collections(store: Any) -> list[dict[str, Any]]:
    rows = []
    seen = set()
    for collection in store.collections:
        cid = collection_id(collection)
        if not cid or cid in seen:
            continue
        seen.add(cid)
        try:
            meta = getattr(collection, "metadata", {}) or {}
        except Exception:
            meta = {}
        score, hits, blob = relevance(collection)
        seed = cid in SEED_IDS
        if score < 2 and not seed:
            continue
        row = {
            "collection_id": cid,
            "title": safe_text(getattr(collection, "title", "")),
            "provider_SAF": metadata_value(meta, ["provider", "organisationName", "responsibleOrganisation", "saf", "originator"]),
            "platform_instrument": " / ".join(x for x in [metadata_value(meta, ["platform", "platformShortName", "satellite"]), metadata_value(meta, ["instrument", "instrumentShortName"])] if x),
            "coverage_region": metadata_value(meta, ["region", "regionCoverage", "geographicalCoverage", "area", "spatial"]),
            "temporal_coverage": metadata_value(meta, ["temporalCoverage", "timePeriod", "beginPosition", "endPosition"]),
            "access_method": "EUMETSAT Data Store / EUMDAC",
            "relevance_score": score,
            "keyword_hits": ";".join(hits),
            "expected_variables": expected_variables(blob),
            "whether_candidate_for_2024_03": "TO_CHECK" if score >= 3 else "LOW_SCORE_NOT_CHECKED",
            "seed_sanity_check": seed,
            "candidate_class": "",
            "abstract": safe_text(getattr(collection, "abstract", "")),
            "metadata_json": safe_text(meta),
        }
        row["candidate_class"] = classify_candidate(row)
        rows.append(row)
    for sid in SEED_IDS:
        if sid not in seen:
            rows.append({
                "collection_id": sid,
                "title": "",
                "provider_SAF": "",
                "platform_instrument": "",
                "coverage_region": "",
                "temporal_coverage": "",
                "access_method": "seed sanity check, not found in enumerated catalogue",
                "relevance_score": 3,
                "keyword_hits": "seed",
                "expected_variables": expected_variables(sid.lower()),
                "whether_candidate_for_2024_03": "TO_CHECK",
                "seed_sanity_check": True,
                "candidate_class": "seed_not_in_catalogue",
                "abstract": "",
                "metadata_json": "",
            })
    return sorted(rows, key=lambda r: (-int(r["relevance_score"]), str(r["collection_id"])))


def discover_collections_rest(bearer: str) -> list[dict[str, Any]]:
    url = "https://api.eumetsat.int/data/browse/1.0.0/collections"
    data = api_get_json(url, bearer, {"format": "json"})
    links = data.get("links", []) if isinstance(data, dict) else []
    rows = []
    seen = set()
    for item in links:
        cid = safe_text(item.get("title") or item.get("id") or item.get("name"))
        title = safe_text(item.get("datasetTitle") or item.get("title"))
        if not cid or cid in seen:
            continue
        seen.add(cid)
        blob = " ".join([cid, title, safe_text(item)]).lower()
        hits = []
        score = 0
        for kw in KEYWORDS:
            if kw.lower() in blob:
                hits.append(kw)
                score += 1
        if any(x in blob for x in ["cloud", "cla", "oca", "cmic", "ctth", "cloud top", "cloud type"]):
            score += 2
        if any(x in blob for x in ["meteosat", "msg", "seviri"]):
            score += 2
        if any(x in blob for x in ["software", "toolbox", "training", "course"]):
            score -= 3
        if any(x in blob for x in ["climate data record", "cdr", "fundamental climate data record"]):
            score -= 1
        seed = cid in SEED_IDS
        if max(score, 0) < 2 and not seed:
            continue
        detail = {}
        if max(score, 0) >= 3 or seed:
            detail_url = f"https://api.eumetsat.int/data/browse/1.0.0/collections/{quote(cid, safe='')}"
            detail = api_get_json(detail_url, bearer, {"format": "json"})
        full_blob = " ".join([blob, safe_text(detail)]).lower()
        row = {
            "collection_id": cid,
            "title": title,
            "provider_SAF": "",
            "platform_instrument": "",
            "coverage_region": "",
            "temporal_coverage": "",
            "access_method": "EUMETSAT Data Store REST catalogue/search",
            "relevance_score": max(score, 0),
            "keyword_hits": ";".join(hits),
            "expected_variables": expected_variables(full_blob),
            "whether_candidate_for_2024_03": "TO_CHECK" if max(score, 0) >= 3 else "LOW_SCORE_NOT_CHECKED",
            "seed_sanity_check": seed,
            "candidate_class": "",
            "abstract": safe_text(detail.get("abstract") or detail.get("description") or item.get("description")),
            "metadata_json": safe_text({"catalogue_link": item, "detail": detail}),
        }
        row["provider_SAF"] = "CM SAF" if "cm saf" in full_blob else ("NWC SAF" if "nwc saf" in full_blob else "")
        if any(x in full_blob for x in ["meteosat", "msg"]):
            row["platform_instrument"] = "Meteosat/MSG"
        if "seviri" in full_blob:
            row["platform_instrument"] = (row["platform_instrument"] + " / SEVIRI").strip(" /")
        row["coverage_region"] = "IODC" if "iodc" in full_blob or "indian ocean" in full_blob else ("0 degree" if "0 degree" in full_blob else "")
        row["candidate_class"] = classify_candidate(row)
        rows.append(row)
    for sid in SEED_IDS:
        if sid not in seen:
            detail_url = f"https://api.eumetsat.int/data/browse/1.0.0/collections/{quote(sid, safe='')}"
            detail = api_get_json(detail_url, bearer, {"format": "json"})
            rows.append({
                "collection_id": sid,
                "title": safe_text(detail.get("datasetTitle") or detail.get("title")),
                "provider_SAF": "CM SAF" if "cm saf" in safe_text(detail).lower() else ("NWC SAF" if "nwc saf" in safe_text(detail).lower() else ""),
                "platform_instrument": "Meteosat/MSG/SEVIRI" if any(x in safe_text(detail).lower() for x in ["meteosat", "msg", "seviri"]) else "",
                "coverage_region": "IODC" if "iodc" in sid.lower() else "",
                "temporal_coverage": "",
                "access_method": "seed sanity check via REST detail/search",
                "relevance_score": 3,
                "keyword_hits": "seed",
                "expected_variables": expected_variables((sid + " " + safe_text(detail)).lower()),
                "whether_candidate_for_2024_03": "TO_CHECK",
                "seed_sanity_check": True,
                "candidate_class": "seed_sanity_check" if detail.get("__status__") != 404 else "seed_not_found_404",
                "abstract": safe_text(detail.get("abstract") or detail.get("description")),
                "metadata_json": safe_text(detail),
            })
    dedup = {r["collection_id"]: r for r in rows}
    return sorted(dedup.values(), key=lambda r: (-int(r["relevance_score"]), str(r["collection_id"])))


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def product_attr(product: Any, attr: str) -> str:
    try:
        value = getattr(product, attr, "")
        return safe_text(value)
    except Exception:
        return ""


def check_availability(store: Any, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    availability = []
    first_products: dict[str, Any] = {}
    for row in rows:
        if int(row.get("relevance_score", 0)) < 3:
            continue
        cid = str(row["collection_id"])
        try:
            collection = store.get_collection(cid)
        except Exception as exc:
            for label, start, end in QUERY_DATES:
                availability.append({
                    "collection_id": cid,
                    "date": label,
                    "available": False,
                    "product_count_checked": 0,
                    "sample_product_id": "",
                    "sample_size": "",
                    "sample_sensing_start": "",
                    "sample_sensing_end": "",
                    "status": f"COLLECTION_ERROR: {type(exc).__name__}: {exc}",
                })
            continue
        for label, start, end in QUERY_DATES:
            status = "OK"
            products = []
            for attempt in range(3):
                try:
                    products = list(collection.search(dtstart=parse_dt(start), dtend=parse_dt(end)))
                    break
                except Exception as exc:
                    status = f"SEARCH_ERROR: {type(exc).__name__}: {exc}"
                    time.sleep(3 * (attempt + 1))
            sample = products[0] if products else None
            if sample is not None and cid not in first_products:
                first_products[cid] = sample
            availability.append({
                "collection_id": cid,
                "date": label,
                "available": bool(products),
                "product_count_checked": len(products),
                "sample_product_id": product_attr(sample, "_id") if sample is not None else "",
                "sample_size": product_attr(sample, "size") if sample is not None else "",
                "sample_sensing_start": product_attr(sample, "sensing_start") if sample is not None else "",
                "sample_sensing_end": product_attr(sample, "sensing_end") if sample is not None else "",
                "status": status,
            })
    return availability, first_products


def check_availability_rest(bearer: str, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    availability = []
    first_products: dict[str, dict[str, Any]] = {}
    url = "https://api.eumetsat.int/data/search-products/1.0.0/os"
    for row in rows:
        if int(row.get("relevance_score", 0)) < 3:
            continue
        cid = str(row["collection_id"])
        for label, start, end in QUERY_DATES:
            params = {"format": "json", "pi": cid, "si": 0, "c": 5, "dtstart": start, "dtend": end}
            data = api_get_json(url, bearer, params)
            feats = data.get("features", []) if isinstance(data, dict) else []
            sample = feats[0] if feats else {}
            product_id = safe_text(sample.get("id", ""))
            if product_id and cid not in first_products:
                first_products[cid] = sample
            availability.append({
                "collection_id": cid,
                "date": label,
                "available": bool(feats),
                "product_count_checked": len(feats),
                "sample_product_id": product_id,
                "sample_size": safe_text(sample.get("properties", {}).get("productInformation", {}).get("size") or sample.get("properties", {}).get("size", "")) if sample else "",
                "sample_sensing_start": safe_text(sample.get("properties", {}).get("date", "")) if sample else "",
                "sample_sensing_end": "",
                "status": f"HTTP_{data.get('__status__')}" if isinstance(data, dict) else "NO_RESPONSE",
            })
    return availability, first_products


def save_product_sample(cid: str, product: Any) -> Path:
    sample_dir = OUT_DIR / "samples"
    sample_dir.mkdir(parents=True, exist_ok=True)
    pid = product_attr(product, "_id") or product_attr(product, "id") or "sample"
    name = re.sub(r"[^A-Za-z0-9._+=-]+", "_", f"{cid}_{pid}").strip("_")[:180] + ".zip"
    path = sample_dir / name
    if path.exists() and path.stat().st_size > 0:
        return path
    with product.open() as stream, path.open("wb") as out:
        shutil.copyfileobj(stream, out)
    return path


def inspect_netcdf_bytes(data: bytes, member: str, base: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    if netCDF4 is None:
        return [{**base, "entry": member, "format": "NetCDF", "status": "netCDF4 unavailable"}]
    with tempfile.NamedTemporaryFile(delete=False, suffix=".nc") as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        ds = netCDF4.Dataset(tmp_path)
        try:
            for name, var in ds.variables.items():
                attrs = {a: safe_text(getattr(var, a)) for a in var.ncattrs()}
                rows.append({
                    **base,
                    "entry": member,
                    "format": "NetCDF",
                    "variable_or_shortName": name,
                    "name": attrs.get("long_name") or attrs.get("standard_name") or "",
                    "paramId": "",
                    "shape": "x".join(str(x) for x in getattr(var, "shape", "")),
                    "units": attrs.get("units", ""),
                    "dtype": safe_text(getattr(var, "dtype", "")),
                    "metadata": safe_text(attrs),
                    "status": "OK",
                })
        finally:
            ds.close()
    except Exception as exc:
        rows.append({**base, "entry": member, "format": "NetCDF", "status": f"NETCDF_ERROR: {type(exc).__name__}: {exc}"})
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return rows


def inspect_grib_bytes(data: bytes, member: str, base: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    if eccodes is None:
        return [{**base, "entry": member, "format": "GRIB", "status": "eccodes unavailable"}]
    with tempfile.NamedTemporaryFile(delete=False, suffix=".grb") as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        with open(tmp_path, "rb") as fh:
            msg = 0
            while True:
                gid = eccodes.codes_grib_new_from_file(fh)
                if gid is None:
                    break
                msg += 1
                row = {**base, "entry": member, "format": "GRIB", "message": msg, "status": "OK"}
                for key in ["shortName", "name", "paramId", "typeOfLevel", "level", "gridType", "Nx", "Ny", "units"]:
                    try:
                        row[key] = eccodes.codes_get(gid, key)
                    except Exception:
                        row[key] = ""
                row["variable_or_shortName"] = row.get("shortName", "")
                row["shape"] = f"{row.get('Ny','')}x{row.get('Nx','')}"
                eccodes.codes_release(gid)
                rows.append(row)
    except Exception as exc:
        rows.append({**base, "entry": member, "format": "GRIB", "status": f"GRIB_ERROR: {type(exc).__name__}: {exc}"})
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return rows


def variable_flags(rows: list[dict[str, Any]]) -> dict[str, bool]:
    blob = " ".join(" ".join(str(r.get(k, "")) for k in ["variable_or_shortName", "shortName", "name", "metadata"]) for r in rows).lower()
    return {label: any(term in blob for term in terms) for label, terms in VARIABLE_TERMS.items()}


def inspect_sample(cid: str, path: Path) -> list[dict[str, Any]]:
    base = {"collection_id": cid, "sample_path": str(path)}
    rows: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(path) as zf:
            entries = zf.infolist()
            rows.append({**base, "entry": "__ZIP_ENTRIES__", "format": "ZIP", "metadata": "; ".join(f"{e.filename}({e.file_size})" for e in entries), "status": "OK"})
            for info in entries:
                lname = info.filename.lower()
                if not (lname.endswith((".grb", ".grib", ".grib2", ".nc", ".nc4", ".h5", ".hdf", ".hdf5"))):
                    continue
                data = zf.read(info)
                if lname.endswith((".grb", ".grib", ".grib2")):
                    rows.extend(inspect_grib_bytes(data, info.filename, base))
                else:
                    rows.extend(inspect_netcdf_bytes(data, info.filename, base))
    except zipfile.BadZipFile:
        data = path.read_bytes()
        if path.suffix.lower() in {".grb", ".grib", ".grib2"}:
            rows.extend(inspect_grib_bytes(data, path.name, base))
        elif path.suffix.lower() in {".nc", ".h5", ".hdf"}:
            rows.extend(inspect_netcdf_bytes(data, path.name, base))
        else:
            rows.append({**base, "entry": path.name, "format": "UNKNOWN", "status": "not a ZIP/GRIB/NetCDF sample"})
    except Exception as exc:
        rows.append({**base, "entry": "", "format": "", "status": f"SAMPLE_ERROR: {type(exc).__name__}: {exc}"})
    flags = variable_flags(rows)
    for row in rows:
        for k, v in flags.items():
            row[f"contains_{k}"] = v
    return rows


def sample_high_relevance(first_products: dict[str, Any], availability: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    available_cids = {r["collection_id"] for r in availability if str(r.get("available")).lower() == "true" or r.get("available") is True}
    for cid in sorted(available_cids):
        product = first_products.get(cid)
        if product is None:
            continue
        try:
            path = save_product_sample(cid, product)
            rows.extend(inspect_sample(cid, path))
        except Exception as exc:
            rows.append({"collection_id": cid, "sample_path": "", "entry": "", "format": "", "status": f"DOWNLOAD_OR_INSPECT_ERROR: {type(exc).__name__}: {exc}"})
    return rows


def sample_high_relevance_rest(bearer: str, first_products: dict[str, dict[str, Any]], availability: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    available_cids = {r["collection_id"] for r in availability if r.get("available") is True}
    sample_dir = OUT_DIR / "samples"
    sample_dir.mkdir(parents=True, exist_ok=True)
    for cid in sorted(available_cids):
        if cid in {"EO:EUM:DAT:MSG:CLM", "EO:EUM:DAT:MSG:CLM-IODC", "EO:EUM:DAT:MSG:CTH", "EO:EUM:DAT:MSG:CTH-IODC"}:
            continue
        feature = first_products.get(cid)
        if not feature:
            continue
        pid = safe_text(feature.get("id", ""))
        if not pid:
            continue
        name = re.sub(r"[^A-Za-z0-9._+=-]+", "_", f"{cid}_{pid}").strip("_")[:180] + ".zip"
        path = sample_dir / name
        try:
            if not path.exists() or path.stat().st_size == 0:
                api_download_product(cid, pid, bearer, path)
            rows.extend(inspect_sample(cid, path))
        except Exception as exc:
            rows.append({"collection_id": cid, "sample_path": str(path), "entry": "", "format": "", "status": f"DOWNLOAD_OR_INSPECT_ERROR: {type(exc).__name__}: {exc}"})
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def md_table(rows: list[dict[str, Any]], fields: list[str], limit: int = 50) -> str:
    if not rows:
        return "_No rows._"
    out = ["| " + " | ".join(fields) + " |", "| " + " | ".join(["---"] * len(fields)) + " |"]
    for row in rows[:limit]:
        vals = []
        for field in fields:
            val = safe_text(row.get(field, "")).replace("|", "\\|").replace("\n", " ")
            if len(val) > 110:
                val = val[:107] + "..."
            vals.append(val)
        out.append("| " + " | ".join(vals) + " |")
    if len(rows) > limit:
        out.append(f"\n_Showing {limit} of {len(rows)} rows._")
    return "\n".join(out)


def write_report(candidates: list[dict[str, Any]], availability: list[dict[str, Any]], inventory: list[dict[str, Any]]) -> None:
    candidate_extra = [r for r in candidates if "CLM" not in r["collection_id"] and "CTH" not in r["collection_id"]]
    available = [r for r in availability if r.get("available") is True]
    flags_by_cid: dict[str, dict[str, bool]] = {}
    for cid in sorted({r.get("collection_id", "") for r in inventory}):
        flags_by_cid[cid] = variable_flags([r for r in inventory if r.get("collection_id") == cid])
    flag_rows = [{"collection_id": cid, **flags} for cid, flags in flags_by_cid.items()]
    class_counts = Counter(r.get("candidate_class", "") for r in candidates)
    lines = [
        "# Meteosat Catalogue Discovery",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "This run enumerated EUMETSAT Data Store catalogue metadata with EUMDAC, scored Meteosat/MSG/SEVIRI cloud relevance, checked March 2024 availability for score >= 3 candidates, and downloaded at most one sample per available collection for smoke testing.",
        "",
        "Known baseline: CLM and CTH conclusions from the previous audit are treated as established and are used only as sanity checks.",
        "",
        "## Discovered Candidates Beyond CLM/CTH",
        "",
        md_table(candidate_extra, ["collection_id", "title", "provider_SAF", "platform_instrument", "coverage_region", "temporal_coverage", "relevance_score", "expected_variables", "candidate_class"], 80),
        "",
        "## March 2024 Availability",
        "",
        md_table(availability, ["collection_id", "date", "available", "product_count_checked", "sample_product_id", "status"], 120),
        "",
        "## Sample Variable Flags",
        "",
        md_table(flag_rows, ["collection_id", "CTT", "CTP", "cloud_type", "cloud_phase", "COT", "CER", "quality"], 80),
        "",
        "## Sample Variable Inventory",
        "",
        md_table(inventory, ["collection_id", "entry", "format", "variable_or_shortName", "name", "paramId", "shape", "units", "status"], 120),
        "",
        "## Classification Counts",
        "",
        md_table([{"candidate_class": k, "count": v} for k, v in class_counts.items()], ["candidate_class", "count"]),
        "",
        "## Answers",
        "",
    ]
    extra_ids = [r["collection_id"] for r in candidate_extra if int(r.get("relevance_score", 0)) >= 3]
    available_ids = sorted({r["collection_id"] for r in available if "CLM" not in r["collection_id"] and "CTH" not in r["collection_id"]})
    sampled_ids = sorted({r["collection_id"] for r in inventory if r.get("status") == "OK" and "CLM" not in r.get("collection_id", "") and "CTH" not in r.get("collection_id", "")})
    useful_ids = [r for r in flag_rows if any(r.get(k) for k in ["CTT", "CTP", "cloud_type", "cloud_phase", "COT", "CER"])]
    lines.extend([
        f"1. 除 CLM/CTH 外，发现的高相关候选 collection 数量为 {len(extra_ids)}；详见 `candidate_collections_discovered.csv`。",
        f"2. 覆盖 2024-03 的非 CLM/CTH 候选为: {', '.join(available_ids) if available_ids else '未发现'}。",
        f"3. 真正可下载并完成样本 smoke test 的非 CLM/CTH 候选为: {', '.join(sampled_ids) if sampled_ids else '未发现'}。",
        f"4. 样本中含 CTT/CTP/cloud type/phase/COT/CER 证据的 collection 为: {', '.join(r['collection_id'] for r in useful_ids) if useful_ids else '未发现'}。",
        "5. regional/CDR/software-only 的分类见 candidate_class；CM SAF/NWC SAF/CDR 类候选不会自动等同于当前 2024-03 MSG 全圆盘 L2 数据源。",
        "6. 是否可升 v1/v2: 若本次未发现可下载且样本含目标变量的非 CLM/CTH collection，则 Meteosat 仍只能稳定支持 v0 cloud mask + CTH；若发现含 CTT/CTP/phase 或 COT/CER 的可下载样本，则优先推荐对应 collection 进入下一轮小批量验证。",
    ])
    (OUT_DIR / "meteosat_catalogue_discovery_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ensure_proxy_env()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    bearer = get_bearer_token()
    candidates = discover_collections_rest(bearer)
    write_csv(OUT_DIR / "candidate_collections_discovered.csv", candidates)
    availability, first_products = check_availability_rest(bearer, candidates)
    write_csv(OUT_DIR / "candidate_collection_availability_202403.csv", availability)
    inventory = sample_high_relevance_rest(bearer, first_products, availability)
    write_csv(OUT_DIR / "candidate_sample_variable_inventory.csv", inventory)
    write_report(candidates, availability, inventory)
    print(f"candidate_collections={len(candidates)}")
    print(f"availability_rows={len(availability)}")
    print(f"sample_inventory_rows={len(inventory)}")
    print(f"report={OUT_DIR / 'meteosat_catalogue_discovery_report.md'}")


if __name__ == "__main__":
    main()
