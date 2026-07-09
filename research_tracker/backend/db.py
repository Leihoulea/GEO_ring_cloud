"""SQLite 数据库层 - 支持增量扫描的本地存储"""

import sqlite3
import json
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass, field, asdict
from datetime import datetime

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    file_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    hash TEXT NOT NULL,
    stage TEXT DEFAULT '',
    subproject TEXT DEFAULT '',
    last_scanned TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ast_nodes (
    id TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    node_type TEXT NOT NULL,
    name TEXT NOT NULL,
    qual_name TEXT NOT NULL,
    lineno INTEGER,
    end_lineno INTEGER,
    docstring TEXT DEFAULT '',
    imports TEXT DEFAULT '[]',
    decorators TEXT DEFAULT '[]',
    base_classes TEXT DEFAULT '[]',
    has_main_guard INTEGER DEFAULT 0,
    calls TEXT DEFAULT '[]',
    file_reads TEXT DEFAULT '[]',
    file_writes TEXT DEFAULT '[]',
    args TEXT DEFAULT '[]',
    returns TEXT DEFAULT '',
    parent TEXT DEFAULT '',
    FOREIGN KEY (file_path) REFERENCES files(path)
);

CREATE TABLE IF NOT EXISTS md_reports (
    id TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    title TEXT,
    headings TEXT DEFAULT '[]',
    results TEXT DEFAULT '[]',
    script_paths TEXT DEFAULT '[]',
    output_paths TEXT DEFAULT '[]',
    warnings TEXT DEFAULT '[]',
    blocking_issues TEXT DEFAULT '[]',
    summary TEXT DEFAULT '',
    FOREIGN KEY (file_path) REFERENCES files(path)
);

CREATE TABLE IF NOT EXISTS csv_tables (
    id TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    format TEXT NOT NULL,
    columns TEXT DEFAULT '[]',
    row_count INTEGER DEFAULT 0,
    key_fields TEXT DEFAULT '[]',
    time_range TEXT DEFAULT '',
    products TEXT DEFAULT '[]',
    satellites TEXT DEFAULT '[]',
    status_fields TEXT DEFAULT '[]',
    FOREIGN KEY (file_path) REFERENCES files(path)
);

CREATE TABLE IF NOT EXISTS edges (
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    confidence REAL DEFAULT 0.5,
    evidence TEXT DEFAULT '',
    PRIMARY KEY (source, target, edge_type)
);

CREATE TABLE IF NOT EXISTS annotations (
    node_id TEXT PRIMARY KEY,
    importance TEXT DEFAULT 'normal',
    status TEXT DEFAULT 'active',
    note TEXT DEFAULT '',
    tags TEXT DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_ast_file ON ast_nodes(file_path);
CREATE INDEX IF NOT EXISTS idx_ast_qual ON ast_nodes(qual_name);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type);
"""


@dataclass
class FileRecord:
    path: str
    file_type: str
    size: int
    mtime: float
    hash: str
    stage: str = ""
    subproject: str = ""
    last_scanned: str = ""


@dataclass
class ASTNode:
    id: str
    file_path: str
    node_type: str
    name: str
    qual_name: str
    lineno: int = 0
    end_lineno: int = 0
    docstring: str = ""
    imports: list = field(default_factory=list)
    decorators: list = field(default_factory=list)
    base_classes: list = field(default_factory=list)
    has_main_guard: bool = False
    calls: list = field(default_factory=list)
    file_reads: list = field(default_factory=list)
    file_writes: list = field(default_factory=list)
    args: list = field(default_factory=list)
    returns: str = ""
    parent: str = ""


@dataclass
class MDReport:
    id: str
    file_path: str
    title: str = ""
    headings: list = field(default_factory=list)
    results: list = field(default_factory=list)
    script_paths: list = field(default_factory=list)
    output_paths: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    blocking_issues: list = field(default_factory=list)
    summary: str = ""


@dataclass
class CSVTable:
    id: str
    file_path: str
    format: str = ""
    columns: list = field(default_factory=list)
    row_count: int = 0
    key_fields: list = field(default_factory=list)
    time_range: str = ""
    products: list = field(default_factory=list)
    satellites: list = field(default_factory=list)
    status_fields: list = field(default_factory=list)


@dataclass
class Edge:
    source: str
    target: str
    edge_type: str
    confidence: float = 0.5
    evidence: str = ""


class Database:
    def __init__(self, db_path: str = "research_tracker.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(DB_SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ---- File operations ----

    def file_exists(self, path: str) -> Optional[FileRecord]:
        row = self.conn.execute(
            "SELECT * FROM files WHERE path = ?", (path,)
        ).fetchone()
        if row:
            return FileRecord(**dict(row))
        return None

    def upsert_file(self, rec: FileRecord):
        self.conn.execute(
            """INSERT OR REPLACE INTO files (path, file_type, size, mtime, hash, stage, subproject, last_scanned)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (rec.path, rec.file_type, rec.size, rec.mtime, rec.hash,
             rec.stage, rec.subproject, rec.last_scanned),
        )

    def delete_file(self, path: str):
        self.conn.execute("DELETE FROM ast_nodes WHERE file_path = ?", (path,))
        self.conn.execute("DELETE FROM md_reports WHERE file_path = ?", (path,))
        self.conn.execute("DELETE FROM csv_tables WHERE file_path = ?", (path,))
        self.conn.execute("DELETE FROM files WHERE path = ?", (path,))
        self.conn.execute(
            "DELETE FROM edges WHERE source = ? OR target = ?", (path, path)
        )

    def all_files(self):
        return [FileRecord(**dict(r)) for r in self.conn.execute("SELECT * FROM files").fetchall()]

    # ---- AST operations ----

    def upsert_ast_node(self, node: ASTNode):
        self.conn.execute(
            """INSERT OR REPLACE INTO ast_nodes
               (id, file_path, node_type, name, qual_name, lineno, end_lineno,
                docstring, imports, decorators, base_classes, has_main_guard,
                calls, file_reads, file_writes, args, returns, parent)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (node.id, node.file_path, node.node_type, node.name, node.qual_name,
             node.lineno, node.end_lineno, node.docstring,
             json.dumps(node.imports), json.dumps(node.decorators),
             json.dumps(node.base_classes), int(node.has_main_guard),
             json.dumps(node.calls), json.dumps(node.file_reads),
             json.dumps(node.file_writes), json.dumps(node.args),
             node.returns, node.parent),
        )

    def ast_nodes_for_file(self, file_path: str):
        rows = self.conn.execute(
            "SELECT * FROM ast_nodes WHERE file_path = ?", (file_path,)
        ).fetchall()
        return [self._row_to_astnode(r) for r in rows]

    def all_ast_nodes(self):
        rows = self.conn.execute("SELECT * FROM ast_nodes").fetchall()
        return [self._row_to_astnode(r) for r in rows]

    def _row_to_astnode(self, r) -> ASTNode:
        d = dict(r)
        for lst_field in ['imports', 'decorators', 'base_classes', 'calls',
                          'file_reads', 'file_writes', 'args']:
            if isinstance(d.get(lst_field), str):
                d[lst_field] = json.loads(d[lst_field])
        d['has_main_guard'] = bool(d.get('has_main_guard', False))
        return ASTNode(**d)

    # ---- MD Report operations ----

    def upsert_md_report(self, report: MDReport):
        self.conn.execute(
            """INSERT OR REPLACE INTO md_reports
               (id, file_path, title, headings, results, script_paths,
                output_paths, warnings, blocking_issues, summary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (report.id, report.file_path, report.title,
             json.dumps(report.headings), json.dumps(report.results),
             json.dumps(report.script_paths), json.dumps(report.output_paths),
             json.dumps(report.warnings), json.dumps(report.blocking_issues),
             report.summary),
        )

    def all_md_reports(self):
        rows = self.conn.execute("SELECT * FROM md_reports").fetchall()
        return [self._row_to_mdr(r) for r in rows]

    def _row_to_mdr(self, r) -> MDReport:
        d = dict(r)
        for lst_field in ['headings', 'results', 'script_paths',
                          'output_paths', 'warnings', 'blocking_issues']:
            if isinstance(d.get(lst_field), str):
                d[lst_field] = json.loads(d[lst_field])
        return MDReport(**d)

    # ---- CSV Table operations ----

    def upsert_csv_table(self, table: CSVTable):
        self.conn.execute(
            """INSERT OR REPLACE INTO csv_tables
               (id, file_path, format, columns, row_count, key_fields,
                time_range, products, satellites, status_fields)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (table.id, table.file_path, table.format,
             json.dumps(table.columns), table.row_count,
             json.dumps(table.key_fields), table.time_range,
             json.dumps(table.products), json.dumps(table.satellites),
             json.dumps(table.status_fields)),
        )

    def all_csv_tables(self):
        rows = self.conn.execute("SELECT * FROM csv_tables").fetchall()
        return [self._row_to_csvt(r) for r in rows]

    def _row_to_csvt(self, r) -> CSVTable:
        d = dict(r)
        for lst_field in ['columns', 'key_fields', 'products',
                          'satellites', 'status_fields']:
            if isinstance(d.get(lst_field), str):
                d[lst_field] = json.loads(d[lst_field])
        return CSVTable(**d)

    # ---- Edge operations ----

    def upsert_edge(self, edge: Edge):
        self.conn.execute(
            """INSERT OR REPLACE INTO edges (source, target, edge_type, confidence, evidence)
               VALUES (?, ?, ?, ?, ?)""",
            (edge.source, edge.target, edge.edge_type, edge.confidence, edge.evidence),
        )

    def edges_batch(self, edges: list[Edge]):
        self.conn.executemany(
            """INSERT OR REPLACE INTO edges (source, target, edge_type, confidence, evidence)
               VALUES (?, ?, ?, ?, ?)""",
            [(e.source, e.target, e.edge_type, e.confidence, e.evidence) for e in edges],
        )

    def all_edges(self):
        rows = self.conn.execute("SELECT * FROM edges").fetchall()
        return [Edge(**dict(r)) for r in rows]

    def edges_of_type(self, edge_type: str):
        rows = self.conn.execute(
            "SELECT * FROM edges WHERE edge_type = ?", (edge_type,)
        ).fetchall()
        return [Edge(**dict(r)) for r in rows]

    # ---- Annotation operations ----

    def upsert_annotation(self, node_id: str, importance: str = "normal",
                          status: str = "active", note: str = "", tags: list = None):
        self.conn.execute(
            """INSERT OR REPLACE INTO annotations (node_id, importance, status, note, tags)
               VALUES (?, ?, ?, ?, ?)""",
            (node_id, importance, status, note, json.dumps(tags or [])),
        )

    def get_annotation(self, node_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM annotations WHERE node_id = ?", (node_id,)
        ).fetchone()
        if row:
            d = dict(row)
            if isinstance(d.get('tags'), str):
                d['tags'] = json.loads(d['tags'])
            return d
        return None

    def all_annotations(self):
        rows = self.conn.execute("SELECT * FROM annotations").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get('tags'), str):
                d['tags'] = json.loads(d['tags'])
            result.append(d)
        return result

    # ---- Bulk export ----

    def _apply_annotation(self, n: dict, node_id: str):
        """把数据库中的标注合并进节点字典"""
        ann = self.get_annotation(node_id)
        if ann:
            n["importance"] = ann["importance"]
            n["status"] = ann["status"]
            n["note"] = ann["note"]
            n["tags"] = ann.get("tags", [])
        else:
            n.setdefault("importance", "normal")
            n.setdefault("status", "active")
            n.setdefault("tags", [])

    def export_nodes_json(self, include_aggregates: bool = True) -> list[dict]:
        """导出所有节点（聚合+文件+AST+MD+CSV）到统一JSON

        节点去重：files 表与 ast_nodes 表的 script 类型合并，避免同一文件出现两次。
        聚合节点：project / subproject / stage / directory（可选）。
        折叠标记：function/class/method 默认 collapsed=true。
        """
        nodes = []
        seen_ids = set()
        _apply_ann = self._apply_annotation  # 局部别名，便于阅读

        # 1) 收集所有 AST 节点的 script id 集合，用于文件去重
        ast_script_ids = set()
        for a in self.all_ast_nodes():
            if a.node_type == "script":
                ast_script_ids.add(a.id)

        # 2) 文件级节点（跳过已在 ast_nodes 表出现的 script，避免重复）
        file_nodes = []
        for f in self.all_files():
            if f.file_type == "script" and f.path in ast_script_ids:
                continue  # 由 ast_nodes 表的 script 节点统一代表
            n = {"id": f.path, "type": f.file_type, "label": Path(f.path).name,
                 "path": f.path, "stage": f.stage, "subproject": f.subproject,
                 "size": f.size, "mtime": f.mtime, "hash": f.hash,
                 "collapsed": False}
            _apply_ann(n, f.path)
            file_nodes.append(n)
            seen_ids.add(f.path)
        nodes.extend(file_nodes)

        # 3) AST 节点（function/class/method 折叠；script 代表 .py 文件）
        for a in self.all_ast_nodes():
            if a.id in seen_ids:
                continue
            is_code = a.node_type in ("function", "method", "class")
            # label: 函数/类/方法只显示短名（a.name），script 显示文件名
            if a.node_type == "script":
                label = Path(a.file_path).name
            else:
                label = a.name
            n = {"id": a.id, "type": a.node_type if a.node_type == "script" else f"ast_{a.node_type}",
                 "label": label,
                 "path": a.file_path, "qual_name": a.qual_name,
                 "lineno": a.lineno, "end_lineno": a.end_lineno,
                 "docstring": a.docstring[:200] if a.docstring else "",
                 "has_main_guard": a.has_main_guard,
                 "args": a.args, "returns": a.returns,
                 "parent": a.parent,
                 "imports": a.imports[:30] if a.node_type == "script" else [],
                 "base_classes": a.base_classes,
                 "collapsed": is_code,  # 函数/类/方法默认折叠
                 "stage": "", "subproject": ""}
            # 继承文件级 stage/subproject
            for f in self.all_files():
                if f.path == a.file_path:
                    n["stage"] = f.stage
                    n["subproject"] = f.subproject
                    break
            _apply_ann(n, a.id)
            nodes.append(n)
            seen_ids.add(a.id)

        # 4) md 报告节点（去重：若文件已是 report 类型则合并字段）
        for r in self.all_md_reports():
            if r.id in seen_ids:
                # 合并到已有 report 节点
                for n in nodes:
                    if n["id"] == r.id:
                        n["title"] = r.title
                        n["summary"] = r.summary[:200] if r.summary else ""
                        n["warnings_count"] = len(r.warnings)
                        n["blocking_count"] = len(r.blocking_issues)
                        n["headings_count"] = len(r.headings)
                        n["results"] = r.results[:20]
                        break
                continue
            n = {"id": r.id, "type": "report", "label": Path(r.file_path).name,
                 "path": r.file_path, "title": r.title,
                 "summary": r.summary[:200] if r.summary else "",
                 "warnings_count": len(r.warnings),
                 "blocking_count": len(r.blocking_issues),
                 "headings_count": len(r.headings),
                 "results": r.results[:20],
                 "collapsed": False}
            _apply_ann(n, r.id)
            nodes.append(n)
            seen_ids.add(r.id)

        # 5) csv/json 表节点（去重）
        for t in self.all_csv_tables():
            if t.id in seen_ids:
                for n in nodes:
                    if n["id"] == t.id:
                        n["format"] = t.format
                        n["columns"] = t.columns
                        n["row_count"] = t.row_count
                        n["key_fields"] = t.key_fields
                        n["time_range"] = t.time_range
                        n["products"] = t.products
                        n["satellites"] = t.satellites
                        break
                continue
            n = {"id": t.id, "type": "csv_table", "label": Path(t.file_path).name,
                 "path": t.file_path, "format": t.format,
                 "columns": t.columns, "row_count": t.row_count,
                 "key_fields": t.key_fields, "time_range": t.time_range,
                 "products": t.products, "satellites": t.satellites,
                 "collapsed": False}
            _apply_ann(n, t.id)
            nodes.append(n)
            seen_ids.add(t.id)

        # 6) 聚合节点：project / subproject / stage
        if include_aggregates:
            nodes = self._add_aggregate_nodes(nodes)

        return nodes

    def _add_aggregate_nodes(self, nodes: list[dict]) -> list[dict]:
        """添加 project / subproject / stage / directory 聚合节点"""
        from collections import defaultdict
        aggs = {}

        def _ensure(agg_id: str, agg_type: str, label: str, parent: str = ""):
            if agg_id not in aggs:
                aggs[agg_id] = {"id": agg_id, "type": agg_type, "label": label,
                                "parent": parent, "collapsed": False,
                                "importance": "normal", "status": "active",
                                "tags": [], "note": "", "child_count": 0}
                self._apply_annotation(aggs[agg_id], agg_id)
            return aggs[agg_id]

        # project 根
        project_id = "__project__"
        _ensure(project_id, "project", "Research Project")

        # subproject / stage 聚合
        sp_stages = defaultdict(set)
        for n in nodes:
            sp = n.get("subproject", "")
            sg = n.get("stage", "")
            if sp:
                sp_id = f"__sp__{sp}"
                _ensure(sp_id, "subproject", sp, project_id)
                n["subproject_id"] = sp_id
                aggs[sp_id]["child_count"] += 1
                if sg:
                    stage_id = f"__stage__{sp}__{sg}"
                    _ensure(stage_id, "stage", f"{sp} / {sg}", sp_id)
                    n["stage_id"] = stage_id
                    aggs[stage_id]["child_count"] += 1
                    sp_stages[sp].add(sg)
            elif sg:
                stage_id = f"__stage__{sg}"
                _ensure(stage_id, "stage", sg, project_id)
                n["stage_id"] = stage_id
                aggs[stage_id]["child_count"] += 1

        return list(aggs.values()) + nodes

    def export_edges_json(self) -> list[dict]:
        return [{"source": e.source, "target": e.target,
                 "type": e.edge_type, "confidence": e.confidence,
                 "evidence": e.evidence}
                for e in self.all_edges()]

    def export_graph_json(self, graph_path: str):
        """写出完整的 nodes.json + edges.json"""
        import json, os
        os.makedirs(os.path.dirname(graph_path) or ".", exist_ok=True)
        nodes = self.export_nodes_json()
        edges = self.export_edges_json()
        graph = {"nodes": nodes, "edges": edges}
        with open(graph_path, "w", encoding="utf-8") as f:
            json.dump(graph, f, ensure_ascii=False, indent=2)
        return graph

    def commit(self):
        self.conn.commit()