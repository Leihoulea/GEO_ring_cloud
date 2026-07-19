from __future__ import annotations

import subprocess
import sys

from geo_ring_cloud.paths import CODE_ROOT


AUDIT_SCRIPT = CODE_ROOT / "06f_unknown_aware_data_asset_audit.py"


def main() -> int:
    completed = subprocess.run(
        [sys.executable, str(AUDIT_SCRIPT), "--reexport-with-obitype-patch"],
        cwd=str(CODE_ROOT),
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
