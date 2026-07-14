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

AMBIGUOUS_STAGE_PATTERNS = [
    re.compile(r"(^|[\\/._-])step[_ -]?\d+[a-z]?", re.IGNORECASE),
    re.compile(r"(^|[\\/._-])stage\s*\d+[a-z]?", re.IGNORECASE),
    re.compile(r"(^|[\\/._-])stage\d+[a-z]?", re.IGNORECASE),
    re.compile(r"(^|[\\/._-])\d{1,2}[_-]stage\d+[a-z]?", re.IGNORECASE),
]

CANONICAL_STAGE_PATTERN = re.compile(
    r"(^|[\\/._-])stage_\d{2}(?:_[0-9]+|[a-z0-9]+|_[a-z0-9]+)?($|[\\/._-])"
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


def check_naming(paths: list[str], added_paths: set[str], baseline_mode: bool) -> list[Finding]:
    findings: list[Finding] = []
    for rel_path in paths:
        normalized = rel_path.replace("\\", "/")
        if ".git/" in normalized:
            continue
        name_only = Path(normalized).name
        has_ambiguous = any(pattern.search(name_only) for pattern in AMBIGUOUS_STAGE_PATTERNS)
        has_canonical = bool(CANONICAL_STAGE_PATTERN.search(name_only))
        if not has_ambiguous or has_canonical:
            continue
        severity = "WARN" if baseline_mode or rel_path not in added_paths else "ERROR"
        findings.append(
            Finding(
                severity,
                rel_path,
                "ambiguous stage/step naming; new stage-owned files should use canonical IDs like stage_09d",
            )
        )
    return findings


def check_paths(paths: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for rel_path in paths:
        suffix = Path(rel_path).suffix.lower()
        text = read_text(rel_path)
        if text is None:
            continue

        normalized = rel_path.replace("\\", "/")
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
                        "WARN",
                        rel_path,
                        "contains absolute D:/AAAresearch_paper path; prefer path_config.py or an environment override",
                    )
                )
    return findings


def print_findings(findings: list[Finding]) -> int:
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
    args = parser.parse_args()

    baseline_mode = not has_head()
    if args.staged:
        paths, added = staged_paths()
    else:
        paths, added = all_repo_candidate_paths()
        if baseline_mode:
            added = set()

    findings: list[Finding] = []
    findings.extend(check_naming(paths, added, baseline_mode=baseline_mode))
    findings.extend(check_paths(paths))
    return print_findings(findings)


if __name__ == "__main__":
    sys.exit(main())
