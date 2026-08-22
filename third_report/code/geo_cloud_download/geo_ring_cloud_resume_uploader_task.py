#!/usr/bin/env python3
"""Short command-line entry point for a durable, safe GEO upload resume.

This wrapper intentionally discovers the existing transfer manifest only.  It
does not create, move, or delete source data, and delegates all remote
preflight, resumability, and SHA-256 verification to the normal uploader.
It exists so Windows Task Scheduler can invoke a resume job without exceeding
its 261-character task-action limit.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List, Optional

from geo_ring_cloud_auto_uploader import main as auto_uploader_main


DEFAULT_TARGET = "dhr@210.45.127.28"
DEFAULT_IDENTITY_FILE = Path.home() / ".ssh" / "id_ed25519_node05"
DEFAULT_SERVER_ROOT = "/data04/1/dhr/geo_ring_cloud_auto_upload"
DEFAULT_ALLOWED_PARENT = "/data04/1/dhr"


def transfer_manifest_for_batch(batch_root: Path) -> Path:
    """Return the sole finalized transfer manifest for ``batch_root``."""
    transfer_dir = batch_root.resolve() / "transfer"
    manifests = sorted(transfer_dir.glob("geo_ring_cloud_transfer_*_manifest.json"))
    if len(manifests) != 1:
        raise RuntimeError(
            "Expected exactly one transfer manifest in {} (found {}).".format(
                transfer_dir, len(manifests)
            )
        )
    return manifests[0]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resume one finalized Geo Ring Cloud upload safely."
    )
    parser.add_argument("--batch-root", required=True)
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--identity-file", default=str(DEFAULT_IDENTITY_FILE))
    parser.add_argument("--server-root", default=DEFAULT_SERVER_ROOT)
    parser.add_argument("--allowed-server-parent", default=DEFAULT_ALLOWED_PARENT)
    parser.add_argument("--max-upload-workers", type=int, default=4)
    parser.add_argument("--server-verify-workers", type=int, default=2)
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    batch_root = Path(args.batch_root).resolve()
    manifest = transfer_manifest_for_batch(batch_root)
    transfer_dir = manifest.parent
    audit_path = transfer_dir / "auto_upload_resume_task_audit.json"
    audit_path.write_text(
        json.dumps(
            {
                "status": "RUNNING",
                "manifest": str(manifest),
                "automatic_delete": False,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    result = auto_uploader_main(
        [
            "--manifest",
            str(manifest),
            "--target",
            str(args.target),
            "--identity-file",
            str(args.identity_file),
            "--server-root",
            str(args.server_root),
            "--allowed-server-parent",
            str(args.allowed_server_parent),
            "--status",
            str(transfer_dir / "auto_upload_status.json"),
            "--verification-report",
            str(transfer_dir / "server_verification.json"),
            "--max-upload-workers",
            str(args.max_upload_workers),
            "--server-verify-workers",
            str(args.server_verify_workers),
        ]
    )
    audit_path.write_text(
        json.dumps(
            {
                "status": "RETURNED",
                "exit_code": result,
                "manifest": str(manifest),
                "automatic_delete": False,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
