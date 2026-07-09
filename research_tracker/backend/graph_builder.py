"""知识图谱构建器 - 从数据库构建节点和边"""

import re
from pathlib import Path
from typing import Any
from collections import defaultdict

from db import Database, Edge


class GraphBuilder:
    """从数据库中的文件/AST/MD/CSV 记录构建知识图谱

    性能注意：构建开始时一次性把所有文件/AST/报告加载到内存并建索引，
    避免在 O(N²) 嵌套循环里反复执行 SQL 查询。
    """

    def __init__(self, db: Database):
        self.db = db
        self._py_index = None
        # 内存缓存（build_all_edges 开始时填充）
        self._files = None        # list[FileRecord]
        self._files_by_path = None  # dict[str, FileRecord]
        self._ast_nodes = None    # list[ASTNode]
        self._md_reports = None   # list[MDReport]
        self._path_substrings = None  # 用于快速子串匹配的文件路径列表

    def _load_cache(self):
        """一次性加载所有数据到内存并建索引"""
        if self._files is not None:
            return
        self._files = self.db.all_files()
        self._files_by_path = {f.path: f for f in self._files}
        self._ast_nodes = self.db.all_ast_nodes()
        self._md_reports = self.db.all_md_reports()
        # 文件路径列表（用于子串匹配）
        self._path_substrings = [f.path for f in self._files]
        # 文件名 → [路径] 索引（用于按文件名快速查找）
        self._files_by_name = defaultdict(list)
        for f in self._files:
            self._files_by_name[Path(f.path).name].append(f.path)
        # 目录 → [文件路径] 索引（用于同目录文件关联，避免 N×N）
        self._files_by_dir = defaultdict(list)
        for f in self._files:
            self._files_by_dir[str(Path(f.path).parent)].append(f.path)
        # py 索引
        self._py_index = defaultdict(list)
        for f in self._files:
            if f.path.endswith(".py"):
                self._py_index[Path(f.path).stem].append(f.path)

    def build_all_edges(self):
        """构建所有类型的边"""
        self._load_cache()
        self._build_contains_edges()
        self._build_imports_edges()
        self._build_calls_edges()
        self._build_reads_writes_edges()
        self._build_documents_edges()
        self._build_validates_edges()
        self._build_derived_from_edges()
        self._build_depends_on_edges()
        self.db.commit()

    def _get_py_index(self):
        """文件名(stem) → [完整路径] 索引"""
        self._load_cache()
        return self._py_index

    def _build_contains_edges(self):
        """构建 contains 边：聚合→文件→函数/类/方法，以及 subproject/stage→文件"""
        edges = []

        # a) 函数/方法/类的父子包含（parent 链）
        for node in self._ast_nodes:
            if node.parent:
                edges.append(Edge(
                    source=node.parent,
                    target=node.id,
                    edge_type="contains",
                    confidence=1.0,
                    evidence=f"{node.node_type} {node.name} defined at line {node.lineno}",
                ))

        # b) 文件 contains 顶层函数/类
        file_nodes = {}
        for node in self._ast_nodes:
            file_nodes.setdefault(node.file_path, []).append(node)
        for file_path, nodes in file_nodes.items():
            top_nodes = [n for n in nodes
                         if n.node_type != "script"
                         and n.parent in ("", file_path, Path(file_path).name)]
            for n in top_nodes:
                edges.append(Edge(
                    source=file_path,
                    target=n.id,
                    edge_type="contains",
                    confidence=1.0,
                    evidence=f"Top-level {n.node_type} {n.name} in {Path(file_path).name}",
                ))

        # c) 聚合节点 contains 文件：project → subproject → stage → file
        for f in self._files:
            sp = f.subproject or ""
            sg = f.stage or ""
            if sp and sg:
                stage_id = f"__stage__{sp}__{sg}"
                edges.append(Edge(source=stage_id, target=f.path, edge_type="contains",
                                  confidence=1.0, evidence=f"File in stage {sp}/{sg}"))
            elif sp:
                sp_id = f"__sp__{sp}"
                edges.append(Edge(source=sp_id, target=f.path, edge_type="contains",
                                  confidence=1.0, evidence=f"File in subproject {sp}"))
            elif sg:
                stage_id = f"__stage__{sg}"
                edges.append(Edge(source=stage_id, target=f.path, edge_type="contains",
                                  confidence=1.0, evidence=f"File in stage {sg}"))

        # d) subproject → stage, project → subproject
        sp_set = set()
        stage_set = set()
        for f in self._files:
            sp = f.subproject or ""
            sg = f.stage or ""
            if sp:
                sp_set.add(sp)
                if sg:
                    stage_set.add((sp, sg))
        for sp in sp_set:
            edges.append(Edge(source="__project__", target=f"__sp__{sp}",
                              edge_type="contains", confidence=1.0,
                              evidence=f"Subproject {sp}"))
        for sp, sg in stage_set:
            edges.append(Edge(source=f"__sp__{sp}", target=f"__stage__{sp}__{sg}",
                              edge_type="contains", confidence=1.0,
                              evidence=f"Stage {sg} of {sp}"))
        # 孤立 stage（无 subproject）
        for f in self._files:
            sg = f.stage or ""
            sp = f.subproject or ""
            if sg and not sp:
                edges.append(Edge(source="__project__", target=f"__stage__{sg}",
                                  edge_type="contains", confidence=1.0,
                                  evidence=f"Stage {sg}"))

        self.db.edges_batch(edges)

    def _build_imports_edges(self):
        """构建 imports 边：脚本之间的导入关系"""
        edges = set()
        for node in self._ast_nodes:
            if node.node_type == "script":
                for imp in node.imports:
                    # 匹配已知文件路径
                    target = self._resolve_import(node.file_path, imp)
                    if target:
                        edges.add((node.file_path, target, "imports"))
        self.db.edges_batch([
            Edge(source=s, target=t, edge_type="imports",
                 confidence=0.8, evidence=f"Python import: {t}")
            for s, t, _ in edges
        ])

    def _resolve_import(self, source_path: str, import_name: str) -> str | None:
        """尝试将 import 名解析为项目内文件路径（用索引加速）"""
        idx = self._get_py_index()
        if not import_name:
            return None
        # 取 import 名最后一段作为模块名（如 from a.b import c → c；import a.b → b）
        parts = import_name.split(".")
        candidates = []
        # 优先匹配最后一段
        for part in reversed(parts):
            if part in idx:
                candidates = idx[part]
                break
        if not candidates:
            return None
        # 优先选同子项目的文件
        source_dir = str(Path(source_path).parent)
        for c in candidates:
            if c.startswith(source_dir):
                return c
        # 否则取第一个
        return candidates[0] if candidates else None

    def _build_calls_edges(self):
        """构建 calls 边：函数/方法之间的调用关系

        匹配策略（按优先级）：
        1. self.method / cls.method → 同类内方法
        2. Module.func（如 np.load）→ 跨文件同名函数
        3. bare func（如 load_data）→ 同文件顶层函数，其次全局同名函数
        4. obj.method → 同文件/全局同名方法
        """
        edges = []
        all_nodes = self._ast_nodes
        # 索引：按 (file_path, name) 和 (name,) 建索引
        by_file_name = {}   # (file_path, name) -> qual_name
        by_name = {}        # name -> [qual_name]
        by_class_method = {}  # (class_qual, method_name) -> method_qual
        for n in all_nodes:
            if n.node_type in ("function", "method"):
                by_file_name.setdefault((n.file_path, n.name), []).append(n.qual_name)
                by_name.setdefault(n.name, []).append(n.qual_name)
                if n.node_type == "method" and n.parent:
                    by_class_method.setdefault((n.parent, n.name), []).append(n.qual_name)

        seen = set()
        for node in all_nodes:
            if node.node_type not in ("function", "method"):
                continue
            for call_name in node.calls:
                target = self._resolve_call(call_name, node, by_file_name, by_name, by_class_method)
                if target and target != node.qual_name:
                    key = (node.qual_name, target, "calls")
                    if key in seen:
                        continue
                    seen.add(key)
                    edges.append(Edge(
                        source=node.qual_name,
                        target=target,
                        edge_type="calls",
                        confidence=0.7,
                        evidence=f"{node.name} calls {call_name} -> {target}",
                    ))

        self.db.edges_batch(edges)

    def _resolve_call(self, call_name: str, node, by_file_name, by_name, by_class_method) -> str | None:
        """解析一个调用名到目标函数/方法的 qual_name"""
        if not call_name:
            return None
        # 1. self.method / cls.method → 当前类内的同名方法
        if call_name.startswith("self.") or call_name.startswith("cls."):
            mname = call_name.split(".", 1)[1]
            # 当前函数的 parent 即所属类
            parent_class = node.parent
            if parent_class:
                cands = by_class_method.get((parent_class, mname), [])
                if cands:
                    return cands[0]
            # 退而求其次：全局同名方法
            return by_name.get(mname, [None])[0]

        parts = call_name.split(".")
        # 2. obj.method 或 Module.func：取最后一段作为名
        short = parts[-1]
        # 3. 优先同文件同名函数
        cands = by_file_name.get((node.file_path, short), [])
        if cands:
            return cands[0]
        # 4. 全局同名函数/方法（取第一个，confidence 较低由调用方控制）
        cands = by_name.get(short, [])
        if cands:
            return cands[0]
        return None

    def _build_reads_writes_edges(self):
        """构建 reads/writes 边：脚本与数据文件之间的读写关系"""
        edges = []
        for node in self._ast_nodes:
            if node.node_type not in ("function", "method", "script"):
                continue
            for read_path in node.file_reads:
                matched = self._match_data_file(read_path)
                for m in matched:
                    edges.append(Edge(
                        source=node.qual_name if node.node_type != "script" else node.file_path,
                        target=m,
                        edge_type="reads",
                        confidence=0.5,
                        evidence=f"File read detected: {read_path}",
                    ))
            for write_path in node.file_writes:
                matched = self._match_data_file(write_path)
                for m in matched:
                    edges.append(Edge(
                        source=node.qual_name if node.node_type != "script" else node.file_path,
                        target=m,
                        edge_type="writes",
                        confidence=0.5,
                        evidence=f"File write detected: {write_path}",
                    ))

        # 从 md_reports 提取 documents 边（用文件名索引加速）
        for report in self._md_reports:
            for sp in report.script_paths:
                if Path(sp).suffix == '.py':
                    fname = Path(sp).name
                    for fpath in self._files_by_name.get(fname, []):
                        if fpath.endswith(sp) or sp in fpath:
                            edges.append(Edge(
                                source=report.file_path,
                                target=fpath,
                                edge_type="documents",
                                confidence=0.8,
                                evidence=f"Report references script: {sp}",
                            ))
                            break
            for op in report.output_paths:
                fname = Path(op).name
                for fpath in self._files_by_name.get(fname, []):
                    if fpath.endswith(op) or op in fpath:
                        edges.append(Edge(
                            source=report.file_path,
                            target=fpath,
                            edge_type="documents",
                            confidence=0.8,
                            evidence=f"Report references output: {op}",
                        ))
                        break

        self.db.edges_batch(edges)

    def _match_data_file(self, pattern: str) -> list[str]:
        """尝试将路径模式与已知文件匹配（用内存缓存，避免反复查库）"""
        if not pattern or pattern == "?":
            return []
        matched = []
        pattern = pattern.strip("'\"")
        if not pattern:
            return []
        # 用缓存的路径列表做子串匹配
        for path in self._path_substrings:
            if pattern in path or pattern in Path(path).name:
                matched.append(path)
                if len(matched) >= 5:
                    break
        return matched

    def _build_documents_edges(self):
        """构建 documents 边：报告与同目录脚本的关系（用目录索引，O(报告数×同目录文件数)）"""
        edges = []
        for report in self._md_reports:
            report_dir = str(Path(report.file_path).parent)
            for fpath in self._files_by_dir.get(report_dir, []):
                if fpath == report.file_path:
                    continue
                if fpath.endswith('.py'):
                    edges.append(Edge(
                        source=report.file_path,
                        target=fpath,
                        edge_type="documents",
                        confidence=0.7,
                        evidence="Report and script in same directory",
                    ))
        self.db.edges_batch(edges)

    def _build_validates_edges(self):
        """构建 validates 边：校验脚本与同目录数据的关系（用目录索引）"""
        edges = []
        for f in self._files:
            name = Path(f.path).stem.lower()
            if 'valid' in name or 'check' in name or 'audit' in name:
                parent = str(Path(f.path).parent)
                for f2path in self._files_by_dir.get(parent, []):
                    if f2path == f.path:
                        continue
                    if f2path.endswith(('.py', '.csv', '.md', '.nc')):
                        edges.append(Edge(
                            source=f.path,
                            target=f2path,
                            edge_type="validates",
                            confidence=0.6,
                            evidence=f"Validation script {Path(f.path).name} in same directory as {Path(f2path).name}",
                        ))
        self.db.edges_batch(edges)

    def _build_derived_from_edges(self):
        """构建 derived_from 边：脚本/数据之间的衍生关系"""
        edges = []
        # epic_ceres: stages 之间的衍生关系。先筛选含 stage_N 的文件（数量少），再两两配对
        stage_files = []  # [(path, stage_num)]
        for f in self._files:
            name = Path(f.path).stem.lower()
            m = re.search(r'stage_(\d+)', name)
            if m:
                stage_files.append((f.path, int(m.group(1))))
        for fpath, stage_num in stage_files:
            for f2path, stage_num2 in stage_files:
                if fpath == f2path:
                    continue
                if stage_num2 < stage_num or stage_num2 == stage_num - 1:
                    edges.append(Edge(
                        source=fpath,
                        target=f2path,
                        edge_type="derived_from",
                        confidence=0.5,
                        evidence=f"Stage {stage_num} derived from stage {stage_num2}",
                    ))
        self.db.edges_batch(edges)

    def _build_depends_on_edges(self):
        """构建 depends_on 边：报告 blocking issue 提到的文件"""
        edges = []
        # 预建 文件名(stem) → [路径] 索引，避免对每个 issue 全表扫描
        stem_index = defaultdict(list)
        for f in self._files:
            stem_index[Path(f.path).stem.lower()].append(f.path)
        for report in self._md_reports:
            for issue in report.blocking_issues:
                issue_lower = issue.lower()
                # 用索引里已知的文件名去 issue 里找提及
                for stem, paths in stem_index.items():
                    if stem and len(stem) > 3 and stem in issue_lower:
                        for p in paths:
                            edges.append(Edge(
                                source=report.file_path,
                                target=p,
                                edge_type="depends_on",
                                confidence=0.4,
                                evidence=f"Blocking issue mentions {stem}: {issue[:100]}",
                            ))
        self.db.edges_batch(edges)