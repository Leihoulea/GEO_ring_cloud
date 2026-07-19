| filename | project_id | canonical_stage_id | component_role | legacy_stage | responsibility | refs_external_paths |
| --- | --- | --- | --- | --- | --- | --- |
| geo_ring_cloud/artifact_io.py | geo_ring_cloud |  | artifact_io |  | 当前文件系统扫描补充脚本 |  |
| geo_ring_cloud/cloud_semantics.py | geo_ring_cloud |  | cloud_semantics |  | 当前文件系统扫描补充脚本 |  |
| geo_ring_cloud/pipeline_support.py | geo_ring_cloud |  | compatibility_facade |  | 当前文件系统扫描补充脚本 |  |
| geo_ring_cloud_claas3_adapter.py | geo_ring_cloud |  | compatibility_shim | common | CLAAS-3 recursive discovery, deterministic deduplication, decoding, unit conversion, QA, and per-variable masks |  |
| geo_ring_cloud_epic_pair_diagnostics.py | geo_ring_cloud |  | compatibility_shim |  | 当前文件系统扫描补充脚本 |  |
| geo_ring_cloud_lineage.py | geo_ring_cloud |  | compatibility_shim | common | Common run and artifact lineage manifest writer |  |
| geo_ring_cloud_run_discovery.py | geo_ring_cloud |  | compatibility_shim | common | Matrix-manifest-first run discovery with legacy time-tag compatibility |  |
| geo_ring_cloud_source_registry.py | geo_ring_cloud |  | compatibility_shim | common | Stable source IDs, processing streams, products, profiles, tolerances, and variable rules |  |
| path_config.py | geo_ring_cloud |  | compatibility_shim | common | Central environment-overridable paths including GEO_RING_CLAAS3_ROOT |  |
| stage1_common.py | geo_ring_cloud |  | compatibility_shim | 公共 | 已登记 compatibility shim；权威 API 见 geo_ring_cloud.pipeline_support、pipeline_layout、cloud_semantics 和 diagnostics.summary |  |
| geo_ring_cloud/data_asset_audit.py | geo_ring_cloud |  | data_asset_audit |  | 当前文件系统扫描补充脚本 |  |
| geo_ring_cloud/diagnostics/epic_pair.py | geo_ring_cloud |  | diagnostics_library |  | 当前文件系统扫描补充脚本 |  |
| geo_ring_cloud/diagnostics/summary.py | geo_ring_cloud |  | diagnostics_library |  | 当前文件系统扫描补充脚本 |  |
| download_geo_geometry_samples.py | geo_ring_cloud |  | downloader | 下载 | 从 AWS S3 下载 GOES-16/18/Himawari-9 几何样本到 geo_geometry_check/ | D:\AAAresearch_paper\geo_geometry_check |
| rebuild_stage1_evidence_pack.py | geo_ring_cloud |  | evidence_pack_builder | 证据包 | 重建 Stage1 证据包：汇总 data_check_report/geo_geometry_check/stage1 全部证据到 evidence_pack/ | D:\AAAresearch_paper\data_check_report,D:\AAAresearch_paper\geo_geometry_check,D:\AAAresearch_paper\geo_ring_cloud_stage1,D:\AAAresearch_paper\geo_ring_cloud_stage1_evidence_pack,D:\AAAresearch_paper\third_report\code\geo_ring_cloud_stage1 |
| geo_ring_cloud_experiment_profile_pair.py | geo_ring_cloud |  | experiment_runner |  | 当前文件系统扫描补充脚本 |  |
| geo_ring_cloud/fusion_support.py | geo_ring_cloud |  | fusion_support |  | 当前文件系统扫描补充脚本 |  |
| geo_ring_cloud/geometry.py | geo_ring_cloud |  | geometry |  | 当前文件系统扫描补充脚本 |  |
| geo_ring_cloud/lineage.py | geo_ring_cloud |  | lineage |  | 当前文件系统扫描补充脚本 |  |
| geo_ring_cloud/overlap.py | geo_ring_cloud |  | overlap_metrics |  | 当前文件系统扫描补充脚本 |  |
| geo_ring_cloud/adapters/__init__.py | geo_ring_cloud |  | package_namespace |  | 当前文件系统扫描补充脚本 |  |
| geo_ring_cloud/diagnostics/__init__.py | geo_ring_cloud |  | package_namespace |  | 当前文件系统扫描补充脚本 |  |
| geo_ring_cloud/__init__.py | geo_ring_cloud |  | package_root |  | 当前文件系统扫描补充脚本 |  |
| geo_ring_cloud/paths.py | geo_ring_cloud |  | path_configuration |  | 当前文件系统扫描补充脚本 |  |
| geo_ring_cloud/pipeline_layout.py | geo_ring_cloud |  | pipeline_layout |  | 当前文件系统扫描补充脚本 |  |
| geo_ring_cloud/adapters/claas3.py | geo_ring_cloud |  | product_adapter |  | 当前文件系统扫描补充脚本 |  |
| geo_ring_cloud/adapters/cloud_products.py | geo_ring_cloud |  | product_adapter |  | 当前文件系统扫描补充脚本 |  |
| geo_ring_cloud/adapters/epic.py | geo_ring_cloud |  | product_adapter |  | 当前文件系统扫描补充脚本 |  |
| geo_ring_cloud/quicklooks.py | geo_ring_cloud |  | quicklook_renderer |  | 当前文件系统扫描补充脚本 |  |
| geo_ring_cloud/reprojection.py | geo_ring_cloud |  | reprojection |  | 当前文件系统扫描补充脚本 |  |
| geo_ring_cloud/run_discovery.py | geo_ring_cloud |  | run_discovery |  | 当前文件系统扫描补充脚本 |  |
| geo_ring_cloud_time_run_matrix.py | geo_ring_cloud |  | runner | runner | Matched operational_baseline and claas3_candidate runner with SQLite indexing |  |
| run_epic_georing_sample_batch.py | geo_ring_cloud |  | runner | 运行器 | EPIC Geo-ring 批量样本运行器（RUNS=time_runs） | D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs\epic_202403_target_selection\recommended_epic_georing_validation_targets.csv |
| run_epic_georing_single_sample.py | geo_ring_cloud |  | runner | 运行器 | EPIC Geo-ring 单时次完整运行流水线（BASE=stage1, RUNS=time_runs） |  |
| geo_ring_cloud/sources.py | geo_ring_cloud |  | source_registry |  | 当前文件系统扫描补充脚本 |  |
| summarize_time_run_20240319_1500.py | geo_ring_cloud |  | summary_helper | 汇总 | 汇总 20240319_1500 时次运行结果 |  |
| tests/geo_ring_cloud_test_claas3.py | geo_ring_cloud |  | support |  | 当前文件系统扫描补充脚本 |  |
| tests/geo_ring_cloud_test_claas3_integration.py | geo_ring_cloud |  | support |  | 当前文件系统扫描补充脚本 |  |
| geo_ring_cloud_time_run_prune_failed.py | geo_ring_cloud |  | time_run_pruning |  | 当前文件系统扫描补充脚本 |  |
| geo_ring_cloud_time_run_prune_intermediates.py | geo_ring_cloud |  | time_run_pruning |  | 当前文件系统扫描补充脚本 |  |
| stage_00d_claas3_integration_readiness.py | geo_ring_cloud | stage_00d |  | 00d | CLAAS-3 March 2024 cadence, structure, QA, projection, and integration readiness gate |  |
| 01_build_core_time_index.py | geo_ring_cloud | stage_01 |  | 01 | 从 data_check_report/parsed_file_metadata.csv 构建核心时次索引，按卫星完整度评分，选定原型时次 2024-03-05T00:00Z |  |
| 02_build_standardized_cloud_native.py | geo_ring_cloud | stage_02 |  | 02 | 读取各卫星原生云产品，按统一变量名映射标准化为 native-grid NPZ，输出到 standardized_native/ |  |
| 03_validate_standardized_cloud_native.py | geo_ring_cloud | stage_03 |  | 03 | 校验 02 产物 NPZ 的变量完整性、dtype、shape 一致性 |  |
| 03_5_semantic_validation_patch.py | geo_ring_cloud | stage_03_5 |  | 03.5 | 语义校验补丁：分类变量码值范围、fill 值合理性 |  |
| 04_check_fy4b_geo_alignment.py | geo_ring_cloud | stage_04 |  | 04 | FY4B GEO 产品与 L2 云产品网格对齐检查（shape/变量/角度范围） |  |
| 04b_fy4b_dqf_bit_decode_diagnostics.py | geo_ring_cloud | stage_04b |  | 04b | FY4B DQF 质量标志位级解码诊断，生成码值统计 |  |
| 05_reproject_cloud_to_grid.py | geo_ring_cloud | stage_05 |  | 05 | 各卫星原生网格重投影到统一 0.05° 全球网格(3600x7200)，KD-tree 最近邻；输出 display/fusion valid_mask |  |
| 06_fuse_best_source.py | geo_ring_cloud | stage_06 |  | 06 | 变量级 best-source 融合：基于 VZA/view_weight/time_weight 逐像素选最优源；输出融合数据+source_map+rating_map |  |
| 06_5_source_selection_diagnostics.py | geo_ring_cloud | stage_06_5 |  | 06.5 | 源选择诊断：验证融合是否以 min-VZA 逻辑驱动 |  |
| 06c_geometry_parameter_audit.py | geo_ring_cloud | stage_06c |  | 06c | 几何参数审计：提取各卫星子午经度/地球半径/轨道高度等 |  |
| 06c_multi_satellite_geometry_metadata_audit.py | geo_ring_cloud | stage_06c |  | 06c | 多卫星几何元数据审计（引用 geo_geometry_check + reprojected_grid + standardized_native） | D:\AAAresearch_paper\geo_geometry_check |
| stage_06c_claas3_geometry_angle_lineage.py | geo_ring_cloud | stage_06c |  | 06c | CLAAS-3 CF projection and navigation-derived angle lineage gate |  |
| 06d_himawari_full_disk_geometry_validation.py | geo_ring_cloud | stage_06d |  | 06d | Himawari 全圆盘几何验证（引用 geo_geometry_check/Himawari-9 与 vza_method_comparison_by_satellite.csv） | D:\AAAresearch_paper\geo_geometry_check\Himawari-9,D:\AAAresearch_paper\geo_geometry_check\vza_method_comparison_by_satellite.csv |
| 06e_full_geometry_angle_source_sync_patch.py | geo_ring_cloud | stage_06e |  | 06e | 几何角度源同步补丁：将传感器/太阳角度层投影到目标网格并重跑 06 融合 |  |
| 06e_vza_ecef_final_audit.py | geo_ring_cloud | stage_06e |  | 06e | VZA ECEF 坐标系最终审计（引用 geo_geometry_check） | D:\AAAresearch_paper\geo_geometry_check |
| 06f_reexport_with_obitype_patch.py | geo_ring_cloud | stage_06f |  | 06f | 带 orbit type 补丁的重新导出 |  |
| 06f_report_sync_patch.py | geo_ring_cloud | stage_06f |  | 06f | 报告同步补丁 |  |
| 06f_unknown_aware_data_asset_audit.py | geo_ring_cloud | stage_06f |  | 06f | unknown-aware 数据资产审计：扫描 data/geo_geometry_check/stage1 子目录识别未知变量 | D:\AAAresearch_paper\data,D:\AAAresearch_paper\geo_geometry_check,D:\AAAresearch_paper\geo_ring_cloud_stage1\fused_best_source,D:\AAAresearch_paper\geo_ring_cloud_stage1\reprojected_grid,D:\AAAresearch_paper\geo_ring_cloud_stage1\standardized_native |
| 07_overlap_consistency_validation.py | geo_ring_cloud | stage_07 |  | 07 | 重叠区一致性验证 v1：相邻卫星覆盖区 cloud_mask/CTH/CTT 差异（历史版） |  |
| 07p_overlap_validator_hotfix.py | geo_ring_cloud | stage_07p |  | 07p | 重叠验证热修复：修 cloud-mask 映射/angle-layer/分层执行 |  |
| stage_07p_claas3_profile_pair_evaluation.py | geo_ring_cloud | stage_07p |  | 07p | Common-domain CLAAS-3 versus operational Meteosat consistency and boundary diagnostics |  |
| 07p_b_source_boundary_magnitude_review.py | geo_ring_cloud | stage_07p_b |  | 07p-b | 源边界跳变幅度审查 |  |
| 07v2_formal_single_time_report.py | geo_ring_cloud | stage_07v2 |  | 07v2 | 正式单时次报告生成：聚合 07p，生成最终验收决策 |  |
| 08_epic_visual_comparison.py | geo_ring_cloud | stage_08 |  | 08 | EPIC(DSCOVR) 目视比较：下载 EPIC 图像与融合结果对比 |  |
| 08b_epic_l2_cloud_audit_compare.py | geo_ring_cloud | stage_08b |  | 08b | EPIC L2 云产品审计比较（引用 E:\GEO_Cloud_2024\CMSAF 下 EPIC L2 文件） | D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs\20240319_1500,E:\GEO_Cloud_2024\CMSAF\DSCOVR_EPIC_L2_CLOUD_03_20240319150052_03.nc4 |
| 08c_epic_cloud_mask_semantic_sensitivity.py | geo_ring_cloud | stage_08c |  | 08c | EPIC 云掩膜语义敏感性分析 |  |
| 08d_select_epic_monthly_semantic_validation_targets.py | geo_ring_cloud | stage_08d |  | 08d | 选择 EPIC 月度语义验证目标 |  |
| 08e_summarize_epic_georing_multisample.py | geo_ring_cloud | stage_08e |  | 08e | 多样本 EPIC Geo-ring 汇总（RUNS_ROOT=time_runs） | D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs |
| 08f_geometry_and_prefusion_epic_diagnostics.py | geo_ring_cloud | stage_08f |  | 08f | 几何与预融合 EPIC 诊断（RUNS_ROOT=time_runs） | D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs |
| 08g_overlap_count_diagnostics.py | geo_ring_cloud | stage_08g |  | 08g | 重叠计数诊断（RUNS_ROOT=time_runs） | D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs |
| 08h_meteosat_time_offset_control_test.py | geo_ring_cloud | stage_08h |  | 08h | Meteosat 时间偏移控制测试（RUNS_ROOT=time_runs） | D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs |
| 08i_meteosat_source_overlap_diagnostics.py | geo_ring_cloud | stage_08i |  | 08i | Meteosat 源重叠诊断（RUNS_ROOT=time_runs） | D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs |
| 08j_prefusion_source_pair_overlap_diagnostics.py | geo_ring_cloud | stage_08j |  | 08j | 预融合源对重叠诊断（RUNS_ROOT=time_runs） | D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs |
| 08k_consolidate_stage08_report.py | geo_ring_cloud | stage_08k |  | 08k | Stage08 报告整合（RUNS_ROOT=time_runs） | D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs |
| 09_stage09_epic_georing_cloud_mask_diagnostics.py | geo_ring_cloud | stage_09 |  | 09 | Stage09 EPIC-Geo-ring 云掩膜诊断（RUNS_ROOT=time_runs） | D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs |
| stage09b_full_overnight/run_stage09b_full_overnight.py | geo_ring_cloud | stage_09b |  | 09b | Stage09b 全夜批量运行（RUNS_ROOT + BASE_STAGE_ROOT） |  |
| stage09c_scaled_batch/run_stage09c_scaled_batch.py | geo_ring_cloud | stage_09c |  | 09c | Stage09c 扩展批量运行（RUNS_ROOT + BASE_STAGE_ROOT） |  |
| stage09d_full_pixel_diagnostics/run_stage09d_full_pixel_diagnostics.py | geo_ring_cloud | stage_09d |  | 09d | 当前文件系统扫描补充脚本；推断阶段 09d |  |
| stage09d_interpretation/analyze_geo_visible_filter.py | geo_ring_cloud | stage_09d |  | 09d | 当前文件系统扫描补充脚本；推断阶段 09d |  |
| stage09d_interpretation/answer_stage09d_questions.py | geo_ring_cloud | stage_09d |  | 09d | 当前文件系统扫描补充脚本；推断阶段 09d |  |
| stage09d_interpretation/audit_meteosat_semantics_stage09d.py | geo_ring_cloud | stage_09d |  | 09d | 当前文件系统扫描补充脚本；推断阶段 09d |  |
| stage09d_interpretation/build_stage09d_interpretation_package.py | geo_ring_cloud | stage_09d |  | 09d | 当前文件系统扫描补充脚本；推断阶段 09d |  |
| stage_09d_claas3_epic_profile_pair_evaluation.py | geo_ring_cloud | stage_09d |  | 09d | Matched common-domain EPIC cloud-mask profile-pair metrics and sample-block bootstrap |  |
| stage_09d_diagnostic_common.py | geo_ring_cloud | stage_09d |  | 09d | 当前文件系统扫描补充脚本；推断阶段 09d |  |
| stage_09d_geo_visible_control/stage_09d_vis_postprocess.py | geo_ring_cloud | stage_09d |  | 09d | 当前文件系统扫描补充脚本；推断阶段 09d |  |
| stage_09d_geo_visible_control/stage_09d_vis_run.py | geo_ring_cloud | stage_09d |  | 09d | 当前文件系统扫描补充脚本；推断阶段 09d |  |
| stage_09d_source_selection_sensitivity/stage_09d_sel_postprocess.py | geo_ring_cloud | stage_09d |  | 09d | 当前文件系统扫描补充脚本；推断阶段 09d |  |
| stage_09d_source_selection_sensitivity/stage_09d_sel_run.py | geo_ring_cloud | stage_09d |  | 09d | 当前文件系统扫描补充脚本；推断阶段 09d |  |
| stage_09d_vis_sel_joint/stage_09d_vis_sel_joint_report.py | geo_ring_cloud | stage_09d |  | 09d | 当前文件系统扫描补充脚本；推断阶段 09d |  |
| stage_09e_psf_sel_qc/stage_09e_make_nature_meeting_figures.py | geo_ring_cloud | stage_09e |  | 09e | 当前文件系统扫描补充脚本；推断阶段 09e |  |
| stage_09e_psf_sel_qc/stage_09e_run_psf_sel_qc.py | geo_ring_cloud | stage_09e |  | 09e | 当前文件系统扫描补充脚本；推断阶段 09e |  |
| stage_09f_spatial_story_maps/stage_09f_make_spatial_story_maps.py | geo_ring_cloud | stage_09f |  | 09f | 当前文件系统扫描补充脚本；推断阶段 09f |  |
| stage_10/stage_10_make_group_meeting_ppt.py | geo_ring_cloud | stage_10 |  | 10 | 当前文件系统扫描补充脚本；推断阶段 10 |  |
| stage_10/stage_10_make_meeting_figures.py | geo_ring_cloud | stage_10 |  | 10 | 当前文件系统扫描补充脚本；推断阶段 10 |  |
| stage_10_claas3_epic_relative_height_evaluation.py | geo_ring_cloud | stage_10 |  | 10 | A/B-band EPIC-relative effective-height profile-pair diagnostics with common approximate PSF |  |
| stage_10_cth_validation/stage_10_qc_audit.py | geo_ring_cloud | stage_10 |  | 10 | 当前文件系统扫描补充脚本；推断阶段 10 |  |
| stage_10_cth_validation/stage_10_rewrite_cth_report.py | geo_ring_cloud | stage_10 |  | 10 | 当前文件系统扫描补充脚本；推断阶段 10 |  |
| stage_10_cth_validation/stage_10_run_cth_validation.py | geo_ring_cloud | stage_10 |  | 10 | 当前文件系统扫描补充脚本；推断阶段 10 |  |
| stage_10p_composite_inventory.py | geo_ring_cloud | stage_10p | data_product_audit | 10p | 当前文件系统扫描补充脚本；推断阶段 10p |  |
| stage_10p2_approx_fov_aggregation/run_stage_10p2_approx_fov.py | geo_ring_cloud | stage_10p2 |  | 10p2 | 当前文件系统扫描补充脚本；推断阶段 10p2 |  |
