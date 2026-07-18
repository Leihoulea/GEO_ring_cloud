"""Run and artifact lineage manifest helpers."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import PROJECT_ID
from .sources import REGISTRY_VERSION


COMPONENT_ROLE = "lineage"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def code_commit(project_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except OSError:
        return ""


def write_manifest(
    path: Path,
    *,
    canonical_stage_id: str,
    component_role: str = "",
    related_stage_ids: Iterable[str] = (),
    generating_script: Path,
    input_paths: Iterable[str | Path],
    output_paths: Iterable[str | Path],
    parameters: dict[str, Any],
    project_root: Path,
    run_id: str = "",
    source_profile: str = "",
    extra: dict[str, Any] | None = None,
) -> Path:
    payload: dict[str, Any] = {
        "project_id": PROJECT_ID,
        "canonical_stage_id": canonical_stage_id,
        "component_role": component_role,
        "related_stage_ids": list(related_stage_ids),
        "run_id": run_id,
        "source_profile": source_profile,
        "generating_script": str(generating_script),
        "input_paths": [str(item) for item in input_paths],
        "output_paths": [str(item) for item in output_paths],
        "parameter_summary": parameters,
        "timestamp_utc": utc_now(),
        "code_commit": code_commit(project_root),
        "source_registry_version": REGISTRY_VERSION,
        "product_versions": {},
    }
    if extra:
        payload.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


__all__ = ["PROJECT_ID", "code_commit", "utc_now", "write_manifest"]
