"""Compatibility entrypoint for the canonical Stage 09d full-pixel runner."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stage_09d_full_pixel_diagnostics.stage_09d_run_full_pixel_diagnostics import *


STAGE_ID = "stage_09d"
COMPONENT_ROLE = "compatibility_entrypoint"


if __name__ == "__main__":
    raise SystemExit(main())
