"""Canonical filesystem layout for the Stage 1 processing workspace."""

from __future__ import annotations

from pathlib import Path

from .paths import DATA_CHECK_GEOMETRY_ROOT, DATA_CHECK_ROOT, STAGE_ROOT


COMPONENT_ROLE = "pipeline_layout"

REPORT_ROOT = DATA_CHECK_ROOT
GEOM_AUDIT_ROOT = DATA_CHECK_GEOMETRY_ROOT
PARSED_METADATA = REPORT_ROOT / "parsed_file_metadata.csv"
VARIABLE_INVENTORY = GEOM_AUDIT_ROOT / "product_variable_inventory_full.csv"
MAPPING_YAML = REPORT_ROOT / "manual_variable_mapping_by_product.yaml"

SCRIPT_DIR = STAGE_ROOT / "scripts"
CONFIG_DIR = STAGE_ROOT / "config"
TIME_INDEX_DIR = STAGE_ROOT / "time_index"
NATIVE_DIR = STAGE_ROOT / "standardized_native"
QUICKLOOK_DIR = STAGE_ROOT / "quicklooks_native"
REPORT_DIR = STAGE_ROOT / "reports"

PIPELINE_DIRECTORIES: tuple[Path, ...] = (
    STAGE_ROOT,
    SCRIPT_DIR,
    CONFIG_DIR,
    TIME_INDEX_DIR,
    NATIVE_DIR,
    QUICKLOOK_DIR,
    REPORT_DIR,
)

__all__ = [
    "REPORT_ROOT",
    "GEOM_AUDIT_ROOT",
    "PARSED_METADATA",
    "VARIABLE_INVENTORY",
    "MAPPING_YAML",
    "STAGE_ROOT",
    "SCRIPT_DIR",
    "CONFIG_DIR",
    "TIME_INDEX_DIR",
    "NATIVE_DIR",
    "QUICKLOOK_DIR",
    "REPORT_DIR",
    "PIPELINE_DIRECTORIES",
    "ensure_pipeline_directories",
]


def ensure_pipeline_directories() -> None:
    """Create the canonical Stage 1 output directories when a run starts."""
    for path in PIPELINE_DIRECTORIES:
        path.mkdir(parents=True, exist_ok=True)
