# GEO-ring Cloud 任务文件索引报告

> **项目根**：`D:\AAAresearch_paper`
> **任务正式名称**：GEO-ring Cloud Stage1（多静止卫星统一云产品融合）
> **生成日期**：2026-07-08
> **配套文件**：`索引图.md`（目录树+血缘图）、`geo_ring_cloud_index.sqlite`、`geo_ring_cloud_index.xlsx`、`build_index.py`（生成脚本）
> **整理原则**：只读探查，不删改任何已有文件，所有产出位于 `_GEO_RING_CLOUD_INDEX/` 纯增量目录。

---

## 0. 阅读指南

本报告基于三重交叉核实：① 3 个 Explore 子代理并行探查；② 主线亲自读码（`stage1_common.py`、`rebuild_stage1_evidence_pack.py`、`download_geo_geometry_samples.py`、`evidence_sources_by_stage.md`、`dataset_summary.md`、`stage_registry.md`）；③ 用 Grep 对 `third_report/code/geo_ring_cloud_stage1/` 全量扫描硬编码路径引用（逐行号核实）。报告中所有"代码引用"均给出**脚本名+行号**，可在源文件直接定位。

相关性四级：**★强相关**（任务核心/被代码硬编码依赖）｜**◆上游相关**（产出供 ring cloud 输入）｜**○弱相关**（治理/参考/汇报，不参与流水线）｜**✗无关**（独立任务，代码零引用）。

---

## 1. 任务定位与当前状态

**GEO-ring Cloud Stage1** 的目标：把 FY4B、GOES-16/18、Himawari-9、Meteosat-9/10 共 6 颗静止卫星的官方云产品（云掩膜/云相态/云分类/云顶高/云顶压/云顶温/云光学厚度/有效半径等）统一重投影到 0.05° 全球网格，在重叠区按观测天顶角（VZA）等多准则做最优源融合，再用 DSCOVR/EPIC L2 云产品做独立对照验证。

- **原型时次**：固定为 `2024-03-05T00:00:00Z`（由 `01_build_core_time_index.py` 从 `parsed_file_metadata.csv` 按完整度评分选定）。
- **单时次闭环状态**：`PASS_WITH_WARNINGS`（见 `stage_registry.md` 与 `stage1_single_time_acceptance_decision.md`）。
- **多时次扩展**：`HOLD`——已在 `geo_ring_cloud_stage1_time_runs/` 跑了 49 个时间批次（2024-03 全月）+ 5 个 EPIC 专题，但未放行 production-grade batch。
- **代码常量**：`GEO_RING_STAGE_ROOT` / `STAGE_ROOT`（`stage1_common.py:24`）。

**关键架构——代码与产物物理分离**（这是理解本项目最重要的一点）：

| 角色 | 路径 | 说明 |
|---|---|---|
| 主代码（source of truth） | `third_report\code\geo_ring_cloud_stage1\` | 41 个 `.py`，01–09 全流水线 + 下载 + 证据包 + 运行器 |
| 主产物根（STAGE_ROOT） | `geo_ring_cloud_stage1\` | 标准化/重投影/融合/重叠验证/报告等全部产物 |
| 脚本副本 | `geo_ring_cloud_stage1\scripts\` | 16 个副本，运行时 `shutil.copy2(__file__, SCRIPT_DIR)` 自动留痕，**非主代码源**（缺 08/09 系列） |
| 多时次运行 | `geo_ring_cloud_stage1_time_runs\` | 49 时间批次 + 5 EPIC 专题 |
| 证据包 | `geo_ring_cloud_stage1_evidence_pack\` | latest + 10 个不可变 snapshots |

> ⚠️ 用户最初担心的"third_report 混合"属实：`third_report\code\` 下既有 ring cloud 主代码（`geo_ring_cloud_stage1\`）、上游标准化主线（`FY4B/GOES/Himawari/Meteosat/L1g/...`），也有**无关的** `epic_ceres\`（EPIC→CERES 深度学习独立分支）和 `paper_notion_manager\`。甄别结果见第 6、7 节。

---

## 2. 顶层目录相关性总表

| 目录/文件 | 相关性 | 角色 | 文件数 | 被代码引用 | 备注 |
|---|---|---|---|---|---|
| `geo_ring_cloud_stage1\` | ★强相关 | Stage1 主产物根（STAGE_ROOT） | 816 | 是 | ~6.4 GB |
| `geo_ring_cloud_stage1_time_runs\` | ★强相关 | 多时次运行（49 批+5 EPIC） | 23391 | 是 | 08e–09 脚本以 RUNS_ROOT 引用 |
| `geo_ring_cloud_stage1_evidence_pack\` | ★强相关 | 证据包（latest+10 snapshots） | 264 | 是 | rebuild_stage1_evidence_pack.py 产出 |
| `third_report\code\geo_ring_cloud_stage1\` | ★强相关 | **Stage1 主代码**（41 个 .py） | 46 | 是 | 真正的代码源 |
| `data_check_report\` | ★强相关 | 前序审计（REPORT_ROOT，00–00f） | 185 | 是 | stage1_common.py:25 |
| `geo_geometry_check\` | ★强相关 | 几何校验样本（06c/06d 用） | 50 | 是 | download_geo_geometry_samples.py:22 |
| `data\` | ★强相关 | 原始卫星数据（FY4B云产品+H09等） | 4598 | 是 | stage1_common.py:27 + 06f 扫描 |
| `third_report\code\L1g\` | ◆上游 | standardized_L1 规范+网格规范 | 4 | 间接 | |
| `third_report\code\FY4B\` `GOES\` `Himawari\` `Meteosat\` | ◆上游 | 各卫星 standardized_L1 builder | 25/18/11/14 | 间接 | 产出供 stage1 输入 |
| `third_report\code\geo_data_audit\` | ◆上游 | 数据审计脚本（产出 data_check_report） | 16 | 间接 | |
| `third_report\code\geo_cloud_download\` | ◆上游 | GEO 云产品下载器 | 23 | 间接 | |
| `third_report\code\priority_download_goes_meteosat\` | ◆上游 | GOES/Meteosat 优先补下载 | 4 | 间接 | 00f 阶段 |
| `third_report\code\preview_baselines\` | ◆上游 | 六星基线 quicklook | 13 | 间接 | |
| `third_report\code\standardized_l1_source_satpy.py` 等 5 个根脚本 | ◆上游 | Satpy 共用骨架/批处理/校验/预览/覆盖图 | 1×5 | 间接 | |
| `third_report\Satellite_Data_20240312\` | ◆上游 | 2024-03-12 数据快照 | 8741 | 间接 | 含 standardized_L1 样例 |
| `research_tracker\` | ○弱相关 | 项目治理元工具（知识图谱） | 36 | 否 | 不参与流水线 |
| `cloud\` | ○弱相关 | 参考文献 PDF（Zhao 2026） | 2 | 否 | |
| `third_report\beamer\` | ○弱相关 | LaTeX 组会幻灯片（讲上游） | 58 | 否 | |
| `6月第二次汇报邓浩然.pptx` / `EPIC数据.pptx` | ○弱相关 | 汇报 PPT | 1/1 | 否 | |
| `forth\` | ✗无关 | 导航相机 DAT+QuickView（独立任务） | 148 | 否 | 代码零引用 |
| `second_report\` | ✗无关 | FY 第二次报告 | 232 | 否 | 代码零引用 |
| `third_report\code\epic_ceres\` | ✗无关 | EPIC→CERES 深度学习分支 | 18 | 否 | 代码零引用 |
| `third_report\outputs\epic_ceres*`（3 个） | ✗无关 | EPIC-CERES 产物 | 268+196+26 | 否 | |
| `third_report\paper_notion_manager\` | ✗无关 | 论文-Notion 管理工具 | 20 | 否 | |
| `data\3-科大蓝（竞赛 科技）.pptx` | ✗无关 | 竞赛演示文稿 | 1 | 否 | |
| `data\A202607010902106154\` | ✗无关 | 空目录 | 0 | 否 | |

**统计**：强相关 7 个目录、上游 15 个、弱相关 5 个、无关 9 个。

---

## 3. 核心 ring cloud 目录详解

### 3.1 主代码 `third_report\code\geo_ring_cloud_stage1\`（41 个 .py）

完整脚本清单（按阶段排序）。"引用外部路径"列见第 5 节详表。

| 阶段 | 脚本 | 职责 |
|---|---|---|
| 公共 | `stage1_common.py` | 核心共享库：路径常量、标准变量名、cloud_mask 码表(FY4B/GOES/Himawari/Meteosat)、产品读取器、单位转换、quicklook 绘图 |
| 01 | `01_build_core_time_index.py` | 从 parsed_file_metadata.csv 构建核心时次索引，选定原型时次 2024-03-05T00:00Z |
| 02 | `02_build_standardized_cloud_native.py` | 各卫星原生云产品→统一变量名 native-grid NPZ |
| 03 | `03_validate_standardized_cloud_native.py` | 校验 NPZ 变量/dtype/shape 一致性 |
| 03.5 | `03_5_semantic_validation_patch.py` | 语义校验：分类变量码值范围、fill 值 |
| 04 | `04_check_fy4b_geo_alignment.py` | FY4B GEO 与 L2 云产品网格对齐检查 |
| 04b | `04b_fy4b_dqf_bit_decode_diagnostics.py` | FY4B DQF 质量标志位级解码诊断 |
| 05 | `05_reproject_cloud_to_grid.py` | 重投影到 0.05° 全球网格(3600×7200)，KD-tree 最近邻 |
| 06 | `06_fuse_best_source.py` | 变量级 best-source 融合（VZA/view/time 权重逐像素选优） |
| 06.5 | `06_5_source_selection_diagnostics.py` | 源选择诊断（验证 min-VZA 驱动） |
| 06c | `06c_geometry_parameter_audit.py` | 几何参数审计（子午经度/地球半径/轨道高度） |
| 06c | `06c_multi_satellite_geometry_metadata_audit.py` | 多卫星几何元数据审计（引用 geo_geometry_check） |
| 06d | `06d_himawari_full_disk_geometry_validation.py` | Himawari 全圆盘几何验证（引用 geo_geometry_check/Himawari-9） |
| 06e | `06e_full_geometry_angle_source_sync_patch.py` | 几何角度源同步补丁，重跑 06 融合 |
| 06e | `06e_vza_ecef_final_audit.py` | VZA ECEF 坐标系最终审计 |
| 06f | `06f_unknown_aware_data_asset_audit.py` | unknown-aware 数据资产审计（扫描 data/geo_geometry_check/stage1） |
| 06f | `06f_reexport_with_obitype_patch.py` | 带 orbit type 补丁重新导出 |
| 06f | `06f_report_sync_patch.py` | 报告同步补丁 |
| 07 | `07_overlap_consistency_validation.py` | 重叠区一致性验证 v1（历史版） |
| 07p | `07p_overlap_validator_hotfix.py` | 重叠验证热修复 |
| 07p-b | `07p_b_source_boundary_magnitude_review.py` | 源边界跳变幅度审查 |
| 07v2 | `07v2_formal_single_time_report.py` | 正式单时次报告+验收决策 |
| 08 | `08_epic_visual_comparison.py` | EPIC(DSCOVR) 目视比较 |
| 08b | `08b_epic_l2_cloud_audit_compare.py` | EPIC L2 云产品审计比较（引用 E 盘 EPIC L2） |
| 08c | `08c_epic_cloud_mask_semantic_sensitivity.py` | EPIC 云掩膜语义敏感性分析 |
| 08d | `08d_select_epic_monthly_semantic_validation_targets.py` | 选择 EPIC 月度语义验证目标 |
| 08e | `08e_summarize_epic_georing_multisample.py` | 多样本 EPIC-Geo-ring 汇总 |
| 08f | `08f_geometry_and_prefusion_epic_diagnostics.py` | 几何与预融合 EPIC 诊断 |
| 08g | `08g_overlap_count_diagnostics.py` | 重叠计数诊断 |
| 08h | `08h_meteosat_time_offset_control_test.py` | Meteosat 时间偏移控制测试 |
| 08i | `08i_meteosat_source_overlap_diagnostics.py` | Meteosat 源重叠诊断 |
| 08j | `08j_prefusion_source_pair_overlap_diagnostics.py` | 预融合源对重叠诊断 |
| 08k | `08k_consolidate_stage08_report.py` | Stage08 报告整合 |
| 09 | `09_stage09_epic_georing_cloud_mask_diagnostics.py` | Stage09 EPIC-Geo-ring 云掩膜诊断 |
| 09b | `stage09b_full_overnight\run_stage09b_full_overnight.py` | Stage09b 全夜批量运行 |
| 09c | `stage09c_scaled_batch\run_stage09c_scaled_batch.py` | Stage09c 扩展批量运行 |
| 下载 | `download_geo_geometry_samples.py` | 从 AWS S3 下载 GOES/Himawari 几何样本→geo_geometry_check |
| 证据包 | `rebuild_stage1_evidence_pack.py` | 汇总全部证据→evidence_pack |
| 运行器 | `run_epic_georing_single_sample.py` | 单时次完整运行流水线 |
| 运行器 | `run_epic_georing_sample_batch.py` | 批量样本运行器 |
| 汇总 | `summarize_time_run_20240319_1500.py` | 汇总 20240319_1500 时次 |

### 3.2 主产物 `geo_ring_cloud_stage1\`（STAGE_ROOT，816 文件）

| 子目录 | 阶段 | 内容 | 规模 |
|---|---|---|---|
| `config\` | — | core_product_definition.yaml | 1 |
| `scripts\` | — | 16 个脚本副本（运行留痕） | 16 |
| `time_index\` | 01 | core_time_index.csv / usable_times_ranked.csv / report | 3 |
| `standardized_native\` | 02/03/03.5 | 6 卫星标准化云 NPZ + 校验/语义/码表 CSV | 33 |
| `quicklooks_native\` | 02 | 原生网格 quicklook PNG | ~20+ |
| `reprojected_grid\` | 05 | 6 卫星子目录重投影 NPZ + target_grid_definition.json | 173 |
| `quicklooks_reprojected\` | 05 | 重投影 quicklook | — |
| `fused_best_source\` | 06 | fused_geo_ring_cloud_*.npz + source/rating/valid_count map + 统计CSV + quicklooks | 71 |
| `source_selection_diagnostics\` | 06.5 | 9 个诊断 CSV + quicklooks | 113 |
| `geometry_audit_06c\` | 06c | 几何参数审计 CSV | — |
| `geometry_angle_sync_06e\` | 06e | 角度层 NPZ(6×7) + policy.yaml + provenance CSV | 124 |
| `data_asset_audit_06f\` | 06f | audit_summary.json + data_asset_audit.sqlite + exports | 7 |
| `overlap_validation\` | 07 | 重叠差异 CSV + 混淆矩阵 + quicklooks | 67 |
| `overlap_validation_07p\` | 07p | v2 重叠验证 CSV + quicklooks | 18 |
| `epic_visual_comparison\` | 08 | EPIC 原始图 + 对比图 + report | — |
| `FY4B_DATA_INTRO\` | — | FY4B 产品说明 PDF | 13 |
| `reports\` | 全 | 21+ markdown 报告 + 附件 CSV/YAML | — |

### 3.3 多时次运行 `geo_ring_cloud_stage1_time_runs\`（23391 文件）

- **49 个时间批次**：`20240305_1500` 至 `20240331_1900`，覆盖 2024-03 全月。每批结构一致：
  `single_sample_run_manifest.json` / `pipeline_run_status.csv` / `time_index` / `standardized_native` / `reprojected_grid`(6卫星) / `fused_best_source` / `epic_l2_cloud_mask_semantic_sensitivity_*` / `reports`(含中英文版) / `scripts`(副本) / `logs`。每批执行 02/03/05/06/08c 五步。
- **5 个 EPIC 专题**：`epic_202403_batch_runs`、`epic_202403_meteosat_time_offset_control`、`epic_202403_multisample_summary`（含 Stage08 组会 PPT）、`epic_202403_overnight_watch`、`epic_202403_target_selection`。

### 3.4 证据包 `geo_ring_cloud_stage1_evidence_pack\`（264 文件）

- `latest\`：`README.md` / `stage_registry.md` / `evidence_manifest.json` / `config\` / `cross_cutting\`（12 个索引：dataset_summary、evidence_sources_by_stage、data_check_report_lineage 等）/ `pipeline_stages\`（00–07 各阶段页）。
- `snapshots\`：10 个不可变快照（`20260623T152635Z`…`20260624T092254Z`）。

---

## 4. 流水线阶段表（00–09）

源自 `evidence_sources_by_stage.md` 与 `stage_registry.md`。

| 阶段 | 名称 | 输入 | 输出 | Gate | 证据目录 |
|---|---|---|---|---|---|
| 00 | 数据下载与完整性审计 | 原始下载 | data_download_audit_report.md, cross_product_match.csv | Complete | data_check_report\ |
| 00b | 一样本一产品真实读取 | 样本 | manual_variable_mapping_by_product.yaml, one_sample_each_product_*.csv | Complete | data_check_report\ |
| 00c | 几何与变量能力审计 | 样本 | product_variable_inventory_full.csv, 能力矩阵 | Complete | data_check_report\geometry_variable_audit\ |
| 00d | Meteosat 产品体系与 catalogue | Meteosat 目录 | meteosat_product_series_audit.md, catalogue_discovery | Complete | data_check_report\meteosat_*\ |
| 00e | v0 标准化原型 | 样本 | standardized_cloud_v0_report.md, *.npz | Complete | data_check_report\standardized_cloud_v0_samples\ |
| 00f | GOES/Meteosat 优先补下载 | 缺口清单 | priority_download_*_report.md | Complete | data_check_report\priority_download_run_*\ |
| 01 | 核心时间索引 | parsed_file_metadata.csv | core_time_index.csv（原型 2024-03-05T00:00Z） | PASS | stage1\time_index\ |
| 02 | 标准化云原生 NPZ | 01 时次+原始文件 | standardized_native\*.npz | PASS | stage1\standardized_native\ |
| 03 | 结构校验 | 02 NPZ | file_validation.csv | PASS | stage1\standardized_native\ |
| 03.5 | 语义校验补丁 | 02 NPZ | semantic_issues.csv, code_tables.csv | PASS_WITH_WARNINGS | stage1\standardized_native\ |
| 04 | FY4B GEO 对齐 | FY4B NPZ | fy4b_geo_alignment_*.csv | PASS_WITH_WARNINGS | stage1\reports\ |
| 04b | FY4B DQF 位解码 | FY4B NPZ DQF | dqf_bit_decode_summary.csv, rules.yaml | PASS_WITH_WARNINGS | stage1\reports\ |
| 05 | 重投影 0.05° | 02 NPZ+04 | reprojected_grid\{6卫星}\*.npz | PASS_WITH_WARNINGS | stage1\reprojected_grid\ |
| 06 | best-source 融合 | 05 NPZ | fused_geo_ring_cloud_*.npz, source/rating map | PASS_WITH_WARNINGS | stage1\fused_best_source\ |
| 06.5 | 源选择诊断 | 06 结果 | selected_vs_min_vza_agreement.csv 等 | PASS | stage1\source_selection_diagnostics\ |
| 06c | 多星几何元数据审计 | 05+geo_geometry_check | geometry_parameter_audit.csv | PASS_WITH_WARNINGS | geo_geometry_check\ + geometry_audit_06c\ |
| 06d | Himawari 全圆盘几何验证 | geo_geometry_check\Himawari-9 | himawari_full_disk_geometry_report.md | PASS_WITH_WARNINGS | geo_geometry_check\Himawari-9\ |
| 06e | 几何角度源同步 | 05/06+角度层 | angle_provenance_inventory.csv, 角度层 NPZ | PASS | stage1\geometry_angle_sync_06e\ |
| 06f | unknown-aware 资产审计 | 全部 NPZ | audit_summary.json, data_asset_audit.sqlite | PASS_WITH_WARNINGS | stage1\data_asset_audit_06f\ |
| 07 | 重叠验证(原版) | 06 结果 | overlap_*.csv, 混淆矩阵 | Historical | stage1\overlap_validation\ |
| 07p | 重叠验证(修复版) | 06e 结果 | *_v2.csv, 07p_report | PASS | stage1\overlap_validation_07p\ |
| 07p-b | 边界跳变幅度审查 | 07p | boundary_magnitude_review.md | PASS | stage1\reports\ |
| 07v2 | 正式单时次报告 | 07p 全部 | 07v2_report, acceptance_decision | PASS_WITH_WARNINGS | stage1\reports\ |
| 08 | EPIC 目视对比(原型) | 06+EPIC | epic_visual_comparison_report.md | — | stage1\epic_visual_comparison\ |
| 08c | EPIC L2 语义敏感性 | time_runs+EPIC L2 | epic_l2_cloud_mask_semantic_sensitivity_* | — | time_runs\*\ |
| 09 | Stage09 EPIC 诊断 | time_runs+EPIC | stage09 系列诊断 | — | time_runs\epic_202403_*\ |

**Gate 分布**：Complete 6 ｜ PASS 7 ｜ PASS_WITH_WARNINGS 9 ｜ Historical 1 ｜ 进行中(—) 3。

---

## 5. 外部关联数据依赖专章（★用户最关注）

以下目录/文件**不在 ring cloud 自身目录内**，但被 ring cloud 代码硬编码引用，是任务的数据依赖。全部行号已亲自 Grep 核实。

### 5.1 `data_check_report\`（REPORT_ROOT，前序审计 00–00f）

`stage1_common.py` 直接定义并读取：

| 行号 | 常量 | 路径 | 用途 |
|---|---|---|---|
| 25 | REPORT_ROOT | `D:\AAAresearch_paper\data_check_report` | 前序审计根 |
| 26 | GEOM_AUDIT_ROOT | `...\data_check_report\geometry_variable_audit` | 几何审计目录 |
| 29 | PARSED_METADATA | `...\data_check_report\parsed_file_metadata.csv` | **01 阶段核心输入**：所有卫星文件元数据索引 |
| 30 | VARIABLE_INVENTORY | `...\geometry_variable_audit\product_variable_inventory_full.csv` | 产品变量清单 |
| 31 | MAPPING_YAML | `...\data_check_report\manual_variable_mapping_by_product.yaml` | **02 阶段直接读取**：手动变量映射表 |

### 5.2 `geo_geometry_check\`（几何校验样本，06c/06d/06e 用）

| 脚本:行号 | 引用路径 | 用途 |
|---|---|---|
| `download_geo_geometry_samples.py:22` | `D:\AAAresearch_paper\geo_geometry_check` | OUT_ROOT——此目录是本脚本的下载产物 |
| `06c_multi_satellite_geometry_metadata_audit.py:27` | 同上 | GEOMETRY_ROOT |
| `06d_himawari_full_disk_geometry_validation.py:28` | `...\geo_geometry_check\vza_method_comparison_by_satellite.csv` | 当前 06c VZA 比较 |
| `06d_...:29` | `...\geo_geometry_check\Himawari-9` | Himawari 全圆盘段数据 |
| `06e_vza_ecef_final_audit.py:20` | `...\geo_geometry_check` | ECEF 审计几何目录 |
| `06f_unknown_aware_data_asset_audit.py:39` | 同上 | SCAN_DIRS 之一 |
| `rebuild_stage1_evidence_pack.py:16` | 同上 | 证据来源 |

### 5.3 `data\`（原始卫星数据）

| 脚本:行号 | 引用路径 | 用途 |
|---|---|---|
| `stage1_common.py:27` | `D:\AAAresearch_paper\data\H09_Data` | HIMAWARI_R21_DIR（Himawari R21 辅助几何） |
| `06f_unknown_aware_data_asset_audit.py:38` | `D:\AAAresearch_paper\data` | SCAN_DIRS 原始数据根 |

`data\` 子目录：`FY4A_Data`(33 HDF)、`FY4B_Data`(22 HDF)、`FY4B-CLM`(637 NC)、`FY4B-CLP`(637)、`FY4B-CLT`(1049)、`FY4B-CTH`(637)、`FY4B-CTP`(637)、`FY4B-CTT`(225)、`FY4B-GEO`(637 HDF,~50GB)、`H09_Data`(11 NC)。注：`data\3-科大蓝（竞赛 科技）.pptx` 与 `data\A202607010902106154\`（空）与 ring cloud 无关。

### 5.4 外部数据盘（不在 D 盘项目内）

| 盘 | 路径 | 引用方:行号 | 用途 |
|---|---|---|---|
| E 盘 | `E:\GEO_Cloud_2024` | `data_check_report\check_download_completeness.py:18`(GEO_ROOT) | GOES/Himawari/Meteosat 大规模原始云产品 |
| E 盘 | `E:\GEO_Cloud_2024\CMSAF\DSCOVR_EPIC_L2_CLOUD_03_20240319150052_03.nc4` | `08b_epic_l2_cloud_audit_compare.py:27` | EPIC L2 云产品文件 |
| F 盘 | `F:\DSCOVR_EPIC_L2_CLOUD_03_2024.03` | 各 `time_runs\*\single_sample_run_manifest.json` | DSCOVR EPIC L2 云产品（每批次引用具体 .nc4） |

### 5.5 跨目录汇总引用（rebuild_stage1_evidence_pack.py，14–18 行）

该脚本汇总四类证据源到证据包：`evidence_pack`(14) / `stage1`(15) / `geo_geometry_check`(16) / `third_report\code\geo_ring_cloud_stage1`(17,CODE_ROOT) / `data_check_report`(18,DATA_CHECK_ROOT)。

### 5.6 `time_runs` 被 9 个脚本引用

`08e/08f/08g/08h/08i/08j/08k/09` 及 `stage09b/stage09c` 子目录脚本均以 `RUNS_ROOT = D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs` 引用；`08b:24` 引用特定批次 `...\20240319_1500`；`run_epic_georing_sample_batch.py:15` 引用 `...\epic_202403_target_selection\recommended_epic_georing_validation_targets.csv`。

---

## 6. 上游标准化主线（`third_report\code\`）

这些模块产出 `standardized_L1_source` 中间层与原始数据探查/预览，是 ring cloud 的上游输入（间接相关）。

| 模块 | 关键文件 | 作用 |
|---|---|---|
| 根脚本 | `standardized_l1_source_satpy.py` | Satpy 共用标准化骨架（被 Himawari/Meteosat builder 调用） |
| 根脚本 | `run_standardized_l1_source_batch.py` | 标准化批处理统一入口（sample/all 模式） |
| 根脚本 | `validate_standardized_l1_source_samples.py` / `preview_runner.py` / `plot_geo_satellite_coverage.py` | 校验/预览/覆盖图 |
| `L1g\` | `standardized_L1_source_spec.md`(v0.2) / `global_grid_005_spec.ipynb` | 规范文档 |
| `FY4B\` | `fy4b_standardized_l1_source_builder.py` + probe/preview notebook + satpy_self_check | FY4B 标准化（Satpy+GEO 优先） |
| `GOES\` | `goes_standardized_l1_source_builder.py` + probe/preview | GOES-16/18 标准化 |
| `Himawari\` | `himawari_standardized_l1_source_builder.py` + probe/preview | Himawari-9 标准化 |
| `Meteosat\` | `meteosat_standardized_l1_source_builder.py` + probe/preview | Meteosat-9/10 标准化 |
| `geo_data_audit\` | `audit_geometry_and_variables.py` / `meteosat_catalogue_discovery.py` 等 | 产出 `data_check_report` 的执行代码 |
| `geo_cloud_download\` | `geo_cloud_downloader.py` / `monitor_dashboard.py` | GEO 云产品下载（EUMETSAT API + S3） |
| `priority_download_goes_meteosat\` | `build/download/verify` 三件套 | 00f 优先补下载 |
| `preview_baselines\` | 六星基线 quicklook + README | 基线预览索引 |

数据快照 `third_report\Satellite_Data_20240312\`（8741 文件）含六星原生数据、`channel_mapping\`、`standardized_L1_source\` 样例、`reprojected_*` 旧版、`inspection_report_*`。

---

## 7. 无关目录排除说明（含零引用证据）

以下目录与 GEO-ring Cloud 任务无关，已通过 Grep 在 `third_report\code\geo_ring_cloud_stage1\` 全量搜索确认 **ring cloud 脚本零引用**（搜索 `epic_ceres|forth|second_report|paper_notion_manager` 命中数 = 0）。

| 目录 | 判定依据 |
|---|---|
| `forth\` | 导航相机 DAT 数据解析 + QuickView 工具。`nav_camera_dat.py` 处理 4120-byte/packet 导航相机数据包（sync word EB 90 55 AA），输出 2048×2048 帧 PNG。与任何 GEO 卫星数据无关，ring cloud 代码零引用。 |
| `second_report\` | FY 数据分析第二次报告。含 FYDataService Electron 桌面程序（100+ dll）、`analyse_FY.ipynb`、`satellitedata\` 探查 notebook、遥感图像拼接论文 PDF。属更早的独立分析管线，ring cloud 代码零引用。 |
| `third_report\code\epic_ceres\` | EPIC→CERES 深度学习独立分支（Stage 1-13：`run_stage_1_5.py`…`run_stage_13_*.py` + `models.py`/`metrics.py`/`training_data.py`）。目标是用 DSCOVR/EPIC 估算 CERES TOA 辐射通量，与云产品融合是不同课题。ring cloud 代码零引用。 |
| `third_report\outputs\epic_ceres*`（3 个目录） | epic_ceres 分支的各阶段/全月/PPT 素材产物。 |
| `third_report\paper_notion_manager\` | 论文→Notion 管理工具（LLM 分块+Notion API），与气象数据处理无关。 |
| `data\3-科大蓝（竞赛 科技）.pptx` | 存于 data\ 下的竞赛演示文稿。 |
| `data\A202607010902106154\` | 空目录。 |

**弱相关但保留说明**：`research_tracker\`（项目治理元工具，扫描全项目建知识图谱，自身不参与流水线）、`cloud\`（参考文献 PDF，代码无路径引用）、`third_report\beamer\`（组会幻灯片，讲稿明确讲标准化上游而非 ring cloud 本体）、根目录两个汇报 PPT——这些不产出 ring cloud 数据，但与项目治理/汇报/文献相关，故标"弱相关"而非"无关"。

---

## 8. 数据血缘图

见独立文件 `索引图.md`，含：
1. 顶层目录树（带相关性颜色标注）；
2. 代码—产物分离关系图；
3. mermaid 数据血缘流程图（原始数据→前序审计00-00f→几何校验→主链01-07→EPIC对照08-09→多时次→证据包）；
4. 外部数据盘依赖表。

---

## 9. 附录：机器可读存储说明

### 9.1 `geo_ring_cloud_index.sqlite`（主存储，7 张业务表 + meta）

| 表 | 行数 | 字段 | 说明 |
|---|---|---|---|
| `directories` | 36 | path,name,parent,relevance,role,file_count,size_bytes,size_text,ext_summary,referenced_by_code,note | 顶层目录相关性 |
| `files` | 774 | path,name,dir_path,ext,category,stage,relevance,size_bytes,note | 全量入库的小文件（代码/配置/报告/清单）；海量数据目录按子目录归纳，不逐文件入库 |
| `scripts` | 41 | path,filename,stage,responsibility,refs_external_paths | ring cloud 主脚本职责+引用的外部路径 |
| `external_data_refs` | 36 | external_path,referenced_by_script,line_no,purpose,location | 外部数据路径引用（含行号，D盘内33/E盘2/F盘1） |
| `pipeline_stages` | 26 | stage,name,input,output,gate_status,evidence_dir | 流水线阶段 |
| `time_runs` | 54 | run_id,kind,target_utc,note | 49 时间批次 + 5 EPIC 专题 |
| `external_disks` | 2 | path,location,description,referenced | E盘/F盘 |
| `meta` | 7 | key,value | 生成元信息 |

**常用查询示例**：
```sql
-- 查所有强相关目录
SELECT name,role,file_count,size_text FROM directories WHERE relevance='强相关';
-- 查某脚本引用了哪些外部数据
SELECT external_path,line_no,purpose FROM external_data_refs WHERE referenced_by_script='stage1_common.py';
-- 查某外部路径被哪些脚本引用
SELECT referenced_by_script,line_no FROM external_data_refs WHERE external_path LIKE '%geo_geometry_check%';
-- 按阶段查脚本
SELECT filename,responsibility FROM scripts WHERE stage='06' ORDER BY filename;
```

### 9.2 `geo_ring_cloud_index.xlsx`（8 个 sheet）

`目录相关性` / `文件清单` / `脚本职责` / `外部数据依赖` / `外部数据盘` / `流水线阶段` / `时间运行批次` / `元信息`。由 sqlite 同源导出，便于直接打开浏览。

### 9.3 复现

`build_index.py` 为生成脚本，重新执行可刷新 sqlite+xlsx：
```powershell
D:\anaconda\envs\pytorch\python.exe D:\AAAresearch_paper\_GEO_RING_CLOUD_INDEX\build_index.py
```
（依赖：Python 3.11 + sqlite3 + openpyxl，已安装于 `D:\anaconda\envs\pytorch`。）

---

## 10. 自检结果

| 检查项 | 结果 |
|---|---|
| 无关目录 forth/second_report/epic_ceres/paper_notion_manager 相关性 | 均为"无关" ✓ |
| ring cloud 脚本引用 forth/second_report/epic_ceres | 命中数 0 ✓ |
| stage1_common.py 7 条外部引用行号 | 24/25/26/27/29/30/31 与源码一致 ✓ |
| geo_geometry_check 被 7 个脚本引用 | 行号精确 ✓ |
| E盘/F盘外部引用 | 3 条（08b:27 / check_download_completeness:18 / manifest:5）✓ |
| 脚本总数 | 41（含 stage09b/09c 子目录）✓ |
| 未修改/删除任何已有文件 | 仅 `_GEO_RING_CLOUD_INDEX\` 为新增 ✓ |

---

> 本报告所有结论均可通过配套 sqlite/xlsx 复核，所有代码引用均可在源文件按行号定位。
