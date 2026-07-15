# -*- coding: utf-8 -*-
"""Stage 09E Nature-style meeting figures.

Diagnostic-only plotting script. It reads existing Stage 09D/09E CSV summaries,
does not rerun pixel-level diagnostics, does not download data, and does not
modify fused cloud-mask production logic.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import path_config  # noqa: E402

STAGE_ID = "stage_09e"
RUN_ID = "stage_09e_nature_meeting_figures_202403"

PSF_ROOT = path_config.RUNS_ROOT / "stage_09e_psf_aware_epic_view_202403"
SELQC_ROOT = path_config.RUNS_ROOT / "stage_09e_sel_qc_common_valid_202403"
VIS_ROOT = path_config.RUNS_ROOT / "stage09d_geo_visible_controlled_metrics_202403"
OUT_ROOT = path_config.RUNS_ROOT / RUN_ID

INPUTS = {
    "psf_kernel_metrics": PSF_ROOT / "01_fused_psf_metrics" / "stage_09e_fused_psf_metrics_by_kernel.csv",
    "psf_boundary_scene": PSF_ROOT / "05_boundary_scene_psf" / "stage_09e_psf_metrics_by_boundary_scene.csv",
    "psf_meteosat_gap": PSF_ROOT / "06_meteosat_gap_psf" / "stage_09e_psf_meteosat_gap_by_kernel.csv",
    "selqc_valid_count": SELQC_ROOT
    / "01_valid_count_consistency"
    / "stage_09e_selqc_valid_count_consistency_summary.csv",
    "selqc_oracle": SELQC_ROOT / "02_oracle_granularity" / "stage_09e_selqc_oracle_granularity_summary.csv",
    "selqc_common_valid": SELQC_ROOT
    / "03_common_valid_comparison"
    / "stage_09e_selqc_common_valid_source_comparison_summary.csv",
    "vis_policy": VIS_ROOT / "02_policy_metrics" / "stage_09d_vis_policy_metrics_by_mask.csv",
    "vis_group": VIS_ROOT / "03_group_source_metrics" / "stage_09d_vis_metrics_by_group.csv",
    "vis_source_pair": VIS_ROOT
    / "04_source_pair_metrics"
    / "stage_09d_vis_source_pair_summary_by_mask.csv",
    "vis_valid_source_count": VIS_ROOT
    / "03_group_source_metrics"
    / "stage_09d_vis_metrics_by_valid_source_count.csv",
    "vis_meteosat_gap": VIS_ROOT
    / "05_meteosat_focus"
    / "stage_09d_vis_meteosat_vs_goes_eastasia_gap.csv",
}

POLICY_ORDER = [
    "A_inclusive_binary",
    "B_high_confidence_only",
    "C_uncertainty_aware_3class",
]
POLICY_LABELS = {
    "A_inclusive_binary": "Policy A",
    "B_high_confidence_only": "Policy B",
    "C_uncertainty_aware_3class": "Policy C",
}
POLICY_COLORS = {
    "A_inclusive_binary": "#3B6FB6",
    "B_high_confidence_only": "#2AA198",
    "C_uncertainty_aware_3class": "#B94A48",
}
SOURCE_COLORS = {
    "Meteosat": "#B65A5A",
    "Meteosat-0deg": "#B65A5A",
    "Meteosat-IODC": "#D17A68",
    "GOES": "#3B6FB6",
    "GOES-16": "#3B6FB6",
    "GOES-18": "#6D8FC8",
    "East Asia": "#2AA198",
    "FY4B": "#179B8E",
    "Himawari-9": "#5DBB63",
    "QC": "#D45B2A",
}
MASK_ORDER = [
    "VIS-0_baseline_current",
    "VIS-3_lat60_visible",
    "VIS-5_clean_core",
    "VIS-6_non_boundary_visible",
    "VIS-7_boundary_visible",
]
MASK_LABELS = {
    "VIS-0_baseline_current": "VIS-0\nbaseline",
    "VIS-1_fused_valid_only": "VIS-1\nvalid",
    "VIS-2_lat70_visible": "VIS-2\nlat70",
    "VIS-3_lat60_visible": "VIS-3\nlat60",
    "VIS-4a_reliable_geometry_without_geo_vza": "VIS-4a\ngeom",
    "VIS-4b_reliable_geometry_with_geo_vza_available_only": "VIS-4b\nGEO VZA",
    "VIS-5_clean_core": "VIS-5\nclean core",
    "VIS-6_non_boundary_visible": "VIS-6\nnon-boundary",
    "VIS-7_boundary_visible": "VIS-7\nboundary",
}
KERNEL_ORDER = [
    "K0_nearest",
    "K1_box_3x3",
    "K3_box_7x7",
    "K5_gaussian_sigma_1p25_cell_radius_5",
]
KERNEL_LABELS = {
    "K0_nearest": "K0\nnearest",
    "K1_box_3x3": "K1\n3x3",
    "K3_box_7x7": "K3\n7x7",
    "K5_gaussian_sigma_1p25_cell_radius_5": "K5\ngauss",
}
PAIR_ORDER = [
    "FY4B vs Himawari-9",
    "FY4B vs Meteosat-IODC",
    "Himawari-9 vs Meteosat-IODC",
    "Meteosat-0deg vs GOES-16",
    "Meteosat-0deg vs Meteosat-IODC",
    "GOES-16 vs GOES-18",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def apply_publication_style() -> None:
    plt.rcParams.update(
        {
            "figure.dpi": 160,
            "savefig.dpi": 600,
            "font.family": "sans-serif",
            "font.sans-serif": ["Arial", "DejaVu Sans", "Liberation Sans"],
            "font.size": 7.0,
            "axes.titlesize": 8.0,
            "axes.labelsize": 7.0,
            "xtick.labelsize": 6.2,
            "ytick.labelsize": 6.2,
            "legend.fontsize": 6.2,
            "axes.linewidth": 0.7,
            "xtick.major.width": 0.6,
            "ytick.major.width": 0.6,
            "xtick.major.size": 2.5,
            "ytick.major.size": 2.5,
            "lines.linewidth": 1.35,
            "lines.markersize": 3.8,
            "svg.fonttype": "none",
            "pdf.fonttype": 42,
            "pdf.use14corefonts": False,
            "axes.spines.top": False,
            "axes.spines.right": False,
        }
    )


def ensure_dirs() -> dict[str, Path]:
    dirs = {
        "figures": OUT_ROOT / "figures",
        "source_data": OUT_ROOT / "source_data",
        "reports": OUT_ROOT / "reports",
        "logs": OUT_ROOT / "logs",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def read_table(name: str, warnings: list[dict[str, Any]]) -> pd.DataFrame:
    path = INPUTS[name]
    if not path.exists():
        warnings.append({"level": "warning", "source": name, "message": f"missing input CSV: {path}"})
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
    except Exception as exc:  # pragma: no cover - defensive run logging
        warnings.append({"level": "warning", "source": name, "message": f"failed to read CSV: {exc}"})
        return pd.DataFrame()
    if df.empty:
        warnings.append({"level": "warning", "source": name, "message": "input CSV has zero rows"})
    return coerce_numeric(df)


def coerce_numeric(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    numeric_hints = (
        "_weighted",
        "_mean",
        "_total",
        "_fraction",
        "_count",
        "_ratio",
        "agreement",
        "precision",
        "recall",
        "f1",
        "iou",
        "TP",
        "TN",
        "FP",
        "FN",
    )
    for col in out.columns:
        if col in {"policy", "mask_name", "kernel_name", "threshold", "pixel_group", "source_set", "source_name"}:
            continue
        if any(hint in col for hint in numeric_hints):
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def select_main_threshold(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "threshold" not in df.columns:
        return df.copy()
    threshold = df["threshold"].astype(str)
    policy = df["policy"].astype(str)
    keep = ((policy != "C_uncertainty_aware_3class") & (threshold == "0.50")) | (
        (policy == "C_uncertainty_aware_3class") & (threshold == "argmax")
    )
    return df.loc[keep].copy()


def order_categorical(df: pd.DataFrame, column: str, order: list[str]) -> pd.DataFrame:
    if df.empty or column not in df.columns:
        return df
    out = df.copy()
    out[column] = pd.Categorical(out[column], categories=order, ordered=True)
    return out.sort_values(column)


def source_csv_path(dirs: dict[str, Path], figure_id: str) -> Path:
    return dirs["source_data"] / f"{RUN_ID}_{figure_id}_source.csv"


def write_source(df: pd.DataFrame, path: Path, warnings: list[dict[str, Any]]) -> None:
    if df.empty:
        warnings.append({"level": "warning", "source": path.name, "message": "source CSV would be empty"})
    df.to_csv(path, index=False, encoding="utf-8-sig")


def save_figure(fig: plt.Figure, dirs: dict[str, Path], figure_id: str) -> dict[str, str]:
    base = dirs["figures"] / f"{RUN_ID}_{figure_id}"
    fig.savefig(base.with_suffix(".svg"), bbox_inches="tight")
    fig.savefig(base.with_suffix(".pdf"), bbox_inches="tight")
    fig.savefig(base.with_suffix(".tiff"), bbox_inches="tight", dpi=600)
    fig.savefig(base.with_suffix(".png"), bbox_inches="tight", dpi=240)
    plt.close(fig)
    return {ext: str(base.with_suffix(f".{ext}")) for ext in ["svg", "pdf", "tiff", "png"]}


def panel_label(ax: plt.Axes, label: str) -> None:
    ax.text(
        0.01,
        0.98,
        label,
        transform=ax.transAxes,
        fontweight="bold",
        fontsize=9,
        va="top",
        ha="left",
        bbox={"facecolor": "white", "edgecolor": "none", "pad": 0.4, "alpha": 0.85},
        zorder=10,
    )


def short_policy(policy: str) -> str:
    return POLICY_LABELS.get(policy, policy)


def short_mask(mask: str) -> str:
    return MASK_LABELS.get(mask, mask)


def short_kernel(kernel: str) -> str:
    return KERNEL_LABELS.get(kernel, kernel)


def fraction_to_pp(values: pd.Series | np.ndarray | list[float]) -> np.ndarray:
    return np.asarray(values, dtype=float) * 100.0


def style_axis(ax: plt.Axes, ylabel: str | None = None, xlabel: str | None = None) -> None:
    ax.grid(axis="y", color="#E6E6E6", linewidth=0.55)
    if ylabel:
        ax.set_ylabel(ylabel)
    if xlabel:
        ax.set_xlabel(xlabel)


def add_direct_label(ax: plt.Axes, x: float, y: float, text: str, color: str) -> None:
    if math.isfinite(float(y)):
        ax.text(x, y, text, color=color, fontsize=6.2, va="center", ha="left")


def get_value(df: pd.DataFrame, filters: dict[str, Any], column: str, default: float = math.nan) -> float:
    if df.empty or column not in df.columns:
        return default
    mask = pd.Series(True, index=df.index)
    for key, value in filters.items():
        if key not in df.columns:
            return default
        mask &= df[key].astype(str) == str(value)
    rows = df.loc[mask, column].dropna()
    if rows.empty:
        return default
    return float(rows.iloc[0])


def markdown_table(df: pd.DataFrame, columns: list[str]) -> str:
    """Small dependency-free Markdown table writer."""
    if df.empty:
        return "_No rows._"
    rows = df.loc[:, columns].fillna("").astype(str).values.tolist()
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join(["---"] * len(columns)) + " |"
    body = ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join([header, sep, *body])


def figure1(
    tables: dict[str, pd.DataFrame], dirs: dict[str, Path], warnings: list[dict[str, Any]]
) -> tuple[dict[str, str], Path]:
    vis = tables["vis_policy"]
    psf = select_main_threshold(tables["psf_kernel_metrics"])
    boundary = select_main_threshold(tables["psf_boundary_scene"])
    gap = select_main_threshold(tables["psf_meteosat_gap"])

    panel_a = vis.loc[
        vis["policy"].isin(POLICY_ORDER) & vis["mask_name"].isin(MASK_ORDER),
        ["policy", "mask_name", "agreement_weighted", "f1_cloud_weighted", "iou_cloud_weighted", "n_valid_total"],
    ].copy()
    panel_a["panel"] = "a_policy_vis_agreement"

    panel_b = psf.loc[
        psf["policy"].isin(POLICY_ORDER) & psf["kernel_name"].isin(KERNEL_ORDER),
        ["policy", "threshold", "kernel_name", "agreement_weighted", "f1_cloud_weighted", "iou_cloud_weighted", "n_valid_total"],
    ].copy()
    panel_b["nearest_agreement"] = panel_b.groupby("policy")["agreement_weighted"].transform(
        lambda s: float(panel_b.loc[s.index[panel_b.loc[s.index, "kernel_name"].astype(str) == "K0_nearest"], "agreement_weighted"].iloc[0])
        if any(panel_b.loc[s.index, "kernel_name"].astype(str) == "K0_nearest")
        else np.nan
    )
    panel_b["delta_agreement"] = panel_b["agreement_weighted"] - panel_b["nearest_agreement"]
    panel_b["panel"] = "b_psf_delta_vs_nearest"

    panel_c = boundary.loc[
        boundary["policy"].isin(POLICY_ORDER) & boundary["kernel_name"].isin(KERNEL_ORDER),
        ["boundary_scene", "policy", "threshold", "kernel_name", "agreement_weighted", "f1_cloud_weighted", "iou_cloud_weighted", "n_valid_total"],
    ].copy()
    panel_c["nearest_agreement"] = panel_c.groupby(["boundary_scene", "policy"])["agreement_weighted"].transform(
        lambda s: float(
            panel_c.loc[s.index[panel_c.loc[s.index, "kernel_name"].astype(str) == "K0_nearest"], "agreement_weighted"].iloc[0]
        )
        if any(panel_c.loc[s.index, "kernel_name"].astype(str) == "K0_nearest")
        else np.nan
    )
    panel_c["delta_agreement"] = panel_c["agreement_weighted"] - panel_c["nearest_agreement"]
    best_idx = panel_c.loc[panel_c["kernel_name"].astype(str) != "K0_nearest"].groupby(["boundary_scene", "policy"])[
        "delta_agreement"
    ].idxmax()
    panel_c_best = panel_c.loc[best_idx.dropna()].copy() if len(best_idx) else panel_c.iloc[0:0].copy()
    panel_c_best["panel"] = "c_best_psf_delta_by_scene"

    panel_d = gap.loc[
        (gap["policy"] == "A_inclusive_binary") & gap["kernel_name"].isin(KERNEL_ORDER),
        [
            "policy",
            "threshold",
            "kernel_name",
            "east_asia_agreement",
            "goes_agreement",
            "meteosat_agreement",
            "best_non_meteosat_minus_meteosat",
            "delta_gap_vs_nearest",
        ],
    ].copy()
    panel_d["panel"] = "d_meteosat_gap_psf"

    source = pd.concat([panel_a, panel_b, panel_c_best, panel_d], ignore_index=True, sort=False)
    src_path = source_csv_path(dirs, "figure1_main_diagnostic_story")
    write_source(source, src_path, warnings)

    fig, axes = plt.subplots(2, 2, figsize=(7.2, 5.2), constrained_layout=True)
    ax = axes[0, 0]
    panel_label(ax, "a")
    for policy in POLICY_ORDER:
        sub = order_categorical(panel_a.loc[panel_a["policy"] == policy], "mask_name", MASK_ORDER)
        if sub.empty:
            continue
        x = np.arange(len(sub))
        y = sub["agreement_weighted"].to_numpy(dtype=float)
        ax.plot(x, y, marker="o", color=POLICY_COLORS[policy])
        add_direct_label(ax, x[-1] + 0.05, y[-1], short_policy(policy), POLICY_COLORS[policy])
    ax.set_xticks(np.arange(len(MASK_ORDER)))
    ax.set_xticklabels([short_mask(m) for m in MASK_ORDER])
    ax.set_ylim(0.42, 0.92)
    style_axis(ax, "Agreement\n(fraction)")
    ax.set_title("Baseline and VIS-controlled agreement")

    ax = axes[0, 1]
    panel_label(ax, "b")
    for policy in POLICY_ORDER:
        sub = order_categorical(panel_b.loc[panel_b["policy"] == policy], "kernel_name", KERNEL_ORDER)
        if sub.empty:
            continue
        x = np.arange(len(sub))
        y = fraction_to_pp(sub["delta_agreement"].to_numpy(dtype=float))
        ax.plot(x, y, marker="o", color=POLICY_COLORS[policy], label=short_policy(policy))
    ax.axhline(0, color="#777777", linewidth=0.7)
    ax.set_xticks(np.arange(len(KERNEL_ORDER)))
    ax.set_xticklabels([short_kernel(k) for k in KERNEL_ORDER])
    style_axis(ax, "Delta agreement\n(percentage points)")
    ax.set_title("PSF-like kernels add only modest lift")
    ax.legend(frameon=False, loc="upper left")

    ax = axes[1, 0]
    panel_label(ax, "c")
    scene_order = ["non_boundary", "boundary_or_broken_cloud"]
    scene_labels = ["Non-boundary", "Boundary/\nbroken cloud"]
    x = np.arange(len(scene_order))
    width = 0.23
    for i, policy in enumerate(POLICY_ORDER):
        vals = []
        labels = []
        for scene in scene_order:
            row = panel_c_best.loc[(panel_c_best["boundary_scene"] == scene) & (panel_c_best["policy"] == policy)]
            vals.append(float(row["delta_agreement"].iloc[0]) * 100 if not row.empty else np.nan)
            labels.append(str(row["kernel_name"].iloc[0]) if not row.empty else "")
        ax.bar(x + (i - 1) * width, vals, width=width, color=POLICY_COLORS[policy], label=short_policy(policy))
        for xi, val, kernel in zip(x + (i - 1) * width, vals, labels):
            if math.isfinite(val):
                ax.text(xi, val + 0.08, kernel.split("_")[0], ha="center", va="bottom", fontsize=5.3, rotation=90)
    ax.axhline(0, color="#777777", linewidth=0.7)
    ax.set_xticks(x)
    ax.set_xticklabels(scene_labels)
    style_axis(ax, "Best PSF delta\n(percentage points)")
    ax.set_title("Boundary pixels benefit more, but not enough")
    ax.legend(frameon=False, ncol=3, loc="upper left")

    ax = axes[1, 1]
    panel_label(ax, "d")
    panel_d = order_categorical(panel_d, "kernel_name", KERNEL_ORDER)
    x = np.arange(len(panel_d))
    for col, label, color in [
        ("meteosat_agreement", "Meteosat", SOURCE_COLORS["Meteosat"]),
        ("goes_agreement", "GOES", SOURCE_COLORS["GOES"]),
        ("east_asia_agreement", "East Asia", SOURCE_COLORS["East Asia"]),
    ]:
        ax.plot(x, panel_d[col].to_numpy(dtype=float), marker="o", color=color, label=label)
    ax.set_xticks(x)
    ax.set_xticklabels([short_kernel(str(k)) for k in panel_d["kernel_name"]])
    ax.set_ylim(0.48, 0.84)
    style_axis(ax, "Agreement\n(fraction)")
    ax2 = ax.twinx()
    ax2.plot(
        x,
        panel_d["best_non_meteosat_minus_meteosat"].to_numpy(dtype=float),
        color="#555555",
        marker="s",
        linestyle="--",
        linewidth=1.0,
        label="Best non-Met minus Met",
    )
    ax2.set_ylabel("Gap (fraction)", fontsize=7)
    ax2.tick_params(labelsize=6.2)
    ax2.spines["top"].set_visible(False)
    ax2.set_ylim(0.16, 0.27)
    ax.set_title("Meteosat gap persists under PSF-like kernels")
    lines, labels = ax.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax.legend(lines + lines2, labels + labels2, frameon=False, loc="lower right")

    paths = save_figure(fig, dirs, "figure1_main_diagnostic_story")
    return paths, src_path


def figure2(
    tables: dict[str, pd.DataFrame], dirs: dict[str, Path], warnings: list[dict[str, Any]]
) -> tuple[dict[str, str], Path]:
    consistency = tables["selqc_valid_count"].copy()
    oracle = tables["selqc_oracle"].copy()
    common = tables["selqc_common_valid"].copy()

    panel_a = consistency.loc[
        consistency["policy"].isin(POLICY_ORDER)
        & consistency["pixel_group"].isin(["ALL_VALID", "valid_source_count_1", "valid_source_count_ge4"]),
        [
            "policy",
            "pixel_group",
            "n_valid_total",
            "equal_count_fraction_weighted",
            "available_gt_stage06_fraction_weighted",
            "available_lt_stage06_fraction_weighted",
        ],
    ].copy()
    panel_a["mismatch_fraction"] = 1.0 - panel_a["equal_count_fraction_weighted"]
    panel_a["panel"] = "a_one_minus_equal_count_fraction"

    panel_b = panel_a.copy()
    panel_b["panel"] = "b_mismatch_direction"

    oracle_groups = [
        "ALL_VALID",
        "boundary_or_broken_cloud",
        "non_boundary",
        "selected_MeteosatIODC",
        "valid_source_count_ge4",
    ]
    panel_c = oracle.loc[
        (oracle["policy"] == "A_inclusive_binary") & oracle["pixel_group"].isin(oracle_groups),
        [
            "policy",
            "pixel_group",
            "n_valid_total",
            "current_selected_agreement_weighted",
            "sample_group_level_best_agreement_weighted",
            "pixel_level_oracle_agreement_weighted",
            "sample_group_regret_weighted",
            "pixel_oracle_regret_weighted",
        ],
    ].copy()
    panel_c["panel"] = "c_oracle_granularity"

    panel_d = common.loc[
        (common["policy"] == "A_inclusive_binary")
        & (common["pixel_group"] == "ALL_VALID")
        & common["source_set"].isin(["FY4B_and_IODC", "Meteosat0deg_and_GOES16", "valid_count_ge4_all_available"]),
        [
            "policy",
            "pixel_group",
            "source_set",
            "source_name",
            "n_common_valid_total",
            "agreement_weighted",
            "f1_cloud_weighted",
            "iou_cloud_weighted",
            "cloud_fraction_bias_weighted",
        ],
    ].copy()
    panel_d["panel"] = "d_common_valid_source_contrast"

    source = pd.concat([panel_a, panel_b, panel_c, panel_d], ignore_index=True, sort=False)
    src_path = source_csv_path(dirs, "figure2_source_selection_valid_count_qc")
    write_source(source, src_path, warnings)

    fig, axes = plt.subplots(2, 2, figsize=(7.2, 5.15), constrained_layout=True)
    group_order = ["ALL_VALID", "valid_source_count_1", "valid_source_count_ge4"]
    group_labels = ["All valid", "Count=1", "Count>=4"]

    ax = axes[0, 0]
    panel_label(ax, "a")
    x = np.arange(len(group_order))
    width = 0.23
    min_positive = 1e-6
    for i, policy in enumerate(POLICY_ORDER):
        vals = []
        for group in group_order:
            val = get_value(panel_a, {"policy": policy, "pixel_group": group}, "mismatch_fraction", 0.0)
            vals.append(max(val, min_positive))
        ax.bar(x + (i - 1) * width, vals, width=width, color=POLICY_COLORS[policy], label=short_policy(policy))
    ax.set_yscale("log")
    ax.set_ylim(8e-7, 1.2)
    ax.set_xticks(x)
    ax.set_xticklabels(group_labels)
    style_axis(ax, "1 - equal count\n(fraction)")
    ax.set_title("Policy B exposes valid-count QC mismatch")
    ax.legend(frameon=False, loc="upper left")

    ax = axes[0, 1]
    panel_label(ax, "b")
    labels = []
    lt_vals = []
    gt_vals = []
    colors = []
    for policy in POLICY_ORDER:
        for group in ["ALL_VALID", "valid_source_count_ge4"]:
            labels.append(f"{short_policy(policy).replace('Policy ', '')}\n{group.replace('valid_source_count_', '')}")
            lt_vals.append(get_value(panel_b, {"policy": policy, "pixel_group": group}, "available_lt_stage06_fraction_weighted", 0.0) * 100)
            gt_vals.append(get_value(panel_b, {"policy": policy, "pixel_group": group}, "available_gt_stage06_fraction_weighted", 0.0) * 100)
            colors.append(POLICY_COLORS[policy])
    xx = np.arange(len(labels))
    ax.bar(xx, lt_vals, color=SOURCE_COLORS["QC"], label="available < Stage06")
    ax.bar(xx, gt_vals, bottom=lt_vals, color="#F2A65A", label="available > Stage06")
    ax.set_xticks(xx)
    ax.set_xticklabels(labels)
    style_axis(ax, "Mismatch direction\n(% of valid pixels)")
    ax.set_title("Mismatch direction is mostly loss under Policy B")
    ax.legend(frameon=False, loc="upper left")

    ax = axes[1, 0]
    panel_label(ax, "c")
    x = np.arange(len(oracle_groups))
    width = 0.22
    metrics = [
        ("current_selected_agreement_weighted", "Current", "#777777"),
        ("sample_group_level_best_agreement_weighted", "Sample oracle", "#3B6FB6"),
        ("pixel_level_oracle_agreement_weighted", "Pixel oracle", "#2AA198"),
    ]
    for i, (col, label, color) in enumerate(metrics):
        vals = [get_value(panel_c, {"pixel_group": group}, col) for group in oracle_groups]
        ax.bar(x + (i - 1) * width, vals, width=width, color=color, label=label)
    ax.set_xticks(x)
    ax.set_xticklabels(["All", "Boundary", "Non-boundary", "sel-IODC", "count>=4"], rotation=20, ha="right")
    ax.set_ylim(0.42, 0.88)
    style_axis(ax, "Agreement\n(fraction)")
    ax.set_title("Oracle gap separates selection from intrinsic scene limits")
    ax.legend(frameon=False, loc="upper left", ncol=3)

    ax = axes[1, 1]
    panel_label(ax, "d")
    contrast_sets = ["FY4B_and_IODC", "Meteosat0deg_and_GOES16", "valid_count_ge4_all_available"]
    y_base = np.arange(len(contrast_sets))
    for yi, source_set in zip(y_base, contrast_sets):
        sub = panel_d.loc[panel_d["source_set"] == source_set].sort_values("agreement_weighted")
        if sub.empty:
            continue
        xs = sub["agreement_weighted"].to_numpy(dtype=float)
        ax.plot([np.nanmin(xs), np.nanmax(xs)], [yi, yi], color="#BBBBBB", linewidth=1.0, zorder=1)
        for _, row in sub.iterrows():
            name = str(row["source_name"])
            ax.scatter(row["agreement_weighted"], yi, s=22, color=SOURCE_COLORS.get(name, "#777777"), zorder=2)
            ax.text(row["agreement_weighted"] + 0.006, yi, name.replace("Meteosat-", "Met-"), va="center", fontsize=5.8)
    ax.set_yticks(y_base)
    ax.set_yticklabels(["FY4B vs IODC", "Met0 vs GOES16", "count>=4\nall sources"])
    ax.set_xlim(0.25, 0.86)
    ax.set_ylim(-0.35, len(contrast_sets) - 0.15)
    style_axis(ax, xlabel="Agreement (fraction)")
    ax.set_title("Same-pixel common-valid source contrasts")

    paths = save_figure(fig, dirs, "figure2_source_selection_valid_count_qc")
    return paths, src_path


def figure3(
    tables: dict[str, pd.DataFrame], dirs: dict[str, Path], warnings: list[dict[str, Any]]
) -> tuple[dict[str, str], Path]:
    gap = tables["vis_meteosat_gap"].copy()
    pair = tables["vis_source_pair"].copy()
    vcount = tables["vis_valid_source_count"].copy()
    common = tables["selqc_common_valid"].copy()
    masks = ["VIS-0_baseline_current", "VIS-3_lat60_visible", "VIS-6_non_boundary_visible", "VIS-7_boundary_visible"]

    panel_a = gap.loc[
        (gap["policy"] == "A_inclusive_binary") & gap["mask_name"].isin(MASK_ORDER),
        ["policy", "mask_name", "meteosat_agreement", "goes_agreement", "east_asia_agreement"],
    ].copy()
    panel_a["panel"] = "a_source_family_vis"

    panel_b = pair.loc[
        (pair["policy"] == "A_inclusive_binary") & pair["mask_name"].isin(masks) & pair["pair"].isin(PAIR_ORDER),
        [
            "policy",
            "mask_name",
            "pair",
            "sample_count",
            "n_overlap_valid_total",
            "source_disagreement_fraction_weighted",
            "source_A_agreement_weighted",
            "source_B_agreement_weighted",
            "A_f1_weighted",
            "B_f1_weighted",
            "A_iou_weighted",
            "B_iou_weighted",
        ],
    ].copy()
    panel_b["panel"] = "b_source_pair_disagreement"

    panel_c = vcount.loc[
        (vcount["policy"] == "A_inclusive_binary") & vcount["mask_name"].isin(masks),
        ["policy", "mask_name", "strata_value", "n_valid_total", "agreement_weighted", "f1_cloud_weighted", "iou_cloud_weighted"],
    ].copy()
    panel_c["panel"] = "c_valid_source_count"

    panel_d = common.loc[
        (common["policy"] == "A_inclusive_binary")
        & (common["pixel_group"] == "selected_MeteosatIODC")
        & common["source_set"].isin(["FY4B_and_IODC", "Himawari_and_IODC", "valid_count_ge4_all_available"]),
        [
            "policy",
            "pixel_group",
            "source_set",
            "source_name",
            "n_common_valid_total",
            "agreement_weighted",
            "f1_cloud_weighted",
            "iou_cloud_weighted",
            "cloud_fraction_bias_weighted",
        ],
    ].copy()
    panel_d["panel"] = "d_selected_iodc_common_valid_alternatives"

    source = pd.concat([panel_a, panel_b, panel_c, panel_d], ignore_index=True, sort=False)
    src_path = source_csv_path(dirs, "figure3_source_family_pair_evidence")
    write_source(source, src_path, warnings)

    fig, axes = plt.subplots(2, 2, figsize=(7.2, 5.35), constrained_layout=True)

    ax = axes[0, 0]
    panel_label(ax, "a")
    panel_a = order_categorical(panel_a, "mask_name", MASK_ORDER)
    x = np.arange(len(panel_a))
    for col, label, color in [
        ("meteosat_agreement", "Meteosat", SOURCE_COLORS["Meteosat"]),
        ("goes_agreement", "GOES", SOURCE_COLORS["GOES"]),
        ("east_asia_agreement", "East Asia", SOURCE_COLORS["East Asia"]),
    ]:
        ax.plot(x, panel_a[col].to_numpy(dtype=float), marker="o", color=color, label=label)
    ax.set_xticks(x)
    ax.set_xticklabels([short_mask(str(m)) for m in panel_a["mask_name"]])
    ax.set_ylim(0.42, 0.92)
    style_axis(ax, "Agreement\n(fraction)")
    ax.set_title("Source-family signal remains under VIS masks")
    ax.legend(frameon=False, loc="lower right")

    ax = axes[0, 1]
    panel_label(ax, "b")
    heat = (
        panel_b.assign(mask_name=pd.Categorical(panel_b["mask_name"], categories=masks, ordered=True))
        .pivot(index="pair", columns="mask_name", values="source_disagreement_fraction_weighted")
        .reindex(PAIR_ORDER)
    )
    im = ax.imshow(heat.to_numpy(dtype=float) * 100, cmap="YlOrRd", vmin=0, vmax=np.nanmax(heat.to_numpy(dtype=float) * 100))
    ax.set_xticks(np.arange(len(masks)))
    ax.set_xticklabels([short_mask(m).replace("\n", " ") for m in masks], rotation=35, ha="right")
    ax.set_yticks(np.arange(len(PAIR_ORDER)))
    ax.set_yticklabels([p.replace("Meteosat-", "Met-") for p in PAIR_ORDER])
    for i in range(heat.shape[0]):
        for j in range(heat.shape[1]):
            val = heat.iloc[i, j]
            if pd.notna(val):
                ax.text(j, i, f"{val * 100:.0f}", ha="center", va="center", fontsize=5.5, color="#222222")
    cbar = fig.colorbar(im, ax=ax, shrink=0.82, pad=0.02)
    cbar.set_label("Disagreement (%)", fontsize=7)
    cbar.ax.tick_params(labelsize=6.2)
    ax.set_title("Same-pixel source-pair disagreement")

    ax = axes[1, 0]
    panel_label(ax, "c")
    strata_order = ["1", "2", "3", ">=4"]
    x = np.arange(len(strata_order))
    for mask, color in zip(masks, ["#777777", "#3B6FB6", "#2AA198", "#B94A48"]):
        sub = panel_c.loc[panel_c["mask_name"] == mask].copy()
        vals = [get_value(sub, {"strata_value": s}, "agreement_weighted") for s in strata_order]
        ax.plot(x, vals, marker="o", color=color, label=short_mask(mask).replace("\n", " "))
    ax.set_xticks(x)
    ax.set_xticklabels(strata_order)
    ax.set_ylim(0.42, 0.9)
    style_axis(ax, "Agreement\n(fraction)", "Stage06 valid source count")
    ax.set_title("valid_source_count >=4 is consistently low")
    ax.legend(frameon=False, loc="upper right")

    ax = axes[1, 1]
    panel_label(ax, "d")
    sub = panel_d.copy()
    order = ["FY4B", "Himawari-9", "Meteosat-0deg", "Meteosat-IODC"]
    y = np.arange(len(order))
    vals = []
    labels = []
    for src in order:
        src_rows = sub.loc[sub["source_name"] == src]
        if src_rows.empty:
            vals.append(np.nan)
            labels.append("")
        else:
            best = src_rows.sort_values("n_common_valid_total", ascending=False).iloc[0]
            vals.append(float(best["agreement_weighted"]))
            labels.append(str(best["source_set"]))
    ax.barh(y, vals, color=[SOURCE_COLORS.get(src, "#777777") for src in order])
    ax.set_yticks(y)
    ax.set_yticklabels(order)
    ax.set_xlim(0.30, 0.82)
    for yi, val, source_set in zip(y, vals, labels):
        if math.isfinite(val):
            ax.text(val + 0.006, yi, f"{val:.3f}", va="center", fontsize=6.0)
            ax.text(0.425, yi - 0.28, source_set.replace("_", " "), va="center", fontsize=5.2, color="#666666")
    style_axis(ax, xlabel="Agreement (fraction)")
    ax.set_title("Alternatives inside selected_MeteosatIODC pixels")

    paths = save_figure(fig, dirs, "figure3_source_family_pair_evidence")
    return paths, src_path


def figure4(
    tables: dict[str, pd.DataFrame], dirs: dict[str, Path], warnings: list[dict[str, Any]]
) -> tuple[dict[str, str], Path]:
    vis = tables["vis_policy"].copy()
    psf = select_main_threshold(tables["psf_kernel_metrics"])
    boundary = select_main_threshold(tables["psf_boundary_scene"])
    gap = select_main_threshold(tables["psf_meteosat_gap"])
    consistency = tables["selqc_valid_count"].copy()
    oracle = tables["selqc_oracle"].copy()

    policy = "A_inclusive_binary"
    base = get_value(vis, {"policy": policy, "mask_name": "VIS-0_baseline_current"}, "agreement_weighted")
    lat60 = get_value(vis, {"policy": policy, "mask_name": "VIS-3_lat60_visible"}, "agreement_weighted")
    clean = get_value(vis, {"policy": policy, "mask_name": "VIS-5_clean_core"}, "agreement_weighted")
    psf_sub = psf.loc[(psf["policy"] == policy) & psf["kernel_name"].isin(KERNEL_ORDER)].copy()
    nearest = get_value(psf_sub, {"kernel_name": "K0_nearest"}, "agreement_weighted")
    psf_sub["delta"] = psf_sub["agreement_weighted"] - nearest
    psf_best = float(psf_sub.loc[psf_sub["kernel_name"] != "K0_nearest", "delta"].max())

    b_sub = boundary.loc[(boundary["policy"] == policy) & boundary["kernel_name"].isin(KERNEL_ORDER)].copy()
    b_nearest = get_value(
        b_sub,
        {"boundary_scene": "boundary_or_broken_cloud", "kernel_name": "K0_nearest"},
        "agreement_weighted",
    )
    nb_nearest = get_value(b_sub, {"boundary_scene": "non_boundary", "kernel_name": "K0_nearest"}, "agreement_weighted")
    boundary_best = float(
        (
            b_sub.loc[b_sub["boundary_scene"] == "boundary_or_broken_cloud", "agreement_weighted"] - b_nearest
        ).max()
    )
    non_boundary_best = float((b_sub.loc[b_sub["boundary_scene"] == "non_boundary", "agreement_weighted"] - nb_nearest).max())

    gap_a = gap.loc[(gap["policy"] == policy) & gap["kernel_name"].isin(KERNEL_ORDER)].copy()
    gap_k0 = get_value(gap_a, {"kernel_name": "K0_nearest"}, "best_non_meteosat_minus_meteosat")
    gap_best = float(gap_a["best_non_meteosat_minus_meteosat"].max())
    b_ge4_lt = get_value(
        consistency,
        {"policy": "B_high_confidence_only", "pixel_group": "valid_source_count_ge4"},
        "available_lt_stage06_fraction_weighted",
    )
    iodc_current = get_value(
        oracle,
        {"policy": policy, "pixel_group": "selected_MeteosatIODC"},
        "current_selected_agreement_weighted",
    )
    iodc_pixel_oracle = get_value(
        oracle,
        {"policy": policy, "pixel_group": "selected_MeteosatIODC"},
        "pixel_level_oracle_agreement_weighted",
    )

    rows = [
        {
            "panel": "take_home",
            "evidence": "VIS lat60 control",
            "metric": "VIS-3 minus VIS-0 agreement",
            "value_fraction": lat60 - base,
            "value_percentage_points": (lat60 - base) * 100,
            "interpretation": "Visibility/latitude filtering barely changes the main agreement.",
        },
        {
            "panel": "take_home",
            "evidence": "VIS clean core",
            "metric": "VIS-5 minus VIS-0 agreement",
            "value_fraction": clean - base,
            "value_percentage_points": (clean - base) * 100,
            "interpretation": "Clean core is high but removes several mechanisms simultaneously.",
        },
        {
            "panel": "take_home",
            "evidence": "PSF-like aggregation",
            "metric": "Best kernel minus nearest agreement",
            "value_fraction": psf_best,
            "value_percentage_points": psf_best * 100,
            "interpretation": "PSF-like aggregation is a modest effect at whole-sample scale.",
        },
        {
            "panel": "take_home",
            "evidence": "Boundary scene",
            "metric": "Boundary best PSF delta",
            "value_fraction": boundary_best,
            "value_percentage_points": boundary_best * 100,
            "interpretation": "Boundary/broken-cloud pixels benefit more than non-boundary pixels.",
        },
        {
            "panel": "take_home",
            "evidence": "Non-boundary scene",
            "metric": "Non-boundary best PSF delta",
            "value_fraction": non_boundary_best,
            "value_percentage_points": non_boundary_best * 100,
            "interpretation": "Non-boundary PSF lift is smaller.",
        },
        {
            "panel": "take_home",
            "evidence": "Meteosat gap",
            "metric": "Gap nearest to max kernel",
            "value_fraction": gap_best - gap_k0,
            "value_percentage_points": (gap_best - gap_k0) * 100,
            "interpretation": "PSF kernels do not close the Meteosat gap.",
        },
        {
            "panel": "take_home",
            "evidence": "Policy B QC",
            "metric": "available < Stage06 for valid_source_count>=4",
            "value_fraction": b_ge4_lt,
            "value_percentage_points": b_ge4_lt * 100,
            "interpretation": "Policy B count discrepancy is a confidence-mask QC issue.",
        },
        {
            "panel": "take_home",
            "evidence": "selected_MeteosatIODC",
            "metric": "Pixel oracle minus current selected agreement",
            "value_fraction": iodc_pixel_oracle - iodc_current,
            "value_percentage_points": (iodc_pixel_oracle - iodc_current) * 100,
            "interpretation": "Source selection contributes inside selected-IODC pixels.",
        },
    ]
    source = pd.DataFrame(rows)
    src_path = source_csv_path(dirs, "figure4_take_home_summary")
    write_source(source, src_path, warnings)

    fig, ax = plt.subplots(figsize=(7.2, 4.05), constrained_layout=True)
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)

    ax.text(0.02, 0.96, "Stage 09E take-home diagnostic chain", fontsize=11, fontweight="bold", va="top")
    ax.text(
        0.02,
        0.89,
        "EPIC is used as an independent diagnostic reference, not an absolute truth label.",
        fontsize=7.2,
        color="#555555",
        va="top",
    )

    boxes = [
        (
            0.03,
            0.58,
            0.21,
            0.22,
            "VIS controls",
            f"lat60 delta {((lat60 - base) * 100):+.2f} pp\nclean core +{((clean - base) * 100):.1f} pp",
            "#EAF1FB",
            POLICY_COLORS["A_inclusive_binary"],
        ),
        (
            0.28,
            0.58,
            0.21,
            0.22,
            "PSF-like kernels",
            f"whole-sample max {psf_best * 100:+.2f} pp\nboundary max {boundary_best * 100:+.2f} pp",
            "#EDF7F4",
            POLICY_COLORS["B_high_confidence_only"],
        ),
        (
            0.53,
            0.58,
            0.21,
            0.22,
            "Meteosat gap",
            f"K0 gap {gap_k0:.3f}\nmax gap {gap_best:.3f}",
            "#F8ECEB",
            SOURCE_COLORS["Meteosat"],
        ),
        (
            0.78,
            0.58,
            0.19,
            0.22,
            "SEL-QC",
            f"Policy B count>=4\nloss {b_ge4_lt * 100:.1f}%",
            "#FFF0E4",
            SOURCE_COLORS["QC"],
        ),
        (
            0.17,
            0.22,
            0.29,
            0.20,
            "Selection effect",
            f"selected-IODC oracle lift\n{(iodc_pixel_oracle - iodc_current) * 100:+.1f} pp",
            "#F2F2F2",
            "#666666",
        ),
        (
            0.54,
            0.22,
            0.32,
            0.20,
            "Working conclusion",
            "No single geometric or PSF factor explains 60-80% agreement.\nSource family, selection, boundary scene and QC act together.",
            "#F7F7F7",
            "#333333",
        ),
    ]
    for x, y, w, h, title, body, face, edge in boxes:
        ax.add_patch(Rectangle((x, y), w, h, facecolor=face, edgecolor=edge, linewidth=1.1))
        ax.text(x + 0.015, y + h - 0.04, title, fontsize=8.5, fontweight="bold", va="top", color=edge)
        ax.text(x + 0.015, y + h - 0.095, body, fontsize=7.0, va="top", color="#222222", linespacing=1.25)
    for x0, x1 in [(0.24, 0.28), (0.49, 0.53), (0.74, 0.78)]:
        ax.annotate("", xy=(x1, 0.69), xytext=(x0, 0.69), arrowprops={"arrowstyle": "->", "color": "#777777", "lw": 1.0})
    ax.annotate("", xy=(0.46, 0.32), xytext=(0.53, 0.58), arrowprops={"arrowstyle": "->", "color": "#777777", "lw": 1.0})
    ax.annotate("", xy=(0.54, 0.32), xytext=(0.49, 0.58), arrowprops={"arrowstyle": "->", "color": "#777777", "lw": 1.0})
    ax.text(0.02, 0.07, "Units: pp = percentage points; all values trace to the figure source CSV.", fontsize=6.5, color="#666666")

    paths = save_figure(fig, dirs, "figure4_take_home_summary")
    return paths, src_path


def write_report(
    dirs: dict[str, Path],
    figure_index: pd.DataFrame,
    manifest: dict[str, Any],
    tables: dict[str, pd.DataFrame],
) -> Path:
    vis = tables["vis_policy"]
    gap = tables["vis_meteosat_gap"]
    consistency = tables["selqc_valid_count"]
    policy = "A_inclusive_binary"
    base = get_value(vis, {"policy": policy, "mask_name": "VIS-0_baseline_current"}, "agreement_weighted")
    lat60 = get_value(vis, {"policy": policy, "mask_name": "VIS-3_lat60_visible"}, "agreement_weighted")
    clean = get_value(vis, {"policy": policy, "mask_name": "VIS-5_clean_core"}, "agreement_weighted")
    met0 = get_value(gap, {"policy": policy, "mask_name": "VIS-0_baseline_current"}, "meteosat_agreement")
    goes0 = get_value(gap, {"policy": policy, "mask_name": "VIS-0_baseline_current"}, "goes_agreement")
    b_ge4_lt = get_value(
        consistency,
        {"policy": "B_high_confidence_only", "pixel_group": "valid_source_count_ge4"},
        "available_lt_stage06_fraction_weighted",
    )

    lines = [
        f"# {STAGE_ID.upper()} Nature-style 组会图表报告",
        "",
        f"- Run ID: `{RUN_ID}`",
        f"- Created UTC: `{manifest['created_utc']}`",
        "- 数据范围：仅使用当前已有的 2024-03 Stage 09D/09E CSV 汇总表。",
        "- 处理边界：未重跑像元级诊断，未联网下载，未修改 fused cloud mask 生产逻辑。",
        "- 制图后端：Python/matplotlib；SVG 文本保持可编辑，PDF 使用 TrueType 字体，TIFF 为 600 dpi。",
        "",
        "## 直接结论",
        "",
        f"1. Policy A baseline agreement 为 `{base:.3f}`；限制到 VIS-3 lat60 visible 后为 `{lat60:.3f}`，变化仅 `{(lat60 - base) * 100:+.2f}` percentage points。",
        f"2. VIS-5 clean core agreement 为 `{clean:.3f}`，但该 mask 同时剔除了高纬、视角、边界和碎云机制，不能解释为单一几何因素。",
        f"3. VIS-0 下 Meteosat agreement `{met0:.3f}`，GOES agreement `{goes0:.3f}`；source-family 差异在 VIS masks 下仍然明显。",
        f"4. Policy B 在 `valid_source_count>=4` 的 `available < Stage06 count` 为 `{b_ge4_lt * 100:.1f}%`，这是 confidence-mask 条件下的 QC/定义差异，不应反推为 Policy A 的问题。",
        "5. PSF-like aggregation 的 whole-sample 提升很小，主要改善 boundary/broken-cloud 像元；Meteosat gap 没有被 PSF-like kernel 消除。",
        "",
        "## Figure Index",
        "",
        markdown_table(figure_index, ["figure_id", "title", "source_csv", "png", "svg", "pdf", "tiff"]),
        "",
        "## 图表说明",
        "",
        "### Figure 1 | Main diagnostic story",
        "Panel a 对比 Policy A/B/C 在 baseline、VIS、clean-core、boundary/non-boundary mask 下的 agreement。Panel b 显示 PSF-like kernel 相对 nearest 的 agreement delta。Panel c 将 PSF 改善拆到 boundary/broken-cloud 与 non-boundary 场景。Panel d 显示 Meteosat/GOES/East Asia 在 PSF kernel 下的 agreement 与 non-Meteosat-minus-Meteosat gap。",
        "",
        "### Figure 2 | Source-selection and valid-count QC",
        "Panel a 使用 `1 - equal_count_fraction` 并采用 log scale，避免 Policy A/C 的近 1.0 equal fraction 被柱状图视觉截断。Panel b 拆分 mismatch direction。Panel c 比较 current selected、sample-group oracle 与 pixel oracle。Panel d 使用 common-valid same-pixel 对比展示 source selection 效应。",
        "",
        "### Figure 3 | Source-family and source-pair evidence",
        "Panel a 展示 Meteosat/GOES/East Asia 在 VIS masks 下的 agreement。Panel b 是 source-pair disagreement heatmap。Panel c 显示 `valid_source_count>=4` 的低 agreement 不是单一 mask 偶然现象。Panel d 聚焦 selected_MeteosatIODC 像元中的 common-valid alternatives。",
        "",
        "### Figure 4 | One-slide take-home summary",
        "压缩展示证据链：VIS filtering 不消除主 gap；PSF-like aggregation 只带来 modest lift；Meteosat/source-selection effects 仍强；Policy B valid-count discrepancy 是 QC/定义问题。",
        "",
        "## Warnings",
        "",
    ]
    if manifest["warnings"]:
        for item in manifest["warnings"]:
            lines.append(f"- `{item.get('source', 'unknown')}`: {item.get('message', '')}")
    else:
        lines.append("- No warnings.")
    lines.append("")
    path = dirs["reports"] / f"{RUN_ID}_report_cn.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def verify_outputs(figure_index: pd.DataFrame, warnings: list[dict[str, Any]]) -> dict[str, Any]:
    checks = []
    for _, row in figure_index.iterrows():
        for col in ["svg", "pdf", "tiff", "png", "source_csv"]:
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
            checks.append({"figure_id": row["figure_id"], "artifact": "source_nonzero_rows", "path": str(src), "ok": bool(ok), "rows": n})
            if not ok:
                warnings.append({"level": "warning", "source": row["figure_id"], "message": f"source CSV has zero readable rows: {src}"})
    return {"checks": checks, "all_ok": all(item["ok"] for item in checks)}


def main() -> int:
    apply_publication_style()
    dirs = ensure_dirs()
    warnings: list[dict[str, Any]] = []
    tables = {name: read_table(name, warnings) for name in INPUTS}

    figures: list[dict[str, Any]] = []
    figure_specs = [
        ("figure1_main_diagnostic_story", "Figure 1 | Main diagnostic story", figure1),
        ("figure2_source_selection_valid_count_qc", "Figure 2 | Source-selection and valid-count QC", figure2),
        ("figure3_source_family_pair_evidence", "Figure 3 | Source-family and source-pair evidence", figure3),
        ("figure4_take_home_summary", "Figure 4 | One-slide take-home summary", figure4),
    ]

    for figure_id, title, fn in figure_specs:
        try:
            paths, src_path = fn(tables, dirs, warnings)
            figures.append(
                {
                    "figure_id": figure_id,
                    "title": title,
                    "source_csv": str(src_path),
                    "svg": paths["svg"],
                    "pdf": paths["pdf"],
                    "tiff": paths["tiff"],
                    "png": paths["png"],
                    "created_utc": utc_now(),
                }
            )
        except Exception as exc:  # pragma: no cover - run should continue and log partial products
            warnings.append({"level": "warning", "source": figure_id, "message": f"figure generation failed: {exc}"})

    figure_index = pd.DataFrame(figures)
    figure_index_path = dirs["logs"] / f"{RUN_ID}_figure_index.csv"
    figure_index.to_csv(figure_index_path, index=False, encoding="utf-8-sig")

    verification = verify_outputs(figure_index, warnings)
    manifest = {
        "stage_id": STAGE_ID,
        "run_id": RUN_ID,
        "created_utc": utc_now(),
        "script": str(Path(__file__).resolve()),
        "output_root": str(OUT_ROOT),
        "inputs": {name: str(path) for name, path in INPUTS.items()},
        "input_row_counts": {name: int(len(df)) for name, df in tables.items()},
        "figures": figures,
        "figure_index": str(figure_index_path),
        "warnings": warnings,
        "verification": verification,
        "constraints": {
            "no_pixel_level_rerun": True,
            "no_network_download": True,
            "no_fusion_logic_change": True,
            "epic_role": "independent diagnostic reference, not absolute truth",
        },
    }

    report_path = write_report(dirs, figure_index, manifest, tables)
    manifest["report"] = str(report_path)
    manifest_path = dirs["logs"] / f"{RUN_ID}_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    warnings_path = dirs["logs"] / f"{RUN_ID}_warnings.csv"
    pd.DataFrame(warnings).to_csv(warnings_path, index=False, encoding="utf-8-sig")
    print(json.dumps({"output_root": str(OUT_ROOT), "figures": len(figures), "all_ok": verification["all_ok"]}, ensure_ascii=False))
    return 0 if verification["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
