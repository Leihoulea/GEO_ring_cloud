# research_tracker - 本地可视化科研进度追踪器

追踪科研项目目录，自动构建"项目—阶段—代码—函数/类—数据—报告—输出产物"的知识图谱，并以前端星系式可视化展示。

## 快速开始

### 1. 扫描项目（生成图谱）

```bash
cd research_tracker/backend
pip install -r requirements.txt
# 扫描并生成图谱 + 报告（默认输出到 frontend/src/data/graph.json）
python run_scan.py D:/AAAresearch_paper --db ../research_tracker.db --gen-reports
```

可选参数：
- `--no-incremental`：关闭增量扫描，重新解析所有文件
- `--rebuild-graph`：跳过扫描，仅基于已有数据库重建图谱边和报告
- `--json-out <path>`：指定图数据 JSON 输出路径
- `--annotations <yaml>`：指定人工标注文件路径

### 2. 启动前端可视化

```bash
cd research_tracker/frontend
npm install
npm run dev
```

浏览器打开 http://localhost:5173

### 3. 更新图谱（增量扫描）

```bash
python run_scan.py D:/AAAresearch_paper --db ../research_tracker.db
```

仅增量扫描：SHA-256 hash 未变的文件跳过解析，秒级完成。

### 4. 人工标注

编辑 `research_tracker/research_tracker_annotations.yml`，修正节点状态、重要性、说明，或手工定义/修正关系边：

```yaml
annotations:
  "third_report/code/standardized_l1_source_satpy.py":
    importance: critical        # critical / high / normal / low / archive
    status: active              # active / deprecated / experimental / planned
    note: "核心Satpy标准化共享库，被Himawari/Meteosat builder复用"
    tags: ["core", "satpy", "standardized"]
    functions:
      satpy_channel_dataset:
        note: "核心函数，组合多视角数据成标准化NetCDF"
        importance: critical
    # 节点内嵌的边修正
    edges:
      - target: "third_report/code/run_standardized_l1_source_batch.py"
        type: imports
        confidence: 1.0
        evidence: "人工确认：被batch入口动态加载"

# 顶层手工边（优先级高于自动推断，confidence 默认 1.0）
edges:
  - source: "third_report/code/epic_ceres/run_stage_10_train.py"
    target: "third_report/code/epic_ceres/models.py"
    type: depends_on
    evidence: "训练依赖模型定义"
```

## 三种视图

### 星系视图（Galaxy View）
- project 为中心（金色五角星），subproject/stage 为星系层（橙色），script/data_file/report 为星球，function/class 为卫星
- **函数/类/方法默认折叠**（虚线边框），双击聚合节点展开/折叠下钻
- 单击节点查看详情，悬停高亮邻居

### 管道视图（Pipeline View）
- 按 stage 分列展示数据流（reads/writes/derived_from/documents/imports 边）
- 直观看到 data → script → output 的流转

### 调用图视图（Call Graph View）
- 展示函数/方法间的 calls 关系（fcose 力导向布局）
- 在左侧选中一个 script 节点后，自动聚焦该脚本内的调用链

## 交互功能

- **搜索**：按节点名/路径/docstring 过滤（聚合节点不受搜索影响，保留可下钻）
- **类型过滤**：左侧勾选要显示的节点类型
- **阶段过滤**：按 stage 筛选
- **状态过滤**：按 active/deprecated/experimental/planned 筛选
- **点击详情**：右侧面板显示节点完整信息（路径/docstring/参数/报告结果/CSV列等）
- **邻居高亮**：悬停节点高亮其邻居和连接边，其余淡化
- **重要性标记**：critical 红边框、high 金边框

## 自动生成的报告

扫描后会在 `research_tracker/reports/` 生成：
- `file_inventory.md` — 文件清单（按类型/子项目/阶段统计 + 全部 Python 脚本列表）
- `graph_quality_report.md` — 图谱质量（节点/边统计、置信度分级、孤点检测）
- `project_progress_summary.md` — 项目进度汇总（子项目活跃度、管道概览）

## 项目结构

```
research_tracker/
├── backend/                 # 后端扫描与图谱构建
│   ├── scanner.py           # 目录扫描器（递归+排除+增量hash）
│   ├── ast_parser.py        # Python AST 解析（imports/函数/类/calls/读写）
│   ├── md_parser.py         # Markdown 报告解析（标题/结果/路径/警告/阻塞）
│   ├── csv_parser.py        # CSV/JSON 解析（列名/产品/卫星/时间范围）
│   ├── db.py                # SQLite 数据层 + 节点去重 + 聚合节点导出
│   ├── graph_builder.py     # 9种边的自动推断（含confidence+evidence）
│   ├── report_generator.py  # 三份自动报告生成器
│   ├── annotations.py       # 人工标注处理器（节点+函数+类+edges段）
│   ├── run_scan.py          # 扫描入口（一键流水线）
│   └── requirements.txt
├── frontend/                # Vite + TypeScript + Cytoscape.js
│   ├── src/
│   │   ├── components/      # FilterPanel / NodeDetail
│   │   ├── views/           # GalaxyView / PipelineView / CallGraphView
│   │   ├── data/            # graph.json + loader（图数据服务）
│   │   ├── types.ts         # 节点/边类型 + 颜色/尺寸配置
│   │   ├── cytoscapeStyle.ts# 共享样式与布局
│   │   └── App.vue          # 主应用（视图切换+搜索+过滤+详情）
│   ├── package.json
│   └── vite.config.ts
├── reports/                 # 自动生成的报告
├── research_tracker_annotations.yml  # 人工标注文件
├── research_tracker.yml     # 子项目归组/排除/颜色配置
└── README.md
```

## 节点类型

| 类型 | 说明 |
|------|------|
| project | 项目根 |
| subproject | 子项目/主题 |
| stage | 流水线阶段 |
| script | Python 脚本 |
| function | 函数 |
| class | 类 |
| method | 方法 |
| data_file | 数据文件 |
| report | 报告文档 |
| csv_table | CSV/数据表 |
| image | 图片 |
| config | 配置文件 |
| log | 日志 |
| directory | 目录 |

## 边类型

| 类型 | 说明 | 自动推断 |
|------|------|----------|
| contains | 包含 | ✓ |
| imports | 导入 | ✓ |
| calls | 调用 | ✓ |
| reads | 读取文件 | ✓ |
| writes | 写出文件 | ✓ |
| documents | 文档化/报告提及 | ✓ |
| validates | 验证 | ✓ |
| derived_from | 源自/衍生自 | ✓ |
| depends_on | 依赖 | ✓ |

所有自动推断的边都含 `confidence`（0~1）和 `evidence`（推断依据文本）。

## 增量扫描

扫描器使用文件 SHA-256 hash 缓存到 SQLite，hash 未变的文件跳过重新解析，实现快速增量更新。