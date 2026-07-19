"""Compatibility entrypoint for the canonical Stage 06f data-asset audit."""

from stage_06f_data_asset_audit.stage_06f_unknown_aware_data_asset_audit import *


STAGE_ID = "stage_06f"
COMPONENT_ROLE = "compatibility_entrypoint"


if __name__ == "__main__":
    raise SystemExit(main())
