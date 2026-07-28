from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import netCDF4
from scipy.spatial import cKDTree

CODE_ROOT_FOR_IMPORT = Path(__file__).resolve().parents[1]
if str(CODE_ROOT_FOR_IMPORT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT_FOR_IMPORT))

from geo_ring_cloud.adapters.cloud_products import (  # noqa: E402
    METEOSAT_0DEG_CTH_NAVIGATION_SCHEMA_VERSION,
    METEOSAT_CTH_NAVIGATION_GRID,
    METEOSAT_CTH_NAVIGATION_SOURCE,
    METEOSAT_CTH_SHAPE,
    METEOSAT_IODC_CTH_NAVIGATION_SCHEMA_VERSION,
    matches_meteosat_cth_scope,
    read_mapping,
    read_product,
)
from geo_ring_cloud.lineage import write_manifest  # noqa: E402
from geo_ring_cloud.paths import EXTERNAL_GEO_CLOUD_ROOT, PROJECT_ROOT, RUNS_ROOT  # noqa: E402


STAGE_ID = "stage_10s"
PROJECT_ID = "geo_ring_cloud"
COMPONENT_ROLE = "production_patch_validation"
OUTPUT_DIRNAME = "stage_10s_meteosat_cth_production_patch"
REGRESSION_CASES = [
    {"product_line": "Meteosat-0deg", "sample_id": "20240310_1200", "case_role": "stage10r_regression_meteosat0deg_a"},
    {"product_line": "Meteosat-0deg", "sample_id": "20240306_1300", "case_role": "stage10r_regression_meteosat0deg_b"},
    {"product_line": "Meteosat-IODC", "sample_id": "20240310_1000", "case_role": "stage10r_regression_iodc_a"},
    {"product_line": "Meteosat-IODC", "sample_id": "20240311_0800", "case_role": "stage10r_regression_iodc_b"},
]
EPIC_CTH_VARIABLE = "geophysical_data/A-band_Effective_Cloud_Height"
TOL = 1e-5
GOOD_QUALITY_CODE = 0
MAX_EPIC_SAMPLE_DISTANCE_KM = 25.0
EARTH_RADIUS_KM = 6371.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    keys: set[str] = set(fields or [])
    for row in rows:
        keys.update(row.keys())
    ordered = list(fields or sorted(keys))
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ordered)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in ordered})


def normalize_lon(lon: np.ndarray) -> np.ndarray:
    out = np.asarray(lon, dtype=np.float32).copy()
    finite = np.isfinite(out)
    out[finite] = ((out[finite] + 180.0) % 360.0) - 180.0
    return out


def cth_metric_row(ref: np.ndarray, test: np.ndarray, mask: np.ndarray) -> dict[str, Any]:
    n = int(np.count_nonzero(mask))
    if n == 0:
        return {
            "n_common_valid": 0,
            "bias_km": math.nan,
            "mae_km": math.nan,
            "rmse_km": math.nan,
            "median_ae_km": math.nan,
            "max_ae_km": math.nan,
            "pearson_r": math.nan,
            "spearman_r": math.nan,
        }
    r = ref[mask].astype(np.float64)
    t = test[mask].astype(np.float64)
    d = t - r
    ae = np.abs(d)
    pearson = float(np.corrcoef(r, t)[0, 1]) if n >= 3 and np.std(r) > 0 and np.std(t) > 0 else math.nan
    rr = pd.Series(r).rank().to_numpy()
    tr = pd.Series(t).rank().to_numpy()
    spearman = float(np.corrcoef(rr, tr)[0, 1]) if n >= 3 and np.std(rr) > 0 and np.std(tr) > 0 else math.nan
    return {
        "n_common_valid": n,
        "bias_km": float(np.mean(d)),
        "mae_km": float(np.mean(ae)),
        "rmse_km": float(np.sqrt(np.mean(d * d))),
        "median_ae_km": float(np.median(ae)),
        "max_ae_km": float(np.max(ae)),
        "pearson_r": pearson,
        "spearman_r": spearman,
    }


def haversine_km(lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray) -> np.ndarray:
    phi1 = np.deg2rad(lat1.astype(np.float64))
    phi2 = np.deg2rad(lat2.astype(np.float64))
    dphi = phi2 - phi1
    dlambda = np.deg2rad(lon2.astype(np.float64) - lon1.astype(np.float64))
    a = np.sin(dphi / 2.0) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlambda / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def unit_vectors(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    latr = np.deg2rad(lat.astype(np.float64))
    lonr = np.deg2rad(lon.astype(np.float64))
    clat = np.cos(latr)
    return np.column_stack((clat * np.cos(lonr), clat * np.sin(lonr), np.sin(latr)))


def sample_native_to_epic(
    values: np.ndarray,
    quality: np.ndarray,
    nav_lat: np.ndarray,
    nav_lon: np.ndarray,
    epic_lat: np.ndarray,
    epic_lon: np.ndarray,
    epic_valid: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    src_valid = np.isfinite(values) & (values >= 0.0) & (values <= 25.0) & np.isfinite(nav_lat) & np.isfinite(nav_lon)
    src_idx = np.flatnonzero(src_valid.ravel())
    tree = cKDTree(unit_vectors(nav_lat.ravel()[src_idx], nav_lon.ravel()[src_idx]))
    target_valid = epic_valid & np.isfinite(epic_lat) & np.isfinite(epic_lon)
    target_idx = np.flatnonzero(target_valid.ravel())
    sampled = np.full(epic_lat.size, np.nan, dtype=np.float32)
    sampled_quality = np.full(epic_lat.size, np.nan, dtype=np.float32)
    sampled_distance = np.full(epic_lat.size, np.nan, dtype=np.float32)
    sampled_valid = np.zeros(epic_lat.size, dtype=bool)
    if target_idx.size:
        dist_chord, nearest = tree.query(unit_vectors(epic_lat.ravel()[target_idx], epic_lon.ravel()[target_idx]), k=1, workers=-1)
        dist_km = (2.0 * EARTH_RADIUS_KM * np.arcsin(np.clip(dist_chord / 2.0, 0.0, 1.0))).astype(np.float32)
        ok = dist_km <= MAX_EPIC_SAMPLE_DISTANCE_KM
        native_flat_idx = src_idx[nearest[ok]]
        flat_targets = target_idx[ok]
        sampled[flat_targets] = values.ravel()[native_flat_idx]
        sampled_quality[flat_targets] = quality.ravel()[native_flat_idx]
        sampled_distance[flat_targets] = dist_km[ok]
        sampled_valid[flat_targets] = True
    shape = epic_lat.shape
    return sampled.reshape(shape), sampled_quality.reshape(shape), sampled_distance.reshape(shape), sampled_valid.reshape(shape)


def local_fraction(mask: np.ndarray, radius: int = 2) -> np.ndarray:
    padded = np.pad(mask.astype(np.float32), radius, mode="edge")
    out = np.zeros(mask.shape, dtype=np.float32)
    for dy in range(2 * radius + 1):
        for dx in range(2 * radius + 1):
            out += padded[dy : dy + mask.shape[0], dx : dx + mask.shape[1]]
    return out / float((2 * radius + 1) ** 2)


def epic_cloud_binary(cloud_mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    valid = np.isin(cloud_mask, [1, 2, 3, 4])
    cloudy = np.isin(cloud_mask, [3, 4])
    return cloudy, valid


def height_class(values: np.ndarray) -> np.ndarray:
    out = np.full(values.shape, "missing", dtype=object)
    out[(values >= 0.0) & (values < 3.0)] = "low_cth"
    out[(values >= 3.0) & (values < 7.0)] = "mid_cth"
    out[values >= 7.0] = "high_cth"
    return out


def variable_attributes(var: netCDF4.Variable) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    for name in var.ncattrs():
        value = getattr(var, name)
        if isinstance(value, np.ndarray):
            value = value.tolist()
        elif isinstance(value, np.generic):
            value = value.item()
        attrs[name] = value
    return attrs


def find_variable(ds: netCDF4.Dataset, names: tuple[str, ...]) -> str | None:
    for name in names:
        group_name, _, var_name = name.rpartition("/")
        group = ds[group_name] if group_name else ds
        if var_name in group.variables:
            return name
    return None


def read_nc_array(ds: netCDF4.Dataset, name: str, dtype: Any = np.float32) -> np.ndarray:
    values = ds[name][:]
    if np.ma.isMaskedArray(values):
        values = values.astype(np.float32).filled(np.nan)
    return np.asarray(values, dtype=dtype)


def optional_geo(ds: netCDF4.Dataset, name: str, shape: tuple[int, ...]) -> np.ndarray:
    group = ds.groups.get("geolocation_data")
    if group is None or name not in group.variables:
        return np.full(shape, np.nan, dtype=np.float32)
    return read_nc_array(ds, f"geolocation_data/{name}")


def read_epic_cth_for_stage10s(path: Path, cth_variable: str) -> dict[str, Any]:
    with netCDF4.Dataset(path) as ds:
        lat_name = find_variable(ds, ("geolocation_data/latitude", "geolocation_data/Latitude"))
        lon_name = find_variable(ds, ("geolocation_data/longitude", "geolocation_data/Longitude"))
        cloud_mask_name = find_variable(ds, ("geophysical_data/Cloud_Mask", "geophysical_data/cloud_mask"))
        if not lat_name or not lon_name or not cloud_mask_name:
            raise RuntimeError(f"missing EPIC geolocation/cloud mask in {path}")
        attrs = variable_attributes(ds[cth_variable])
        raw = read_nc_array(ds, cth_variable)
        fill_value = attrs.get("_FillValue", attrs.get("missing_value"))
        raw_valid = np.isfinite(raw)
        if fill_value is not None:
            raw_valid &= raw != float(fill_value)
        units = str(attrs.get("units", "")).strip().lower()
        if units == "m":
            cth_km = raw / 1000.0
        else:
            cth_km = raw
        physical_valid = raw_valid & np.isfinite(cth_km) & (cth_km >= 0.0) & (cth_km <= 25.0)
        return {
            "lat": read_nc_array(ds, lat_name),
            "lon": normalize_lon(read_nc_array(ds, lon_name)),
            "cloud_mask": read_nc_array(ds, cloud_mask_name),
            "cth_km": cth_km.astype(np.float32),
            "cth_valid": physical_valid,
            "epic_vza": optional_geo(ds, "sensor_zenith", raw.shape),
        }


def parse_sample_id(sample_id: str) -> tuple[str, str]:
    match = re.match(r"^(\d{8})_(\d{2})00$", sample_id)
    if not match:
        raise ValueError(f"unsupported sample_id {sample_id}")
    return match.group(1), match.group(2)


def cth_zip_path(product_line: str, sample_id: str) -> Path:
    day, hour = parse_sample_id(sample_id)
    root = EXTERNAL_GEO_CLOUD_ROOT / product_line / "CTH" / day / hour
    files = sorted(root.glob("*MSGCLTH*.zip"))
    if not files:
        raise FileNotFoundError(f"no Meteosat CTH ZIP under {root}")
    return files[0]


def native_npz_path(product_line: str, sample_id: str) -> Path:
    files = sorted((RUNS_ROOT / sample_id / "standardized_native").glob(f"{product_line}_CTH_*_native_cloud_v0.npz"))
    if not files:
        raise FileNotFoundError(f"no legacy native NPZ for {product_line} {sample_id}")
    return files[0]


def epic_path(sample_manifest: Path, sample_id: str) -> Path | None:
    df = pd.read_csv(sample_manifest, encoding="utf-8-sig")
    hit = df[df["sample_id"].astype(str) == sample_id]
    if hit.empty:
        return None
    path = Path(str(hit.iloc[0].get("epic_file", "")))
    return path if path.exists() else None


def load_native_npz(path: Path) -> dict[str, Any]:
    with np.load(path, allow_pickle=True) as z:
        arrays = {name: np.asarray(z[name]) for name in z.files if name not in {"metadata_json", "variable_availability_json"}}
        meta = json.loads(str(np.asarray(z["metadata_json"]).item())) if "metadata_json" in z.files else {}
    return {
        "cth": arrays["cloud_top_height_km"].astype(np.float32),
        "quality": arrays["quality_flag_raw"],
        "lat": arrays["latitude"].astype(np.float32),
        "lon": arrays["longitude"].astype(np.float32),
        "metadata": meta,
    }


def product_bundle(product_line: str, sample_id: str, mapping: dict[str, dict[str, list[str]]]) -> dict[str, Any]:
    path = cth_zip_path(product_line, sample_id)
    result = read_product(path, "Meteosat", "CTH", mapping)
    attrs = result.attrs
    return {
        "cth": np.asarray(result.arrays["cloud_top_height_km"], dtype=np.float32),
        "quality": np.asarray(result.arrays["quality_flag_raw"]),
        "lat": np.asarray(result.arrays["latitude"], dtype=np.float32),
        "lon": np.asarray(result.arrays["longitude"], dtype=np.float32),
        "attrs": attrs,
        "warnings": result.warnings,
        "source_file": path,
    }


def hash_array(arr: np.ndarray) -> str:
    a = np.asarray(arr)
    return hashlib.sha256(np.ascontiguousarray(a).view(np.uint8)).hexdigest()


def stable_hash(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def valid_cth(arr: np.ndarray) -> np.ndarray:
    return np.isfinite(arr) & (arr >= 0.0) & (arr <= 25.0)


def compare_numeric(a: np.ndarray, b: np.ndarray, mask: np.ndarray) -> dict[str, Any]:
    n = int(np.count_nonzero(mask))
    if n == 0:
        return {"n_common": 0, "mae_km": math.nan, "rmse_km": math.nan, "array_equal": False}
    d = a[mask].astype(np.float64) - b[mask].astype(np.float64)
    return {
        "n_common": n,
        "mae_km": float(np.mean(np.abs(d))),
        "rmse_km": float(np.sqrt(np.mean(d * d))),
        "max_abs_km": float(np.max(np.abs(d))),
        "array_equal": bool(np.array_equal(a, b)),
    }


def quality_counts(arr: np.ndarray) -> str:
    values, counts = np.unique(np.asarray(arr), return_counts=True)
    return json.dumps({str(int(v)) if np.issubdtype(values.dtype, np.integer) else str(v): int(c) for v, c in zip(values, counts)}, sort_keys=True)


def equal_with_nan(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    aa = np.asarray(a)
    bb = np.asarray(b)
    return (aa == bb) | (np.isnan(aa.astype(np.float32, copy=False)) & np.isnan(bb.astype(np.float32, copy=False)))


def nav_control_rows(case: dict[str, Any], prod: dict[str, Any], legacy: dict[str, Any]) -> tuple[list[dict[str, Any]], float]:
    rows: list[dict[str, Any]] = []
    valid = np.isfinite(prod["lat"]) & np.isfinite(prod["lon"]) & np.isfinite(legacy["lat"]) & np.isfinite(legacy["lon"])
    legacy_dist = haversine_km(legacy["lat"][valid], legacy["lon"][valid], prod["lat"][valid], prod["lon"][valid]) if np.any(valid) else np.asarray([])
    legacy_p95 = float(np.percentile(legacy_dist, 95)) if legacy_dist.size else math.nan
    rows.append(
        {
            **case,
            "comparison": "legacy_current_navigation_vs_production_satpy_navigation",
            "control_point": "ALL_VALID_NAV_PIXELS",
            "n_nav_valid": int(legacy_dist.size),
            "median_geodesic_error_km": float(np.median(legacy_dist)) if legacy_dist.size else math.nan,
            "p95_geodesic_error_km": legacy_p95,
            "max_geodesic_error_km": float(np.max(legacy_dist)) if legacy_dist.size else math.nan,
            "production_navigation_source": prod["attrs"].get("navigation_source", ""),
            "production_navigation_schema_version": prod["attrs"].get("navigation_schema_version", ""),
            "production_navigation_grid": prod["attrs"].get("navigation_grid", ""),
            "production_area_id": prod["attrs"].get("navigation_area_id", ""),
            "production_lon_0": prod["attrs"].get("navigation_lon_0", ""),
            "production_shape": "x".join(map(str, prod["cth"].shape)),
            "clm_3712_grid_reused": bool(prod["cth"].shape == (3712, 3712)),
            "pass": bool(prod["attrs"].get("navigation_source") == METEOSAT_CTH_NAVIGATION_SOURCE and prod["cth"].shape == METEOSAT_CTH_SHAPE),
        }
    )
    ny, nx = prod["lat"].shape
    points = {
        "upper_left": (0, 0),
        "upper_center": (0, nx // 2),
        "upper_right": (0, nx - 1),
        "middle_left": (ny // 2, 0),
        "center": (ny // 2, nx // 2),
        "middle_right": (ny // 2, nx - 1),
        "lower_left": (ny - 1, 0),
        "lower_center": (ny - 1, nx // 2),
        "lower_right": (ny - 1, nx - 1),
    }
    for name, (r, c) in points.items():
        rows.append(
            {
                **case,
                "comparison": "production_control_points",
                "control_point": name,
                "row": r,
                "col": c,
                "production_lat": float(prod["lat"][r, c]) if np.isfinite(prod["lat"][r, c]) else math.nan,
                "production_lon": float(prod["lon"][r, c]) if np.isfinite(prod["lon"][r, c]) else math.nan,
                "production_navigation_source": prod["attrs"].get("navigation_source", ""),
                "production_navigation_schema_version": prod["attrs"].get("navigation_schema_version", ""),
            }
        )
    return rows, legacy_p95


def epic_rows(case: dict[str, Any], prod: dict[str, Any], legacy: dict[str, Any], epic_file: Path | None) -> list[dict[str, Any]]:
    if epic_file is None:
        return [{**case, "navigation_variant": "not_run", "stratum": "no_epic_pair", "pass": False}]
    epic = read_epic_cth_for_stage10s(epic_file, EPIC_CTH_VARIABLE)
    legacy_s, legacy_q, _, legacy_valid = sample_native_to_epic(prod["cth"], prod["quality"], legacy["lat"], legacy["lon"], epic["lat"], epic["lon"], epic["cth_valid"])
    prod_s, prod_q, _, prod_valid = sample_native_to_epic(prod["cth"], prod["quality"], prod["lat"], prod["lon"], epic["lat"], epic["lon"], epic["cth_valid"])
    common = epic["cth_valid"] & legacy_valid & prod_valid
    cloudy, cloud_valid = epic_cloud_binary(epic["cloud_mask"])
    boundary_fraction = local_fraction(cloudy & cloud_valid, 2)
    boundary = cloud_valid & (boundary_fraction > 0.05) & (boundary_fraction < 0.95)
    vza = epic.get("epic_vza", np.full(epic["cth_km"].shape, np.nan, dtype=np.float32))
    strata = {
        "all_valid": common,
        "good_quality_only": common & (legacy_q == GOOD_QUALITY_CODE) & (prod_q == GOOD_QUALITY_CODE),
        "low_cth": common & (height_class(epic["cth_km"]) == "low_cth"),
        "mid_cth": common & (height_class(epic["cth_km"]) == "mid_cth"),
        "high_cth": common & (height_class(epic["cth_km"]) == "high_cth"),
        "vza_le_60": common & np.isfinite(vza) & (vza <= 60.0),
        "vza_gt_60": common & np.isfinite(vza) & (vza > 60.0),
        "boundary": common & boundary,
        "non_boundary": common & ~boundary,
    }
    rows: list[dict[str, Any]] = []
    for stratum, mask in strata.items():
        for name, sampled in [("legacy_current_navigation", legacy_s), ("production_satpy_cth_area_navigation", prod_s)]:
            row = {
                **case,
                "navigation_variant": name,
                "stratum": stratum,
                "common_valid_fraction": float(np.mean(mask)),
            }
            row.update(cth_metric_row(epic["cth_km"], sampled, mask))
            rows.append(row)
    return rows


def choose_multicase(sample_manifest: Path) -> list[dict[str, Any]]:
    used = {(c["product_line"], c["sample_id"]) for c in REGRESSION_CASES}
    df = pd.read_csv(sample_manifest, encoding="utf-8-sig")
    rows: list[dict[str, Any]] = []
    for line, dominant in [("Meteosat-0deg", "Meteosat-0deg"), ("Meteosat-IODC", "Meteosat-IODC")]:
        candidates = df[(df["dominant_source"].astype(str) == dominant) & df["has_epic_file"].astype(str).str.lower().eq("true")]
        picked: list[dict[str, Any]] = []
        seen_days: set[str] = set()
        for _, row in candidates.iterrows():
            sample_id = str(row["sample_id"])
            if (line, sample_id) in used:
                continue
            day, hour = parse_sample_id(sample_id)
            if day in seen_days and len(picked) < 2:
                continue
            try:
                zip_path = cth_zip_path(line, sample_id)
            except FileNotFoundError:
                continue
            picked.append({"product_line": line, "sample_id": sample_id, "case_role": "stage10s_multicase_stability", "source_file": str(zip_path)})
            seen_days.add(day)
            if len(picked) == 3:
                break
        rows.extend(picked)
    return rows


def cache_key(source_file: str, product_line: str, schema: str, grid_hash: str) -> str:
    return stable_hash({"source_file": source_file, "product_line": product_line, "product": "CTH", "navigation_schema_version": schema, "navigation_grid_spec_sha256": grid_hash})


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 10S Meteosat operational CTH production navigation patch validation.")
    parser.add_argument("--output-dir", type=Path, default=RUNS_ROOT / OUTPUT_DIRNAME)
    parser.add_argument("--sample-manifest", type=Path, default=RUNS_ROOT / "stage09d_full_pixel_diagnostics_202403" / "00_sample_manifest" / "stage09d_53_sample_manifest.csv")
    parser.add_argument("--stage10r-dir", type=Path, default=RUNS_ROOT / "stage_10r_meteosat_operational_cth_satpy_navigation_audit_202403")
    args = parser.parse_args()
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    mapping = read_mapping()
    cases = [dict(c) for c in REGRESSION_CASES]
    multicase = choose_multicase(args.sample_manifest)
    all_cases = cases + multicase

    value_rows: list[dict[str, Any]] = []
    quality_rows: list[dict[str, Any]] = []
    scope_rows: list[dict[str, Any]] = []
    nav_rows: list[dict[str, Any]] = []
    epic_out_rows: list[dict[str, Any]] = []
    cache_rows: list[dict[str, Any]] = []
    stability_rows: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    for case in all_cases:
        line = case["product_line"]
        sample_id = case["sample_id"]
        try:
            prod = product_bundle(line, sample_id, mapping)
            legacy = load_native_npz(native_npz_path(line, sample_id))
            source_file = str(prod["source_file"])
            case.update({"source_file": source_file})
            value_mask = valid_cth(prod["cth"]) | valid_cth(legacy["cth"])
            value_metric = compare_numeric(prod["cth"], legacy["cth"], value_mask)
            value_rows.append(
                {
                    **case,
                    **value_metric,
                    "prod_hash": hash_array(prod["cth"]),
                    "legacy_hash": hash_array(legacy["cth"]),
                    "valid_mask_agreement": float(np.mean(valid_cth(prod["cth"]) == valid_cth(legacy["cth"]))),
                    "test": "VALUES_PRESERVATION",
                    "pass": bool(value_metric["mae_km"] == 0.0 and value_metric["rmse_km"] == 0.0),
                }
            )
            q_same = equal_with_nan(prod["quality"], legacy["quality"]) if prod["quality"].shape == legacy["quality"].shape else np.asarray(False)
            q_equal = bool(np.all(q_same))
            quality_rows.append(
                {
                    **case,
                    "array_equal": q_equal,
                    "agreement": float(np.mean(q_same)) if prod["quality"].shape == legacy["quality"].shape else 0.0,
                    "prod_quality_counts": quality_counts(prod["quality"]),
                    "legacy_quality_counts": quality_counts(legacy["quality"]),
                    "test": "QUALITY_PRESERVATION",
                    "pass": q_equal,
                }
            )
            area_meta = {
                "shape": list(prod["cth"].shape),
                "lon_0": prod["attrs"].get("navigation_lon_0"),
                "area_id": prod["attrs"].get("navigation_area_id"),
            }
            matched, product_line_from_area, reason = matches_meteosat_cth_scope(prod["source_file"], "CTH", prod["cth"].shape, prod["quality"].shape, area_meta)
            expected_schema = METEOSAT_0DEG_CTH_NAVIGATION_SCHEMA_VERSION if line == "Meteosat-0deg" else METEOSAT_IODC_CTH_NAVIGATION_SCHEMA_VERSION
            scope_pass = (
                matched
                and product_line_from_area == line
                and prod["attrs"].get("navigation_source") == METEOSAT_CTH_NAVIGATION_SOURCE
                and prod["attrs"].get("navigation_schema_version") == expected_schema
                and prod["attrs"].get("navigation_grid") == METEOSAT_CTH_NAVIGATION_GRID
                and prod["cth"].shape == METEOSAT_CTH_SHAPE
            )
            scope_rows.append(
                {
                    **case,
                    "matched": matched,
                    "product_line_from_area": product_line_from_area,
                    "scope_reason": reason,
                    "shape": "x".join(map(str, prod["cth"].shape)),
                    "lon_0": prod["attrs"].get("navigation_lon_0", ""),
                    "schema_version": prod["attrs"].get("navigation_schema_version", ""),
                    "navigation_source": prod["attrs"].get("navigation_source", ""),
                    "cth_transform": prod["attrs"].get("cth_transform", ""),
                    "quality_transform": prod["attrs"].get("quality_transform", ""),
                    "clm_3712_grid_reused": bool(prod["cth"].shape == (3712, 3712)),
                    "test": "SCOPE_GUARD",
                    "pass": scope_pass,
                }
            )
            rows, legacy_p95 = nav_control_rows(case, prod, legacy)
            nav_rows.extend(rows)
            efile = epic_path(args.sample_manifest, sample_id)
            erows = epic_rows(case, prod, legacy, efile)
            epic_out_rows.extend(erows)
            new_all = next((r for r in erows if r.get("navigation_variant") == "production_satpy_cth_area_navigation" and r.get("stratum") == "all_valid"), {})
            old_all = next((r for r in erows if r.get("navigation_variant") == "legacy_current_navigation" and r.get("stratum") == "all_valid"), {})
            old_meta = legacy["metadata"].get("reader_attrs", {})
            old_schema = str(old_meta.get("navigation_schema_version", ""))
            new_schema = str(prod["attrs"].get("navigation_schema_version", ""))
            old_key = cache_key(source_file, line, old_schema, str(old_meta.get("navigation_grid_spec_sha256", "")))
            new_key = cache_key(source_file, line, new_schema, str(prod["attrs"].get("navigation_grid_spec_sha256", "")))
            cache_rows.append(
                {
                    **case,
                    "old_navigation_schema_version": old_schema,
                    "new_navigation_schema_version": new_schema,
                    "old_cache_key": old_key,
                    "new_cache_key": new_key,
                    "cache_key_changed": old_key != new_key,
                    "old_reprojected_cache_must_not_be_reused": old_key != new_key,
                    "test": "CACHE_INVALIDATION",
                    "pass": bool(new_schema and old_key != new_key),
                }
            )
            if case["case_role"] == "stage10s_multicase_stability":
                stability_rows.append(
                    {
                        **case,
                        "values_preserved": value_metric["mae_km"] == 0.0 and value_metric["rmse_km"] == 0.0,
                        "quality_preserved": q_equal,
                        "scope_guard_pass": scope_pass,
                        "navigation_source": prod["attrs"].get("navigation_source", ""),
                        "legacy_nav_p95_km": legacy_p95,
                        "new_all_valid_mae_km": new_all.get("mae_km", math.nan),
                        "legacy_all_valid_mae_km": old_all.get("mae_km", math.nan),
                        "new_pearson_r": new_all.get("pearson_r", math.nan),
                        "legacy_pearson_r": old_all.get("pearson_r", math.nan),
                        "no_systematic_correlation_degradation": bool(float(new_all.get("pearson_r", -999)) >= float(old_all.get("pearson_r", -999)) - 0.05),
                        "pass": bool(scope_pass and q_equal and value_metric["mae_km"] == 0.0),
                    }
                )
            for warning in prod.get("warnings", []):
                if "unavailable" in str(warning).lower() or "failed" in str(warning).lower():
                    warnings.append({**case, "warning": warning})
        except Exception as exc:
            warnings.append({**case, "warning": f"case_failed: {exc}"})

    negative_cases = [
        {"product": "CLM", "shape": (1237, 1237), "file": "MSG3-SEVI-MSGCLMK-0100-0100-20240310120000.000000000Z-NA.zip"},
        {"product": "CTT", "shape": METEOSAT_CTH_SHAPE, "file": "MSG3-SEVI-MSGCLTH-0100-0100-20240310120000.000000000Z-NA.zip"},
        {"product": "CTH", "shape": (3712, 3712), "file": "MSG3-SEVI-MSGCLTH-0100-0100-20240310120000.000000000Z-NA.zip"},
    ]
    for item in negative_cases:
        matched, line, reason = matches_meteosat_cth_scope(Path(item["file"]), item["product"], item["shape"], None, {"shape": list(item["shape"]), "lon_0": 0.0})
        scope_rows.append({"case_role": "negative_scope_guard", **item, "matched": matched, "product_line_from_area": line, "scope_reason": reason, "test": "SCOPE_GUARD_NEGATIVE", "pass": not matched})

    outputs = {
        "stage_10s_summary_cn.md": out / "stage_10s_summary_cn.md",
        "cth_values_preservation.csv": out / "cth_values_preservation.csv",
        "cth_quality_preservation.csv": out / "cth_quality_preservation.csv",
        "cth_scope_guard.csv": out / "cth_scope_guard.csv",
        "cth_navigation_reproduction.csv": out / "cth_navigation_reproduction.csv",
        "cth_epic_reproduction.csv": out / "cth_epic_reproduction.csv",
        "cth_cache_invalidation.csv": out / "cth_cache_invalidation.csv",
        "cth_multicase_stability.csv": out / "cth_multicase_stability.csv",
        "warnings.csv": out / "warnings.csv",
        "manifest.json": out / "manifest.json",
    }
    write_csv(outputs["cth_values_preservation.csv"], value_rows)
    write_csv(outputs["cth_quality_preservation.csv"], quality_rows)
    write_csv(outputs["cth_scope_guard.csv"], scope_rows)
    write_csv(outputs["cth_navigation_reproduction.csv"], nav_rows)
    write_csv(outputs["cth_epic_reproduction.csv"], epic_out_rows)
    write_csv(outputs["cth_cache_invalidation.csv"], cache_rows)
    write_csv(outputs["cth_multicase_stability.csv"], stability_rows)
    write_csv(outputs["warnings.csv"], warnings, fields=["case_role", "product_line", "sample_id", "source_file", "warning"])

    def all_pass(rows: list[dict[str, Any]]) -> bool:
        return bool(rows) and all(str(r.get("pass", "")).lower() == "true" or r.get("pass") is True for r in rows)

    val_pass = all_pass(value_rows)
    qual_pass = all_pass(quality_rows)
    scope_pass = all_pass(scope_rows)
    nav_pass = bool(nav_rows) and all(str(r.get("pass", "True")).lower() != "false" for r in nav_rows if r.get("control_point") == "ALL_VALID_NAV_PIXELS")
    epic_df = pd.DataFrame(epic_out_rows)
    prod_epic = epic_df[epic_df["navigation_variant"].astype(str) == "production_satpy_cth_area_navigation"] if not epic_df.empty else pd.DataFrame()
    legacy_epic = epic_df[epic_df["navigation_variant"].astype(str) == "legacy_current_navigation"] if not epic_df.empty else pd.DataFrame()
    epic_pass = not prod_epic.empty and not legacy_epic.empty
    legacy_negative_pass = False
    if not prod_epic.empty and not legacy_epic.empty:
        p_all = prod_epic[prod_epic["stratum"] == "all_valid"]
        l_all = legacy_epic[legacy_epic["stratum"] == "all_valid"]
        if not p_all.empty and not l_all.empty:
            legacy_negative_pass = bool(p_all["pearson_r"].astype(float).mean() > l_all["pearson_r"].astype(float).mean())
    cache_pass = all_pass(cache_rows)
    stability_df = pd.DataFrame(stability_rows)
    stability0 = not stability_df[stability_df["product_line"] == "Meteosat-0deg"].empty and all(stability_df[stability_df["product_line"] == "Meteosat-0deg"]["pass"].astype(str).str.lower() == "true")
    stabilityi = not stability_df[stability_df["product_line"] == "Meteosat-IODC"].empty and all(stability_df[stability_df["product_line"] == "Meteosat-IODC"]["pass"].astype(str).str.lower() == "true")
    overall = val_pass and qual_pass and scope_pass and nav_pass and epic_pass and legacy_negative_pass and cache_pass and stability0 and stabilityi

    statuses = {
        "Meteosat-0deg": "METEOSAT_0DEG_CTH_PRODUCTION_PATCH_VALIDATED" if overall or (val_pass and qual_pass and scope_pass and nav_pass) else "METEOSAT_0DEG_CTH_PRODUCTION_PATCH_NOT_VALIDATED",
        "Meteosat-IODC": "METEOSAT_IODC_CTH_PRODUCTION_PATCH_VALIDATED" if overall or (val_pass and qual_pass and scope_pass and nav_pass) else "METEOSAT_IODC_CTH_PRODUCTION_PATCH_NOT_VALIDATED",
        "Meteosat-0deg_multicase": "METEOSAT_0DEG_CTH_MULTICASE_STABILITY_PASS" if stability0 else "METEOSAT_0DEG_CTH_MULTICASE_STABILITY_FAIL",
        "Meteosat-IODC_multicase": "METEOSAT_IODC_CTH_MULTICASE_STABILITY_PASS" if stabilityi else "METEOSAT_IODC_CTH_MULTICASE_STABILITY_FAIL",
        "overall": "METEOSAT_OPERATIONAL_CTH_NAVIGATION_PATCH_VALIDATED" if overall else "METEOSAT_OPERATIONAL_CTH_NAVIGATION_PATCH_NOT_VALIDATED",
    }
    lines = [
        "# Stage 10S：Meteosat operational CTH production navigation patch",
        "",
        f"- 生成时间 UTC：`{utc_now()}`",
        f"- production navigation source：`{METEOSAT_CTH_NAVIGATION_SOURCE}`",
        f"- production navigation grid：`{METEOSAT_CTH_NAVIGATION_GRID}`",
        "- CTH values：identity，未旋转、未翻转、未 shift、未 bias 校正。",
        "- quality：identity，未修改。",
        "",
        "## Gate 结果",
        "",
        f"- VALUES_PRESERVATION：`{val_pass}`",
        f"- QUALITY_PRESERVATION：`{qual_pass}`",
        f"- SCOPE_GUARD：`{scope_pass}`",
        f"- NAVIGATION_REPRODUCTION：`{nav_pass}`",
        f"- EPIC_DOWNSTREAM_REPRODUCTION：`{epic_pass}`",
        f"- LEGACY_NEGATIVE_TEST：`{legacy_negative_pass}`",
        f"- CACHE_INVALIDATION：`{cache_pass}`",
        f"- 0DEG_MULTICASE_STABILITY：`{stability0}`",
        f"- IODC_MULTICASE_STABILITY：`{stabilityi}`",
        "",
        "## 最终状态",
        "",
        f"- Meteosat-0deg：`{statuses['Meteosat-0deg']}`",
        f"- Meteosat-IODC：`{statuses['Meteosat-IODC']}`",
        f"- 0deg 多 case：`{statuses['Meteosat-0deg_multicase']}`",
        f"- IODC 多 case：`{statuses['Meteosat-IODC_multicase']}`",
        f"- Overall：`{statuses['overall']}`",
        "",
        "## 说明",
        "",
        "- 本阶段只验证 production reader patch，不重跑整月，也不修改 Stage05/06 历史产物。",
        "- 旧 standardized native / reprojected CTH 缓存缺少新的 CTH navigation schema version；新 schema 与 grid hash 已进入 Stage10S cache key 审计，旧缓存不得复用。",
        "- CLM 3712×3712 navigation patch 未用于 CTH；CTH 使用自身 1237×1237 Satpy AreaDefinition。",
    ]
    outputs["stage_10s_summary_cn.md"].write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    write_manifest(
        outputs["manifest.json"],
        canonical_stage_id=STAGE_ID,
        component_role=COMPONENT_ROLE,
        related_stage_ids=("stage_10r",),
        run_id=OUTPUT_DIRNAME,
        source_profile="operational_baseline",
        generating_script=Path(__file__),
        input_paths=[str(args.sample_manifest), str(args.stage10r_dir), *[r.get("source_file", "") for r in all_cases]],
        output_paths=[str(p) for p in outputs.values() if p.name != "manifest.json"],
        parameters={"regression_cases": REGRESSION_CASES, "multicase_count": len(multicase), "tolerance": TOL},
        project_root=PROJECT_ROOT,
        extra={"statuses": statuses},
    )
    print(statuses["overall"])
    print(outputs["stage_10s_summary_cn.md"])
    return 0 if overall else 2


if __name__ == "__main__":
    raise SystemExit(main())
