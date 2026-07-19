"""Compatibility entrypoint for the canonical Stage 09 diagnostic runner."""

from stage_09.stage_09_run_epic_geo_ring_cloud_mask_diagnostics import *


STAGE_ID = "stage_09"
COMPONENT_ROLE = "compatibility_entrypoint"


if __name__ == "__main__":
    raise SystemExit(main())
