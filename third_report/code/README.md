# Code Directory Guide

## Satpy Reader-Specific Backend Status

As of 2026-05-11, the `Satpy reader-specific backend` layer has been brought to
a usable state for all currently targeted geostationary satellites:

- `FY4B / AGRI`: production route is **Satpy + GEO first**
- `GOES-16 / GOES-18 / ABI`: both **direct** and **Satpy** backends are now available, and the recommended default is now **Satpy**
- `Himawari-9 / AHI`: production route uses **Satpy**
- `Meteosat-9 / Meteosat-10 / SEVIRI`: production route uses **Satpy**

Current interpretation:

- The backend layer is now **implemented** across FY4B, GOES, Himawari, and
  Meteosat.
- The GOES backend question has now been narrowed and resolved at the
  `projection_x / projection_y` semantics level: the recommended production
  default is Satpy, so that all geostationary satellites share the same
  geostationary projection-axis interpretation in `standardized_L1_source`.

Useful verification and entry files:

- FY4B self-check: [D:\AAAresearch_paper\third_report\code\FY4B\fy4b_satpy_self_check.py](D:\AAAresearch_paper\third_report\code\FY4B\fy4b_satpy_self_check.py)
- GOES self-check: [D:\AAAresearch_paper\third_report\code\GOES\goes_satpy_self_check.py](D:\AAAresearch_paper\third_report\code\GOES\goes_satpy_self_check.py)
- Shared Satpy helper: [D:\AAAresearch_paper\third_report\code\standardized_l1_source_satpy.py](D:\AAAresearch_paper\third_report\code\standardized_l1_source_satpy.py)
- Batch runner: [D:\AAAresearch_paper\third_report\code\run_standardized_l1_source_batch.py](D:\AAAresearch_paper\third_report\code\run_standardized_l1_source_batch.py)

## FY4B Route Update

As of 2026-05-10, the FY4B `standardized_L1_source` production route has been
switched to **Satpy + GEO first**.

- Default builder: `code/FY4B/fy4b_standardized_l1_source_builder.py`
- Default backend: `backend="satpy"`
- GEO matching: recursive cross-folder search under the full FY4B root
- Geometry priority: official Satpy angle datasets when a matching GEO file is found
- Legacy `native_lut` route: retained only for regression comparison

Recommended FY4B entry order:

```text
fy4b_satpy_self_check.py
    -> fy4b_standardized_l1_source_builder.py
    -> fy4b_build_standardized_l1_source_v02.ipynb
```

更新时间：2026-05-07

这份 README 用来说明 `code/` 目录中各文件夹、各模块、各入口脚本分别负责什么，以及现在推荐从哪里开始运行。

---

## 1. 总体结构

当前 `code/` 目录围绕三条主线组织：

1. 原始数据探查  
   目标：搞清楚各卫星原始文件结构、单位、通道和标定方式

2. 预览与检查  
   目标：把各卫星原始像元映射到统一全球规则网格上做 quicklook 检查

3. standardized_L1_source 中间层构建  
   目标：把不同卫星的官方/原生 L1B 统一整理成标准化中间层，为后续正式 L1g 产品做输入

推荐理解顺序：

```text
L1 unit probe
    -> global preview
    -> standardized_L1_source
    -> single-satellite L1g (后续阶段)
```

---

## 2. 当前最重要的入口

如果你只想记住最重要的几个入口，优先看这几个：

### 2.1 standardized_L1_source 批处理入口

- [run_standardized_l1_source_batch.py](D:/AAAresearch_paper/third_report/code/run_standardized_l1_source_batch.py)

用途：

- 统一调用 GOES / Himawari / Meteosat 的 standardized_L1_source 构建流程
- 支持 `sample` 和 `all` 两种模式

推荐命令：

```powershell
D:\anaconda\envs\pytorch\python.exe code\run_standardized_l1_source_batch.py --mode sample
```

说明：

- `sample` 只跑每颗卫星的代表性通道，用于快速检查链路
- `all` 跑所有已配置通道，耗时和文件体积都会明显增加

### 2.2 预览批处理入口

- [preview_runner.py](D:/AAAresearch_paper/third_report/code/preview_runner.py)

用途：

- 批量运行各卫星预览 notebook
- 生成全球 0.05° quicklook 图像

### 2.3 standardized_L1_source 规范文档

- [standardized_L1_source_spec.md](D:/AAAresearch_paper/third_report/code/L1g/standardized_L1_source_spec.md)

用途：

- 定义 standardized_L1_source 的变量、时间、投影、通道元数据和质量控制约定
- 后续开发 L1g 时，这份文档应作为标准输入接口参考

### 2.4 阶段总结文档

- [project_progress_summary_20260505.md](D:/AAAresearch_paper/third_report/code/project_progress_summary_20260505.md)

用途：

- 总结目前项目已经完成的工作
- 适合作为组会汇报和阶段性回顾材料

---

## 3. 根目录文件说明

### [README.md](D:/AAAresearch_paper/third_report/code/README.md)

用途：

- 当前这份总说明文档

### [preview_runner.py](D:/AAAresearch_paper/third_report/code/preview_runner.py)

用途：

- 统一运行各卫星 global preview notebook

说明：

- 适合做 quicklook 自动化
- 不直接生成 standardized 中间层

### [run_all_previews.ipynb](D:/AAAresearch_paper/third_report/code/run_all_previews.ipynb)

用途：

- 用 notebook 方式批量运行预览流程

说明：

- 和 `preview_runner.py` 功能相关
- 更适合在交互环境中查看运行过程

### [run_standardized_l1_source_batch.py](D:/AAAresearch_paper/third_report/code/run_standardized_l1_source_batch.py)

用途：

- standardized_L1_source 的统一批处理脚本

模块职责：

- 动态加载各卫星 builder
- 组织 sample / all 两种批处理模式
- 统一调度 GOES / Himawari / Meteosat 的标准化构建

注意：

- 当前 FY4B 标准化主要通过专用 builder 和 notebook 单独维护，没有接入这个总批处理脚本

### [standardized_l1_source_satpy.py](D:/AAAresearch_paper/third_report/code/standardized_l1_source_satpy.py)

用途：

- Satpy 共用标准化模块

模块职责：

- 供 Himawari / Meteosat 复用
- 统一处理 Satpy 读取出的：
  - counts
  - radiance
  - reflectance
  - brightness_temperature
- 统一生成：
  - `raw_count`
  - `calibrated_value`
  - `valid_mask`
  - `quality_flag`
  - `projection_x`
  - `projection_y`
  - `time`
  - `line_time_offset`
  - `grid_mapping`

特别说明：

- 这里还处理了 Satpy 不同 calibration 结果之间的坐标微小差异问题
- 即：按原生数组位置保持一致，而不是按浮点坐标标签求交集

### [project_progress_summary_20260505.md](D:/AAAresearch_paper/third_report/code/project_progress_summary_20260505.md)

用途：

- 当前项目阶段总总结

适合：

- 组会
- 汇报材料
- 回头查阅项目脉络

---

## 4. 各子目录说明

---

## 4.1 `FY4B/`

这个目录负责 FY4B / AGRI 相关代码和输出。

### 主要文件

#### [fy4b_l1_unit_probe.ipynb](D:/AAAresearch_paper/third_report/code/FY4B/fy4b_l1_unit_probe.ipynb)

用途：

- 探查 FY4B 原始 HDF 文件结构
- 确认原始 `NOMChannelXX` 是否为 DN
- 确认 `CALChannelXX` 的 LUT 标定关系

这是 FY4B 的“原始数据探查入口”。

#### [fy4b_global_grid_preview_005.ipynb](D:/AAAresearch_paper/third_report/code/FY4B/fy4b_global_grid_preview_005.ipynb)

用途：

- 将 FY4B 原始像元映射到 0.05° 全球规则网格
- 生成 quicklook 预览图

这是 FY4B 的“预览入口”。

#### [fy4b_standardized_l1_source_builder.py](D:/AAAresearch_paper/third_report/code/FY4B/fy4b_standardized_l1_source_builder.py)

用途：

- FY4B standardized_L1_source 主构建模块

模块职责：

- 读取 FY4B 原始 HDF
- 构建投影坐标
- 反演经纬度
- 近似计算观测/太阳几何角度
- 读取 DN
- 通过 LUT 标定生成反射率/亮温
- 组织标准化 NetCDF 输出

这是 FY4B standardized 中间层的核心代码。

#### [fy4b_build_standardized_l1_source_v02.ipynb](D:/AAAresearch_paper/third_report/code/FY4B/fy4b_build_standardized_l1_source_v02.ipynb)

用途：

- 以 notebook 形式调用 FY4B builder

适合：

- 交互式调试
- 手动切换处理时次
- 现场查看输出信息

### 输出目录

#### `outputs/`

用途：

- 保存 FY4B 数据结构探查时导出的 csv 结果

#### `preview_005deg_outputs/`

用途：

- 保存 FY4B 的 quicklook 预览图

### 推荐入口顺序

```text
fy4b_l1_unit_probe.ipynb
    -> fy4b_global_grid_preview_005.ipynb
    -> fy4b_standardized_l1_source_builder.py / fy4b_build_standardized_l1_source_v02.ipynb
```

---

## 4.2 `GOES/`

这个目录负责 GOES-16 / GOES-18 / ABI 相关代码和输出。

### 主要文件

#### [goes_abi_l1_unit_probe.ipynb](D:/AAAresearch_paper/third_report/code/GOES/goes_abi_l1_unit_probe.ipynb)

用途：

- 探查 GOES ABI L1b 文件结构
- 确认 `Rad` 是 packed radiance
- 检查 `scale_factor`、`add_offset`、Planck 参数

#### [goes16_global_grid_preview_005.ipynb](D:/AAAresearch_paper/third_report/code/GOES/goes16_global_grid_preview_005.ipynb)
#### [goes18_global_grid_preview_005.ipynb](D:/AAAresearch_paper/third_report/code/GOES/goes18_global_grid_preview_005.ipynb)

用途：

- 分别生成 GOES-16、GOES-18 的全球网格预览图

#### [goes_standardized_l1_source_builder.py](D:/AAAresearch_paper/third_report/code/GOES/goes_standardized_l1_source_builder.py)

用途：

- GOES ABI standardized_L1_source 主构建模块

模块职责：

- 按通道逐文件处理
- 保留原生 fixed grid 投影
- 解码 packed radiance
- 对可见通道生成 reflectance
- 对红外通道生成 brightness temperature
- 输出单通道 standardized NetCDF

说明：

- ABI 多分辨率差异很大，因此当前不强行把所有通道堆成同一个 `channel, y, x`
- 采用“单通道一个 standardized 文件”的策略

#### [goes_build_standardized_l1_source_v02.ipynb](D:/AAAresearch_paper/third_report/code/GOES/goes_build_standardized_l1_source_v02.ipynb)

用途：

- notebook 形式调用 GOES builder

### 输出目录

#### `outputs/`

用途：

- 存放 GOES 原始结构/单位探查导出结果

#### `GOES-16_preview_005deg_outputs/`
#### `GOES-18_preview_005deg_outputs/`

用途：

- 存放 GOES-16 / GOES-18 的 quicklook 预览图

### 推荐入口顺序

```text
goes_abi_l1_unit_probe.ipynb
    -> goes16_global_grid_preview_005.ipynb / goes18_global_grid_preview_005.ipynb
    -> goes_standardized_l1_source_builder.py / goes_build_standardized_l1_source_v02.ipynb
```

---

## 4.3 `Himawari/`

这个目录负责 Himawari-9 / AHI 相关代码和输出。

### 主要文件

#### [himawari9_l1_unit_probe.ipynb](D:/AAAresearch_paper/third_report/code/Himawari/himawari9_l1_unit_probe.ipynb)

用途：

- 探查 Himawari HSD 文件结构
- 理解 header / calibration block

#### [himawari9_global_grid_preview_005.ipynb](D:/AAAresearch_paper/third_report/code/Himawari/himawari9_global_grid_preview_005.ipynb)

用途：

- 生成 Himawari-9 全球规则网格预览图

#### [himawari_standardized_l1_source_builder.py](D:/AAAresearch_paper/third_report/code/Himawari/himawari_standardized_l1_source_builder.py)

用途：

- Himawari-9 standardized_L1_source builder

模块职责：

- 定义 AHI 通道表
- 选择指定 UTC 小时的数据文件
- 调用 Satpy 共用标准化模块生成单通道 standardized 文件

#### [himawari9_build_standardized_l1_source_v02.ipynb](D:/AAAresearch_paper/third_report/code/Himawari/himawari9_build_standardized_l1_source_v02.ipynb)

用途：

- notebook 形式调用 Himawari builder

### 输出目录

#### `outputs/`

用途：

- 存放 Himawari 数据结构探查导出结果

#### `preview_005deg_outputs/`

用途：

- 存放 Himawari-9 全球网格预览图

### 推荐入口顺序

```text
himawari9_l1_unit_probe.ipynb
    -> himawari9_global_grid_preview_005.ipynb
    -> himawari_standardized_l1_source_builder.py / himawari9_build_standardized_l1_source_v02.ipynb
```

---

## 4.4 `Meteosat/`

这个目录负责 Meteosat-9 / Meteosat-10 / SEVIRI 相关代码和输出。

### 主要文件

#### [meteosat_seviri_l1_unit_probe.ipynb](D:/AAAresearch_paper/third_report/code/Meteosat/meteosat_seviri_l1_unit_probe.ipynb)

用途：

- 探查 SEVIRI native 文件结构
- 检查 Satpy 可直接提供的 calibrated 物理量
- 理解 HRV 与常规通道几何差异

#### [meteosat9_global_grid_preview_005.ipynb](D:/AAAresearch_paper/third_report/code/Meteosat/meteosat9_global_grid_preview_005.ipynb)
#### [meteosat10_global_grid_preview_005.ipynb](D:/AAAresearch_paper/third_report/code/Meteosat/meteosat10_global_grid_preview_005.ipynb)

用途：

- 分别生成 Meteosat-9、Meteosat-10 的全球网格预览图

#### [meteosat_standardized_l1_source_builder.py](D:/AAAresearch_paper/third_report/code/Meteosat/meteosat_standardized_l1_source_builder.py)

用途：

- Meteosat standardized_L1_source builder

模块职责：

- 定义 SEVIRI 通道表
- 选择 native 文件
- 调用 Satpy 共用标准化模块构建单通道 standardized 文件

#### [meteosat_build_standardized_l1_source_v02.ipynb](D:/AAAresearch_paper/third_report/code/Meteosat/meteosat_build_standardized_l1_source_v02.ipynb)

用途：

- notebook 形式调用 Meteosat builder

### 输出目录

#### `outputs/`

用途：

- 存放 Meteosat 数据结构探查导出结果

#### `Meteosat-9_preview_005deg_outputs/`
#### `Meteosat-10_preview_005deg_outputs/`

用途：

- 存放 Meteosat-9 / Meteosat-10 的预览图

### 推荐入口顺序

```text
meteosat_seviri_l1_unit_probe.ipynb
    -> meteosat9_global_grid_preview_005.ipynb / meteosat10_global_grid_preview_005.ipynb
    -> meteosat_standardized_l1_source_builder.py / meteosat_build_standardized_l1_source_v02.ipynb
```

---

## 4.5 `L1g/`

这个目录存放与正式 L1g 产品相关的公共规范和网格基础。

### 主要文件

#### [global_grid_005_spec.ipynb](D:/AAAresearch_paper/third_report/code/L1g/global_grid_005_spec.ipynb)

用途：

- 定义全球规则经纬度网格
- 当前使用 `0.05°` 分辨率

#### [standardized_L1_source_spec.md](D:/AAAresearch_paper/third_report/code/L1g/standardized_L1_source_spec.md)

用途：

- 定义 `standardized_L1_source` 规范

这份文件是后续标准化和 L1g 构建的核心参考。

### 输出目录

#### `outputs/`

用途：

- 存储全局规则网格的一维坐标和规格 json

---

## 5. 当前主线模块关系

下面是当前 standardized_L1_source 主线的模块关系：

```text
run_standardized_l1_source_batch.py
    ├── GOES/goes_standardized_l1_source_builder.py
    ├── Himawari/himawari_standardized_l1_source_builder.py
    └── Meteosat/meteosat_standardized_l1_source_builder.py

FY4B/fy4b_standardized_l1_source_builder.py
    └── FY4B 单独维护

standardized_l1_source_satpy.py
    ├── 被 Himawari builder 调用
    └── 被 Meteosat builder 调用
```

说明：

- FY4B builder 目前仍是单独维护的，但它的默认生产后端已经切换到 Satpy，并会在找到 GEO 文件时优先走官方几何路径
- Himawari / Meteosat 复用了同一个 Satpy 共用层
- GOES 因为原生 NetCDF 结构和 fixed grid 特征，也采用单独 builder

---

## 6. 现在推荐怎么用

根据不同目的，推荐入口如下。

### 场景 A：想快速回顾项目结构

先看：

1. [project_progress_summary_20260505.md](D:/AAAresearch_paper/third_report/code/project_progress_summary_20260505.md)
2. [standardized_L1_source_spec.md](D:/AAAresearch_paper/third_report/code/L1g/standardized_L1_source_spec.md)
3. 当前这份 [README.md](D:/AAAresearch_paper/third_report/code/README.md)

### 场景 B：想检查某颗卫星原始数据结构

直接进入对应卫星目录，先看：

- `*_l1_unit_probe.ipynb`

### 场景 C：想看 quicklook 预览

单独跑某颗卫星：

- 对应的 `*_global_grid_preview_005.ipynb`

批量跑：

- [preview_runner.py](D:/AAAresearch_paper/third_report/code/preview_runner.py)

### 场景 D：想构建 standardized_L1_source

#### 快速样例测试

```powershell
D:\anaconda\envs\pytorch\python.exe code\run_standardized_l1_source_batch.py --mode sample
```

#### 交互式调试

进入对应卫星目录运行：

- FY4B: [fy4b_build_standardized_l1_source_v02.ipynb](D:/AAAresearch_paper/third_report/code/FY4B/fy4b_build_standardized_l1_source_v02.ipynb)
- GOES: [goes_build_standardized_l1_source_v02.ipynb](D:/AAAresearch_paper/third_report/code/GOES/goes_build_standardized_l1_source_v02.ipynb)
- Himawari: [himawari9_build_standardized_l1_source_v02.ipynb](D:/AAAresearch_paper/third_report/code/Himawari/himawari9_build_standardized_l1_source_v02.ipynb)
- Meteosat: [meteosat_build_standardized_l1_source_v02.ipynb](D:/AAAresearch_paper/third_report/code/Meteosat/meteosat_build_standardized_l1_source_v02.ipynb)

#### 全量通道构建

```powershell
D:\anaconda\envs\pytorch\python.exe code\run_standardized_l1_source_batch.py --mode all
```

注意：

- `all` 模式会显著增加运行时间和输出体积
- GOES C02、SEVIRI HRV 等通道特别大

---

## 7. 命名规则说明

当前主线文件命名遵循下面的约定：

### Python builder

```text
<satellite>_standardized_l1_source_builder.py
```

例子：

- `fy4b_standardized_l1_source_builder.py`
- `goes_standardized_l1_source_builder.py`
- `himawari_standardized_l1_source_builder.py`
- `meteosat_standardized_l1_source_builder.py`

### notebook 构建入口

```text
<satellite>_build_standardized_l1_source_v02.ipynb
```

### 统一批处理入口

```text
run_standardized_l1_source_batch.py
```

### Satpy 共用模块

```text
standardized_l1_source_satpy.py
```

---

## 8. 当前保留原则

当前 `code/` 目录保留的是：

- 现役主线代码
- 仍有参考价值的探查 notebook
- 仍有参考价值的预览 notebook
- 关键规范文档
- 关键阶段总结
- 关键输出样例目录

已经清理掉的是：

- 早期 scratch/试验目录
- 被 v0.2 取代的旧版 standardized notebook
- 旧版阶段总结
- `__pycache__`
- 已无用途的 FY4B v0.1 standardized 产物

---

## 9. 下一步建议

如果后续继续整理，我建议优先做两件事：

1. 给各 builder 补更细的模块级注释，尤其是：
   - 时间组织
   - quality flag
   - 原始值与标定值的关系

2. 进入下一阶段时，在 `L1g/` 目录下新增：
   - 单星 L1g 映射 builder
   - 排序/主次层选择模块
   - L1g 输出规范文档

这样后面从 standardized 到正式 L1g 的工程主线也能保持现在这种清晰度。
