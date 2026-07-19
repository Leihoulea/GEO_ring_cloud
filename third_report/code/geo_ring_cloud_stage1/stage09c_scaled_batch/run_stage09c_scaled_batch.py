"""Compatibility entrypoint for the canonical Stage 09c scaled-batch runner."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stage_09c_scaled_batch.stage_09c_run_scaled_batch import *


STAGE_ID = "stage_09c"
COMPONENT_ROLE = "compatibility_entrypoint"


if __name__ == "__main__":
    raise SystemExit(main())
