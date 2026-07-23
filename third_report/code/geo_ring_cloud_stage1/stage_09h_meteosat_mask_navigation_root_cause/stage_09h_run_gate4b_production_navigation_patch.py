# -*- coding: utf-8 -*-
"""Stage 09H Gate 4B production Meteosat navigation patch validation.

This gate validates the production reader patch and immediately performs the
minimal EPIC-view downstream recovery check. The cloud_mask array must remain
identity relative to the legacy raw cfgrib values.
"""

from __future__ import annotations

import gc
import hashlib
import json
import math
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pyproj import Geod
from scipy.ndimage import distance_transform_edt

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from geo_ring_cloud import paths as path_config  # noqa: E402
from geo_ring_cloud.adapters import cloud_products  # noqa: E402
from geo_ring_cloud.adapters.cloud_products import reshape_square_if_needed  # noqa: E402
from geo_ring_cloud.adapters.meteosat_native_navigation import (  # noqa: E402
    METEOSAT_0DEG_CLM_GRID_SPEC,
    NAVIGATION_SCHEMA_VERSION,
    build_meteosat_0deg_clm_raw_navigation,
    grid_spec_sha256,
    matches_meteosat_0deg_clm_scope,
)
from geo_ring_cloud.cloud_semantics import cloud_mask_masks  # noqa: E402
from geo_ring_cloud.diagnostics import full_pixel  # noqa: E402
from geo_ring_cloud.diagnostics.full_pixel_workflow import load_manifest  # noqa: E402
from geo_ring_cloud.reprojection import build_tree, normalize_longitude, query_reproject  # noqa: E402

PROJECT_ID = "geo_ring_cloud"
STAGE_ID = "stage_09h"
GATE_ID = "gate4b_production_navigation_patch"
RUN_ID = "stage_09h_meteosat_mask_navigation_root_cause_202403"
SOURCE = "Meteosat-0deg"
PRODUCT = "CLM"
POLICY_NAME = "A_inclusive_binary"
OUT_ROOT = path_config.RUNS_ROOT / RUN_ID / GATE_ID
CLM_ROOT = path_config.EXTERNAL_GEO_CLOUD_ROOT / SOURCE / "CLM" / "20240312"
STAGE09D_DIR = path_config.RUNS_ROOT / "stage09d_full_pixel_diagnostics_202403"
GATE4A_SOURCE = path_config.RUNS_ROOT / RUN_ID / "source_data"
GEOD = Geod(a=6378169.0, rf=295.488065897014)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_dirs(root: Path) -> dict[str, Path]:
    dirs = {
        "source_data": root / "source_data",
        "reports": root / "reports",
        "logs": root / "logs",
        "cache": root / "cache",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def write_csv(rows: list[dict[str, Any]] | pd.DataFrame, path: Path) -> Path:
    df = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def write_json(obj: Any, path: Path) -> Path:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str), encoding="utf-8-sig")
    return path


def warn(warnings: list[dict[str, Any]], code: str, message: str, severity: str = "WARN", **extra: Any) -> None:
    row = {
        "timestamp_utc": utc_now(),
        "stage_id": STAGE_ID,
        "gate_id": GATE_ID,
        "severity": severity,
        "warning_code": code,
        "message": message,
    }
    row.update(extra)
    warnings.append(row)


def find_clm_zip(case_id: str) -> Path:
    date, hour = case_id.split("_")
    path = CLM_ROOT / hour[:2] / f"MSG3-SEVI-MSGCLMK-0100-0100-{date}{hour[:2]}0000.000000000Z-NA.zip"
    if path.exists():
        return path
    hits = sorted(CLM_ROOT.rglob(f"*{date}{hour[:2]}0000*.zip"))
    if hits:
        return hits[0]
    raise FileNotFoundError(f"missing Meteosat-0deg CLM ZIP for {case_id}: {path}")


def configure_eccodes_library(warnings: list[dict[str, Any]]) -> None:
    if sys.platform != "win32":
        return
    dll = Path(sys.prefix) / "Library" / "bin" / "eccodes.dll"
    if not dll.exists():
        warn(warnings, "eccodes_dll_missing", f"ecCodes DLL not found at {dll}")
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
        warn(warnings, "eccodes_findlibs_patch_failed", str(exc))


def read_legacy_clm_zip(path: Path, cache_dir: Path, warnings: list[dict[str, Any]]) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    configure_eccodes_library(warnings)
    import xarray as xr

    meta: dict[str, Any] = {"source_path": str(path), "reader": "legacy_zip_cfgrib_raw_values+C_order_reshape"}
    with zipfile.ZipFile(path) as zf:
        grib_entries = [entry for entry in zf.namelist() if entry.lower().endswith((".grb", ".grib", ".grb2", ".bin"))]
        if not grib_entries:
            raise RuntimeError(f"No GRIB entry found in {path}")
        entry = grib_entries[0]
        payload = zf.read(entry)
    cache_key = hashlib.sha1(f"{path.resolve()}|{entry}".encode("utf-8")).hexdigest()
    extracted = cache_dir / f"{cache_key}{Path(entry).suffix or '.grb'}"
    if not extracted.exists() or extracted.stat().st_size != len(payload):
        extracted.write_bytes(payload)
    ds = xr.open_dataset(extracted, engine="cfgrib", backend_kwargs={"indexpath": ""})
    try:
        var_name = "p260537" if "p260537" in ds.data_vars else list(ds.data_vars)[0]
        mask = reshape_square_if_needed(np.asarray(ds[var_name].values))
        lat = reshape_square_if_needed(np.asarray(ds["latitude"].values, dtype=np.float32))
        lon = reshape_square_if_needed(np.asarray(ds["longitude"].values, dtype=np.float32))
        meta.update(
            {
                "selected_grib_entry": entry,
                "selected_grib_cache": str(extracted),
                "raw_variable_name": var_name,
                "mask_shape": str(mask.shape),
                "latitude_shape": str(lat.shape),
                "longitude_shape": str(lon.shape),
                "dataset_attrs_json": json.dumps({k: str(v) for k, v in ds.attrs.items()}, ensure_ascii=False),
            }
        )
    finally:
        ds.close()
    return {"cloud_mask": mask, "latitude": lat, "longitude": lon}, meta


def read_patched_product(path: Path) -> tuple[dict[str, np.ndarray], dict[str, Any], list[str], dict[str, str]]:
    mapping = cloud_products.read_mapping()
    result = cloud_products.read_product(path, "Meteosat", "CLM", mapping)
    return result.arrays, result.attrs, result.warnings, result.source_variables


def array_sha256(arr: np.ndarray) -> str:
    a = np.ascontiguousarray(np.asarray(arr))
    return hashlib.sha256(a.view(np.uint8)).hexdigest()


def circular_lon_diff_deg(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return ((np.asarray(a, dtype=np.float32) - np.asarray(b, dtype=np.float32) + 180.0) % 360.0) - 180.0


def geodesic_distance_km(lon_a: np.ndarray, lat_a: np.ndarray, lon_b: np.ndarray, lat_b: np.ndarray, chunk: int = 1_000_000) -> np.ndarray:
    n = lon_a.size
    out = np.empty(n, dtype=np.float32)
    for start in range(0, n, chunk):
        stop = min(start + chunk, n)
        _, _, dist_m = GEOD.inv(
            lon_a[start:stop].astype(np.float64),
            lat_a[start:stop].astype(np.float64),
            lon_b[start:stop].astype(np.float64),
            lat_b[start:stop].astype(np.float64),
        )
        out[start:stop] = (dist_m / 1000.0).astype(np.float32)
    return out


def distance_summary(label: str, candidate_lat: np.ndarray, candidate_lon: np.ndarray, ref_lat: np.ndarray, ref_lon: np.ndarray, valid: np.ndarray) -> dict[str, Any]:
    mask = valid & np.isfinite(candidate_lat) & np.isfinite(candidate_lon) & np.isfinite(ref_lat) & np.isfinite(ref_lon)
    dist = geodesic_distance_km(
        normalize_longitude(candidate_lon)[mask].ravel(),
        candidate_lat[mask].ravel(),
        normalize_longitude(ref_lon)[mask].ravel(),
        ref_lat[mask].ravel(),
    )
    return {
        "comparison": label,
        "n_valid": int(dist.size),
        "latitude_mae_deg": float(np.nanmean(np.abs(candidate_lat[mask] - ref_lat[mask]))),
        "longitude_circular_mae_deg": float(np.nanmean(np.abs(circular_lon_diff_deg(candidate_lon[mask], ref_lon[mask])))),
        "median_geodesic_error_km": float(np.nanmedian(dist)),
        "p95_geodesic_error_km": float(np.nanpercentile(dist, 95)),
        "within_1km_fraction": float(np.mean(dist <= 1.0)),
        "within_5km_fraction": float(np.mean(dist <= 5.0)),
        "within_10km_fraction": float(np.mean(dist <= 10.0)),
    }


def control_point_rows(case_id: str, patched_lat: np.ndarray, patched_lon: np.ndarray, legacy_lat: np.ndarray, legacy_lon: np.ndarray) -> list[dict[str, Any]]:
    cp = pd.read_csv(GATE4A_SOURCE / "stage_09h_gate3b_navigation_control_points.csv")
    cp = cp[cp["case_id"] == case_id]
    rows: list[dict[str, Any]] = []
    for rec in cp.to_dict("records"):
        r = int(rec["native_row"])
        c = int(rec["native_col"])
        ref_lat = float(rec["reference_lat_deg"])
        ref_lon = float(rec["reference_lon_deg"])
        _, _, pdist_m = GEOD.inv(float(patched_lon[r, c]), float(patched_lat[r, c]), ref_lon, ref_lat)
        _, _, ldist_m = GEOD.inv(float(legacy_lon[r, c]), float(legacy_lat[r, c]), ref_lon, ref_lat)
        rows.append(
            {
                "case_id": case_id,
                "control_point": rec["control_point"],
                "native_row": r,
                "native_col": c,
                "reference_lat_deg": ref_lat,
                "reference_lon_deg": ref_lon,
                "patched_lat_deg": float(patched_lat[r, c]),
                "patched_lon_deg": float(patched_lon[r, c]),
                "legacy_lat_deg": float(legacy_lat[r, c]),
                "legacy_lon_deg": float(legacy_lon[r, c]),
                "patched_error_km": float(pdist_m / 1000.0),
                "legacy_error_km": float(ldist_m / 1000.0),
            }
        )
    return rows


def target_lon_lat(grid: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    lon = np.linspace(float(grid["lon_centers_first_last"][0]), float(grid["lon_centers_first_last"][1]), int(grid["lon_size"]), dtype=np.float64)
    lat = np.linspace(float(grid["lat_centers_first_last"][0]), float(grid["lat_centers_first_last"][1]), int(grid["lat_size"]), dtype=np.float64)
    return lon, lat


def reproject_mask(mask: np.ndarray, lat: np.ndarray, lon: np.ndarray, grid: dict[str, Any]) -> tuple[np.ndarray, np.ndarray, int]:
    _, fusion_valid, _ = cloud_mask_masks(SOURCE, PRODUCT, mask)
    lon_norm = normalize_longitude(lon)
    finite_nav = np.isfinite(lat) & np.isfinite(lon_norm) & (lat >= -90.0) & (lat <= 90.0)
    zero_fill = np.isclose(lat, 0.0) & np.isclose(lon, 0.0)
    source_valid = fusion_valid & finite_nav & (~zero_fill)
    tree, src_y, src_x, _ = build_tree(lon_norm, lat.astype(np.float32), source_valid)
    target_lon, target_lat = target_lon_lat(grid)
    data_grid, valid_grid_u8 = query_reproject(
        tree,
        src_y,
        src_x,
        mask.astype(np.int16, copy=False),
        target_lon,
        target_lat,
        np.dtype(np.int16),
        -9999,
    )
    return data_grid, valid_grid_u8.astype(bool), int(np.count_nonzero(source_valid))


def shifted_after_rot180(arr: np.ndarray, dr: int, dc: int, fill: float = np.nan) -> np.ndarray:
    rot = np.asarray(arr)[::-1, ::-1]
    out = np.full(rot.shape, fill, dtype=rot.dtype)
    nrow, ncol = rot.shape
    src_r0 = max(0, dr)
    src_r1 = min(nrow, nrow + dr)
    dst_r0 = max(0, -dr)
    dst_r1 = dst_r0 + max(0, src_r1 - src_r0)
    src_c0 = max(0, dc)
    src_c1 = min(ncol, ncol + dc)
    dst_c0 = max(0, -dc)
    dst_c1 = dst_c0 + max(0, src_c1 - src_c0)
    if src_r1 > src_r0 and src_c1 > src_c0:
        out[dst_r0:dst_r1, dst_c0:dst_c1] = rot[src_r0:src_r1, src_c0:src_c1]
    return out


def confusion_metrics(epic_cls: np.ndarray, geo_cls: np.ndarray, valid: np.ndarray, positive: int = 1) -> dict[str, Any]:
    n_valid = int(np.count_nonzero(valid))
    if n_valid == 0:
        return {"n_valid": 0, "agreement": math.nan, "balanced_accuracy": math.nan, "f1_cloud": math.nan, "iou_cloud": math.nan, "mcc": math.nan, "TP": 0, "TN": 0, "FP": 0, "FN": 0}
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
    specificity = tn / max(tn + fp, 1)
    denom = math.sqrt(max((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn), 0))
    return {
        "n_valid": n_valid,
        "agreement": float(np.mean(e == g)),
        "precision_cloud": precision,
        "recall_cloud": recall,
        "specificity_clear": specificity,
        "balanced_accuracy": (recall + specificity) / 2.0,
        "f1_cloud": 2.0 * precision * recall / max(precision + recall, 1e-12),
        "iou_cloud": tp / max(tp + fp + fn, 1),
        "mcc": ((tp * tn) - (fp * fn)) / denom if denom else math.nan,
        "TP": tp,
        "TN": tn,
        "FP": fp,
        "FN": fn,
        "cloud_fraction_epic": float(np.mean(ep)),
        "cloud_fraction_geo": float(np.mean(gp)),
        "cloud_fraction_bias_geo_minus_epic": float(np.mean(gp) - np.mean(ep)),
    }


def boundary_mask(cls: np.ndarray, valid: np.ndarray, positive: int = 1) -> np.ndarray:
    cloud = np.asarray(cls) == positive
    v = np.asarray(valid, dtype=bool)
    padded_cloud = np.pad(cloud.astype(np.int8), 1, mode="edge")
    padded_valid = np.pad(v.astype(np.int8), 1, mode="constant")
    count = np.zeros(cloud.shape, dtype=np.int16)
    valid_count = np.zeros(cloud.shape, dtype=np.int16)
    for dy in range(3):
        for dx in range(3):
            count += padded_cloud[dy : dy + cloud.shape[0], dx : dx + cloud.shape[1]]
            valid_count += padded_valid[dy : dy + cloud.shape[0], dx : dx + cloud.shape[1]]
    return v & (valid_count >= 5) & (count > 0) & (count < valid_count)


def boundary_metrics(epic_cls: np.ndarray, geo_cls: np.ndarray, valid: np.ndarray, positive: int = 1) -> dict[str, Any]:
    if int(np.count_nonzero(valid)) == 0:
        return {"boundary_f1": math.nan, "boundary_precision": math.nan, "boundary_recall": math.nan, "boundary_chamfer_px": math.nan, "n_boundary_epic": 0, "n_boundary_geo": 0}
    eb = boundary_mask(epic_cls, valid, positive)
    gb = boundary_mask(geo_cls, valid, positive)
    tp = int(np.count_nonzero(eb & gb))
    precision = tp / max(int(np.count_nonzero(gb)), 1)
    recall = tp / max(int(np.count_nonzero(eb)), 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-12)
    if np.any(eb) and np.any(gb):
        dist_to_geo = distance_transform_edt(~gb)
        dist_to_epic = distance_transform_edt(~eb)
        chamfer = (float(np.mean(dist_to_geo[eb])) + float(np.mean(dist_to_epic[gb]))) / 2.0
    else:
        chamfer = math.nan
    return {
        "boundary_f1": f1,
        "boundary_precision": precision,
        "boundary_recall": recall,
        "boundary_chamfer_px": chamfer,
        "n_boundary_epic": int(np.count_nonzero(eb)),
        "n_boundary_geo": int(np.count_nonzero(gb)),
    }


def evaluate_epic_case(
    case_id: str,
    mask: np.ndarray,
    legacy_lat: np.ndarray,
    legacy_lon: np.ndarray,
    patched_lat: np.ndarray,
    patched_lon: np.ndarray,
    warnings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    manifest = pd.DataFrame(load_manifest(STAGE09D_DIR))
    hit = manifest[manifest["sample_id"] == case_id]
    if hit.empty:
        warn(warnings, "epic_case_missing", f"{case_id} not present in Stage 09D manifest; EPIC comparison skipped", case_id=case_id)
        return [], []
    row = hit.iloc[0].to_dict()
    ctx = full_pixel.sample_context(row)
    policy = full_pixel.POLICIES[POLICY_NAME]
    epic_cls, epic_policy_valid = full_pixel.apply_policy(ctx["epic"]["cloud_mask"], policy["epic"])
    valid_earth = np.isin(ctx["epic"]["cloud_mask"], [1, 2, 3, 4])
    best_shift = pd.read_csv(GATE4A_SOURCE / "stage_09h_gate4a_exact_navigation_mapping.csv")
    best_hit = best_shift[(best_shift["case_id"] == case_id) & (best_shift["mapping_role"] == "best")]
    dr = int(best_hit.iloc[0]["dr_after_rot180"]) if not best_hit.empty else 1
    dc = int(best_hit.iloc[0]["dc_after_rot180"]) if not best_hit.empty else 1
    variants = {
        "A_legacy_current_navigation": (legacy_lat, legacy_lon, "legacy current cfgrib latitude/longitude"),
        "internal_simple_rot180_navigation": (legacy_lat[::-1, ::-1], legacy_lon[::-1, ::-1], "internal common-domain guard: legacy lat/lon rot180"),
        "internal_rot180_best_shift_navigation": (
            shifted_after_rot180(legacy_lat, dr, dc),
            shifted_after_rot180(legacy_lon, dr, dc),
            f"internal common-domain guard: legacy lat/lon rot180 plus dr={dr}, dc={dc}",
        ),
        "B_patched_production_navigation": (patched_lat, patched_lon, "patched production verified SEVIRI native-area navigation"),
    }
    sampled: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    for variant, (lat, lon, note) in variants.items():
        grid_data, grid_valid, source_valid_count = reproject_mask(mask, lat, lon, ctx["grid"])
        raw_on_epic, raw_valid_on_epic = full_pixel.sample_grid(grid_data, grid_valid, ctx["epic"]["lat"], ctx["epic"]["lon"], ctx["grid"])
        standard = full_pixel.source_to_standard(SOURCE, raw_on_epic)
        geo_cls, geo_policy_valid = full_pixel.apply_policy(standard, policy["geo"])
        valid = valid_earth & epic_policy_valid & raw_valid_on_epic & geo_policy_valid
        sampled[variant] = {
            "geo_cls": geo_cls,
            "valid": valid,
            "source_valid_count": source_valid_count,
            "epic_source_valid_before_policy": int(np.count_nonzero(raw_valid_on_epic & valid_earth)),
            "note": note,
        }
        del grid_data, grid_valid, raw_on_epic, raw_valid_on_epic, standard, geo_policy_valid
        gc.collect()
    common = valid_earth & epic_policy_valid
    for item in sampled.values():
        common &= np.asarray(item["valid"], dtype=bool)
    epic_boundary = boundary_mask(epic_cls, common, policy["positive"])
    inner = common & np.isfinite(ctx["epic"].get("epic_vza")) & (ctx["epic"]["epic_vza"] <= 60.0)
    limb = common & np.isfinite(ctx["epic"].get("epic_vza")) & (ctx["epic"]["epic_vza"] > 60.0)
    strata = {
        "common_valid_all_candidates": common,
        "boundary_on_common_valid": common & epic_boundary,
        "nonboundary_on_common_valid": common & (~epic_boundary),
        "inner_disk_vza_le_60_on_common_valid": inner,
        "limb_vza_gt_60_on_common_valid": limb,
    }
    for variant, item in sampled.items():
        if variant.startswith("internal_"):
            continue
        geo_cls = np.asarray(item["geo_cls"])
        for domain, domain_valid in strata.items():
            metrics = confusion_metrics(epic_cls, geo_cls, domain_valid, positive=policy["positive"])
            metrics.update(boundary_metrics(epic_cls, geo_cls, domain_valid, positive=policy["positive"]))
            metrics.update(
                {
                    "case_id": case_id,
                    "candidate_variant": variant,
                    "comparison_domain": domain,
                    "policy": POLICY_NAME,
                    "source": SOURCE,
                    "navigation_note": item["note"],
                    "cloud_mask_transform": "identity_cloud_mask_not_rotated",
                    "source_valid_count_before_reprojection": int(item["source_valid_count"]),
                    "epic_source_valid_before_policy": int(item["epic_source_valid_before_policy"]),
                    "stage09d_epic_time_utc": row.get("epic_time_utc", ""),
                    "stage_run_dir": row.get("stage_run_dir", ""),
                }
            )
            rows.append(metrics)
    baseline_rows: list[dict[str, Any]] = []
    baseline_path = GATE4A_SOURCE / "stage_09h_gate4a_candidate_fix_comparison.csv"
    if baseline_path.exists():
        baseline = pd.read_csv(baseline_path)
        bhit = baseline[
            (baseline["case_id"] == case_id)
            & (baseline["candidate_variant"] == "D_official_area_derived_navigation")
            & (baseline["comparison_domain"] == "common_valid_all_candidates")
        ]
        for rec in bhit.to_dict("records"):
            rec["candidate_variant"] = "C_gate4a_recorded_official_area_baseline"
            rec["navigation_note"] = "recorded Gate 4A official-area baseline; EPIC is downstream recovery evidence"
            baseline_rows.append(rec)
    return rows, baseline_rows


def gate4a_reproduction_rows(epic_rows: list[dict[str, Any]], baseline_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    baseline_by_case = {row["case_id"]: row for row in baseline_rows if row.get("comparison_domain") == "common_valid_all_candidates"}
    for row in epic_rows:
        if row.get("candidate_variant") != "B_patched_production_navigation" or row.get("comparison_domain") != "common_valid_all_candidates":
            continue
        base = baseline_by_case.get(row["case_id"])
        if not base:
            rows.append({"case_id": row["case_id"], "status": "SKIPPED_NO_GATE4A_BASELINE"})
            continue
        agreement_delta = abs(float(row["agreement"]) - float(base["agreement"]))
        mcc_delta = abs(float(row["mcc"]) - float(base["mcc"]))
        agreement_tolerance = 1e-5
        mcc_tolerance = 1e-5
        rows.append(
            {
                "case_id": row["case_id"],
                "patched_agreement": float(row["agreement"]),
                "gate4a_agreement": float(base["agreement"]),
                "agreement_abs_delta": agreement_delta,
                "patched_mcc": float(row["mcc"]),
                "gate4a_mcc": float(base["mcc"]),
                "mcc_abs_delta": mcc_delta,
                "agreement_tolerance": agreement_tolerance,
                "mcc_tolerance": mcc_tolerance,
                "status": "PASS" if agreement_delta <= agreement_tolerance and mcc_delta <= mcc_tolerance else "FAIL",
            }
        )
    return rows


def status_row(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"status_name": name, "status": "PASS" if passed else "FAIL", "detail": detail}


def dataframe_to_markdown(df: pd.DataFrame, floatfmt: str = ".6f") -> str:
    if df.empty:
        return ""
    cols = [str(col) for col in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for _, row in df.iterrows():
        cells: list[str] = []
        for col in df.columns:
            value = row[col]
            if isinstance(value, (float, np.floating)):
                cells.append("" if not np.isfinite(value) else format(float(value), floatfmt))
            else:
                cells.append(str(value))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def write_report(path: Path, statuses: list[dict[str, Any]], reproduction: list[dict[str, Any]], cases: list[str]) -> None:
    final = "PRODUCTION_NAVIGATION_PATCH_VALIDATED" if all(row["status"] == "PASS" for row in statuses) else "PRODUCTION_NAVIGATION_PATCH_NOT_VALIDATED"
    lines = [
        "# Stage 09H Gate 4B Production Navigation Patch Report",
        "",
        f"- Generated UTC: `{utc_now()}`",
        f"- Cases: `{', '.join(cases)}`",
        f"- Final status: `{final}`",
        "",
        "## Core Rule",
        "",
        "- `cloud_mask` remains identity relative to legacy raw cfgrib values.",
        "- `latitude/longitude` are replaced only for audited Meteosat-0deg CLM MSG3 3712x3712 files.",
        "- EPIC comparison is downstream recovery evidence; native navigation truth is anchored to Gate 4A SEVIRI area controls.",
        "",
        "## Gate Statuses",
        "",
        dataframe_to_markdown(pd.DataFrame(statuses)),
        "",
        "## Gate 4A Reproduction",
        "",
        dataframe_to_markdown(pd.DataFrame(reproduction)) if reproduction else "No Gate 4A baseline rows available.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8-sig")


def main() -> int:
    dirs = ensure_dirs(OUT_ROOT)
    warnings: list[dict[str, Any]] = []
    case_candidates = ["20240312_1500", "20240312_1200"]
    manifest = pd.DataFrame(load_manifest(STAGE09D_DIR))
    cases = [case for case in case_candidates if case in set(manifest["sample_id"].astype(str))]
    if "20240312_1500" not in cases:
        raise RuntimeError("required EPIC case 20240312_1500 is not available in Stage 09D manifest")
    if "20240312_1200" not in cases:
        warn(warnings, "optional_epic_case_unavailable", "20240312_1200 not present in Stage 09D manifest; skipped")

    mask_rows: list[dict[str, Any]] = []
    nav_metric_rows: list[dict[str, Any]] = []
    control_rows: list[dict[str, Any]] = []
    scope_rows: list[dict[str, Any]] = []
    epic_rows: list[dict[str, Any]] = []
    baseline_rows: list[dict[str, Any]] = []
    cache_rows: list[dict[str, Any]] = []

    for case_id in cases:
        zip_path = find_clm_zip(case_id)
        legacy, legacy_meta = read_legacy_clm_zip(zip_path, dirs["cache"], warnings)
        patched, patched_attrs, patched_warnings, source_vars = read_patched_product(zip_path)
        for message in patched_warnings:
            if "applied" not in str(message):
                warn(warnings, "production_reader_warning", str(message), case_id=case_id)
        mask_equal = np.array_equal(np.asarray(patched["cloud_mask"]), np.asarray(legacy["cloud_mask"]))
        hash_equal = array_sha256(patched["cloud_mask"]) == array_sha256(legacy["cloud_mask"])
        mask_rows.append(
            {
                "case_id": case_id,
                "source_file": str(zip_path),
                "mask_shape": str(np.asarray(patched["cloud_mask"]).shape),
                "legacy_mask_hash": array_sha256(legacy["cloud_mask"]),
                "patched_mask_hash": array_sha256(patched["cloud_mask"]),
                "array_equal": bool(mask_equal),
                "hash_equal": bool(hash_equal),
                "mask_transform": patched_attrs.get("mask_transform", ""),
                "status": "PASS" if mask_equal and hash_equal and patched_attrs.get("mask_transform") == "identity" else "FAIL",
            }
        )
        scope_ok = matches_meteosat_0deg_clm_scope(zip_path, "CLM", tuple(np.asarray(patched["cloud_mask"]).shape), patched_attrs.get("cfgrib_attrs", {}))
        scope_rows.append(
            {
                "case_id": case_id,
                "source_file": str(zip_path),
                "scope_guard_expected": True,
                "scope_guard_result": bool(scope_ok),
                "navigation_schema_version": patched_attrs.get("navigation_schema_version", ""),
                "source_variable_latitude": source_vars.get("latitude", ""),
                "source_variable_longitude": source_vars.get("longitude", ""),
                "status": "PASS" if scope_ok and patched_attrs.get("navigation_schema_version") == NAVIGATION_SCHEMA_VERSION else "FAIL",
            }
        )
        ref_lat, ref_lon, ref_meta = build_meteosat_0deg_clm_raw_navigation(tuple(np.asarray(patched["cloud_mask"]).shape))
        ref_valid = np.isfinite(ref_lat) & np.isfinite(ref_lon)
        nav_metric_rows.append(distance_summary(f"{case_id}:patched_vs_gate4a_grid_spec", patched["latitude"], patched["longitude"], ref_lat, ref_lon, ref_valid))
        nav_metric_rows[-1]["case_id"] = case_id
        nav_metric_rows.append(distance_summary(f"{case_id}:legacy_vs_gate4a_grid_spec", legacy["latitude"], legacy["longitude"], ref_lat, ref_lon, ref_valid))
        nav_metric_rows[-1]["case_id"] = case_id
        control_rows.extend(control_point_rows(case_id, patched["latitude"], patched["longitude"], legacy["latitude"], legacy["longitude"]))
        e_rows, b_rows = evaluate_epic_case(case_id, patched["cloud_mask"], legacy["latitude"], legacy["longitude"], patched["latitude"], patched["longitude"], warnings)
        epic_rows.extend(e_rows)
        baseline_rows.extend(b_rows)
        cache_rows.append(
            {
                "case_id": case_id,
                "navigation_schema_version": patched_attrs.get("navigation_schema_version", ""),
                "navigation_grid_spec_sha256": patched_attrs.get("navigation_grid_spec_sha256", ""),
                "expected_grid_spec_sha256": grid_spec_sha256(METEOSAT_0DEG_CLM_GRID_SPEC),
                "cache_invalidation_required_for_old_schema": True,
                "status": "PASS" if patched_attrs.get("navigation_grid_spec_sha256") == grid_spec_sha256(METEOSAT_0DEG_CLM_GRID_SPEC) else "FAIL",
            }
        )
        del legacy, patched, ref_lat, ref_lon
        gc.collect()

    reproduction = gate4a_reproduction_rows(epic_rows, baseline_rows)
    cp_df = pd.DataFrame(control_rows)
    nav_df = pd.DataFrame(nav_metric_rows)
    statuses = [
        status_row("MASK_PRESERVATION", all(row["status"] == "PASS" for row in mask_rows), "patched cloud_mask equals legacy raw cfgrib values byte-for-byte"),
        status_row("SCOPE_GUARD", all(row["status"] == "PASS" for row in scope_rows), "patch applies only to audited Meteosat-0deg CLM files"),
        status_row(
            "NATIVE_NAVIGATION_REFERENCE",
            (not cp_df.empty)
            and float(cp_df["patched_error_km"].max()) <= 0.01
            and float(cp_df["legacy_error_km"].median()) >= 1000.0,
            "patched control points match frozen Gate 3B/4A references; legacy remains a negative control",
        ),
        status_row(
            "LEGACY_NAV_NEGATIVE_TEST",
            (not nav_df.empty)
            and float(nav_df[nav_df["comparison"].str.contains("legacy")]["median_geodesic_error_km"].max()) >= 1000.0,
            "legacy cfgrib identity navigation is still grossly inconsistent with raw-storage reference",
        ),
        status_row("DETERMINISTIC_OUTPUT", all(row["status"] == "PASS" for row in cache_rows), "grid spec hash is stable and embedded"),
        status_row("CACHE_INVALIDATION", all(row["status"] == "PASS" for row in cache_rows), f"new schema version {NAVIGATION_SCHEMA_VERSION} is present for cache keys"),
        status_row(
            "EPIC_DOWNSTREAM_RECOVERY",
            bool(reproduction) and all(row["status"] == "PASS" for row in reproduction),
            "patched production EPIC metrics reproduce Gate 4A official-area baseline",
        ),
    ]

    final_status = "PRODUCTION_NAVIGATION_PATCH_VALIDATED" if all(row["status"] == "PASS" for row in statuses) else "PRODUCTION_NAVIGATION_PATCH_NOT_VALIDATED"
    write_csv(mask_rows, dirs["source_data"] / "mask_preservation_test.csv")
    write_csv(scope_rows, dirs["source_data"] / "patch_scope_registry.csv")
    write_csv(nav_metric_rows, dirs["source_data"] / "navigation_reference_metrics.csv")
    write_csv(control_rows, dirs["source_data"] / "navigation_control_points.csv")
    write_csv(epic_rows + baseline_rows, dirs["source_data"] / "downstream_before_after.csv")
    write_csv(reproduction, dirs["source_data"] / "gate4a_reproduction_check.csv")
    write_csv(cache_rows, dirs["source_data"] / "cache_invalidation_inventory.csv")
    write_csv(statuses, dirs["source_data"] / "gate4b_final_status.csv")
    write_csv(warnings, dirs["logs"] / "warnings.csv")
    manifest_obj = {
        "project_id": PROJECT_ID,
        "canonical_stage_id": STAGE_ID,
        "gate_id": GATE_ID,
        "generated_utc": utc_now(),
        "cases": cases,
        "navigation_schema_version": NAVIGATION_SCHEMA_VERSION,
        "navigation_grid_spec_sha256": grid_spec_sha256(METEOSAT_0DEG_CLM_GRID_SPEC),
        "final_status": final_status,
        "statuses": statuses,
        "inputs": {
            "stage09d_manifest_dir": str(STAGE09D_DIR),
            "gate4a_source_data": str(GATE4A_SOURCE),
            "clm_root": str(CLM_ROOT),
        },
    }
    write_json(manifest_obj, dirs["logs"] / "manifest.json")
    write_report(dirs["reports"] / "production_navigation_patch_report_cn.md", statuses, reproduction, cases)
    print(json.dumps({"status": final_status, "cases": cases, "output_root": str(OUT_ROOT)}, ensure_ascii=False))
    return 0 if final_status == "PRODUCTION_NAVIGATION_PATCH_VALIDATED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
