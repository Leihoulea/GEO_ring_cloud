"""Compatibility entrypoint for the canonical Stage 06c CLAAS-3 lineage gate."""

from stage_06c_geometry_audit.stage_06c_claas3_geometry_angle_lineage import *


STAGE_ID = "stage_06c"
COMPONENT_ROLE = "compatibility_entrypoint"


if __name__ == "__main__":
    raise SystemExit(main())
