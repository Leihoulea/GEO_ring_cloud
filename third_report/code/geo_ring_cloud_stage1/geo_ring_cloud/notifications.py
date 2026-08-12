"""Persistent notification delivery for Geo Ring Cloud operations.

Credentials are read from environment variables only.  The persisted state
contains transition snapshots, an outbox, retry evidence, and health metadata;
it never contains SMTP passwords or SSH credentials.
"""

from __future__ import annotations

import hashlib
import base64
import json
import os
import re
import smtplib
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


COMPONENT_ROLE = "notification_delivery"
RELATED_STAGE_IDS = ["stage_00"]
SCHEMA_VERSION = 1
SECURE_CONFIG_SCHEMA_VERSION = 1
EMAIL_ENV_KEYS = (
    "GEO_RING_NOTIFY_SMTP_HOST",
    "GEO_RING_NOTIFY_SMTP_PORT",
    "GEO_RING_NOTIFY_SMTP_USER",
    "GEO_RING_NOTIFY_SMTP_PASSWORD",
    "GEO_RING_NOTIFY_EMAIL_FROM",
    "GEO_RING_NOTIFY_EMAIL_TO",
    "GEO_RING_NOTIFY_SMTP_SSL",
    "GEO_RING_NOTIFY_SMTP_STARTTLS",
)
RETRY_DELAYS_SECONDS = (60, 300, 900, 3600, 10800)
TERMINAL_STATES = {
    "download": {"COMPLETE", "FAIL", "FAILED"},
    "upload": {"PASS", "FAIL", "STOPPED", "STALLED"},
    "server": {"PASS", "FAIL"},
}


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: object) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def write_json_atomic(path: Path, payload: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, ensure_ascii=False)
    last_error: Optional[OSError] = None
    for attempt in range(12):
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
            if attempt < 11:
                time.sleep(0.25)
    assert last_error is not None
    raise last_error


def secure_email_config_path() -> Optional[Path]:
    """Return the per-user encrypted SMTP configuration path."""
    configured = os.environ.get("GEO_RING_NOTIFY_CONFIG_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        return None
    return Path(local_app_data) / "GeoRingCloud" / "email_notification.json"


def _dpapi_protect(value: str) -> str:
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI 仅能在 Windows 上使用。")
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    script = (
        "$s=ConvertTo-SecureString ([Text.Encoding]::UTF8.GetString("
        "[Convert]::FromBase64String($env:GEO_RING_DPAPI_INPUT))) -AsPlainText -Force;"
        "ConvertFrom-SecureString $s"
    )
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GEO_RING_DPAPI_INPUT": encoded},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    protected = completed.stdout.strip()
    if not protected:
        raise RuntimeError("Windows DPAPI 未返回加密结果。")
    return protected


def _dpapi_unprotect(value: str) -> str:
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI 仅能在 Windows 上使用。")
    script = (
        "$s=ConvertTo-SecureString $env:GEO_RING_DPAPI_INPUT;"
        "$b=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s);"
        "try{$p=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($b);"
        "[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($p))}"
        "finally{[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b)}"
    )
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GEO_RING_DPAPI_INPUT": value},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    decoded = base64.b64decode(completed.stdout.strip().encode("ascii"), validate=True)
    return decoded.decode("utf-8")


def save_secure_email_config(
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    sender: str,
    recipient: str,
    use_ssl: bool,
    use_starttls: bool,
) -> Path:
    """Persist SMTP settings with only the password protected by user-scoped DPAPI."""
    path = secure_email_config_path()
    if path is None:
        raise RuntimeError("找不到 LOCALAPPDATA，无法保存本机加密配置。")
    host = str(smtp_host or "").strip()
    user = str(smtp_user or "").strip()
    from_address = str(sender or "").strip()
    to_address = str(recipient or "").strip()
    password = str(smtp_password or "")
    email_pattern = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    if not host:
        raise RuntimeError("SMTP 服务器不能为空。")
    if not 1 <= int(smtp_port) <= 65535:
        raise RuntimeError("SMTP 端口无效。")
    if not user or not email_pattern.fullmatch(from_address):
        raise RuntimeError("发件邮箱格式无效。")
    if not email_pattern.fullmatch(to_address):
        raise RuntimeError("收件邮箱格式无效。")
    if not password:
        raise RuntimeError("客户端专用密码不能为空。")
    payload: Dict[str, object] = {
        "schema_version": SECURE_CONFIG_SCHEMA_VERSION,
        "project_id": "geo_ring_cloud",
        "canonical_stage_id": "",
        "component_role": COMPONENT_ROLE,
        "related_stage_ids": RELATED_STAGE_IDS,
        "smtp_host": host,
        "smtp_port": int(smtp_port),
        "smtp_user": user,
        "email_from": from_address,
        "email_to": to_address,
        "smtp_ssl": bool(use_ssl),
        "smtp_starttls": bool(use_starttls),
        "password_dpapi": _dpapi_protect(password),
        "password_scope": "WindowsCurrentUser",
        "updated_at": utc_now_text(),
    }
    write_json_atomic(path, payload)
    return path


def _load_secure_email_config() -> Tuple[Dict[str, object], Optional[Path]]:
    path = secure_email_config_path()
    if path is None or not path.is_file():
        return {}, path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("配置根节点不是对象")
        encrypted = str(
            payload.get("password_dpapi", payload.get("password_dpapi_b64", ""))
        )
        if not encrypted:
            raise ValueError("配置缺少 DPAPI 密文")
        settings = dict(payload)
        settings["smtp_password"] = _dpapi_unprotect(encrypted)
        settings.pop("password_dpapi", None)
        settings.pop("password_dpapi_b64", None)
        return settings, path
    except Exception:
        return {}, path


def empty_state() -> Dict[str, object]:
    now = utc_now_text()
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": "geo_ring_cloud",
        "canonical_stage_id": "",
        "component_role": COMPONENT_ROLE,
        "related_stage_ids": RELATED_STAGE_IDS,
        "created_at": now,
        "updated_at": now,
        "snapshots": {},
        "outbox": [],
        "sent_count": 0,
        "failed_count": 0,
        "last_sent_at": "",
        "last_error": "",
        "monitor": {},
        "credentials_persisted": False,
        "automatic_delete": False,
    }


def read_state(path: Path) -> Dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return empty_state()
    if not isinstance(payload, dict):
        return empty_state()
    payload.setdefault("snapshots", {})
    payload.setdefault("outbox", [])
    payload.setdefault("sent_count", 0)
    payload.setdefault("failed_count", 0)
    payload.setdefault("last_sent_at", "")
    payload.setdefault("last_error", "")
    payload.setdefault("monitor", {})
    payload.setdefault("credentials_persisted", False)
    payload["automatic_delete"] = False
    return payload


class PersistentEmailNotifier:
    """Transition-driven email notification with a persistent retry outbox."""

    def __init__(self, state_path: Path):
        self.state_path = state_path
        self.host = ""
        self.port = 587
        self.username = ""
        self.password = ""
        self.sender = ""
        self.recipient = ""
        self.use_ssl = False
        self.use_starttls = True
        self.enabled = False
        self.credential_source = "not_configured"
        self.credentials_persisted = False
        self.config_path: Optional[Path] = None
        self.reload_settings()
        self.desktop_enabled = os.name == "nt" and bool(shutil.which("powershell.exe"))
        self._lock = threading.Lock()

    def reload_settings(self) -> None:
        """Refresh runtime settings so a live monitor can pick up DPAPI config."""
        if any(key in os.environ for key in EMAIL_ENV_KEYS):
            self.host = os.environ.get("GEO_RING_NOTIFY_SMTP_HOST", "").strip()
            self.port = int(os.environ.get("GEO_RING_NOTIFY_SMTP_PORT", "587") or 587)
            self.username = os.environ.get("GEO_RING_NOTIFY_SMTP_USER", "").strip()
            self.password = os.environ.get("GEO_RING_NOTIFY_SMTP_PASSWORD", "")
            self.sender = os.environ.get("GEO_RING_NOTIFY_EMAIL_FROM", "").strip()
            self.recipient = os.environ.get("GEO_RING_NOTIFY_EMAIL_TO", "").strip()
            self.use_ssl = os.environ.get("GEO_RING_NOTIFY_SMTP_SSL", "0").strip() == "1"
            self.use_starttls = (
                os.environ.get("GEO_RING_NOTIFY_SMTP_STARTTLS", "1").strip() != "0"
            )
            self.credential_source = "environment_variables"
            self.credentials_persisted = False
            self.config_path = None
        else:
            settings, path = _load_secure_email_config()
            self.host = str(settings.get("smtp_host", "")).strip()
            self.port = int(settings.get("smtp_port", 587) or 587)
            self.username = str(settings.get("smtp_user", "")).strip()
            self.password = str(settings.get("smtp_password", ""))
            self.sender = str(settings.get("email_from", "")).strip()
            self.recipient = str(settings.get("email_to", "")).strip()
            self.use_ssl = bool(settings.get("smtp_ssl", False))
            self.use_starttls = bool(settings.get("smtp_starttls", True))
            self.credential_source = "windows_dpapi" if settings else "not_configured"
            self.credentials_persisted = bool(settings)
            self.config_path = path
        self.enabled = bool(self.host and self.sender and self.recipient)

    @staticmethod
    def _snapshot(task: Dict[str, object]) -> Tuple[str, str, str]:
        return (
            str(task.get("download_status", "")).upper(),
            str(task.get("upload_status", "")).upper(),
            str(task.get("server_status", "")).upper(),
        )

    @staticmethod
    def _event_id(
        batch_name: str,
        stage: str,
        status: str,
        transition_at: str,
    ) -> str:
        semantic = "|".join((batch_name, stage, status, transition_at))
        return hashlib.sha256(semantic.encode("utf-8")).hexdigest()

    @staticmethod
    def _event_from_task(
        task: Dict[str, object],
        stage: str,
        stage_name: str,
        status: str,
    ) -> Dict[str, object]:
        transition_at = str(task.get("updated_at") or utc_now_text())
        batch_name = str(task.get("batch_name", ""))
        return {
            "event_id": PersistentEmailNotifier._event_id(
                batch_name, stage, status, transition_at
            ),
            "created_at": utc_now_text(),
            "transition_at": transition_at,
            "batch_name": batch_name,
            "stage": stage,
            "stage_name": stage_name,
            "status": status,
            "error": str(task.get("error", ""))[:2000],
            "download_phase": str(task.get("download_phase", "")),
            "upload_phase": str(task.get("upload_phase", "")),
            "upload_percent": float(task.get("upload_percent", 0) or 0),
            "upload_completed_files": int(task.get("upload_completed_files", 0) or 0),
            "upload_file_count": int(task.get("upload_file_count", 0) or 0),
            "attempts": 0,
            "next_attempt_at": utc_now_text(),
            "delivery_status": "PENDING",
            "last_error": "",
        }

    def observe(self, tasks: Iterable[Dict[str, object]]) -> int:
        """Persist new terminal transitions; initial observation is a quiet baseline."""
        with self._lock:
            state = read_state(self.state_path)
            snapshots = dict(state.get("snapshots", {}))
            outbox = list(state.get("outbox", []))
            known_ids = {str(event.get("event_id")) for event in outbox}
            created_events: List[Dict[str, object]] = []
            stages = (
                ("download", "下载", 0),
                ("upload", "上传", 1),
                ("server", "服务器复核", 2),
            )
            for task in tasks:
                batch_name = str(task.get("batch_name", ""))
                if not batch_name:
                    continue
                current = self._snapshot(task)
                previous_raw = snapshots.get(batch_name)
                previous = tuple(previous_raw) if isinstance(previous_raw, list) else None
                snapshots[batch_name] = list(current)
                if previous is None or previous == current:
                    continue
                for stage, stage_name, index in stages:
                    status = current[index]
                    if status == previous[index] or status not in TERMINAL_STATES[stage]:
                        continue
                    event = self._event_from_task(task, stage, stage_name, status)
                    if str(event["event_id"]) in known_ids:
                        continue
                    outbox.append(event)
                    known_ids.add(str(event["event_id"]))
                    created_events.append(event)
            state["snapshots"] = snapshots
            state["outbox"] = outbox[-500:]
            state["updated_at"] = utc_now_text()
            write_json_atomic(self.state_path, state)
        if self.desktop_enabled:
            for event in created_events:
                self._send_desktop(event)
        return len(created_events)

    @staticmethod
    def _send_desktop(event: Dict[str, object]) -> None:
        title = "GEO 任务：{}".format(event.get("batch_name", ""))
        body = "{} {}".format(event.get("stage_name", ""), event.get("status", ""))
        if event.get("error"):
            body += "；{}".format(str(event["error"])[:180])
        script = (
            "param([string]$Title,[string]$Body);"
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$n=New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon=[System.Drawing.SystemIcons]::Information;"
            "$n.BalloonTipTitle=$Title;$n.BalloonTipText=$Body;$n.Visible=$true;"
            "$n.ShowBalloonTip(8000);Start-Sleep -Seconds 9;$n.Dispose()"
        )
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-WindowStyle",
                "Hidden",
                "-Command",
                script,
                title,
                body,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _message(self, event: Dict[str, object], test: bool = False) -> EmailMessage:
        message = EmailMessage()
        if test:
            message["Subject"] = "[Geo Ring Cloud] 测试邮件：通知配置正常"
            body = (
                "这是一封 Geo Ring Cloud 测试邮件。\n\n"
                "如果你在手机上收到它，说明 SMTP 与手机邮件提醒已经配置成功。\n"
                "发送时间：{}\n"
                "说明：通知系统不会保存 SMTP 密码，也不会自动删除任何数据。"
            ).format(utc_now_text())
        else:
            message["Subject"] = "[Geo Ring Cloud] {} {} {}".format(
                event.get("batch_name", ""),
                event.get("stage_name", ""),
                event.get("status", ""),
            )
            body = (
                "GEO 数据任务状态发生变化\n\n"
                "批次：{batch}\n"
                "阶段：{stage}\n"
                "状态：{status}\n"
                "下载阶段：{download_phase}\n"
                "上传阶段：{upload_phase}\n"
                "上传进度：{upload_percent:.2f}%（{uploaded}/{total} 个文件）\n"
                "事件时间：{transition_at}\n"
                "通知时间：{sent_at}"
            ).format(
                batch=event.get("batch_name", ""),
                stage=event.get("stage_name", ""),
                status=event.get("status", ""),
                download_phase=event.get("download_phase", ""),
                upload_phase=event.get("upload_phase", ""),
                upload_percent=float(event.get("upload_percent", 0) or 0),
                uploaded=int(event.get("upload_completed_files", 0) or 0),
                total=int(event.get("upload_file_count", 0) or 0),
                transition_at=event.get("transition_at", ""),
                sent_at=utc_now_text(),
            )
            if event.get("error"):
                body += "\n\n详情：{}".format(event["error"])
        message["From"] = self.sender
        message["To"] = self.recipient
        message.set_content(body)
        return message

    def _send_message(self, message: EmailMessage) -> None:
        if not self.enabled:
            raise RuntimeError("邮件通知尚未配置完整的 SMTP 主机、发件地址和收件地址。")
        if self.use_ssl:
            client = smtplib.SMTP_SSL(self.host, self.port, timeout=20)
        else:
            client = smtplib.SMTP(self.host, self.port, timeout=20)
        with client:
            if not self.use_ssl and self.use_starttls:
                client.starttls()
            if self.username:
                client.login(self.username, self.password)
            client.send_message(message)

    def send_test(self) -> Dict[str, object]:
        self.reload_settings()
        self._send_message(self._message({}, test=True))
        return {
            "status": "PASS",
            "sent_at": utc_now_text(),
            "recipient": self.masked_recipient(),
        }

    def deliver_due(self, limit: int = 5) -> int:
        """Deliver due events and retain failures for bounded exponential retry."""
        self.reload_settings()
        if not self.enabled:
            return 0
        delivered = 0
        with self._lock:
            state = read_state(self.state_path)
            outbox = list(state.get("outbox", []))
            now = datetime.now(timezone.utc)
            for event in outbox:
                if delivered >= max(1, limit):
                    break
                if event.get("delivery_status") == "SENT":
                    continue
                next_attempt = parse_utc(event.get("next_attempt_at"))
                if next_attempt is not None and next_attempt > now:
                    continue
                attempts = int(event.get("attempts", 0) or 0)
                try:
                    self._send_message(self._message(event))
                    event["delivery_status"] = "SENT"
                    event["sent_at"] = utc_now_text()
                    event["last_error"] = ""
                    state["sent_count"] = int(state.get("sent_count", 0) or 0) + 1
                    state["last_sent_at"] = event["sent_at"]
                    state["last_error"] = ""
                    delivered += 1
                except Exception as exc:
                    attempts += 1
                    event["attempts"] = attempts
                    event["last_error"] = "{}: {}".format(type(exc).__name__, exc)
                    state["last_error"] = event["last_error"]
                    if attempts >= len(RETRY_DELAYS_SECONDS):
                        event["delivery_status"] = "FAILED"
                        state["failed_count"] = int(state.get("failed_count", 0) or 0) + 1
                    else:
                        event["delivery_status"] = "RETRY_WAIT"
                        event["next_attempt_at"] = (
                            now + timedelta(seconds=RETRY_DELAYS_SECONDS[attempts - 1])
                        ).isoformat().replace("+00:00", "Z")
            state["outbox"] = outbox[-500:]
            state["updated_at"] = utc_now_text()
            write_json_atomic(self.state_path, state)
        return delivered

    def update_monitor(self, **values: object) -> None:
        with self._lock:
            state = read_state(self.state_path)
            monitor = dict(state.get("monitor", {}))
            monitor.update(values)
            monitor["updated_at"] = utc_now_text()
            state["monitor"] = monitor
            state["updated_at"] = utc_now_text()
            write_json_atomic(self.state_path, state)

    def masked_recipient(self) -> str:
        if "@" not in self.recipient:
            return self.recipient
        local, domain = self.recipient.split("@", 1)
        return "{}***@{}".format(local[:2], domain)

    def public_status(self) -> Dict[str, object]:
        self.reload_settings()
        state = read_state(self.state_path)
        outbox = list(state.get("outbox", []))
        pending = sum(
            1
            for event in outbox
            if event.get("delivery_status") in {"PENDING", "RETRY_WAIT"}
        )
        failed = sum(1 for event in outbox if event.get("delivery_status") == "FAILED")
        return {
            "enabled": self.enabled,
            "desktop_enabled": self.desktop_enabled,
            "recipient": self.masked_recipient(),
            "host": self.host if self.enabled else "",
            "port": self.port if self.enabled else None,
            "transport": "implicit_tls" if self.use_ssl else "starttls" if self.use_starttls else "plain",
            "pending_events": pending,
            "failed_events": failed,
            "sent_count": int(state.get("sent_count", 0) or 0),
            "last_sent_at": str(state.get("last_sent_at", "")),
            "last_error": str(state.get("last_error", "")),
            "monitor": dict(state.get("monitor", {})),
            "state_path": str(self.state_path),
            "credential_source": self.credential_source,
            "credentials_persisted": self.credentials_persisted,
            "config_path": str(self.config_path) if self.config_path else "",
        }
