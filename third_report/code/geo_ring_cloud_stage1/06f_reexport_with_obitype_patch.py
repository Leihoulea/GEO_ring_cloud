"""Compatibility entrypoint for the canonical Stage 06f re-export command."""

from stage_06f_data_asset_audit.stage_06f_reexport_with_obitype_patch import *


STAGE_ID = "stage_06f"
COMPONENT_ROLE = "compatibility_entrypoint"


if __name__ == "__main__":
    raise SystemExit(main())
