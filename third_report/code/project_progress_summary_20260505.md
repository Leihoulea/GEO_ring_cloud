# 多静止卫星统一 L1g 产品项目阶段总结

## FY4B 最新状态更新（2026-05-10）

FY4B 的 `standardized_L1_source` 主路线已经完成一次重要收敛：

- 默认 builder 仍是 [fy4b_standardized_l1_source_builder.py](D:/AAAresearch_paper/third_report/code/FY4B/fy4b_standardized_l1_source_builder.py)
- 默认后端已切换为 `backend="satpy"`
- builder 会自动在整个 FY4B 根目录下跨目录匹配同时间段 GEO 文件
- 一旦找到 GEO 文件，`sensor_*` 与 `solar_*` 角度场优先使用 Satpy 官方几何路径
- 此时输出 attrs 会记录 `backend = satpy`、`geolocation_method = official_native`、`angle_computation_method = official_native`
- 旧的 `native_lut` 路线保留为回归对照路径，不再作为默认生产路径

这意味着 FY4B 现在已经不仅是 “Satpy 负责辐射值”，而是已经进入 **Satpy + GEO 优先** 的生产状态。

## GOES 最新状态更新（2026-05-11）
GOES 的 `Satpy reader-specific backend` 也已经补齐并完成对照验证：

- `abi_l1b` reader 已通过自检
- `counts` 与源文件 `Rad` 逐点一致
- `radiance` 与 `scale_factor + add_offset` 逐点一致
- `brightness_temperature` 与直接 Planck 计算逐点一致
- 可见光 `reflectance` 在单位归一化后与直接解码一致
- `goes_standardized_l1_source_builder.py` 现在同时保留：
  - `backend="satpy"`：推荐默认生产路径
  - `backend="direct"`：官方变量直读回归路径

同时，`projection_x / projection_y` 的统一口径已经明确：

- 不再以 GOES 原始 scan angle 作为 `standardized_L1_source` 的正式统一语义
- 统一采用 **native geostationary projection coordinate axes**
- 因此 GOES 的推荐默认 backend 已调整为 **Satpy**

更新时间：2026-05-05  
适用场景：组会汇报 / PPT 提纲 / 阶段性记录

---

## 1. 项目目标

本项目的总体目标，是构建一条面向多静止气象卫星的统一 Level-1g 产品链。当前涉及的数据源包括：

- FY4B / AGRI
- GOES-16 / ABI
- GOES-18 / ABI
- Himawari-9 / AHI
- Meteosat-9 / SEVIRI
- Meteosat-10 / SEVIRI

目标处理链为：

```text
官方/原生 L1B
    -> standardized_L1_source
    -> 单星 L1g
    -> 多星统一全球 L1g
```

当前阶段工作的重点，是完成从原始 L1B 到 `standardized_L1_source` 的前期准备与原型实现，为后续正式 L1g 产品构建打基础。

---

## 2. 已明确的总体技术路线

目前已经明确的技术路线如下：

1. 先构建规则经纬度全球网格
2. 初步网格分辨率设为 `0.05°`
3. 暂时不做跨星融合
4. 先将每颗卫星的原始像元独立映射到全球规则网格
5. 在正式进入 L1g 之前，先完成：
   - 原始数据结构探查
   - 通道统一映射
   - 预览自动化
   - 中间层标准化

这一分层保证了产品职责清晰，避免把“源数据标准化”“单星格点化”“跨星融合”混在同一个处理阶段。

---

## 3. 已完成的原始数据结构探查

### 3.1 探查目标

探查内容主要包括：

- 原始文件格式
- 原始像元组织方式
- 通道结构
- 原始值单位
- 标定方式
- 可用几何/时间信息

### 3.2 各卫星主要结论

#### FY4B / AGRI

- 原始 `Data/NOMChannelXX` 为 `DN`
- `Calibration/CALChannelXX` 提供查找表
- 可见光/近红外通道通过 LUT 转换到反射率量级
- 红外通道通过 LUT 转换到亮温量级

相关 notebook：

- [fy4b_l1_unit_probe.ipynb](D:/AAAresearch_paper/third_report/code/FY4B/fy4b_l1_unit_probe.ipynb)

#### GOES-16 / GOES-18 / ABI

- `Rad` 为 packed radiance
- 需使用 `scale_factor + add_offset` 解码
- 红外通道可结合 Planck 参数转换为亮温

相关 notebook：

- [goes_abi_l1_unit_probe.ipynb](D:/AAAresearch_paper/third_report/code/GOES/goes_abi_l1_unit_probe.ipynb)

#### Himawari-9 / AHI

- 原始 HSD 文件可读取 count
- Satpy 可进一步读取 radiance、亮温等物理量

相关 notebook：

- [himawari9_l1_unit_probe.ipynb](D:/AAAresearch_paper/third_report/code/Himawari/himawari9_l1_unit_probe.ipynb)

#### Meteosat-9 / Meteosat-10 / SEVIRI

- Satpy 可读取 counts、radiance、reflectance、brightness temperature
- HRV 与常规 VIS/IR 通道原生几何覆盖不同

相关 notebook：

- [meteosat_seviri_l1_unit_probe.ipynb](D:/AAAresearch_paper/third_report/code/Meteosat/meteosat_seviri_l1_unit_probe.ipynb)

### 3.3 本阶段意义

这一阶段解决了后续标准化中最基础的问题：不同卫星原始数据到底是什么、是否是 DN、是否已经是辐亮度、需要怎样标定。这一步为中间层规范设计提供了直接依据。

---

## 4. 已完成的通道统一映射

### 4.1 工作内容

为了后续实现跨卫星统一产品，已经对各卫星通道进行了系统梳理，并区分为：

- 核心公共通道
- 扩展通道

### 4.2 已形成的成果文件

- [channel_mapping_long.csv](D:/AAAresearch_paper/third_report/Satellite_Data_20240312/channel_mapping/channel_mapping_long.csv)
- [common_channel_matrix.csv](D:/AAAresearch_paper/third_report/Satellite_Data_20240312/channel_mapping/common_channel_matrix.csv)
- [extension_channel_matrix.csv](D:/AAAresearch_paper/third_report/Satellite_Data_20240312/channel_mapping/extension_channel_matrix.csv)
- [channel_availability_summary.csv](D:/AAAresearch_paper/third_report/Satellite_Data_20240312/channel_mapping/channel_availability_summary.csv)
- [channel_mapping.json](D:/AAAresearch_paper/third_report/Satellite_Data_20240312/channel_mapping/channel_mapping.json)

### 4.3 本阶段意义

通道映射解决了“不同卫星同类通道如何对齐”的问题，为后续统一语义槽位、单星 L1g 和多星融合奠定了基础。

---

## 5. 已完成的全球规则网格基础定义

当前已定义统一的全球规则经纬度网格：

- 全球范围：`[-180, 180] x [-90, 90]`
- 空间分辨率：`0.05°`
- 经向格点数：约 `7200`
- 纬向格点数：约 `3600`

相关 notebook：

- [global_grid_005_spec.ipynb](D:/AAAresearch_paper/third_report/code/L1g/global_grid_005_spec.ipynb)

### 本阶段意义

这一部分为后续单星 L1g 提供统一输出坐标框架，使所有卫星最终可以投影到同一全球规则格点体系上。

---

## 6. 已完成的多卫星预览自动化流程

### 6.1 工作目标

在正式 L1g 映射前，先对各卫星数据进行全球网格预览，主要目的是：

- 检查时次是否位于日照半球
- 检查重采样后的空间覆盖是否合理
- 检查通道图像质量
- 为后续产品构建提供可视化直觉

### 6.2 已完成的卫星预览 notebook

- [fy4b_global_grid_preview_005.ipynb](D:/AAAresearch_paper/third_report/code/FY4B/fy4b_global_grid_preview_005.ipynb)
- [goes16_global_grid_preview_005.ipynb](D:/AAAresearch_paper/third_report/code/GOES/goes16_global_grid_preview_005.ipynb)
- [goes18_global_grid_preview_005.ipynb](D:/AAAresearch_paper/third_report/code/GOES/goes18_global_grid_preview_005.ipynb)
- [himawari9_global_grid_preview_005.ipynb](D:/AAAresearch_paper/third_report/code/Himawari/himawari9_global_grid_preview_005.ipynb)
- [meteosat9_global_grid_preview_005.ipynb](D:/AAAresearch_paper/third_report/code/Meteosat/meteosat9_global_grid_preview_005.ipynb)
- [meteosat10_global_grid_preview_005.ipynb](D:/AAAresearch_paper/third_report/code/Meteosat/meteosat10_global_grid_preview_005.ipynb)

### 6.3 已实现的功能特征

- 每颗卫星均可生成：
  - 可见光/近红外合成图
  - 红外亮温合成图
  - 其他通道预览图
- 统一采用 `TARGET_HOUR_UTC` 控制时次
- 改一个变量即可同步修改：
  - 选取文件
  - 图标题
  - 输出文件名
- 对假彩色构成和显示拉伸方式做了注释说明
- 对 Meteosat HRV 不规则覆盖形状做了解释

### 6.4 自动化调用入口

- [preview_runner.py](D:/AAAresearch_paper/third_report/code/preview_runner.py)
- [run_all_previews.ipynb](D:/AAAresearch_paper/third_report/code/run_all_previews.ipynb)

### 6.5 本阶段意义

这一阶段使数据检查从“单次人工试验”升级为“可重复、可批量执行的自动化 quicklook 流程”。

---

## 7. 已形成阶段性总结文档

此前已经整理过一版阶段总结文档：

- 旧版阶段总结已被当前文档取代。

该文档主要记录了：

- 数据探查阶段结果
- 预览自动化阶段结果
- 网格基础与项目思路
- 输出图像示例链接

当前这份文档是在其基础上的一次更全面扩展，更适合直接作为组会汇报材料底稿。

---

## 8. 已定义 standardized_L1_source 中间层规范

### 8.1 设计目标

`standardized_L1_source` 被定义为正式 L1g 产品链的中间层。其核心定位为：

- 输入：各卫星官方/原生 L1B 数据
- 输出：统一命名、统一语义、统一元数据结构的单星原始投影/原始扫描网格产品
- 明确不做：
  - 全球规则格点化
  - 跨星融合

### 8.2 规范文档

- [standardized_L1_source_spec.md](D:/AAAresearch_paper/third_report/code/L1g/standardized_L1_source_spec.md)

### 8.3 规范迭代过程

规范经历了 `v0.1 -> v0.2` 的迭代。`v0.2` 重点增强了以下内容：

- 使用 CF-compatible numeric time
- 保留 ISO-8601 可读时间属性
- 引入 `channel_presence_flag`
- 引入 `channel_data_available_flag`
- 将 `calibrated_value` 拆解为：
  - `radiance`
  - `reflectance`
  - `brightness_temperature`
- 保留 `calibrated_value` 作为兼容层
- 引入 bit-field `quality_flag`
- 引入 `line_time_offset`
- 引入 CF `grid_mapping`
- 将通道统一语义改进为 `standard_channel_slot`

### 8.4 本阶段意义

这一步标志着项目已经从“数据探查”进入“产品结构设计与标准化实现”阶段。

---

## 9. FY4B standardized_L1_source 已完整实现

### 9.1 相关代码与文件

- 构建模块：
  - [fy4b_standardized_l1_source_builder.py](D:/AAAresearch_paper/third_report/code/FY4B/fy4b_standardized_l1_source_builder.py)
- notebook 入口：
  - [fy4b_build_standardized_l1_source_v02.ipynb](D:/AAAresearch_paper/third_report/code/FY4B/fy4b_build_standardized_l1_source_v02.ipynb)
- 输出文件：
  - [FY4B_AGRI_FD_20240312_0300_standardized_L1_source_v0.2.nc](D:/AAAresearch_paper/third_report/Satellite_Data_20240312/standardized_L1_source/FY4B/FY4B_AGRI_FD_20240312_0300_standardized_L1_source_v0.2.nc)

### 9.2 主要实现内容

FY4B v0.2 已实现：

- 默认 `backend = satpy`
- 自动跨目录匹配同时间段 GEO 文件
- 若匹配 GEO 成功，则 `sensor_*` / `solar_*` 角度场走 Satpy 官方几何路径
- `native_lut` 路线保留为回归对照，不再是默认生产路径
- `raw_count`
- `reflectance`
- `brightness_temperature`
- `radiance` 占位
- `calibrated_value`
- `valid_mask`
- `quality_flag`
- `time`
- `observation_start_time`
- `observation_end_time`
- `line_time_offset`
- `projection_x`
- `projection_y`
- `latitude`
- `longitude`
- `sensor_zenith_angle`
- `sensor_azimuth_angle`
- `solar_zenith_angle`
- `solar_azimuth_angle`
- 通道级元数据
- CF 投影描述

### 9.3 本阶段意义

FY4B 已经从“探查对象”变成了“标准化中间层模板实现”。

---

## 10. standardized_L1_source 已扩展到其他卫星

### 10.1 GOES-16 / GOES-18

#### 实现思路

- ABI 通道分辨率差异较大
- 采用“按原生通道逐文件输出”的策略
- 保留固定网格投影坐标与 `goes_imager_projection`
- 不强行将多分辨率通道堆叠到同一个 `channel,y,x`

#### 相关代码

- [goes_standardized_l1_source_builder.py](D:/AAAresearch_paper/third_report/code/GOES/goes_standardized_l1_source_builder.py)
- [goes_build_standardized_l1_source_v02.ipynb](D:/AAAresearch_paper/third_report/code/GOES/goes_build_standardized_l1_source_v02.ipynb)

#### 已验证输出样例

- [GOES16_ABI_FD_C13_20240312_1800_standardized_L1_source_v0.2.nc](D:/AAAresearch_paper/third_report/Satellite_Data_20240312/standardized_L1_source/GOES-16/C13/GOES16_ABI_FD_C13_20240312_1800_standardized_L1_source_v0.2.nc)
- [GOES18_ABI_FD_C13_20240312_2100_standardized_L1_source_v0.2.nc](D:/AAAresearch_paper/third_report/Satellite_Data_20240312/standardized_L1_source/GOES-18/C13/GOES18_ABI_FD_C13_20240312_2100_standardized_L1_source_v0.2.nc)

### 10.2 Himawari-9

#### 实现思路

- 使用 Satpy `ahi_hsd`
- 按原生通道逐文件输出
- 保留原生静止卫星投影定义

#### 相关代码

- [himawari_standardized_l1_source_builder.py](D:/AAAresearch_paper/third_report/code/Himawari/himawari_standardized_l1_source_builder.py)
- [himawari9_build_standardized_l1_source_v02.ipynb](D:/AAAresearch_paper/third_report/code/Himawari/himawari9_build_standardized_l1_source_v02.ipynb)

#### 已验证输出样例

- [Himawari9_AHI_FD_B13_20240312_0300_standardized_L1_source_v0.2.nc](D:/AAAresearch_paper/third_report/Satellite_Data_20240312/standardized_L1_source/Himawari-9/B13/Himawari9_AHI_FD_B13_20240312_0300_standardized_L1_source_v0.2.nc)

### 10.3 Meteosat-9 / Meteosat-10

#### 实现思路

- 使用 Satpy `seviri_l1b_native`
- 按原生通道逐文件输出
- HRV 与常规 VIS/IR 通道保持独立处理空间

#### 相关代码

- [meteosat_standardized_l1_source_builder.py](D:/AAAresearch_paper/third_report/code/Meteosat/meteosat_standardized_l1_source_builder.py)
- [meteosat_build_standardized_l1_source_v02.ipynb](D:/AAAresearch_paper/third_report/code/Meteosat/meteosat_build_standardized_l1_source_v02.ipynb)

#### 已验证输出样例

- [Meteosat9_SEVIRI_FD_IR_108_20240312_0900_standardized_L1_source_v0.2.nc](D:/AAAresearch_paper/third_report/Satellite_Data_20240312/standardized_L1_source/Meteosat-9/IR_108/Meteosat9_SEVIRI_FD_IR_108_20240312_0900_standardized_L1_source_v0.2.nc)
- [Meteosat10_SEVIRI_FD_IR_108_20240312_1200_standardized_L1_source_v0.2.nc](D:/AAAresearch_paper/third_report/Satellite_Data_20240312/standardized_L1_source/Meteosat-10/IR_108/Meteosat10_SEVIRI_FD_IR_108_20240312_1200_standardized_L1_source_v0.2.nc)

---

## 11. 已抽取跨卫星共用标准化模块

为了避免 Himawari 与 Meteosat 重复实现，已抽取通用 Satpy 构建模块：

- [standardized_l1_source_satpy.py](D:/AAAresearch_paper/third_report/code/standardized_l1_source_satpy.py)

这一模块负责：

- 读取 counts / radiance / reflectance / brightness_temperature
- 统一生成：
  - `raw_count`
  - `radiance`
  - `reflectance`
  - `brightness_temperature`
  - `calibrated_value`
  - `valid_mask`
  - `quality_flag`
- 统一输出时间变量
- 统一输出投影坐标与 CF `grid_mapping`
- 统一输出中间层 NetCDF

这一层已经体现出代码模块化和可扩展性。

---

## 12. 已实现统一批处理入口

为了统一调用各卫星标准化流程，已增加统一 runner：

- [run_standardized_l1_source_batch.py](D:/AAAresearch_paper/third_report/code/run_standardized_l1_source_batch.py)

支持两种模式：

### `sample`

仅处理每颗卫星的代表性通道，用于快速验证整条链路。

示例：

```powershell
D:\anaconda\envs\pytorch\python.exe code\run_standardized_l1_source_batch.py --mode sample
```

### `all`

处理全部已配置通道，用于正式批处理构建。

示例：

```powershell
D:\anaconda\envs\pytorch\python.exe code\run_standardized_l1_source_batch.py --mode all
```

说明：

- `all` 模式下会处理 GOES C02、SEVIRI HRV 等较大通道
- 文件体积和运行时间会显著增加
- 当前阶段已用 `sample` 模式完成整链路验证

---

## 13. 已解决的重要工程问题

在实现过程中，已经识别并解决了一批关键工程问题：

### 13.1 xarray / CF 自动解码问题

- 整型 DN / count 在自动 mask / scale 后可能变成浮点
- 已在中间层中对 `raw_count` 的保存方式做针对性控制

### 13.2 时间变量兼容问题

- `line_time_offset` 的表达方式需要兼容 CF/xarray
- 当前已采用更稳妥的时间变量组织方式

### 13.3 Satpy calibration 对齐问题

- 同一通道不同 calibration 读出后坐标标签可能有细微差异
- 若直接按坐标对齐，会导致数据求交后几乎为空
- 已改为按原生数组位置一致性处理

### 13.4 Satpy CRS 对象无法直接写入 NetCDF

- Satpy 自带 `crs` 对象不适合直接落盘
- 已统一改为写数值型 `projection_x / projection_y + CF grid_mapping`

### 13.5 多分辨率通道组织问题

- GOES ABI / Himawari AHI / Meteosat SEVIRI 各通道空间分辨率不同
- 已明确不强行堆叠到同一网格
- 改为按原生通道逐文件输出

### 本阶段意义

这些问题的解决，使当前代码和规范更接近真实产品化流程，而不是停留在实验性脚本阶段。

---

## 14. 截至目前的成果状态

如果从项目推进阶段来看，目前已经完成了：

### 已完成

- 原始数据结构探查
- 多卫星通道统一梳理
- 全球规则网格基础定义
- 多卫星预览自动化
- 阶段性总结文档
- `standardized_L1_source` 规范设计
- FY4B 完整标准化实现
- GOES / Himawari / Meteosat 代表性标准化实现
- 统一批处理入口

### 当前形成的能力

当前项目已经具备：

- 面向多静止卫星的统一中间层设计能力
- 面向后续 L1g 的稳定输入接口
- 可重复的批处理与快速验证能力
- 继续向正式单星 L1g 映射推进的工程基础

---

## 15. 当前尚未完成的部分

虽然地基已经搭得比较扎实，但以下内容尚未正式完成：

1. 各卫星所有通道的 `standardized_L1_source` 全量生成
2. 从 `standardized_L1_source` 到单星 `L1g` 的正式映射代码
3. `primary / secondary / tertiary` 多层输出机制
4. 基于视角和时间的格点排序策略实现
5. 多星统一全球 L1g 融合产品
6. 更严格的逐像元几何角度和逐像元时间场
7. 更强的质量控制和官方质量标记接入

---

## 16. 当前阶段结论

截至目前，本项目已经完成了从“原始数据摸底”到“中间层标准化原型系统”的关键跨越。

可以用一句话概括当前状态：

> 我们已经不再停留在原始数据分析和零散试验阶段，而是已经建立起一条面向统一 L1g 产品构建的前半段工程链路，包括通道映射、预览自动化、规则网格基础和 standardized_L1_source 中间层原型实现。

这意味着下一阶段可以更聚焦地进入：

- 单星 L1g 原始像元到规则格点的正式映射
- 多层输出设计
- 后续多星统一融合

---

## 17. 建议的 PPT 章节结构

基于当前工作，组会 PPT 可以考虑按下面结构展开：

1. 研究背景与目标
2. 数据来源与卫星体系
3. 总体处理链设计
4. 原始数据结构探查结果
5. 通道统一映射设计
6. 全球规则网格与预览流程
7. standardized_L1_source 中间层规范
8. FY4B 标准化实现
9. GOES / Himawari / Meteosat 扩展实现
10. 当前成果与典型输出
11. 存在问题与下一步计划

如果需要，后续还可以在这份文档基础上继续补：

- “适合直接上 PPT 的精简版”
- “带图版汇报文档”
- “下一阶段工作计划页”
