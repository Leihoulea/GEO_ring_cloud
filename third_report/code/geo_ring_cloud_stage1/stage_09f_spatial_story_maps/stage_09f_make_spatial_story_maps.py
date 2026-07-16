# -*- coding: utf-8 -*-
"""Stage 09F spatial story maps for GEO-ring vs EPIC diagnostics.

Diagnostic-only plotting stage.  This script reads existing Stage 09D/09E
March 2024 products, reuses Stage 09D EPIC/GEO sampling helpers, and writes
traceable map source CSVs plus PNG/SVG/PDF figures.  It does not rerun fusion,
does not modify Stage 05/06 production logic, and does not use EPIC as an
absolute truth label.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.patches import Patch
import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import path_config  # noqa: E402
from stage_09d_diagnostic_common import (  # noqa: E402
    SOURCE_FAMILY,
    SOURCE_ID_TO_NAME,
    SOURCE_NAME_TO_ID,
    base_valid_mask,
    bool_series,
    d09d,
    family_array,
    load_manifest,
    scene_boundary,
    selected_source_array,
    source_samples,
)

STAGE_ID = "stage_09f"
PROJECT_ID = "geo_ring_cloud"
RUN_ID = "stage_09f_spatial_story_maps_202403"
DEFAULT_STAGE09D_DIR = path_config.RUNS_ROOT / "stage09d_full_pixel_diagnostics_202403"
DEFAULT_VIS_DIR = path_config.RUNS_ROOT / "stage09d_geo_visible_controlled_metrics_202403"
DEFAULT_OUT = path_config.RUNS_ROOT / RUN_ID

POLICY = "A_inclusive_binary"
MASK_FOR_PAIR = "VIS-3_lat60_visible"
SOURCES = ["FY4B", "Himawari-9", "GOES-16", "GOES-18", "Meteosat-0deg", "Meteosat-IODC"]
PAIR_LIST = [
    ("Meteosat-0deg", "Meteosat-IODC"),
    ("FY4B", "Meteosat-IODC"),
    ("Meteosat-0deg", "GOES-16"),
    ("GOES-16", "GOES-18"),
]

CLASS_CMAP = ListedColormap(["#D0D0D0", "#F2E8C9", "#4E79A7"])
CLASS_NORM = BoundaryNorm([-0.5, 0.5, 1.5, 2.5], CLASS_CMAP.N)
MISMATCH_CMAP = ListedColormap(["#D0D0D0", "#E8E8E8", "#6BAED6", "#F2B84B", "#D95F5F"])
MISMATCH_NORM = BoundaryNorm([-0.5, 0.5, 1.5, 2.5, 3.5, 4.5], MISMATCH_CMAP.N)
FAMILY_CMAP = ListedColormap(["#D0D0D0", "#3B6FB6", "#2AA198", "#B65A5A"])
FAMILY_NORM = BoundaryNorm([-0.5, 0.5, 1.5, 2.5, 3.5], FAMILY_CMAP.N)
COUNT_CMAP = ListedColormap(["#D0D0D0", "#E8F1FA", "#A7C7E7", "#4E79A7", "#1F4E79"])
COUNT_NORM = BoundaryNorm([-0.5, 0.5, 1.5, 2.5, 3.5, 4.5], COUNT_CMAP.N)
SCENE_CMAP = ListedColormap(["#D0D0D0", "#E8E8E8", "#8EC07C", "#C95F5F", "#9E77B5"])
SCENE_NORM = BoundaryNorm([-0.5, 0.5, 1.5, 2.5, 3.5, 4.5], SCENE_CMAP.N)
BOOL_CMAP = ListedColormap(["#D0D0D0", "#F4F4F4", "#2AA198"])
BOOL_NORM = BoundaryNorm([-0.5, 0.5, 1.5, 2.5], BOOL_CMAP.N)
PAIR_CMAP = ListedColormap(["#D0D0D0", "#E8E8E8", "#3B6FB6", "#2AA198", "#D95F5F", "#F2B84B"])
PAIR_NORM = BoundaryNorm([-0.5, 0.5, 1.5, 2.5, 3.5, 4.5, 5.5], PAIR_CMAP.N)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def apply_style() -> None:
    plt.rcParams.update(
        {
            "figure.dpi": 150,
            "savefig.dpi": 300,
            "font.family": "sans-serif",
            "font.sans-serif": ["Arial", "DejaVu Sans", "Liberation Sans"],
            "font.size": 7.0,
            "axes.titlesize": 8.0,
            "axes.labelsize": 7.0,
            "xtick.labelsize": 6.0,
            "ytick.labelsize": 6.0,
            "legend.fontsize": 6.0,
            "axes.linewidth": 0.7,
            "svg.fonttype": "none",
            "pdf.fonttype": 42,
            "pdf.use14corefonts": False,
            "axes.spines.top": False,
            "axes.spines.right": False,
        }
    )


def ensure_dirs(root: Path) -> dict[str, Path]:
    dirs = {
        "figures": root / "figures",
        "source_data": root / "source_data",
        "reports": root / "reports",
        "logs": root / "logs",
    }
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)
    return dirs


def read_csv(path: Path, warnings: list[dict[str, Any]], label: str) -> pd.DataFrame:
    if not path.exists():
        warnings.append({"level": "warning", "source": label, "message": f"missing CSV: {path}"})
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception as exc:
        warnings.append({"level": "warning", "source": label, "message": f"failed to read CSV: {exc}"})
        return pd.DataFrame()


def save_figure(fig: plt.Figure, dirs: dict[str, Path], figure_id: str) -> dict[str, str]:
    base = dirs["figures"] / f"{RUN_ID}_{figure_id}"
    fig.savefig(base.with_suffix(".png"), bbox_inches="tight", dpi=240)
    fig.savefig(base.with_suffix(".svg"), bbox_inches="tight")
    fig.savefig(base.with_suffix(".pdf"), bbox_inches="tight")
    plt.close(fig)
    return {ext: str(base.with_suffix(f".{ext}")) for ext in ["png", "svg", "pdf"]}


def source_path(dirs: dict[str, Path], figure_id: str) -> Path:
    return dirs["source_data"] / f"{RUN_ID}_{figure_id}_source.csv"


def write_source(df: pd.DataFrame, path: Path, warnings: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if df.empty:
        warnings.append({"level": "warning", "source": path.name, "message": "source CSV has zero rows"})
    df.to_csv(path, index=False, encoding="utf-8-sig")


def short_sample(sample_id: str) -> str:
    return f"{sample_id[:4]}-{sample_id[4:6]}-{sample_id[6:8]} {sample_id[9:11]}:{sample_id[11:13]}"


def short_reason(reason: str) -> str:
    text = str(reason)
    if "Meteosat" in text and "agreement" in text:
        return "low Met\nagreement"
    if "boundary" in text:
        return "high boundary"
    if "source-pair" in text:
        return "source-pair\ndisagreement"
    if "valid_source_count" in text:
        return "valid_count>=4\nfocus"
    if "high-agreement" in text:
        return "high-agreement\ncontrol"
    return text[:28]


def source_family_code(names: np.ndarray) -> np.ndarray:
    family = family_array(names)
    out = np.zeros(family.shape, dtype=np.int16)
    out[family == "GOES"] = 1
    out[family == "EastAsia"] = 2
    out[family == "Meteosat"] = 3
    return out


def class_label_array(cls: np.ndarray, valid: np.ndarray) -> np.ndarray:
    out = np.zeros(cls.shape, dtype=np.int16)
    out[valid & (cls == 0)] = 1
    out[valid & (cls == 1)] = 2
    return out


def count_code(valid_count: np.ndarray, valid: np.ndarray) -> np.ndarray:
    out = np.zeros(valid_count.shape, dtype=np.int16)
    out[valid & (valid_count == 1)] = 1
    out[valid & (valid_count == 2)] = 2
    out[valid & (valid_count == 3)] = 3
    out[valid & (valid_count >= 4)] = 4
    return out


def mismatch_code(epic_cls: np.ndarray, geo_cls: np.ndarray, valid: np.ndarray) -> np.ndarray:
    out = np.zeros(epic_cls.shape, dtype=np.int16)
    out[valid & (epic_cls == geo_cls) & (epic_cls == 0)] = 1
    out[valid & (epic_cls == geo_cls) & (epic_cls == 1)] = 2
    out[valid & (epic_cls == 1) & (geo_cls == 0)] = 3
    out[valid & (epic_cls == 0) & (geo_cls == 1)] = 4
    return out


def scene_code(scene: dict[str, np.ndarray], valid: np.ndarray) -> np.ndarray:
    out = np.zeros(valid.shape, dtype=np.int16)
    out[valid & (scene["boundary_class"] == "non_boundary") & (scene["scene_type"] == "homogeneous_clear")] = 1
    out[valid & (scene["boundary_class"] == "non_boundary") & (scene["scene_type"] == "homogeneous_cloud")] = 2
    out[valid & (scene["boundary_class"] != "non_boundary")] = 3
    out[valid & (scene["scene_type"] == "broken_cloud")] = 4
    return out


def orient(arr: np.ndarray, lat_ref: np.ndarray) -> np.ndarray:
    top = float(np.nanmean(lat_ref[: max(1, lat_ref.shape[0] // 10), :]))
    bottom = float(np.nanmean(lat_ref[-max(1, lat_ref.shape[0] // 10) :, :]))
    return np.flipud(arr) if math.isfinite(top) and math.isfinite(bottom) and top < bottom else arr


def ds(arr: np.ndarray, stride: int) -> np.ndarray:
    return arr[::stride, ::stride]


def image_panel(ax: plt.Axes, arr: np.ndarray, title: str, cmap: ListedColormap, norm: BoundaryNorm) -> None:
    ax.imshow(arr, cmap=cmap, norm=norm, interpolation="nearest", origin="upper")
    ax.set_title(title, pad=2)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(True)
        spine.set_linewidth(0.35)
        spine.set_edgecolor("#BBBBBB")


def add_legend(ax: plt.Axes, labels: list[tuple[int, str, str]], ncol: int = 1) -> None:
    handles = [Patch(facecolor=color, edgecolor="none", label=label) for _, label, color in labels]
    ax.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, -0.02), ncol=ncol, frameon=False)


def context_cache_get(cache: dict[str, dict[str, Any]], row: dict[str, Any]) -> dict[str, Any]:
    sid = row["sample_id"]
    if sid not in cache:
        cache[sid] = d09d.sample_context(row)
    return cache[sid]


def sample_metrics_for_context(ctx: dict[str, Any]) -> dict[str, Any]:
    base, epic_cls, geo_cls = base_valid_mask(ctx, POLICY)
    scene = scene_boundary(ctx, POLICY)
    selected_names = selected_source_array(ctx["selected_source"])
    return {
        "base": base,
        "epic_cls": epic_cls,
        "geo_cls": geo_cls,
        "scene": scene,
        "selected_names": selected_names,
        "family_code": source_family_code(selected_names),
        "epic_code": class_label_array(epic_cls, base),
        "geo_code": class_label_array(geo_cls, base),
        "mismatch_code": mismatch_code(epic_cls, geo_cls, base),
        "count_code": count_code(ctx["valid_count"], base),
        "scene_code": scene_code(scene, base),
    }


def choose_representative_samples(
    manifest: list[dict[str, Any]],
    policy_sample: pd.DataFrame,
    pair_metrics: pd.DataFrame,
    strata: pd.DataFrame,
    max_samples: int,
    warnings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {r["sample_id"]: r for r in manifest}
    choices: list[dict[str, str]] = []

    p = policy_sample.copy()
    if not p.empty:
        p = p[(p["policy"] == POLICY) & (p["mask_name"] == "VIS-0_baseline_current")]
        met = p[p["dominant_source"].astype(str).str.contains("Meteosat", na=False) | p["candidate_group"].astype(str).str.contains("METEOSAT", na=False)]
        if not met.empty:
            row = met.sort_values("agreement", ascending=True).iloc[0]
            choices.append({"sample_id": str(row["sample_id"]), "reason": "low Meteosat-related agreement"})
        boundary = p.copy()
        if "boundary_fraction" in boundary.columns and not boundary.empty:
            row = boundary.sort_values("boundary_fraction", ascending=False).iloc[0]
            choices.append({"sample_id": str(row["sample_id"]), "reason": "high boundary/broken-cloud fraction"})
        good = p.sort_values("agreement", ascending=False).iloc[0] if not p.empty else None
        if good is not None:
            choices.append({"sample_id": str(good["sample_id"]), "reason": "high-agreement control sample"})

    if not pair_metrics.empty:
        q = pair_metrics[
            (pair_metrics["policy"] == POLICY)
            & (pair_metrics["mask_name"].isin([MASK_FOR_PAIR, "VIS-0_baseline_current"]))
            & (pd.to_numeric(pair_metrics["n_overlap_valid"], errors="coerce") > 10000)
        ].copy()
        if not q.empty:
            q["score"] = pd.to_numeric(q["source_disagreement_fraction"], errors="coerce") * np.log1p(
                pd.to_numeric(q["n_overlap_valid"], errors="coerce")
            )
            row = q.sort_values("score", ascending=False).iloc[0]
            choices.append({"sample_id": str(row["sample_id"]), "reason": f"high source-pair disagreement: {row['source_A']} vs {row['source_B']}"})

    if not strata.empty:
        s = strata[
            (strata["policy"] == POLICY)
            & (strata["mask_name"] == "VIS-0_baseline_current")
            & (strata["strata_dimension"] == "valid_source_count_bin")
            & (strata["strata_value"].astype(str) == ">=4")
        ].copy()
        if not s.empty:
            s["score"] = (1.0 - pd.to_numeric(s["agreement"], errors="coerce")) * np.log1p(pd.to_numeric(s["n_valid"], errors="coerce"))
            row = s.sort_values("score", ascending=False).iloc[0]
            choices.append({"sample_id": str(row["sample_id"]), "reason": "valid_source_count>=4 low-agreement focus"})

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in choices:
        sid = item["sample_id"]
        if sid in by_id and sid not in seen:
            row = dict(by_id[sid])
            row["stage_09f_selection_reason"] = item["reason"]
            selected.append(row)
            seen.add(sid)
        if len(selected) >= max_samples:
            break
    if len(selected) < min(max_samples, 3):
        warnings.append({"level": "warning", "source": "sample_selection", "message": "needed fallback representative samples"})
        for row in manifest:
            sid = row["sample_id"]
            if sid not in seen:
                item = dict(row)
                item["stage_09f_selection_reason"] = "fallback runnable sample"
                selected.append(item)
                seen.add(sid)
            if len(selected) >= max_samples:
                break
    return selected


def flatten_sample_source(sample_id: str, lat: np.ndarray, lon: np.ndarray, arrays: dict[str, np.ndarray], stride: int) -> pd.DataFrame:
    lat_d = ds(lat, stride)
    lon_d = ds(lon, stride)
    rows = {
        "sample_id": np.full(lat_d.size, sample_id, dtype=object),
        "display_row": np.repeat(np.arange(lat_d.shape[0]), lat_d.shape[1]),
        "display_col": np.tile(np.arange(lat_d.shape[1]), lat_d.shape[0]),
        "latitude_deg": lat_d.ravel(),
        "longitude_deg": lon_d.ravel(),
        "plot_stride": np.full(lat_d.size, stride, dtype=np.int16),
    }
    for name, arr in arrays.items():
        rows[name] = ds(arr, stride).ravel()
    return pd.DataFrame(rows)


def figure1_representative_disk(
    dirs: dict[str, Path],
    samples: list[dict[str, Any]],
    cache: dict[str, dict[str, Any]],
    stride: int,
    warnings: list[dict[str, Any]],
) -> tuple[dict[str, str], Path, list[dict[str, Any]]]:
    n = len(samples)
    fig, axes = plt.subplots(n, 6, figsize=(9.0, max(1.35 * n, 4.8)), constrained_layout=True)
    if n == 1:
        axes = np.asarray([axes])
    source_frames = []
    summary_rows = []
    col_titles = ["EPIC", "GEO-ring", "Mismatch", "Selected family", "valid count", "Boundary/scene"]
    for i, row in enumerate(samples):
        try:
            ctx = context_cache_get(cache, row)
            m = sample_metrics_for_context(ctx)
            lat = orient(ctx["epic"]["lat"], ctx["epic"]["lat"])
            lon = orient(ctx["epic"]["lon"], ctx["epic"]["lat"])
            arrays = {
                "epic_policy_a_class_code": orient(m["epic_code"], ctx["epic"]["lat"]),
                "georing_policy_a_class_code": orient(m["geo_code"], ctx["epic"]["lat"]),
                "mismatch_category_code": orient(m["mismatch_code"], ctx["epic"]["lat"]),
                "selected_family_code": orient(m["family_code"], ctx["epic"]["lat"]),
                "valid_source_count_code": orient(m["count_code"], ctx["epic"]["lat"]),
                "boundary_scene_code": orient(m["scene_code"], ctx["epic"]["lat"]),
            }
            source_frames.append(flatten_sample_source(row["sample_id"], lat, lon, arrays, stride))
            total = int(np.count_nonzero(m["base"]))
            disagree = int(np.count_nonzero(m["base"] & (m["epic_cls"] != m["geo_cls"])))
            summary_rows.append(
                {
                    "sample_id": row["sample_id"],
                    "selection_reason": row.get("stage_09f_selection_reason", ""),
                    "n_valid_policy_a": total,
                    "n_disagreement_policy_a": disagree,
                    "agreement_policy_a": 1.0 - disagree / total if total else math.nan,
                    "boundary_fraction": float(np.mean(m["scene"]["boundary_bool"][m["base"]])) if total else math.nan,
                    "broken_cloud_fraction": float(np.mean((m["scene"]["scene_type"] == "broken_cloud")[m["base"]])) if total else math.nan,
                    "meteosat_selected_fraction": float(np.mean(m["family_code"][m["base"]] == 3)) if total else math.nan,
                    "valid_source_count_ge4_fraction": float(np.mean(ctx["valid_count"][m["base"]] >= 4)) if total else math.nan,
                }
            )
            plot_arrays = [
                ("EPIC", arrays["epic_policy_a_class_code"], CLASS_CMAP, CLASS_NORM),
                ("GEO-ring", arrays["georing_policy_a_class_code"], CLASS_CMAP, CLASS_NORM),
                ("Mismatch", arrays["mismatch_category_code"], MISMATCH_CMAP, MISMATCH_NORM),
                ("Selected family", arrays["selected_family_code"], FAMILY_CMAP, FAMILY_NORM),
                ("valid count", arrays["valid_source_count_code"], COUNT_CMAP, COUNT_NORM),
                ("Boundary/scene", arrays["boundary_scene_code"], SCENE_CMAP, SCENE_NORM),
            ]
            for j, (title, arr, cmap, norm) in enumerate(plot_arrays):
                image_panel(axes[i, j], ds(arr, stride), title if i == 0 else "", cmap, norm)
                if j == 0:
                    axes[i, j].text(
                        -0.06,
                        0.50,
                        f"{short_sample(row['sample_id'])}\n{short_reason(row.get('stage_09f_selection_reason', ''))}",
                        transform=axes[i, j].transAxes,
                        ha="right",
                        va="center",
                        fontsize=6.0,
                        linespacing=1.1,
                    )
        except Exception as exc:
            warnings.append({"level": "warning", "source": "figure1", "sample_id": row.get("sample_id", ""), "message": str(exc), "traceback": traceback.format_exc()})
    fig.suptitle("Figure 1 | Representative EPIC-disk diagnostics, Policy A", fontsize=10)
    fig.text(
        0.5,
        -0.006,
        "Color guide: class cream=clear, blue=cloud; mismatch gray/blue=agree, orange=GEO misses EPIC cloud, red=GEO extra cloud; family blue=GOES, teal=EastAsia, red=Meteosat; valid count light-to-dark=1,2,3,>=4; scene green=homogeneous, purple=boundary/broken.",
        ha="center",
        va="top",
        fontsize=5.4,
        color="#555555",
    )
    source_df = pd.concat(source_frames, ignore_index=True) if source_frames else pd.DataFrame()
    src = source_path(dirs, "figure1_representative_disk_diagnostic")
    write_source(source_df, src, warnings)
    pd.DataFrame(summary_rows).to_csv(dirs["source_data"] / f"{RUN_ID}_figure1_representative_sample_summary.csv", index=False, encoding="utf-8-sig")
    paths = save_figure(fig, dirs, "figure1_representative_disk_diagnostic")
    return paths, src, summary_rows


def figure2_source_coverage(
    dirs: dict[str, Path],
    sample: dict[str, Any],
    cache: dict[str, dict[str, Any]],
    stride: int,
    warnings: list[dict[str, Any]],
) -> tuple[dict[str, str], Path, dict[str, Any]]:
    ctx = context_cache_get(cache, sample)
    source_cls, source_valid, source_warnings = source_samples(ctx)
    for w in source_warnings:
        warnings.append({"level": "warning", "source": "figure2_source_samples", "sample_id": sample["sample_id"], "message": w})
    selected_family = source_family_code(selected_source_array(ctx["selected_source"]))
    base, _, _ = base_valid_mask(ctx, POLICY)
    coverage_arrays: dict[str, np.ndarray] = {}
    coverage_arrays["FY4B_valid"] = source_valid.get("FY4B", np.zeros(base.shape, dtype=bool)).astype(np.int16) + 1
    coverage_arrays["Himawari9_valid"] = source_valid.get("Himawari-9", np.zeros(base.shape, dtype=bool)).astype(np.int16) + 1
    goes_valid = np.zeros(base.shape, dtype=bool)
    for s in ["GOES-16", "GOES-18"]:
        goes_valid |= source_valid.get(s, np.zeros(base.shape, dtype=bool))
    coverage_arrays["GOES_any_valid"] = goes_valid.astype(np.int16) + 1
    coverage_arrays["Meteosat0deg_valid"] = source_valid.get("Meteosat-0deg", np.zeros(base.shape, dtype=bool)).astype(np.int16) + 1
    coverage_arrays["MeteosatIODC_valid"] = source_valid.get("Meteosat-IODC", np.zeros(base.shape, dtype=bool)).astype(np.int16) + 1
    coverage_arrays["selected_family_code"] = selected_family
    lat = orient(ctx["epic"]["lat"], ctx["epic"]["lat"])
    lon = orient(ctx["epic"]["lon"], ctx["epic"]["lat"])
    arrays = {k: orient(v, ctx["epic"]["lat"]) for k, v in coverage_arrays.items()}
    src_df = flatten_sample_source(sample["sample_id"], lat, lon, arrays, stride)
    src = source_path(dirs, "figure2_source_coverage_selected_family")
    write_source(src_df, src, warnings)

    fig, axes = plt.subplots(2, 3, figsize=(8.2, 5.1), constrained_layout=True)
    panels = [
        ("FY4B valid", "FY4B_valid", BOOL_CMAP, BOOL_NORM),
        ("Himawari-9 valid", "Himawari9_valid", BOOL_CMAP, BOOL_NORM),
        ("GOES any valid", "GOES_any_valid", BOOL_CMAP, BOOL_NORM),
        ("Meteosat-0deg valid", "Meteosat0deg_valid", BOOL_CMAP, BOOL_NORM),
        ("Meteosat-IODC valid", "MeteosatIODC_valid", BOOL_CMAP, BOOL_NORM),
        ("Selected source family", "selected_family_code", FAMILY_CMAP, FAMILY_NORM),
    ]
    for ax, (title, key, cmap, norm) in zip(axes.ravel(), panels):
        image_panel(ax, ds(arrays[key], stride), title, cmap, norm)
    fig.suptitle(f"Figure 2 | Source valid-coverage proxy and selected family: {short_sample(sample['sample_id'])}", fontsize=10)
    paths = save_figure(fig, dirs, "figure2_source_coverage_selected_family")
    summary = {
        "sample_id": sample["sample_id"],
        "figure2_role": "source valid mask and selected family coverage proxy",
        "n_policy_a_valid": int(np.count_nonzero(base)),
        "fy4b_valid_fraction": float(np.mean(source_valid.get("FY4B", np.zeros(base.shape, dtype=bool))[base])) if np.any(base) else math.nan,
        "himawari9_valid_fraction": float(np.mean(source_valid.get("Himawari-9", np.zeros(base.shape, dtype=bool))[base])) if np.any(base) else math.nan,
        "goes_any_valid_fraction": float(np.mean(goes_valid[base])) if np.any(base) else math.nan,
        "meteosat0deg_valid_fraction": float(np.mean(source_valid.get("Meteosat-0deg", np.zeros(base.shape, dtype=bool))[base])) if np.any(base) else math.nan,
        "meteosat_iodc_valid_fraction": float(np.mean(source_valid.get("Meteosat-IODC", np.zeros(base.shape, dtype=bool))[base])) if np.any(base) else math.nan,
    }
    return paths, src, summary


def apply_source_policy(source_cls: dict[str, np.ndarray], source_valid: dict[str, np.ndarray], source: str) -> tuple[np.ndarray, np.ndarray]:
    cls, pv = d09d.apply_policy(source_cls[source], d09d.POLICIES[POLICY]["geo"])
    return cls, source_valid[source] & pv


def pair_correctness_code(
    epic_cls: np.ndarray,
    cls_a: np.ndarray,
    cls_b: np.ndarray,
    valid: np.ndarray,
) -> np.ndarray:
    out = np.zeros(epic_cls.shape, dtype=np.int16)
    a_match = valid & (cls_a == epic_cls)
    b_match = valid & (cls_b == epic_cls)
    out[valid & a_match & b_match] = 2
    out[valid & a_match & (~b_match)] = 3
    out[valid & (~a_match) & b_match] = 4
    out[valid & (~a_match) & (~b_match)] = 5
    out[~valid] = 0
    return out


def choose_pair_samples(pair_metrics: pd.DataFrame, manifest: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    by_id = {r["sample_id"]: r for r in manifest}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    if pair_metrics.empty:
        warnings.append({"level": "warning", "source": "pair_selection", "message": "source-pair metrics table is empty"})
        return out
    for pair in PAIR_LIST:
        a, b = pair
        q = pair_metrics[
            (pair_metrics["policy"] == POLICY)
            & (pair_metrics["source_A"] == a)
            & (pair_metrics["source_B"] == b)
            & (pair_metrics["mask_name"].isin([MASK_FOR_PAIR, "VIS-0_baseline_current"]))
        ].copy()
        if q.empty:
            q = pair_metrics[
                (pair_metrics["policy"] == POLICY)
                & (pair_metrics["source_A"] == a)
                & (pair_metrics["source_B"] == b)
            ].copy()
        if q.empty:
            warnings.append({"level": "warning", "source": "pair_selection", "message": f"missing pair rows for {a} vs {b}"})
            continue
        q["score"] = pd.to_numeric(q["source_disagreement_fraction"], errors="coerce") * np.log1p(pd.to_numeric(q["n_overlap_valid"], errors="coerce"))
        row = q.sort_values("score", ascending=False).iloc[0]
        sid = str(row["sample_id"])
        if sid in by_id:
            item = dict(by_id[sid])
            item["stage_09f_pair_reason"] = f"{a} vs {b}; disagreement={float(row['source_disagreement_fraction']):.3f}; n={int(row['n_overlap_valid'])}"
            out[pair] = item
    return out


def figure3_source_pair_maps(
    dirs: dict[str, Path],
    pair_samples: dict[tuple[str, str], dict[str, Any]],
    cache: dict[str, dict[str, Any]],
    stride: int,
    warnings: list[dict[str, Any]],
) -> tuple[dict[str, str], Path, list[dict[str, Any]]]:
    pairs = [p for p in PAIR_LIST if p in pair_samples]
    fig, axes = plt.subplots(len(pairs), 3, figsize=(7.4, max(1.55 * len(pairs), 4.4)), constrained_layout=True)
    if len(pairs) == 1:
        axes = np.asarray([axes])
    frames = []
    summary = []
    for i, (source_a, source_b) in enumerate(pairs):
        row = pair_samples[(source_a, source_b)]
        try:
            ctx = context_cache_get(cache, row)
            base, epic_cls, _ = base_valid_mask(ctx, POLICY)
            source_cls, source_valid, source_warnings = source_samples(ctx)
            for w in source_warnings:
                warnings.append({"level": "warning", "source": "figure3_source_samples", "sample_id": row["sample_id"], "message": w})
            if source_a not in source_cls or source_b not in source_cls:
                warnings.append({"level": "warning", "source": "figure3", "sample_id": row["sample_id"], "message": f"missing source pair arrays: {source_a}, {source_b}"})
                continue
            cls_a, valid_a = apply_source_policy(source_cls, source_valid, source_a)
            cls_b, valid_b = apply_source_policy(source_cls, source_valid, source_b)
            common = base & valid_a & valid_b
            disagreement = common & (cls_a != cls_b)
            common_code = common.astype(np.int16) + 1
            disagreement_code = np.ones(common.shape, dtype=np.int16)
            disagreement_code[common] = 1
            disagreement_code[disagreement] = 2
            correct_code = pair_correctness_code(epic_cls, cls_a, cls_b, common)
            arrays = {
                "common_valid_code": orient(common_code, ctx["epic"]["lat"]),
                "source_disagreement_code": orient(disagreement_code, ctx["epic"]["lat"]),
                "correctness_vs_epic_code": orient(correct_code, ctx["epic"]["lat"]),
            }
            lat = orient(ctx["epic"]["lat"], ctx["epic"]["lat"])
            lon = orient(ctx["epic"]["lon"], ctx["epic"]["lat"])
            pair_df = flatten_sample_source(row["sample_id"], lat, lon, arrays, stride)
            pair_df["source_A"] = source_a
            pair_df["source_B"] = source_b
            frames.append(pair_df)
            n = int(np.count_nonzero(common))
            summary.append(
                {
                    "sample_id": row["sample_id"],
                    "source_A": source_a,
                    "source_B": source_b,
                    "n_common_valid_policy_a": n,
                    "source_disagreement_fraction": int(np.count_nonzero(disagreement)) / n if n else math.nan,
                    "both_correct_fraction": int(np.count_nonzero(common & (cls_a == epic_cls) & (cls_b == epic_cls))) / n if n else math.nan,
                    "A_only_correct_fraction": int(np.count_nonzero(common & (cls_a == epic_cls) & (cls_b != epic_cls))) / n if n else math.nan,
                    "B_only_correct_fraction": int(np.count_nonzero(common & (cls_a != epic_cls) & (cls_b == epic_cls))) / n if n else math.nan,
                    "both_wrong_fraction": int(np.count_nonzero(common & (cls_a != epic_cls) & (cls_b != epic_cls))) / n if n else math.nan,
                    "selection_reason": row.get("stage_09f_pair_reason", ""),
                }
            )
            image_panel(axes[i, 0], ds(arrays["common_valid_code"], stride), "Common valid" if i == 0 else "", BOOL_CMAP, BOOL_NORM)
            image_panel(axes[i, 1], ds(arrays["source_disagreement_code"], stride), "A/B disagreement" if i == 0 else "", BOOL_CMAP, BOOL_NORM)
            image_panel(axes[i, 2], ds(arrays["correctness_vs_epic_code"], stride), "Relative to EPIC" if i == 0 else "", PAIR_CMAP, PAIR_NORM)
            axes[i, 0].set_ylabel(f"{source_a}\nvs {source_b}\n{short_sample(row['sample_id'])}", fontsize=6.0)
        except Exception as exc:
            warnings.append({"level": "warning", "source": "figure3", "sample_id": row.get("sample_id", ""), "message": str(exc), "traceback": traceback.format_exc()})
    fig.suptitle("Figure 3 | Source-pair spatial disagreement on the EPIC disk, Policy A", fontsize=10)
    fig.text(
        0.5,
        -0.006,
        "Color guide: common/disagreement teal=condition true; relative to EPIC blue=both correct, teal=A only correct, red=B only correct, yellow=both wrong. EPIC remains a diagnostic reference, not absolute truth.",
        ha="center",
        va="top",
        fontsize=5.6,
        color="#555555",
    )
    src_df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    src = source_path(dirs, "figure3_source_pair_spatial_disagreement")
    write_source(src_df, src, warnings)
    pd.DataFrame(summary).to_csv(dirs["source_data"] / f"{RUN_ID}_figure3_source_pair_summary.csv", index=False, encoding="utf-8-sig")
    paths = save_figure(fig, dirs, "figure3_source_pair_spatial_disagreement")
    return paths, src, summary


def init_bins() -> tuple[np.ndarray, np.ndarray]:
    lon_edges = np.arange(-180, 180 + 5, 5, dtype=float)
    lat_edges = np.arange(-90, 90 + 5, 5, dtype=float)
    return lon_edges, lat_edges


def add_to_grid(acc: dict[str, np.ndarray], lat: np.ndarray, lon: np.ndarray, valid: np.ndarray, fields: dict[str, np.ndarray], lon_edges: np.ndarray, lat_edges: np.ndarray) -> None:
    ok = valid & np.isfinite(lat) & np.isfinite(lon)
    if not np.any(ok):
        return
    lon_norm = ((lon[ok] + 180.0) % 360.0) - 180.0
    lat_v = lat[ok]
    li = np.digitize(lat_v, lat_edges) - 1
    lj = np.digitize(lon_norm, lon_edges) - 1
    inside = (li >= 0) & (li < len(lat_edges) - 1) & (lj >= 0) & (lj < len(lon_edges) - 1)
    li = li[inside]
    lj = lj[inside]
    np.add.at(acc["n_valid"], (li, lj), 1)
    for name, arr in fields.items():
        vals = arr[ok][inside].astype(np.int64)
        np.add.at(acc[name], (li, lj), vals)


def figure4_accumulated_geography(
    dirs: dict[str, Path],
    manifest: list[dict[str, Any]],
    cache: dict[str, dict[str, Any]],
    warnings: list[dict[str, Any]],
    max_samples: int = 0,
) -> tuple[dict[str, str], Path, dict[str, Any]]:
    lon_edges, lat_edges = init_bins()
    shape = (len(lat_edges) - 1, len(lon_edges) - 1)
    acc = {
        "n_valid": np.zeros(shape, dtype=np.int64),
        "n_disagreement": np.zeros(shape, dtype=np.int64),
        "n_meteosat_selected": np.zeros(shape, dtype=np.int64),
        "n_valid_count_ge4": np.zeros(shape, dtype=np.int64),
        "n_boundary_or_broken": np.zeros(shape, dtype=np.int64),
    }
    used = 0
    for idx, row in enumerate(manifest):
        if max_samples and used >= max_samples:
            break
        try:
            ctx = context_cache_get(cache, row)
            m = sample_metrics_for_context(ctx)
            fields = {
                "n_disagreement": (m["epic_cls"] != m["geo_cls"]).astype(np.int16),
                "n_meteosat_selected": (m["family_code"] == 3).astype(np.int16),
                "n_valid_count_ge4": (ctx["valid_count"] >= 4).astype(np.int16),
                "n_boundary_or_broken": ((m["scene"]["boundary_class"] != "non_boundary") | (m["scene"]["scene_type"] == "broken_cloud")).astype(np.int16),
            }
            add_to_grid(acc, ctx["epic"]["lat"], ctx["epic"]["lon"], m["base"], fields, lon_edges, lat_edges)
            used += 1
            if (idx + 1) % 10 == 0:
                print(f"[stage_09f] accumulated {idx + 1}/{len(manifest)} samples", flush=True)
        except Exception as exc:
            warnings.append({"level": "warning", "source": "figure4_accumulation", "sample_id": row.get("sample_id", ""), "message": str(exc), "traceback": traceback.format_exc()})

    n = acc["n_valid"].astype(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        maps = {
            "disagreement_fraction": acc["n_disagreement"] / n,
            "meteosat_selected_fraction": acc["n_meteosat_selected"] / n,
            "valid_count_ge4_frequency": acc["n_valid_count_ge4"] / n,
            "boundary_or_broken_frequency": acc["n_boundary_or_broken"] / n,
        }
    rows = []
    for i in range(shape[0]):
        for j in range(shape[1]):
            if acc["n_valid"][i, j] <= 0:
                continue
            rows.append(
                {
                    "lat_bin_min_deg": lat_edges[i],
                    "lat_bin_max_deg": lat_edges[i + 1],
                    "lon_bin_min_deg": lon_edges[j],
                    "lon_bin_max_deg": lon_edges[j + 1],
                    "n_valid": int(acc["n_valid"][i, j]),
                    "n_disagreement": int(acc["n_disagreement"][i, j]),
                    "n_meteosat_selected": int(acc["n_meteosat_selected"][i, j]),
                    "n_valid_count_ge4": int(acc["n_valid_count_ge4"][i, j]),
                    "n_boundary_or_broken": int(acc["n_boundary_or_broken"][i, j]),
                    **{k: float(v[i, j]) for k, v in maps.items()},
                }
            )
    src = source_path(dirs, "figure4_accumulated_disagreement_geography")
    write_source(pd.DataFrame(rows), src, warnings)

    fig, axes = plt.subplots(2, 2, figsize=(8.4, 4.9), constrained_layout=True)
    panels = [
        ("Disagreement fraction", maps["disagreement_fraction"], "magma", 0.0, 0.75),
        ("Meteosat-selected fraction", maps["meteosat_selected_fraction"], "Reds", 0.0, 1.0),
        ("valid_source_count >=4 frequency", maps["valid_count_ge4_frequency"], "Blues", 0.0, 1.0),
        ("Boundary/broken-cloud frequency", maps["boundary_or_broken_frequency"], "Purples", 0.0, 1.0),
    ]
    for ax, (title, data, cmap, vmin, vmax) in zip(axes.ravel(), panels):
        masked = np.ma.masked_invalid(data)
        im = ax.imshow(masked, origin="lower", extent=[-180, 180, -90, 90], aspect="auto", cmap=cmap, vmin=vmin, vmax=vmax)
        ax.set_title(title)
        ax.set_xlabel("Longitude (deg)")
        ax.set_ylabel("Latitude (deg)")
        ax.grid(color="#FFFFFF", alpha=0.35, linewidth=0.4)
        fig.colorbar(im, ax=ax, fraction=0.035, pad=0.02)
    fig.suptitle("Figure 4 | March 2024 diagnostic sample composite, Policy A", fontsize=10)
    paths = save_figure(fig, dirs, "figure4_accumulated_disagreement_geography")
    summary = {
        "samples_used": used,
        "n_bins_nonzero": len(rows),
        "total_valid_pixels": int(acc["n_valid"].sum()),
        "overall_disagreement_fraction_from_bins": float(acc["n_disagreement"].sum() / max(acc["n_valid"].sum(), 1)),
        "note": "5-degree lat/lon bins; diagnostic March 2024 sample composite, not climatology",
    }
    return paths, src, summary


def write_report(
    dirs: dict[str, Path],
    figure_index: pd.DataFrame,
    representative_summary: list[dict[str, Any]],
    coverage_summary: dict[str, Any],
    pair_summary: list[dict[str, Any]],
    geography_summary: dict[str, Any],
    warnings: list[dict[str, Any]],
) -> Path:
    lines = [
        "# Stage 09F 空间叙事图诊断报告",
        "",
        f"- Run ID: `{RUN_ID}`",
        f"- Generated UTC: `{utc_now()}`",
        "- 范围：只使用 2024-03 已有 Stage 09D/09E 结果和本地产品；未联网下载；未修改 fused cloud mask 生产逻辑。",
        "- 参照关系：EPIC 只作为 independent diagnostic reference，不作为绝对真值。",
        "- 覆盖范围解释：source coverage 图使用 `source valid mask` / `selected source family` 作为可视化代理，不等同于严格物理 FOV 边界。",
        "- 单位：经纬度为 degree；agreement、fraction、frequency 为无量纲比例。",
        "",
        "## 主要读图逻辑",
        "",
        "1. Figure 1 把代表样本拆成 EPIC、当前 GEO-ring、差异方向、当前选择的 source family、valid_source_count 和 boundary/scene。它回答“不一致具体长在哪里、对应的当前 source 是谁”。",
        "2. Figure 2 显示同一 EPIC 盘面上的单源有效覆盖代理和当前 selected family。它帮助区分“看不到/无效”与“看得到但选择了某一类 source”。",
        "3. Figure 3 只比较两个 GEO source 在共同有效像元上的空间差异，同时给出二者相对 EPIC 的 correct/wrong 关系。它不是融合结果，也不是用 EPIC 当真值训练 source selection。",
        "4. Figure 4 把 53 个样本按 5° 经纬度 bin 累计，用于讲清楚 disagreement、Meteosat-selected、valid_count>=4 和 boundary/broken-cloud 是否在空间上同位出现。该图不是气候统计。",
        "",
        "## Representative Samples",
    ]
    if representative_summary:
        lines.append(markdown_table(pd.DataFrame(representative_summary)))
    else:
        lines.append("_No representative samples._")
    lines.extend(["", "## Source Coverage Summary", ""])
    lines.append(markdown_table(pd.DataFrame([coverage_summary])) if coverage_summary else "_No source coverage summary._")
    lines.extend(["", "## Source-pair Summary", ""])
    lines.append(markdown_table(pd.DataFrame(pair_summary)) if pair_summary else "_No pair summary._")
    lines.extend(["", "## Accumulated Geography Summary", ""])
    lines.append(markdown_table(pd.DataFrame([geography_summary])) if geography_summary else "_No geography summary._")
    lines.extend(["", "## Figure Index", ""])
    lines.append(markdown_table(figure_index) if not figure_index.empty else "_No figures._")
    lines.extend(["", "## Warnings", ""])
    if warnings:
        for w in warnings[:80]:
            lines.append(f"- `{w.get('source', 'unknown')}` {w.get('sample_id', '')}: {w.get('message', '')}")
        if len(warnings) > 80:
            lines.append(f"- ... {len(warnings) - 80} additional warnings in warnings.csv")
    else:
        lines.append("- No warnings.")
    lines.append("")
    report = dirs["reports"] / f"{RUN_ID}_report_cn.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def verify_outputs(figure_index: pd.DataFrame, warnings: list[dict[str, Any]]) -> dict[str, Any]:
    checks = []
    for _, row in figure_index.iterrows():
        for col in ["png", "svg", "pdf", "source_csv"]:
            path = Path(str(row[col]))
            ok = path.exists() and path.stat().st_size > 0
            checks.append({"figure_id": row["figure_id"], "artifact": col, "path": str(path), "ok": bool(ok)})
            if not ok:
                warnings.append({"level": "warning", "source": row["figure_id"], "message": f"missing or empty {col}: {path}"})
        src = Path(str(row["source_csv"]))
        if src.exists():
            try:
                n = len(pd.read_csv(src))
            except Exception:
                n = 0
            ok = n > 0
            checks.append({"figure_id": row["figure_id"], "artifact": "source_nonzero_rows", "path": str(src), "rows": int(n), "ok": bool(ok)})
            if not ok:
                warnings.append({"level": "warning", "source": row["figure_id"], "message": f"source CSV has zero readable rows: {src}"})
    return {"checks": checks, "all_ok": all(c["ok"] for c in checks)}


def write_warnings(path: Path, warnings: list[dict[str, Any]]) -> None:
    if warnings:
        pd.DataFrame(warnings).to_csv(path, index=False, encoding="utf-8-sig")
    else:
        pd.DataFrame(columns=["level", "source", "sample_id", "message"]).to_csv(path, index=False, encoding="utf-8-sig")


def markdown_table(df: pd.DataFrame, max_rows: int = 80) -> str:
    if df.empty:
        return "_No rows._"
    small = df.head(max_rows).copy()
    for col in small.columns:
        if pd.api.types.is_float_dtype(small[col]):
            small[col] = small[col].map(lambda x: "" if pd.isna(x) else f"{float(x):.4f}")
        else:
            small[col] = small[col].fillna("").astype(str)
    cols = [str(c) for c in small.columns]
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join(["---"] * len(cols)) + " |"]
    for _, row in small.iterrows():
        vals = [str(row[c]).replace("|", "\\|") for c in small.columns]
        lines.append("| " + " | ".join(vals) + " |")
    if len(df) > max_rows:
        lines.append(f"\n_Only first {max_rows} of {len(df)} rows shown; see source CSVs for full data._")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Stage 09F spatial story maps for GEO-ring vs EPIC diagnostics.")
    ap.add_argument("--stage09d-dir", default=str(DEFAULT_STAGE09D_DIR))
    ap.add_argument("--vis-dir", default=str(DEFAULT_VIS_DIR))
    ap.add_argument("--output-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--plot-stride", type=int, default=6, help="EPIC disk stride used for displayed map source CSVs.")
    ap.add_argument("--representative-samples", type=int, default=5)
    ap.add_argument("--max-aggregate-samples", type=int, default=0, help="0 means all runnable March 2024 samples.")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    apply_style()
    out = Path(args.output_dir)
    dirs = ensure_dirs(out)
    warnings: list[dict[str, Any]] = []
    stage09d_dir = Path(args.stage09d_dir)
    vis_dir = Path(args.vis_dir)
    manifest_all = load_manifest(stage09d_dir)
    manifest = [r for r in manifest_all if bool_series(r.get("can_run_sampling"))]
    if not manifest:
        raise RuntimeError("no runnable Stage 09D samples found")

    policy_sample = read_csv(vis_dir / "02_policy_metrics" / "stage_09d_vis_policy_metrics_by_sample.csv", warnings, "policy_sample")
    pair_metrics = read_csv(vis_dir / "04_source_pair_metrics" / "stage_09d_vis_source_pair_metrics.csv", warnings, "source_pair_metrics")
    strata = read_csv(vis_dir / "03_group_source_metrics" / "stage_09d_vis_metrics_by_strata.csv", warnings, "strata_metrics")

    selected = choose_representative_samples(manifest, policy_sample, pair_metrics, strata, args.representative_samples, warnings)
    pair_samples = choose_pair_samples(pair_metrics, manifest, warnings)
    cache: dict[str, dict[str, Any]] = {}
    figures: list[dict[str, Any]] = []

    print(f"[stage_09f] representative samples: {[r['sample_id'] for r in selected]}", flush=True)
    paths, src, representative_summary = figure1_representative_disk(dirs, selected, cache, args.plot_stride, warnings)
    figures.append({"figure_id": "figure1_representative_disk_diagnostic", "title": "Figure 1 | Representative disk diagnostic", "source_csv": str(src), **paths, "created_utc": utc_now()})

    coverage_sample = selected[0] if selected else manifest[0]
    paths, src, coverage_summary = figure2_source_coverage(dirs, coverage_sample, cache, args.plot_stride, warnings)
    figures.append({"figure_id": "figure2_source_coverage_selected_family", "title": "Figure 2 | Source coverage and selected-family map", "source_csv": str(src), **paths, "created_utc": utc_now()})

    paths, src, pair_summary = figure3_source_pair_maps(dirs, pair_samples, cache, args.plot_stride, warnings)
    figures.append({"figure_id": "figure3_source_pair_spatial_disagreement", "title": "Figure 3 | Source-pair spatial disagreement", "source_csv": str(src), **paths, "created_utc": utc_now()})

    paths, src, geography_summary = figure4_accumulated_geography(dirs, manifest, cache, warnings, args.max_aggregate_samples)
    figures.append({"figure_id": "figure4_accumulated_disagreement_geography", "title": "Figure 4 | Accumulated disagreement geography", "source_csv": str(src), **paths, "created_utc": utc_now()})

    figure_index = pd.DataFrame(figures)
    figure_index_path = dirs["logs"] / "figure_index.csv"
    figure_index.to_csv(figure_index_path, index=False, encoding="utf-8-sig")

    report_path = write_report(dirs, figure_index, representative_summary, coverage_summary, pair_summary, geography_summary, warnings)
    warnings_path = dirs["logs"] / "warnings.csv"
    write_warnings(warnings_path, warnings)
    verification = verify_outputs(figure_index, warnings)
    # Re-write warnings after verification can append entries.
    write_warnings(warnings_path, warnings)
    manifest_obj = {
        "project_id": PROJECT_ID,
        "canonical_stage_id": STAGE_ID,
        "run_id": RUN_ID,
        "created_utc": utc_now(),
        "script_path": str(Path(__file__).resolve()),
        "input_paths": {
            "stage09d_manifest": str(stage09d_dir / "00_sample_manifest" / "stage09d_53_sample_manifest.csv"),
            "policy_sample": str(vis_dir / "02_policy_metrics" / "stage_09d_vis_policy_metrics_by_sample.csv"),
            "source_pair_metrics": str(vis_dir / "04_source_pair_metrics" / "stage_09d_vis_source_pair_metrics.csv"),
            "strata_metrics": str(vis_dir / "03_group_source_metrics" / "stage_09d_vis_metrics_by_strata.csv"),
        },
        "output_root": str(out),
        "output_paths": {
            "figure_index": str(figure_index_path),
            "report": str(report_path),
            "warnings": str(warnings_path),
            "manifest": str(dirs["logs"] / "manifest.json"),
        },
        "parameters": {
            "policy": POLICY,
            "plot_stride": args.plot_stride,
            "representative_samples": args.representative_samples,
            "max_aggregate_samples": args.max_aggregate_samples,
            "lat_lon_bin_deg": 5,
        },
        "row_counts": {
            "manifest_all": len(manifest_all),
            "manifest_runnable": len(manifest),
            "figures": len(figures),
            "warnings": len(warnings),
        },
        "figures": figures,
        "verification": verification,
        "constraints": {
            "no_fusion_logic_change": True,
            "no_fusion_v2": True,
            "no_network_download": True,
            "epic_role": "independent diagnostic reference, not absolute truth",
            "coverage_proxy_note": "source valid mask / selected source family, not strict physical FOV boundary",
        },
    }
    manifest_path = dirs["logs"] / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_obj, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"output_root": str(out), "figures": len(figures), "all_ok": verification["all_ok"], "warnings": len(warnings)}, ensure_ascii=False), flush=True)
    return 0 if verification["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
