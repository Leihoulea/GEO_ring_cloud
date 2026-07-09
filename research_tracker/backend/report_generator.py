"""报告生成器 - 自动生成文件清单、图质量报告、项目进度汇总"""

import json
from datetime import datetime
from pathlib import Path

from db import Database


class ReportGenerator:
    def __init__(self, db: Database, project_root: str):
        self.db = db
        self.root = Path(project_root)
        self.reports_dir = self.root / "research_tracker" / "reports"

    def generate_all(self):
        """生成所有报告"""
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self._file_inventory()
        self._graph_quality_report()
        self._project_progress_summary()

    def _file_inventory(self):
        """生成 reports/file_inventory.md"""
        lines = [
            "# 文件清单\n",
            f"> 生成时间: {datetime.now().isoformat()}\n",
            f"> 扫描根目录: {self.root}\n",
            "",
            "## 按文件类型统计\n",
            "| 类型 | 数量 | 总大小 |",
            "|------|------|--------|",
        ]

        files = self.db.all_files()
        type_stats = {}
        for f in files:
            type_stats.setdefault(f.file_type, {"count": 0, "size": 0})
            type_stats[f.file_type]["count"] += 1
            type_stats[f.file_type]["size"] += f.size

        for ft, stats in sorted(type_stats.items()):
            size_str = self._human_size(stats["size"])
            lines.append(f"| {ft} | {stats['count']} | {size_str} |")

        lines += ["", "## 按子项目统计\n", "| 子项目 | 数量 | 总大小 |", "|--------|------|--------|"]
        subproject_stats = {}
        for f in files:
            sp = f.subproject or "(未归组)"
            subproject_stats.setdefault(sp, {"count": 0, "size": 0})
            subproject_stats[sp]["count"] += 1
            subproject_stats[sp]["size"] += f.size

        for sp, stats in sorted(subproject_stats.items()):
            lines.append(f"| {sp} | {stats['count']} | {self._human_size(stats['size'])} |")

        lines += ["", "## 按阶段统计\n", "| 阶段 | 数量 | 总大小 |", "|------|------|--------|"]
        stage_stats = {}
        for f in files:
            sg = f.stage or "(未阶段)"
            stage_stats.setdefault(sg, {"count": 0, "size": 0})
            stage_stats[sg]["count"] += 1
            stage_stats[sg]["size"] += f.size

        for sg, stats in sorted(stage_stats.items()):
            lines.append(f"| {sg} | {stats['count']} | {self._human_size(stats['size'])} |")

        lines += ["", "## 所有 Python 脚本\n", "| 文件 | 子项目 | 阶段 | 大小 |", "|------|--------|------|------|"]
        for f in sorted(files, key=lambda x: x.path):
            if f.path.endswith('.py'):
                lines.append(f"| {f.path} | {f.subproject} | {f.stage} | {self._human_size(f.size)} |")

        self._write_report("file_inventory.md", lines)

    def _graph_quality_report(self):
        """生成 reports/graph_quality_report.md"""
        nodes = self.db.export_nodes_json()
        edges = self.db.export_edges_json()

        # 统计
        node_types = {}
        for n in nodes:
            nt = n.get("type", "unknown")
            node_types[nt] = node_types.get(nt, 0) + 1

        edge_types = {}
        for e in edges:
            et = e.get("type", "unknown")
            edge_types[et] = edge_types.get(et, 0) + 1

        # 按自信度分级的边
        edges_high = [e for e in edges if e.get("confidence", 0) >= 0.8]
        edges_med = [e for e in edges if 0.5 <= e.get("confidence", 0) < 0.8]
        edges_low = [e for e in edges if e.get("confidence", 0) < 0.5]

        lines = [
            "# 知识图谱质量报告\n",
            f"> 生成时间: {datetime.now().isoformat()}\n",
            "",
            "## 节点统计\n",
            "| 类型 | 数量 |",
            "|------|------|",
        ]
        for nt, count in sorted(node_types.items(), key=lambda x: -x[1]):
            lines.append(f"| {nt} | {count} |")

        lines += ["", "## 边统计\n", "| 类型 | 数量 | 高置信度(≥0.8) | 中置信度(0.5~0.8) | 低置信度(<0.5) |",
                  "|------|------|-----|-----|-----|"]
        for et in sorted(edge_types.keys()):
            total = edge_types[et]
            high = sum(1 for e in edges_high if e.get("type") == et)
            med = sum(1 for e in edges_med if e.get("type") == et)
            low = sum(1 for e in edges_low if e.get("type") == et)
            lines.append(f"| {et} | {total} | {high} | {med} | {low} |")

        # 按证据分类的边
        lines += ["", "## 按推断方式分类\n", "| 推断来源 | 边数 |", "|----------|------|"]
        evidence_types = {}
        for e in edges:
            ev = e.get("evidence", "")[:50]
            evidence_types[ev] = evidence_types.get(ev, 0) + 1

        for ev, count in sorted(evidence_types.items(), key=lambda x: -x[1])[:15]:
            lines.append(f"| {ev}... | {count} |")

        # 未连接节点
        connected_nodes = set()
        for e in edges:
            connected_nodes.add(e.get("source", ""))
            connected_nodes.add(e.get("target", ""))

        isolated = [n for n in nodes if n.get("id", "") not in connected_nodes]
        lines += [
            "",
            "## 孤点检测\n",
            f"总节点: {len(nodes)}, 连通节点: {len(connected_nodes)}, 孤立节点: {len(isolated)}\n",
        ]

        if isolated:
            for n in isolated[:20]:
                lines.append(f"- {n.get('id', '?')} ({n.get('type', '?')})")
            if len(isolated) > 20:
                lines.append(f"- ... 另有 {len(isolated) - 20} 个孤点")

        self._write_report("graph_quality_report.md", lines)

    def _project_progress_summary(self):
        """生成 reports/project_progress_summary.md"""
        files = self.db.all_files()
        ast_nodes = self.db.all_ast_nodes()
        md_reports = self.db.all_md_reports()
        csv_tables = self.db.all_csv_tables()
        edges = self.db.all_edges()

        # 计算覆盖率
        py_files = [f for f in files if f.path.endswith('.py')]
        ast_covered = set(n.file_path for n in ast_nodes)
        ast_coverage = f"{len(ast_covered)}/{len(py_files)}" if py_files else "0/0"

        md_files = [f for f in files if f.path.endswith('.md')]
        md_covered = set(r.file_path for r in md_reports)
        md_coverage = f"{len(md_covered)}/{len(md_files)}" if md_files else "0/0"

        # 函数/类统计
        functions = [n for n in ast_nodes if n.node_type == "function"]
        classes = [n for n in ast_nodes if n.node_type == "class"]
        methods = [n for n in ast_nodes if n.node_type == "method"]

        # reports 统计
        results_all = []
        for r in md_reports:
            results_all.extend(r.results)
        pass_count = sum(1 for r in results_all if r.get("status") in ("PASS", "OK"))
        fail_count = sum(1 for r in results_all if r.get("status") in ("FAIL", "ERROR"))

        lines = [
            "# 项目进度汇总\n",
            f"> 生成时间: {datetime.now().isoformat()}\n",
            f"> 扫描根目录: {self.root}\n",
            "",
            "## 总体统计\n",
            f"- 扫描文件总数: {len(files)}",
            f"- Python 脚本: {len(py_files)} (已解析 AST: {ast_coverage})",
            f"- Markdown 报告: {len(md_files)} (已解析: {md_coverage})",
            f"- CSV/JSON 数据表: {len(csv_tables)}",
            f"- 函数: {len(functions)}, 类: {len(classes)}, 方法: {len(methods)}",
            f"- 知识图谱边: {len(edges)}",
            f"- 报告检测: PASS={pass_count}, FAIL={fail_count}",
            "",
            "## 子项目活跃度\n",
            "| 子项目 | 文件数 | 函数 | 类 |",
            "|--------|--------|------|------|",
        ]

        subproject_stats = {}
        for f in files:
            sp = f.subproject or "(未归组)"
            if sp not in subproject_stats:
                subproject_stats[sp] = {"files": 0, "funcs": 0, "classes": 0}
            subproject_stats[sp]["files"] += 1

        for n in ast_nodes:
            for f in files:
                if f.path == n.file_path:
                    sp = f.subproject or "(未归组)"
                    if sp not in subproject_stats:
                        subproject_stats[sp] = {"files": 0, "funcs": 0, "classes": 0}
                    if n.node_type == "function":
                        subproject_stats[sp]["funcs"] += 1
                    elif n.node_type == "class":
                        subproject_stats[sp]["classes"] += 1
                    break

        for sp, stats in sorted(subproject_stats.items(), key=lambda x: -x[1]["files"]):
            lines.append(f"| {sp} | {stats['files']} | {stats['funcs']} | {stats['classes']} |")

        lines += [
            "",
            "## 管道概览\n",
            "主要数据流管道:",
            "",
            "1. **标准化 L1 主线**: ",
            "   standardized_l1_source_satpy.py → per-satellite builder → ",
            "   run_standardized_l1_source_batch.py → validate_standardized_l1_source_samples.py",
            "",
            "2. **EPIC/CERES 深度学习管道**: ",
            "   run_stage_1_5 (配对匹配) → run_stage_6_9 (样本生成) → ",
            "   run_stage_10_train → run_stage_11_evaluate → stage12/13 (诊断/审计)",
            "",
            "3. **云产品融合管道**: ",
            "   01_build_core_time_index → 02_build_standardized_cloud_native → ... → ",
            "   06_fuse_best_source → 07_overlap_consistency_validation → 08_epic_visual_comparison",
            "",
            "4. **数据下载管道**: ",
            "   geo_cloud_downloader → priority_download → verify",
        ]

        self._write_report("project_progress_summary.md", lines)

    def _human_size(self, num: int) -> str:
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if abs(num) < 1024:
                return f"{num:.1f} {unit}"
            num /= 1024
        return f"{num:.1f} PB"

    def _write_report(self, name: str, lines: list[str]):
        path = self.reports_dir / name
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except Exception as e:
            print(f"Error writing {path}: {e}")