from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stage_06f_data_asset_audit.stage_06f_unknown_aware_data_asset_audit import (
    reexport_with_obitype_patch,
)


STAGE_ID = "stage_06f"


def main() -> int:
    return reexport_with_obitype_patch()


if __name__ == "__main__":
    raise SystemExit(main())
