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
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from pathlib import Path
from typing import Callable, Iterable, Optional

CORE_CODE_ROOT = Path(__file__).resolve().parents[1] / "geo_ring_cloud_stage1"
if str(CORE_CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_CODE_ROOT))

from geo_ring_cloud.lineage import write_manifest as write_lineage_manifest  # noqa: E402
from geo_ring_cloud.paths import EXTERNAL_GEO_CLOUD_ROOT, PROJECT_ROOT  # noqa: E402


COMPONENT_ROLE = "data_download_orchestrator"
DOWNLOAD_MONTHS = [(2024, 1), (2024, 3), (2024, 5)]
TEST_DAY = "2024-03-12"
DEFAULT_ROOT = EXTERNAL_GEO_CLOUD_ROOT
RETRY_DELAYS_SECONDS = [5, 10, 20, 40, 60, 120, 180, 300]
DEFAULT_S3_RANGE_MIB = 4
DEFAULT_INVENTORY_WORKERS = 8
MAX_S3_WORKERS = 16
MAX_INVENTORY_WORKERS = 16
MAX_METEOSAT_WORKERS = 8
DEFAULT_ADAPTIVE_MIN_WORKERS = 2
DEFAULT_ADAPTIVE_INITIAL_WORKERS = 4
ADAPTIVE_TUNE_SECONDS = 30.0
EUMETSAT_CHUNK_SIZE = 1024 * 512
EUMETSAT_SEARCH_URL = "https://api.eumetsat.int/data/search-products/1.0.0/os"
EUMETSAT_TOKEN_URL = "https://api.eumetsat.int/token"
_EUMETSAT_BEARER_TOKEN: Optional[str] = None
_EUMETSAT_TOKEN_LOCK = threading.Lock()
INVENTORY_SCHEMA_VERSION = 2
RELATED_STAGE_IDS = ("stage_00", "stage_00f")

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

PLATFORM_CHOICES = tuple(
    list(GOES_CONFIG)
    + [HIMAWARI_CONFIG["platform"]]
    + list(METEOSAT_CONFIG)
)

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


def iter_days_between(start_date: str, end_date: str) -> Iterable[datetime]:
    start = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def validate_worker_count(value: int, maximum: int, label: str) -> int:
    if value < 1 or value > maximum:
        raise ValueError(f"{label} must be between 1 and {maximum}, got {value}")
    return value


def disable_proxy_environment() -> None:
    """Force every supported provider to use a direct network connection."""
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "GEO_CLOUD_GOES_PROXY",
        "GEO_CLOUD_HIMAWARI_PROXY",
        "GEO_CLOUD_S3_PROXY",
        "GEO_RING_LOCAL_PROXY",
    ):
        os.environ.pop(name, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"


def inventory_request(
    kind: str,
    start_date: str,
    end_date: str,
    platforms: Iterable[str],
    inventory_workers: int,
) -> dict:
    semantic_payload = {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "kind": kind,
        "start_date": start_date,
        "end_date": end_date,
        "platforms": sorted(set(platforms)),
        "network_mode": "direct_only",
    }
    canonical = json.dumps(semantic_payload, sort_keys=True, separators=(",", ":"))
    payload = {**semantic_payload, "inventory_workers": inventory_workers}
    payload["fingerprint"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return payload


def inventory_cache_matches(
    csv_path: Path,
    lineage_path: Path,
    request: dict,
) -> bool:
    if not csv_path.is_file() or not lineage_path.is_file():
        return False
    try:
        payload = json.loads(lineage_path.read_text(encoding="utf-8"))
        if payload.get("inventory_fingerprint") != request["fingerprint"]:
            return False
        rows = read_manifest(csv_path)
        return bool(rows) and int(payload.get("row_count", -1)) == len(rows)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


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


def choose_adaptive_worker_count(
    current_workers: int,
    min_workers: int,
    max_workers: int,
    current_rate_bps: float,
    previous_rate_bps: Optional[float],
    completed_count: int,
    failed_count: int,
) -> tuple[int, str]:
    """Choose the next conservative concurrency using AIMD-like feedback."""
    current_workers = max(min_workers, min(max_workers, current_workers))
    attempts = completed_count + failed_count
    error_ratio = failed_count / attempts if attempts else 0.0
    if failed_count >= 2 or error_ratio >= 0.20:
        return max(min_workers, current_workers - 1), "errors_backoff"
    if (
        previous_rate_bps is not None
        and previous_rate_bps > 0
        and current_rate_bps < previous_rate_bps * 0.75
    ):
        return max(min_workers, current_workers - 1), "throughput_backoff"
    if failed_count == 0 and completed_count > 0 and current_workers < max_workers:
        if previous_rate_bps is None or current_rate_bps >= previous_rate_bps * 0.90:
            return current_workers + 1, "throughput_probe_up"
    return current_workers, "steady"


def write_download_parallelism_status(root: Path, payload: dict) -> None:
    path = root / "logs" / "download_parallelism_status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    replace_with_retry(temporary, path)


def run_download_pool(
    root: Path,
    pending: list[dict],
    worker: Callable[[dict], dict],
    provider: str,
    max_workers: int,
    adaptive_workers: bool,
    min_workers: int,
    initial_workers: int,
    log,
) -> list[dict]:
    """Run downloads with a dynamic submission limit and persist tuning evidence."""
    min_workers = max(1, min(min_workers, max_workers))
    target_workers = (
        max(min_workers, min(initial_workers, max_workers))
        if adaptive_workers
        else max_workers
    )
    mode = "adaptive" if adaptive_workers else "fixed"
    outputs: list[dict] = []
    iterator = iter(pending)
    exhausted = False
    completed_total = 0
    window_completed = 0
    window_failed = 0
    window_bytes = 0
    previous_rate: Optional[float] = None
    window_started = time.monotonic()

    def status(reason: str, active_workers: int, rate_bps: Optional[float] = None) -> None:
        write_download_parallelism_status(
            root,
            {
                "project_id": "geo_ring_cloud",
                "canonical_stage_id": "",
                "component_role": COMPONENT_ROLE,
                "related_stage_ids": list(RELATED_STAGE_IDS),
                "updated_at": utc_now(),
                "status": "RUNNING" if completed_total < len(pending) else "COMPLETE",
                "provider": provider,
                "mode": mode,
                "current_workers": target_workers,
                "active_workers": active_workers,
                "min_workers": min_workers,
                "max_workers": max_workers,
                "reason": reason,
                "rate_bps": rate_bps,
                "completed_files": completed_total,
                "total_files": len(pending),
                "network_mode": "direct_only",
            },
        )

    status("initial", 0)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        active = {}

        def fill_slots() -> None:
            nonlocal exhausted
            while not exhausted and len(active) < target_workers:
                try:
                    row = next(iterator)
                except StopIteration:
                    exhausted = True
                    break
                active[executor.submit(worker, row)] = row

        fill_slots()
        status("running", len(active))
        while active:
            done, _ = wait(tuple(active), timeout=1.0, return_when=FIRST_COMPLETED)
            if not done:
                continue
            for future in done:
                row = active.pop(future)
                completed_total += 1
                try:
                    out = future.result()
                except Exception as exc:
                    out = dict(row)
                    out["status"] = "corrupt"
                    out["note"] = f"{type(exc).__name__}: {exc}"
                outputs.append(out)
                if out.get("status") == "downloaded":
                    window_completed += 1
                    try:
                        window_bytes += int(row.get("size_bytes") or 0)
                    except (TypeError, ValueError):
                        pass
                else:
                    window_failed += 1
                log.write(
                    f"{utc_now()} {completed_total}/{len(pending)} {out.get('status')} "
                    f"{out.get('platform')} {out.get('product')} {out.get('target_time_utc')} "
                    f"{out.get('note')}\n"
                )
                log.flush()

            elapsed = max(0.001, time.monotonic() - window_started)
            enough_samples = window_completed + window_failed >= max(2, target_workers * 2)
            should_tune = adaptive_workers and (
                window_failed >= 2
                or elapsed >= ADAPTIVE_TUNE_SECONDS
                or (enough_samples and elapsed >= 3.0)
            )
            reason = "running"
            rate: Optional[float] = None
            if should_tune:
                rate = window_bytes / elapsed
                target_workers, reason = choose_adaptive_worker_count(
                    target_workers,
                    min_workers,
                    max_workers,
                    rate,
                    previous_rate,
                    window_completed,
                    window_failed,
                )
                log.write(
                    f"{utc_now()} download_parallelism provider={provider} mode=adaptive "
                    f"workers={target_workers} rate_bps={rate:.1f} reason={reason} "
                    f"window_ok={window_completed} window_fail={window_failed}\n"
                )
                log.flush()
                previous_rate = rate
                window_started = time.monotonic()
                window_completed = 0
                window_failed = 0
                window_bytes = 0
            fill_slots()
            status(reason, len(active), rate)
    status("complete", 0, previous_rate)
    return outputs


def read_manifest(path: Path) -> list[dict]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def get_s3_client():
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config

    return boto3.client(
        "s3",
        config=Config(
            signature_version=UNSIGNED,
            proxies={},
            connect_timeout=20,
            read_timeout=45,
            retries={"max_attempts": 3, "mode": "standard"},
            tcp_keepalive=True,
        ),
    )


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


def inventory_goes(
    root: Path,
    s3_client,
    target_time: datetime,
    platforms: Optional[set[str]] = None,
) -> list[dict]:
    rows: list[dict] = []
    year = target_time.strftime("%Y")
    doy = target_time.strftime("%j")
    hour = target_time.strftime("%H")
    for platform, cfg in GOES_CONFIG.items():
        if platforms is not None and platform not in platforms:
            continue
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


def inventory_goes_day(root: Path, day: datetime, platform: str, full_product: str) -> list[dict]:
    """List one daily prefix once, then select all 24 hourly targets locally."""
    cfg = GOES_CONFIG[platform]
    short_product = cfg["short_products"][full_product]
    prefix = f"{full_product}/{day:%Y}/{day:%j}/"
    try:
        objects = list_s3_objects(get_s3_client(), cfg["bucket"], prefix)
        listing_error = ""
    except Exception as exc:
        objects = []
        listing_error = f"{type(exc).__name__}: {exc}"

    rows: list[dict] = []
    for hour in range(24):
        target_time = day.replace(hour=hour)
        candidate = pick_goes_candidate(objects, target_time)
        if candidate:
            filename = Path(candidate.remote_id).name
            rows.append(
                base_row(
                    target_time,
                    platform,
                    cfg["service"],
                    short_product,
                    "",
                    "s3",
                    cfg["bucket"],
                    candidate.remote_id,
                    candidate.start_time,
                    candidate.end_time,
                    candidate.size_bytes or "",
                    "found",
                    local_path_for(root, platform, short_product, target_time, filename),
                    f"daily_prefix={prefix};{candidate.note}",
                )
            )
        else:
            rows.append(
                base_row(
                    target_time,
                    platform,
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
                    listing_error or f"no_candidate_within_5min daily_prefix={prefix}",
                )
            )
    return rows


def inventory_himawari_day(root: Path, day: datetime) -> list[dict]:
    """List one Himawari daily prefix once and build the hourly rows locally."""
    cfg = HIMAWARI_CONFIG
    prefix = f"{cfg['base_prefix']}/{day:%Y/%m/%d}/"
    try:
        objects = list_s3_objects(get_s3_client(), cfg["bucket"], prefix)
        listing_error = ""
    except Exception as exc:
        objects = []
        listing_error = f"{type(exc).__name__}: {exc}"

    rows: list[dict] = []
    for hour in range(24):
        target_time = day.replace(hour=hour)
        hour_prefix = f"{prefix}{hour:02d}00/"
        hourly = [obj for obj in objects if obj.get("Key", "").startswith(hour_prefix)]
        for file_prefix, short_product in cfg["prefixes"].items():
            matching = [obj for obj in hourly if Path(obj["Key"]).name.startswith(file_prefix)]
            if matching:
                obj = min(matching, key=lambda item: item["Key"])
                filename = Path(obj["Key"]).name
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
                        local_path_for(root, cfg["platform"], short_product, target_time, filename),
                        f"daily_prefix={prefix}",
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
                        listing_error or f"no_file_prefix={file_prefix} hourly_prefix={hour_prefix}",
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

    with _EUMETSAT_TOKEN_LOCK:
        if _EUMETSAT_BEARER_TOKEN:
            return _EUMETSAT_BEARER_TOKEN
        last_error: Optional[str] = None
        session = requests.Session()
        session.trust_env = False
        for delay_index, delay in enumerate([0] + RETRY_DELAYS_SECONDS[:5]):
            if delay:
                time.sleep(delay)
            try:
                response = session.post(
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


def eumetsat_search_features_range(
    collection_id: str,
    start_search: datetime,
    end_search: datetime,
    count: int = 1000,
) -> list[dict]:
    import requests

    params = {
        "format": "json",
        "pi": collection_id,
        "si": 0,
        "c": count,
        "dtstart": start_search.isoformat(),
        "dtend": end_search.isoformat(),
    }
    last_error: Optional[str] = None
    session = requests.Session()
    session.trust_env = False
    for delay_index, delay in enumerate([0] + RETRY_DELAYS_SECONDS[:6]):
        if delay:
            time.sleep(delay)
        try:
            response = session.get(
                EUMETSAT_SEARCH_URL,
                headers={"Authorization": f"Bearer {get_eumetsat_bearer_token()}"},
                params=params,
                timeout=90,
            )
            if response.status_code == 401:
                global _EUMETSAT_BEARER_TOKEN
                _EUMETSAT_BEARER_TOKEN = None
                response = session.get(
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


def eumetsat_search_features(collection_id: str, target_time: datetime) -> list[dict]:
    return eumetsat_search_features_range(
        collection_id,
        target_time - timedelta(minutes=5),
        target_time + timedelta(minutes=20),
        count=100,
    )


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


def run_meteosat_inventory_range(
    root: Path,
    start_date: str,
    end_date: str,
    platforms: Optional[set[str]] = None,
) -> Path:
    ensure_dirs(root)
    rows: list[dict] = []
    log_path = root / "logs" / "meteosat_inventory.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"{utc_now()} meteosat_inventory_start start={start_date} end={end_date}\n")
        times = iter_target_times_between(start_date, end_date)
        for idx, target_time in enumerate(times, start=1):
            rows.extend(inventory_meteosat(root, target_time, platforms))
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


def run_download_meteosat_range(
    root: Path,
    start_date: str,
    end_date: str,
    platforms: Optional[set[str]] = None,
    max_workers: int = 2,
    adaptive_workers: bool = False,
    min_workers: int = DEFAULT_ADAPTIVE_MIN_WORKERS,
    initial_workers: int = DEFAULT_ADAPTIVE_INITIAL_WORKERS,
) -> Path:
    disable_proxy_environment()
    max_workers = validate_worker_count(max_workers, MAX_METEOSAT_WORKERS, "max_workers")
    combined_inventory = manifest_path(root, "manifest_inventory.csv")
    meteosat_inventory = manifest_path(root, "manifest_meteosat_inventory.csv")
    inventory = combined_inventory if combined_inventory.exists() else meteosat_inventory
    if not inventory.exists():
        raise FileNotFoundError(f"Meteosat inventory not found: {inventory}")

    start_prefix = f"{start_date}T"
    end_dt = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc) + timedelta(days=1)
    rows = []
    for row in read_manifest(inventory):
        if row["status"] != "found" or row["remote_type"] != "eumetsat":
            continue
        if platforms is not None and row.get("platform") not in platforms:
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

    downloaded: list[dict] = list(skipped)
    thread_state = threading.local()
    log_path = root / "logs" / "download_meteosat_range.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(
            f"{utc_now()} download_meteosat_range_start start={start_date} end={end_date} "
            f"rows={len(rows)} skipped_existing={len(skipped)} pending={len(pending)} "
            f"max_workers={max_workers} adaptive_workers={adaptive_workers} "
            f"initial_workers={initial_workers} network_mode=direct_only\n"
        )
        log.flush()

        def worker(row: dict) -> dict:
            if not hasattr(thread_state, "datastore"):
                thread_state.datastore = get_eumdac_datastore()
            success, note = download_eumetsat_row(thread_state.datastore, row)
            out = dict(row)
            out["status"] = "downloaded" if success else "corrupt"
            out["note"] = note
            return out

        downloaded.extend(
            run_download_pool(
                root,
                pending,
                worker,
                "eumetsat",
                max_workers,
                adaptive_workers,
                min_workers,
                initial_workers,
                log,
            )
        )

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


def inventory_meteosat(
    root: Path,
    target_time: datetime,
    platforms: Optional[set[str]] = None,
) -> list[dict]:
    rows: list[dict] = []
    for service, collections in METEOSAT_CONFIG.items():
        if platforms is not None and service not in platforms:
            continue
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


def inventory_meteosat_day(
    root: Path,
    day: datetime,
    service: str,
    collection_id: str,
) -> list[dict]:
    """Search one EUMETSAT collection once for a day, then select 24 hourly targets."""
    short_product = METEOSAT_CONFIG[service][collection_id]
    start_search = day - timedelta(minutes=5)
    end_search = day + timedelta(days=1, minutes=20)
    try:
        features = eumetsat_search_features_range(collection_id, start_search, end_search)
        listing_error = ""
    except Exception as exc:
        features = []
        listing_error = f"{type(exc).__name__}: {exc}"

    rows: list[dict] = []
    for hour in range(24):
        target_time = day.replace(hour=hour)
        window_start = target_time - timedelta(minutes=5)
        window_end = target_time + timedelta(minutes=20)
        hourly_features = []
        for feature in features:
            feature_start = feature_time_attr(feature, "start")
            feature_end = feature_time_attr(feature, "end")
            if feature_start and feature_end:
                if feature_end >= window_start and feature_start <= window_end:
                    hourly_features.append(feature)
            elif feature_start and window_start <= feature_start <= window_end:
                hourly_features.append(feature)
        picked = pick_meteosat_feature(hourly_features, target_time)
        if picked is None:
            rows.append(
                base_row(
                    target_time,
                    service,
                    service,
                    short_product,
                    collection_id,
                    "eumetsat",
                    "",
                    "",
                    None,
                    None,
                    "",
                    "error" if listing_error else "missing",
                    "",
                    listing_error or "no_product_in_daily_search",
                )
            )
            continue
        feature, start, end = picked
        remote_id = feature_identifier(feature)
        filename = safe_filename(remote_id)
        if not filename.lower().endswith(".zip"):
            filename += ".zip"
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
                feature_size(feature) or "",
                "found",
                local_path_for(root, service, short_product, target_time, filename),
                "daily_rest_search",
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
    platforms: Optional[set[str]] = None,
    inventory_workers: int = DEFAULT_INVENTORY_WORKERS,
    refresh_inventory: bool = False,
) -> Path:
    disable_proxy_environment()
    ensure_dirs(root)
    inventory_workers = validate_worker_count(
        inventory_workers, MAX_INVENTORY_WORKERS, "inventory_workers"
    )
    selected = set(PLATFORM_CHOICES) if platforms is None else set(platforms)
    unknown = selected.difference(PLATFORM_CHOICES)
    if unknown:
        raise ValueError(f"Unknown platforms: {','.join(sorted(unknown))}")
    include_goes = bool(selected.intersection(GOES_CONFIG))
    include_himawari = HIMAWARI_CONFIG["platform"] in selected
    include_selected_meteosat = include_meteosat and bool(selected.intersection(METEOSAT_CONFIG))
    if bool(start_date) != bool(end_date):
        raise ValueError("start_date and end_date must be supplied together")
    if start_date and end_date:
        days = list(iter_days_between(start_date, end_date))
        effective_start = start_date
        effective_end = end_date
    else:
        days = [
            day
            for year, month in DOWNLOAD_MONTHS
            for day in iter_days_between(
                f"{year:04d}-{month:02d}-01",
                f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}",
            )
        ]
        effective_start = ",".join(f"{year:04d}-{month:02d}" for year, month in DOWNLOAD_MONTHS)
        effective_end = effective_start

    out_path = manifest_path(root, "manifest_inventory.csv")
    lineage_path = manifest_path(root, "manifest_inventory.lineage.json")
    request = inventory_request(
        "combined_daily_inventory",
        effective_start,
        effective_end,
        selected,
        inventory_workers,
    )
    if not refresh_inventory and inventory_cache_matches(out_path, lineage_path, request):
        with (root / "logs" / "inventory.log").open("a", encoding="utf-8") as log:
            log.write(f"{utc_now()} inventory_cache_hit fingerprint={request['fingerprint']}\n")
        return out_path

    jobs: list[tuple[str, Callable[[], list[dict]]]] = []
    for day in days:
        if include_goes:
            for platform in sorted(selected.intersection(GOES_CONFIG)):
                for full_product in GOES_CONFIG[platform]["short_products"]:
                    jobs.append(
                        (
                            f"{day:%Y-%m-%d} {platform} {full_product}",
                            lambda day=day, platform=platform, full_product=full_product: inventory_goes_day(
                                root, day, platform, full_product
                            ),
                        )
                    )
        if include_himawari:
            jobs.append(
                (
                    f"{day:%Y-%m-%d} {HIMAWARI_CONFIG['platform']}",
                    lambda day=day: inventory_himawari_day(root, day),
                )
            )
        if include_selected_meteosat:
            for service in sorted(selected.intersection(METEOSAT_CONFIG)):
                for collection_id in METEOSAT_CONFIG[service]:
                    jobs.append(
                        (
                            f"{day:%Y-%m-%d} {service} {collection_id}",
                            lambda day=day, service=service, collection_id=collection_id: inventory_meteosat_day(
                                root, day, service, collection_id
                            ),
                        )
                    )

    rows: list[dict] = []
    log_path = root / "logs" / "inventory.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(
            f"{utc_now()} inventory_start include_meteosat={include_selected_meteosat} "
            f"platforms={','.join(sorted(selected))} daily_jobs={len(jobs)} "
            f"inventory_workers={inventory_workers} network_mode=direct_only\n"
        )
        completed = 0
        with ThreadPoolExecutor(max_workers=inventory_workers) as executor:
            futures = {executor.submit(job): label for label, job in jobs}
            for future in as_completed(futures):
                label = futures[future]
                completed += 1
                try:
                    rows.extend(future.result())
                    status = "ok"
                except Exception as exc:
                    status = f"error={type(exc).__name__}:{exc}"
                log.write(f"{utc_now()} inventory_job={completed}/{len(jobs)} {status} {label}\n")
                log.flush()
    rows.sort(key=lambda row: (row["target_time_utc"], row["platform"], row["product"]))
    write_csv(out_path, rows)
    write_inventory_summary(root, rows)
    write_lineage_manifest(
        lineage_path,
        canonical_stage_id="",
        component_role=COMPONENT_ROLE,
        related_stage_ids=RELATED_STAGE_IDS,
        generating_script=Path(__file__).resolve(),
        input_paths=[],
        output_paths=[out_path, manifest_path(root, "inventory_summary.json")],
        parameters=request,
        project_root=PROJECT_ROOT,
        extra={
            "inventory_fingerprint": request["fingerprint"],
            "row_count": len(rows),
            "daily_job_count": len(jobs),
            "cache_policy": "exact_semantic_fingerprint_and_row_count",
        },
    )
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


def download_s3_row(
    s3_client,
    row: dict,
    range_mib: int = DEFAULT_S3_RANGE_MIB,
) -> tuple[bool, str]:
    """Download one S3 object with resumable bounded Range requests."""
    target = Path(row["local_path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    if target.exists():
        ok, note = validate_file(target, row)
        if ok:
            return True, f"skipped_existing:{note}"
    if range_mib < 1 or range_mib > 64:
        raise ValueError(f"range_mib must be between 1 and 64, got {range_mib}")
    expected_size_text = str(row.get("size_bytes", ""))
    if expected_size_text.isdigit() and int(expected_size_text) > 0:
        expected_size = int(expected_size_text)
    else:
        metadata = s3_client.head_object(
            Bucket=row["bucket"], Key=row["remote_key_or_product_id"]
        )
        expected_size = int(metadata["ContentLength"])
    range_bytes = range_mib * 1024 * 1024
    if tmp.exists() and tmp.stat().st_size > expected_size:
        platform_root = next(
            (ancestor for ancestor in target.parents if ancestor.name in PLATFORM_CHOICES),
            None,
        )
        quarantine_dir = (
            platform_root.parent / "quarantine" if platform_root is not None else target.parent
        )
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        quarantine = quarantine_dir / f"{tmp.name}.oversize.{int(time.time())}"
        replace_with_retry(tmp, quarantine)

    resumed_from = tmp.stat().st_size if tmp.exists() else 0
    offset = resumed_from
    while offset < expected_size:
        range_end = min(offset + range_bytes - 1, expected_size - 1)
        last_error = ""
        segment_complete = False
        for delay_index, delay in enumerate([0] + RETRY_DELAYS_SECONDS):
            if delay:
                time.sleep(delay)
            body = None
            try:
                response = s3_client.get_object(
                    Bucket=row["bucket"],
                    Key=row["remote_key_or_product_id"],
                    Range=f"bytes={offset}-{range_end}",
                )
                body = response["Body"]
                segment = body.read()
                required = range_end - offset + 1
                if len(segment) != required:
                    raise RuntimeError(
                        f"short_range expected={required} actual={len(segment)} "
                        f"range={offset}-{range_end}"
                    )
                with tmp.open("ab") as handle:
                    handle.write(segment)
                    handle.flush()
                offset += len(segment)
                segment_complete = True
                break
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if delay_index == len(RETRY_DELAYS_SECONDS):
                    return False, (
                        f"range_failed offset={offset} expected_size={expected_size} "
                        f"preserved_part={tmp.exists()} error={last_error}"
                    )
            finally:
                if body is not None:
                    try:
                        body.close()
                    except Exception:
                        pass
        if not segment_complete:
            return False, f"unreachable_range_retry_state offset={offset} error={last_error}"

    try:
        if tmp.stat().st_size != expected_size:
            return False, f"size_mismatch expected={expected_size} actual={tmp.stat().st_size}"
        ok, note = validate_file(tmp, row)
        if not ok:
            return False, note
        replace_with_retry(tmp, target)
        return True, f"{note};range_mib={range_mib};resumed_from={resumed_from}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc};preserved_part={tmp.exists()}"


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
    platforms: Optional[set[str]] = None,
    range_mib: int = DEFAULT_S3_RANGE_MIB,
    adaptive_workers: bool = False,
    min_workers: int = DEFAULT_ADAPTIVE_MIN_WORKERS,
    initial_workers: int = DEFAULT_ADAPTIVE_INITIAL_WORKERS,
) -> Path:
    disable_proxy_environment()
    max_workers = validate_worker_count(max_workers, MAX_S3_WORKERS, "max_workers")
    if range_mib < 1 or range_mib > 64:
        raise ValueError(f"range_mib must be between 1 and 64, got {range_mib}")
    inventory = manifest_path(root, "manifest_inventory.csv")
    if not inventory.exists():
        raise FileNotFoundError(f"Inventory not found: {inventory}")

    start_prefix = f"{start_date}T"
    end_dt = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc) + timedelta(days=1)
    rows = []
    for row in read_manifest(inventory):
        if row["status"] != "found" or row["remote_type"] != "s3":
            continue
        if platforms is not None and row.get("platform") not in platforms:
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
            f"max_workers={max_workers} adaptive_workers={adaptive_workers} "
            f"initial_workers={initial_workers} range_mib={range_mib} network_mode=direct_only\n"
        )
        log.flush()

        def worker(row: dict) -> dict:
            s3_client = get_s3_client()
            success, note = download_s3_row(s3_client, row, range_mib=range_mib)
            out = dict(row)
            out["status"] = "downloaded" if success else "corrupt"
            out["note"] = note
            return out

        downloaded.extend(
            run_download_pool(
                root,
                pending,
                worker,
                "s3",
                max_workers,
                adaptive_workers,
                min_workers,
                initial_workers,
                log,
            )
        )

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
    inventory_parser.add_argument(
        "--inventory-workers",
        type=int,
        default=DEFAULT_INVENTORY_WORKERS,
        help=f"Concurrent daily inventory requests (1-{MAX_INVENTORY_WORKERS}).",
    )
    inventory_parser.add_argument(
        "--refresh-inventory",
        action="store_true",
        help="Ignore a matching inventory cache and query providers again.",
    )
    inventory_parser.add_argument(
        "--platform",
        action="append",
        choices=PLATFORM_CHOICES,
        help="Limit inventory to one or more platforms; repeat this option.",
    )

    test_parser = sub.add_parser("download-test-day", help="Download and validate the test day.")
    test_parser.add_argument("--date", default=TEST_DAY, help="UTC test day, YYYY-MM-DD.")

    s3_parser = sub.add_parser("download-s3-range", help="Download S3 rows from inventory for a date range.")
    s3_parser.add_argument("--start-date", required=True, help="UTC start date, YYYY-MM-DD.")
    s3_parser.add_argument("--end-date", required=True, help="UTC end date, YYYY-MM-DD.")
    s3_parser.add_argument("--max-workers", type=int, default=8, help="Concurrent S3 downloads.")
    s3_parser.add_argument(
        "--adaptive-workers",
        action="store_true",
        help="Start conservatively and tune concurrent downloads from measured results.",
    )
    s3_parser.add_argument("--min-workers", type=int, default=DEFAULT_ADAPTIVE_MIN_WORKERS)
    s3_parser.add_argument("--initial-workers", type=int, default=DEFAULT_ADAPTIVE_INITIAL_WORKERS)
    s3_parser.add_argument(
        "--range-mib",
        type=int,
        default=DEFAULT_S3_RANGE_MIB,
        help="Resumable S3 Range request size in MiB (1-64).",
    )
    s3_parser.add_argument(
        "--platform",
        action="append",
        choices=PLATFORM_CHOICES,
        help="Limit downloads to one or more platforms; repeat this option.",
    )

    sub.add_parser("validate", help="Validate downloaded files and write reports.")

    sub.add_parser("meteosat-options", help="Write EUMETSAT collection titles and search options.")

    met_smoke = sub.add_parser("meteosat-smoke", help="Search Meteosat collections for one UTC time.")
    met_smoke.add_argument("--date", default=TEST_DAY, help="UTC date, YYYY-MM-DD.")
    met_smoke.add_argument("--hour", type=int, default=0, help="UTC hour.")
    met_smoke.add_argument("--minute", type=int, default=0, help="UTC minute.")

    met_inventory = sub.add_parser("meteosat-inventory", help="Build Meteosat-only inventory for a date range.")
    met_inventory.add_argument("--start-date", required=True, help="UTC start date, YYYY-MM-DD.")
    met_inventory.add_argument("--end-date", required=True, help="UTC end date, YYYY-MM-DD.")
    met_inventory.add_argument(
        "--platform",
        action="append",
        choices=tuple(METEOSAT_CONFIG),
        help="Limit inventory to one or more Meteosat services; repeat this option.",
    )

    met_download = sub.add_parser("download-meteosat-range", help="Download Meteosat rows from Meteosat inventory.")
    met_download.add_argument("--start-date", required=True, help="UTC start date, YYYY-MM-DD.")
    met_download.add_argument("--end-date", required=True, help="UTC end date, YYYY-MM-DD.")
    met_download.add_argument(
        "--max-workers",
        type=int,
        default=2,
        help=f"Concurrent EUMETSAT downloads (1-{MAX_METEOSAT_WORKERS}).",
    )
    met_download.add_argument(
        "--adaptive-workers",
        action="store_true",
        help="Start conservatively and tune concurrent downloads from measured results.",
    )
    met_download.add_argument("--min-workers", type=int, default=DEFAULT_ADAPTIVE_MIN_WORKERS)
    met_download.add_argument("--initial-workers", type=int, default=DEFAULT_ADAPTIVE_INITIAL_WORKERS)
    met_download.add_argument(
        "--platform",
        action="append",
        choices=tuple(METEOSAT_CONFIG),
        help="Limit downloads to one or more Meteosat services; repeat this option.",
    )

    first = sub.add_parser("first-round", help="Run inventory then test-day download.")
    first.add_argument("--date", default=TEST_DAY, help="UTC test day, YYYY-MM-DD.")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).resolve()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_manifest = (
        root
        / "logs"
        / f"geo_ring_cloud_downloader_{args.command}_{run_id}_manifest.json"
    )

    def record_run(status: str, output_paths: Iterable[str | Path], error: str = "") -> None:
        if not root.exists():
            return
        try:
            write_lineage_manifest(
                run_manifest,
                canonical_stage_id="",
                component_role=COMPONENT_ROLE,
                related_stage_ids=RELATED_STAGE_IDS,
                generating_script=Path(__file__).resolve(),
                input_paths=(root / "manifests" / "manifest_inventory.csv",),
                output_paths=output_paths,
                parameters={
                    key: value
                    for key, value in vars(args).items()
                    if key not in {"password", "token", "secret"}
                },
                project_root=PROJECT_ROOT,
                run_id=run_id,
                extra={"final_status": status, "error": error},
            )
        except Exception as manifest_exc:
            print(
                f"WARNING: failed to write run lineage manifest: "
                f"{type(manifest_exc).__name__}: {manifest_exc}",
                file=sys.stderr,
            )

    try:
        out: str | Path = root
        if args.command == "inventory":
            out = run_inventory(
                root,
                include_meteosat=not args.skip_meteosat,
                start_date=args.start_date,
                end_date=args.end_date,
                platforms=set(args.platform) if args.platform else None,
                inventory_workers=args.inventory_workers,
                refresh_inventory=args.refresh_inventory,
            )
            print(out)
        elif args.command == "download-test-day":
            out = run_download_test_day(root, args.date)
            print(out)
        elif args.command == "download-s3-range":
            out = run_download_s3_range(
                root,
                args.start_date,
                args.end_date,
                args.max_workers,
                set(args.platform) if args.platform else None,
                args.range_mib,
                args.adaptive_workers,
                args.min_workers,
                args.initial_workers,
            )
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
            out = run_meteosat_inventory_range(
                root,
                args.start_date,
                args.end_date,
                set(args.platform) if args.platform else None,
                args.max_workers,
            )
            print(out)
        elif args.command == "download-meteosat-range":
            out = run_download_meteosat_range(
                root,
                args.start_date,
                args.end_date,
                set(args.platform) if args.platform else None,
                args.max_workers,
                args.adaptive_workers,
                args.min_workers,
                args.initial_workers,
            )
            print(out)
        elif args.command == "first-round":
            run_inventory(root, include_meteosat=True)
            out = run_download_test_day(root, args.date)
            print(out)
        record_run("PASS", (root, out))
        return 0
    except Exception as exc:
        record_run(
            "FAIL",
            (root,),
            error=f"{type(exc).__name__}: {exc}",
        )
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
