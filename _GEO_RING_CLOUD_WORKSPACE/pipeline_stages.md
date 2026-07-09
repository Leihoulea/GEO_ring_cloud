| stage | name | input | output | gate_status | evidence_dir |
| --- | --- | --- | --- | --- | --- |
| 00 | 数据下载与完整性审计 | 原始卫星数据下载 | data_download_audit_report.md, *_cross_product_match.csv | Complete | data_check_report/ |
| 00b | 一样本一产品真实读取 | 样本文件 | manual_variable_mapping_by_product.yaml, one_sample_each_product_*.csv | Complete | data_check_report/ |
| 00c | 几何与变量能力审计 | 样本文件 | product_variable_inventory_full.csv, geometry/cloud/quality_flag_capability_matrix.csv | Complete | data_check_report/geometry_variable_audit/ |
| 00d | Meteosat 产品体系与 catalogue discovery | Meteosat 目录 | meteosat_product_series_audit.md, meteosat_catalogue_discovery_report.md | Complete | data_check_report/meteosat_*/ |
| 00e | 单时次 v0 标准化原型 | 样本 | standardized_cloud_v0_report.md, FY4B_CLM/GOES-16_ACMF/Himawari-9_CMSK.npz | Complete | data_check_report/standardized_cloud_v0_samples/ |
| 00f | GOES/Meteosat 优先补下载 | 缺口清单 | priority_download_goes_meteosat_report.md, meteosat_priority_download_report.md | Complete | data_check_report/priority_download_run_*/ |
| 01 | 核心时间索引 | parsed_file_metadata.csv | core_time_index.csv, usable_times_ranked.csv（原型 2024-03-05T00:00Z） | PASS | geo_ring_cloud_stage1/time_index/ |
| 02 | 标准化云原生 NPZ | 01 选定时次 + 原始卫星文件 | standardized_native/*.npz, inventory/stats CSV, quicklook | PASS | geo_ring_cloud_stage1/standardized_native/ |
| 03 | 结构校验 | 02 的 NPZ | standardized_native_file_validation.csv, validate report | PASS | geo_ring_cloud_stage1/standardized_native/ |
| 03.5 | 语义校验补丁 | 02 的 NPZ | standardized_native_semantic_issues.csv, code_tables.csv | PASS_WITH_WARNINGS | geo_ring_cloud_stage1/standardized_native/ |
| 04 | FY4B GEO 对齐检查 | FY4B NPZ(CLM-GEO) | fy4b_geo_alignment_*.csv, fy4b_geo_alignment_report.md | PASS_WITH_WARNINGS | geo_ring_cloud_stage1/reports/ |
| 04b | FY4B DQF 位解码诊断 | FY4B NPZ DQF | fy4b_dqf_bit_decode_summary.csv, fy4b_quality_flag_rules.yaml | PASS_WITH_WARNINGS | geo_ring_cloud_stage1/reports/ |
| 05 | 重投影到 0.05° 网格 | 02 NPZ + 04 GEO 对齐 | reprojected_grid/{6卫星}/*.npz, target_grid_definition.json, coverage map | PASS_WITH_WARNINGS | geo_ring_cloud_stage1/reprojected_grid/ |
| 06 | best-source 融合 | 05 重投影 NPZ | fused_geo_ring_cloud_*.npz, source_map/rating_map, fusion_stats.csv | PASS_WITH_WARNINGS | geo_ring_cloud_stage1/fused_best_source/ |
| 06.5 | 源选择诊断 | 06 融合结果 | selected_vs_min_vza_agreement.csv 等 9 个 CSV | PASS | geo_ring_cloud_stage1/source_selection_diagnostics/ |
| 06c | 多星几何元数据审计 | 05 重投影 + geo_geometry_check | satellite_geometry_parameter_audit.csv, vza_method_comparison.csv | PASS_WITH_WARNINGS | geo_geometry_check/ + geometry_audit_06c/ |
| 06d | Himawari 全圆盘几何验证 | geo_geometry_check/Himawari-9 | himawari_full_disk_geometry_report.md/audit.csv | PASS_WITH_WARNINGS | geo_geometry_check/Himawari-9/ |
| 06e | 几何角度源同步 | 05/06 结果 + 角度层 | angle_provenance_inventory.csv, geometry_angle_source_policy.yaml, 角度层 NPZ | PASS | geo_ring_cloud_stage1/geometry_angle_sync_06e/ |
| 06f | unknown-aware 数据资产审计 | 全部 NPZ 产物 | audit_summary.json, data_asset_audit.sqlite, blocking_issues.csv | PASS_WITH_WARNINGS | geo_ring_cloud_stage1/data_asset_audit_06f/ |
| 07 | 重叠一致性验证(原版) | 06 融合结果 | overlap_*.csv, overlap_confusion_matrices.json | Historical | geo_ring_cloud_stage1/overlap_validation/ |
| 07p | 重叠验证修复版 | 06e 修正后结果 | *_v2.csv, overlap_validation_07p_report.md | PASS | geo_ring_cloud_stage1/overlap_validation_07p/ |
| 07p-b | 边界跳变幅度审查 | 07p 结果 | 07p_boundary_magnitude_review.md | PASS | geo_ring_cloud_stage1/reports/ |
| 07v2 | 正式单时次报告 | 07p 全部结果 | 07v2_overlap_consistency_validation_report.md, stage1_single_time_acceptance_decision.md | PASS_WITH_WARNINGS | geo_ring_cloud_stage1/reports/ |
| 08 | EPIC 目视对比(原型时次) | 06 融合 + EPIC 图像 | epic_visual_comparison_report.md, epic_*.csv, 对比图 | — | geo_ring_cloud_stage1/epic_visual_comparison/ |
| 08c | EPIC L2 云掩膜语义敏感性 | time_runs 各批次 + EPIC L2 | epic_l2_cloud_mask_semantic_sensitivity_*/ | — | geo_ring_cloud_stage1_time_runs/*/ |
| 09 | Stage09 EPIC-Geo-ring 诊断 | time_runs + EPIC | stage09 系列诊断 | — | geo_ring_cloud_stage1_time_runs/epic_202403_*/ |
