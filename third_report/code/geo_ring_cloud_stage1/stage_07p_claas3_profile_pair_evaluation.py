"""Compatibility entrypoint for the canonical Stage 07p CLAAS-3 profile-pair gate."""

from stage_07p_overlap_validation.stage_07p_claas3_profile_pair_evaluation import *


STAGE_ID = "stage_07p"
COMPONENT_ROLE = "compatibility_entrypoint"


if __name__ == "__main__":
    raise SystemExit(main())
