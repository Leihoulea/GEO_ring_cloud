"""Discover canonical and legacy time-run directories."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


COMPONENT_ROLE = "run_discovery"
LEGACY_TAG_RE = re.compile(r"^202403\d{2}_\d{4}$")
MATRIX_MANIFEST = "geo_ring_cloud_time_run_matrix_manifest.json"


def matrix_manifests(runs_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    rows: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(runs_root.glob(f"*/{MATRIX_MANIFEST}")):
        try:
            rows.append((path, json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError):
            continue
    return rows


def resolve_run_dir(
    runs_root: Path,
    run_or_time_tag: str,
    source_profile: str = "operational_baseline",
) -> Path | None:
    direct_profile = runs_root / run_or_time_tag / source_profile
    if (direct_profile / "single_sample_run_manifest.json").exists():
        return direct_profile
    for _, manifest in matrix_manifests(runs_root):
        common = manifest.get("common_inputs", {})
        if run_or_time_tag not in {str(manifest.get("run_id", "")), str(common.get("time_tag", ""))}:
            continue
        for row in manifest.get("profile_runs", []):
            if row.get("source_profile") == source_profile:
                path = Path(str(row.get("profile_root", "")))
                if path.exists():
                    return path
    legacy = runs_root / run_or_time_tag
    if LEGACY_TAG_RE.match(run_or_time_tag) and legacy.is_dir():
        return legacy
    return None


def discover_run_dirs(runs_root: Path, source_profile: str = "operational_baseline") -> list[Path]:
    discovered: dict[str, Path] = {}
    for _, manifest in matrix_manifests(runs_root):
        time_tag = str(manifest.get("common_inputs", {}).get("time_tag", ""))
        for row in manifest.get("profile_runs", []):
            if row.get("source_profile") == source_profile:
                path = Path(str(row.get("profile_root", "")))
                if path.is_dir():
                    discovered[time_tag or path.name] = path
    for path in sorted(runs_root.iterdir() if runs_root.exists() else []):
        if path.is_dir() and LEGACY_TAG_RE.match(path.name):
            discovered.setdefault(path.name, path)
    return [discovered[key] for key in sorted(discovered)]


def run_time_tag(run_dir: Path) -> str:
    manifest = run_dir / "single_sample_run_manifest.json"
    if manifest.exists():
        try:
            return str(json.loads(manifest.read_text(encoding="utf-8")).get("time_tag", run_dir.name))
        except (OSError, json.JSONDecodeError):
            pass
    return run_dir.name


__all__ = [
    "LEGACY_TAG_RE",
    "MATRIX_MANIFEST",
    "discover_run_dirs",
    "matrix_manifests",
    "resolve_run_dir",
    "run_time_tag",
]
