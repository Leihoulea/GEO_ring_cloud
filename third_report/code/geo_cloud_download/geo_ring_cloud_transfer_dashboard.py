"""Local Chinese dashboard for GEO download, Xftp, and server verification.

The dashboard is intentionally local-only.  Its two POST actions create audit
markers; neither action uploads, moves, truncates, or deletes raw data.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

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
BATCH_SCRIPT_PATH = APP_ROOT / "geo_ring_cloud_transfer_batch.ps1"
DOWNLOAD_PLATFORM_NAMES = (
    "GOES-16",
    "GOES-18",
    "Himawari-9",
    "Meteosat-0deg",
    "Meteosat-IODC",
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
_PART_SNAPSHOT: Dict[str, Tuple[int, float]] = {}


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def iso_mtime(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
    except OSError:
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
    current: Dict[str, Tuple[int, float]] = {}
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
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        key = str(path)
        current[key] = (stat.st_size, now)
        previous = _PART_SNAPSHOT.get(key)
        rate = None
        if previous:
            elapsed = max(now - previous[1], 0.001)
            rate = max(stat.st_size - previous[0], 0) / elapsed
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
    _PART_SNAPSHOT = current
    return {
        "count": len(rows),
        "displayed_count": len(rows),
        "total_rate_bps": total_rate if measured else None,
        "total_rate_label": "{}/s".format(format_bytes(total_rate)) if measured else "测量中",
        "items": rows,
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
    return payload


def process_is_running(pid: object) -> bool:
    try:
        numeric_pid = int(pid)
        if numeric_pid <= 0:
            return False
        os.kill(numeric_pid, 0)
        return True
    except (OSError, TypeError, ValueError):
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
    payload["process_alive"] = process_is_running(payload.get("pid"))
    if (
        payload.get("status") in {"STARTING", "RUNNING"}
        and not payload["process_alive"]
        and raw_batch_status.get("status") != "complete"
    ):
        previous_message = str(payload.get("message", "")).strip()
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
    elif auto_upload.get("status") == "FAIL":
        xftp_state = "fail"
        upload_detail = str(auto_upload.get("error", "自动上传失败"))
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


def write_json_atomic(path: Path, payload: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


class DashboardState:
    def __init__(
        self,
        batch_root: Path,
        ssh_target: str = "",
        identity_file: Optional[Path] = None,
        auto_upload_root: str = "/data04/1/dhr/geo_ring_cloud_auto_upload",
        allowed_server_parent: str = "/data04/1/dhr",
        conda_environment: str = "pytorch",
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

    def status(self) -> Dict[str, object]:
        combined_inventory_path = self.manifest_dir / "manifest_inventory.csv"
        legacy_met_inventory_path = self.manifest_dir / "manifest_meteosat_inventory.csv"
        met_inventory_path = (
            combined_inventory_path if combined_inventory_path.is_file() else legacy_met_inventory_path
        )
        s3_inventory_path = combined_inventory_path
        s3_inventory = read_inventory(s3_inventory_path, "s3")
        met_inventory = read_inventory(met_inventory_path, "eumetsat")
        s3_download = summarize_download(
            self.log_dir / "download_s3_range.log",
            s3_inventory_path,
            S3_EVENT_RE,
            "download_s3_range_start",
        )
        met_download = summarize_download(
            self.log_dir / "download_meteosat_range.log",
            met_inventory_path,
            MET_DOWNLOAD_RE,
            "download_meteosat_range_start",
        )
        parts = active_parts(self.batch_root)
        transfer = transfer_manifest_status(self.transfer_dir)
        auto_upload = auto_upload_status(self.transfer_dir)
        server = server_verification_status(self.transfer_dir)
        xftp = marker_status(self.transfer_dir / "xftp_upload_complete.json")
        cleanup = marker_status(self.transfer_dir / "local_cleanup_approval.json")
        s3_validation = read_json(self.manifest_dir / "download_summary.json")
        met_validation = read_json(self.manifest_dir / "meteosat_download_summary.json")
        validation = {}
        if s3_validation or met_validation:
            validation = {
                "corrupt_rows": int(s3_validation.get("corrupt_rows", 0))
                + int(met_validation.get("corrupt_rows", 0)),
                "downloaded_rows": int(s3_validation.get("downloaded_rows", 0))
                + int(met_validation.get("downloaded_rows", 0)),
            }
        combined_download = {
            "total": int(s3_download.get("total", 0)) + int(met_download.get("total", 0)),
            "overall_completed": int(s3_download.get("overall_completed", 0))
            + int(met_download.get("overall_completed", 0)),
            "failed": int(s3_download.get("failed", 0)) + int(met_download.get("failed", 0)),
        }
        total = int(combined_download["total"])
        completed = int(combined_download["overall_completed"])
        combined_download["percent"] = round(completed / total * 100, 2) if total else 0
        recent = sorted(
            list(s3_download.get("recent", [])) + list(met_download.get("recent", [])),
            key=lambda row: row.get("ts", ""),
        )[-12:]
        platform_found = merge_platform_counts(
            s3_inventory.get("by_platform", {}), met_inventory.get("by_platform", {})
        )
        platform_done = merge_platform_counts(
            s3_download.get("by_platform", {}), met_download.get("by_platform", {})
        )
        raw_batch_status = read_json(self.transfer_dir / "batch_status.json")
        launcher = download_launcher_status(self.transfer_dir, raw_batch_status)
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
        if launcher.get("status") == "FAIL" or raw_batch_status.get("status") == "failed":
            stages[0] = stage(
                "fail",
                "inventory",
                "本地清单",
                str(
                    launcher.get("message")
                    or raw_batch_status.get("message")
                    or "启动失败"
                ),
            )
        overall_state = "running"
        if launcher.get("status") == "FAIL" or raw_batch_status.get("status") == "failed":
            overall_state = "launch_failed"
        elif server.get("status") == "FAIL":
            overall_state = "blocked"
        elif auto_upload.get("status") == "FAIL":
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
        return {
            "project_id": "geo_ring_cloud",
            "canonical_stage_id": "",
            "component_role": COMPONENT_ROLE,
            "related_stage_ids": RELATED_STAGE_IDS,
            "generated_at": utc_now_text(),
            "batch_root": str(self.batch_root),
            "batch_name": self.batch_root.name,
            "overall_state": overall_state,
            "raw_batch_status": raw_batch_status,
            "launcher_status": launcher,
            "disk": disk_status(self.batch_root),
            "inventory": {"s3": s3_inventory, "meteosat": met_inventory},
            "download": {
                "combined": combined_download,
                "s3": s3_download,
                "meteosat": met_download,
                "recent": recent,
            },
            "active_parts": parts,
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
                "download_workers_default": 4,
                "download_workers_max": 16,
                "s3_range_mib": 4,
                "meteosat_worker_cap": 8,
                "network_mode": "direct_only",
                "inventory_mode": "daily_parallel_cache",
                "batch_parent": str(self.batch_parent),
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

            try:
                inventory_workers = int(request.get("inventory_workers", 8))
                download_workers = int(request.get("download_workers", 4))
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
            batch_root = (self.batch_parent / batch_name).resolve()
            if self.batch_parent != batch_root.parent:
                raise RuntimeError("新批次目录必须位于允许的批次父目录内。")
            lock_path = batch_root / "transfer" / "batch_run.lock"
            if lock_path.exists():
                raise RuntimeError("该批次已有任务运行中：{}".format(batch_root))
            batch_root.mkdir(parents=True, exist_ok=True)
            (batch_root / "transfer").mkdir(parents=True, exist_ok=True)
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
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
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
                "automatic_delete": False,
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
            return {
                "status": "STARTING",
                "pid": process.pid,
                "batch_root": str(batch_root),
                "platforms": platforms,
                "inventory_workers": inventory_workers,
                "download_workers": download_workers,
                "network_mode": "direct_only",
                "automatic_delete": False,
            }

    def start_auto_upload(self) -> Dict[str, object]:
        with self._upload_lock:
            transfer = transfer_manifest_status(self.transfer_dir)
            if transfer.get("status") != "READY_FOR_XFTP_UPLOAD":
                raise RuntimeError("传输清单尚未就绪，不能开始自动上传。")
            if not self.ssh_target or self.identity_file is None:
                raise RuntimeError("仪表板尚未配置 SSH 目标或密钥文件。")
            if not self.identity_file.is_file():
                raise RuntimeError("SSH 密钥文件不存在：{}".format(self.identity_file))
            current = auto_upload_status(self.transfer_dir)
            if current.get("status") in {"RUNNING", "STARTING"} and process_is_running(
                current.get("pid")
            ):
                raise RuntimeError("自动上传进程已经在运行。")

            status_path = self.transfer_dir / "auto_upload_status.json"
            write_json_atomic(
                status_path,
                {
                    "project_id": "geo_ring_cloud",
                    "canonical_stage_id": "",
                    "component_role": "automated_data_uploader",
                    "related_stage_ids": RELATED_STAGE_IDS,
                    "status": "STARTING",
                    "phase": "starting",
                    "created_at": utc_now_text(),
                    "updated_at": utc_now_text(),
                    "manifest": transfer.get("path", ""),
                    "target": self.ssh_target,
                    "server_root": self.auto_upload_root,
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
                str(self.transfer_dir / "server_verification.json"),
            ]
            stdout_path = self.transfer_dir / "auto_upload.stdout.log"
            stderr_path = self.transfer_dir / "auto_upload.stderr.log"
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
                "target": self.ssh_target,
                "server_root": self.auto_upload_root,
                "automatic_delete": False,
            }

    def mark_xftp_complete(self) -> Dict[str, object]:
        transfer = transfer_manifest_status(self.transfer_dir)
        if transfer.get("status") != "READY_FOR_XFTP_UPLOAD":
            raise RuntimeError("传输清单尚未就绪，不能确认 Xftp 上传完成。")
        path = self.transfer_dir / "xftp_upload_complete.json"
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

    def approve_cleanup(self) -> Dict[str, object]:
        server = server_verification_status(self.transfer_dir)
        xftp = marker_status(self.transfer_dir / "xftp_upload_complete.json")
        if not xftp.get("exists"):
            raise RuntimeError("尚未确认 Xftp 上传完成。")
        if server.get("status") != "PASS":
            raise RuntimeError("服务器 SHA-256 复核尚未 PASS，禁止批准本地清理。")
        path = self.transfer_dir / "local_cleanup_approval.json"
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

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/api/status":
                self.send_json(state.status())
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
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                    except ValueError:
                        length = 0
                    if length < 2 or length > 65536:
                        self.send_json({"ok": False, "error": "请求正文大小无效。"}, 400)
                        return
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(payload, dict):
                        self.send_json({"ok": False, "error": "请求正文必须是 JSON 对象。"}, 400)
                        return
                    self.send_json({"ok": True, "download": state.start_download(payload)})
                    return
                if path == "/api/actions/start-auto-upload":
                    self.send_json({"ok": True, "upload": state.start_auto_upload()})
                    return
                if path == "/api/actions/mark-xftp-complete":
                    self.send_json({"ok": True, "marker": state.mark_xftp_complete()})
                    return
                if path == "/api/actions/approve-cleanup":
                    self.send_json({"ok": True, "marker": state.approve_cleanup()})
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
    )
    server = ThreadingHTTPServer((args.host, args.port), make_handler(state))
    print("http://{}:{}".format(args.host, args.port), flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
