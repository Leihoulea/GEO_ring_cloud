from __future__ import annotations

import csv
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from scipy.ndimage import maximum_filter, minimum_filter, uniform_filter
from scipy.stats import rankdata

from ..adapters.epic import read_epic_cth
from ..sources import SOURCE_BY_KEY, SOURCE_ID_TO_KEY


PROJECT_ID = "geo_ring_cloud"
COMPONENT_ROLE = "diagnostics_library"
PROFILES = ("operational_baseline", "claas3_candidate")
POLICIES = {
    "A_inclusive_binary": {
        "epic": {1: 0, 2: 0, 3: 1, 4: 1},
        "geo": {0: 0, 1: 0, 2: 1, 3: 1},
        "classes": (0, 1),
        "cloud_class": 1,
    },
    "B_high_confidence_only": {
        "epic": {1: 0, 4: 1},
        "geo": {0: 0, 3: 1},
        "classes": (0, 1),
        "cloud_class": 1,
    },
    "C_uncertainty_aware_3class": {
        "epic": {1: 0, 2: 1, 3: 1, 4: 2},
        "geo": {0: 0, 1: 1, 2: 1, 3: 2},
        "classes": (0, 1, 2),
        "cloud_class": 2,
    },
}
AGGREGATIONS = ("nearest", "box_3x3", "box_5x5", "box_7x7")
LAT_BINS = (("0-20", 0, 20), ("20-40", 20, 40), ("40-60", 40, 60), ("60-70", 60, 70), ("70-80", 70, 80), (">=80", 80, 91))
VZA_BINS = (("0-20", 0, 20), ("20-40", 20, 40), ("40-60", 40, 60), ("60-70", 60, 70), (">=70", 70, 181))
SZA_BINS = (("0-30", 0, 30), ("30-50", 30, 50), ("50-70", 50, 70), ("70-80", 70, 80), (">=80", 80, 181))
VALID_COUNT_BINS = (("1", 1, 2), ("2", 2, 3), ("3", 3, 4), (">=4", 4, 100))
SOURCE_STREAM = {key: value.processing_stream for key, value in SOURCE_BY_KEY.items()}
PRIMARY_PREFUSION_SPECS = {
    "Meteosat-0deg": {"profile": "operational_baseline", "mask_product": "CLM", "cth_product": "CTH"},
    "CLAAS3-0deg": {"profile": "claas3_candidate", "mask_product": "CMA", "cth_product": "CTX"},
    "GOES-16": {"profile": "operational_baseline", "mask_product": "ACMF", "cth_product": "ACHAF"},
    "Meteosat-IODC": {"profile": "operational_baseline", "mask_product": "CLM", "cth_product": "CTH"},
}
PRIMARY_PAIRS = (
    ("Meteosat-0deg", "CLAAS3-0deg"),
    ("Meteosat-0deg", "GOES-16"),
    ("CLAAS3-0deg", "GOES-16"),
    ("Meteosat-0deg", "Meteosat-IODC"),
    ("CLAAS3-0deg", "Meteosat-IODC"),
)

__all__ = [
    "PROJECT_ID", "COMPONENT_ROLE", "PROFILES", "POLICIES", "AGGREGATIONS",
    "LAT_BINS", "VZA_BINS", "SZA_BINS", "VALID_COUNT_BINS", "SOURCE_STREAM",
    "PRIMARY_PREFUSION_SPECS", "PRIMARY_PAIRS", "utc_now", "write_csv", "read_csv",
    "load_npz", "matrix_context", "epic_time_from_name", "time_delta_minutes", "load_grid",
    "read_epic", "apply_policy", "row_col", "sample_nearest", "aggregate_mask_samples",
    "aggregate_height_samples", "epic_morphology", "numeric_bin_masks", "base_strata",
    "add_profile_strata", "add_visibility_strata", "source_domains", "classification_metrics",
    "finite_mean", "paired_classification_metrics", "height_metrics", "paired_height_metrics",
    "correlation", "rank_values", "height_class", "find_prefusion_layer", "prefusion_samples",
    "sample_fused_profile", "sample_fused_aux", "source_stability_samples", "selected_geo_vza",
    "build_context", "common_metadata", "block_bootstrap", "file_sha256",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def load_npz(path: Path) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    with np.load(path, allow_pickle=False) as z:
        data = np.asarray(z["data"])
        if "fusion_valid_mask" in z.files:
            valid = np.asarray(z["fusion_valid_mask"]).astype(bool)
        elif "valid_mask" in z.files:
            valid = np.asarray(z["valid_mask"]).astype(bool)
        else:
            valid = np.isfinite(data)
        metadata: dict[str, Any] = {}
        if "metadata_json" in z.files:
            try:
                metadata = json.loads(str(np.asarray(z["metadata_json"]).item()))
            except (json.JSONDecodeError, TypeError, ValueError):
                metadata = {"metadata_json_parse_error": True}
    return data, valid, metadata


def matrix_context(path: Path) -> dict[str, Any]:
    matrix = json.loads(path.read_text(encoding="utf-8"))
    profiles = {row["source_profile"]: Path(row["profile_root"]) for row in matrix["profile_runs"]}
    if set(profiles) != set(PROFILES):
        raise RuntimeError(f"profile matrix incomplete: {path}")
    common = matrix["common_inputs"]
    epic_time = str(common.get("epic_time_utc", ""))
    geo_time = str(common["target_time"])
    delta = common.get("time_delta_min")
    if delta is None:
        epic_time = epic_time or epic_time_from_name(Path(common["epic_l2"]))
        delta = time_delta_minutes(epic_time, geo_time) if epic_time else math.nan
    return {
        "matrix_path": path,
        "matrix": matrix,
        "profiles": profiles,
        "run_id": matrix["run_id"],
        "time_tag": common["time_tag"],
        "geo_time": geo_time,
        "epic_time": epic_time,
        "time_delta_min": float(delta),
        "epic_path": Path(common["epic_l2"]),
    }


def epic_time_from_name(path: Path) -> str:
    import re

    match = re.search(r"_(20\d{12})_", path.name)
    if not match:
        return ""
    value = match.group(1)
    return f"{value[0:4]}-{value[4:6]}-{value[6:8]}T{value[8:10]}:{value[10:12]}:{value[12:14]}Z"


def time_delta_minutes(epic_time: str, geo_time: str) -> float:
    def parse(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    return abs((parse(epic_time) - parse(geo_time)).total_seconds()) / 60.0


def load_grid(profile_root: Path) -> dict[str, Any]:
    path = profile_root / "reprojected_grid" / "target_grid_definition.json"
    grid = json.loads(path.read_text(encoding="utf-8"))
    expected = {"lat_min": -90.0, "lat_max": 90.0, "lon_min": -180.0, "lon_max": 180.0, "resolution_degree": 0.05, "lat_size": 3600, "lon_size": 7200}
    failures = {key: (grid.get(key), value) for key, value in expected.items() if float(grid.get(key, math.nan)) != float(value)}
    if failures:
        raise RuntimeError(f"target-grid contract mismatch: {failures}")
    return grid


def read_epic(path: Path) -> dict[str, Any]:
    a = read_epic_cth(path, "geophysical_data/A-band_Effective_Cloud_Height")
    b = read_epic_cth(path, "geophysical_data/B-band_Effective_Cloud_Height")
    return {
        "lat": a["lat"],
        "lon": a["lon"],
        "cloud_mask": a["cloud_mask"],
        "epic_vza": a["epic_vza"],
        "sza": a["sza"],
        "A_band": a["cth_km"],
        "A_band_valid": a["cth_valid"],
        "B_band": b["cth_km"],
        "B_band_valid": b["cth_valid"],
    }


def apply_policy(values: np.ndarray, mapping: dict[int, int]) -> tuple[np.ndarray, np.ndarray]:
    out = np.full(values.shape, -1, dtype=np.int8)
    valid = np.zeros(values.shape, dtype=bool)
    for raw, mapped in mapping.items():
        hit = np.isfinite(values) & (values == raw)
        out[hit] = mapped
        valid |= hit
    return out, valid


def row_col(lat: np.ndarray, lon: np.ndarray, grid: dict[str, Any], shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    resolution = float(grid["resolution_degree"])
    lat0 = float(grid["lat_centers_first_last"][0])
    lon0 = float(grid["lon_centers_first_last"][0])
    lon_norm = np.full(lon.shape, np.nan, dtype=np.float32)
    finite_lon = np.isfinite(lon)
    lon_norm[finite_lon] = ((lon[finite_lon].astype(np.float32) + 180.0) % 360.0) - 180.0
    row = np.zeros(lat.shape, dtype=np.int32)
    col = np.zeros(lon.shape, dtype=np.int32)
    finite = np.isfinite(lat) & np.isfinite(lon_norm)
    row[finite] = np.rint((lat[finite].astype(np.float32) - lat0) / resolution).astype(np.int32)
    col[finite] = np.rint((lon_norm[finite] - lon0) / resolution).astype(np.int32)
    ok = finite & (row >= 0) & (row < shape[0]) & (col >= 0) & (col < shape[1])
    return row, col, ok


def sample_nearest(data: np.ndarray, valid: np.ndarray, lat: np.ndarray, lon: np.ndarray, grid: dict[str, Any], fill: float = np.nan) -> tuple[np.ndarray, np.ndarray]:
    row, col, ok = row_col(lat, lon, grid, data.shape)
    out = np.full(lat.shape, fill, dtype=np.float32)
    out_valid = np.zeros(lat.shape, dtype=bool)
    out[ok] = data[row[ok], col[ok]].astype(np.float32)
    out_valid[ok] = valid[row[ok], col[ok]]
    out[~out_valid] = fill
    return out, out_valid


def aggregate_mask_samples(raw: np.ndarray, valid: np.ndarray, policy_name: str, lat: np.ndarray, lon: np.ndarray, grid: dict[str, Any], min_support: float = 0.5) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    policy = POLICIES[policy_name]
    classes, policy_valid = apply_policy(raw, policy["geo"])
    valid_grid = valid & policy_valid
    nearest, nearest_valid = sample_nearest(classes, valid_grid, lat, lon, grid, fill=-1)
    result = {"nearest": (nearest.astype(np.int8), nearest_valid)}
    for window in (3, 5, 7):
        support = uniform_filter(valid_grid.astype(np.float32), size=window, mode="constant", cval=0.0)
        out_valid_grid = support >= min_support
        if len(policy["classes"]) == 2:
            cloud = (classes == policy["cloud_class"]) & valid_grid
            cloud_fraction = uniform_filter(cloud.astype(np.float32), size=window, mode="constant", cval=0.0)
            ratio = np.zeros(raw.shape, dtype=np.float32)
            ratio[out_valid_grid] = cloud_fraction[out_valid_grid] / support[out_valid_grid]
            aggregated = (ratio >= 0.5).astype(np.int8)
        else:
            aggregated = np.full(raw.shape, -1, dtype=np.int8)
            best = np.full(raw.shape, -1.0, dtype=np.float32)
            for label in policy["classes"]:
                count = uniform_filter(((classes == label) & valid_grid).astype(np.float32), size=window, mode="constant", cval=0.0)
                choose = out_valid_grid & (count > best)
                aggregated[choose] = label
                best[choose] = count[choose]
        sampled, sampled_valid = sample_nearest(aggregated, out_valid_grid, lat, lon, grid, fill=-1)
        result[f"box_{window}x{window}"] = (sampled.astype(np.int8), sampled_valid)
    return result


def aggregate_height_samples(data: np.ndarray, valid: np.ndarray, lat: np.ndarray, lon: np.ndarray, grid: dict[str, Any], min_support: float = 0.25) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    physical = valid & np.isfinite(data) & (data >= 0) & (data <= 25)
    result = {"nearest": sample_nearest(data, physical, lat, lon, grid)}
    values = np.where(physical, data, 0.0).astype(np.float32)
    for window in (3, 5, 7):
        support = uniform_filter(physical.astype(np.float32), size=window, mode="constant", cval=0.0)
        numerator = uniform_filter(values, size=window, mode="constant", cval=0.0)
        out_valid = support > min_support
        averaged = np.full(data.shape, np.nan, dtype=np.float32)
        averaged[out_valid] = numerator[out_valid] / support[out_valid]
        result[f"box_{window}x{window}"] = sample_nearest(averaged, out_valid, lat, lon, grid)
    return result


def epic_morphology(epic_cloud_mask: np.ndarray) -> dict[str, np.ndarray]:
    classes, valid = apply_policy(epic_cloud_mask, POLICIES["A_inclusive_binary"]["epic"])
    cloud = (classes == 1) & valid
    support = uniform_filter(valid.astype(np.float32), size=3, mode="constant", cval=0.0)
    numerator = uniform_filter(cloud.astype(np.float32), size=3, mode="constant", cval=0.0)
    fraction = np.full(classes.shape, np.nan, dtype=np.float32)
    ok = support > 0
    fraction[ok] = numerator[ok] / support[ok]
    scene = np.full(classes.shape, -1, dtype=np.int8)
    scene[ok & (fraction <= 0.1)] = 0
    scene[ok & (fraction > 0.1) & (fraction < 0.9)] = 1
    scene[ok & (fraction >= 0.9)] = 2
    boundary = valid & (fraction > 0) & (fraction < 1)
    return {"valid": valid, "fraction": fraction, "scene": scene, "boundary": boundary}


def numeric_bin_masks(values: np.ndarray, bins: Iterable[tuple[str, float, float]], prefix: str) -> dict[str, np.ndarray]:
    return {f"{prefix}:{label}": np.isfinite(values) & (values >= lo) & (values < hi) for label, lo, hi in bins}


def base_strata(epic: dict[str, Any], morphology: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    shape = epic["lat"].shape
    strata: dict[str, np.ndarray] = {"all": np.ones(shape, dtype=bool)}
    strata.update({
        "scene_type:homogeneous_clear": morphology["scene"] == 0,
        "scene_type:broken_cloud": morphology["scene"] == 1,
        "scene_type:homogeneous_cloud": morphology["scene"] == 2,
        "boundary_class:near_boundary_1cell": morphology["boundary"],
        "boundary_class:non_boundary": morphology["valid"] & ~morphology["boundary"],
        "visibility:abs_lat_lt70": np.isfinite(epic["lat"]) & (np.abs(epic["lat"]) < 70),
        "visibility:abs_lat_lt60": np.isfinite(epic["lat"]) & (np.abs(epic["lat"]) < 60),
    })
    strata.update(numeric_bin_masks(np.abs(epic["lat"]), LAT_BINS, "abs_lat_bin"))
    strata.update(numeric_bin_masks(epic["epic_vza"], VZA_BINS, "EPIC_VZA_bin"))
    strata.update(numeric_bin_masks(epic["sza"], SZA_BINS, "SZA_bin"))
    return strata


def add_profile_strata(strata: dict[str, np.ndarray], base_source: np.ndarray, cand_source: np.ndarray, base_count: np.ndarray, cand_count: np.ndarray, base_geo_vza: np.ndarray | None = None, cand_geo_vza: np.ndarray | None = None) -> dict[str, np.ndarray]:
    result = dict(strata)
    source_valid = np.isfinite(base_source) & np.isfinite(cand_source)
    base_id = np.zeros(base_source.shape, dtype=np.int16)
    cand_id = np.zeros(cand_source.shape, dtype=np.int16)
    base_id[source_valid] = np.rint(base_source[source_valid]).astype(np.int16)
    cand_id[source_valid] = np.rint(cand_source[source_valid]).astype(np.int16)
    for left, right in sorted(set(zip(base_id[source_valid].tolist(), cand_id[source_valid].tolist()))):
        result[f"source_transition:{left}->{right}"] = source_valid & (base_id == left) & (cand_id == right)
    common_count = np.minimum(base_count, cand_count)
    result.update(numeric_bin_masks(common_count, VALID_COUNT_BINS, "valid_source_count_bin"))
    if base_geo_vza is not None and cand_geo_vza is not None:
        both = np.isfinite(base_geo_vza) & np.isfinite(cand_geo_vza)
        worst = np.full(base_geo_vza.shape, np.nan, dtype=np.float32)
        worst[both] = np.maximum(base_geo_vza[both], cand_geo_vza[both])
        result.update(numeric_bin_masks(worst, VZA_BINS, "GEO_VZA_bin"))
        result["visibility:reliable_geometry"] = both & (worst < 70)
        result["visibility:clean_geometry"] = both & (worst < 60)
    return result


def add_visibility_strata(
    strata: dict[str, np.ndarray],
    epic: dict[str, Any],
    morphology: dict[str, np.ndarray],
    base_source_valid: np.ndarray,
    cand_source_valid: np.ndarray,
    base_count: np.ndarray,
    cand_count: np.ndarray,
    base_geo_vza: np.ndarray,
    cand_geo_vza: np.ndarray,
) -> dict[str, np.ndarray]:
    result = dict(strata)
    vis1 = base_source_valid & cand_source_valid & (base_count >= 1) & (cand_count >= 1)
    abs_lat = np.abs(epic["lat"])
    both_geo = np.isfinite(base_geo_vza) & np.isfinite(cand_geo_vza)
    worst_geo = np.full(abs_lat.shape, np.nan, dtype=np.float32)
    worst_geo[both_geo] = np.maximum(base_geo_vza[both_geo], cand_geo_vza[both_geo])
    non_boundary = morphology["valid"] & ~morphology["boundary"]
    homogeneous = (morphology["scene"] == 0) | (morphology["scene"] == 2)
    result.update({
        "VIS-0:all_EPIC_earth": np.isfinite(epic["lat"]) & np.isfinite(epic["lon"]),
        "VIS-1:both_profiles_valid_source": vis1,
        "VIS-2:VIS1_abs_lat_lt70": vis1 & (abs_lat < 70),
        "VIS-3:VIS1_abs_lat_lt60": vis1 & (abs_lat < 60),
        "VIS-4a:geometry_screen_without_GEO_VZA": vis1 & (abs_lat < 70) & (epic["epic_vza"] < 70) & (epic["sza"] < 80),
        "VIS-4b:geometry_screen_with_GEO_VZA": vis1 & (abs_lat < 70) & (epic["epic_vza"] < 70) & (epic["sza"] < 80) & both_geo & (worst_geo < 70),
        "VIS-5:clean_core": vis1 & (abs_lat < 60) & (epic["epic_vza"] < 60) & (epic["sza"] < 70) & both_geo & (worst_geo < 60) & non_boundary & homogeneous,
        "VIS-6:non_boundary_lt60": vis1 & (abs_lat < 60) & non_boundary,
        "VIS-7:boundary_or_broken_lt60": vis1 & (abs_lat < 60) & (morphology["boundary"] | (morphology["scene"] == 1)),
    })
    return result


def source_domains(common: np.ndarray, base_source: np.ndarray, base_source_valid: np.ndarray, cand_source: np.ndarray, cand_source_valid: np.ndarray) -> dict[str, np.ndarray]:
    source_common = common & base_source_valid & cand_source_valid & np.isfinite(base_source) & np.isfinite(cand_source)
    base_id = np.zeros(common.shape, dtype=np.int16)
    cand_id = np.zeros(common.shape, dtype=np.int16)
    base_id[source_common] = np.rint(base_source[source_common]).astype(np.int16)
    cand_id[source_common] = np.rint(cand_source[source_common]).astype(np.int16)
    unchanged = source_common & (base_id == cand_id) & ~np.isin(base_id, [0, 5, 7])
    return {
        "global_common": common,
        "replacement_active": source_common & (base_id == 5) & (cand_id == 7),
        "unchanged_control": unchanged,
    }


def classification_metrics(reference: np.ndarray, candidate: np.ndarray, valid: np.ndarray, classes: tuple[int, ...], cloud_class: int) -> dict[str, float | int]:
    n = int(np.count_nonzero(valid))
    if n == 0:
        return {"common_n": 0}
    ref = reference[valid]
    cand = candidate[valid]
    agreement = float(np.mean(ref == cand))
    recalls: list[float] = []
    f1s: list[float] = []
    class_counts: dict[str, int] = {}
    for label in classes:
        rp = ref == label
        cp = cand == label
        tp = int(np.count_nonzero(rp & cp))
        fp = int(np.count_nonzero(~rp & cp))
        fn = int(np.count_nonzero(rp & ~cp))
        precision = tp / (tp + fp) if tp + fp else math.nan
        recall = tp / (tp + fn) if tp + fn else math.nan
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else math.nan
        recalls.append(recall)
        f1s.append(f1)
        class_counts[f"class_{label}_tp"] = tp
        class_counts[f"class_{label}_fp"] = fp
        class_counts[f"class_{label}_fn"] = fn
    rp = ref == cloud_class
    cp = cand == cloud_class
    tp = int(np.count_nonzero(rp & cp))
    fp = int(np.count_nonzero(~rp & cp))
    fn = int(np.count_nonzero(rp & ~cp))
    precision = tp / (tp + fp) if tp + fp else math.nan
    recall = tp / (tp + fn) if tp + fn else math.nan
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else math.nan
    iou = tp / (tp + fp + fn) if tp + fp + fn else math.nan
    result: dict[str, float | int] = {
        "common_n": n,
        "agreement": agreement,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "iou": iou,
        "balanced_accuracy": finite_mean(recalls),
        "macro_f1": finite_mean(f1s),
        "cloud_fraction": float(np.mean(cp)),
        "reference_cloud_fraction": float(np.mean(rp)),
        "cloud_fraction_bias": float(np.mean(cp) - np.mean(rp)),
        "cloud_tp": tp,
        "cloud_fp": fp,
        "cloud_fn": fn,
        "cloud_tn": int(np.count_nonzero(~rp & ~cp)),
    }
    result.update(class_counts)
    return result


def finite_mean(values: Iterable[float]) -> float:
    finite = [float(value) for value in values if np.isfinite(value)]
    return float(np.mean(finite)) if finite else math.nan


def paired_classification_metrics(reference: np.ndarray, source_a: np.ndarray, source_b: np.ndarray, valid: np.ndarray, classes: tuple[int, ...], cloud_class: int) -> dict[str, Any]:
    a = classification_metrics(reference, source_a, valid, classes, cloud_class)
    b = classification_metrics(reference, source_b, valid, classes, cloud_class)
    if int(a.get("common_n", 0)) == 0:
        return {"common_n": 0}
    av = source_a[valid]
    bv = source_b[valid]
    rv = reference[valid]
    a_correct = av == rv
    b_correct = bv == rv
    result: dict[str, Any] = {"common_n": a["common_n"]}
    for key in ("agreement", "precision", "recall", "f1", "iou", "balanced_accuracy", "macro_f1", "cloud_fraction", "cloud_fraction_bias"):
        result[f"A_{key}"] = a.get(key, math.nan)
        result[f"B_{key}"] = b.get(key, math.nan)
        result[f"B_minus_A_{key}"] = float(b.get(key, math.nan)) - float(a.get(key, math.nan))
    for prefix, metrics in (("A", a), ("B", b)):
        for key, value in metrics.items():
            if key.startswith("class_") or key in {"cloud_tp", "cloud_fp", "cloud_fn", "cloud_tn"}:
                result[f"{prefix}_{key}"] = value
    result.update({
        "A_only_correct_fraction": float(np.mean(a_correct & ~b_correct)),
        "B_only_correct_fraction": float(np.mean(b_correct & ~a_correct)),
        "both_correct_fraction": float(np.mean(a_correct & b_correct)),
        "both_wrong_fraction": float(np.mean(~a_correct & ~b_correct)),
        "source_disagreement_fraction": float(np.mean(av != bv)),
    })
    return result


def height_metrics(reference: np.ndarray, candidate: np.ndarray, valid: np.ndarray) -> dict[str, Any]:
    n = int(np.count_nonzero(valid))
    if n == 0:
        return {"common_n": 0}
    ref = reference[valid].astype(np.float64)
    cand = candidate[valid].astype(np.float64)
    delta = cand - ref
    absolute = np.abs(delta)
    return {
        "common_n": n,
        "bias_km": float(np.mean(delta)),
        "mae_km": float(np.mean(absolute)),
        "rmse_km": float(np.sqrt(np.mean(delta * delta))),
        "median_abs_error_km": float(np.median(absolute)),
        "p90_abs_error_km": float(np.percentile(absolute, 90)),
        "within_1km_fraction": float(np.mean(absolute <= 1)),
        "within_2km_fraction": float(np.mean(absolute <= 2)),
        "within_3km_fraction": float(np.mean(absolute <= 3)),
        "pearson_corr": correlation(ref, cand, False),
        "spearman_corr": correlation(ref, cand, True),
        "low_mid_high_class_agreement": float(np.mean(height_class(ref) == height_class(cand))),
        "sum_error_km": float(np.sum(delta)),
        "sum_abs_error_km": float(np.sum(absolute)),
        "sum_squared_error_km2": float(np.sum(delta * delta)),
    }


def paired_height_metrics(reference: np.ndarray, source_a: np.ndarray, source_b: np.ndarray, valid: np.ndarray) -> dict[str, Any]:
    a = height_metrics(reference, source_a, valid)
    b = height_metrics(reference, source_b, valid)
    if int(a.get("common_n", 0)) == 0:
        return {"common_n": 0}
    av = source_a[valid].astype(np.float64)
    bv = source_b[valid].astype(np.float64)
    rv = reference[valid].astype(np.float64)
    ae = np.abs(av - rv)
    be = np.abs(bv - rv)
    result: dict[str, Any] = {"common_n": a["common_n"]}
    for key in ("bias_km", "mae_km", "rmse_km", "median_abs_error_km", "p90_abs_error_km", "within_1km_fraction", "within_2km_fraction", "within_3km_fraction", "pearson_corr", "spearman_corr", "low_mid_high_class_agreement"):
        result[f"A_{key}"] = a.get(key, math.nan)
        result[f"B_{key}"] = b.get(key, math.nan)
        result[f"B_minus_A_{key}"] = float(b.get(key, math.nan)) - float(a.get(key, math.nan))
    for prefix, metrics in (("A", a), ("B", b)):
        for key in ("sum_error_km", "sum_abs_error_km", "sum_squared_error_km2"):
            result[f"{prefix}_{key}"] = metrics[key]
    result.update({
        "A_better_fraction_by_abs_error": float(np.mean(ae < be)),
        "B_better_fraction_by_abs_error": float(np.mean(be < ae)),
        "both_bad_fraction_abs_error_gt3km": float(np.mean((ae > 3) & (be > 3))),
        "source_cth_disagreement_mean_abs_km": float(np.mean(np.abs(av - bv))),
    })
    return result


def correlation(left: np.ndarray, right: np.ndarray, rank: bool) -> float:
    if left.size < 3 or np.std(left) == 0 or np.std(right) == 0:
        return math.nan
    if rank:
        left = rank_values(left)
        right = rank_values(right)
    return float(np.corrcoef(left, right)[0, 1])


def rank_values(values: np.ndarray) -> np.ndarray:
    return rankdata(values, method="average").astype(np.float64)


def height_class(values: np.ndarray) -> np.ndarray:
    out = np.full(values.shape, -1, dtype=np.int8)
    out[(values >= 0) & (values < 3)] = 0
    out[(values >= 3) & (values < 7)] = 1
    out[values >= 7] = 2
    return out


def find_prefusion_layer(profile_root: Path, source_key: str, product: str | None, variable: str) -> Path:
    inventory_path = profile_root / "reprojected_grid" / "reprojected_variable_inventory.csv"
    rows = read_csv(inventory_path)
    matches = [row for row in rows if row.get("satellite") == source_key and row.get("variable") == variable and row.get("status") == "OK" and (product is None or row.get("product") == product)]
    paths = [Path(row["output_file"]) for row in matches if Path(row["output_file"]).exists()]
    if len(paths) != 1:
        raise RuntimeError(f"expected one prefusion layer for {source_key}/{product}/{variable}, found {len(paths)}")
    return paths[0]


def prefusion_samples(context: dict[str, Any], variable: str, policy_name: str | None = None) -> dict[str, dict[str, tuple[np.ndarray, np.ndarray]]]:
    epic = context["epic"]
    result: dict[str, dict[str, tuple[np.ndarray, np.ndarray]]] = {}
    for source, spec in PRIMARY_PREFUSION_SPECS.items():
        root = context["profiles"][spec["profile"]]
        product = spec["mask_product"] if variable == "cloud_mask" else spec["cth_product"]
        path = find_prefusion_layer(root, source, product, variable)
        data, valid, _ = load_npz(path)
        grid = context["grid"]
        if variable == "cloud_mask":
            if policy_name is None:
                raise ValueError("policy_name is required for cloud mask")
            result[source] = aggregate_mask_samples(data, valid, policy_name, epic["lat"], epic["lon"], grid)
        else:
            result[source] = aggregate_height_samples(data.astype(np.float32), valid, epic["lat"], epic["lon"], grid)
    return result


def sample_fused_profile(context: dict[str, Any], profile: str, variable: str, policy_name: str | None = None) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    root = context["profiles"][profile]
    data, valid, _ = load_npz(root / "fused_best_source" / f"fused_{variable}.npz")
    if variable == "cloud_mask":
        if policy_name is None:
            raise ValueError("policy_name is required for cloud mask")
        return aggregate_mask_samples(data, valid, policy_name, context["epic"]["lat"], context["epic"]["lon"], context["grid"])
    return aggregate_height_samples(data.astype(np.float32), valid, context["epic"]["lat"], context["epic"]["lon"], context["grid"])


def sample_fused_aux(context: dict[str, Any], profile: str, variable: str) -> tuple[np.ndarray, np.ndarray]:
    root = context["profiles"][profile]
    data, valid, _ = load_npz(root / "fused_best_source" / f"{variable}.npz")
    return sample_nearest(data, valid, context["epic"]["lat"], context["epic"]["lon"], context["grid"], fill=0.0)


def source_stability_samples(context: dict[str, Any], profile: str, variable: str) -> dict[str, np.ndarray]:
    root = context["profiles"][profile]
    data, valid, _ = load_npz(root / "fused_best_source" / f"source_map_{variable}.npz")
    source = np.zeros(data.shape, dtype=np.int16)
    usable = valid & np.isfinite(data)
    source[usable] = np.rint(data[usable]).astype(np.int16)
    result: dict[str, np.ndarray] = {"nearest": sample_nearest(usable.astype(np.float32), np.ones(usable.shape, dtype=bool), context["epic"]["lat"], context["epic"]["lon"], context["grid"], fill=0.0)[0] > 0.5}
    for window in (3, 5, 7):
        min_source = minimum_filter(source, size=window, mode="constant", cval=0)
        max_source = maximum_filter(source, size=window, mode="constant", cval=0)
        all_valid = minimum_filter(usable.astype(np.uint8), size=window, mode="constant", cval=0) == 1
        stable = all_valid & (min_source == max_source) & (min_source > 0)
        sampled, _ = sample_nearest(stable.astype(np.float32), np.ones(stable.shape, dtype=bool), context["epic"]["lat"], context["epic"]["lon"], context["grid"], fill=0.0)
        result[f"box_{window}x{window}"] = sampled > 0.5
    return result


def selected_geo_vza(context: dict[str, Any], profile: str, source_values: np.ndarray, source_valid: np.ndarray) -> np.ndarray:
    out = np.full(source_values.shape, np.nan, dtype=np.float32)
    root = context["profiles"][profile]
    source_ids = np.zeros(source_values.shape, dtype=np.int16)
    finite_source = source_valid & np.isfinite(source_values)
    source_ids[finite_source] = np.rint(source_values[finite_source]).astype(np.int16)
    for source_id, source_key in SOURCE_ID_TO_KEY.items():
        domain = finite_source & (source_ids == source_id)
        if not np.any(domain):
            continue
        try:
            spec = PRIMARY_PREFUSION_SPECS.get(source_key)
            product = spec["mask_product"] if spec else None
            path = find_prefusion_layer(root, source_key, product, "sensor_zenith_angle")
            data, valid, _ = load_npz(path)
            sampled, sampled_valid = sample_nearest(data, valid, context["epic"]["lat"], context["epic"]["lon"], context["grid"])
            use = domain & sampled_valid
            out[use] = sampled[use]
        except RuntimeError:
            continue
    return out


def build_context(matrix_path: Path) -> dict[str, Any]:
    context = matrix_context(matrix_path)
    context["grid"] = load_grid(context["profiles"]["operational_baseline"])
    candidate_grid = load_grid(context["profiles"]["claas3_candidate"])
    if candidate_grid != context["grid"]:
        raise RuntimeError("profile target-grid definitions differ")
    context["epic"] = read_epic(context["epic_path"])
    context["morphology"] = epic_morphology(context["epic"]["cloud_mask"])
    context["base_strata"] = base_strata(context["epic"], context["morphology"])
    return context


def common_metadata(context: dict[str, Any], **extra: Any) -> dict[str, Any]:
    payload = {
        "run_id": context["run_id"],
        "time_tag": context["time_tag"],
        "epic_time": context["epic_time"],
        "geo_time": context["geo_time"],
        "time_delta_min": context["time_delta_min"],
        "primary_time_domain": bool(context["time_delta_min"] <= 10.0),
    }
    payload.update(extra)
    return payload


def block_bootstrap(values: np.ndarray, seed: int, draws: int = 10000) -> tuple[float, float]:
    finite = values[np.isfinite(values)]
    if finite.size < 2:
        return math.nan, math.nan
    rng = np.random.default_rng(seed)
    means = np.mean(finite[rng.integers(0, finite.size, size=(draws, finite.size))], axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
