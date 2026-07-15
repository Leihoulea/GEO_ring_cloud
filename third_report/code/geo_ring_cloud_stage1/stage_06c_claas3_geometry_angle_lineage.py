from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

import path_config
from geo_ring_cloud_lineage import write_manifest
from geo_ring_cloud_source_registry import REGISTRY_VERSION, SOURCE_BY_KEY


STAGE_ID = "stage_06c"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_metadata(path: Path) -> dict[str, Any]:
    with np.load(path, allow_pickle=False) as z:
        return json.loads(str(z["metadata_json"]))


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 06c CLAAS-3 CF geometry and derived-angle lineage gate")
    parser.add_argument("--stage-root", type=Path, default=path_config.STAGE_ROOT)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--source-profile", default="claas3_candidate")
    args = parser.parse_args()
    native_dir = args.stage_root / "standardized_native"
    files = sorted(native_dir.glob("CLAAS3-0deg_*_native_cloud_v0.npz"))
    if not files:
        raise RuntimeError(f"no CLAAS-3 native products found under {native_dir}")
    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    for path in files:
        meta = load_metadata(path)
        projection = meta.get("geostationary_projection_attrs", {})
        required = {
            "grid_mapping_name", "perspective_point_height", "longitude_of_projection_origin",
            "semi_major_axis", "semi_minor_axis", "sweep_angle_axis",
        }
        missing = sorted(required - set(projection))
        if missing:
            failures.append(f"{path.name}: missing {missing}")
        rows.append({
            "source_key": "CLAAS3-0deg",
            "platform": meta.get("platform", "METEOSAT-10"),
            "processing_stream": meta.get("processing_stream", ""),
            "product": meta.get("product", ""),
            "native_file": str(path),
            "projection_lineage": "CF_projection_variable_plus_x_y_scan_angles",
            "grid_mapping_name": projection.get("grid_mapping_name", ""),
            "service_longitude_deg": projection.get("longitude_of_projection_origin", ""),
            "sweep_angle_axis": projection.get("sweep_angle_axis", ""),
            "angle_lineage": "NAV_DERIVED_GEOSTATIONARY_VZA",
            "official_angle_layer_available": False,
            "status": "FAIL" if missing else "PASS",
        })
    output = args.stage_root / "geometry_angle_sync_06c" / "claas3"
    output.mkdir(parents=True, exist_ok=True)
    inventory = output / "stage_06c_claas3_geometry_angle_lineage.csv"
    with inventory.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    report = output / "stage_06c_claas3_geometry_angle_lineage_report.md"
    status = "FAIL" if failures else "PASS"
    report.write_text(
        "\n".join([
            "# Stage 06c CLAAS-3 geometry and angle lineage",
            "",
            f"- Generated UTC: {utc_now()}",
            f"- Status: **{status}**",
            f"- Service longitude: `{SOURCE_BY_KEY['CLAAS3-0deg'].service_longitude_deg}` degrees",
            "- Geolocation is derived from the product CF `projection` variable and physical `x/y` scan angles.",
            "- Viewing zenith used by Stage 06 is navigation-derived. It is not represented as an official CLAAS-3 angle product.",
            *[f"- FAIL: {item}" for item in failures],
        ]),
        encoding="utf-8",
    )
    write_manifest(
        output / "stage_06c_claas3_geometry_angle_lineage_manifest.json",
        canonical_stage_id="stage_06c",
        run_id=args.run_id,
        source_profile=args.source_profile,
        generating_script=Path(__file__),
        input_paths=files,
        output_paths=[inventory, report],
        parameters={"official_angle_layer_required": False, "derived_vza_allowed": True},
        project_root=path_config.PROJECT_ROOT,
        extra={"registry_version": REGISTRY_VERSION, "product_versions": {"CLAAS3": "405"}, "status": status},
    )
    print(f"stage_06c {status}: {report}")
    return 0 if status == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
