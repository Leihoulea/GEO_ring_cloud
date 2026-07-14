from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

import sys

CODE_ROOT = Path(__file__).resolve().parents[1]
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from path_config import RUNS_ROOT  # noqa: E402


def fmt(value: Any) -> str:
    try:
        if pd.isna(value):
            return "NA"
        return f"{float(value):.3f}"
    except Exception:
        return str(value)


def first_row(df: pd.DataFrame, **filters: str) -> pd.Series | None:
    if df.empty:
        return None
    mask = pd.Series([True] * len(df))
    for key, value in filters.items():
        mask &= df[key].astype(str).eq(str(value))
    if not mask.any():
        return None
    return df.loc[mask].iloc[0]


def row_value(row: pd.Series | None, key: str, default: Any = "NA") -> Any:
    if row is None:
        return default
    return row.get(key, default)


def join_metric_rows(df: pd.DataFrame, label_col: str, value_cols: list[str]) -> str:
    if df.empty:
        return "无可用记录"
    parts: list[str] = []
    for row in df.itertuples(index=False):
        label = getattr(row, label_col)
        metrics = []
        for col in value_cols:
            metrics.append(f"{col} {fmt(getattr(row, col))}")
        parts.append(f"{label}: " + ", ".join(metrics))
    return "；".join(parts)


def build_report(output_dir: Path) -> str:
    domain = pd.read_csv(output_dir / "02_fused_cth_metrics" / "stage_10_fused_cth_metrics_by_domain.csv", encoding="utf-8-sig")
    selected = pd.read_csv(output_dir / "03_fused_cth_by_selected_source" / "stage_10_fused_cth_metrics_by_selected_source.csv", encoding="utf-8-sig")
    valid_count = pd.read_csv(output_dir / "03_fused_cth_by_selected_source" / "stage_10_fused_cth_metrics_by_valid_source_count.csv", encoding="utf-8-sig")
    prefusion = pd.read_csv(output_dir / "04_prefusion_source_cth" / "stage_10_prefusion_source_cth_metrics_by_source.csv", encoding="utf-8-sig")
    pair = pd.read_csv(output_dir / "05_source_pair_cth" / "stage_10_cth_source_pair_summary.csv", encoding="utf-8-sig")
    regret = pd.read_csv(output_dir / "06_selection_sensitivity_cth" / "stage_10_cth_selection_regret_summary.csv", encoding="utf-8-sig")
    groups = pd.read_csv(output_dir / "07_geometry_boundary_height" / "stage_10_cth_error_by_height_geometry_boundary.csv", encoding="utf-8-sig")
    summary = json.loads((output_dir / "stage_10_cth_summary.json").read_text(encoding="utf-8"))

    a_d0 = first_row(domain, policy="A_inclusive_binary", domain="D0_common_valid_cth")
    a_d1 = first_row(domain, policy="A_inclusive_binary", domain="D1_both_cloud")
    b_d1 = first_row(domain, policy="B_high_confidence_only", domain="D1_both_cloud")
    d5 = first_row(domain, policy="A_inclusive_binary", domain="D5_clean_core_cloud")
    d6 = first_row(domain, policy="A_inclusive_binary", domain="D6_boundary_or_broken_cloud")
    d7 = first_row(domain, policy="A_inclusive_binary", domain="D7_high_cloud")

    source_rows = selected[selected["policy"].eq("A_inclusive_binary")].copy().sort_values("mae_km")
    valid_rows = valid_count[valid_count["policy"].eq("A_inclusive_binary")].copy()
    pref_rows = prefusion[prefusion["policy"].eq("A_inclusive_binary")].copy().sort_values("mae_km")
    regret_rows = regret[regret["policy"].eq("A_inclusive_binary")].copy()
    height_rows = groups[(groups["policy"].eq("A_inclusive_binary")) & (groups["group_dimension"].eq("epic_cth_class"))].copy()
    fused_height_rows = groups[(groups["policy"].eq("A_inclusive_binary")) & (groups["group_dimension"].eq("fused_cth_class"))].copy()
    boundary_rows = groups[(groups["policy"].eq("A_inclusive_binary")) & (groups["group_dimension"].eq("boundary_class"))].copy()
    vza_rows = groups[(groups["policy"].eq("A_inclusive_binary")) & (groups["group_dimension"].eq("EPIC_VZA_bin"))].copy()
    pair_rows = pair[pair["policy"].eq("A_inclusive_binary")].copy().sort_values("B_minus_A_mae").head(6)

    valid_ge4_exists = valid_rows["valid_source_count_bin"].astype(str).eq("valid_source_count_ge4").any()
    valid_ge4_note = (
        "存在 `valid_source_count_ge4` 分层。"
        if valid_ge4_exists
        else "CTH 的 `valid_count_map_cloud_top_height_km` 在 D1 both-cloud 中只出现 1/2/3 类，没有 `valid_source_count>=4`；因此不能把 cloud-mask 阶段的 ge4 诊断口径直接搬到 CTH 有效源计数。"
    )

    lines = [
        "# Stage 10 GEO-ring fused CTH product validation and mechanism diagnostics",
        "",
        "## 阶段定位",
        "",
        "本阶段使用 `geo_ring_cloud.stage_10` 命名，性质为 Stage 06 fused CTH 的后验验证和机制诊断。运行只读取既有 2024-03 本地样本、Stage 06 fused product、Stage 09D 样本清单和 EPIC L2 Cloud 文件；未新增样本、未重跑 Stage 05/06、未修改 fusion 逻辑、未生成 fusion v2。EPIC 仅作为 independent diagnostic reference，不作为绝对真值。",
        "",
        "## 变量与单位审计",
        "",
        "GEO-ring 主变量为 `fused_cloud_top_height_km`，单位 km，分析中使用 0-25 km 物理范围。EPIC 自动审计发现 CTH-like 变量为 `geophysical_data/A-band_Effective_Cloud_Height` 与 `geophysical_data/B-band_Effective_Cloud_Height`，本次主分析采用 A-band，原单位 m，统一转换为 km。该变量是 Oxygen A-band effective cloud height，因此所有结论均写作“相对 EPIC effective cloud height 的偏离”，不写作真实 CTH 误差。",
        "",
        "## 主指标",
        "",
        f"Policy A / D1 both-cloud 共 `{int(row_value(a_d1, 'n_valid_cth', 0))}` 个有效比较像元，overall bias = `{fmt(row_value(a_d1, 'bias_km'))}` km，MAE = `{fmt(row_value(a_d1, 'mae_km'))}` km，RMSE = `{fmt(row_value(a_d1, 'rmse_km'))}` km，within 2 km = `{fmt(row_value(a_d1, 'within_2km_fraction'))}`，low/mid/high class agreement = `{fmt(row_value(a_d1, 'low_mid_high_class_agreement'))}`。",
        f"D0 common-valid MAE = `{fmt(row_value(a_d0, 'mae_km'))}` km；Policy B high-confidence both-cloud MAE = `{fmt(row_value(b_d1, 'mae_km'))}` km，与 Policy A 接近，说明主结论不主要由低置信云掩膜类别驱动。",
        "",
        "## Source Selection 与重叠区",
        "",
        "按 selected source 的 Policy A / D1 MAE 排序：",
        "",
        join_metric_rows(source_rows[["selected_source", "n_valid_cth", "mae_km", "bias_km", "rmse_km"]], "selected_source", ["n_valid_cth", "mae_km", "bias_km", "rmse_km"]),
        "",
        "Meteosat-0deg 与 Meteosat-IODC 是 CTH 高 MAE 区，但这仍是 EPIC-referenced 诊断，不是源真实性排名。必须与覆盖区、几何条件、EPIC effective-height 语义差异一起解释。",
        "",
        valid_ge4_note,
        "",
        "CTH valid source count 实际分层：",
        "",
        join_metric_rows(valid_rows[["valid_source_count_bin", "n_valid_cth", "mae_km", "bias_km"]], "valid_source_count_bin", ["n_valid_cth", "mae_km", "bias_km"]),
        "",
        "## Prefusion 与 Source Pair",
        "",
        "Prefusion source CTH 相对 EPIC 的 MAE 排序：",
        "",
        join_metric_rows(pref_rows[["source_name", "n_valid_cth", "mae_km", "bias_km"]], "source_name", ["n_valid_cth", "mae_km", "bias_km"]),
        "",
        "Source-pair same-pixel 诊断中，`B_minus_A_mae < 0` 表示 B 更接近 EPIC。关键对照如下：",
        "",
        join_metric_rows(pair_rows[["source_A", "source_B", "B_minus_A_mae", "source_cth_disagreement_mean_abs_km"]].assign(source_A=lambda x: x["source_A"] + " vs " + x["source_B"]), "source_A", ["B_minus_A_mae", "source_cth_disagreement_mean_abs_km"]),
        "",
        "## Selection Regret",
        "",
        "EPIC-referenced retrospective oracle 的 regret 摘要：",
        "",
        join_metric_rows(regret_rows[["pixel_group", "n_valid_cth", "current_selected_mae_km", "best_available_mae_km", "selection_regret_mae_km"]], "pixel_group", ["n_valid_cth", "current_selected_mae_km", "best_available_mae_km", "selection_regret_mae_km"]),
        "",
        f"selected_MeteosatIODC 的 CTH regret 约 `{fmt(summary.get('iodc_regret'))}` km，高云组 regret 也明显偏高。这表明 CTH source-selection 机制与 Stage 09D cloud-mask SEL 结论方向一致：Meteosat selected 区域是后续机制诊断重点，但不应直接转化为生产规则。",
        "",
        "## 高度、边界与几何",
        "",
        "按 EPIC height class：",
        "",
        join_metric_rows(height_rows[["group_value", "n_valid_cth", "mae_km", "rmse_km", "low_mid_high_class_agreement"]], "group_value", ["n_valid_cth", "mae_km", "rmse_km", "low_mid_high_class_agreement"]),
        "",
        "按 fused height class：",
        "",
        join_metric_rows(fused_height_rows[["group_value", "n_valid_cth", "mae_km", "rmse_km"]], "group_value", ["n_valid_cth", "mae_km", "rmse_km"]),
        "",
        "边界/碎云分层：",
        "",
        join_metric_rows(boundary_rows[["group_value", "n_valid_cth", "mae_km", "rmse_km"]], "group_value", ["n_valid_cth", "mae_km", "rmse_km"]),
        "",
        f"本次定义下 clean-core MAE = `{fmt(row_value(d5, 'mae_km'))}` km，boundary/broken MAE = `{fmt(row_value(d6, 'mae_km'))}` km；边界层未比 clean-core 更高，说明 CTH 偏差更受高度语义、源选择和区域系统差异影响，而不是单纯云边界造成。",
        "",
        "EPIC VZA 分层：",
        "",
        join_metric_rows(vza_rows[["group_value", "n_valid_cth", "mae_km"]], "group_value", ["n_valid_cth", "mae_km"]),
        "",
        f"当前结果不支持简单的“EPIC VZA 越高 MAE 越大”；high-cloud 组 MAE = `{fmt(row_value(d7, 'mae_km'))}` km、RMSE = `{fmt(row_value(d7, 'rmse_km'))}` km，仍支持 targeted parallax/height-semantics pilot，但 pilot 应优先聚焦 high cloud + Meteosat selected + source disagreement 区，而不是只按 VZA 阈值筛选。",
        "",
        "## 可进入汇报的结论",
        "",
        f"可进入汇报：Stage 06 fused CTH 与 EPIC effective cloud height 在 53 个样本的 both-cloud 域中 MAE 约 `{fmt(row_value(a_d1, 'mae_km'))}` km、RMSE 约 `{fmt(row_value(a_d1, 'rmse_km'))}` km；Meteosat selected 区域 CTH MAE 较高；EPIC effective height 与 GEO CTH 变量语义不完全一致；high-cloud 是最重要的误差层；CTH 有效源计数中没有 `>=4`，不能照搬 cloud-mask valid_source_count>=4 结论。",
        "",
        "仅作为内部诊断：source-pair 的相对好坏、EPIC oracle selection regret、selected source 机制替代规则、parallax pilot 候选区。这些结果依赖 EPIC reference 和当前投影采样，不能直接作为 production fusion v2 规则。",
        "",
        "## 建议下一步",
        "",
        "优先做 semantic mapping：明确 EPIC effective cloud height、GEO cloud top height、source-specific CTH 的物理定义差异。其次做 PSF-aware / representativeness 对照，避免 EPIC 2048 像元与 0.05° GEO grid 的尺度差异污染判断。parallax pilot 可以启动，但应作为 targeted diagnostic pilot，聚焦 high-cloud + Meteosat selected + source-pair disagreement 区域。",
        "",
        "## 输出索引",
        "",
        f"- 主报告：`{output_dir / 'reports' / 'stage_10_cth_fused_product_validation_report_cn.md'}`",
        f"- fused/EPIC/source variable inventory：`{output_dir / '00_inventory' / 'stage_10_cth_variable_inventory.csv'}`",
        f"- EPIC CTH variable inventory：`{output_dir / '00_inventory' / 'stage_10_epic_cth_variable_inventory.csv'}`",
        f"- unit conversion table：`{output_dir / '00_inventory' / 'stage_10_cth_unit_conversion_table.csv'}`",
        f"- fused CTH by domain/sample：`{output_dir / '02_fused_cth_metrics' / 'stage_10_fused_cth_metrics_by_domain.csv'}` / `{output_dir / '02_fused_cth_metrics' / 'stage_10_fused_cth_metrics_by_sample.csv'}`",
        f"- fused CTH by selected_source：`{output_dir / '03_fused_cth_by_selected_source' / 'stage_10_fused_cth_metrics_by_selected_source.csv'}`",
        f"- fused CTH by valid_source_count：`{output_dir / '03_fused_cth_by_selected_source' / 'stage_10_fused_cth_metrics_by_valid_source_count.csv'}`",
        f"- prefusion source CTH：`{output_dir / '04_prefusion_source_cth' / 'stage_10_prefusion_source_cth_metrics_by_source.csv'}`",
        f"- source-pair CTH：`{output_dir / '05_source_pair_cth' / 'stage_10_cth_source_pair_summary.csv'}`",
        f"- CTH selection regret：`{output_dir / '06_selection_sensitivity_cth' / 'stage_10_cth_selection_regret_summary.csv'}`",
        f"- geometry / boundary / height stratification：`{output_dir / '07_geometry_boundary_height' / 'stage_10_cth_error_by_height_geometry_boundary.csv'}`",
        f"- case atlas index：`{output_dir / '08_case_atlas' / 'stage_10_cth_case_inventory.csv'}`",
        f"- plot index：`{output_dir / '09_figures' / 'stage_10_cth_plot_index.csv'}`",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Rewrite the Stage 10 CTH Chinese report from existing UTF-8 CSV/JSON outputs.")
    parser.add_argument("--output-dir", type=Path, default=RUNS_ROOT / "stage_10_cth_fused_product_validation_202403")
    args = parser.parse_args()
    report = args.output_dir / "reports" / "stage_10_cth_fused_product_validation_report_cn.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(build_report(args.output_dir), encoding="utf-8-sig")
    print(report)


if __name__ == "__main__":
    main()
