from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import ndimage

from geo_ring_cloud.pipeline_support import REPORT_DIR, SCRIPT_DIR, ensure_dirs, utc_now


STAGE_ROOT = Path(r"D:\AAAresearch_paper\geo_ring_cloud_stage1")
REPROJECT_DIR = STAGE_ROOT / "reprojected_grid"
FUSED_DIR = STAGE_ROOT / "fused_best_source"
OUT_DIR = STAGE_ROOT / "overlap_validation_07p"
QUICKLOOK_DIR = OUT_DIR / "quicklooks"

TARGET_TIME = "2024-03-05T00:00:00Z"
TIME_TAG = "20240305_0000"
MIN_PIXELS = 2000

PAIR_METRICS_CSV = OUT_DIR / "overlap_pair_metrics_v2.csv"
STRATIFIED_CSV = OUT_DIR / "overlap_stratified_metrics_v2.csv"
CONSISTENCY_CSV = OUT_DIR / "selected_vs_alternative_consistency_v2.csv"
CONTINUOUS_MASK_CSV = OUT_DIR / "continuous_metrics_by_mask.csv"
BOUNDARY_CSV = OUT_DIR / "source_boundary_metrics_v2.csv"
BOUNDARY_TRANSITION_CSV = OUT_DIR / "source_boundary_transition_matrix.csv"
CLOUD_MASK_AUDIT_CSV = OUT_DIR / "cloud_mask_binary_mapping_audit.csv"
INVENTORY_CSV = OUT_DIR / "overlap_validation_inventory_v2.csv"
CONFUSIONS_JSON = OUT_DIR / "overlap_confusion_matrices_v2.json"
REPORT_MD = REPORT_DIR / "overlap_validation_07p_report.md"

PAIRS = [
    ("GOES-16", "GOES-18"),
    ("GOES-16", "Meteosat-0deg"),
    ("GOES-18", "GOES-16"),
    ("Meteosat-0deg", "Meteosat-IODC"),
    ("Meteosat-IODC", "FY4B"),
    ("FY4B", "Himawari-9"),
    ("Himawari-9", "Meteosat-IODC"),
    ("GOES-18", "Himawari-9"),
]

VARIABLES = [
    "cloud_mask",
    "cloud_top_height_km",
    "cloud_top_temperature_K",
    "cloud_top_pressure_hPa",
    "cloud_optical_thickness",
    "cloud_effective_radius_um",
    "cloud_phase",
    "cloud_type",
]

CONTINUOUS_VARS = {
    "cloud_top_height_km",
    "cloud_top_temperature_K",
    "cloud_top_pressure_hPa",
    "cloud_optical_thickness",
    "cloud_effective_radius_um",
}
CATEGORICAL_VARS = {"cloud_phase", "cloud_type"}
METEOSAT_ALLOWED = {"cloud_mask", "cloud_top_height_km"}
ALLOWED_GEOMETRY_SOURCES = {"OFFICIAL_PIXEL_ANGLE", "OFFICIAL_GRIDDED_ANGLE", "OFFICIAL_NAV_DERIVED"}

VZA_BINS = [0, 30, 45, 60, 75, 180]
VZA_LABELS = ["0-30", "30-45", "45-60", "60-75", ">75"]
MARGIN_BINS = [0, 0.02, 0.05, 0.10, np.inf]
MARGIN_LABELS = ["0-0.02", "0.02-0.05", "0.05-0.10", ">0.10"]
CTH_BINS = [0, 2, 6, 10, np.inf]
CTH_LABELS = ["0-2 km", "2-6 km", "6-10 km", ">10 km"]
SZA_BINS = [0, 60, 75, 80, 90, np.inf]
SZA_LABELS = ["0-60", "60-75", "75-80", "80-90", ">90"]
RAA_BINS = [0, 30, 60, 90, 120, 180.1]
RAA_LABELS = ["0-30", "30-60", "60-90", "90-120", "120-180"]
GLINT_BINS = [0, 20, 40, 60, 90, 180.1]
GLINT_LABELS = ["0-20", "20-40", "40-60", "60-90", "90-180"]


def load_module(script_name: str, module_name: str):
    path = SCRIPT_DIR / script_name
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module {script_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


F06 = load_module("06_fuse_best_source.py", "stage1_f06_for_07p")
F07 = load_module("07_overlap_consistency_validation.py", "stage1_f07_base_for_07p")


def pair_name(a: str, b: str) -> str:
    return f"{a}__vs__{b}"


def load_npz_payload(path: Path) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    with np.load(path, allow_pickle=False) as z:
        meta = json.loads(str(z["metadata_json"]))
        arrays = {name: np.asarray(z[name]) for name in z.files if not name.endswith("_json")}
    return meta, arrays


def build_catalog() -> dict[tuple[str, str, str], Path]:
    return F06.load_catalog()


def build_target() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    grid = F06.target_grid_from_any(build_catalog())
    lon_1d, lat_1d = F06.build_target_lon_lat(grid)
    lon2d, lat2d = np.meshgrid(lon_1d, lat_1d)
    return lon_1d.astype(np.float32), lat_1d.astype(np.float32), lon2d.astype(np.float32), lat2d.astype(np.float32)


def load_reprojected_bundle(catalog: dict[tuple[str, str, str], Path], satellite: str, product: str, variable: str) -> dict[str, Any] | None:
    path = catalog.get((satellite, product, variable))
    if path is None or not path.exists():
        return None
    meta, arrays = load_npz_payload(path)
    return {"path": path, "meta": meta, "arrays": arrays}


def load_cloud_mask_bundle(catalog: dict[tuple[str, str, str], Path], satellite: str, product: str) -> dict[str, Any] | None:
    bundle = load_reprojected_bundle(catalog, satellite, product, "cloud_mask")
    if bundle is None:
        return None
    arrays = bundle["arrays"]
    raw = np.asarray(arrays["data"], dtype=np.int16)
    if "fusion_valid_mask" in arrays:
        fusion_valid = np.asarray(arrays["fusion_valid_mask"], dtype=bool)
    elif "valid_mask" in arrays:
        fusion_valid = np.asarray(arrays["valid_mask"], dtype=bool)
    else:
        fusion_valid = np.isfinite(raw)
    std = F06.cloud_mask_to_standard(satellite, product, raw)
    binary = F06.cloud_binary_from_standard(std, fusion_valid)
    bundle.update(
        {
            "data": raw,
            "valid": fusion_valid,
            "standard": std.astype(np.int16, copy=False),
            "binary": binary.astype(np.int8, copy=False),
        }
    )
    return bundle


def load_science_bundle(
    catalog: dict[tuple[str, str, str], Path],
    variable_to_product: dict[tuple[str, str], str],
    satellite: str,
    variable: str,
) -> dict[str, Any] | None:
    product = variable_to_product.get((satellite, variable))
    if product is None:
        return None
    if variable == "cloud_mask":
        bundle = load_cloud_mask_bundle(catalog, satellite, product)
        if bundle is not None:
            bundle["product"] = product
            bundle["variable"] = variable
        return bundle
    bundle = load_reprojected_bundle(catalog, satellite, product, variable)
    if bundle is None:
        return None
    data = np.asarray(bundle["arrays"]["data"], dtype=np.float32)
    valid = np.asarray(bundle["arrays"].get("valid_mask", np.isfinite(data)), dtype=bool) & np.isfinite(data)
    bundle.update({"data": data, "valid": valid, "product": product, "variable": variable})
    return bundle


def load_angle_layer(catalog: dict[tuple[str, str, str], Path], satellite: str, angle_name: str) -> dict[str, Any] | None:
    path = catalog.get((satellite, "ANGLE", angle_name))
    if path is None or not path.exists():
        return None
    meta, arrays = load_npz_payload(path)
    data = np.asarray(arrays["data"])
    valid = np.asarray(arrays.get("valid_mask", np.isfinite(data)), dtype=bool)
    return {"path": path, "meta": meta, "data": data, "valid": valid}


def bin_indices(values: np.ndarray, bins: list[float], labels: list[str]) -> list[tuple[str, np.ndarray]]:
    finite = np.isfinite(values)
    out: list[tuple[str, np.ndarray]] = []
    for left, right, label in zip(bins[:-1], bins[1:], labels):
        out.append((label, finite & (values >= left) & (values < right)))
    return out


def cloud_mask_metrics(a: np.ndarray, b: np.ndarray) -> dict[str, Any]:
    cm = F07.confusion_from_binary(a, b)
    n = cm["tn"] + cm["fp"] + cm["fn"] + cm["tp"]
    if n == 0:
        return {"sample_count": 0}
    agreement = (cm["tn"] + cm["tp"]) / n
    precision_den = cm["tp"] + cm["fp"]
    recall_den = cm["tp"] + cm["fn"]
    precision = cm["tp"] / precision_den if precision_den else np.nan
    recall = cm["tp"] / recall_den if recall_den else np.nan
    f1 = 2 * precision * recall / (precision + recall) if precision_den and recall_den and (precision + recall) else np.nan
    iou_den = cm["tp"] + cm["fp"] + cm["fn"]
    return {
        "sample_count": int(n),
        "overall_agreement": float(agreement),
        "precision": float(precision) if np.isfinite(precision) else np.nan,
        "recall": float(recall) if np.isfinite(recall) else np.nan,
        "F1": float(f1) if np.isfinite(f1) else np.nan,
        "IoU": float(cm["tp"] / iou_den) if iou_den else np.nan,
        "binary_clear_count_A": int(np.count_nonzero(a == 0)),
        "binary_cloudy_count_A": int(np.count_nonzero(a == 1)),
        "binary_clear_count_B": int(np.count_nonzero(b == 0)),
        "binary_cloudy_count_B": int(np.count_nonzero(b == 1)),
        "confusion_matrix": cm,
    }


def continuous_metrics(a: np.ndarray, b: np.ndarray) -> dict[str, Any]:
    diff = a.astype(np.float64) - b.astype(np.float64)
    abs_diff = np.abs(diff)
    return {
        "sample_count": int(diff.size),
        "mean_A": float(np.mean(a)),
        "mean_B": float(np.mean(b)),
        "bias_A_minus_B": float(np.mean(diff)),
        "MAE": float(np.mean(abs_diff)),
        "RMSE": float(np.sqrt(np.mean(diff * diff))),
        "median_diff": float(np.median(diff)),
        "p05_diff": float(np.percentile(diff, 5)),
        "p95_diff": float(np.percentile(diff, 95)),
        "max_abs_diff": float(np.max(abs_diff)),
    }


def categorical_metrics(a: np.ndarray, b: np.ndarray) -> dict[str, Any]:
    n = int(a.size)
    if n == 0:
        return {"sample_count": 0}
    pairs = np.column_stack([a.astype(np.int32), b.astype(np.int32)])
    uniq, counts = np.unique(pairs, axis=0, return_counts=True)
    matrix = {f"{int(p[0])}->{int(p[1])}": int(c) for p, c in zip(uniq, counts)}
    return {
        "sample_count": n,
        "agreement": float(np.mean(a == b)),
        "confusion_matrix": matrix,
    }


def evaluate_masked(metric_type: str, a_data: np.ndarray, b_data: np.ndarray, mask: np.ndarray) -> dict[str, Any]:
    n = int(np.count_nonzero(mask))
    if n < MIN_PIXELS:
        return {"sample_count": n, "status": "SKIPPED_TOO_FEW"}
    if metric_type == "cloud_mask":
        stats = cloud_mask_metrics(a_data[mask], b_data[mask])
    elif metric_type == "continuous":
        stats = continuous_metrics(a_data[mask], b_data[mask])
    else:
        stats = categorical_metrics(a_data[mask], b_data[mask])
    stats["status"] = "OK"
    return stats


def add_stratified_rows(
    rows: list[dict[str, Any]],
    pair: str,
    variable: str,
    metric_type: str,
    a_values: np.ndarray,
    b_values: np.ndarray,
    base_mask: np.ndarray,
    stratifier_name: str,
    stratifier_values: np.ndarray | None,
    bins: list[float],
    labels: list[str],
) -> None:
    if stratifier_values is None:
        rows.append(
            {
                "pair": pair,
                "variable": variable,
                "metric_type": metric_type,
                "stratifier": stratifier_name,
                "bin_label": "SKIPPED_NO_DATA",
                "sample_count": 0,
                "status": "SKIPPED_NO_DATA",
            }
        )
        return
    for label, mask_bin in bin_indices(stratifier_values, bins, labels):
        stats = evaluate_masked(metric_type, a_values, b_values, base_mask & mask_bin)
        row = {"pair": pair, "variable": variable, "metric_type": metric_type, "stratifier": stratifier_name, "bin_label": label}
        row.update(stats)
        rows.append(row)


def should_skip_pair_variable(a: str, b: str, variable: str) -> str | None:
    if a.startswith("Meteosat") and variable not in METEOSAT_ALLOWED:
        return "not_applicable_by_design"
    if b.startswith("Meteosat") and variable not in METEOSAT_ALLOWED:
        return "not_applicable_by_design"
    return None


def build_candidate_cache(
    catalog: dict[tuple[str, str, str], Path],
    lon2d: np.ndarray,
    lat2d: np.ndarray,
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, dict[str, Any]]]:
    subpoints = F06.build_subpoint_longitude_map(catalog)
    cache: dict[str, dict[str, dict[str, Any]]] = {var: {} for var in F06.VARIABLE_RULES}
    for variable, rules in F06.VARIABLE_RULES.items():
        for rule in rules:
            cand = F06.build_candidate(variable, rule, catalog, lon2d, lat2d, subpoints)
            if cand is not None:
                cache[variable][rule["satellite"]] = cand
    return cache, subpoints


def mean_angle_pair(a: dict[str, Any] | None, b: dict[str, Any] | None, mask: np.ndarray) -> np.ndarray | None:
    if a is None or b is None:
        return None
    out = np.full(mask.shape, np.nan, dtype=np.float32)
    common = mask & a["valid"] & b["valid"]
    if np.any(common):
        out[common] = (np.asarray(a["data"], dtype=np.float32)[common] + np.asarray(b["data"], dtype=np.float32)[common]) / 2.0
    return out


def min_angle_pair(a: dict[str, Any] | None, b: dict[str, Any] | None, mask: np.ndarray) -> np.ndarray | None:
    if a is None or b is None:
        return None
    out = np.full(mask.shape, np.nan, dtype=np.float32)
    common = mask & a["valid"] & b["valid"]
    if np.any(common):
        out[common] = np.minimum(np.asarray(a["data"], dtype=np.float32)[common], np.asarray(b["data"], dtype=np.float32)[common])
    return out


def compute_boundary_mask(source_map: np.ndarray, valid: np.ndarray) -> np.ndarray:
    return F07.compute_boundary_mask(source_map, valid)


def source_boundary_transitions(source_map: np.ndarray, valid: np.ndarray, variable: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    counts: dict[tuple[int, int], int] = {}
    for axis in [0, 1]:
        s1 = [slice(None), slice(None)]
        s2 = [slice(None), slice(None)]
        s1[axis] = slice(1, None)
        s2[axis] = slice(None, -1)
        src1 = source_map[tuple(s1)]
        src2 = source_map[tuple(s2)]
        ok = valid[tuple(s1)] & valid[tuple(s2)] & (src1 > 0) & (src2 > 0) & (src1 != src2)
        if not np.any(ok):
            continue
        a = src1[ok].astype(np.int16)
        b = src2[ok].astype(np.int16)
        lo = np.minimum(a, b)
        hi = np.maximum(a, b)
        uniq, uniq_counts = np.unique(np.column_stack([lo, hi]), axis=0, return_counts=True)
        for pair_ids, count in zip(uniq, uniq_counts):
            key = (int(pair_ids[0]), int(pair_ids[1]))
            counts[key] = counts.get(key, 0) + int(count)
    inv_map = {v: k for k, v in F06.SOURCE_ID_MAP.items()}
    for (a_id, b_id), count in sorted(counts.items()):
        rows.append(
            {
                "variable": variable,
                "source_a_id": a_id,
                "source_a": inv_map.get(a_id, str(a_id)),
                "source_b_id": b_id,
                "source_b": inv_map.get(b_id, str(b_id)),
                "edge_count": count,
            }
        )
    return rows


def load_fused_array(name: str) -> tuple[np.ndarray, np.ndarray]:
    meta, arrays = load_npz_payload(FUSED_DIR / f"{name}.npz")
    _ = meta
    return np.asarray(arrays["data"]), np.asarray(arrays["valid_mask"], dtype=bool)


def main() -> int:
    ensure_dirs()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    QUICKLOOK_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(__file__, SCRIPT_DIR / Path(__file__).name)

    catalog = build_catalog()
    variable_to_product = F07.build_variable_to_product()
    lon_1d, lat_1d, lon2d, lat2d = build_target()
    candidate_cache, subpoints = build_candidate_cache(catalog, lon2d, lat2d)
    _ = (lon_1d, lat_1d, subpoints)

    cloud_mask_audit_rows: list[dict[str, Any]] = []
    pair_rows: list[dict[str, Any]] = []
    stratified_rows: list[dict[str, Any]] = []
    consistency_rows: list[dict[str, Any]] = []
    boundary_rows: list[dict[str, Any]] = []
    boundary_transition_rows: list[dict[str, Any]] = []
    continuous_mask_rows: list[dict[str, Any]] = []
    inventory_rows: list[dict[str, Any]] = []
    confusion_dict: dict[str, Any] = {}

    gates = {
        "CLOUD_MASK_MAPPING_GATE": "PASS",
        "ANGLE_LAYER_USAGE_GATE": "PASS",
        "RATING_DIAGNOSTIC_GATE": "PASS",
        "STRATIFICATION_GATE": "PASS",
        "REPORT_LOGIC_GATE": "PASS",
    }

    angle_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
    for sat in F06.TIE_ORDER:
        for angle in ["solar_zenith_angle", "relative_azimuth_angle", "sun_glint_angle", "day_night_flag", "sensor_zenith_angle"]:
            angle_cache[(sat, angle)] = load_angle_layer(catalog, sat, angle)
        sensor = angle_cache[(sat, "sensor_zenith_angle")]
        if sensor is None:
            gates["ANGLE_LAYER_USAGE_GATE"] = "FAIL"
        else:
            source_level = str(sensor["meta"].get("angle_source_level", ""))
            if source_level not in ALLOWED_GEOMETRY_SOURCES:
                gates["ANGLE_LAYER_USAGE_GATE"] = "FAIL"

    # Cloud mask mapping audit and gate.
    for sat in F06.TIE_ORDER:
        cm_product = F07.build_variable_to_product().get((sat, "cloud_mask"))
        cm = load_cloud_mask_bundle(catalog, sat, cm_product) if cm_product is not None else None
        cth_product = F07.build_variable_to_product().get((sat, "cloud_top_height_km"))
        cth = load_science_bundle(catalog, variable_to_product, sat, "cloud_top_height_km") if cth_product is not None else None
        if cm is None:
            cloud_mask_audit_rows.append({"satellite": sat, "status": "MISSING_CLOUD_MASK"})
            gates["CLOUD_MASK_MAPPING_GATE"] = "FAIL"
            continue
        valid = cm["valid"]
        binary = cm["binary"]
        raw = cm["data"]
        std = cm["standard"]
        raw_vals, raw_counts = np.unique(raw[valid], return_counts=True) if np.any(valid) else (np.asarray([]), np.asarray([]))
        std_vals, std_counts = np.unique(std[valid], return_counts=True) if np.any(valid) else (np.asarray([]), np.asarray([]))
        binary_valid = binary[valid]
        cloudy_count = int(np.count_nonzero(binary_valid == 1))
        clear_count = int(np.count_nonzero(binary_valid == 0))
        cth_valid_count = int(np.count_nonzero(cth["valid"])) if cth is not None else 0
        row = {
            "satellite": sat,
            "product": cm_product,
            "cloud_mask_valid_count": int(np.count_nonzero(valid)),
            "cloud_mask_binary_cloudy_count": cloudy_count,
            "cloud_mask_binary_clear_count": clear_count,
            "cloud_mask_binary_invalid_count": int(np.count_nonzero(binary_valid < 0)),
            "cth_valid_count": cth_valid_count,
            "raw_unique_counts": json.dumps({int(v): int(c) for v, c in zip(raw_vals.tolist(), raw_counts.tolist())}, ensure_ascii=False),
            "standard_unique_counts": json.dumps({int(v): int(c) for v, c in zip(std_vals.tolist(), std_counts.tolist())}, ensure_ascii=False),
            "status": "OK",
        }
        if cth_valid_count >= MIN_PIXELS and cloudy_count == 0:
            row["status"] = "CLOUDY_COUNT_ZERO_WITH_VALID_CTH"
            gates["CLOUD_MASK_MAPPING_GATE"] = "FAIL"
        cloud_mask_audit_rows.append(row)

    source_maps: dict[str, np.ndarray] = {}
    source_valids: dict[str, np.ndarray] = {}
    rating_maps: dict[str, np.ndarray] = {}
    for variable in F06.VARIABLE_RULES:
        source_map, source_valid = load_fused_array(f"source_map_{variable}")
        rating_map, _ = load_fused_array(f"rating_map_{variable}")
        source_maps[variable] = source_map.astype(np.int16)
        source_valids[variable] = source_valid.astype(bool)
        rating_maps[variable] = rating_map.astype(np.float32)

    # Pairwise overlap validation.
    for sat_a, sat_b in PAIRS:
        pname = pair_name(sat_a, sat_b)
        pair_cloud_mask = load_science_bundle(catalog, variable_to_product, sat_a, "cloud_mask")
        pair_cloud_mask_b = load_science_bundle(catalog, variable_to_product, sat_b, "cloud_mask")
        for variable in VARIABLES:
            skip_reason = should_skip_pair_variable(sat_a, sat_b, variable)
            if skip_reason is not None:
                inventory_rows.append({"pair": pname, "satellite_A": sat_a, "satellite_B": sat_b, "variable": variable, "status": "SKIPPED", "reason": skip_reason, "sample_count": 0})
                continue

            bundle_a = load_science_bundle(catalog, variable_to_product, sat_a, variable)
            bundle_b = load_science_bundle(catalog, variable_to_product, sat_b, variable)
            if bundle_a is None or bundle_b is None:
                inventory_rows.append({"pair": pname, "satellite_A": sat_a, "satellite_B": sat_b, "variable": variable, "status": "SKIPPED", "reason": "missing_variable_for_pair", "sample_count": 0})
                continue

            metric_type = "cloud_mask" if variable == "cloud_mask" else ("continuous" if variable in CONTINUOUS_VARS else "categorical")
            if variable == "cloud_mask":
                a_data = bundle_a["binary"]
                b_data = bundle_b["binary"]
                base_mask = bundle_a["valid"] & bundle_b["valid"] & (a_data >= 0) & (b_data >= 0)
            else:
                a_data = np.asarray(bundle_a["data"])
                b_data = np.asarray(bundle_b["data"])
                base_mask = bundle_a["valid"] & bundle_b["valid"]

            # Use day / SZA<80 only for main COT/CER metrics.
            twilight_night_count = np.nan
            if variable in {"cloud_optical_thickness", "cloud_effective_radius_um"}:
                sza_a = angle_cache[(sat_a, "solar_zenith_angle")]
                sza_b = angle_cache[(sat_b, "solar_zenith_angle")]
                sza_mean = mean_angle_pair(sza_a, sza_b, base_mask)
                if sza_mean is None:
                    base_mask = np.zeros(base_mask.shape, dtype=bool)
                    gates["STRATIFICATION_GATE"] = "FAIL"
                else:
                    twilight_night_count = int(np.count_nonzero(base_mask & np.isfinite(sza_mean) & (sza_mean >= 80.0)))
                    base_mask = base_mask & np.isfinite(sza_mean) & (sza_mean < 80.0)

            stats = evaluate_masked(metric_type, a_data, b_data, base_mask)
            sample_count = int(stats.get("sample_count", 0))
            if stats["status"] != "OK":
                inventory_rows.append({"pair": pname, "satellite_A": sat_a, "satellite_B": sat_b, "variable": variable, "status": "SKIPPED", "reason": stats["status"], "sample_count": sample_count})
                continue

            row = {"pair": pname, "satellite_A": sat_a, "satellite_B": sat_b, "variable": variable, "metric_type": metric_type}
            row.update({k: v for k, v in stats.items() if k not in {"confusion_matrix", "status"}})
            if variable in {"cloud_optical_thickness", "cloud_effective_radius_um"}:
                row["twilight_night_sample_count_excluded_from_main"] = twilight_night_count
            pair_rows.append(row)
            if "confusion_matrix" in stats:
                confusion_dict.setdefault(pname, {})[variable] = stats["confusion_matrix"]
            inventory_rows.append({"pair": pname, "satellite_A": sat_a, "satellite_B": sat_b, "variable": variable, "status": "OK", "reason": "", "sample_count": sample_count})

            cand_a = candidate_cache.get(variable, {}).get(sat_a)
            cand_b = candidate_cache.get(variable, {}).get(sat_b)
            source_map = source_maps[variable]
            source_valid = source_valids[variable]
            if cand_a is not None and cand_b is not None:
                pair_mask = base_mask & source_valid
                if np.count_nonzero(pair_mask) >= MIN_PIXELS:
                    a_id = F06.SOURCE_ID_MAP[sat_a]
                    b_id = F06.SOURCE_ID_MAP[sat_b]
                    choose_a = pair_mask & (source_map == a_id)
                    choose_b = pair_mask & (source_map == b_id)
                    choose_other = pair_mask & ~np.isin(source_map, [a_id, b_id])
                    rating_a = np.asarray(cand_a["view_weight"], dtype=np.float32) * np.float32(cand_a["time_weight"]) * np.float32(cand_a["product_level_weight"])
                    rating_b = np.asarray(cand_b["view_weight"], dtype=np.float32) * np.float32(cand_b["time_weight"]) * np.float32(cand_b["product_level_weight"])
                    selected_margin = np.full(pair_mask.shape, np.nan, dtype=np.float32)
                    selected_margin[choose_a] = rating_a[choose_a] - rating_b[choose_a]
                    selected_margin[choose_b] = rating_b[choose_b] - rating_a[choose_b]
                    rating_consistent = np.concatenate(
                        [
                            (rating_a[choose_a] >= rating_b[choose_a] - 1e-12).astype(np.float32),
                            (rating_b[choose_b] >= rating_a[choose_b] - 1e-12).astype(np.float32),
                        ]
                    ) if (np.count_nonzero(choose_a) + np.count_nonzero(choose_b)) else np.asarray([], dtype=np.float32)
                    geom_a = str(cand_a.get("vza_source_level", ""))
                    geom_b = str(cand_b.get("vza_source_level", ""))
                    if geom_a not in ALLOWED_GEOMETRY_SOURCES or geom_b not in ALLOWED_GEOMETRY_SOURCES:
                        gates["ANGLE_LAYER_USAGE_GATE"] = "FAIL"
                    consistency_row = {
                        "pair": pname,
                        "satellite_A": sat_a,
                        "satellite_B": sat_b,
                        "variable": variable,
                        "pair_overlap_count": int(np.count_nonzero(pair_mask)),
                        "selected_A_fraction": float(np.count_nonzero(choose_a) / np.count_nonzero(pair_mask)),
                        "selected_B_fraction": float(np.count_nonzero(choose_b) / np.count_nonzero(pair_mask)),
                        "selected_other_fraction": float(np.count_nonzero(choose_other) / np.count_nonzero(pair_mask)),
                        "selected_higher_rating_fraction": float(np.mean(rating_consistent)) if rating_consistent.size else np.nan,
                        "selected_minus_alternative_margin_mean": float(np.nanmean(selected_margin)) if np.isfinite(selected_margin).any() else np.nan,
                        "selected_minus_alternative_margin_p05": float(np.nanpercentile(selected_margin[np.isfinite(selected_margin)], 5)) if np.isfinite(selected_margin).any() else np.nan,
                        "selected_minus_alternative_margin_p95": float(np.nanpercentile(selected_margin[np.isfinite(selected_margin)], 95)) if np.isfinite(selected_margin).any() else np.nan,
                        "geometry_source_A": geom_a,
                        "geometry_source_B": geom_b,
                    }
                    consistency_rows.append(consistency_row)
                    frac = consistency_row["selected_higher_rating_fraction"]
                    if np.isfinite(frac) and frac < 0.95:
                        gates["RATING_DIAGNOSTIC_GATE"] = "FAIL"

            # Stratification from 06e ANGLE layers only.
            margin_vals = np.where(source_valid, rating_maps[variable], np.nan)
            cand_vza_a = candidate_cache.get(variable, {}).get(sat_a)
            cand_vza_b = candidate_cache.get(variable, {}).get(sat_b)
            vza_max = None
            if cand_vza_a is not None and cand_vza_b is not None:
                vza_max = np.maximum(np.asarray(cand_vza_a["vza_for_rating"], dtype=np.float32), np.asarray(cand_vza_b["vza_for_rating"], dtype=np.float32))
            add_stratified_rows(stratified_rows, pname, variable, metric_type, a_data, b_data, base_mask, "VZA_max", vza_max, VZA_BINS, VZA_LABELS)
            add_stratified_rows(stratified_rows, pname, variable, metric_type, a_data, b_data, base_mask, "rating_margin", margin_vals, MARGIN_BINS, MARGIN_LABELS)

            if variable != "cloud_top_height_km":
                cth_a = load_science_bundle(catalog, variable_to_product, sat_a, "cloud_top_height_km")
                cth_b = load_science_bundle(catalog, variable_to_product, sat_b, "cloud_top_height_km")
                cth_mean = None
                if cth_a is not None and cth_b is not None:
                    cth_mean = np.full(base_mask.shape, np.nan, dtype=np.float32)
                    common = base_mask & cth_a["valid"] & cth_b["valid"]
                    if np.any(common):
                        cth_mean[common] = (cth_a["data"][common] + cth_b["data"][common]) / 2.0
            else:
                cth_mean = np.full(base_mask.shape, np.nan, dtype=np.float32)
                cth_mean[base_mask] = (a_data[base_mask] + b_data[base_mask]) / 2.0
            add_stratified_rows(stratified_rows, pname, variable, metric_type, a_data, b_data, base_mask, "CTH_mean", cth_mean, CTH_BINS, CTH_LABELS)

            sza_mean = mean_angle_pair(angle_cache[(sat_a, "solar_zenith_angle")], angle_cache[(sat_b, "solar_zenith_angle")], base_mask)
            raa_mean = mean_angle_pair(angle_cache[(sat_a, "relative_azimuth_angle")], angle_cache[(sat_b, "relative_azimuth_angle")], base_mask)
            glint_min = min_angle_pair(angle_cache[(sat_a, "sun_glint_angle")], angle_cache[(sat_b, "sun_glint_angle")], base_mask)
            add_stratified_rows(stratified_rows, pname, variable, metric_type, a_data, b_data, base_mask, "SZA_mean", sza_mean, SZA_BINS, SZA_LABELS)
            add_stratified_rows(stratified_rows, pname, variable, metric_type, a_data, b_data, base_mask, "RAA_mean", raa_mean, RAA_BINS, RAA_LABELS)
            add_stratified_rows(stratified_rows, pname, variable, metric_type, a_data, b_data, base_mask, "glint_min", glint_min, GLINT_BINS, GLINT_LABELS)

            # Continuous metrics by mask.
            if variable in CONTINUOUS_VARS:
                boundary = compute_boundary_mask(source_maps[variable], source_valids[variable])
                if pair_cloud_mask is not None and pair_cloud_mask_b is not None:
                    both_cloudy = (
                        base_mask
                        & pair_cloud_mask["valid"]
                        & pair_cloud_mask_b["valid"]
                        & (pair_cloud_mask["binary"] == 1)
                        & (pair_cloud_mask_b["binary"] == 1)
                    )
                else:
                    both_cloudy = np.zeros(base_mask.shape, dtype=bool)
                mask_map = {
                    "all_valid": base_mask,
                    "both_cloudy": both_cloudy,
                    "non_boundary": base_mask & ~boundary,
                    "both_cloudy_and_non_boundary": both_cloudy & ~boundary,
                }
                for mask_name, mask_value in mask_map.items():
                    stats_mask = evaluate_masked("continuous", np.asarray(bundle_a["data"]), np.asarray(bundle_b["data"]), mask_value)
                    row_mask = {"pair": pname, "satellite_A": sat_a, "satellite_B": sat_b, "variable": variable, "mask_name": mask_name}
                    row_mask.update({k: v for k, v in stats_mask.items() if k != "confusion_matrix"})
                    continuous_mask_rows.append(row_mask)

    # Boundary summaries and transition matrix.
    for variable in F06.VARIABLE_RULES:
        source_map = source_maps[variable]
        valid = source_valids[variable]
        boundary = compute_boundary_mask(source_map, valid)
        transition_rows = source_boundary_transitions(source_map, valid, variable)
        boundary_transition_rows.extend(transition_rows)
        if variable == "cloud_mask":
            fused_data, fused_valid = load_fused_array("fused_cloud_binary")
            fused_float = fused_data.astype(np.float32)
            valid_for_jump = fused_valid.astype(bool)
        else:
            fused_data, fused_valid = load_fused_array(f"fused_{variable}")
            fused_float = fused_data.astype(np.float32)
            valid_for_jump = fused_valid.astype(bool)
        jump_boundary, jump_same = F07.neighbor_edge_arrays(fused_float, valid_for_jump, source_map)
        boundary_rows.append(
            {
                "variable": variable,
                "sample_count": int(np.count_nonzero(boundary)),
                "boundary_fraction_among_valid": float(np.count_nonzero(boundary) / np.count_nonzero(valid)) if np.count_nonzero(valid) else np.nan,
                "edge_jump_mean_abs": float(np.mean(jump_boundary)) if jump_boundary.size else np.nan,
                "edge_jump_p95_abs": float(np.percentile(jump_boundary, 95)) if jump_boundary.size else np.nan,
                "same_source_neighbor_mean_abs": float(np.mean(jump_same)) if jump_same.size else np.nan,
                "same_source_neighbor_p95_abs": float(np.percentile(jump_same, 95)) if jump_same.size else np.nan,
            }
        )
        F07.make_simple_quicklook(boundary.astype(np.float32), QUICKLOOK_DIR / f"source_boundary_{variable}.png", f"07p source boundary {variable} {TARGET_TIME}", categorical_labels={0: "non-boundary", 1: "boundary"})

    pair_df = pd.DataFrame(pair_rows)
    strat_df = pd.DataFrame(stratified_rows)
    consistency_df = pd.DataFrame(consistency_rows)
    boundary_df = pd.DataFrame(boundary_rows)
    boundary_transition_df = pd.DataFrame(boundary_transition_rows)
    inventory_df = pd.DataFrame(inventory_rows)
    cloud_mask_audit_df = pd.DataFrame(cloud_mask_audit_rows)
    continuous_mask_df = pd.DataFrame(continuous_mask_rows)

    # Gates from stratification/report logic.
    for stratifier_name in ["SZA_mean", "RAA_mean", "glint_min"]:
        subset = strat_df[strat_df["stratifier"] == stratifier_name] if not strat_df.empty else pd.DataFrame()
        if subset.empty or subset["status"].eq("SKIPPED_NO_DATA").all():
            gates["STRATIFICATION_GATE"] = "FAIL"
    if not consistency_df.empty:
        bad_geom = consistency_df[
            ~consistency_df["geometry_source_A"].isin(ALLOWED_GEOMETRY_SOURCES)
            | ~consistency_df["geometry_source_B"].isin(ALLOWED_GEOMETRY_SOURCES)
        ]
        if not bad_geom.empty:
            gates["ANGLE_LAYER_USAGE_GATE"] = "FAIL"

    pair_df.to_csv(PAIR_METRICS_CSV, index=False, encoding="utf-8-sig")
    strat_df.to_csv(STRATIFIED_CSV, index=False, encoding="utf-8-sig")
    consistency_df.to_csv(CONSISTENCY_CSV, index=False, encoding="utf-8-sig")
    continuous_mask_df.to_csv(CONTINUOUS_MASK_CSV, index=False, encoding="utf-8-sig")
    boundary_df.to_csv(BOUNDARY_CSV, index=False, encoding="utf-8-sig")
    boundary_transition_df.to_csv(BOUNDARY_TRANSITION_CSV, index=False, encoding="utf-8-sig")
    cloud_mask_audit_df.to_csv(CLOUD_MASK_AUDIT_CSV, index=False, encoding="utf-8-sig")
    inventory_df.to_csv(INVENTORY_CSV, index=False, encoding="utf-8-sig")
    CONFUSIONS_JSON.write_text(json.dumps(confusion_dict, ensure_ascii=False, indent=2), encoding="utf-8")

    required_for_formal_07 = (
        gates["CLOUD_MASK_MAPPING_GATE"] == "PASS"
        and gates["ANGLE_LAYER_USAGE_GATE"] == "PASS"
        and gates["RATING_DIAGNOSTIC_GATE"] == "PASS"
        and gates["REPORT_LOGIC_GATE"] == "PASS"
    )
    overall_status = "PASS" if required_for_formal_07 and gates["STRATIFICATION_GATE"] == "PASS" else "FAIL"

    report_lines = [
        "# 07p Overlap Validator Hotfix Report",
        "",
        f"- 生成时间 UTC: {utc_now()}",
        f"- 目标时次: `{TARGET_TIME}`",
        f"- 结论: **{overall_status}**",
        "",
        "## Gate",
        "",
    ]
    for gate_name, gate_value in gates.items():
        report_lines.append(f"- {gate_name}: **{gate_value}**")
    report_lines.extend(
        [
            "",
            "## 1. cloud_mask hotfix",
            "",
            "- cloud_mask 二值化已改为复用 `F06.cloud_mask_to_standard()` 和 `F06.cloud_binary_from_standard()`。",
            "- cloud_mask overlap 只使用 `fusion_valid_mask`，不使用 `display_valid_mask`。",
            f"- 审计表: `{CLOUD_MASK_AUDIT_CSV}`",
            "",
            "## 2. angle / rating hotfix",
            "",
            "- 07p 不再自行调用 `approximate_geostationary_vza()` 重算 VZA。",
            "- 每颗卫星、每个变量的 rating 诊断均通过 `F06.build_candidate()` 重建，geometry_source 直接来自 06 当前入口。",
            "- SZA / RAA / glint / day-night 只从 06e `ANGLE` 层读取。",
            f"- 一致性表: `{CONSISTENCY_CSV}`",
            "",
            "## 3. 关键检查",
            "",
            f"- pair 指标数: {len(pair_df)}",
            f"- stratified 指标数: {len(strat_df)}",
            f"- continuous by mask 指标数: {len(continuous_mask_df)}",
            f"- source boundary transition 行数: {len(boundary_transition_df)}",
            "",
            "## 4. 是否允许重跑正式 07",
            "",
            f"- 允许条件结果: **{'YES' if required_for_formal_07 else 'NO'}**",
        ]
    )
    if overall_status == "FAIL":
        report_lines.extend(
            [
                "",
                "## 5. 停止条件",
                "",
                "- 07p 结果为 FAIL，不允许在报告中写“进入 6 个代表时次”。",
                "- 应先修正 gate 失败项，再决定是否重跑正式 07。",
            ]
        )
    report_lines.extend(
        [
            "",
            "## 输出文件",
            "",
            f"- `{PAIR_METRICS_CSV}`",
            f"- `{STRATIFIED_CSV}`",
            f"- `{CONSISTENCY_CSV}`",
            f"- `{CONTINUOUS_MASK_CSV}`",
            f"- `{BOUNDARY_CSV}`",
            f"- `{BOUNDARY_TRANSITION_CSV}`",
            f"- `{CLOUD_MASK_AUDIT_CSV}`",
            f"- `{INVENTORY_CSV}`",
            f"- `{CONFUSIONS_JSON}`",
            f"- `{REPORT_MD}`",
            f"- `{QUICKLOOK_DIR}`",
        ]
    )
    report_text = "\n".join(report_lines)
    if overall_status == "FAIL" and "6 个代表时次" in report_text:
        gates["REPORT_LOGIC_GATE"] = "FAIL"
    REPORT_MD.write_text(report_text, encoding="utf-8")

    print(f"07p {overall_status}: pair_metrics={len(pair_df)} stratified={len(strat_df)}")
    print(f"report={REPORT_MD}")
    return 0 if required_for_formal_07 else 2


if __name__ == "__main__":
    raise SystemExit(main())
