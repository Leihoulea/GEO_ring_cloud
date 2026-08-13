"""Prepare and verify immutable GEO cloud transfer batches.

The utility never uploads or deletes data.  It creates an end-to-end manifest
for Xftp/SFTP transfer and can verify that the uploaded server files match the
local SHA-256 and byte counts using only the Python standard library.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Tuple

CORE_CODE_ROOT = Path(__file__).resolve().parents[1] / "geo_ring_cloud_stage1"
if CORE_CODE_ROOT.is_dir() and str(CORE_CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_CODE_ROOT))
try:
    from geo_ring_cloud.lineage import code_commit, generating_script_state
    from geo_ring_cloud.paths import PROJECT_ROOT
except ImportError:  # The standalone server-side verifier has no project package.
    code_commit = None
    generating_script_state = None
    PROJECT_ROOT = None


COMPONENT_ROLE = "data_transfer_orchestrator"
RELATED_STAGE_IDS = ["stage_00"]
DATE_PATTERN = re.compile(r"^20\d{6}$")
CONTROL_DIRECTORIES = {"logs", "manifests", "quarantine", "transfer"}
PLATFORM_REMOTE_RELATIVE = {
    "GOES-16": PurePosixPath("GOES16/Cloud/GOES-16"),
    "GOES-18": PurePosixPath("GOES-18_cloud"),
    "Himawari-9": PurePosixPath("H09/cloud"),
    "Meteosat-0deg": PurePosixPath("Meteosat-0deg"),
    "Meteosat-IODC": PurePosixPath("Meteosat-IODC"),
    "FY4B": PurePosixPath("FY4B"),
    "CMSAF": PurePosixPath("CM SAF"),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def runtime_lineage() -> Dict[str, object]:
    script = Path(__file__).resolve()
    if generating_script_state is None or code_commit is None or PROJECT_ROOT is None:
        return {
            "generating_script": str(script),
            "code_commit": "",
            "code_commit_scope": "unavailable_outside_repository",
            "generating_script_state": {
                "path": str(script),
                "sha256": sha256_file(script),
                "git_state": "outside_repository",
                "git_tracked": False,
                "worktree_blob": "",
                "commit_blob": "",
                "commit_represents_script": False,
            },
            "lineage_warnings": [
                "repository metadata unavailable; exact script hash retained"
            ],
        }
    state = generating_script_state(script, PROJECT_ROOT)
    return {
        "generating_script": str(script),
        "code_commit": code_commit(PROJECT_ROOT),
        "code_commit_scope": "repository_head_at_manifest_write",
        "generating_script_state": state,
        "lineage_warnings": (
            []
            if state["commit_represents_script"]
            else ["code_commit does not fully represent the generating script content"]
        ),
    }


def parse_day(value: str) -> str:
    return datetime.strptime(value, "%Y-%m-%d").strftime("%Y%m%d")


def date_from_relative_path(path: Path) -> Optional[str]:
    for part in path.parts:
        if DATE_PATTERN.fullmatch(part):
            return part
    return None


def iter_batch_files(
    batch_root: Path,
    start_day: str,
    end_day: str,
    platforms: Optional[set] = None,
) -> Iterable[Tuple[str, Path, Path]]:
    selected = set(PLATFORM_REMOTE_RELATIVE) if platforms is None else set(platforms)
    for platform in sorted(selected):
        platform_root = batch_root / platform
        if not platform_root.is_dir():
            continue
        for path in sorted(platform_root.rglob("*")):
            if not path.is_file() or path.name.endswith(".part"):
                continue
            relative = path.relative_to(platform_root)
            if any(part.lower() in CONTROL_DIRECTORIES for part in relative.parts):
                continue
            file_day = date_from_relative_path(relative)
            if file_day is not None and not (start_day <= file_day <= end_day):
                continue
            yield platform, path, relative


def find_partial_files(batch_root: Path) -> List[str]:
    partial_files: List[str] = []
    for path in batch_root.rglob("*.part"):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(batch_root)
        except ValueError:
            continue
        if relative.parts and relative.parts[0].lower() in CONTROL_DIRECTORIES:
            continue
        partial_files.append(str(path))
    return sorted(partial_files)


def write_csv_manifest(path: Path, rows: List[Dict[str, object]]) -> None:
    fields = [
        "platform",
        "product",
        "target_day",
        "local_path",
        "remote_path",
        "size_bytes",
        "sha256",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def prepare_manifest(
    batch_root: Path,
    output_dir: Path,
    server_root: PurePosixPath,
    start_date: str,
    end_date: str,
    platforms: Optional[set] = None,
) -> Path:
    batch_root = batch_root.resolve()
    if not batch_root.is_dir():
        raise FileNotFoundError("Batch root does not exist: {}".format(batch_root))
    start_day = parse_day(start_date)
    end_day = parse_day(end_date)
    if start_day > end_day:
        raise ValueError("start_date must not be later than end_date")

    partial_files = find_partial_files(batch_root)
    if partial_files:
        raise RuntimeError(
            "Batch is not ready: {} .part file(s) remain".format(len(partial_files))
        )

    rows: List[Dict[str, object]] = []
    for platform, local_path, relative in iter_batch_files(
        batch_root, start_day, end_day, platforms
    ):
        product = relative.parts[0] if relative.parts else ""
        target_day = date_from_relative_path(relative) or ""
        remote_path = server_root / PLATFORM_REMOTE_RELATIVE[platform] / PurePosixPath(
            relative.as_posix()
        )
        rows.append(
            {
                "platform": platform,
                "product": product,
                "target_day": target_day,
                "local_path": str(local_path),
                "remote_path": str(remote_path),
                "size_bytes": local_path.stat().st_size,
                "sha256": sha256_file(local_path),
            }
        )

    if not rows:
        raise RuntimeError("No data files matched the requested batch")

    output_dir.mkdir(parents=True, exist_ok=True)
    batch_id = "{}_{}".format(start_day, end_day)
    csv_path = output_dir / "geo_ring_cloud_transfer_{}_files.csv".format(batch_id)
    json_path = output_dir / "geo_ring_cloud_transfer_{}_manifest.json".format(batch_id)
    report_path = output_dir / "geo_ring_cloud_transfer_{}_upload_plan_cn.md".format(batch_id)
    write_csv_manifest(csv_path, rows)

    by_platform: Dict[str, Dict[str, int]] = {}
    for row in rows:
        summary = by_platform.setdefault(str(row["platform"]), {"file_count": 0, "size_bytes": 0})
        summary["file_count"] += 1
        summary["size_bytes"] += int(row["size_bytes"])

    manifest = {
        "project_id": "geo_ring_cloud",
        "canonical_stage_id": "",
        "component_role": COMPONENT_ROLE,
        "related_stage_ids": RELATED_STAGE_IDS,
        **runtime_lineage(),
        "created_at": utc_now(),
        "batch_id": batch_id,
        "date_range": {"start": start_date, "end": end_date},
        "source_root": str(batch_root),
        "server_root": str(server_root),
        "parameters": {
            "start_date": start_date,
            "end_date": end_date,
            "platforms": sorted(set(PLATFORM_REMOTE_RELATIVE) if platforms is None else platforms),
        },
        "status": "READY_FOR_XFTP_UPLOAD",
        "file_count": len(rows),
        "total_size_bytes": sum(int(row["size_bytes"]) for row in rows),
        "platform_summary": by_platform,
        "files_csv": str(csv_path),
        "files": rows,
        "deletion_policy": {
            "automatic_delete": False,
            "delete_allowed_only_after": "server verification status PASS and explicit user confirmation",
        },
    }
    json_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [
        "# GEO 云数据上传计划",
        "",
        "- 批次：`{}`".format(batch_id),
        "- 状态：`READY_FOR_XFTP_UPLOAD`",
        "- 文件数：{}".format(len(rows)),
        "- 总大小：{:.3f} GiB".format(manifest["total_size_bytes"] / (1024 ** 3)),
        "- 本地根目录：`{}`".format(batch_root),
        "- 服务器根目录：`{}`".format(server_root),
        "- 删除策略：服务器验证 PASS 且用户明确确认前，禁止删除本地批次。",
        "",
        "## 按平台汇总",
        "",
        "| platform | file_count | size_gib |",
        "| --- | ---: | ---: |",
    ]
    for platform, summary in sorted(by_platform.items()):
        lines.append(
            "| {} | {} | {:.3f} |".format(
                platform,
                summary["file_count"],
                summary["size_bytes"] / (1024 ** 3),
            )
        )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path


def verify_manifest(manifest_path: Path, report_path: Path, location: str) -> int:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    results = []
    for row in manifest.get("files", []):
        candidate = Path(row["remote_path"] if location == "server" else row["local_path"])
        result = {
            "path": str(candidate),
            "exists": candidate.is_file(),
            "size_match": False,
            "sha256_match": False,
            "status": "FAIL",
        }
        if candidate.is_file():
            result["actual_size_bytes"] = candidate.stat().st_size
            result["size_match"] = candidate.stat().st_size == int(row["size_bytes"])
            if result["size_match"]:
                result["actual_sha256"] = sha256_file(candidate)
                result["sha256_match"] = result["actual_sha256"] == row["sha256"]
        if result["exists"] and result["size_match"] and result["sha256_match"]:
            result["status"] = "PASS"
        results.append(result)

    failures = [row for row in results if row["status"] != "PASS"]
    report = {
        "project_id": manifest.get("project_id", "geo_ring_cloud"),
        "canonical_stage_id": "",
        "component_role": COMPONENT_ROLE,
        "related_stage_ids": RELATED_STAGE_IDS,
        "verified_at": utc_now(),
        "location": location,
        "source_manifest": str(manifest_path),
        "status": "PASS" if results and not failures else "FAIL",
        "verified_file_count": len(results),
        "failed_file_count": len(failures),
        "delete_local_allowed": False,
        "results": results,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(report_path)
    return 0 if report["status"] == "PASS" else 2


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare or verify an immutable GEO transfer batch.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="Hash local files and create the Xftp upload manifest.")
    prepare.add_argument("--batch-root", required=True)
    prepare.add_argument("--output-dir", required=True)
    prepare.add_argument("--server-root", required=True)
    prepare.add_argument("--start-date", required=True)
    prepare.add_argument("--end-date", required=True)
    prepare.add_argument("--platform", action="append", choices=tuple(PLATFORM_REMOTE_RELATIVE))

    verify = subparsers.add_parser("verify", help="Verify local files or uploaded server files.")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--report", required=True)
    verify.add_argument("--location", choices=("local", "server"), required=True)
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.command == "prepare":
        manifest = prepare_manifest(
            Path(args.batch_root),
            Path(args.output_dir),
            PurePosixPath(args.server_root),
            args.start_date,
            args.end_date,
            set(args.platform) if args.platform else None,
        )
        print(manifest)
        return 0
    return verify_manifest(Path(args.manifest), Path(args.report), args.location)


if __name__ == "__main__":
    raise SystemExit(main())
