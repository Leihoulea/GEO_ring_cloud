from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pandas as pd

from geo_ring_cloud.paths import EXTERNAL_EPIC_L2_ROOT, RUNS_ROOT


SUMMARY_ROOT = RUNS_ROOT / "epic_202403_multisample_summary"
TIME_CONTROL_ROOT = RUNS_ROOT / "epic_202403_meteosat_time_offset_control"
TARGET_SELECTION_ROOT = RUNS_ROOT / "epic_202403_target_selection"
OUT_REPORT = SUMMARY_ROOT / "stage08_epic_comparison_full_integrated_report_cn.md"


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, encoding="utf-8-sig").fillna("")


def fmt(x: Any, nd: int = 3) -> str:
    try:
        if x == "":
            return ""
        return f"{float(x):.{nd}f}"
    except Exception:
        return str(x)


def md_table(df: pd.DataFrame, cols: list[str], headers: list[str] | None = None, max_rows: int | None = None, float_nd: int = 3) -> str:
    if df.empty:
        return "_无可用数据。_"
    d = df.copy()
    if max_rows is not None:
        d = d.head(max_rows)
    headers = headers or cols
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for _, row in d.iterrows():
        vals = []
        for c in cols:
            v = row.get(c, "")
            if isinstance(v, float) or (isinstance(v, str) and v.replace(".", "", 1).replace("-", "", 1).isdigit()):
                vals.append(fmt(v, float_nd))
            else:
                vals.append(str(v))
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines)


def rel(path: Path) -> str:
    return str(path).replace("\\", "/")


def mean_group(df: pd.DataFrame, group_cols: list[str], value_cols: list[str]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    work = df.copy()
    for c in value_cols:
        work[c] = pd.to_numeric(work[c], errors="coerce")
    return work.groupby(group_cols, dropna=False)[value_cols].mean().reset_index()


def run() -> Path:
    summary = read_csv(SUMMARY_ROOT / "epic_georing_multisample_summary.csv")
    source_metrics = read_csv(SUMMARY_ROOT / "epic_georing_multisample_source_metrics.csv")
    quicklook_index = read_csv(SUMMARY_ROOT / "epic_georing_multisample_quicklook_index.csv")
    candidate_inventory = read_csv(TARGET_SELECTION_ROOT / "epic_202403_geo_source_candidate_inventory.csv")
    recommended_targets = read_csv(TARGET_SELECTION_ROOT / "recommended_epic_georing_validation_targets.csv")
    geom_lat = read_csv(SUMMARY_ROOT / "geometry_prefusion_diagnostics" / "fused_metrics_by_latitude_and_epic_view_angle.csv")
    prefusion = read_csv(SUMMARY_ROOT / "geometry_prefusion_diagnostics" / "prefusion_source_cloud_mask_vs_epic_metrics.csv")
    overlap_count = read_csv(SUMMARY_ROOT / "geometry_prefusion_diagnostics" / "fused_metrics_by_overlap_count.csv")
    time_control_candidates = read_csv(TIME_CONTROL_ROOT / "08h_meteosat_time_offset_candidates.csv")
    time_control_metrics = read_csv(TIME_CONTROL_ROOT / "08h_meteosat_time_offset_metrics.csv")
    time_control_summary = read_csv(TIME_CONTROL_ROOT / "08h_meteosat_time_offset_summary.csv")
    source_overlap_dir = TIME_CONTROL_ROOT / "source_overlap_diagnostics"
    s08i_prefusion = read_csv(source_overlap_dir / "08i_prefusion_single_source_metrics.csv")
    s08i_selected = read_csv(source_overlap_dir / "08i_fused_selected_region_metrics.csv")
    s08i_overlap = read_csv(source_overlap_dir / "08i_overlap_count_metrics.csv")
    s08i_drag = read_csv(source_overlap_dir / "08i_meteosat_drag_diagnostics.csv")
    pair_dir = TIME_CONTROL_ROOT / "prefusion_source_pair_overlap_diagnostics"
    s08j_source = read_csv(pair_dir / "08j_prefusion_single_source_vs_epic.csv")
    s08j_pair = read_csv(pair_dir / "08j_prefusion_pair_overlap_vs_epic.csv")

    lines: list[str] = []
    lines += [
        "# 阶段 08：GEO-ring 云产品与 DSCOVR EPIC 对比验证综合报告",
        "",
        "本报告统合截至目前阶段 08 的全部主要实验、比较数据和结论，覆盖 EPIC L2 云产品结构理解、GEO-ring 与 EPIC 的语义统一、单时次与多样本对比、几何/纬度/视角诊断、融合前 source-by-source 诊断、Meteosat 时间差控制实验、以及未融合源产品的成对重叠区诊断。",
        "",
        "重要说明：EPIC 在本阶段被作为独立参照和诊断工具，而不是绝对真值，也不参与 GEO-ring 生产融合。所有与 EPIC 的 agreement / F1 / IoU 都应解释为“在当前语义映射和几何采样方式下的一致性指标”。",
        "",
        "## 0. 输出目录与主要文件",
        "",
        f"- 多样本汇总目录：`{SUMMARY_ROOT}`",
        f"- EPIC 月样本候选目录：`{TARGET_SELECTION_ROOT}`",
        f"- Meteosat 时间差控制目录：`{TIME_CONTROL_ROOT}`",
        f"- 本综合报告：`{OUT_REPORT}`",
        "",
        "主要脚本：",
        "",
        "- `08_epic_visual_comparison.py`：最早的 EPIC 视觉对比。",
        "- `08b_epic_l2_cloud_audit_compare.py`：EPIC L2 cloud 产品结构探查与初步比较。",
        "- `08c_epic_cloud_mask_semantic_sensitivity.py`：cloud mask 语义敏感性比较。",
        "- `08d_select_epic_monthly_semantic_validation_targets.py`：从 2024-03 EPIC L2 月数据中选择代表性样本。",
        "- `run_epic_georing_single_sample.py`：复用 02/03/05/06/08c 的单样本工程化 runner。",
        "- `run_epic_georing_sample_batch.py`：多样本批量运行。",
        "- `08e_summarize_epic_georing_multisample.py`：多样本指标和 quicklook 汇总。",
        "- `08f_geometry_and_prefusion_epic_diagnostics.py`：几何/纬度/EPIC view angle 与融合前 source 诊断。",
        "- `08g_overlap_count_diagnostics.py`：valid source count / 重叠区诊断。",
        "- `08h_meteosat_time_offset_control_test.py`：Meteosat 时间差控制实验。",
        "- `08i_meteosat_source_overlap_diagnostics.py`：Meteosat 小时间差样本的 source / overlap 诊断。",
        "- `08j_prefusion_source_pair_overlap_diagnostics.py`：未融合源产品成对重叠区 vs EPIC 诊断。",
        "",
        "## 1. 阶段 08 的科学问题",
        "",
        "阶段 07 已经回答了 GEO-ring 内部相邻卫星重叠区是否自洽，但内部一致性不能说明拼接后的云场从独立观测视角看是否合理。阶段 08 使用 DSCOVR EPIC L2 Cloud 作为外部独立参照，重点回答：",
        "",
        "1. GEO-ring fused cloud mask 与 EPIC cloud mask 在视觉和统计上是否一致？",
        "2. 差异是来自 cloud mask 类别语义，还是来自几何、纬度、边缘视角、源卫星产品差异或融合策略？",
        "3. 当前 hard best-source cloud mask 融合是否足够，还是需要 fusion v2？",
        "4. Meteosat 低一致性是否只是时间差造成，还是存在产品/语义/区域性问题？",
        "",
        "## 2. 数据与比较方式",
        "",
        "### 2.1 EPIC 数据",
        "",
        f"本阶段使用本地 2024 年 3 月 EPIC L2 Cloud 文件：`{EXTERNAL_EPIC_L2_ROOT}`。已扫描文件数为 395 个。每个 EPIC L2 样本至少读取：",
        "",
        "- `geophysical_data/Cloud_Mask` 或同义变量；",
        "- `geolocation_data/latitude`；",
        "- `geolocation_data/longitude`；",
        "- 若存在，则读取 view angle / sun angle 用于几何分层诊断。",
        "",
        "### 2.2 GEO-ring 数据",
        "",
        "使用阶段 06 输出的 GEO-ring fused cloud products，以及阶段 05 输出的未融合、单卫星、单变量重投影 cloud mask。对于后续 source-pair 诊断，直接使用未融合源产品：FY4B CLM、GOES-16 ACMF、GOES-18 ACMF、Himawari-9 CMSK、Meteosat-0deg CLM、Meteosat-IODC CLM。",
        "",
        "### 2.3 空间匹配方式",
        "",
        "当前定量比较不是完整 EPIC 视角辐射级重投影，而是：",
        "",
        "1. GEO-ring 产品位于 0.05° 经纬网；",
        "2. 对每个 EPIC L2 像元，读取其 latitude / longitude；",
        "3. 从 GEO-ring 0.05° 网格最近邻采样对应变量；",
        "4. 在 EPIC 像元空间计算 agreement、precision、recall、F1、IoU 和 confusion matrix。",
        "",
        "因此，高纬、EPIC 圆盘边缘、云边界和强视差区域可能存在代表性误差。这个限制贯穿所有结论。",
        "",
        "## 3. Cloud mask 语义统一策略",
        "",
        "不同卫星的 cloud mask 编码不一致。GOES 接近二值，FY4B/Himawari/Meteosat/EPIC 常包含 clear、probably clear、probably cloudy、cloudy、not processed/off-disc 等类别。因此阶段 08 不直接比较原始数值，而定义三套语义策略：",
        "",
        "| 模式 | 名称 | EPIC 映射 | GEO-ring 标准映射 | 用途 |",
        "|---|---|---|---|---|",
        "| Mode A | inclusive binary | 1/2=clear, 3/4=cloud | 0/1=clear, 2/3=cloud | 看总体云区覆盖，包容可能云类 |",
        "| Mode B | high-confidence binary | 1=clear, 4=cloud，其余剔除 | 0=clear, 3=cloud，其余剔除 | 看高置信一致性，减少不确定类别影响 |",
        "| Mode C | uncertainty-aware 3-class | 1=clear, 2/3=uncertain, 4=cloud | 0=clear, 1/2=uncertain, 3=cloud | 诊断语义不确定来源，不强行二值化 |",
        "",
        "Mode A 和 Mode B 的差异是阶段 08 的核心诊断量之一。如果 B 明显高于 A，说明不确定类别/低置信类别对 mismatch 贡献较大。",
        "",
        "## 4. EPIC 月样本选择",
        "",
        f"- 候选 inventory：`{TARGET_SELECTION_ROOT / 'epic_202403_geo_source_candidate_inventory.csv'}`",
        f"- 推荐目标：`{TARGET_SELECTION_ROOT / 'recommended_epic_georing_validation_targets.csv'}`",
        "",
        "候选选择逻辑：扫描 EPIC 2024-03 L2 Cloud 文件，按文件名解析 EPIC 时间，匹配最近整点 GEO-ring core time index，并估计 EPIC 圆盘采样像元的主导 GEO source。样本类型包括 FY4B/Himawari 主导、GOES 主导、Meteosat 主导和 mixed/boundary。",
        "",
        "推荐样本概览：",
        "",
    ]
    if not recommended_targets.empty:
        tmp = recommended_targets.copy()
        keep = ["selection_role", "candidate_class", "epic_time", "nearest_hour", "nearest_hour_delta_min", "dominant_satellite_estimate", "dominant_fraction_estimate", "east_asia_fraction_estimate"]
        lines.append(md_table(tmp[keep], keep, ["选择角色", "候选类型", "EPIC 时间", "最近 GEO 整点", "时间差 min", "主导卫星", "主导比例", "FY4B+Himawari 比例"], max_rows=20))
    else:
        lines.append("_未找到推荐样本表。_")

    lines += [
        "",
        "## 5. 多样本 GEO-ring vs EPIC 语义对比结果",
        "",
        f"汇总表：`{SUMMARY_ROOT / 'epic_georing_multisample_summary.csv'}`",
        "",
        "### 5.1 单样本指标",
        "",
    ]
    if not summary.empty:
        cols = ["time_tag", "candidate_class", "epic_delta_min", "estimated_dominant_satellite", "estimated_dominant_fraction", "A_agreement", "A_f1", "A_iou", "B_agreement", "B_f1", "B_iou", "C_agreement"]
        lines.append(md_table(summary[cols], cols, ["样本", "类型", "时间差 min", "主导源", "主导比例", "A agree", "A F1", "A IoU", "B agree", "B F1", "B IoU", "C agree"]))
    lines += [
        "",
        "图：单样本 Mode A/B 指标。",
        "",
        f"![sample metrics]({rel(SUMMARY_ROOT / '01_sample_level_metrics_cn.png')})",
        "",
        f"![agreement by sample]({rel(SUMMARY_ROOT / 'plots' / 'agreement_by_sample_A_B.png')})",
        "",
        "### 5.2 按样本类型分组",
        "",
    ]
    if not summary.empty:
        group = mean_group(summary, ["candidate_class"], ["epic_delta_min", "A_agreement", "A_f1", "A_iou", "B_agreement", "B_f1", "B_iou", "C_agreement"])
        count = summary.groupby("candidate_class").size().reset_index(name="n")
        group = group.merge(count, on="candidate_class", how="left")
        cols = ["candidate_class", "n", "epic_delta_min", "A_agreement", "A_f1", "B_agreement", "B_f1", "C_agreement"]
        lines.append(md_table(group[cols], cols, ["样本类型", "n", "平均时间差", "A agree", "A F1", "B agree", "B F1", "C agree"]))
    lines += [
        "",
        f"![group summary]({rel(SUMMARY_ROOT / '02_group_summary_cn.png')})",
        "",
        f"![group B metrics]({rel(SUMMARY_ROOT / '06_group_B_metrics_bar_cn.png')})",
        "",
        "核心观察：GOES-dominant 和 FY4B/Himawari-dominant 样本整体较好；Meteosat-dominant 样本最弱；mixed/boundary 介于两者之间。Mode B 通常高于 Mode A，说明低置信/不确定 cloud mask 类别确实影响 agreement。",
        "",
        "## 6. Quicklook 与视觉检查",
        "",
        f"- Quicklook index：`{SUMMARY_ROOT / 'epic_georing_multisample_quicklook_index.csv'}`",
        "",
        "代表性 quicklook：",
        "",
        "### 6.1 FY4B/Himawari 主导样本",
        "",
        f"![east asia sample]({rel(SUMMARY_ROOT / 'renamed_quicklooks' / '20240313_0400_east-asia-fy4b-himawari-priority_Himawari-9_B_B_high_confidence_only_epic_vs_georing_cloud_mask.png')})",
        "",
        "### 6.2 GOES 主导样本",
        "",
        f"![goes sample]({rel(SUMMARY_ROOT / 'renamed_quicklooks' / '20240313_2200_goes-dominant-control_GOES-18_B_B_high_confidence_only_epic_vs_georing_cloud_mask.png')})",
        "",
        "### 6.3 Meteosat 主导样本",
        "",
        f"![meteosat sample]({rel(SUMMARY_ROOT / 'renamed_quicklooks' / '20240311_1400_meteosat-dominant-control_Meteosat-0deg_B_B_high_confidence_only_epic_vs_georing_cloud_mask.png')})",
        "",
        "这些图不只用于展示“像不像”，还用于定位 mismatch 是否集中于边缘、高纬、source boundary 或特定服务区。",
        "",
        "## 7. 几何、纬度与 EPIC view angle 诊断",
        "",
        f"诊断报告：`{SUMMARY_ROOT / 'geometry_prefusion_diagnostics' / 'geometry_prefusion_diagnostics_report.md'}`",
        "",
    ]
    if not geom_lat.empty:
        lines += ["可用分层结果节选：", ""]
        cols = [c for c in ["mode", "group", "bin", "n", "agreement", "f1", "iou"] if c in geom_lat.columns]
        if cols:
            lines.append(md_table(geom_lat[cols], cols, max_rows=30))
    lines += [
        "",
        "总体结论：高纬和 EPIC 圆盘边缘会降低一致性，尤其 70°–90° 或大 EPIC view angle 区域；但 |lat|<60° 后总体 agreement 并没有大幅提升，因此纬度/边缘效应不是全部解释。",
        "",
        "## 8. 融合前 source-by-source 诊断",
        "",
        f"融合前 source 指标：`{SUMMARY_ROOT / 'geometry_prefusion_diagnostics' / 'prefusion_source_cloud_mask_vs_epic_metrics.csv'}`",
        "",
    ]
    if not prefusion.empty and "source" in prefusion.columns:
        # Some older outputs may have unnamed columns when displayed via PowerShell, but pandas should preserve headers.
        cols_existing = [c for c in ["source", "mode", "agreement", "f1", "iou", "pixels"] if c in prefusion.columns]
        if cols_existing:
            pgroup = mean_group(prefusion, ["source", "mode"], [c for c in ["agreement", "f1", "iou", "pixels"] if c in prefusion.columns])
            lines.append(md_table(pgroup, cols_existing if set(cols_existing).issubset(pgroup.columns) else list(pgroup.columns), max_rows=60))
    lines += [
        "",
        f"![source performance]({rel(SUMMARY_ROOT / '03_source_performance_cn.png')})",
        "",
        f"![source heatmap]({rel(SUMMARY_ROOT / 'plots' / 'agreement_by_source_heatmap_A.png')})",
        "",
        "核心观察：source family 的差异非常明显。Meteosat 相关区域较弱，但其他源在非主服务区或边缘区也可能明显变弱，因此不能简单地用单一 source 绝对优先级解释所有 mismatch。",
        "",
        "## 9. valid source count / 重叠区诊断",
        "",
        f"重叠区诊断表：`{SUMMARY_ROOT / 'geometry_prefusion_diagnostics' / 'fused_metrics_by_overlap_count.csv'}`",
        "",
    ]
    if not overlap_count.empty:
        cols = [c for c in ["mode", "overlap_scope", "n", "agreement", "f1", "iou", "pixel_fraction"] if c in overlap_count.columns]
        if cols:
            lines.append(md_table(overlap_count[cols], cols, max_rows=40))
    lines += [
        "",
        "结论：低一致性不只发生在少量 source boundary。部分样本中 valid_count>=2 的多源重叠区占绝大多数，并且其指标接近整体指标。因此后续 fusion v2 需要处理整个多源可用区域，而不是只修补一条拼接边线。",
        "",
        "## 10. Meteosat 时间差控制实验（08h）",
        "",
        "### 10.1 问题",
        "",
        "旧的两个 Meteosat-dominant 样本与 EPIC 的时间差最大，约 22–28 分钟。因此需要控制时间差，判断 Meteosat 低一致性是否主要由时间差造成。",
        "",
        "### 10.2 新筛选的小时间差 Meteosat 样本",
        "",
    ]
    if not time_control_candidates.empty:
        cols = ["time_tag", "epic_time", "nearest_hour", "nearest_hour_delta_min", "dominant_satellite_estimate", "meteosat_fraction_estimate"]
        lines.append(md_table(time_control_candidates[cols], cols, ["样本", "EPIC 时间", "GEO 整点", "时间差 min", "主导源", "Meteosat 估计占比"]))
    lines += [
        "",
        "### 10.3 新旧 Meteosat 样本对比",
        "",
    ]
    if not time_control_summary.empty:
        cols = ["cohort", "policy", "n", "delta_min_mean", "agreement_mean", "f1_mean", "iou_mean"]
        lines.append(md_table(time_control_summary[cols], cols, ["样本组", "策略", "n", "平均时间差", "mean agreement", "mean F1", "mean IoU"]))
    lines += [
        "",
        f"![time offset scatter]({rel(TIME_CONTROL_ROOT / '08h_meteosat_time_offset_agreement_scatter.png')})",
        "",
        "08h 结论：把 EPIC-GEO 时间差从约 25 分钟降到约 2.4 分钟后，Meteosat agreement 没有提高，反而略低。因此当前证据不支持“时间差是 Meteosat 低一致性的主因”。时间差是混杂因素，但主要矛盾更可能在 Meteosat CLM 语义、源产品特性、区域几何或当前融合策略。",
        "",
        "## 11. Meteosat 小时间差样本的 source / overlap 诊断（08i）",
        "",
        "08i 先看当前 fused source_map 分区：哪些像元由哪个源选中，以及这些选中区域与 EPIC 的一致性。",
        "",
        "### 11.1 当前 source_map 选中区域指标",
        "",
    ]
    if not s08i_selected.empty:
        sel = s08i_selected[(s08i_selected["scope"] == "fused_pixels_selected_from_source") & (s08i_selected["status"] == "OK")].copy()
        if not sel.empty:
            group = mean_group(sel, ["policy", "source_name"], ["selected_pixel_fraction_of_fused_valid", "agreement", "f1", "iou", "n"])
            cols = ["policy", "source_name", "selected_pixel_fraction_of_fused_valid", "agreement", "f1", "n"]
            lines.append(md_table(group[cols], cols, ["策略", "选中源", "选中比例", "agreement", "F1", "像元数"]))
    lines += [
        "",
        "观察：Meteosat-IODC 在当前 source_map 中选中比例高且 agreement 最低，是这组三个小时间差样本里更明显的低一致性贡献源；Meteosat-0deg 也偏弱，但通常优于 IODC。",
        "",
        "### 11.2 08i 重叠区与 oracle 反事实",
        "",
    ]
    if not s08i_overlap.empty:
        group = mean_group(s08i_overlap[s08i_overlap["status"] == "OK"], ["policy", "overlap_scope"], ["pixel_fraction_of_fused_valid", "agreement", "f1", "n"])
        cols = ["policy", "overlap_scope", "pixel_fraction_of_fused_valid", "agreement", "f1", "n"]
        lines.append(md_table(group[cols], cols, ["策略", "重叠范围", "像元比例", "agreement", "F1", "像元数"], max_rows=30))
    lines += ["", "Meteosat 选中区域如果强行只换成非 Meteosat 的 oracle 上限：", ""]
    if not s08i_drag.empty:
        group = mean_group(s08i_drag, ["policy"], ["n", "current_selected_agreement", "non_meteosat_oracle_possible_agreement", "potential_gain_if_perfectly_replaced_by_non_meteosat", "non_meteosat_available_fraction"])
        cols = ["policy", "n", "current_selected_agreement", "non_meteosat_oracle_possible_agreement", "potential_gain_if_perfectly_replaced_by_non_meteosat", "non_meteosat_available_fraction"]
        lines.append(md_table(group[cols], cols, ["策略", "像元数", "当前 Meteosat agree", "非 Meteosat oracle", "潜在增益", "非 Meteosat 可用比例"]))
    lines += [
        "",
        "注意：这个 oracle 是事后诊断，不是生产算法。它说明在这些 Meteosat 主导区域，粗暴禁用 Meteosat 并不自动变好。",
        "",
        "## 12. 未融合源产品成对重叠区诊断（08j）",
        "",
        "08j 回到未融合的各卫星重投影 cloud_mask，在同一 EPIC 像元上直接比较源 A 与源 B。它比 08i 更适合回答：在实际重叠区，到底该更相信哪个源。",
        "",
        "### 12.1 单源直接对 EPIC",
        "",
    ]
    if not s08j_source.empty:
        group = mean_group(s08j_source[s08j_source["status"] == "OK"], ["policy", "source"], ["n", "agreement", "f1", "iou"])
        cols = ["policy", "source", "n", "agreement", "f1", "iou"]
        lines.append(md_table(group[cols], cols, ["策略", "源", "有效像元", "agreement", "F1", "IoU"], max_rows=60))
    lines += [
        "",
        "### 12.2 成对重叠区结果",
        "",
    ]
    if not s08j_pair.empty:
        pair_ok = s08j_pair[s08j_pair["status"] == "OK"].copy()
        group = mean_group(
            pair_ok,
            ["policy", "source_a", "source_b", "pair_meaning"],
            ["n", "source_a_agreement", "source_b_agreement", "agreement_b_minus_a", "source_a_only_correct_fraction", "source_b_only_correct_fraction", "both_wrong_fraction", "source_disagreement_fraction"],
        )
        cols = ["policy", "source_a", "source_b", "pair_meaning", "n", "source_a_agreement", "source_b_agreement", "agreement_b_minus_a", "source_a_only_correct_fraction", "source_b_only_correct_fraction", "both_wrong_fraction", "source_disagreement_fraction"]
        lines.append(md_table(group[cols], cols, ["策略", "源 A", "源 B", "重叠意义", "像元数", "A agree", "B agree", "B-A", "仅 A 对", "仅 B 对", "都错", "源分歧"], max_rows=80))
    lines += [
        "",
        "08j 的关键结论：",
        "",
        "1. Meteosat-0deg 在与 GOES-16 和 IODC 的重叠区并不一定更差；在 Mode A 下，0deg 通常略优于 GOES-16 和 IODC。",
        "2. Meteosat-IODC 在与 FY4B 的重叠区明显优于 FY4B，因此不能简单用 FY4B 替代 IODC。",
        "3. FY4B 与 Himawari 的东亚重叠区中，Himawari 明显优于 FY4B；这可能反映样本位置、FY4B 西/边缘几何、语义映射或 cloud mask 产品差异，不代表 FY4B 全局失败。",
        "4. Meteosat-IODC 与 Himawari 的远东诊断重叠区中 Himawari 更好，但这不是 Meteosat 主重叠关系，不能直接推广为大范围替代策略。",
        "5. 因此，fusion v2 应该是 source-pair-aware 和 region-aware，而不是全局禁用某颗卫星。",
        "",
        "## 13. 截至目前的综合结论",
        "",
        "### 13.1 关于 EPIC 对比本身",
        "",
        "- 阶段 08 已从最初视觉 quicklook 扩展为可复用的 EPIC-L2 cloud mask 统计验证框架。",
        "- 当前比较是在 EPIC 像元空间采样 GEO-ring 0.05° 网格，不是完整 EPIC 视角辐射重投影。",
        "- EPIC 不是绝对真值，尤其 cloud mask 类别语义、空间分辨率和观测几何与 GEO 不完全等价。",
        "",
        "### 13.2 关于总体指标",
        "",
        "- GOES-dominant 和 FY4B/Himawari-dominant 样本表现较好。",
        "- Meteosat-dominant 样本表现最弱。",
        "- Mode B 通常优于 Mode A，说明 cloud mask 低置信/不确定类别是 mismatch 的重要来源。",
        "",
        "### 13.3 关于 Meteosat",
        "",
        "- 大时间差不是 Meteosat 低一致性的主因；小时间差复测未带来改善。",
        "- Meteosat-IODC 比 Meteosat-0deg 更需要重点排查。",
        "- 但不能简单认为“禁用 Meteosat 就会更好”。在部分实际重叠区，Meteosat 反而优于替代源。",
        "- 因此 Meteosat 的处理应按 service、source pair 和区域分别建模，而不是统一降权。",
        "",
        "### 13.4 关于融合策略",
        "",
        "- 当前 06 hard best-source 是有效的原型基线，但不是最终科学融合策略。",
        "- 低一致性并不只来自 source boundary，而是在大面积多源可用区也存在。",
        "- 下一步 cloud mask fusion v2 应引入：cloud probability / confidence、source-pair-aware 权重、多源 consensus、边界/不确定性图，以及按区域的源策略。",
        "",
        "## 14. 建议下一步实验",
        "",
        "1. 对 Meteosat-IODC 单独做产品语义和原始 CLM code table 再确认，尤其 cloud / probably cloud / clear / not processed 的定义。",
        "2. 对 FY4B 在 Meteosat-dominant 样本中的异常低表现做边缘几何与原始 CLM 质量检查，避免误把边缘样本结论推广到 FY4B 全局。",
        "3. 实现 cloud mask fusion v2 的试验版：在重叠区用 source-pair-aware 权重和多源共识，而不是 hard best-source 单源切换。",
        "4. 增加 EPIC-view 严格重投影样本，用来评估当前 nearest-neighbor lon/lat 采样造成的几何代表性误差。",
        "5. 扩展样本数，按 source pair 分层抽样，而不是只按 dominant source 分组。",
        "",
        "## 15. 文件索引",
        "",
        "### 15.1 多样本汇总文件",
        "",
        f"- `{SUMMARY_ROOT / 'epic_georing_multisample_summary.csv'}`",
        f"- `{SUMMARY_ROOT / 'epic_georing_multisample_source_metrics.csv'}`",
        f"- `{SUMMARY_ROOT / 'epic_georing_multisample_quicklook_index.csv'}`",
        f"- `{SUMMARY_ROOT / 'epic_georing_multisample_summary_report.md'}`",
        f"- `{SUMMARY_ROOT / 'epic_georing_multisample_summary_report_cn.md'}`",
        "",
        "### 15.2 几何与融合前诊断",
        "",
        f"- `{SUMMARY_ROOT / 'geometry_prefusion_diagnostics' / 'geometry_prefusion_diagnostics_report.md'}`",
        f"- `{SUMMARY_ROOT / 'geometry_prefusion_diagnostics' / 'fused_metrics_by_latitude_and_epic_view_angle.csv'}`",
        f"- `{SUMMARY_ROOT / 'geometry_prefusion_diagnostics' / 'prefusion_source_cloud_mask_vs_epic_metrics.csv'}`",
        f"- `{SUMMARY_ROOT / 'geometry_prefusion_diagnostics' / 'fused_metrics_by_overlap_count.csv'}`",
        "",
        "### 15.3 Meteosat 时间差控制与源对诊断",
        "",
        f"- `{TIME_CONTROL_ROOT / '08h_meteosat_time_offset_control_report.md'}`",
        f"- `{TIME_CONTROL_ROOT / '08h_meteosat_time_offset_metrics.csv'}`",
        f"- `{source_overlap_dir / '08i_meteosat_source_overlap_diagnostics_report.md'}`",
        f"- `{source_overlap_dir / '08i_prefusion_single_source_metrics.csv'}`",
        f"- `{source_overlap_dir / '08i_overlap_count_metrics.csv'}`",
        f"- `{pair_dir / '08j_prefusion_source_pair_overlap_diagnostics_report.md'}`",
        f"- `{pair_dir / '08j_prefusion_pair_overlap_vs_epic.csv'}`",
        "",
        "### 15.4 PPT 与图表",
        "",
        f"- `{SUMMARY_ROOT / 'ppt_group_meeting' / 'Stage08_EPIC_GEO_ring_cloud_comparison_group_meeting_CN.pptx'}`",
        f"- `{SUMMARY_ROOT / 'ppt_group_meeting' / 'Stage08_EPIC_GEO_ring_cloud_comparison_group_meeting_CN.pdf'}`",
        f"- `{SUMMARY_ROOT / '01_sample_level_metrics_cn.png'}`",
        f"- `{SUMMARY_ROOT / '02_group_summary_cn.png'}`",
        f"- `{SUMMARY_ROOT / '03_source_performance_cn.png'}`",
        f"- `{SUMMARY_ROOT / '04_source_fraction_by_sample_cn.png'}`",
        f"- `{SUMMARY_ROOT / '05_quicklook_index_cn.png'}`",
        f"- `{SUMMARY_ROOT / '06_group_B_metrics_bar_cn.png'}`",
    ]

    OUT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    OUT_REPORT.write_text("\n".join(lines), encoding="utf-8")
    return OUT_REPORT


if __name__ == "__main__":
    print(run())
