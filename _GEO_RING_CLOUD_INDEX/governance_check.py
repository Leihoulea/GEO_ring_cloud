#!/usr/bin/env python
r"""Lightweight governance checks for the Geo Ring Cloud repository.

The first commit establishes a historical baseline, so legacy naming issues are
reported as warnings. After HEAD exists, newly added files that use ambiguous
Step/Stage naming are treated as errors.
"""

from __future__ import annotations

import argparse
import ast
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
COMPONENT_ROLE_ASSIGNMENT = re.compile(
    r"^\s*\$?COMPONENT_ROLE\s*=\s*['\"]([a-z][a-z0-9_]*)['\"]",
    re.MULTILINE,
)
PYTHON_MODULE_FILENAME = re.compile(r"^[a-z][a-z0-9_]*\.py$")

DANGEROUS_PATH_PATTERNS = [
    re.compile(r"_NON_GEO_ARCHIVE", re.IGNORECASE),
    re.compile(r"third_report[\\/]+outputs[\\/]+epic_ceres", re.IGNORECASE),
    re.compile(r"third_report[\\/]+code[\\/]+epic_ceres", re.IGNORECASE),
    re.compile(r"(^|[\\/])second_report([\\/]|$)", re.IGNORECASE),
    re.compile(r"(^|[\\/])forth([\\/]|$)", re.IGNORECASE),
]

ABSOLUTE_LOCAL_PATH = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]+")

ABSOLUTE_PATH_ALLOWLIST = {
    "_GEO_RING_CLOUD_INDEX/build_index.py",
    "_GEO_RING_CLOUD_INDEX/governance_check.py",
    "third_report/code/geo_ring_cloud_stage1/geo_ring_cloud/paths.py",
    "third_report/code/geo_ring_cloud_stage1/geo_ring_cloud_path_configuration.ps1",
    "third_report/code/geo_ring_cloud_stage1/path_config.py",
}

ABSOLUTE_LOCAL_PATH_ENFORCED_PREFIXES = (
    "third_report/code/geo_cloud_download/",
    "third_report/code/geo_data_audit/",
    "third_report/code/geo_ring_cloud_stage1/",
    "third_report/code/priority_download_goes_meteosat/",
    "third_report/scripts/",
)

PATH_REFERENCE_ENFORCED_PREFIXES = (
    "third_report/code/geo_ring_cloud_stage1/",
)

CORE_CODE_PREFIX = "third_report/code/geo_ring_cloud_stage1/"
CORE_PACKAGE_PREFIX = f"{CORE_CODE_PREFIX}geo_ring_cloud/"
COMPATIBILITY_SHIM_PATHS = {
    f"{CORE_CODE_PREFIX}path_config.py": "geo_ring_cloud.paths",
    f"{CORE_CODE_PREFIX}geo_ring_cloud_source_registry.py": "geo_ring_cloud.sources",
    f"{CORE_CODE_PREFIX}geo_ring_cloud_lineage.py": "geo_ring_cloud.lineage",
    f"{CORE_CODE_PREFIX}geo_ring_cloud_run_discovery.py": "geo_ring_cloud.run_discovery",
    f"{CORE_CODE_PREFIX}geo_ring_cloud_claas3_adapter.py": "geo_ring_cloud.adapters.claas3",
    f"{CORE_CODE_PREFIX}geo_ring_cloud_epic_pair_diagnostics.py": "geo_ring_cloud.diagnostics.epic_pair",
    f"{CORE_CODE_PREFIX}stage_09d_diagnostic_common.py": "geo_ring_cloud.diagnostics.full_pixel_workflow",
    f"{CORE_CODE_PREFIX}stage1_common.py": "geo_ring_cloud.pipeline_support",
}
COMPONENT_COMPATIBILITY_ENTRYPOINTS = {
    f"{CORE_CODE_PREFIX}rebuild_stage1_evidence_pack.py": (
        "geo_ring_cloud.evidence_pack",
        "evidence_pack_builder",
    ),
}
POWERSHELL_COMPATIBILITY_ENTRYPOINTS = {
    f"{CORE_CODE_PREFIX}make_stage08_epic_group_meeting_ppt.ps1": (
        "tools/presentation/geo_ring_cloud_epic_group_meeting.ps1"
    ),
    f"{CORE_CODE_PREFIX}make_stage08_epic_group_meeting_ppt_cn.ps1": (
        "tools/presentation/geo_ring_cloud_epic_group_meeting_cn.ps1"
    ),
}
STAGE_COMPATIBILITY_ENTRYPOINTS = {
    f"{CORE_CODE_PREFIX}08k_consolidate_stage08_report.py": (
        "stage_08k_reporting.stage_08k_consolidate_report",
        "stage_08k",
    ),
    f"{CORE_CODE_PREFIX}09_stage09_epic_georing_cloud_mask_diagnostics.py": (
        "stage_09.stage_09_run_epic_geo_ring_cloud_mask_diagnostics",
        "stage_09",
    ),
    f"{CORE_CODE_PREFIX}06c_geometry_parameter_audit.py": (
        "stage_06c_geometry_audit.stage_06c_geometry_parameter_audit",
        "stage_06c",
    ),
    f"{CORE_CODE_PREFIX}06c_multi_satellite_geometry_metadata_audit.py": (
        "stage_06c_geometry_audit.stage_06c_multi_satellite_geometry_metadata_audit",
        "stage_06c",
    ),
    f"{CORE_CODE_PREFIX}stage_06c_claas3_geometry_angle_lineage.py": (
        "stage_06c_geometry_audit.stage_06c_claas3_geometry_angle_lineage",
        "stage_06c",
    ),
    f"{CORE_CODE_PREFIX}07p_overlap_validator_hotfix.py": (
        "stage_07p_overlap_validation.stage_07p_overlap_validator",
        "stage_07p",
    ),
    f"{CORE_CODE_PREFIX}stage_07p_claas3_profile_pair_evaluation.py": (
        "stage_07p_overlap_validation.stage_07p_claas3_profile_pair_evaluation",
        "stage_07p",
    ),
    f"{CORE_CODE_PREFIX}stage09d_full_pixel_diagnostics/run_stage09d_full_pixel_diagnostics.py": (
        "stage_09d_full_pixel_diagnostics.stage_09d_run_full_pixel_diagnostics",
        "stage_09d",
    ),
    f"{CORE_CODE_PREFIX}stage09d_interpretation/analyze_geo_visible_filter.py": (
        "stage_09d_interpretation.stage_09d_analyze_geo_visible_filter",
        "stage_09d",
    ),
    f"{CORE_CODE_PREFIX}stage09d_interpretation/answer_stage09d_questions.py": (
        "stage_09d_interpretation.stage_09d_answer_questions",
        "stage_09d",
    ),
    f"{CORE_CODE_PREFIX}stage09d_interpretation/audit_meteosat_semantics_stage09d.py": (
        "stage_09d_interpretation.stage_09d_audit_meteosat_semantics",
        "stage_09d",
    ),
    f"{CORE_CODE_PREFIX}stage09d_interpretation/build_stage09d_interpretation_package.py": (
        "stage_09d_interpretation.stage_09d_build_interpretation_package",
        "stage_09d",
    ),
    f"{CORE_CODE_PREFIX}stage09b_full_overnight/run_stage09b_full_overnight.py": (
        "stage_09b_full_overnight.stage_09b_run_full_overnight",
        "stage_09b",
    ),
    f"{CORE_CODE_PREFIX}stage09c_scaled_batch/run_stage09c_scaled_batch.py": (
        "stage_09c_scaled_batch.stage_09c_run_scaled_batch",
        "stage_09c",
    ),
    f"{CORE_CODE_PREFIX}06e_full_geometry_angle_source_sync_patch.py": (
        "stage_06e_geometry_angle_sync.stage_06e_full_geometry_angle_source_sync",
        "stage_06e",
    ),
    f"{CORE_CODE_PREFIX}06e_vza_ecef_final_audit.py": (
        "stage_06e_geometry_angle_sync.stage_06e_vza_ecef_final_audit",
        "stage_06e",
    ),
    f"{CORE_CODE_PREFIX}06f_unknown_aware_data_asset_audit.py": (
        "stage_06f_data_asset_audit.stage_06f_unknown_aware_data_asset_audit",
        "stage_06f",
    ),
    f"{CORE_CODE_PREFIX}06f_reexport_with_obitype_patch.py": (
        "stage_06f_data_asset_audit.stage_06f_reexport_with_obitype_patch",
        "stage_06f",
    ),
    f"{CORE_CODE_PREFIX}06f_report_sync_patch.py": (
        "stage_06f_data_asset_audit.stage_06f_report_sync",
        "stage_06f",
    ),
}
REGISTERED_STAGE_MIGRATION_TARGETS = {
    f"{CORE_CODE_PREFIX}{module.replace('.', '/')}.py"
    for module, _ in STAGE_COMPATIBILITY_ENTRYPOINTS.values()
}
REGISTERED_STAGE_MIGRATION_TARGETS.update(
    f"{Path(path).parent.as_posix()}/__init__.py"
    for path in tuple(REGISTERED_STAGE_MIGRATION_TARGETS)
)
PACKAGE_FACADE_PATHS = {
    f"{CORE_PACKAGE_PREFIX}pipeline_support.py": "compatibility_facade",
}
LEGACY_IMPORT_MODULES = {
    "path_config",
    "stage1_common",
    "geo_ring_cloud_source_registry",
    "geo_ring_cloud_lineage",
    "geo_ring_cloud_run_discovery",
    "geo_ring_cloud_claas3_adapter",
    "geo_ring_cloud_epic_pair_diagnostics",
    "stage_09d_diagnostic_common",
}
FORBIDDEN_ACTIVE_IMPORT_MODULES = LEGACY_IMPORT_MODULES | {
    "geo_ring_cloud.pipeline_support",
}
LEGACY_IMPORT_TEST_ALLOWLIST = {
    f"{CORE_CODE_PREFIX}tests/geo_ring_cloud_test_claas3.py",
}
LEGACY_DYNAMIC_STAGE_LOADERS: set[str] = set()
WORKSPACE_INDEX_DOCS = {
    "_GEO_RING_CLOUD_WORKSPACE/stage_registry.md",
    "_GEO_RING_CLOUD_WORKSPACE/artifact_index.md",
    "_GEO_RING_CLOUD_WORKSPACE/data_product_audits.md",
}
MODIFIED_STAGE_INDEX_DOCS = {
    "_GEO_RING_CLOUD_WORKSPACE/artifact_index.md",
    "_GEO_RING_CLOUD_WORKSPACE/engineering_status.md",
    "_GEO_RING_CLOUD_WORKSPACE/code_migrations.md",
}
MODULE_REGISTRY_DOC = "_GEO_RING_CLOUD_WORKSPACE/module_registry.md"
REQUIRED_ENGINEERING_FILES = {
    ".github/workflows/geo-ring-cloud-governance.yml",
    "_GEO_RING_CLOUD_INDEX/ci_check.py",
    "_GEO_RING_CLOUD_WORKSPACE/architecture.md",
    "_GEO_RING_CLOUD_WORKSPACE/code_migrations.md",
    "_GEO_RING_CLOUD_WORKSPACE/engineering_policy.md",
    "_GEO_RING_CLOUD_WORKSPACE/engineering_status.md",
    MODULE_REGISTRY_DOC,
    "third_report/code/geo_ring_cloud_stage1/DEPENDENCIES.md",
    "third_report/code/geo_ring_cloud_stage1/environment.yml",
    "third_report/code/geo_ring_cloud_stage1/geo_ring_cloud/__init__.py",
    "third_report/code/geo_ring_cloud_stage1/pyproject.toml",
}
COMMIT_MESSAGE_TOKEN = re.compile(
    r"\b(stage_\d{2}(?:_[0-9]+|[a-z0-9]+|_[a-z0-9]+)?|geo_ring_cloud|governance|index|artifact|skill|path_config|data_audit|research_tracker)\b",
    re.IGNORECASE,
)
COMMIT_COMPONENT_ROLES = {
    "compatibility_entrypoint",
    "data_product_audit",
    "downloader",
    "evidence_pack_builder",
    "experiment_runner",
    "path_configuration",
    "presentation_builder",
    "presentation_lineage",
    "runner",
    "shared_library",
    "summary_helper",
}
COMMIT_COMPONENT_ROLE_TOKEN = re.compile(
    r"\b(?:" + "|".join(sorted(map(re.escape, COMMIT_COMPONENT_ROLES))) + r")\b",
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


def is_core_package_module(rel_path: str) -> bool:
    return normalize_path(rel_path).startswith(CORE_PACKAGE_PREFIX)


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


def component_role_in_script(rel_path: str) -> str:
    text = read_text(rel_path) or ""
    match = COMPONENT_ROLE_ASSIGNMENT.search(text)
    return match.group(1) if match else ""


def legacy_imports_in_script(rel_path: str) -> set[str]:
    text = read_text(rel_path) or ""
    try:
        tree = ast.parse(text, filename=rel_path)
    except SyntaxError:
        return set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in FORBIDDEN_ACTIVE_IMPORT_MODULES:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(
                alias.name for alias in node.names if alias.name in FORBIDDEN_ACTIVE_IMPORT_MODULES
            )
    return modules


def check_naming(paths: list[str], added_paths: set[str], baseline_mode: bool) -> list[Finding]:
    findings: list[Finding] = []
    for rel_path in paths:
        normalized = normalize_path(rel_path)
        if not (ROOT / normalized).is_file():
            continue
        if ".git/" in normalized:
            continue
        if (
            normalized in COMPATIBILITY_SHIM_PATHS
            or normalized in COMPONENT_COMPATIBILITY_ENTRYPOINTS
            or normalized in POWERSHELL_COMPATIBILITY_ENTRYPOINTS
            or normalized in STAGE_COMPATIBILITY_ENTRYPOINTS
        ):
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
    added_stage_scripts = False
    added_package_modules: list[str] = []
    normalized_paths = {normalize_path(p) for p in paths}
    normalized_added = {normalize_path(p) for p in added_paths}

    for rel_path in paths:
        normalized = normalize_path(rel_path)
        if not (ROOT / normalized).is_file():
            continue
        if (
            normalized in COMPATIBILITY_SHIM_PATHS
            or normalized in COMPONENT_COMPATIBILITY_ENTRYPOINTS
            or normalized in POWERSHELL_COMPATIBILITY_ENTRYPOINTS
            or normalized in STAGE_COMPATIBILITY_ENTRYPOINTS
        ):
            continue
        if not is_core_code(normalized):
            continue
        suffix = Path(normalized).suffix.lower()
        if suffix not in CODE_AND_CONFIG_EXTENSIONS:
            continue

        if (
            enforce_index_docs
            and suffix == ".py"
            and normalized not in LEGACY_IMPORT_TEST_ALLOWLIST
        ):
            for module in sorted(legacy_imports_in_script(rel_path)):
                findings.append(
                    Finding(
                        "ERROR",
                        rel_path,
                        f"staged code must import a focused canonical module, not compatibility boundary: {module}",
                    )
                )

        if suffix == ".py" and is_stage_owned_path(normalized):
            changed_stage_scripts = True
            added_stage_scripts = added_stage_scripts or (
                normalized in normalized_added
                and normalized not in REGISTERED_STAGE_MIGRATION_TARGETS
            )
        if suffix == ".py" and is_core_package_module(normalized) and normalized in normalized_added:
            added_package_modules.append(normalized)

        if normalized not in normalized_added or suffix != ".py":
            continue

        if is_core_package_module(normalized):
            name = Path(normalized).name
            if name == "__init__.py":
                continue
            if not PYTHON_MODULE_FILENAME.fullmatch(name):
                findings.append(
                    Finding(
                        "ERROR",
                        rel_path,
                        "package modules must use lowercase snake_case.py names",
                    )
                )
            elif not component_role_in_script(rel_path):
                findings.append(
                    Finding(
                        "ERROR",
                        rel_path,
                        "new geo_ring_cloud package modules must declare COMPONENT_ROLE",
                    )
                )
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
        else:
            if not Path(normalized).name.startswith("geo_ring_cloud_"):
                findings.append(
                    Finding(
                        "ERROR",
                        rel_path,
                        "new non-stage core utilities must use a geo_ring_cloud_<role>_<purpose>.py filename",
                    )
                )
            elif not component_role_in_script(rel_path):
                findings.append(
                    Finding(
                        "ERROR",
                        rel_path,
                        "new non-stage core utilities must declare COMPONENT_ROLE",
                    )
                )

    if changed_stage_scripts and enforce_index_docs:
        if added_stage_scripts:
            for doc in sorted(WORKSPACE_INDEX_DOCS - normalized_paths):
                findings.append(
                    Finding(
                        "ERROR",
                        doc,
                        "new stage code added; run build_index.py and stage the workspace index Markdown",
                    )
                )
        elif not MODIFIED_STAGE_INDEX_DOCS.intersection(normalized_paths):
            findings.append(
                Finding(
                    "ERROR",
                    "_GEO_RING_CLOUD_WORKSPACE",
                    "stage code changed; run build_index.py and stage artifact_index.md or engineering_status.md",
                )
            )
    if added_package_modules:
        registry_text = read_text(MODULE_REGISTRY_DOC) or ""
        for module_path in added_package_modules:
            rel_module = module_path.removeprefix(CORE_PACKAGE_PREFIX).removesuffix(".py")
            if Path(rel_module).name == "__init__":
                continue
            canonical_module = "geo_ring_cloud." + rel_module.replace("/", ".")
            if canonical_module not in registry_text:
                findings.append(
                    Finding(
                        "ERROR",
                        module_path,
                        f"new package module must be registered in {MODULE_REGISTRY_DOC}: {canonical_module}",
                    )
                )
        if enforce_index_docs and MODULE_REGISTRY_DOC not in normalized_paths:
            findings.append(
                Finding(
                    "ERROR",
                    MODULE_REGISTRY_DOC,
                    "new package module added; run build_index.py and stage the module registry",
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


def check_engineering_contract() -> list[Finding]:
    findings: list[Finding] = []
    for rel_path in sorted(REQUIRED_ENGINEERING_FILES):
        if not (ROOT / rel_path).is_file():
            findings.append(
                Finding(
                    "ERROR",
                    rel_path,
                    "required engineering contract file is missing",
                )
            )
    return findings


def check_compatibility_shims() -> list[Finding]:
    findings: list[Finding] = []
    allowed_node_types = (ast.Expr, ast.ImportFrom, ast.Assign)
    for rel_path, canonical_module in sorted(COMPATIBILITY_SHIM_PATHS.items()):
        canonical_path = f"{CORE_CODE_PREFIX}{canonical_module.replace('.', '/')}.py"
        if not (ROOT / canonical_path).is_file():
            findings.append(Finding("ERROR", canonical_path, "registered canonical package module is missing"))
        text = read_text(rel_path)
        if text is None:
            findings.append(Finding("ERROR", rel_path, "registered compatibility shim is missing or unreadable"))
            continue
        try:
            tree = ast.parse(text, filename=rel_path)
        except SyntaxError as exc:
            findings.append(Finding("ERROR", rel_path, f"compatibility shim is not valid Python: {exc}"))
            continue
        if component_role_in_script(rel_path) != "compatibility_shim":
            findings.append(Finding("ERROR", rel_path, "compatibility shim must declare COMPONENT_ROLE='compatibility_shim'"))
        imports_canonical = False
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                continue
            if not isinstance(node, allowed_node_types):
                findings.append(Finding("ERROR", rel_path, "compatibility shim contains implementation logic"))
                break
            if isinstance(node, ast.ImportFrom):
                imports_canonical = imports_canonical or node.module == canonical_module
                continue
            if isinstance(node, ast.Assign):
                targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
                if targets != ["COMPONENT_ROLE"]:
                    findings.append(Finding("ERROR", rel_path, "compatibility shim may only assign COMPONENT_ROLE"))
                    break
        if not imports_canonical:
            findings.append(Finding("ERROR", rel_path, f"compatibility shim must import {canonical_module}"))
    return findings


def check_stage_compatibility_entrypoints() -> list[Finding]:
    """Keep migrated historical stage paths executable but implementation-free."""
    findings: list[Finding] = []
    for rel_path, (canonical_module, expected_stage_id) in sorted(
        STAGE_COMPATIBILITY_ENTRYPOINTS.items()
    ):
        canonical_path = f"{CORE_CODE_PREFIX}{canonical_module.replace('.', '/')}.py"
        if not (ROOT / canonical_path).is_file():
            findings.append(Finding("ERROR", canonical_path, "registered canonical stage module is missing"))
        text = read_text(rel_path)
        if text is None:
            findings.append(
                Finding("ERROR", rel_path, "registered stage compatibility entrypoint is missing or unreadable")
            )
            continue
        try:
            tree = ast.parse(text, filename=rel_path)
        except SyntaxError as exc:
            findings.append(
                Finding("ERROR", rel_path, f"stage compatibility entrypoint is not valid Python: {exc}")
            )
            continue
        if component_role_in_script(rel_path) != "compatibility_entrypoint":
            findings.append(
                Finding(
                    "ERROR",
                    rel_path,
                    "stage compatibility entrypoint must declare COMPONENT_ROLE='compatibility_entrypoint'",
                )
            )
        if stage_ids_in_script(rel_path) != {expected_stage_id}:
            findings.append(
                Finding(
                    "ERROR",
                    rel_path,
                    f"stage compatibility entrypoint must declare STAGE_ID='{expected_stage_id}'",
                )
            )

        imports_canonical = False
        invalid = False
        for node in tree.body:
            if isinstance(node, ast.Expr):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    continue
                if ast.unparse(node) == "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))":
                    continue
                invalid = True
                break
            if isinstance(node, ast.Import):
                if [alias.name for alias in node.names] != ["sys"]:
                    invalid = True
                    break
                continue
            if isinstance(node, ast.ImportFrom):
                if node.module == "pathlib" and [alias.name for alias in node.names] == ["Path"]:
                    continue
                if node.module != canonical_module:
                    invalid = True
                    break
                imports_canonical = True
                continue
            if isinstance(node, ast.Assign):
                targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
                if targets not in (["STAGE_ID"], ["COMPONENT_ROLE"]):
                    invalid = True
                    break
                continue
            if isinstance(node, ast.If):
                is_main_guard = (
                    isinstance(node.test, ast.Compare)
                    and isinstance(node.test.left, ast.Name)
                    and node.test.left.id == "__name__"
                    and len(node.test.ops) == 1
                    and isinstance(node.test.ops[0], ast.Eq)
                    and len(node.test.comparators) == 1
                    and isinstance(node.test.comparators[0], ast.Constant)
                    and node.test.comparators[0].value == "__main__"
                    and not node.orelse
                )
                statement = node.body[0] if len(node.body) == 1 else None
                direct_main_call = (
                    isinstance(statement, ast.Expr)
                    and isinstance(statement.value, ast.Call)
                    and isinstance(statement.value.func, ast.Name)
                    and statement.value.func.id == "main"
                    and not statement.value.args
                    and not statement.value.keywords
                )
                system_exit_main_call = (
                    isinstance(statement, ast.Raise)
                    and isinstance(statement.exc, ast.Call)
                    and isinstance(statement.exc.func, ast.Name)
                    and statement.exc.func.id == "SystemExit"
                    and len(statement.exc.args) == 1
                    and not statement.exc.keywords
                    and isinstance(statement.exc.args[0], ast.Call)
                    and isinstance(statement.exc.args[0].func, ast.Name)
                    and statement.exc.args[0].func.id == "main"
                    and not statement.exc.args[0].args
                    and not statement.exc.args[0].keywords
                )
                calls_main = direct_main_call or system_exit_main_call
                if not is_main_guard or not calls_main:
                    invalid = True
                    break
                continue
            invalid = True
            break
        if invalid:
            findings.append(
                Finding("ERROR", rel_path, "stage compatibility entrypoint contains implementation logic")
            )
        if not imports_canonical:
            findings.append(
                Finding("ERROR", rel_path, f"stage compatibility entrypoint must import {canonical_module}")
            )
    return findings


def check_component_compatibility_entrypoints() -> list[Finding]:
    """Keep historical component commands executable without duplicate implementation."""
    findings: list[Finding] = []
    for rel_path, (canonical_module, canonical_role) in sorted(
        COMPONENT_COMPATIBILITY_ENTRYPOINTS.items()
    ):
        canonical_path = f"{CORE_CODE_PREFIX}{canonical_module.replace('.', '/')}.py"
        if not (ROOT / canonical_path).is_file():
            findings.append(Finding("ERROR", canonical_path, "registered canonical component is missing"))
        elif component_role_in_script(canonical_path) != canonical_role:
            findings.append(
                Finding(
                    "ERROR",
                    canonical_path,
                    f"canonical component must declare COMPONENT_ROLE='{canonical_role}'",
                )
            )
        text = read_text(rel_path)
        if text is None:
            findings.append(
                Finding("ERROR", rel_path, "registered component compatibility entrypoint is missing")
            )
            continue
        try:
            tree = ast.parse(text, filename=rel_path)
        except SyntaxError as exc:
            findings.append(
                Finding("ERROR", rel_path, f"component compatibility entrypoint is invalid Python: {exc}")
            )
            continue
        if component_role_in_script(rel_path) != "compatibility_entrypoint":
            findings.append(
                Finding(
                    "ERROR",
                    rel_path,
                    "component compatibility entrypoint must declare COMPONENT_ROLE='compatibility_entrypoint'",
                )
            )
        if stage_ids_in_script(rel_path):
            findings.append(
                Finding("ERROR", rel_path, "component compatibility entrypoint must not declare a stage ID")
            )

        imports_canonical = False
        invalid = False
        for node in tree.body:
            if isinstance(node, ast.Expr):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    continue
                invalid = True
                break
            if isinstance(node, ast.ImportFrom):
                if node.module != canonical_module:
                    invalid = True
                    break
                imports_canonical = True
                continue
            if isinstance(node, ast.Assign):
                targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
                if targets != ["COMPONENT_ROLE"]:
                    invalid = True
                    break
                continue
            if isinstance(node, ast.If):
                is_main_guard = (
                    isinstance(node.test, ast.Compare)
                    and isinstance(node.test.left, ast.Name)
                    and node.test.left.id == "__name__"
                    and len(node.test.ops) == 1
                    and isinstance(node.test.ops[0], ast.Eq)
                    and len(node.test.comparators) == 1
                    and isinstance(node.test.comparators[0], ast.Constant)
                    and node.test.comparators[0].value == "__main__"
                    and not node.orelse
                )
                statement = node.body[0] if len(node.body) == 1 else None
                calls_main = (
                    isinstance(statement, ast.Raise)
                    and isinstance(statement.exc, ast.Call)
                    and isinstance(statement.exc.func, ast.Name)
                    and statement.exc.func.id == "SystemExit"
                    and len(statement.exc.args) == 1
                    and isinstance(statement.exc.args[0], ast.Call)
                    and isinstance(statement.exc.args[0].func, ast.Name)
                    and statement.exc.args[0].func.id == "main"
                    and not statement.exc.args[0].args
                    and not statement.exc.args[0].keywords
                )
                if not is_main_guard or not calls_main:
                    invalid = True
                    break
                continue
            invalid = True
            break
        if invalid:
            findings.append(
                Finding("ERROR", rel_path, "component compatibility entrypoint contains implementation logic")
            )
        if not imports_canonical:
            findings.append(
                Finding("ERROR", rel_path, f"component compatibility entrypoint must import {canonical_module}")
            )
    return findings


def check_powershell_compatibility_entrypoints() -> list[Finding]:
    """Keep historical PowerShell commands as short parameter-forwarding wrappers."""
    findings: list[Finding] = []
    forbidden = re.compile(
        r"(?im)^\s*function\s|New-Object|ComObject|ConvertFrom-Json|Set-Content|Add-TextBox|PowerPoint"
    )
    for rel_path, canonical_relative in sorted(POWERSHELL_COMPATIBILITY_ENTRYPOINTS.items()):
        canonical_path = f"{CORE_CODE_PREFIX}{canonical_relative}"
        canonical_text = read_text(canonical_path)
        if canonical_text is None:
            findings.append(Finding("ERROR", canonical_path, "registered canonical PowerShell component is missing"))
        else:
            required = (
                '$COMPONENT_ROLE = "presentation_builder"',
                "geo_ring_cloud_path_configuration.ps1",
                "geo_ring_cloud_presentation_manifest.ps1",
            )
            for marker in required:
                if marker not in canonical_text:
                    findings.append(
                        Finding("ERROR", canonical_path, f"canonical presentation component missing {marker}")
                    )

        text = read_text(rel_path)
        if text is None:
            findings.append(Finding("ERROR", rel_path, "registered PowerShell compatibility entrypoint is missing"))
            continue
        nonblank_lines = [line for line in text.splitlines() if line.strip()]
        normalized_text = text.replace("/", "\\").lower()
        expected_target = canonical_relative.replace("/", "\\").lower()
        if len(nonblank_lines) > 15 or forbidden.search(text):
            findings.append(
                Finding("ERROR", rel_path, "PowerShell compatibility entrypoint contains implementation logic")
            )
        if expected_target not in normalized_text or "@psboundparameters" not in normalized_text:
            findings.append(
                Finding(
                    "ERROR",
                    rel_path,
                    f"PowerShell compatibility entrypoint must forward parameters to {canonical_relative}",
                )
            )
    return findings


def check_package_facades() -> list[Finding]:
    """Keep transitional package facades free of implementation responsibilities."""
    findings: list[Finding] = []
    allowed_node_types = (ast.Expr, ast.ImportFrom, ast.Assign, ast.AnnAssign)
    for rel_path, expected_role in sorted(PACKAGE_FACADE_PATHS.items()):
        text = read_text(rel_path)
        if text is None:
            findings.append(Finding("ERROR", rel_path, "registered package facade is missing or unreadable"))
            continue
        try:
            tree = ast.parse(text, filename=rel_path)
        except SyntaxError as exc:
            findings.append(Finding("ERROR", rel_path, f"package facade is not valid Python: {exc}"))
            continue
        if component_role_in_script(rel_path) != expected_role:
            findings.append(
                Finding("ERROR", rel_path, f"package facade must declare COMPONENT_ROLE='{expected_role}'")
            )
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                continue
            if not isinstance(node, allowed_node_types):
                findings.append(Finding("ERROR", rel_path, "package facade contains implementation logic"))
                break
    return findings


def check_package_dependency_boundaries(paths: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for rel_path in paths:
        normalized = normalize_path(rel_path)
        if not is_core_package_module(normalized):
            continue
        text = read_text(rel_path)
        if text is None:
            continue
        try:
            tree = ast.parse(text, filename=rel_path)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            module = ""
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            elif isinstance(node, ast.Import):
                module = next((alias.name for alias in node.names), "")
            if module and re.match(r"^(?:stage_?\d|run_stage_?\d|\d)", module):
                findings.append(
                    Finding("ERROR", rel_path, f"package module must not depend on stage module: {module}")
                )
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "spec_from_file_location"
            ):
                findings.append(
                    Finding("ERROR", rel_path, "package module must not dynamically load stage scripts")
                )
    return findings


def check_dynamic_stage_loading(paths: list[str], baseline_mode: bool) -> list[Finding]:
    """Reject stage-to-stage implementation imports outside the registered legacy baseline."""
    findings: list[Finding] = []
    for rel_path in paths:
        normalized = normalize_path(rel_path)
        if (
            not normalized.startswith(CORE_CODE_PREFIX)
            or normalized.startswith(CORE_PACKAGE_PREFIX)
            or Path(normalized).suffix.lower() != ".py"
        ):
            continue
        text = read_text(rel_path)
        if text is None:
            continue
        try:
            tree = ast.parse(text, filename=rel_path)
        except SyntaxError:
            continue
        has_dynamic_loader = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"spec_from_file_location", "SourceFileLoader"}
            for node in ast.walk(tree)
        )
        if not has_dynamic_loader:
            continue
        is_legacy = normalized in LEGACY_DYNAMIC_STAGE_LOADERS
        severity = "WARN" if baseline_mode or is_legacy else "ERROR"
        message = (
            "legacy dynamic stage-script loading; migrate callable logic to a registered "
            "geo_ring_cloud package module"
            if is_legacy
            else "stage scripts must not dynamically load other stage scripts; use a registered package API"
        )
        findings.append(Finding(severity, rel_path, message))
    return findings


def check_python_structure(paths: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for rel_path in paths:
        if Path(rel_path).suffix.lower() != ".py":
            continue
        text = read_text(rel_path)
        if text is None:
            continue
        try:
            tree = ast.parse(text, filename=rel_path)
        except SyntaxError:
            continue
        names: dict[str, int] = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names[node.name] = names.get(node.name, 0) + 1
        duplicates = sorted(name for name, count in names.items() if count > 1)
        if duplicates:
            findings.append(
                Finding(
                    "ERROR",
                    rel_path,
                    "duplicate top-level function definitions: " + ", ".join(duplicates),
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

        enforce_absolute_local_paths = normalized.startswith(ABSOLUTE_LOCAL_PATH_ENFORCED_PREFIXES)
        if (
            suffix in CODE_AND_CONFIG_EXTENSIONS
            and enforce_absolute_local_paths
            and normalized not in ABSOLUTE_PATH_ALLOWLIST
        ):
            if ABSOLUTE_LOCAL_PATH.search(text):
                findings.append(
                    Finding(
                        "WARN" if baseline_mode else "ERROR",
                        rel_path,
                        "contains a machine-local absolute path; use geo_ring_cloud.paths or an environment override",
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
    if COMMIT_MESSAGE_TOKEN.search(first_line) or COMMIT_COMPONENT_ROLE_TOKEN.search(first_line):
        return []
    return [
        Finding(
            "ERROR",
            str(path),
            "commit message must name a canonical stage ID or component role such as stage_10p2 or geo_ring_cloud governance",
        )
    ]


def print_findings(
    findings: list[Finding],
    strict: bool = False,
    quiet_warnings: bool = False,
) -> int:
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
    visible_findings = errors if quiet_warnings else findings
    for item in visible_findings:
        print(f"[{item.severity}] {item.path}: {item.message}")

    print(f"\nSummary: {len(errors)} error(s), {len(warnings)} warning(s)")
    if quiet_warnings and warnings and not errors:
        print("Historical warnings suppressed; run governance_check.py --all for details.")
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="check staged files for pre-commit use")
    parser.add_argument("--all", action="store_true", help="check tracked and untracked non-ignored files")
    parser.add_argument("--strict", action="store_true", help="treat historical warnings as errors")
    parser.add_argument("--quiet-warnings", action="store_true", help="show warning totals without listing each warning")
    parser.add_argument("--commit-msg", type=Path, help="check a Git commit message file")
    args = parser.parse_args()

    if args.commit_msg:
        return print_findings(
            check_commit_message(args.commit_msg),
            strict=args.strict,
            quiet_warnings=args.quiet_warnings,
        )

    baseline_mode = not has_head()
    if args.staged:
        paths, added = staged_paths()
    else:
        paths, added = all_repo_candidate_paths()
        if baseline_mode:
            added = set()

    findings: list[Finding] = []
    findings.extend(check_engineering_contract())
    findings.extend(check_compatibility_shims())
    findings.extend(check_component_compatibility_entrypoints())
    findings.extend(check_powershell_compatibility_entrypoints())
    findings.extend(check_stage_compatibility_entrypoints())
    findings.extend(check_package_facades())
    findings.extend(check_package_dependency_boundaries(paths))
    findings.extend(check_dynamic_stage_loading(paths, baseline_mode=baseline_mode))
    findings.extend(check_python_structure(paths))
    findings.extend(check_naming(paths, added, baseline_mode=baseline_mode))
    findings.extend(check_stage_contract(paths, added, enforce_index_docs=args.staged))
    if args.staged:
        findings.extend(check_generated_artifacts(paths))
    findings.extend(check_paths(paths, added, baseline_mode=baseline_mode))
    return print_findings(
        findings,
        strict=args.strict,
        quiet_warnings=args.quiet_warnings,
    )


if __name__ == "__main__":
    sys.exit(main())
