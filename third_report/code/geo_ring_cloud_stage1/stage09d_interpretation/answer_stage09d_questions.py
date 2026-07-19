from __future__ import annotations

import math
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from geo_ring_cloud.paths import CODE_ROOT, RUNS_ROOT  # noqa: E402


ROOT = RUNS_ROOT / "stage09d_full_pixel_diagnostics_202403"
OUT = ROOT / "interpretation_package" / "stage09d_question_followup"
FIG = OUT / "figures"
TAB = OUT / "tables"
STAGE09D_CODE = CODE_ROOT / "stage09d_full_pixel_diagnostics"


def read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding="utf-8-sig")


def num(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def wmean(df: pd.DataFrame, value: str, weight: str) -> float:
    d = df[[value, weight]].dropna()
    d = d[d[weight] > 0]
    if d.empty:
        return math.nan
    return float(np.average(d[value], weights=d[weight]))


def write(df: pd.DataFrame, name: str) -> Path:
    TAB.mkdir(parents=True, exist_ok=True)
    path = TAB / name
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def fmt(v: object) -> str:
    try:
        x = float(v)
    except Exception:
        return "NA"
    return f"{x:.3f}" if math.isfinite(x) else "NA"


def md_table(df: pd.DataFrame, cols: list[str] | None = None, n: int = 30) -> str:
    if df.empty:
        return "_无可用数据_"
    sub = df.copy()
    if cols:
        sub = sub[cols]
    sub = sub.head(n)
    lines = [
        "| " + " | ".join(sub.columns.astype(str)) + " |",
        "| " + " | ".join(["---"] * len(sub.columns)) + " |",
    ]
    for _, row in sub.iterrows():
        cells = []
        for c in sub.columns:
            v = row[c]
            if isinstance(v, float):
                cells.append(fmt(v))
            else:
                cells.append(str(v).replace("|", "\\|"))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def bar(
    path: Path,
    df: pd.DataFrame,
    label: str,
    value: str,
    title: str,
    ylabel: str,
    ylim: tuple[float, float] | None = (0, 1),
) -> None:
    FIG.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(max(8, 0.7 * len(df)), 4.5), constrained_layout=True)
    ax.bar(np.arange(len(df)), df[value].astype(float), color="#386fa4")
    ax.set_xticks(np.arange(len(df)))
    ax.set_xticklabels(df[label].astype(str), rotation=35, ha="right")
    if ylim:
        ax.set_ylim(*ylim)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(axis="y", alpha=0.25)
    fig.savefig(path, dpi=170)
    plt.close(fig)


def build_valid_count_pixel_diagnostics() -> tuple[pd.DataFrame, pd.DataFrame]:
    sys.path.insert(0, str(STAGE09D_CODE))
    import run_stage09d_full_pixel_diagnostics as d  # type: ignore

    manifest = d.read_csv(ROOT / "00_sample_manifest" / "stage09d_53_sample_manifest.csv")
    bins = [("1", 1, 2), ("2", 2, 3), ("3", 3, 4), (">=4", 4, 100)]
    rows = []
    selected_rows = []
    for row in manifest:
        if not d.truthy(row.get("can_run_source_pair")):
            continue
        ctx = d.sample_context(row)
        epic = ctx["epic"]
        epic_cls, epic_pv = d.apply_policy(epic["cloud_mask"], d.POLICIES["A_inclusive_binary"]["epic"])
        valid_earth = np.isin(epic["cloud_mask"], [1, 2, 3, 4])
        labels = d.classify_array(ctx["valid_count"], bins)
        fused_cls, fused_pv = d.apply_policy(ctx["fused_on_epic"], d.POLICIES["A_inclusive_binary"]["geo"])
        fused_base = valid_earth & epic_pv & ctx["fused_on_valid"] & fused_pv
        source_id_to_name = {0: "none", **d.SOURCE_ID}
        for label, _, _ in bins:
            label_valid = fused_base & (labels == label)
            label_n = int(np.count_nonzero(label_valid))
            if label_n == 0:
                continue
            for sid, name in source_id_to_name.items():
                n = int(np.count_nonzero(label_valid & (ctx["selected_source"] == sid)))
                if n:
                    selected_rows.append(
                        {
                            "sample_id": row["sample_id"],
                            "valid_source_count_bin": label,
                            "selected_source": name,
                            "n_pixels": n,
                            "fraction_in_bin": n / label_n,
                        }
                    )
        for source in d.SOURCES:
            pref = d.find_prefusion(ctx["run_dir"], source, row["sample_id"])
            if pref is None:
                continue
            arrs = d.load_npz(pref)
            raw_valid = np.asarray(arrs.get("fusion_valid_mask", arrs.get("valid_mask", np.isfinite(arrs["data"])))).astype(bool)
            raw_on, raw_valid_on = d.sample_grid(arrs["data"], raw_valid, epic["lat"], epic["lon"], ctx["grid"])
            std = d.source_to_standard(source, raw_on)
            geo_cls, geo_pv = d.apply_policy(std, d.POLICIES["A_inclusive_binary"]["geo"])
            base_valid = valid_earth & epic_pv & raw_valid_on & (std >= 0) & geo_pv
            for label, _, _ in bins:
                valid = base_valid & (labels == label)
                if int(np.count_nonzero(valid)) == 0:
                    continue
                m = d.binary_metrics(epic_cls, geo_cls, valid, d.POLICIES["A_inclusive_binary"]["positive"])
                rows.append(
                    {
                        "sample_id": row["sample_id"],
                        "source_name": source,
                        "valid_source_count_bin": label,
                        "n_valid": m.get("n_valid"),
                        "agreement": m.get("agreement"),
                        "f1_cloud": m.get("f1_cloud"),
                        "iou_cloud": m.get("iou_cloud"),
                        "cloud_fraction_epic": m.get("cloud_fraction_epic"),
                        "cloud_fraction_source": m.get("cloud_fraction_source"),
                    }
                )
    raw = pd.DataFrame(rows)
    if raw.empty:
        return raw
    raw_csv = write(raw, "single_source_by_valid_source_count_policyA_raw.csv")
    summary = (
        raw.groupby(["valid_source_count_bin", "source_name"], dropna=False)
        .apply(
            lambda g: pd.Series(
                {
                    "sample_count": g["sample_id"].nunique(),
                    "row_count": len(g),
                    "n_valid_total": g["n_valid"].sum(),
                    "agreement_weighted": wmean(g, "agreement", "n_valid"),
                    "f1_weighted": wmean(g, "f1_cloud", "n_valid"),
                    "iou_weighted": wmean(g, "iou_cloud", "n_valid"),
                    "cloud_fraction_epic_weighted": wmean(g, "cloud_fraction_epic", "n_valid"),
                    "cloud_fraction_source_weighted": wmean(g, "cloud_fraction_source", "n_valid"),
                    "raw_csv": str(raw_csv),
                }
            )
        )
        .reset_index()
        .sort_values(["valid_source_count_bin", "agreement_weighted"])
    )
    selected_raw = pd.DataFrame(selected_rows)
    write(selected_raw, "selected_source_by_valid_source_count_policyA_raw.csv")
    selected_summary = (
        selected_raw.groupby(["valid_source_count_bin", "selected_source"], dropna=False)
        .agg(sample_count=("sample_id", "nunique"), n_pixels=("n_pixels", "sum"))
        .reset_index()
    )
    totals = selected_summary.groupby("valid_source_count_bin")["n_pixels"].transform("sum")
    selected_summary["fraction_in_bin"] = selected_summary["n_pixels"] / totals
    selected_summary = selected_summary.sort_values(["valid_source_count_bin", "fraction_in_bin"], ascending=[True, False])
    return summary, selected_summary


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    src = num(
        read_csv(ROOT / "01_source_pair_recompute" / "stage09d_source_by_source_metrics.csv"),
        ["n_valid", "agreement", "f1_cloud", "iou_cloud", "cloud_fraction_epic", "cloud_fraction_source"],
    )
    pair = num(
        read_csv(ROOT / "01_source_pair_recompute" / "stage09d_source_pair_overlap_metrics.csv"),
        [
            "n_overlap_valid",
            "A_agreement_to_epic",
            "B_agreement_to_epic",
            "A_f1",
            "B_f1",
            "A_iou",
            "B_iou",
            "source_disagreement_fraction",
            "both_wrong_fraction",
        ],
    )
    geom = num(
        read_csv(ROOT / "03_geometry_stratification" / "stage09d_geometry_bin_metrics.csv"),
        ["n_valid", "agreement", "mismatch_rate", "pixel_fraction", "mismatch_fraction", "enrichment"],
    )
    ge = num(
        read_csv(ROOT / "03_geometry_stratification" / "stage09d_geometry_mismatch_enrichment.csv"),
        ["sample_count", "agreement_mean", "mismatch_rate_mean", "enrichment_mean"],
    )
    scene = num(
        read_csv(ROOT / "04_boundary_broken_cloud" / "stage09d_scene_metrics_by_class.csv"),
        ["sample_count", "agreement_mean", "mismatch_rate_mean", "mismatch_enrichment_mean"],
    )
    vis = num(
        read_csv(OUT.parent / "visibility_filter_sensitivity" / "geo_visible_valid_source_count_bin_metrics.csv"),
        ["sample_count", "n_valid", "agreement_weighted", "mismatch_weighted", "enrichment_mean"],
    )

    stable_src = src[src["n_valid"] >= 50000].copy()
    source_policy = (
        stable_src.groupby(["policy", "source_name"], dropna=False)
        .apply(
            lambda g: pd.Series(
                {
                    "sample_count": g["sample_id"].nunique(),
                    "row_count": len(g),
                    "n_valid_total": g["n_valid"].sum(),
                    "agreement_weighted": wmean(g, "agreement", "n_valid"),
                    "f1_weighted": wmean(g, "f1_cloud", "n_valid"),
                    "iou_weighted": wmean(g, "iou_cloud", "n_valid"),
                    "epic_positive_fraction_weighted": wmean(g, "cloud_fraction_epic", "n_valid"),
                    "source_positive_fraction_weighted": wmean(g, "cloud_fraction_source", "n_valid"),
                }
            )
        )
        .reset_index()
        .sort_values(["policy", "agreement_weighted"], ascending=[True, False])
    )
    source_policy_csv = write(source_policy, "source_metrics_all_policies_with_f1_iou.csv")
    policy_c_source = source_policy[source_policy["policy"] == "C_uncertainty_aware_3class"].copy()
    policy_c_csv = write(policy_c_source, "source_metrics_policyC_summary.csv")

    stable_pair = pair[pair["n_overlap_valid"] >= 50000].copy()
    pair_summary = (
        stable_pair.groupby(["policy", "source_A", "source_B"], dropna=False)
        .agg(
            sample_count=("sample_id", "nunique"),
            row_count=("sample_id", "count"),
            n_overlap_valid_mean=("n_overlap_valid", "mean"),
            A_agreement_mean=("A_agreement_to_epic", "mean"),
            B_agreement_mean=("B_agreement_to_epic", "mean"),
            A_f1_mean=("A_f1", "mean"),
            B_f1_mean=("B_f1", "mean"),
            A_iou_mean=("A_iou", "mean"),
            B_iou_mean=("B_iou", "mean"),
            source_disagreement_mean=("source_disagreement_fraction", "mean"),
            both_wrong_mean=("both_wrong_fraction", "mean"),
        )
        .reset_index()
        .sort_values(["policy", "source_disagreement_mean"], ascending=[True, False])
    )
    pair_csv = write(pair_summary, "source_pair_all_policies_with_f1_iou.csv")

    ge_a = ge[ge["policy"] == "A_inclusive_binary"].sort_values("enrichment_mean", ascending=False)
    ge_csv = write(ge_a, "geometry_policyA_all_bins_sorted_by_enrichment.csv")
    geom_weighted = []
    for (dim, b), g in geom[geom["policy"] == "A_inclusive_binary"].groupby(["dimension", "bin"], dropna=False):
        geom_weighted.append(
            {
                "dimension": dim,
                "bin": b,
                "sample_count": g["sample_id"].nunique(),
                "n_valid_total": g["n_valid"].sum(),
                "agreement_weighted": wmean(g, "agreement", "n_valid"),
                "mismatch_rate_weighted": wmean(g, "mismatch_rate", "n_valid"),
                "pixel_fraction_mean": g["pixel_fraction"].mean(),
                "mismatch_fraction_mean": g["mismatch_fraction"].mean(),
                "enrichment_mean": g["enrichment"].mean(),
            }
        )
    geom_weighted = pd.DataFrame(geom_weighted).sort_values("enrichment_mean", ascending=False)
    geom_weighted_csv = write(geom_weighted, "geometry_policyA_weighted_bin_context.csv")

    count_context = geom_weighted[geom_weighted["dimension"] == "valid_source_count_bin"].copy()
    count_context_csv = write(count_context, "valid_source_count_policyA_context.csv")
    source_by_count = (
        geom[(geom["policy"] == "A_inclusive_binary") & (geom["dimension"] == "valid_source_count_bin")]
        .groupby(["bin", "dominant_source"], dropna=False)
        .apply(
            lambda g: pd.Series(
                {
                    "sample_count": g["sample_id"].nunique(),
                    "row_count": len(g),
                    "n_valid_total": g["n_valid"].sum(),
                    "agreement_weighted": wmean(g, "agreement", "n_valid"),
                    "mismatch_rate_weighted": wmean(g, "mismatch_rate", "n_valid"),
                    "pixel_fraction_mean": g["pixel_fraction"].mean(),
                    "enrichment_mean": g["enrichment"].mean(),
                }
            )
        )
        .reset_index()
        .sort_values(["bin", "agreement_weighted"])
    )
    source_by_count_csv = write(source_by_count, "valid_source_count_by_dominant_source_policyA.csv")
    single_source_count, selected_source_count = build_valid_count_pixel_diagnostics()
    single_source_count_csv = write(single_source_count, "single_source_by_valid_source_count_policyA_summary.csv")
    selected_source_count_csv = write(selected_source_count, "selected_source_by_valid_source_count_policyA_summary.csv")

    scene_a = scene[scene["policy"] == "A_inclusive_binary"].copy()
    scene_csv = write(scene_a, "boundary_scene_policyA_definition_context.csv")
    vis_csv = write(vis, "geo_visible_valid_source_count_reference.csv")

    plot_index = []
    bar(FIG / "policyC_source_agreement.png", policy_c_source, "source_name", "agreement_weighted", "Policy C source agreement", "agreement")
    plot_index.append({"plot_path": str(FIG / "policyC_source_agreement.png"), "source_csv": str(policy_c_csv), "description": "Policy C 单源 agreement"})
    top_geom = ge_a.head(12)
    geom_ymax = max(2.0, float(top_geom["enrichment_mean"].max()) * 1.15)
    bar(
        FIG / "geometry_policyA_top_enrichment.png",
        top_geom.assign(label=top_geom["dimension"] + ":" + top_geom["bin"]),
        "label",
        "enrichment_mean",
        "Policy A top geometry enrichment",
        "enrichment",
        (0, geom_ymax),
    )
    plot_index.append({"plot_path": str(FIG / "geometry_policyA_top_enrichment.png"), "source_csv": str(ge_csv), "description": "Policy A 几何 bin 富集排序"})
    bar(FIG / "valid_source_count_agreement_policyA.png", count_context.sort_values("bin"), "bin", "agreement_weighted", "Policy A agreement by valid_source_count", "agreement")
    plot_index.append({"plot_path": str(FIG / "valid_source_count_agreement_policyA.png"), "source_csv": str(count_context_csv), "description": "valid_source_count bin 的 Policy A agreement"})
    write(pd.DataFrame(plot_index), "stage09d_question_followup_plot_index.csv")

    report = [
        "# Stage 09D Question Follow-up",
        "",
        "本补充只读取既有 Stage09C/09D CSV，不修改 fused cloud mask 生产逻辑，不生成 fusion v2。",
        "",
        "## Policy C 单源指标",
        md_table(policy_c_source, ["source_name", "sample_count", "n_valid_total", "agreement_weighted", "f1_weighted", "iou_weighted", "epic_positive_fraction_weighted", "source_positive_fraction_weighted"]),
        "",
        "## Source-pair 全 Policy 与 F1/IoU",
        md_table(pair_summary, ["policy", "source_A", "source_B", "sample_count", "A_agreement_mean", "B_agreement_mean", "A_f1_mean", "B_f1_mean", "A_iou_mean", "B_iou_mean", "source_disagreement_mean"]),
        "",
        "## Policy A 几何富集排序",
        md_table(ge_a, ["dimension", "bin", "sample_count", "agreement_mean", "mismatch_rate_mean", "enrichment_mean"]),
        "",
        "## valid_source_count 上下文",
        md_table(count_context, ["bin", "sample_count", "n_valid_total", "agreement_weighted", "mismatch_rate_weighted", "pixel_fraction_mean", "mismatch_fraction_mean", "enrichment_mean"]),
        "",
        "## valid_source_count x dominant_source",
        md_table(source_by_count, ["bin", "dominant_source", "sample_count", "n_valid_total", "agreement_weighted", "pixel_fraction_mean", "enrichment_mean"]),
        "",
        "## valid_source_count x single source（Policy A 像元级重算）",
        md_table(single_source_count, ["valid_source_count_bin", "source_name", "sample_count", "n_valid_total", "agreement_weighted", "f1_weighted", "iou_weighted", "cloud_fraction_source_weighted"]),
        "",
        "## valid_source_count x selected source（fused 选择分布）",
        md_table(selected_source_count, ["valid_source_count_bin", "selected_source", "sample_count", "n_pixels", "fraction_in_bin"]),
        "",
        "## Boundary / scene 定义上下文",
        md_table(scene_a, ["scene_type", "boundary_class", "sample_count", "agreement_mean", "mismatch_rate_mean", "mismatch_enrichment_mean"]),
        "",
        "## 输出索引",
        f"- `{source_policy_csv}`",
        f"- `{policy_c_csv}`",
        f"- `{pair_csv}`",
        f"- `{ge_csv}`",
        f"- `{geom_weighted_csv}`",
        f"- `{count_context_csv}`",
        f"- `{source_by_count_csv}`",
        f"- `{single_source_count_csv}`",
        f"- `{selected_source_count_csv}`",
        f"- `{scene_csv}`",
        f"- `{vis_csv}`",
    ]
    (OUT / "stage09d_question_followup_report.md").write_text("\n".join(report), encoding="utf-8")


if __name__ == "__main__":
    main()
