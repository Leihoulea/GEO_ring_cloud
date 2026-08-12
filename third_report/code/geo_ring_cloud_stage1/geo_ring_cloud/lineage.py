"""Run and artifact lineage manifest helpers."""

from __future__ import annotations

import hashlib
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


def _git_output(project_root: Path, args: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        # Git porcelain uses the first two columns as state. Preserve leading
        # spaces and remove line terminators only, otherwise `` M`` is
        # misclassified as a staged change.
        return result.returncode, result.stdout.rstrip("\r\n")
    except OSError:
        return 1, ""


def generating_script_state(generating_script: Path, project_root: Path) -> dict[str, Any]:
    """Describe the exact script content and whether HEAD contains that content."""
    script = generating_script.resolve()
    root_rc, root_text = _git_output(project_root.resolve(), ["rev-parse", "--show-toplevel"])
    root = Path(root_text).resolve() if root_rc == 0 and root_text else project_root.resolve()
    state: dict[str, Any] = {
        "path": str(script),
        "sha256": "",
        "git_state": "missing",
        "git_tracked": False,
        "worktree_blob": "",
        "commit_blob": "",
        "commit_represents_script": False,
    }
    if not script.is_file():
        return state

    state["sha256"] = hashlib.sha256(script.read_bytes()).hexdigest()
    try:
        rel = script.relative_to(root).as_posix()
    except ValueError:
        state["git_state"] = "outside_repository"
        return state

    state["repository_relative_path"] = rel
    tracked_rc, _ = _git_output(root, ["ls-files", "--error-unmatch", "--", rel])
    state["git_tracked"] = tracked_rc == 0

    _, worktree_blob = _git_output(root, ["hash-object", "--", rel])
    state["worktree_blob"] = worktree_blob
    commit_rc, commit_blob = _git_output(root, ["rev-parse", f"HEAD:{rel}"])
    if commit_rc == 0:
        state["commit_blob"] = commit_blob

    _, status = _git_output(root, ["status", "--porcelain=v1", "--untracked-files=all", "--", rel])
    code = status[:2] if status else ""
    if code == "??" or not state["git_tracked"]:
        git_state = "untracked"
    elif not code:
        git_state = "clean"
    elif code[0] != " " and code[1] != " ":
        git_state = "staged_and_modified"
    elif code[0] != " ":
        git_state = "staged"
    else:
        git_state = "modified"
    state["git_state"] = git_state
    state["commit_represents_script"] = bool(
        git_state == "clean"
        and state["commit_blob"]
        and state["commit_blob"] == state["worktree_blob"]
    )
    return state


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
    script_state = generating_script_state(generating_script, project_root)
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
        "code_commit_scope": "repository_head_at_manifest_write",
        "generating_script_state": script_state,
        "lineage_warnings": (
            []
            if script_state["commit_represents_script"]
            else ["code_commit does not fully represent the generating script content"]
        ),
        "source_registry_version": REGISTRY_VERSION,
        "product_versions": {},
    }
    if extra:
        payload.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


__all__ = [
    "PROJECT_ID",
    "code_commit",
    "generating_script_state",
    "utc_now",
    "write_manifest",
]
