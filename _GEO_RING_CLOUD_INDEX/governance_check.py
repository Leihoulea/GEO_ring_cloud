#!/usr/bin/env python
r"""Lightweight governance checks for the Geo Ring Cloud repository.

The first commit establishes a historical baseline, so legacy naming issues are
reported as warnings. After HEAD exists, newly added files that use ambiguous
Step/Stage naming are treated as errors.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

TEXT_EXTENSIONS = {
    ".bat",
    ".cmd",
    ".csv",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

CODE_AND_CONFIG_EXTENSIONS = {
    ".bat",
    ".cmd",
    ".json",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".yaml",
    ".yml",
}

GENERATED_OR_LARGE_EXTENSIONS = {
    ".db",
    ".h5",
    ".hdf",
    ".jpg",
    ".jpeg",
    ".nc",
    ".nc4",
    ".npz",
    ".pdf",
    ".png",
    ".pptx",
    ".sqlite",
    ".xlsx",
}

AMBIGUOUS_STAGE_PATTERNS = [
    re.compile(r"(^|[\\/._-])step[_ -]?\d+[a-z]?", re.IGNORECASE),
    re.compile(r"(^|[\\/._-])stage\s*\d+[a-z]?", re.IGNORECASE),
    re.compile(r"(^|[\\/._-])stage\d+[a-z]?", re.IGNORECASE),
    re.compile(r"(^|[\\/._-])\d{1,2}[_-]stage\d+[a-z]?", re.IGNORECASE),
]

CANONICAL_STAGE_PATTERN = re.compile(
    r"(^|[\\/._-])stage_\d{2}(?:_[0-9]+|[a-z0-9]+|_[a-z0-9]+)?($|[\\/._-])"
)
CANONICAL_STAGE_TOKEN = re.compile(
    r"stage_\d{2}(?:_[0-9]+|[a-z0-9]+|_[a-z0-9]+)?", re.IGNORECASE
)
LEGACY_NUMERIC_STAGE_NAME = re.compile(r"^\d{1,2}(?:[a-z]|\.\d+)?[_-]", re.IGNORECASE)
STAGE_ID_ASSIGNMENT = re.compile(
    r"^\s*(STAGE_ID|PROJECT_STAGE_ID)\s*=\s*['\"]([^'\"]+)['\"]",
    re.MULTILINE,
)

DANGEROUS_PATH_PATTERNS = [
    re.compile(r"_NON_GEO_ARCHIVE", re.IGNORECASE),
    re.compile(r"third_report[\\/]+outputs[\\/]+epic_ceres", re.IGNORECASE),
    re.compile(r"third_report[\\/]+code[\\/]+epic_ceres", re.IGNORECASE),
    re.compile(r"(^|[\\/])second_report([\\/]|$)", re.IGNORECASE),
    re.compile(r"(^|[\\/])forth([\\/]|$)", re.IGNORECASE),
]

ABSOLUTE_PROJECT_PATH = re.compile(
    r"[A-Za-z]:[\\/]+AAAresearch_paper[\\/]+", re.IGNORECASE
)

ABSOLUTE_PATH_ALLOWLIST = {
    "_GEO_RING_CLOUD_INDEX/build_index.py",
    "_GEO_RING_CLOUD_INDEX/governance_check.py",
    "third_report/code/geo_ring_cloud_stage1/path_config.py",
}

PATH_REFERENCE_ENFORCED_PREFIXES = (
    "third_report/code/geo_ring_cloud_stage1/",
)

CORE_CODE_PREFIX = "third_report/code/geo_ring_cloud_stage1/"
WORKSPACE_INDEX_DOCS = {
    "_GEO_RING_CLOUD_WORKSPACE/stage_registry.md",
    "_GEO_RING_CLOUD_WORKSPACE/artifact_index.md",
}
COMMIT_MESSAGE_TOKEN = re.compile(
    r"\b(stage_\d{2}(?:_[0-9]+|[a-z0-9]+|_[a-z0-9]+)?|geo_ring_cloud|governance|index|artifact|skill|path_config|data_audit|research_tracker)\b",
    re.IGNORECASE,
)


@dataclass
class Finding:
    severity: str
    path: str
    message: str


def run_git(args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=check,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def has_head() -> bool:
    return run_git(["rev-parse", "--verify", "HEAD"]).returncode == 0


def staged_paths() -> tuple[list[str], set[str]]:
    names = run_git(["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"])
    paths = [p for p in names.stdout.split("\0") if p]

    status = run_git(["diff", "--cached", "--name-status", "--diff-filter=ACMR", "-z"])
    fields = [p for p in status.stdout.split("\0") if p]
    added: set[str] = set()
    i = 0
    while i < len(fields):
        code = fields[i]
        if code.startswith("R") or code.startswith("C"):
            if i + 2 < len(fields):
                added.add(fields[i + 2])
            i += 3
        else:
            if i + 1 < len(fields) and code.startswith("A"):
                added.add(fields[i + 1])
            i += 2
    return paths, added


def all_repo_candidate_paths() -> tuple[list[str], set[str]]:
    tracked = run_git(["ls-files", "-z"])
    untracked = run_git(["ls-files", "-o", "--exclude-standard", "-z"])
    tracked_paths = [p for p in tracked.stdout.split("\0") if p]
    untracked_paths = [p for p in untracked.stdout.split("\0") if p]
    paths = sorted(set(tracked_paths + untracked_paths))
    return paths, set(untracked_paths)


def read_text(rel_path: str) -> str | None:
    path = ROOT / rel_path
    if not path.exists() or not path.is_file():
        return None
    if path.suffix.lower() not in TEXT_EXTENSIONS:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            return None


def severity_for(rel_path: str, added_paths: set[str], baseline_mode: bool) -> str:
    return "WARN" if baseline_mode or rel_path not in added_paths else "ERROR"


def normalize_path(rel_path: str) -> str:
    return rel_path.replace("\\", "/")


def canonical_stage_from_text(text: str) -> str:
    match = CANONICAL_STAGE_TOKEN.search(text)
    return match.group(0).lower() if match else ""


def path_canonical_stage(rel_path: str) -> str:
    return canonical_stage_from_text(normalize_path(rel_path))


def is_core_code(rel_path: str) -> bool:
    return normalize_path(rel_path).startswith(CORE_CODE_PREFIX)


def is_stage_owned_path(rel_path: str) -> bool:
    normalized = normalize_path(rel_path)
    name = Path(normalized).name
    return bool(
        path_canonical_stage(normalized)
        or any(pattern.search(name) for pattern in AMBIGUOUS_STAGE_PATTERNS)
        or LEGACY_NUMERIC_STAGE_NAME.search(name)
    )


def stage_ids_in_script(rel_path: str) -> set[str]:
    text = read_text(rel_path) or ""
    values: set[str] = set()
    for _, value in STAGE_ID_ASSIGNMENT.findall(text):
        value = value.strip().lower()
        if value.startswith("geo_ring_cloud."):
            value = value.split(".", 1)[1]
        values.add(value)
    return values


def check_naming(paths: list[str], added_paths: set[str], baseline_mode: bool) -> list[Finding]:
    findings: list[Finding] = []
    for rel_path in paths:
        normalized = normalize_path(rel_path)
        if ".git/" in normalized:
            continue
        name_only = Path(normalized).name
        has_ambiguous = any(pattern.search(name_only) for pattern in AMBIGUOUS_STAGE_PATTERNS)
        has_canonical = bool(CANONICAL_STAGE_PATTERN.search(name_only))
        if not has_ambiguous or has_canonical:
            continue
        findings.append(
            Finding(
                severity_for(rel_path, added_paths, baseline_mode),
                rel_path,
                "ambiguous stage/step naming; new stage-owned files should use canonical IDs like stage_09d",
            )
        )
    return findings


def check_stage_contract(paths: list[str], added_paths: set[str], enforce_index_docs: bool) -> list[Finding]:
    findings: list[Finding] = []
    changed_stage_scripts = False
    normalized_paths = {normalize_path(p) for p in paths}
    normalized_added = {normalize_path(p) for p in added_paths}

    for rel_path in paths:
        normalized = normalize_path(rel_path)
        if not is_core_code(normalized):
            continue
        suffix = Path(normalized).suffix.lower()
        if suffix not in CODE_AND_CONFIG_EXTENSIONS:
            continue

        if suffix == ".py" and is_stage_owned_path(normalized):
            changed_stage_scripts = True

        if normalized not in normalized_added or suffix != ".py":
            continue

        stage_owned = is_stage_owned_path(normalized)
        canonical = path_canonical_stage(normalized)
        if stage_owned and not canonical:
            findings.append(
                Finding(
                    "ERROR",
                    rel_path,
                    "new stage-owned code must use canonical stage IDs such as stage_10p2, not legacy stage10/step names",
                )
            )
            continue

        if stage_owned:
            script_stage_ids = stage_ids_in_script(rel_path)
            if not script_stage_ids:
                findings.append(
                    Finding(
                        "ERROR",
                        rel_path,
                        "new stage script must define STAGE_ID or PROJECT_STAGE_ID",
                    )
                )
            elif canonical not in script_stage_ids:
                findings.append(
                    Finding(
                        "ERROR",
                        rel_path,
                        f"stage identifier mismatch: path implies {canonical}, but script declares {sorted(script_stage_ids)}",
                    )
                )
        elif not Path(normalized).name.startswith("geo_ring_cloud_"):
            findings.append(
                Finding(
                    "ERROR",
                    rel_path,
                    "new non-stage core utilities must use a geo_ring_cloud_<role> filename",
                )
            )

    if changed_stage_scripts and enforce_index_docs:
        for doc in sorted(WORKSPACE_INDEX_DOCS - normalized_paths):
            findings.append(
                Finding(
                    "ERROR",
                    doc,
                    "stage code changed; run build_index.py and stage updated workspace index Markdown",
                )
            )
    return findings


def check_generated_artifacts(paths: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for rel_path in paths:
        suffix = Path(rel_path).suffix.lower()
        if suffix in GENERATED_OR_LARGE_EXTENSIONS:
            findings.append(
                Finding(
                    "ERROR",
                    rel_path,
                    f"generated or large artifact ({suffix}) must not be committed; keep it outside Git or add a narrow allowlist",
                )
            )
    return findings


def check_paths(paths: list[str], added_paths: set[str], baseline_mode: bool) -> list[Finding]:
    findings: list[Finding] = []
    for rel_path in paths:
        suffix = Path(rel_path).suffix.lower()
        text = read_text(rel_path)
        if text is None:
            continue

        normalized = normalize_path(rel_path)
        enforce_dangerous_paths = normalized.startswith(PATH_REFERENCE_ENFORCED_PREFIXES)

        if suffix in CODE_AND_CONFIG_EXTENSIONS and enforce_dangerous_paths:
            for pattern in DANGEROUS_PATH_PATTERNS:
                if pattern.search(text):
                    findings.append(
                        Finding(
                            "ERROR",
                            rel_path,
                            f"code/config references archived or non-Geo path pattern: {pattern.pattern}",
                        )
                    )

        if suffix in CODE_AND_CONFIG_EXTENSIONS and normalized not in ABSOLUTE_PATH_ALLOWLIST:
            if ABSOLUTE_PROJECT_PATH.search(text):
                findings.append(
                    Finding(
                        severity_for(rel_path, added_paths, baseline_mode),
                        rel_path,
                        "contains absolute D:/AAAresearch_paper path; prefer path_config.py or an environment override",
                    )
                )
    return findings


def check_commit_message(path: Path) -> list[Finding]:
    try:
        message = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        return [Finding("ERROR", str(path), f"cannot read commit message: {exc}")]
    first_line = next((line.strip() for line in message.splitlines() if line.strip()), "")
    if not first_line:
        return [Finding("ERROR", str(path), "commit message is empty")]
    if first_line.startswith(("Merge ", "Revert ", "fixup!", "squash!")):
        return []
    if COMMIT_MESSAGE_TOKEN.search(first_line):
        return []
    return [
        Finding(
            "ERROR",
            str(path),
            "commit message must name a canonical stage ID or component role such as stage_10p2 or geo_ring_cloud governance",
        )
    ]


def print_findings(findings: list[Finding], strict: bool = False) -> int:
    if strict:
        findings = [
            Finding("ERROR" if item.severity == "WARN" else item.severity, item.path, item.message)
            for item in findings
        ]
    errors = [item for item in findings if item.severity == "ERROR"]
    warnings = [item for item in findings if item.severity == "WARN"]

    if not findings:
        print("Geo Ring Cloud governance check: OK")
        return 0

    print("Geo Ring Cloud governance check:")
    for item in findings:
        print(f"[{item.severity}] {item.path}: {item.message}")

    print(f"\nSummary: {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="check staged files for pre-commit use")
    parser.add_argument("--all", action="store_true", help="check tracked and untracked non-ignored files")
    parser.add_argument("--strict", action="store_true", help="treat historical warnings as errors")
    parser.add_argument("--commit-msg", type=Path, help="check a Git commit message file")
    args = parser.parse_args()

    if args.commit_msg:
        return print_findings(check_commit_message(args.commit_msg), strict=args.strict)

    baseline_mode = not has_head()
    if args.staged:
        paths, added = staged_paths()
    else:
        paths, added = all_repo_candidate_paths()
        if baseline_mode:
            added = set()

    findings: list[Finding] = []
    findings.extend(check_naming(paths, added, baseline_mode=baseline_mode))
    findings.extend(check_stage_contract(paths, added, enforce_index_docs=args.staged))
    if args.staged:
        findings.extend(check_generated_artifacts(paths))
    findings.extend(check_paths(paths, added, baseline_mode=baseline_mode))
    return print_findings(findings, strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
