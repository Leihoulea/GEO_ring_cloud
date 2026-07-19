"""Compatibility entrypoint for the canonical Stage 07p overlap validator."""

from stage_07p_overlap_validation.stage_07p_overlap_validator import *


STAGE_ID = "stage_07p"
COMPONENT_ROLE = "compatibility_entrypoint"


if __name__ == "__main__":
    raise SystemExit(main())
