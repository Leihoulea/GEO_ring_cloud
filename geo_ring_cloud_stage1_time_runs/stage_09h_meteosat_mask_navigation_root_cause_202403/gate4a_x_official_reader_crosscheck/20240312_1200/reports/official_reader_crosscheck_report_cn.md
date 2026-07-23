# Stage 09H Gate 4A-X EUMETSAT Data Tailor official-reader cross-check

- Generated UTC: 2026-07-23T16:14:21Z
- Case: `20240312_1200` / `Meteosat-0deg` / `CLM`
- Strict exact-native-grid decision: `OFFICIAL_READER_CROSSCHECK_INCONCLUSIVE`
- orientation_status: `OFFICIAL_READER_CONFIRMS_LOCAL_MASK_NAVIGATION_ORDER_MISMATCH`
- exact_native_navigation_status: `INCONCLUSIVE_DUE_TO_GEOTIFF_GRID_NORMALIZATION`
- 约束执行：未修改 production reader；未旋转 `cloud_mask`；未重跑整月；未覆盖 Gate 3A/3B/4A；quicklook 方向没有被当作数组方向证据；本次修订未重新运行 EPCT。

## Input Hash

| path_role | path | sha256 | expected_sha256 | hash_matches_expected | size_bytes |
| --- | --- | --- | --- | --- | --- |
| original_raw_zip | /mnt/e/GEO_Cloud_2024/Meteosat-0deg/CLM/20240312/12/MSG3-SEVI-MSGCLMK-0100-0100-20240312120000.000000000Z-NA.zip | dbd4f0947291d9bbc12f4354c57b9932f7d9c1adedaec1f32ca1417c0b305dce | dbd4f0947291d9bbc12f4354c57b9932f7d9c1adedaec1f32ca1417c0b305dce | True | 603191 |
| wsl_workspace_copy | /home/dministratordian/epct_workspace/input/MSG3-SEVI-MSGCLMK-0100-0100-20240312120000.000000000Z-NA.zip | dbd4f0947291d9bbc12f4354c57b9932f7d9c1adedaec1f32ca1417c0b305dce | dbd4f0947291d9bbc12f4354c57b9932f7d9c1adedaec1f32ca1417c0b305dce | True | 603191 |

## EPCT / Plugin Inventory

完整 stdout/stderr 见 `logs/command_log.txt`；本表只保留命令摘要。

| item | returncode |
| --- | --- |
| uname_a | 0 |
| conda_env_list | 0 |
| which_epct | 0 |
| epct_version | 0 |
| epct_info | 0 |
| conda_list | 0 |
| epct_read_products | 0 |
| epct_read_products_query_MSGCLMK | 0 |
| epct_read_products_query_cloud_mask | 0 |
| epct_read_formats_MSGCLMK | 0 |
| epct_read_chains_MSGCLMK | 0 |
| epct_read_chain_msgclmk_cloud_mask | 0 |
| epct_read_expanded_MSGCLMK | 0 |
| epct_run_chain_help | 0 |

## Data Tailor Output Inventory

| path | format | reader | driver | width | height | count | dtype | shape | nodata | crs | transform | bounds | res | tags_json | is_native_shape_3712 | has_crs_or_transform | resampling_detected |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| /home/dministratordian/epct_workspace/output/gate4a_x_official_reader_crosscheck_20240312_1200/MSGCLMK_20240312T120000Z_20240312T120000Z_epct_8233a5d2_F.tif | GeoTIFF | osgeo.gdal | GTiff | 3712 | 3712 | 1 | float32 | (3712, 3712) | 3.000000 | PROJCS["unknown",GEOGCS["GCS_unknown",DATUM["D_Unknown_based_on_GRS_1980_ellipsoid",SPHEROID["GRS 1980",6378137,298.257222101,AUTHORITY["EPSG","7019"]]],PRIMEM["Greenwich",0],UNIT["Degree",0.0174532925199433]],PROJECTION["Geostationary_Satellite"],PARAMETER["central_meridian",0],PARAMETER["satellite_height",35785831],PARAMETER["false_easting",0],PARAMETER["false_northing",0],UNIT["metre",1,AUTHORITY["EPSG","9001"]],AXIS["Easting",EAST],AXIS["Northing",NORTH]] | (-5565747.7, 2998.7864762931035, 0.0, 5565747.7, 0.0, -2998.7864762931035) | (-5565747.7, -5565747.7, 5565747.7, 5565747.7) | (2998.7864762931035, -2998.7864762931035) | {"AREA_OR_POINT": "Area"} | True | True | unknown_from_geotiff_only |

## Cloud Mask Orientation Comparison

| comparison | same_shape | raw_dtype | data_tailor_dtype | agreement | equal_hash_if_same_dtype_and_shape |
| --- | --- | --- | --- | --- | --- |
| data_tailor_identity_vs_raw_grib_identity | True | float32 | float32 | 0.570902966 | False |
| data_tailor_rot180_vs_raw_grib_identity | True | float32 | float32 | 1.000000000 | True |
| data_tailor_flipud_vs_raw_grib_identity | True | float32 | float32 | 0.632591874 | False |
| data_tailor_fliplr_vs_raw_grib_identity | True | float32 | float32 | 0.619740671 | False |

## Navigation Comparison

| case_id | data_tailor_orientation | data_tailor_orientation_note | comparison_target | n_valid | valid_fraction_of_data_tailor_disk | latitude_mae_deg | longitude_circular_mae_deg | median_geodesic_error_km | p95_geodesic_error_km | matched_fraction_within_1km | matched_fraction_within_5km | matched_fraction_within_10km |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 20240312_1200 | data_tailor_output_storage | GeoTIFF storage as written by Data Tailor | A_gate4a_official_area_reference | 10280792 | 0.998902 | 47.743786 | 55.073807 | 8660.764648 | 15253.670508 | 0.000000 | 0.000000 | 0.000001 |
| 20240312_1200 | data_tailor_output_storage | GeoTIFF storage as written by Data Tailor | B_current_cfgrib_identity | 10279748 | 0.998801 | 0.026834 | 0.052121 | 3.786334 | 17.489369 | 0.063425 | 0.631839 | 0.865980 |
| 20240312_1200 | data_tailor_output_storage | GeoTIFF storage as written by Data Tailor | C_current_cfgrib_rot180 | 10279748 | 0.998801 | 47.743095 | 55.069298 | 8660.394531 | 15252.356689 | 0.000000 | 0.000000 | 0.000001 |
| 20240312_1200 | data_tailor_output_storage | GeoTIFF storage as written by Data Tailor | D_current_cfgrib_rot180_dr1_dc1 | 10279748 | 0.998801 | 47.743095 | 55.069279 | 8660.392578 | 15252.356689 | 0.000000 | 0.000000 | 0.000001 |
| 20240312_1200 | data_tailor_rot180_to_raw_storage | Data Tailor output rotated 180 degrees to match raw GRIB storage, as established by mask comparison | A_gate4a_official_area_reference | 10280792 | 0.998902 | 0.022713 | 0.044247 | 3.175140 | 14.666774 | 0.095245 | 0.704323 | 0.900090 |
| 20240312_1200 | data_tailor_rot180_to_raw_storage | Data Tailor output rotated 180 degrees to match raw GRIB storage, as established by mask comparison | B_current_cfgrib_identity | 10279748 | 0.998801 | 47.743095 | 55.069286 | 8660.394531 | 15252.356689 | 0.000000 | 0.000000 | 0.000001 |
| 20240312_1200 | data_tailor_rot180_to_raw_storage | Data Tailor output rotated 180 degrees to match raw GRIB storage, as established by mask comparison | C_current_cfgrib_rot180 | 10279748 | 0.998801 | 0.026834 | 0.052121 | 3.786334 | 17.489369 | 0.063425 | 0.631839 | 0.865980 |
| 20240312_1200 | data_tailor_rot180_to_raw_storage | Data Tailor output rotated 180 degrees to match raw GRIB storage, as established by mask comparison | D_current_cfgrib_rot180_dr1_dc1 | 10279748 | 0.998801 | 0.026834 | 0.052121 | 3.786308 | 17.489386 | 0.063429 | 0.631839 | 0.865978 |

## Status Logic

- `strict_exact_native_grid_status` 只回答最严格问题：Data Tailor 输出是否保持 raw GRIB 的 exact native storage，可否逐像元 identity 作为官方 native 网格参照。由于 Data Tailor 输出为 GeoTIFF normalized storage，所以这里保留 `OFFICIAL_READER_CROSSCHECK_INCONCLUSIVE`。
- `orientation_status` 回答正交问题：官方 reader 输出、raw mask 值、当前 decoded navigation 三者的数组方向是否暴露出本地 mask-navigation storage-order 不统一。该问题不要求 Data Tailor GeoTIFF 保持 raw storage identity。
- `exact_native_navigation_status` 回答 Data Tailor GeoTIFF navigation 是否能替代 Gate 4A exact native navigation。由于 GeoTIFF 含有网格归一化、椭球、extent 和像元中心约定差异，这里判为 `INCONCLUSIVE_DUE_TO_GEOTIFF_GRID_NORMALIZATION`。

## Interpretation

- 严格 exact-native-grid 判定仍为 `OFFICIAL_READER_CROSSCHECK_INCONCLUSIVE`：Data Tailor 输出是 3712x3712 fixed-grid GeoTIFF，但它没有保持 raw GRIB storage orientation，因此不能作为“原始数组逐像元 identity”的严格官方 native-storage 证据。
- Cloud-mask 值保持不变的判定允许 `identity/rot180/flipud/fliplr` 这类无损一一变换。实际最优变换是 `rot180`，agreement = 1.000000000，hash equal = True。
- 具体数值是：Data Tailor output-storage mask 与 raw GRIB identity agreement = 0.570902966；Data Tailor rot180 后与 raw GRIB agreement = 1.000000000 且 hash 一致。这说明 Data Tailor 没有改变 CLM 类别值，只是输出 storage order 与 raw storage 相差 180 度。
- Data Tailor output-storage navigation 接近当前 cfgrib identity navigation，median geodesic error = 3.786334 km；而 raw mask 需要 rot180 才进入 Data Tailor/cfgrib identity 的方向。
- 将 Data Tailor GeoTIFF rot180 回 raw-storage 后，与 Gate 4A official-area reference 的 median geodesic error = 3.175140 km，p95 = 14.666774 km。这个残差不解释为方向不确定，而归类为 GeoTIFF grid parameter、GRS80/其他椭球参数、area extent 或 pixel-center convention 差异。
- 因此，本次修订的方向结论是 `OFFICIAL_READER_CONFIRMS_LOCAL_MASK_NAVIGATION_ORDER_MISMATCH`：本地 reader 链路没有把 raw cloud-mask values 与 decoded navigation 的 storage order 统一起来。
- 这里不把问题表述为 cfgrib 软件缺陷，也不归咎于 EUMETSAT CLM mask 本身；更准确的表述是本地读取/标准化链路的 storage-order 合约没有显式统一。
- 本报告仍不修改 production、不加入永久 rot180、不重新跑整月；后续修复应沿 Gate 4A 的 production-fix design 做独立最小 patch 和回归测试。

## Warnings

- Warning rows: 1. See `logs/warnings.csv`.
