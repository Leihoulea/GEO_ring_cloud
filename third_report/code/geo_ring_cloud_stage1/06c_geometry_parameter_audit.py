"""Compatibility entrypoint for the canonical Stage 06c geometry parameter audit."""

from stage_06c_geometry_audit.stage_06c_geometry_parameter_audit import *


STAGE_ID = "stage_06c"
COMPONENT_ROLE = "compatibility_entrypoint"


if __name__ == "__main__":
    raise SystemExit(main())
