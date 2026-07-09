# standardized_L1_source NetCDF 规范

版本：v0.2  
更新时间：2026-04-23

## 1. 定位

`standardized_L1_source` 是正式 L1g 产品链的中间层。

它的输入是各卫星官方或标准化的 Level-1B 辐射数据，输出是统一变量命名、统一通道语义、统一时间格式、统一几何变量和统一元数据结构的单星原始投影/原始扫描网格产品。

它不做全球规则格点化，也不做跨星融合。

推荐处理链：

```text
official/native L1B
        ↓
standardized_L1_source
        ↓
single-satellite L1g on 0.05° grid
        ↓
multi-source primary/secondary/tertiary L1g
```

## 2. 设计原则

1. 保留原始卫星像元网格。
2. 尽量同时保留原始计数值和标定后的物理量。
3. 对所有卫星使用相同变量名。
4. 通道维使用该文件真实存在或可标准映射的通道集合，不强制填充所有全球通道槽位。
5. 所有通道使用稳定的语义槽位名，并同时保存原生通道名和实际中心波长。
6. 时间使用 CF-compliant numeric time 作为主表达，同时保存 ISO-8601 字符串便于人工检查。
7. 所有角度单位使用 degree，方位角约定为正北 0°、顺时针增加、范围 `[0, 360)`。
8. 原始计数、反射率、亮温和辐亮度等不同物理量必须可区分，不能只依赖一个含混变量。
9. 质量控制至少包含二元有效掩膜和 bit-field 质量标记。
10. 对 GOES ABI、Himawari AHI、Meteosat SEVIRI 等多分辨率传感器，不强制把不同分辨率通道堆叠到同一个 `channel,y,x` 网格；推荐按原生通道或同分辨率网格分文件保存。

## 3. 文件命名

推荐命名：

```text
<satellite_id>_<sensor_id>_<scene_type>_<YYYYMMDD>_<HHMM>_standardized_L1_source_v0.2.nc
```

示例：

```text
FY4B_AGRI_FD_20240312_0300_standardized_L1_source_v0.2.nc
GOES16_ABI_FD_20240312_1800_standardized_L1_source_v0.2.nc
Himawari9_AHI_FD_20240312_0300_standardized_L1_source_v0.2.nc
Meteosat10_SEVIRI_FD_20240312_1200_standardized_L1_source_v0.2.nc
```

`scene_type` 建议值：

| 值 | 含义 |
|---|---|
| `FD` | full disk |
| `REG` | regional scan |
| `MESO` | mesoscale / rapid scan |
| `UNK` | 未知或待确认 |

## 4. 维度

| 维度 | 含义 |
|---|---|
| `channel` | 本文件中真实存在或可标准映射的通道维度 |
| `y` | 原始卫星行方向 |
| `x` | 原始卫星列方向 |

FY4B AGRI full disk 4 km 例子：

```text
channel = 15
y = 2748
x = 2748
```

## 5. 时间变量

主时间变量采用数值型 CF 时间：

| 变量 | 维度 | dtype | 单位 | 说明 |
|---|---|---|---|---|
| `time` | scalar | `float64` | seconds since 1970-01-01 00:00:00 UTC | 名义观测时次 |
| `observation_start_time` | scalar | `float64` | seconds since 1970-01-01 00:00:00 UTC | 文件观测开始时间 |
| `observation_end_time` | scalar | `float64` | seconds since 1970-01-01 00:00:00 UTC | 文件观测结束时间 |
| `line_time_offset` | `y` | `float32` | seconds | 扫描行相对起始时间偏移；属性 `reference_time = observation_start_time` |

建议同时保存全局属性：

| 属性 | 说明 |
|---|---|
| `nominal_time_utc` | ISO-8601 字符串 |
| `observation_start_time_utc` | ISO-8601 字符串 |
| `observation_end_time_utc` | ISO-8601 字符串 |

若卫星产品提供逐像元时间，可增加：

| 变量 | 维度 | 单位 | 说明 |
|---|---|---|---|
| `pixel_time_offset` | `y, x` | seconds | 像元级时间偏移；属性 `reference_time = observation_start_time` |

## 6. 坐标变量与投影

| 变量 | 维度 | 单位 | 说明 |
|---|---|---|---|
| `channel` | `channel` | 1 | 通道索引 |
| `y` | `y` | pixel | 原始行索引 |
| `x` | `x` | pixel | 原始列索引 |
| `latitude` | `y, x` | degree_north | 每个原始像元中心纬度 |
| `longitude` | `y, x` | degree_east | 每个原始像元中心经度 |
| `projection_x` | `x` | m | 原生静止投影坐标轴 x；与 `grid_mapping` 配套解释，不再把扫描角作为正式统一语义 |
| `projection_y` | `y` | m | 原生静止投影坐标轴 y；与 `grid_mapping` 配套解释，不再把扫描角作为正式统一语义 |
| `goes_imager_projection` 或同类 `grid_mapping` | scalar | 1 | CF grid mapping 变量 |

`latitude` / `longitude` 与 `projection_x` / `projection_y` 至少需要满足其一：

- 若文件写入逐像元 `latitude` / `longitude`，它们建议设置 `grid_mapping` 属性指向投影变量。
- 若文件不写逐像元 `latitude` / `longitude`，则必须写入可反演定位的 `projection_x`、`projection_y` 和 CF `grid_mapping` 变量。
- `projection_x` / `projection_y` 的正式统一定义为：native geostationary projection coordinate axes。
- 对某些原始产品（例如 GOES ABI）中本来以 scan angle 表达的坐标，进入 `standardized_L1_source` 后应统一转换到与 `grid_mapping` 一致的投影坐标轴语义。
- 若后续仍需追溯源文件中的扫描角，可作为卫星专用附加变量或元数据保留，但不再占用统一主变量 `projection_x` / `projection_y` 的语义槽位。

对于静止卫星，投影变量至少应包含：

| 属性 | 说明 |
|---|---|
| `grid_mapping_name` | `geostationary` |
| `longitude_of_projection_origin` | 星下点经度 |
| `perspective_point_height` | 卫星相对椭球面的高度 |
| `semi_major_axis` | 椭球长半轴 |
| `semi_minor_axis` | 椭球短半轴 |
| `sweep_angle_axis` | `x` 或 `y` |

## 7. 核心数据变量

| 变量 | 维度 | 建议 dtype | 单位 | 说明 |
|---|---|---|---|---|
| `raw_count` | `channel, y, x` | `uint16` 或 `int16` | count / DN | 原始计数值、DN、packed radiance 或 LUT index |
| `radiance` | `channel, y, x` | `float32` | channel-dependent | 辐亮度；没有该物理量时为 NaN |
| `reflectance` | `channel, y, x` | `float32` | 1 | 反射率；没有该物理量时为 NaN |
| `brightness_temperature` | `channel, y, x` | `float32` | K | 亮温；没有该物理量时为 NaN |
| `calibrated_value` | `channel, y, x` | `float32` | channel-dependent | 兼容层；保存该通道当前首选物理量 |
| `valid_mask` | `channel, y, x` | `uint8` | 1 | 1 表示有效，0 表示无效 |
| `quality_flag` | `channel, y, x` | `uint16` | 1 | bit-field 质量标记 |

`calibrated_value` 仅作为兼容变量使用。后续科学计算应优先读取 `radiance`、`reflectance` 或 `brightness_temperature`。

读取约定：

- `raw_count` 优先保持原始整数 dtype；无效码建议写入 `raw_fill_value_code` 或卫星专属属性，并通过 `valid_mask` 判断有效性。
- 为避免 xarray 自动 CF 解码时把整型 DN/count 转成浮点 masked array，读取原始计数层时可使用 `xr.open_dataset(path, mask_and_scale=False)`。
- `radiance`、`reflectance`、`brightness_temperature`、经纬度和角度变量可以使用 NaN 作为无效值。

## 8. 质量控制

`valid_mask` 只表达最终二元有效性。`quality_flag` 使用 bit-field，建议初始定义如下：

| bit | 值 | 含义 |
|---|---|---|
| 0 | 1 | invalid raw / DN / packed value |
| 1 | 2 | invalid geolocation |
| 2 | 4 | off disk |
| 3 | 8 | calibration failed |
| 4 | 16 | high view angle |
| 5 | 32 | sun angle invalid |
| 6 | 64 | suspicious source quality |
| 7 | 128 | reserved |

`valid_mask = 1` 通常要求 `quality_flag == 0`，但后续可按应用需要允许某些 warning bit 仍参与重采样。

## 9. 几何角度变量

| 变量 | 维度 | 单位 | 说明 |
|---|---|---|---|
| `sensor_zenith_angle` | `y, x` | degree | 卫星观测天顶角 |
| `sensor_azimuth_angle` | `y, x` | degree | 卫星观测方位角 |
| `solar_zenith_angle` | `y, x` | degree | 太阳天顶角 |
| `solar_azimuth_angle` | `y, x` | degree | 太阳方位角 |

全局属性需要明确：

| 属性 | 建议值 |
|---|---|
| `geolocation_method` | `official_native` / `approximate_inverse_geostationary` / `external_navigation` |
| `angle_computation_method` | `official_native` / `exact_geometry` / `approximate_geometry` |

## 10. 通道元数据变量

| 变量 | 维度 | 说明 |
|---|---|---|
| `native_channel_id` | `channel` | 原始通道名，如 `NOMChannel13` |
| `standard_channel_slot` | `channel` | 统一产品语义槽位，如 `IR_WINDOW_MAIN` |
| `standard_channel_id` | `channel` | 兼容字段，默认等同于 `standard_channel_slot` |
| `channel_group` | `channel` | `core_common` 或 `extension` |
| `channel_presence_flag` | `channel` | 该卫星/传感器是否具备该通道能力 |
| `channel_data_available_flag` | `channel` | 该文件该时次该通道是否实际有数据 |
| `native_channel_center_um` | `channel` | 原生通道中心波长 |
| `central_wavelength_um` | `channel` | 兼容字段，默认等同于 `native_channel_center_um` |
| `physical_quantity_type` | `channel` | 首选物理量：`radiance` / `reflectance` / `brightness_temperature` / `count` |
| `standard_units` | `channel` | 首选物理量单位 |
| `raw_data_type` | `channel` | `DN` / `count` / `packed_radiance` / `lut_code` |
| `calibration_type` | `channel` | `reflectance_lookup_table`、`brightness_temperature_lookup_table` 等 |
| `native_spatial_resolution_km` | `channel` | 原始空间分辨率 |

## 11. 核心公共通道槽位

公共通道层建议使用功能槽位名，而不是把槽位名写成过于精确的波长：

| 槽位 | 目标谱段 |
|---|---|
| `VIS_RED` | 0.65 μm 可见光 |
| `NIR_VEG` | 0.86 μm 近红外 |
| `NIR_SNOW` | 1.6 μm 短波红外 |
| `SWIR_CLOUD` | 2.2 μm 短波红外 |
| `SWIR_FOG` | 3.8 / 3.9 μm |
| `WV_UPPER` | 6.2 μm 水汽 |
| `WV_LOWER` | 7.3 μm 水汽 |
| `IR_86` | 8.6 μm |
| `IR_WINDOW_LOWER` | 10.3 / 10.4 μm 窗区 |
| `IR_WINDOW_MAIN` | 10.8 / 11.0 / 11.2 μm 主窗区 |
| `IR_SPLIT_WINDOW` | 12.0 / 12.3 / 12.4 μm 分裂窗 |

各卫星特有但有价值的通道进入扩展通道层，例如 `VIS_BLUE`、`CIRRUS_138`、`CO2_133` 等。

## 12. 全局属性

| 属性 | 说明 |
|---|---|
| `Conventions` | 建议 `CF-1.8` |
| `product_name` | `standardized_L1_source` |
| `product_version` | 规范版本 |
| `institution` | 机构或项目名 |
| `history` | 处理历史 |
| `processing_software_version` | 处理软件版本 |
| `satellite_id` | 统一卫星 ID |
| `platform_name` | 平台名称 |
| `sensor_id` | 传感器 ID |
| `scene_type` | `FD` / `REG` / `MESO` / `UNK` |
| `source_file` | 原始文件路径 |
| `source_file_format` | 原始文件格式 |
| `source_l1b_version` | 源 L1B 版本 |
| `calibration_reference` | 定标参考 |
| `spectral_response_reference` | 光谱响应参考 |
| `nominal_time_utc` | UTC 名义时次 |
| `observation_start_time_utc` | UTC 起始时间 |
| `observation_end_time_utc` | UTC 结束时间 |
| `geolocation_method` | 经纬度生成方法 |
| `angle_computation_method` | 几何角度计算方法 |
| `notes` | 说明 |

## 13. 后续 L1g 层关系

L1g 产品读取 `standardized_L1_source` 后，把各源像元投影到 0.05° 全球网格。

每个格点建议输出：

- `primary layer`
- `secondary layer`
- `tertiary layer`

初始排序规则：

```text
sensor_zenith_angle 从小到大排序
```

后续准同步产品可加入时间距离：

```text
score = f(sensor_zenith_angle, abs(pixel_or_line_time - target_time), quality_flag)
```

## 14. FY4B v0.2 实现说明

FY4B AGRI 当前实现：

- `raw_count` 保存原始 DN。
- `raw_count` 无效码为 65535，保存在 `raw_fill_value_code` 属性；逐像元有效性以 `valid_mask` 为准。
- 可见光/近红外/短波反射通道写入 `reflectance`。
- 红外通道写入 `brightness_temperature`。
- `radiance` 当前保留为 NaN，因为 FY4B 当前输入文件使用 LUT 直接给出反射率或亮温。
- `calibrated_value` 保留为兼容层，保存每个通道首选物理量。
- `latitude` / `longitude` 由 FY4B geostationary 投影近似反算生成。
- `line_time_offset` 在观测起止时间之间按扫描行线性近似。
- `sensor_zenith_angle` / `sensor_azimuth_angle` 由卫星位置和地面像元近似计算。
- `solar_zenith_angle` / `solar_azimuth_angle` 由观测中间时刻近似计算。
- `quality_flag` 当前包含 invalid raw、invalid geolocation、off disk、calibration failed、high view angle 和 sun angle invalid 等基础 bit。
