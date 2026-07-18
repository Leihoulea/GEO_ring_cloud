from __future__ import annotations

import argparse
import os
import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

import path_config
from geo_ring_cloud_claas3_adapter import read_product as read_claas3_product
from geo_ring_cloud_lineage import write_manifest
from geo_ring_cloud_source_registry import REGISTRY_VERSION, validate_profile
from stage1_common import (
    CONFIG_DIR,
    CORE_PRODUCTS,
    find_himawari_r21_geometry_file,
    NATIVE_DIR,
    QUICKLOOK_DIR,
    read_himawari_r21_geometry,
    REPORT_DIR,
    SCRIPT_DIR,
    STANDARD_VARS,
    TIME_INDEX_DIR,
    ensure_dirs,
    finite_stats,
    make_quicklook,
    read_mapping,
    read_product,
    safe_name,
    utc_now,
    write_json_npz,
)


BASE_STAGE_ROOT = Path(os.environ.get("GEO_RING_BASE_STAGE_ROOT", str(path_config.BASE_STAGE_ROOT)))
BASE_TIME_INDEX_DIR = BASE_STAGE_ROOT / "time_index"


def load_selected_time(mode: str) -> str:
    ranked = pd.read_csv(BASE_TIME_INDEX_DIR / "usable_times_ranked.csv")
    if mode == "prototype":
        rows = ranked[ranked["selection_role"] == "prototype"]
        if rows.empty:
            raise RuntimeError("No prototype time found in usable_times_ranked.csv")
        return str(rows.iloc[0]["nominal_time"])
    raise ValueError(f"unsupported mode {mode}")


def file_map_for_time(nominal_time: str) -> pd.DataFrame:
    index_df = pd.read_csv(BASE_TIME_INDEX_DIR / "core_time_index.csv")
    rows = index_df[index_df["nominal_time"] == nominal_time].copy()
    if rows.empty:
        raise RuntimeError(f"No core_time_index rows for {nominal_time}")
    return rows


def load_fy4b_geo(rows: pd.DataFrame, mapping: dict[str, dict[str, list[str]]]) -> dict[str, np.ndarray]:
    fy4b = rows[rows["satellite_group"] == "FY4B"]
    if fy4b.empty:
        return {}
    files = json.loads(str(fy4b.iloc[0]["product_files_json"]))
    geo = files.get("GEO")
    if not geo:
        return {}
    result = read_product(Path(geo["file_path"]), "FY4B", "GEO", mapping)
    keep = {
        "sensor_zenith_angle",
        "sensor_azimuth_angle",
        "solar_zenith_angle",
        "solar_azimuth_angle",
        "sun_glint_angle",
        "quality_flag_raw",
    }
    return {k: v for k, v in result.arrays.items() if k in keep}


def add_missing_standard_vars(arrays: dict[str, np.ndarray]) -> dict[str, bool]:
    availability: dict[str, bool] = {}
    for name in STANDARD_VARS:
        if name == "variable_availability":
            continue
        availability[f"has_{name}"] = name in arrays and np.asarray(arrays[name]).size > 0
        if name not in arrays:
            arrays[name] = np.asarray(np.nan, dtype=np.float32)
    arrays["variable_availability"] = np.asarray(json.dumps(availability, ensure_ascii=False))
    return availability


def merge_himawari_r21_angles(
    nominal_time: str,
    product: str,
    arrays: dict[str, np.ndarray],
    mapping: dict[str, dict[str, list[str]]],
) -> list[str]:
    notes: list[str] = []
    need_sensor = any(name not in arrays for name in ["sensor_zenith_angle", "sensor_azimuth_angle"])
    need_solar = any(name not in arrays for name in ["solar_zenith_angle", "solar_azimuth_angle"])
    if not need_sensor and not need_solar:
        return notes
    aux_path, aux_info = find_himawari_r21_geometry_file(nominal_time)
    if aux_path is None:
        notes.append(f"Himawari R21_FLDK auxiliary geometry unavailable: {aux_info.get('status', 'unknown')}")
        return notes
    aux = read_himawari_r21_geometry(aux_path, mapping)
    if need_sensor:
        for key in ["sensor_zenith_angle", "sensor_azimuth_angle"]:
            if key not in arrays and key in aux.arrays:
                arrays[key] = np.asarray(aux.arrays[key])
        notes.append(
            "Borrowed Himawari sensor geometry from R21_FLDK auxiliary file "
            f"{aux_path.name} (selected_time={aux_info.get('selected_time')}, dt_minutes={aux_info.get('dt_minutes')}). "
            "This is acceptable for fixed-grid GEO viewing geometry because satellite zenith/azimuth are effectively time-invariant for a fixed satellite slot."
        )
    if need_solar:
        if aux_info.get("same_minute", False):
            for key in ["solar_zenith_angle", "solar_azimuth_angle"]:
                if key not in arrays and key in aux.arrays:
                    arrays[key] = np.asarray(aux.arrays[key])
            notes.append(
                "Borrowed Himawari solar geometry from same-minute R21_FLDK auxiliary file "
                f"{aux_path.name}."
            )
        else:
            notes.append(
                "Did not borrow Himawari solar geometry from R21_FLDK because selected auxiliary time "
                f"{aux_info.get('selected_time')} does not match target minute {nominal_time}. "
                "Solar zenith/azimuth are time-dependent and remain unset to avoid cross-time contamination."
            )
    if "latitude" not in arrays and "latitude" in aux.arrays:
        arrays["latitude"] = np.asarray(aux.arrays["latitude"])
    if "longitude" not in arrays and "longitude" in aux.arrays:
        arrays["longitude"] = np.asarray(aux.arrays["longitude"])
    return notes


def build_one_product(
    nominal_time: str,
    satellite_group: str,
    family: str,
    product: str,
    file_path: Path,
    mapping: dict[str, dict[str, list[str]]],
    fy4b_geo_arrays: dict[str, np.ndarray],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    result = read_product(file_path, family, product, mapping)
    arrays = dict(result.arrays)
    if satellite_group == "FY4B" and product != "GEO":
        for key, value in fy4b_geo_arrays.items():
            if key not in arrays:
                arrays[key] = value
    if satellite_group == "Himawari-9":
        result.warnings.extend(merge_himawari_r21_angles(nominal_time, product, arrays, mapping))
    availability = add_missing_standard_vars(arrays)
    metadata = {
        "generated_utc": utc_now(),
        "nominal_time": nominal_time,
        "satellite_group": satellite_group,
        "satellite_family": family,
        "product": product,
        "source_file": str(file_path),
        "source_variables": result.source_variables,
        "reader_attrs": result.attrs,
        "warnings": result.warnings,
        "notes": [
            "Native-grid product; no reprojection or cross-resolution merge performed.",
            "Missing variables are represented by scalar NaN and has_xxx=False.",
        ],
    }
    out_name = f"{safe_name(satellite_group)}_{safe_name(product)}_{nominal_time[0:10].replace('-', '')}_{nominal_time[11:13]}00_native_cloud_v0.npz"
    out_path = NATIVE_DIR / out_name
    write_json_npz(out_path, arrays, metadata, availability)
    quick_var = next((name for name in arrays if name in [
        "cloud_mask",
        "cloud_top_height_km",
        "cloud_top_temperature_K",
        "cloud_top_pressure_hPa",
        "cloud_probability",
        "cloud_phase",
        "cloud_type",
        "cloud_optical_thickness",
        "cloud_effective_radius_um",
        "sensor_zenith_angle",
    ] and np.asarray(arrays[name]).ndim >= 2 and availability.get(f"has_{name}", False)), None)
    if quick_var:
        qpath = QUICKLOOK_DIR / f"{out_path.stem}_{quick_var}.png"
        make_quicklook(arrays[quick_var], qpath, f"{satellite_group} {product} {quick_var} {nominal_time}", quick_var)
    inventory_row = {
        "nominal_time": nominal_time,
        "satellite_group": satellite_group,
        "satellite_family": family,
        "product": product,
        "source_file": str(file_path),
        "npz_file": str(out_path),
        "quicklook_variable": quick_var or "",
        "status": "OK" if result.arrays else "WARN_EMPTY",
        "warnings": "|".join(result.warnings),
        **availability,
    }
    stats_rows: list[dict[str, object]] = []
    for name, arr in arrays.items():
        if name in {"metadata_json", "variable_availability_json", "variable_availability"}:
            continue
        stats = finite_stats(np.asarray(arr))
        stats.update(
            {
                "nominal_time": nominal_time,
                "satellite_group": satellite_group,
                "product": product,
                "variable": name,
                "npz_file": str(out_path),
                "available": availability.get(f"has_{name}", False),
            }
        )
        stats_rows.append(stats)
    return inventory_row, stats_rows


def build_one_claas3_product(
    nominal_time: str,
    product: str,
    file_path: Path,
    source_profile: str,
    run_id: str,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    result = read_claas3_product(file_path)
    arrays = dict(result.arrays)
    availability = add_missing_standard_vars(arrays)
    metadata = {
        **result.metadata,
        "generated_utc": utc_now(),
        "run_id": run_id,
        "source_profile": source_profile,
        "notes": [
            "Native CLAAS-3 grid; no reprojection or cross-resolution merge performed.",
            "NetCDF auto scaling was disabled and scale_factor/add_offset were applied exactly once by the CLAAS-3 adapter.",
            "Physical, fusion, and diagnostic validity are stored independently for each science variable.",
        ],
    }
    out_name = f"CLAAS3-0deg_{product}_{nominal_time[0:10].replace('-', '')}_{nominal_time[11:13]}00_native_cloud_v0.npz"
    out_path = NATIVE_DIR / out_name
    write_json_npz(out_path, arrays, metadata, availability)
    quick_var = next((name for name in [
        "cloud_mask", "cloud_top_height_km", "cloud_phase", "cloud_optical_thickness",
    ] if availability.get(f"has_{name}", False)), None)
    if quick_var:
        make_quicklook(arrays[quick_var], QUICKLOOK_DIR / f"{out_path.stem}_{quick_var}.png", f"CLAAS3-0deg {product} {quick_var} {nominal_time}", quick_var)
    inventory_row = {
        "nominal_time": nominal_time,
        "satellite_group": "CLAAS3-0deg",
        "satellite_family": "CLAAS3",
        "source_key": "CLAAS3-0deg",
        "processing_stream": result.metadata["processing_stream"],
        "product_version": result.metadata["product_version"],
        "profile_eligibility": "claas3_candidate",
        "product": product,
        "source_file": str(file_path),
        "npz_file": str(out_path),
        "quicklook_variable": quick_var or "",
        "status": "OK" if result.arrays else "WARN_EMPTY",
        "warnings": "|".join(result.warnings),
        **availability,
    }
    stats_rows: list[dict[str, object]] = []
    for name, arr in arrays.items():
        if name in {"metadata_json", "variable_availability_json", "variable_availability", "geostationary_projection"}:
            continue
        stats = finite_stats(np.asarray(arr))
        stats.update({
            "nominal_time": nominal_time,
            "satellite_group": "CLAAS3-0deg",
            "product": product,
            "variable": name,
            "npz_file": str(out_path),
            "available": availability.get(f"has_{name}", name.startswith(("physical_valid_mask_", "fusion_valid_mask_", "diagnostic_valid_mask_", "claas3_"))),
        })
        stats_rows.append(stats)
    return inventory_row, stats_rows


def write_report(nominal_time: str, inventory: pd.DataFrame) -> None:
    lines = [
        "# Standardized Native Build Report",
        "",
        f"- Generated UTC: {utc_now()}",
        f"- Prototype time: `{nominal_time}`",
        "- No download, no reprojection, no fusion.",
        "- Different native-resolution products are written as separate NPZ files.",
        "",
        "## Product Status",
        "",
    ]
    for _, row in inventory.iterrows():
        lines.append(
            f"- {row['satellite_group']} {row['product']}: {row['status']} "
            f"npz=`{row['npz_file']}` quicklook={row['quicklook_variable'] or 'none'}"
        )
    (REPORT_DIR / "standardized_native_build_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="prototype", choices=["prototype"])
    parser.add_argument("--target-time", default=os.environ.get("GEO_RING_TARGET_TIME", ""))
    parser.add_argument("--source-profile", default=os.environ.get("GEO_RING_SOURCE_PROFILE", "operational_baseline"), choices=["operational_baseline", "claas3_candidate"])
    parser.add_argument("--claas3-root", type=Path, default=path_config.CLAAS3_ROOT)
    parser.add_argument("--run-id", default=os.environ.get("GEO_RING_RUN_ID", ""))
    parser.add_argument("--reuse-operational-native-root", type=Path)
    args = parser.parse_args()
    source_profile = validate_profile(args.source_profile)
    ensure_dirs()
    shutil.copy2(__file__, SCRIPT_DIR / Path(__file__).name)
    shutil.copy2(Path(__file__).with_name("stage1_common.py"), SCRIPT_DIR / "stage1_common.py")
    mapping = read_mapping()
    nominal_time = args.target_time.strip() or load_selected_time(args.mode)
    rows = file_map_for_time(nominal_time)
    if source_profile == "operational_baseline":
        rows = rows[rows["satellite_group"] != "CLAAS3-0deg"]
    elif args.reuse_operational_native_root:
        rows = rows[rows["satellite_group"] == "CLAAS3-0deg"]
    fy4b_geo_arrays = load_fy4b_geo(rows, mapping)
    inventory_rows: list[dict[str, object]] = []
    stats_rows: list[dict[str, object]] = []
    reused_input_paths: list[str] = []
    if args.reuse_operational_native_root:
        base_inventory_path = args.reuse_operational_native_root / "standardized_native_inventory.csv"
        base_stats_path = args.reuse_operational_native_root / "standardized_native_variable_stats.csv"
        base_inventory = pd.read_csv(base_inventory_path)
        base_stats = pd.read_csv(base_stats_path)
        inventory_rows.extend(base_inventory.to_dict("records"))
        stats_rows.extend(base_stats.to_dict("records"))
        reused_input_paths.extend([str(base_inventory_path), str(base_stats_path)])
    for _, row in rows.iterrows():
        satellite_group = str(row["satellite_group"])
        family = str(row["satellite_family"])
        files = json.loads(str(row["product_files_json"]))
        products = list(CORE_PRODUCTS[satellite_group]["core"]) if satellite_group in CORE_PRODUCTS else ["CMA", "CTX", "CPP"]
        for product in products:
            item = files.get(product)
            if not item:
                continue
            try:
                if satellite_group == "CLAAS3-0deg":
                    inv, stats = build_one_claas3_product(nominal_time, product, Path(item["file_path"]), source_profile, args.run_id)
                else:
                    inv, stats = build_one_product(
                        nominal_time,
                        satellite_group,
                        family,
                        product,
                        Path(item["file_path"]),
                        mapping,
                        fy4b_geo_arrays,
                    )
                inventory_rows.append(inv)
                stats_rows.extend(stats)
            except Exception as exc:  # noqa: BLE001 - keep the time run alive and report the bad product.
                inventory_rows.append(
                    {
                        "nominal_time": nominal_time,
                        "satellite_group": satellite_group,
                        "family": family,
                        "product": product,
                        "source_file": item.get("file_path", ""),
                        "npz_file": "",
                        "status": "FAILED_READ",
                        "quicklook_variable": "",
                        "available_variables": "",
                        "missing_core_variables": "",
                        "notes": str(exc),
                    }
                )
    inventory = pd.DataFrame(inventory_rows)
    stats = pd.DataFrame(stats_rows)
    inventory.to_csv(NATIVE_DIR / "standardized_native_inventory.csv", index=False, encoding="utf-8-sig")
    stats.to_csv(NATIVE_DIR / "standardized_native_variable_stats.csv", index=False, encoding="utf-8-sig")
    write_report(nominal_time, inventory)
    write_manifest(
        NATIVE_DIR / "stage_02_claas3_native_manifest.json",
        canonical_stage_id="stage_02",
        run_id=args.run_id,
        source_profile=source_profile,
        generating_script=Path(__file__),
        input_paths=[*reused_input_paths, *(inventory["source_file"].dropna().astype(str).tolist() if not inventory.empty else [])],
        output_paths=inventory["npz_file"].dropna().astype(str).tolist() if not inventory.empty else [],
        parameters={"nominal_time": nominal_time, "claas3_root": str(args.claas3_root), "reuse_operational_native_root": str(args.reuse_operational_native_root or "")},
        project_root=path_config.PROJECT_ROOT,
        extra={"registry_version": REGISTRY_VERSION, "product_versions": {"CLAAS3": "405"} if source_profile == "claas3_candidate" else {}},
    )
    next_stage = REPORT_DIR / "geo_ring_cloud_next_stage_report.md"
    with next_stage.open("a", encoding="utf-8") as f:
        f.write(f"\n- Stage status update UTC {utc_now()}: 02 completed for `{nominal_time}`; products={len(inventory)}.\n")
    print(f"02 OK: prototype={nominal_time} products={len(inventory)}")
    print(f"inventory={NATIVE_DIR / 'standardized_native_inventory.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
