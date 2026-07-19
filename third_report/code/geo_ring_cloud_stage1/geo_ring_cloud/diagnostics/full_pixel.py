"""Reusable full-pixel EPIC/GEO sampling and classification primitives."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import netCDF4
import numpy as np

from .epic_pair import POLICIES as PAIR_POLICIES


PROJECT_ID = "geo_ring_cloud"
COMPONENT_ROLE = "diagnostics_library"

SOURCES = [
    "FY4B",
    "GOES-16",
    "GOES-18",
    "Himawari-9",
    "Meteosat-0deg",
    "Meteosat-IODC",
]
SOURCE_PRODUCTS = {
    "FY4B": "CLM",
    "GOES-16": "ACMF",
    "GOES-18": "ACMF",
    "Himawari-9": "CMSK",
    "Meteosat-0deg": "CLM",
    "Meteosat-IODC": "CLM",
}
SOURCE_ID = {
    1: "GOES-16",
    2: "GOES-18",
    3: "FY4B",
    4: "Himawari-9",
    5: "Meteosat-0deg",
    6: "Meteosat-IODC",
}
SOURCE_PAIR_LIST = [
    ("FY4B", "Himawari-9"),
    ("FY4B", "Meteosat-IODC"),
    ("Himawari-9", "Meteosat-IODC"),
    ("Meteosat-0deg", "Meteosat-IODC"),
    ("Meteosat-0deg", "GOES-16"),
    ("GOES-16", "GOES-18"),
]
POLICIES = {
    name: {
        "epic": dict(spec["epic"]),
        "geo": dict(spec["geo"]),
        "positive": int(spec["cloud_class"]),
    }
    for name, spec in PAIR_POLICIES.items()
}

__all__ = [
    "PROJECT_ID",
    "COMPONENT_ROLE",
    "SOURCES",
    "SOURCE_PRODUCTS",
    "SOURCE_ID",
    "SOURCE_PAIR_LIST",
    "POLICIES",
    "load_npz",
    "load_grid",
    "row_col",
    "sample_grid",
    "read_epic",
    "apply_policy",
    "source_to_standard",
    "binary_metrics",
    "classify_array",
    "find_prefusion",
    "sample_context",
    "make_boundary",
]


def load_npz(path: Path) -> dict[str, np.ndarray]:
    with np.load(path, allow_pickle=True) as payload:
        return {
            key: np.asarray(payload[key])
            for key in payload.files
            if not key.endswith("json")
        }


def load_grid(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "reprojected_grid" / "target_grid_definition.json"
    return json.loads(path.read_text(encoding="utf-8"))


def row_col(
    lat: np.ndarray,
    lon: np.ndarray,
    grid: dict[str, Any],
    shape: tuple[int, int],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    resolution = float(grid["resolution_degree"])
    if "lat_centers_first_last" in grid:
        lat0 = float(grid["lat_centers_first_last"][0])
        lon0 = float(grid["lon_centers_first_last"][0])
    else:
        lat0 = float(grid["lat_min"]) + resolution / 2.0
        lon0 = float(grid["lon_min"]) + resolution / 2.0
    lon_norm = ((lon.astype(np.float32) + 180.0) % 360.0) - 180.0
    row = np.rint((lat.astype(np.float32) - lat0) / resolution).astype(np.int64)
    col = np.rint((lon_norm - lon0) / resolution).astype(np.int64)
    ok = (
        np.isfinite(lat)
        & np.isfinite(lon_norm)
        & (row >= 0)
        & (row < shape[0])
        & (col >= 0)
        & (col < shape[1])
    )
    return row, col, ok


def sample_grid(
    data: np.ndarray,
    valid: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    grid: dict[str, Any],
    fill: float = np.nan,
) -> tuple[np.ndarray, np.ndarray]:
    row, col, ok = row_col(lat, lon, grid, data.shape)
    out = np.full(lat.shape, fill, dtype=np.float32)
    out_valid = np.zeros(lat.shape, dtype=bool)
    out[ok] = data[row[ok], col[ok]].astype(np.float32)
    out_valid[ok] = valid[row[ok], col[ok]].astype(bool)
    out[~out_valid] = fill
    return out, out_valid


def _find_nc_variable(ds: netCDF4.Dataset, options: tuple[str, ...]) -> str | None:
    for name in options:
        group, _, variable = name.rpartition("/")
        owner = ds[group] if group else ds
        if variable in owner.variables:
            return name
    return None


def read_epic(path: Path) -> dict[str, np.ndarray]:
    with netCDF4.Dataset(path) as ds:
        lat_name = _find_nc_variable(
            ds,
            ("geolocation_data/latitude", "geolocation_data/Latitude"),
        )
        lon_name = _find_nc_variable(
            ds,
            ("geolocation_data/longitude", "geolocation_data/Longitude"),
        )
        mask_name = _find_nc_variable(
            ds,
            ("geophysical_data/Cloud_Mask", "geophysical_data/cloud_mask"),
        )
        if not lat_name or not lon_name or not mask_name:
            raise RuntimeError(f"missing EPIC lat/lon/cloud in {path}")

        result = {
            "lat": np.asarray(ds[lat_name][:], dtype=np.float32),
            "lon": np.asarray(ds[lon_name][:], dtype=np.float32),
            "cloud_mask": np.asarray(ds[mask_name][:], dtype=np.float32),
        }
        vza_name = _find_nc_variable(
            ds,
            (
                "geolocation_data/sensor_zenith",
                "geolocation_data/SensorZenith",
                "geolocation_data/ViewAngle",
            ),
        )
        sza_name = _find_nc_variable(
            ds,
            (
                "geolocation_data/solar_zenith",
                "geolocation_data/SolarZenith",
                "geolocation_data/SunAngle",
            ),
        )
        result["epic_vza"] = (
            np.asarray(ds[vza_name][:], dtype=np.float32)
            if vza_name
            else np.full(result["lat"].shape, np.nan, dtype=np.float32)
        )
        result["sza"] = (
            np.asarray(ds[sza_name][:], dtype=np.float32)
            if sza_name
            else np.full(result["lat"].shape, np.nan, dtype=np.float32)
        )
        return result


def apply_policy(
    values: np.ndarray,
    mapping: dict[int, int],
) -> tuple[np.ndarray, np.ndarray]:
    out = np.full(values.shape, -1, dtype=np.int16)
    valid = np.zeros(values.shape, dtype=bool)
    for raw, mapped in mapping.items():
        hit = values == raw
        out[hit] = mapped
        valid |= hit
    return out, valid


def source_to_standard(source: str, raw: np.ndarray) -> np.ndarray:
    out = np.full(raw.shape, -9999, dtype=np.int16)
    if source == "FY4B":
        mapping = {0: 3, 1: 2, 2: 1, 3: 0}
    elif source.startswith("GOES"):
        mapping = {0: 0, 1: 3}
    elif source.startswith("Himawari"):
        mapping = {0: 0, 1: 1, 2: 2, 3: 3}
    elif source.startswith("Meteosat"):
        mapping = {0: 0, 1: 0, 2: 3}
    else:
        mapping = {}
    for raw_code, standard_code in mapping.items():
        out[raw == raw_code] = standard_code
    return out


def binary_metrics(
    epic: np.ndarray,
    geo: np.ndarray,
    valid: np.ndarray,
    positive: int,
) -> dict[str, Any]:
    count = int(np.count_nonzero(valid))
    if count == 0:
        return {"n_valid": 0, "agreement": math.nan}
    epic_valid = epic[valid]
    geo_valid = geo[valid]
    epic_positive = epic_valid == positive
    geo_positive = geo_valid == positive
    tp = int(np.count_nonzero(epic_positive & geo_positive))
    tn = int(np.count_nonzero((~epic_positive) & (~geo_positive)))
    fp = int(np.count_nonzero((~epic_positive) & geo_positive))
    fn = int(np.count_nonzero(epic_positive & (~geo_positive)))
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    specificity = tn / max(tn + fp, 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-12)
    return {
        "n_valid": count,
        "agreement": float(np.mean(epic_valid == geo_valid)),
        "precision_cloud": precision,
        "recall_cloud": recall,
        "f1_cloud": f1,
        "iou_cloud": tp / max(tp + fp + fn, 1),
        "balanced_accuracy": (recall + specificity) / 2.0,
        "cloud_fraction_epic": float(np.mean(epic_positive)),
        "cloud_fraction_source": float(np.mean(geo_positive)),
        "cloud_fraction_georing": float(np.mean(geo_positive)),
        "cloud_fraction_bias": float(np.mean(geo_positive) - np.mean(epic_positive)),
        "TP": tp,
        "TN": tn,
        "FP": fp,
        "FN": fn,
    }


def classify_array(
    values: np.ndarray,
    bins: list[tuple[str, float, float]],
) -> np.ndarray:
    out = np.full(values.shape, "missing", dtype=object)
    for label, lower, upper in bins:
        out[(values >= lower) & (values < upper)] = label
    return out


def find_prefusion(run_dir: Path, source: str, tag: str) -> Path | None:
    root = run_dir / "reprojected_grid" / source
    product = SOURCE_PRODUCTS[source]
    hits = list(root.glob(f"{source}_{product}_cloud_mask_grid_{tag}.npz"))
    if not hits:
        hits = list(root.glob(f"*cloud_mask*{tag}.npz"))
    return hits[0] if hits else None


def sample_context(row: dict[str, Any]) -> dict[str, Any]:
    tag = row["sample_id"]
    run_dir = Path(row["stage_run_dir"])
    epic = read_epic(Path(row["epic_file"]))
    grid = load_grid(run_dir)
    fused = load_npz(run_dir / "fused_best_source" / "fused_cloud_mask.npz")
    fused_data = np.asarray(fused["data"])
    fused_valid = np.asarray(
        fused.get("valid_mask", np.isfinite(fused_data))
    ).astype(bool)
    fused_on_epic, fused_on_valid = sample_grid(
        fused_data,
        fused_valid,
        epic["lat"],
        epic["lon"],
        grid,
    )

    source_on_epic = np.full(epic["lat"].shape, np.nan, dtype=np.float32)
    count_on_epic = np.full(epic["lat"].shape, np.nan, dtype=np.float32)
    source_map_path = run_dir / "fused_best_source" / "source_map_cloud_mask.npz"
    count_path = run_dir / "fused_best_source" / "valid_count_map_cloud_mask.npz"
    if source_map_path.exists():
        source_map = load_npz(source_map_path)
        source_on_epic, _ = sample_grid(
            source_map["data"],
            np.asarray(
                source_map.get(
                    "valid_mask",
                    np.isfinite(source_map["data"]),
                )
            ).astype(bool),
            epic["lat"],
            epic["lon"],
            grid,
        )
    if count_path.exists():
        valid_count = load_npz(count_path)
        count_on_epic, _ = sample_grid(
            valid_count["data"],
            np.asarray(
                valid_count.get(
                    "valid_mask",
                    np.isfinite(valid_count["data"]),
                )
            ).astype(bool),
            epic["lat"],
            epic["lon"],
            grid,
        )
    return {
        "tag": tag,
        "run_dir": run_dir,
        "epic": epic,
        "grid": grid,
        "fused_data": fused_data,
        "fused_valid": fused_valid,
        "fused_on_epic": fused_on_epic,
        "fused_on_valid": fused_on_valid,
        "selected_source": source_on_epic,
        "valid_count": count_on_epic,
    }


def make_boundary(
    fused_standard: np.ndarray,
    fused_valid: np.ndarray,
    policy_name: str,
) -> tuple[np.ndarray, np.ndarray]:
    policy = POLICIES[policy_name]
    classes, policy_valid = apply_policy(fused_standard, policy["geo"])
    cloud = classes == policy["positive"]
    valid = fused_valid & policy_valid
    padded_cloud = np.pad(cloud.astype(np.int8), 1, mode="edge")
    padded_valid = np.pad(valid.astype(np.int8), 1, mode="constant")
    count = np.zeros(cloud.shape, dtype=np.int16)
    valid_count = np.zeros(cloud.shape, dtype=np.int16)
    for delta_y in range(3):
        for delta_x in range(3):
            count += padded_cloud[
                delta_y : delta_y + cloud.shape[0],
                delta_x : delta_x + cloud.shape[1],
            ]
            valid_count += padded_valid[
                delta_y : delta_y + cloud.shape[0],
                delta_x : delta_x + cloud.shape[1],
            ]
    boundary = valid & (count > 0) & (count < valid_count)
    fraction = np.where(
        valid_count > 0,
        count / np.maximum(valid_count, 1),
        np.nan,
    )
    return fraction.astype(np.float32), boundary
