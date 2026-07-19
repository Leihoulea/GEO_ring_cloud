"""Compatibility entrypoint for the canonical Stage 06c multi-satellite audit."""

from stage_06c_geometry_audit.stage_06c_multi_satellite_geometry_metadata_audit import *


STAGE_ID = "stage_06c"
COMPONENT_ROLE = "compatibility_entrypoint"


if __name__ == "__main__":
    raise SystemExit(main())
