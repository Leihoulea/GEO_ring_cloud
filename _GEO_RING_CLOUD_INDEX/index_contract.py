"""Stable source fingerprint contract for Geo Ring Cloud project memory."""

from __future__ import annotations

import hashlib
from pathlib import Path


COMPONENT_ROLE = "index_contract"
SOURCE_SUFFIXES = {
    ".html",
    ".js",
    ".mjs",
    ".ps1",
    ".py",
    ".toml",
    ".yaml",
    ".yml",
}
IGNORED_PARTS = {
    ".git",
    "__pycache__",
    "_tmp",
    "node_modules",
}
SOURCE_ROOTS = (
    "_GEO_RING_CLOUD_INDEX",
    "third_report/code/geo_ring_cloud_stage1",
    "third_report/code/geo_cloud_download",
)


def governed_source_paths(project_root: Path) -> list[Path]:
    project_root = project_root.resolve()
    paths: list[Path] = []
    for relative_root in SOURCE_ROOTS:
        source_root = project_root / relative_root
        if not source_root.is_dir():
            continue
        for path in source_root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            relative = path.relative_to(project_root)
            if any(part in IGNORED_PARTS for part in relative.parts):
                continue
            paths.append(path)
    return sorted(paths, key=lambda path: path.relative_to(project_root).as_posix())


def source_fingerprint(project_root: Path) -> dict[str, object]:
    """Hash governed path names and bytes using an order-stable contract."""
    project_root = project_root.resolve()
    digest = hashlib.sha256()
    paths = governed_source_paths(project_root)
    for path in paths:
        relative = path.relative_to(project_root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        content = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return {
        "algorithm": "sha256(path_length+path+content_length+lf_normalized_content)",
        "sha256": digest.hexdigest(),
        "file_count": len(paths),
    }


__all__ = ["governed_source_paths", "source_fingerprint"]
