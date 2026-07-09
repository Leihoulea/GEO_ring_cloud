"""Inventory and download GEO-ring cloud products.

This module is intentionally conservative:
- API secrets are read only from environment variables.
- Files are written as .part first and atomically renamed after validation.
- The first-round workflow stops after full inventory plus the 2024-03-12 test day.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import json
import os
import re
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, Optional


DOWNLOAD_MONTHS = [(2024, 1), (2024, 3), (2024, 5)]
TEST_DAY = "2024-03-12"
DEFAULT_ROOT = Path(r"E:\GEO_Cloud_2024")
RETRY_DELAYS_SECONDS = [5, 10, 20, 40, 60, 120, 180, 300]
S3_CHUNK_SIZE = 1024 * 1024
EUMETSAT_CHUNK_SIZE = 1024 * 512
EUMETSAT_SEARCH_URL = "https://api.eumetsat.int/data/search-products/1.0.0/os"
EUMETSAT_TOKEN_URL = "https://api.eumetsat.int/token"
_EUMETSAT_BEARER_TOKEN: Optional[str] = None

GOES_CONFIG = {
    "GOES-16": {
        "bucket": "noaa-goes16",
        "service": "GOES-16",
        "short_products": {"ABI-L2-ACMF": "ACMF", "ABI-L2-ACHAF": "ACHAF"},
    },
    "GOES-18": {
        "bucket": "noaa-goes18",
        "service": "GOES-18",
        "short_products": {"ABI-L2-ACMF": "ACMF", "ABI-L2-ACHAF": "ACHAF"},
    },
}

HIMAWARI_CONFIG = {
    "platform": "Himawari-9",
    "service": "Himawari-9",
    "bucket": "noaa-himawari9",
    "base_prefix": "AHI-L2-FLDK-Clouds",
    "prefixes": {"AHI-CMSK_": "CMSK", "AHI-CHGT_": "CHGT"},
}

METEOSAT_CONFIG = {
    "Meteosat-0deg": {
        "EO:EUM:DAT:MSG:CLM": "CLM",
        "EO:EUM:DAT:MSG:CTH": "CTH",
    },
    "Meteosat-IODC": {
        "EO:EUM:DAT:MSG:CLM-IODC": "CLM",
        "EO:EUM:DAT:MSG:CTH-IODC": "CTH",
    },
}

MANIFEST_FIELDS = [
    "target_time_utc",
    "platform",
    "service",
    "product",
    "collection_id",
    "remote_type",
    "bucket",
    "remote_key_or_product_id",
    "actual_start_time",
    "actual_end_time",
    "time_difference_seconds",
    "size_bytes",
    "status",
    "local_path",
    "note",
]


@dataclass(frozen=True)
class Candidate:
    remote_id: str
    start_time: Optional[datetime]
    end_time: Optional[datetime]
    size_bytes: Optional[int]
    note: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def iter_target_times(months: Iterable[tuple[int, int]] = DOWNLOAD_MONTHS) -> Iterable[datetime]:
    for year, month in months:
        for day in range(1, calendar.monthrange(year, month)[1] + 1):
            for hour in range(24):
                yield datetime(year, month, day, hour, tzinfo=timezone.utc)


def iter_target_times_between(start_date: str, end_date: str) -> Iterable[datetime]:
    start = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)
    current = start
    while current <= end:
        for hour in range(24):
            yield current.replace(hour=hour)
        current += timedelta(days=1)


def ensure_dirs(root: Path) -> None:
    for name in ["manifests", "logs", "quarantine"]:
        (root / name).mkdir(parents=True, exist_ok=True)


def manifest_path(root: Path, name: str) -> Path:
    return root / "manifests" / name


def safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._+=-]+", "_", value).strip("_") or "product"


def local_path_for(root: Path, platform: str, product: str, target_time: datetime, filename: str) -> Path:
    day = target_time.strftime("%Y%m%d")
    hour = target_time.strftime("%H")
    return root / platform / product / day / hour / filename


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in MANIFEST_FIELDS})
    replace_with_retry(tmp, path)


def replace_with_retry(src: Path, dst: Path, attempts: int = 8) -> None:
    last_error: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.25 * (attempt + 1))
    try:
        shutil.copyfile(src, dst)
        try:
            src.unlink(missing_ok=True)
        except PermissionError:
            pass
        return
    except Exception:
        if last_error:
            raise last_error
        raise


def read_manifest(path: Path) -> list[dict]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def get_s3_client(proxy_url: str = ""):
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config

    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else {}
    return boto3.client("s3", config=Config(signature_version=UNSIGNED, proxies=proxies))


def s3_proxy_for_row(row: dict) -> str:
    platform = row.get("platform", "")
    if platform.startswith("GOES-"):
        return os.environ.get("GEO_CLOUD_GOES_PROXY", "").strip()
    if platform.startswith("Himawari-"):
        return os.environ.get("GEO_CLOUD_HIMAWARI_PROXY", "").strip()
    return os.environ.get("GEO_CLOUD_S3_PROXY", "").strip()


def list_s3_objects(s3_client, bucket: str, prefix: str) -> list[dict]:
    paginator = s3_client.get_paginator("list_objects_v2")
    objects: list[dict] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        objects.extend(page.get("Contents", []))
    return objects


GOES_START_RE = re.compile(r"_s(?P<stamp>\d{13})")


def parse_goes_start_time(key: str) -> Optional[datetime]:
    match = GOES_START_RE.search(Path(key).name)
    if not match:
        return None
    stamp = match.group("stamp")
    year = int(stamp[0:4])
    doy = int(stamp[4:7])
    hour = int(stamp[7:9])
    minute = int(stamp[9:11])
    second = int(stamp[11:13])
    return datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(
        days=doy - 1, hours=hour, minutes=minute, seconds=second
    )


def pick_goes_candidate(objects: list[dict], target_time: datetime) -> Optional[Candidate]:
    candidates: list[Candidate] = []
    for obj in objects:
        key = obj["Key"]
        start = parse_goes_start_time(key)
        if start is None:
            continue
        diff = abs((start - target_time).total_seconds())
        if diff <= 5 * 60:
            candidates.append(
                Candidate(
                    remote_id=key,
                    start_time=start,
                    end_time=None,
                    size_bytes=int(obj.get("Size", 0)),
                    note=f"goes_start_diff_seconds={int(diff)}",
                )
            )
    if not candidates:
        return None
    return min(candidates, key=lambda item: abs((item.start_time - target_time).total_seconds()))


def inventory_goes(root: Path, s3_client, target_time: datetime) -> list[dict]:
    rows: list[dict] = []
    year = target_time.strftime("%Y")
    doy = target_time.strftime("%j")
    hour = target_time.strftime("%H")
    for platform, cfg in GOES_CONFIG.items():
        for full_product, short_product in cfg["short_products"].items():
            prefix = f"{full_product}/{year}/{doy}/{hour}/"
            try:
                objects = list_s3_objects(s3_client, cfg["bucket"], prefix)
                candidate = pick_goes_candidate(objects, target_time)
                if candidate:
                    filename = Path(candidate.remote_id).name
                    status = "found"
                    note = candidate.note
                    local_path = local_path_for(root, platform, short_product, target_time, filename)
                    remote_id = candidate.remote_id
                    start = candidate.start_time
                    end = candidate.end_time
                    size = candidate.size_bytes
                else:
                    status = "missing"
                    note = f"no_candidate_within_5min prefix={prefix}"
                    local_path = ""
                    remote_id = ""
                    start = None
                    end = None
                    size = ""
            except Exception as exc:
                status = "error"
                note = f"{type(exc).__name__}: {exc}"
                local_path = ""
                remote_id = ""
                start = None
                end = None
                size = ""
            rows.append(
                base_row(
                    target_time,
                    platform,
                    cfg["service"],
                    short_product,
                    "",
                    "s3",
                    cfg["bucket"],
                    remote_id,
                    start,
                    end,
                    size,
                    status,
                    local_path,
                    note,
                )
            )
    return rows


def inventory_himawari(root: Path, s3_client, target_time: datetime) -> list[dict]:
    rows: list[dict] = []
    cfg = HIMAWARI_CONFIG
    prefix = f"{cfg['base_prefix']}/{target_time:%Y/%m/%d/%H}00/"
    try:
        objects = list_s3_objects(s3_client, cfg["bucket"], prefix)
    except Exception as exc:
        objects = []
        listing_error = f"{type(exc).__name__}: {exc}"
    else:
        listing_error = ""

    for file_prefix, short_product in cfg["prefixes"].items():
        matching = [obj for obj in objects if Path(obj["Key"]).name.startswith(file_prefix)]
        if matching:
            obj = sorted(matching, key=lambda item: item["Key"])[0]
            filename = Path(obj["Key"]).name
            local_path = local_path_for(root, cfg["platform"], short_product, target_time, filename)
            rows.append(
                base_row(
                    target_time,
                    cfg["platform"],
                    cfg["service"],
                    short_product,
                    "",
                    "s3",
                    cfg["bucket"],
                    obj["Key"],
                    target_time,
                    None,
                    int(obj.get("Size", 0)),
                    "found",
                    local_path,
                    f"prefix={prefix}",
                )
            )
        else:
            rows.append(
                base_row(
                    target_time,
                    cfg["platform"],
                    cfg["service"],
                    short_product,
                    "",
                    "s3",
                    cfg["bucket"],
                    "",
                    None,
                    None,
                    "",
                    "error" if listing_error else "missing",
                    "",
                    listing_error or f"no_file_prefix={file_prefix} prefix={prefix}",
                )
            )
    return rows


def get_eumdac_datastore():
    import eumdac

    key = os.environ.get("EUMETSAT_CONSUMER_KEY", "").strip()
    secret = os.environ.get("EUMETSAT_CONSUMER_SECRET", "").strip()
    if not key or not secret:
        raise RuntimeError("EUMETSAT_CONSUMER_KEY/SECRET are not set")
    token = eumdac.AccessToken((key, secret))
    return eumdac.DataStore(token=token)


def get_eumetsat_bearer_token() -> str:
    global _EUMETSAT_BEARER_TOKEN
    if _EUMETSAT_BEARER_TOKEN:
        return _EUMETSAT_BEARER_TOKEN

    import requests

    key = os.environ.get("EUMETSAT_CONSUMER_KEY", "").strip()
    secret = os.environ.get("EUMETSAT_CONSUMER_SECRET", "").strip()
    if not key or not secret:
        raise RuntimeError("EUMETSAT_CONSUMER_KEY/SECRET are not set")

    last_error: Optional[str] = None
    for delay_index, delay in enumerate([0] + RETRY_DELAYS_SECONDS[:5]):
        if delay:
            time.sleep(delay)
        try:
            response = requests.post(
                EUMETSAT_TOKEN_URL,
                auth=(key, secret),
                data={"grant_type": "client_credentials"},
                timeout=60,
            )
            response.raise_for_status()
            token = response.json().get("access_token")
            if not token:
                raise RuntimeError("token_response_missing_access_token")
            _EUMETSAT_BEARER_TOKEN = str(token)
            return _EUMETSAT_BEARER_TOKEN
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if delay_index == len(RETRY_DELAYS_SECONDS[:5]):
                raise RuntimeError(f"eumetsat_token_failed: {last_error}") from exc
    raise RuntimeError(f"eumetsat_token_failed: {last_error}")


def eumetsat_search_features(collection_id: str, target_time: datetime) -> list[dict]:
    import requests

    start_search = target_time - timedelta(minutes=5)
    end_search = target_time + timedelta(minutes=20)
    params = {
        "format": "json",
        "pi": collection_id,
        "si": 0,
        "c": 100,
        "dtstart": start_search.isoformat(),
        "dtend": end_search.isoformat(),
    }
    last_error: Optional[str] = None
    for delay_index, delay in enumerate([0] + RETRY_DELAYS_SECONDS[:6]):
        if delay:
            time.sleep(delay)
        try:
            response = requests.get(
                EUMETSAT_SEARCH_URL,
                headers={"Authorization": f"Bearer {get_eumetsat_bearer_token()}"},
                params=params,
                timeout=90,
            )
            if response.status_code == 401:
                global _EUMETSAT_BEARER_TOKEN
                _EUMETSAT_BEARER_TOKEN = None
                response = requests.get(
                    EUMETSAT_SEARCH_URL,
                    headers={"Authorization": f"Bearer {get_eumetsat_bearer_token()}"},
                    params=params,
                    timeout=90,
                )
            response.raise_for_status()
            payload = response.json()
            return list(payload.get("features") or [])
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if delay_index == len(RETRY_DELAYS_SECONDS[:6]):
                raise RuntimeError(f"eumetsat_search_failed: {last_error}") from exc
    raise RuntimeError(f"eumetsat_search_failed: {last_error}")


def product_time_attr(product, names: list[str]) -> Optional[datetime]:
    for name in names:
        value = getattr(product, name, None)
        if value is None and hasattr(product, "metadata"):
            try:
                value = product.metadata.get(name)
            except Exception:
                value = None
        if value is None:
            continue
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc)
        if isinstance(value, str):
            try:
                return parse_iso_utc(value)
            except Exception:
                continue
    return None


def product_size(product) -> Optional[int]:
    for name in ["size", "size_bytes", "content_length"]:
        value = getattr(product, name, None)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def product_identifier(product) -> str:
    for name in ["id", "identifier", "title", "name"]:
        value = getattr(product, name, None)
        if value:
            return str(value)
    return str(product)


def feature_identifier(feature: dict) -> str:
    props = feature.get("properties") or {}
    for value in [feature.get("id"), props.get("identifier"), props.get("title")]:
        if value:
            return str(value)
    return ""


def feature_time_attr(feature: dict, which: str) -> Optional[datetime]:
    props = feature.get("properties") or {}
    date_range = props.get("date")
    if isinstance(date_range, str) and date_range:
        parts = date_range.split("/")
        index = 0 if which == "start" else min(1, len(parts) - 1)
        try:
            return parse_iso_utc(parts[index])
        except Exception:
            pass
    for key in ([which, f"sensing_{which}", "beginposition"] if which == "start" else [which, f"sensing_{which}", "endposition"]):
        value = props.get(key)
        if isinstance(value, str):
            try:
                return parse_iso_utc(value)
            except Exception:
                continue
    return None


def feature_size(feature: dict) -> Optional[int]:
    props = feature.get("properties") or {}
    product_info = props.get("productInformation") or {}
    for value in [product_info.get("size"), props.get("size"), props.get("contentLength")]:
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def pick_meteosat_feature(features: list[dict], target_time: datetime):
    if not features:
        return None

    target_end = target_time + timedelta(minutes=15)
    enriched = []
    for feature in features:
        start = feature_time_attr(feature, "start")
        end = feature_time_attr(feature, "end")
        covers_target = bool(start and end and start <= target_time and end >= target_end)
        sort_start = start or target_time + timedelta(days=999)
        enriched.append((not covers_target, abs((sort_start - target_time).total_seconds()), feature, start, end))
    _, _, feature, start, end = sorted(enriched, key=lambda item: (item[0], item[1], feature_identifier(item[2])))[0]
    return feature, start, end


def product_summary(product) -> dict:
    def iso(value):
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return value or ""

    entries = []
    try:
        entries = list(product.entries)
    except Exception as exc:
        entries = [f"entries_error={type(exc).__name__}: {exc}"]

    return {
        "id": product_identifier(product),
        "url": getattr(product, "url", ""),
        "size_bytes": product_size(product) or "",
        "format": getattr(product, "format", ""),
        "satellite": getattr(product, "satellite", ""),
        "sensing_start": iso(getattr(product, "sensing_start", None)),
        "sensing_end": iso(getattr(product, "sensing_end", None)),
        "entries": entries,
    }


def pick_meteosat_product(collection, target_time: datetime):
    start_search = target_time - timedelta(minutes=5)
    end_search = target_time + timedelta(minutes=20)
    products = list(collection.search(dtstart=start_search, dtend=end_search))
    return pick_meteosat_product_from_products(products, target_time)


def pick_meteosat_product_from_products(products: list, target_time: datetime):
    if not products:
        return None

    target_end = target_time + timedelta(minutes=15)
    enriched = []
    for product in products:
        start = product_time_attr(product, ["sensing_start", "start", "start_time", "beginposition"])
        end = product_time_attr(product, ["sensing_end", "end", "end_time", "endposition"])
        covers_target = bool(start and end and start <= target_time and end >= target_end)
        sort_start = start or target_time + timedelta(days=999)
        enriched.append((not covers_target, abs((sort_start - target_time).total_seconds()), product, start, end))
    _, _, product, start, end = sorted(enriched, key=lambda item: (item[0], item[1], product_identifier(item[2])))[0]
    return product, start, end


def meteosat_search_products(datastore, collection_id: str, target_time: datetime) -> list:
    products = []
    for feature in eumetsat_search_features(collection_id, target_time):
        products.append(datastore.get_product_from_search_feature(collection_id, feature))
    return products


def run_meteosat_options(root: Path) -> Path:
    ensure_dirs(root)
    datastore = get_eumdac_datastore()
    rows = []
    for service, collections in METEOSAT_CONFIG.items():
        for collection_id, short_product in collections.items():
            try:
                collection = datastore.get_collection(collection_id)
                rows.append(
                    {
                        "service": service,
                        "product": short_product,
                        "collection_id": collection_id,
                        "title": collection.title,
                        "product_type": collection.product_type,
                        "search_options": collection.search_options,
                    }
                )
            except Exception as exc:
                rows.append(
                    {
                        "service": service,
                        "product": short_product,
                        "collection_id": collection_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
    out_path = manifest_path(root, "meteosat_collection_options.json")
    out_path.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    return out_path


def run_meteosat_smoke(root: Path, date: str, hour: int, minute: int = 0) -> Path:
    ensure_dirs(root)
    datastore = get_eumdac_datastore()
    target_time = datetime.fromisoformat(date).replace(
        hour=hour, minute=minute, second=0, microsecond=0, tzinfo=timezone.utc
    )
    results = {
        "target_time_utc": target_time.isoformat().replace("+00:00", "Z"),
        "search_window_utc": [
            (target_time - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
            (target_time + timedelta(minutes=20)).isoformat().replace("+00:00", "Z"),
        ],
        "services": [],
    }
    for service, collections in METEOSAT_CONFIG.items():
        for collection_id, short_product in collections.items():
            item = {
                "service": service,
                "product": short_product,
                "collection_id": collection_id,
                "products": [],
                "selected": None,
            }
            try:
                products = meteosat_search_products(datastore, collection_id, target_time)
                item["products"] = [product_summary(product) for product in products]
                picked = pick_meteosat_product_from_products(products, target_time)
                if picked:
                    item["selected"] = product_summary(picked[0])
            except Exception as exc:
                item["error"] = f"{type(exc).__name__}: {exc}"
            results["services"].append(item)
    out_path = manifest_path(root, f"meteosat_smoke_{date}_{hour:02d}{minute:02d}.json")
    out_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    return out_path


def run_meteosat_inventory_range(root: Path, start_date: str, end_date: str) -> Path:
    ensure_dirs(root)
    rows: list[dict] = []
    log_path = root / "logs" / "meteosat_inventory.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"{utc_now()} meteosat_inventory_start start={start_date} end={end_date}\n")
        times = iter_target_times_between(start_date, end_date)
        for idx, target_time in enumerate(times, start=1):
            rows.extend(inventory_meteosat(root, target_time))
            if idx % 24 == 0:
                log.write(f"{utc_now()} inventoried_through={target_time.isoformat()}\n")
                log.flush()
    out_path = manifest_path(root, "manifest_meteosat_inventory.csv")
    write_csv(out_path, rows)
    write_meteosat_inventory_summary(root, rows)
    return out_path


def write_meteosat_inventory_summary(root: Path, rows: list[dict]) -> None:
    found = [row for row in rows if row["status"] == "found"]
    total_size = sum(int(row["size_bytes"]) for row in found if str(row["size_bytes"]).isdigit())
    summary = {
        "created_at": utc_now(),
        "rows": len(rows),
        "found": len(found),
        "missing": sum(1 for row in rows if row["status"] == "missing"),
        "errors": sum(1 for row in rows if row["status"] == "error"),
        "estimated_size_bytes": total_size,
        "estimated_size_gib": round(total_size / (1024**3), 3),
    }
    manifest_path(root, "meteosat_inventory_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )


def run_download_meteosat_range(root: Path, start_date: str, end_date: str) -> Path:
    inventory = manifest_path(root, "manifest_meteosat_inventory.csv")
    if not inventory.exists():
        raise FileNotFoundError(f"Meteosat inventory not found: {inventory}")

    start_prefix = f"{start_date}T"
    end_dt = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc) + timedelta(days=1)
    rows = []
    for row in read_manifest(inventory):
        if row["status"] != "found" or row["remote_type"] != "eumetsat":
            continue
        target_dt = parse_iso_utc(row["target_time_utc"])
        if row["target_time_utc"] >= start_prefix and target_dt < end_dt:
            rows.append(row)

    skipped: list[dict] = []
    pending: list[dict] = []
    for row in rows:
        local = Path(row["local_path"])
        if local.exists():
            if os.environ.get("GEO_CLOUD_FAST_SKIP_EXISTING", "").strip() == "1" and local.stat().st_size > 0:
                ok, note = True, "fast_skip_existing_size_gt_0"
            else:
                ok, note = validate_file(local, row)
            if ok:
                out = dict(row)
                out["status"] = "downloaded"
                out["note"] = f"skipped_existing:{note}"
                skipped.append(out)
                continue
        pending.append(row)

    if os.environ.get("GEO_CLOUD_PRIORITIZE_GOES", "").strip() == "1":
        pending.sort(
            key=lambda row: (
                0 if row.get("platform", "").startswith("GOES-") else 1,
                row.get("target_time_utc", ""),
                row.get("platform", ""),
                row.get("product", ""),
            )
        )

    ok_space, space = enough_free_space(root, pending)
    space["total_rows"] = len(rows)
    space["skipped_existing_rows"] = len(skipped)
    space["pending_rows"] = len(pending)
    manifest_path(root, "meteosat_space_check.json").write_text(json.dumps(space, indent=2), encoding="utf-8")
    if not ok_space:
        raise RuntimeError(f"Not enough free space: {space}")

    datastore = get_eumdac_datastore()
    downloaded: list[dict] = list(skipped)
    log_path = root / "logs" / "download_meteosat_range.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(
            f"{utc_now()} download_meteosat_range_start start={start_date} end={end_date} "
            f"rows={len(rows)} skipped_existing={len(skipped)} pending={len(pending)} max_workers=1\n"
        )
        log.flush()
        for index, row in enumerate(pending, start=1):
            success, note = download_eumetsat_row(datastore, row)
            out = dict(row)
            out["status"] = "downloaded" if success else "corrupt"
            out["note"] = note
            downloaded.append(out)
            log.write(
                f"{utc_now()} {index}/{len(pending)} {out['status']} "
                f"{row['platform']} {row['product']} {row['target_time_utc']} {note}\n"
            )
            log.flush()

    out_path = manifest_path(root, "manifest_meteosat_downloaded.csv")
    write_csv(out_path, downloaded)
    write_meteosat_download_summary(root, downloaded)
    return out_path


def write_meteosat_download_summary(root: Path, rows: list[dict]) -> None:
    summary = {
        "created_at": utc_now(),
        "downloaded_rows": len(rows),
        "ok_rows": sum(1 for row in rows if row["status"] == "downloaded"),
        "corrupt_rows": sum(1 for row in rows if row["status"] == "corrupt"),
    }
    manifest_path(root, "meteosat_download_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )


def inventory_meteosat(root: Path, target_time: datetime) -> list[dict]:
    rows: list[dict] = []
    for service, collections in METEOSAT_CONFIG.items():
        for collection_id, short_product in collections.items():
            try:
                features = eumetsat_search_features(collection_id, target_time)
                picked = pick_meteosat_feature(features, target_time)
                if picked is None:
                    status = "missing"
                    remote_id = ""
                    start = None
                    end = None
                    size = ""
                    note = "no_product_in_t_minus_5_to_plus_20"
                    local_path = ""
                else:
                    feature, start, end = picked
                    remote_id = feature_identifier(feature)
                    filename = safe_filename(remote_id)
                    if not filename.lower().endswith(".zip"):
                        filename += ".zip"
                    status = "found"
                    size = feature_size(feature) or ""
                    note = "rest_search"
                    local_path = local_path_for(root, service, short_product, target_time, filename)
            except Exception as exc:
                status = "error"
                remote_id = ""
                start = None
                end = None
                size = ""
                note = f"{type(exc).__name__}: {exc}"
                local_path = ""
            rows.append(
                base_row(
                    target_time,
                    service,
                    service,
                    short_product,
                    collection_id,
                    "eumetsat",
                    "",
                    remote_id,
                    start,
                    end,
                    size,
                    status,
                    local_path,
                    note,
                )
            )
    return rows


def base_row(
    target_time: datetime,
    platform: str,
    service: str,
    product: str,
    collection_id: str,
    remote_type: str,
    bucket: str,
    remote_id: str,
    start_time: Optional[datetime],
    end_time: Optional[datetime],
    size_bytes,
    status: str,
    local_path,
    note: str,
) -> dict:
    if start_time:
        diff = int((start_time - target_time).total_seconds())
    else:
        diff = ""
    return {
        "target_time_utc": target_time.isoformat().replace("+00:00", "Z"),
        "platform": platform,
        "service": service,
        "product": product,
        "collection_id": collection_id,
        "remote_type": remote_type,
        "bucket": bucket,
        "remote_key_or_product_id": remote_id,
        "actual_start_time": start_time.isoformat().replace("+00:00", "Z") if start_time else "",
        "actual_end_time": end_time.isoformat().replace("+00:00", "Z") if end_time else "",
        "time_difference_seconds": diff,
        "size_bytes": size_bytes,
        "status": status,
        "local_path": str(local_path) if local_path else "",
        "note": note,
    }


def run_inventory(
    root: Path,
    include_meteosat: bool = True,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Path:
    ensure_dirs(root)
    s3_client = get_s3_client()
    datastore = get_eumdac_datastore() if include_meteosat else None
    rows: list[dict] = []
    log_path = root / "logs" / "inventory.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"{utc_now()} inventory_start include_meteosat={include_meteosat}\n")
        times = iter_target_times_between(start_date, end_date) if start_date and end_date else iter_target_times()
        for idx, target_time in enumerate(times, start=1):
            rows.extend(inventory_goes(root, s3_client, target_time))
            rows.extend(inventory_himawari(root, s3_client, target_time))
            if include_meteosat and datastore is not None:
                rows.extend(inventory_meteosat(root, target_time))
            if idx % 24 == 0:
                log.write(f"{utc_now()} inventoried_through={target_time.isoformat()}\n")
                log.flush()
    out_path = manifest_path(root, "manifest_inventory.csv")
    write_csv(out_path, rows)
    write_inventory_summary(root, rows)
    return out_path


def write_inventory_summary(root: Path, rows: list[dict]) -> None:
    found = [row for row in rows if row["status"] == "found"]
    total_size = sum(int(row["size_bytes"]) for row in found if str(row["size_bytes"]).isdigit())
    summary = {
        "created_at": utc_now(),
        "rows": len(rows),
        "found": len(found),
        "missing": sum(1 for row in rows if row["status"] == "missing"),
        "errors": sum(1 for row in rows if row["status"] == "error"),
        "estimated_size_bytes": total_size,
        "estimated_size_gib": round(total_size / (1024**3), 3),
    }
    manifest_path(root, "inventory_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def enough_free_space(root: Path, rows: list[dict], multiplier: float = 1.2) -> tuple[bool, dict]:
    drive = root.anchor or str(root)
    usage = shutil.disk_usage(drive)
    needed = sum(int(row["size_bytes"]) for row in rows if str(row.get("size_bytes", "")).isdigit())
    needed = int(needed * multiplier)
    return usage.free >= needed, {
        "drive": drive,
        "free_bytes": usage.free,
        "needed_bytes_with_margin": needed,
        "free_gib": round(usage.free / (1024**3), 3),
        "needed_gib_with_margin": round(needed / (1024**3), 3),
    }


def validate_file(path: Path, row: Optional[dict] = None) -> tuple[bool, str]:
    if not path.exists():
        return False, "missing_local_file"
    if path.stat().st_size <= 0:
        return False, "empty_file"
    suffixes = [suffix.lower() for suffix in path.suffixes]
    if ".zip" in suffixes:
        try:
            with zipfile.ZipFile(path) as zf:
                bad = zf.testzip()
                if bad:
                    return False, f"bad_zip_member={bad}"
                names = zf.namelist()
                if not names:
                    return False, "empty_zip"
            return True, "zip_ok"
        except Exception as exc:
            return False, f"zip_error={type(exc).__name__}: {exc}"

    try:
        from netCDF4 import Dataset

        with Dataset(path, "r") as ds:
            names = list(ds.variables.keys())
            if not names:
                return False, "no_variables"
            product = (row or {}).get("product", "")
            lowered = " ".join(name.lower() for name in names)
            if product == "ACMF" and not any(token in lowered for token in ["cloud", "mask", "bcm", "acm"]):
                return False, "acmf_expected_cloud_mask_variable_not_detected"
            if product == "ACHAF" and not any(token in lowered for token in ["height", "ht", "acha"]):
                return False, "achaf_expected_height_variable_not_detected"
            if product in {"CMSK", "CHGT"}:
                log_variable_table(path, names)
                if product == "CMSK" and not any(token in lowered for token in ["mask", "cloud", "cmsk"]):
                    return False, "cmsk_expected_mask_variable_not_detected"
                if product == "CHGT" and not any(token in lowered for token in ["height", "hgt", "chgt"]):
                    return False, "chgt_expected_height_variable_not_detected"
        return True, "netcdf_ok"
    except Exception as exc:
        return False, f"netcdf_error={type(exc).__name__}: {exc}"


def log_variable_table(path: Path, names: list[str]) -> None:
    root = find_root_from_path(path)
    if root is None:
        return
    out_path = root / "logs" / "himawari_variable_tables.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    exists = out_path.exists()
    with out_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if not exists:
            writer.writerow(["file", "variables"])
        writer.writerow([str(path), ";".join(names)])


def find_root_from_path(path: Path) -> Optional[Path]:
    parts = path.resolve().parts
    for index, part in enumerate(parts):
        if part == "GEO_Cloud_2024":
            return Path(*parts[: index + 1])
    return None


def download_s3_row(s3_client, row: dict) -> tuple[bool, str]:
    target = Path(row["local_path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    if target.exists():
        ok, note = validate_file(target, row)
        if ok:
            return True, f"skipped_existing:{note}"
    for delay_index, delay in enumerate([0] + RETRY_DELAYS_SECONDS):
        if delay:
            time.sleep(delay)
        try:
            if tmp.exists():
                tmp.unlink()
            response = s3_client.get_object(Bucket=row["bucket"], Key=row["remote_key_or_product_id"])
            with tmp.open("wb") as handle:
                for chunk in iter(lambda: response["Body"].read(S3_CHUNK_SIZE), b""):
                    handle.write(chunk)
            ok, note = validate_file(tmp, row)
            if not ok:
                raise RuntimeError(note)
            replace_with_retry(tmp, target)
            return True, note
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if tmp.exists():
                tmp.unlink()
            if delay_index == len(RETRY_DELAYS_SECONDS):
                return False, last_error
    return False, "unreachable_retry_state"


def locate_eumetsat_product(datastore, row: dict):
    row_id = row["remote_key_or_product_id"]
    try:
        return datastore.get_product(row["collection_id"], row_id)
    except Exception:
        pass

    target_time = parse_iso_utc(row["target_time_utc"])
    products = meteosat_search_products(datastore, row["collection_id"], target_time)
    for product in products:
        if product_identifier(product) == row_id or str(product) == row_id:
            return product
    if products:
        return products[0]
    raise RuntimeError("product_not_found_in_repeat_search")


def download_eumetsat_row(datastore, row: dict) -> tuple[bool, str]:
    target = Path(row["local_path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    if target.exists():
        ok, note = validate_file(target, row)
        if ok:
            return True, f"skipped_existing:{note}"
    for delay_index, delay in enumerate([0] + RETRY_DELAYS_SECONDS):
        if delay:
            time.sleep(delay)
        try:
            if tmp.exists():
                tmp.unlink()
            product = locate_eumetsat_product(datastore, row)
            with product.open() as src, tmp.open("wb") as dst:
                while True:
                    chunk = src.read(EUMETSAT_CHUNK_SIZE)
                    if not chunk:
                        break
                    dst.write(chunk)
            ok, note = validate_file(tmp, row)
            if not ok:
                raise RuntimeError(note)
            replace_with_retry(tmp, target)
            return True, note
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if tmp.exists():
                tmp.unlink()
            if delay_index == len(RETRY_DELAYS_SECONDS):
                return False, last_error
    return False, "unreachable_retry_state"


def run_download_test_day(root: Path, test_day: str = TEST_DAY) -> Path:
    inventory = manifest_path(root, "manifest_inventory.csv")
    if not inventory.exists():
        raise FileNotFoundError(f"Inventory not found: {inventory}")
    rows = [row for row in read_manifest(inventory) if row["status"] == "found" and row["target_time_utc"].startswith(test_day)]
    ok_space, space = enough_free_space(root, rows)
    manifest_path(root, "test_day_space_check.json").write_text(json.dumps(space, indent=2), encoding="utf-8")
    if not ok_space:
        raise RuntimeError(f"Not enough free space: {space}")

    s3_client = get_s3_client()
    datastore = get_eumdac_datastore() if any(row["remote_type"] == "eumetsat" for row in rows) else None
    downloaded: list[dict] = []
    log_path = root / "logs" / "download_test_day.log"
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"{utc_now()} download_test_day_start rows={len(rows)}\n")
        for index, row in enumerate(rows, start=1):
            if row["remote_type"] == "s3":
                success, note = download_s3_row(s3_client, row)
            else:
                success, note = download_eumetsat_row(datastore, row)
            out = dict(row)
            out["status"] = "downloaded" if success else "corrupt"
            out["note"] = note
            downloaded.append(out)
            log.write(f"{utc_now()} {index}/{len(rows)} {out['status']} {row['platform']} {row['product']} {note}\n")
            log.flush()
    out_path = manifest_path(root, "manifest_downloaded.csv")
    write_csv(out_path, downloaded)
    run_validate(root)
    return out_path


def run_download_s3_range(
    root: Path,
    start_date: str,
    end_date: str,
    max_workers: int = 8,
) -> Path:
    inventory = manifest_path(root, "manifest_inventory.csv")
    if not inventory.exists():
        raise FileNotFoundError(f"Inventory not found: {inventory}")

    start_prefix = f"{start_date}T"
    end_dt = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc) + timedelta(days=1)
    rows = []
    for row in read_manifest(inventory):
        if row["status"] != "found" or row["remote_type"] != "s3":
            continue
        target_dt = parse_iso_utc(row["target_time_utc"])
        if row["target_time_utc"] >= start_prefix and target_dt < end_dt:
            rows.append(row)

    skipped: list[dict] = []
    pending: list[dict] = []
    for row in rows:
        local = Path(row["local_path"])
        if local.exists():
            ok, note = validate_file(local, row)
            if ok:
                out = dict(row)
                out["status"] = "downloaded"
                out["note"] = f"skipped_existing:{note}"
                skipped.append(out)
                continue
        pending.append(row)

    if os.environ.get("GEO_CLOUD_PRIORITIZE_GOES", "").strip() == "1":
        pending.sort(
            key=lambda row: (
                0 if row.get("platform", "").startswith("GOES-") else 1,
                row.get("target_time_utc", ""),
                row.get("platform", ""),
                row.get("product", ""),
            )
        )

    ok_space, space = enough_free_space(root, pending)
    space["total_rows"] = len(rows)
    space["skipped_existing_rows"] = len(skipped)
    space["pending_rows"] = len(pending)
    manifest_path(root, "s3_range_space_check.json").write_text(json.dumps(space, indent=2), encoding="utf-8")
    if not ok_space:
        raise RuntimeError(f"Not enough free space: {space}")

    downloaded: list[dict] = list(skipped)
    log_path = root / "logs" / "download_s3_range.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(
            f"{utc_now()} download_s3_range_start start={start_date} end={end_date} "
            f"rows={len(rows)} skipped_existing={len(skipped)} pending={len(pending)} "
            f"max_workers={max_workers}\n"
        )
        log.flush()

        def worker(row: dict) -> dict:
            s3_client = get_s3_client(s3_proxy_for_row(row))
            success, note = download_s3_row(s3_client, row)
            out = dict(row)
            out["status"] = "downloaded" if success else "corrupt"
            out["note"] = note
            return out

        completed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(worker, row) for row in pending]
            for future in as_completed(futures):
                completed += 1
                try:
                    out = future.result()
                except Exception as exc:
                    out = {field: "" for field in MANIFEST_FIELDS}
                    out["status"] = "corrupt"
                    out["note"] = f"{type(exc).__name__}: {exc}"
                downloaded.append(out)
                log.write(
                    f"{utc_now()} {completed}/{len(pending)} {out.get('status')} "
                    f"{out.get('platform')} {out.get('product')} {out.get('target_time_utc')} "
                    f"{out.get('note')}\n"
                )
                log.flush()

    out_path = manifest_path(root, "manifest_downloaded.csv")
    write_csv(out_path, downloaded)
    run_validate(root)
    return out_path


def run_validate(root: Path) -> None:
    inventory_path = manifest_path(root, "manifest_inventory.csv")
    downloaded_path = manifest_path(root, "manifest_downloaded.csv")
    inventory_rows = read_manifest(inventory_path) if inventory_path.exists() else []
    downloaded_rows = read_manifest(downloaded_path) if downloaded_path.exists() else []

    missing_rows = [row for row in inventory_rows if row["status"] != "found"]
    corrupt_rows: list[dict] = []
    seen: dict[str, dict] = {}
    duplicate_rows: list[dict] = []

    for row in downloaded_rows:
        local_path = row.get("local_path", "")
        if not local_path:
            corrupt_rows.append({**row, "note": "no_local_path"})
            continue
        key = local_path.lower()
        if key in seen:
            duplicate_rows.extend([seen[key], row])
        else:
            seen[key] = row
        ok, note = validate_file(Path(local_path), row)
        if not ok:
            corrupt_rows.append({**row, "note": note})

    write_csv(manifest_path(root, "missing_targets.csv"), missing_rows)
    write_csv(manifest_path(root, "corrupt_files.csv"), corrupt_rows)
    write_csv(manifest_path(root, "duplicate_files.csv"), duplicate_rows)

    summary = {
        "created_at": utc_now(),
        "inventory_rows": len(inventory_rows),
        "downloaded_rows": len(downloaded_rows),
        "missing_rows": len(missing_rows),
        "corrupt_rows": len(corrupt_rows),
        "duplicate_rows": len(duplicate_rows),
    }
    manifest_path(root, "download_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inventory and download GEO cloud products.")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="Download root directory.")
    sub = parser.add_subparsers(dest="command", required=True)

    inventory_parser = sub.add_parser("inventory", help="Build remote inventory only.")
    inventory_parser.add_argument("--skip-meteosat", action="store_true", help="Do not query EUMETSAT.")
    inventory_parser.add_argument("--start-date", help="Optional inventory start date, YYYY-MM-DD.")
    inventory_parser.add_argument("--end-date", help="Optional inventory end date, YYYY-MM-DD.")

    test_parser = sub.add_parser("download-test-day", help="Download and validate the test day.")
    test_parser.add_argument("--date", default=TEST_DAY, help="UTC test day, YYYY-MM-DD.")

    s3_parser = sub.add_parser("download-s3-range", help="Download S3 rows from inventory for a date range.")
    s3_parser.add_argument("--start-date", required=True, help="UTC start date, YYYY-MM-DD.")
    s3_parser.add_argument("--end-date", required=True, help="UTC end date, YYYY-MM-DD.")
    s3_parser.add_argument("--max-workers", type=int, default=8, help="Concurrent S3 downloads.")

    sub.add_parser("validate", help="Validate downloaded files and write reports.")

    sub.add_parser("meteosat-options", help="Write EUMETSAT collection titles and search options.")

    met_smoke = sub.add_parser("meteosat-smoke", help="Search Meteosat collections for one UTC time.")
    met_smoke.add_argument("--date", default=TEST_DAY, help="UTC date, YYYY-MM-DD.")
    met_smoke.add_argument("--hour", type=int, default=0, help="UTC hour.")
    met_smoke.add_argument("--minute", type=int, default=0, help="UTC minute.")

    met_inventory = sub.add_parser("meteosat-inventory", help="Build Meteosat-only inventory for a date range.")
    met_inventory.add_argument("--start-date", required=True, help="UTC start date, YYYY-MM-DD.")
    met_inventory.add_argument("--end-date", required=True, help="UTC end date, YYYY-MM-DD.")

    met_download = sub.add_parser("download-meteosat-range", help="Download Meteosat rows from Meteosat inventory.")
    met_download.add_argument("--start-date", required=True, help="UTC start date, YYYY-MM-DD.")
    met_download.add_argument("--end-date", required=True, help="UTC end date, YYYY-MM-DD.")

    first = sub.add_parser("first-round", help="Run inventory then test-day download.")
    first.add_argument("--date", default=TEST_DAY, help="UTC test day, YYYY-MM-DD.")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    root = Path(args.root)
    try:
        if args.command == "inventory":
            out = run_inventory(
                root,
                include_meteosat=not args.skip_meteosat,
                start_date=args.start_date,
                end_date=args.end_date,
            )
            print(out)
        elif args.command == "download-test-day":
            out = run_download_test_day(root, args.date)
            print(out)
        elif args.command == "download-s3-range":
            out = run_download_s3_range(root, args.start_date, args.end_date, args.max_workers)
            print(out)
        elif args.command == "validate":
            run_validate(root)
            print(manifest_path(root, "download_summary.json"))
        elif args.command == "meteosat-options":
            out = run_meteosat_options(root)
            print(out)
        elif args.command == "meteosat-smoke":
            out = run_meteosat_smoke(root, args.date, args.hour, args.minute)
            print(out)
        elif args.command == "meteosat-inventory":
            out = run_meteosat_inventory_range(root, args.start_date, args.end_date)
            print(out)
        elif args.command == "download-meteosat-range":
            out = run_download_meteosat_range(root, args.start_date, args.end_date)
            print(out)
        elif args.command == "first-round":
            run_inventory(root, include_meteosat=True)
            out = run_download_test_day(root, args.date)
            print(out)
        return 0
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
