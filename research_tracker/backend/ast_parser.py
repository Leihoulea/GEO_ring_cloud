"""Python AST 解析器 - 提取 imports/functions/classes/methods、调用关系、文件读写"""

import ast
import json
from pathlib import Path
from typing import Any, Optional

from db import Database, ASTNode


class CallVisitor(ast.NodeVisitor):
    """提取函数内部的调用关系和文件读写"""
    def __init__(self, current_func: str):
        self.current_func = current_func
        self.calls = []      # [(func_name, lineno)]
        self.file_reads = []  # [(file_pattern, lineno)]
        self.file_writes = [] # [(file_pattern, lineno)]

    def visit_Call(self, node: ast.Call):
        func_name = self._name_of(node.func)
        if func_name:
            self.calls.append((func_name, node.lineno or 0))
        self.generic_visit(node)

    READ_PATTERNS = ('read_csv', 'read_json', 'read_excel', 'read_hdf',
                     'read_parquet', 'open_dataset', 'open_mfdataset',
                     'loads', 'loadtxt', 'genfromtxt', 'from_csv',
                     'read_text', 'readlines', 'readline', 'imread')

    WRITE_PATTERNS = ('to_csv', 'to_netcdf', 'to_json', 'to_excel',
                      'to_hdf', 'to_parquet', 'to_pickle', 'to_sql',
                      'writelines', 'write_text', 'savefig', 'imsave',
                      'savemat')

    def _detect_read_call(self, call_node: ast.Call, lineno: int):
        """检测 read_csv/open_dataset/load 等读取调用"""
        name = self._name_of(call_node.func)
        if not name:
            return
        # 名字以任一读取方法结尾，或包含 .load / np.load / pickle.load / json.load
        if (any(name.endswith(pat) for pat in self.READ_PATTERNS)
                or name.endswith('.load') or name.endswith('Image.open')
                or name == 'np.load' or name == 'open'):
            args = [self._name_of(a) for a in call_node.args if self._name_of(a)]
            target = args[0] if args else "?"
            self.file_reads.append((target, lineno))

    def _detect_write_call(self, call_node: ast.Call, lineno: int):
        """统一检测 to_csv/to_netcdf/savefig 等写出调用"""
        name = self._name_of(call_node.func)
        if not name:
            return
        # 名字以任一写出方法结尾（如 df.to_csv / plt.savefig / np.save）
        if (any(name.endswith(pat) for pat in self.WRITE_PATTERNS)
                or name.endswith('.save') or name.endswith('.dump')
                or name.endswith('.dump') or name == 'open'):
            args = [self._name_of(a) for a in call_node.args if self._name_of(a)]
            target = args[0] if args else "?"
            self.file_writes.append((target, lineno))

    def visit_With(self, node: ast.With):
        # 检测 with open(...) as f: 形式
        for item in node.items:
            context_expr = item.context_expr
            if isinstance(context_expr, ast.Call):
                name = self._name_of(context_expr.func)
                if name and ('open' in name or 'load' in name or 'read' in name):
                    args = [self._name_of(a) for a in context_expr.args if self._name_of(a)]
                    # 判断是读还是写
                    if len(context_expr.args) >= 2:
                        mode_arg = context_expr.args[1]
                        if isinstance(mode_arg, ast.Constant) and isinstance(mode_arg.value, str):
                            if 'w' in mode_arg.value or 'a' in mode_arg.value:
                                self.file_writes.append((args[0] if args else "?", context_expr.lineno or 0))
                                continue
                    self.file_reads.append((args[0] if args else "?", context_expr.lineno or 0))
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign):
        # 检测 df.to_csv(...) 作为赋值右值（少见）以及 = open(...) 形式
        if isinstance(node.value, ast.Call):
            self._detect_write_call(node.value, node.lineno or 0)
            self._detect_read_call(node.value, node.lineno or 0)
        self.generic_visit(node)

    def visit_Expr(self, node: ast.Expr):
        # 检测独立语句形式的调用：df.to_csv("x.csv") / plt.savefig("y.png") / pd.read_csv(...)
        if isinstance(node.value, ast.Call):
            self._detect_write_call(node.value, node.lineno or 0)
            self._detect_read_call(node.value, node.lineno or 0)
        self.generic_visit(node)

    def _name_of(self, node) -> str:
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            return self._name_of(node.value) + "." + node.attr
        elif isinstance(node, ast.Call):
            return self._name_of(node.func)
        elif isinstance(node, ast.Constant):
            return str(node.value) if isinstance(node.value, str) else ""
        elif isinstance(node, ast.Subscript):
            return self._name_of(node.value) + "[]"
        return ""


class ASTParser:
    def __init__(self, db: Database):
        self.db = db

    def parse(self, file_path: str) -> list[ASTNode]:
        """解析单个 .py 文件，返回 ASTNode 列表"""
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                source = f.read()
        except Exception:
            return []

        try:
            tree = ast.parse(source, filename=file_path)
        except SyntaxError:
            return []

        nodes = []
        self._visit_module(tree, file_path, nodes)
        return nodes

    def _visit_module(self, node: ast.Module, file_path: str, nodes: list[ASTNode]):
        imports = []
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Import):
                for alias in child.names:
                    imports.append(alias.name)
            elif isinstance(child, ast.ImportFrom):
                module = child.module or ""
                for alias in child.names:
                    full = f"{module}.{alias.name}" if module else alias.name
                    imports.append(full)

        # has_main_guard
        has_main = any(
            isinstance(stmt, ast.If)
            and isinstance(stmt.test, ast.Compare)
            and any(
                isinstance(d, ast.Name) and d.id == "__name__"
                for d in ast.walk(stmt.test)
            )
            for stmt in node.body
        )

        # script level node
        script_id = file_path
        script_node = ASTNode(
            id=script_id,
            file_path=file_path,
            node_type="script",
            name=Path(file_path).name,
            qual_name=Path(file_path).name,
            imports=imports,
            has_main_guard=has_main,
        )
        nodes.append(script_node)

        # top-level functions & classes
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef):
                self._visit_function(child, file_path, script_id, nodes, imports)
            elif isinstance(child, ast.AsyncFunctionDef):
                self._visit_function(child, file_path, script_id, nodes, imports, is_async=True)
            elif isinstance(child, ast.ClassDef):
                self._visit_class(child, file_path, script_id, nodes, imports)

    def _visit_function(self, node, file_path: str, parent: str,
                        nodes: list[ASTNode], imports: list[str],
                        is_async: bool = False):
        func_name = node.name
        qual_name = f"{parent}::{func_name}" if parent else func_name
        docstring = ast.get_docstring(node) or ""

        # args
        args = [a.arg for a in node.args.args]
        returns = ast.unparse(node.returns) if node.returns else ""

        # calls and file reads/writes
        cv = CallVisitor(func_name)
        cv.visit(node)

        func_node = ASTNode(
            id=qual_name,
            file_path=file_path,
            node_type="function",
            name=func_name,
            qual_name=qual_name,
            lineno=node.lineno or 0,
            end_lineno=node.end_lineno or 0,
            docstring=docstring[:500],
            imports=imports,
            decorators=[ast.unparse(d) for d in node.decorator_list],
            has_main_guard=False,
            calls=[c[0] for c in cv.calls],
            file_reads=[r[0] for r in cv.file_reads],
            file_writes=[w[0] for w in cv.file_writes],
            args=args,
            returns=returns,
            parent=parent,
        )
        nodes.append(func_node)

        # inner functions & classes
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._visit_function(child, file_path, qual_name, nodes, imports)
            elif isinstance(child, ast.ClassDef):
                self._visit_class(child, file_path, qual_name, nodes, imports)

    def _visit_class(self, node, file_path: str, parent: str,
                     nodes: list[ASTNode], imports: list[str]):
        class_name = node.name
        qual_name = f"{parent}::{class_name}" if parent else class_name
        docstring = ast.get_docstring(node) or ""
        base_classes = [ast.unparse(b) for b in node.bases]

        class_node = ASTNode(
            id=qual_name,
            file_path=file_path,
            node_type="class",
            name=class_name,
            qual_name=qual_name,
            lineno=node.lineno or 0,
            end_lineno=node.end_lineno or 0,
            docstring=docstring[:500],
            imports=imports,
            decorators=[ast.unparse(d) for d in node.decorator_list],
            base_classes=base_classes,
        )
        nodes.append(class_node)

        # methods
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._visit_function(child, file_path, qual_name, nodes, imports)

    def parse_and_store(self, file_path: str):
        nodes = self.parse(file_path)
        for n in nodes:
            self.db.upsert_ast_node(n)
        self.db.commit()
        return len(nodes)