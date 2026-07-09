# 多静止卫星统一 L1g 前期工作阶段总结

更新时间：2026-05-11

这份文档用于总结截至目前多静止卫星统一产品链的前期工作进展。它面向后续两类用途：

- 作为组会 / 阶段汇报的书面底稿
- 作为进入 `single-satellite L1g` 之前的技术冻结版参考

本文档尽量把“做了什么、为什么这么做、目前到哪一步、还缺什么”讲清楚，并和现有代码、规范文档、样例输出、预览图保持一致。

## 1. 项目目标与总体产品链

当前工作的总体目标，是构建面向多静止气象卫星的统一全球 Level-1g 产品链。当前纳入的数据源包括：

- FY4B / AGRI
- GOES-16 / ABI
- GOES-18 / ABI
- Himawari-9 / AHI
- Meteosat-9 / SEVIRI
- Meteosat-10 / SEVIRI

我们当前明确采用的产品链是：

```text
官方/原生 L1B
-> Satpy reader-specific backend
-> standardized_L1_source
-> single-satellite L1g
-> multi-satellite unified global L1g
```

截至目前，已经基本完成的是前三层：

- 原始/官方 L1B 的结构探查
- `Satpy reader-specific backend` 的打通与收敛
- `standardized_L1_source` 中间层规范和样例实现

尚未正式进入的是：

- `single-satellite L1g`
- `multi-satellite unified global L1g`

因此，当前阶段的准确定位是：

> 已完成统一输入层和标准化中间层的主体搭建，并把这部分从原型推进到了可验证、可批处理、可回归的状态。

## 2. 当前阶段的核心目标

在正式做 L1g 之前，我们把前期工作拆成了四个彼此衔接的任务：

1. 探查原始数据结构  
   搞清楚每颗卫星的原始数据文件结构、原始值语义、通道组织、标定路径和几何信息来源。

2. 建立跨卫星通道映射  
   把各卫星原生通道编号整理成统一语义槽位，为后续统一产品提供通道层接口。

3. 构建预览与质量检查流程  
   在统一的全球规则网格上生成 quicklook 图，检查日照时次、空间覆盖、通道行为和重采样结果。

4. 构建 `standardized_L1_source` 中间层  
   把不同卫星的原始产品整理成统一变量命名、统一物理量分层、统一时间表达、统一几何接口的标准化输入层。

## 3. 原始数据结构探查结论

### 3.1 FY4B / AGRI

已完成对 FY4B AGRI L1 HDF 文件的结构探查。当前确认：

- 原始观测数组 `Data/NOMChannelXX` 为 `DN`
- `Calibration/CALChannelXX` 提供逐通道 LUT
- 反射类通道经 LUT 转换后对应 `reflectance`
- 热红外 / 水汽通道经 LUT 转换后对应 `brightness_temperature`

对应探查入口：

- [D:\AAAresearch_paper\third_report\code\FY4B\fy4b_l1_unit_probe.ipynb](D:\AAAresearch_paper\third_report\code\FY4B\fy4b_l1_unit_probe.ipynb)

### 3.2 GOES-16 / GOES-18 / ABI

已确认：

- 源文件主观测量 `Rad` 为 packed radiance
- 需通过 `scale_factor + add_offset` 解码
- 可见反射通道可进一步得到 `reflectance`
- 红外通道可进一步通过 Planck 参数得到 `brightness_temperature`

对应探查入口：

- [D:\AAAresearch_paper\third_report\code\GOES\goes_abi_l1_unit_probe.ipynb](D:\AAAresearch_paper\third_report\code\GOES\goes_abi_l1_unit_probe.ipynb)

### 3.3 Himawari-9 / AHI

已确认：

- 原始 HSD 产品底层可读取 `count`
- 通过 Satpy reader 可直接读取 `counts / radiance / reflectance / brightness_temperature`
- 适合采用 reader-based calibration 路线

对应探查入口：

- [D:\AAAresearch_paper\third_report\code\Himawari\himawari9_l1_unit_probe.ipynb](D:\AAAresearch_paper\third_report\code\Himawari\himawari9_l1_unit_probe.ipynb)

### 3.4 Meteosat-9 / Meteosat-10 / SEVIRI

已确认：

- 通过 Satpy reader 可直接读取 `counts / radiance / reflectance / brightness_temperature`
- HRV 与常规 VIS/IR 通道原生几何不同，需单独认识其空间覆盖特征

对应探查入口：

- [D:\AAAresearch_paper\third_report\code\Meteosat\meteosat_seviri_l1_unit_probe.ipynb](D:\AAAresearch_paper\third_report\code\Meteosat\meteosat_seviri_l1_unit_probe.ipynb)

## 4. 通道统一映射工作

跨卫星通道统一映射已经完成，主要成果位于：

- [D:\AAAresearch_paper\third_report\Satellite_Data_20240312\channel_mapping\channel_mapping_long.csv](D:\AAAresearch_paper\third_report\Satellite_Data_20240312\channel_mapping\channel_mapping_long.csv)
- [D:\AAAresearch_paper\third_report\Satellite_Data_20240312\channel_mapping\common_channel_matrix.csv](D:\AAAresearch_paper\third_report\Satellite_Data_20240312\channel_mapping\common_channel_matrix.csv)
- [D:\AAAresearch_paper\third_report\Satellite_Data_20240312\channel_mapping\extension_channel_matrix.csv](D:\AAAresearch_paper\third_report\Satellite_Data_20240312\channel_mapping\extension_channel_matrix.csv)
- [D:\AAAresearch_paper\third_report\Satellite_Data_20240312\channel_mapping\channel_availability_summary.csv](D:\AAAresearch_paper\third_report\Satellite_Data_20240312\channel_mapping\channel_availability_summary.csv)
- [D:\AAAresearch_paper\third_report\Satellite_Data_20240312\channel_mapping\channel_mapping.json](D:\AAAresearch_paper\third_report\Satellite_Data_20240312\channel_mapping\channel_mapping.json)

这些文件的角色分工是：

- `channel_mapping_long.csv`：最完整的长表底稿
- `common_channel_matrix.csv`：公共核心通道矩阵
- `extension_channel_matrix.csv`：扩展通道矩阵
- `channel_availability_summary.csv`：覆盖情况摘要
- `channel_mapping.json`：程序调用配置

这一步的意义在于：

> 后续 standardized 和 L1g 的通道组织，不再依赖各卫星各自的原生编号，而是进入统一语义槽位体系。

## 5. 全球规则网格基础

当前已明确采用的规则网格基础为：

- 经纬度范围：`[-180, 180] x [-90, 90]`
- 分辨率：`0.05°`

对应网格规格入口：

- [D:\AAAresearch_paper\third_report\code\L1g\global_grid_005_spec.ipynb](D:\AAAresearch_paper\third_report\code\L1g\global_grid_005_spec.ipynb)

这一步当前主要用于：

- quicklook 预览
- 后续 L1g 落格策略设计

目前还没有在这一层正式进入主产品输出。

## 6. 多卫星预览流程

### 6.1 预览流程目的

在正式做 L1g 前，先将各卫星原始像元独立映射到统一规则网格上做 quicklook 检查，主要用于：

- 检查所选 UTC 时次是否位于日照半球
- 检查空间覆盖是否合理
- 检查不同通道组的成像形态
- 检查预处理和重采样流程是否基本正常

### 6.2 当前状态

当前六类卫星的预览流程都已实现：

- FY4B
- GOES-16
- GOES-18
- Himawari-9
- Meteosat-9
- Meteosat-10

并且当前预览层已经统一到 Satpy 路线：

- FY4B：已切换到 `Satpy + GEO first`
- GOES：Satpy
- Himawari：Satpy
- Meteosat：Satpy

FY4B 当前预览入口：

- [D:\AAAresearch_paper\third_report\code\FY4B\fy4b_global_preview_satpy.py](D:\AAAresearch_paper\third_report\code\FY4B\fy4b_global_preview_satpy.py)
- [D:\AAAresearch_paper\third_report\code\FY4B\fy4b_global_grid_preview_005.ipynb](D:\AAAresearch_paper\third_report\code\FY4B\fy4b_global_grid_preview_005.ipynb)

统一总调用入口：

- [D:\AAAresearch_paper\third_report\code\preview_runner.py](D:\AAAresearch_paper\third_report\code\preview_runner.py)

### 6.3 当前代表性预览时次

为尽量选择各自所在半球白天时段，当前 quicklook 代表性时次为：

- FY4B：UTC 03
- GOES-16：UTC 18
- GOES-18：UTC 21
- Himawari-9：UTC 03
- Meteosat-9：UTC 09
- Meteosat-10：UTC 12

## 7. Satpy reader-specific backend 层收敛情况

这一层是当前阶段的关键收敛成果之一。

准确的当前状态是：

- FY4B / AGRI：**Satpy + GEO first**
- GOES-16 / GOES-18 / ABI：**Satpy 为默认生产后端，direct 保留为回归路径**
- Himawari-9 / AHI：**Satpy**
- Meteosat-9 / Meteosat-10 / SEVIRI：**Satpy**

### 7.1 FY4B

FY4B 已不再把“自写 LUT + 近似几何”作为唯一主路径，而是收敛到：

- calibrated 通道值：Satpy 主路径
- 有 GEO 文件时：Satpy 官方几何角度场主路径
- 无 GEO 文件时：近似 fallback

对应入口：

- [D:\AAAresearch_paper\third_report\code\FY4B\fy4b_standardized_l1_source_builder.py](D:\AAAresearch_paper\third_report\code\FY4B\fy4b_standardized_l1_source_builder.py)
- [D:\AAAresearch_paper\third_report\code\FY4B\fy4b_satpy_self_check.py](D:\AAAresearch_paper\third_report\code\FY4B\fy4b_satpy_self_check.py)

### 7.2 GOES

GOES 当前已具备双后端：

- `backend="satpy"`：当前默认推荐
- `backend="direct"`：保留为官方变量直读回归路径

对应入口：

- [D:\AAAresearch_paper\third_report\code\GOES\goes_standardized_l1_source_builder.py](D:\AAAresearch_paper\third_report\code\GOES\goes_standardized_l1_source_builder.py)
- [D:\AAAresearch_paper\third_report\code\GOES\goes_satpy_self_check.py](D:\AAAresearch_paper\third_report\code\GOES\goes_satpy_self_check.py)

### 7.3 Himawari / Meteosat

两者已经稳定复用 Satpy 共用标准化骨架：

- [D:\AAAresearch_paper\third_report\code\standardized_l1_source_satpy.py](D:\AAAresearch_paper\third_report\code\standardized_l1_source_satpy.py)

对应 builder：

- [D:\AAAresearch_paper\third_report\code\Himawari\himawari_standardized_l1_source_builder.py](D:\AAAresearch_paper\third_report\code\Himawari\himawari_standardized_l1_source_builder.py)
- [D:\AAAresearch_paper\third_report\code\Meteosat\meteosat_standardized_l1_source_builder.py](D:\AAAresearch_paper\third_report\code\Meteosat\meteosat_standardized_l1_source_builder.py)

## 8. standardized_L1_source 规范收敛

规范文档位于：

- [D:\AAAresearch_paper\third_report\code\L1g\standardized_L1_source_spec.md](D:\AAAresearch_paper\third_report\code\L1g\standardized_L1_source_spec.md)

当前版本已收敛到 `v0.2`，关键点包括：

- 时间采用 CF numeric time 为主表达
- `raw_count` 与 calibrated 物理量层分离
- `radiance / reflectance / brightness_temperature` 分层
- `calibrated_value` 作为兼容层保留
- `valid_mask + quality_flag(bit-field)` 形成基础质量控制框架
- `channel_presence_flag / channel_data_available_flag` 用于区分通道能力与通道实际存在性
- `grid_mapping` 明确采用 geostationary projection

### 8.1 `projection_x / projection_y` 的正式定义

这是近期最重要的一次规范收口。

当前正式定义已经统一为：

> `projection_x / projection_y` 表示与文件 `grid_mapping` 配套使用的原生静止投影坐标轴（native geostationary projection coordinate axes），单位统一为 `m`。

这意味着：

- 不再把 GOES 的 scan angle 作为主变量正式统一语义
- 不同卫星进入 standardized 后，都尽量统一到投影轴语义
- 若后续需要保留 scan angle，应作为附加变量或附加元数据，不再占用统一主变量语义

这是为了：

- 增强跨星一致性
- 让 standardized 更像统一产品接口，而不是原始源文件镜像
- 更稳定地服务后续 L1g 重采样和定位

## 9. standardized 主线实现与入口

### 9.1 核心入口

- 批处理入口：  
  [D:\AAAresearch_paper\third_report\code\run_standardized_l1_source_batch.py](D:\AAAresearch_paper\third_report\code\run_standardized_l1_source_batch.py)

- 样例自动校验入口：  
  [D:\AAAresearch_paper\third_report\code\validate_standardized_l1_source_samples.py](D:\AAAresearch_paper\third_report\code\validate_standardized_l1_source_samples.py)

### 9.2 当前 build / validate 工作流

当前已经将流程明确拆分为两步：

1. `build`  
   负责从原始数据重新生成 standardized 文件。  
   这一步慢，是正常现象。

2. `validate`  
   负责读取已生成样例并检查关键 schema / dtype / unit / 变量语义。  
   这一步应当很快。

这样可以清晰地区分：

- 哪一步是在重新生产产品
- 哪一步只是在做结果验收

## 10. 已修复的重要 bug 与工程问题

### 10.1 Satpy 路线 `projection_x / projection_y` 写成 NaN

这是一个真实出现过的 bug。  
原因是写 NetCDF 时轴变量与 coords 发生不恰当对齐，导致 Satpy 路线下的投影轴被污染。

当前已修复。

### 10.2 Himawari / Meteosat `raw_count` 漂成 float32

这与“保留原始整数层”的设计不一致。  
当前已修复为：

- Himawari：`uint16`
- Meteosat：`uint16`

并保留 `raw_fill_value_code`。

### 10.3 GOES `projection_x / projection_y` 语义不统一

此前 GOES direct 路径更像 scan angle，而 Satpy 路径更像投影坐标轴。  
当前已通过规范和默认实现一起收口，正式统一到投影轴语义。

### 10.4 GOES-16 样例文件锁

曾有一次被中断流程留下的后台 Python 进程锁住 GOES-16 样例文件。  
当前已：

- 定位锁源
- 清理相关残留进程
- 干净重建样例文件

该问题已收尾。

## 11. 当前自动校验结果

2026-05-11 再次运行：

- [D:\AAAresearch_paper\third_report\code\validate_standardized_l1_source_samples.py](D:\AAAresearch_paper\third_report\code\validate_standardized_l1_source_samples.py)

样例通过情况为：

- FY4B：OK
- GOES-18：OK
- Himawari-9：OK
- Meteosat-9：OK

校验内容包括：

- required variables 是否存在
- `raw_count` 是否保留整数层
- `valid_mask` / `quality_flag` dtype
- `projection_x / projection_y` 单位与非 NaN 性
- 时间变量是否使用统一 CF 单位
- `grid_mapping` 是否存在且为 `geostationary`

这意味着：

> 当前主线样例输出与规范文档在核心变量、dtype、单位和几何接口层面是一致的。

## 12. 代表性样例与参考文件

### 12.1 standardized 样例

- FY4B：  
  [D:\AAAresearch_paper\third_report\Satellite_Data_20240312\standardized_L1_source\FY4B\FY4B_AGRI_FD_20240312_0300_standardized_L1_source_v0.2.nc](D:\AAAresearch_paper\third_report\Satellite_Data_20240312\standardized_L1_source\FY4B\FY4B_AGRI_FD_20240312_0300_standardized_L1_source_v0.2.nc)

- GOES-16 / C13：  
  [D:\AAAresearch_paper\third_report\Satellite_Data_20240312\standardized_L1_source\GOES-16\C13\GOES16_ABI_FD_C13_20240312_1800_standardized_L1_source_v0.2.nc](D:\AAAresearch_paper\third_report\Satellite_Data_20240312\standardized_L1_source\GOES-16\C13\GOES16_ABI_FD_C13_20240312_1800_standardized_L1_source_v0.2.nc)

- GOES-18 / C13：  
  [D:\AAAresearch_paper\third_report\Satellite_Data_20240312\standardized_L1_source\GOES-18\C13\GOES18_ABI_FD_C13_20240312_2100_standardized_L1_source_v0.2.nc](D:\AAAresearch_paper\third_report\Satellite_Data_20240312\standardized_L1_source\GOES-18\C13\GOES18_ABI_FD_C13_20240312_2100_standardized_L1_source_v0.2.nc)

- Himawari-9 / B13：  
  [D:\AAAresearch_paper\third_report\Satellite_Data_20240312\standardized_L1_source\Himawari-9\B13\Himawari9_AHI_FD_B13_20240312_0300_standardized_L1_source_v0.2.nc](D:\AAAresearch_paper\third_report\Satellite_Data_20240312\standardized_L1_source\Himawari-9\B13\Himawari9_AHI_FD_B13_20240312_0300_standardized_L1_source_v0.2.nc)

- Meteosat-9 / IR_108：  
  [D:\AAAresearch_paper\third_report\Satellite_Data_20240312\standardized_L1_source\Meteosat-9\IR_108\Meteosat9_SEVIRI_FD_IR_108_20240312_0900_standardized_L1_source_v0.2.nc](D:\AAAresearch_paper\third_report\Satellite_Data_20240312\standardized_L1_source\Meteosat-9\IR_108\Meteosat9_SEVIRI_FD_IR_108_20240312_0900_standardized_L1_source_v0.2.nc)

### 12.2 基线预览图

为了后续和 L1g 结果做对照，已整理一组基线 quicklook 索引：

- [D:\AAAresearch_paper\third_report\code\preview_baselines\README.md](D:\AAAresearch_paper\third_report\code\preview_baselines\README.md)

## 13. 当前仍未完成的内容

尽管这一阶段已经比较完整，但还没有正式完成：

- `single-satellite L1g` 像元到规则网格的正式落格实现
- `primary / secondary / tertiary` 层次输出
- 视角与时间联合排序策略
- `multi-satellite unified global L1g`
- 大规模全量批处理与长期回归测试体系

因此当前阶段最准确的判断是：

> 输入层、标准化层和预览层已经基本扎实；真正的统一格点产品层还没有开始正式落地。

## 14. 现阶段综合评价

如果从工程成熟度来评价，当前已经比较稳的部分包括：

- 原始数据物理语义理解
- 跨卫星通道映射
- 统一预览流程
- Satpy backend 收敛
- standardized 规范与样例实现
- 主线变量和几何接口校验

仍需谨慎推进的部分包括：

- L1g 像元优选与排序策略
- 单星落格算法细节
- 多星联合时的冲突处理
- 大规模生成时的性能、存储和版本管理

## 15. 建议的 PPT 章节结构

如果用于汇报老师，推荐按下面的逻辑组织 PPT：

1. 研究目标与产品链定位
2. 原始数据结构探查结论
3. 跨卫星通道统一映射
4. 全球规则网格与预览检查流程
5. Satpy reader-specific backend 收敛情况
6. `standardized_L1_source` 规范设计与关键决策
7. 样例实现与自动校验结果
8. 当前阶段成果与下一步计划

## 16. 一句话结论

截至 2026-05-11，我们已经把多静止卫星统一 L1g 产品链的前半段真正搭起来了：原始结构探查、通道映射、统一预览、Satpy reader/backend 收敛、`standardized_L1_source` 规范与样例实现，都已经进入可验证、可复用、可汇报的状态。
