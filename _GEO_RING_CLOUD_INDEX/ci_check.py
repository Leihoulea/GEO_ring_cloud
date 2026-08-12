#!/usr/bin/env python
"""Run the repository's deterministic Geo Ring Cloud quality gates."""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "third_report" / "code" / "geo_ring_cloud_stage1"
DOWNLOAD_COMPONENT = ROOT / "third_report" / "code" / "geo_cloud_download"
INDEX_DATABASE = ROOT / "_GEO_RING_CLOUD_INDEX" / "geo_ring_cloud_index.sqlite"
COMPONENT_ROLE = "quality_gate"

INDEX_MINIMUM_ROWS = {
    "scripts": 1,
    "module_registry": 1,
    "stage_registry": 1,
    "artifact_index": 1,
    "data_product_audits": 1,
    "meta": 1,
}

REQUIRED_ENGINEERING_FILES = (
    ROOT / "README.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    ROOT / ".github" / "CODEOWNERS",
    ROOT / ".github" / "workflows" / "geo-ring-cloud-governance.yml",
    CORE / "DEPENDENCIES.md",
    CORE / "environment.yml",
    CORE / "geo_ring_cloud" / "__init__.py",
    CORE / "pyproject.toml",
    ROOT / "_GEO_RING_CLOUD_WORKSPACE" / "architecture.md",
    ROOT / "_GEO_RING_CLOUD_WORKSPACE" / "engineering_policy.md",
    ROOT / "_GEO_RING_CLOUD_WORKSPACE" / "engineering_status.md",
    ROOT / "_GEO_RING_CLOUD_WORKSPACE" / "module_registry.md",
)


def run(label: str, command: list[str]) -> None:
    print(f"\n== {label} ==", flush=True)
    completed = subprocess.run(command, cwd=ROOT, check=False)
    if completed.returncode:
        raise SystemExit(f"{label} failed with exit code {completed.returncode}")


def check_contract_files() -> None:
    missing = [str(path.relative_to(ROOT)) for path in REQUIRED_ENGINEERING_FILES if not path.is_file()]
    if missing:
        raise SystemExit("Missing engineering contract files:\n- " + "\n- ".join(missing))
    print("Engineering contract files: OK", flush=True)


def check_local_index() -> None:
    """Reject an empty or stale local project-memory database when it exists."""
    if not INDEX_DATABASE.is_file():
        print("Local SQLite index: SKIP (generated index is not committed)", flush=True)
        return
    with sqlite3.connect(INDEX_DATABASE) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise SystemExit(f"Local SQLite index integrity check failed: {integrity}")
        table_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = sorted(set(INDEX_MINIMUM_ROWS) - table_names)
        if missing:
            raise SystemExit(
                "Local SQLite index is missing tables: " + ", ".join(missing)
            )
        counts = {
            table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in INDEX_MINIMUM_ROWS
        }
        empty = [table for table, count in counts.items() if count < INDEX_MINIMUM_ROWS[table]]
        if empty:
            raise SystemExit(
                "Local SQLite index has empty authoritative tables: "
                + ", ".join(f"{table}={counts[table]}" for table in empty)
            )
        generated_row = conn.execute(
            "SELECT value FROM meta WHERE key='generated_at'"
        ).fetchone()
    if generated_row is None:
        raise SystemExit("Local SQLite index has no meta.generated_at value")
    try:
        generated_at = datetime.fromisoformat(
            str(generated_row[0]).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except ValueError as exc:
        raise SystemExit("Local SQLite index has invalid meta.generated_at") from exc

    governed_roots = (CORE, DOWNLOAD_COMPONENT, ROOT / "_GEO_RING_CLOUD_INDEX")
    governed_suffixes = {".py", ".ps1", ".mjs", ".js", ".toml", ".yaml", ".yml"}
    latest_path: Path | None = None
    latest_mtime = 0.0
    for governed_root in governed_roots:
        if not governed_root.is_dir():
            continue
        for path in governed_root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in governed_suffixes:
                continue
            if any(part in {"__pycache__", "_tmp", "node_modules"} for part in path.parts):
                continue
            mtime = path.stat().st_mtime
            if mtime > latest_mtime:
                latest_path = path
                latest_mtime = mtime
    if latest_path is not None:
        latest_at = datetime.fromtimestamp(latest_mtime, timezone.utc)
        if latest_at > generated_at:
            raise SystemExit(
                "Local SQLite index is stale; run build_index.py after changing "
                f"{latest_path.relative_to(ROOT)}"
            )
    print(
        "Local SQLite index: OK "
        f"(scripts={counts['scripts']}, stages={counts['stage_registry']}, "
        f"artifacts={counts['artifact_index']})",
        flush=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scientific-tests",
        action="store_true",
        help="run dependency-backed unit tests after lightweight checks",
    )
    parser.add_argument(
        "--integration-tests",
        action="store_true",
        help="also run real-data integration tests; requires configured local data",
    )
    args = parser.parse_args()

    check_contract_files()
    check_local_index()
    run(
        "Python syntax",
        [sys.executable, "-m", "compileall", "-q", "_GEO_RING_CLOUD_INDEX", str(CORE)],
    )
    run(
        "Repository governance",
        [
            sys.executable,
            "_GEO_RING_CLOUD_INDEX/governance_check.py",
            "--all",
            "--quiet-warnings",
        ],
    )
    run(
        "Governance unit tests",
        [sys.executable, "_GEO_RING_CLOUD_INDEX/tests/test_governance_check.py"],
    )
    run(
        "Data transfer and notification unit tests",
        [sys.executable, str(DOWNLOAD_COMPONENT / "tests" / "test_geo_ring_cloud_transfer_batch.py")],
    )

    if os.name == "nt":
        run(
            "PowerShell path configuration contract",
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                "_GEO_RING_CLOUD_INDEX/tests/test_path_configuration.ps1",
            ],
        )

    if args.scientific_tests or args.integration_tests:
        run(
            "Scientific unit tests",
            [sys.executable, str(CORE / "tests" / "geo_ring_cloud_test_claas3.py")],
        )
    if args.integration_tests:
        run(
            "Real-data integration tests",
            [sys.executable, str(CORE / "tests" / "geo_ring_cloud_test_claas3_integration.py")],
        )

    print("\nGeo Ring Cloud CI checks: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
