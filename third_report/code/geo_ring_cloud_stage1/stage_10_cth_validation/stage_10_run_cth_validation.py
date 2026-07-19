"""Canonical Stage 10 entrypoint for the shared CTH-validation workflow."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from geo_ring_cloud.diagnostics.cth_validation import *

STAGE_ID = "stage_10"
COMPONENT_ROLE = "compatibility_entrypoint"


if __name__ == "__main__":
    raise SystemExit(main())
