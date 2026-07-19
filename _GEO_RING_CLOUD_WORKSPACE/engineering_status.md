# GEO-ring Cloud Engineering Status

Generated: `2026-07-19T05:06:03Z`

## 当前规模

- 索引脚本：96
- canonical shared modules：14
- canonical stages：43
- SQLite 详细 artifact 记录：971
- Markdown 快查 artifact 记录：436
- data product audits：19
- time-run 顶层目录：156
- 已登记历史命名 warning：32
- 历史绝对路径 warning 文件：48

## 已建立的工程能力

- Git 仓库、远端、`.gitignore`、`.gitattributes` 与本地 pre-commit hook。
- canonical stage taxonomy、artifact index、data product audit index 和跨项目 collision guard。
- `geo_ring_cloud.paths` 环境变量覆盖、统一 lineage manifest helper 与 staged governance check。
- `geo_ring_cloud` package、`pyproject.toml`、module registry 与旧 import compatibility shims。
- 已验证直接依赖基线、统一 `ci_check.py` 入口与 GitHub 轻量 CI 门禁。
- 大数据、time-run、图片、Office 文件和生成数据库默认不进入 Git。

## 尚未达到的目标

- `stage1_common.py` 已降为 compatibility shim；`pipeline_support` 已降为纯兼容 facade，layout、cloud semantics、产品读取、quicklook、artifact IO 与数组摘要统计均已拆入专责模块。
- 仍有历史绝对路径和非 canonical 命名；普通模式保留 warning，新增污染会被 hook 阻断。
- `environment.yml` 已固定已验证的直接依赖；跨平台传递依赖锁仍应在正式实验发布时按平台生成。
- 一部分旧 time-run 使用 `stage0910` 等组合标签；为保障续跑暂保留，只作为 legacy alias，不得用于新组件命名。

## 优先级

1. P0：任何新增 governance error 必须在提交前清零。
2. P1：逐批参数化仍活跃脚本中的历史绝对路径，并保持默认路径行为不变。
3. P1：逐批清理阶段脚本通过动态加载彼此实现的编排耦合。
4. P2：为正式实验发布生成平台化传递依赖锁；大数据集成测试继续本地运行。
5. P2：按依赖审计结果渐进迁移扁平脚本，禁止一次性大搬迁。
