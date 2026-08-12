"""Analyze the completed frozen 80-EPIC CLM/CTH experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from geo_ring_cloud.diagnostics.cth_validation import load_grid, load_npz_array, read_epic, sample_grid
from geo_ring_cloud.lineage import write_manifest
from geo_ring_cloud.paths import PROJECT_ROOT, RUNS_ROOT


COMPONENT_ROLE = "summary_helper"
RELATED_STAGE_IDS = ("stage_09c", "stage_10")
RUN_ID = "stage10_epic80_satpy_navigation_analysis_202403"
EXPECTED_COMPARISONS = 80
EXPECTED_GEO_SAMPLES = 79
DEFAULT_STAGE09C_ROOT = RUNS_ROOT / "stage_09c_epic_80_satpy_navigation_rerun_202403"
DEFAULT_STAGE10_ROOT = RUNS_ROOT / "stage_10_cth_epic_80_satpy_navigation_rerun_202403"
DEFAULT_OUTPUT_ROOT = RUNS_ROOT / "stage_10_epic_80_satpy_navigation_analysis_202403"
POLICY_A = "A_inclusive_binary"
POLICY_B = "B_high_confidence_only"
COLORS = {
    "blue": "#2070B4",
    "cyan": "#25A7A1",
    "orange": "#E28E2C",
    "red": "#C7473B",
    "gray": "#68737D",
    "light": "#E8EDF1",
    "dark": "#1F2933",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_output_dirs(root: Path) -> dict[str, Path]:
    dirs = {
        "control": root / "00_control",
        "tables": root / "01_tables",
        "source": root / "02_figure_source_data",
        "figures": root / "03_figures",
        "quicklooks": root / "04_quicklooks",
        "reports": root / "reports",
        "pptx": root / "pptx",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def validate_inputs(
    stage09c: Path,
    stage10: Path,
    *,
    expected_comparisons: int = EXPECTED_COMPARISONS,
    expected_geo_samples: int = EXPECTED_GEO_SAMPLES,
    allow_partial: bool = False,
) -> dict[str, Path]:
    paths = {
        "stage09c_manifest": stage09c / "manifest.json",
        "stage10_manifest": stage10 / "manifest.json",
        "targets": stage09c / "00_control" / "frozen_epic_80_target_manifest.csv",
        "stage10_samples": stage09c / "00_control" / "stage_10_epic_80_sample_manifest.csv",
        "navigation": stage09c / "00_control" / "navigation_schema_verification.csv",
        "status": stage09c / "00_control" / "epic_80_run_status.csv",
        "clm": stage09c / "08_clm_epic" / "epic_80_cloud_mask_sensitivity_metrics.csv",
        "cth_sample": stage10 / "02_fused_cth_metrics" / "stage_10_fused_cth_metrics_by_sample.csv",
        "cth_domain": stage10 / "02_fused_cth_metrics" / "stage_10_fused_cth_metrics_by_domain.csv",
        "cth_source": stage10 / "03_fused_cth_by_selected_source" / "stage_10_fused_cth_metrics_by_selected_source.csv",
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    require(not missing, f"required completed-run outputs are missing: {missing}")
    stage09c_status = read_json(paths["stage09c_manifest"]).get("final_status")
    accepted_stage09c = {"PASS", "PARTIAL_WITH_FAILURES"} if allow_partial else {"PASS"}
    require(stage09c_status in accepted_stage09c, f"Stage 09C final_status is not accepted: {stage09c_status}")
    stage10_manifest = read_json(paths["stage10_manifest"])
    require(stage10_manifest.get("final_status") == "PASS", "Stage 10 final_status is not PASS")
    stage10_inputs = [Path(item) for item in stage10_manifest.get("input_paths", [])]
    completed_manifests = [
        item
        for item in stage10_inputs
        if item.is_file() and "completed_sample_manifest" in item.name
    ]
    if completed_manifests:
        paths["stage10_samples"] = completed_manifests[0]

    targets = pd.read_csv(paths["targets"], dtype=str).fillna("")
    require(len(targets) == EXPECTED_COMPARISONS, f"expected 80 target rows, found {len(targets)}")
    require(targets["sample_id"].nunique() == EXPECTED_GEO_SAMPLES, "expected 79 unique GEO samples")
    require(targets["comparison_id"].nunique() == EXPECTED_COMPARISONS, "comparison_id coverage is incomplete")

    stage10_samples = pd.read_csv(paths["stage10_samples"], dtype=str).fillna("")
    require(len(stage10_samples) == expected_comparisons, f"expected {expected_comparisons} completed comparisons, found {len(stage10_samples)}")
    require(stage10_samples["geo_sample_id"].nunique() == expected_geo_samples, f"expected {expected_geo_samples} completed GEO samples")
    completed_geo = set(stage10_samples["geo_sample_id"])

    nav = pd.read_csv(paths["navigation"], dtype=str).fillna("")
    nav = nav[nav["sample_id"].isin(completed_geo)].copy()
    require(len(nav) == expected_geo_samples * 4, f"expected {expected_geo_samples * 4} completed navigation rows, found {len(nav)}")
    require(nav["status"].eq("PASS").all(), "navigation verification contains a non-PASS row")
    require(nav.groupby("sample_id")["product"].nunique().eq(4).all(), "navigation product coverage is incomplete")

    status = pd.read_csv(paths["status"], dtype=str).fillna("")
    latest_status = status.drop_duplicates(
        ["scope", "sample_id", "comparison_id", "step"],
        keep="last",
    )
    failures = latest_status[latest_status["status"].eq("FAIL")]
    if allow_partial:
        require(set(failures["sample_id"]).isdisjoint(completed_geo), "a completed GEO sample retains an unresolved FAIL")
    else:
        require(failures.empty, "experiment status log contains an unresolved FAIL")
    return paths


def confusion_metrics(tp: float, tn: float, fp: float, fn: float) -> dict[str, float]:
    total = tp + tn + fp + fn
    precision = tp / (tp + fp) if tp + fp else math.nan
    recall = tp / (tp + fn) if tp + fn else math.nan
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else math.nan
    iou = tp / (tp + fp + fn) if tp + fp + fn else math.nan
    agreement = (tp + tn) / total if total else math.nan
    denom = math.sqrt((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
    mcc = (tp * tn - fp * fn) / denom if denom else math.nan
    return {
        "agreement": agreement,
        "f1": f1,
        "iou": iou,
        "precision": precision,
        "recall": recall,
        "mcc": mcc,
    }


def bootstrap_mean_ci(values: np.ndarray, *, seed: int, draws: int = 5000) -> tuple[float, float]:
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    require(values.size > 0, "cannot bootstrap an empty metric")
    rng = np.random.Generator(np.random.PCG64(seed))
    means = np.empty(draws, dtype=np.float64)
    for start in range(0, draws, 250):
        n_draw = min(250, draws - start)
        indices = rng.integers(0, values.size, size=(n_draw, values.size))
        means[start : start + n_draw] = values[indices].mean(axis=1)
    return tuple(float(value) for value in np.quantile(means, [0.025, 0.975]))


def summarize_clm(clm: pd.DataFrame, expected_comparisons: int = EXPECTED_COMPARISONS) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    n_before = len(clm)
    clm = clm[clm["status"].astype(str).str.upper().eq("OK")].copy()
    excluded_count = n_before - len(clm)
    require(excluded_count == 0, f"CLM status filtering excluded {excluded_count} rows")
    clm = clm[clm["policy"].isin([POLICY_A, POLICY_B])].copy()
    require(clm["comparison_id"].nunique() == expected_comparisons, f"CLM comparison coverage is not {expected_comparisons}")
    require(set(clm["policy"]) == {POLICY_A, POLICY_B}, "CLM policy coverage is unexpected")
    numeric = ["agreement", "f1", "iou", "precision", "recall", "n", "tp", "tn", "fp", "fn"]
    for column in numeric:
        clm[column] = pd.to_numeric(clm[column], errors="coerce")

    rows: list[dict[str, Any]] = []
    weighted_rows: list[dict[str, Any]] = []
    for policy, group in clm.groupby("policy", sort=False):
        row: dict[str, Any] = {"policy": policy, "sample_count": int(group["comparison_id"].nunique())}
        for metric in ["agreement", "f1", "iou", "precision", "recall"]:
            values = group[metric].dropna().to_numpy(float)
            low, high = bootstrap_mean_ci(values, seed=202403 + len(rows) * 10 + len(metric))
            row.update(
                {
                    f"{metric}_mean": float(np.mean(values)),
                    f"{metric}_median": float(np.median(values)),
                    f"{metric}_std": float(np.std(values, ddof=1)),
                    f"{metric}_p05": float(np.quantile(values, 0.05)),
                    f"{metric}_p95": float(np.quantile(values, 0.95)),
                    f"{metric}_mean_ci_low": low,
                    f"{metric}_mean_ci_high": high,
                }
            )
        rows.append(row)
        counts = {key: float(group[key].sum()) for key in ["tp", "tn", "fp", "fn"]}
        weighted_rows.append(
            {
                "policy": policy,
                "n_pixels": int(group["n"].sum()),
                **{key: int(value) for key, value in counts.items()},
                **confusion_metrics(**counts),
            }
        )

    policy_a = clm[clm["policy"].eq(POLICY_A)].sort_values(["agreement", "comparison_id"]).reset_index(drop=True)
    positions = {"worst": 0, "median": len(policy_a) // 2, "best": len(policy_a) - 1}
    representative = []
    for role, position in positions.items():
        item = policy_a.iloc[position].to_dict()
        representative.append({"case_role": role, **item})
    return pd.DataFrame(rows), pd.DataFrame(weighted_rows), pd.DataFrame(representative)


def weighted_average(values: pd.Series, weights: pd.Series) -> float:
    value_array = pd.to_numeric(values, errors="coerce").to_numpy(float)
    weight_array = pd.to_numeric(weights, errors="coerce").to_numpy(float)
    valid = np.isfinite(value_array) & np.isfinite(weight_array) & (weight_array > 0)
    return float(np.average(value_array[valid], weights=weight_array[valid])) if np.any(valid) else math.nan


def summarize_cth(
    cth_sample: pd.DataFrame,
    cth_domain: pd.DataFrame | None = None,
    expected_comparisons: int = EXPECTED_COMPARISONS,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    focus = cth_sample[(cth_sample["policy"] == POLICY_A) & (cth_sample["domain"] == "D1_both_cloud")].copy()
    require(focus["sample_id"].nunique() == expected_comparisons, f"CTH Policy A D1 coverage is not {expected_comparisons}")
    numeric = [
        "n_valid_cth",
        "bias_km",
        "mae_km",
        "rmse_km",
        "median_abs_error_km",
        "p90_abs_error_km",
        "within_1km_fraction",
        "within_2km_fraction",
        "within_3km_fraction",
        "pearson_corr",
        "spearman_corr",
        "low_mid_high_class_agreement",
    ]
    for column in numeric:
        focus[column] = pd.to_numeric(focus[column], errors="coerce")
    rows: list[dict[str, Any]] = []
    row: dict[str, Any] = {
        "policy": POLICY_A,
        "domain": "D1_both_cloud",
        "sample_count": int(focus["sample_id"].nunique()),
        "n_valid_cth": int(focus["n_valid_cth"].sum()),
    }
    aggregate: pd.Series | None = None
    if cth_domain is not None:
        selected = cth_domain[(cth_domain["policy"] == POLICY_A) & (cth_domain["domain"] == "D1_both_cloud")]
        require(len(selected) == 1, "Stage 10 aggregate CTH Policy A D1 row is missing or duplicated")
        aggregate = selected.iloc[0]
        require(int(float(aggregate["n_valid_cth"])) == row["n_valid_cth"], "CTH aggregate and sample valid-pixel counts disagree")
    for metric in numeric[1:]:
        values = focus[metric].dropna().to_numpy(float)
        row[f"{metric}_sample_mean"] = float(np.mean(values))
        row[f"{metric}_sample_median"] = float(np.median(values))
        row[f"{metric}_weighted"] = (
            float(aggregate[metric])
            if aggregate is not None and metric in aggregate and pd.notna(aggregate[metric])
            else weighted_average(focus[metric], focus["n_valid_cth"])
        )
    rows.append(row)

    ordered = focus.sort_values(["mae_km", "sample_id"]).reset_index(drop=True)
    positions = {"best": 0, "median": len(ordered) // 2, "worst": len(ordered) - 1}
    representatives = []
    for role, position in positions.items():
        representatives.append({"case_role": role, **ordered.iloc[position].to_dict()})
    return pd.DataFrame(rows), pd.DataFrame(representatives)


def setup_plot_style() -> None:
    plt.rcParams.update(
        {
            "font.family": "Arial",
            "font.size": 9,
            "axes.titlesize": 11,
            "axes.labelsize": 9,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.linewidth": 0.8,
            "xtick.labelsize": 8,
            "ytick.labelsize": 8,
            "legend.fontsize": 8,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "savefig.facecolor": "white",
            "savefig.bbox": "tight",
            "svg.fonttype": "none",
            "pdf.fonttype": 42,
        }
    )


def save_figure(fig: plt.Figure, stem: Path) -> list[Path]:
    outputs = []
    for suffix, kwargs in (
        (".png", {"dpi": 300}),
        (".svg", {}),
        (".pdf", {}),
        (".tiff", {"dpi": 600, "pil_kwargs": {"compression": "tiff_lzw"}}),
    ):
        path = stem.with_suffix(suffix)
        fig.savefig(path, **kwargs)
        outputs.append(path)
    plt.close(fig)
    return outputs


def plot_clm_policy(clm: pd.DataFrame, source_path: Path, figure_dir: Path) -> list[Path]:
    metrics = ["agreement", "f1", "iou"]
    source = clm[clm["policy"].isin([POLICY_A, POLICY_B])][["comparison_id", "policy", *metrics]].copy()
    source.to_csv(source_path, index=False, encoding="utf-8-sig")
    fig, axes = plt.subplots(1, 3, figsize=(7.2, 2.65), sharey=True)
    labels = {POLICY_A: "Policy A", POLICY_B: "Policy B"}
    for axis, metric in zip(axes, metrics):
        groups = [source.loc[source["policy"].eq(policy), metric].astype(float).dropna() for policy in [POLICY_A, POLICY_B]]
        bp = axis.boxplot(groups, patch_artist=True, widths=0.52, showfliers=False)
        for patch, color in zip(bp["boxes"], [COLORS["blue"], COLORS["cyan"]]):
            patch.set_facecolor(color)
            patch.set_alpha(0.75)
        for i, values in enumerate(groups, start=1):
            jitter = np.linspace(-0.12, 0.12, len(values))
            axis.scatter(i + jitter, np.sort(values), s=7, color=COLORS["dark"], alpha=0.28, linewidths=0)
        axis.set_xticks([1, 2], [labels[POLICY_A], labels[POLICY_B]])
        axis.set_title(metric.upper())
        axis.grid(axis="y", color=COLORS["light"], linewidth=0.7)
        axis.set_ylim(0.45, 1.01)
    axes[0].set_ylabel("Sample-level score")
    sample_count = source["comparison_id"].nunique()
    fig.suptitle(f"CLM agreement across {sample_count} valid EPIC comparisons", y=1.02, fontsize=12)
    fig.text(0.01, -0.02, f"Boxes: IQR; line: median; points: individual comparisons. n = {sample_count} per policy.", fontsize=7, color=COLORS["gray"])
    fig.tight_layout()
    return save_figure(fig, figure_dir / "fig01_clm_policy_metrics")


def plot_clm_time_series(clm: pd.DataFrame, representatives: pd.DataFrame, source_path: Path, figure_dir: Path) -> list[Path]:
    source = clm[clm["policy"].eq(POLICY_A)][["comparison_id", "sample_id", "agreement", "f1", "iou"]].copy()
    source["time"] = pd.to_datetime(source["sample_id"], format="%Y%m%d_%H%M", errors="coerce")
    source = source.sort_values(["time", "comparison_id"])
    source.to_csv(source_path, index=False, encoding="utf-8-sig")
    fig, axis = plt.subplots(figsize=(7.2, 3.0))
    axis.plot(source["time"], source["agreement"], color=COLORS["blue"], linewidth=1.2, marker="o", markersize=2.6, label="Agreement")
    axis.plot(source["time"], source["f1"], color=COLORS["orange"], linewidth=1.0, alpha=0.9, label="F1")
    role_colors = {"worst": COLORS["red"], "median": COLORS["gray"], "best": COLORS["cyan"]}
    for _, row in representatives.iterrows():
        item = source[source["comparison_id"].eq(row["comparison_id"])].iloc[0]
        axis.scatter(item["time"], item["agreement"], s=38, color=role_colors[row["case_role"]], edgecolor="white", linewidth=0.7, zorder=5)
        axis.annotate(row["case_role"], (item["time"], item["agreement"]), xytext=(3, 5), textcoords="offset points", fontsize=7)
    axis.set_ylabel("Policy A score")
    axis.set_xlabel("EPIC comparison date (March 2024)")
    axis.grid(axis="y", color=COLORS["light"], linewidth=0.7)
    axis.legend(frameon=False, ncol=2, loc="lower right")
    axis.set_title("Temporal stability of CLM comparison metrics")
    fig.text(0.01, -0.02, f"n = {source['comparison_id'].nunique()} valid comparisons; frozen target failures are excluded and reported separately.", fontsize=7, color=COLORS["gray"])
    fig.tight_layout()
    return save_figure(fig, figure_dir / "fig02_clm_time_series")


def plot_cth_domains(domain: pd.DataFrame, source_path: Path, figure_dir: Path) -> list[Path]:
    source = domain[domain["policy"].eq(POLICY_A)].copy()
    source = source[source["domain"].isin(["D0_common_valid_cth", "D1_both_cloud", "D5_clean_core_cloud", "D6_boundary_or_broken_cloud", "D7_high_cloud"])]
    source.to_csv(source_path, index=False, encoding="utf-8-sig")
    labels = {
        "D0_common_valid_cth": "Common valid",
        "D1_both_cloud": "Both cloud",
        "D5_clean_core_cloud": "Clean core",
        "D6_boundary_or_broken_cloud": "Boundary",
        "D7_high_cloud": "High cloud",
    }
    source["label"] = source["domain"].map(labels)
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.1))
    axes[0].barh(source["label"], source["mae_km"], color=COLORS["blue"])
    axes[0].set_xlabel("MAE (km)")
    axes[0].set_title("Absolute error")
    axes[0].grid(axis="x", color=COLORS["light"], linewidth=0.7)
    axes[1].barh(source["label"], source["bias_km"], color=np.where(source["bias_km"] >= 0, COLORS["orange"], COLORS["cyan"]))
    axes[1].axvline(0, color=COLORS["dark"], linewidth=0.8)
    axes[1].set_xlabel("Bias, GEO minus EPIC (km)")
    axes[1].set_title("Signed error")
    axes[1].grid(axis="x", color=COLORS["light"], linewidth=0.7)
    fig.suptitle("CTH error depends on cloud domain", y=1.02, fontsize=12)
    fig.text(0.01, -0.02, "Policy A; aggregate pixel metrics. EPIC effective cloud height is a diagnostic reference, not truth.", fontsize=7, color=COLORS["gray"])
    fig.tight_layout()
    return save_figure(fig, figure_dir / "fig03_cth_domain_metrics")


def plot_cth_sources(source_df: pd.DataFrame, source_path: Path, figure_dir: Path) -> list[Path]:
    source = source_df[(source_df["policy"].eq(POLICY_A)) & (source_df["domain"].eq("D1_both_cloud"))].copy()
    source = source[pd.to_numeric(source["n_valid_cth"], errors="coerce") > 0].sort_values("mae_km")
    source.to_csv(source_path, index=False, encoding="utf-8-sig")
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.25))
    axes[0].barh(source["selected_source"], source["mae_km"], color=COLORS["blue"])
    axes[0].set_xlabel("MAE (km)")
    axes[0].set_title("Error by selected source")
    axes[0].grid(axis="x", color=COLORS["light"], linewidth=0.7)
    axes[1].barh(source["selected_source"], source["within_2km_fraction"], color=COLORS["cyan"])
    axes[1].set_xlim(0, 1)
    axes[1].set_xlabel("Within 2 km fraction")
    axes[1].set_title("Tolerance hit rate")
    axes[1].grid(axis="x", color=COLORS["light"], linewidth=0.7)
    fig.suptitle("Selected-source CTH performance on common cloudy pixels", y=1.02, fontsize=12)
    fig.text(0.01, -0.02, "Policy A, D1 both-cloud domain; categories are pixel selection outcomes, not independent product rankings.", fontsize=7, color=COLORS["gray"])
    fig.tight_layout()
    return save_figure(fig, figure_dir / "fig04_cth_selected_source")


def plot_clm_cth_relationship(clm: pd.DataFrame, cth: pd.DataFrame, source_path: Path, figure_dir: Path) -> list[Path]:
    a = clm[clm["policy"].eq(POLICY_A)][["comparison_id", "agreement", "f1", "iou"]].copy()
    b = cth[(cth["policy"].eq(POLICY_A)) & (cth["domain"].eq("D1_both_cloud"))][["sample_id", "mae_km", "bias_km", "n_valid_cth", "dominant_source"]].copy()
    source = a.merge(b, left_on="comparison_id", right_on="sample_id", validate="one_to_one")
    source.to_csv(source_path, index=False, encoding="utf-8-sig")
    categories = sorted(source["dominant_source"].fillna("Unknown").astype(str).unique())
    palette = [COLORS["blue"], COLORS["orange"], COLORS["cyan"], COLORS["red"], "#7B61A8", "#748B52", COLORS["gray"]]
    fig, axis = plt.subplots(figsize=(5.2, 3.6))
    for category, color in zip(categories, palette * 2):
        group = source[source["dominant_source"].fillna("Unknown").astype(str).eq(category)]
        axis.scatter(group["agreement"], group["mae_km"], s=18, alpha=0.72, label=category, color=color, linewidths=0)
    rho = source[["agreement", "mae_km"]].corr(method="spearman").iloc[0, 1]
    axis.set_xlabel("CLM agreement (Policy A)")
    axis.set_ylabel("CTH MAE (km, D1 both cloud)")
    axis.set_title(f"CLM agreement and CTH error are distinct diagnostics (Spearman r = {rho:.2f})")
    axis.grid(color=COLORS["light"], linewidth=0.7)
    axis.legend(frameon=False, fontsize=6.5, ncol=2, loc="best")
    fig.text(0.01, -0.02, f"n = {len(source)} paired comparisons. Color denotes the preassigned dominant-source case group.", fontsize=7, color=COLORS["gray"])
    fig.tight_layout()
    return save_figure(fig, figure_dir / "fig05_clm_cth_relationship")


def plot_cth_distribution(cth: pd.DataFrame, source_path: Path, figure_dir: Path) -> list[Path]:
    source = cth[(cth["policy"].eq(POLICY_A)) & (cth["domain"].eq("D1_both_cloud"))][["sample_id", "mae_km", "bias_km", "rmse_km", "within_2km_fraction"]].copy()
    source.to_csv(source_path, index=False, encoding="utf-8-sig")
    fig, axes = plt.subplots(1, 3, figsize=(7.2, 2.8))
    for axis, metric, color, label in zip(
        axes,
        ["mae_km", "bias_km", "within_2km_fraction"],
        [COLORS["blue"], COLORS["orange"], COLORS["cyan"]],
        ["MAE (km)", "Bias (km)", "Within 2 km"],
    ):
        values = pd.to_numeric(source[metric], errors="coerce").dropna()
        axis.hist(values, bins=14, color=color, alpha=0.85, edgecolor="white", linewidth=0.5)
        axis.axvline(values.median(), color=COLORS["dark"], linewidth=1.2, linestyle="--", label=f"median {values.median():.2f}")
        axis.set_xlabel(label)
        axis.set_ylabel("Comparisons")
        axis.legend(frameon=False, fontsize=7)
    fig.suptitle("Sample-level CTH diagnostic distribution", y=1.02, fontsize=12)
    fig.text(0.01, -0.02, f"Policy A, D1 both-cloud domain; n = {len(source)} comparisons.", fontsize=7, color=COLORS["gray"])
    fig.tight_layout()
    return save_figure(fig, figure_dir / "fig06_cth_sample_distribution")


def resolve_clm_quicklook(stage09c: Path, comparison_id: str, sample_id: str) -> Path:
    extra = stage09c / "clm_epic_comparisons" / comparison_id / "quicklooks" / "A_inclusive_binary_epic_vs_georing_cloud_mask.png"
    if extra.is_file():
        return extra
    run_dir = stage09c / "runs" / sample_id
    candidates = list(run_dir.glob("epic_l2_cloud_mask_semantic_sensitivity_*/quicklooks/A_inclusive_binary_epic_vs_georing_cloud_mask.png"))
    require(len(candidates) == 1, f"cannot resolve one CLM quicklook for {comparison_id}: {candidates}")
    return candidates[0]


def make_clm_quicklook_montage(stage09c: Path, representatives: pd.DataFrame, quicklook_dir: Path) -> list[Path]:
    copied: list[tuple[str, Path, pd.Series]] = []
    for _, row in representatives.iterrows():
        source = resolve_clm_quicklook(stage09c, str(row["comparison_id"]), str(row["sample_id"]))
        target = quicklook_dir / f"clm_{row['case_role']}_{row['comparison_id']}.png"
        shutil.copy2(source, target)
        copied.append((str(row["case_role"]), target, row))
    fig, axes = plt.subplots(3, 1, figsize=(8.4, 10.2))
    for axis, (role, path, row) in zip(axes, copied):
        axis.imshow(plt.imread(path))
        axis.set_title(f"{role.upper()} | {row['comparison_id']} | agreement={float(row['agreement']):.3f}, F1={float(row['f1']):.3f}", loc="left", fontsize=10)
        axis.axis("off")
    fig.suptitle("Representative CLM versus EPIC quicklooks (Policy A)", y=0.995, fontsize=13)
    fig.tight_layout()
    montage = quicklook_dir / "quicklook01_clm_best_median_worst.png"
    fig.savefig(montage, dpi=600, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return [path for _, path, _ in copied] + [montage]


def make_cth_quicklook(
    stage10_samples: pd.DataFrame,
    representatives: pd.DataFrame,
    quicklook_dir: Path,
) -> list[Path]:
    outputs: list[Path] = []
    selected = representatives[representatives["case_role"].isin(["median", "worst"])]
    for _, rep in selected.iterrows():
        sample = stage10_samples[stage10_samples["sample_id"].eq(rep["sample_id"])]
        require(len(sample) == 1, f"Stage 10 manifest row missing for {rep['sample_id']}")
        row = sample.iloc[0]
        run_dir = Path(str(row["stage_run_dir"]))
        epic = read_epic(Path(str(row["epic_file"])), "geophysical_data/A-band_Effective_Cloud_Height")
        fused, valid, _ = load_npz_array(run_dir / "fused_best_source" / "fused_cloud_top_height_km.npz")
        fused_on, fused_valid = sample_grid(fused, valid & (fused >= 0) & (fused <= 25), epic["lat"], epic["lon"], load_grid(run_dir))
        common = epic["cth_valid"] & fused_valid & np.isfinite(fused_on)
        step = max(1, int(math.sqrt(common.size / 160000)))
        slicer = (slice(None, None, step), slice(None, None, step))
        lon = epic["lon"][slicer]
        lat = epic["lat"][slicer]
        mask = common[slicer]
        e = epic["cth_km"][slicer]
        f = fused_on[slicer]
        panels = [(e, "EPIC effective cloud height", "viridis", 0, 18), (f, "GEO-ring fused CTH", "viridis", 0, 18), (f - e, "GEO minus EPIC", "RdBu_r", -8, 8)]
        fig, axes = plt.subplots(1, 3, figsize=(10.5, 3.4), sharex=True, sharey=True)
        for axis, (values, title, cmap, vmin, vmax) in zip(axes, panels):
            plotted = axis.scatter(lon[mask], lat[mask], c=values[mask], s=1.0, cmap=cmap, vmin=vmin, vmax=vmax, linewidths=0, rasterized=True)
            axis.set_title(title)
            axis.set_xlabel("Longitude")
            axis.grid(color="#D9DEE3", linewidth=0.35)
            fig.colorbar(plotted, ax=axis, shrink=0.76, pad=0.02, label="km")
        axes[0].set_ylabel("Latitude")
        fig.suptitle(f"{rep['case_role'].upper()} CTH case | {rep['sample_id']} | MAE={float(rep['mae_km']):.2f} km", y=1.02, fontsize=12)
        fig.text(0.01, -0.02, f"Common-valid display pixels after {step}x spatial subsampling; full-resolution n={int(rep['n_valid_cth']):,}.", fontsize=7, color=COLORS["gray"])
        fig.tight_layout()
        path = quicklook_dir / f"quicklook_cth_{rep['case_role']}_{rep['sample_id']}.png"
        fig.savefig(path, dpi=600, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        outputs.append(path)
    return outputs


def write_report(
    path: Path,
    weighted_clm: pd.DataFrame,
    cth_summary: pd.DataFrame,
    representatives_clm: pd.DataFrame,
    representatives_cth: pd.DataFrame,
    analyzed_comparisons: int,
    analyzed_geo: int,
    excluded_samples: list[str],
) -> None:
    a = weighted_clm[weighted_clm["policy"].eq(POLICY_A)].iloc[0]
    b = weighted_clm[weighted_clm["policy"].eq(POLICY_B)].iloc[0]
    c = cth_summary.iloc[0]
    lines = [
        "# 冻结 80 配对 EPIC 实验有效样本分析",
        "",
        "## 做了什么",
        "",
        f"- 冻结目标为 80 个 EPIC 配对、79 个 GEO 时次；实际分析 {analyzed_comparisons} 个配对、{analyzed_geo} 个 GEO 时次。",
        f"- 排除失败时次：{', '.join(excluded_samples) if excluded_samples else '无'}。排除项不进入统计分母。",
        "- 汇总 CLM 分类指标、CTH 高度诊断、时间稳定性和来源分层，并生成代表性空间 quicklook。",
        "- CLM 使用像元级混淆矩阵加权总指标；CTH 以 Policy A、D1 双方均判云域为主。",
        "",
        "## 关键数值",
        "",
        f"- CLM Policy A：agreement={a['agreement']:.4f}，F1={a['f1']:.4f}，IoU={a['iou']:.4f}，MCC={a['mcc']:.4f}，有效像元={int(a['n_pixels']):,}。",
        f"- CLM Policy B：agreement={b['agreement']:.4f}，F1={b['f1']:.4f}，IoU={b['iou']:.4f}，MCC={b['mcc']:.4f}，有效像元={int(b['n_pixels']):,}。",
        f"- CTH Policy A / D1：加权 MAE={c['mae_km_weighted']:.3f} km，加权 bias={c['bias_km_weighted']:.3f} km，加权 within-2-km={c['within_2km_fraction_weighted']:.3f}，有效像元={int(c['n_valid_cth']):,}。",
        f"- CLM 代表案例：{', '.join(representatives_clm['comparison_id'].astype(str))}（worst/median/best）。",
        f"- CTH 代表案例：{', '.join(representatives_cth['sample_id'].astype(str))}（best/median/worst）。",
        "",
        "## 最终状态",
        "",
        f"`EPIC80_VALID{analyzed_comparisons}_CLM_CTH_ANALYSIS_PASS`",
        "",
        "## 下一步",
        "",
        "当前步骤是结果解释与汇报，不需要修改 production reader；后续应先审阅图表和 PPT，再决定是否进入 648 时次整月重跑。",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage09c-root", type=Path, default=DEFAULT_STAGE09C_ROOT)
    parser.add_argument("--stage10-root", type=Path, default=DEFAULT_STAGE10_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--expected-comparisons", type=int, default=EXPECTED_COMPARISONS)
    parser.add_argument("--expected-geo-samples", type=int, default=EXPECTED_GEO_SAMPLES)
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args()

    stage09c = args.stage09c_root.resolve()
    stage10 = args.stage10_root.resolve()
    output = args.output_root.resolve()
    dirs = ensure_output_dirs(output)
    paths = validate_inputs(
        stage09c,
        stage10,
        expected_comparisons=args.expected_comparisons,
        expected_geo_samples=args.expected_geo_samples,
        allow_partial=args.allow_partial,
    )
    setup_plot_style()

    figure_contract = {
        "core_conclusion": "Quantify CLM classification stability and CTH diagnostic behavior across the frozen 80 EPIC comparisons.",
        "evidence_chain": ["completed-run hard gates", "pixel-weighted CLM confusion metrics", "sample and pixel-weighted CTH summaries", "representative spatial quicklooks"],
        "archetypes": ["distribution", "time series", "group comparison", "paired diagnostic relationship", "spatial quicklook"],
        "export_contract": {"figures": ["PNG 300 dpi", "SVG", "PDF", "TIFF 600 dpi LZW"], "quicklooks": "PNG", "source_data": "UTF-8-SIG CSV"},
        "created_utc": utc_now(),
    }
    (dirs["control"] / "figure_contract.json").write_text(json.dumps(figure_contract, ensure_ascii=False, indent=2), encoding="utf-8")

    clm = pd.read_csv(paths["clm"], encoding="utf-8-sig")
    binary_clm = clm[clm["policy"].isin([POLICY_A, POLICY_B])].copy()
    clm_summary, clm_weighted, clm_representatives = summarize_clm(binary_clm, args.expected_comparisons)
    cth_domain = pd.read_csv(paths["cth_domain"], encoding="utf-8-sig")
    cth_sample = pd.read_csv(paths["cth_sample"], encoding="utf-8-sig")
    cth_summary, cth_representatives = summarize_cth(cth_sample, cth_domain, args.expected_comparisons)
    cth_source = pd.read_csv(paths["cth_source"], encoding="utf-8-sig")
    stage10_samples = pd.read_csv(paths["stage10_samples"], dtype=str, encoding="utf-8-sig").fillna("")

    table_paths = {
        "clm_summary": dirs["tables"] / "clm_policy_sample_summary.csv",
        "clm_weighted": dirs["tables"] / "clm_policy_pixel_weighted_metrics.csv",
        "clm_representatives": dirs["tables"] / "clm_representative_cases.csv",
        "cth_summary": dirs["tables"] / "cth_policy_a_d1_summary.csv",
        "cth_representatives": dirs["tables"] / "cth_representative_cases.csv",
    }
    for frame, path in [
        (clm_summary, table_paths["clm_summary"]),
        (clm_weighted, table_paths["clm_weighted"]),
        (clm_representatives, table_paths["clm_representatives"]),
        (cth_summary, table_paths["cth_summary"]),
        (cth_representatives, table_paths["cth_representatives"]),
    ]:
        frame.to_csv(path, index=False, encoding="utf-8-sig")

    figure_paths: list[Path] = []
    figure_paths += plot_clm_policy(binary_clm, dirs["source"] / "fig01_clm_policy_metrics.csv", dirs["figures"])
    figure_paths += plot_clm_time_series(binary_clm, clm_representatives, dirs["source"] / "fig02_clm_time_series.csv", dirs["figures"])
    figure_paths += plot_cth_domains(cth_domain, dirs["source"] / "fig03_cth_domain_metrics.csv", dirs["figures"])
    figure_paths += plot_cth_sources(cth_source, dirs["source"] / "fig04_cth_selected_source.csv", dirs["figures"])
    figure_paths += plot_clm_cth_relationship(binary_clm, cth_sample, dirs["source"] / "fig05_clm_cth_relationship.csv", dirs["figures"])
    figure_paths += plot_cth_distribution(cth_sample, dirs["source"] / "fig06_cth_sample_distribution.csv", dirs["figures"])
    quicklook_paths = make_clm_quicklook_montage(stage09c, clm_representatives, dirs["quicklooks"])
    quicklook_paths += make_cth_quicklook(stage10_samples, cth_representatives, dirs["quicklooks"])

    status = pd.read_csv(paths["status"], dtype=str, encoding="utf-8-sig").fillna("")
    latest_batch = status[status["scope"].eq("batch_case")].drop_duplicates("sample_id", keep="last")
    excluded_samples = sorted(latest_batch.loc[latest_batch["status"].eq("FAIL"), "sample_id"].astype(str).tolist())
    warnings_path = dirs["control"] / "warnings.csv"
    warning_rows = [
        {
            "level": "WARNING",
            "source": "stage_09c",
            "sample_id": row["sample_id"],
            "figure_id": "",
            "message": row.get("message", "excluded failed GEO reconstruction"),
            "traceback": "",
        }
        for _, row in latest_batch[latest_batch["status"].eq("FAIL")].iterrows()
    ]
    pd.DataFrame(warning_rows, columns=["level", "source", "sample_id", "figure_id", "message", "traceback"]).to_csv(
        warnings_path,
        index=False,
        encoding="utf-8-sig",
    )
    report = dirs["reports"] / "epic_80_valid_subset_clm_cth_analysis_summary_cn.md"
    write_report(
        report,
        clm_weighted,
        cth_summary,
        clm_representatives,
        cth_representatives,
        args.expected_comparisons,
        args.expected_geo_samples,
        excluded_samples,
    )
    a = clm_weighted[clm_weighted["policy"].eq(POLICY_A)].iloc[0]
    b = clm_weighted[clm_weighted["policy"].eq(POLICY_B)].iloc[0]
    c = cth_summary.iloc[0]
    summary_json = output / "analysis_summary.json"
    summary = {
        "status": f"EPIC80_VALID{args.expected_comparisons}_CLM_CTH_ANALYSIS_PASS",
        "created_utc": utc_now(),
        "frozen_comparison_count": EXPECTED_COMPARISONS,
        "frozen_unique_geo_count": EXPECTED_GEO_SAMPLES,
        "comparison_count": args.expected_comparisons,
        "unique_geo_count": args.expected_geo_samples,
        "excluded_samples": excluded_samples,
        "clm": {
            "policy_a": {key: float(a[key]) for key in ["agreement", "f1", "iou", "precision", "recall", "mcc"]} | {"n_pixels": int(a["n_pixels"])},
            "policy_b": {key: float(b[key]) for key in ["agreement", "f1", "iou", "precision", "recall", "mcc"]} | {"n_pixels": int(b["n_pixels"])},
            "representative_cases": clm_representatives[["case_role", "comparison_id", "sample_id", "agreement", "f1", "iou"]].to_dict("records"),
        },
        "cth": {
            "policy_a_d1": {
                "n_valid_cth": int(c["n_valid_cth"]),
                "mae_km": float(c["mae_km_weighted"]),
                "bias_km": float(c["bias_km_weighted"]),
                "rmse_km": float(c["rmse_km_weighted"]),
                "within_2km_fraction": float(c["within_2km_fraction_weighted"]),
                "pearson_corr": float(c["pearson_corr_weighted"]),
            },
            "representative_cases": cth_representatives[["case_role", "sample_id", "mae_km", "bias_km", "n_valid_cth"]].to_dict("records"),
        },
        "assets": {
            "figures": [str(path) for path in figure_paths if path.suffix == ".png"],
            "quicklooks": [str(path) for path in quicklook_paths],
            "tables": {key: str(value) for key, value in table_paths.items()},
            "report": str(report),
            "warnings": str(warnings_path),
        },
    }
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    artifact_rows = []
    for path in [*table_paths.values(), *figure_paths, *quicklook_paths, warnings_path, report, summary_json]:
        artifact_rows.append({"path": str(path), "size_bytes": path.stat().st_size, "sha256": sha256(path)})
    artifact_index = output / "artifact_manifest.csv"
    pd.DataFrame(artifact_rows).to_csv(artifact_index, index=False, encoding="utf-8-sig")
    write_manifest(
        output / "manifest.json",
        canonical_stage_id="stage_10",
        component_role=COMPONENT_ROLE,
        related_stage_ids=("stage_09c",),
        generating_script=Path(__file__).resolve(),
        input_paths=tuple(paths.values()),
        output_paths=(output, summary_json, report, artifact_index),
        parameters={
            "frozen_comparison_count": 80,
            "comparison_count": args.expected_comparisons,
            "unique_geo_count": args.expected_geo_samples,
            "excluded_samples": excluded_samples,
            "bootstrap_draws": 5000,
            "cth_primary_domain": "A_inclusive_binary/D1_both_cloud",
        },
        project_root=PROJECT_ROOT,
        run_id=RUN_ID,
        source_profile="operational_baseline",
        extra={"final_status": "PASS", "analysis_status": summary["status"], "coverage_status": f"{args.expected_comparisons}/80"},
    )
    print(f"{summary['status']}: {summary_json}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
