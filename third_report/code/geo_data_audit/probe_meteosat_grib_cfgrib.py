from __future__ import annotations

import tempfile
import sys
import zipfile
from pathlib import Path

import cfgrib
import numpy as np

CORE_CODE_ROOT = Path(__file__).resolve().parents[1] / "geo_ring_cloud_stage1"
if str(CORE_CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_CODE_ROOT))

from geo_ring_cloud.paths import EXTERNAL_GEO_CLOUD_ROOT  # noqa: E402


SAMPLES = [
    EXTERNAL_GEO_CLOUD_ROOT / "Meteosat-0deg" / "CLM" / "20240316" / "13" / "MSG3-SEVI-MSGCLMK-0100-0100-20240316130000.000000000Z-NA.zip",
    EXTERNAL_GEO_CLOUD_ROOT / "Meteosat-0deg" / "CTH" / "20240316" / "14" / "MSG3-SEVI-MSGCLTH-0100-0100-20240316140000.000000000Z-NA.zip",
]


def main() -> int:
    for path in SAMPLES:
        print(f"ZIP {path}")
        with zipfile.ZipFile(path) as zf:
            gribs = [n for n in zf.namelist() if n.lower().endswith((".grb", ".grib", ".grib2"))]
            print(f"members {gribs}")
            for name in gribs[:1]:
                with tempfile.NamedTemporaryFile(suffix=".grib", delete=False) as tmp:
                    tmp.write(zf.read(name))
                    tmp_path = Path(tmp.name)
                try:
                    dss = cfgrib.open_datasets(str(tmp_path), indexpath="")
                    print(f"datasets {len(dss)}")
                    for i, ds in enumerate(dss):
                        print(f"dataset {i} dims {dict(ds.sizes)} coords {list(ds.coords)} vars {list(ds.data_vars)}")
                        for v in ds.data_vars:
                            da = ds[v]
                            vals = da.values
                            print(
                                " var",
                                v,
                                da.shape,
                                da.dtype,
                                da.attrs.get("GRIB_shortName"),
                                da.attrs.get("long_name"),
                                da.attrs.get("units"),
                                "minmax",
                                float(np.nanmin(vals)),
                                float(np.nanmax(vals)),
                            )
                        ds.close()
                finally:
                    tmp_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
