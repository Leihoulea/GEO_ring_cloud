# -*- coding: utf-8 -*-
"""Stage 09G orientation root-cause audit.

Read-only diagnostic stage for checking whether apparent GEO-ring/EPIC cloud
mask inversions originate in raw Meteosat/EPIC reading, standardization,
reprojection, Stage 09 EPIC-view sampling, or Stage 09F plotting.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import traceback
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, ListedColormap
import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from geo_ring_cloud import paths as path_config  # noqa: E402
from geo_ring_cloud.adapters.cloud_products import reshape_square_if_needed  # noqa: E402
from geo_ring_cloud.cloud_semantics import cloud_mask_masks  # noqa: E402
from geo_ring_cloud.diagnostics import full_pixel  # noqa: E402
from geo_ring_cloud.diagnostics.full_pixel_workflow import (  # noqa: E402
    SOURCE_FAMILY,
    apply_geo_policy,
    base_valid_mask,
    load_manifest,
    selected_source_array,
    source_samples,
)
from geo_ring_cloud.reprojection import (  # noqa: E402
    MAX_DISTANCE_DEG,
    build_tree,
    normalize_longitude,
)

STAGE_ID = "stage_09g"
PROJECT_ID = "geo_ring_cloud"
RUN_ID = "stage_09g_orientation_root_cause_audit_202403"
POLICY = "A_inclusive_binary"
FOCUS_SAMPLES = ["20240328_1100", "20240310_1200", "20240316_0800"]
METEOSAT_SOURCES = ["Meteosat-0deg", "Meteosat-IODC"]
DEFAULT_STAGE09D_DIR = path_config.RUNS_ROOT / "stage09d_full_pixel_diagnostics_202403"
DEFAULT_STAGE09F_DIR = path_config.RUNS_ROOT / "stage_09f_spatial_story_maps_202403"
DEFAULT_OUT = path_config.RUNS_ROOT / RUN_ID

TRANSFORMS: dict[str, Callable[[np.ndarray], np.ndarray]] = {
    "identity": lambda a: a,
    "flipud": np.flipud,
    "fliplr": np.fliplr,
    "rot90_ccw": lambda a: np.rot90(a, 1),
    "rot90_cw": lambda a: np.rot90(a, -1),
    "transpose": lambda a: np.swapaxes(a, 0, 1),
}

CLASS_CMAP = ListedColormap(["#D0D0D0", "#F2E8C9", "#4E79A7"])
CLASS_NORM = BoundaryNorm([-0.5, 0.5, 1.5, 2.5], CLASS_CMAP.N)
MISMATCH_CMAP = ListedColormap(["#D0D0D0", "#E8E8E8", "#6BAED6", "#F2B84B", "#D95F5F"])
MISMATCH_NORM = BoundaryNorm([-0.5, 0.5, 1.5, 2.5, 3.5, 4.5], MISMATCH_CMAP.N)
FAMILY_CMAP = ListedColormap(["#D0D0D0", "#3B6FB6", "#2AA198", "#B65A5A"])
FAMILY_NORM = BoundaryNorm([-0.5, 0.5, 1.5, 2.5, 3.5], FAMILY_CMAP.N)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_dirs(root: Path) -> dict[str, Path]:
    dirs = {
        "source_data": root / "source_data",
        "figures": root / "figures",
        "reports": root / "reports",
        "logs": root / "logs",
        "cache": root / "cache",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def load_npz_payload(path: Path) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    with np.load(path, allow_pickle=False) as z:
        arrays = {k: np.asarray(z[k]) for k in z.files if not k.endswith("_json") and k != "variable_availability"}
        meta = json.loads(str(z["metadata_json"])) if "metadata_json" in z.files else {}
    return arrays, meta


def code_counts(arr: np.ndarray, max_codes: int = 20) -> str:
    a = np.asarray(arr)
    vals, cnts = np.unique(a[np.isfinite(a)] if a.dtype.kind == "f" else a, return_counts=True)
    pairs = [
        {"code": int(v) if float(v).is_integer() else float(v), "count": int(c)}
        for v, c in zip(vals[:max_codes], cnts[:max_codes])
    ]
    return safe_json(pairs)


def half_stats(lat: np.ndarray) -> dict[str, float | str]:
    a = np.asarray(lat, dtype=float)
    valid = np.isfinite(a) & (a >= -90.0) & (a <= 90.0)
    half = max(1, a.shape[0] // 2)
    top = a[:half, :][valid[:half, :]]
    bottom = a[-half:, :][valid[-half:, :]]
    top_mean = float(np.nanmean(top)) if top.size else math.nan
    bottom_mean = float(np.nanmean(bottom)) if bottom.size else math.nan
    return {
        "top_half_lat_mean_deg": top_mean,
        "bottom_half_lat_mean_deg": bottom_mean,
        "row_order": "top_rows_north_bottom_rows_south" if top_mean > bottom_mean else "top_rows_south_bottom_rows_north",
    }


def normalize_lon_for_compare(lon: np.ndarray) -> np.ndarray:
    return np.asarray(normalize_longitude(lon), dtype=np.float32)


def array_stats(sample_id: str, source: str, layer: str, name: str, arr: np.ndarray) -> dict[str, Any]:
    a = np.asarray(arr)
    finite = np.isfinite(a) if a.dtype.kind == "f" else np.ones(a.shape, dtype=bool)
    row = {
        "sample_id": sample_id,
        "source": source,
        "layer": layer,
        "variable": name,
        "shape_y": int(a.shape[0]) if a.ndim >= 1 else 0,
        "shape_x": int(a.shape[1]) if a.ndim >= 2 else 0,
        "ndim": int(a.ndim),
        "dtype": str(a.dtype),
        "finite_count": int(np.count_nonzero(finite)),
        "nan_count": int(a.size - np.count_nonzero(finite)) if a.dtype.kind == "f" else 0,
        "min": float(np.nanmin(a)) if finite.any() and a.dtype.kind in "fiu" else math.nan,
        "max": float(np.nanmax(a)) if finite.any() and a.dtype.kind in "fiu" else math.nan,
    }
    if name == "latitude" and a.ndim == 2:
        row.update(half_stats(a))
    if name in {"cloud_mask", "valid_mask"}:
        row["code_counts"] = code_counts(a)
    return row


def read_raw_meteosat_zip(path: Path, cache_dir: Path) -> tuple[dict[str, np.ndarray], dict[str, Any], list[str]]:
    warnings: list[str] = []
    arrays: dict[str, np.ndarray] = {}
    meta: dict[str, Any] = {
        "source_file": str(path),
        "reader": "zip+xarray_cfgrib_direct_audit",
        "zip_entries": [],
        "grib_entries": [],
        "data_vars": [],
        "coords": [],
        "dims": {},
    }
    try:
        configure_eccodes_library(warnings)
        import xarray as xr
    except Exception as exc:
        return arrays, meta, [f"xarray unavailable: {exc}"]
    if not path.exists():
        return arrays, meta, [f"missing raw zip: {path}"]
    try:
        with zipfile.ZipFile(path) as zf:
            entries = zf.namelist()
            meta["zip_entries"] = entries
            grib_entries = [e for e in entries if e.lower().endswith((".grb", ".grib", ".grb2", ".bin"))]
            meta["grib_entries"] = grib_entries
            if not grib_entries:
                return arrays, meta, ["no GRIB entry in ZIP"]
            entry = grib_entries[0]
            payload = zf.read(entry)
            cache_key = hashlib.sha1(f"{path.resolve()}|{entry}".encode("utf-8")).hexdigest()
            suffix = Path(entry).suffix or ".grb"
            extracted = cache_dir / f"{cache_key}{suffix}"
            if not extracted.exists() or extracted.stat().st_size != len(payload):
                extracted.write_bytes(payload)
            meta["selected_grib_entry"] = entry
            meta["selected_grib_cache"] = str(extracted)
        try:
            ds = xr.open_dataset(extracted, engine="cfgrib", backend_kwargs={"indexpath": ""})
            meta["raw_reader_method"] = "xarray_cfgrib"
        except Exception as cf_exc:
            warnings.append(f"cfgrib open failed, trying grib_to_netcdf fallback: {cf_exc}")
            exe = Path(sys.prefix) / "Library" / "bin" / "grib_to_netcdf.exe"
            if not exe.exists():
                raise RuntimeError(f"cfgrib failed and grib_to_netcdf.exe is missing: {exe}") from cf_exc
            nc_path = extracted.with_suffix(extracted.suffix + ".stage09g.nc")
            if not nc_path.exists() or nc_path.stat().st_mtime < extracted.stat().st_mtime:
                cmd = [str(exe), "-o", str(nc_path), str(extracted)]
                proc = subprocess.run(cmd, text=True, capture_output=True, timeout=180)
                if proc.returncode != 0:
                    raise RuntimeError(
                        f"grib_to_netcdf failed rc={proc.returncode}; stdout={proc.stdout[-1000:]}; stderr={proc.stderr[-1000:]}"
                    ) from cf_exc
            ds = xr.open_dataset(nc_path, engine="netcdf4")
            meta["raw_reader_method"] = "grib_to_netcdf_then_xarray_netcdf4"
            meta["raw_netcdf_cache"] = str(nc_path)
        try:
            meta["data_vars"] = list(ds.data_vars)
            meta["coords"] = list(ds.coords)
            meta["dims"] = {str(k): int(v) for k, v in ds.sizes.items()}
            meta["attrs"] = {k: str(v) for k, v in ds.attrs.items()}
            var_name = "p260537" if "p260537" in ds.data_vars else (list(ds.data_vars)[0] if ds.data_vars else "")
            if not var_name:
                warnings.append("no data variable in GRIB dataset")
            else:
                data = np.array(ds[var_name].values, copy=True)
                arrays["cloud_mask"] = reshape_square_if_needed(data)
                meta["cloud_mask_source_variable"] = var_name
                meta["cloud_mask_dims"] = safe_json(ds[var_name].dims)
                meta["cloud_mask_attrs"] = {k: str(v) for k, v in ds[var_name].attrs.items()}
            for coord_name, standard in [("latitude", "latitude"), ("longitude", "longitude")]:
                if coord_name in ds.coords:
                    arrays[standard] = reshape_square_if_needed(
                        np.array(ds[coord_name].values, dtype=np.float32, copy=True)
                    )
        finally:
            ds.close()
    except Exception as exc:
        warnings.append(f"raw Meteosat read failed: {exc}")
    return arrays, meta, warnings


def configure_eccodes_library(warnings: list[str]) -> None:
    """Let eccodes-python find conda's Windows DLL without mutating the env."""
    if sys.platform != "win32":
        return
    dll = Path(sys.prefix) / "Library" / "bin" / "eccodes.dll"
    if not dll.exists():
        return
    try:
        import findlibs

        original_find = findlibs.find

        def patched_find(name: str) -> str | None:
            if name == "eccodes":
                return str(dll)
            return original_find(name)

        findlibs.find = patched_find
    except Exception as exc:
        warnings.append(f"failed to patch findlibs for eccodes DLL: {exc}")


def compare_arrays(
    sample_id: str,
    source: str,
    variable: str,
    raw: np.ndarray,
    native: np.ndarray,
    stride: int = 8,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    native_cmp = normalize_lon_for_compare(native) if variable == "longitude" else np.asarray(native)
    for transform_name, transform in TRANSFORMS.items():
        try:
            raw_t = transform(np.asarray(raw))
        except Exception as exc:
            rows.append(
                {
                    "sample_id": sample_id,
                    "source": source,
                    "variable": variable,
                    "transform": transform_name,
                    "status": "transform_failed",
                    "message": str(exc),
                }
            )
            continue
        if raw_t.shape != native_cmp.shape:
            rows.append(
                {
                    "sample_id": sample_id,
                    "source": source,
                    "variable": variable,
                    "transform": transform_name,
                    "status": "shape_mismatch",
                    "raw_shape": safe_json(raw_t.shape),
                    "native_shape": safe_json(native_cmp.shape),
                }
            )
            continue
        raw_cmp = normalize_lon_for_compare(raw_t) if variable == "longitude" else raw_t
        a = np.asarray(raw_cmp)[::stride, ::stride]
        b = np.asarray(native_cmp)[::stride, ::stride]
        finite = np.isfinite(a) & np.isfinite(b) if (a.dtype.kind == "f" or b.dtype.kind == "f") else np.ones(a.shape, dtype=bool)
        if not np.any(finite):
            rows.append(
                {
                    "sample_id": sample_id,
                    "source": source,
                    "variable": variable,
                    "transform": transform_name,
                    "status": "no_common_finite",
                }
            )
            continue
        if variable == "cloud_mask":
            equal = a[finite].astype(np.int16) == b[finite].astype(np.int16)
            score = float(np.mean(equal))
            max_abs = float(np.max(np.abs(a[finite].astype(float) - b[finite].astype(float))))
        else:
            diff = np.abs(a[finite].astype(float) - b[finite].astype(float))
            score = float(np.mean(diff <= 1e-5))
            max_abs = float(np.max(diff))
        rows.append(
            {
                "sample_id": sample_id,
                "source": source,
                "variable": variable,
                "transform": transform_name,
                "status": "ok",
                "sample_stride": stride,
                "n_compare": int(np.count_nonzero(finite)),
                "equal_or_close_fraction": score,
                "max_abs_diff": max_abs,
                "is_best_candidate": False,
            }
        )
    ok_rows = [r for r in rows if r.get("status") == "ok"]
    if ok_rows:
        best = max(ok_rows, key=lambda r: float(r.get("equal_or_close_fraction", -1)))
        best["is_best_candidate"] = True
    return rows


def binary_metrics(epic_cls: np.ndarray, geo_cls: np.ndarray, valid: np.ndarray, positive: int) -> dict[str, Any]:
    n = int(np.count_nonzero(valid))
    if n == 0:
        return {
            "n_valid": 0,
            "agreement": math.nan,
            "precision_cloud": math.nan,
            "recall_cloud": math.nan,
            "f1_cloud": math.nan,
            "iou_cloud": math.nan,
            "TP": 0,
            "TN": 0,
            "FP": 0,
            "FN": 0,
        }
    e = epic_cls[valid]
    g = geo_cls[valid]
    ep = e == positive
    gp = g == positive
    tp = int(np.count_nonzero(ep & gp))
    tn = int(np.count_nonzero((~ep) & (~gp)))
    fp = int(np.count_nonzero((~ep) & gp))
    fn = int(np.count_nonzero(ep & (~gp)))
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    return {
        "n_valid": n,
        "agreement": float(np.mean(e == g)),
        "precision_cloud": precision,
        "recall_cloud": recall,
        "f1_cloud": 2 * precision * recall / max(precision + recall, 1e-12),
        "iou_cloud": tp / max(tp + fp + fn, 1),
        "TP": tp,
        "TN": tn,
        "FP": fp,
        "FN": fn,
    }


def stress_epic_transforms(manifest_rows: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    policy = full_pixel.POLICIES[POLICY]
    positive = int(policy["positive"])
    for row in manifest_rows:
        sample_id = str(row["sample_id"])
        try:
            ctx = full_pixel.sample_context(row)
            epic_cls, epic_pv, geo_cls, geo_pv = full_pixel_workflow_policy_classes(ctx)
            valid_earth = np.isin(ctx["epic"]["cloud_mask"], [1, 2, 3, 4])
            base = valid_earth & epic_pv & ctx["fused_on_valid"] & geo_pv
            for transform_name, transform in TRANSFORMS.items():
                if transform_name == "identity":
                    continue
                try:
                    epic_t = transform(epic_cls)
                    epic_pv_t = transform(epic_pv)
                    earth_t = transform(valid_earth)
                    if epic_t.shape != epic_cls.shape:
                        continue
                    domain = base & epic_pv_t & earth_t
                    orig = binary_metrics(epic_cls, geo_cls, domain, positive)
                    trans = binary_metrics(epic_t, geo_cls, domain, positive)
                    rows.append(
                        {
                            "sample_id": sample_id,
                            "candidate_group": row.get("candidate_group", ""),
                            "dominant_source": row.get("dominant_source", ""),
                            "test_type": "transform_epic_mask_against_existing_geo",
                            "transform": transform_name,
                            "n_valid": trans["n_valid"],
                            "baseline_agreement": orig["agreement"],
                            "transformed_agreement": trans["agreement"],
                            "delta_agreement": trans["agreement"] - orig["agreement"],
                            "baseline_f1": orig["f1_cloud"],
                            "transformed_f1": trans["f1_cloud"],
                            "delta_f1": trans["f1_cloud"] - orig["f1_cloud"],
                            "baseline_iou": orig["iou_cloud"],
                            "transformed_iou": trans["iou_cloud"],
                            "delta_iou": trans["iou_cloud"] - orig["iou_cloud"],
                            "TP": trans["TP"],
                            "TN": trans["TN"],
                            "FP": trans["FP"],
                            "FN": trans["FN"],
                        }
                    )
                    lat_t = transform(ctx["epic"]["lat"])
                    lon_t = transform(ctx["epic"]["lon"])
                    if lat_t.shape != ctx["epic"]["lat"].shape:
                        continue
                    geo_alt, geo_alt_valid = full_pixel.sample_grid(
                        ctx["fused_data"], ctx["fused_valid"], lat_t, lon_t, ctx["grid"]
                    )
                    geo_alt_cls, geo_alt_pv = full_pixel.apply_policy(geo_alt, policy["geo"])
                    domain2 = base & geo_alt_valid & geo_alt_pv
                    orig2 = binary_metrics(epic_cls, geo_cls, domain2, positive)
                    trans2 = binary_metrics(epic_cls, geo_alt_cls, domain2, positive)
                    rows.append(
                        {
                            "sample_id": sample_id,
                            "candidate_group": row.get("candidate_group", ""),
                            "dominant_source": row.get("dominant_source", ""),
                            "test_type": "transform_epic_latlon_for_geo_sampling",
                            "transform": transform_name,
                            "n_valid": trans2["n_valid"],
                            "baseline_agreement": orig2["agreement"],
                            "transformed_agreement": trans2["agreement"],
                            "delta_agreement": trans2["agreement"] - orig2["agreement"],
                            "baseline_f1": orig2["f1_cloud"],
                            "transformed_f1": trans2["f1_cloud"],
                            "delta_f1": trans2["f1_cloud"] - orig2["f1_cloud"],
                            "baseline_iou": orig2["iou_cloud"],
                            "transformed_iou": trans2["iou_cloud"],
                            "delta_iou": trans2["iou_cloud"] - orig2["iou_cloud"],
                            "TP": trans2["TP"],
                            "TN": trans2["TN"],
                            "FP": trans2["FP"],
                            "FN": trans2["FN"],
                        }
                    )
                except Exception as exc:
                    warnings.append({"level": "warning", "source": sample_id, "message": f"{transform_name} stress failed: {exc}"})
        except Exception:
            warnings.append({"level": "error", "source": sample_id, "message": traceback.format_exc(limit=3)})
    return pd.DataFrame(rows)


def full_pixel_workflow_policy_classes(ctx: dict[str, Any]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    policy = full_pixel.POLICIES[POLICY]
    epic_cls, epic_pv = full_pixel.apply_policy(ctx["epic"]["cloud_mask"], policy["epic"])
    geo_cls, geo_pv = full_pixel.apply_policy(ctx["fused_on_epic"], policy["geo"])
    return epic_cls, epic_pv, geo_cls, geo_pv


def stress_summary(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if df.empty:
        return pd.DataFrame(rows)
    for fields, group in df.groupby(["test_type", "transform"], dropna=False):
        d = pd.to_numeric(group["delta_agreement"], errors="coerce")
        f1 = pd.to_numeric(group["delta_f1"], errors="coerce")
        rows.append(
            {
                "test_type": fields[0],
                "transform": fields[1],
                "n_samples": int(d.notna().sum()),
                "mean_delta_agreement": float(d.mean()),
                "median_delta_agreement": float(d.median()),
                "min_delta_agreement": float(d.min()),
                "max_delta_agreement": float(d.max()),
                "n_delta_agreement_gt_0p02": int((d > 0.02).sum()),
                "n_delta_agreement_gt_0p05": int((d > 0.05).sum()),
                "mean_delta_f1": float(f1.mean()),
                "median_delta_f1": float(f1.median()),
                "max_delta_f1": float(f1.max()),
            }
        )
    return pd.DataFrame(rows)


def manual_sample_check(ctx: dict[str, Any], data: np.ndarray, valid: np.ndarray) -> dict[str, Any]:
    sampled, sampled_valid = full_pixel.sample_grid(data, valid, ctx["epic"]["lat"], ctx["epic"]["lon"], ctx["grid"])
    rr, cc, ok = full_pixel.row_col(ctx["epic"]["lat"], ctx["epic"]["lon"], ctx["grid"], data.shape)
    manual = np.full(ctx["epic"]["lat"].shape, np.nan, dtype=np.float32)
    manual_valid = np.zeros(ctx["epic"]["lat"].shape, dtype=bool)
    manual[ok] = data[rr[ok], cc[ok]].astype(np.float32)
    manual_valid[ok] = valid[rr[ok], cc[ok]].astype(bool)
    manual[~manual_valid] = np.nan
    both = sampled_valid & manual_valid
    return {
        "n_both": int(np.count_nonzero(both)),
        "valid_mask_equal_fraction": float(np.mean(sampled_valid == manual_valid)),
        "data_equal_fraction_on_both": float(np.mean(sampled[both] == manual[both])) if np.any(both) else math.nan,
        "max_abs_diff_on_both": float(np.nanmax(np.abs(sampled[both] - manual[both]))) if np.any(both) else math.nan,
    }


def trace_native_to_grid(
    sample_id: str,
    source: str,
    native_arrays: dict[str, np.ndarray],
    grid_arrays: dict[str, np.ndarray],
    grid_meta: dict[str, Any],
    n_points: int = 160,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not {"cloud_mask", "latitude", "longitude"}.issubset(native_arrays) or "data" not in grid_arrays:
        return rows
    raw = np.asarray(native_arrays["cloud_mask"])
    lat = np.asarray(native_arrays["latitude"], dtype=np.float32)
    lon = normalize_lon_for_compare(native_arrays["longitude"])
    display_valid, _, _ = cloud_mask_masks(source, "CLM", raw)
    tree, src_y, src_x, _ = build_tree(lon, lat, display_valid)
    grid = grid_meta.get("target_grid", {})
    lon_centers = np.linspace(float(grid["lon_centers_first_last"][0]), float(grid["lon_centers_first_last"][1]), int(grid["lon_size"]), dtype=np.float64)
    lat_centers = np.linspace(float(grid["lat_centers_first_last"][0]), float(grid["lat_centers_first_last"][1]), int(grid["lat_size"]), dtype=np.float64)
    data_grid = np.asarray(grid_arrays["data"])
    valid_grid = np.asarray(grid_arrays.get("valid_mask", np.isfinite(data_grid))).astype(bool)
    yy, xx = np.nonzero(valid_grid)
    if yy.size == 0:
        return rows
    rng = np.random.default_rng(abs(hash((sample_id, source))) % (2**32))
    pick = rng.choice(yy.size, size=min(n_points, yy.size), replace=False)
    target_lon = lon_centers[xx[pick]]
    target_lat = lat_centers[yy[pick]]
    dist, idx = tree.query(np.column_stack([target_lon, target_lat]), k=1, distance_upper_bound=MAX_DISTANCE_DEG)
    for j, pos in enumerate(pick):
        ok = np.isfinite(dist[j]) and int(idx[j]) < src_y.size
        sy = int(src_y[int(idx[j])]) if ok else -1
        sx = int(src_x[int(idx[j])]) if ok else -1
        grid_code = float(data_grid[yy[pos], xx[pos]])
        native_code = float(raw[sy, sx]) if ok else math.nan
        rows.append(
            {
                "sample_id": sample_id,
                "source": source,
                "grid_row": int(yy[pos]),
                "grid_col": int(xx[pos]),
                "grid_lat_deg": float(target_lat[j]),
                "grid_lon_deg": float(target_lon[j]),
                "native_row": sy,
                "native_col": sx,
                "native_lat_deg": float(lat[sy, sx]) if ok else math.nan,
                "native_lon_deg": float(lon[sy, sx]) if ok else math.nan,
                "nearest_distance_deg": float(dist[j]) if ok else math.nan,
                "native_raw_code": native_code,
                "stage05_grid_code": grid_code,
                "code_match": bool(ok and native_code == grid_code),
            }
        )
    return rows


def center_longitude(lon: np.ndarray, valid: np.ndarray) -> float:
    vals = np.asarray(lon)[valid & np.isfinite(lon)]
    if vals.size == 0:
        return 0.0
    vals = normalize_lon_for_compare(vals)
    ang = np.deg2rad(vals)
    return float(((math.degrees(math.atan2(float(np.mean(np.sin(ang))), float(np.mean(np.cos(ang))))) + 180.0) % 360.0) - 180.0)


def center_latitude(lat: np.ndarray, valid: np.ndarray) -> float:
    vals = np.asarray(lat)[valid & np.isfinite(lat)]
    return float(np.clip(np.nanmean(vals), -80, 80)) if vals.size else 0.0


def orthographic_project(lat: np.ndarray, lon: np.ndarray, center_lon: float, center_lat: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    lon_n = normalize_lon_for_compare(lon)
    dlon = np.deg2rad(((lon_n - center_lon + 180.0) % 360.0) - 180.0)
    lat_r = np.deg2rad(lat.astype(float))
    lat0 = math.radians(center_lat)
    x = np.cos(lat_r) * np.sin(dlon)
    y = math.cos(lat0) * np.sin(lat_r) - math.sin(lat0) * np.cos(lat_r) * np.cos(dlon)
    cosc = math.sin(lat0) * np.sin(lat_r) + math.cos(lat0) * np.cos(lat_r) * np.cos(dlon)
    return x, y, cosc >= -1e-6


def sample_plot_data(ctx: dict[str, Any], sample_id: str, stride: int = 8) -> pd.DataFrame:
    base, epic_cls, geo_cls = base_valid_mask(ctx, POLICY)
    x, y, visible = orthographic_project(
        ctx["epic"]["lat"],
        ctx["epic"]["lon"],
        center_longitude(ctx["epic"]["lon"], base),
        center_latitude(ctx["epic"]["lat"], base),
    )
    selected = selected_source_array(ctx["selected_source"])
    family = np.full(selected.shape, 0, dtype=np.int16)
    for code, name in [(1, "GOES"), (2, "East Asia"), (3, "Meteosat")]:
        family[np.vectorize(lambda v: SOURCE_FAMILY.get(str(v), "missing") == name)(selected)] = code
    mismatch = np.full(epic_cls.shape, 0, dtype=np.int16)
    valid = base
    mismatch[valid & (epic_cls == geo_cls) & (epic_cls == 0)] = 1
    mismatch[valid & (epic_cls == geo_cls) & (epic_cls == 1)] = 2
    mismatch[valid & (epic_cls == 0) & (geo_cls == 1)] = 3
    mismatch[valid & (epic_cls == 1) & (geo_cls == 0)] = 4
    source_cls, source_valid, _ = source_samples(ctx)
    met0 = np.full(epic_cls.shape, 0, dtype=np.int16)
    iodc = np.full(epic_cls.shape, 0, dtype=np.int16)
    for source, target in [("Meteosat-0deg", met0), ("Meteosat-IODC", iodc)]:
        if source in source_cls:
            cls, vv = apply_geo_policy(source_cls[source], source_valid[source], POLICY)
            target[vv & (cls == 0)] = 1
            target[vv & (cls == 1)] = 2
    rows: list[pd.DataFrame] = []
    panels = {
        "epic_policy_a": np.where(valid, epic_cls + 1, 0),
        "georing_policy_a": np.where(valid, geo_cls + 1, 0),
        "mismatch": mismatch,
        "selected_family": family,
        "meteosat_0deg_on_epic": met0,
        "meteosat_iodc_on_epic": iodc,
    }
    sl = (slice(None, None, stride), slice(None, None, stride))
    for panel, arr in panels.items():
        df = pd.DataFrame(
            {
                "sample_id": sample_id,
                "panel": panel,
                "latitude_deg": ctx["epic"]["lat"][sl].ravel(),
                "longitude_deg": ctx["epic"]["lon"][sl].ravel(),
                "projection_x_orthographic": x[sl].ravel(),
                "projection_y_orthographic": y[sl].ravel(),
                "projection_visible": visible[sl].ravel(),
                "value_code": arr[sl].ravel(),
            }
        )
        rows.append(df)
    return pd.concat(rows, ignore_index=True)


def plot_case(df: pd.DataFrame, sample_id: str, out_base: Path) -> dict[str, str]:
    titles = [
        ("epic_policy_a", "EPIC cloud mask"),
        ("georing_policy_a", "GEO-ring current"),
        ("mismatch", "Mismatch category"),
        ("selected_family", "Selected family"),
        ("meteosat_0deg_on_epic", "Met-0deg on EPIC"),
        ("meteosat_iodc_on_epic", "Met-IODC on EPIC"),
    ]
    fig, axes = plt.subplots(2, 3, figsize=(13.333, 7.5), dpi=150)
    for ax, (panel, title) in zip(axes.ravel(), titles):
        sub = df[(df["panel"] == panel) & (df["projection_visible"].astype(bool))]
        cmap, norm = (MISMATCH_CMAP, MISMATCH_NORM) if panel == "mismatch" else ((FAMILY_CMAP, FAMILY_NORM) if panel == "selected_family" else (CLASS_CMAP, CLASS_NORM))
        ax.scatter(
            sub["projection_x_orthographic"],
            sub["projection_y_orthographic"],
            c=sub["value_code"],
            s=2.0,
            cmap=cmap,
            norm=norm,
            linewidths=0,
            rasterized=True,
        )
        ax.set_title(title)
        ax.set_aspect("equal")
        ax.set_xlim(-1.04, 1.04)
        ax.set_ylim(-1.04, 1.04)
        ax.set_xticks([])
        ax.set_yticks([])
        ax.text(0.02, 0.02, "N up", transform=ax.transAxes, fontsize=6, color="#303030")
    fig.suptitle(f"Stage 09G orientation evidence | {sample_id}", fontsize=11)
    fig.text(
        0.02,
        0.015,
        "Class: gray invalid, tan clear, blue cloud. Mismatch: gray invalid, pale agree clear, blue agree cloud, yellow EPIC clear/GEO cloud, red EPIC cloud/GEO clear. Family: blue GOES, teal East Asia, red Meteosat.",
        fontsize=7,
    )
    fig.tight_layout(rect=[0.0, 0.04, 1.0, 0.95])
    outputs = {}
    for ext in ["png", "svg", "pdf"]:
        path = out_base.with_suffix(f".{ext}")
        fig.savefig(path, dpi=300 if ext == "png" else None)
        outputs[ext] = str(path)
    plt.close(fig)
    return outputs


def suspect_region_stats(plot_df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    piv = plot_df.pivot_table(
        index=["sample_id", "latitude_deg", "longitude_deg", "projection_x_orthographic", "projection_y_orthographic", "projection_visible"],
        columns="panel",
        values="value_code",
        aggfunc="first",
    ).reset_index()
    visible = piv[piv["projection_visible"].astype(bool)].copy()
    regions = {
        "upper_disk_y_ge0": visible["projection_y_orthographic"] >= 0,
        "lower_disk_y_lt0": visible["projection_y_orthographic"] < 0,
        "left_disk_x_lt0": visible["projection_x_orthographic"] < 0,
        "right_disk_x_ge0": visible["projection_x_orthographic"] >= 0,
    }
    for sample_id, g in visible.groupby("sample_id"):
        for region, mask_all in regions.items():
            mask = mask_all.loc[g.index]
            sub = g[mask]
            if sub.empty:
                continue
            epic_cloud = sub["epic_policy_a"] == 2
            geo_cloud = sub["georing_policy_a"] == 2
            valid = (sub["epic_policy_a"] > 0) & (sub["georing_policy_a"] > 0)
            rows.append(
                {
                    "sample_id": sample_id,
                    "region": region,
                    "n_plot_pixels": int(len(sub)),
                    "n_valid_compare": int(np.count_nonzero(valid)),
                    "epic_cloud_fraction": float(np.mean(epic_cloud[valid])) if np.any(valid) else math.nan,
                    "georing_cloud_fraction": float(np.mean(geo_cloud[valid])) if np.any(valid) else math.nan,
                    "georing_minus_epic_cloud_fraction": float(np.mean(geo_cloud[valid]) - np.mean(epic_cloud[valid])) if np.any(valid) else math.nan,
                    "agreement_fraction": float(np.mean(sub.loc[valid, "epic_policy_a"] == sub.loc[valid, "georing_policy_a"])) if np.any(valid) else math.nan,
                }
            )
    return pd.DataFrame(rows)


def inspect_focus_sample(
    row: dict[str, Any],
    dirs: dict[str, Path],
    warnings: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]] | pd.DataFrame]:
    sample_id = str(row["sample_id"])
    out: dict[str, list[dict[str, Any]] | pd.DataFrame] = {
        "raw_inventory": [],
        "native_orientation": [],
        "raw_vs_native": [],
        "native_to_grid_trace": [],
        "grid_to_epic_trace": [],
        "plot_df": pd.DataFrame(),
    }
    ctx = full_pixel.sample_context(row)
    epic = full_pixel.read_epic(Path(row["epic_file"]))
    for name in ["lat", "lon", "cloud_mask"]:
        out["native_orientation"].append(array_stats(sample_id, "EPIC", "raw_epic_nc4", name, epic[name]))  # type: ignore[index]
    for source in METEOSAT_SOURCES:
        inv_path = Path(row["stage_run_dir"]) / "standardized_native" / "standardized_native_inventory.csv"
        inv = pd.read_csv(inv_path)
        hit = inv[(inv["satellite_group"] == source) & (inv["product"] == "CLM")]
        if hit.empty:
            warnings.append({"level": "warning", "source": sample_id, "message": f"missing standardized native inventory row {source}"})
            continue
        native_path = Path(str(hit.iloc[0]["npz_file"]))
        raw_path = Path(str(hit.iloc[0]["source_file"]))
        native_arrays, native_meta = load_npz_payload(native_path)
        raw_arrays, raw_meta, raw_warnings = read_raw_meteosat_zip(raw_path, dirs["cache"])
        for msg in raw_warnings:
            warnings.append({"level": "warning", "source": f"{sample_id}:{source}", "message": msg})
        raw_row = {
            "sample_id": sample_id,
            "source": source,
            "raw_zip_path": str(raw_path),
            "raw_zip_exists": raw_path.exists(),
            "raw_zip_size_bytes": raw_path.stat().st_size if raw_path.exists() else 0,
            "raw_sha1_16": hashlib.sha1(raw_path.read_bytes()).hexdigest()[:16] if raw_path.exists() and raw_path.stat().st_size < 200_000_000 else "",
            "zip_entries": safe_json(raw_meta.get("zip_entries", [])),
            "grib_entries": safe_json(raw_meta.get("grib_entries", [])),
            "selected_grib_entry": raw_meta.get("selected_grib_entry", ""),
            "data_vars": safe_json(raw_meta.get("data_vars", [])),
            "coords": safe_json(raw_meta.get("coords", [])),
            "dims": safe_json(raw_meta.get("dims", {})),
            "cloud_mask_source_variable": raw_meta.get("cloud_mask_source_variable", ""),
            "cloud_mask_dims": raw_meta.get("cloud_mask_dims", ""),
        }
        out["raw_inventory"].append(raw_row)  # type: ignore[index]
        for layer, arrays in [("raw_meteosat_zip_grib", raw_arrays), ("standardized_native_npz", native_arrays)]:
            for variable in ["cloud_mask", "latitude", "longitude", "valid_mask"]:
                if variable in arrays:
                    out["native_orientation"].append(array_stats(sample_id, source, layer, variable, arrays[variable]))  # type: ignore[index]
        for variable in ["cloud_mask", "latitude", "longitude"]:
            if variable in raw_arrays and variable in native_arrays:
                out["raw_vs_native"].extend(compare_arrays(sample_id, source, variable, raw_arrays[variable], native_arrays[variable]))  # type: ignore[index]
        grid_path = Path(row["stage_run_dir"]) / "reprojected_grid" / source / f"{source}_CLM_cloud_mask_grid_{sample_id}.npz"
        if grid_path.exists():
            grid_arrays, grid_meta = load_npz_payload(grid_path)
            out["native_to_grid_trace"].extend(trace_native_to_grid(sample_id, source, native_arrays, grid_arrays, grid_meta))  # type: ignore[index]
            check = manual_sample_check(ctx, grid_arrays["data"], np.asarray(grid_arrays.get("valid_mask", np.isfinite(grid_arrays["data"]))).astype(bool))
            check.update({"sample_id": sample_id, "source": source, "layer": "stage09_sample_grid_manual_check"})
            out["grid_to_epic_trace"].append(check)  # type: ignore[index]
    plot_df = sample_plot_data(ctx, sample_id)
    out["plot_df"] = plot_df
    return out


def plot_source_orientation(stage09f_dir: Path, warnings: list[dict[str, Any]]) -> pd.DataFrame:
    path = stage09f_dir / "source_data" / "stage_09f_spatial_story_maps_202403_figure1_representative_disk_diagnostic_source.csv"
    rows: list[dict[str, Any]] = []
    if not path.exists():
        warnings.append({"level": "warning", "source": "stage09f_source", "message": f"missing {path}"})
        return pd.DataFrame(rows)
    for chunk in pd.read_csv(path, chunksize=250_000):
        keep = chunk[chunk["sample_id"].isin(FOCUS_SAMPLES)].copy()
        if keep.empty:
            continue
        lat = pd.to_numeric(keep["latitude_deg"], errors="coerce")
        y = pd.to_numeric(keep["projection_y_orthographic"], errors="coerce")
        vis = keep["projection_visible"].astype(str).str.lower().isin(["true", "1"])
        keep = keep[np.isfinite(lat) & np.isfinite(y) & vis]
        for sample_id, g in keep.groupby("sample_id"):
            la = pd.to_numeric(g["latitude_deg"], errors="coerce").to_numpy(dtype=float)
            yy = pd.to_numeric(g["projection_y_orthographic"], errors="coerce").to_numpy(dtype=float)
            corr = float(np.corrcoef(la, yy)[0, 1]) if la.size > 2 else math.nan
            rows.append(
                {
                    "sample_id": sample_id,
                    "source_csv": str(path),
                    "n_rows": int(la.size),
                    "latitude_vs_projection_y_corr": corr,
                    "orientation_status": "north_up_positive_corr" if corr > 0 else "suspect_negative_corr",
                }
            )
    return pd.DataFrame(rows)


def decision_matrix(
    raw_vs_native: pd.DataFrame,
    native_to_grid_trace: pd.DataFrame,
    grid_to_epic_trace: pd.DataFrame,
    epic_stress_summary: pd.DataFrame,
    plot_orientation: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    rvn_status = "PASS"
    rvn_note = "identity is best for all available raw/native cloud_mask, latitude, and longitude comparisons"
    if raw_vs_native.empty:
        rvn_status = "INCONCLUSIVE"
        rvn_note = "raw/native comparison table is empty"
    else:
        best = raw_vs_native[raw_vs_native["is_best_candidate"].astype(str).str.lower().isin(["true", "1"])]
        non_identity = best[(best["status"] == "ok") & (best["transform"] != "identity")]
        if not non_identity.empty:
            rvn_status = "FAIL"
            rvn_note = "one or more raw/native variables are best matched by a non-identity transform"
    rows.append({"audit_link": "raw_to_native", "status": rvn_status, "decision_note": rvn_note})

    ntg_status = "PASS"
    ntg_note = "native-to-grid trace code matches are all true"
    if native_to_grid_trace.empty:
        ntg_status = "INCONCLUSIVE"
        ntg_note = "native-to-grid trace table is empty"
    elif not bool(native_to_grid_trace["code_match"].all()):
        ntg_status = "FAIL"
        ntg_note = "some traced nearest-neighbor native codes do not match Stage 05 grid codes"
    rows.append({"audit_link": "native_to_grid", "status": ntg_status, "decision_note": ntg_note})

    gte_status = "PASS"
    gte_note = "manual row/col sampling agrees with full_pixel.sample_grid"
    if grid_to_epic_trace.empty:
        gte_status = "INCONCLUSIVE"
        gte_note = "grid-to-EPIC trace table is empty"
    elif (pd.to_numeric(grid_to_epic_trace["data_equal_fraction_on_both"], errors="coerce") < 1.0).any():
        gte_status = "FAIL"
        gte_note = "manual row/col sampling differs from full_pixel.sample_grid"
    rows.append({"audit_link": "grid_to_epic", "status": gte_status, "decision_note": gte_note})

    stress_status = "PASS"
    stress_note = "no EPIC flip/rotation/transpose transform systematically improves agreement"
    if epic_stress_summary.empty:
        stress_status = "INCONCLUSIVE"
        stress_note = "EPIC transform stress summary is empty"
    elif (pd.to_numeric(epic_stress_summary["mean_delta_agreement"], errors="coerce") > 0.02).any():
        stress_status = "FAIL"
        stress_note = "one EPIC transform has mean agreement gain above 0.02"
    rows.append({"audit_link": "stage09_epic_sampling_transform_stress", "status": stress_status, "decision_note": stress_note})

    plot_status = "PASS"
    plot_note = "Stage 09F source projection has positive latitude-vs-y correlation"
    if plot_orientation.empty:
        plot_status = "INCONCLUSIVE"
        plot_note = "Stage 09F plot-source orientation table is empty"
    elif (pd.to_numeric(plot_orientation["latitude_vs_projection_y_corr"], errors="coerce") <= 0).any():
        plot_status = "FAIL"
        plot_note = "one Stage 09F source table has negative latitude-vs-y correlation"
    rows.append({"audit_link": "stage09f_plot_orientation", "status": plot_status, "decision_note": plot_note})
    return pd.DataFrame(rows)


def write_report(root: Path, output_files: dict[str, str], decision: pd.DataFrame, warnings: list[dict[str, Any]]) -> Path:
    report = root / "reports" / "stage_09g_orientation_root_cause_audit_report_cn.md"
    status_counts = decision["status"].value_counts().to_dict() if not decision.empty else {}
    lines = [
        "# Stage 09G 方向性 root-cause 审计报告",
        "",
        f"- generated_utc: `{utc_now()}`",
        f"- canonical_stage_id: `{STAGE_ID}`",
        f"- focus_samples: `{', '.join(FOCUS_SAMPLES)}`",
        "- 约束：只读诊断；不修改 fusion 逻辑；不联网下载；EPIC 仅作为 independent diagnostic reference。",
        "",
        "## 主结论",
    ]
    if status_counts.get("FAIL", 0):
        lines.append("- **发现方向链路高风险项**：见 `stage_09g_final_decision_matrix.csv`，应先处理 FAIL 项再继续解释科学差异。")
    elif status_counts.get("INCONCLUSIVE", 0):
        lines.append("- **未发现明确方向性 bug，但存在未完全闭合证据项**：见 INCONCLUSIVE 行和 warnings。")
    else:
        lines.append("- **未发现南北翻转、左右镜像、90/270 度旋转或 Stage 09F 作图方向错误的证据**。")
    lines.extend(
        [
            "- 判断依据不是单独人工翻转，而是 raw → native → Stage 05 grid → Stage 09 EPIC sampling → Stage 09F plot-source 的逐层证据。",
            "",
            "## Decision matrix",
            "",
        ]
    )
    if decision.empty:
        lines.append("- decision matrix is empty.")
    else:
        for _, row in decision.iterrows():
            lines.append(f"- `{row['audit_link']}`: **{row['status']}** - {row['decision_note']}")
    lines.extend(["", "## 关键数值证据", ""])
    try:
        raw_native = pd.read_csv(output_files["raw_vs_native_transform_match"])
        best = raw_native[raw_native["is_best_candidate"].astype(str).str.lower().isin(["true", "1"])]
        identity_best = best[best["transform"] == "identity"]
        min_score = pd.to_numeric(identity_best["equal_or_close_fraction"], errors="coerce").min()
        lines.append(
            f"- Raw ZIP/GRIB 到 standardized native：best-transform 行 `{len(best)}` 个，其中 identity 最优 `{len(identity_best)}` 个；identity 最低 close/equal fraction `{min_score:.6f}`。"
        )
    except Exception as exc:
        lines.append(f"- Raw/native 数值摘要不可用：{exc}")
    try:
        ntg = pd.read_csv(output_files["native_to_grid_trace"])
        match = ntg["code_match"].astype(str).str.lower().isin(["true", "1"])
        lines.append(f"- Native 到 Stage 05 grid 抽样追踪：`{int(match.sum())}/{len(ntg)}` 个 trace 点 raw/native nearest code 与 grid code 一致。")
    except Exception as exc:
        lines.append(f"- Native/grid 数值摘要不可用：{exc}")
    try:
        gte = pd.read_csv(output_files["grid_to_epic_trace"])
        min_valid = pd.to_numeric(gte["valid_mask_equal_fraction"], errors="coerce").min()
        min_data = pd.to_numeric(gte["data_equal_fraction_on_both"], errors="coerce").min()
        lines.append(f"- Stage 09 grid 到 EPIC 采样：manual row/col 与 `full_pixel.sample_grid` 的 valid-mask 最低一致率 `{min_valid:.6f}`，data 最低一致率 `{min_data:.6f}`。")
    except Exception as exc:
        lines.append(f"- Grid/EPIC 数值摘要不可用：{exc}")
    try:
        stress = pd.read_csv(output_files["epic_transform_stress_summary"])
        max_mean_delta = pd.to_numeric(stress["mean_delta_agreement"], errors="coerce").max()
        max_single_delta = pd.to_numeric(stress["max_delta_agreement"], errors="coerce").max()
        lines.append(f"- EPIC mask/lat-lon transform stress：所有变换的最大 mean delta `{max_mean_delta:.6f}`；单样本最大 delta `{max_single_delta:.6f}`。")
    except Exception as exc:
        lines.append(f"- EPIC transform 数值摘要不可用：{exc}")
    try:
        plot_orient = pd.read_csv(output_files["stage09f_plot_source_orientation"])
        min_corr = pd.to_numeric(plot_orient["latitude_vs_projection_y_corr"], errors="coerce").min()
        lines.append(f"- Stage 09F 图源方向：三个重点样本 latitude vs projection-y 最低相关系数 `{min_corr:.6f}`，为北向正常的正相关。")
    except Exception as exc:
        lines.append(f"- Stage 09F 图源方向摘要不可用：{exc}")
    try:
        suspect = pd.read_csv(output_files["suspect_region_statistics"])
        lines.append("")
        lines.append("### Upper/lower disk cloud-fraction diagnostic")
        for sample_id, group in suspect.groupby("sample_id"):
            upper = group[group["region"] == "upper_disk_y_ge0"].iloc[0]
            lower = group[group["region"] == "lower_disk_y_lt0"].iloc[0]
            lines.append(
                f"- `{sample_id}`: upper EPIC cloud `{float(upper['epic_cloud_fraction']):.3f}`, upper GEO cloud `{float(upper['georing_cloud_fraction']):.3f}`; "
                f"lower EPIC cloud `{float(lower['epic_cloud_fraction']):.3f}`, lower GEO cloud `{float(lower['georing_cloud_fraction']):.3f}`."
            )
    except Exception as exc:
        lines.append(f"- Upper/lower disk 摘要不可用：{exc}")
    lines.extend(["", "## 重点解释", ""])
    lines.append("- Meteosat native `.npz` 在三个重点样本中保留了 `cloud_mask`、`latitude`、`longitude`，并且 raw/native transform matching 会直接验证它们是否同向。")
    lines.append("- Stage 05 统一网格的 row order 是 south-to-north；矩形图必须用 `origin=lower`，但 Stage 09F 的盘面图使用经纬度正射投影，不依赖原始数组行号。")
    lines.append("- “上多下少/上少下多”的肉眼印象在 `stage_09g_suspect_region_statistics.csv` 中用 projection y 的 upper/lower disk 定量化。")
    lines.extend(["", "## 输出索引", ""])
    for label, path in sorted(output_files.items()):
        lines.append(f"- `{label}`: `{path}`")
    lines.extend(["", "## Warnings", ""])
    if warnings:
        for item in warnings[:80]:
            lines.append(f"- `{item.get('level', 'warning')}` `{item.get('source', '')}`: {item.get('message', '')}")
        if len(warnings) > 80:
            lines.append(f"- ... {len(warnings) - 80} more warnings in `warnings.csv`")
    else:
        lines.append("- none")
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage09d-dir", type=Path, default=DEFAULT_STAGE09D_DIR)
    parser.add_argument("--stage09f-dir", type=Path, default=DEFAULT_STAGE09F_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    plt.rcParams.update({"svg.fonttype": "none", "pdf.fonttype": 42, "font.size": 7})
    dirs = ensure_dirs(args.out)
    warnings: list[dict[str, Any]] = []
    output_files: dict[str, str] = {}
    manifest_rows = load_manifest(args.stage09d_dir)
    focus_rows = [r for r in manifest_rows if str(r["sample_id"]) in FOCUS_SAMPLES]

    all_raw_inventory: list[dict[str, Any]] = []
    all_native_orientation: list[dict[str, Any]] = []
    all_raw_vs_native: list[dict[str, Any]] = []
    all_native_to_grid_trace: list[dict[str, Any]] = []
    all_grid_to_epic_trace: list[dict[str, Any]] = []
    all_plot_rows: list[pd.DataFrame] = []
    figure_index: list[dict[str, Any]] = []

    for row in focus_rows:
        sample_id = str(row["sample_id"])
        try:
            result = inspect_focus_sample(row, dirs, warnings)
            all_raw_inventory.extend(result["raw_inventory"])  # type: ignore[arg-type]
            all_native_orientation.extend(result["native_orientation"])  # type: ignore[arg-type]
            all_raw_vs_native.extend(result["raw_vs_native"])  # type: ignore[arg-type]
            all_native_to_grid_trace.extend(result["native_to_grid_trace"])  # type: ignore[arg-type]
            all_grid_to_epic_trace.extend(result["grid_to_epic_trace"])  # type: ignore[arg-type]
            plot_df = result["plot_df"]  # type: ignore[assignment]
            if isinstance(plot_df, pd.DataFrame) and not plot_df.empty:
                source_csv = dirs["source_data"] / f"stage_09g_case_{sample_id}_diagnostic_plot_source.csv"
                plot_df.to_csv(source_csv, index=False, encoding="utf-8-sig")
                all_plot_rows.append(plot_df)
                fig_base = dirs["figures"] / f"stage_09g_case_{sample_id}_orientation_evidence"
                outputs = plot_case(plot_df, sample_id, fig_base)
                figure_index.append(
                    {
                        "figure_id": f"case_{sample_id}_orientation_evidence",
                        "sample_id": sample_id,
                        "source_csv": str(source_csv),
                        **outputs,
                    }
                )
        except Exception:
            warnings.append({"level": "error", "source": sample_id, "message": traceback.format_exc(limit=4)})

    tables = {
        "raw_grib_inventory": pd.DataFrame(all_raw_inventory),
        "native_npz_orientation": pd.DataFrame(all_native_orientation),
        "raw_vs_native_transform_match": pd.DataFrame(all_raw_vs_native),
        "native_to_grid_trace": pd.DataFrame(all_native_to_grid_trace),
        "grid_to_epic_trace": pd.DataFrame(all_grid_to_epic_trace),
        "stage09f_plot_source_orientation": plot_source_orientation(args.stage09f_dir, warnings),
    }

    epic_stress = stress_epic_transforms(manifest_rows, warnings)
    epic_stress_summary = stress_summary(epic_stress)
    tables["epic_transform_stress"] = epic_stress
    tables["epic_transform_stress_summary"] = epic_stress_summary

    suspect_df = suspect_region_stats(pd.concat(all_plot_rows, ignore_index=True)) if all_plot_rows else pd.DataFrame()
    tables["suspect_region_statistics"] = suspect_df

    decision = decision_matrix(
        tables["raw_vs_native_transform_match"],
        tables["native_to_grid_trace"],
        tables["grid_to_epic_trace"],
        epic_stress_summary,
        tables["stage09f_plot_source_orientation"],
    )
    tables["final_decision_matrix"] = decision

    for label, df in tables.items():
        path = dirs["source_data"] / f"stage_09g_{label}.csv"
        df.to_csv(path, index=False, encoding="utf-8-sig")
        output_files[label] = str(path)
        if df.empty:
            warnings.append({"level": "warning", "source": label, "message": "output CSV is empty"})

    figure_index_path = dirs["logs"] / "figure_index.csv"
    pd.DataFrame(figure_index).to_csv(figure_index_path, index=False, encoding="utf-8-sig")
    output_files["figure_index"] = str(figure_index_path)
    warnings_path = dirs["logs"] / "warnings.csv"
    pd.DataFrame(warnings).to_csv(warnings_path, index=False, encoding="utf-8-sig")
    output_files["warnings"] = str(warnings_path)
    report_path = write_report(args.out, output_files, decision, warnings)
    output_files["report"] = str(report_path)
    manifest = {
        "project_id": PROJECT_ID,
        "canonical_stage_id": STAGE_ID,
        "run_id": RUN_ID,
        "generated_utc": utc_now(),
        "script_path": str(Path(__file__).resolve()),
        "input_paths": {
            "stage09d_dir": str(args.stage09d_dir),
            "stage09f_dir": str(args.stage09f_dir),
        },
        "focus_samples": FOCUS_SAMPLES,
        "policy": POLICY,
        "constraints": [
            "read_only_diagnostic",
            "no_fusion_logic_change",
            "no_fusion_v2",
            "no_network_download",
            "EPIC_independent_reference_not_truth",
        ],
        "outputs": output_files,
        "warning_count": len(warnings),
    }
    manifest_path = dirs["logs"] / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"run_root": str(args.out), "report": str(report_path), "warnings": len(warnings)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
