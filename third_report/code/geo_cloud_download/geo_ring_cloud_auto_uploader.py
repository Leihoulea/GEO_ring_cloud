"""Upload a verified GEO transfer batch to the lab server over SFTP.

The uploader is deliberately conservative: local data are never modified or
deleted, remote payloads are first written as ``.part`` files, interrupted
uploads are resumed, and completed remote files are never overwritten.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

CORE_CODE_ROOT = Path(__file__).resolve().parents[1] / "geo_ring_cloud_stage1"
if str(CORE_CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_CODE_ROOT))

from geo_ring_cloud.lineage import code_commit, generating_script_state  # noqa: E402
from geo_ring_cloud.paths import PROJECT_ROOT  # noqa: E402
from geo_ring_cloud_transfer_batch import (
    PLATFORM_REMOTE_RELATIVE,
    iter_batch_files,
    parse_day,
    sha256_file,
)


COMPONENT_ROLE = "automated_data_uploader"
RELATED_STAGE_IDS = ["stage_00"]
DEFAULT_SERVER_ROOT = PurePosixPath("/data04/1/dhr/geo_ring_cloud_auto_upload")
DEFAULT_ALLOWED_PARENT = PurePosixPath("/data04/1/dhr")
MAX_UPLOAD_WORKERS = 4
DEFAULT_SERVER_VERIFY_WORKERS = 2
REMOTE_PREFLIGHT_CHUNK_SIZE = 128
MAX_SERVER_VERIFY_WORKERS = 4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def current_process_created_epoch() -> Optional[float]:
    """Return a PID-reuse guard when psutil is available."""
    try:
        import psutil  # type: ignore

        return float(psutil.Process(os.getpid()).create_time())
    except (ImportError, OSError, ValueError):
        return None


def runtime_lineage() -> Dict[str, object]:
    script = Path(__file__).resolve()
    state = generating_script_state(script, PROJECT_ROOT)
    return {
        "generating_script": str(script),
        "code_commit": code_commit(PROJECT_ROOT),
        "code_commit_scope": "repository_head_at_run_start",
        "generating_script_state": state,
        "lineage_warnings": (
            []
            if state["commit_represents_script"]
            else ["code_commit does not fully represent the generating script content"]
        ),
    }


def subprocess_creation_flags() -> int:
    """Keep OpenSSH child processes invisible on Windows."""
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def subprocess_startupinfo():
    """Hide transient Windows console windows created by OpenSSH children.

    ``CREATE_NO_WINDOW`` is necessary but is not sufficient on every Windows
    OpenSSH build: a short-lived ``conhost.exe`` can still be created while an
    ``ssh.exe`` or ``sftp.exe`` child starts.  Explicitly requesting
    ``SW_HIDE`` prevents that console from being shown to the desktop user.
    """
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return startupinfo


def write_json_atomic(
    path: Path,
    payload: Dict[str, object],
    max_attempts: int = 12,
    retry_seconds: float = 0.25,
) -> None:
    """Persist a small control record without letting transient Windows locks win.

    The upload ledger and raw data are independent of this status record.  A
    temporary antivirus/indexer lock therefore must not terminate a running
    transfer merely because the dashboard cannot be refreshed momentarily.
    """
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


def short_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:12]


def is_within_posix(path: PurePosixPath, parent: PurePosixPath) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_server_root(server_root: PurePosixPath, allowed_parent: PurePosixPath) -> None:
    if not server_root.is_absolute() or not allowed_parent.is_absolute():
        raise ValueError("Server paths must be absolute POSIX paths")
    if server_root == allowed_parent or not is_within_posix(server_root, allowed_parent):
        raise ValueError(
            "Automatic upload root must be a child of {}".format(allowed_parent)
        )


def build_auto_upload_manifest(
    source_manifest_path: Path,
    server_root: PurePosixPath,
    output_path: Optional[Path] = None,
    progress_callback: Optional[Callable[[Dict[str, object]], None]] = None,
) -> Tuple[Path, Dict[str, object]]:
    source_manifest_path = source_manifest_path.resolve()
    source = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    if source.get("status") != "READY_FOR_XFTP_UPLOAD":
        raise RuntimeError("Transfer manifest is not READY_FOR_XFTP_UPLOAD")
    source_server_root = PurePosixPath(str(source.get("server_root", "")))
    if not source_server_root.is_absolute():
        raise RuntimeError("Transfer manifest has no absolute server_root")

    source_files = list(source.get("files", []))
    if not source_files:
        raise RuntimeError("Transfer manifest contains no files")
    total_source_bytes = sum(int(item.get("size_bytes", 0) or 0) for item in source_files)
    batch_id = str(source.get("batch_id") or source_manifest_path.stem)
    target = output_path or source_manifest_path.parent / (
        "geo_ring_cloud_auto_upload_{}_manifest.json".format(batch_id)
    )

    # A completed automatic manifest already contains immutable per-file
    # SHA-256 values.  Rebuilding it on every resume used to read every FY4B
    # file again before SFTP could start.  Reuse it only after a cheap local
    # identity check (path, size, mapped remote path, and checksum shape).
    # Any mismatch falls through to a full rehash, preserving integrity.
    existing = read_json_file(target) if target.is_file() else {}
    existing_files = list(existing.get("files", [])) if isinstance(existing, dict) else []
    source_by_path = {str(item.get("local_path", "")): item for item in source_files}
    reusable = (
        isinstance(existing, dict)
        and existing.get("status") == "READY_FOR_AUTOMATED_SFTP_UPLOAD"
        and str(existing.get("source_transfer_manifest", "")) == str(source_manifest_path)
        and str(existing.get("source_server_root", "")) == str(source_server_root)
        and str(existing.get("server_root", "")) == str(server_root)
        and len(existing_files) == len(source_files)
        and len(source_by_path) == len(source_files)
    )
    if reusable:
        for cached in existing_files:
            local_key = str(cached.get("local_path", ""))
            source_item = source_by_path.get(local_key)
            digest = str(cached.get("sha256", "")).strip().lower()
            if source_item is None or not re.fullmatch(r"[0-9a-f]{64}", digest):
                reusable = False
                break
            try:
                expected_size = int(source_item.get("size_bytes", -1))
                if int(cached.get("size_bytes", -2)) != expected_size:
                    reusable = False
                    break
                local_path = Path(local_key)
                if not local_path.is_file() or local_path.stat().st_size != expected_size:
                    reusable = False
                    break
                old_remote = PurePosixPath(str(source_item.get("remote_path", "")))
                expected_remote = str(server_root / old_remote.relative_to(source_server_root))
                if str(cached.get("remote_path", "")) != expected_remote:
                    reusable = False
                    break
            except (OSError, TypeError, ValueError):
                reusable = False
                break
    if reusable:
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "preparing_manifest",
                    "preflight_completed_files": len(existing_files),
                    "preflight_file_count": len(source_files),
                    "preflight_completed_size_bytes": total_source_bytes,
                    "preflight_total_size_bytes": total_source_bytes,
                    "preflight_percent": 100.0,
                    "current_file": "",
                    "current_files": [],
                    "preflight_reused_existing_sha256": True,
                }
            )
        return target, existing

    remapped_files: List[Dict[str, object]] = []
    prepared_files = 0
    prepared_bytes = 0
    for item in source_files:
        local_path = Path(str(item.get("local_path", "")))
        if not local_path.is_file():
            raise FileNotFoundError("Local source file is missing: {}".format(local_path))
        expected_size = int(item.get("size_bytes", -1))
        before = local_path.stat()
        if before.st_size != expected_size:
            raise RuntimeError("Local source size changed: {}".format(local_path))
        old_remote = PurePosixPath(str(item.get("remote_path", "")))
        try:
            relative = old_remote.relative_to(source_server_root)
        except ValueError as exc:
            raise RuntimeError(
                "Remote path is outside manifest server_root: {}".format(old_remote)
            ) from exc
        new_item = dict(item)
        # Official-client imports first write a fast path/size manifest.  Compute
        # their checksum inside this detached uploader process so the dashboard
        # remains responsive, but reject a source that changes while hashing.
        if not str(new_item.get("sha256", "")).strip():
            new_item["sha256"] = sha256_file(local_path)
        after = local_path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError("Local source changed while hashing: {}".format(local_path))
        new_item["remote_path"] = str(server_root / relative)
        remapped_files.append(new_item)
        prepared_files += 1
        prepared_bytes += expected_size
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "preparing_manifest",
                    "preflight_completed_files": prepared_files,
                    "preflight_file_count": len(source_files),
                    "preflight_completed_size_bytes": prepared_bytes,
                    "preflight_total_size_bytes": total_source_bytes,
                    "preflight_percent": (
                        round(prepared_bytes / total_source_bytes * 100, 2)
                        if total_source_bytes
                        else 100.0
                    ),
                    "current_file": str(local_path),
                    "current_files": [str(local_path)],
                }
            )

    if not remapped_files:
        raise RuntimeError("Transfer manifest contains no files")

    payload = dict(source)
    payload.update(
        {
            "component_role": COMPONENT_ROLE,
            "related_stage_ids": RELATED_STAGE_IDS,
            "created_at": utc_now(),
            "source_transfer_manifest": str(source_manifest_path),
            "source_server_root": str(source_server_root),
            "server_root": str(server_root),
            "status": "READY_FOR_AUTOMATED_SFTP_UPLOAD",
            "files": remapped_files,
            "file_count": len(remapped_files),
            "total_size_bytes": sum(int(item["size_bytes"]) for item in remapped_files),
            "deletion_policy": {
                "automatic_delete": False,
                "delete_allowed_only_after": (
                    "server SHA-256 verification PASS and explicit user confirmation"
                ),
            },
            **runtime_lineage(),
        }
    )
    write_json_atomic(target, payload)
    return target, payload


def ssh_base(target: str, identity_file: Path, connect_timeout: int) -> List[str]:
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "ConnectTimeout={}".format(connect_timeout),
        "-i",
        str(identity_file),
        target,
    ]


def sftp_base(target: str, identity_file: Path, connect_timeout: int) -> List[str]:
    return [
        "sftp",
        "-q",
        "-b",
        "-",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "ConnectTimeout={}".format(connect_timeout),
        "-i",
        str(identity_file),
        target,
    ]


def run_ssh(
    target: str,
    identity_file: Path,
    command: str,
    connect_timeout: int,
    input_text: Optional[str] = None,
    check: bool = True,
    command_timeout: Optional[int] = None,
) -> subprocess.CompletedProcess:
    run_kwargs = {
        "text": True,
        "capture_output": True,
        "check": False,
        "creationflags": subprocess_creation_flags(),
        "startupinfo": subprocess_startupinfo(),
    }
    if input_text is None:
        # A Scheduled Task running with S4U has no interactive console.  Do not
        # let OpenSSH inherit an unusable Session-0 stdin handle and wait on it.
        run_kwargs["stdin"] = subprocess.DEVNULL
    else:
        run_kwargs["input"] = input_text
    if command_timeout is not None:
        run_kwargs["timeout"] = command_timeout
    try:
        result = subprocess.run(
            ssh_base(target, identity_file, connect_timeout) + [command],
            **run_kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "SSH command timed out after {} seconds: {}".format(
                command_timeout, command
            )
        ) from exc
    if check and result.returncode != 0:
        message = (result.stderr or result.stdout or "SSH command failed").strip()
        raise RuntimeError(message)
    return result


def sftp_quote(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("SFTP paths must not contain newlines")
    return '"{}"'.format(value.replace("\\", "\\\\").replace('"', '\\"'))


def run_sftp_batch(
    target: str,
    identity_file: Path,
    commands: Iterable[str],
    connect_timeout: int,
) -> None:
    batch = "\n".join(commands) + "\n"
    result = subprocess.run(
        sftp_base(target, identity_file, connect_timeout),
        input=batch,
        text=True,
        capture_output=True,
        check=False,
        creationflags=subprocess_creation_flags(),
        startupinfo=subprocess_startupinfo(),
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "SFTP command failed").strip()
        raise RuntimeError(message)


REMOTE_INSPECT_SCRIPT = r"""
import json, os, sys
p = json.load(sys.stdin)
root = os.path.realpath(p['root'])
allowed = os.path.realpath(p['allowed_parent'])
if root == allowed or not root.startswith(allowed.rstrip('/') + '/'):
    raise SystemExit('unsafe automatic upload root')
os.makedirs(root, mode=0o750, exist_ok=True)
for directory in p.get('directories', []):
    candidate = os.path.realpath(directory)
    if candidate != root and not candidate.startswith(root.rstrip('/') + '/'):
        raise SystemExit('unsafe directory: ' + directory)
    os.makedirs(candidate, mode=0o750, exist_ok=True)
result = {}
for path in p.get('paths', []):
    candidate = os.path.realpath(path)
    if not candidate.startswith(root.rstrip('/') + '/'):
        raise SystemExit('unsafe payload path: ' + path)
    result[path] = {
        'final_size': os.path.getsize(path) if os.path.isfile(path) else None,
        'part_size': os.path.getsize(path + '.part') if os.path.isfile(path + '.part') else None,
    }
json.dump(result, sys.stdout)
""".strip()


def inspect_remote(
    target: str,
    identity_file: Path,
    server_root: PurePosixPath,
    allowed_parent: PurePosixPath,
    paths: Sequence[str],
    directories: Sequence[str],
    connect_timeout: int,
) -> Dict[str, Dict[str, Optional[int]]]:
    command = "python3 -c {}".format(shlex.quote(REMOTE_INSPECT_SCRIPT))
    payload = json.dumps(
        {
            "root": str(server_root),
            "allowed_parent": str(allowed_parent),
            "paths": list(paths),
            "directories": list(directories),
        }
    )
    result = run_ssh(
        target,
        identity_file,
        command,
        connect_timeout,
        input_text=payload,
    )
    return json.loads(result.stdout or "{}")


def upload_one(
    target: str,
    identity_file: Path,
    local_path: Path,
    remote_path: str,
    connect_timeout: int,
    resume: bool = False,
) -> None:
    local_sftp = local_path.resolve().as_posix()
    part_path = remote_path + ".part"
    transfer_command = "reput" if resume else "put"
    run_sftp_batch(
        target,
        identity_file,
        [
            "{} -p {} {}".format(
                transfer_command, sftp_quote(local_sftp), sftp_quote(part_path)
            ),
            "rename {} {}".format(sftp_quote(part_path), sftp_quote(remote_path)),
        ],
        connect_timeout,
    )


def progressive_upload_worker_counts(total_files: int, max_workers: int) -> List[int]:
    """Plan conservative upload waves: 1 while downloading, then 2 -> 3 -> 4."""
    if total_files < 0:
        raise ValueError("total_files must be non-negative")
    if not 1 <= max_workers <= MAX_UPLOAD_WORKERS:
        raise ValueError("max_workers must be between 1 and {}".format(MAX_UPLOAD_WORKERS))
    remaining = total_files
    workers = 1 if max_workers == 1 else 2
    waves: List[int] = []
    while remaining:
        wave_size = min(workers, remaining)
        waves.append(wave_size)
        remaining -= wave_size
        workers = min(workers + 1, max_workers)
    return waves


def chunked_items(items: Sequence[str], chunk_size: int) -> Iterable[Sequence[str]]:
    """Yield bounded remote-preflight requests without materializing copies."""
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    for offset in range(0, len(items), chunk_size):
        yield items[offset : offset + chunk_size]


def upload_manifest_item(
    item: Dict[str, object],
    remote_state: Dict[str, Optional[int]],
    target: str,
    identity_file: Path,
    connect_timeout: int,
) -> Dict[str, object]:
    """Upload one immutable manifest item and return its confirmed accounting row."""
    local_path = Path(str(item["local_path"]))
    remote_path = str(item["remote_path"])
    expected_size = int(item["size_bytes"])
    before = local_path.stat()
    if before.st_size != expected_size:
        raise RuntimeError("Local source size changed: {}".format(local_path))
    final_size = remote_state.get("final_size")
    part_size = remote_state.get("part_size")
    if final_size is not None:
        if int(final_size) != expected_size:
            raise RuntimeError(
                "Remote final file exists with unexpected size; refusing overwrite: {}".format(
                    remote_path
                )
            )
    else:
        if part_size is not None and int(part_size) > expected_size:
            raise RuntimeError(
                "Remote .part file is larger than source; refusing overwrite: {}.part".format(
                    remote_path
                )
            )
        upload_one(
            target,
            identity_file,
            local_path,
            remote_path,
            connect_timeout,
            resume=part_size is not None,
        )
    after = local_path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError("Local source changed during upload: {}".format(local_path))
    return {
        "local_path": str(local_path),
        "remote_path": remote_path,
        "size_bytes": expected_size,
        "remote_preexisting": final_size is not None,
    }


def download_one(
    target: str,
    identity_file: Path,
    remote_path: str,
    local_path: Path,
    connect_timeout: int,
) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = local_path.with_suffix(local_path.suffix + ".part")
    run_sftp_batch(
        target,
        identity_file,
        ["get -p {} {}".format(sftp_quote(remote_path), sftp_quote(temporary.resolve().as_posix()))],
        connect_timeout,
    )
    os.replace(temporary, local_path)


def ensure_tools_and_identity(identity_file: Path) -> None:
    if shutil.which("ssh") is None or shutil.which("sftp") is None:
        raise RuntimeError("Windows OpenSSH ssh/sftp commands are unavailable")
    if not identity_file.is_file():
        raise FileNotFoundError("SSH identity file is missing: {}".format(identity_file))


def read_json_file(path: Path) -> Dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_stream_ledger(path: Path) -> Dict[str, Dict[str, object]]:
    completed: Dict[str, Dict[str, object]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return completed
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("local_path"):
            completed[str(row["local_path"])] = row
    return completed


def ledger_progress_for_files(
    files: Sequence[Dict[str, object]], ledger: Dict[str, Dict[str, object]]
) -> Tuple[int, int]:
    """Return conservatively matched continuous-upload progress.

    A continuous ledger entry is only a progress observation until the remote
    preflight checks it again.  Matching both local path and expected size
    keeps that observation useful without allowing an old or changed file to
    inflate the displayed count.
    """
    completed_files = 0
    completed_bytes = 0
    for item in files:
        local_path = str(item.get("local_path", ""))
        expected_size = int(item.get("size_bytes", 0) or 0)
        entry = ledger.get(local_path, {})
        try:
            recorded_size = int(entry.get("size_bytes", -1) or -1)
        except (TypeError, ValueError):
            recorded_size = -1
        if local_path and recorded_size == expected_size:
            completed_files += 1
            completed_bytes += expected_size
    return completed_files, completed_bytes


def append_stream_ledger(path: Path, row: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        handle.flush()


def expected_inventory_files(
    batch_root: Path,
    start_date: str,
    end_date: str,
    platforms: Sequence[str],
) -> int:
    inventory = batch_root / "manifests" / "manifest_inventory.csv"
    if not inventory.is_file():
        return 0
    start_prefix = start_date + "T"
    end_day = datetime.strptime(end_date, "%Y-%m-%d").date()
    selected = set(platforms)
    paths = set()
    with inventory.open("r", newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if row.get("status") != "found" or row.get("platform") not in selected:
                continue
            target_time = str(row.get("target_time_utc", ""))
            if target_time < start_prefix:
                continue
            try:
                target_day = datetime.fromisoformat(target_time.replace("Z", "+00:00")).date()
            except ValueError:
                continue
            if target_day > end_day:
                continue
            local_path = str(row.get("local_path", "")).strip()
            if local_path:
                paths.add(local_path)
    return len(paths)


def discover_completed_files(
    batch_root: Path,
    start_date: str,
    end_date: str,
    platforms: Sequence[str],
) -> List[Tuple[str, Path, Path]]:
    return list(
        iter_batch_files(
            batch_root.resolve(),
            parse_day(start_date),
            parse_day(end_date),
            set(platforms),
        )
    )


def latest_transfer_manifest(transfer_dir: Path) -> Optional[Path]:
    candidates = sorted(
        transfer_dir.glob("geo_ring_cloud_transfer_*_manifest.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        if read_json_file(path).get("status") == "READY_FOR_XFTP_UPLOAD":
            return path
    return None


def watch_and_upload(
    batch_root: Path,
    start_date: str,
    end_date: str,
    platforms: Sequence[str],
    target: str,
    identity_file: Path,
    server_root: PurePosixPath,
    allowed_parent: PurePosixPath,
    status_path: Path,
    verification_report: Path,
    poll_seconds: int = 10,
    connect_timeout: int = 20,
    max_upload_workers: int = MAX_UPLOAD_WORKERS,
    server_verify_workers: int = DEFAULT_SERVER_VERIFY_WORKERS,
) -> int:
    """Upload finalized files while the downloader continues, then reconcile fully."""
    batch_root = batch_root.resolve()
    transfer_dir = batch_root / "transfer"
    ledger_path = transfer_dir / "continuous_upload_ledger.jsonl"
    validate_server_root(server_root, allowed_parent)
    ensure_tools_and_identity(identity_file)
    if not 1 <= max_upload_workers <= MAX_UPLOAD_WORKERS:
        raise ValueError(
            "max_upload_workers must be between 1 and {}".format(MAX_UPLOAD_WORKERS)
        )
    completed = load_stream_ledger(ledger_path)
    completed_bytes = sum(int(row.get("size_bytes", 0)) for row in completed.values())
    expected_files = expected_inventory_files(batch_root, start_date, end_date, platforms)
    stable_signatures: Dict[str, Tuple[int, int]] = {}
    retry_count = 0
    started_at = utc_now()

    status: Dict[str, object] = {
        "project_id": "geo_ring_cloud",
        "canonical_stage_id": "",
        "component_role": "continuous_data_uploader",
        "related_stage_ids": RELATED_STAGE_IDS,
        "status": "RUNNING",
        "phase": "watching_download",
        "mode": "continuous_download_upload",
        "started_at": started_at,
        "updated_at": started_at,
        "pid": os.getpid(),
        "process_created_epoch": current_process_created_epoch(),
        "batch_root": str(batch_root),
        "target": target,
        "server_root": str(server_root),
        "file_count": expected_files,
        "completed_files": len(completed),
        "completed_size_bytes": completed_bytes,
        "percent": round(len(completed) / expected_files * 100, 2) if expected_files else 0.0,
        "current_file": "",
        "retry_count": 0,
        "parallelism_mode": "adaptive",
        "active_workers": 1,
        "max_workers": max_upload_workers,
        "parallelism_reason": "download_active_single_stream",
        "automatic_delete": False,
        **runtime_lineage(),
    }

    def update(**values: object) -> bool:
        status.update(values)
        status["updated_at"] = utc_now()
        try:
            write_json_atomic(status_path, status)
            return True
        except OSError as exc:
            # Status must never be a single point of failure for data transfer.
            # A future successful update persists this diagnostic as well.
            status["status_write_failures"] = int(status.get("status_write_failures", 0)) + 1
            status["last_status_write_error"] = "{}: {}".format(type(exc).__name__, exc)
            print(
                "status_write_retry_deferred {}: {}".format(type(exc).__name__, exc),
                file=sys.stderr,
                flush=True,
            )
            return False

    update()
    while True:
        final_manifest = latest_transfer_manifest(transfer_dir)
        if final_manifest is not None:
            update(
                phase="final_reconcile_and_verify",
                manifest=str(final_manifest),
                current_file="",
            )
            return upload_batch(
                final_manifest,
                target,
                identity_file,
                server_root,
                allowed_parent,
                status_path,
                verification_report,
                connect_timeout,
                max_upload_workers,
                server_verify_workers,
            )

        raw_batch = read_json_file(transfer_dir / "batch_status.json")
        launcher = read_json_file(transfer_dir / "download_launcher_status.json")
        launcher_active = str(launcher.get("status", "")).upper() in {"STARTING", "RUNNING"}
        if raw_batch.get("status") == "failed" and not launcher_active:
            update(
                status="FAIL",
                phase="download_failed",
                failed_at=utc_now(),
                error=str(raw_batch.get("message") or "下载失败，持续上传已停止。"),
                current_file="",
            )
            return 2

        discovered = discover_completed_files(batch_root, start_date, end_date, platforms)
        current_paths = {str(path) for _, path, _ in discovered}
        for missing in set(stable_signatures) - current_paths:
            stable_signatures.pop(missing, None)

        ready: List[Tuple[str, Path, Path, int]] = []
        for platform, local_path, relative in discovered:
            key = str(local_path)
            if key in completed:
                continue
            stat = local_path.stat()
            signature = (stat.st_size, stat.st_mtime_ns)
            if stable_signatures.get(key) == signature:
                ready.append((platform, local_path, relative, stat.st_size))
            else:
                stable_signatures[key] = signature

        if expected_files == 0:
            expected_files = expected_inventory_files(batch_root, start_date, end_date, platforms)

        if ready:
            batch = ready[:8]
            remote_paths = [
                str(server_root / PLATFORM_REMOTE_RELATIVE[platform] / PurePosixPath(relative.as_posix()))
                for platform, _, relative, _ in batch
            ]
            directories = sorted({str(PurePosixPath(path).parent) for path in remote_paths})
            try:
                remote = inspect_remote(
                    target,
                    identity_file,
                    server_root,
                    allowed_parent,
                    remote_paths,
                    directories,
                    connect_timeout,
                )
                for (platform, local_path, relative, expected_size), remote_path in zip(batch, remote_paths):
                    update(
                        phase="streaming_upload",
                        current_file=str(local_path),
                        discovered_files=len(discovered),
                        active_workers=1,
                        parallelism_reason="download_active_single_stream",
                    )
                    before = local_path.stat()
                    digest = sha256_file(local_path)
                    after = local_path.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        stable_signatures.pop(str(local_path), None)
                        continue
                    remote_state = remote.get(remote_path, {})
                    final_size = remote_state.get("final_size")
                    part_size = remote_state.get("part_size")
                    if final_size is not None and int(final_size) != expected_size:
                        raise RuntimeError(
                            "Remote final file exists with unexpected size; refusing overwrite: {}".format(
                                remote_path
                            )
                        )
                    if final_size is None:
                        if part_size is not None and int(part_size) > expected_size:
                            raise RuntimeError(
                                "Remote .part file is larger than source; refusing overwrite: {}.part".format(
                                    remote_path
                                )
                            )
                        upload_one(
                            target,
                            identity_file,
                            local_path,
                            remote_path,
                            connect_timeout,
                            resume=part_size is not None,
                        )
                    row = {
                        "uploaded_at": utc_now(),
                        "platform": platform,
                        "local_path": str(local_path),
                        "remote_path": remote_path,
                        "size_bytes": expected_size,
                        "local_signature": {
                            "size_bytes": int(after.st_size),
                            "mtime_ns": int(after.st_mtime_ns),
                        },
                        "sha256": digest,
                        "remote_preexisting": final_size is not None,
                    }
                    append_stream_ledger(ledger_path, row)
                    completed[str(local_path)] = row
                    completed_bytes += expected_size
                    retry_count = 0
                    update(
                        completed_files=len(completed),
                        completed_size_bytes=completed_bytes,
                        file_count=expected_files,
                        percent=(
                            round(len(completed) / expected_files * 100, 2)
                            if expected_files
                            else 0.0
                        ),
                        retry_count=retry_count,
                    )
            except Exception as exc:
                retry_count += 1
                update(
                    phase="retry_wait",
                    retry_count=retry_count,
                    last_error="{}: {}".format(type(exc).__name__, exc),
                    current_file="",
                )
                time.sleep(min(300, poll_seconds * (2 ** min(retry_count, 5))))
                continue
        else:
            update(
                phase="watching_download",
                discovered_files=len(discovered),
                completed_files=len(completed),
                file_count=expected_files,
                percent=(
                    round(len(completed) / expected_files * 100, 2)
                    if expected_files
                    else 0.0
                ),
                current_file="",
                active_workers=0,
                parallelism_reason="waiting_for_finalized_files",
            )
        time.sleep(max(2, poll_seconds))


def upload_batch(
    manifest_path: Path,
    target: str,
    identity_file: Path,
    server_root: PurePosixPath,
    allowed_parent: PurePosixPath,
    status_path: Path,
    verification_report: Path,
    connect_timeout: int = 20,
    max_upload_workers: int = MAX_UPLOAD_WORKERS,
    server_verify_workers: int = DEFAULT_SERVER_VERIFY_WORKERS,
) -> int:
    validate_server_root(server_root, allowed_parent)
    ensure_tools_and_identity(identity_file)
    if not 1 <= max_upload_workers <= MAX_UPLOAD_WORKERS:
        raise ValueError(
            "max_upload_workers must be between 1 and {}".format(MAX_UPLOAD_WORKERS)
        )
    if not 1 <= server_verify_workers <= MAX_SERVER_VERIFY_WORKERS:
        raise ValueError(
            "server_verify_workers must be between 1 and {}".format(MAX_SERVER_VERIFY_WORKERS)
        )
    source = json.loads(manifest_path.resolve().read_text(encoding="utf-8"))
    source_files = list(source.get("files", []))
    source_total_bytes = sum(int(item.get("size_bytes", 0) or 0) for item in source_files)
    continuous_ledger = load_stream_ledger(status_path.parent / "continuous_upload_ledger.jsonl")
    ledger_completed_files, ledger_completed_bytes = ledger_progress_for_files(
        source_files, continuous_ledger
    )
    # A restarted uploader always performs remote preflight again before it
    # skips any payload.  Preserve a bounded, previously confirmed remote
    # observation merely as a UI baseline so an interrupted preflight does not
    # appear to erase hundreds of completed uploads.
    previous_status = read_json_file(status_path)
    prior_completed_files = max(
        0,
        min(len(source_files), int(previous_status.get("completed_files", 0) or 0)),
    )
    prior_completed_bytes = max(
        0,
        min(source_total_bytes, int(previous_status.get("completed_size_bytes", 0) or 0)),
    )
    prior_is_remote_observation = str(previous_status.get("progress_source", "")) in {
        "remote_preflight",
        "previous_remote_preflight_pending_recheck",
    }
    if prior_is_remote_observation:
        ledger_completed_files = max(ledger_completed_files, prior_completed_files)
        ledger_completed_bytes = max(ledger_completed_bytes, prior_completed_bytes)
    ledger_percent = (
        round(ledger_completed_bytes / source_total_bytes * 100, 2)
        if source_total_bytes
        else 100.0
    )
    preflight_status: Dict[str, object] = {
        "project_id": "geo_ring_cloud",
        "canonical_stage_id": "",
        "component_role": COMPONENT_ROLE,
        "related_stage_ids": RELATED_STAGE_IDS,
        "batch_id": str(source.get("batch_id") or "batch"),
        "target": target,
        "server_root": str(server_root),
        "manifest": str(manifest_path),
        "status": "RUNNING",
        "phase": "preparing_manifest",
        "mode": "adaptive_upload",
        "started_at": utc_now(),
        "updated_at": utc_now(),
        "pid": os.getpid(),
        "process_created_epoch": current_process_created_epoch(),
        "file_count": len(source_files),
        "completed_files": ledger_completed_files,
        "total_size_bytes": source_total_bytes,
        "completed_size_bytes": ledger_completed_bytes,
        "percent": ledger_percent,
        "progress_source": (
            "previous_remote_preflight_pending_recheck"
            if prior_is_remote_observation
            else "continuous_upload_ledger_pending_remote_preflight"
        ),
        "preflight_file_count": len(source_files),
        "preflight_completed_files": 0,
        "preflight_total_size_bytes": source_total_bytes,
        "preflight_completed_size_bytes": 0,
        "preflight_percent": 0.0,
        "current_file": "",
        "current_files": [],
        "parallelism_mode": "adaptive",
        "active_workers": 0,
        "max_workers": max_upload_workers,
        "parallelism_reason": "preserving_continuous_upload_progress_while_preparing_manifest",
        "automatic_delete": False,
    }
    write_json_atomic(status_path, preflight_status)
    last_preflight_write = 0.0

    def report_manifest_progress(progress: Dict[str, object]) -> None:
        nonlocal last_preflight_write
        now = time.monotonic()
        completed = int(progress.get("preflight_completed_files", 0) or 0)
        total = int(progress.get("preflight_file_count", 0) or 0)
        if completed < total and now - last_preflight_write < 0.5:
            return
        preflight_status.update(progress)
        preflight_status["updated_at"] = utc_now()
        write_json_atomic(status_path, preflight_status)
        last_preflight_write = now

    try:
        auto_manifest_path, manifest = build_auto_upload_manifest(
            manifest_path,
            server_root,
            progress_callback=report_manifest_progress,
        )
    except Exception as exc:
        preflight_status.update(
            {
                "status": "FAIL",
                "phase": "failed",
                "failed_at": utc_now(),
                "updated_at": utc_now(),
                "error": "{}: {}".format(type(exc).__name__, exc),
            }
        )
        write_json_atomic(status_path, preflight_status)
        print(preflight_status["error"], file=sys.stderr)
        return 2
    files = list(manifest["files"])
    total_bytes = int(manifest["total_size_bytes"])
    batch_id = str(manifest.get("batch_id") or "batch")
    control_root = server_root / "_control" / batch_id
    verifier_local = Path(__file__).with_name("geo_ring_cloud_transfer_batch.py").resolve()
    remote_manifest = control_root / "{}_{}.json".format(
        auto_manifest_path.stem, short_sha256(auto_manifest_path)
    )
    remote_verifier = control_root / "{}_{}.py".format(
        verifier_local.stem, short_sha256(verifier_local)
    )
    remote_report = control_root / "server_verification.json"
    remote_progress = control_root / "server_verification_progress.json"
    local_progress = status_path.parent / "server_verification_progress.json"

    base_status: Dict[str, object] = {
        "project_id": "geo_ring_cloud",
        "canonical_stage_id": "",
        "component_role": COMPONENT_ROLE,
        "related_stage_ids": RELATED_STAGE_IDS,
        "batch_id": batch_id,
        "target": target,
        "server_root": str(server_root),
        "manifest": str(auto_manifest_path),
        "status": "RUNNING",
        "phase": "preflight",
        "mode": "adaptive_upload",
        "started_at": utc_now(),
        "updated_at": utc_now(),
        "pid": os.getpid(),
        "process_created_epoch": current_process_created_epoch(),
        "file_count": len(files),
        "completed_files": ledger_completed_files,
        "total_size_bytes": total_bytes,
        "completed_size_bytes": ledger_completed_bytes,
        "percent": ledger_percent,
        "progress_source": "continuous_upload_ledger_pending_remote_preflight",
        "current_file": "",
        "current_files": [],
        "parallelism_mode": "adaptive",
        "active_workers": 0,
        "max_workers": max_upload_workers,
        "parallelism_reason": "preflight",
        "automatic_delete": False,
    }

    def update(**values: object) -> bool:
        base_status.update(values)
        base_status["updated_at"] = utc_now()
        try:
            write_json_atomic(status_path, base_status)
            return True
        except OSError as exc:
            base_status["status_write_failures"] = int(
                base_status.get("status_write_failures", 0)
            ) + 1
            base_status["last_status_write_error"] = "{}: {}".format(type(exc).__name__, exc)
            print(
                "status_write_retry_deferred {}: {}".format(type(exc).__name__, exc),
                file=sys.stderr,
                flush=True,
            )
            return False

    update()
    try:
        update(
            phase="connectivity_check",
            current_file="",
            current_files=[],
            active_workers=1,
            parallelism_reason="checking_ssh_connection",
        )
        run_ssh(
            target,
            identity_file,
            "true",
            connect_timeout,
            command_timeout=max(30, connect_timeout + 10),
        )
        remote_paths = [str(item["remote_path"]) for item in files]
        update(
            phase="remote_preflight",
            active_workers=1,
            preflight_file_count=len(remote_paths),
            preflight_completed_files=0,
            preflight_percent=0.0,
            parallelism_reason="checking_remote_completed_files_in_bounded_batches",
        )
        remote: Dict[str, Dict[str, Optional[int]]] = {}
        completed_preflight = 0
        for remote_chunk in chunked_items(remote_paths, REMOTE_PREFLIGHT_CHUNK_SIZE):
            directories = sorted(
                {str(PurePosixPath(path).parent) for path in remote_chunk}
                | {str(control_root)}
            )
            remote.update(
                inspect_remote(
                    target,
                    identity_file,
                    server_root,
                    allowed_parent,
                    remote_chunk,
                    directories,
                    connect_timeout,
                )
            )
            completed_preflight += len(remote_chunk)
            update(
                phase="remote_preflight",
                preflight_completed_files=completed_preflight,
                preflight_file_count=len(remote_paths),
                preflight_percent=round(completed_preflight / len(remote_paths) * 100, 2),
                current_file="",
                current_files=[],
            )
        completed_files = 0
        completed_bytes = 0
        pending_items: List[Dict[str, object]] = []
        for item in files:
            remote_path = str(item["remote_path"])
            expected_size = int(item["size_bytes"])
            remote_state = remote.get(remote_path, {})
            final_size = remote_state.get("final_size")
            if final_size is not None:
                if int(final_size) != expected_size:
                    raise RuntimeError(
                        "Remote final file exists with unexpected size; refusing overwrite: {}".format(
                            remote_path
                        )
                    )
                completed_files += 1
                completed_bytes += expected_size
            else:
                pending_items.append(item)

        update(
            phase="upload_ramp",
            completed_files=completed_files,
            completed_size_bytes=completed_bytes,
            percent=round(completed_bytes / total_bytes * 100, 2) if total_bytes else 100.0,
            progress_source="remote_preflight",
            parallelism_reason="download_complete_ramping",
        )
        cursor = 0
        for worker_count in progressive_upload_worker_counts(
            len(pending_items), max_upload_workers
        ):
            wave = pending_items[cursor : cursor + worker_count]
            cursor += len(wave)
            current_files = [str(item["local_path"]) for item in wave]
            update(
                phase="uploading_parallel",
                active_workers=worker_count,
                current_files=current_files,
                current_file=current_files[0] if current_files else "",
                parallelism_reason="download_complete_{}_streams".format(worker_count),
            )
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = {
                    executor.submit(
                        upload_manifest_item,
                        item,
                        remote.get(str(item["remote_path"]), {}),
                        target,
                        identity_file,
                        connect_timeout,
                    ): item
                    for item in wave
                }
                for future in as_completed(futures):
                    result = future.result()
                    completed_files += 1
                    completed_bytes += int(result["size_bytes"])
                    finished_path = str(result["local_path"])
                    current_files = [path for path in current_files if path != finished_path]
                    update(
                        completed_files=completed_files,
                        completed_size_bytes=completed_bytes,
                        percent=(
                            round(completed_bytes / total_bytes * 100, 2)
                            if total_bytes
                            else 100.0
                        ),
                        current_files=current_files,
                        current_file=current_files[0] if current_files else "",
                    )

        update(
            phase="uploading_control_files",
            current_file=verifier_local.name,
            current_files=[verifier_local.name],
            active_workers=1,
            parallelism_reason="control_files_serial",
        )
        for local_path, remote_path in (
            (verifier_local, str(remote_verifier)),
            (auto_manifest_path, str(remote_manifest)),
        ):
            control_state = inspect_remote(
                target,
                identity_file,
                server_root,
                allowed_parent,
                [remote_path],
                [str(control_root)],
                connect_timeout,
            ).get(remote_path, {})
            if control_state.get("final_size") == local_path.stat().st_size:
                continue
            if control_state.get("final_size") is not None:
                raise RuntimeError(
                    "Remote control file exists with unexpected size: {}".format(remote_path)
                )
            upload_one(
                target,
                identity_file,
                local_path,
                remote_path,
                connect_timeout,
                resume=control_state.get("part_size") is not None,
            )

        update(
            phase="server_sha256_verification",
            current_file="",
            current_files=[],
            active_workers=server_verify_workers,
            parallelism_reason="server_sha256_verification_{}way".format(server_verify_workers),
            verification_status="RUNNING",
            verification_file_count=len(files),
            verification_completed_files=0,
            verification_failed_files=0,
            verification_total_size_bytes=total_bytes,
            verification_completed_size_bytes=0,
            verification_percent=0.0,
            verification_current_file="",
        )
        verify_command = "python3 {} verify --manifest {} --report {} --progress {} --location server --workers {}".format(
            shlex.quote(str(remote_verifier)),
            shlex.quote(str(remote_manifest)),
            shlex.quote(str(remote_report)),
            shlex.quote(str(remote_progress)),
            server_verify_workers,
        )

        def sync_verification_progress() -> None:
            try:
                download_one(
                    target,
                    identity_file,
                    str(remote_progress),
                    local_progress,
                    connect_timeout,
                )
            except (OSError, RuntimeError):
                # The first poll can occur before the server-side verifier has
                # written its initial control record.  Verification remains
                # authoritative even if this observational refresh is missed.
                return
            progress = read_json_file(local_progress)
            if not progress:
                return
            total_progress_files = max(
                0, min(len(files), int(progress.get("total_file_count", len(files)) or 0))
            )
            completed_progress_files = max(
                0,
                min(total_progress_files, int(progress.get("completed_file_count", 0) or 0)),
            )
            total_progress_bytes = max(
                0, int(progress.get("total_size_bytes", total_bytes) or 0)
            )
            completed_progress_bytes = max(
                0,
                min(
                    total_progress_bytes,
                    int(progress.get("completed_size_bytes", 0) or 0),
                ),
            )
            update(
                verification_status=str(progress.get("status", "RUNNING")),
                verification_file_count=total_progress_files,
                verification_completed_files=completed_progress_files,
                verification_failed_files=max(
                    0, int(progress.get("failed_file_count", 0) or 0)
                ),
                verification_total_size_bytes=total_progress_bytes,
                verification_completed_size_bytes=completed_progress_bytes,
                verification_percent=float(progress.get("percent", 0) or 0),
                verification_current_file=str(progress.get("current_file", "")),
            )

        with ThreadPoolExecutor(max_workers=1) as verification_executor:
            verification_future = verification_executor.submit(
                run_ssh,
                target,
                identity_file,
                verify_command,
                connect_timeout,
                None,
                False,
            )
            while not verification_future.done():
                time.sleep(5)
                sync_verification_progress()
            verification = verification_future.result()
        sync_verification_progress()
        download_one(
            target,
            identity_file,
            str(remote_report),
            verification_report,
            connect_timeout,
        )
        report = json.loads(verification_report.read_text(encoding="utf-8"))
        if verification.returncode != 0 or report.get("status") != "PASS":
            raise RuntimeError("Server SHA-256 verification failed; see {}".format(verification_report))

        marker_path = status_path.parent / "xftp_upload_complete.json"
        write_json_atomic(
            marker_path,
            {
                "project_id": "geo_ring_cloud",
                "canonical_stage_id": "",
                "component_role": COMPONENT_ROLE,
                "related_stage_ids": RELATED_STAGE_IDS,
                "created_at": utc_now(),
                "status": "AUTOMATED_SFTP_COMPLETE",
                "transfer_manifest": str(auto_manifest_path),
                "server_verification": str(verification_report),
                "automatic_delete": False,
            },
        )
        update(
            status="PASS",
            phase="complete",
            completed_at=utc_now(),
            completed_files=len(files),
            completed_size_bytes=total_bytes,
            percent=100.0,
            current_file="",
            current_files=[],
            active_workers=0,
            parallelism_reason="complete",
            server_verification=str(verification_report),
            remote_control_root=str(control_root),
        )
        return 0
    except Exception as exc:
        update(
            status="FAIL",
            phase="failed",
            failed_at=utc_now(),
            error="{}: {}".format(type(exc).__name__, exc),
            automatic_delete=False,
        )
        print(base_status["error"], file=sys.stderr)
        return 2


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Automatically upload a verified GEO batch via SFTP.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest")
    source.add_argument(
        "--watch-batch-root",
        help="Continuously upload finalized files while this batch is downloading.",
    )
    parser.add_argument("--target", required=True, help="SSH target, for example dhr@node05")
    parser.add_argument("--identity-file", required=True)
    parser.add_argument("--server-root", default=str(DEFAULT_SERVER_ROOT))
    parser.add_argument("--allowed-server-parent", default=str(DEFAULT_ALLOWED_PARENT))
    parser.add_argument("--status")
    parser.add_argument("--verification-report")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--platform", action="append", default=[])
    parser.add_argument("--poll-seconds", type=int, default=10)
    parser.add_argument("--connect-timeout", type=int, default=20)
    parser.add_argument(
        "--max-upload-workers",
        type=int,
        default=MAX_UPLOAD_WORKERS,
        choices=range(1, MAX_UPLOAD_WORKERS + 1),
        help="After download completion, ramp SFTP streams up to this value (1-4).",
    )
    parser.add_argument(
        "--server-verify-workers",
        type=int,
        default=DEFAULT_SERVER_VERIFY_WORKERS,
        choices=range(1, MAX_SERVER_VERIFY_WORKERS + 1),
        help="Bounded concurrent SHA-256 reads on the lab server (default: 2).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.watch_batch_root:
        if not args.start_date or not args.end_date or not args.platform:
            raise SystemExit(
                "--watch-batch-root requires --start-date, --end-date and at least one --platform"
            )
        batch_root = Path(args.watch_batch_root).resolve()
        transfer_dir = batch_root / "transfer"
    else:
        manifest_path = Path(args.manifest)
        transfer_dir = manifest_path.resolve().parent
    status_path = Path(args.status) if args.status else transfer_dir / "auto_upload_status.json"
    report_path = (
        Path(args.verification_report)
        if args.verification_report
        else transfer_dir / "server_verification.json"
    )
    if args.watch_batch_root:
        return watch_and_upload(
            batch_root,
            args.start_date,
            args.end_date,
            args.platform,
            args.target,
            Path(args.identity_file).expanduser().resolve(),
            PurePosixPath(args.server_root),
            PurePosixPath(args.allowed_server_parent),
            status_path,
            report_path,
            args.poll_seconds,
            args.connect_timeout,
            args.max_upload_workers,
            args.server_verify_workers,
        )
    return upload_batch(
        manifest_path,
        args.target,
        Path(args.identity_file).expanduser().resolve(),
        PurePosixPath(args.server_root),
        PurePosixPath(args.allowed_server_parent),
        status_path,
        report_path,
        args.connect_timeout,
        args.max_upload_workers,
        args.server_verify_workers,
    )


if __name__ == "__main__":
    raise SystemExit(main())
