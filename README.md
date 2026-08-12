# Geo Ring Cloud

本仓库保存 Geo Ring Cloud 的代码、工程治理、阶段 taxonomy 和轻量项目记忆。
原始遥感数据、time-run 产物、证据包、图片及生成型数据库不进入 Git。

## 权威入口

- 项目记忆与当前状态：`_GEO_RING_CLOUD_WORKSPACE/README.md`
- 工程合约：`_GEO_RING_CLOUD_WORKSPACE/engineering_policy.md`
- canonical stage：`_GEO_RING_CLOUD_WORKSPACE/stage_registry.md`
- artifact 快查：`_GEO_RING_CLOUD_WORKSPACE/artifact_index.md`
- 共享模块：`_GEO_RING_CLOUD_WORKSPACE/module_registry.md`
- 主代码：`third_report/code/geo_ring_cloud_stage1`

开始新任务前先查项目记忆和 SQLite 索引，再搜索代码。不要扫描大型数据目录来代替索引查询。

## 质量门禁

```powershell
python _GEO_RING_CLOUD_INDEX\build_index.py
python _GEO_RING_CLOUD_INDEX\governance_check.py --all --strict
python _GEO_RING_CLOUD_INDEX\ci_check.py --scientific-tests
```

提交要求、运行隔离和分支规则见 `CONTRIBUTING.md`。安全与凭据规则见 `SECURITY.md`。
