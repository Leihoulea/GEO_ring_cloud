# Geo Ring Cloud 依赖契约

本文件说明依赖的职责边界。`environment.yml` 固定的是 2026-07-19 在 Windows 上通过核心科学测试的直接依赖版本；它不是跨平台的完整传递依赖锁文件。

## 标准环境

```powershell
conda env create -f third_report\code\geo_ring_cloud_stage1\environment.yml
conda activate geo-ring-cloud
python _GEO_RING_CLOUD_INDEX\ci_check.py --scientific-tests
```

已有同名环境更新时使用：

```powershell
conda env update -f third_report\code\geo_ring_cloud_stage1\environment.yml --prune
```

## 能力分层

| capability | direct dependencies | policy |
| --- | --- | --- |
| lightweight governance | Python standard library, Git | GitHub CI 必跑；不读取本地大数据 |
| scientific core | numpy, pandas, scipy, h5py, netCDF4, xarray | 数组、表格、统计与 HDF5/NetCDF 读取 |
| geospatial and plotting | pyproj, matplotlib, Pillow, PyYAML | 投影、图件和配置读取 |
| EO readers and download | satpy, boto3, botocore | 特定格式读取与对象存储下载；脚本应在入口处检查能力 |
| optional columnar IO | pyarrow | 仅在需要 Parquet/Arrow 的任务中安装 |
| optional presentation | python-pptx, pypdf or PyPDF2 | 仅在生成 PPT/PDF 的任务中安装，不属于科学核心 |

## 约束

- MUST 使用 `environment.yml` 作为默认开发环境基线，不提交个人环境的完整 `pip freeze`。
- MUST 在引入新的必需第三方包时更新本文件和 `environment.yml`，并运行科学测试。
- MUST 将仅由单个报告、演示或格式适配器需要的包保持为可选能力；缺失时给出明确错误，不得在模块导入阶段无条件破坏 `--help` 或治理检查。
- SHOULD 在需要严格复现实验发布时，另行生成带平台标识的传递依赖锁文件，并记录生成工具、平台和日期；不要手工维护伪锁文件。
- GitHub 轻量 CI 不安装科学栈。真实数据集成测试依赖本地数据路径，通过 `--integration-tests` 显式运行。
