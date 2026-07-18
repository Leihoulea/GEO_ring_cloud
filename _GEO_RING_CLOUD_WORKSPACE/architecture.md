# GEO-ring Cloud Architecture

## 权威边界

- 控制面：`_GEO_RING_CLOUD_WORKSPACE` 与 `_GEO_RING_CLOUD_INDEX`，负责 taxonomy、检索、治理和项目记忆。
- 代码面：`third_report/code/geo_ring_cloud_stage1`，是 Geo Ring Cloud 主代码唯一权威入口。
- 数据面：`data`、`geo_ring_cloud_stage1`、`geo_ring_cloud_stage1_time_runs`、`data_check_report`、`geo_geometry_check`，保留原位并通过 `path_config.py` 配置。
- 证据面：stage manifest、关键 CSV/Markdown 索引和 evidence pack；大体量二进制产物不进入 Git。

## 逻辑模块

| layer | ownership | examples |
| --- | --- | --- |
| configuration | 路径、数据源 ID、环境覆盖、依赖契约 | `geo_ring_cloud.paths`, `geo_ring_cloud.sources`, `environment.yml` |
| lineage | manifest、commit、输入输出追踪 | `geo_ring_cloud.lineage` |
| adapters | 产品读取、格式适配、变量解码 | `geo_ring_cloud_claas3_adapter.py`, `geo_data_audit/` |
| stage pipeline | 单一 canonical stage 的科学处理与验证 | `stage_09d_*`, `stage_10_*` |
| orchestration | 跨阶段实验、批处理、time-run matrix | `geo_ring_cloud_experiment_profile_pair.py`, `geo_ring_cloud_time_run_matrix.py` |
| diagnostics | 可复用指标、采样和分层统计 | `geo_ring_cloud_epic_pair_diagnostics.py` |
| presentation | 代表性图、组会材料生成 | `stage_10/stage_10_make_*` |
| tests | 轻量单元、smoke 与回归测试；生成物只放 `tests/_tmp` | `tests/` |

## 物理迁移原则

`geo_ring_cloud/` 是共享 Python API 的权威 package；顶层同名旧模块只允许作为 compatibility shim。当前已迁移路径配置、数据源注册、lineage 和 run discovery。其余扁平历史 stage 脚本不得为目录美观一次性移动；只有在导入引用、运行器路径、证据引用和 rollback manifest 均验证后，才分批迁移。

新 stage 若只有一个脚本，可使用 `stage_XX_<purpose>.py`；若有多个脚本，必须放入 `stage_XX_<purpose>/`。跨阶段工具不得伪造组合 stage，必须使用 `geo_ring_cloud_<role>_<purpose>.py`、声明 `COMPONENT_ROLE`，并在 manifest 中记录 `related_stage_ids`。
