# -*- coding: utf-8 -*-
"""Stage 10 Nature-style group-meeting figures.

This script reads existing Stage 09F, Stage 10, Stage 10P, and Stage 10P2
diagnostic tables. It does not rerun upstream production stages, does not
download data, and treats EPIC A-band Effective Cloud Height as an independent
diagnostic reference rather than an absolute geometric cloud-top truth.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch, Rectangle
import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import path_config  # noqa: E402

PROJECT_ID = "geo_ring_cloud"
STAGE_ID = "stage_10"
RUN_ID = "stage_10_meeting_figures_202403"

STAGE09F_ROOT = path_config.RUNS_ROOT / "stage_09f_spatial_story_maps_202403"
STAGE10_ROOT = path_config.RUNS_ROOT / "stage_10_cth_fused_product_validation_202403"
STAGE10P_ROOT_CANONICAL = path_config.RUNS_ROOT / "stage_10p_psf_inventory_202401"
STAGE10P_ROOT_LEGACY = path_config.RUNS_ROOT / "stage10p_psf_inventory_202401"
STAGE10P2_ROOT_CANONICAL = path_config.RUNS_ROOT / "stage_10p2_approx_epic_fov_aggregation_202403"
STAGE10P2_ROOT_LEGACY = path_config.RUNS_ROOT / "stage10p2_approx_epic_fov_aggregation_202403"
OUT_ROOT = path_config.RUNS_ROOT / RUN_ID

POLICY_A = "A_inclusive_binary"
POLICY_B = "B_high_confidence_only"

COLORS = {
    "blue": "#2F5D9B",
    "blue_light": "#8FB3D9",
    "green": "#3B8F6B",
    "green_light": "#A8D5BA",
    "red": "#B94A48",
    "red_light": "#E0A3A1",
    "gold": "#C88A2D",
    "purple": "#7B6BA8",
    "teal": "#3F9CA6",
    "grey": "#767676",
    "grey_light": "#D7D7D7",
    "black": "#272727",
}

SOURCE_COLORS = {
    "FY4B": "#3B8F6B",
    "GOES-16": "#2F5D9B",
    "GOES-18": "#8FB3D9",
    "Himawari-9": "#62A85B",
    "Meteosat-0deg": "#B94A48",
    "Meteosat-IODC": "#D1846A",
}

DOMAIN_LABELS = {
    "D0_common_valid_cth": "D0 common\nvalid CTH",
    "D1_both_cloud": "D1 both\ncloud",
    "D3_EPIC_cloud_GEO_not_cloud": "D3 EPIC cloud\nGEO clear",
    "D4_GEO_cloud_EPIC_not_cloud": "D4 GEO cloud\nEPIC clear",
    "D5_clean_core_cloud": "D5 clean\ncore",
    "D6_boundary_or_broken_cloud": "D6 boundary/\nbroken",
    "D7_high_cloud": "D7 high\ncloud",
}

METHOD_ORDER = [
    "box_3x3",
    "box_5x5",
    "box_7x7",
    "gaussian_5x5_sigma1p0",
    "gaussian_7x7_sigma1p5",
    "gaussian_9x9_sigma2p0",
]

METHOD_LABELS = {
    "nearest": "nearest",
    "box_3x3": "box 3x3",
    "box_5x5": "box 5x5",
    "box_7x7": "box 7x7",
    "gaussian_5x5_sigma1p0": "gauss 5x5",
    "gaussian_7x7_sigma1p5": "gauss 7x7",
    "gaussian_9x9_sigma2p0": "gauss 9x9",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def apply_publication_style() -> None:
    plt.rcParams.update(
        {
            "figure.dpi": 160,
            "savefig.dpi": 600,
            "font.family": "sans-serif",
            "font.sans-serif": ["Arial", "DejaVu Sans", "Liberation Sans", "sans-serif"],
            "font.size": 7.2,
            "axes.titlesize": 8.2,
            "axes.labelsize": 7.2,
            "xtick.labelsize": 6.4,
            "ytick.labelsize": 6.4,
            "legend.fontsize": 6.4,
            "axes.linewidth": 0.75,
            "xtick.major.width": 0.6,
            "ytick.major.width": 0.6,
            "xtick.major.size": 2.5,
            "ytick.major.size": 2.5,
            "lines.linewidth": 1.25,
            "lines.markersize": 3.8,
            "svg.fonttype": "none",
            "pdf.fonttype": 42,
            "pdf.use14corefonts": False,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "legend.frameon": False,
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


def resolve_existing(canonical: Path, legacy: Path, warnings: list[dict[str, Any]], label: str) -> Path:
    if canonical.exists():
        return canonical
    if legacy.exists():
        warnings.append(
            {
                "level": "warning",
                "source": label,
                "message": (
                    "Using legacy-named input directory as read-only evidence. "
                    "New outputs remain canonical stage_10 artifacts."
                ),
                "legacy_input": str(legacy),
                "canonical_expected": str(canonical),
            }
        )
        return legacy
    warnings.append({"level": "error", "source": label, "message": "missing input directory"})
    return canonical


def read_csv(path: Path, warnings: list[dict[str, Any]], label: str, required: bool = True) -> pd.DataFrame:
    if not path.exists():
        level = "error" if required else "warning"
        warnings.append({"level": level, "source": label, "message": f"missing CSV: {path}"})
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        warnings.append({"level": "warning", "source": label, "message": f"empty CSV: {path}"})
        return pd.DataFrame()
    except Exception as exc:  # pragma: no cover - defensive run logging
        warnings.append({"level": "error", "source": label, "message": f"failed to read CSV: {exc}", "path": str(path)})
        return pd.DataFrame()
    if df.empty:
        warnings.append({"level": "warning", "source": label, "message": f"CSV has zero rows: {path}"})
    return df


def coerce_numeric(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in out.columns:
        if col.endswith(("_km", "_fraction", "_count", "_valid", "_mean", "_bias")):
            out[col] = pd.to_numeric(out[col], errors="coerce")
        if col.startswith(("n_", "delta_", "nearest_", "within_", "mae", "rmse", "bias")):
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def save_source(df: pd.DataFrame, dirs: dict[str, Path], name: str) -> Path:
    path = dirs["source_data"] / f"{name}_source.csv"
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def save_figure(fig: plt.Figure, dirs: dict[str, Path], stem: str) -> dict[str, str]:
    base = dirs["figures"] / stem
    fig.savefig(base.with_suffix(".svg"), bbox_inches="tight")
    fig.savefig(base.with_suffix(".pdf"), bbox_inches="tight")
    fig.savefig(base.with_suffix(".png"), dpi=300, bbox_inches="tight")
    fig.savefig(base.with_suffix(".tiff"), dpi=600, bbox_inches="tight")
    plt.close(fig)
    return {
        "svg": str(base.with_suffix(".svg")),
        "pdf": str(base.with_suffix(".pdf")),
        "png": str(base.with_suffix(".png")),
        "tiff": str(base.with_suffix(".tiff")),
    }


def panel_label(ax: plt.Axes, label: str) -> None:
    ax.text(
        -0.08,
        1.05,
        label,
        transform=ax.transAxes,
        fontsize=8.5,
        fontweight="bold",
        va="top",
        ha="right",
    )


def annotate_bars(ax: plt.Axes, bars: Any, fmt: str = "{:.2f}", dy: float = 0.02) -> None:
    ymin, ymax = ax.get_ylim()
    span = ymax - ymin if ymax != ymin else 1.0
    for bar in bars:
        val = bar.get_height()
        if not np.isfinite(val):
            continue
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            val + span * dy,
            fmt.format(val),
            ha="center",
            va="bottom",
            fontsize=5.8,
            color=COLORS["black"],
            rotation=0,
        )


def safe_query(df: pd.DataFrame, expr: str) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    try:
        return df.query(expr).copy()
    except Exception:
        return pd.DataFrame(columns=df.columns)


def first_value(df: pd.DataFrame, column: str, default: float | str | None = None) -> Any:
    if df.empty or column not in df.columns:
        return default
    value = df[column].iloc[0]
    if pd.isna(value):
        return default
    return value


def format_int(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "NA"
    return f"{int(round(float(value))):,}"


def load_inputs(warnings: list[dict[str, Any]]) -> tuple[dict[str, pd.DataFrame], dict[str, Path]]:
    stage10p_root = resolve_existing(STAGE10P_ROOT_CANONICAL, STAGE10P_ROOT_LEGACY, warnings, "stage_10p_input")
    stage10p2_root = resolve_existing(STAGE10P2_ROOT_CANONICAL, STAGE10P2_ROOT_LEGACY, warnings, "stage_10p2_input")
    paths = {
        "stage09f_root": STAGE09F_ROOT,
        "stage10_root": STAGE10_ROOT,
        "stage10p_root": stage10p_root,
        "stage10p2_root": stage10p2_root,
    }
    tables = {
        "s09f_summary": read_csv(
            STAGE09F_ROOT
            / "source_data"
            / "stage_09f_spatial_story_maps_202403_figure1_representative_sample_summary.csv",
            warnings,
            "stage09f_representative_summary",
        ),
        "s10_domain": read_csv(
            STAGE10_ROOT / "02_fused_cth_metrics" / "stage_10_fused_cth_metrics_by_domain.csv",
            warnings,
            "stage10_domain_metrics",
        ),
        "s10_selected_source": read_csv(
            STAGE10_ROOT
            / "03_fused_cth_by_selected_source"
            / "stage_10_fused_cth_metrics_by_selected_source.csv",
            warnings,
            "stage10_selected_source_metrics",
        ),
        "s10_prefusion_source": read_csv(
            STAGE10_ROOT / "04_prefusion_source_cth" / "stage_10_prefusion_source_cth_metrics_by_source.csv",
            warnings,
            "stage10_prefusion_source_metrics",
        ),
        "s10_ab_sensitivity": read_csv(
            STAGE10_ROOT / "10_qc" / "stage_10_qc_a_band_vs_b_band_sensitivity.csv",
            warnings,
            "stage10_ab_sensitivity",
        ),
        "s10_regret": read_csv(
            STAGE10_ROOT / "10_qc" / "stage_10_qc_selection_regret_pixel_weighted.csv",
            warnings,
            "stage10_selection_regret",
        ),
        "s10_clean_core": read_csv(
            STAGE10_ROOT / "10_qc" / "stage_10_qc_clean_core_metrics.csv",
            warnings,
            "stage10_clean_core_metrics",
        ),
        "s10_semantic": read_csv(
            STAGE10_ROOT / "10_qc" / "stage_10_qc_semantic_variable_audit.csv",
            warnings,
            "stage10_semantic_audit",
        ),
        "s10p_files": read_csv(
            stage10p_root / "stage10p_composite_file_inventory.csv",
            warnings,
            "stage10p_file_inventory",
            required=stage10p_root == STAGE10P_ROOT_LEGACY,
        ),
        "s10p_candidates": read_csv(
            stage10p_root / "stage10p_cloud_property_variable_candidates.csv",
            warnings,
            "stage10p_cloud_property_candidates",
            required=stage10p_root == STAGE10P_ROOT_LEGACY,
        ),
        "s10p_variables": read_csv(
            stage10p_root / "stage10p_composite_variable_inventory.csv",
            warnings,
            "stage10p_variable_inventory",
            required=stage10p_root == STAGE10P_ROOT_LEGACY,
        ),
        "s10p_keywords": read_csv(
            stage10p_root / "stage10p_psf_keyword_search_results.csv",
            warnings,
            "stage10p_keyword_results",
            required=stage10p_root == STAGE10P_ROOT_LEGACY,
        ),
        "s10p2_cloud_delta": read_csv(
            stage10p2_root / "stage10p2_cloud_mask_delta_vs_nearest.csv",
            warnings,
            "stage10p2_cloud_mask_delta",
            required=stage10p2_root == STAGE10P2_ROOT_LEGACY,
        ),
        "s10p2_cth_delta": read_csv(
            stage10p2_root / "stage10p2_cth_delta_vs_nearest.csv",
            warnings,
            "stage10p2_cth_delta",
            required=stage10p2_root == STAGE10P2_ROOT_LEGACY,
        ),
        "s10p2_high_delta": read_csv(
            stage10p2_root / "stage10p2_fused_high_cloud_cth_delta.csv",
            warnings,
            "stage10p2_high_cloud_delta",
            required=stage10p2_root == STAGE10P2_ROOT_LEGACY,
        ),
        "s10p2_meteosat_delta": read_csv(
            stage10p2_root / "stage10p2_selected_meteosat_cth_delta.csv",
            warnings,
            "stage10p2_meteosat_delta",
            required=stage10p2_root == STAGE10P2_ROOT_LEGACY,
        ),
    }
    return {name: coerce_numeric(df) for name, df in tables.items()}, paths


def draw_flow_panel(ax: plt.Axes) -> None:
    ax.set_axis_off()
    boxes = [
        (0.03, 0.62, 0.20, 0.25, "Stage 09F\nspatial cloud-mask story", COLORS["blue_light"]),
        (0.29, 0.62, 0.20, 0.25, "Stage 10\nfused CTH validation", COLORS["blue"]),
        (0.55, 0.62, 0.18, 0.25, "Stage 10P\nComposite audit", COLORS["green"]),
        (0.78, 0.62, 0.19, 0.25, "Stage 10P2\napprox. FOV sensitivity", COLORS["teal"]),
    ]
    for x, y, w, h, text, color in boxes:
        rect = Rectangle((x, y), w, h, transform=ax.transAxes, facecolor=color, edgecolor="white", lw=1.0)
        ax.add_patch(rect)
        ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", color="white", fontsize=7.1)
    for x0, x1 in [(0.23, 0.29), (0.49, 0.55), (0.73, 0.78)]:
        ax.annotate(
            "",
            xy=(x1, 0.745),
            xytext=(x0, 0.745),
            xycoords=ax.transAxes,
            arrowprops={"arrowstyle": "->", "lw": 1.0, "color": COLORS["grey"]},
        )
    ax.text(
        0.03,
        0.23,
        "Claim: spatial cloud-mask mismatch motivated a height-product validation, then PSF/FOV diagnostics tested whether footprint representation could explain the residual CTH error.",
        transform=ax.transAxes,
        ha="left",
        va="top",
        fontsize=7.0,
        color=COLORS["black"],
        wrap=True,
    )


def make_fig01(tables: dict[str, pd.DataFrame], dirs: dict[str, Path]) -> dict[str, Any]:
    df = tables["s09f_summary"].copy()
    keep = [
        "sample_id",
        "short_reason",
        "agreement_policy_a",
        "boundary_fraction",
        "broken_cloud_fraction",
        "meteosat_selected_fraction",
        "valid_source_count_ge4_fraction",
    ]
    source = df[[c for c in keep if c in df.columns]].copy()
    source["figure_role"] = "stage09f_to_stage10_evidence_chain"
    source_path = save_source(source, dirs, "stage_10_group_meeting_fig01")

    plot_df = source.head(6).copy()
    labels = plot_df["sample_id"].astype(str).tolist()
    x = np.arange(len(plot_df))
    width = 0.18

    fig = plt.figure(figsize=(7.2, 4.4), constrained_layout=True)
    gs = fig.add_gridspec(2, 1, height_ratios=[1.0, 2.0])
    ax0 = fig.add_subplot(gs[0, 0])
    ax1 = fig.add_subplot(gs[1, 0])
    draw_flow_panel(ax0)
    panel_label(ax0, "a")

    metrics = [
        ("agreement_policy_a", "Agreement", COLORS["blue"]),
        ("boundary_fraction", "Boundary/broken", COLORS["red_light"]),
        ("meteosat_selected_fraction", "Meteosat selected", COLORS["red"]),
        ("valid_source_count_ge4_fraction", ">=4 valid sources", COLORS["green"]),
    ]
    for i, (col, label, color) in enumerate(metrics):
        vals = pd.to_numeric(plot_df[col], errors="coerce").to_numpy()
        ax1.bar(x + (i - 1.5) * width, vals, width=width, label=label, color=color, edgecolor="white", lw=0.4)
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels, rotation=35, ha="right")
    ax1.set_ylim(0, 1.02)
    ax1.set_ylabel("Fraction")
    ax1.set_title("Representative March 2024 samples used to motivate height validation")
    ax1.axhline(0.5, color=COLORS["grey"], lw=0.7, ls="--", alpha=0.5)
    ax1.legend(ncol=4, loc="upper center", bbox_to_anchor=(0.5, 1.18))
    panel_label(ax1, "b")

    paths = save_figure(fig, dirs, "stage_10_group_meeting_fig01_stage09f_to_stage10_evidence_chain")
    return {
        "figure_id": "fig01_stage09f_to_stage10_evidence_chain",
        "title": "Stage09F spatial evidence motivates Stage10 CTH validation",
        "source_csv": str(source_path),
        **paths,
    }


def make_fig02(tables: dict[str, pd.DataFrame], dirs: dict[str, Path]) -> dict[str, Any]:
    df = safe_query(tables["s10_domain"], "policy == @POLICY_A").copy()
    order = list(DOMAIN_LABELS)
    df["domain_order"] = df["domain"].map({d: i for i, d in enumerate(order)})
    df = df.sort_values("domain_order")
    source_path = save_source(df, dirs, "stage_10_group_meeting_fig02")

    x = np.arange(len(df))
    labels = [DOMAIN_LABELS.get(d, d) for d in df["domain"]]
    fig, axes = plt.subplots(2, 2, figsize=(7.2, 5.2), constrained_layout=True)
    ax_bias, ax_mae, ax_rmse, ax_w2 = axes.ravel()

    bars = ax_bias.bar(x, df["bias_km"], color=COLORS["blue"], edgecolor="white", lw=0.4)
    ax_bias.axhline(0, color=COLORS["grey"], lw=0.8)
    ax_bias.set_ylabel("Bias (km)")
    ax_bias.set_title("Mean signed offset")
    annotate_bars(ax_bias, bars, "{:.1f}", dy=0.01)

    bars = ax_mae.bar(x, df["mae_km"], color=COLORS["red"], edgecolor="white", lw=0.4)
    ax_mae.set_ylabel("MAE (km)")
    ax_mae.set_title("Absolute height error")
    annotate_bars(ax_mae, bars, "{:.1f}", dy=0.01)

    bars = ax_rmse.bar(x, df["rmse_km"], color=COLORS["purple"], edgecolor="white", lw=0.4)
    ax_rmse.set_ylabel("RMSE (km)")
    ax_rmse.set_title("Large-error sensitivity")
    annotate_bars(ax_rmse, bars, "{:.1f}", dy=0.01)

    bars = ax_w2.bar(x, df["within_2km_fraction"], color=COLORS["green"], edgecolor="white", lw=0.4)
    ax_w2.set_ylabel("Within 2 km fraction")
    ax_w2.set_ylim(0, max(0.65, float(df["within_2km_fraction"].max()) + 0.08))
    ax_w2.set_title("Tolerance-band agreement")
    annotate_bars(ax_w2, bars, "{:.2f}", dy=0.015)

    for label, ax in zip(["a", "b", "c", "d"], axes.ravel(), strict=False):
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=35, ha="right")
        panel_label(ax, label)

    d1 = df[df["domain"] == "D1_both_cloud"]
    if not d1.empty:
        text = (
            f"D1 both-cloud: n={format_int(first_value(d1, 'n_valid_cth'))}, "
            f"bias={first_value(d1, 'bias_km'):.3f} km, "
            f"MAE={first_value(d1, 'mae_km'):.3f} km, "
            f"RMSE={first_value(d1, 'rmse_km'):.3f} km"
        )
        fig.suptitle(text, y=1.02, fontsize=8.0)

    paths = save_figure(fig, dirs, "stage_10_group_meeting_fig02_fused_cth_main_metrics")
    return {
        "figure_id": "fig02_fused_cth_main_metrics",
        "title": "Stage10 fused CTH main metrics by diagnostic domain",
        "source_csv": str(source_path),
        **paths,
    }


def make_fig03(tables: dict[str, pd.DataFrame], dirs: dict[str, Path]) -> dict[str, Any]:
    selected = safe_query(tables["s10_selected_source"], "policy == @POLICY_A and domain == 'D1_both_cloud'").copy()
    selected["panel"] = "selected_source"
    selected["source"] = selected["selected_source"]
    prefusion = safe_query(tables["s10_prefusion_source"], "policy == @POLICY_A and domain == 'D1_both_cloud'").copy()
    prefusion["panel"] = "prefusion_source"
    prefusion["source"] = prefusion["source_name"]
    source = pd.concat([selected, prefusion], ignore_index=True, sort=False)
    source_path = save_source(source, dirs, "stage_10_group_meeting_fig03")

    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.6), sharey=True, constrained_layout=True)
    for ax, subset, title, letter in [
        (axes[0], selected, "Fused pixels grouped by selected source", "a"),
        (axes[1], prefusion, "Pre-fusion source CTH versus EPIC reference", "b"),
    ]:
        subset = subset.sort_values("mae_km")
        y = np.arange(len(subset))
        colors = [SOURCE_COLORS.get(s, COLORS["grey"]) for s in subset["source"]]
        ax.barh(y, subset["mae_km"], color=colors, edgecolor="white", lw=0.4)
        ax.scatter(subset["bias_km"], y, marker="D", s=18, color=COLORS["black"], label="bias")
        ax.set_yticks(y)
        ax.set_yticklabels(subset["source"])
        ax.set_xlabel("MAE (bar) and bias (diamond), km")
        ax.set_title(title)
        ax.axvline(0, color=COLORS["grey"], lw=0.7)
        ax.invert_yaxis()
        panel_label(ax, letter)
    axes[1].legend(loc="lower right")

    paths = save_figure(fig, dirs, "stage_10_group_meeting_fig03_source_error_decomposition")
    return {
        "figure_id": "fig03_source_error_decomposition",
        "title": "Selected-source and pre-fusion source contributions to CTH error",
        "source_csv": str(source_path),
        **paths,
    }


def make_fig04(tables: dict[str, pd.DataFrame], dirs: dict[str, Path]) -> dict[str, Any]:
    semantic = tables["s10_semantic"].copy()
    sensitivity = tables["s10_ab_sensitivity"].copy()
    sensitivity = safe_query(sensitivity, "policy == @POLICY_A")
    source = pd.concat(
        [
            semantic.assign(panel="semantic_audit"),
            sensitivity.assign(panel="a_vs_b_sensitivity"),
        ],
        ignore_index=True,
        sort=False,
    )
    source_path = save_source(source, dirs, "stage_10_group_meeting_fig04")

    fig = plt.figure(figsize=(7.2, 4.4), constrained_layout=True)
    gs = fig.add_gridspec(1, 2, width_ratios=[1.35, 1.0])
    ax0 = fig.add_subplot(gs[0, 0])
    ax1 = fig.add_subplot(gs[0, 1])
    ax0.set_axis_off()
    panel_label(ax0, "a")
    ax0.set_title("Semantic audit: reference and test quantities", loc="left")
    rows = [
        ("EPIC A-band", "Effective Cloud Height", "reference", "not strict CTH"),
        ("EPIC B-band", "Effective Cloud Height", "sensitivity", "different O2 response"),
        ("GEO fused", "fused_cloud_top_height_km", "test product", "strict CTH-like"),
        ("GOES HT", "cloud top height", "prefusion source", "strict CTH"),
        ("FY4B/Himawari/Meteosat", "CTH / CldTopHght / ctoph", "prefusion source", "strict CTH"),
    ]
    col_x = [0.02, 0.29, 0.61, 0.82]
    headers = ["Product", "Variable", "Stage10 role", "Caution"]
    for x, header in zip(col_x, headers, strict=False):
        ax0.text(x, 0.92, header, transform=ax0.transAxes, fontweight="bold", fontsize=6.6)
    for i, row in enumerate(rows):
        y = 0.82 - i * 0.145
        shade = "#F4F4F4" if i % 2 == 0 else "white"
        ax0.add_patch(Rectangle((0.0, y - 0.035), 0.98, 0.105, transform=ax0.transAxes, color=shade, ec="none"))
        for x, text in zip(col_x, row, strict=False):
            ax0.text(x, y, text, transform=ax0.transAxes, fontsize=6.0, va="center", wrap=True)
    ax0.text(
        0.02,
        0.05,
        "Interpretation: Stage10 is an EPIC-relative diagnostic; it is not a geometric truth validation.",
        transform=ax0.transAxes,
        fontsize=6.8,
        color=COLORS["red"],
    )

    sens = sensitivity[
        sensitivity["group"].isin(
            [
                "ALL",
                "selected_source=FY4B",
                "selected_source=GOES-16",
                "selected_source=GOES-18",
                "selected_source=Himawari-9",
                "selected_source=Meteosat-0deg",
                "selected_source=Meteosat-IODC",
            ]
        )
        & (sensitivity["domain"] == "D1_both_cloud")
    ].copy()
    sens["plot_group"] = sens["group"].str.replace("selected_source=", "", regex=False)
    sens = sens.sort_values("b_minus_a_mae_km")
    y = np.arange(len(sens))
    colors = [SOURCE_COLORS.get(g, COLORS["blue"]) for g in sens["plot_group"]]
    ax1.barh(y, sens["b_minus_a_mae_km"], color=colors, edgecolor="white", lw=0.4)
    ax1.axvline(0, color=COLORS["grey"], lw=0.8)
    ax1.set_yticks(y)
    ax1.set_yticklabels(sens["plot_group"])
    ax1.set_xlabel("B-band minus A-band MAE (km)")
    ax1.set_title("Reference-band sensitivity")
    panel_label(ax1, "b")

    paths = save_figure(fig, dirs, "stage_10_group_meeting_fig04_semantic_audit_ab_sensitivity")
    return {
        "figure_id": "fig04_semantic_audit_ab_sensitivity",
        "title": "Effective-height semantics and A/B-band reference sensitivity",
        "source_csv": str(source_path),
        **paths,
    }


def make_fig05(tables: dict[str, pd.DataFrame], dirs: dict[str, Path]) -> dict[str, Any]:
    regret = safe_query(tables["s10_regret"], "policy == @POLICY_A").copy()
    clean = safe_query(tables["s10_clean_core"], "policy == @POLICY_A").copy()
    source = pd.concat(
        [regret.assign(panel="selection_regret"), clean.assign(panel="clean_boundary_metrics")],
        ignore_index=True,
        sort=False,
    )
    source_path = save_source(source, dirs, "stage_10_group_meeting_fig05")

    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.8), constrained_layout=True)
    key_groups = [
        "ALL_VALID_CTH",
        "clean_core",
        "boundary_or_broken_cloud",
        "high_cloud",
        "selected_Meteosat0deg",
        "selected_MeteosatIODC",
    ]
    reg = regret[regret["pixel_group"].isin(key_groups)].copy()
    reg["order"] = reg["pixel_group"].map({g: i for i, g in enumerate(key_groups)})
    reg = reg.sort_values("order")
    x = np.arange(len(reg))
    axes[0].bar(
        x - 0.18,
        reg["current_selected_mae_km"],
        width=0.36,
        color=COLORS["red"],
        label="current selected",
        edgecolor="white",
        lw=0.4,
    )
    axes[0].bar(
        x + 0.18,
        reg["best_available_mae_km"],
        width=0.36,
        color=COLORS["green"],
        label="best available oracle",
        edgecolor="white",
        lw=0.4,
    )
    axes[0].set_xticks(x)
    axes[0].set_xticklabels(
        ["ALL", "clean\ncore", "boundary/\nbroken", "high\ncloud", "Met-0deg", "Met-IODC"],
        rotation=0,
    )
    axes[0].set_ylabel("MAE (km)")
    axes[0].set_title("Selection regret is pixel-group dependent")
    axes[0].legend(loc="upper left")
    panel_label(axes[0], "a")

    clean_order = ["boundary_or_broken_cloud", "clean_core", "non_boundary"]
    cl = clean[clean["clean_group"].isin(clean_order)].copy()
    cl["order"] = cl["clean_group"].map({g: i for i, g in enumerate(clean_order)})
    cl = cl.sort_values("order")
    x2 = np.arange(len(cl))
    bars = axes[1].bar(
        x2,
        cl["mae_km"],
        color=[COLORS["red_light"], COLORS["blue"], COLORS["blue_light"]],
        edgecolor="white",
        lw=0.4,
    )
    axes[1].set_xticks(x2)
    axes[1].set_xticklabels(["boundary/\nbroken", "clean\ncore", "non-\nboundary"])
    axes[1].set_ylabel("MAE (km)")
    axes[1].set_title("Clean-core is not automatically easier")
    annotate_bars(axes[1], bars, "{:.2f}", dy=0.012)
    panel_label(axes[1], "b")

    paths = save_figure(fig, dirs, "stage_10_group_meeting_fig05_regret_high_cloud_boundary")
    return {
        "figure_id": "fig05_regret_high_cloud_boundary",
        "title": "Selection regret and high-cloud/boundary mechanisms",
        "source_csv": str(source_path),
        **paths,
    }


def stage10p_summary(tables: dict[str, pd.DataFrame]) -> dict[str, Any]:
    files = tables["s10p_files"]
    candidates = tables["s10p_candidates"]
    variables = tables["s10p_variables"]
    keywords = tables["s10p_keywords"]
    open_ok = int((files["open_status"] == "ok").sum()) if "open_status" in files else 0
    total_files = int(len(files))
    explicit_kernel = 0
    explicit_weight = 0
    if not variables.empty:
        if "is_explicit_psf_kernel_candidate" in variables:
            explicit_kernel = int(variables["is_explicit_psf_kernel_candidate"].astype(bool).sum())
        if "is_explicit_weight_candidate" in variables:
            explicit_weight = int(variables["is_explicit_weight_candidate"].astype(bool).sum())
    role_counts = {}
    if "candidate_role" in candidates:
        role_counts = candidates.groupby("candidate_role").size().to_dict()
    keyword_counts = {}
    if "keyword" in keywords:
        keyword_counts = keywords.groupby("keyword").size().to_dict()
    return {
        "total_files": total_files,
        "open_ok_files": open_ok,
        "explicit_psf_kernel_candidates": explicit_kernel,
        "explicit_weight_candidates": explicit_weight,
        "candidate_role_counts": role_counts,
        "keyword_counts": keyword_counts,
        "psf_hits": int(keyword_counts.get("psf", 0)),
        "fov_hits": int(keyword_counts.get("fov", 0)),
        "weight_hits": int(keyword_counts.get("weight", 0)),
    }


def find_stage10p2_deltas(tables: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    cloud = tables["s10p2_cloud_delta"].copy()
    cth = tables["s10p2_cth_delta"].copy()
    high = tables["s10p2_high_delta"].copy()
    cloud_focus = safe_query(
        cloud,
        "policy == @POLICY_A and group_dimension == 'domain' and group_value == 'ALL_VALID'",
    )
    if cloud_focus.empty:
        cloud_focus = safe_query(
            cloud,
            "policy == @POLICY_A and group_dimension == 'selected_source_focus' and group_value == 'ALL_VALID'",
        )
    cth_focus = safe_query(
        cth,
        "policy == @POLICY_A and group_dimension == 'domain' and group_value == 'D1_both_cloud_Policy_A'",
    )
    if cth_focus.empty:
        cth_focus = safe_query(cth, "policy == @POLICY_A and domain == 'D1_both_cloud_Policy_A'")
    high_focus = safe_query(high, "policy == @POLICY_A and group_value == 'fused_high_cloud'")
    for df in [cloud_focus, cth_focus, high_focus]:
        if "method_name" in df:
            df["method_order"] = df["method_name"].map({m: i for i, m in enumerate(METHOD_ORDER)})
            df.sort_values("method_order", inplace=True)
    return cloud_focus, cth_focus, high_focus


def make_fig06(tables: dict[str, pd.DataFrame], dirs: dict[str, Path]) -> dict[str, Any]:
    comp = stage10p_summary(tables)
    cloud_focus, cth_focus, high_focus = find_stage10p2_deltas(tables)
    comp_rows = [
        {"panel": "composite_inventory", "metric": "open_ok_files", "value": comp["open_ok_files"]},
        {"panel": "composite_inventory", "metric": "total_files", "value": comp["total_files"]},
        {"panel": "composite_inventory", "metric": "explicit_psf_kernel_candidates", "value": comp["explicit_psf_kernel_candidates"]},
        {"panel": "composite_inventory", "metric": "explicit_weight_candidates", "value": comp["explicit_weight_candidates"]},
        {"panel": "composite_inventory", "metric": "psf_keyword_hits", "value": comp["psf_hits"]},
        {"panel": "composite_inventory", "metric": "fov_keyword_hits", "value": comp["fov_hits"]},
        {"panel": "composite_inventory", "metric": "weight_keyword_hits", "value": comp["weight_hits"]},
    ]
    source = pd.concat(
        [
            pd.DataFrame(comp_rows),
            cloud_focus.assign(panel="cloud_mask_delta"),
            cth_focus.assign(panel="cth_delta_d1"),
            high_focus.assign(panel="high_cloud_delta"),
        ],
        ignore_index=True,
        sort=False,
    )
    source_path = save_source(source, dirs, "stage_10_group_meeting_fig06")

    fig = plt.figure(figsize=(7.2, 4.2), constrained_layout=True)
    gs = fig.add_gridspec(1, 2, width_ratios=[0.9, 1.35])
    ax0 = fig.add_subplot(gs[0, 0])
    ax1 = fig.add_subplot(gs[0, 1])
    panel_label(ax0, "a")
    panel_label(ax1, "b")

    ax0.set_title("EPIC Composite inventory")
    metrics = [
        ("Files OK", comp["open_ok_files"], COLORS["green"]),
        ("PSF kernel\nvariables", comp["explicit_psf_kernel_candidates"], COLORS["red"]),
        ("Weight\nvariables", comp["explicit_weight_candidates"], COLORS["red_light"]),
        ("PSF/FOV\nkeyword hits", comp["psf_hits"] + comp["fov_hits"], COLORS["blue"]),
    ]
    x = np.arange(len(metrics))
    vals = [m[1] for m in metrics]
    bars = ax0.bar(x, vals, color=[m[2] for m in metrics], edgecolor="white", lw=0.4)
    ax0.set_xticks(x)
    ax0.set_xticklabels([m[0] for m in metrics], rotation=0)
    ax0.set_ylabel("Count")
    annotate_bars(ax0, bars, "{:.0f}", dy=0.015)
    ax0.text(
        0.02,
        0.88,
        "Conclusion:\nofficial PSF-aware\nbenchmark evidence,\nnot an official\nPSF kernel.",
        transform=ax0.transAxes,
        ha="left",
        va="top",
        fontsize=6.4,
        color=COLORS["black"],
        bbox={"facecolor": "white", "edgecolor": COLORS["grey_light"], "pad": 3},
    )

    methods = cth_focus["method_name"].tolist()
    xpos = np.arange(len(methods))
    width = 0.26
    if not cloud_focus.empty:
        cloud_map = cloud_focus.set_index("method_name")["delta_agreement_vs_nearest"]
        ax1.bar(
            xpos - width,
            [cloud_map.get(m, np.nan) for m in methods],
            width=width,
            color=COLORS["green"],
            label="cloud agreement delta",
            edgecolor="white",
            lw=0.4,
        )
    ax1.bar(
        xpos,
        cth_focus["delta_mae_km_vs_nearest"],
        width=width,
        color=COLORS["blue"],
        label="D1 CTH MAE delta",
        edgecolor="white",
        lw=0.4,
    )
    if not high_focus.empty:
        high_map = high_focus.set_index("method_name")["delta_mae_km_vs_nearest"]
        ax1.bar(
            xpos + width,
            [high_map.get(m, np.nan) for m in methods],
            width=width,
            color=COLORS["red"],
            label="high-cloud MAE delta",
            edgecolor="white",
            lw=0.4,
        )
    ax1.axhline(0, color=COLORS["grey"], lw=0.8)
    ax1.set_xticks(xpos)
    ax1.set_xticklabels([METHOD_LABELS.get(m, m) for m in methods], rotation=35, ha="right")
    ax1.set_ylabel("Delta vs nearest (fraction or km)")
    ax1.set_title("Approximate EPIC-FOV aggregation sensitivity")
    ax1.legend(loc="lower left")

    paths = save_figure(fig, dirs, "stage_10_group_meeting_fig06_composite_fov_mechanism_closure")
    return {
        "figure_id": "fig06_composite_fov_mechanism_closure",
        "title": "Composite audit and approximate FOV sensitivity close the mechanism loop",
        "source_csv": str(source_path),
        **paths,
    }


def write_plot_index(rows: list[dict[str, Any]], dirs: dict[str, Path]) -> Path:
    df = pd.DataFrame(rows)
    path = dirs["logs"] / "stage_10_group_meeting_plot_index.csv"
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def write_figure_guide(rows: list[dict[str, Any]], tables: dict[str, pd.DataFrame], dirs: dict[str, Path]) -> Path:
    d1 = safe_query(tables["s10_domain"], "policy == @POLICY_A and domain == 'D1_both_cloud'")
    regret = safe_query(tables["s10_regret"], "policy == @POLICY_A and pixel_group == 'ALL_VALID_CTH'")
    high = safe_query(tables["s10_regret"], "policy == @POLICY_A and pixel_group == 'high_cloud'")
    stage10p = stage10p_summary(tables)
    cloud_focus, cth_focus, high_focus = find_stage10p2_deltas(tables)
    box7_cth = cth_focus[cth_focus["method_name"] == "box_7x7"]
    box7_high = high_focus[high_focus["method_name"] == "box_7x7"]
    box7_cloud = cloud_focus[cloud_focus["method_name"] == "box_7x7"]

    text = f"""# Stage 10 组会图版逐图讲解

Run ID: `{RUN_ID}`

## 总体结论

这套图的主线是：Stage09F 的空间云掩膜诊断说明差异不是随机噪声，Stage10 进一步把问题推进到 fused CTH 产品验证；随后 Stage10P 和 Stage10P2 用 EPIC Composite 审计和近似 EPIC-FOV 聚合敏感性检查 footprint/PSF 机制。所有 CTH 数值都应读作相对于 `EPIC A-band Effective Cloud Height` 的诊断结果，不应说成相对于绝对真值。

## Fig. 1 | 从 Stage09F 空间诊断到 Stage10 CTH 验证

- 这张图回答：为什么要从 cloud mask 进入 CTH 产品验证。
- Panel a 是证据链流程图。`Stage 09F` 表示已有空间故事图；`Stage 10` 表示 fused CTH 对 EPIC effective height 的验证；`Stage 10P` 和 `Stage 10P2` 分别表示 Composite 审计和近似 FOV 敏感性。
- Panel b 的横轴是代表性 EPIC 时次，纵轴是比例。`Agreement` 是 Policy A 下 GEO-ring 与 EPIC 云/晴一致比例；`Boundary/broken` 是局地云边界或碎云场景比例；`Meteosat selected` 是当前 fused 选源落在 Meteosat 家族的比例；`>=4 valid sources` 是多源候选重叠比例。
- 汇报时可以说：Stage09F 不是只挑失败案例，而是同时包含低一致性、边界高、多源冲突和高一致性对照样本。

## Fig. 2 | Stage10 fused CTH 主结果

- 这张图回答：fused CTH 与 EPIC A-band effective height 的整体偏差有多大。
- D1 both-cloud 是最核心口径，因为 GEO 和 EPIC 都判为 cloud。D1 像元数为 `{format_int(first_value(d1, 'n_valid_cth'))}`，bias 为 `{first_value(d1, 'bias_km'):.3f} km`，MAE 为 `{first_value(d1, 'mae_km'):.3f} km`，RMSE 为 `{first_value(d1, 'rmse_km'):.3f} km`，within-2km fraction 为 `{first_value(d1, 'within_2km_fraction'):.3f}`。
- `bias_km` 是 GEO fused CTH 减 EPIC A-band effective height 的平均有符号差；正值表示 fused height 更高。
- `mae_km` 是平均绝对差，是本图最稳健的误差读数；`rmse_km` 对大误差更敏感；`within_2km_fraction` 是绝对差不超过 2 km 的像元比例。
- D3/D4 是云掩膜不一致域，不能和 D1 做同等物理解释；它们主要用于说明 cloud-mask mismatch 对高度统计口径的影响。

## Fig. 3 | 误差来源分解：selected source 与 prefusion source

- 这张图回答：误差是否集中在特定源或选源机制。
- 左图按当前 fused selected source 分组，表示“最后被融合产品采用的源”对应的 EPIC-relative error。
- 右图按 prefusion source 分组，表示各 GEO 源自身 CTH 与 EPIC reference 的差异。
- 条形是 MAE，黑色菱形是 bias。MAE 看误差大小，bias 看系统性偏高或偏低。
- 如果导师问是否可以直接说某颗卫星错：不能。这里的参考是 EPIC effective height，不是几何真值；但可以说 Meteosat-selected 区域在 Stage10 诊断中表现出较高 EPIC-relative error，是后续机制检查重点。

## Fig. 4 | 语义审计与 A/B-band 敏感性

- 这张图回答：EPIC 和 GEO 的 height 变量到底是不是同一种 CTH。
- EPIC `A-band_Effective_Cloud_Height` 和 `B-band_Effective_Cloud_Height` 是 oxygen absorption retrieval 的 effective height，不是严格几何 cloud top height。
- GOES-16/18 的 `HT`、FY4B 的 `CTH`、Himawari 的 `CldTopHght`、Meteosat 的 `ctoph` 在 Stage10 中按 cloud top height 类变量使用。
- 右图的 `B-band minus A-band MAE` 表示如果把 EPIC reference 从 A-band 换成 B-band，MAE 增加多少。D1 ALL 的增量约为 `+0.560 km`，说明 reference band 语义本身会影响结论幅度。

## Fig. 5 | Selection regret 与高云/边界机制

- 这张图回答：Stage10 的误差是否主要来自选源 regret、高云，或边界碎云。
- Panel a 的红条是当前 selected source 的 MAE，绿条是同一像元内 best available oracle source 的 MAE。二者差值就是 `selection_regret_mae_km`。
- ALL_VALID_CTH 的 current MAE 为 `{first_value(regret, 'current_selected_mae_km'):.3f} km`，best available oracle 为 `{first_value(regret, 'best_available_mae_km'):.3f} km`，selection regret 为 `{first_value(regret, 'selection_regret_mae_km'):.3f} km`。
- high-cloud 的 regret 为 `{first_value(high, 'selection_regret_mae_km'):.3f} km`，说明高云是比普通云更敏感的机制域。
- Panel b 显示 clean-core MAE 高于 boundary/broken，并不支持“只要避开边界就自然变好”的简单解释。

## Fig. 6 | Composite 审计与近似 FOV 灵敏度闭环

- 这张图回答：没有官方 PSF kernel 时，Stage10P/10P2 能支持什么机制判断。
- 左图显示 2024-01 Composite 文件成功打开 `{stage10p['open_ok_files']}` 个；没有找到显式 PSF kernel 变量，也没有找到显式 weight 数值变量。因此只能写作 official PSF-aware benchmark evidence，不能声称使用 official PSF kernel。
- 右图展示近似 FOV 聚合相对 nearest 的 delta。`cloud agreement delta` 是云掩膜一致率相对 nearest 的变化；`D1 CTH MAE delta` 和 `high-cloud MAE delta` 是 MAE 相对 nearest 的变化，负值表示 MAE 下降。
- box 7x7 对 D1 CTH MAE 的改善约为 `{first_value(box7_cth, 'delta_mae_km_vs_nearest'):.3f} km`；对 high-cloud MAE 的改善约为 `{first_value(box7_high, 'delta_mae_km_vs_nearest'):.3f} km`；cloud-mask agreement 改善约为 `{first_value(box7_cloud, 'delta_agreement_vs_nearest'):.4f}`。
- 汇报口径：近似 FOV 会带来可见改善，尤其高云更明显，但改善幅度不足以推翻 Stage10 的主结论。

## 输出索引

"""
    for row in rows:
        text += (
            f"- `{row['figure_id']}`: {row['title']}；source data: `{Path(row['source_csv']).name}`；"
            f"exports: SVG/PDF/PNG/TIFF。\n"
        )
    path = dirs["reports"] / "stage_10_group_meeting_figure_guide_cn.md"
    path.write_text(text, encoding="utf-8-sig")
    return path


def write_interrogation_guide(tables: dict[str, pd.DataFrame], dirs: dict[str, Path]) -> Path:
    text = """# Stage 10 组会导师追问口径

## 1. EPIC Effective Cloud Height 和 GEO CTH 有什么区别？

EPIC L2 Cloud 的 `A-band_Effective_Cloud_Height` / `B-band_Effective_Cloud_Height` 是基于氧气吸收带反演的 effective height。它更接近光子路径或辐射敏感层高度，不等同于严格几何 cloud top height。GEO 侧 GOES `HT`、FY4B `CTH`、Himawari `CldTopHght`、Meteosat `ctoph` 在本阶段按 CTH 类变量使用。因此 Stage10 是 EPIC-relative diagnostic validation，不是绝对真值验证。

答辩句式：我不会把 EPIC 叫作 truth。我的表述是“relative to EPIC A-band Effective Cloud Height reference”。这个 reference 对云高语义很有价值，但它本身和几何 CTH 有物理语义差异。

## 2. GOES-16/18 的 `HT` 为什么可以按 CTH 处理？

历史代码和语义审计把 GOES ABI L2 ACHAF 的 `HT` 标准化为 `cloud_top_height`，单位进入 km 后参与 prefusion source CTH 诊断。它和 EPIC A/B effective height 的区别在于：GOES `HT` 是 GEO 源产品提供的 cloud top height 类变量；EPIC A/B 是 effective cloud height reference。二者可以比较，但比较结果必须解释为 retrieval/product semantic difference plus source/fusion error，而不是单纯几何误差。

## 3. 为什么不能直接把 2024-01 Composite 和 2024-03 Stage10 数值比较？

Stage10P 审计的是本地 2024-01 DSCOVR_EPIC_L2_COMPOSITE_02 文件，而 Stage10/10P2 的样本是 2024-03。月份不同，采样时次、云场、太阳几何、观测几何和下载产品集合都不同。因此 2024-01 Composite 可以支持官方 Composite/PSF-aware 信息审计，但不能直接支持 2024-03 数值 benchmark 结论。

## 4. 为什么不能说用了官方 PSF kernel？

Stage10P 没有在文件中找到显式 PSF kernel 数值变量，也没有找到显式 weight 数值变量；只找到了 Composite/FOV/PSF integration 相关证据。这说明官方 Composite 产物可以被称为 official PSF-aware benchmark evidence，但不能说我们获得并使用了 official PSF kernel。

## 5. Stage10P2 的近似 FOV 结果说明什么？

近似 FOV 聚合相对 nearest 改善了部分指标，尤其 high-cloud CTH MAE 改善更明显。但 cloud-mask agreement 的改善较小，CTH MAE 的改善也不足以完全解释 Stage10 中 3 km 量级的 MAE。因此 footprint/representativeness 是可见因素，不是唯一或决定性因素。后续应做 2024-01 小样本 GEO-ring 与 official Composite benchmark pilot。

## 6. 如果导师问“你的产品到底好不好”怎么答？

目前结论不是简单好/坏，而是机制化定位：在 D1 both-cloud 域，fused CTH 相对 EPIC A-band reference 的 MAE 约 3.5 km，存在正 bias；source selection regret 和 high-cloud 区域贡献显著；reference-band 语义也会带来约 0.56 km 的 MAE 差异。因此下一步不是盲目调参，而是分 source、分 height regime、分 FOV/PSF 机制做 targeted correction。

## 7. 变量速查

- `bias_km`: GEO fused 或 source CTH 减 EPIC effective height 的平均有符号差。
- `mae_km`: 平均绝对差，是主要误差读数。
- `rmse_km`: 均方根误差，对大误差更敏感。
- `within_2km_fraction`: 绝对差不超过 2 km 的像元比例。
- `n_valid_cth`: 当前统计口径下参与 CTH 比较的有效像元数。
- `selected_source`: 当前 fused 产品对像元采用的 GEO 源。
- `current_selected_mae_km`: 当前选源机制下的 MAE。
- `best_available_mae_km`: 同一像元内可用候选源的 oracle 最低 MAE。
- `selection_regret_mae_km`: 当前选源 MAE 减 oracle MAE。
- `delta_mae_km_vs_nearest`: 近似 FOV 聚合 MAE 减 nearest MAE；负值表示改善。
- `delta_agreement_vs_nearest`: 近似 FOV 聚合云掩膜 agreement 减 nearest agreement；正值表示改善。
"""
    path = dirs["reports"] / "stage_10_group_meeting_interrogation_guide_cn.md"
    path.write_text(text, encoding="utf-8-sig")
    return path


def git_commit_hash() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path_config.PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
    except Exception:
        return None
    return result.stdout.strip()


def write_manifest(
    rows: list[dict[str, Any]],
    dirs: dict[str, Path],
    input_paths: dict[str, Path],
    warnings: list[dict[str, Any]],
    guide_path: Path,
    interrogation_path: Path,
    plot_index_path: Path,
) -> Path:
    outputs = {
        "figures": rows,
        "figure_guide": str(guide_path),
        "interrogation_guide": str(interrogation_path),
        "plot_index": str(plot_index_path),
    }
    manifest = {
        "project_id": PROJECT_ID,
        "canonical_stage_id": STAGE_ID,
        "run_id": RUN_ID,
        "generated_utc": utc_now(),
        "script_path": str(Path(__file__).resolve()),
        "code_commit": git_commit_hash(),
        "backend": "python/matplotlib",
        "input_paths": {k: str(v) for k, v in input_paths.items()},
        "output_paths": outputs,
        "parameter_summary": {
            "policy_for_main_figures": POLICY_A,
            "reference": "EPIC A-band Effective Cloud Height diagnostic reference",
            "stage10p_inputs": "read-only; canonical path preferred, legacy stage10p path accepted as existing input",
            "stage10p2_inputs": "read-only; canonical path preferred, legacy stage10p2 path accepted as existing input",
            "exports": ["svg", "pdf", "png", "tiff"],
        },
        "warnings": warnings,
    }
    path = dirs["logs"] / "stage_10_group_meeting_manifest.json"
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8-sig")
    return path


def find_mojibake(path: Path) -> list[str]:
    if path.suffix.lower() not in {".py", ".md", ".csv", ".json"}:
        return []
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception as exc:
        return [f"failed_to_read:{exc}"]
    markers = ["锛", "鐨", "璇", "鍥", "鈥", "�"]
    return [marker for marker in markers if marker in text]


def write_qa_report(rows: list[dict[str, Any]], dirs: dict[str, Path], warnings: list[dict[str, Any]]) -> Path:
    checks: list[dict[str, Any]] = []
    expected_count = 6
    checks.append({"check": "figure_count", "status": "pass" if len(rows) == expected_count else "fail", "value": len(rows)})
    for row in rows:
        for ext in ["svg", "pdf", "png", "tiff"]:
            p = Path(row[ext])
            checks.append({"check": f"{row['figure_id']}_{ext}_exists", "status": "pass" if p.exists() else "fail", "path": str(p)})
        source = Path(row["source_csv"])
        source_ok = source.exists() and source.stat().st_size > 0
        checks.append({"check": f"{row['figure_id']}_source_exists", "status": "pass" if source_ok else "fail", "path": str(source)})
        if source_ok:
            try:
                n_rows = len(pd.read_csv(source))
            except Exception:
                n_rows = -1
            checks.append({"check": f"{row['figure_id']}_source_row_count", "status": "pass" if n_rows > 0 else "fail", "rows": n_rows})
    text_files = list(dirs["reports"].glob("*.md")) + list(dirs["logs"].glob("*.json")) + list(dirs["logs"].glob("*.csv")) + list(dirs["source_data"].glob("*.csv"))
    suspect = []
    for path in text_files:
        hits = find_mojibake(path)
        if hits:
            suspect.append({"path": str(path), "markers": hits})
    checks.append({"check": "mojibake_scan", "status": "pass" if not suspect else "fail", "suspect_files": suspect})
    checks.append({"check": "runtime_warnings", "status": "pass" if not any(w["level"] == "error" for w in warnings) else "fail", "warnings": warnings})
    report = {
        "project_id": PROJECT_ID,
        "canonical_stage_id": STAGE_ID,
        "run_id": RUN_ID,
        "generated_utc": utc_now(),
        "checks": checks,
    }
    path = dirs["logs"] / "stage_10_group_meeting_qa_report.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8-sig")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    apply_publication_style()
    dirs = ensure_dirs()
    warnings: list[dict[str, Any]] = []
    tables, input_paths = load_inputs(warnings)

    rows = [
        make_fig01(tables, dirs),
        make_fig02(tables, dirs),
        make_fig03(tables, dirs),
        make_fig04(tables, dirs),
        make_fig05(tables, dirs),
        make_fig06(tables, dirs),
    ]
    plot_index_path = write_plot_index(rows, dirs)
    guide_path = write_figure_guide(rows, tables, dirs)
    interrogation_path = write_interrogation_guide(tables, dirs)
    manifest_path = write_manifest(rows, dirs, input_paths, warnings, guide_path, interrogation_path, plot_index_path)
    qa_path = write_qa_report(rows, dirs, warnings)
    print(json.dumps({"run_id": RUN_ID, "figures": len(rows), "manifest": str(manifest_path), "qa": str(qa_path)}, ensure_ascii=False))
    return 1 if any(w["level"] == "error" for w in warnings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
