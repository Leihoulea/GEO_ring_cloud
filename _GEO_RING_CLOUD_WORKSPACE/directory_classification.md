| path | relevance | role | file_count | size_text | referenced_by_code | exists_now | move_candidate | path_risk | note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D:\AAAresearch_paper\data | 强相关 | 原始卫星数据（FY4A/FY4B L1 + FY4B 云产品 CLM/CLP/CLT/CTH/CTP/CTT/GEO + H09_Data） | 4650 | 102.1 GB | 是 | 是 | 否 | keep_in_place | geo_ring_cloud.paths.HIMAWARI_R21_DIR；06f 扫描 data/ |
| D:\AAAresearch_paper\data_check_report | 强相关 | 前序数据审计报告（REPORT_ROOT，00-00f 阶段证据源） | 250 | 887.2 MB | 是 | 是 | 否 | keep_in_place | geo_ring_cloud.paths + pipeline_layout 中的 REPORT_ROOT、PARSED_METADATA、MAPPING_YAML |
| D:\AAAresearch_paper\geo_geometry_check | 强相关 | 几何校验样本（download_geo_geometry_samples.py 产物 + 06c/06d 审计） | 50 | 1.3 GB | 是 | 是 | 否 | keep_in_place | download_geo_geometry_samples.py:22 OUT_ROOT；06c/06d/06e 引用 |
| D:\AAAresearch_paper\geo_ring_cloud_stage1 | 强相关 | Stage1 主产物根目录（STAGE_ROOT） | 819 | 6.3 GB | 是 | 是 | 否 | keep_in_place | standardized_native/reprojected_grid/fused_best_source/overlap_validation/reports 等产物 + scripts 副本 |
| D:\AAAresearch_paper\geo_ring_cloud_stage1_evidence_pack | 强相关 | Stage1 证据包（latest + 10 snapshots） | 264 | 471.2 KB | 是 | 是 | 否 | keep_in_place | rebuild_stage1_evidence_pack.py:EVIDENCE_ROOT 产出 |
| D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs | 强相关 | 多时次运行、跨阶段实验与阶段诊断产物根目录 | 43207 | 228.2 GB | 是 | 是 | 否 | keep_in_place | 运行器与 stage_08 以后诊断脚本通过 RUNS_ROOT 引用；大产物保留原位 |
| D:\AAAresearch_paper\third_report\code\geo_ring_cloud_stage1 | 强相关 | GEO-ring Cloud 主代码、阶段脚本、共享组件与测试 | 111 | 2.0 MB | 是 | 是 | 否 | keep_in_place | 权威代码源；产物写入配置化的 stage/time-runs 根目录 |
| D:\AAAresearch_paper\third_report\Satellite_Data_20240312 | 上游相关 | 2024-03-12 主数据集快照（六星原生 + standardized_L1 样例 + reprojected 旧版） | 8741 | 122.4 GB | 间接 | 是 | 否 | keep_in_place | 含 channel_mapping/standardized_L1_source 等 |
| D:\AAAresearch_paper\third_report\code\FY4B | 上游相关 | FY4B/AGRI standardized_L1 builder + 探查/预览 | 25 | 25.1 MB | 间接 | 是 | 否 | keep_in_place | 产出 standardized_L1_source 供 stage1 输入 |
| D:\AAAresearch_paper\third_report\code\GOES | 上游相关 | GOES-16/18/ABI standardized_L1 builder | 18 | 14.2 MB | 间接 | 是 | 否 | keep_in_place | 同上 |
| D:\AAAresearch_paper\third_report\code\Himawari | 上游相关 | Himawari-9/AHI standardized_L1 builder | 11 | 8.5 MB | 间接 | 是 | 否 | keep_in_place | 同上 |
| D:\AAAresearch_paper\third_report\code\L1g | 上游相关 | standardized_L1_source 规范 + 全球 0.05° 网格规范 | 4 | 60.9 KB | 间接 | 是 | 否 | keep_in_place | ring cloud 上游标准化层规范文档 |
| D:\AAAresearch_paper\third_report\code\Meteosat | 上游相关 | Meteosat-9/10/SEVIRI standardized_L1 builder | 14 | 8.4 MB | 间接 | 是 | 否 | keep_in_place | 同上 |
| D:\AAAresearch_paper\third_report\code\geo_cloud_download | 上游相关 | GEO 云产品下载器（EUMETSAT API + S3） | 23 | 117.3 KB | 间接 | 是 | 否 | keep_in_place | 下载原始云产品到 data/ 与 E 盘 |
| D:\AAAresearch_paper\third_report\code\geo_data_audit | 上游相关 | 数据审计脚本（前序审计 00-00f 的执行代码） | 17 | 482.0 KB | 间接 | 是 | 否 | keep_in_place | audit_geometry_and_variables.py 等产出 data_check_report |
| D:\AAAresearch_paper\third_report\code\plot_geo_satellite_coverage.py | 上游相关 | 六星地球覆盖边界图 | 1 | 4.2 KB | 间接 | 是 | 否 | keep_in_place |  |
| D:\AAAresearch_paper\third_report\code\preview_baselines | 上游相关 | 基线预览图索引 | 13 | 20.8 MB | 间接 | 是 | 否 | keep_in_place | 六星基线 quicklook |
| D:\AAAresearch_paper\third_report\code\preview_runner.py | 上游相关 | 预览批处理入口 | 1 | 2.5 KB | 间接 | 是 | 否 | keep_in_place |  |
| D:\AAAresearch_paper\third_report\code\priority_download_goes_meteosat | 上游相关 | GOES/Meteosat 优先补下载（00f 阶段） | 4 | 39.4 KB | 间接 | 是 | 否 | keep_in_place | build/download/verify 三件套 |
| D:\AAAresearch_paper\third_report\code\run_standardized_l1_source_batch.py | 上游相关 | 标准化批处理统一入口 | 1 | 2.9 KB | 间接 | 是 | 否 | keep_in_place | 调度各卫星 builder |
| D:\AAAresearch_paper\third_report\code\standardized_l1_source_satpy.py | 上游相关 | Satpy 共用标准化骨架 | 1 | 15.1 KB | 间接 | 是 | 否 | keep_in_place | 被 Himawari/Meteosat builder 调用 |
| D:\AAAresearch_paper\third_report\code\validate_standardized_l1_source_samples.py | 上游相关 | 标准化样例自动校验 | 1 | 5.0 KB | 间接 | 是 | 否 | keep_in_place |  |
| D:\AAAresearch_paper\6月第二次汇报邓浩然.pptx | 弱相关 | 6 月组会汇报 PPT |  | 约 52 MB | 否 | 否 | 否 | missing | 项目阶段汇报材料 |
| D:\AAAresearch_paper\EPIC数据.pptx | 弱相关 | EPIC 数据 PPT | 1 | 36.6 KB | 否 | 是 | 否 | document_only | 偏向 EPIC/CERES 分支 |
| D:\AAAresearch_paper\cloud | 弱相关 | 云产品参考文献 PDF（Zhao 等 2026 全天全球云物理属性） | 65 | 33.4 MB | 否 | 是 | 否 | document_only | 方法论参考，代码无路径引用 |
| D:\AAAresearch_paper\research_tracker | 弱相关 | 研究追踪器（项目治理元工具，扫描全项目建知识图谱） | 36 | 88.4 MB | 否 | 是 | 否 | document_only | 元工具，不参与流水线；识别 ring_cloud 为最大子项目 |
| D:\AAAresearch_paper\third_report\beamer | 弱相关 | LaTeX Beamer 组会幻灯片（讲标准化上游） | 58 | 45.0 MB | 否 | 是 | 否 | document_only | 讲稿明确讲 L1g 前期工作，非 ring cloud 本体 |
| D:\AAAresearch_paper\data\3-科大蓝（竞赛 科技）.pptx | 无关 | 存于 data/ 下的竞赛演示文稿 | 1 | 43.0 MB | 否 | 是 | 是 | archive_candidate | 与 ring cloud 无关 |
| D:\AAAresearch_paper\data\A202607010902106154 | 无关 | 空目录 |  | 0 | 否 | 是 | 是 | archive_candidate | 空 |
| D:\AAAresearch_paper\forth | 无关 | 导航相机 DAT 数据解析 + QuickView 工具（独立任务） |  | 约 1.3 GB | 否 | 否 | 否 | missing | nav_camera_dat.py；ring cloud 代码零引用 |
| D:\AAAresearch_paper\second_report | 无关 | FY 数据分析第二次报告（FYDataService + 探查 notebook + 论文） |  | 约 18 GB | 否 | 否 | 否 | missing | ring cloud 代码零引用 |
| D:\AAAresearch_paper\third_report\code\epic_ceres | 无关 | EPIC→CERES 深度学习独立分支（Stage 1-13） |  | 中 | 否 | 否 | 否 | missing | 估算 CERES TOA 辐射通量，与云产品融合无关；代码零引用 |
| D:\AAAresearch_paper\third_report\outputs\epic_ceres | 无关 | EPIC-CERES 各阶段输出 |  | 大 | 否 | 否 | 否 | missing | epic_ceres 分支产物 |
| D:\AAAresearch_paper\third_report\outputs\epic_ceres_fullmonth_v2 | 无关 | EPIC-CERES 全月 v2 训练输出 |  | 大 | 否 | 否 | 否 | missing | epic_ceres 分支产物 |
| D:\AAAresearch_paper\third_report\outputs\epic_ceres_ppt_assets | 无关 | EPIC-CERES PPT 素材 |  | 小 | 否 | 否 | 否 | missing | epic_ceres 分支产物 |
| D:\AAAresearch_paper\third_report\paper_notion_manager | 无关 | 论文-Notion 管理工具 |  | 小 | 否 | 否 | 否 | missing | LLM+Notion 管理论文，与气象无关 |
