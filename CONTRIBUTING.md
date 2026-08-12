# Contributing

Geo Ring Cloud 是可审计科研工程。任何人或 AI agent 修改前都必须遵守
`_GEO_RING_CLOUD_WORKSPACE/engineering_policy.md`。

## 工作顺序

1. 从 `_GEO_RING_CLOUD_WORKSPACE/README.md` 进入项目记忆。
2. 查询 stage、artifact、module registry 和 SQLite，确认已有能力与 canonical 身份。
3. 在 `codex/<topic>` 或其他明确命名的功能分支工作；不要在运行任务使用的目录中修改其脚本。
4. 新运行必须使用干净 commit。确需试验 dirty 代码时，manifest 必须保留脚本 SHA-256、Git blob 和 `commit_represents_script=false`。
5. 代码、测试、taxonomy/index 更新必须处于同一职责明确的提交中。

## 验证

```powershell
python _GEO_RING_CLOUD_INDEX\build_index.py
python _GEO_RING_CLOUD_INDEX\governance_check.py --all --strict
python _GEO_RING_CLOUD_INDEX\ci_check.py --scientific-tests
python _GEO_RING_CLOUD_INDEX\governance_check.py --staged
```

真实数据集成测试必须显式运行，不能把“未运行”写成“通过”。运行中的下载、上传或科学计算不得被测试清理程序终止或覆盖。

## 提交

- 一个提交只承担一个 stage 或 component role 的职责。
- 提交消息必须包含 canonical stage ID 或 component role。
- 不提交 raw data、time runs、图片、PPTX、SQLite/XLSX 或凭据。
- 不使用 force-add 绕过 artifact 规则。
- 合并前要求 Git 工作区干净、治理检查通过、索引时间与提交内容一致。
