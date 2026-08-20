"""Local Chinese dashboard for GEO download, Xftp, and server verification.

The dashboard is intentionally local-only.  Its two POST actions create audit
markers; neither action uploads, moves, truncates, or deletes raw data.
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import os
import re
import secrets
import shutil
import string
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

CORE_CODE_ROOT = Path(__file__).resolve().parents[1] / "geo_ring_cloud_stage1"
if str(CORE_CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_CODE_ROOT))

from geo_ring_cloud.notifications import (  # noqa: E402
    PersistentEmailNotifier,
    save_secure_email_config,
)
from geo_ring_cloud.batch_queue import (  # noqa: E402
    ACTIVE_QUEUE_STATUSES,
    CANCELLABLE_QUEUE_STATUSES,
    estimate_required_space,
    make_queue_item,
    normalize_request,
    public_queue_state,
    read_queue_state,
    refine_estimate_from_inventory,
    semantic_key,
    write_json_atomic as write_queue_json_atomic,
)

from monitor_dashboard import (
    MET_DOWNLOAD_RE,
    S3_EVENT_RE,
    format_bytes,
    parse_download_log,
    read_json,
    read_manifest_sizes,
    tail_lines,
)


COMPONENT_ROLE = "data_transfer_dashboard"
RELATED_STAGE_IDS = ["stage_00"]
PLATFORM_NAMES = (
    "GOES-16",
    "GOES-18",
    "Himawari-9",
    "Meteosat-0deg",
    "Meteosat-IODC",
    "FY4B",
    "CMSAF",
)

APP_ROOT = Path(__file__).resolve().parent
HTML_PATH = APP_ROOT / "geo_ring_cloud_transfer_dashboard.html"
GUIDE_PATH = APP_ROOT / "geo_ring_cloud_data_transfer_operation_guide_cn.md"
AUTO_UPLOADER_PATH = APP_ROOT / "geo_ring_cloud_auto_uploader.py"
NOTIFICATION_SERVICE_PATH = APP_ROOT / "geo_ring_cloud_notification_service.py"
BATCH_SCRIPT_PATH = APP_ROOT / "geo_ring_cloud_transfer_batch.ps1"
AUTO_UPLOAD_STALE_SECONDS = 15 * 60
DOWNLOAD_PLATFORM_NAMES = (
    "GOES-16",
    "GOES-18",
    "Himawari-9",
    "Meteosat-0deg",
    "Meteosat-IODC",
)
FY4B_EXTERNAL_SOURCE = "FY4B 官方应用本地导入"
FY4B_OFFICIAL_MAPPING_PROFILE = "fy4b_agri_l2_product_day_hour_v1"
# Examples from the FY4B official application use a nominal acquisition start
# time after ``NOM_``.  Do not derive archive paths from a user-supplied folder
# name: product, day, and hour must be present in the official filename.
FY4B_OFFICIAL_FILENAME_RE = re.compile(
    r"^FY4B-_AGRI--_N_DISK_1050E_L2-_"
    r"(?P<product>[A-Z0-9]+)-_.+?_NOM_(?P<nominal_time>\d{14})_\d{14}_.+\.NC$",
    re.IGNORECASE,
)
ORDER_SOURCE_CONFIG = {
    "CLAAS3-0deg": {
        "display_name": "CLAAS-3（CM SAF）",
        "products": ["CMA", "CTX", "CPP"],
        "coverage_role": "Meteosat 0° 云物理增强层",
        "delivery_mode": "CM SAF 登录下单后，通过 HTTPS 或 SFTP 交付",
        "official_order_url": (
            "https://wui.cmsaf.eu/safira/action/"
            "viewDoiDetails?acronym=CLAAS_V003"
        ),
        "status": "requires_cmsaf_account_order",
        "direct_download": False,
    }
}
# A dashboard process can serve several batches at once (and users often keep
# an old completed batch open in a second tab).  Keep sampling state per batch:
# replacing one batch's empty ``.part`` list must not reset the live batch's
# next speed calculation.  A one-minute rolling window also avoids reporting
# zero whenever the downloader is between its 4 MiB range-file writes.
_PART_SNAPSHOT: Dict[str, List[Tuple[float, Dict[str, int]]]] = {}
_PART_SNAPSHOT_LOCK = threading.Lock()
PART_RATE_WINDOW_SECONDS = 60
_UPLOAD_SNAPSHOT: Dict[str, List[Tuple[float, int]]] = {}
_UPLOAD_SNAPSHOT_LOCK = threading.Lock()
UPLOAD_RATE_WINDOW_SECONDS = 300
TREND_SAMPLE_INTERVAL_SECONDS = 300
TREND_WARMUP_INTERVAL_SECONDS = 30
TREND_RETURN_SAMPLE_LIMIT = 480
_TREND_WRITE_LOCK = threading.Lock()
_TREND_LAST_WRITE: Dict[str, float] = {}


def format_gib(value: object) -> str:
    try:
        return "{:.3f} GiB".format(float(value))
    except (TypeError, ValueError):
        return "--"


def parse_disk_gate(message: object) -> Dict[str, object]:
    text = str(message or "").strip()
    marker = "Not enough free space:"
    if marker not in text:
        return {"exists": False, "message": text}
    try:
        payload = ast.literal_eval(text.split(marker, 1)[1].strip())
    except (SyntaxError, ValueError):
        return {"exists": True, "message": text, "parse_error": True}
    if not isinstance(payload, dict):
        return {"exists": True, "message": text, "parse_error": True}
    free_gib = float(payload.get("free_gib", 0) or 0)
    needed_gib = float(payload.get("needed_gib_with_margin", 0) or 0)
    shortfall_gib = max(0.0, needed_gib - free_gib)
    return {
        "exists": True,
        "passes": shortfall_gib <= 0,
        "drive": str(payload.get("drive", "")),
        "free_bytes": int(payload.get("free_bytes", 0) or 0),
        "needed_bytes_with_margin": int(payload.get("needed_bytes_with_margin", 0) or 0),
        "free_gib": round(free_gib, 3),
        "needed_gib": round(needed_gib, 3),
        "shortfall_gib": round(shortfall_gib, 3),
        "free_label": format_gib(free_gib),
        "needed_label": format_gib(needed_gib),
        "shortfall_label": format_gib(shortfall_gib),
        "total_rows": int(payload.get("total_rows", 0) or 0),
        "pending_rows": int(payload.get("pending_rows", 0) or 0),
        "skipped_existing_rows": int(payload.get("skipped_existing_rows", 0) or 0),
        "message": text,
    }


def available_download_drives() -> List[Dict[str, object]]:
    roots: List[Path] = []
    if os.name == "nt":
        roots = [Path("{}:\\".format(letter)) for letter in string.ascii_uppercase]
    else:
        roots = [Path("/")]
    rows: List[Dict[str, object]] = []
    for root in roots:
        if not root.exists():
            continue
        try:
            usage = shutil.disk_usage(str(root))
        except OSError:
            continue
        drive = root.drive.upper() if root.drive else str(root)
        rows.append(
            {
                "value": drive,
                "root": str(root),
                "batch_parent": str(root / "GEO_Cloud_2024_batches"),
                "free_bytes": usage.free,
                "free_gib": round(usage.free / (1024 ** 3), 3),
                "free_label": format_bytes(usage.free),
                "total_label": format_bytes(usage.total),
            }
        )
    return rows


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def iso_mtime(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
    except (OSError, SystemError):
        return ""


def latest_file(directory: Path, pattern: str) -> Optional[Path]:
    try:
        files = [path for path in directory.glob(pattern) if path.is_file()]
        return max(files, key=lambda path: path.stat().st_mtime) if files else None
    except OSError:
        return None


def read_inventory(path: Path, remote_type: str = "") -> Dict[str, object]:
    result: Dict[str, object] = {
        "path": str(path),
        "exists": path.is_file(),
        "rows": 0,
        "found": 0,
        "missing": 0,
        "error": 0,
        "estimated_size_bytes": 0,
        "by_platform": {},
        "by_product": {},
    }
    if not path.is_file():
        return result
    by_platform: Counter = Counter()
    by_product: Counter = Counter()
    try:
        with path.open("r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if remote_type and row.get("remote_type", "") != remote_type:
                    continue
                result["rows"] = int(result["rows"]) + 1
                status = row.get("status", "")
                if status in {"found", "missing", "error"}:
                    result[status] = int(result[status]) + 1
                if status == "found":
                    platform = row.get("platform", "")
                    product = row.get("product", "")
                    by_platform[platform] += 1
                    by_product["{} {}".format(platform, product)] += 1
                    size = row.get("size_bytes", "")
                    if str(size).isdigit():
                        result["estimated_size_bytes"] = int(result["estimated_size_bytes"]) + int(size)
    except Exception as exc:
        result["read_error"] = "{}: {}".format(type(exc).__name__, exc)
    result["estimated_size_label"] = format_bytes(int(result["estimated_size_bytes"]))
    result["by_platform"] = dict(by_platform)
    result["by_product"] = dict(by_product)
    result["updated_at"] = iso_mtime(path)
    return result


def read_downloaded_manifest(path: Path, remote_type: str = "") -> Dict[str, object]:
    """Count durable completed rows instead of only the latest log session."""
    result: Dict[str, object] = {
        "path": str(path),
        "exists": path.is_file(),
        "completed": 0,
        "failed": 0,
        "by_platform": {},
    }
    if not path.is_file():
        return result
    by_platform: Counter = Counter()
    try:
        with path.open("r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if remote_type and row.get("remote_type", "") != remote_type:
                    continue
                if row.get("status", "") == "downloaded":
                    result["completed"] = int(result["completed"]) + 1
                    by_platform[row.get("platform", "")] += 1
                else:
                    result["failed"] = int(result["failed"]) + 1
    except Exception as exc:
        result["read_error"] = "{}: {}".format(type(exc).__name__, exc)
    result["by_platform"] = dict(by_platform)
    result["updated_at"] = iso_mtime(path)
    return result


def parse_start(path: Path, token: str) -> Dict[str, int]:
    for line in reversed(tail_lines(path, 10000)):
        if token not in line:
            continue
        values = {}
        for key in ("rows", "skipped_existing", "pending"):
            marker = "{}=".format(key)
            for part in line.split():
                if part.startswith(marker):
                    value = part[len(marker) :]
                    if value.isdigit():
                        values[key] = int(value)
        if "rows" in values:
            return {
                "rows": values.get("rows", 0),
                "skipped_existing": values.get("skipped_existing", 0),
                "pending": values.get("pending", values.get("rows", 0)),
            }
    return {"rows": 0, "skipped_existing": 0, "pending": 0}


def summarize_download(
    log_path: Path,
    manifest_path: Path,
    pattern,
    start_token: str,
) -> Dict[str, object]:
    sizes = read_manifest_sizes(manifest_path)
    events = parse_download_log(log_path, pattern, after_last_start=True)
    start = parse_start(log_path, start_token)
    completed_events = [event for event in events if event["status"] == "downloaded"]
    failed_events = [event for event in events if event["status"] == "corrupt"]
    completed = max([event["num"] for event in events], default=0)
    pending = start.get("pending", 0) or max([event["total"] for event in events], default=0)
    skipped = start.get("skipped_existing", 0)
    total = start.get("rows", 0) or pending
    overall_completed = skipped + completed
    completed_bytes = sum(
        sizes.get((event["platform"], event["product"], event["target"]), 0)
        for event in completed_events
    )
    now = datetime.now(timezone.utc)
    recent_hour = [
        event for event in completed_events if (now - event["time"]).total_seconds() <= 3600
    ]
    remaining = max(pending - completed, 0)
    eta_hours = remaining / len(recent_hour) if recent_hour else None
    by_platform = Counter(event["platform"] for event in completed_events)
    by_product = Counter(
        "{} {}".format(event["platform"], event["product"]) for event in completed_events
    )
    last_event = events[-1] if events else None
    return {
        "log": str(log_path),
        "exists": log_path.is_file(),
        "total": total,
        "pending": pending,
        "skipped_existing": skipped,
        "completed": completed,
        "overall_completed": overall_completed,
        "percent": round(overall_completed / total * 100, 2) if total else 0,
        "failed": len(failed_events),
        "completed_bytes": completed_bytes,
        "completed_bytes_label": format_bytes(completed_bytes),
        "recent_hour_files": len(recent_hour),
        "eta_hours": round(eta_hours, 2) if eta_hours is not None else None,
        "by_platform": dict(by_platform),
        "by_product": dict(by_product),
        "last_event": None
        if last_event is None
        else {
            key: value
            for key, value in last_event.items()
            if key != "time"
        },
        "recent": [
            {key: value for key, value in event.items() if key != "time"}
            for event in events[-10:]
        ],
        "updated_at": iso_mtime(log_path),
    }


def active_parts(batch_root: Path) -> Dict[str, object]:
    global _PART_SNAPSHOT
    now = time.time()
    batch_key = str(batch_root.resolve())
    current: Dict[str, int] = {}
    rows = []
    try:
        paths = sorted(
            batch_root.rglob("*.part"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:30]
    except OSError:
        paths = []
    total_rate = 0.0
    measured = False
    with _PART_SNAPSHOT_LOCK:
        history = list(_PART_SNAPSHOT.get(batch_key, []))
        history = [
            sample
            for sample in history
            if sample[0] >= now - (PART_RATE_WINDOW_SECONDS * 2)
        ]
        window_history = [
            sample for sample in history if sample[0] >= now - PART_RATE_WINDOW_SECONDS
        ]
        baseline = window_history[0] if window_history else (history[-1] if history else None)
        previous_batch = baseline[1] if baseline else {}
        elapsed = max(now - baseline[0], 0.001) if baseline else 0.0
        for path in paths:
            try:
                relative = path.relative_to(batch_root)
            except ValueError:
                continue
            # Status/manifest writes also use atomic temporary files.  They
            # are not raw-data downloads and would otherwise add misleading
            # rows such as ``auto_upload_status.json.part`` to this panel.
            if relative.parts and relative.parts[0] in {"transfer", "logs", "manifests"}:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            key = str(path)
            current[key] = stat.st_size
            previous = previous_batch.get(key)
            rate = None
            if history:
                rate = max(stat.st_size - int(previous or 0), 0) / elapsed
                total_rate += rate
                measured = True
            rows.append(
                {
                    "name": path.name,
                    "path": key,
                    "size_bytes": stat.st_size,
                    "size_label": format_bytes(stat.st_size),
                    "rate_bps": rate,
                    "rate_label": "{}/s".format(format_bytes(rate)) if rate is not None else "测量中",
                    "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                }
            )
        history.append((now, current))
        _PART_SNAPSHOT[batch_key] = history
    return {
        "count": len(rows),
        "displayed_count": len(rows),
        "display_limit": 30,
        "total_rate_bps": total_rate if measured else None,
        "total_rate_label": "{}/s".format(format_bytes(total_rate)) if measured else "测量中",
        "items": rows,
    }


def _read_recent_jsonl(path: Path, limit: int) -> List[Dict[str, object]]:
    """Read a bounded tail without loading a long-running batch history."""
    if not path.is_file() or limit < 1:
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 262144), os.SEEK_SET)
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows: List[Dict[str, object]] = []
    for line in lines[-limit:]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def dashboard_trends(
    transfer_dir: Path,
    sample: Dict[str, object],
    should_record: bool,
) -> Dict[str, object]:
    """Persist a compact five-minute dashboard history outside raw data."""
    path = transfer_dir / "dashboard_trends.jsonl"
    now = time.time()
    key = str(path.resolve())
    if should_record and transfer_dir.is_dir():
        with _TREND_WRITE_LOCK:
            last = _TREND_LAST_WRITE.get(key)
            existing_samples = _read_recent_jsonl(path, 2)
            has_speed_sample = any(
                sample.get("download_rate_bps") is not None
                or sample.get("upload_rate_bps") is not None
                for sample in existing_samples
            )
            required_interval = (
                TREND_WARMUP_INTERVAL_SECONDS
                if len(existing_samples) < 2 or not has_speed_sample
                else TREND_SAMPLE_INTERVAL_SECONDS
            )
            if last is None or now - last >= required_interval:
                payload = {
                    "ts": utc_now_text(),
                    "download_rate_bps": sample.get("download_rate_bps"),
                    "upload_rate_bps": sample.get("upload_rate_bps"),
                    "disk_free_bytes": sample.get("disk_free_bytes"),
                    "download_percent": sample.get("download_percent"),
                    "upload_percent": sample.get("upload_percent"),
                }
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
                _TREND_LAST_WRITE[key] = now
    samples = _read_recent_jsonl(path, TREND_RETURN_SAMPLE_LIMIT)
    has_speed_sample = any(
        sample.get("download_rate_bps") is not None
        or sample.get("upload_rate_bps") is not None
        for sample in samples
    )
    return {
        "path": str(path),
        "sample_interval_seconds": TREND_SAMPLE_INTERVAL_SECONDS,
        "warmup_interval_seconds": TREND_WARMUP_INTERVAL_SECONDS,
        "warmup_pending": len(samples) < 2 or not has_speed_sample,
        "return_limit": TREND_RETURN_SAMPLE_LIMIT,
        "samples": samples,
    }


def upload_throughput(batch_root: Path, upload_status: Dict[str, object]) -> Dict[str, object]:
    """Return a rolling throughput from bytes confirmed by the uploader.

    SFTP's command-line client does not expose a portable live byte counter.
    The uploader does persist ``completed_size_bytes`` after every verified
    file, which gives an audit-safe upload throughput without probing or
    changing the server.  The five-minute window makes the value useful for
    large files whose completion events are naturally sparse.
    """
    global _UPLOAD_SNAPSHOT
    now = time.time()
    batch_key = str(batch_root.resolve())
    try:
        completed_bytes = max(0, int(upload_status.get("completed_size_bytes", 0) or 0))
    except (TypeError, ValueError):
        completed_bytes = 0
    with _UPLOAD_SNAPSHOT_LOCK:
        history = [
            sample
            for sample in _UPLOAD_SNAPSHOT.get(batch_key, [])
            if sample[0] >= now - (UPLOAD_RATE_WINDOW_SECONDS * 2)
        ]
        window_history = [
            sample for sample in history if sample[0] >= now - UPLOAD_RATE_WINDOW_SECONDS
        ]
        baseline = window_history[0] if window_history else (history[-1] if history else None)
        if baseline is None:
            rate = None
            elapsed = 0.0
        else:
            elapsed = max(now - baseline[0], 0.001)
            rate = max(completed_bytes - baseline[1], 0) / elapsed
        history.append((now, completed_bytes))
        _UPLOAD_SNAPSHOT[batch_key] = history
    return {
        "rate_bps": rate,
        "rate_label": "{}/s".format(format_bytes(rate)) if rate is not None else "测量中",
        "rate_window_seconds": round(elapsed, 1),
        "rate_basis": "confirmed_completed_bytes",
    }


def disk_status(batch_root: Path) -> Dict[str, object]:
    try:
        usage = shutil.disk_usage(str(batch_root.anchor or batch_root))
    except OSError:
        return {}
    return {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "total_label": format_bytes(usage.total),
        "used_label": format_bytes(usage.used),
        "free_label": format_bytes(usage.free),
        "used_percent": round(usage.used / usage.total * 100, 2),
    }


def enrich_disk_gate(gate: Dict[str, object], batch_root: Path) -> Dict[str, object]:
    if not gate.get("exists"):
        return gate
    current = disk_status(batch_root)
    if not current:
        return gate
    current_free_gib = int(current.get("free_bytes", 0)) / (1024 ** 3)
    needed_gib = float(gate.get("needed_gib", 0) or 0)
    current_shortfall_gib = max(0.0, needed_gib - current_free_gib)
    gate.update(
        {
            "current_free_gib": round(current_free_gib, 3),
            "current_shortfall_gib": round(current_shortfall_gib, 3),
            "current_free_label": format_gib(current_free_gib),
            "current_shortfall_label": format_gib(current_shortfall_gib),
            "currently_passes": current_shortfall_gib <= 0,
        }
    )
    return gate


def transfer_manifest_status(transfer_dir: Path) -> Dict[str, object]:
    path = latest_file(transfer_dir, "geo_ring_cloud_transfer_*_manifest.json")
    if path is None:
        return {"exists": False, "status": "PENDING", "path": ""}
    payload = read_json(path)
    return {
        "exists": True,
        "path": str(path),
        "filename": path.name,
        "status": payload.get("status", "UNKNOWN"),
        "file_count": payload.get("file_count", 0),
        "total_size_bytes": payload.get("total_size_bytes", 0),
        "total_size_label": format_bytes(payload.get("total_size_bytes", 0)),
        "platform_summary": payload.get("platform_summary", {}),
        "created_at": payload.get("created_at", iso_mtime(path)),
    }


def server_verification_status(transfer_dir: Path) -> Dict[str, object]:
    exact = transfer_dir / "server_verification.json"
    path = exact if exact.is_file() else latest_file(transfer_dir, "server_verification*.json")
    if path is None:
        return {"exists": False, "status": "PENDING", "path": ""}
    payload = read_json(path)
    failures = [row for row in payload.get("results", []) if row.get("status") != "PASS"]
    return {
        "exists": True,
        "path": str(path),
        "status": payload.get("status", "UNKNOWN"),
        "verified_file_count": payload.get("verified_file_count", 0),
        "failed_file_count": payload.get("failed_file_count", len(failures)),
        "verified_at": payload.get("verified_at", iso_mtime(path)),
        "failure_examples": failures[:5],
    }


def marker_status(path: Path) -> Dict[str, object]:
    payload = read_json(path) if path.is_file() else {}
    return {
        "exists": path.is_file(),
        "path": str(path),
        "created_at": payload.get("created_at", iso_mtime(path) if path.is_file() else ""),
        "payload": payload,
    }


def auto_upload_status(transfer_dir: Path) -> Dict[str, object]:
    path = transfer_dir / "auto_upload_status.json"
    if not path.is_file():
        return {
            "exists": False,
            "path": str(path),
            "status": "PENDING",
            "phase": "waiting",
            "completed_files": 0,
            "file_count": 0,
            "percent": 0,
        }
    payload = read_json(path)
    payload["exists"] = True
    payload["path"] = str(path)
    payload.setdefault("status", "UNKNOWN")
    payload.setdefault("phase", "unknown")
    payload.setdefault("percent", 0)
    status = str(payload.get("status", "UNKNOWN")).upper()
    terminal_statuses = {"PASS", "FAIL", "FAILED", "STOPPED", "EXITED", "CANCELLED", "CANCELED"}
    process_alive = False if status in terminal_statuses else process_is_running(payload.get("pid"))
    payload["process_alive"] = process_alive
    if status not in {"STARTING", "RUNNING"}:
        return payload
    if not process_alive:
        previous_phase = str(payload.get("phase", "unknown"))
        payload["reported_status"] = status
        payload["status"] = "STOPPED"
        payload["phase"] = "stopped"
        payload["error"] = (
            "自动上传进程已停止；最后记录阶段为 {}。可点击“接管当前下载并自动上传”安全续传。"
        ).format(previous_phase)
        return payload
    updated_at = str(payload.get("updated_at", ""))
    try:
        updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        age_seconds = max(0, int((datetime.now(timezone.utc) - updated).total_seconds()))
    except ValueError:
        age_seconds = None
    payload["status_age_seconds"] = age_seconds
    if age_seconds is not None and age_seconds > AUTO_UPLOAD_STALE_SECONDS:
        payload["reported_status"] = status
        payload["status"] = "STALLED"
        payload["phase"] = "status_stale"
        payload["error"] = (
            "自动上传进程仍存在，但已 {} 分钟没有写入状态；请检查网络或上传日志。"
        ).format(max(1, age_seconds // 60))
    return payload


def process_is_running(pid: object) -> bool:
    try:
        numeric_pid = int(pid)
        if numeric_pid <= 0:
            return False
        os.kill(numeric_pid, 0)
        return True
    except (OSError, SystemError):
        # Windows may return access denied *or* ERROR_INVALID_PARAMETER for
        # os.kill(pid, 0) even when the PID is healthy.  psutil's PID table
        # query remains read-only and avoids falsely marking that task failed.
        if os.name == "nt":
            try:
                import psutil  # type: ignore

                return psutil.pid_exists(numeric_pid)
            except (ImportError, OSError, ValueError):
                return False
        return False
    except (TypeError, ValueError):
        return False


def inspect_batch_lock(lock_path: Path) -> Dict[str, object]:
    """Read-only probe for a batch lock left behind after an abnormal exit.

    The PowerShell orchestrator holds this file with FileShare.None.  If this
    process can open it, no orchestrator currently owns that lock and the
    control file can be recovered without touching downloaded data.
    """
    result: Dict[str, object] = {
        "path": str(lock_path),
        "exists": lock_path.exists(),
        "held": False,
        "stale": False,
        "size_bytes": 0,
    }
    if not lock_path.exists():
        return result
    try:
        result["size_bytes"] = lock_path.stat().st_size
        with lock_path.open("rb") as handle:
            preview = handle.read(4096).decode("utf-8", errors="replace").strip()
    except OSError as exc:
        result["held"] = True
        result["error"] = "{}: {}".format(type(exc).__name__, exc)
        return result
    result["stale"] = True
    if preview:
        try:
            metadata = json.loads(preview)
        except json.JSONDecodeError:
            metadata = {"raw_preview": preview[:500]}
        result["metadata"] = metadata
    return result


def has_recent_download_part(transfer_dir: Path, max_age_seconds: int = 300) -> bool:
    """Detect an orphaned launcher whose child downloader is still writing raw data."""
    cutoff = time.time() - max_age_seconds
    try:
        for path in transfer_dir.parent.rglob("*.part"):
            try:
                relative = path.relative_to(transfer_dir.parent)
                if relative.parts and relative.parts[0] in {"transfer", "logs", "manifests"}:
                    continue
                if path.stat().st_mtime >= cutoff:
                    return True
            except (OSError, ValueError):
                continue
    except OSError:
        return False
    return False


def download_launcher_status(
    transfer_dir: Path, raw_batch_status: Dict[str, object]
) -> Dict[str, object]:
    path = transfer_dir / "download_launcher_status.json"
    payload = read_json(path)
    if not payload:
        return {
            "exists": False,
            "path": str(path),
            "status": "UNKNOWN",
            "process_alive": False,
            "message": "尚无启动器状态记录",
        }
    payload["exists"] = True
    payload["path"] = str(path)
    payload.setdefault("status", "UNKNOWN")
    payload.setdefault("message", "")
    terminal_statuses = {
        "FAIL",
        "FAILED",
        "COMPLETE",
        "COMPLETED",
        "PASS",
        "EXITED",
        "CANCELLED",
        "CANCELED",
    }
    has_terminal_record = (
        str(payload.get("status", "")).upper() in terminal_statuses
        or bool(payload.get("finished_at"))
        or payload.get("exit_code") is not None
    )
    # A recorded terminal state is authoritative.  Probing only the numeric PID
    # after completion can mistake a recycled Windows PID for the old launcher.
    payload["process_alive"] = (
        False
        if has_terminal_record
        else process_is_running(payload.get("pid"))
    )
    if (
        payload.get("status") in {"STARTING", "RUNNING"}
        and not payload["process_alive"]
        and raw_batch_status.get("status") != "complete"
    ):
        previous_message = str(payload.get("message", "")).strip()
        if raw_batch_status.get("status") == "running" and has_recent_download_part(
            transfer_dir
        ):
            payload["status"] = "RUNNING"
            payload["detached_child_activity"] = True
            payload["message"] = (
                "启动器父进程已退出，但检测到下载临时文件仍在更新；按活动子下载显示运行中。"
            )
        else:
            payload["status"] = "FAIL"
            payload["message"] = (
                "下载启动进程已经退出，但批次没有生成完成状态。"
                + (" 上次记录：{}".format(previous_message) if previous_message else "")
            )
    return payload


def merge_platform_counts(*mappings: Dict[str, int]) -> Dict[str, int]:
    combined: Counter = Counter()
    for mapping in mappings:
        combined.update(mapping or {})
    return dict(combined)


def stage(status: str, key: str, title: str, detail: str) -> Dict[str, str]:
    return {"key": key, "title": title, "status": status, "detail": detail}


def build_pipeline_stages(
    inventory_rows: int,
    download: Dict[str, object],
    parts: Dict[str, object],
    validation: Dict[str, object],
    transfer: Dict[str, object],
    auto_upload: Dict[str, object],
    xftp: Dict[str, object],
    server: Dict[str, object],
    cleanup: Dict[str, object],
) -> List[Dict[str, str]]:
    inventory_state = "pass" if inventory_rows else "running"
    download_total = int(download.get("total", 0))
    download_done = int(download.get("overall_completed", 0))
    if download_total and download_done >= download_total and not parts.get("count"):
        download_state = "pass" if not download.get("failed") else "warning"
    elif download_total:
        download_state = "running"
    else:
        download_state = "pending"
    validation_state = "pending"
    if validation:
        validation_state = "pass" if int(validation.get("corrupt_rows", 0)) == 0 else "fail"
    transfer_state = "pass" if transfer.get("status") == "READY_FOR_XFTP_UPLOAD" else "pending"
    if xftp.get("exists"):
        xftp_state = "pass"
        upload_detail = (
            "自动 SFTP 已完成"
            if xftp.get("payload", {}).get("status") == "AUTOMATED_SFTP_COMPLETE"
            else "Xftp 已人工确认"
        )
    elif auto_upload.get("status") in {"RUNNING", "STARTING"}:
        xftp_state = "running"
        upload_detail = "自动上传 {}%".format(auto_upload.get("percent", 0))
    elif auto_upload.get("status") in {"FAIL", "STOPPED", "STALLED"}:
        xftp_state = "fail"
        upload_detail = str(auto_upload.get("error", "自动上传已停止"))
    else:
        xftp_state = "pending"
        upload_detail = "等待自动 SFTP 或人工 Xftp"
    server_state = "pass" if server.get("status") == "PASS" else (
        "fail" if server.get("status") == "FAIL" else "pending"
    )
    cleanup_state = (
        "pass"
        if cleanup.get("exists")
        else "pending"
        if server.get("status") == "PASS" and xftp.get("exists")
        else "locked"
    )
    return [
        stage(inventory_state, "inventory", "本地清单", "已发现 {} 条目标记录".format(inventory_rows)),
        stage(
            download_state,
            "download",
            "本地下载",
            "{} / {} 个文件".format(download_done, download_total),
        ),
        stage(
            validation_state,
            "validation",
            "本地校验",
            "等待完整批次" if not validation else "损坏记录 {}".format(validation.get("corrupt_rows", 0)),
        ),
        stage(transfer_state, "manifest", "传输清单", transfer.get("status", "等待生成")),
        stage(xftp_state, "upload", "上传到服务器", upload_detail),
        stage(server_state, "server", "服务器复核", server.get("status", "等待报告")),
        stage(
            cleanup_state,
            "cleanup",
            "本地清理许可",
            "已确认；仍未执行删除"
            if cleanup.get("exists")
            else "等待用户明确确认"
            if cleanup_state == "pending"
            else "锁定",
        ),
    ]


def write_json_atomic(
    path: Path,
    payload: Dict[str, object],
    max_attempts: int = 12,
    retry_seconds: float = 0.25,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, ensure_ascii=False)
    last_error: Optional[OSError] = None
    for attempt in range(max(1, max_attempts)):
        temporary = path.with_name(
            ".{}.{}.{}.tmp".format(path.name, os.getpid(), time.time_ns())
        )
        try:
            temporary.write_text(serialized, encoding="utf-8")
            os.replace(temporary, path)
            return
        except OSError as exc:
            last_error = exc
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            if attempt + 1 < max(1, max_attempts):
                time.sleep(max(0.0, retry_seconds))
    assert last_error is not None
    raise last_error


class DashboardState:
    def __init__(
        self,
        batch_root: Path,
        ssh_target: str = "",
        identity_file: Optional[Path] = None,
        auto_upload_root: str = "/data04/1/dhr/geo_ring_cloud_auto_upload",
        allowed_server_parent: str = "/data04/1/dhr",
        conda_environment: str = "pytorch",
        notification_setup_sender: str = "",
        notification_setup_recipient: str = "",
    ):
        self.batch_parent = batch_root.resolve().parent
        self.batch_root = batch_root.resolve()
        self.manifest_dir = self.batch_root / "manifests"
        self.log_dir = self.batch_root / "logs"
        self.transfer_dir = self.batch_root / "transfer"
        self.ssh_target = ssh_target
        self.identity_file = identity_file.resolve() if identity_file else None
        self.auto_upload_root = auto_upload_root
        self.allowed_server_parent = allowed_server_parent
        self.conda_environment = conda_environment
        self._upload_lock = threading.Lock()
        self._download_lock = threading.Lock()
        self._queue_lock = threading.RLock()
        self._queue_dispatch_lock = threading.Lock()
        self._queue_stop = threading.Event()
        self._queue_thread: Optional[threading.Thread] = None
        self._queue_last_check_at = ""
        self._queue_last_error = ""
        self.queue_state_path = (
            self.batch_parent / "_geo_ring_cloud_control" / "batch_queue.json"
        )
        self.notification_root = self.batch_parent / "_geo_ring_cloud_control" / "notifications"
        self.notification_state_path = self.notification_root / "notification_state.json"
        self.email_notifier = PersistentEmailNotifier(self.notification_state_path)
        self.notification_setup_token = secrets.token_urlsafe(32)
        self.notification_setup_defaults = {
            "sender": str(notification_setup_sender or os.environ.get("GEO_RING_NOTIFY_SETUP_SENDER", "")).strip(),
            "recipient": str(notification_setup_recipient or os.environ.get("GEO_RING_NOTIFY_SETUP_RECIPIENT", "")).strip(),
        }
        self._notification_process: Optional[subprocess.Popen] = None

    def _known_batch_parents(self) -> List[Path]:
        parents = {self.batch_parent.resolve()}
        if self.batch_parent.name == "GEO_Cloud_2024_batches":
            for drive in available_download_drives():
                candidate = Path(str(drive["batch_parent"]))
                if candidate.is_dir():
                    parents.add(candidate.resolve())
        return sorted(parents, key=lambda value: str(value).lower())

    def _resolve_existing_batch(self, batch_name: str = "") -> Path:
        name = str(batch_name or "").strip()
        if not name:
            return self.batch_root
        if Path(name).name != name or name in {".", ".."}:
            raise RuntimeError("批次名称无效。")
        matches = []
        for parent in self._known_batch_parents():
            candidate = (parent / name).resolve()
            if candidate.parent == parent and candidate.is_dir():
                matches.append(candidate)
        if not matches:
            raise RuntimeError("找不到批次：{}".format(name))
        if len(matches) > 1:
            raise RuntimeError("多个磁盘存在同名批次，请先修改批次名称。")
        return matches[0]

    def _resolve_download_parent(self, request: Dict[str, object]) -> Path:
        requested = str(request.get("download_drive", "") or "").strip().upper()
        requested = requested.rstrip("\\/")
        if not requested:
            return self.batch_parent
        drives = {str(row["value"]).upper(): row for row in available_download_drives()}
        if requested not in drives:
            raise RuntimeError("下载磁盘不可用：{}".format(requested))
        return Path(str(drives[requested]["batch_parent"])).resolve()

    @staticmethod
    def _fy4b_source_files(source_root: Path) -> List[Dict[str, object]]:
        """Validate and map official FY4B L2 files without modifying them.

        The source folders created by the official application can be flat.
        Its filename is the authoritative archive key for this importer:
        ``FY4B/<product>/<YYYYMMDD>/<HH>/<original filename>``.
        """
        ignored_names = {"thumbs.db", "desktop.ini"}
        records: List[Dict[str, object]] = []
        rejected: List[str] = []
        mapped_paths: Dict[str, Path] = {}
        for candidate in source_root.rglob("*"):
            try:
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                relative = candidate.relative_to(source_root)
                if (
                    "transfer" in relative.parts
                    or candidate.name.lower() in ignored_names
                    or candidate.suffix.lower() == ".part"
                ):
                    continue
                if candidate.suffix.lower() != ".nc":
                    continue
                match = FY4B_OFFICIAL_FILENAME_RE.fullmatch(candidate.name)
                if not match:
                    rejected.append(relative.as_posix())
                    continue
                nominal_time = match.group("nominal_time")
                try:
                    datetime.strptime(nominal_time, "%Y%m%d%H%M%S")
                except ValueError:
                    rejected.append(relative.as_posix())
                    continue
                product = match.group("product").upper()
                remote_relative_path = PurePosixPath(
                    "FY4B",
                    product,
                    nominal_time[:8],
                    nominal_time[8:10],
                    candidate.name,
                )
                remote_key = remote_relative_path.as_posix()
                prior = mapped_paths.get(remote_key)
                if prior is not None and prior != candidate.resolve():
                    raise RuntimeError(
                        "FY4B 来源中有两个文件会映射到同一服务器路径：{}".format(
                            remote_key
                        )
                    )
                stat = candidate.stat()
            except (OSError, ValueError):
                continue
            mapped_paths[remote_key] = candidate.resolve()
            records.append(
                {
                    "local_path": candidate.resolve(),
                    "source_relative_path": relative.as_posix(),
                    "product": product,
                    "nominal_time": nominal_time,
                    "remote_relative_path": remote_key,
                    "size_bytes": int(stat.st_size),
                }
            )
        if rejected:
            examples = "；".join(rejected[:5])
            suffix = "（仅显示前 5 个）" if len(rejected) > 5 else ""
            raise RuntimeError(
                "发现 {} 个 .NC 文件不符合 FY4B AGRI L2 官方命名规则，无法安全判断变量、日期和小时：{}{}".format(
                    len(rejected), examples, suffix
                )
            )
        return sorted(records, key=lambda row: str(row["remote_relative_path"]).lower())

    def _fy4b_batch_label(self, source_root: Path, records: List[Dict[str, object]]) -> str:
        """Derive a stable, resume-safe FY4B control label from official timestamps."""
        first_day = min(str(row["nominal_time"])[:8] for row in records)
        last_day = max(str(row["nominal_time"])[:8] for row in records)
        base_label = "{}_{}".format(first_day, last_day)
        base_root = (self.batch_parent / "fy4b_{}".format(base_label)).resolve()
        if not base_root.exists():
            return base_label
        existing = read_json(base_root / "transfer" / "fy4b_official_import_request.json")
        if str(existing.get("source_root", "")) == str(source_root):
            return base_label
        # A different official-client folder may legitimately cover the same
        # dates.  Add a deterministic source fingerprint instead of asking the
        # user to invent a label; the raw server archive path is unaffected.
        source_tag = hashlib.sha256(str(source_root).casefold().encode("utf-8")).hexdigest()[:8]
        return "{}_{}".format(base_label, source_tag)

    def preview_fy4b_official_upload(self, request: Dict[str, object]) -> Dict[str, object]:
        """Return a read-only mapping preview for official FY4B files."""
        source_text = str(request.get("source_path", "")).strip()
        if not source_text:
            raise RuntimeError("请填写 FY4B 官方应用的本地下载目录。")
        source_root = Path(source_text).expanduser().resolve()
        if not source_root.is_dir() or source_root == Path(source_root.anchor):
            raise RuntimeError("FY4B 来源必须是存在的非根目录。")
        if any(source_root == parent for parent in self._known_batch_parents()):
            raise RuntimeError("FY4B 来源不能是整个 GEO_Cloud_2024_batches 父目录。")
        records = self._fy4b_source_files(source_root)
        if not records:
            raise RuntimeError("FY4B 来源目录中未发现符合命名规则的已完成 .NC 文件。")
        products = []
        for product in sorted({str(row["product"]) for row in records}):
            product_rows = [row for row in records if row["product"] == product]
            products.append(
                {
                    "product": product,
                    "file_count": len(product_rows),
                    "size_bytes": sum(int(row["size_bytes"]) for row in product_rows),
                    "first_nominal_time": min(str(row["nominal_time"]) for row in product_rows),
                    "last_nominal_time": max(str(row["nominal_time"]) for row in product_rows),
                }
            )
        return {
            "source_root": str(source_root),
            "suggested_batch_label": self._fy4b_batch_label(source_root, records),
            "mapping_profile": FY4B_OFFICIAL_MAPPING_PROFILE,
            "remote_root": str(PurePosixPath(self.auto_upload_root) / "FY4B"),
            "file_count": len(records),
            "total_size_bytes": sum(int(row["size_bytes"]) for row in records),
            "products": products,
            "sample_mappings": [
                {
                    "source_relative_path": str(row["source_relative_path"]),
                    "remote_path": str(PurePosixPath(self.auto_upload_root) / str(row["remote_relative_path"])),
                }
                for row in records[:10]
            ],
        }

    def start_fy4b_official_upload(self, request: Dict[str, object]) -> Dict[str, object]:
        """Create a control-only FY4B import batch, then reuse the safe uploader."""
        self._require_upload_configuration()
        source_text = str(request.get("source_path", "")).strip()
        if not source_text:
            raise RuntimeError("请填写 FY4B 官方应用的本地下载目录。")
        source_root = Path(source_text).expanduser().resolve()
        if not source_root.is_dir() or source_root == Path(source_root.anchor):
            raise RuntimeError("FY4B 来源必须是存在的非根目录。")
        if any(source_root == parent for parent in self._known_batch_parents()):
            raise RuntimeError("FY4B 来源不能是整个 GEO_Cloud_2024_batches 父目录。")

        records = self._fy4b_source_files(source_root)
        if not records:
            raise RuntimeError("FY4B 来源目录中未发现可上传的已完成文件。")
        label = self._fy4b_batch_label(source_root, records)

        batch_name = "fy4b_{}".format(label)
        batch_root = (self.batch_parent / batch_name).resolve()
        if batch_root.parent != self.batch_parent:
            raise RuntimeError("FY4B 控制批次目录无效。")
        transfer_dir = batch_root / "transfer"
        request_path = transfer_dir / "fy4b_official_import_request.json"
        manifest_path = transfer_dir / "geo_ring_cloud_transfer_{}_manifest.json".format(
            batch_name
        )
        if batch_root.exists():
            existing = read_json(request_path)
            if str(existing.get("source_root", "")) != str(source_root):
                raise RuntimeError("同名 FY4B 批次已对应另一来源目录；请使用新的批次标识。")
            if manifest_path.is_file():
                result = self.start_auto_upload(batch_name)
                result.update({"source_root": str(source_root), "resumed": True})
                return result
            raise RuntimeError("同名 FY4B 批次的控制记录不完整；请使用新的批次标识。")

        batch_root.mkdir(parents=True, exist_ok=False)
        transfer_dir.mkdir(parents=True, exist_ok=True)
        files = [
            {
                "platform": "FY4B",
                "product": str(record["product"]),
                "local_path": str(record["local_path"]),
                "source_relative_path": str(record["source_relative_path"]),
                "nominal_time": str(record["nominal_time"]),
                "remote_path": str(
                    PurePosixPath(self.auto_upload_root)
                    / PurePosixPath(str(record["remote_relative_path"]))
                ),
                "size_bytes": int(record["size_bytes"]),
            }
            for record in records
        ]
        now = utc_now_text()
        common = {
            "project_id": "geo_ring_cloud",
            "canonical_stage_id": "",
            "related_stage_ids": RELATED_STAGE_IDS,
            "batch_id": batch_name,
            "platforms": ["FY4B"],
            "source_mode": FY4B_EXTERNAL_SOURCE,
            "mapping_profile": FY4B_OFFICIAL_MAPPING_PROFILE,
            "source_root": str(source_root),
            "automatic_delete": False,
        }
        write_json_atomic(
            request_path,
            {
                **common,
                "component_role": "external_source_upload_request",
                "created_at": now,
                "source_file_count": len(files),
                "source_size_bytes": sum(item["size_bytes"] for item in files),
                "mapping_profile": FY4B_OFFICIAL_MAPPING_PROFILE,
                "mapped_products": sorted({str(item["product"]) for item in files}),
                "note": "仅记录来源；不会复制、移动或删除 FY4B 原始文件。",
            },
        )
        write_json_atomic(
            transfer_dir / "batch_status.json",
            {
                **common,
                "component_role": "external_source_upload_orchestrator",
                "status": "complete",
                "phase": "ready_for_xftp",
                "message": "FY4B 官方应用本地文件已登记，等待自动上传。",
                "updated_at": now,
            },
        )
        write_json_atomic(
            transfer_dir / "download_launcher_status.json",
            {
                **common,
                "component_role": "external_source_upload_orchestrator",
                "status": "COMPLETE",
                "process_alive": False,
                "message": "FY4B 数据由官方应用下载；本系统不执行远端下载。",
                "updated_at": now,
            },
        )
        write_json_atomic(
            manifest_path,
            {
                **common,
                "component_role": "external_source_transfer_manifest",
                "created_at": now,
                "status": "READY_FOR_XFTP_UPLOAD",
                "server_root": self.auto_upload_root,
                "files": files,
                "file_count": len(files),
                "total_size_bytes": sum(item["size_bytes"] for item in files),
                "deletion_policy": {
                    "automatic_delete": False,
                    "delete_allowed_only_after": "server SHA-256 verification PASS and explicit user confirmation",
                },
            },
        )
        self._set_batch_root(batch_root)
        result = self.start_auto_upload(batch_name)
        result.update(
            {
                "source_root": str(source_root),
                "source_file_count": len(files),
                "source_size_bytes": sum(item["size_bytes"] for item in files),
                "mapping_profile": FY4B_OFFICIAL_MAPPING_PROFILE,
                "mapped_products": sorted({str(item["product"]) for item in files}),
                "resumed": False,
            }
        )
        return result

    @staticmethod
    def _task_summary(batch_root: Path) -> Dict[str, object]:
        transfer_dir = batch_root / "transfer"
        raw = read_json(transfer_dir / "batch_status.json")
        launcher = download_launcher_status(transfer_dir, raw)
        upload = auto_upload_status(transfer_dir)
        transfer = transfer_manifest_status(transfer_dir)
        server = server_verification_status(transfer_dir)
        xftp = marker_status(transfer_dir / "xftp_upload_complete.json")
        error = str(launcher.get("message") or raw.get("message") or upload.get("error") or "")
        disk_gate = enrich_disk_gate(parse_disk_gate(error), batch_root)
        download_status = str(launcher.get("status", "UNKNOWN"))
        if raw.get("status") == "complete":
            download_status = "COMPLETE"
        elif raw.get("status") == "failed":
            download_status = "FAIL"
        upload_status = str(upload.get("status", "PENDING"))
        if xftp.get("payload", {}).get("status") == "AUTOMATED_SFTP_COMPLETE":
            upload_status = "PASS"
        updated_candidates = [
            str(raw.get("updated_at", "")),
            str(launcher.get("updated_at", "")),
            str(upload.get("updated_at", "")),
            str(server.get("verified_at", "")),
        ]
        return {
            "batch_name": batch_root.name,
            "batch_root": str(batch_root),
            "start_date": raw.get("start_date", launcher.get("start_date", "")),
            "end_date": raw.get("end_date", launcher.get("end_date", "")),
            "platforms": raw.get("platforms", launcher.get("platforms", [])),
            "download_status": download_status,
            "download_phase": raw.get("phase", "waiting"),
            "download_process_alive": bool(launcher.get("process_alive")),
            "upload_status": upload_status,
            "upload_phase": upload.get("phase", "waiting"),
            "upload_completed_files": int(upload.get("completed_files", 0) or 0),
            "upload_file_count": int(upload.get("file_count", transfer.get("file_count", 0)) or 0),
            "upload_percent": float(upload.get("percent", 0) or 0),
            "server_status": str(server.get("status", "PENDING")),
            "transfer_status": str(transfer.get("status", "PENDING")),
            "error": error,
            "disk_gate": disk_gate,
            "updated_at": max(updated_candidates),
            "automatic_delete": False,
        }

    def task_summaries(self, limit: Optional[int] = 30) -> List[Dict[str, object]]:
        tasks = []
        for parent in self._known_batch_parents():
            try:
                directories = [path for path in parent.iterdir() if path.is_dir()]
            except OSError:
                continue
            for batch_root in directories:
                transfer_dir = batch_root / "transfer"
                if not transfer_dir.is_dir():
                    continue
                tasks.append(self._task_summary(batch_root))
        tasks.sort(key=lambda row: str(row.get("updated_at", "")), reverse=True)
        return tasks[:limit] if limit else tasks

    def _active_download_task(self, exclude_batch_name: str = "") -> Optional[Dict[str, object]]:
        for task in self.task_summaries(limit=None):
            if exclude_batch_name and task.get("batch_name") == exclude_batch_name:
                continue
            if str(task.get("download_status", "")) in {"STARTING", "RUNNING"}:
                return task
        return None

    def _save_queue_state(self, state: Dict[str, object]) -> None:
        state["updated_at"] = utc_now_text()
        state["automatic_delete"] = False
        write_queue_json_atomic(self.queue_state_path, state)

    def _queue_target_parent(self, request: Dict[str, object]) -> Path:
        parent = self._resolve_download_parent(request)
        drive_root = Path(parent.anchor) if parent.anchor else parent
        if not drive_root.exists():
            raise RuntimeError("队列目标磁盘不可用：{}".format(request.get("download_drive", "")))
        return parent

    def _queue_estimate(self, request: Dict[str, object]) -> Dict[str, object]:
        estimate = estimate_required_space(request)
        parent = self._queue_target_parent(request)
        candidate = parent / str(make_queue_item(request, estimate)["target_batch_name"])
        if candidate.is_dir():
            estimate = refine_estimate_from_inventory(
                estimate, candidate / "manifests" / "manifest_inventory.csv"
            )
            transfer_dir = candidate / "transfer"
            raw = read_json(transfer_dir / "batch_status.json")
            launcher = read_json(transfer_dir / "download_launcher_status.json")
            gate = parse_disk_gate(launcher.get("message") or raw.get("message"))
            if (
                estimate.get("basis") != "trusted_inventory_pending_bytes_v2"
                and gate.get("exists")
                and int(gate.get("needed_bytes_with_margin", 0) or 0) > 0
            ):
                required_bytes = int(gate["needed_bytes_with_margin"])
                estimate.update(
                    {
                        "required_bytes": required_bytes,
                        "required_gib": round(required_bytes / (1024 ** 3), 3),
                        "basis": "prior_inventory_disk_gate",
                    }
                )
        return estimate

    def _refresh_pending_queue_estimates(self, state: Dict[str, object]) -> None:
        """Refresh waiting items so persisted v1 estimates cannot become stale."""
        for item in state.get("items", []):
            if not isinstance(item, dict) or item.get("status") not in {
                "QUEUED",
                "WAITING_ACTIVE_DOWNLOAD",
                "WAITING_SPACE",
            }:
                continue
            request = item.get("request")
            if not isinstance(request, dict):
                continue
            item["estimate"] = self._queue_estimate(request)
            item["updated_at"] = utc_now_text()

    def enqueue_download(self, request: Dict[str, object]) -> Dict[str, object]:
        try:
            normalized = normalize_request(request, DOWNLOAD_PLATFORM_NAMES)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        if normalized["continuous_upload"]:
            self._require_upload_configuration()
        self._queue_target_parent(normalized)
        estimate = self._queue_estimate(normalized)
        item = make_queue_item(normalized, estimate)
        with self._queue_lock:
            state = read_queue_state(self.queue_state_path)
            for existing in state["items"]:
                if (
                    isinstance(existing, dict)
                    and existing.get("semantic_key") == semantic_key(normalized)
                    and existing.get("status") in ACTIVE_QUEUE_STATUSES
                ):
                    return dict(existing)
            state["items"].append(item)
            self._save_queue_state(state)
        self.process_batch_queue_once()
        with self._queue_lock:
            state = read_queue_state(self.queue_state_path)
            return next(
                (dict(row) for row in state["items"] if row.get("queue_id") == item["queue_id"]),
                item,
            )

    def cancel_queued_download(self, queue_id: str) -> Dict[str, object]:
        identifier = str(queue_id or "").strip()
        if not identifier:
            raise RuntimeError("缺少 queue_id。")
        with self._queue_lock:
            state = read_queue_state(self.queue_state_path)
            for item in state["items"]:
                if not isinstance(item, dict) or item.get("queue_id") != identifier:
                    continue
                if item.get("status") not in CANCELLABLE_QUEUE_STATUSES:
                    raise RuntimeError("该队列项已经启动或结束，不能从队列取消。")
                item.update(
                    {
                        "status": "CANCELLED",
                        "status_message": "用户取消了尚未启动的队列项；没有删除任何数据。",
                        "updated_at": utc_now_text(),
                        "automatic_delete": False,
                    }
                )
                self._save_queue_state(state)
                return dict(item)
        raise RuntimeError("找不到队列项：{}".format(identifier))

    def batch_queue_status(self) -> Dict[str, object]:
        with self._queue_lock:
            result = public_queue_state(read_queue_state(self.queue_state_path))
            result["scheduler"] = {
                "running": bool(self._queue_thread and self._queue_thread.is_alive()),
                "last_check_at": self._queue_last_check_at,
                "last_error": self._queue_last_error,
                "interval_seconds": 15,
            }
            return result

    def _queue_gate(self, request: Dict[str, object], estimate: Dict[str, object]) -> Dict[str, object]:
        parent = self._queue_target_parent(request)
        usage = shutil.disk_usage(str(Path(parent.anchor) if parent.anchor else parent))
        required_bytes = int(estimate.get("required_bytes", 0) or 0)
        shortfall = max(0, required_bytes - usage.free)
        return {
            "drive": request.get("download_drive") or parent.anchor or str(parent),
            "free_bytes": usage.free,
            "free_gib": round(usage.free / (1024 ** 3), 3),
            "free_label": format_bytes(usage.free),
            "required_bytes": required_bytes,
            "required_gib": round(required_bytes / (1024 ** 3), 3),
            "required_label": format_bytes(required_bytes),
            "shortfall_bytes": shortfall,
            "shortfall_gib": round(shortfall / (1024 ** 3), 3),
            "shortfall_label": format_bytes(shortfall),
            "passes": shortfall == 0,
            "checked_at": utc_now_text(),
        }

    def _sync_launched_queue_items(self, state: Dict[str, object]) -> None:
        tasks = {task["batch_name"]: task for task in self.task_summaries(limit=None)}
        for item in state["items"]:
            if not isinstance(item, dict) or item.get("status") not in {"STARTING", "RUNNING"}:
                continue
            task = tasks.get(item.get("target_batch_name"))
            if not task:
                if item.get("status") == "STARTING":
                    item.update(
                        status="QUEUED",
                        status_message="控制台重启后已恢复为待调度状态。",
                        updated_at=utc_now_text(),
                    )
                continue
            download_status = str(task.get("download_status", ""))
            if download_status == "COMPLETE":
                item.update(
                    status="COMPLETE",
                    status_message="下载批次已完成。",
                    updated_at=utc_now_text(),
                )
            elif download_status in {"FAIL", "failed"}:
                disk_gate = task.get("disk_gate", {})
                if isinstance(disk_gate, dict) and disk_gate.get("exists"):
                    required = int(disk_gate.get("needed_bytes_with_margin", 0) or 0)
                    if required:
                        item["estimate"].update(
                            required_bytes=required,
                            required_gib=round(required / (1024 ** 3), 3),
                            basis="inventory_disk_gate",
                        )
                    item.update(
                        status="WAITING_SPACE",
                        status_message="实际清单磁盘门禁未通过，等待释放足够空间。",
                        updated_at=utc_now_text(),
                    )
                else:
                    item.update(
                        status="FAILED",
                        status_message=str(task.get("error") or "下载批次启动或运行失败。"),
                        updated_at=utc_now_text(),
                    )
            elif download_status in {"STARTING", "RUNNING"}:
                item.update(
                    status="RUNNING",
                    status_message="下载批次正在运行。",
                    updated_at=utc_now_text(),
                )

    def process_batch_queue_once(self) -> Dict[str, object]:
        with self._queue_dispatch_lock:
            return self._process_batch_queue_once_unlocked()

    def _process_batch_queue_once_unlocked(self) -> Dict[str, object]:
        self._queue_last_check_at = utc_now_text()
        self._queue_last_error = ""
        launch_item: Optional[Dict[str, object]] = None
        with self._queue_lock:
            state = read_queue_state(self.queue_state_path)
            self._sync_launched_queue_items(state)
            self._refresh_pending_queue_estimates(state)
            active = self._active_download_task()
            for item in state["items"]:
                if not isinstance(item, dict) or item.get("status") not in {
                    "QUEUED",
                    "WAITING_ACTIVE_DOWNLOAD",
                    "WAITING_SPACE",
                }:
                    continue
                gate = self._queue_gate(item["request"], item["estimate"])
                item["gate"] = gate
                item["updated_at"] = utc_now_text()
                if active:
                    item["status"] = "WAITING_ACTIVE_DOWNLOAD"
                    item["status_message"] = "等待当前下载批次 {} 完成。".format(
                        active.get("batch_name", "")
                    )
                    continue
                if not gate["passes"]:
                    item["status"] = "WAITING_SPACE"
                    item["status_message"] = "磁盘空间不足，还缺 {}。".format(
                        gate["shortfall_label"]
                    )
                    continue
                item["status"] = "STARTING"
                item["status_message"] = "下载与空间门禁均已通过，正在启动。"
                launch_item = dict(item)
                break
            self._save_queue_state(state)

        if launch_item is None:
            return self.batch_queue_status()
        try:
            result = self.start_download(dict(launch_item["request"]))
        except Exception as exc:
            with self._queue_lock:
                state = read_queue_state(self.queue_state_path)
                for item in state["items"]:
                    if item.get("queue_id") == launch_item["queue_id"]:
                        item.update(
                            status="FAILED",
                            status_message="{}: {}".format(type(exc).__name__, exc),
                            updated_at=utc_now_text(),
                        )
                        break
                self._save_queue_state(state)
            return self.batch_queue_status()

        with self._queue_lock:
            state = read_queue_state(self.queue_state_path)
            for item in state["items"]:
                if item.get("queue_id") == launch_item["queue_id"]:
                    item.update(
                        status="RUNNING",
                        status_message="下载批次已经在后台启动。",
                        updated_at=utc_now_text(),
                        launch=result,
                    )
                    break
            self._save_queue_state(state)
        return self.batch_queue_status()

    def start_batch_queue_scheduler(self, interval_seconds: int = 15) -> None:
        if self._queue_thread and self._queue_thread.is_alive():
            return

        def run() -> None:
            while not self._queue_stop.is_set():
                try:
                    self.process_batch_queue_once()
                except Exception as exc:
                    self._queue_last_error = "{}: {}".format(type(exc).__name__, exc)
                self._queue_stop.wait(max(5, interval_seconds))

        self._queue_thread = threading.Thread(
            target=run,
            daemon=True,
            name="geo-cloud-space-aware-batch-queue",
        )
        self._queue_thread.start()

    def start_notification_monitor(self, interval_seconds: int = 15) -> None:
        if not NOTIFICATION_SERVICE_PATH.is_file():
            return
        current = self.email_notifier.public_status().get("monitor", {})
        if isinstance(current, dict) and process_is_running(current.get("pid")):
            return
        self.notification_root.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            str(NOTIFICATION_SERVICE_PATH),
            "--batch-root",
            str(self.batch_root),
            "--state-path",
            str(self.notification_state_path),
            "--interval-seconds",
            str(max(5, interval_seconds)),
        ]
        creationflags = 0
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        stdout_path = self.notification_root / "notification_service.stdout.log"
        stderr_path = self.notification_root / "notification_service.stderr.log"
        with stdout_path.open("ab") as stdout_handle, stderr_path.open("ab") as stderr_handle:
            self._notification_process = subprocess.Popen(
                command,
                cwd=str(APP_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                creationflags=creationflags,
                start_new_session=os.name != "nt",
            )

    def send_test_email(self) -> Dict[str, object]:
        return self.email_notifier.send_test()

    def configure_email(self, request: Dict[str, object]) -> Dict[str, object]:
        token = str(request.get("setup_token", ""))
        if not secrets.compare_digest(token, self.notification_setup_token):
            raise RuntimeError("邮件配置会话已失效，请刷新仪表板后重试。")
        sender = str(request.get("sender", "")).strip()
        recipient = str(request.get("recipient", "")).strip()
        password = str(request.get("client_password", ""))
        expected_sender = self.notification_setup_defaults["sender"]
        expected_recipient = self.notification_setup_defaults["recipient"]
        if expected_sender and sender.casefold() != expected_sender.casefold():
            raise RuntimeError("发件邮箱与本次已确认的地址不一致。")
        if expected_recipient and recipient.casefold() != expected_recipient.casefold():
            raise RuntimeError("接收邮箱与本次已确认的地址不一致。")
        config_path = save_secure_email_config(
            smtp_host="mail.ustc.edu.cn",
            smtp_port=465,
            smtp_user=sender,
            smtp_password=password,
            sender=sender,
            recipient=recipient,
            use_ssl=True,
            use_starttls=False,
        )
        password = ""
        self.email_notifier.reload_settings()
        test = self.email_notifier.send_test()
        return {
            "status": "PASS",
            "recipient": test["recipient"],
            "sent_at": test["sent_at"],
            "credential_source": "windows_dpapi",
            "credentials_persisted": True,
            "config_path": str(config_path),
        }

    def _set_batch_root(self, batch_root: Path) -> None:
        self.batch_root = batch_root.resolve()
        self.manifest_dir = self.batch_root / "manifests"
        self.log_dir = self.batch_root / "logs"
        self.transfer_dir = self.batch_root / "transfer"

    @staticmethod
    def _watch_download_process(
        process: subprocess.Popen,
        batch_root: Path,
        launcher_status_path: Path,
    ) -> None:
        exit_code = process.wait()
        raw_batch_status = read_json(batch_root / "transfer" / "batch_status.json")
        completed = raw_batch_status.get("status") == "complete"
        failed_message = str(raw_batch_status.get("message", "")).strip()
        payload = read_json(launcher_status_path)
        payload.update(
            {
                "status": "COMPLETE" if exit_code == 0 and completed else "FAIL",
                "exit_code": exit_code,
                "process_alive": False,
                "finished_at": utc_now_text(),
                "message": (
                    "批次脚本正常完成。"
                    if exit_code == 0 and completed
                    else failed_message
                    or "批次脚本退出，但没有生成完整的完成状态。"
                ),
            }
        )
        write_json_atomic(launcher_status_path, payload)

    def status(self, batch_name: str = "") -> Dict[str, object]:
        batch_root = self._resolve_existing_batch(batch_name)
        manifest_dir = batch_root / "manifests"
        log_dir = batch_root / "logs"
        transfer_dir = batch_root / "transfer"
        combined_inventory_path = manifest_dir / "manifest_inventory.csv"
        legacy_met_inventory_path = manifest_dir / "manifest_meteosat_inventory.csv"
        met_inventory_path = (
            combined_inventory_path if combined_inventory_path.is_file() else legacy_met_inventory_path
        )
        s3_inventory_path = combined_inventory_path
        s3_inventory = read_inventory(s3_inventory_path, "s3")
        met_inventory = read_inventory(met_inventory_path, "eumetsat")
        s3_download = summarize_download(
            log_dir / "download_s3_range.log",
            s3_inventory_path,
            S3_EVENT_RE,
            "download_s3_range_start",
        )
        met_download = summarize_download(
            log_dir / "download_meteosat_range.log",
            met_inventory_path,
            MET_DOWNLOAD_RE,
            "download_meteosat_range_start",
        )
        s3_completed_manifest = read_downloaded_manifest(
            manifest_dir / "manifest_downloaded.csv", "s3"
        )
        met_completed_manifest = read_downloaded_manifest(
            manifest_dir / "manifest_meteosat_downloaded.csv", "eumetsat"
        )
        parts = active_parts(batch_root)
        transfer = transfer_manifest_status(transfer_dir)
        auto_upload = auto_upload_status(transfer_dir)
        auto_upload.update(upload_throughput(batch_root, auto_upload))
        download_parallelism = read_json(log_dir / "download_parallelism_status.json")
        server = server_verification_status(transfer_dir)
        xftp = marker_status(transfer_dir / "xftp_upload_complete.json")
        cleanup = marker_status(transfer_dir / "local_cleanup_approval.json")
        s3_validation = read_json(manifest_dir / "download_summary.json")
        met_validation = read_json(manifest_dir / "meteosat_download_summary.json")
        validation = {}
        if s3_validation or met_validation:
            validation = {
                "corrupt_rows": int(s3_validation.get("corrupt_rows", 0))
                + int(met_validation.get("corrupt_rows", 0)),
                "downloaded_rows": int(s3_validation.get("downloaded_rows", 0))
                + int(met_validation.get("downloaded_rows", 0)),
            }
        inventory_total = int(s3_inventory.get("found", 0)) + int(
            met_inventory.get("found", 0)
        )
        started_total = int(s3_download.get("total", 0)) + int(
            met_download.get("total", 0)
        )
        s3_completed = (
            int(s3_completed_manifest.get("completed", 0))
            if s3_completed_manifest.get("exists")
            else int(s3_download.get("overall_completed", 0))
        )
        met_completed = (
            int(met_completed_manifest.get("completed", 0))
            if met_completed_manifest.get("exists")
            else int(met_download.get("overall_completed", 0))
        )
        completed = s3_completed + met_completed
        total = max(inventory_total, started_total, completed)
        combined_download = {
            "total": total,
            "inventory_total": inventory_total,
            "started_total": started_total,
            "overall_completed": completed,
            "remaining": max(total - completed, 0),
            "failed": int(s3_download.get("failed", 0)) + int(met_download.get("failed", 0)),
            "scope": "full_inventory" if inventory_total else "started_downloads",
        }
        combined_download["percent"] = round(completed / total * 100, 2) if total else 0
        recent = sorted(
            list(s3_download.get("recent", [])) + list(met_download.get("recent", [])),
            key=lambda row: row.get("ts", ""),
        )[-12:]
        platform_found = merge_platform_counts(
            s3_inventory.get("by_platform", {}), met_inventory.get("by_platform", {})
        )
        platform_done = merge_platform_counts(
            s3_completed_manifest.get("by_platform", {})
            if s3_completed_manifest.get("exists")
            else s3_download.get("by_platform", {}),
            met_completed_manifest.get("by_platform", {})
            if met_completed_manifest.get("exists")
            else met_download.get("by_platform", {}),
        )
        raw_batch_status = read_json(transfer_dir / "batch_status.json")
        launcher = download_launcher_status(transfer_dir, raw_batch_status)
        run_failed = (
            launcher.get("status") == "FAIL"
            or raw_batch_status.get("status") == "failed"
        )
        files_complete = bool(total and completed >= total and not parts.get("count"))
        if run_failed and files_complete:
            combined_download["completion_state"] = "files_complete_unfinalized"
            combined_download["completion_message"] = (
                "目标文件已经齐全，但批次进程在生成最终清单前失败，不能视为完整完成。"
            )
        elif run_failed:
            combined_download["completion_state"] = "failed"
            combined_download["completion_message"] = str(
                launcher.get("message")
                or raw_batch_status.get("message")
                or "下载批次失败。"
            )
        elif files_complete:
            combined_download["completion_state"] = "files_complete"
            combined_download["completion_message"] = "目标文件已经齐全。"
        else:
            combined_download["completion_state"] = "running"
            combined_download["completion_message"] = "下载仍在进行。"
        stages = build_pipeline_stages(
            int(s3_inventory.get("found", 0)) + int(met_inventory.get("found", 0)),
            combined_download,
            parts,
            validation,
            transfer,
            auto_upload,
            xftp,
            server,
            cleanup,
        )
        if run_failed:
            failure_message = str(
                launcher.get("message")
                or raw_batch_status.get("message")
                or "下载批次失败。"
            )
            if not inventory_total:
                stages[0] = stage(
                    "fail",
                    "inventory",
                    "本地清单",
                    failure_message,
                )
            if files_complete:
                stages[1] = stage(
                    "warning",
                    "download",
                    "本地下载",
                    "{} / {} 个文件已齐，但批次未完成最终清单：{}".format(
                        completed, total, failure_message
                    ),
                )
            else:
                stages[1] = stage(
                    "fail",
                    "download",
                    "本地下载",
                    "{} / {} 个文件；{}".format(completed, total, failure_message),
                )
        overall_state = "running"
        if run_failed:
            overall_state = "launch_failed"
        elif server.get("status") == "FAIL":
            overall_state = "blocked"
        elif auto_upload.get("status") in {"FAIL", "STOPPED", "STALLED"}:
            overall_state = "upload_failed"
        elif cleanup.get("exists"):
            overall_state = "cleanup_approved"
        elif server.get("status") == "PASS":
            overall_state = "server_verified"
        elif xftp.get("exists"):
            overall_state = "awaiting_server_verification"
        elif auto_upload.get("status") in {"RUNNING", "STARTING"}:
            overall_state = "auto_uploading"
        elif transfer.get("status") == "READY_FOR_XFTP_UPLOAD":
            overall_state = "ready_for_xftp"
        disk_gate = enrich_disk_gate(
            parse_disk_gate(launcher.get("message") or raw_batch_status.get("message")),
            batch_root,
        )
        disk = disk_status(batch_root)
        trends = dashboard_trends(
            transfer_dir,
            {
                "download_rate_bps": parts.get("total_rate_bps"),
                "upload_rate_bps": auto_upload.get("rate_bps"),
                "disk_free_bytes": disk.get("free_bytes"),
                "download_percent": combined_download.get("percent"),
                "upload_percent": auto_upload.get("percent"),
            },
            should_record=(
                combined_download.get("completion_state") == "running"
                or auto_upload.get("status") in {"RUNNING", "STARTING"}
            ),
        )
        tasks = self.task_summaries()
        notification_status = self.email_notifier.public_status()
        notification_monitor = dict(notification_status.get("monitor", {}))
        notification_monitor["process_alive"] = process_is_running(
            notification_monitor.get("pid")
        )
        notification_status["monitor"] = notification_monitor
        return {
            "project_id": "geo_ring_cloud",
            "canonical_stage_id": "",
            "component_role": COMPONENT_ROLE,
            "related_stage_ids": RELATED_STAGE_IDS,
            "generated_at": utc_now_text(),
            "batch_root": str(batch_root),
            "batch_name": batch_root.name,
            "overall_state": overall_state,
            "raw_batch_status": raw_batch_status,
            "launcher_status": launcher,
            "disk": disk,
            "disk_gate": disk_gate,
            "tasks": tasks,
            "batch_queue": self.batch_queue_status(),
            "inventory": {"s3": s3_inventory, "meteosat": met_inventory},
            "download": {
                "combined": combined_download,
                "s3": s3_download,
                "meteosat": met_download,
                "recent": recent,
                "parallelism": download_parallelism,
            },
            "active_parts": parts,
            "trends": trends,
            "validation": validation,
            "transfer_manifest": transfer,
            "auto_upload": auto_upload,
            "xftp": xftp,
            "server_verification": server,
            "cleanup_approval": cleanup,
            "pipeline_stages": stages,
            "platforms": [
                {
                    "platform": name,
                    "found": int(platform_found.get(name, 0)),
                    "completed": int(platform_done.get(name, 0)),
                    "percent": round(
                        int(platform_done.get(name, 0)) / int(platform_found.get(name, 0)) * 100,
                        2,
                    )
                    if int(platform_found.get(name, 0))
                    else 0,
                }
                for name in PLATFORM_NAMES
                if int(platform_found.get(name, 0)) or int(platform_done.get(name, 0))
            ],
            "server_verify_command": "python3 geo_ring_cloud_transfer_batch.py verify --manifest {} --report server_verification.json --location server".format(
                transfer.get("filename", "<transfer_manifest.json>")
            ),
            "auto_upload_config": {
                "enabled": bool(self.ssh_target and self.identity_file and AUTO_UPLOADER_PATH.is_file()),
                "target": self.ssh_target,
                "identity_file": str(self.identity_file) if self.identity_file else "",
                "server_root": self.auto_upload_root,
                "credential_mode": "ssh-agent",
                "passphrase_stored": False,
            },
            "download_config": {
                "available_platforms": list(DOWNLOAD_PLATFORM_NAMES),
                "order_sources": ORDER_SOURCE_CONFIG,
                "inventory_workers_default": 8,
                "inventory_workers_max": 16,
                "download_workers_default": 12,
                "download_workers_max": 16,
                "adaptive_download_default": True,
                "adaptive_download_min_workers": 2,
                "adaptive_download_initial_workers": 4,
                "s3_range_mib": 4,
                "meteosat_worker_cap": 8,
                "network_mode": "direct_only",
                "inventory_mode": "daily_parallel_cache",
                "batch_parent": str(batch_root.parent),
                "available_drives": available_download_drives(),
            },
            "notifications": {
                "browser_supported": True,
                "email": notification_status,
                "setup_token": self.notification_setup_token,
                "setup_defaults": self.notification_setup_defaults,
            },
            "safety": {
                "automatic_upload": True,
                "automatic_delete": False,
                "cleanup_button_deletes_data": False,
            },
        }

    def start_download(self, request: Dict[str, object]) -> Dict[str, object]:
        with self._download_lock:
            if not BATCH_SCRIPT_PATH.is_file():
                raise RuntimeError("批处理脚本不存在：{}".format(BATCH_SCRIPT_PATH))
            start_text = str(request.get("start_date", "")).strip()
            end_text = str(request.get("end_date", "")).strip()
            try:
                start_date = datetime.strptime(start_text, "%Y-%m-%d").date()
                end_date = datetime.strptime(end_text, "%Y-%m-%d").date()
            except ValueError as exc:
                raise RuntimeError("开始和结束日期必须使用 YYYY-MM-DD。") from exc
            if end_date < start_date:
                raise RuntimeError("结束日期不能早于开始日期。")

            requested_platforms = request.get("platforms", [])
            if not isinstance(requested_platforms, list):
                raise RuntimeError("platforms 必须是数组。")
            platforms = []
            for value in requested_platforms:
                platform = str(value).strip()
                if platform not in DOWNLOAD_PLATFORM_NAMES:
                    raise RuntimeError("不支持的卫星平台：{}".format(platform))
                if platform not in platforms:
                    platforms.append(platform)
            if not platforms:
                raise RuntimeError("请至少选择一个卫星平台。")

            continuous_upload = bool(request.get("continuous_upload", True))
            adaptive_download = bool(request.get("adaptive_download", True))
            if continuous_upload:
                self._require_upload_configuration()

            try:
                inventory_workers = int(request.get("inventory_workers", 8))
                download_workers = int(request.get("download_workers", 12))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("并行数必须是整数。") from exc
            if not 1 <= inventory_workers <= 16:
                raise RuntimeError("清单并行数必须在 1–16 之间。")
            if not 1 <= download_workers <= 16:
                raise RuntimeError("下载并行数必须在 1–16 之间。")

            short_names = {
                "GOES-16": "g16",
                "GOES-18": "g18",
                "Himawari-9": "h9",
                "Meteosat-0deg": "m0",
                "Meteosat-IODC": "miodc",
            }
            batch_name = "{}_{}_{}".format(
                start_date.strftime("%Y%m%d"),
                end_date.strftime("%Y%m%d"),
                "-".join(short_names[name] for name in platforms),
            )
            active_task = self._active_download_task()
            if active_task:
                raise RuntimeError(
                    "当前下载批次仍在进行：{}。可将新任务加入自动队列。".format(
                        active_task.get("batch_name", "")
                    )
                )
            batch_parent = self._resolve_download_parent(request)
            batch_parent.mkdir(parents=True, exist_ok=True)
            batch_root = (batch_parent / batch_name).resolve()
            if batch_parent != batch_root.parent:
                raise RuntimeError("新批次目录必须位于允许的批次父目录内。")
            batch_root.mkdir(parents=True, exist_ok=True)
            (batch_root / "transfer").mkdir(parents=True, exist_ok=True)
            lock_path = batch_root / "transfer" / "batch_run.lock"
            launcher_status_path = batch_root / "transfer" / "download_launcher_status.json"
            existing_launcher = download_launcher_status(
                batch_root / "transfer",
                read_json(batch_root / "transfer" / "batch_status.json"),
            )
            if existing_launcher.get("process_alive"):
                raise RuntimeError(
                    "该批次的下载启动进程仍在运行：PID {}".format(
                        existing_launcher.get("pid", "")
                    )
                )
            stale_lock_recovery: Dict[str, object] = {}
            if lock_path.exists():
                lock_state = inspect_batch_lock(lock_path)
                if lock_state.get("held") or not lock_state.get("stale"):
                    raise RuntimeError("该批次已有任务运行中：{}".format(batch_root))
                try:
                    lock_path.unlink()
                except OSError as exc:
                    raise RuntimeError(
                        "残留批次锁无法安全移除：{}: {}".format(type(exc).__name__, exc)
                    ) from exc
                stale_lock_recovery = {
                    "project_id": "geo_ring_cloud",
                    "component_role": "data_download_orchestrator",
                    "recovered_at": utc_now_text(),
                    "lock": lock_state,
                    "previous_launcher_status": existing_launcher.get("status", "UNKNOWN"),
                    "previous_launcher_pid": existing_launcher.get("pid"),
                    "automatic_delete": False,
                    "message": "已移除无人占用的残留控制锁；未删除任何下载数据。",
                }
                write_json_atomic(
                    batch_root / "transfer" / "stale_lock_recovery.json",
                    stale_lock_recovery,
                )

            command = [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(BATCH_SCRIPT_PATH),
                "-BatchRoot",
                str(batch_root),
                "-ServerRoot",
                self.auto_upload_root,
                "-StartDate",
                start_text,
                "-EndDate",
                end_text,
                "-CondaEnvironment",
                self.conda_environment,
                "-Platforms",
                ",".join(platforms),
                "-InventoryWorkers",
                str(inventory_workers),
                "-DownloadWorkers",
                str(download_workers),
                "-S3RangeMiB",
                "4",
            ]
            if adaptive_download:
                command.extend(
                    [
                        "-AdaptiveDownload",
                        "-DownloadMinWorkers",
                        "2",
                        "-DownloadInitialWorkers",
                        str(min(4, download_workers)),
                    ]
                )
            if bool(request.get("refresh_inventory", False)):
                command.append("-RefreshInventory")
            environment = os.environ.copy()
            for name in (
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy",
                "all_proxy", "GEO_RING_LOCAL_PROXY", "GEO_CLOUD_GOES_PROXY",
                "GEO_CLOUD_HIMAWARI_PROXY", "GEO_CLOUD_S3_PROXY",
            ):
                environment.pop(name, None)
            environment["NO_PROXY"] = "*"
            environment["no_proxy"] = "*"
            stdout_path = batch_root / "transfer" / "launcher.stdout.log"
            stderr_path = batch_root / "transfer" / "launcher.stderr.log"
            creationflags = 0
            if os.name == "nt":
                creationflags = (
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                )
            launcher_payload = {
                "project_id": "geo_ring_cloud",
                "canonical_stage_id": "",
                "component_role": "data_download_orchestrator",
                "related_stage_ids": ["stage_00", "stage_00f"],
                "status": "STARTING",
                "process_alive": False,
                "started_at": utc_now_text(),
                "updated_at": utc_now_text(),
                "batch_root": str(batch_root),
                "start_date": start_text,
                "end_date": end_text,
                "platforms": platforms,
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "network_mode": "direct_only",
                "adaptive_download": adaptive_download,
                "download_initial_workers": min(4, download_workers),
                "download_max_workers": download_workers,
                "automatic_delete": False,
                "stale_lock_recovered": bool(stale_lock_recovery),
                "message": "正在创建后台下载进程。",
            }
            write_json_atomic(launcher_status_path, launcher_payload)
            try:
                with stdout_path.open("ab") as stdout_handle, stderr_path.open(
                    "ab"
                ) as stderr_handle:
                    process = subprocess.Popen(
                        command,
                        cwd=str(APP_ROOT),
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout_handle,
                        stderr=stderr_handle,
                        creationflags=creationflags,
                        start_new_session=os.name != "nt",
                    )
            except Exception as exc:
                launcher_payload.update(
                    {
                        "status": "FAIL",
                        "updated_at": utc_now_text(),
                        "message": "创建后台下载进程失败：{}".format(exc),
                    }
                )
                write_json_atomic(launcher_status_path, launcher_payload)
                raise
            launcher_payload.update(
                {
                    "status": "RUNNING",
                    "pid": process.pid,
                    "process_alive": True,
                    "updated_at": utc_now_text(),
                    "message": "后台下载进程已经启动。",
                }
            )
            write_json_atomic(launcher_status_path, launcher_payload)
            if process.poll() is None:
                threading.Thread(
                    target=self._watch_download_process,
                    args=(process, batch_root, launcher_status_path),
                    daemon=True,
                    name="geo-cloud-download-watcher-{}".format(process.pid),
                ).start()
            else:
                self._watch_download_process(process, batch_root, launcher_status_path)
            self._set_batch_root(batch_root)
            result = {
                "status": "STARTING",
                "pid": process.pid,
                "batch_name": batch_name,
                "batch_root": str(batch_root),
                "download_drive": batch_root.drive or str(batch_root.anchor),
                "platforms": platforms,
                "inventory_workers": inventory_workers,
                "download_workers": download_workers,
                "adaptive_download": adaptive_download,
                "network_mode": "direct_only",
                "continuous_upload": continuous_upload,
                "stale_lock_recovered": bool(stale_lock_recovery),
                "automatic_delete": False,
            }
            if continuous_upload:
                try:
                    result["continuous_upload_status"] = self.start_continuous_upload(
                        batch_name
                    )
                except Exception as exc:
                    result["continuous_upload_error"] = "{}: {}".format(
                        type(exc).__name__, exc
                    )
            return result

    def _require_upload_configuration(self) -> None:
        if not self.ssh_target or self.identity_file is None:
            raise RuntimeError("全自动流水线需要先配置 SSH 目标和密钥文件。")
        if not self.identity_file.is_file():
            raise RuntimeError("SSH 密钥文件不存在：{}".format(self.identity_file))

    def start_continuous_upload(self, batch_name: str = "") -> Dict[str, object]:
        with self._upload_lock:
            batch_root = self._resolve_existing_batch(batch_name)
            transfer_dir = batch_root / "transfer"
            raw = read_json(transfer_dir / "batch_status.json")
            launcher = download_launcher_status(transfer_dir, raw)
            self._require_upload_configuration()
            start_date = str(raw.get("start_date") or launcher.get("start_date") or "")
            end_date = str(raw.get("end_date") or launcher.get("end_date") or "")
            platforms = list(raw.get("platforms") or launcher.get("platforms") or [])
            if not start_date or not end_date or not platforms:
                raise RuntimeError("批次缺少日期或平台信息，不能启动持续上传。")

            current = auto_upload_status(transfer_dir)
            if current.get("status") in {"RUNNING", "STARTING"} and process_is_running(
                current.get("pid")
            ):
                return {
                    "status": str(current.get("status")),
                    "pid": current.get("pid"),
                    "batch_name": batch_root.name,
                    "already_running": True,
                    "mode": current.get("mode", "continuous_download_upload"),
                    "automatic_delete": False,
                }

            status_path = transfer_dir / "auto_upload_status.json"
            write_json_atomic(
                status_path,
                {
                    "project_id": "geo_ring_cloud",
                    "canonical_stage_id": "",
                    "component_role": "continuous_data_uploader",
                    "related_stage_ids": RELATED_STAGE_IDS,
                    "status": "STARTING",
                    "phase": "starting",
                    "mode": "continuous_download_upload",
                    "created_at": utc_now_text(),
                    "updated_at": utc_now_text(),
                    "batch_root": str(batch_root),
                    "target": self.ssh_target,
                    "server_root": self.auto_upload_root,
                    "file_count": 0,
                    "completed_files": 0,
                    "percent": 0.0,
                    "parallelism_mode": "adaptive",
                    "active_workers": 0,
                    "max_workers": 4,
                    "parallelism_reason": "starting",
                    "automatic_delete": False,
                },
            )
            command = [
                sys.executable,
                str(AUTO_UPLOADER_PATH),
                "--watch-batch-root",
                str(batch_root),
                "--start-date",
                start_date,
                "--end-date",
                end_date,
                "--target",
                self.ssh_target,
                "--identity-file",
                str(self.identity_file),
                "--server-root",
                self.auto_upload_root,
                "--allowed-server-parent",
                self.allowed_server_parent,
                "--status",
                str(status_path),
                "--verification-report",
                str(transfer_dir / "server_verification.json"),
                "--poll-seconds",
                "10",
                "--max-upload-workers",
                "4",
            ]
            for platform in platforms:
                command.extend(["--platform", str(platform)])
            stdout_path = transfer_dir / "continuous_upload.stdout.log"
            stderr_path = transfer_dir / "continuous_upload.stderr.log"
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                    subprocess, "DETACHED_PROCESS", 0
                )
            try:
                with stdout_path.open("ab") as stdout_handle, stderr_path.open(
                    "ab"
                ) as stderr_handle:
                    process = subprocess.Popen(
                        command,
                        cwd=str(APP_ROOT),
                        stdin=subprocess.DEVNULL,
                        stdout=stdout_handle,
                        stderr=stderr_handle,
                        creationflags=creationflags,
                        start_new_session=os.name != "nt",
                    )
            except Exception as exc:
                payload = read_json(status_path)
                payload.update(
                    {
                        "status": "FAIL",
                        "phase": "launch_failed",
                        "updated_at": utc_now_text(),
                        "error": "{}: {}".format(type(exc).__name__, exc),
                    }
                )
                write_json_atomic(status_path, payload)
                raise
            payload = read_json(status_path)
            payload.update(
                {
                    "status": "RUNNING",
                    "phase": "watching_download",
                    "pid": process.pid,
                    "updated_at": utc_now_text(),
                }
            )
            write_json_atomic(status_path, payload)
            return {
                "status": "RUNNING",
                "pid": process.pid,
                "batch_name": batch_root.name,
                "batch_root": str(batch_root),
                "mode": "continuous_download_upload",
                "target": self.ssh_target,
                "server_root": self.auto_upload_root,
                "automatic_delete": False,
            }

    def start_auto_upload(self, batch_name: str = "") -> Dict[str, object]:
        with self._upload_lock:
            batch_root = self._resolve_existing_batch(batch_name)
            transfer_dir = batch_root / "transfer"
            transfer = transfer_manifest_status(transfer_dir)
            if transfer.get("status") != "READY_FOR_XFTP_UPLOAD":
                raise RuntimeError("传输清单尚未就绪，不能开始自动上传。")
            self._require_upload_configuration()
            current = auto_upload_status(transfer_dir)
            if current.get("status") in {"RUNNING", "STARTING"} and process_is_running(
                current.get("pid")
            ):
                raise RuntimeError("自动上传进程已经在运行。")

            status_path = transfer_dir / "auto_upload_status.json"
            write_json_atomic(
                status_path,
                {
                    "project_id": "geo_ring_cloud",
                    "canonical_stage_id": "",
                    "component_role": "automated_data_uploader",
                    "related_stage_ids": RELATED_STAGE_IDS,
                    "status": "STARTING",
                    "phase": "starting",
                    "mode": "adaptive_upload",
                    "created_at": utc_now_text(),
                    "updated_at": utc_now_text(),
                    "manifest": transfer.get("path", ""),
                    "target": self.ssh_target,
                    "server_root": self.auto_upload_root,
                    "parallelism_mode": "adaptive",
                    "active_workers": 0,
                    "max_workers": 4,
                    "parallelism_reason": "starting",
                    "automatic_delete": False,
                },
            )
            command = [
                sys.executable,
                str(AUTO_UPLOADER_PATH),
                "--manifest",
                str(transfer["path"]),
                "--target",
                self.ssh_target,
                "--identity-file",
                str(self.identity_file),
                "--server-root",
                self.auto_upload_root,
                "--allowed-server-parent",
                self.allowed_server_parent,
                "--status",
                str(status_path),
                "--verification-report",
                str(transfer_dir / "server_verification.json"),
                "--max-upload-workers",
                "4",
            ]
            stdout_path = transfer_dir / "auto_upload.stdout.log"
            stderr_path = transfer_dir / "auto_upload.stderr.log"
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                    subprocess, "DETACHED_PROCESS", 0
                )
            with stdout_path.open("ab") as stdout_handle, stderr_path.open("ab") as stderr_handle:
                process = subprocess.Popen(
                    command,
                    cwd=str(APP_ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    creationflags=creationflags,
                    start_new_session=os.name != "nt",
                )
            payload = read_json(status_path)
            payload["pid"] = process.pid
            payload["updated_at"] = utc_now_text()
            write_json_atomic(status_path, payload)
            return {
                "status": "STARTING",
                "pid": process.pid,
                "batch_name": batch_root.name,
                "batch_root": str(batch_root),
                "target": self.ssh_target,
                "server_root": self.auto_upload_root,
                "automatic_delete": False,
            }

    def mark_xftp_complete(self, batch_name: str = "") -> Dict[str, object]:
        batch_root = self._resolve_existing_batch(batch_name)
        transfer_dir = batch_root / "transfer"
        transfer = transfer_manifest_status(transfer_dir)
        if transfer.get("status") != "READY_FOR_XFTP_UPLOAD":
            raise RuntimeError("传输清单尚未就绪，不能确认 Xftp 上传完成。")
        path = transfer_dir / "xftp_upload_complete.json"
        payload = {
            "project_id": "geo_ring_cloud",
            "canonical_stage_id": "",
            "component_role": COMPONENT_ROLE,
            "related_stage_ids": RELATED_STAGE_IDS,
            "created_at": utc_now_text(),
            "status": "USER_CONFIRMED_XFTP_COMPLETE",
            "transfer_manifest": transfer.get("path", ""),
            "automatic_delete": False,
        }
        write_json_atomic(path, payload)
        return payload

    def approve_cleanup(self, batch_name: str = "") -> Dict[str, object]:
        batch_root = self._resolve_existing_batch(batch_name)
        transfer_dir = batch_root / "transfer"
        server = server_verification_status(transfer_dir)
        xftp = marker_status(transfer_dir / "xftp_upload_complete.json")
        if not xftp.get("exists"):
            raise RuntimeError("尚未确认 Xftp 上传完成。")
        if server.get("status") != "PASS":
            raise RuntimeError("服务器 SHA-256 复核尚未 PASS，禁止批准本地清理。")
        path = transfer_dir / "local_cleanup_approval.json"
        payload = {
            "project_id": "geo_ring_cloud",
            "canonical_stage_id": "",
            "component_role": COMPONENT_ROLE,
            "related_stage_ids": RELATED_STAGE_IDS,
            "created_at": utc_now_text(),
            "status": "USER_APPROVED_CLEANUP",
            "server_verification": server.get("path", ""),
            "approval_only": True,
            "delete_executed": False,
            "automatic_delete": False,
        }
        write_json_atomic(path, payload)
        return payload

    def open_cleanup_folder(self, batch_name: str = "") -> Dict[str, object]:
        """Open Explorer only after the user has recorded cleanup approval."""
        batch_root = self._resolve_existing_batch(batch_name)
        transfer_dir = batch_root / "transfer"
        approval = marker_status(transfer_dir / "local_cleanup_approval.json")
        if not approval.get("exists"):
            raise RuntimeError("请先确认允许清理；此操作不会删除任何文件。")
        if os.name != "nt":
            raise RuntimeError("当前系统不支持打开 Windows 资源管理器。")
        # FY4B files remain in the official application's external source
        # folder; the dashboard batch only holds control manifests.  Open the
        # actual approved raw-data location so the user can make the explicit
        # deletion decision after server verification, while other platforms
        # keep opening their batch root.
        folder = batch_root
        source_kind = "batch_root"
        fy4b_request = read_json(transfer_dir / "fy4b_official_import_request.json")
        source_text = str(fy4b_request.get("source_root", "")).strip()
        if source_text:
            source_root = Path(source_text).expanduser()
            if source_root.is_dir() and source_root != Path(source_root.anchor):
                folder = source_root.resolve()
                source_kind = "fy4b_official_source"
        try:
            subprocess.Popen(
                ["explorer.exe", str(folder)],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise RuntimeError("无法打开本地数据文件夹：{}".format(exc)) from exc
        return {
            "batch_name": batch_root.name,
            "folder": str(folder),
            "folder_kind": source_kind,
            "opened": True,
            "delete_executed": False,
        }


def make_handler(state: DashboardState):
    class Handler(BaseHTTPRequestHandler):
        def send_bytes(self, payload: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def send_json(self, payload: Dict[str, object], status: int = 200) -> None:
            self.send_bytes(
                json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
                "application/json; charset=utf-8",
                status,
            )

        def read_json_body(self, required: bool = False) -> Dict[str, object]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise RuntimeError("请求正文大小无效。") from exc
            if length == 0 and not required:
                return {}
            if length < 2 or length > 65536:
                raise RuntimeError("请求正文大小无效。")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise RuntimeError("请求正文必须是 JSON 对象。")
            return payload

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/status":
                batch_name = parse_qs(parsed.query).get("batch_name", [""])[0]
                try:
                    self.send_json(state.status(batch_name))
                except RuntimeError as exc:
                    self.send_json({"ok": False, "error": str(exc)}, 404)
                return
            if path == "/guide":
                guide = GUIDE_PATH.read_text(encoding="utf-8") if GUIDE_PATH.is_file() else "操作说明不存在。"
                html = (
                    "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                    "<title>GEO 数据操作说明</title><style>body{margin:0;background:#f4f2ec;color:#172421;"
                    "font-family:'Microsoft YaHei','Segoe UI',sans-serif}main{max-width:980px;margin:auto;padding:32px}"
                    "pre{white-space:pre-wrap;background:#fff;border:1px solid #d7d6cf;border-radius:18px;padding:28px;"
                    "font:14px/1.8 Consolas,'Microsoft YaHei',sans-serif;box-shadow:0 16px 48px #18372d14}"
                    "a{color:#12634c}</style><main><p><a href='/'>← 返回仪表板</a></p><pre>"
                    + guide.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    + "</pre></main></html>"
                )
                self.send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path in {"/", "/index.html"}:
                if not HTML_PATH.is_file():
                    self.send_json({"error": "前端页面文件不存在。"}, 500)
                    return
                self.send_bytes(HTML_PATH.read_bytes(), "text/html; charset=utf-8")
                return
            self.send_json({"error": "not found"}, 404)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            try:
                if path == "/api/actions/start-download":
                    payload = self.read_json_body(required=True)
                    self.send_json({"ok": True, "download": state.start_download(payload)})
                    return
                if path == "/api/actions/enqueue-download":
                    payload = self.read_json_body(required=True)
                    self.send_json({"ok": True, "queue_item": state.enqueue_download(payload)})
                    return
                if path == "/api/actions/cancel-queued-download":
                    payload = self.read_json_body(required=True)
                    self.send_json(
                        {
                            "ok": True,
                            "queue_item": state.cancel_queued_download(
                                str(payload.get("queue_id", ""))
                            ),
                        }
                    )
                    return
                if path == "/api/actions/start-auto-upload":
                    payload = self.read_json_body()
                    self.send_json(
                        {
                            "ok": True,
                            "upload": state.start_auto_upload(
                                str(payload.get("batch_name", ""))
                            ),
                        }
                    )
                    return
                if path == "/api/actions/preview-fy4b-official-upload":
                    payload = self.read_json_body(required=True)
                    self.send_json(
                        {
                            "ok": True,
                            "preview": state.preview_fy4b_official_upload(payload),
                        }
                    )
                    return
                if path == "/api/actions/start-fy4b-official-upload":
                    payload = self.read_json_body(required=True)
                    self.send_json(
                        {
                            "ok": True,
                            "fy4b": state.start_fy4b_official_upload(payload),
                        }
                    )
                    return
                if path == "/api/actions/start-continuous-upload":
                    payload = self.read_json_body()
                    self.send_json(
                        {
                            "ok": True,
                            "upload": state.start_continuous_upload(
                                str(payload.get("batch_name", ""))
                            ),
                        }
                    )
                    return
                if path == "/api/actions/send-test-email":
                    self.read_json_body()
                    self.send_json(
                        {
                            "ok": True,
                            "email": state.send_test_email(),
                        }
                    )
                    return
                if path == "/api/actions/configure-email":
                    payload = self.read_json_body(required=True)
                    self.send_json(
                        {
                            "ok": True,
                            "email": state.configure_email(payload),
                        }
                    )
                    return
                if path == "/api/actions/mark-xftp-complete":
                    payload = self.read_json_body()
                    self.send_json(
                        {
                            "ok": True,
                            "marker": state.mark_xftp_complete(
                                str(payload.get("batch_name", ""))
                            ),
                        }
                    )
                    return
                if path == "/api/actions/approve-cleanup":
                    payload = self.read_json_body()
                    self.send_json(
                        {
                            "ok": True,
                            "marker": state.approve_cleanup(
                                str(payload.get("batch_name", ""))
                            ),
                        }
                    )
                    return
                if path == "/api/actions/open-cleanup-folder":
                    payload = self.read_json_body()
                    self.send_json(
                        {
                            "ok": True,
                            "folder": state.open_cleanup_folder(
                                str(payload.get("batch_name", ""))
                            ),
                        }
                    )
                    return
                self.send_json({"error": "not found"}, 404)
            except RuntimeError as exc:
                self.send_json({"ok": False, "error": str(exc)}, 409)
            except Exception as exc:
                self.send_json({"ok": False, "error": "{}: {}".format(type(exc).__name__, exc)}, 500)

        def log_message(self, fmt: str, *args) -> None:
            return

    return Handler


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GEO 数据下载和传输中文仪表板。")
    parser.add_argument(
        "--batch-root",
        default=os.environ.get("GEO_RING_TRANSFER_BATCH_ROOT", ""),
        help="本地批次根目录，也可通过 GEO_RING_TRANSFER_BATCH_ROOT 设置。",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--ssh-target",
        default=os.environ.get("GEO_RING_UPLOAD_SSH_TARGET", ""),
        help="SSH target used by automatic upload, for example dhr@210.45.127.28.",
    )
    parser.add_argument(
        "--identity-file",
        default=os.environ.get("GEO_RING_UPLOAD_IDENTITY_FILE", ""),
        help="Private key path. Its passphrase must be unlocked in ssh-agent.",
    )
    parser.add_argument(
        "--auto-upload-root",
        default=os.environ.get(
            "GEO_RING_AUTO_UPLOAD_ROOT", "/data04/1/dhr/geo_ring_cloud_auto_upload"
        ),
    )
    parser.add_argument(
        "--allowed-server-parent",
        default=os.environ.get("GEO_RING_ALLOWED_SERVER_PARENT", "/data04/1/dhr"),
    )
    parser.add_argument(
        "--conda-environment",
        default=os.environ.get("GEO_RING_DOWNLOAD_CONDA_ENVIRONMENT", "pytorch"),
    )
    parser.add_argument(
        "--notification-setup-sender",
        default=os.environ.get("GEO_RING_NOTIFY_SETUP_SENDER", ""),
    )
    parser.add_argument(
        "--notification-setup-recipient",
        default=os.environ.get("GEO_RING_NOTIFY_SETUP_RECIPIENT", ""),
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if not args.batch_root:
        print("ERROR: 必须提供 --batch-root 或 GEO_RING_TRANSFER_BATCH_ROOT。", file=sys.stderr)
        return 2
    batch_root = Path(args.batch_root)
    if not batch_root.is_dir():
        print("ERROR: 批次目录不存在：{}".format(batch_root), file=sys.stderr)
        return 2
    identity_file = Path(args.identity_file).expanduser() if args.identity_file else None
    state = DashboardState(
        batch_root,
        ssh_target=args.ssh_target,
        identity_file=identity_file,
        auto_upload_root=args.auto_upload_root,
        allowed_server_parent=args.allowed_server_parent,
        conda_environment=args.conda_environment,
        notification_setup_sender=args.notification_setup_sender,
        notification_setup_recipient=args.notification_setup_recipient,
    )
    state.start_notification_monitor()
    state.start_batch_queue_scheduler()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(state))
    print("http://{}:{}".format(args.host, args.port), flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
