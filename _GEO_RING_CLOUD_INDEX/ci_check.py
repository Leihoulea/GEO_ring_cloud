#!/usr/bin/env python
"""Run the repository's deterministic Geo Ring Cloud quality gates."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "third_report" / "code" / "geo_ring_cloud_stage1"
COMPONENT_ROLE = "quality_gate"

REQUIRED_ENGINEERING_FILES = (
    ROOT / ".github" / "workflows" / "geo-ring-cloud-governance.yml",
    CORE / "DEPENDENCIES.md",
    CORE / "environment.yml",
    ROOT / "_GEO_RING_CLOUD_WORKSPACE" / "architecture.md",
    ROOT / "_GEO_RING_CLOUD_WORKSPACE" / "engineering_policy.md",
    ROOT / "_GEO_RING_CLOUD_WORKSPACE" / "engineering_status.md",
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
    run(
        "Python syntax",
        [sys.executable, "-m", "compileall", "-q", "_GEO_RING_CLOUD_INDEX", str(CORE)],
    )
    run(
        "Repository governance",
        [sys.executable, "_GEO_RING_CLOUD_INDEX/governance_check.py", "--all"],
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
