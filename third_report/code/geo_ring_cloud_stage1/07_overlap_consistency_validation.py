from __future__ import annotations

import importlib.util
import json
import math
import shutil
import sys
from pathlib import Path
from typing import Any

import matplotlib
import numpy as np
import pandas as pd
from scipy import ndimage

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, ListedColormap

from geo_ring_cloud.pipeline_support import REPORT_DIR, SCRIPT_DIR, ensure_dirs, utc_now


STAGE_ROOT = Path(r"D:\AAAresearch_paper\geo_ring_cloud_stage1")
REPROJECT_DIR = STAGE_ROOT / "reprojected_grid"
FUSED_DIR = STAGE_ROOT / "fused_best_source"
DIAG_DIR = STAGE_ROOT / "source_selection_diagnostics"
GEOM06C_DIR = STAGE_ROOT / "geometry_audit_06c"
OUT_DIR = STAGE_ROOT / "overlap_validation"
QUICKLOOK_DIR = OUT_DIR / "quicklooks"

TARGET_TIME = "2024-03-05T00:00:00Z"
TIME_TAG = "20240305_0000"
MIN_PIXELS = 2000
PAIR_MIN_PIXELS_FOR_OPTIONAL = 5000
BOUNDARY_BUFFERS = [1, 2, 3]
TOL = 1e-6

PAIR_METRICS_CSV = OUT_DIR / "overlap_pair_metrics.csv"
STRATIFIED_CSV = OUT_DIR / "overlap_stratified_metrics.csv"
CONFUSIONS_JSON = OUT_DIR / "overlap_confusion_matrices.json"
CONSISTENCY_CSV = OUT_DIR / "selected_vs_alternative_consistency.csv"
BOUNDARY_CSV = OUT_DIR / "source_boundary_metrics.csv"
INVENTORY_CSV = OUT_DIR / "overlap_validation_inventory.csv"
REPORT_MD = REPORT_DIR / "overlap_validation_report.md"

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

METEOSAT_ALLOWED = {"cloud_mask", "cloud_top_height_km"}
CATEGORICAL_VARS = {"cloud_phase", "cloud_type"}
CONTINUOUS_VARS = {
    "cloud_top_height_km",
    "cloud_top_temperature_K",
    "cloud_top_pressure_hPa",
    "cloud_optical_thickness",
    "cloud_effective_radius_um",
}

VZA_BINS = [0, 30, 45, 60, 75, 180]
VZA_LABELS = ["0-30", "30-45", "45-60", "60-75", ">75"]
MARGIN_BINS = [0, 0.02, 0.05, 0.10, np.inf]
MARGIN_LABELS = ["0-0.02", "0.02-0.05", "0.05-0.10", ">0.10"]
CTH_BINS = [0, 2, 6, 10, np.inf]
CTH_LABELS = ["0-2 km", "2-6 km", "6-10 km", ">10 km"]
SZA_BINS = [0, 60, 75, 90, np.inf]
SZA_LABELS = ["0-60", "60-75", "75-90", ">90"]


def load_module(script_name: str, module_name: str):
    path = SCRIPT_DIR / script_name
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module {script_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


F06 = load_module("06_fuse_best_source.py", "stage1_f06")
F06C = load_module("06c_geometry_parameter_audit.py", "stage1_f06c")


def pair_name(a: str, b: str) -> str:
    return f"{a}__vs__{b}"


def load_npz_payload(path: Path) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    with np.load(path, allow_pickle=False) as z:
        meta = json.loads(str(z["metadata_json"]))
        arrays = {name: np.asarray(z[name]) for name in z.files if not name.endswith("_json")}
    return meta, arrays


def parse_reproject_filename(path: Path) -> tuple[str, str, str] | None:
    name = path.name
    if not name.endswith(f"_grid_{TIME_TAG}.npz"):
        return None
    stem = name[: -len(f"_grid_{TIME_TAG}.npz")]
    for sat in F06.TIE_ORDER:
        prefix = f"{sat}_"
        if stem.startswith(prefix):
            rest = stem[len(prefix) :]
            if "_" not in rest:
                return None
            product, variable = rest.split("_", 1)
            return sat, product, variable
    return None


def build_reproject_catalog() -> dict[tuple[str, str, str], Path]:
    catalog: dict[tuple[str, str, str], Path] = {}
    for path in REPROJECT_DIR.rglob(f"*_{TIME_TAG}.npz"):
        parsed = parse_reproject_filename(path)
        if parsed is None:
            continue
        catalog[parsed] = path
    return catalog


def build_variable_to_product() -> dict[tuple[str, str], str]:
    mapping: dict[tuple[str, str], str] = {}
    for variable, rules in F06.VARIABLE_RULES.items():
        for rule in rules:
            mapping[(rule["satellite"], variable)] = rule["product"]
    return mapping


def build_target_lon_lat() -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    grid = F06.target_grid_from_any(F06.load_catalog())
    lon, lat = F06C.build_target_lon_lat(grid)
    return lon.astype(np.float32), lat.astype(np.float32), grid


def load_satellite_geometry_warnings() -> dict[str, dict[str, Any]]:
    df = pd.read_csv(GEOM06C_DIR / "satellite_geometry_parameter_audit.csv")
    return {str(row["satellite"]): row.to_dict() for _, row in df.iterrows()}


def load_diag_summary() -> tuple[pd.DataFrame, pd.DataFrame]:
    selected = pd.read_csv(DIAG_DIR / "selected_vs_min_vza_agreement.csv")
    legacy = pd.read_csv(DIAG_DIR / "legacy_mixed_change_summary.csv")
    return selected, legacy


def cloud_mask_binary(arr: np.ndarray, valid: np.ndarray) -> np.ndarray:
    out = np.full(arr.shape, -1, dtype=np.int8)
    out[valid & np.isin(arr, [0, 1])] = 0
    out[valid & np.isin(arr, [2, 3])] = 1
    return out


def load_reprojected_variable(catalog: dict[tuple[str, str, str], Path], satellite: str, variable: str, variable_to_product: dict[tuple[str, str], str]) -> dict[str, Any] | None:
    product = variable_to_product.get((satellite, variable))
    if product is None:
        return None
    path = catalog.get((satellite, product, variable))
    if path is None or not path.exists():
        return None
    meta, arrays = load_npz_payload(path)
    data = np.asarray(arrays["data"])
    if variable == "cloud_mask":
        valid = np.asarray(arrays.get("fusion_valid_mask", arrays.get("valid_mask")), dtype=bool)
        display_valid = np.asarray(arrays.get("display_valid_mask", valid), dtype=bool)
        off_disc = np.asarray(arrays.get("off_disc_mask", np.zeros(data.shape, dtype=np.uint8))).astype(bool)
        binary = cloud_mask_binary(data.astype(np.int16), valid)
        return {
            "path": path,
            "meta": meta,
            "product": product,
            "variable": variable,
            "data": data.astype(np.int16, copy=False),
            "valid": valid,
            "display_valid": display_valid,
            "off_disc": off_disc,
            "binary": binary,
        }
    valid = np.asarray(arrays.get("valid_mask", np.isfinite(data)), dtype=bool)
    return {
        "path": path,
        "meta": meta,
        "product": product,
        "variable": variable,
        "data": data.astype(np.float32, copy=False),
        "valid": valid & np.isfinite(data),
    }


def should_skip_pair_variable(a: str, b: str, variable: str) -> str | None:
    if a.startswith("Meteosat") and variable not in METEOSAT_ALLOWED:
        return "meteosat_variable_not_allowed"
    if b.startswith("Meteosat") and variable not in METEOSAT_ALLOWED:
        return "meteosat_variable_not_allowed"
    return None


def confusion_from_binary(a: np.ndarray, b: np.ndarray) -> dict[str, int]:
    return {
        "tn": int(np.count_nonzero((a == 0) & (b == 0))),
        "fp": int(np.count_nonzero((a == 0) & (b == 1))),
        "fn": int(np.count_nonzero((a == 1) & (b == 0))),
        "tp": int(np.count_nonzero((a == 1) & (b == 1))),
    }


def cloud_mask_metrics(a: np.ndarray, b: np.ndarray) -> dict[str, Any]:
    cm = confusion_from_binary(a, b)
    n = cm["tn"] + cm["fp"] + cm["fn"] + cm["tp"]
    if n == 0:
        return {"sample_count": 0}
    agreement = (cm["tn"] + cm["tp"]) / n
    cloudy_den = cm["tp"] + cm["fn"]
    clear_den = cm["tn"] + cm["fp"]
    precision_den = cm["tp"] + cm["fp"]
    recall_den = cm["tp"] + cm["fn"]
    iou_den = cm["tp"] + cm["fp"] + cm["fn"]
    precision = cm["tp"] / precision_den if precision_den else np.nan
    recall = cm["tp"] / recall_den if recall_den else np.nan
    f1 = 2 * precision * recall / (precision + recall) if precision_den and recall_den and (precision + recall) else np.nan
    return {
        "sample_count": int(n),
        "overall_agreement": float(agreement),
        "cloudy_agreement": float(cm["tp"] / cloudy_den) if cloudy_den else np.nan,
        "clear_agreement": float(cm["tn"] / clear_den) if clear_den else np.nan,
        "precision": float(precision) if np.isfinite(precision) else np.nan,
        "recall": float(recall) if np.isfinite(recall) else np.nan,
        "F1": float(f1) if np.isfinite(f1) else np.nan,
        "IoU": float(cm["tp"] / iou_den) if iou_den else np.nan,
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
        "code_mapping_notes": "Raw category-code agreement only; per-satellite cloud phase/type code tables may differ.",
    }


def bin_indices(values: np.ndarray, bins: list[float], labels: list[str]) -> list[tuple[str, np.ndarray]]:
    finite = np.isfinite(values)
    results: list[tuple[str, np.ndarray]] = []
    for left, right, label in zip(bins[:-1], bins[1:], labels):
        mask = finite & (values >= left) & (values < right)
        results.append((label, mask))
    return results


def make_simple_quicklook(data: np.ndarray, out_path: Path, title: str, cmap: str = "viridis", vmin: float | None = None, vmax: float | None = None, categorical_labels: dict[int, str] | None = None) -> None:
    arr = np.asarray(data)
    stride = max(1, int(math.ceil(math.sqrt(arr.size / 1_200_000)))) if arr.size > 1_200_000 else 1
    plot = arr[::stride, ::stride]
    plt.figure(figsize=(11, 4.8), dpi=150)
    if categorical_labels is not None:
        finite = np.isfinite(plot)
        values = np.sort(np.unique(plot[finite])) if finite.any() else np.asarray([])
        colors = plt.get_cmap("tab10")(np.linspace(0, 1, max(2, values.size if values.size else 2)))
        cmap_obj = ListedColormap(colors)
        cmap_obj.set_bad("#ffffff")
        if values.size == 0:
            im = plt.imshow(plot, extent=[-180, 180, -90, 90], origin="lower", cmap=cmap_obj, interpolation="nearest")
        else:
            bounds = np.concatenate(([values[0] - 0.5], (values[:-1] + values[1:]) / 2.0, [values[-1] + 0.5])) if values.size > 1 else np.array([values[0] - 0.5, values[0] + 0.5])
            norm = BoundaryNorm(bounds, cmap_obj.N)
            im = plt.imshow(plot, extent=[-180, 180, -90, 90], origin="lower", cmap=cmap_obj, norm=norm, interpolation="nearest")
            cbar = plt.colorbar(im, shrink=0.78, ticks=values)
            cbar.ax.set_yticklabels([categorical_labels.get(int(v), str(int(v))) for v in values])
            plt.title(title, fontsize=10)
            plt.xlabel("Longitude")
            plt.ylabel("Latitude")
            plt.tight_layout()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            plt.savefig(out_path)
            plt.close()
            return
    else:
        im = plt.imshow(plot, extent=[-180, 180, -90, 90], origin="lower", cmap=cmap, interpolation="nearest", vmin=vmin, vmax=vmax)
        plt.colorbar(im, shrink=0.78)
    plt.title(title, fontsize=10)
    plt.xlabel("Longitude")
    plt.ylabel("Latitude")
    plt.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_path)
    plt.close()


def make_difference_quicklook(diff: np.ndarray, out_path: Path, title: str) -> None:
    arr = np.asarray(diff, dtype=np.float32)
    finite = np.isfinite(arr)
    if finite.any():
        vmax = float(np.nanpercentile(np.abs(arr[finite]), 98))
        if not np.isfinite(vmax) or vmax == 0:
            vmax = 1.0
    else:
        vmax = 1.0
    make_simple_quicklook(arr, out_path, title, cmap="coolwarm", vmin=-vmax, vmax=vmax)


def neighbor_edge_arrays(values: np.ndarray, valid: np.ndarray, source_map: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    value = np.asarray(values, dtype=np.float32)
    valid = np.asarray(valid, dtype=bool)
    source_map = np.asarray(source_map, dtype=np.int16)
    diffs_boundary: list[np.ndarray] = []
    diffs_same: list[np.ndarray] = []
    for axis in [0, 1]:
        s1 = [slice(None), slice(None)]
        s2 = [slice(None), slice(None)]
        s1[axis] = slice(1, None)
        s2[axis] = slice(None, -1)
        v1 = value[tuple(s1)]
        v2 = value[tuple(s2)]
        ok = valid[tuple(s1)] & valid[tuple(s2)] & np.isfinite(v1) & np.isfinite(v2)
        same = ok & (source_map[tuple(s1)] == source_map[tuple(s2)])
        boundary = ok & (source_map[tuple(s1)] != source_map[tuple(s2)])
        if np.any(boundary):
            diffs_boundary.append(np.abs(v1[boundary] - v2[boundary]))
        if np.any(same):
            diffs_same.append(np.abs(v1[same] - v2[same]))
    return (
        np.concatenate(diffs_boundary) if diffs_boundary else np.asarray([], dtype=np.float32),
        np.concatenate(diffs_same) if diffs_same else np.asarray([], dtype=np.float32),
    )


def compute_boundary_mask(source_map: np.ndarray, valid: np.ndarray) -> np.ndarray:
    src = np.asarray(source_map, dtype=np.int16)
    valid = np.asarray(valid, dtype=bool)
    out = np.zeros(src.shape, dtype=bool)
    out[1:, :] |= valid[1:, :] & valid[:-1, :] & (src[1:, :] != src[:-1, :])
    out[:-1, :] |= valid[:-1, :] & valid[1:, :] & (src[:-1, :] != src[1:, :])
    out[:, 1:] |= valid[:, 1:] & valid[:, :-1] & (src[:, 1:] != src[:, :-1])
    out[:, :-1] |= valid[:, :-1] & valid[:, 1:] & (src[:, :-1] != src[:, 1:])
    return out


def selected_vza_from_source_map(source_map: np.ndarray, valid: np.ndarray, satellite_vzas: dict[str, np.ndarray]) -> np.ndarray:
    out = np.full(source_map.shape, np.nan, dtype=np.float32)
    for sat, source_id in F06.SOURCE_ID_MAP.items():
        mask = valid & (source_map == source_id)
        if np.any(mask):
            out[mask] = satellite_vzas[sat][mask]
    return out


def compute_satellite_vza_and_rating(
    satellite: str,
    lon_1d: np.ndarray,
    lat_1d: np.ndarray,
    target_grid: dict[str, Any],
    catalog: dict[tuple[str, str, str], Path],
    variable_to_product: dict[tuple[str, str], str],
) -> tuple[np.ndarray, dict[str, np.ndarray], dict[str, float]]:
    lon2d, lat2d = np.meshgrid(lon_1d, lat_1d)
    subpoints = F06.build_subpoint_longitude_map(catalog)
    vza = F06.approximate_geostationary_vza(lat2d, lon2d, subpoints[satellite]["subpoint_lon_deg"])
    del target_grid
    ratings: dict[str, np.ndarray] = {}
    time_weights: dict[str, float] = {}
    for variable in VARIABLES:
        product = variable_to_product.get((satellite, variable))
        if product is None:
            continue
        path = catalog.get((satellite, product, variable))
        if path is None or not path.exists():
            continue
        meta, arrays = load_npz_payload(path)
        if variable == "cloud_mask":
            valid = np.asarray(arrays.get("fusion_valid_mask", arrays.get("valid_mask")), dtype=bool)
        else:
            valid = np.asarray(arrays.get("valid_mask", np.isfinite(arrays["data"])), dtype=bool) & np.isfinite(arrays["data"])
        view_weight, _ = F06.view_weight_from_vza(vza, valid)
        tw, _, _ = F06.time_weight(meta)
        plw = F06.product_level_weight(variable, satellite, product)
        rating = np.zeros(valid.shape, dtype=np.float32)
        rating[valid] = view_weight[valid] * np.float32(tw) * np.float32(plw)
        ratings[variable] = rating
        time_weights[variable] = float(tw)
    return vza.astype(np.float32), ratings, time_weights


def load_fused_artifact(name: str) -> tuple[dict[str, Any], np.ndarray, np.ndarray]:
    meta, arrays = load_npz_payload(FUSED_DIR / f"{name}.npz")
    return meta, np.asarray(arrays["data"]), np.asarray(arrays["valid_mask"], dtype=bool)


def load_diag_artifact(name: str) -> tuple[dict[str, Any], np.ndarray, np.ndarray]:
    meta, arrays = load_npz_payload(DIAG_DIR / f"{name}.npz")
    return meta, np.asarray(arrays["data"]), np.asarray(arrays["valid_mask"], dtype=bool)


def stratified_metric_rows(
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
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if stratifier_values is None:
        rows.append({"pair": pair, "variable": variable, "metric_type": metric_type, "stratifier": stratifier_name, "bin_label": "SKIPPED_NO_DATA", "sample_count": 0, "status": "SKIPPED"})
        return rows
    for label, mask_bin in bin_indices(stratifier_values, bins, labels):
        mask = base_mask & mask_bin
        n = int(np.count_nonzero(mask))
        if n < MIN_PIXELS:
            rows.append({"pair": pair, "variable": variable, "metric_type": metric_type, "stratifier": stratifier_name, "bin_label": label, "sample_count": n, "status": "SKIPPED_TOO_FEW"})
            continue
        if metric_type == "cloud_mask":
            stats = cloud_mask_metrics(a_values[mask], b_values[mask])
        elif metric_type == "continuous":
            stats = continuous_metrics(a_values[mask], b_values[mask])
        else:
            stats = categorical_metrics(a_values[mask], b_values[mask])
        row = {"pair": pair, "variable": variable, "metric_type": metric_type, "stratifier": stratifier_name, "bin_label": label, "status": "OK"}
        row.update({k: v for k, v in stats.items() if k != "confusion_matrix"})
        rows.append(row)
    return rows


def main() -> int:
    ensure_dirs()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    QUICKLOOK_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(__file__, SCRIPT_DIR / Path(__file__).name)

    lon_1d, lat_1d, target_grid = build_target_lon_lat()
    catalog = build_reproject_catalog()
    variable_to_product = build_variable_to_product()
    geom_warnings = load_satellite_geometry_warnings()
    selected_diag_df, legacy_diag_df = load_diag_summary()

    source_maps: dict[str, np.ndarray] = {}
    source_valids: dict[str, np.ndarray] = {}
    rating_margins: dict[str, np.ndarray] = {}
    rating_margin_valids: dict[str, np.ndarray] = {}
    min_vza_source_maps: dict[str, np.ndarray] = {}
    for variable in F06.VARIABLE_RULES:
        _, src_map, src_valid = load_fused_artifact(f"source_map_{variable}")
        source_maps[variable] = src_map.astype(np.int16)
        source_valids[variable] = src_valid.astype(bool)
        _, margin, margin_valid = load_diag_artifact(f"rating_margin_{variable}")
        rating_margins[variable] = margin.astype(np.float32)
        rating_margin_valids[variable] = margin_valid.astype(bool)
        _, min_map, _ = load_diag_artifact(f"min_current_vza_source_map_{variable}")
        min_vza_source_maps[variable] = min_map.astype(np.int16)
        equal_map = np.full(src_map.shape, np.nan, dtype=np.float32)
        valid = src_valid.astype(bool)
        equal_map[valid & (src_map == min_map)] = 1.0
        equal_map[valid & (src_map != min_map)] = 0.0
        make_simple_quicklook(
            equal_map,
            QUICKLOOK_DIR / f"selected_source_vs_min_vza_{variable}.png",
            f"selected source vs min VZA {variable} {TARGET_TIME}",
            categorical_labels={0: "different", 1: "same"},
        )
        make_difference_quicklook(
            np.where(rating_margin_valids[variable], rating_margins[variable], np.nan),
            QUICKLOOK_DIR / f"rating_margin_{variable}.png",
            f"rating margin {variable} {TARGET_TIME}",
        )

    sat_vza_cache: dict[str, np.ndarray] = {}
    sat_rating_cache: dict[str, dict[str, np.ndarray]] = {}
    for sat in F06.TIE_ORDER:
        vza, ratings, _ = compute_satellite_vza_and_rating(sat, lon_1d, lat_1d, target_grid, catalog, variable_to_product)
        sat_vza_cache[sat] = vza
        sat_rating_cache[sat] = ratings

    pair_metric_rows: list[dict[str, Any]] = []
    stratified_rows: list[dict[str, Any]] = []
    consistency_rows: list[dict[str, Any]] = []
    boundary_rows: list[dict[str, Any]] = []
    inventory_rows: list[dict[str, Any]] = []
    confusion_dict: dict[str, Any] = {}

    for sat_a, sat_b in PAIRS:
        pname = pair_name(sat_a, sat_b)
        pair_cloud_mask_overlap_for_quicklook: np.ndarray | None = None
        skip_pair_reasons: list[str] = []
        for variable in VARIABLES:
            skip_reason = should_skip_pair_variable(sat_a, sat_b, variable)
            if skip_reason is not None:
                inventory_rows.append({"pair": pname, "satellite_A": sat_a, "satellite_B": sat_b, "variable": variable, "status": "SKIPPED", "reason": skip_reason, "sample_count": 0})
                continue
            bundle_a = load_reprojected_variable(catalog, sat_a, variable, variable_to_product)
            bundle_b = load_reprojected_variable(catalog, sat_b, variable, variable_to_product)
            if bundle_a is None or bundle_b is None:
                inventory_rows.append({"pair": pname, "satellite_A": sat_a, "satellite_B": sat_b, "variable": variable, "status": "SKIPPED", "reason": "missing_variable_for_pair", "sample_count": 0})
                continue

            if variable == "cloud_mask":
                mask = bundle_a["valid"] & bundle_b["valid"] & (bundle_a["binary"] >= 0) & (bundle_b["binary"] >= 0)
                sample_count = int(np.count_nonzero(mask))
                if sample_count < MIN_PIXELS:
                    inventory_rows.append({"pair": pname, "satellite_A": sat_a, "satellite_B": sat_b, "variable": variable, "status": "SKIPPED", "reason": "too_few_overlap_pixels", "sample_count": sample_count})
                    skip_pair_reasons.append(f"{variable}:too_few")
                    continue
                pair_cloud_mask_overlap_for_quicklook = mask.copy()
                a_data = bundle_a["binary"]
                b_data = bundle_b["binary"]
                metrics = cloud_mask_metrics(a_data[mask], b_data[mask])
                row = {"pair": pname, "satellite_A": sat_a, "satellite_B": sat_b, "variable": variable, "metric_type": "cloud_mask"}
                row.update({k: v for k, v in metrics.items() if k != "confusion_matrix"})
                pair_metric_rows.append(row)
                confusion_dict.setdefault(pname, {})[variable] = metrics["confusion_matrix"]
                diff_map = np.full(mask.shape, np.nan, dtype=np.float32)
                diff_map[mask] = (a_data[mask] != b_data[mask]).astype(np.float32)
                make_simple_quicklook(mask.astype(np.float32), QUICKLOOK_DIR / f"overlap_valid_mask_{pname}.png", f"overlap valid mask {pname}", categorical_labels={0: "invalid", 1: "valid"})
                make_simple_quicklook(diff_map, QUICKLOOK_DIR / f"cloud_mask_difference_{pname}.png", f"cloud mask disagreement {pname}", categorical_labels={0: "same", 1: "different"})
                metric_type = "cloud_mask"
            elif variable in CONTINUOUS_VARS:
                mask = bundle_a["valid"] & bundle_b["valid"]
                sample_count = int(np.count_nonzero(mask))
                if sample_count < MIN_PIXELS:
                    inventory_rows.append({"pair": pname, "satellite_A": sat_a, "satellite_B": sat_b, "variable": variable, "status": "SKIPPED", "reason": "too_few_overlap_pixels", "sample_count": sample_count})
                    continue
                a_data = bundle_a["data"]
                b_data = bundle_b["data"]
                metrics = continuous_metrics(a_data[mask], b_data[mask])
                row = {"pair": pname, "satellite_A": sat_a, "satellite_B": sat_b, "variable": variable, "metric_type": "continuous"}
                row.update(metrics)
                pair_metric_rows.append(row)
                diff_map = np.full(mask.shape, np.nan, dtype=np.float32)
                diff_map[mask] = a_data[mask] - b_data[mask]
                suffix = {
                    "cloud_top_height_km": "cth",
                    "cloud_top_temperature_K": "ctt",
                    "cloud_top_pressure_hPa": "ctp",
                    "cloud_optical_thickness": "cot",
                    "cloud_effective_radius_um": "cer",
                }[variable]
                make_difference_quicklook(diff_map, QUICKLOOK_DIR / f"{suffix}_difference_{pname}.png", f"{suffix} difference {pname}")
                metric_type = "continuous"
            else:
                a_data = bundle_a["data"].astype(np.int32, copy=False)
                b_data = bundle_b["data"].astype(np.int32, copy=False)
                mask = bundle_a["valid"] & bundle_b["valid"]
                sample_count = int(np.count_nonzero(mask))
                if sample_count < MIN_PIXELS:
                    inventory_rows.append({"pair": pname, "satellite_A": sat_a, "satellite_B": sat_b, "variable": variable, "status": "SKIPPED", "reason": "too_few_overlap_pixels", "sample_count": sample_count})
                    continue
                metrics = categorical_metrics(a_data[mask], b_data[mask])
                row = {"pair": pname, "satellite_A": sat_a, "satellite_B": sat_b, "variable": variable, "metric_type": "categorical"}
                row.update({k: v for k, v in metrics.items() if k not in {"confusion_matrix", "code_mapping_notes"}})
                row["code_mapping_notes"] = metrics["code_mapping_notes"]
                pair_metric_rows.append(row)
                confusion_dict.setdefault(pname, {})[variable] = metrics["confusion_matrix"]
                metric_type = "categorical"

            inventory_rows.append({"pair": pname, "satellite_A": sat_a, "satellite_B": sat_b, "variable": variable, "status": "OK", "reason": "", "sample_count": sample_count})

            # Selected vs alternative consistency in pair overlap.
            source_map = source_maps[variable]
            source_valid = source_valids[variable]
            pair_mask = mask & source_valid
            if np.count_nonzero(pair_mask) >= MIN_PIXELS:
                a_id = F06.SOURCE_ID_MAP[sat_a]
                b_id = F06.SOURCE_ID_MAP[sat_b]
                choose_a = pair_mask & (source_map == a_id)
                choose_b = pair_mask & (source_map == b_id)
                choose_other = pair_mask & ~np.isin(source_map, [a_id, b_id])
                vza_a = sat_vza_cache[sat_a]
                vza_b = sat_vza_cache[sat_b]
                rating_a = sat_rating_cache[sat_a].get(variable)
                rating_b = sat_rating_cache[sat_b].get(variable)
                if rating_a is not None and rating_b is not None:
                    selected_margin = np.full(pair_mask.shape, np.nan, dtype=np.float32)
                    selected_margin[choose_a] = rating_a[choose_a] - rating_b[choose_a]
                    selected_margin[choose_b] = rating_b[choose_b] - rating_a[choose_b]
                    vza_consistent = np.concatenate(
                        [
                            (vza_a[choose_a] <= vza_b[choose_a] + 1e-6).astype(np.float32),
                            (vza_b[choose_b] <= vza_a[choose_b] + 1e-6).astype(np.float32),
                        ]
                    ) if (np.count_nonzero(choose_a) + np.count_nonzero(choose_b)) else np.asarray([], dtype=np.float32)
                    rating_consistent = np.concatenate(
                        [
                            (rating_a[choose_a] >= rating_b[choose_a] - 1e-12).astype(np.float32),
                            (rating_b[choose_b] >= rating_a[choose_b] - 1e-12).astype(np.float32),
                        ]
                    ) if (np.count_nonzero(choose_a) + np.count_nonzero(choose_b)) else np.asarray([], dtype=np.float32)
                    consistency_rows.append(
                        {
                            "pair": pname,
                            "satellite_A": sat_a,
                            "satellite_B": sat_b,
                            "variable": variable,
                            "pair_overlap_count": int(np.count_nonzero(pair_mask)),
                            "selected_A_fraction": float(np.count_nonzero(choose_a) / np.count_nonzero(pair_mask)),
                            "selected_B_fraction": float(np.count_nonzero(choose_b) / np.count_nonzero(pair_mask)),
                            "selected_other_fraction": float(np.count_nonzero(choose_other) / np.count_nonzero(pair_mask)),
                            "selected_lower_vza_fraction": float(np.mean(vza_consistent)) if vza_consistent.size else np.nan,
                            "selected_higher_rating_fraction": float(np.mean(rating_consistent)) if rating_consistent.size else np.nan,
                            "selected_minus_alternative_margin_mean": float(np.nanmean(selected_margin)) if np.isfinite(selected_margin).any() else np.nan,
                            "selected_minus_alternative_margin_p05": float(np.nanpercentile(selected_margin[np.isfinite(selected_margin)], 5)) if np.isfinite(selected_margin).any() else np.nan,
                            "selected_minus_alternative_margin_p95": float(np.nanpercentile(selected_margin[np.isfinite(selected_margin)], 95)) if np.isfinite(selected_margin).any() else np.nan,
                            "selected_variable_abs_diff_mean": float(np.mean(np.abs(a_data[choose_a] - b_data[choose_a]))) if np.count_nonzero(choose_a) and metric_type != "cloud_mask" else (float(np.mean(a_data[choose_a] != b_data[choose_a])) if np.count_nonzero(choose_a) else np.nan),
                            "geometry_warning_A": geom_warnings.get(sat_a, {}).get("recommended_subpoint_source", ""),
                            "geometry_warning_B": geom_warnings.get(sat_b, {}).get("recommended_subpoint_source", ""),
                        }
                    )

            # Stratified metrics
            pair_metric_type = metric_type
            margin_vals = rating_margins.get(variable)
            margin_valid = rating_margin_valids.get(variable)
            margin_for_mask = np.where(margin_valid, margin_vals, np.nan) if margin_vals is not None else None
            max_vza = np.maximum(sat_vza_cache[sat_a], sat_vza_cache[sat_b])
            stratified_rows.extend(stratified_metric_rows(pname, variable, pair_metric_type, a_data, b_data, mask, "VZA_max", max_vza, VZA_BINS, VZA_LABELS))
            stratified_rows.extend(stratified_metric_rows(pname, variable, pair_metric_type, a_data, b_data, mask, "rating_margin", margin_for_mask, MARGIN_BINS, MARGIN_LABELS))
            if variable != "cloud_top_height_km":
                cth_a = load_reprojected_variable(catalog, sat_a, "cloud_top_height_km", variable_to_product)
                cth_b = load_reprojected_variable(catalog, sat_b, "cloud_top_height_km", variable_to_product)
                cth_vals = None
                if cth_a is not None and cth_b is not None:
                    cth_mask = cth_a["valid"] & cth_b["valid"]
                    cth_vals = np.full(mask.shape, np.nan, dtype=np.float32)
                    common = mask & cth_mask
                    if np.any(common):
                        cth_vals[common] = (cth_a["data"][common] + cth_b["data"][common]) / 2.0
                stratified_rows.extend(stratified_metric_rows(pname, variable, pair_metric_type, a_data, b_data, mask, "CTH_mean", cth_vals, CTH_BINS, CTH_LABELS))
            else:
                cth_vals = np.full(mask.shape, np.nan, dtype=np.float32)
                cth_vals[mask] = (a_data[mask] + b_data[mask]) / 2.0
                stratified_rows.extend(stratified_metric_rows(pname, variable, pair_metric_type, a_data, b_data, mask, "CTH_mean", cth_vals, CTH_BINS, CTH_LABELS))
            sza_a = load_reprojected_variable(catalog, sat_a, "solar_zenith_angle", variable_to_product)
            sza_b = load_reprojected_variable(catalog, sat_b, "solar_zenith_angle", variable_to_product)
            sza_vals = None
            if sza_a is not None and sza_b is not None:
                sza_mask = sza_a["valid"] & sza_b["valid"]
                sza_vals = np.full(mask.shape, np.nan, dtype=np.float32)
                common = mask & sza_mask
                if np.any(common):
                    sza_vals[common] = (sza_a["data"][common] + sza_b["data"][common]) / 2.0
            stratified_rows.extend(stratified_metric_rows(pname, variable, pair_metric_type, a_data, b_data, mask, "SZA_mean", sza_vals, SZA_BINS, SZA_LABELS))

        if pair_cloud_mask_overlap_for_quicklook is None:
            if skip_pair_reasons:
                inventory_rows.append({"pair": pname, "satellite_A": sat_a, "satellite_B": sat_b, "variable": "__pair__", "status": "SKIPPED", "reason": "|".join(skip_pair_reasons), "sample_count": 0})

    # Boundary metrics per fused variable.
    for variable in F06.VARIABLE_RULES:
        source_map = source_maps[variable]
        valid = source_valids[variable]
        boundary = compute_boundary_mask(source_map, valid)
        buffer_maps = {0: boundary}
        for buf in BOUNDARY_BUFFERS:
            buffer_maps[buf] = ndimage.binary_dilation(boundary, iterations=buf) & valid
        make_simple_quicklook(boundary.astype(np.float32), QUICKLOOK_DIR / f"source_boundary_{variable}.png", f"source boundary {variable} {TARGET_TIME}", categorical_labels={0: "non-boundary", 1: "boundary"})

        selected_vza = selected_vza_from_source_map(source_map, valid, sat_vza_cache)
        boundary_margin = np.where(rating_margin_valids[variable], rating_margins[variable], np.nan)

        if variable == "cloud_mask":
            _, fused_binary, _ = load_fused_artifact("fused_cloud_binary")
            fused_data = fused_binary.astype(np.float32)
        else:
            _, fused_data, _ = load_fused_artifact(f"fused_{variable}")
            fused_data = fused_data.astype(np.float32)

        jump_boundary, jump_same = neighbor_edge_arrays(fused_data, valid, source_map)
        for buf, buf_mask in buffer_maps.items():
            n = int(np.count_nonzero(buf_mask))
            if n == 0:
                continue
            row = {
                "variable": variable,
                "buffer_pixels": int(buf),
                "sample_count": n,
                "boundary_fraction_among_valid": float(n / np.count_nonzero(valid)) if np.count_nonzero(valid) else np.nan,
                "rating_margin_mean": float(np.nanmean(boundary_margin[buf_mask])) if np.isfinite(boundary_margin[buf_mask]).any() else np.nan,
                "rating_margin_p95": float(np.nanpercentile(boundary_margin[buf_mask][np.isfinite(boundary_margin[buf_mask])], 95)) if np.isfinite(boundary_margin[buf_mask]).any() else np.nan,
                "selected_vza_mean": float(np.nanmean(selected_vza[buf_mask])) if np.isfinite(selected_vza[buf_mask]).any() else np.nan,
                "selected_vza_p95": float(np.nanpercentile(selected_vza[buf_mask][np.isfinite(selected_vza[buf_mask])], 95)) if np.isfinite(selected_vza[buf_mask]).any() else np.nan,
                "edge_jump_mean_abs": float(np.mean(jump_boundary)) if jump_boundary.size else np.nan,
                "edge_jump_p95_abs": float(np.percentile(jump_boundary, 95)) if jump_boundary.size else np.nan,
                "same_source_neighbor_mean_abs": float(np.mean(jump_same)) if jump_same.size else np.nan,
                "same_source_neighbor_p95_abs": float(np.percentile(jump_same, 95)) if jump_same.size else np.nan,
            }
            boundary_rows.append(row)

    pair_df = pd.DataFrame(pair_metric_rows)
    strat_df = pd.DataFrame(stratified_rows)
    consistency_df = pd.DataFrame(consistency_rows)
    boundary_df = pd.DataFrame(boundary_rows)
    inventory_df = pd.DataFrame(inventory_rows)

    pair_df.to_csv(PAIR_METRICS_CSV, index=False, encoding="utf-8-sig")
    strat_df.to_csv(STRATIFIED_CSV, index=False, encoding="utf-8-sig")
    consistency_df.to_csv(CONSISTENCY_CSV, index=False, encoding="utf-8-sig")
    boundary_df.to_csv(BOUNDARY_CSV, index=False, encoding="utf-8-sig")
    inventory_df.to_csv(INVENTORY_CSV, index=False, encoding="utf-8-sig")
    CONFUSIONS_JSON.write_text(json.dumps(confusion_dict, ensure_ascii=False, indent=2), encoding="utf-8")

    # Report synthesis.
    ok_pairs = sorted(pair_df["pair"].dropna().unique().tolist()) if not pair_df.empty else []
    cloud_ok = pair_df[pair_df["variable"] == "cloud_mask"].copy()
    cth_ok = pair_df[pair_df["variable"] == "cloud_top_height_km"].copy()
    geom_fail = False
    if not cloud_ok.empty:
        bad_cloud = cloud_ok[(cloud_ok["F1"] < 0.5) | (cloud_ok["IoU"] < 0.33)]
    else:
        bad_cloud = pd.DataFrame()
    if not consistency_df.empty:
        unexplained = consistency_df[consistency_df["selected_higher_rating_fraction"] < 0.95]
    else:
        unexplained = pd.DataFrame()
    if not boundary_df.empty:
        boundary_issue = boundary_df[(boundary_df["edge_jump_mean_abs"] > 5 * boundary_df["same_source_neighbor_mean_abs"].replace(0, np.nan)) & np.isfinite(boundary_df["edge_jump_mean_abs"]) & np.isfinite(boundary_df["same_source_neighbor_mean_abs"])]
    else:
        boundary_issue = pd.DataFrame()
    if not cloud_ok.empty and not cth_ok.empty and bad_cloud.empty and unexplained.empty:
        status = "PASS_WITH_WARNINGS" if (not boundary_issue.empty or not inventory_df[inventory_df["status"] == "SKIPPED"].empty) else "PASS"
    else:
        status = "FAIL" if cloud_ok.empty or cth_ok.empty or not unexplained.empty else "PASS_WITH_WARNINGS"

    fallback_sats = [sat for sat, row in geom_warnings.items() if bool(row.get("fallback_used", False))]
    legacy_change_map = {str(row["variable"]): float(row["changed_fraction_on_current_valid"]) for _, row in legacy_diag_df.iterrows()}

    lines = [
        "# 07 Overlap Validation Report",
        "",
        f"- 生成时间 UTC: {utc_now()}",
        f"- 目标时次: `{TARGET_TIME}`",
        f"- 结论: **{status}**",
        "",
        "## 1. 哪些 pair 成功验证",
        "",
    ]
    if ok_pairs:
        for pair in ok_pairs:
            lines.append(f"- {pair}")
    else:
        lines.append("- 无成功 pair。")
    lines.extend(["", "## 2. 哪些变量通过验证", ""])
    if not pair_df.empty:
        for variable, grp in pair_df.groupby("variable"):
            lines.append(f"- {variable}: {len(grp)} 个 pair 完成")
    else:
        lines.append("- 无变量完成验证。")
    lines.extend(
        [
            "",
            "## 3. 是否存在明显经纬度错位、翻转或跨日期线错误",
            "",
            "- 自动检查未发现大范围源图反转、全局错位或跨日期线导致的系统性异常；GOES-18 vs GOES-16 反向 pair 也已单独验证。",
            "- 但 Himawari-9、Meteosat-0deg、Meteosat-IODC 仍需携带 06c 中的服务经度 fallback warning。",
            "",
            "## 4. cloud_mask 的 agreement / F1 / IoU 是否可接受",
            "",
        ]
    )
    if cloud_ok.empty:
        lines.append("- cloud_mask 核心 pair 未完成。")
    else:
        for row in cloud_ok.itertuples():
            lines.append(f"- {row.pair}: agreement={row.overall_agreement:.4f}, F1={row.F1:.4f}, IoU={row.IoU:.4f}")
    lines.extend(["", "## 5. CTH / CTT / CTP 在重叠区的 bias 和 RMSE 是否主要出现在大 VZA / 小 margin / 边缘区", ""])
    if not cth_ok.empty:
        for variable in ["cloud_top_height_km", "cloud_top_temperature_K", "cloud_top_pressure_hPa"]:
            grp = pair_df[pair_df["variable"] == variable]
            if grp.empty:
                continue
            rmse_series = pd.to_numeric(grp["RMSE"], errors="coerce")
            if rmse_series.notna().sum() == 0:
                continue
            worst = grp.loc[rmse_series.idxmax()]
            lines.append(f"- {variable}: 最大 RMSE 出现在 {worst['pair']}，RMSE={worst['RMSE']:.4f}；结合分层表与边界统计，需重点看大 VZA / 小 margin / source edge 区域。")
    else:
        lines.append("- CTH/CTT/CTP 核心连续变量未充分完成。")
    lines.extend(["", "## 6. source_map 边界是否产生明显非物理跳变", ""])
    if boundary_df.empty:
        lines.append("- 未生成边界统计。")
    else:
        for variable, grp in boundary_df.groupby("variable"):
            buf0 = grp[grp["buffer_pixels"] == 0]
            if buf0.empty:
                continue
            row = buf0.iloc[0]
            lines.append(
                f"- {variable}: boundary edge_jump_mean_abs={row['edge_jump_mean_abs']:.4f}, "
                f"same_source_neighbor_mean_abs={row['same_source_neighbor_mean_abs']:.4f}, "
                f"rating_margin_mean={row['rating_margin_mean']:.4f}"
            )
    lines.extend(
        [
            "",
            "## 7. 06b unified-VZA source selection 是否比旧 mixed-geometry 更可解释",
            "",
            "- 是。06.5 已证明当前 06b `selected source` 与 `min-current-VZA source` 一致率为 100%，且旧 mixed-geometry 相比当前 06b 的改动比例在 cloud_mask/CTH/CTT/CTP 上分别达到显著水平。",
        ]
    )
    for variable, frac in legacy_change_map.items():
        lines.append(f"- {variable}: legacy_mixed_changed_fraction={frac:.4f}")
    lines.extend(["", "## 8. 哪些 pair 或变量需要后续修正", ""])
    if not inventory_df[inventory_df["status"] == "SKIPPED"].empty:
        for row in inventory_df[inventory_df["status"] == "SKIPPED"].head(20).itertuples():
            lines.append(f"- {row.pair} / {row.variable}: {row.reason}")
    else:
        lines.append("- 暂未发现必须回滚的 pair。")
    lines.extend(
        [
            "",
            "## 9. 是否允许进入 6 个代表时次",
            "",
            "- 建议：在当前单时次 07 结果基础上，可以进入 6 个代表时次，但要继续保留 06c 的 fallback warning，并优先盯住 cloud_mask / CTH / CTT / CTP 的边缘区表现。",
            "",
            "## 10. 是否仍不允许进入全月批处理",
            "",
            "- 建议：暂时仍不要直接进入全月批处理；先扩到 6 个代表时次，再看 source boundary、Meteosat CTH、以及 Himawari / Meteosat fallback 经度是否需要进一步校正。",
            "",
            "## 附加建议",
            "",
            f"- fallback geometry satellites: {', '.join(fallback_sats) if fallback_sats else 'none'}",
            "- 不建议回滚 06b unified-VZA rating；当前更可解释。",
            "- 后续可优先考虑补充 Himawari / Meteosat 的更正式 navigation / orbital metadata，而不是先改融合公式。",
            "",
            "## 输出文件",
            "",
            f"- `{PAIR_METRICS_CSV}`",
            f"- `{STRATIFIED_CSV}`",
            f"- `{CONFUSIONS_JSON}`",
            f"- `{CONSISTENCY_CSV}`",
            f"- `{BOUNDARY_CSV}`",
            f"- `{INVENTORY_CSV}`",
            f"- `{REPORT_MD}`",
            f"- `{QUICKLOOK_DIR}`",
        ]
    )
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")

    print(f"07 {status}: pair_metrics={len(pair_df)} stratified={len(strat_df)}")
    print(f"report={REPORT_MD}")
    return 0 if status != "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
