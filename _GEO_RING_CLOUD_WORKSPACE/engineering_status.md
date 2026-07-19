# GEO-ring Cloud Engineering Status

Generated: `2026-07-19T08:38:41Z`

## 当前规模

- 索引脚本：119
- canonical shared modules：22
- 已登记物理代码迁移：19
- canonical stages：43
- SQLite 详细 artifact 记录：1018
- Markdown 快查 artifact 记录：436
- data product audits：19
- time-run 顶层目录：156
- 已登记历史命名 warning：31
- 历史绝对路径 warning 文件：0
- 历史动态阶段加载 warning 文件：0

## 已建立的工程能力

- Git 仓库、远端、`.gitignore`、`.gitattributes` 与本地 pre-commit hook。
- canonical stage taxonomy、artifact index、data product audit index 和跨项目 collision guard。
- Python `geo_ring_cloud.paths` 与 PowerShell `geo_ring_cloud_path_configuration.ps1` 共享环境变量契约；统一 lineage manifest helper 与 staged governance check。
- `geo_ring_cloud` package、`pyproject.toml`、module registry 与旧 import compatibility shims。
- SQLite/Markdown `code_migrations` 记录 canonical 路径、历史入口、验证证据和回滚说明。
- 已验证直接依赖基线、统一 `ci_check.py` 入口与 GitHub 轻量 CI 门禁。
- 大数据、time-run、图片、Office 文件和生成数据库默认不进入 Git。

## 尚未达到的目标

- `stage1_common.py` 已降为 compatibility shim；`pipeline_support` 已降为纯兼容 facade，layout、cloud semantics、重投影、GEO 几何、融合支撑、重叠统计、数据资产审计语义、产品读取、quicklook、artifact IO 与数组摘要统计均已拆入专责模块。
- Stage 06c 三个实现已迁入 `stage_06c_geometry_audit/`；CLAAS-3 lineage gate 已直接使用 canonical paths、lineage 与 source registry API。
- Stage 06e 两个实现已迁入 `stage_06e_geometry_angle_sync/`；子进程与报告根分别由 `CODE_ROOT`、`THIRD_REPORT_ROOT` 稳定解析。
- Stage 06f 三个实现已迁入 `stage_06f_data_asset_audit/`；原路径由 AST 门禁约束为薄兼容入口。
- Stage 07p 两个实现已迁入 `stage_07p_overlap_validation/`；实验 runner 已切换 canonical 路径，`stage_07p_b` 保持独立。
- Stage 08k/09 实现已迁入 `stage_08k_reporting/`、`stage_09/`；两者均补充标准 CLI 与 canonical lineage manifest，Stage 09b 复用审计指向 canonical Stage 09 路径。
- Stage 09b/09c runner 已迁入 `stage_09b_full_overnight/`、`stage_09c_scaled_batch/`；业务路径改用 `CODE_ROOT`，并补充 canonical lineage manifest，历史 artifact schema 保留用于续跑兼容。
- Stage 09d full-pixel runner 与四个解释实现已迁入 `stage_09d_full_pixel_diagnostics/`、`stage_09d_interpretation/`；历史嵌套路径保留受治理的可执行薄兼容入口。
- Stage 09d/09e/09f 的 full-pixel 采样、policy 与 workflow support 已进入 `geo_ring_cloud.diagnostics`；后续阶段不再反向导入 Stage 09d 脚本。
- 阶段脚本之间的动态实现加载已清零；Stage 05/06/07 主链均使用静态 package API。
- 活跃项目代码中的机器本地绝对路径 warning 已清零；历史非 canonical 命名继续由 alias/baseline 吸收。
- `environment.yml` 已固定已验证的直接依赖；跨平台传递依赖锁仍应在正式实验发布时按平台生成。
- 一部分旧 time-run 使用 `stage0910` 等组合标签；为保障续跑暂保留，只作为 legacy alias，不得用于新组件命名。

## 优先级

1. P0：任何新增 governance error 必须在提交前清零。
2. P1：保持机器本地绝对路径 warning 为零，并为新增 Python/PowerShell 路径执行治理门禁。
3. P1：保持动态阶段加载为零；复用逻辑必须进入已登记 package API。
4. P2：为正式实验发布生成平台化传递依赖锁；大数据集成测试继续本地运行。
5. P2：按依赖审计结果渐进迁移扁平脚本，禁止一次性大搬迁。
