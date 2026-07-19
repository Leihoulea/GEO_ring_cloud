"""Compatibility entrypoint for canonical Stage 06e geometry-angle synchronization."""

from stage_06e_geometry_angle_sync.stage_06e_full_geometry_angle_source_sync import *


STAGE_ID = "stage_06e"
COMPONENT_ROLE = "compatibility_entrypoint"


if __name__ == "__main__":
    raise SystemExit(main())
