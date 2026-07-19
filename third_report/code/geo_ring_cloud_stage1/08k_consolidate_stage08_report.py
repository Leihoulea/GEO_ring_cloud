"""Compatibility entrypoint for the canonical Stage 08k report builder."""

from stage_08k_reporting.stage_08k_consolidate_report import *


STAGE_ID = "stage_08k"
COMPONENT_ROLE = "compatibility_entrypoint"


if __name__ == "__main__":
    raise SystemExit(main())
