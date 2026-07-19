from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

from geo_ring_cloud import paths as path_config
from geo_ring_cloud.adapters.cloud_products import STANDARD_VARS
from geo_ring_cloud.lineage import utc_now, write_manifest
from geo_ring_cloud.pipeline_layout import (
    NATIVE_DIR,
    QUICKLOOK_DIR,
    REPORT_DIR,
    SCRIPT_DIR,
    ensure_pipeline_directories as ensure_dirs,
)
from geo_ring_cloud.sources import REGISTRY_VERSION, validate_profile
from geo_ring_cloud.diagnostics.summary import finite_stats


REQUIRED_BY_GROUP = {
    "FY4B": [
        "cloud_mask",
        "cloud_phase",
        "cloud_type",
        "cloud_top_height_km",
        "cloud_top_temperature_K",
        "cloud_top_pressure_hPa",
        "sensor_zenith_angle",
        "sensor_azimuth_angle",
        "solar_zenith_angle",
        "solar_azimuth_angle",
    ],
    "GOES-16": [
        "cloud_mask",
        "cloud_top_height_km",
        "cloud_top_temperature_K",
        "cloud_top_pressure_hPa",
        "cloud_phase",
        "cloud_optical_thickness",
        "cloud_effective_radius_um",
        "projection_x",
        "projection_y",
    ],
    "GOES-18": [
        "cloud_mask",
        "cloud_top_height_km",
        "cloud_top_temperature_K",
        "cloud_top_pressure_hPa",
        "cloud_phase",
        "cloud_optical_thickness",
        "cloud_effective_radius_um",
        "projection_x",
        "projection_y",
    ],
    "Himawari-9": [
        "cloud_mask",
        "cloud_probability",
        "cloud_top_height_km",
        "cloud_top_temperature_K",
        "cloud_top_pressure_hPa",
        "cloud_optical_thickness",
        "latitude",
        "longitude",
    ],
    "Meteosat-0deg": ["cloud_mask", "cloud_top_height_km"],
    "Meteosat-IODC": ["cloud_mask", "cloud_top_height_km"],
    "CLAAS3-0deg": [
        "cloud_mask", "cloud_probability", "cloud_top_height_km", "cloud_top_temperature_K",
        "cloud_top_pressure_hPa", "cloud_phase", "cloud_optical_thickness",
        "cloud_effective_radius_um", "cloud_water_path_g_m2", "projection_x", "projection_y",
    ],
}


def load_npz_inventory() -> pd.DataFrame:
    inv_path = NATIVE_DIR / "standardized_native_inventory.csv"
    if not inv_path.exists():
        raise RuntimeError(f"Missing inventory: {inv_path}")
    return pd.read_csv(inv_path)


def read_availability(path: Path) -> tuple[dict[str, bool], dict[str, object], list[dict[str, object]]]:
    stats_rows: list[dict[str, object]] = []
    with np.load(path, allow_pickle=False) as npz:
        metadata = json.loads(str(npz["metadata_json"]))
        availability = json.loads(str(npz["variable_availability_json"]))
        for name in npz.files:
            if name.endswith("_json") or name == "variable_availability":
                continue
            stats = finite_stats(np.asarray(npz[name]))
            stats["variable"] = name
            stats["npz_file"] = str(path)
            stats["available"] = bool(availability.get(f"has_{name}", False))
            stats_rows.append(stats)
    return availability, metadata, stats_rows


def validate(mode: str, source_profile: str) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    inv = load_npz_inventory()
    rows: list[dict[str, object]] = []
    stats_rows: list[dict[str, object]] = []
    group_available: dict[str, set[str]] = {}
    group_warnings: dict[str, list[str]] = {}
    semantic_failures: dict[str, list[str]] = {}
    for _, item in inv.iterrows():
        npz_path = Path(str(item["npz_file"]))
        satellite_group = str(item["satellite_group"])
        product = str(item["product"])
        try:
            availability, metadata, stats = read_availability(npz_path)
            available_vars = {k.replace("has_", "") for k, v in availability.items() if v}
            group_available.setdefault(satellite_group, set()).update(available_vars)
            group_warnings.setdefault(satellite_group, []).extend(metadata.get("warnings", []))
            status = "OK"
            reason = ""
            stats_rows.extend(
                {
                    **row,
                    "satellite_group": satellite_group,
                    "product": product,
                    "nominal_time": item["nominal_time"],
                }
                for row in stats
            )
            if satellite_group == "CLAAS3-0deg":
                with np.load(npz_path, allow_pickle=False) as npz:
                    science = [name for name in npz.files if availability.get(f"has_{name}", False) and np.asarray(npz[name]).ndim == 2]
                    for variable in science:
                        for prefix in ("physical_valid_mask_", "fusion_valid_mask_", "diagnostic_valid_mask_"):
                            mask_name = f"{prefix}{variable}"
                            if mask_name not in npz.files or np.asarray(npz[mask_name]).shape != np.asarray(npz[variable]).shape:
                                semantic_failures.setdefault(satellite_group, []).append(f"{product}:{mask_name} missing or shape mismatch")
                    if metadata.get("scale_offset_policy", "").count("exactly once") != 1:
                        semantic_failures.setdefault(satellite_group, []).append(f"{product}: scaling-once provenance missing")
        except Exception as exc:
            status = "FAILED_OPEN"
            reason = str(exc)
        rows.append(
            {
                "nominal_time": item["nominal_time"],
                "satellite_group": satellite_group,
                "product": product,
                "npz_file": str(npz_path),
                "status": status,
                "reason": reason,
            }
        )
    group_status: dict[str, object] = {}
    any_fail = False
    any_warn = False
    for group, required in REQUIRED_BY_GROUP.items():
        if group == "CLAAS3-0deg" and source_profile != "claas3_candidate":
            continue
        have = group_available.get(group, set())
        missing = [name for name in required if name not in have]
        if missing or semantic_failures.get(group):
            any_fail = True
            status = "FAIL"
        elif group_warnings.get(group):
            any_warn = True
            status = "PASS_WITH_WARNINGS"
        else:
            status = "PASS"
        group_status[group] = {
            "status": status,
            "available_variables": sorted(have),
            "missing_required": missing,
            "warnings": group_warnings.get(group, []),
            "semantic_failures": semantic_failures.get(group, []),
        }
    overall = "FAIL" if any_fail else ("PASS_WITH_WARNINGS" if any_warn else "PASS")
    summary = {"mode": mode, "source_profile": source_profile, "overall_status": overall, "group_status": group_status}
    return pd.DataFrame(rows), pd.DataFrame(stats_rows), summary


def write_report(validate_rows: pd.DataFrame, stats: pd.DataFrame, summary: dict[str, object]) -> None:
    lines = [
        "# Standardized Native Validate Report",
        "",
        f"- Generated UTC: {utc_now()}",
        f"- Overall status: **{summary['overall_status']}**",
        "- Scope: 01-03 prototype only; no reprojection or fusion.",
        "",
        "## Satellite Group Status",
        "",
    ]
    for group, info in summary["group_status"].items():
        lines.append(f"- {group}: **{info['status']}**")
        if info["missing_required"]:
            lines.append(f"  - Missing required: {', '.join(info['missing_required'])}")
        lines.append(f"  - Available: {', '.join(info['available_variables']) or 'none'}")
        if info["warnings"]:
            lines.append(f"  - Reader warnings: {' | '.join(map(str, info['warnings'][:5]))}")
    lines.extend(["", "## Product Files", ""])
    for _, row in validate_rows.iterrows():
        lines.append(f"- {row['satellite_group']} {row['product']}: {row['status']} `{row['npz_file']}`")
    lines.extend(
        [
            "",
            "## Gate To 04",
            "",
            "- Enter 04 FY4B GEO alignment only if the overall status is not FAIL and FY4B has angle fields attached to cloud products.",
            "- If this report is FAIL, inspect missing_required before running reprojection.",
        ]
    )
    (REPORT_DIR / "standardized_native_validate_report.md").write_text("\n".join(lines), encoding="utf-8")
    quicklook_count = len(list(QUICKLOOK_DIR.glob("*.png"))) if QUICKLOOK_DIR.exists() else 0
    groups_ok = [group for group, info in summary["group_status"].items() if info["status"] == "PASS"]
    next_lines = [
        "# GEO-ring Cloud Next Stage Report",
        "",
        f"- Generated UTC: {utc_now()}",
        "- Scope completed in this run: 01 core time index, 02 native-grid standardization, 03 native validation.",
        "- Scope not run yet: 04 FY4B GEO alignment, 05 reprojection, 06 fusion, 07 overlap validation.",
        f"- Overall 01-03 status: **{summary['overall_status']}**",
        "",
        "## 1. 是否完成单时次标准化",
        "",
        f"- Yes. Prototype time products validated: {len(validate_rows)} NPZ files.",
        f"- Native quicklooks generated: {quicklook_count} PNG files.",
        "",
        "## 2. FY4B GEO 是否通过同格验证",
        "",
        "- Not tested yet. This belongs to step 04.",
        "- Current prerequisite is satisfied: FY4B native standardization includes cloud variables and GEO angle fields.",
        "",
        "## 3. 哪些卫星成功重投影",
        "",
        "- Not tested yet. Reprojection belongs to step 05.",
        "",
        "## 4. 哪些变量实现融合",
        "",
        "- Not tested yet. Best-source fusion belongs to step 06.",
        "",
        "## 5. source_map 是否合理",
        "",
        "- Not tested yet. source_map is produced by step 06.",
        "",
        "## 6. 重叠区一致性如何",
        "",
        "- Not tested yet. Overlap validation belongs to step 07.",
        "",
        "## 7. 是否可以扩展到 6 个代表时次或全月",
        "",
        "- 01-03 can now be extended to the 6 representative times.",
        "- Do not start full-month processing before 04 FY4B GEO alignment and 05 one-time reprojection pass.",
        "",
        "## Native Validation By Satellite",
        "",
    ]
    for group, info in summary["group_status"].items():
        next_lines.append(f"- {group}: **{info['status']}**; available={', '.join(info['available_variables']) or 'none'}")
    next_lines.extend(
        [
            "",
            "## Gate Decision",
            "",
            f"- 01-03 gate: **{summary['overall_status']}**.",
            "- Recommended next action: run 04 on the same prototype time only.",
        ]
    )
    (REPORT_DIR / "geo_ring_cloud_next_stage_report.md").write_text("\n".join(next_lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="prototype", choices=["prototype"])
    parser.add_argument("--source-profile", default="operational_baseline", choices=["operational_baseline", "claas3_candidate"])
    parser.add_argument("--run-id", default="")
    args = parser.parse_args()
    source_profile = validate_profile(args.source_profile)
    ensure_dirs()
    shutil.copy2(__file__, SCRIPT_DIR / Path(__file__).name)
    validate_rows, stats, summary = validate(args.mode, source_profile)
    validate_rows.to_csv(NATIVE_DIR / "standardized_native_file_validation.csv", index=False, encoding="utf-8-sig")
    stats.to_csv(NATIVE_DIR / "standardized_native_variable_stats_validated.csv", index=False, encoding="utf-8-sig")
    write_report(validate_rows, stats, summary)
    write_manifest(
        NATIVE_DIR / "stage_03_claas3_validation_manifest.json",
        canonical_stage_id="stage_03",
        run_id=args.run_id,
        source_profile=source_profile,
        generating_script=Path(__file__),
        input_paths=validate_rows["npz_file"].astype(str).tolist(),
        output_paths=[NATIVE_DIR / "standardized_native_file_validation.csv", NATIVE_DIR / "standardized_native_variable_stats_validated.csv", REPORT_DIR / "standardized_native_validate_report.md"],
        parameters={"mode": args.mode},
        project_root=path_config.PROJECT_ROOT,
        extra={"registry_version": REGISTRY_VERSION, "product_versions": {"CLAAS3": "405"} if source_profile == "claas3_candidate" else {}, "status": summary["overall_status"]},
    )
    print(f"03 {summary['overall_status']}: files={len(validate_rows)}")
    print(f"report={REPORT_DIR / 'standardized_native_validate_report.md'}")
    return 0 if summary["overall_status"] != "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
