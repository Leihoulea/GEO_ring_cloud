from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from scipy.ndimage import binary_erosion

from geo_ring_cloud import paths as path_config
from geo_ring_cloud.lineage import write_manifest


STAGE_ID = "stage_07p"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_layer(path: Path) -> tuple[np.ndarray, np.ndarray]:
    with np.load(path, allow_pickle=False) as z:
        data = np.asarray(z["data"])
        valid = np.asarray(z["fusion_valid_mask"] if "fusion_valid_mask" in z.files else z["valid_mask"]).astype(bool)
    return data, valid


def find_layer(root: Path, source: str, product: str, variable: str, tag: str) -> Path:
    expected = root / "reprojected_grid" / source / f"{source}_{product}_{variable}_grid_{tag}.npz"
    if expected.exists():
        return expected
    hits = sorted((root / "reprojected_grid" / source).glob(f"*_{product}_{variable}_grid_{tag}.npz"))
    if not hits:
        raise FileNotFoundError(expected)
    return hits[0]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 07p operational-Meteosat versus CLAAS-3 common-domain diagnostics")
    parser.add_argument("--matrix-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    matrix = json.loads(args.matrix_manifest.read_text(encoding="utf-8"))
    profiles = {row["source_profile"]: Path(row["profile_root"]) for row in matrix["profile_runs"]}
    baseline = profiles["operational_baseline"]
    candidate = profiles["claas3_candidate"]
    tag = matrix["common_inputs"]["time_tag"]
    output = args.output_dir or args.matrix_manifest.parent / "stage_07p_claas3_profile_pair"
    output.mkdir(parents=True, exist_ok=True)

    op_mask, op_mask_valid = load_layer(find_layer(baseline, "Meteosat-0deg", "CLM", "cloud_mask", tag))
    cl_mask, cl_mask_valid = load_layer(find_layer(candidate, "CLAAS3-0deg", "CMA", "cloud_mask", tag))
    common_mask = op_mask_valid & cl_mask_valid
    op_binary = np.isin(op_mask, [2])
    cl_binary = np.isin(cl_mask, [3])
    agree = op_binary == cl_binary
    tp = int(np.count_nonzero(common_mask & op_binary & cl_binary))
    fp = int(np.count_nonzero(common_mask & ~op_binary & cl_binary))
    fn = int(np.count_nonzero(common_mask & op_binary & ~cl_binary))
    precision = tp / (tp + fp) if tp + fp else float("nan")
    recall = tp / (tp + fn) if tp + fn else float("nan")
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else float("nan")

    op_cth, op_cth_valid = load_layer(find_layer(baseline, "Meteosat-0deg", "CTH", "cloud_top_height_km", tag))
    cl_cth, cl_cth_valid = load_layer(find_layer(candidate, "CLAAS3-0deg", "CTX", "cloud_top_height_km", tag))
    common_cth = op_cth_valid & cl_cth_valid & np.isfinite(op_cth) & np.isfinite(cl_cth)
    delta = cl_cth.astype(np.float32) - op_cth.astype(np.float32)
    interior = binary_erosion(common_cth, iterations=2)
    edge = common_cth & ~interior

    rows: list[dict[str, Any]] = [{
        "variable": "cloud_mask",
        "stratum": "all_common",
        "common_pixel_count": int(np.count_nonzero(common_mask)),
        "operational_coverage": float(np.mean(op_mask_valid)),
        "claas3_coverage": float(np.mean(cl_mask_valid)),
        "common_agreement": float(np.mean(agree[common_mask])) if np.any(common_mask) else float("nan"),
        "precision_claas3_relative_to_operational": precision,
        "recall_claas3_relative_to_operational": recall,
        "f1_claas3_relative_to_operational": f1,
        "interpretation": "product consistency, not truth accuracy",
    }]
    for label, domain in (("all_common", common_cth), ("internal", interior), ("valid_edge", edge)):
        values = delta[domain]
        rows.append({
            "variable": "cloud_top_height_km",
            "stratum": label,
            "common_pixel_count": int(values.size),
            "mean_claas3_minus_operational_km": float(np.mean(values)) if values.size else float("nan"),
            "median_claas3_minus_operational_km": float(np.median(values)) if values.size else float("nan"),
            "mae_km": float(np.mean(np.abs(values))) if values.size else float("nan"),
            "rmse_km": float(np.sqrt(np.mean(values * values))) if values.size else float("nan"),
            "p95_abs_difference_km": float(np.percentile(np.abs(values), 95)) if values.size else float("nan"),
            "interpretation": "product difference, not CTH truth error",
        })
    for lo, hi in ((0, 3), (3, 7), (7, 12), (12, 25)):
        domain = common_cth & (op_cth >= lo) & (op_cth < hi)
        values = delta[domain]
        rows.append({
            "variable": "cloud_top_height_km",
            "stratum": f"operational_height_{lo}_{hi}_km",
            "common_pixel_count": int(values.size),
            "mean_claas3_minus_operational_km": float(np.mean(values)) if values.size else float("nan"),
            "mae_km": float(np.mean(np.abs(values))) if values.size else float("nan"),
            "interpretation": "height-stratified product difference",
        })
    metrics_path = output / "stage_07p_claas3_operational_common_domain_metrics.csv"
    write_csv(metrics_path, rows)
    limitations = [
        {"state": "day_night", "status": "UNRESOLVED_IF_NO_COMMON_SZA_LAYER"},
        {"state": "land_sea", "status": "UNRESOLVED_IF_NO_COMMON_SURFACE_MASK"},
        {"state": "cloud_phase", "status": "DIAGNOSTIC_ONLY_CLAAS3_CPP_HAS_NO_MATCHED_OPERATIONAL_METEOSAT_PHASE"},
        {"state": "QA", "status": "CLAAS3_FUSION_MASK_APPLIED_OPERATIONAL_QA_NOT_HARMONIZED"},
    ]
    limitations_path = output / "stage_07p_claas3_state_availability.csv"
    write_csv(limitations_path, limitations)
    report = output / "stage_07p_claas3_profile_pair_report.md"
    report.write_text(
        "\n".join([
            "# Stage 07p CLAAS-3 profile-pair evaluation",
            "",
            f"- Generated UTC: {utc_now()}",
            f"- Target time: `{matrix['common_inputs']['target_time']}`",
            "- CLM-CMA and CTH-CTX are compared on the same target grid and common valid domain.",
            "- All values describe agreement, coverage, and boundary behavior. They are not truth errors.",
            "- Unavailable state controls remain explicitly unresolved rather than silently pooled.",
        ]),
        encoding="utf-8",
    )
    write_manifest(
        output / "stage_07p_claas3_profile_pair_manifest.json",
        canonical_stage_id="stage_07p",
        run_id=matrix["run_id"],
        source_profile="profile_pair",
        generating_script=Path(__file__),
        input_paths=[args.matrix_manifest],
        output_paths=[metrics_path, limitations_path, report],
        parameters={"common_grid": True, "common_valid_domain": True, "nearest_reprojection_held_fixed": True},
        project_root=path_config.PROJECT_ROOT,
        extra={"registry_version": matrix["source_registry_version"], "product_versions": {"CLAAS3": "405"}},
    )
    print(f"stage_07p OK: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
