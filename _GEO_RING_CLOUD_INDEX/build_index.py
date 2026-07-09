# -*- coding: utf-8 -*-
r"""
build_index.py — 构建 GEO-ring Cloud 任务文件索引（sqlite + xlsx）

只读扫描 D:\AAAresearch_paper 下与 GEO-ring Cloud 任务相关的目录，
结合人工已核实的结构化元数据（相关性判定、脚本职责、外部路径引用、
流水线阶段、time_runs 批次），生成：
  - geo_ring_cloud_index.sqlite   （机器可读主存储，7 张表）
  - geo_ring_cloud_index.xlsx     （人机兼顾多 sheet 表格视图）

不修改任何已有文件，所有产出写入本脚本所在目录 _GEO_RING_CLOUD_INDEX/。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(r"D:\AAAresearch_paper")
OUT_DIR = Path(r"D:\AAAresearch_paper\_GEO_RING_CLOUD_INDEX")
DB_PATH = OUT_DIR / "geo_ring_cloud_index.sqlite"
REFRESHED_DB_PATH = OUT_DIR / "geo_ring_cloud_index_refreshed.sqlite"
XLSX_PATH = OUT_DIR / "geo_ring_cloud_index.xlsx"
WORKSPACE_DIR = ROOT / "_GEO_RING_CLOUD_WORKSPACE"
ARCHIVE_DIR = ROOT / "_NON_GEO_ARCHIVE"
GENERATED_AT = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# ---------------------------------------------------------------------------
# 1. 目录扫描工具
# ---------------------------------------------------------------------------

def dir_stats(p: Path):
    """递归统计目录内文件数与总体积（字节）。跳过 __pycache__ / node_modules。"""
    nfiles = 0
    nbytes = 0
    exts = {}
    if not p.exists():
        return 0, 0, {}
    for dirpath, dirnames, filenames in os.walk(p):
        # 剪枝：跳过缓存/依赖目录，不计入统计
        dirnames[:] = [d for d in dirnames if d not in
                       ("__pycache__", "node_modules", ".git", ".claude", "dist", "venv")]
        for f in filenames:
            fp = Path(dirpath) / f
            try:
                sz = fp.stat().st_size
            except OSError:
                sz = 0
            nfiles += 1
            nbytes += sz
            ext = fp.suffix.lower() or "(无扩展名)"
            exts[ext] = exts.get(ext, 0) + 1
    return nfiles, nbytes, exts


def list_subdirs(p: Path, depth=1):
    """列出目录下指定深度的子目录。"""
    out = []
    if not p.exists():
        return out
    if depth == 1:
        for d in sorted(p.iterdir()):
            if d.is_dir() and not d.name.startswith(".") and d.name not in ("__pycache__", "node_modules"):
                out.append(d)
    return out


def fmt_size(b: int) -> str:
    if b is None:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024 or unit == "TB":
            return f"{b:.1f} {unit}" if unit != "B" else f"{b} B"
        b /= 1024
    return f"{b:.1f} TB"


def bool_text(value: bool) -> str:
    return "是" if value else "否"


def path_risk(relevance: str, referenced_by_code: str, exists_now: bool) -> str:
    if not exists_now:
        return "missing"
    if relevance == "强相关" or referenced_by_code in ("是", "间接"):
        return "keep_in_place"
    if relevance == "无关":
        return "archive_candidate"
    return "document_only"


def move_candidate(relevance: str, referenced_by_code: str, exists_now: bool) -> str:
    return bool_text(exists_now and relevance == "无关" and referenced_by_code == "否")


def infer_stage_from_name(rel_name: str) -> str:
    name = rel_name.replace("\\", "/")
    base = Path(name).name
    if base == "stage1_common.py":
        return "公共"
    if base.startswith("stage09d_") or name.startswith("stage09d_") or "/stage09d_" in name:
        return "09d"
    if base.startswith("stage09c_") or name.startswith("stage09c_") or "/stage09c_" in name:
        return "09c"
    if base.startswith("stage09b_") or name.startswith("stage09b_") or "/stage09b_" in name:
        return "09b"
    prefix = base.split("_", 1)[0]
    if prefix[:2].isdigit():
        return prefix
    return ""


def summarize_script(rel_name: str) -> str:
    stage = infer_stage_from_name(rel_name)
    if stage:
        return f"当前文件系统扫描补充脚本；推断阶段 {stage}"
    return "当前文件系统扫描补充脚本"


# ---------------------------------------------------------------------------
# 2. 结构化元数据（已通过读码 + 子代理 + 亲自 Grep 核实）
# ---------------------------------------------------------------------------

# 顶层目录相关性判定
# relevance: 强相关 / 上游相关 / 弱相关 / 无关
# referenced_by_code: 是否被 ring cloud 代码硬编码引用
TOP_DIRS = [
    # (相对路径, 相关性, 角色, 体积说明, 被代码引用, 备注)
    ("geo_ring_cloud_stage1", "强相关", "Stage1 主产物根目录（STAGE_ROOT）", "约 6.4 GB", "是", "standardized_native/reprojected_grid/fused_best_source/overlap_validation/reports 等产物 + scripts 副本"),
    ("geo_ring_cloud_stage1_time_runs", "强相关", "多时次运行目录（48 批次 + 5 EPIC 专题）", "较大", "是", "08e/08f/08g/08h/08i/08j/09 等脚本以 RUNS_ROOT 引用"),
    ("geo_ring_cloud_stage1_evidence_pack", "强相关", "Stage1 证据包（latest + 10 snapshots）", "约 1.2 MB", "是", "rebuild_stage1_evidence_pack.py:EVIDENCE_ROOT 产出"),
    ("third_report/code/geo_ring_cloud_stage1", "强相关", "Stage1 主代码（41 个 .py，01-09 全流水线）", "代码目录", "是", "ring cloud 真正的代码源；产物写到根 geo_ring_cloud_stage1/"),
    ("data_check_report", "强相关", "前序数据审计报告（REPORT_ROOT，00-00f 阶段证据源）", "约 872 MB / 185 文件", "是", "stage1_common.py:25 REPORT_ROOT；PARSED_METADATA/MAPPING_YAML 等"),
    ("geo_geometry_check", "强相关", "几何校验样本（download_geo_geometry_samples.py 产物 + 06c/06d 审计）", "约 1.34 GB / 50 文件", "是", "download_geo_geometry_samples.py:22 OUT_ROOT；06c/06d/06e 引用"),
    ("data", "强相关", "原始卫星数据（FY4A/FY4B L1 + FY4B 云产品 CLM/CLP/CLT/CTH/CTP/CTT/GEO + H09_Data）", "约 100+ GB", "是", "stage1_common.py:27 HIMAWARI_R21_DIR=data/H09_Data；06f 扫描 data/"),
    ("third_report/code/L1g", "上游相关", "standardized_L1_source 规范 + 全球 0.05° 网格规范", "小", "间接", "ring cloud 上游标准化层规范文档"),
    ("third_report/code/FY4B", "上游相关", "FY4B/AGRI standardized_L1 builder + 探查/预览", "小", "间接", "产出 standardized_L1_source 供 stage1 输入"),
    ("third_report/code/GOES", "上游相关", "GOES-16/18/ABI standardized_L1 builder", "小", "间接", "同上"),
    ("third_report/code/Himawari", "上游相关", "Himawari-9/AHI standardized_L1 builder", "小", "间接", "同上"),
    ("third_report/code/Meteosat", "上游相关", "Meteosat-9/10/SEVIRI standardized_L1 builder", "小", "间接", "同上"),
    ("third_report/code/geo_data_audit", "上游相关", "数据审计脚本（前序审计 00-00f 的执行代码）", "小", "间接", "audit_geometry_and_variables.py 等产出 data_check_report"),
    ("third_report/code/geo_cloud_download", "上游相关", "GEO 云产品下载器（EUMETSAT API + S3）", "小", "间接", "下载原始云产品到 data/ 与 E 盘"),
    ("third_report/code/priority_download_goes_meteosat", "上游相关", "GOES/Meteosat 优先补下载（00f 阶段）", "小", "间接", "build/download/verify 三件套"),
    ("third_report/code/preview_baselines", "上游相关", "基线预览图索引", "小", "间接", "六星基线 quicklook"),
    ("third_report/code/standardized_l1_source_satpy.py", "上游相关", "Satpy 共用标准化骨架", "小", "间接", "被 Himawari/Meteosat builder 调用"),
    ("third_report/code/run_standardized_l1_source_batch.py", "上游相关", "标准化批处理统一入口", "小", "间接", "调度各卫星 builder"),
    ("third_report/code/validate_standardized_l1_source_samples.py", "上游相关", "标准化样例自动校验", "小", "间接", ""),
    ("third_report/code/preview_runner.py", "上游相关", "预览批处理入口", "小", "间接", ""),
    ("third_report/code/plot_geo_satellite_coverage.py", "上游相关", "六星地球覆盖边界图", "小", "间接", ""),
    ("third_report/Satellite_Data_20240312", "上游相关", "2024-03-12 主数据集快照（六星原生 + standardized_L1 样例 + reprojected 旧版）", "大", "间接", "含 channel_mapping/standardized_L1_source 等"),
    ("research_tracker", "弱相关", "研究追踪器（项目治理元工具，扫描全项目建知识图谱）", "中", "否", "元工具，不参与流水线；识别 ring_cloud 为最大子项目"),
    ("cloud", "弱相关", "云产品参考文献 PDF（Zhao 等 2026 全天全球云物理属性）", "约 1.2 MB", "否", "方法论参考，代码无路径引用"),
    ("third_report/beamer", "弱相关", "LaTeX Beamer 组会幻灯片（讲标准化上游）", "中", "否", "讲稿明确讲 L1g 前期工作，非 ring cloud 本体"),
    ("6月第二次汇报邓浩然.pptx", "弱相关", "6 月组会汇报 PPT", "约 52 MB", "否", "项目阶段汇报材料"),
    ("EPIC数据.pptx", "弱相关", "EPIC 数据 PPT", "约 38 KB", "否", "偏向 EPIC/CERES 分支"),
    ("forth", "无关", "导航相机 DAT 数据解析 + QuickView 工具（独立任务）", "约 1.3 GB", "否", "nav_camera_dat.py；ring cloud 代码零引用"),
    ("second_report", "无关", "FY 数据分析第二次报告（FYDataService + 探查 notebook + 论文）", "约 18 GB", "否", "ring cloud 代码零引用"),
    ("third_report/code/epic_ceres", "无关", "EPIC→CERES 深度学习独立分支（Stage 1-13）", "中", "否", "估算 CERES TOA 辐射通量，与云产品融合无关；代码零引用"),
    ("third_report/outputs/epic_ceres", "无关", "EPIC-CERES 各阶段输出", "大", "否", "epic_ceres 分支产物"),
    ("third_report/outputs/epic_ceres_fullmonth_v2", "无关", "EPIC-CERES 全月 v2 训练输出", "大", "否", "epic_ceres 分支产物"),
    ("third_report/outputs/epic_ceres_ppt_assets", "无关", "EPIC-CERES PPT 素材", "小", "否", "epic_ceres 分支产物"),
    ("third_report/paper_notion_manager", "无关", "论文-Notion 管理工具", "小", "否", "LLM+Notion 管理论文，与气象无关"),
    ("data/3-科大蓝（竞赛 科技）.pptx", "无关", "存于 data/ 下的竞赛演示文稿", "约 43 MB", "否", "与 ring cloud 无关"),
    ("data/A202607010902106154", "无关", "空目录", "0", "否", "空"),
]

# 外部数据盘（不在 D:\AAAresearch_paper 内）
EXTERNAL_DISKS = [
    ("E:\\GEO_Cloud_2024", "E盘", "GOES/Himawari/Meteosat 大规模原始云产品存储根（check_download_completeness.py GEO_ROOT；08b 引用 EPIC L2 CMSAF 子目录）", "是"),
    ("F:\\DSCOVR_EPIC_L2_CLOUD_03_2024.03", "F盘", "DSCOVR EPIC L2 云产品数据（time_runs manifest 中 single_sample_run_manifest.json 引用）", "是"),
]

# ---------------------------------------------------------------------------
# 3. ring cloud 主脚本职责表（third_report/code/geo_ring_cloud_stage1/ 下 41 个 .py）
#    (文件名, 阶段, 职责)
# ---------------------------------------------------------------------------
SCRIPTS = [
    ("stage1_common.py", "公共", "核心共享库：路径常量(STAGE_ROOT/REPORT_ROOT/HIMAWARI_R21_DIR)、标准变量名、cloud_mask 码表(FY4B/GOES/Himawari/Meteosat)、产品读取器、单位转换、quicklook 绘图"),
    ("01_build_core_time_index.py", "01", "从 data_check_report/parsed_file_metadata.csv 构建核心时次索引，按卫星完整度评分，选定原型时次 2024-03-05T00:00Z"),
    ("02_build_standardized_cloud_native.py", "02", "读取各卫星原生云产品，按统一变量名映射标准化为 native-grid NPZ，输出到 standardized_native/"),
    ("03_validate_standardized_cloud_native.py", "03", "校验 02 产物 NPZ 的变量完整性、dtype、shape 一致性"),
    ("03_5_semantic_validation_patch.py", "03.5", "语义校验补丁：分类变量码值范围、fill 值合理性"),
    ("04_check_fy4b_geo_alignment.py", "04", "FY4B GEO 产品与 L2 云产品网格对齐检查（shape/变量/角度范围）"),
    ("04b_fy4b_dqf_bit_decode_diagnostics.py", "04b", "FY4B DQF 质量标志位级解码诊断，生成码值统计"),
    ("05_reproject_cloud_to_grid.py", "05", "各卫星原生网格重投影到统一 0.05° 全球网格(3600x7200)，KD-tree 最近邻；输出 display/fusion valid_mask"),
    ("06_fuse_best_source.py", "06", "变量级 best-source 融合：基于 VZA/view_weight/time_weight 逐像素选最优源；输出融合数据+source_map+rating_map"),
    ("06_5_source_selection_diagnostics.py", "06.5", "源选择诊断：验证融合是否以 min-VZA 逻辑驱动"),
    ("06c_geometry_parameter_audit.py", "06c", "几何参数审计：提取各卫星子午经度/地球半径/轨道高度等"),
    ("06c_multi_satellite_geometry_metadata_audit.py", "06c", "多卫星几何元数据审计（引用 geo_geometry_check + reprojected_grid + standardized_native）"),
    ("06d_himawari_full_disk_geometry_validation.py", "06d", "Himawari 全圆盘几何验证（引用 geo_geometry_check/Himawari-9 与 vza_method_comparison_by_satellite.csv）"),
    ("06e_full_geometry_angle_source_sync_patch.py", "06e", "几何角度源同步补丁：将传感器/太阳角度层投影到目标网格并重跑 06 融合"),
    ("06e_vza_ecef_final_audit.py", "06e", "VZA ECEF 坐标系最终审计（引用 geo_geometry_check）"),
    ("06f_unknown_aware_data_asset_audit.py", "06f", "unknown-aware 数据资产审计：扫描 data/geo_geometry_check/stage1 子目录识别未知变量"),
    ("06f_reexport_with_obitype_patch.py", "06f", "带 orbit type 补丁的重新导出"),
    ("06f_report_sync_patch.py", "06f", "报告同步补丁"),
    ("07_overlap_consistency_validation.py", "07", "重叠区一致性验证 v1：相邻卫星覆盖区 cloud_mask/CTH/CTT 差异（历史版）"),
    ("07p_overlap_validator_hotfix.py", "07p", "重叠验证热修复：修 cloud-mask 映射/angle-layer/分层执行"),
    ("07p_b_source_boundary_magnitude_review.py", "07p-b", "源边界跳变幅度审查"),
    ("07v2_formal_single_time_report.py", "07v2", "正式单时次报告生成：聚合 07p，生成最终验收决策"),
    ("08_epic_visual_comparison.py", "08", "EPIC(DSCOVR) 目视比较：下载 EPIC 图像与融合结果对比"),
    ("08b_epic_l2_cloud_audit_compare.py", "08b", "EPIC L2 云产品审计比较（引用 E:\\GEO_Cloud_2024\\CMSAF 下 EPIC L2 文件）"),
    ("08c_epic_cloud_mask_semantic_sensitivity.py", "08c", "EPIC 云掩膜语义敏感性分析"),
    ("08d_select_epic_monthly_semantic_validation_targets.py", "08d", "选择 EPIC 月度语义验证目标"),
    ("08e_summarize_epic_georing_multisample.py", "08e", "多样本 EPIC Geo-ring 汇总（RUNS_ROOT=time_runs）"),
    ("08f_geometry_and_prefusion_epic_diagnostics.py", "08f", "几何与预融合 EPIC 诊断（RUNS_ROOT=time_runs）"),
    ("08g_overlap_count_diagnostics.py", "08g", "重叠计数诊断（RUNS_ROOT=time_runs）"),
    ("08h_meteosat_time_offset_control_test.py", "08h", "Meteosat 时间偏移控制测试（RUNS_ROOT=time_runs）"),
    ("08i_meteosat_source_overlap_diagnostics.py", "08i", "Meteosat 源重叠诊断（RUNS_ROOT=time_runs）"),
    ("08j_prefusion_source_pair_overlap_diagnostics.py", "08j", "预融合源对重叠诊断（RUNS_ROOT=time_runs）"),
    ("08k_consolidate_stage08_report.py", "08k", "Stage08 报告整合（RUNS_ROOT=time_runs）"),
    ("09_stage09_epic_georing_cloud_mask_diagnostics.py", "09", "Stage09 EPIC-Geo-ring 云掩膜诊断（RUNS_ROOT=time_runs）"),
    ("stage09b_full_overnight/run_stage09b_full_overnight.py", "09b", "Stage09b 全夜批量运行（RUNS_ROOT + BASE_STAGE_ROOT）"),
    ("stage09c_scaled_batch/run_stage09c_scaled_batch.py", "09c", "Stage09c 扩展批量运行（RUNS_ROOT + BASE_STAGE_ROOT）"),
    ("download_geo_geometry_samples.py", "下载", "从 AWS S3 下载 GOES-16/18/Himawari-9 几何样本到 geo_geometry_check/"),
    ("rebuild_stage1_evidence_pack.py", "证据包", "重建 Stage1 证据包：汇总 data_check_report/geo_geometry_check/stage1 全部证据到 evidence_pack/"),
    ("run_epic_georing_single_sample.py", "运行器", "EPIC Geo-ring 单时次完整运行流水线（BASE=stage1, RUNS=time_runs）"),
    ("run_epic_georing_sample_batch.py", "运行器", "EPIC Geo-ring 批量样本运行器（RUNS=time_runs）"),
    ("summarize_time_run_20240319_1500.py", "汇总", "汇总 20240319_1500 时次运行结果"),
]

# ---------------------------------------------------------------------------
# 4. 外部数据路径引用表（亲自 Grep 核实，含行号）
#    (外部路径, 引用脚本, 行号, 用途, 位置)
# ---------------------------------------------------------------------------
EXT_REFS = [
    # stage1_common.py 中的核心路径常量
    (r"D:\AAAresearch_paper\geo_ring_cloud_stage1", "stage1_common.py", 24, "STAGE_ROOT 产物根（可被 GEO_RING_STAGE_ROOT 环境变量覆盖）", "D盘内"),
    (r"D:\AAAresearch_paper\data_check_report", "stage1_common.py", 25, "REPORT_ROOT 前序审计报告根", "D盘内"),
    (r"D:\AAAresearch_paper\data_check_report\geometry_variable_audit", "stage1_common.py", 26, "GEOM_AUDIT_ROOT 几何审计目录", "D盘内"),
    (r"D:\AAAresearch_paper\data\H09_Data", "stage1_common.py", 27, "HIMAWARI_R21_DIR Himawari R21 辅助几何文件", "D盘内"),
    (r"D:\AAAresearch_paper\data_check_report\parsed_file_metadata.csv", "stage1_common.py", 29, "PARSED_METADATA 所有卫星文件元数据索引（01 阶段核心输入）", "D盘内"),
    (r"D:\AAAresearch_paper\data_check_report\geometry_variable_audit\product_variable_inventory_full.csv", "stage1_common.py", 30, "VARIABLE_INVENTORY 产品变量清单", "D盘内"),
    (r"D:\AAAresearch_paper\data_check_report\manual_variable_mapping_by_product.yaml", "stage1_common.py", 31, "MAPPING_YAML 手动变量映射表（02 直接读取）", "D盘内"),
    # download_geo_geometry_samples.py
    (r"D:\AAAresearch_paper\geo_geometry_check", "download_geo_geometry_samples.py", 22, "OUT_ROOT 几何样本下载产物根", "D盘内"),
    # 06c/06d/06e 对 geo_geometry_check 的引用
    (r"D:\AAAresearch_paper\geo_geometry_check", "06c_multi_satellite_geometry_metadata_audit.py", 27, "GEOMETRY_ROOT 几何样本根", "D盘内"),
    (r"D:\AAAresearch_paper\geo_geometry_check\vza_method_comparison_by_satellite.csv", "06d_himawari_full_disk_geometry_validation.py", 28, "CURRENT06C_VZA_CSV 当前 06c VZA 比较结果", "D盘内"),
    (r"D:\AAAresearch_paper\geo_geometry_check\Himawari-9", "06d_himawari_full_disk_geometry_validation.py", 29, "GEOMETRY_ROOT Himawari 全圆盘段数据", "D盘内"),
    (r"D:\AAAresearch_paper\geo_geometry_check", "06e_vza_ecef_final_audit.py", 20, "EXTERNAL_GEOMETRY_AUDIT_DIR 几何审计目录", "D盘内"),
    # 06f 扫描的多个数据目录
    (r"D:\AAAresearch_paper\data", "06f_unknown_aware_data_asset_audit.py", 38, "SCAN_DIRS 原始卫星数据根", "D盘内"),
    (r"D:\AAAresearch_paper\geo_geometry_check", "06f_unknown_aware_data_asset_audit.py", 39, "SCAN_DIRS 几何校验样本", "D盘内"),
    (r"D:\AAAresearch_paper\geo_ring_cloud_stage1\standardized_native", "06f_unknown_aware_data_asset_audit.py", 40, "SCAN_DIRS 标准化原生产物", "D盘内"),
    (r"D:\AAAresearch_paper\geo_ring_cloud_stage1\reprojected_grid", "06f_unknown_aware_data_asset_audit.py", 41, "SCAN_DIRS 重投影产物", "D盘内"),
    (r"D:\AAAresearch_paper\geo_ring_cloud_stage1\fused_best_source", "06f_unknown_aware_data_asset_audit.py", 42, "SCAN_DIRS 融合产物", "D盘内"),
    # rebuild_stage1_evidence_pack.py 汇总的所有证据源
    (r"D:\AAAresearch_paper\geo_ring_cloud_stage1_evidence_pack", "rebuild_stage1_evidence_pack.py", 14, "EVIDENCE_ROOT 证据包输出根", "D盘内"),
    (r"D:\AAAresearch_paper\geo_ring_cloud_stage1", "rebuild_stage1_evidence_pack.py", 15, "STAGE1_ROOT 主产物根（汇总证据来源）", "D盘内"),
    (r"D:\AAAresearch_paper\geo_geometry_check", "rebuild_stage1_evidence_pack.py", 16, "GEOMETRY_ROOT 几何证据来源", "D盘内"),
    (r"D:\AAAresearch_paper\third_report\code\geo_ring_cloud_stage1", "rebuild_stage1_evidence_pack.py", 17, "CODE_ROOT 主代码来源", "D盘内"),
    (r"D:\AAAresearch_paper\data_check_report", "rebuild_stage1_evidence_pack.py", 18, "DATA_CHECK_ROOT 前序审计证据来源", "D盘内"),
    # time_runs 引用
    (r"D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs", "08e_summarize_epic_georing_multisample.py", 16, "RUNS_ROOT 多时次运行根", "D盘内"),
    (r"D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs", "08f_geometry_and_prefusion_epic_diagnostics.py", 14, "RUNS_ROOT 多时次运行根", "D盘内"),
    (r"D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs", "08g_overlap_count_diagnostics.py", 13, "ROOT 多时次运行根", "D盘内"),
    (r"D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs", "08h_meteosat_time_offset_control_test.py", 15, "RUNS_ROOT 多时次运行根", "D盘内"),
    (r"D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs", "08i_meteosat_source_overlap_diagnostics.py", 14, "RUNS_ROOT 多时次运行根", "D盘内"),
    (r"D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs", "08j_prefusion_source_pair_overlap_diagnostics.py", 13, "RUNS_ROOT 多时次运行根", "D盘内"),
    (r"D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs", "08k_consolidate_stage08_report.py", 11, "RUNS_ROOT 多时次运行根", "D盘内"),
    (r"D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs", "09_stage09_epic_georing_cloud_mask_diagnostics.py", 21, "RUNS_ROOT 多时次运行根", "D盘内"),
    (r"D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs\20240319_1500", "08b_epic_l2_cloud_audit_compare.py", 24, "TIME_RUN_ROOT 特定时次运行目录", "D盘内"),
    (r"D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs\epic_202403_target_selection\recommended_epic_georing_validation_targets.csv", "run_epic_georing_sample_batch.py", 15, "DEFAULT_SELECTION_CSV EPIC 验证目标选择清单", "D盘内"),
    # 外部盘 E 盘
    (r"E:\GEO_Cloud_2024\CMSAF\DSCOVR_EPIC_L2_CLOUD_03_20240319150052_03.nc4", "08b_epic_l2_cloud_audit_compare.py", 27, "EPIC_L2_FILE DSCOVR EPIC L2 云产品文件", "E盘"),
    # 外部盘 F 盘（time_runs manifest 引用）
    (r"F:\DSCOVR_EPIC_L2_CLOUD_03_2024.03", "time_runs/*/single_sample_run_manifest.json", 5, "DSCOVR EPIC L2 云产品数据根（各批次 manifest 引用具体 .nc4 文件）", "F盘"),
    # check_download_completeness.py（在 data_check_report，引用 data 与 E 盘）
    (r"D:\AAAresearch_paper\data", "data_check_report/check_download_completeness.py", 17, "DATA_ROOT 原始卫星数据根", "D盘内"),
    (r"E:\GEO_Cloud_2024", "data_check_report/check_download_completeness.py", 18, "GEO_ROOT 外部数据盘 GEO 云产品存储根", "E盘"),
]

# ---------------------------------------------------------------------------
# 5. 流水线阶段表（源自 evidence_sources_by_stage.md + stage_registry.md）
#    (阶段, 名称, 输入, 输出, Gate状态, 证据源目录)
# ---------------------------------------------------------------------------
PIPELINE_STAGES = [
    ("00", "数据下载与完整性审计", "原始卫星数据下载", "data_download_audit_report.md, *_cross_product_match.csv", "Complete", "data_check_report/"),
    ("00b", "一样本一产品真实读取", "样本文件", "manual_variable_mapping_by_product.yaml, one_sample_each_product_*.csv", "Complete", "data_check_report/"),
    ("00c", "几何与变量能力审计", "样本文件", "product_variable_inventory_full.csv, geometry/cloud/quality_flag_capability_matrix.csv", "Complete", "data_check_report/geometry_variable_audit/"),
    ("00d", "Meteosat 产品体系与 catalogue discovery", "Meteosat 目录", "meteosat_product_series_audit.md, meteosat_catalogue_discovery_report.md", "Complete", "data_check_report/meteosat_*/"),
    ("00e", "单时次 v0 标准化原型", "样本", "standardized_cloud_v0_report.md, FY4B_CLM/GOES-16_ACMF/Himawari-9_CMSK.npz", "Complete", "data_check_report/standardized_cloud_v0_samples/"),
    ("00f", "GOES/Meteosat 优先补下载", "缺口清单", "priority_download_goes_meteosat_report.md, meteosat_priority_download_report.md", "Complete", "data_check_report/priority_download_run_*/"),
    ("01", "核心时间索引", "parsed_file_metadata.csv", "core_time_index.csv, usable_times_ranked.csv（原型 2024-03-05T00:00Z）", "PASS", "geo_ring_cloud_stage1/time_index/"),
    ("02", "标准化云原生 NPZ", "01 选定时次 + 原始卫星文件", "standardized_native/*.npz, inventory/stats CSV, quicklook", "PASS", "geo_ring_cloud_stage1/standardized_native/"),
    ("03", "结构校验", "02 的 NPZ", "standardized_native_file_validation.csv, validate report", "PASS", "geo_ring_cloud_stage1/standardized_native/"),
    ("03.5", "语义校验补丁", "02 的 NPZ", "standardized_native_semantic_issues.csv, code_tables.csv", "PASS_WITH_WARNINGS", "geo_ring_cloud_stage1/standardized_native/"),
    ("04", "FY4B GEO 对齐检查", "FY4B NPZ(CLM-GEO)", "fy4b_geo_alignment_*.csv, fy4b_geo_alignment_report.md", "PASS_WITH_WARNINGS", "geo_ring_cloud_stage1/reports/"),
    ("04b", "FY4B DQF 位解码诊断", "FY4B NPZ DQF", "fy4b_dqf_bit_decode_summary.csv, fy4b_quality_flag_rules.yaml", "PASS_WITH_WARNINGS", "geo_ring_cloud_stage1/reports/"),
    ("05", "重投影到 0.05° 网格", "02 NPZ + 04 GEO 对齐", "reprojected_grid/{6卫星}/*.npz, target_grid_definition.json, coverage map", "PASS_WITH_WARNINGS", "geo_ring_cloud_stage1/reprojected_grid/"),
    ("06", "best-source 融合", "05 重投影 NPZ", "fused_geo_ring_cloud_*.npz, source_map/rating_map, fusion_stats.csv", "PASS_WITH_WARNINGS", "geo_ring_cloud_stage1/fused_best_source/"),
    ("06.5", "源选择诊断", "06 融合结果", "selected_vs_min_vza_agreement.csv 等 9 个 CSV", "PASS", "geo_ring_cloud_stage1/source_selection_diagnostics/"),
    ("06c", "多星几何元数据审计", "05 重投影 + geo_geometry_check", "satellite_geometry_parameter_audit.csv, vza_method_comparison.csv", "PASS_WITH_WARNINGS", "geo_geometry_check/ + geometry_audit_06c/"),
    ("06d", "Himawari 全圆盘几何验证", "geo_geometry_check/Himawari-9", "himawari_full_disk_geometry_report.md/audit.csv", "PASS_WITH_WARNINGS", "geo_geometry_check/Himawari-9/"),
    ("06e", "几何角度源同步", "05/06 结果 + 角度层", "angle_provenance_inventory.csv, geometry_angle_source_policy.yaml, 角度层 NPZ", "PASS", "geo_ring_cloud_stage1/geometry_angle_sync_06e/"),
    ("06f", "unknown-aware 数据资产审计", "全部 NPZ 产物", "audit_summary.json, data_asset_audit.sqlite, blocking_issues.csv", "PASS_WITH_WARNINGS", "geo_ring_cloud_stage1/data_asset_audit_06f/"),
    ("07", "重叠一致性验证(原版)", "06 融合结果", "overlap_*.csv, overlap_confusion_matrices.json", "Historical", "geo_ring_cloud_stage1/overlap_validation/"),
    ("07p", "重叠验证修复版", "06e 修正后结果", "*_v2.csv, overlap_validation_07p_report.md", "PASS", "geo_ring_cloud_stage1/overlap_validation_07p/"),
    ("07p-b", "边界跳变幅度审查", "07p 结果", "07p_boundary_magnitude_review.md", "PASS", "geo_ring_cloud_stage1/reports/"),
    ("07v2", "正式单时次报告", "07p 全部结果", "07v2_overlap_consistency_validation_report.md, stage1_single_time_acceptance_decision.md", "PASS_WITH_WARNINGS", "geo_ring_cloud_stage1/reports/"),
    ("08", "EPIC 目视对比(原型时次)", "06 融合 + EPIC 图像", "epic_visual_comparison_report.md, epic_*.csv, 对比图", "—", "geo_ring_cloud_stage1/epic_visual_comparison/"),
    ("08c", "EPIC L2 云掩膜语义敏感性", "time_runs 各批次 + EPIC L2", "epic_l2_cloud_mask_semantic_sensitivity_*/", "—", "geo_ring_cloud_stage1_time_runs/*/"),
    ("09", "Stage09 EPIC-Geo-ring 诊断", "time_runs + EPIC", "stage09 系列诊断", "—", "geo_ring_cloud_stage1_time_runs/epic_202403_*/"),
]

# ---------------------------------------------------------------------------
# 6. time_runs 批次表（48 个时间批次 + 5 个 EPIC 专题，Glob 核实）
# ---------------------------------------------------------------------------
TIME_RUNS = [
    ("20240305_1500",), ("20240306_1100",), ("20240306_1300",), ("20240307_0900",),
    ("20240308_0400",), ("20240308_1300",), ("20240309_0100",), ("20240309_0800",),
    ("20240309_1300",), ("20240309_1700",), ("20240310_1000",), ("20240310_1200",),
    ("20240311_0800",), ("20240311_1000",), ("20240311_1400",), ("20240311_1900",),
    ("20240311_2300",), ("20240312_1500",), ("20240313_0400",), ("20240313_1100",),
    ("20240313_2200",), ("20240314_0900",), ("20240315_0400",), ("20240315_1300",),
    ("20240316_0800",), ("20240316_1700",), ("20240317_1000",), ("20240317_1200",),
    ("20240318_0800",), ("20240318_1000",), ("20240319_1500",), ("20240320_0400",),
    ("20240321_0900",), ("20240321_1100",), ("20240322_0400",), ("20240322_1300",),
    ("20240323_0800",), ("20240324_1200",), ("20240325_0800",), ("20240325_1000",),
    ("20240326_1500",), ("20240327_1100",), ("20240328_0900",), ("20240328_1100",),
    ("20240329_1300",), ("20240330_0800",), ("20240330_1700",), ("20240331_1200",),
    ("20240331_1900",),
]
EPIC_TOPICS = [
    ("epic_202403_batch_runs", "EPIC Geo-ring 批量运行状态与报告"),
    ("epic_202403_meteosat_time_offset_control", "Meteosat 时间偏移控制实验"),
    ("epic_202403_multisample_summary", "多样本 EPIC-Geo-ring 汇总 + Stage08 组会 PPT"),
    ("epic_202403_overnight_watch", "过夜观测"),
    ("epic_202403_target_selection", "EPIC 验证目标选择候选清单"),
]

PROJECT_ID = "geo_ring_cloud"
KEY_ARTIFACT_EXTS = {".md", ".csv", ".json", ".yaml", ".yml", ".xlsx", ".py", ".ps1"}
AGGREGATE_EXTS = {".png", ".npz", ".nc", ".nc4", ".h5", ".hdf", ""}
COMPONENT_ROLES = {
    "公共": "shared_library",
    "运行器": "runner",
    "下载": "downloader",
    "证据包": "evidence_pack_builder",
    "汇总": "summary_helper",
    "": "support",
}
ARTIFACT_STAGE_HINTS = {
    "core_time_index": "stage_01",
    "standardized_native_build": "stage_02",
    "standardized_native_validate": "stage_03",
    "semantic_validation": "stage_03_5",
    "fy4b_geo_alignment": "stage_04",
    "fy4b_dqf": "stage_04b",
    "reproject_cloud_to_grid": "stage_05",
    "fuse_best_source": "stage_06",
    "cloud_mask_fusion": "stage_06",
    "source_selection": "stage_06_5",
    "geometry_parameter": "stage_06c",
    "geometry_metadata": "stage_06c",
    "vza_method": "stage_06c",
    "himawari_full_disk": "stage_06d",
    "angle_source": "stage_06e",
    "unknown_aware": "stage_06f",
    "data_asset_audit": "stage_06f",
    "overlap_validation_07p": "stage_07p",
    "overlap_validation_report": "stage_07",
    "boundary_magnitude": "stage_07p_b",
    "single_time_acceptance": "stage_07v2",
    "epic_visual": "stage_08",
    "epic_l2_cloud": "stage_08b",
    "semantic_sensitivity": "stage_08c",
    "target_selection": "stage_08d",
    "multisample_summary": "stage_08e",
    "geometry_prefusion": "stage_08f",
    "overlap_count": "stage_08g",
    "time_offset": "stage_08h",
    "source_overlap": "stage_08i",
    "prefusion_source_pair": "stage_08j",
    "stage08_epic_comparison": "stage_08k",
    "stage09b": "stage_09b",
    "stage09c": "stage_09c",
    "stage09d": "stage_09d",
    "stage09": "stage_09",
}


def canonical_stage_id(label: str) -> str:
    """Return canonical stage id for a legacy stage label, or empty for non-stage roles."""
    if not label or label in COMPONENT_ROLES:
        return ""
    value = label.strip().lower().replace("stage", "").replace("step", "")
    value = value.replace(".", "_").replace("-", "_")
    return f"stage_{value}" if value.startswith("0") else f"stage_{value.zfill(2)}"


def display_stage(canonical_id: str) -> str:
    if not canonical_id:
        return ""
    value = canonical_id.replace("stage_", "")
    return "Stage " + value.replace("_", ".")


def stage_sort_key(canonical_id: str) -> str:
    return canonical_id.replace("stage_", "")


def legacy_labels_for(label: str, script_name: str = "") -> str:
    if not label:
        return ""
    labels = {label}
    compact = label.replace(".", "").replace("-", "")
    labels.add(f"Stage{label}")
    labels.add(f"stage{label}")
    labels.add(f"Stage{compact}")
    labels.add(f"stage{compact}")
    labels.add(f"stage_{label.replace('.', '_').replace('-', '_')}")
    if script_name:
        labels.add(script_name)
    return ",".join(sorted(labels))


def classify_artifact(path: Path) -> str:
    if path.is_dir():
        return "directory_summary"
    ext = path.suffix.lower()
    if ext == ".py":
        return "script"
    if ext == ".ps1":
        return "script_helper"
    if ext == ".md":
        return "report"
    if ext == ".csv":
        return "table"
    if ext in (".json", ".yaml", ".yml"):
        return "config_or_manifest"
    if ext == ".xlsx":
        return "workbook"
    return "other"


def infer_canonical_from_path(path: Path) -> tuple[str, str, str]:
    """Infer project, canonical stage, and legacy stage label from a path."""
    text = str(path).replace("\\", "/").lower()
    name = path.name.lower()
    if "_non_geo_archive" in text and "epic_ceres" in text:
        if "stage9_5" in text or "stage_9_5" in text or "run_stage_9_5" in text:
            return "epic_ceres", "stage_09_5", "epic_ceres_stage9_5"
        if "stage9" in text or "run_stage_6_9" in text:
            return "epic_ceres", "stage_09", "epic_ceres_stage9"
        return "epic_ceres", "", ""
    if "geo_ring_cloud_stage1_time_runs/stage09d" in text or "stage09d" in name:
        return PROJECT_ID, "stage_09d", "stage09d"
    if "geo_ring_cloud_stage1_time_runs/stage09c" in text or "stage09c" in name:
        return PROJECT_ID, "stage_09c", "stage09c"
    if "geo_ring_cloud_stage1_time_runs/stage09b" in text or "stage09b" in name:
        return PROJECT_ID, "stage_09b", "stage09b"
    if "stage09_epic_georing_cloud_mask_diagnostics" in text or "stage09_" in name:
        return PROJECT_ID, "stage_09", "stage09"
    for token, canonical in ARTIFACT_STAGE_HINTS.items():
        if token in text:
            return PROJECT_ID, canonical, token
    if "/pipeline_stages/" in text:
        for stage, _, _, _, _, _ in PIPELINE_STAGES:
            compact = stage.replace(".", "_").replace("-", "_")
            if f"/{compact}_" in text or f"/stage_{compact}" in text:
                return PROJECT_ID, canonical_stage_id(stage), stage
    for stage, _, _, _, _, _ in PIPELINE_STAGES:
        canonical = canonical_stage_id(stage)
        if not canonical:
            continue
        token = canonical.replace("stage_", "")
        candidates = {
            f"/{token}_",
            f"/{token.replace('_', '.')}_",
            f"stage{token.replace('_', '')}",
            f"_{token}_",
            f"_{token.replace('_', '.')}_",
        }
        if any(c in text for c in candidates):
            return PROJECT_ID, canonical, stage
    return PROJECT_ID, "", ""

# ---------------------------------------------------------------------------
# 7. 主流程：扫描 → 建 sqlite → 导出 xlsx
# ---------------------------------------------------------------------------

def create_schema(conn):
    cur = conn.cursor()
    cur.executescript("""
    DROP TABLE IF EXISTS directories;
    DROP TABLE IF EXISTS files;
    DROP TABLE IF EXISTS scripts;
    DROP TABLE IF EXISTS external_data_refs;
    DROP TABLE IF EXISTS pipeline_stages;
    DROP TABLE IF EXISTS time_runs;
    DROP TABLE IF EXISTS external_disks;
    DROP TABLE IF EXISTS stage_registry;
    DROP TABLE IF EXISTS stage_aliases;
    DROP TABLE IF EXISTS artifact_index;
    DROP TABLE IF EXISTS naming_violations;
    DROP TABLE IF EXISTS meta;

    CREATE TABLE directories (
        id INTEGER PRIMARY KEY,
        path TEXT, name TEXT, parent TEXT,
        relevance TEXT, role TEXT,
        file_count INTEGER, size_bytes INTEGER, size_text TEXT,
        ext_summary TEXT, referenced_by_code TEXT, note TEXT,
        last_seen TEXT, exists_now TEXT, move_candidate TEXT, path_risk TEXT
    );
    CREATE TABLE files (
        id INTEGER PRIMARY KEY,
        path TEXT, name TEXT, dir_path TEXT,
        ext TEXT, category TEXT, stage TEXT, relevance TEXT,
        size_bytes INTEGER, note TEXT
    );
    CREATE TABLE scripts (
        id INTEGER PRIMARY KEY,
        path TEXT, filename TEXT, stage TEXT, project_id TEXT,
        canonical_stage_id TEXT, component_role TEXT, legacy_stage TEXT, responsibility TEXT,
        refs_external_paths TEXT
    );
    CREATE TABLE external_data_refs (
        id INTEGER PRIMARY KEY,
        external_path TEXT, referenced_by_script TEXT, line_no TEXT,
        purpose TEXT, location TEXT
    );
    CREATE TABLE pipeline_stages (
        id INTEGER PRIMARY KEY,
        stage TEXT, name TEXT, input TEXT, output TEXT,
        gate_status TEXT, evidence_dir TEXT
    );
    CREATE TABLE time_runs (
        id INTEGER PRIMARY KEY,
        run_id TEXT, kind TEXT, target_utc TEXT, note TEXT
    );
    CREATE TABLE external_disks (
        id INTEGER PRIMARY KEY,
        path TEXT, location TEXT, description TEXT, referenced TEXT
    );
    CREATE TABLE stage_registry (
        id INTEGER PRIMARY KEY,
        project_id TEXT,
        canonical_stage_id TEXT,
        display_label TEXT,
        legacy_labels TEXT,
        component_role TEXT,
        stage_order TEXT,
        name TEXT,
        meaning TEXT,
        input TEXT,
        output TEXT,
        status TEXT,
        evidence_paths TEXT,
        do_not_merge_with TEXT,
        notes TEXT,
        UNIQUE(project_id, canonical_stage_id)
    );
    CREATE TABLE stage_aliases (
        id INTEGER PRIMARY KEY,
        project_id TEXT,
        alias TEXT,
        canonical_stage_id TEXT,
        confidence REAL,
        reason TEXT
    );
    CREATE TABLE artifact_index (
        id INTEGER PRIMARY KEY,
        project_id TEXT,
        canonical_stage_id TEXT,
        component_role TEXT,
        artifact_type TEXT,
        path TEXT,
        legacy_stage_label TEXT,
        role TEXT,
        size_bytes INTEGER,
        file_count INTEGER,
        ext_summary TEXT,
        last_modified TEXT,
        summary TEXT,
        trace_source TEXT
    );
    CREATE TABLE naming_violations (
        id INTEGER PRIMARY KEY,
        project_id TEXT,
        path TEXT,
        legacy_label TEXT,
        issue_type TEXT,
        severity TEXT,
        suggested_canonical_stage_id TEXT,
        suggested_new_path TEXT,
        reason TEXT
    );
    CREATE TABLE meta (
        key TEXT PRIMARY KEY, value TEXT
    );
    CREATE INDEX idx_dirs_rel ON directories(relevance);
    CREATE INDEX idx_files_rel ON files(relevance);
    CREATE INDEX idx_files_cat ON files(category);
    CREATE INDEX idx_extref_loc ON external_data_refs(location);
    CREATE INDEX idx_stage_registry_project ON stage_registry(project_id);
    CREATE INDEX idx_artifact_stage ON artifact_index(project_id, canonical_stage_id);
    CREATE INDEX idx_alias_lookup ON stage_aliases(project_id, alias);
    """)
    conn.commit()


def categorize(name: str, ext: str) -> str:
    e = ext.lower()
    if e in (".py",):
        return "code"
    if e in (".ipynb",):
        return "notebook"
    if e in (".yaml", ".yml", ".json", ".toml", ".ini", ".cfg"):
        return "config"
    if e in (".md", ".rst", ".txt"):
        return "report"
    if e in (".csv",):
        return "table"
    if e in (".png", ".jpg", ".jpeg", ".svg", ".pdf"):
        return "quicklook"
    if e in (".npz", ".npy"):
        return "data_npz"
    if e in (".nc", ".nc4", ".hdf", ".h5", ".nat", ".dat", ".bz2", ".zip", ".rar", ".sqlite", ".db"):
        return "data"
    return "other"


def scan_files_in_dir(dirpath: Path, relevance: str, stage: str, conn):
    """全量入库一个目录下的所有文件（用于小文件目录：code/config/report/manifest）。"""
    cur = conn.cursor()
    if not dirpath.exists():
        return
    for dirpath_, dirnames, filenames in os.walk(dirpath):
        dirnames[:] = [d for d in dirnames if d not in
                       ("__pycache__", "node_modules", ".git", ".claude", "dist")]
        for f in filenames:
            if f.startswith("~$"):
                continue
            fp = Path(dirpath_) / f
            try:
                sz = fp.stat().st_size
            except OSError:
                sz = None
            ext = fp.suffix.lower()
            cat = categorize(f, ext)
            cur.execute(
                "INSERT INTO files(path,name,dir_path,ext,category,stage,relevance,size_bytes,note) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (str(fp), f, str(dirpath_), ext, cat, stage, relevance, sz, ""),
            )
    conn.commit()


def summary_in_dir(dirpath: Path):
    """只统计不逐文件入库（用于海量数据目录：返回文件数/体积/扩展名汇总）。"""
    n, b, exts = dir_stats(dirpath)
    ext_summary = ", ".join(f"{k}:{v}" for k, v in sorted(exts.items(), key=lambda x: -x[1])[:6])
    return n, b, ext_summary


def insert_stage_registry(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    rows: dict[tuple[str, str], dict[str, str]] = {}

    for stage, name, inp, outp, gate, evidence in PIPELINE_STAGES:
        canonical = canonical_stage_id(stage)
        rows[(PROJECT_ID, canonical)] = {
            "project_id": PROJECT_ID,
            "canonical_stage_id": canonical,
            "display_label": display_stage(canonical),
            "legacy_labels": legacy_labels_for(stage),
            "component_role": "",
            "stage_order": stage_sort_key(canonical),
            "name": name,
            "meaning": name,
            "input": inp,
            "output": outp,
            "status": gate,
            "evidence_paths": evidence,
            "do_not_merge_with": "epic_ceres.stage_09,epic_ceres.stage_09_5" if canonical == "stage_09" else "",
            "notes": "Canonical GEO-ring Cloud pipeline stage",
        }

    for fname, stage, responsibility in SCRIPTS:
        canonical = canonical_stage_id(stage)
        if canonical:
            key = (PROJECT_ID, canonical)
            existing = rows.get(key)
            if existing:
                existing["legacy_labels"] = ",".join(sorted(set((existing["legacy_labels"] + "," + legacy_labels_for(stage, fname)).split(","))))
                if fname not in existing["evidence_paths"]:
                    existing["evidence_paths"] = (existing["evidence_paths"] + "," + fname).strip(",")
            else:
                rows[key] = {
                    "project_id": PROJECT_ID,
                    "canonical_stage_id": canonical,
                    "display_label": display_stage(canonical),
                    "legacy_labels": legacy_labels_for(stage, fname),
                    "component_role": "",
                    "stage_order": stage_sort_key(canonical),
                    "name": responsibility.split("：", 1)[0],
                    "meaning": responsibility,
                    "input": "",
                    "output": "",
                    "status": "",
                    "evidence_paths": fname,
                    "do_not_merge_with": "epic_ceres.stage_09,epic_ceres.stage_09_5" if canonical == "stage_09" else "",
                    "notes": "Script-derived canonical stage not present in original pipeline table",
                }

    # Current Stage09d code was discovered from the file system and must be first-class in the registry.
    for canonical, name, evidence in [
        ("stage_09b", "Stage 09b full overnight diagnostics", "geo_ring_cloud_stage1_time_runs/stage09b_full_202403_overnight_diagnostics"),
        ("stage_09c", "Stage 09c scaled March batch", "geo_ring_cloud_stage1_time_runs/stage09c_scaled_202403_batch"),
        ("stage_09d", "Stage 09d full-pixel diagnostics and interpretation", "geo_ring_cloud_stage1_time_runs/stage09d_full_pixel_diagnostics_202403"),
    ]:
        key = (PROJECT_ID, canonical)
        rows.setdefault(key, {
            "project_id": PROJECT_ID,
            "canonical_stage_id": canonical,
            "display_label": display_stage(canonical),
            "legacy_labels": canonical.replace("stage_", "stage") + "," + canonical.replace("stage_", "Stage"),
            "component_role": "",
            "stage_order": stage_sort_key(canonical),
            "name": name,
            "meaning": name,
            "input": "Stage09 / time_runs outputs",
            "output": evidence,
            "status": "",
            "evidence_paths": evidence,
            "do_not_merge_with": "epic_ceres.stage_09,epic_ceres.stage_09_5",
            "notes": "Current filesystem-derived Stage09 extension",
        })
        rows[key]["do_not_merge_with"] = "epic_ceres.stage_09,epic_ceres.stage_09_5"
        if evidence not in rows[key]["evidence_paths"]:
            rows[key]["evidence_paths"] = (rows[key]["evidence_paths"] + "," + evidence).strip(",")

    code_root = ROOT / "third_report/code/geo_ring_cloud_stage1"
    if code_root.exists():
        for fp in sorted(code_root.rglob("*.py")):
            if "__pycache__" in fp.parts:
                continue
            rel_name = fp.relative_to(code_root).as_posix()
            stage = infer_stage_from_name(rel_name)
            canonical = canonical_stage_id(stage)
            if not canonical:
                continue
            key = (PROJECT_ID, canonical)
            if key not in rows:
                continue
            labels = set(filter(None, rows[key]["legacy_labels"].split(",")))
            labels.add(rel_name)
            rows[key]["legacy_labels"] = ",".join(sorted(labels))
            if rel_name not in rows[key]["evidence_paths"]:
                rows[key]["evidence_paths"] = (rows[key]["evidence_paths"] + "," + rel_name).strip(",")

    for project_id, canonical, name, legacy, notes in [
        ("epic_ceres", "stage_09", "EPIC-CERES Stage 09 statistics", "Stage9,stage9,Step9", "Collision guard only; not part of GEO-ring Cloud"),
        ("epic_ceres", "stage_09_5", "EPIC-CERES Stage 09.5 audit", "Stage9.5,stage9_5,run_stage_9_5_audit.py", "Collision guard only; not part of GEO-ring Cloud"),
    ]:
        rows[(project_id, canonical)] = {
            "project_id": project_id,
            "canonical_stage_id": canonical,
            "display_label": display_stage(canonical),
            "legacy_labels": legacy,
            "component_role": "",
            "stage_order": stage_sort_key(canonical),
            "name": name,
            "meaning": name,
            "input": "",
            "output": "",
            "status": "archived",
            "evidence_paths": str(ARCHIVE_DIR / "third_report"),
            "do_not_merge_with": "geo_ring_cloud.stage_09,geo_ring_cloud.stage_09b,geo_ring_cloud.stage_09c,geo_ring_cloud.stage_09d",
            "notes": notes,
        }

    for row in rows.values():
        cur.execute(
            "INSERT INTO stage_registry(project_id,canonical_stage_id,display_label,legacy_labels,component_role,stage_order,name,meaning,input,output,status,evidence_paths,do_not_merge_with,notes) "
            "VALUES(:project_id,:canonical_stage_id,:display_label,:legacy_labels,:component_role,:stage_order,:name,:meaning,:input,:output,:status,:evidence_paths,:do_not_merge_with,:notes)",
            row,
        )

    for row in rows.values():
        if not row["canonical_stage_id"]:
            continue
        aliases = [a for a in row["legacy_labels"].split(",") if a]
        aliases += [row["canonical_stage_id"], f"{row['project_id']}.{row['canonical_stage_id']}"]
        for alias in sorted(set(aliases)):
            cur.execute(
                "INSERT INTO stage_aliases(project_id,alias,canonical_stage_id,confidence,reason) VALUES(?,?,?,?,?)",
                (row["project_id"], alias, row["canonical_stage_id"], 1.0, "registry-defined alias"),
            )
    conn.commit()


def insert_artifact(conn: sqlite3.Connection, path: Path, role: str, trace_source: str, summary: str = "") -> None:
    cur = conn.cursor()
    project_id, canonical, legacy = infer_canonical_from_path(path)
    component_role = "" if canonical else COMPONENT_ROLES.get(legacy, "")
    try:
        stat = path.stat()
        size = stat.st_size if path.is_file() else None
        modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
    except OSError:
        size = None
        modified = ""
    file_count = None
    ext_summary = ""
    if path.is_dir():
        file_count, size, ext_summary = summary_in_dir(path)
    cur.execute(
        "INSERT INTO artifact_index(project_id,canonical_stage_id,component_role,artifact_type,path,legacy_stage_label,role,size_bytes,file_count,ext_summary,last_modified,summary,trace_source) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            project_id,
            canonical,
            component_role,
            classify_artifact(path),
            str(path),
            legacy,
            role,
            size,
            file_count,
            ext_summary,
            modified,
            summary or path.name,
            trace_source,
        ),
    )


def insert_artifacts(conn: sqlite3.Connection) -> None:
    roots = [
        (ROOT / "third_report/code/geo_ring_cloud_stage1", "source_code", "code root"),
        (ROOT / "geo_ring_cloud_stage1/reports", "stage_report", "stage1 reports"),
        (ROOT / "geo_ring_cloud_stage1/config", "config", "stage1 config"),
        (ROOT / "geo_ring_cloud_stage1/time_index", "table", "stage1 time index"),
        (ROOT / "geo_ring_cloud_stage1_evidence_pack/latest", "evidence", "latest evidence pack"),
    ]
    time_runs = ROOT / "geo_ring_cloud_stage1_time_runs"
    if time_runs.exists():
        for d in sorted(time_runs.iterdir()):
            if d.is_dir() and d.name.lower().startswith("stage09"):
                roots.append((d, "stage09_artifact_root", "time_runs stage09 extension"))
                for sub in sorted(d.rglob("*")):
                    if sub.is_dir() and sub.name != "_tmp":
                        insert_artifact(conn, sub, "directory_summary", "directory aggregate")
                    elif sub.is_dir() and sub.name == "_tmp":
                        insert_artifact(conn, sub, "temporary_directory_aggregate", "large tmp aggregate")

    for root, role, trace in roots:
        if not root.exists():
            continue
        insert_artifact(conn, root, "directory_summary" if root.is_dir() else role, trace)
        if root.is_file():
            continue
        for fp in sorted(root.rglob("*")):
            if not fp.is_file():
                continue
            if "__pycache__" in fp.parts:
                continue
            if fp.suffix.lower() in KEY_ARTIFACT_EXTS:
                insert_artifact(conn, fp, role, trace)
    conn.commit()


def insert_naming_violations(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    for project_id, path, legacy, issue, severity, suggested, new_path, reason in [
        (PROJECT_ID, str(ROOT / "research_tracker"), "Step*/Stage* inferred labels", "untrusted_tracker_stage_inference", "warning", "", "", "research_tracker regex labels are not canonical taxonomy"),
        (PROJECT_ID, str(ROOT / "third_report/code/geo_ring_cloud_stage1"), "公共/运行器/下载/证据包/汇总", "component_role_misused_as_stage", "info", "", "", "These are component roles, not pipeline stages"),
        ("epic_ceres", str(ARCHIVE_DIR / "third_report/code/epic_ceres"), "Stage9/Stage9.5", "cross_project_stage_collision", "warning", "epic_ceres.stage_09 / epic_ceres.stage_09_5", "", "Do not merge archived EPIC-CERES stages with GEO-ring Cloud Stage09"),
    ]:
        cur.execute(
            "INSERT INTO naming_violations(project_id,path,legacy_label,issue_type,severity,suggested_canonical_stage_id,suggested_new_path,reason) VALUES(?,?,?,?,?,?,?,?)",
            (project_id, path, legacy, issue, severity, suggested, new_path, reason),
        )

    tracker_db = ROOT / "research_tracker" / "research_tracker.db"
    if tracker_db.exists():
        try:
            tc = sqlite3.connect(tracker_db)
            for stage, count in tc.execute("select stage,count(*) from files where stage like 'Step%' or stage like 'BuildStep%' or stage='Stage9' group by stage"):
                cur.execute(
                    "INSERT INTO naming_violations(project_id,path,legacy_label,issue_type,severity,suggested_canonical_stage_id,suggested_new_path,reason) VALUES(?,?,?,?,?,?,?,?)",
                    (PROJECT_ID, str(tracker_db), stage, "legacy_tracker_label", "warning", "", "", f"Observed {count} files with non-canonical tracker label; requires project-aware review"),
                )
            tc.close()
        except sqlite3.Error as exc:
            cur.execute(
                "INSERT INTO naming_violations(project_id,path,legacy_label,issue_type,severity,suggested_canonical_stage_id,suggested_new_path,reason) VALUES(?,?,?,?,?,?,?,?)",
                (PROJECT_ID, str(tracker_db), "", "tracker_db_unreadable", "warning", "", "", str(exc)),
            )
    conn.commit()


def build_sqlite():
    db_path = DB_PATH
    try:
        conn = sqlite3.connect(db_path)
        create_schema(conn)
    except sqlite3.Error:
        try:
            conn.close()
        except Exception:
            pass
        db_path = REFRESHED_DB_PATH
        if db_path.exists():
            db_path.unlink()
        conn = sqlite3.connect(db_path)
        create_schema(conn)
    cur = conn.cursor()

    # --- directories 表 ---
    for rel, relevance, role, size_note, refby, note in TOP_DIRS:
        full = ROOT / rel
        exists_now = full.exists()
        if full.is_file():
            # 单文件条目（如 pptx）
            try:
                n, b, ext_sum = 1, full.stat().st_size, full.suffix.lower()
            except OSError:
                n, b, ext_sum = 1, 0, ""
            parent = str(full.parent)
        else:
            n, b, ext_sum = summary_in_dir(full)
            parent = str(full.parent) if full.exists() else ""
        cur.execute(
            "INSERT INTO directories(path,name,parent,relevance,role,file_count,size_bytes,size_text,ext_summary,referenced_by_code,note,last_seen,exists_now,move_candidate,path_risk) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(full),
                full.name,
                parent,
                relevance,
                role,
                n,
                b,
                fmt_size(b) if b else size_note,
                ext_sum,
                refby,
                note,
                GENERATED_AT if exists_now else "",
                bool_text(exists_now),
                move_candidate(relevance, refby, exists_now),
                path_risk(relevance, refby, exists_now),
            ),
        )
    conn.commit()

    # --- files 表：全量入库小文件目录（代码/配置/报告/清单），数据目录只入库其下的脚本与文档 ---
    # 1) ring cloud 主代码（全量）
    scan_files_in_dir(ROOT / "third_report/code/geo_ring_cloud_stage1", "强相关", "", conn)
    # 2) geo_ring_cloud_stage1 产物根下的脚本/配置/报告/清单（全量，但跳过海量 npz/png 子目录的逐文件）
    #    此处只入库该目录直接子层及 reports/ scripts/ config/ time_index/ standardized_native 里的非数据小文件
    for sub in ["scripts", "config", "reports"]:
        d = ROOT / "geo_ring_cloud_stage1" / sub
        scan_files_in_dir(d, "强相关", "", conn)
    for sub in ["time_index"]:
        d = ROOT / "geo_ring_cloud_stage1" / sub
        scan_files_in_dir(d, "强相关", "01", conn)
    # 3) data_check_report 全量（都是审计报告/csv/yaml，小文件）
    scan_files_in_dir(ROOT / "data_check_report", "强相关", "00", conn)
    # 4) geo_geometry_check 全量（报告/csv + 少量样本）
    scan_files_in_dir(ROOT / "geo_geometry_check", "强相关", "06c", conn)
    # 5) evidence_pack 全量（md/json/csv/yaml）
    scan_files_in_dir(ROOT / "geo_ring_cloud_stage1_evidence_pack", "强相关", "", conn)
    # 6) third_report/code 上游模块（全量小文件）
    for sub in ["L1g", "FY4B", "GOES", "Himawari", "Meteosat", "geo_data_audit",
                "geo_cloud_download", "priority_download_goes_meteosat", "preview_baselines"]:
        scan_files_in_dir(ROOT / "third_report/code" / sub, "上游相关", "", conn)
    for f in ["standardized_l1_source_satpy.py", "run_standardized_l1_source_batch.py",
              "validate_standardized_l1_source_samples.py", "preview_runner.py",
              "plot_geo_satellite_coverage.py", "README.md",
              "project_progress_summary_20260505.md", "project_progress_summary_20260511.md"]:
        fp = ROOT / "third_report/code" / f
        if fp.is_file():
            cur.execute(
                "INSERT INTO files(path,name,dir_path,ext,category,stage,relevance,size_bytes,note) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (str(fp), f, str(fp.parent), fp.suffix.lower(), categorize(f, fp.suffix.lower()),
                 "", "上游相关", fp.stat().st_size, ""))
    conn.commit()

    # --- scripts 表 ---
    code_root = ROOT / "third_report/code/geo_ring_cloud_stage1"
    # 计算每个脚本引用的外部路径
    refs_by_script = {}
    for ext_path, script, lineno, purpose, loc in EXT_REFS:
        refs_by_script.setdefault(script, []).append(ext_path)
    for fname, stage, resp in SCRIPTS:
        fp = code_root / fname
        refs = ",".join(sorted(set(refs_by_script.get(fname, []))))
        canonical = canonical_stage_id(stage)
        component_role = "" if canonical else COMPONENT_ROLES.get(stage, "support")
        cur.execute(
            "INSERT INTO scripts(path,filename,stage,project_id,canonical_stage_id,component_role,legacy_stage,responsibility,refs_external_paths) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (str(fp), fname, stage, PROJECT_ID, canonical, component_role, stage, resp, refs))
    known_script_names = {fname.replace("\\", "/") for fname, _, _ in SCRIPTS}
    if code_root.exists():
        for fp in sorted(code_root.rglob("*.py")):
            if "__pycache__" in fp.parts:
                continue
            rel_name = fp.relative_to(code_root).as_posix()
            if rel_name in known_script_names:
                continue
            stage = infer_stage_from_name(rel_name)
            canonical = canonical_stage_id(stage)
            component_role = "" if canonical else "support"
            cur.execute(
                "INSERT INTO scripts(path,filename,stage,project_id,canonical_stage_id,component_role,legacy_stage,responsibility,refs_external_paths) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (str(fp), rel_name, stage, PROJECT_ID, canonical, component_role, stage, summarize_script(rel_name), ""))
    conn.commit()

    # --- external_data_refs 表 ---
    for ext_path, script, lineno, purpose, loc in EXT_REFS:
        cur.execute(
            "INSERT INTO external_data_refs(external_path,referenced_by_script,line_no,purpose,location) "
            "VALUES(?,?,?,?,?)",
            (ext_path, script, str(lineno), purpose, loc))
    # 外部盘
    for path, loc, desc, refby in EXTERNAL_DISKS:
        cur.execute(
            "INSERT INTO external_disks(path,location,description,referenced) VALUES(?,?,?,?)",
            (path, loc, desc, refby))
    conn.commit()

    # --- pipeline_stages 表 ---
    for stage, name, inp, outp, gate, edir in PIPELINE_STAGES:
        cur.execute(
            "INSERT INTO pipeline_stages(stage,name,input,output,gate_status,evidence_dir) "
            "VALUES(?,?,?,?,?,?)",
            (stage, name, inp, outp, gate, edir))
    conn.commit()

    # --- time_runs 表 ---
    for (rid,) in TIME_RUNS:
        # rid 如 20240308_0400 -> 2024-03-08T04:00Z
        try:
            d, h = rid.split("_")
            utc = f"{d[0:4]}-{d[4:6]}-{d[6:8]}T{h[0:2]}:{h[2:4]}Z"
        except Exception:
            utc = ""
        cur.execute("INSERT INTO time_runs(run_id,kind,target_utc,note) VALUES(?,?,?,?)",
                    (rid, "时间批次", utc, ""))
    for tid, desc in EPIC_TOPICS:
        cur.execute("INSERT INTO time_runs(run_id,kind,target_utc,note) VALUES(?,?,?,?)",
                    (tid, "EPIC专题", "", desc))
    conn.commit()

    # --- canonical taxonomy / memory index ---
    insert_stage_registry(conn)
    insert_artifacts(conn)
    insert_naming_violations(conn)

    # --- meta 表 ---
    ts = GENERATED_AT
    meta = {
        "generated_at": ts,
        "project_root": str(ROOT),
        "schema_version": "2.0",
        "source_note": "由 build_index.py 基于人工相关性标签 + 当前文件系统扫描 + canonical taxonomy 生成；不移动项目数据",
        "task_name": "GEO-ring Cloud Stage1（多静止卫星统一云产品融合）",
        "prototype_time": "2024-03-05T00:00:00Z",
        "topology": "主代码 third_report/code/geo_ring_cloud_stage1 → 产物 geo_ring_cloud_stage1 → 多时次 time_runs → 证据包 evidence_pack",
        "workspace_dir": str(WORKSPACE_DIR),
        "archive_dir": str(ARCHIVE_DIR),
    }
    for k, v in meta.items():
        cur.execute("INSERT INTO meta(key,value) VALUES(?,?)", (k, v))
    conn.commit()
    conn.close()
    print(f"[OK] sqlite 写入: {db_path}")
    return db_path


def export_xlsx(db_path: Path = DB_PATH):
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    conn = sqlite3.connect(db_path)
    wb = openpyxl.Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="4472C4")
    wrap = Alignment(wrap_text=True, vertical="top")

    sheets = {
        "Canonical阶段注册表": "SELECT project_id,canonical_stage_id,display_label,name,status,legacy_labels,evidence_paths,do_not_merge_with,notes FROM stage_registry ORDER BY project_id,stage_order,canonical_stage_id",
        "阶段别名": "SELECT project_id,alias,canonical_stage_id,confidence,reason FROM stage_aliases ORDER BY project_id,alias",
        "产物索引": "SELECT project_id,canonical_stage_id,component_role,artifact_type,path,legacy_stage_label,role,size_bytes,file_count,ext_summary,last_modified,summary FROM artifact_index ORDER BY project_id,canonical_stage_id,artifact_type,path",
        "命名问题": "SELECT project_id,path,legacy_label,issue_type,severity,suggested_canonical_stage_id,suggested_new_path,reason FROM naming_violations ORDER BY severity DESC,project_id,path",
        "目录相关性": "SELECT path,relevance,role,file_count,size_text,referenced_by_code,exists_now,move_candidate,path_risk,last_seen,note FROM directories ORDER BY CASE relevance WHEN '强相关' THEN 1 WHEN '上游相关' THEN 2 WHEN '弱相关' THEN 3 ELSE 4 END, path",
        "文件清单": "SELECT path,name,ext,category,stage,relevance,size_bytes FROM files ORDER BY relevance,category,path",
        "脚本职责": "SELECT path,project_id,canonical_stage_id,component_role,legacy_stage,responsibility,refs_external_paths FROM scripts ORDER BY project_id,canonical_stage_id,component_role,path",
        "外部数据依赖": "SELECT external_path,referenced_by_script,line_no,purpose,location FROM external_data_refs ORDER BY location,referenced_by_script",
        "外部数据盘": "SELECT path,location,description,referenced FROM external_disks",
        "流水线阶段": "SELECT stage,name,input,output,gate_status,evidence_dir FROM pipeline_stages ORDER BY CAST(stage AS REAL), stage",
        "时间运行批次": "SELECT run_id,kind,target_utc,note FROM time_runs ORDER BY run_id",
        "元信息": "SELECT key,value FROM meta",
    }
    # 删除默认 sheet
    wb.remove(wb.active)
    for title, sql in sheets.items():
        ws = wb.create_sheet(title=title)
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description]
        ws.append(cols)
        for c in ws[1]:
            c.font = header_font
            c.fill = header_fill
            c.alignment = wrap
        for row in cur.fetchall():
            ws.append(list(row))
        # 列宽自适应（粗略）
        for i, col in enumerate(cols, 1):
            maxlen = max([len(str(col))] + [len(str(r[i-1])) if r[i-1] is not None else 0 for r in [cols]] )
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = min(max(maxlen * 1.6, 10), 60)
        ws.freeze_panes = "A2"
    conn.close()
    wb.save(XLSX_PATH)
    print(f"[OK] xlsx 导出: {XLSX_PATH}")


def fetch_dicts(conn: sqlite3.Connection, sql: str) -> list[dict[str, object]]:
    cur = conn.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def write_markdown_table(path: Path, rows: list[dict[str, object]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join(["---"] * len(columns)) + " |",
    ]
    for row in rows:
        vals = []
        for col in columns:
            text = str(row.get(col, "") or "").replace("\n", " ").replace("|", "\\|")
            vals.append(text)
        lines.append("| " + " | ".join(vals) + " |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def export_workspace_reports(db_path: Path = DB_PATH) -> None:
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    dirs = fetch_dicts(
        conn,
        "SELECT path,relevance,role,file_count,size_text,referenced_by_code,exists_now,move_candidate,path_risk,note "
        "FROM directories ORDER BY CASE relevance WHEN '强相关' THEN 1 WHEN '上游相关' THEN 2 WHEN '弱相关' THEN 3 ELSE 4 END, path",
    )
    scripts = fetch_dicts(
        conn,
        "SELECT filename,project_id,canonical_stage_id,component_role,legacy_stage,responsibility,refs_external_paths FROM scripts ORDER BY project_id,canonical_stage_id,component_role,filename",
    )
    stages = fetch_dicts(
        conn,
        "SELECT stage,name,input,output,gate_status,evidence_dir FROM pipeline_stages ORDER BY id",
    )
    refs = fetch_dicts(
        conn,
        "SELECT external_path,referenced_by_script,line_no,purpose,location FROM external_data_refs ORDER BY location,referenced_by_script",
    )
    registry = fetch_dicts(
        conn,
        "SELECT project_id,canonical_stage_id,display_label,name,status,legacy_labels,evidence_paths,do_not_merge_with,notes FROM stage_registry ORDER BY project_id,stage_order,canonical_stage_id",
    )
    aliases = fetch_dicts(
        conn,
        "SELECT project_id,alias,canonical_stage_id,confidence,reason FROM stage_aliases ORDER BY project_id,alias",
    )
    artifacts = fetch_dicts(
        conn,
        "SELECT project_id,canonical_stage_id,component_role,artifact_type,path,legacy_stage_label,role,file_count,ext_summary,summary FROM artifact_index ORDER BY project_id,canonical_stage_id,artifact_type,path",
    )
    violations = fetch_dicts(
        conn,
        "SELECT project_id,path,legacy_label,issue_type,severity,suggested_canonical_stage_id,suggested_new_path,reason FROM naming_violations ORDER BY severity DESC,project_id,path",
    )
    conn.close()

    readme = f"""# GEO-ring Cloud Workspace

Generated: `{GENERATED_AT}`

This folder is a lightweight control surface for the GEO-ring Cloud project. It intentionally does not copy large data products.

## Source of Truth

- Main code: `{ROOT / "third_report" / "code" / "geo_ring_cloud_stage1"}`
- Stage1 products: `{ROOT / "geo_ring_cloud_stage1"}`
- Time-run products: `{ROOT / "geo_ring_cloud_stage1_time_runs"}`
- Evidence pack: `{ROOT / "geo_ring_cloud_stage1_evidence_pack"}`
- Index database: `{db_path}`

## Files Here

- `directory_classification.md`: relevance, existence, path risk, and archive candidacy.
- `script_inventory.md`: current GEO-ring Cloud scripts, including newly discovered Stage09d files.
- `pipeline_stages.md`: stage-level inputs, outputs, and evidence directories.
- `path_mapping.md`: code/data path dependencies and override strategy.
- `archive_manifest_dry_run.csv`: dry-run archive candidates generated before physical moves.
- `stage_registry.md`: canonical stage taxonomy and collision guards.
- `artifact_index.md`: project memory index for key reports, tables, configs, workbooks, scripts, and directory summaries.
- `legacy_aliases.md`: legacy labels mapped to canonical stage IDs.
- `naming_policy.md`: naming rules for new work and known non-canonical labels.
"""
    (WORKSPACE_DIR / "README.md").write_text(readme, encoding="utf-8")
    write_markdown_table(
        WORKSPACE_DIR / "directory_classification.md",
        dirs,
        ["path", "relevance", "role", "file_count", "size_text", "referenced_by_code", "exists_now", "move_candidate", "path_risk", "note"],
    )
    write_markdown_table(
        WORKSPACE_DIR / "script_inventory.md",
        scripts,
        ["filename", "project_id", "canonical_stage_id", "component_role", "legacy_stage", "responsibility", "refs_external_paths"],
    )
    write_markdown_table(
        WORKSPACE_DIR / "pipeline_stages.md",
        stages,
        ["stage", "name", "input", "output", "gate_status", "evidence_dir"],
    )
    write_markdown_table(
        WORKSPACE_DIR / "path_mapping.md",
        refs,
        ["external_path", "referenced_by_script", "line_no", "purpose", "location"],
    )
    write_markdown_table(
        WORKSPACE_DIR / "stage_registry.md",
        registry,
        ["project_id", "canonical_stage_id", "display_label", "name", "status", "legacy_labels", "evidence_paths", "do_not_merge_with", "notes"],
    )
    write_markdown_table(
        WORKSPACE_DIR / "artifact_index.md",
        artifacts,
        ["project_id", "canonical_stage_id", "component_role", "artifact_type", "path", "legacy_stage_label", "role", "file_count", "ext_summary", "summary"],
    )
    write_markdown_table(
        WORKSPACE_DIR / "legacy_aliases.md",
        aliases,
        ["project_id", "alias", "canonical_stage_id", "confidence", "reason"],
    )
    write_markdown_table(
        WORKSPACE_DIR / "naming_violations.md",
        violations,
        ["project_id", "path", "legacy_label", "issue_type", "severity", "suggested_canonical_stage_id", "suggested_new_path", "reason"],
    )
    naming_policy = """# GEO-ring Cloud Naming Policy

## Canonical identifiers

- Use `project_id + canonical_stage_id` for every stage decision.
- Main project namespace: `geo_ring_cloud`.
- Stage IDs use lowercase ASCII: `stage_00`, `stage_03_5`, `stage_06c`, `stage_07p_b`, `stage_07v2`, `stage_09d`.
- Do not use `Step` for project-level phases. `Step` may only describe an internal procedure inside a script or report.

## New file and directory names

- Prefix new stage-owned files with the canonical stage ID, for example `stage_09d_full_pixel_diagnostics_report.md`.
- Put substep numbers after the stage directory or in report sections, for example `stage_09d/00_sample_manifest`.
- Shared utilities should use `component_role`, not a fake stage: `shared_library`, `runner`, `downloader`, `evidence_pack_builder`, `summary_helper`.

## Collision rules

- `geo_ring_cloud.stage_09` is not `epic_ceres.stage_09`.
- `geo_ring_cloud.stage_09` is not `epic_ceres.stage_09_5`.
- `research_tracker` labels such as `Step9`, `BuildStep9`, and `Stage9` are untrusted legacy inference labels until reviewed in `stage_registry`.

## Migration rule

Historical files are not renamed by default. Rename only after code references, evidence references, and a rollback manifest are checked.
"""
    (WORKSPACE_DIR / "naming_policy.md").write_text(naming_policy, encoding="utf-8")

    suggestions = f"""# GEO-ring Cloud 整理建议

> 生成时间：`{GENERATED_AT}`

## 结论

- 强相关目录保持原位，避免破坏现有流水线和历史产物路径。
- 无关目录只在归档 manifest 确认零引用后移动到 `{ARCHIVE_DIR}`。
- 运行时代码路径通过 `third_report/code/geo_ring_cloud_stage1/path_config.py` 参数化。

## 高优先级保留

- `geo_ring_cloud_stage1`
- `geo_ring_cloud_stage1_time_runs`
- `geo_ring_cloud_stage1_evidence_pack`
- `third_report/code/geo_ring_cloud_stage1`
- `data_check_report`
- `geo_geometry_check`
- `data`

## 可归档候选

见 `_NON_GEO_ARCHIVE/_move_manifest.csv` 或本 workspace 的 `archive_manifest_dry_run.csv`。

## 命名体系

- 权威阶段表见 `_GEO_RING_CLOUD_WORKSPACE/stage_registry.md`。
- 旧名称映射见 `_GEO_RING_CLOUD_WORKSPACE/legacy_aliases.md`。
- 关键产物索引见 `_GEO_RING_CLOUD_WORKSPACE/artifact_index.md`。
- 命名规则见 `_GEO_RING_CLOUD_WORKSPACE/naming_policy.md`。
"""
    (OUT_DIR / "整理建议.md").write_text(suggestions, encoding="utf-8")
    print(f"[OK] workspace reports: {WORKSPACE_DIR}")
    print(f"[OK] 整理建议: {OUT_DIR / '整理建议.md'}")


if __name__ == "__main__":
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    built_db = build_sqlite()
    export_xlsx(built_db)
    export_workspace_reports(built_db)
    print("[DONE]")
