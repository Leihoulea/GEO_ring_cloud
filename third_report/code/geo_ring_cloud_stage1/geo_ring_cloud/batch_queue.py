"""Persistent, space-aware queue primitives for GEO download batches.

The queue stores control-plane metadata only.  It never creates a batch data
directory and never deletes local data.  The dashboard remains responsible for
checking active processes and launching the existing download orchestrator.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, MutableMapping


COMPONENT_ROLE = "batch_queue"
SCHEMA_VERSION = 1
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

# Conservative raw-data planning values.  The 1.20 safety factor below is
# applied separately.  Himawari is calibrated from the April 2024 inventory;
# GOES values intentionally err high until the self-learning phase is added.
DEFAULT_RAW_GIB_PER_PLATFORM_DAY = {
    "GOES-16": 22.0,
    "GOES-18": 22.0,
    "Himawari-9": 25.2,
    "Meteosat-0deg": 0.25,
    "Meteosat-IODC": 0.25,
}
DEFAULT_SAFETY_FACTOR = 1.20


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
    safety_factor: float = DEFAULT_SAFETY_FACTOR,
) -> Dict[str, object]:
    rates = dict(DEFAULT_RAW_GIB_PER_PLATFORM_DAY)
    if raw_gib_per_platform_day:
        rates.update({key: float(value) for key, value in raw_gib_per_platform_day.items()})
    start_date = date.fromisoformat(str(request["start_date"]))
    end_date = date.fromisoformat(str(request["end_date"]))
    days = (end_date - start_date).days + 1
    platform_rates = {name: rates[name] for name in request["platforms"]}
    raw_gib = days * sum(platform_rates.values())
    required_gib = raw_gib * max(1.0, float(safety_factor))
    return {
        "days": days,
        "raw_gib": round(raw_gib, 3),
        "required_gib": round(required_gib, 3),
        "required_bytes": int(required_gib * (1024 ** 3)),
        "safety_factor": round(max(1.0, float(safety_factor)), 3),
        "platform_raw_gib_per_day": platform_rates,
        "basis": "conservative_platform_day_v1",
    }


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
