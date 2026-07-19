from __future__ import annotations

import math
import shutil
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

from geo_ring_cloud.lineage import utc_now  # noqa: E402
from geo_ring_cloud.paths import RUNS_ROOT  # noqa: E402


STAGE_ID = "stage_09d"
ROOT = RUNS_ROOT / "stage09d_full_pixel_diagnostics_202403"
STAGE09C = RUNS_ROOT / "stage09c_scaled_202403_batch"
OUT = ROOT / "interpretation_package"
FIG = OUT / "figures"
TAB = OUT / "tables"

POLICY_LABEL = {
    "A_inclusive_binary": "A 包容二值",
    "B_high_confidence_only": "B 高置信",
    "C_uncertainty_aware_3class": "C 三类不确定性",
}

GROUP_LABEL = {
    "METEOSAT_DOMINANT_CONTROL": "Meteosat-dominant",
    "GOES_DOMINANT_CONTROL": "GOES-dominant",
    "EAST_ASIA_FY4B_HIMAWARI_PRIORITY": "East Asia",
    "MIXED_OR_BOUNDARY": "Mixed/boundary",
}

EVIDENCE_SCORE = {
    "strong": 4,
    "moderate": 3,
    "weak": 2,
    "not_supported": 1,
    "insufficient_data": 0,
}


def setup_dirs() -> None:
    for d in [OUT, FIG, TAB]:
        d.mkdir(parents=True, exist_ok=True)


def set_font() -> None:
    plt.rcParams["axes.unicode_minus"] = False
    for font in ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Arial Unicode MS", "DejaVu Sans"]:
        plt.rcParams["font.sans-serif"] = [font]
        break


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, encoding="utf-8-sig")


def num(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def write_csv(df: pd.DataFrame, name: str) -> Path:
    path = TAB / name
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def md_table(df: pd.DataFrame, n: int = 20, cols: list[str] | None = None) -> str:
    if df.empty:
        return "_无可用数据_"
    sub = df.copy()
    if cols:
        sub = sub[cols]
    sub = sub.head(n)
    def cell(v: object) -> str:
        if isinstance(v, float):
            if math.isfinite(v):
                return f"{v:.3f}"
            return "NA"
        text = "" if pd.isna(v) else str(v)
        return text.replace("|", "\\|").replace("\n", "<br>")
    headers = [str(c) for c in sub.columns]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for _, row in sub.iterrows():
        lines.append("| " + " | ".join(cell(row[c]) for c in sub.columns) + " |")
    return "\n".join(lines)


def fmt(x: object, digits: int = 3) -> str:
    try:
        v = float(x)
    except Exception:
        return "NA"
    if not math.isfinite(v):
        return "NA"
    return f"{v:.{digits}f}"


def weighted_mean(df: pd.DataFrame, value: str, weight: str = "n_valid") -> float:
    d = df[[value, weight]].dropna()
    d = d[d[weight] > 0]
    if d.empty:
        return math.nan
    return float(np.average(d[value], weights=d[weight]))


def bar_plot(path: Path, labels: list[str], values: list[float], title: str, ylabel: str, source: Path, plot_index: list[dict[str, str]], color: str = "#386fa4", ylim: tuple[float, float] | None = None) -> None:
    fig, ax = plt.subplots(figsize=(max(8, 0.55 * len(labels)), 4.8), constrained_layout=True)
    ax.bar(range(len(labels)), values, color=color)
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=35, ha="right")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(axis="y", alpha=0.25)
    if ylim:
        ax.set_ylim(*ylim)
    fig.savefig(path, dpi=180)
    plt.close(fig)
    add_plot(plot_index, path, source, title)


def grouped_bar(path: Path, df: pd.DataFrame, xcol: str, value_cols: list[str], title: str, ylabel: str, source: Path, plot_index: list[dict[str, str]], ylim: tuple[float, float] | None = None) -> None:
    labels = df[xcol].astype(str).tolist()
    x = np.arange(len(labels))
    width = 0.8 / max(len(value_cols), 1)
    fig, ax = plt.subplots(figsize=(max(9, 0.9 * len(labels)), 5.0), constrained_layout=True)
    colors = ["#386fa4", "#2a9d8f", "#e76f51", "#7a5195"]
    for i, col in enumerate(value_cols):
        ax.bar(x + (i - (len(value_cols) - 1) / 2) * width, df[col].astype(float), width=width, label=col, color=colors[i % len(colors)])
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=30, ha="right")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend()
    ax.grid(axis="y", alpha=0.25)
    if ylim:
        ax.set_ylim(*ylim)
    fig.savefig(path, dpi=180)
    plt.close(fig)
    add_plot(plot_index, path, source, title)


def heatmap(path: Path, mat: pd.DataFrame, title: str, source: Path, plot_index: list[dict[str, str]], cmap: str = "viridis", vmin: float | None = None, vmax: float | None = None) -> None:
    fig, ax = plt.subplots(figsize=(max(7, 0.65 * len(mat.columns)), max(5, 0.45 * len(mat.index))), constrained_layout=True)
    data = mat.astype(float).to_numpy()
    im = ax.imshow(data, cmap=cmap, vmin=vmin, vmax=vmax, aspect="auto")
    ax.set_xticks(np.arange(len(mat.columns)))
    ax.set_xticklabels(mat.columns, rotation=40, ha="right")
    ax.set_yticks(np.arange(len(mat.index)))
    ax.set_yticklabels(mat.index)
    ax.set_title(title)
    for i in range(data.shape[0]):
        for j in range(data.shape[1]):
            if np.isfinite(data[i, j]):
                ax.text(j, i, f"{data[i, j]:.2f}", ha="center", va="center", color="white" if data[i, j] < np.nanmean(data) else "black", fontsize=8)
    fig.colorbar(im, ax=ax, fraction=0.035)
    fig.savefig(path, dpi=180)
    plt.close(fig)
    add_plot(plot_index, path, source, title)


def add_plot(plot_index: list[dict[str, str]], path: Path, source: Path, desc: str) -> None:
    plot_index.append({
        "plot_path": str(path),
        "source_csv": str(source),
        "description_cn": desc,
        "created_time_utc": utc_now(),
    })


def load_inputs() -> dict[str, pd.DataFrame]:
    d: dict[str, pd.DataFrame] = {}
    d["sample09c"] = read_csv(STAGE09C / "02_expanded_diagnostics" / "stage09c_sample_level_semantic_summary.csv")
    d["group09c"] = read_csv(STAGE09C / "02_expanded_diagnostics" / "stage09c_group_level_summary.csv")
    d["source"] = read_csv(ROOT / "01_source_pair_recompute" / "stage09d_source_by_source_metrics.csv")
    d["pair"] = read_csv(ROOT / "01_source_pair_recompute" / "stage09d_source_pair_overlap_metrics.csv")
    d["pair_summary"] = read_csv(ROOT / "01_source_pair_recompute" / "stage09d_source_pair_summary_by_pair.csv")
    d["meteosat_pair"] = read_csv(ROOT / "01_source_pair_recompute" / "stage09d_meteosat_pair_special_audit.csv")
    d["pair_cases"] = read_csv(ROOT / "01_source_pair_recompute" / "stage09d_source_pair_disagreement_cases.csv")
    d["sampling"] = read_csv(ROOT / "02_sampling_sensitivity" / "stage09d_sampling_sensitivity_by_sample.csv")
    d["sampling_group"] = read_csv(ROOT / "02_sampling_sensitivity" / "stage09d_sampling_sensitivity_by_group.csv")
    d["sampling_boundary"] = read_csv(ROOT / "02_sampling_sensitivity" / "stage09d_sampling_sensitivity_by_boundary_class.csv")
    d["geom"] = read_csv(ROOT / "03_geometry_stratification" / "stage09d_geometry_bin_metrics.csv")
    d["geom_enrich"] = read_csv(ROOT / "03_geometry_stratification" / "stage09d_geometry_mismatch_enrichment.csv")
    d["scene"] = read_csv(ROOT / "04_boundary_broken_cloud" / "stage09d_scene_metrics_by_sample.csv")
    d["scene_class"] = read_csv(ROOT / "04_boundary_broken_cloud" / "stage09d_scene_metrics_by_class.csv")
    d["atlas"] = read_csv(ROOT / "05_error_atlas" / "stage09d_error_case_inventory.csv")
    d["atlas_pixel"] = read_csv(ROOT / "05_error_atlas" / "stage09d_error_type_pixel_summary.csv")
    d["factor"] = read_csv(ROOT / "06_integrated_factor_summary" / "stage09d_factor_contribution_summary.csv")
    return d


def build_core(d: dict[str, pd.DataFrame], plot_index: list[dict[str, str]]) -> dict[str, pd.DataFrame]:
    sample = num(d["sample09c"].copy(), ["A_agreement", "B_agreement", "C_agreement", "B_minus_A_agreement", "A_cloud_fraction_bias"])
    sample["group_cn"] = sample["candidate_group"].map(GROUP_LABEL).fillna(sample["candidate_group"])
    overall = pd.DataFrame([{
        "样本数": sample["sample_id"].nunique(),
        "A均值": sample["A_agreement"].mean(),
        "A范围": f"{fmt(sample['A_agreement'].min())}-{fmt(sample['A_agreement'].max())}",
        "B均值": sample["B_agreement"].mean(),
        "B范围": f"{fmt(sample['B_agreement'].min())}-{fmt(sample['B_agreement'].max())}",
        "C均值": sample["C_agreement"].mean(),
        "C范围": f"{fmt(sample['C_agreement'].min())}-{fmt(sample['C_agreement'].max())}",
        "B-A均值": sample["B_minus_A_agreement"].mean(),
        "解释": "A/B/C 分别代表包容二值、高置信二值、三类不确定性策略；EPIC 是独立诊断参照，不是绝对真值。",
    }])
    overall_csv = write_csv(overall, "core_overall_agreement_summary.csv")
    group = sample.groupby(["candidate_group", "group_cn"], dropna=False).agg(
        样本数=("sample_id", "nunique"),
        A均值=("A_agreement", "mean"),
        B均值=("B_agreement", "mean"),
        C均值=("C_agreement", "mean"),
        B_A_lift=("B_minus_A_agreement", "mean"),
        A_cloud_fraction_bias_mean=("A_cloud_fraction_bias", "mean"),
    ).reset_index().sort_values("A均值", ascending=False)
    group_csv = write_csv(group, "core_group_agreement_summary.csv")
    factor = d["factor"].copy()
    factor["evidence_score"] = factor["evidence_level"].map(EVIDENCE_SCORE).fillna(-1)
    factor["中文解释"] = factor.apply(lambda r: factor_cn(r), axis=1)
    factor = factor.sort_values(["evidence_score", "factor"], ascending=[False, True])
    factor_csv = write_csv(factor, "core_factor_evidence_summary.csv")

    grouped_bar(FIG / "core_group_mean_ABC_agreement.png", group, "group_cn", ["A均值", "B均值", "C均值"], "分组 A/B/C agreement 均值", "agreement", group_csv, plot_index, (0, 1))
    bar_plot(FIG / "core_B_minus_A_lift_by_group.png", group["group_cn"].tolist(), group["B_A_lift"].tolist(), "B-A lift by group：低置信类别处理的影响", "B-A agreement lift", group_csv, plot_index, "#2a9d8f")
    bar_plot(FIG / "core_factor_evidence_summary.png", factor["factor"].tolist(), factor["evidence_score"].tolist(), "Stage 09D factor evidence strength", "strong=4, moderate=3, weak=2, insufficient=0", factor_csv, plot_index, "#7a5195", (0, 4.5))
    return {"overall": overall, "group": group, "factor": factor, "sample": sample}


def factor_cn(r: pd.Series) -> str:
    f = str(r.get("factor", ""))
    lvl = str(r.get("evidence_level", ""))
    if f == "source_scene_family":
        return "分组差异显著，是一阶主因候选；尤其 Meteosat-dominant 低于 GOES/East Asia。"
    if f == "cloud_boundary":
        return "边界像元 mismatch 富集，是重要场景因子，但不是单独产品错误证据。"
    if f == "epic_view_geometry":
        return "EPIC 视角几何存在 mismatch 富集，属于观测几何/采样共同差异。"
    if f == "nearest_neighbor_sampling":
        return "窗口采样增益用于判断最近邻代表性误差；若 lift 小，则不能把主因归给最近邻。"
    if "meteosat" in f:
        return "Meteosat 相关场景 agreement 偏低，但当前不能归因于单一机制，需语义映射审计。"
    if lvl == "insufficient_data":
        return "当前变量覆盖或诊断设计不足，只能作为待验证因素。"
    return "作为辅助解释因子纳入综合证据链。"


def build_source(d: dict[str, pd.DataFrame], plot_index: list[dict[str, str]]) -> dict[str, pd.DataFrame]:
    src = num(d["source"].copy(), ["n_valid", "agreement", "f1_cloud", "iou_cloud", "cloud_fraction_bias"])
    src_valid = src[src["n_valid"] >= 50000].copy()
    agg_rows = []
    for (policy, source), g in src_valid.groupby(["policy", "source_name"]):
        agg_rows.append({
            "policy": policy,
            "policy_cn": POLICY_LABEL.get(policy, policy),
            "source_name": source,
            "sample_rows": len(g),
            "sample_count": g["sample_id"].nunique(),
            "n_valid_total": g["n_valid"].sum(),
            "agreement_weighted": weighted_mean(g, "agreement"),
            "agreement_mean": g["agreement"].mean(),
            "f1_weighted": weighted_mean(g, "f1_cloud"),
            "iou_weighted": weighted_mean(g, "iou_cloud"),
            "cloud_fraction_bias_mean": g["cloud_fraction_bias"].mean(),
            "low_n_rows_excluded": int(((src["policy"] == policy) & (src["source_name"] == source) & (src["n_valid"] < 50000)).sum()),
            "解释": "按 n_valid>=50000 的行做稳健汇总；单源在非主服务区的行需谨慎解释。",
        })
    src_agg = pd.DataFrame(agg_rows).sort_values(["policy", "agreement_weighted"], ascending=[True, False])
    src_agg_csv = write_csv(src_agg, "source_by_source_interpretation_summary.csv")

    pivot_ag = src_agg.pivot(index="source_name", columns="policy_cn", values="agreement_weighted")
    heatmap(FIG / "source_by_source_agreement_heatmap.png", pivot_ag, "source-by-source agreement（n_valid 加权）", src_agg_csv, plot_index, "viridis", 0, 1)
    a_bias = src_agg[src_agg["policy"] == "A_inclusive_binary"].sort_values("cloud_fraction_bias_mean")
    bar_plot(FIG / "source_cloud_fraction_bias_policyA.png", a_bias["source_name"].tolist(), a_bias["cloud_fraction_bias_mean"].tolist(), "Policy A source cloud fraction bias", "GEO-EPIC cloud fraction", src_agg_csv, plot_index, "#e76f51")

    a = src_agg[src_agg["policy"] == "A_inclusive_binary"][["source_name", "agreement_weighted"]].rename(columns={"agreement_weighted": "A_agreement_weighted"})
    b = src_agg[src_agg["policy"] == "B_high_confidence_only"][["source_name", "agreement_weighted"]].rename(columns={"agreement_weighted": "B_agreement_weighted"})
    lift = a.merge(b, on="source_name", how="outer")
    lift["B_minus_A_by_source"] = lift["B_agreement_weighted"] - lift["A_agreement_weighted"]
    lift["解释"] = np.where(lift["B_minus_A_by_source"] > 0.02, "Policy B 明显改善，说明低置信/不确定类别语义会影响一致性。", "Policy B 改善有限，语义低置信不是该 source 的单独主因。")
    lift_csv = write_csv(lift.sort_values("B_minus_A_by_source", ascending=False), "source_policy_B_minus_A_lift_summary.csv")

    pair_summary = num(d["pair_summary"].copy(), ["sample_count", "A_agreement_mean", "B_agreement_mean", "B_minus_A_mean", "source_disagreement_fraction_mean", "both_wrong_fraction_mean"])
    pair_a = pair_summary[pair_summary["policy"] == "A_inclusive_binary"].copy()
    pair_a["pair"] = pair_a["source_A"] + " vs " + pair_a["source_B"]
    pair_a["解释"] = pair_a.apply(lambda r: pair_explain(r), axis=1)
    pair_csv = write_csv(pair_a.sort_values("source_disagreement_fraction_mean", ascending=False), "source_pair_policyA_interpretation_summary.csv")

    sources = sorted(set(pair_a["source_A"]).union(set(pair_a["source_B"])))
    mat = pd.DataFrame(np.nan, index=sources, columns=sources)
    dir_mat = pd.DataFrame(np.nan, index=sources, columns=sources)
    for _, r in pair_a.iterrows():
        a0, b0 = r["source_A"], r["source_B"]
        mat.loc[a0, b0] = mat.loc[b0, a0] = r["source_disagreement_fraction_mean"]
        dir_mat.loc[a0, b0] = r["B_minus_A_mean"]
        dir_mat.loc[b0, a0] = -r["B_minus_A_mean"]
    heatmap(FIG / "source_pair_disagreement_heatmap.png", mat, "source-pair disagreement fraction（Policy A）", pair_csv, plot_index, "magma", 0, 0.6)
    heatmap(FIG / "source_pair_B_minus_A_direction_heatmap.png", dir_mat, "pair direction: source_B - source_A agreement", pair_csv, plot_index, "coolwarm", -0.12, 0.12)

    cases = num(d["pair_cases"].copy(), ["source_disagreement_fraction", "B_minus_A_agreement", "n_overlap_valid"])
    cases["pair"] = cases["source_A"] + " vs " + cases["source_B"]
    cases_top = cases.sort_values("source_disagreement_fraction", ascending=False).head(30)
    cases_csv = write_csv(cases_top, "source_pair_high_disagreement_cases_top30.csv")
    bar_plot(FIG / "source_pair_high_disagreement_cases_top30.png", (cases_top["sample_id"] + "\n" + cases_top["pair"]).tolist(), cases_top["source_disagreement_fraction"].tolist(), "High-disagreement source-pair cases", "disagreement fraction", cases_csv, plot_index, "#c43b3b", (0, 1))
    return {"src_agg": src_agg, "lift": lift, "pair_a": pair_a, "cases_top": cases_top}


def pair_explain(r: pd.Series) -> str:
    disag = float(r.get("source_disagreement_fraction_mean", math.nan))
    ba = float(r.get("B_minus_A_mean", math.nan))
    both_wrong = float(r.get("both_wrong_fraction_mean", math.nan))
    if disag >= 0.4 and abs(ba) < 0.03:
        return "两源互相差异大，但没有稳定一方明显更接近 EPIC；更像语义/区域/场景共同差异。"
    if abs(ba) >= 0.05:
        better = str(r.get("source_B")) if ba > 0 else str(r.get("source_A"))
        return f"{better} 在重叠像元上更接近 EPIC 诊断参照；需结合 n_overlap 和服务区解释。"
    if both_wrong >= 0.25:
        return "两者同时偏离 EPIC 的比例较高，source-pair 本身不能单独解释 mismatch。"
    return "pair 差异存在，但主结论应基于 summary 而非单个 case。"


def build_sampling(d: dict[str, pd.DataFrame], plot_index: list[dict[str, str]]) -> dict[str, pd.DataFrame]:
    samp = num(d["sampling"].copy(), ["n_valid", "agreement", "delta_agreement_vs_nearest", "delta_f1_vs_nearest"])
    a = samp[samp["policy"] == "A_inclusive_binary"].copy()
    method = a.groupby("sampling_method").agg(
        sample_count=("sample_id", "nunique"),
        agreement_mean=("agreement", "mean"),
        agreement_min=("agreement", "min"),
        agreement_max=("agreement", "max"),
        delta_mean=("delta_agreement_vs_nearest", "mean"),
        delta_min=("delta_agreement_vs_nearest", "min"),
        delta_max=("delta_agreement_vs_nearest", "max"),
    ).reset_index()
    method["解释"] = method["delta_mean"].apply(lambda x: "相对最近邻有可见改善，代表性误差需纳入解释。" if x > 0.02 else "相对最近邻改善有限，不能把 mismatch 主因归给采样方法。")
    method_csv = write_csv(method.sort_values("agreement_mean", ascending=False), "sampling_method_policyA_summary.csv")
    bar_plot(FIG / "sampling_method_agreement_comparison.png", method["sampling_method"].tolist(), method["agreement_mean"].tolist(), "Policy A sampling method agreement comparison", "agreement", method_csv, plot_index, "#386fa4", (0, 1))

    sg = num(d["sampling_group"].copy(), ["agreement_mean", "delta_agreement_vs_nearest_mean", "f1_mean"])
    sg_a = sg[(sg["policy"] == "A_inclusive_binary") & (sg["sampling_method"] != "nearest")].copy()
    sg_a["group_cn"] = sg_a["candidate_group"].map(GROUP_LABEL).fillna(sg_a["candidate_group"])
    sg_csv = write_csv(sg_a, "sampling_delta_by_group_policyA.csv")
    top = sg_a.sort_values("delta_agreement_vs_nearest_mean", ascending=False).head(36)
    bar_plot(FIG / "sampling_delta_agreement_by_group.png", (top["sampling_method"] + "\n" + top["group_cn"]).tolist(), top["delta_agreement_vs_nearest_mean"].tolist(), "delta agreement vs nearest by group", "delta agreement", sg_csv, plot_index, "#2a9d8f")

    sb = num(d["sampling_boundary"].copy(), ["agreement_mean", "delta_agreement_vs_nearest_mean", "f1_mean"])
    sb_a = sb[(sb["policy"] == "A_inclusive_binary") & (sb["sampling_method"] != "nearest")].copy()
    sb_csv = write_csv(sb_a, "sampling_delta_by_boundary_class_policyA.csv")
    bar_plot(FIG / "sampling_effect_by_boundary_class.png", (sb_a["sampling_method"] + "\n" + sb_a["boundary_class"]).tolist(), sb_a["delta_agreement_vs_nearest_mean"].tolist(), "sampling effect by boundary class", "delta agreement", sb_csv, plot_index, "#8ab17d")
    return {"method": method, "group": sg_a, "boundary": sb_a}


def build_geometry(d: dict[str, pd.DataFrame], plot_index: list[dict[str, str]]) -> dict[str, pd.DataFrame]:
    geom = num(d["geom"].copy(), ["n_valid", "agreement", "mismatch_rate", "enrichment", "pixel_fraction", "mismatch_fraction"])
    ge = num(d["geom_enrich"].copy(), ["sample_count", "agreement_mean", "mismatch_rate_mean", "enrichment_mean"])
    ge_a = ge[ge["policy"] == "A_inclusive_binary"].copy()
    ge_a["解释"] = ge_a.apply(lambda r: geom_explain(r), axis=1)
    ge_csv = write_csv(ge_a.sort_values("enrichment_mean", ascending=False), "geometry_policyA_enrichment_summary.csv")

    for dim, fname, title in [
        ("abs_lat_bin", "geometry_latitude_bin_agreement.png", "latitude bin agreement"),
        ("epic_view_zenith_bin", "geometry_epic_vza_bin_agreement.png", "EPIC VZA bin agreement"),
    ]:
        sub = ge_a[ge_a["dimension"] == dim].copy()
        if not sub.empty:
            order = sub.sort_values("bin")
            bar_plot(FIG / fname, order["bin"].tolist(), order["agreement_mean"].tolist(), title, "agreement", ge_csv, plot_index, "#386fa4", (0, 1))
    mat = ge_a.pivot(index="dimension", columns="bin", values="enrichment_mean")
    heatmap(FIG / "geometry_mismatch_enrichment_heatmap.png", mat, "geometry mismatch enrichment（Policy A）", ge_csv, plot_index, "magma", 0, max(2.5, np.nanmax(mat.to_numpy()) if mat.size else 2.5))

    source_geom = geom[(geom["policy"] == "A_inclusive_binary") & geom["dominant_source"].notna()].copy()
    sg = source_geom.groupby(["dominant_source", "dimension"]).agg(
        agreement_mean=("agreement", "mean"),
        enrichment_mean=("enrichment", "mean"),
        row_count=("sample_id", "count"),
    ).reset_index()
    sg_csv = write_csv(sg, "selected_source_by_geometry_summary.csv")
    if not sg.empty:
        mat2 = sg.pivot(index="dominant_source", columns="dimension", values="enrichment_mean")
        heatmap(FIG / "selected_source_by_geometry_enrichment_heatmap.png", mat2, "dominant source x geometry enrichment", sg_csv, plot_index, "viridis")
    return {"geom_enrich": ge_a, "source_geom": sg}


def geom_explain(r: pd.Series) -> str:
    e = float(r.get("enrichment_mean", math.nan))
    dim = str(r.get("dimension", ""))
    if e >= 1.5:
        return f"{dim}={r.get('bin')} 的 mismatch 富集达到 >=1.5，应作为几何/覆盖条件重点解释。"
    return "未达到强富集阈值，更多作为背景控制因子。"


def build_boundary_atlas(d: dict[str, pd.DataFrame], plot_index: list[dict[str, str]]) -> dict[str, pd.DataFrame]:
    scene = num(d["scene_class"].copy(), ["sample_count", "agreement_mean", "mismatch_rate_mean", "mismatch_enrichment_mean"])
    scene_a = scene[scene["policy"] == "A_inclusive_binary"].copy()
    scene_a["解释"] = scene_a.apply(lambda r: scene_explain(r), axis=1)
    scene_csv = write_csv(scene_a, "boundary_broken_cloud_policyA_summary.csv")
    bar_plot(FIG / "boundary_vs_nonboundary_mismatch_rate.png", (scene_a["scene_type"] + "\n" + scene_a["boundary_class"]).tolist(), scene_a["mismatch_rate_mean"].tolist(), "boundary / non-boundary mismatch rate", "mismatch rate", scene_csv, plot_index, "#c43b3b")
    bar_plot(FIG / "broken_cloud_mismatch_enrichment.png", (scene_a["scene_type"] + "\n" + scene_a["boundary_class"]).tolist(), scene_a["mismatch_enrichment_mean"].tolist(), "broken cloud / boundary mismatch enrichment", "enrichment", scene_csv, plot_index, "#7a5195")

    atlas = d["atlas"].copy()
    atlas["中文类型解释"] = atlas["case_type"].apply(case_type_cn)
    atlas_csv = write_csv(atlas, "error_atlas_case_interpretation_index.csv")
    if not atlas.empty:
        bar_plot(FIG / "error_atlas_case_agreement_overview.png", (atlas["case_type"] + "\n" + atlas["sample_id"]).tolist(), pd.to_numeric(atlas["A_agreement"], errors="coerce").tolist(), "error atlas cases: Policy A agreement", "A agreement", atlas_csv, plot_index, "#386fa4", (0, 1))
        make_montage(atlas, atlas_csv, plot_index)
    return {"scene": scene_a, "atlas": atlas}


def scene_explain(r: pd.Series) -> str:
    e = float(r.get("mismatch_enrichment_mean", math.nan))
    st = str(r.get("scene_type"))
    bc = str(r.get("boundary_class"))
    if e >= 2:
        return f"{st}/{bc} mismatch 明显富集，是 strong 场景因子。"
    if e >= 1.5:
        return f"{st}/{bc} mismatch 中等富集，是 moderate 场景因子。"
    return "富集有限，不能单独解释整体低一致性。"


def case_type_cn(case_type: str) -> str:
    if "best_agreement" in case_type:
        return "高一致性对照样本，用于观察系统能达成一致的场景。"
    if "worst_agreement" in case_type:
        return "最低一致性样本，是综合失配模式代表。"
    if "B_minus_A" in case_type:
        return "高置信策略提升最大的样本，提示低置信/不确定类别语义敏感。"
    if "cloud_fraction_bias" in case_type:
        return "云量偏差最大的样本，提示云/晴语义或空间代表性差异。"
    if "group" in case_type:
        return "按场景家族选取的低一致性代表。"
    if "source" in case_type:
        return "按 dominant source 选取的低一致性代表。"
    return "代表性诊断样本。"


def make_montage(atlas: pd.DataFrame, source_csv: Path, plot_index: list[dict[str, str]]) -> None:
    panels = []
    labels = []
    for _, r in atlas.head(6).iterrows():
        p = Path(str(r.get("case_panel", "")))
        if p.exists():
            panels.append(plt.imread(p))
            labels.append(f"{r.get('sample_id')} | {r.get('case_type')}")
    if not panels:
        return
    fig, axes = plt.subplots(2, 3, figsize=(18, 10), constrained_layout=True)
    for ax, img, label in zip(axes.ravel(), panels, labels):
        ax.imshow(img)
        ax.set_title(label, fontsize=9)
        ax.axis("off")
    for ax in axes.ravel()[len(panels):]:
        ax.axis("off")
    path = FIG / "error_atlas_representative_case_montage.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    add_plot(plot_index, path, source_csv, "representative error atlas montage")


def build_evidence_chain(core: dict[str, pd.DataFrame], source: dict[str, pd.DataFrame], sampling: dict[str, pd.DataFrame], geometry: dict[str, pd.DataFrame], boundary: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = [
        {
            "rank": 1,
            "factor": "source / scene family",
            "evidence strength": "strong",
            "supporting tables": "core_group_agreement_summary.csv; source_pair_policyA_interpretation_summary.csv",
            "interpretation": "Meteosat-dominant 与 GOES/East Asia 的 agreement 差距最大，说明 source family/场景家族是一阶主因。",
            "caveat": "不能写成某个源坏了；EPIC 不是真值，且 source-by-source 包含非主服务区像元。",
            "next action": "semantic mapping delta validation，尤其 Meteosat CLM 与其他源的 cloud mask code 语义。",
        },
        {
            "rank": 2,
            "factor": "cloud boundary / broken cloud",
            "evidence strength": "strong-to-moderate",
            "supporting tables": "boundary_broken_cloud_policyA_summary.csv",
            "interpretation": "边界/碎云像元 mismatch 富集，能解释部分 60%-80% 一致性区间。",
            "caveat": "边界富集不等价于融合逻辑错误，也可能来自 EPIC-GEO 分辨率与观测时间/几何差异。",
            "next action": "结合 error atlas 对高 mismatch 场景做人工审计。",
        },
        {
            "rank": 3,
            "factor": "EPIC view geometry / latitude",
            "evidence strength": "moderate-to-strong",
            "supporting tables": "geometry_policyA_enrichment_summary.csv",
            "interpretation": "若若干几何 bin enrichment >=1.5，说明观测几何与覆盖条件会放大 mismatch。",
            "caveat": "GEO VZA / solar geometry 覆盖不足，不能强行得出单变量因果。",
            "next action": "保留几何分层作为后续语义审计的控制变量。",
        },
        {
            "rank": 4,
            "factor": "semantic low-confidence classes",
            "evidence strength": "moderate",
            "supporting tables": "core_group_agreement_summary.csv; source_policy_B_minus_A_lift_summary.csv",
            "interpretation": "B-A lift 为正，说明低置信/不确定类别处理会改善一致性，但 lift 大小不足以单独解释全部 mismatch。",
            "caveat": "Policy B 也改变了有效样本语义，不能直接视为更真实。",
            "next action": "做 raw-code / semantic mapping delta validation。",
        },
        {
            "rank": 5,
            "factor": "nearest-neighbor sampling",
            "evidence strength": "weak-to-moderate",
            "supporting tables": "sampling_method_policyA_summary.csv; sampling_delta_by_group_policyA.csv",
            "interpretation": "窗口采样 delta 用来估计空间代表性误差；若平均改善有限，最近邻不是主因。",
            "caveat": "窗口采样是诊断敏感性，不是 fusion v2，也不能替代正式产品逻辑。",
            "next action": "作为误差条而非改产品建议使用。",
        },
        {
            "rank": 6,
            "factor": "unresolved product semantics / raw code mapping",
            "evidence strength": "insufficient direct evidence but high priority",
            "supporting tables": "core_factor_evidence_summary.csv; error_atlas_case_interpretation_index.csv",
            "interpretation": "现有证据指向产品语义差异，但还缺 raw code 级别闭环验证。",
            "caveat": "不能把 Meteosat low agreement 归因于单一机制。",
            "next action": "优先做 semantic mapping delta validation，而不是继续扩样本或 fusion v2。",
        },
    ]
    df = pd.DataFrame(rows)
    write_csv(df, "integrated_evidence_chain_cn.csv")
    return df


def write_reports(core: dict[str, pd.DataFrame], source: dict[str, pd.DataFrame], sampling: dict[str, pd.DataFrame], geometry: dict[str, pd.DataFrame], boundary: dict[str, pd.DataFrame], evidence: pd.DataFrame) -> None:
    overall = core["overall"].iloc[0]
    group = core["group"]
    factor = core["factor"]
    src_agg = source["src_agg"]
    pair_a = source["pair_a"].sort_values("source_disagreement_fraction_mean", ascending=False)
    method = sampling["method"].sort_values("delta_mean", ascending=False)
    geom_top = geometry["geom_enrich"].sort_values("enrichment_mean", ascending=False)
    scene = boundary["scene"].sort_values("mismatch_enrichment_mean", ascending=False)
    atlas = boundary["atlas"]

    (OUT / "01_core_metrics_and_factor_interpretation_cn.md").write_text(f"""# 01 核心指标与因子解释

生成时间：`{utc_now()}`

## 总体解释框架

本解释包只读取 Stage 09C/09D 既有输出，不新增样本、不重跑 Stage 05/06/09D、不修改 fused cloud mask 生产逻辑。EPIC 在这里是 independent diagnostic reference，不是绝对真值。

53 个样本的总体 agreement 水平为：Policy A `{fmt(overall['A均值'])}`（范围 {overall['A范围']}），Policy B `{fmt(overall['B均值'])}`（范围 {overall['B范围']}），Policy C `{fmt(overall['C均值'])}`（范围 {overall['C范围']}）。这说明 60%-80% 的一致性不是单一异常，而是不同语义、几何、采样和场景条件叠加后的诊断结果。

## 分组差异

{md_table(group, cols=['group_cn','样本数','A均值','B均值','C均值','B_A_lift','A_cloud_fraction_bias_mean'])}

Meteosat-dominant 与 GOES/East Asia 的差异是当前最稳定的一阶信号。Mixed/boundary 处于中间状态，说明边界和多源重叠会改变一致性，但它不是唯一因素。

## B-A lift 的含义

B-A lift 反映高置信二值语义相对包容二值语义的变化。若 lift 为正，说明低置信/不确定类别确实影响 agreement；但如果 lift 只有几个百分点，它更像二阶修饰因子，而不是足以解释全部 60%-80% 一致性的主因。

## 因子证据等级

{md_table(factor, n=30, cols=['factor','evidence_level','observed_value','中文解释','source_csv'])}

一阶主因：source / scene family，尤其 Meteosat 相关场景的系统性低 agreement。二阶修饰因子：boundary/broken cloud、EPIC view geometry、低置信类别语义、nearest-neighbor sampling。待确认因子：GEO VZA、solar geometry、unresolved mismatch。
""", encoding="utf-8")

    (OUT / "02_source_and_source_pair_interpretation_cn.md").write_text(f"""# 02 Source 与 Source-pair 解释

## Source-by-source

{md_table(src_agg.sort_values(['policy','agreement_weighted'], ascending=[True, False]), n=40, cols=['policy_cn','source_name','sample_count','n_valid_total','agreement_weighted','f1_weighted','iou_weighted','cloud_fraction_bias_mean','low_n_rows_excluded','解释'])}

source-by-source 结果不能直接解释为各 source 在主服务区的真实性能，因为部分行来自非主服务区或重叠边界。n_valid 很小的行已经不作为主汇总依据。

## Policy B 对 source 的改善

{md_table(source['lift'].sort_values('B_minus_A_by_source', ascending=False), n=20)}

Policy B 的改善若集中在某些 source，说明低置信/不确定类别语义对这些 source 更敏感；若改善有限，则语义低置信类别不是该 source 的单独主因。

## Source-pair 机制

{md_table(pair_a, n=30, cols=['pair','sample_count','A_agreement_mean','B_agreement_mean','B_minus_A_mean','source_disagreement_fraction_mean','both_wrong_fraction_mean','解释'])}

最大 source-pair disagreement 仍集中在涉及 Meteosat-IODC、Meteosat-0deg 的组合，其次是 FY4B vs Himawari。GOES-16 vs GOES-18 的 disagreement 相对较低，可作为相对稳定的内部参照，但仍不是真值参照。

Meteosat-0deg 与 Meteosat-IODC 尚不能简单分出绝对优劣；需要看 pair-level 的方向和 both-wrong fraction。若两者 disagreement 高但 B_minus_A 接近零，说明问题不一定是某一方明显更差，而可能是产品语义、区域几何和 EPIC-GEO 观测差异共同作用。

## 高 disagreement cases

{md_table(source['cases_top'], n=30)}

这些 cases 更适合作为后续 raw-code / semantic mapping delta validation 的抽样入口，而不是直接作为 source 排名证据。
""", encoding="utf-8")

    (OUT / "03_sampling_sensitivity_interpretation_cn.md").write_text(f"""# 03 Sampling Sensitivity 解释

## 方法对比

{md_table(method, n=30)}

window sampling 的主要问题不是“是否比 nearest 更真实”，而是估计 EPIC-GEO 空间代表性误差的量级。若 delta_agreement_vs_nearest 平均值有限，则最近邻采样不是 60%-80% agreement 的主因；若某些 group 或 boundary class delta 明显，则这些场景需要把空间代表性误差作为解释项。

## 按 group 的 delta

{md_table(sampling['group'].sort_values('delta_agreement_vs_nearest_mean', ascending=False), n=40)}

## 按 boundary class 的 delta

{md_table(sampling['boundary'].sort_values('delta_agreement_vs_nearest_mean', ascending=False), n=30)}

结论：sampling 是诊断敏感性，不是 fusion v2。它只能帮助解释 representativeness error，不能被用来生成新的 fused product。
""", encoding="utf-8")

    (OUT / "04_geometry_interpretation_cn.md").write_text(f"""# 04 Geometry 解释

## mismatch enrichment >= 1.5 的几何条件

{md_table(geom_top[geom_top['enrichment_mean'] >= 1.5], n=40, cols=['dimension','bin','sample_count','agreement_mean','mismatch_rate_mean','enrichment_mean','解释'])}

## 全部几何分层

{md_table(geom_top, n=80, cols=['dimension','bin','sample_count','agreement_mean','mismatch_rate_mean','enrichment_mean','解释'])}

EPIC view geometry、latitude 和 valid_source_count 都是解释 mismatch 的控制变量。valid_source_count 高不必然意味着更好，它也可能代表多源重叠、边界或复杂区域。GEO VZA / solar geometry 因变量覆盖不足，当前不能强行作为主因。

Meteosat low agreement 不能仅由 geometry 解释；geometry 更像放大器或分层条件，核心仍需回到 product semantics 与 raw code mapping。
""", encoding="utf-8")

    (OUT / "05_boundary_broken_cloud_error_atlas_interpretation_cn.md").write_text(f"""# 05 Boundary / Broken Cloud / Error Atlas 解释

## Boundary 与 broken cloud

{md_table(scene, n=30)}

boundary 和 broken cloud 的 mismatch enrichment 若达到 1.5 或 2.0，说明云边界/碎云是重要场景因子。它可以解释一部分 source-pair disagreement：不同 source 在云边界、薄云、碎云区域的离散分类语义更容易分歧。

但若 homogeneous clear/cloud 区域中 Meteosat 仍偏低，则边界不能解释全部问题，仍需 product semantics 审计。

## Error atlas cases

{md_table(atlas, n=20, cols=['case_type','sample_id','A_agreement','B_minus_A','中文类型解释','case_panel','source_csv'])}

10 个 indexed cases 覆盖了高一致性对照、最低一致性、B-A lift 最大、云量偏差最大、不同 group/source 的低一致性代表。它们的用途是形成 failure pattern 线索，而不是替代统计汇总。
""", encoding="utf-8")

    (OUT / "06_integrated_synthesis_cn.md").write_text(f"""# 06 Stage 09D 综合解释报告

## 核心结论

当前 GEO-ring fused / prefusion cloud mask 与 EPIC 的 60%-80% 一致性，应解释为多机制共同作用下的诊断一致性水平，而不是某个 source 或 fusion 逻辑“坏了”。EPIC 是独立诊断参照，不是绝对真值；因此所有结论都应写成“相对 EPIC 的一致性/偏离”，不能写成“真实错误率”。

一阶主因是 source / scene family 差异，尤其 Meteosat 相关场景相对 EPIC 的一致性显著偏低。最合理解释不是单一 source 失败，而是 Meteosat 产品语义、区域几何、云边界/碎云、EPIC-GEO 观测差异共同构成当前 mismatch。

二阶修饰因素包括：低置信/不确定类别语义、nearest-neighbor sampling 代表性误差、boundary/broken cloud 场景富集、EPIC view geometry 与 latitude/source-count 条件。这些因素会放大或减弱 agreement，但当前证据不足以把任何一个二阶因素单独列为主因。

## 必答问题

1. 60%-80% 一致性是独立参照下的多因素诊断结果，不是绝对精度。
2. 一阶主因：source/scene family，尤其 Meteosat-dominant 场景。
3. 二阶修饰因子：boundary/broken cloud、EPIC view geometry、低置信类别语义、sampling representativeness。
4. 强证据：分组差异、边界/碎云富集、若干几何 bin 富集、source-pair disagreement。
5. 证据不足：GEO VZA、solar geometry、raw code 级语义差异的直接闭环。
6. Meteosat low agreement 最合理解释：产品语义 + 区域几何 + 云边界/碎云 + EPIC-GEO 观测差异共同作用。
7. FY4B/Himawari 仍需关注，但当前最强矛盾已转向 Meteosat 相关 pair 与场景家族。
8. 低置信类别不是唯一主因；B-A lift 说明它重要，但更像二阶语义修饰。
9. 最近邻采样不是主因；window sampling 是敏感性诊断，不是新融合方案。
10. 云边界/碎云是主因之一或强二阶因素，尤其用于解释局部 mismatch 富集。
11. EPIC view geometry 是重要几何修饰因子，但不能单独解释 Meteosat low agreement。
12. 后续应优先 semantic mapping delta validation，而不是继续扩样本或提出 fusion v2。
13. 可放进汇报的结论：分组差异、source-pair disagreement、boundary/geometry enrichment、B-A lift 有限；内部诊断保留项：具体 raw code 映射怀疑、单 case failure pattern。

## 证据链表

{md_table(evidence, n=20)}

## 建议表达

建议写成：Meteosat 相关场景中 GEO-ring/prefusion cloud mask 与 EPIC 的一致性显著偏低，但其机制可能由产品语义、区域几何、云边界/碎云和 EPIC-GEO 观测差异共同构成；当前证据不能将其归因于单一机制。
""", encoding="utf-8")


def write_workbook(tables: dict[str, pd.DataFrame]) -> Path:
    path = OUT / "stage09d_interpretation_key_tables.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, df in tables.items():
            sheet = name[:31]
            df.to_excel(writer, sheet_name=sheet, index=False)
            ws = writer.book[sheet]
            ws.freeze_panes = "A2"
            for col in ws.columns:
                max_len = max(len(str(cell.value)) if cell.value is not None else 0 for cell in col[:80])
                ws.column_dimensions[col[0].column_letter].width = min(max(max_len + 2, 10), 45)
    return path


def copy_existing_plot_index() -> None:
    old = ROOT / "07_summary_figures" / "stage09d_plot_index.csv"
    if old.exists():
        shutil.copy2(old, OUT / "stage09d_original_plot_index_reference.csv")


def main() -> None:
    setup_dirs()
    set_font()
    plot_index: list[dict[str, str]] = []
    d = load_inputs()
    core = build_core(d, plot_index)
    source = build_source(d, plot_index)
    sampling = build_sampling(d, plot_index)
    geometry = build_geometry(d, plot_index)
    boundary = build_boundary_atlas(d, plot_index)
    evidence = build_evidence_chain(core, source, sampling, geometry, boundary)
    write_reports(core, source, sampling, geometry, boundary, evidence)
    write_workbook({
        "core_overall": core["overall"],
        "core_group": core["group"],
        "factor_evidence": core["factor"],
        "source_summary": source["src_agg"],
        "source_BA_lift": source["lift"],
        "pair_summary": source["pair_a"],
        "high_pair_cases": source["cases_top"],
        "sampling_method": sampling["method"],
        "sampling_group": sampling["group"],
        "sampling_boundary": sampling["boundary"],
        "geometry_enrichment": geometry["geom_enrich"],
        "source_geometry": geometry["source_geom"],
        "boundary_scene": boundary["scene"],
        "error_atlas": boundary["atlas"],
        "evidence_chain": evidence,
    })
    pd.DataFrame(plot_index).to_csv(OUT / "stage09d_interpretation_plot_index.csv", index=False, encoding="utf-8-sig")
    copy_existing_plot_index()
    print(f"Interpretation package complete: {OUT}")


if __name__ == "__main__":
    main()
