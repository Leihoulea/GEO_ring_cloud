"""Compatibility entrypoint for the canonical Stage 06e VZA/ECEF audit."""

from stage_06e_geometry_angle_sync.stage_06e_vza_ecef_final_audit import *


STAGE_ID = "stage_06e"
COMPONENT_ROLE = "compatibility_entrypoint"


if __name__ == "__main__":
    raise SystemExit(main())
