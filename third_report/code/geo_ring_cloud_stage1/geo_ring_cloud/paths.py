"""Central path defaults and environment overrides."""

from __future__ import annotations

import os
from pathlib import Path


COMPONENT_ROLE = "path_configuration"


def env_path(name: str, default: str | Path) -> Path:
    return Path(os.environ.get(name, str(default)))


PROJECT_ROOT = env_path("GEO_RING_PROJECT_ROOT", r"D:\AAAresearch_paper")
THIRD_REPORT_ROOT = env_path("GEO_RING_THIRD_REPORT_ROOT", PROJECT_ROOT / "third_report")
CODE_ROOT = env_path("GEO_RING_CODE_ROOT", THIRD_REPORT_ROOT / "code" / "geo_ring_cloud_stage1")
EUMETSAT_CREDENTIALS_FILE = env_path(
    "GEO_RING_EUMETSAT_CREDENTIALS_FILE",
    THIRD_REPORT_ROOT / "eumetsat_dataservices_API.txt",
)

STAGE_ROOT = env_path("GEO_RING_STAGE_ROOT", PROJECT_ROOT / "geo_ring_cloud_stage1")
BASE_STAGE_ROOT = env_path("GEO_RING_BASE_STAGE_ROOT", STAGE_ROOT)
RUNS_ROOT = env_path("GEO_RING_RUNS_ROOT", PROJECT_ROOT / "geo_ring_cloud_stage1_time_runs")
EVIDENCE_ROOT = env_path("GEO_RING_EVIDENCE_ROOT", PROJECT_ROOT / "geo_ring_cloud_stage1_evidence_pack")

DATA_CHECK_ROOT = env_path("GEO_RING_DATA_CHECK_ROOT", PROJECT_ROOT / "data_check_report")
DATA_CHECK_GEOMETRY_ROOT = env_path(
    "GEO_RING_DATA_CHECK_GEOMETRY_ROOT",
    DATA_CHECK_ROOT / "geometry_variable_audit",
)
GEOMETRY_ROOT = env_path("GEO_RING_GEOMETRY_ROOT", PROJECT_ROOT / "geo_geometry_check")
DATA_ROOT = env_path("GEO_RING_DATA_ROOT", PROJECT_ROOT / "data")
HIMAWARI_R21_DIR = env_path("GEO_RING_HIMAWARI_R21_DIR", DATA_ROOT / "H09_Data")

EXTERNAL_GEO_CLOUD_ROOT = env_path("GEO_RING_EXTERNAL_GEO_CLOUD_ROOT", r"E:\GEO_Cloud_2024")
CLAAS3_ROOT = env_path("GEO_RING_CLAAS3_ROOT", EXTERNAL_GEO_CLOUD_ROOT / "CMSAF")
EXTERNAL_EPIC_L2_ROOT = env_path(
    "GEO_RING_EXTERNAL_EPIC_L2_ROOT",
    r"F:\DSCOVR_EPIC_L2_CLOUD_03_2024.03",
)
EXTERNAL_EPIC_COMPOSITE_ROOT = env_path(
    "GEO_RING_EXTERNAL_EPIC_COMPOSITE_ROOT",
    r"F:\DSCOVR_EPIC_L2_COMPOSITE_02_2024.01",
)

__all__ = [
    "BASE_STAGE_ROOT",
    "CLAAS3_ROOT",
    "CODE_ROOT",
    "DATA_CHECK_GEOMETRY_ROOT",
    "DATA_CHECK_ROOT",
    "DATA_ROOT",
    "EVIDENCE_ROOT",
    "EUMETSAT_CREDENTIALS_FILE",
    "EXTERNAL_EPIC_L2_ROOT",
    "EXTERNAL_EPIC_COMPOSITE_ROOT",
    "EXTERNAL_GEO_CLOUD_ROOT",
    "GEOMETRY_ROOT",
    "HIMAWARI_R21_DIR",
    "PROJECT_ROOT",
    "RUNS_ROOT",
    "STAGE_ROOT",
    "THIRD_REPORT_ROOT",
    "env_path",
]
