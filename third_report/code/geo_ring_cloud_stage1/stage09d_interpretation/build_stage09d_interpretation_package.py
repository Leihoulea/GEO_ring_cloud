"""Compatibility entrypoint for the canonical Stage 09d interpretation builder."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stage_09d_interpretation.stage_09d_build_interpretation_package import *


STAGE_ID = "stage_09d"
COMPONENT_ROLE = "compatibility_entrypoint"


if __name__ == "__main__":
    raise SystemExit(main())
