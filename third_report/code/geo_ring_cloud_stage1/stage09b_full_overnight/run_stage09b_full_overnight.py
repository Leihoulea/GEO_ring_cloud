"""Compatibility entrypoint for the canonical Stage 09b overnight runner."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stage_09b_full_overnight.stage_09b_run_full_overnight import *


STAGE_ID = "stage_09b"
COMPONENT_ROLE = "compatibility_entrypoint"


if __name__ == "__main__":
    raise SystemExit(main())
