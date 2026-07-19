"""Compatibility entrypoint for the canonical Stage 06f report sync."""

from stage_06f_data_asset_audit.stage_06f_report_sync import *


STAGE_ID = "stage_06f"
COMPONENT_ROLE = "compatibility_entrypoint"


if __name__ == "__main__":
    main()
