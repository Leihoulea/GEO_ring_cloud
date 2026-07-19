# -*- coding: utf-8 -*-
"""Cross-stage workflow support for full-pixel EPIC/GEO diagnostics.

The helpers are shared by Stage 09d, Stage 09e, and Stage 09f. They do not
modify Stage 05/06 production outputs.
"""
from __future__ import annotations

import csv
import json
import math
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ..lineage import code_commit
from ..paths import PROJECT_ROOT, RUNS_ROOT
from . import full_pixel


PROJECT_ID = "geo_ring_cloud"
COMPONENT_ROLE = "diagnostics_workflow"
DEFAULT_STAGE09D_DIR = RUNS_ROOT / "stage09d_full_pixel_diagnostics_202403"
SOURCE_ID_TO_NAME = {1: "GOES-16", 2: "GOES-18", 3: "FY4B", 4: "Himawari-9", 5: "Meteosat-0deg", 6: "Meteosat-IODC"}
SOURCE_NAME_TO_ID = {v: k for k, v in SOURCE_ID_TO_NAME.items()}
SOURCE_FAMILY = {
    "GOES-16": "GOES",
    "GOES-18": "GOES",
    "FY4B": "EastAsia",
    "Himawari-9": "EastAsia",
    "Meteosat-0deg": "Meteosat",
    "Meteosat-IODC": "Meteosat",
}
SOURCE_LABEL_SAFE = {
    "GOES-16": "GOES16",
    "GOES-18": "GOES18",
    "FY4B": "FY4B",
    "Himawari-9": "Himawari9",
    "Meteosat-0deg": "Meteosat0deg",
    "Meteosat-IODC": "MeteosatIODC",
}

ABS_LAT_BINS = [("0-20", 0, 20), ("20-40", 20, 40), ("40-60", 40, 60), ("60-70", 60, 70), ("70-80", 70, 80), (">=80", 80, 91)]
EPIC_VZA_BINS = [("0-20", 0, 20), ("20-40", 20, 40), ("40-60", 40, 60), ("60-70", 60, 70), (">=70", 70, 181)]
SZA_BINS = [("0-30", 0, 30), ("30-50", 30, 50), ("50-70", 50, 70), ("70-80", 70, 80), (">=80", 80, 181)]
VALID_COUNT_BINS = [("1", 1, 2), ("2", 2, 3), ("3", 3, 4), (">=4", 4, 100)]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def ensure_dirs(root: Path, names: list[str]) -> None:
    for name in names:
        (root / name).mkdir(parents=True, exist_ok=True)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        keys: list[str] = []
        seen = set()
        for row in rows:
            for key in row.keys():
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
        fields = keys
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def safe_float(value: Any, default: float = math.nan) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def bool_series(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes", "y"}


def mean(values: list[float]) -> float:
    vals = [v for v in values if math.isfinite(v)]
    return sum(vals) / len(vals) if vals else math.nan


def weighted_mean(rows: list[dict[str, Any]], value: str, weight: str = "n_valid") -> float:
    num = 0.0
    den = 0.0
    for row in rows:
        v = safe_float(row.get(value))
        w = safe_float(row.get(weight), 0.0)
        if math.isfinite(v) and math.isfinite(w) and w > 0:
            num += v * w
            den += w
    return num / den if den > 0 else math.nan


def finite_any(arr: np.ndarray) -> bool:
    return bool(np.any(np.isfinite(arr)))


def selected_source_name(source_id: Any) -> str:
    try:
        sid = int(source_id)
    except Exception:
        return "missing"
    return SOURCE_ID_TO_NAME.get(sid, f"unknown_{sid}")


def selected_source_array(selected_source: np.ndarray) -> np.ndarray:
    out = np.full(selected_source.shape, "missing", dtype=object)
    for sid, name in SOURCE_ID_TO_NAME.items():
        out[selected_source == sid] = name
    return out


def family_array(selected_names: np.ndarray) -> np.ndarray:
    out = np.full(selected_names.shape, "missing", dtype=object)
    for name, family in SOURCE_FAMILY.items():
        out[selected_names == name] = family
    return out


def classify(values: np.ndarray, bins: list[tuple[str, float, float]]) -> np.ndarray:
    return full_pixel.classify_array(values, bins)


def load_manifest(stage09d_dir: Path) -> list[dict[str, Any]]:
    manifest_path = stage09d_dir / "00_sample_manifest" / "stage09d_53_sample_manifest.csv"
    rows = read_csv(manifest_path)
    if not rows:
        raise RuntimeError(f"missing Stage 09d manifest: {manifest_path}")
    return rows


def load_angle_on_epic(ctx: dict[str, Any], source: str, variable_keyword: str) -> tuple[np.ndarray, bool, str]:
    """Sample a reprojected GEO angle layer on EPIC pixels if available."""
    tag = ctx["tag"]
    root = ctx["run_dir"] / "reprojected_grid" / source
    hits = sorted(root.glob(f"*{variable_keyword}*{tag}.npz"))
    if not hits:
        return np.full(ctx["epic"]["lat"].shape, np.nan, dtype=np.float32), False, f"missing_{source}_{variable_keyword}"
    arrs = full_pixel.load_npz(hits[0])
    data = np.asarray(arrs["data"], dtype=np.float32)
    valid = np.asarray(arrs.get("valid_mask", np.isfinite(data))).astype(bool)
    sampled, sampled_valid = full_pixel.sample_grid(data, valid, ctx["epic"]["lat"], ctx["epic"]["lon"], ctx["grid"])
    sampled[~sampled_valid] = np.nan
    return sampled, True, str(hits[0])


def selected_geo_vza(ctx: dict[str, Any]) -> tuple[np.ndarray, list[str]]:
    """Build a selected-source GEO VZA array in degrees, where available."""
    out = np.full(ctx["epic"]["lat"].shape, np.nan, dtype=np.float32)
    warnings: list[str] = []
    for source, sid in SOURCE_NAME_TO_ID.items():
        arr, ok, note = load_angle_on_epic(ctx, source, "sensor_zenith_angle")
        if not ok:
            warnings.append(note)
            continue
        out[ctx["selected_source"] == sid] = arr[ctx["selected_source"] == sid]
    return out, warnings


def add_condition_if_available(base: np.ndarray, arr: np.ndarray, threshold: float) -> tuple[np.ndarray, str]:
    if finite_any(arr):
        return base & np.isfinite(arr) & (arr < threshold), "applied"
    return base, "missing_not_applied"


def scene_boundary(ctx: dict[str, Any], policy_name: str = "A_inclusive_binary") -> dict[str, np.ndarray]:
    frac, boundary = full_pixel.make_boundary(ctx["fused_on_epic"], ctx["fused_on_valid"], policy_name)
    scene = np.full(boundary.shape, "broken_cloud", dtype=object)
    scene[frac <= 0.1] = "homogeneous_clear"
    scene[frac >= 0.9] = "homogeneous_cloud"
    boundary_class = np.where(boundary, "near_boundary_1cell", "non_boundary")
    return {"local_cloud_fraction_3x3": frac, "boundary_bool": boundary, "boundary_class": boundary_class, "scene_type": scene}


def policy_classes(ctx: dict[str, Any], policy_name: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    policy = full_pixel.POLICIES[policy_name]
    epic_cls, epic_pv = full_pixel.apply_policy(ctx["epic"]["cloud_mask"], policy["epic"])
    geo_cls, geo_pv = full_pixel.apply_policy(ctx["fused_on_epic"], policy["geo"])
    return epic_cls, epic_pv, geo_cls, geo_pv


def metric_row(epic_cls: np.ndarray, geo_cls: np.ndarray, valid: np.ndarray, policy_name: str) -> dict[str, Any]:
    policy = full_pixel.POLICIES[policy_name]
    row = full_pixel.binary_metrics(epic_cls, geo_cls, valid, policy["positive"])
    row["policy"] = policy_name
    if policy_name == "C_uncertainty_aware_3class":
        row.update(multiclass_c_metrics(epic_cls, geo_cls, valid))
    return row


def multiclass_c_metrics(epic_cls: np.ndarray, geo_cls: np.ndarray, valid: np.ndarray) -> dict[str, Any]:
    labels = [0, 1, 2]
    n = int(np.count_nonzero(valid))
    if n == 0:
        return {
            "C_macro_f1": math.nan,
            "C_per_class_recall_clear": math.nan,
            "C_per_class_recall_uncertain": math.nan,
            "C_per_class_recall_cloud": math.nan,
            "C_uncertain_confusion_fraction": math.nan,
        }
    e = epic_cls[valid]
    g = geo_cls[valid]
    f1s = []
    recalls = {}
    for label in labels:
        ep = e == label
        gp = g == label
        tp = int(np.count_nonzero(ep & gp))
        fp = int(np.count_nonzero((~ep) & gp))
        fn = int(np.count_nonzero(ep & (~gp)))
        precision = tp / max(tp + fp, 1)
        recall = tp / max(tp + fn, 1)
        f1s.append(2 * precision * recall / max(precision + recall, 1e-12))
        recalls[label] = recall
    uncertain = (e == 1) | (g == 1)
    uncertain_conf = uncertain & (e != g)
    return {
        "C_macro_f1": float(np.mean(f1s)),
        "C_per_class_recall_clear": recalls[0],
        "C_per_class_recall_uncertain": recalls[1],
        "C_per_class_recall_cloud": recalls[2],
        "C_uncertain_confusion_fraction": int(np.count_nonzero(uncertain_conf)) / n,
    }


def base_valid_mask(ctx: dict[str, Any], policy_name: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    epic_cls, epic_pv, geo_cls, geo_pv = policy_classes(ctx, policy_name)
    valid_earth = np.isin(ctx["epic"]["cloud_mask"], [1, 2, 3, 4])
    base = valid_earth & epic_pv & ctx["fused_on_valid"] & geo_pv
    return base, epic_cls, geo_cls


def source_samples(ctx: dict[str, Any]) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], list[str]]:
    classes: dict[str, np.ndarray] = {}
    valids: dict[str, np.ndarray] = {}
    warnings: list[str] = []
    for source in full_pixel.SOURCES:
        pref = full_pixel.find_prefusion(ctx["run_dir"], source, ctx["tag"])
        if pref is None:
            warnings.append(f"missing_prefusion_{source}")
            continue
        try:
            arrs = full_pixel.load_npz(pref)
            raw_valid = np.asarray(arrs.get("fusion_valid_mask", arrs.get("valid_mask", np.isfinite(arrs["data"])))).astype(bool)
            raw_on, raw_valid_on = full_pixel.sample_grid(arrs["data"], raw_valid, ctx["epic"]["lat"], ctx["epic"]["lon"], ctx["grid"])
            std = full_pixel.source_to_standard(source, raw_on)
            classes[source] = std
            valids[source] = raw_valid_on & (std >= 0)
        except Exception as exc:
            warnings.append(f"source_load_failed_{source}:{exc}")
    return classes, valids, warnings


def apply_geo_policy(std: np.ndarray, valid: np.ndarray, policy_name: str) -> tuple[np.ndarray, np.ndarray]:
    policy = full_pixel.POLICIES[policy_name]
    cls, pv = full_pixel.apply_policy(std, policy["geo"])
    return cls, valid & pv


def summarize_metric_rows(rows: list[dict[str, Any]], group_fields: list[str], weight: str = "n_valid") -> list[dict[str, Any]]:
    buckets: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[tuple(row.get(f, "") for f in group_fields)].append(row)
    metrics = [
        "agreement",
        "precision_cloud",
        "recall_cloud",
        "f1_cloud",
        "iou_cloud",
        "balanced_accuracy",
        "cloud_fraction_epic",
        "cloud_fraction_georing",
        "cloud_fraction_source",
        "cloud_fraction_bias",
        "C_macro_f1",
        "C_per_class_recall_clear",
        "C_per_class_recall_uncertain",
        "C_per_class_recall_cloud",
        "C_uncertain_confusion_fraction",
        "current_selected_agreement",
        "best_available_agreement",
        "selection_regret_agreement",
        "current_selected_f1",
        "best_available_f1",
        "selection_regret_f1",
        "current_selected_iou",
        "best_available_iou",
        "selection_regret_iou",
        "current_selected_agreement_rank",
        "current_selected_f1_rank",
        "current_selected_iou_rank",
        "selected_is_best_fraction",
        "num_available_sources_mean",
        "current_selected_source_mode_fraction",
    ]
    out = []
    for key, vals in sorted(buckets.items(), key=lambda kv: kv[0]):
        item = {field: value for field, value in zip(group_fields, key)}
        item["row_count"] = len(vals)
        item["sample_count"] = len({v.get("sample_id", "") for v in vals if v.get("sample_id", "")})
        item[f"{weight}_total"] = sum(safe_float(v.get(weight), 0.0) for v in vals)
        for metric in metrics:
            if any(metric in v for v in vals):
                item[f"{metric}_weighted"] = weighted_mean(vals, metric, weight)
                item[f"{metric}_mean"] = mean([safe_float(v.get(metric)) for v in vals])
        for c in ["TP", "TN", "FP", "FN"]:
            item[c] = sum(safe_float(v.get(c), 0.0) for v in vals)
        out.append(item)
    return out


def metric_fields(extra: list[str] | None = None) -> list[str]:
    base = [
        "sample_id",
        "mask_name",
        "pixel_group",
        "policy",
        "n_valid",
        "n_valid_retention_ratio",
        "agreement",
        "precision_cloud",
        "recall_cloud",
        "f1_cloud",
        "iou_cloud",
        "balanced_accuracy",
        "cloud_fraction_epic",
        "cloud_fraction_georing",
        "cloud_fraction_source",
        "cloud_fraction_bias",
        "TP",
        "TN",
        "FP",
        "FN",
        "C_macro_f1",
        "C_per_class_recall_clear",
        "C_per_class_recall_uncertain",
        "C_per_class_recall_cloud",
        "C_uncertain_confusion_fraction",
        "candidate_group",
        "dominant_source",
        "mean_abs_lat",
        "mean_epic_vza",
        "mean_sza",
        "boundary_fraction",
        "broken_cloud_fraction",
    ]
    if extra:
        return extra + [x for x in base if x not in extra]
    return base


def nanmean(arr: np.ndarray, valid: np.ndarray) -> float:
    if not np.any(valid):
        return math.nan
    vals = arr[valid]
    return float(np.nanmean(vals)) if np.any(np.isfinite(vals)) else math.nan


def fraction(mask: np.ndarray, valid: np.ndarray) -> float:
    n = int(np.count_nonzero(valid))
    return int(np.count_nonzero(mask & valid)) / n if n else math.nan


def describe_valid_context(ctx: dict[str, Any], valid: np.ndarray, scene: dict[str, np.ndarray]) -> dict[str, Any]:
    return {
        "mean_abs_lat": nanmean(np.abs(ctx["epic"]["lat"]), valid),
        "mean_epic_vza": nanmean(ctx["epic"]["epic_vza"], valid),
        "mean_sza": nanmean(ctx["epic"]["sza"], valid),
        "boundary_fraction": fraction(scene["boundary_bool"], valid),
        "broken_cloud_fraction": fraction(scene["scene_type"] == "broken_cloud", valid),
    }


def add_plot(plot_index: list[dict[str, str]], path: Path, source_csv: Path, description: str) -> None:
    plot_index.append({"plot_path": str(path), "source_csv": str(source_csv), "description": description, "created_time_utc": utc_now()})


def bar_plot(path: Path, labels: list[str], values: list[float], title: str, ylabel: str, source_csv: Path, plot_index: list[dict[str, str]], ylim: tuple[float, float] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(max(8, 0.55 * len(labels)), 4.8), constrained_layout=True)
    ax.bar(range(len(labels)), values, color="#386fa4")
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=35, ha="right")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(axis="y", alpha=0.25)
    if ylim:
        ax.set_ylim(*ylim)
    fig.savefig(path, dpi=170)
    plt.close(fig)
    add_plot(plot_index, path, source_csv, title)


def grouped_bar(path: Path, df: pd.DataFrame, xcol: str, value_cols: list[str], title: str, ylabel: str, source_csv: Path, plot_index: list[dict[str, str]], ylim: tuple[float, float] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    labels = df[xcol].astype(str).tolist()
    x = np.arange(len(labels))
    width = 0.8 / max(len(value_cols), 1)
    fig, ax = plt.subplots(figsize=(max(9, 0.75 * len(labels)), 5.0), constrained_layout=True)
    colors = ["#386fa4", "#2a9d8f", "#e76f51", "#7a5195", "#8ab17d"]
    for idx, col in enumerate(value_cols):
        ax.bar(x + (idx - (len(value_cols) - 1) / 2) * width, pd.to_numeric(df[col], errors="coerce"), width=width, label=col, color=colors[idx % len(colors)])
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=35, ha="right")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend()
    ax.grid(axis="y", alpha=0.25)
    if ylim:
        ax.set_ylim(*ylim)
    fig.savefig(path, dpi=170)
    plt.close(fig)
    add_plot(plot_index, path, source_csv, title)


def heatmap(path: Path, mat: pd.DataFrame, title: str, source_csv: Path, plot_index: list[dict[str, str]], vmin: float | None = None, vmax: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(max(7, 0.6 * len(mat.columns)), max(5, 0.38 * len(mat.index))), constrained_layout=True)
    data = mat.astype(float).to_numpy()
    im = ax.imshow(data, cmap="viridis", vmin=vmin, vmax=vmax, aspect="auto")
    ax.set_xticks(np.arange(len(mat.columns)))
    ax.set_xticklabels(mat.columns, rotation=35, ha="right")
    ax.set_yticks(np.arange(len(mat.index)))
    ax.set_yticklabels(mat.index)
    ax.set_title(title)
    for i in range(data.shape[0]):
        for j in range(data.shape[1]):
            if np.isfinite(data[i, j]):
                ax.text(j, i, f"{data[i, j]:.2f}", ha="center", va="center", fontsize=8, color="white" if data[i, j] < np.nanmean(data) else "black")
    fig.colorbar(im, ax=ax, fraction=0.035)
    fig.savefig(path, dpi=170)
    plt.close(fig)
    add_plot(plot_index, path, source_csv, title)


def md_table(rows: list[dict[str, Any]], columns: list[str], limit: int = 20) -> str:
    if not rows:
        return "_No rows._"
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
    for row in rows[:limit]:
        vals = []
        for col in columns:
            value = row.get(col, "")
            if isinstance(value, float):
                vals.append(f"{value:.3f}" if math.isfinite(value) else "NA")
            else:
                vals.append(str(value).replace("|", "\\|"))
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines)


def write_run_manifest(
    path: Path,
    *,
    canonical_stage_id: str,
    script_path: Path,
    input_paths: list[Path],
    output_paths: list[Path],
    filters: list[str],
    unit_conversions: list[dict[str, Any]],
    row_counts: dict[str, int],
    warnings: list[dict[str, Any]],
) -> None:
    timestamp = utc_now()
    parameter_summary = {
        "filters": filters,
        "unit_conversions": unit_conversions,
        "row_counts": row_counts,
    }
    write_json(
        path,
        {
            "project_id": PROJECT_ID,
            "canonical_stage_id": canonical_stage_id,
            "stage_id": canonical_stage_id,
            "component_role": "stage_run_artifact",
            "timestamp": timestamp,
            "run_time_utc": timestamp,
            "code_commit": code_commit(PROJECT_ROOT),
            "generating_script": str(script_path),
            "script_path": str(script_path),
            "input_paths": [str(p) for p in input_paths],
            "output_paths": [str(p) for p in output_paths],
            "parameter_summary": parameter_summary,
            "filters": filters,
            "unit_conversions": unit_conversions,
            "row_counts": row_counts,
            "warnings": warnings,
        },
    )


def sample_safe(row: dict[str, Any], fn: Any) -> tuple[Any | None, list[dict[str, Any]]]:
    try:
        return fn(row), []
    except Exception as exc:
        return None, [{"sample_id": row.get("sample_id", ""), "warning": str(exc), "traceback": traceback.format_exc()}]
