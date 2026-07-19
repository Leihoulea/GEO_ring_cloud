from __future__ import annotations

import argparse
import csv
import ctypes
import json
import math
import os
import re
import sys
import traceback
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

for _thread_env in ["OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"]:
    os.environ.setdefault(_thread_env, "1")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from geo_ring_cloud.lineage import utc_now  # noqa: E402
from geo_ring_cloud.paths import RUNS_ROOT  # noqa: E402
from geo_ring_cloud.diagnostics.full_pixel import (  # noqa: E402
    POLICIES,
    SOURCES,
    SOURCE_ID,
    SOURCE_PAIR_LIST,
    binary_metrics,
    classify_array,
    find_prefusion,
    load_grid,
    load_npz,
    make_boundary,
    read_epic,
    row_col,
    sample_context,
    sample_grid,
    source_to_standard,
    apply_policy,
)


STAGE_ID = "stage_09d"
DEFAULT_09C_DIR = RUNS_ROOT / "stage09c_scaled_202403_batch"
DEFAULT_OUT_DIR = RUNS_ROOT / "stage09d_full_pixel_diagnostics_202403"
INV_09B = RUNS_ROOT / "stage09b_full_202403_overnight_diagnostics" / "01_inventory_expansion" / "stage09b_full_candidate_inventory_202403.csv"


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        keys: set[str] = set()
        for row in rows:
            keys.update(row.keys())
        fields = sorted(keys)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})


def append_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})


def safe_float(v: Any, default: float = math.nan) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def ensure_dirs(out: Path) -> None:
    for name in ["00_sample_manifest", "01_source_pair_recompute", "02_sampling_sensitivity", "03_geometry_stratification", "04_boundary_broken_cloud", "05_error_atlas", "06_integrated_factor_summary", "07_summary_figures", "reports", "logs"]:
        (out / name).mkdir(parents=True, exist_ok=True)


def nanmean_mask(arr: np.ndarray, valid: np.ndarray) -> float:
    if not np.any(valid):
        return math.nan
    x = arr[valid]
    return float(np.nanmean(x)) if np.any(np.isfinite(x)) else math.nan


def bin_value(value: float, bins: list[tuple[str, float, float]]) -> str:
    if not math.isfinite(value):
        return "missing"
    for label, lo, hi in bins:
        if lo <= value < hi:
            return label
    return bins[-1][0]


def integral_sum(mask: np.ndarray) -> np.ndarray:
    return np.pad(mask.astype(np.float32).cumsum(axis=0).cumsum(axis=1), ((1, 0), (1, 0)), mode="constant")


def window_counts(ii: np.ndarray, r: np.ndarray, c: np.ndarray, ok: np.ndarray, radius: int, shape: tuple[int, int]) -> np.ndarray:
    y0 = np.clip(r - radius, 0, shape[0] - 1)
    y1 = np.clip(r + radius, 0, shape[0] - 1)
    x0 = np.clip(c - radius, 0, shape[1] - 1)
    x1 = np.clip(c + radius, 0, shape[1] - 1)
    out = np.zeros(r.shape, dtype=np.float32)
    m = ok
    out[m] = ii[y1[m] + 1, x1[m] + 1] - ii[y0[m], x1[m] + 1] - ii[y1[m] + 1, x0[m]] + ii[y0[m], x0[m]]
    return out


def setup_fields() -> dict[str, list[str]]:
    return {
        "source": ["sample_id", "policy", "source_name", "n_valid", "agreement", "precision_cloud", "recall_cloud", "f1_cloud", "iou_cloud", "balanced_accuracy", "cloud_fraction_epic", "cloud_fraction_source", "cloud_fraction_bias", "TP", "TN", "FP", "FN", "mean_abs_lat", "mean_epic_vza", "mean_sza"],
        "pair": ["sample_id", "policy", "source_A", "source_B", "n_overlap_valid", "A_agreement_to_epic", "B_agreement_to_epic", "B_minus_A_agreement", "A_f1", "B_f1", "A_iou", "B_iou", "A_cloud_fraction", "B_cloud_fraction", "EPIC_cloud_fraction", "A_cloud_fraction_bias", "B_cloud_fraction_bias", "A_only_correct_fraction", "B_only_correct_fraction", "both_correct_fraction", "both_wrong_fraction", "source_disagreement_fraction", "boundary_fraction", "mean_abs_lat", "mean_epic_vza", "mean_sza", "dominant_source", "candidate_group"],
        "sampling": ["sample_id", "policy", "sampling_method", "window_size", "threshold", "n_valid", "agreement", "f1_cloud", "iou_cloud", "balanced_accuracy", "cloud_fraction_epic", "cloud_fraction_sampled_georing", "cloud_fraction_bias", "delta_agreement_vs_nearest", "delta_f1_vs_nearest", "candidate_group", "dominant_source", "boundary_class"],
        "geometry": ["sample_id", "policy", "dimension", "bin", "n_valid", "agreement", "mismatch_rate", "pixel_fraction", "mismatch_fraction", "enrichment", "candidate_group", "dominant_source"],
        "boundary": ["sample_id", "policy", "scene_type", "boundary_class", "n_valid", "agreement", "mismatch_rate", "mismatch_enrichment", "cloud_fraction_epic", "cloud_fraction_georing", "dominant_source", "selected_source", "mean_abs_lat", "mean_epic_vza"],
    }


def build_lookup(stage09c_dir: Path) -> dict[str, dict[str, str]]:
    lookup: dict[str, dict[str, str]] = {}
    for r in read_csv(INV_09B):
        lookup[r["sample_id"]] = dict(r)
    for r in read_csv(stage09c_dir / "00_target_selection" / "stage09c_scaled_target_list.csv"):
        lookup.setdefault(r["sample_id"], {}).update({
            "epic_file": r.get("epic_file", ""),
            "epic_time_utc": r.get("epic_time_utc", ""),
            "nearest_georing_time_utc": r.get("nearest_georing_time_utc", ""),
            "candidate_group": r.get("candidate_group", ""),
            "estimated_dominant_source": r.get("estimated_dominant_source", ""),
            "time_diff_min": r.get("time_diff_min", ""),
        })
    return lookup


def find_any(run_dir: Path, pattern: str) -> Path | None:
    hits = list(run_dir.glob(pattern))
    return hits[0] if hits else None


def build_manifest(stage09c_dir: Path, out: Path) -> list[dict[str, Any]]:
    lookup = build_lookup(stage09c_dir)
    samples = read_csv(stage09c_dir / "02_expanded_diagnostics" / "stage09c_sample_level_semantic_summary.csv")
    rows: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for s in samples:
        tag = s["sample_id"]
        meta = lookup.get(tag, {})
        run_dir = RUNS_ROOT / tag
        epic_file = meta.get("epic_file", "")
        fused = run_dir / "fused_best_source" / "fused_cloud_mask.npz"
        source_map = run_dir / "fused_best_source" / "source_map_cloud_mask.npz"
        count_map = run_dir / "fused_best_source" / "valid_count_map_cloud_mask.npz"
        pref = {src: find_prefusion(run_dir, src, tag) for src in SOURCES}
        has_geo_vza = any(any((run_dir / "reprojected_grid" / src).glob(f"*sensor_zenith_angle*{tag}.npz")) for src in SOURCES)
        has_sza = any(any((run_dir / "reprojected_grid" / src).glob(f"*solar_zenith_angle*{tag}.npz")) for src in SOURCES)
        has_cth = any(any((run_dir / "reprojected_grid" / src).glob(f"*cloud_top_height_km*{tag}.npz")) for src in SOURCES)
        can_pair = all(pref.values()) and fused.exists() and bool(epic_file)
        can_sampling = fused.exists() and bool(epic_file)
        can_geometry = can_sampling
        can_boundary = can_sampling
        reasons = []
        if not epic_file:
            reasons.append("missing_epic_file_path")
        if not fused.exists():
            reasons.append("missing_fused_cloud_mask")
        if not all(pref.values()):
            reasons.append("missing_prefusion:" + "|".join(src for src, p in pref.items() if p is None))
        row = {
            "sample_id": tag,
            "epic_time_utc": meta.get("epic_time_utc") or meta.get("epic_time", ""),
            "nearest_georing_time_utc": meta.get("nearest_georing_time_utc") or meta.get("target_time", ""),
            "time_diff_min": meta.get("time_diff_min", ""),
            "candidate_group": meta.get("candidate_group", s.get("candidate_group", "")),
            "dominant_source": meta.get("estimated_dominant_source", s.get("dominant_source", "")),
            "stage_run_dir": str(run_dir),
            "epic_file": epic_file,
            "has_epic_file": bool(epic_file),
            "has_fused_cloud_mask": fused.exists(),
            "has_source_map": source_map.exists(),
            "has_valid_source_count": count_map.exists(),
            "has_prefusion_FY4B": pref["FY4B"] is not None,
            "has_prefusion_GOES16": pref["GOES-16"] is not None,
            "has_prefusion_GOES18": pref["GOES-18"] is not None,
            "has_prefusion_Himawari9": pref["Himawari-9"] is not None,
            "has_prefusion_Meteosat0deg": pref["Meteosat-0deg"] is not None,
            "has_prefusion_MeteosatIODC": pref["Meteosat-IODC"] is not None,
            "has_epic_vza": "unknown_until_read",
            "has_epic_sza": "unknown_until_read",
            "has_geo_vza": has_geo_vza,
            "has_geo_sza": has_sza,
            "has_cth": has_cth,
            "can_run_source_pair": can_pair,
            "can_run_sampling": can_sampling,
            "can_run_geometry": can_geometry,
            "can_run_boundary": can_boundary,
            "cannot_run_reason": ";".join(reasons),
        }
        rows.append(row)
        if reasons:
            missing.append({"sample_id": tag, "missing_or_warning": ";".join(reasons)})
    fields = ["sample_id", "epic_time_utc", "nearest_georing_time_utc", "time_diff_min", "candidate_group", "dominant_source", "stage_run_dir", "has_epic_file", "has_fused_cloud_mask", "has_source_map", "has_valid_source_count", "has_prefusion_FY4B", "has_prefusion_GOES16", "has_prefusion_GOES18", "has_prefusion_Himawari9", "has_prefusion_Meteosat0deg", "has_prefusion_MeteosatIODC", "has_epic_vza", "has_epic_sza", "has_geo_vza", "has_geo_sza", "has_cth", "can_run_source_pair", "can_run_sampling", "can_run_geometry", "can_run_boundary", "cannot_run_reason", "epic_file"]
    write_csv(out / "00_sample_manifest" / "stage09d_53_sample_manifest.csv", rows, fields)
    write_csv(out / "00_sample_manifest" / "stage09d_missing_inputs_report.csv", missing, ["sample_id", "missing_or_warning"])
    lines = ["# Stage 09D Sample Manifest", "", f"- Created UTC: `{utc_now()}`", f"- Samples: `{len(rows)}`", f"- Source-pair runnable: `{sum(str(r['can_run_source_pair']) == 'True' or r['can_run_source_pair'] is True for r in rows)}`", f"- Sampling runnable: `{sum(str(r['can_run_sampling']) == 'True' or r['can_run_sampling'] is True for r in rows)}`"]
    (out / "00_sample_manifest" / "stage09d_manifest_report.md").write_text("\n".join(lines), encoding="utf-8")
    return rows


def truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"true", "1", "yes", "y"}


def total_memory_gb() -> float:
    if os.name == "nt":
        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.ullTotalPhys / (1024 ** 3)
    return math.nan


def resolve_workers(args: argparse.Namespace) -> int:
    if args.workers and args.workers > 0:
        return args.workers
    cpu_workers = max(1, int(math.floor((os.cpu_count() or 2) * args.cpu_target)))
    mem_gb = total_memory_gb()
    if math.isfinite(mem_gb) and args.worker_memory_gb > 0:
        mem_workers = max(1, int(math.floor((mem_gb * args.memory_target) / args.worker_memory_gb)))
        return max(1, min(cpu_workers, mem_workers))
    return cpu_workers


def run_parallel(rows: list[dict[str, Any]], args: argparse.Namespace, worker_fn: Any, module: str) -> list[dict[str, Any]]:
    if not rows:
        return []
    workers = min(resolve_workers(args), len(rows))
    print(f"[stage09d] {module}: {len(rows)} samples, workers={workers}, cpu_target={args.cpu_target:.2f}, memory_target={args.memory_target:.2f}", flush=True)
    if workers <= 1:
        out = []
        for idx, row in enumerate(rows, 1):
            out.append(worker_fn(row))
            print(f"[stage09d] {module}: {idx}/{len(rows)} {row.get('sample_id')}", flush=True)
        return out
    out = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(worker_fn, row): row.get("sample_id") for row in rows}
        for idx, fut in enumerate(as_completed(futures), 1):
            tag = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:
                result = {"sample_id": tag, "warnings": [{"sample_id": tag, "module": module, "warning": str(exc), "traceback": traceback.format_exc()}]}
            out.append(result)
            print(f"[stage09d] {module}: {idx}/{len(rows)} {tag}", flush=True)
    return out


def compute_source_pair_for_sample(row: dict[str, Any]) -> dict[str, Any]:
    tag = row["sample_id"]
    try:
        ctx = sample_context(row)
        epic = ctx["epic"]
        valid_earth = np.isin(epic["cloud_mask"], [1, 2, 3, 4])
        source_samples: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        for source in SOURCES:
            p = find_prefusion(ctx["run_dir"], source, tag)
            if p is None:
                continue
            arrs = load_npz(p)
            raw_valid = np.asarray(arrs.get("fusion_valid_mask", arrs.get("valid_mask", np.isfinite(arrs["data"])))).astype(bool)
            raw_on, raw_valid_on = sample_grid(arrs["data"], raw_valid, epic["lat"], epic["lon"], ctx["grid"])
            std = source_to_standard(source, raw_on)
            source_samples[source] = (std, raw_valid_on & (std >= 0))
        source_rows: list[dict[str, Any]] = []
        pair_rows: list[dict[str, Any]] = []
        case_rows: list[dict[str, Any]] = []
        for policy_name, policy in POLICIES.items():
            epic_cls, epic_pv = apply_policy(epic["cloud_mask"], policy["epic"])
            classes = {}
            valids = {}
            for source, (std, sv) in source_samples.items():
                cls, pv = apply_policy(std, policy["geo"])
                classes[source] = cls
                valids[source] = sv & pv
                valid = valid_earth & epic_pv & valids[source]
                m = binary_metrics(epic_cls, cls, valid, policy["positive"])
                source_rows.append({"sample_id": tag, "policy": policy_name, "source_name": source, **m, "mean_abs_lat": nanmean_mask(np.abs(epic["lat"]), valid), "mean_epic_vza": nanmean_mask(epic["epic_vza"], valid), "mean_sza": nanmean_mask(epic["sza"], valid)})
            for a, b in SOURCE_PAIR_LIST:
                if a not in classes or b not in classes:
                    continue
                valid = valid_earth & epic_pv & valids[a] & valids[b]
                n = int(np.count_nonzero(valid))
                if n == 0:
                    continue
                ma = binary_metrics(epic_cls, classes[a], valid, policy["positive"])
                mb = binary_metrics(epic_cls, classes[b], valid, policy["positive"])
                a_match = valid & (classes[a] == epic_cls)
                b_match = valid & (classes[b] == epic_cls)
                both_correct = a_match & b_match
                both_wrong = valid & (~a_match) & (~b_match)
                a_only = a_match & (~b_match)
                b_only = b_match & (~a_match)
                disagree = valid & (classes[a] != classes[b])
                boundary = make_boundary(ctx["fused_on_epic"], ctx["fused_on_valid"], policy_name)[1]
                pair_rows.append({
                    "sample_id": tag, "policy": policy_name, "source_A": a, "source_B": b, "n_overlap_valid": n,
                    "A_agreement_to_epic": ma.get("agreement"), "B_agreement_to_epic": mb.get("agreement"),
                    "B_minus_A_agreement": safe_float(mb.get("agreement")) - safe_float(ma.get("agreement")),
                    "A_f1": ma.get("f1_cloud"), "B_f1": mb.get("f1_cloud"), "A_iou": ma.get("iou_cloud"), "B_iou": mb.get("iou_cloud"),
                    "A_cloud_fraction": ma.get("cloud_fraction_source"), "B_cloud_fraction": mb.get("cloud_fraction_source"), "EPIC_cloud_fraction": ma.get("cloud_fraction_epic"),
                    "A_cloud_fraction_bias": ma.get("cloud_fraction_bias"), "B_cloud_fraction_bias": mb.get("cloud_fraction_bias"),
                    "A_only_correct_fraction": int(np.count_nonzero(a_only)) / n, "B_only_correct_fraction": int(np.count_nonzero(b_only)) / n,
                    "both_correct_fraction": int(np.count_nonzero(both_correct)) / n, "both_wrong_fraction": int(np.count_nonzero(both_wrong)) / n,
                    "source_disagreement_fraction": int(np.count_nonzero(disagree)) / n, "boundary_fraction": float(np.mean(boundary[valid])),
                    "mean_abs_lat": nanmean_mask(np.abs(epic["lat"]), valid), "mean_epic_vza": nanmean_mask(epic["epic_vza"], valid), "mean_sza": nanmean_mask(epic["sza"], valid),
                    "dominant_source": row.get("dominant_source"), "candidate_group": row.get("candidate_group"),
                })
                if policy_name == "A_inclusive_binary" and n >= 50000:
                    case_rows.append({"sample_id": tag, "source_A": a, "source_B": b, "source_disagreement_fraction": int(np.count_nonzero(disagree)) / n, "B_minus_A_agreement": safe_float(mb.get("agreement")) - safe_float(ma.get("agreement")), "n_overlap_valid": n})
        return {"sample_id": tag, "source_rows": source_rows, "pair_rows": pair_rows, "case_rows": case_rows, "warnings": []}
    except Exception as exc:
        return {"sample_id": tag, "source_rows": [], "pair_rows": [], "case_rows": [], "warnings": [{"sample_id": tag, "module": "source_pair", "warning": str(exc), "traceback": traceback.format_exc()}]}


def source_pair_module(manifest: list[dict[str, Any]], out: Path, args: argparse.Namespace) -> None:
    fields = setup_fields()
    src_csv = out / "01_source_pair_recompute" / "stage09d_source_by_source_metrics.csv"
    pair_csv = out / "01_source_pair_recompute" / "stage09d_source_pair_overlap_metrics.csv"
    case_csv = out / "01_source_pair_recompute" / "stage09d_source_pair_disagreement_cases.csv"
    for p in [src_csv, pair_csv, case_csv]:
        if p.exists() and not args.resume:
            p.unlink()
    done = {r["sample_id"] for r in read_csv(src_csv)} if args.skip_existing and src_csv.exists() else set()
    rows = [row for row in manifest if row["sample_id"] not in done and truthy(row.get("can_run_source_pair"))]
    warnings: list[dict[str, Any]] = []
    for result in run_parallel(rows, args, compute_source_pair_for_sample, "source_pair"):
        append_csv(src_csv, result.get("source_rows", []), fields["source"])
        append_csv(pair_csv, result.get("pair_rows", []), fields["pair"])
        append_csv(case_csv, result.get("case_rows", []), ["sample_id", "source_A", "source_B", "source_disagreement_fraction", "B_minus_A_agreement", "n_overlap_valid"])
        warnings.extend(result.get("warnings", []))
    write_csv(out / "01_source_pair_recompute" / "stage09d_source_pair_warnings.csv", warnings)
    summarize_source_pair(out)
    checkpoint(out, "source_pair")


def summarize_source_pair(out: Path) -> None:
    rows = read_csv(out / "01_source_pair_recompute" / "stage09d_source_pair_overlap_metrics.csv")
    buckets: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        if safe_float(r.get("n_overlap_valid"), 0) >= 50000:
            buckets[(r["policy"], r["source_A"], r["source_B"])].append(r)
    summary = []
    for (policy, a, b), vals in sorted(buckets.items()):
        summary.append({
            "policy": policy, "source_A": a, "source_B": b, "sample_count": len(vals),
            "n_overlap_valid_mean": mean([safe_float(v["n_overlap_valid"]) for v in vals]),
            "A_agreement_mean": mean([safe_float(v["A_agreement_to_epic"]) for v in vals]),
            "B_agreement_mean": mean([safe_float(v["B_agreement_to_epic"]) for v in vals]),
            "B_minus_A_mean": mean([safe_float(v["B_minus_A_agreement"]) for v in vals]),
            "source_disagreement_fraction_mean": mean([safe_float(v["source_disagreement_fraction"]) for v in vals]),
            "both_wrong_fraction_mean": mean([safe_float(v["both_wrong_fraction"]) for v in vals]),
        })
    write_csv(out / "01_source_pair_recompute" / "stage09d_source_pair_summary_by_pair.csv", summary)
    met = [r for r in summary if "Meteosat" in r["source_A"] or "Meteosat" in r["source_B"]]
    write_csv(out / "01_source_pair_recompute" / "stage09d_meteosat_pair_special_audit.csv", met)
    lines = ["# Stage 09D Source-Pair Recompute Report", "", f"- Generated UTC: `{utc_now()}`", f"- Pair rows: `{len(rows)}`", f"- Summary pairs: `{len(summary)}`"]
    (out / "01_source_pair_recompute" / "stage09d_source_pair_recompute_report.md").write_text("\n".join(lines), encoding="utf-8")


def mean(vals: list[float]) -> float:
    vals = [v for v in vals if math.isfinite(v)]
    return sum(vals) / len(vals) if vals else math.nan


def compute_sampling_for_sample(row: dict[str, Any]) -> dict[str, Any]:
    tag = row["sample_id"]
    try:
        ctx = sample_context(row)
        epic = ctx["epic"]
        r, c, ok = row_col(epic["lat"], epic["lon"], ctx["grid"], ctx["fused_data"].shape)
        valid_earth = np.isin(epic["cloud_mask"], [1, 2, 3, 4])
        rows = []
        for policy_name, policy in POLICIES.items():
            epic_cls, epic_pv = apply_policy(epic["cloud_mask"], policy["epic"])
            fused_cls, fused_pv = apply_policy(ctx["fused_on_epic"], policy["geo"])
            base_valid = valid_earth & epic_pv & ctx["fused_on_valid"] & fused_pv
            nearest = binary_metrics(epic_cls, fused_cls, base_valid, policy["positive"])
            nearest_ag = safe_float(nearest.get("agreement"))
            nearest_f1 = safe_float(nearest.get("f1_cloud"))
            _frac5, boundary = make_boundary(ctx["fused_on_epic"], ctx["fused_on_valid"], policy_name)
            boundary_label = "boundary_mixed" if np.nanmean(boundary[base_valid]) > 0.1 else "mostly_non_boundary"
            rows.append(sample_row(row, policy_name, "nearest", 1, math.nan, nearest, 0, 0, boundary_label))
            grid_cls, grid_pv = apply_policy(ctx["fused_data"], policy["geo"])
            grid_cloud = (grid_cls == policy["positive"]) & ctx["fused_valid"] & grid_pv
            grid_valid = ctx["fused_valid"] & grid_pv
            ii_cloud = integral_sum(grid_cloud)
            ii_valid = integral_sum(grid_valid)
            for win in [3, 5, 7]:
                rad = win // 2
                cloud_count = window_counts(ii_cloud, r, c, ok, rad, ctx["fused_data"].shape)
                valid_count = window_counts(ii_valid, r, c, ok, rad, ctx["fused_data"].shape)
                frac = np.where(valid_count > 0, cloud_count / np.maximum(valid_count, 1), np.nan)
                for method, sampled in [
                    (f"majority_{win}x{win}", np.where(frac >= 0.5, policy["positive"], 0)),
                    (f"fraction_{win}x{win}_threshold_0.5", np.where(frac >= 0.5, policy["positive"], 0)),
                ]:
                    valid = valid_earth & epic_pv & np.isfinite(frac) & (valid_count > 0)
                    sampled = sampled.astype(np.int16)
                    m = binary_metrics(epic_cls, sampled, valid, policy["positive"])
                    rows.append(sample_row(row, policy_name, method, win, 0.5, m, safe_float(m.get("agreement")) - nearest_ag, safe_float(m.get("f1_cloud")) - nearest_f1, boundary_label))
        return {"sample_id": tag, "rows": rows, "warnings": []}
    except Exception as exc:
        return {"sample_id": tag, "rows": [], "warnings": [{"sample_id": tag, "module": "sampling", "warning": str(exc), "traceback": traceback.format_exc()}]}


def sampling_module(manifest: list[dict[str, Any]], out: Path, args: argparse.Namespace) -> None:
    fields = setup_fields()
    csv_path = out / "02_sampling_sensitivity" / "stage09d_sampling_sensitivity_by_sample.csv"
    if csv_path.exists() and not args.resume:
        csv_path.unlink()
    done = {r["sample_id"] for r in read_csv(csv_path)} if args.skip_existing and csv_path.exists() else set()
    warnings = []
    rows = [row for row in manifest if row["sample_id"] not in done and truthy(row.get("can_run_sampling"))]
    for result in run_parallel(rows, args, compute_sampling_for_sample, "sampling"):
        append_csv(csv_path, result.get("rows", []), fields["sampling"])
        warnings.extend(result.get("warnings", []))
    write_csv(out / "02_sampling_sensitivity" / "stage09d_sampling_warnings.csv", warnings)
    summarize_sampling(out)
    checkpoint(out, "sampling")


def sample_row(row: dict[str, Any], policy: str, method: str, win: int, thr: float, m: dict[str, Any], dag: float, df1: float, boundary: str) -> dict[str, Any]:
    return {"sample_id": row["sample_id"], "policy": policy, "sampling_method": method, "window_size": win, "threshold": thr, "n_valid": m.get("n_valid"), "agreement": m.get("agreement"), "f1_cloud": m.get("f1_cloud"), "iou_cloud": m.get("iou_cloud"), "balanced_accuracy": m.get("balanced_accuracy"), "cloud_fraction_epic": m.get("cloud_fraction_epic"), "cloud_fraction_sampled_georing": m.get("cloud_fraction_source"), "cloud_fraction_bias": m.get("cloud_fraction_bias"), "delta_agreement_vs_nearest": dag, "delta_f1_vs_nearest": df1, "candidate_group": row.get("candidate_group"), "dominant_source": row.get("dominant_source"), "boundary_class": boundary}


def summarize_sampling(out: Path) -> None:
    rows = read_csv(out / "02_sampling_sensitivity" / "stage09d_sampling_sensitivity_by_sample.csv")
    for field, name in [("candidate_group", "stage09d_sampling_sensitivity_by_group.csv"), ("boundary_class", "stage09d_sampling_sensitivity_by_boundary_class.csv")]:
        buckets: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
        for r in rows:
            buckets[(r["policy"], r["sampling_method"], r.get(field, ""))].append(r)
        out_rows = []
        for (policy, method, key), vals in sorted(buckets.items()):
            out_rows.append({"policy": policy, "sampling_method": method, field: key, "sample_count": len(set(v["sample_id"] for v in vals)), "agreement_mean": mean([safe_float(v["agreement"]) for v in vals]), "delta_agreement_vs_nearest_mean": mean([safe_float(v["delta_agreement_vs_nearest"]) for v in vals]), "f1_mean": mean([safe_float(v["f1_cloud"]) for v in vals])})
        write_csv(out / "02_sampling_sensitivity" / name, out_rows)
    (out / "02_sampling_sensitivity" / "stage09d_sampling_sensitivity_report.md").write_text(f"# Stage 09D Sampling Sensitivity\n\n- Rows: `{len(rows)}`\n", encoding="utf-8")


def compute_geometry_boundary_for_sample(row: dict[str, Any]) -> dict[str, Any]:
    tag = row["sample_id"]
    abs_bins = [("0-20", 0, 20), ("20-40", 20, 40), ("40-60", 40, 60), ("60-70", 60, 70), ("70-80", 70, 80), ("80-90", 80, 91)]
    vza_bins = [("0-20", 0, 20), ("20-40", 20, 40), ("40-60", 40, 60), ("60-70", 60, 70), (">70", 70, 181)]
    sza_bins = [("0-30", 0, 30), ("30-50", 30, 50), ("50-70", 50, 70), ("70-80", 70, 80), (">80", 80, 181)]
    count_bins = [("1", 1, 2), ("2", 2, 3), ("3", 3, 4), (">=4", 4, 100)]
    try:
        ctx = sample_context(row)
        epic = ctx["epic"]
        valid_earth = np.isin(epic["cloud_mask"], [1, 2, 3, 4])
        geom_rows = []
        scene_rows = []
        for policy_name, policy in POLICIES.items():
            epic_cls, epic_pv = apply_policy(epic["cloud_mask"], policy["epic"])
            fused_cls, fused_pv = apply_policy(ctx["fused_on_epic"], policy["geo"])
            base = valid_earth & epic_pv & ctx["fused_on_valid"] & fused_pv
            mismatch = base & (epic_cls != fused_cls)
            total_n = max(int(np.count_nonzero(base)), 1)
            total_m = max(int(np.count_nonzero(mismatch)), 1)
            dims = {
                "abs_lat_bin": (np.abs(epic["lat"]), abs_bins),
                "epic_view_zenith_bin": (epic["epic_vza"], vza_bins),
                "solar_zenith_bin": (epic["sza"], sza_bins),
                "valid_source_count_bin": (ctx["valid_count"], count_bins),
            }
            for dim, (arr, bins) in dims.items():
                labels = classify_array(arr, bins)
                for label in sorted(set(labels[base])):
                    v = base & (labels == label)
                    n = int(np.count_nonzero(v))
                    if n == 0:
                        continue
                    mm = int(np.count_nonzero(mismatch & (labels == label)))
                    m = binary_metrics(epic_cls, fused_cls, v, policy["positive"])
                    pf = n / total_n
                    mf = mm / total_m
                    geom_rows.append({"sample_id": tag, "policy": policy_name, "dimension": dim, "bin": label, "n_valid": n, "agreement": m.get("agreement"), "mismatch_rate": 1 - safe_float(m.get("agreement")), "pixel_fraction": pf, "mismatch_fraction": mf, "enrichment": mf / max(pf, 1e-12), "candidate_group": row.get("candidate_group"), "dominant_source": row.get("dominant_source")})
            frac5, boundary = make_boundary(ctx["fused_on_epic"], ctx["fused_on_valid"], policy_name)
            scene_type = np.full(base.shape, "broken_cloud", dtype=object)
            scene_type[frac5 <= 0.1] = "homogeneous_clear"
            scene_type[frac5 >= 0.9] = "homogeneous_cloud"
            bclass = np.where(boundary, "near_boundary_1cell", "non_boundary")
            for st in ["homogeneous_clear", "homogeneous_cloud", "broken_cloud"]:
                for bc in ["near_boundary_1cell", "non_boundary"]:
                    v = base & (scene_type == st) & (bclass == bc)
                    n = int(np.count_nonzero(v))
                    if n == 0:
                        continue
                    m = binary_metrics(epic_cls, fused_cls, v, policy["positive"])
                    mm = int(np.count_nonzero(mismatch & (scene_type == st) & (bclass == bc)))
                    pf = n / total_n
                    mf = mm / total_m
                    scene_rows.append({"sample_id": tag, "policy": policy_name, "scene_type": st, "boundary_class": bc, "n_valid": n, "agreement": m.get("agreement"), "mismatch_rate": 1 - safe_float(m.get("agreement")), "mismatch_enrichment": mf / max(pf, 1e-12), "cloud_fraction_epic": m.get("cloud_fraction_epic"), "cloud_fraction_georing": m.get("cloud_fraction_source"), "dominant_source": row.get("dominant_source"), "selected_source": "", "mean_abs_lat": nanmean_mask(np.abs(epic["lat"]), v), "mean_epic_vza": nanmean_mask(epic["epic_vza"], v)})
        return {"sample_id": tag, "geom_rows": geom_rows, "scene_rows": scene_rows, "warnings": []}
    except Exception as exc:
        return {"sample_id": tag, "geom_rows": [], "scene_rows": [], "warnings": [{"sample_id": tag, "module": "geometry_boundary", "warning": str(exc), "traceback": traceback.format_exc()}]}


def geometry_boundary_modules(manifest: list[dict[str, Any]], out: Path, args: argparse.Namespace) -> None:
    fields = setup_fields()
    geom_csv = out / "03_geometry_stratification" / "stage09d_geometry_bin_metrics.csv"
    boundary_csv = out / "04_boundary_broken_cloud" / "stage09d_scene_metrics_by_sample.csv"
    if not args.resume:
        for p in [geom_csv, boundary_csv]:
            if p.exists():
                p.unlink()
    done = {r["sample_id"] for r in read_csv(geom_csv)} if args.skip_existing and geom_csv.exists() else set()
    warnings = []
    rows = [row for row in manifest if row["sample_id"] not in done and truthy(row.get("can_run_geometry"))]
    for result in run_parallel(rows, args, compute_geometry_boundary_for_sample, "geometry_boundary"):
        append_csv(geom_csv, result.get("geom_rows", []), fields["geometry"])
        append_csv(boundary_csv, result.get("scene_rows", []), fields["boundary"])
        warnings.extend(result.get("warnings", []))
    write_csv(out / "03_geometry_stratification" / "stage09d_geometry_warnings.csv", warnings)
    summarize_geometry_boundary(out)
    checkpoint(out, "geometry_boundary")


def summarize_geometry_boundary(out: Path) -> None:
    geom = read_csv(out / "03_geometry_stratification" / "stage09d_geometry_bin_metrics.csv")
    buckets: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for r in geom:
        buckets[(r["policy"], r["dimension"], r["bin"])].append(r)
    gsum = []
    for (policy, dim, b), vals in sorted(buckets.items()):
        gsum.append({"policy": policy, "dimension": dim, "bin": b, "sample_count": len(set(v["sample_id"] for v in vals)), "agreement_mean": mean([safe_float(v["agreement"]) for v in vals]), "mismatch_rate_mean": mean([safe_float(v["mismatch_rate"]) for v in vals]), "enrichment_mean": mean([safe_float(v["enrichment"]) for v in vals])})
    write_csv(out / "03_geometry_stratification" / "stage09d_geometry_mismatch_enrichment.csv", gsum)
    write_csv(out / "03_geometry_stratification" / "stage09d_geometry_cross_bin_metrics.csv", gsum)
    (out / "03_geometry_stratification" / "stage09d_geometry_report.md").write_text(f"# Stage 09D Geometry Report\n\n- Rows: `{len(geom)}`\n", encoding="utf-8")

    scene = read_csv(out / "04_boundary_broken_cloud" / "stage09d_scene_metrics_by_sample.csv")
    buckets2: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for r in scene:
        buckets2[(r["policy"], r["scene_type"], r["boundary_class"])].append(r)
    rows = []
    for (policy, st, bc), vals in sorted(buckets2.items()):
        rows.append({"policy": policy, "scene_type": st, "boundary_class": bc, "sample_count": len(set(v["sample_id"] for v in vals)), "agreement_mean": mean([safe_float(v["agreement"]) for v in vals]), "mismatch_rate_mean": mean([safe_float(v["mismatch_rate"]) for v in vals]), "mismatch_enrichment_mean": mean([safe_float(v["mismatch_enrichment"]) for v in vals])})
    write_csv(out / "04_boundary_broken_cloud" / "stage09d_scene_metrics_by_class.csv", rows)
    write_csv(out / "04_boundary_broken_cloud" / "stage09d_boundary_mismatch_enrichment.csv", rows)
    (out / "04_boundary_broken_cloud" / "stage09d_broken_cloud_report.md").write_text(f"# Stage 09D Boundary/Broken Cloud Report\n\n- Rows: `{len(scene)}`\n", encoding="utf-8")


def checkpoint(out: Path, module: str) -> None:
    (out / "logs" / "stage09d_checkpoint.json").write_text(json.dumps({"module": module, "time": utc_now()}, indent=2), encoding="utf-8")


def error_atlas(out: Path, manifest: list[dict[str, Any]]) -> None:
    samples = read_csv(DEFAULT_09C_DIR / "02_expanded_diagnostics" / "stage09c_sample_level_semantic_summary.csv")
    if not samples:
        return
    def sf(r, k): return safe_float(r.get(k))
    def clean_label(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower() or "unknown"
    choices: list[tuple[str, dict[str, str]]] = []
    selected: set[str] = set()
    def add_choice(name: str, row: dict[str, str]) -> None:
        tag = row.get("sample_id", "")
        if not tag or tag in selected:
            return
        selected.add(tag)
        choices.append((name, row))
    add_choice("best_agreement_case", max(samples, key=lambda r: sf(r, "A_agreement")))
    add_choice("worst_agreement_case", min(samples, key=lambda r: sf(r, "A_agreement")))
    add_choice("largest_B_minus_A_case", max(samples, key=lambda r: sf(r, "B_minus_A_agreement")))
    add_choice("largest_abs_cloud_fraction_bias_case", max(samples, key=lambda r: abs(sf(r, "A_cloud_fraction_bias"))))
    for group in sorted(set(r.get("candidate_group", "") for r in samples)):
        group_rows = [r for r in samples if r.get("candidate_group") == group]
        if group_rows:
            add_choice(f"lowest_agreement_group_{clean_label(group)}", min(group_rows, key=lambda r: sf(r, "A_agreement")))
    for source in sorted(set(r.get("dominant_source", "") for r in samples)):
        source_rows = [r for r in samples if r.get("dominant_source") == source]
        if source_rows:
            add_choice(f"lowest_agreement_source_{clean_label(source)}", min(source_rows, key=lambda r: sf(r, "A_agreement")))
    for rank, row in enumerate(sorted(samples, key=lambda r: sf(r, "A_agreement")), 1):
        add_choice(f"low_agreement_rank_{rank:02d}", row)
        if len(choices) >= 9:
            break
    man = {r["sample_id"]: r for r in manifest}
    inv_rows = []
    for name, row in choices:
        tag = row["sample_id"]
        if tag not in man:
            continue
        case_dir = out / "05_error_atlas" / f"case_{name}_{tag}"
        case_dir.mkdir(parents=True, exist_ok=True)
        case_summary = case_dir / "case_summary.csv"
        case_panel = case_dir / "case_panel.png"
        inv_rows.append({"case_type": name, "sample_id": tag, "A_agreement": row.get("A_agreement"), "B_minus_A": row.get("B_minus_A_agreement"), "case_dir": str(case_dir), "case_panel": str(case_panel), "source_csv": str(case_summary)})
        write_csv(case_summary, [dict(row, case_type=name)])
        try:
            ctx = sample_context(man[tag])
            epic_cls, ev = apply_policy(ctx["epic"]["cloud_mask"], POLICIES["A_inclusive_binary"]["epic"])
            geo_cls, gv = apply_policy(ctx["fused_on_epic"], POLICIES["A_inclusive_binary"]["geo"])
            valid = ev & gv & ctx["fused_on_valid"]
            mismatch = np.full(epic_cls.shape, np.nan, dtype=np.float32)
            mismatch[valid & (epic_cls == 0) & (geo_cls == 0)] = 0
            mismatch[valid & (epic_cls == 1) & (geo_cls == 1)] = 1
            mismatch[valid & (epic_cls == 0) & (geo_cls == 1)] = 2
            mismatch[valid & (epic_cls == 1) & (geo_cls == 0)] = 3
            frac5, boundary = make_boundary(ctx["fused_on_epic"], ctx["fused_on_valid"], "A_inclusive_binary")
            arrays = [epic_cls, geo_cls, mismatch, ctx["selected_source"], ctx["valid_count"], boundary.astype(float), frac5, np.abs(ctx["epic"]["lat"])]
            titles = ["EPIC", "GEO fused", "TP/TN/FP/FN", "source_map", "valid_count", "boundary", "local_frac5", "abs_lat"]
            fig, axes = plt.subplots(2, 4, figsize=(13, 7), constrained_layout=True)
            stride = max(1, epic_cls.shape[0] // 512)
            for ax, arr, title in zip(axes.ravel(), arrays, titles):
                im = ax.imshow(arr[::stride, ::stride], interpolation="nearest")
                ax.set_title(title)
                ax.axis("off")
                fig.colorbar(im, ax=ax, fraction=0.035)
            fig.suptitle(f"{name} {tag}")
            fig.savefig(case_panel, dpi=150)
            plt.close(fig)
        except Exception as exc:
            write_csv(case_dir / "case_error.csv", [{"error": str(exc), "traceback": traceback.format_exc()}])
    write_csv(out / "05_error_atlas" / "stage09d_error_case_inventory.csv", inv_rows)
    write_csv(out / "05_error_atlas" / "stage09d_error_type_pixel_summary.csv", inv_rows)
    (out / "05_error_atlas" / "stage09d_error_atlas_report.md").write_text(f"# Stage 09D Error Atlas\n\n- Cases: `{len(inv_rows)}`\n", encoding="utf-8")


def integrated_summary(out: Path) -> list[dict[str, Any]]:
    group = read_csv(DEFAULT_09C_DIR / "02_expanded_diagnostics" / "stage09c_group_level_summary.csv")
    sampling = read_csv(out / "02_sampling_sensitivity" / "stage09d_sampling_sensitivity_by_group.csv")
    boundary = read_csv(out / "04_boundary_broken_cloud" / "stage09d_scene_metrics_by_class.csv")
    geom = read_csv(out / "03_geometry_stratification" / "stage09d_geometry_mismatch_enrichment.csv")
    pair = read_csv(out / "01_source_pair_recompute" / "stage09d_source_pair_summary_by_pair.csv")
    factors: list[dict[str, Any]] = []
    group_a = [r for r in group if r.get("group_field") == "candidate_group"]
    meteosat = next((r for r in group_a if r.get("group_value") == "METEOSAT_DOMINANT_CONTROL"), {})
    best = max([safe_float(r.get("A_agreement_mean")) for r in group_a], default=math.nan)
    diff = best - safe_float(meteosat.get("A_agreement_mean"))
    factors.append(factor("source_scene_family", "candidate-group A agreement spread", "best_group_minus_meteosat", diff, ">=0.15", "strong" if diff >= 0.15 else "moderate", "Meteosat-dominant scenes remain lower than GOES/East Asia.", "", "audit Meteosat product semantics", "stage09c_group_level_summary.csv"))
    ba = mean([safe_float(r.get("delta_agreement_vs_nearest_mean")) for r in sampling if r.get("sampling_method", "").startswith("majority_") and r.get("policy") == "A_inclusive_binary"])
    factors.append(factor("nearest_neighbor_sampling", "window sampling delta", "mean_delta_agreement", ba, ">=0.02 moderate, >=0.05 strong", "strong" if ba >= 0.05 else "moderate" if ba >= 0.02 else "weak", "Window sampling effect estimates representativeness error.", "", "use only as diagnostic, not fusion v2", "stage09d_sampling_sensitivity_by_group.csv"))
    broken = max([safe_float(r.get("mismatch_enrichment_mean")) for r in boundary if r.get("scene_type") == "broken_cloud" and r.get("policy") == "A_inclusive_binary"], default=math.nan)
    factors.append(factor("broken_cloud", "broken cloud mismatch enrichment", "max_enrichment", broken, ">=1.5", "strong" if broken >= 2 else "moderate" if broken >= 1.5 else "weak", "Broken-cloud enrichment tests cloud-scale mismatch.", "", "inspect atlas cases", "stage09d_scene_metrics_by_class.csv"))
    boundary_rows = [r for r in boundary if r.get("policy") == "A_inclusive_binary"]
    br = mean([safe_float(r.get("mismatch_rate_mean")) for r in boundary_rows if r.get("boundary_class") == "near_boundary_1cell"])
    nbr = mean([safe_float(r.get("mismatch_rate_mean")) for r in boundary_rows if r.get("boundary_class") == "non_boundary"])
    factors.append(factor("cloud_boundary", "boundary minus non-boundary mismatch", "rate_delta", br - nbr, ">=0.10", "strong" if br - nbr >= 0.2 else "moderate" if br - nbr >= 0.1 else "weak", "Cloud boundaries are compared against homogeneous/non-boundary pixels.", "", "add pixel atlas review", "stage09d_scene_metrics_by_class.csv"))
    max_pair = max([safe_float(r.get("source_disagreement_fraction_mean")) for r in pair if r.get("policy") == "A_inclusive_binary"], default=math.nan)
    factors.append(factor("source_pair_disagreement", "max source-pair disagreement", "max_fraction", max_pair, ">=0.5", "strong" if max_pair >= 0.5 else "weak", "Large pair disagreement means source choice can explain part of mismatch.", "", "audit pair by region", "stage09d_source_pair_summary_by_pair.csv"))
    max_geom = max([safe_float(r.get("enrichment_mean")) for r in geom if r.get("policy") == "A_inclusive_binary"], default=math.nan)
    factors.append(factor("epic_view_geometry", "max geometry enrichment", "max_enrichment", max_geom, ">=1.5", "strong" if max_geom >= 2 else "moderate" if max_geom >= 1.5 else "weak", "Geometry bins identify mismatch-enriched regions.", "EPIC VZA may be missing for some products.", "audit geometry-specific bins", "stage09d_geometry_mismatch_enrichment.csv"))
    for name in ["meteosat_low_agreement", "meteosat_0deg_specific", "meteosat_iodc_specific", "semantic_low_confidence", "raw_code_mapping", "high_latitude", "solar_geometry", "geo_view_geometry", "valid_source_count", "time_offset", "unresolved_mismatch"]:
        factors.append(factor(name, "available evidence table", "see_related_csv", math.nan, "", "insufficient_data" if name in {"geo_view_geometry", "solar_geometry", "unresolved_mismatch"} else "moderate", "Included for completeness; see source CSV and limitations.", "Some variables may be unavailable in EPIC/GEO run outputs.", "continue targeted audit", "multiple"))
    write_csv(out / "06_integrated_factor_summary" / "stage09d_factor_contribution_summary.csv", factors)
    write_csv(out / "06_integrated_factor_summary" / "stage09d_integrated_diagnosis_table.csv", factors)
    return factors


def factor(factor: str, test: str, metric: str, value: Any, threshold: str, level: str, interp: str, limitation: str, next_action: str, source_csv: str) -> dict[str, Any]:
    return {"factor": factor, "diagnostic_test": test, "metric": metric, "observed_value": value, "threshold": threshold, "evidence_level": level, "interpretation": interp, "limitation": limitation, "next_action": next_action, "source_csv": source_csv}


def build_plots(out: Path) -> list[dict[str, Any]]:
    idx = []
    t = utc_now()
    def add(path: Path, source: Path, desc: str):
        idx.append({"plot_path": str(path), "source_csv": str(source), "description": desc, "created_time": t})
    def bar(path: Path, labels: list[str], vals: list[float], title: str, source: Path, desc: str, ylim: tuple[float, float] | None = None):
        fig, ax = plt.subplots(figsize=(max(8, 0.6 * len(labels)), 4.8), constrained_layout=True)
        ax.bar(range(len(labels)), vals, color="#386fa4")
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=45, ha="right")
        ax.set_title(title)
        ax.grid(axis="y", alpha=0.25)
        if ylim:
            ax.set_ylim(*ylim)
        fig.savefig(path, dpi=170)
        plt.close(fig)
        add(path, source, desc)
    pair_csv = out / "01_source_pair_recompute" / "stage09d_source_pair_summary_by_pair.csv"
    pair = [r for r in read_csv(pair_csv) if r.get("policy") == "A_inclusive_binary"]
    bar(out / "07_summary_figures" / "stage09d_source_pair_overlap_heatmap.png", [f"{r['source_A']}|{r['source_B']}" for r in pair], [safe_float(r["source_disagreement_fraction_mean"], 0) for r in pair], "Source-pair disagreement", pair_csv, "Source-pair overlap disagreement", (0, 1))
    src_csv = out / "01_source_pair_recompute" / "stage09d_source_by_source_metrics.csv"
    src_rows = [r for r in read_csv(src_csv) if r.get("policy") == "A_inclusive_binary"]
    buckets: dict[str, list[float]] = defaultdict(list)
    for r in src_rows:
        buckets[r["source_name"]].append(safe_float(r["agreement"]))
    bar(out / "07_summary_figures" / "stage09d_source_by_source_agreement.png", list(buckets), [mean(v) for v in buckets.values()], "Source-by-source agreement", src_csv, "Single-source agreement", (0, 1))
    met_csv = out / "01_source_pair_recompute" / "stage09d_meteosat_pair_special_audit.csv"
    met = read_csv(met_csv)
    bar(out / "07_summary_figures" / "stage09d_meteosat_pair_special_audit.png", [f"{r['source_A']}|{r['source_B']}" for r in met], [safe_float(r.get("source_disagreement_fraction_mean"), 0) for r in met], "Meteosat pair audit", met_csv, "Meteosat pair special audit", (0, 1))
    samp_csv = out / "02_sampling_sensitivity" / "stage09d_sampling_sensitivity_by_group.csv"
    samp = [r for r in read_csv(samp_csv) if r.get("policy") == "A_inclusive_binary" and r.get("sampling_method") != "nearest"]
    bar(out / "07_summary_figures" / "stage09d_sampling_sensitivity.png", [r["sampling_method"] + "|" + r.get("candidate_group", "") for r in samp[:30]], [safe_float(r.get("delta_agreement_vs_nearest_mean"), 0) for r in samp[:30]], "Sampling sensitivity", samp_csv, "Window sampling deltas")
    scene_csv = out / "04_boundary_broken_cloud" / "stage09d_scene_metrics_by_class.csv"
    scene = [r for r in read_csv(scene_csv) if r.get("policy") == "A_inclusive_binary"]
    bar(out / "07_summary_figures" / "stage09d_boundary_vs_nonboundary_mismatch.png", [r["scene_type"] + "|" + r["boundary_class"] for r in scene], [safe_float(r["mismatch_rate_mean"], 0) for r in scene], "Boundary mismatch rate", scene_csv, "Boundary vs non-boundary mismatch")
    bar(out / "07_summary_figures" / "stage09d_broken_cloud_enrichment.png", [r["scene_type"] + "|" + r["boundary_class"] for r in scene], [safe_float(r["mismatch_enrichment_mean"], 0) for r in scene], "Broken cloud enrichment", scene_csv, "Broken cloud mismatch enrichment")
    geom_csv = out / "03_geometry_stratification" / "stage09d_geometry_mismatch_enrichment.csv"
    geom = [r for r in read_csv(geom_csv) if r.get("policy") == "A_inclusive_binary"]
    bar(out / "07_summary_figures" / "stage09d_geometry_lat_epicvza_heatmap.png", [r["dimension"] + "|" + r["bin"] for r in geom[:40]], [safe_float(r["enrichment_mean"], 0) for r in geom[:40]], "Geometry enrichment", geom_csv, "Geometry bin mismatch enrichment")
    bar(out / "07_summary_figures" / "stage09d_selected_source_geo_vza_heatmap.png", [r["dimension"] + "|" + r["bin"] for r in geom if r["dimension"] == "valid_source_count_bin"], [safe_float(r["enrichment_mean"], 0) for r in geom if r["dimension"] == "valid_source_count_bin"], "Valid source count enrichment", geom_csv, "Valid source count bins")
    fac_csv = out / "06_integrated_factor_summary" / "stage09d_factor_contribution_summary.csv"
    fac = read_csv(fac_csv)
    order = {"strong": 4, "moderate": 3, "weak": 2, "not_supported": 1, "insufficient_data": 0}
    bar(out / "07_summary_figures" / "stage09d_factor_evidence_summary.png", [r["factor"] for r in fac], [order.get(r["evidence_level"], 0) for r in fac], "Factor evidence level", fac_csv, "Integrated evidence levels")
    atlas_csv = out / "05_error_atlas" / "stage09d_error_case_inventory.csv"
    atlas = read_csv(atlas_csv)
    bar(out / "07_summary_figures" / "stage09d_error_atlas_overview.png", [r["case_type"] for r in atlas], [safe_float(r.get("A_agreement"), 0) for r in atlas], "Error atlas cases", atlas_csv, "Selected error atlas cases", (0, 1))
    for panel in sorted((out / "05_error_atlas").glob("case_*/case_panel.png")):
        source = panel.parent / "case_summary.csv"
        if source.exists():
            add(panel, source, "Error atlas case panel")
    write_csv(out / "07_summary_figures" / "stage09d_plot_index.csv", idx, ["plot_path", "source_csv", "description", "created_time"])
    return idx


def build_report(out: Path, manifest: list[dict[str, Any]], factors: list[dict[str, Any]]) -> Path:
    pair_rows = read_csv(out / "01_source_pair_recompute" / "stage09d_source_pair_overlap_metrics.csv")
    pair_summary = read_csv(out / "01_source_pair_recompute" / "stage09d_source_pair_summary_by_pair.csv")
    sampling = read_csv(out / "02_sampling_sensitivity" / "stage09d_sampling_sensitivity_by_sample.csv")
    geom = read_csv(out / "03_geometry_stratification" / "stage09d_geometry_bin_metrics.csv")
    scene = read_csv(out / "04_boundary_broken_cloud" / "stage09d_scene_metrics_by_sample.csv")
    source_pair_samples = len(set(r["sample_id"] for r in pair_rows))
    sampling_samples = len(set(r["sample_id"] for r in sampling))
    geom_samples = len(set(r["sample_id"] for r in geom))
    scene_samples = len(set(r["sample_id"] for r in scene))
    top_pairs = sorted([r for r in pair_summary if r.get("policy") == "A_inclusive_binary"], key=lambda r: safe_float(r.get("source_disagreement_fraction_mean"), 0), reverse=True)[:5]
    insufficient = [r["factor"] for r in factors if r["evidence_level"] == "insufficient_data"]
    strongest = [r["factor"] for r in sorted(factors, key=lambda r: {"strong": 4, "moderate": 3, "weak": 2, "not_supported": 1, "insufficient_data": 0}.get(r["evidence_level"], 0), reverse=True)[:5]]
    lines = [
        "# Stage 09D Full Pixel Diagnostics Report",
        "",
        f"- Generated UTC: `{utc_now()}`",
        "- Scope: 2024-03 local Stage 09C diagnostic samples only; no new sample production; no fusion v2.",
        f"- Manifest samples: `{len(manifest)}`",
        f"- Source-pair recomputation samples: `{source_pair_samples}`",
        f"- Sampling sensitivity samples: `{sampling_samples}`",
        f"- Geometry diagnostics samples: `{geom_samples}`",
        f"- Boundary/broken-cloud samples: `{scene_samples}`",
        f"- Factor rows: `{len(factors)}`",
        "",
        "## Required Answers",
        "",
        f"1. Source-pair recomputation completed for `{source_pair_samples}` samples.",
        "2. Largest source-pair disagreement pairs: " + "; ".join(f"{r['source_A']} vs {r['source_B']}={fmt(r.get('source_disagreement_fraction_mean'))}" for r in top_pairs),
        "3. FY4B vs Himawari high disagreement is evaluated in the source-pair summary; see `stage09d_source_pair_summary_by_pair.csv`.",
        "4. Meteosat-0deg vs IODC pair-level comparison is in `stage09d_meteosat_pair_special_audit.csv`; current evidence should be read pair-by-pair rather than only dominant-source means.",
        "5. Meteosat homogeneous clear/cloud behavior is covered by boundary scene class CSV, but needs product-code audit for definitive mechanism attribution.",
        "6. Nearest-neighbor sampling bias is tested by window deltas in `stage09d_sampling_sensitivity_by_sample.csv`.",
        "7. 3x3/5x5/7x7 window effect is summarized in `stage09d_sampling_sensitivity_by_group.csv`.",
        "8. Boundary/broken-cloud enrichment is summarized in `stage09d_boundary_mismatch_enrichment.csv`.",
        "9. High-latitude / EPIC VZA / SZA / source-count enrichment is summarized in geometry CSVs; GEO VZA is only available where source products expose it.",
        "10. Valid source count is stratified as a geometry dimension.",
        "11. Explained mismatch factors are summarized in the integrated factor table.",
        "12. Unresolved mismatch remains for factors marked `insufficient_data` and for pixels not captured by semantic/sampling/boundary/source-pair strata.",
        "13. Meteosat low agreement cannot yet be attributed to one single mechanism; source/scene family and product semantics remain the leading explanation, with geometry and boundary effects as secondary tests.",
        "14. The next step should be product semantics/raw code-table audit before more broad sample expansion.",
        "",
        "## Strongest Evidence Factors",
        "",
    ]
    for f in strongest:
        lines.append(f"- {f}")
    lines.extend(["", "## Insufficient Data Factors", ""])
    for f in insufficient:
        lines.append(f"- {f}")
    lines.extend(["", "## Key Outputs", ""])
    for p in [
        out / "01_source_pair_recompute" / "stage09d_source_pair_overlap_metrics.csv",
        out / "02_sampling_sensitivity" / "stage09d_sampling_sensitivity_by_sample.csv",
        out / "03_geometry_stratification" / "stage09d_geometry_bin_metrics.csv",
        out / "04_boundary_broken_cloud" / "stage09d_scene_metrics_by_sample.csv",
        out / "05_error_atlas" / "stage09d_error_case_inventory.csv",
        out / "06_integrated_factor_summary" / "stage09d_factor_contribution_summary.csv",
        out / "07_summary_figures" / "stage09d_plot_index.csv",
    ]:
        lines.append(f"- `{p}`")
    report = out / "reports" / "stage09d_full_pixel_diagnostics_report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def fmt(v: Any) -> str:
    x = safe_float(v)
    return "NA" if not math.isfinite(x) else f"{x:.3f}"


def run_all(args: argparse.Namespace) -> None:
    out = Path(args.output_dir)
    ensure_dirs(out)
    manifest = build_manifest(Path(args.stage09c_dir), out)
    if args.max_samples and args.max_samples > 0:
        manifest = manifest[:args.max_samples]
        print(f"[stage09d] limiting manifest to first {len(manifest)} samples for probe run", flush=True)
    only = set(args.only.split(",")) if args.only else {"source_pair", "sampling", "geometry", "boundary", "summary"}
    if "source_pair" in only:
        source_pair_module(manifest, out, args)
    if "sampling" in only:
        sampling_module(manifest, out, args)
    if "geometry" in only or "boundary" in only:
        geometry_boundary_modules(manifest, out, args)
    if "summary" in only:
        error_atlas(out, manifest)
        factors = integrated_summary(out)
        build_plots(out)
        report = build_report(out, manifest, factors)
        print(f"Stage09D complete: report={report}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Stage 09D full per-sample pixel diagnostics for Stage 09C samples.")
    p.add_argument("--stage09c-dir", default=str(DEFAULT_09C_DIR))
    p.add_argument("--output-dir", default=str(DEFAULT_OUT_DIR))
    p.add_argument("--resume", action="store_true")
    p.add_argument("--skip-existing", action="store_true")
    p.add_argument("--only", default="", help="Comma list: source_pair,sampling,geometry,boundary,summary")
    p.add_argument("--workers", type=int, default=0, help="Parallel worker processes. 0 means auto from CPU/memory targets.")
    p.add_argument("--cpu-target", type=float, default=0.70, help="Auto workers target fraction of logical CPUs.")
    p.add_argument("--memory-target", type=float, default=0.70, help="Auto workers target fraction of physical memory.")
    p.add_argument("--worker-memory-gb", type=float, default=1.5, help="Estimated peak memory per worker for auto sizing.")
    p.add_argument("--max-samples", type=int, default=0, help="Debug/probe only: limit manifest rows after manifest creation.")
    p.add_argument("--log-level", default="INFO")
    return p


def main() -> int:
    run_all(build_parser().parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
