"""Upload a verified GEO transfer batch to the lab server over SFTP.

The uploader is deliberately conservative: local data are never modified or
deleted, remote payloads are first written as ``.part`` files, interrupted
uploads are resumed, and completed remote files are never overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


COMPONENT_ROLE = "automated_data_uploader"
RELATED_STAGE_IDS = ["stage_00"]
DEFAULT_SERVER_ROOT = PurePosixPath("/data04/1/dhr/geo_ring_cloud_auto_upload")
DEFAULT_ALLOWED_PARENT = PurePosixPath("/data04/1/dhr")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def subprocess_creation_flags() -> int:
    """Keep OpenSSH child processes invisible on Windows."""
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def write_json_atomic(path: Path, payload: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


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
) -> Tuple[Path, Dict[str, object]]:
    source_manifest_path = source_manifest_path.resolve()
    source = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    if source.get("status") != "READY_FOR_XFTP_UPLOAD":
        raise RuntimeError("Transfer manifest is not READY_FOR_XFTP_UPLOAD")
    source_server_root = PurePosixPath(str(source.get("server_root", "")))
    if not source_server_root.is_absolute():
        raise RuntimeError("Transfer manifest has no absolute server_root")

    remapped_files: List[Dict[str, object]] = []
    for item in source.get("files", []):
        local_path = Path(str(item.get("local_path", "")))
        if not local_path.is_file():
            raise FileNotFoundError("Local source file is missing: {}".format(local_path))
        expected_size = int(item.get("size_bytes", -1))
        if local_path.stat().st_size != expected_size:
            raise RuntimeError("Local source size changed: {}".format(local_path))
        old_remote = PurePosixPath(str(item.get("remote_path", "")))
        try:
            relative = old_remote.relative_to(source_server_root)
        except ValueError as exc:
            raise RuntimeError(
                "Remote path is outside manifest server_root: {}".format(old_remote)
            ) from exc
        new_item = dict(item)
        new_item["remote_path"] = str(server_root / relative)
        remapped_files.append(new_item)

    if not remapped_files:
        raise RuntimeError("Transfer manifest contains no files")

    batch_id = str(source.get("batch_id") or source_manifest_path.stem)
    target = output_path or source_manifest_path.parent / (
        "geo_ring_cloud_auto_upload_{}_manifest.json".format(batch_id)
    )
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
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ssh_base(target, identity_file, connect_timeout) + [command],
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        creationflags=subprocess_creation_flags(),
    )
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


def upload_batch(
    manifest_path: Path,
    target: str,
    identity_file: Path,
    server_root: PurePosixPath,
    allowed_parent: PurePosixPath,
    status_path: Path,
    verification_report: Path,
    connect_timeout: int = 20,
) -> int:
    validate_server_root(server_root, allowed_parent)
    ensure_tools_and_identity(identity_file)
    auto_manifest_path, manifest = build_auto_upload_manifest(manifest_path, server_root)
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
        "started_at": utc_now(),
        "updated_at": utc_now(),
        "pid": os.getpid(),
        "file_count": len(files),
        "completed_files": 0,
        "total_size_bytes": total_bytes,
        "completed_size_bytes": 0,
        "current_file": "",
        "automatic_delete": False,
    }

    def update(**values: object) -> None:
        base_status.update(values)
        base_status["updated_at"] = utc_now()
        write_json_atomic(status_path, base_status)

    update()
    try:
        run_ssh(target, identity_file, "true", connect_timeout)
        remote_paths = [str(item["remote_path"]) for item in files]
        directories = sorted(
            {str(PurePosixPath(path).parent) for path in remote_paths}
            | {str(control_root)}
        )
        remote = inspect_remote(
            target,
            identity_file,
            server_root,
            allowed_parent,
            remote_paths,
            directories,
            connect_timeout,
        )
        completed_files = 0
        completed_bytes = 0
        for index, item in enumerate(files, 1):
            local_path = Path(str(item["local_path"]))
            remote_path = str(item["remote_path"])
            expected_size = int(item["size_bytes"])
            remote_state = remote.get(remote_path, {})
            final_size = remote_state.get("final_size")
            part_size = remote_state.get("part_size")
            update(
                phase="uploading",
                current_file=str(local_path),
                current_index=index,
                remote_path=remote_path,
                completed_files=completed_files,
                completed_size_bytes=completed_bytes,
            )
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
            completed_files += 1
            completed_bytes += expected_size
            update(
                completed_files=completed_files,
                completed_size_bytes=completed_bytes,
                percent=round(completed_bytes / total_bytes * 100, 2) if total_bytes else 100.0,
            )

        update(phase="uploading_control_files", current_file=verifier_local.name)
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

        update(phase="server_sha256_verification", current_file="")
        verify_command = "python3 {} verify --manifest {} --report {} --location server".format(
            shlex.quote(str(remote_verifier)),
            shlex.quote(str(remote_manifest)),
            shlex.quote(str(remote_report)),
        )
        verification = run_ssh(
            target,
            identity_file,
            verify_command,
            connect_timeout,
            check=False,
        )
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
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--target", required=True, help="SSH target, for example dhr@node05")
    parser.add_argument("--identity-file", required=True)
    parser.add_argument("--server-root", default=str(DEFAULT_SERVER_ROOT))
    parser.add_argument("--allowed-server-parent", default=str(DEFAULT_ALLOWED_PARENT))
    parser.add_argument("--status")
    parser.add_argument("--verification-report")
    parser.add_argument("--connect-timeout", type=int, default=20)
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    manifest_path = Path(args.manifest)
    transfer_dir = manifest_path.resolve().parent
    status_path = Path(args.status) if args.status else transfer_dir / "auto_upload_status.json"
    report_path = (
        Path(args.verification_report)
        if args.verification_report
        else transfer_dir / "server_verification.json"
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
    )


if __name__ == "__main__":
    raise SystemExit(main())
