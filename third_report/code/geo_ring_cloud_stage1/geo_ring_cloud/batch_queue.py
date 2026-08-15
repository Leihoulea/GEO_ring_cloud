"""Persistent, space-aware queue primitives for GEO download batches.

The queue stores control-plane metadata only.  It never creates a batch data
directory and never deletes local data.  The dashboard remains responsible for
checking active processes and launching the existing download orchestrator.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, MutableMapping


COMPONENT_ROLE = "batch_queue"
SCHEMA_VERSION = 2
ACTIVE_QUEUE_STATUSES = {
    "QUEUED",
    "WAITING_ACTIVE_DOWNLOAD",
    "WAITING_SPACE",
    "STARTING",
    "RUNNING",
}
CANCELLABLE_QUEUE_STATUSES = {
    "QUEUED",
    "WAITING_ACTIVE_DOWNLOAD",
    "WAITING_SPACE",
}

# Empirical raw-data planning values for the currently selected cloud-product
# mix.  They are deliberately keyed by platform rather than shared across a
# provider: changing the selected products requires a new calibration version.
#
# Evidence available on 2026-08-16:
# - Himawari-9: two completed May 2024 batches, 25.71--25.86 GiB/day.
# - GOES-16/18: June 1--10 inventory, about 0.53/0.48 GiB/day.
# - Meteosat: completed June 2024 transfer manifest, about 0.022 GiB/day each.
#
# The EUMETSAT catalogue ``size_bytes`` values describe catalogue records (for
# example 565 bytes) rather than the delivered ZIP payloads (about 0.5 MiB), so
# they must not be treated as authoritative byte estimates.
DEFAULT_RAW_GIB_PER_PLATFORM_DAY = {
    "GOES-16": 0.60,
    "GOES-18": 0.55,
    "Himawari-9": 26.0,
    "Meteosat-0deg": 0.025,
    "Meteosat-IODC": 0.025,
}
DEFAULT_PLATFORM_SAFETY_FACTOR = {
    "GOES-16": 1.30,
    "GOES-18": 1.30,
    "Himawari-9": 1.20,
    "Meteosat-0deg": 1.30,
    "Meteosat-IODC": 1.30,
}
DEFAULT_SAFETY_FACTOR = 1.20
DEFAULT_FIXED_OVERHEAD_GIB = 2.0
ESTIMATE_BASIS = "adaptive_platform_product_v2"
ESTIMATE_CALIBRATED_AT = "2026-08-16"


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".{}.{}.{}.tmp".format(path.name, os.getpid(), time.time_ns()))
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def empty_queue_state() -> Dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": "geo_ring_cloud",
        "canonical_stage_id": "",
        "component_role": COMPONENT_ROLE,
        "related_stage_ids": ["stage_00"],
        "updated_at": utc_now_text(),
        "automatic_delete": False,
        "items": [],
    }


def read_queue_state(path: Path) -> Dict[str, object]:
    if not path.is_file():
        return empty_queue_state()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_queue_state()
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return empty_queue_state()
    result = empty_queue_state()
    result.update(payload)
    result["automatic_delete"] = False
    return result


def normalize_request(
    request: Mapping[str, object], allowed_platforms: Iterable[str]
) -> Dict[str, object]:
    start_text = str(request.get("start_date", "")).strip()
    end_text = str(request.get("end_date", "")).strip()
    try:
        start_date = date.fromisoformat(start_text)
        end_date = date.fromisoformat(end_text)
    except ValueError as exc:
        raise ValueError("开始和结束日期必须使用 YYYY-MM-DD。") from exc
    if end_date < start_date:
        raise ValueError("结束日期不能早于开始日期。")

    allowed = set(allowed_platforms)
    requested = request.get("platforms", [])
    if not isinstance(requested, list):
        raise ValueError("platforms 必须是数组。")
    platforms: List[str] = []
    for value in requested:
        platform = str(value).strip()
        if platform not in allowed:
            raise ValueError("不支持的卫星平台：{}".format(platform))
        if platform not in platforms:
            platforms.append(platform)
    if not platforms:
        raise ValueError("请至少选择一个卫星平台。")

    try:
        inventory_workers = int(request.get("inventory_workers", 8))
        download_workers = int(request.get("download_workers", 12))
    except (TypeError, ValueError) as exc:
        raise ValueError("并行数必须是整数。") from exc
    if not 1 <= inventory_workers <= 16 or not 1 <= download_workers <= 16:
        raise ValueError("清单与下载并行数必须在 1–16 之间。")

    drive = str(request.get("download_drive", "") or "").strip().upper().rstrip("\\/")
    return {
        "start_date": start_text,
        "end_date": end_text,
        "platforms": platforms,
        "download_drive": drive,
        "inventory_workers": inventory_workers,
        "download_workers": download_workers,
        "adaptive_download": bool(request.get("adaptive_download", True)),
        "refresh_inventory": bool(request.get("refresh_inventory", False)),
        "continuous_upload": bool(request.get("continuous_upload", True)),
    }


def batch_name_for_request(request: Mapping[str, object]) -> str:
    short_names = {
        "GOES-16": "g16",
        "GOES-18": "g18",
        "Himawari-9": "h9",
        "Meteosat-0deg": "m0",
        "Meteosat-IODC": "miodc",
    }
    start_date = date.fromisoformat(str(request["start_date"]))
    end_date = date.fromisoformat(str(request["end_date"]))
    platforms = list(request["platforms"])
    return "{}_{}_{}".format(
        start_date.strftime("%Y%m%d"),
        end_date.strftime("%Y%m%d"),
        "-".join(short_names[name] for name in platforms),
    )


def semantic_key(request: Mapping[str, object]) -> str:
    payload = {
        "start_date": request["start_date"],
        "end_date": request["end_date"],
        "platforms": sorted(request["platforms"]),
        "download_drive": request.get("download_drive", ""),
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def estimate_required_space(
    request: Mapping[str, object],
    raw_gib_per_platform_day: Mapping[str, float] | None = None,
    safety_factor: float | None = None,
    platform_safety_factor: Mapping[str, float] | None = None,
    fixed_overhead_gib: float = DEFAULT_FIXED_OVERHEAD_GIB,
) -> Dict[str, object]:
    rates = dict(DEFAULT_RAW_GIB_PER_PLATFORM_DAY)
    if raw_gib_per_platform_day:
        rates.update({key: float(value) for key, value in raw_gib_per_platform_day.items()})
    factors = dict(DEFAULT_PLATFORM_SAFETY_FACTOR)
    if platform_safety_factor:
        factors.update({key: float(value) for key, value in platform_safety_factor.items()})
    if safety_factor is not None:
        factors = {key: max(1.0, float(safety_factor)) for key in rates}
    start_date = date.fromisoformat(str(request["start_date"]))
    end_date = date.fromisoformat(str(request["end_date"]))
    days = (end_date - start_date).days + 1
    platform_rates = {name: rates[name] for name in request["platforms"]}
    platform_factors = {
        name: max(1.0, factors.get(name, DEFAULT_SAFETY_FACTOR))
        for name in request["platforms"]
    }
    platform_estimates = {
        name: {
            "raw_gib_per_day": round(platform_rates[name], 6),
            "safety_factor": round(platform_factors[name], 3),
            "raw_gib": round(days * platform_rates[name], 3),
            "protected_gib": round(
                days * platform_rates[name] * platform_factors[name], 3
            ),
        }
        for name in request["platforms"]
    }
    raw_gib = days * sum(platform_rates.values())
    protected_data_gib = sum(
        days * platform_rates[name] * platform_factors[name]
        for name in request["platforms"]
    )
    overhead = max(0.0, float(fixed_overhead_gib))
    required_gib = protected_data_gib + overhead
    return {
        "days": days,
        "raw_gib": round(raw_gib, 3),
        "protected_data_gib": round(protected_data_gib, 3),
        "fixed_overhead_gib": round(overhead, 3),
        "required_gib": round(required_gib, 3),
        "required_bytes": int(required_gib * (1024 ** 3)),
        "safety_factor": round(max(platform_factors.values()), 3),
        "platform_raw_gib_per_day": platform_rates,
        "platform_safety_factor": platform_factors,
        "platform_estimates": platform_estimates,
        "basis": ESTIMATE_BASIS,
        "calibrated_at": ESTIMATE_CALIBRATED_AT,
        "product_profile": "geo_cloud_priority_products_2024_v1",
        "confidence": "medium",
        "catalogue_size_policy": {
            "s3": "authoritative_content_length_when_inventory_exists",
            "eumetsat": "enumeration_only_use_empirical_payload_rate",
        },
    }


def refine_estimate_from_inventory(
    estimate: Mapping[str, object], inventory_path: Path
) -> Dict[str, object]:
    """Replace empirical S3 estimates with authoritative pending bytes.

    S3 inventory sizes are object ``ContentLength`` values.  EUMETSAT catalogue
    sizes are intentionally ignored because they describe catalogue records,
    not the delivered ZIP payloads.
    """
    result = dict(estimate)
    platform_estimates = {
        str(name): dict(value)
        for name, value in dict(estimate.get("platform_estimates", {})).items()
        if isinstance(value, Mapping)
    }
    if not inventory_path.is_file() or not platform_estimates:
        return result

    authoritative: Dict[str, Dict[str, int]] = {}
    try:
        with inventory_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                platform = str(row.get("platform", ""))
                if platform not in platform_estimates or row.get("remote_type") != "s3":
                    continue
                if row.get("status") != "found":
                    continue
                try:
                    expected = int(str(row.get("size_bytes", "")))
                except ValueError:
                    continue
                if expected <= 0:
                    continue
                summary = authoritative.setdefault(
                    platform, {"file_count": 0, "total_bytes": 0, "pending_bytes": 0}
                )
                summary["file_count"] += 1
                summary["total_bytes"] += expected
                local_path = Path(str(row.get("local_path", "")))
                try:
                    complete = local_path.is_file() and local_path.stat().st_size == expected
                except OSError:
                    complete = False
                if not complete:
                    summary["pending_bytes"] += expected
    except (OSError, csv.Error):
        return result

    if not authoritative:
        return result
    required_gib = float(result.get("required_gib", 0) or 0)
    raw_gib = float(result.get("raw_gib", 0) or 0)
    for platform, summary in authoritative.items():
        detail = platform_estimates[platform]
        old_raw = float(detail.get("raw_gib", 0) or 0)
        old_protected = float(detail.get("protected_gib", 0) or 0)
        pending_gib = summary["pending_bytes"] / (1024**3)
        total_gib = summary["total_bytes"] / (1024**3)
        factor = float(detail.get("safety_factor", DEFAULT_SAFETY_FACTOR) or 1)
        protected_gib = pending_gib * max(1.0, factor)
        raw_gib += pending_gib - old_raw
        required_gib += protected_gib - old_protected
        detail.update(
            raw_gib=round(pending_gib, 3),
            protected_gib=round(protected_gib, 3),
            inventory_total_gib=round(total_gib, 3),
            inventory_file_count=summary["file_count"],
            estimate_source="trusted_s3_inventory_pending_bytes",
        )

    result.update(
        raw_gib=round(max(0.0, raw_gib), 3),
        protected_data_gib=round(
            max(0.0, required_gib - float(result.get("fixed_overhead_gib", 0) or 0)),
            3,
        ),
        required_gib=round(max(0.0, required_gib), 3),
        required_bytes=int(max(0.0, required_gib) * (1024**3)),
        platform_estimates=platform_estimates,
        basis="trusted_inventory_pending_bytes_v2",
        inventory_path=str(inventory_path),
        inventory_authoritative_platforms=sorted(authoritative),
        confidence="high_for_listed_s3_objects",
    )
    return result


def make_queue_item(
    request: Mapping[str, object], estimate: Mapping[str, object]
) -> Dict[str, object]:
    now = utc_now_text()
    key = semantic_key(request)
    return {
        "queue_id": "geo-{}".format(key[:16]),
        "semantic_key": key,
        "created_at": now,
        "updated_at": now,
        "status": "QUEUED",
        "status_message": "已加入队列，等待调度检查。",
        "request": dict(request),
        "target_batch_name": batch_name_for_request(request),
        "estimate": dict(estimate),
        "gate": {},
        "launch": {},
        "automatic_delete": False,
    }


def public_queue_state(state: Mapping[str, object]) -> Dict[str, object]:
    items = [dict(item) for item in state.get("items", []) if isinstance(item, dict)]
    counts: MutableMapping[str, int] = {}
    for item in items:
        status = str(item.get("status", "UNKNOWN"))
        counts[status] = counts.get(status, 0) + 1
    return {
        "schema_version": state.get("schema_version", SCHEMA_VERSION),
        "updated_at": state.get("updated_at", ""),
        "automatic_delete": False,
        "items": items,
        "counts": dict(counts),
        "active_count": sum(1 for item in items if item.get("status") in ACTIVE_QUEUE_STATUSES),
    }
