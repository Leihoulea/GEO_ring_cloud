from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


ROOT = Path(r"D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs\stage09d_full_pixel_diagnostics_202403")
STAGE09C = Path(r"D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs\stage09c_scaled_202403_batch")
OUT = ROOT / "interpretation_package" / "visibility_filter_sensitivity"
FIG = OUT / "figures"


POLICY_CN = {
    "A_inclusive_binary": "A 包容二值",
    "B_high_confidence_only": "B 高置信",
    "C_uncertainty_aware_3class": "C 三类",
}

GROUP_CN = {
    "EAST_ASIA_FY4B_HIMAWARI_PRIORITY": "East Asia",
    "GOES_DOMINANT_CONTROL": "GOES-dominant",
    "METEOSAT_DOMINANT_CONTROL": "Meteosat-dominant",
    "MIXED_OR_BOUNDARY": "Mixed/boundary",
}


def fmt(x: float) -> str:
    return f"{x:.3f}"


def set_font() -> None:
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Arial Unicode MS", "DejaVu Sans"]


def md_table(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join(["---"] * len(cols)) + " |"]
    for _, r in df.iterrows():
        vals = []
        for c in cols:
            v = r[c]
            vals.append(f"{v:.3f}" if isinstance(v, float) else str(v))
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines)


def weighted(g: pd.DataFrame, value: str) -> float:
    n = g["n_valid"].sum()
    return float((g[value] * g["n_valid"]).sum() / n)


def add_plot(index: list[dict[str, str]], path: Path, source_csv: Path, desc: str) -> None:
    index.append({"plot_path": str(path), "source_csv": str(source_csv), "description_cn": desc})


def bar(path: Path, labels: list[str], values: list[float], title: str, ylabel: str, source_csv: Path, index: list[dict[str, str]], ylim=(0, 1)) -> None:
    fig, ax = plt.subplots(figsize=(max(8, len(labels) * 0.65), 4.8), constrained_layout=True)
    ax.bar(range(len(labels)), values, color="#386fa4")
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=30, ha="right")
    ax.set_ylim(*ylim)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(axis="y", alpha=0.25)
    fig.savefig(path, dpi=180)
    plt.close(fig)
    add_plot(index, path, source_csv, title)


def grouped(path: Path, df: pd.DataFrame, title: str, source_csv: Path, index: list[dict[str, str]]) -> None:
    labels = df["mask_cn"].tolist()
    x = range(len(labels))
    fig, ax = plt.subplots(figsize=(9, 5), constrained_layout=True)
    width = 0.24
    cols = ["A_agreement", "B_agreement", "C_agreement"]
    colors = ["#386fa4", "#2a9d8f", "#e76f51"]
    for i, col in enumerate(cols):
        ax.bar([v + (i - 1) * width for v in x], df[col], width=width, label=col.replace("_agreement", ""), color=colors[i])
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, rotation=20, ha="right")
    ax.set_ylabel("agreement")
    ax.set_ylim(0, 1)
    ax.set_title(title)
    ax.legend()
    ax.grid(axis="y", alpha=0.25)
    fig.savefig(path, dpi=180)
    plt.close(fig)
    add_plot(index, path, source_csv, title)


def main() -> None:
    set_font()
    OUT.mkdir(parents=True, exist_ok=True)
    FIG.mkdir(parents=True, exist_ok=True)
    plot_index: list[dict[str, str]] = []

    sample = pd.read_csv(STAGE09C / "02_expanded_diagnostics" / "stage09c_sample_level_semantic_summary.csv")
    geom = pd.read_csv(ROOT / "03_geometry_stratification" / "stage09d_geometry_bin_metrics.csv")
    geom = geom[geom["dimension"] == "valid_source_count_bin"].copy()
    geom["count_min"] = geom["bin"].map({"1": 1, "2": 2, "3": 3, ">=4": 4})
    for col in ["n_valid", "agreement", "mismatch_rate", "enrichment"]:
        geom[col] = pd.to_numeric(geom[col], errors="coerce")

    baseline = pd.DataFrame([{
        "口径": "Stage09C sample mean",
        "说明": "Stage09C 53 个样本的样本均值；A/C 有效像元几乎已是 GEO fused 有效区，B 还额外排除低置信类别。",
        "A_agreement": sample["A_agreement"].mean(),
        "B_agreement": sample["B_agreement"].mean(),
        "C_agreement": sample["C_agreement"].mean(),
        "A_valid_fraction_mean": sample["A_valid_fraction_of_epic_earth"].mean(),
        "B_valid_fraction_mean": sample["B_valid_fraction_of_epic_earth"].mean(),
        "C_valid_fraction_mean": sample["C_valid_fraction_of_epic_earth"].mean(),
    }])
    baseline_csv = OUT / "visibility_baseline_stage09c_sample_mean.csv"
    baseline.to_csv(baseline_csv, index=False, encoding="utf-8-sig")

    rows = []
    for thr in [1, 2, 3, 4]:
        row = {
            "mask": f"valid_source_count>={thr}",
            "mask_cn": f"GEO有效源数>={thr}",
        }
        for policy, col in [
            ("A_inclusive_binary", "A_agreement"),
            ("B_high_confidence_only", "B_agreement"),
            ("C_uncertainty_aware_3class", "C_agreement"),
        ]:
            base = geom[geom["policy"] == policy]
            g = base[base["count_min"] >= thr]
            n = g["n_valid"].sum()
            row[col] = weighted(g, "agreement")
            row[col.replace("agreement", "mismatch")] = weighted(g, "mismatch_rate")
            row[col.replace("agreement", "n_valid")] = int(n)
            row[col.replace("agreement", "pixel_share_within_visible")] = float(n / base[base["count_min"] >= 1]["n_valid"].sum())
        rows.append(row)
    summary = pd.DataFrame(rows)
    summary_csv = OUT / "geo_visible_threshold_agreement_summary.csv"
    summary.to_csv(summary_csv, index=False, encoding="utf-8-sig")

    by_count_rows = []
    for (policy, b), g in geom.groupby(["policy", "bin"]):
        by_count_rows.append({
            "policy": policy,
            "policy_cn": POLICY_CN.get(policy, policy),
            "valid_source_count_bin": b,
            "sample_count": g["sample_id"].nunique(),
            "n_valid": int(g["n_valid"].sum()),
            "agreement_weighted": weighted(g, "agreement"),
            "mismatch_weighted": weighted(g, "mismatch_rate"),
            "enrichment_mean": g["enrichment"].mean(),
        })
    by_count = pd.DataFrame(by_count_rows).sort_values(["policy", "valid_source_count_bin"])
    by_count_csv = OUT / "geo_visible_valid_source_count_bin_metrics.csv"
    by_count.to_csv(by_count_csv, index=False, encoding="utf-8-sig")

    group_rows = []
    for (policy, grp), g in geom.groupby(["policy", "candidate_group"]):
        group_rows.append({
            "policy": policy,
            "policy_cn": POLICY_CN.get(policy, policy),
            "candidate_group": grp,
            "group_cn": GROUP_CN.get(grp, grp),
            "n_valid": int(g["n_valid"].sum()),
            "agreement_weighted": weighted(g, "agreement"),
            "mismatch_weighted": weighted(g, "mismatch_rate"),
        })
    by_group = pd.DataFrame(group_rows).sort_values(["policy", "agreement_weighted"], ascending=[True, False])
    by_group_csv = OUT / "geo_visible_agreement_by_group.csv"
    by_group.to_csv(by_group_csv, index=False, encoding="utf-8-sig")

    grouped(FIG / "geo_visible_threshold_ABC_agreement.png", summary, "只在 GEO 有效覆盖区比较：A/B/C agreement", summary_csv, plot_index)
    a_count = by_count[by_count["policy"] == "A_inclusive_binary"]
    bar(FIG / "geo_visible_valid_source_count_policyA.png", a_count["valid_source_count_bin"].tolist(), a_count["agreement_weighted"].tolist(), "Policy A: agreement by valid source count", "agreement", by_count_csv, plot_index)
    a_group = by_group[by_group["policy"] == "A_inclusive_binary"]
    bar(FIG / "geo_visible_policyA_group_agreement.png", a_group["group_cn"].tolist(), a_group["agreement_weighted"].tolist(), "Policy A: GEO 有效覆盖区分组 agreement", "agreement", by_group_csv, plot_index)
    pd.DataFrame(plot_index).to_csv(OUT / "geo_visible_filter_plot_index.csv", index=False, encoding="utf-8-sig")

    a0 = float(baseline.loc[0, "A_agreement"])
    b0 = float(baseline.loc[0, "B_agreement"])
    c0 = float(baseline.loc[0, "C_agreement"])
    a1 = float(summary.loc[0, "A_agreement"])
    b1 = float(summary.loc[0, "B_agreement"])
    c1 = float(summary.loc[0, "C_agreement"])
    report = f"""# GEO visible filter sensitivity

## 结论

Stage 09D 的核心比较事实上已经基本限制在 GEO fused 有效覆盖区：Stage 09C 中 Policy A/C 的 `valid_fraction_of_epic_earth` 均值为 `{baseline.loc[0, 'A_valid_fraction_mean']:.3f}`，最小值也在 `{sample['A_valid_fraction_of_epic_earth'].min():.3f}` 以上。因此，单纯“排除静止卫星看不到的范围”不会把 agreement 从 60%-80% 大幅抬升到接近 90% 或更高。

按 `valid_source_count>=1` 的像元加权口径，agreement 为：

{md_table(summary[['mask_cn','A_agreement','B_agreement','C_agreement','A_pixel_share_within_visible','B_pixel_share_within_visible','C_pixel_share_within_visible']])}

与 Stage09C 样本均值相比：

- Policy A：`{a0:.3f}` -> GEO 有效像元加权 `{a1:.3f}`
- Policy B：`{b0:.3f}` -> GEO 有效像元加权 `{b1:.3f}`
- Policy C：`{c0:.3f}` -> GEO 有效像元加权 `{c1:.3f}`

这个变化幅度很小，说明当前 60%-80% 一致性主要不是由 GEO 完全不可见区造成的。

## 更保守的多源覆盖区

若进一步要求 `valid_source_count>=2`，Policy A/B/C 分别约为 `{summary.loc[1, 'A_agreement']:.3f}` / `{summary.loc[1, 'B_agreement']:.3f}` / `{summary.loc[1, 'C_agreement']:.3f}`。如果要求 `>=3`，则约为 `{summary.loc[2, 'A_agreement']:.3f}` / `{summary.loc[2, 'B_agreement']:.3f}` / `{summary.loc[2, 'C_agreement']:.3f}`。

注意这不是“越多源越好”的简单关系。`>=3` 或 `>=4` 往往代表多源重叠、边界或复杂区域，agreement 反而可能下降。因此 valid source count 应解释为覆盖/重叠复杂度指标，而不是质量单调指标。

## 按 valid source count 的分层

{md_table(by_count)}

## 按场景组的 GEO 有效区结果

{md_table(by_group)}

## 解释边界

这里的 GEO-visible 口径来自 Stage 09D 已有 `valid_source_count_map_cloud_mask` 分层结果，不新增实验、不重跑投影，也不改变 fused product。EPIC 仍是 independent diagnostic reference，不是绝对真值。
"""
    (OUT / "geo_visible_filter_sensitivity_report_cn.md").write_text(report, encoding="utf-8")
    print(OUT)


if __name__ == "__main__":
    main()
