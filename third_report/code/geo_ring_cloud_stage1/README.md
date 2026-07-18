# GEO-ring Cloud Core Code

本目录是 Geo Ring Cloud 主代码的唯一权威入口。开始新任务前，先读：

1. `D:\AAAresearch_paper\_GEO_RING_CLOUD_WORKSPACE\architecture.md`
2. `D:\AAAresearch_paper\_GEO_RING_CLOUD_WORKSPACE\engineering_status.md`
3. `D:\AAAresearch_paper\_GEO_RING_CLOUD_WORKSPACE\stage_registry.md`
4. `D:\AAAresearch_paper\_GEO_RING_CLOUD_WORKSPACE\artifact_index.md`

阶段代码使用 `stage_XX...`；跨阶段编排、共享诊断、下载和证据工具使用 `geo_ring_cloud_<role>_<purpose>.py` 并声明 `COMPONENT_ROLE`。不要根据 `Step9`、`Stage9` 或组合标签猜阶段。

## 模块边界

- `geo_ring_cloud/` 是共享 Python API 的权威实现，新代码必须从该 package 导入。
- 根目录的 `path_config.py`、`geo_ring_cloud_source_registry.py`、`geo_ring_cloud_lineage.py` 和 `geo_ring_cloud_run_discovery.py` 仅为历史兼容入口，不得再放置实现逻辑。
- stage 脚本保留在 canonical stage 文件或目录中；跨阶段运行器保留 `geo_ring_cloud_<role>_<purpose>.py` 命名。
- `pyproject.toml` 只描述标准库共享 package；完整科学依赖由 `environment.yml` 管理。

开发环境可将共享 package 以 editable 模式安装：

```powershell
python -m pip install -e third_report\code\geo_ring_cloud_stage1 --no-deps
```

所有项目路径通过 `path_config.py` 或环境变量覆盖。运行产物写入 `geo_ring_cloud_stage1`、`geo_ring_cloud_stage1_time_runs` 或明确的数据审计目录，不写入源码目录；测试临时产物只允许位于 `tests/_tmp`，且不会进入 Git 或正式 artifact index。

依赖职责、标准环境和可选能力见 `DEPENDENCIES.md`。创建环境后，统一运行：

```powershell
python _GEO_RING_CLOUD_INDEX\ci_check.py --scientific-tests
```

真实数据集成测试仅在本地数据可用时运行：

```powershell
python _GEO_RING_CLOUD_INDEX\ci_check.py --integration-tests
```

修改 stage 脚本后还必须执行：

```powershell
python _GEO_RING_CLOUD_INDEX\build_index.py
python _GEO_RING_CLOUD_INDEX\governance_check.py --staged
```
