"""人工标注处理器 - 从 YAML 文件读取标注并更新数据库"""

from pathlib import Path
from typing import Any

from db import Database, Edge


class AnnotationProcessor:
    """处理 research_tracker_annotations.yml 中的标注"""

    def __init__(self, db: Database):
        self.db = db
        self.edge_count = 0

    def load_and_apply(self, yaml_path: str) -> int:
        """从 YAML 文件加载标注并应用到数据库

        支持：
        - annotations.<node_path>: 节点级标注（importance/status/note/tags）
        - annotations.<node_path>.functions.<name>: 函数级标注
        - annotations.<node_path>.classes.<name>: 类级标注
        - annotations.<node_path>.edges: 该节点相关边修正（补充/覆盖）
        - edges: 顶层边标注（手工定义/修正关系）
        - scan: 扫描配置（排除规则等，供 scanner 参考）
        """
        try:
            import yaml
        except ImportError:
            print("⚠️  pyyaml 未安装，跳过标注处理")
            return 0

        yaml_path = Path(yaml_path)
        if not yaml_path.exists():
            return 0

        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception as e:
            print(f"⚠️  标注文件解析失败: {e}")
            return 0

        if not data:
            return 0

        count = 0
        annotations = data.get("annotations") or {}

        for node_path, annotation in annotations.items():
            if not isinstance(annotation, dict):
                continue
            # 节点级标注
            self.db.upsert_annotation(
                node_id=node_path,
                importance=annotation.get("importance", "normal"),
                status=annotation.get("status", "active"),
                note=annotation.get("note", ""),
                tags=annotation.get("tags", []),
            )
            count += 1

            # 函数级标注
            for func_name, func_ann in (annotation.get("functions") or {}).items():
                qual_name = f"{node_path}::{func_name}"
                self.db.upsert_annotation(
                    node_id=qual_name,
                    importance=func_ann.get("importance", "normal"),
                    status=func_ann.get("status", "active"),
                    note=func_ann.get("note", ""),
                    tags=func_ann.get("tags", []),
                )
                count += 1

            # 类级标注
            for class_name, class_ann in (annotation.get("classes") or {}).items():
                qual_name = f"{node_path}::{class_name}"
                self.db.upsert_annotation(
                    node_id=qual_name,
                    importance=class_ann.get("importance", "normal"),
                    status=class_ann.get("status", "active"),
                    note=class_ann.get("note", ""),
                    tags=class_ann.get("tags", []),
                )
                count += 1

            # 节点内嵌的边修正
            for e in (annotation.get("edges") or []):
                self._apply_edge(e, default_source=node_path)
                count += 1

        # 顶层 edges 段：手工定义/修正全局边
        for e in data.get("edges", []) or []:
            self._apply_edge(e)
            count += 1

        self.db.commit()
        return count

    def _apply_edge(self, e: dict, default_source: str = ""):
        """应用一条边标注。手工边 confidence 默认 1.0，evidence 标注来源。"""
        source = e.get("source") or default_source
        target = e.get("target")
        etype = e.get("type") or e.get("edge_type")
        if not source or not target or not etype:
            return
        confidence = e.get("confidence", 1.0)  # 手工边默认高置信
        evidence = e.get("evidence", "manual annotation")
        self.db.upsert_edge(Edge(
            source=source, target=target, edge_type=etype,
            confidence=confidence, evidence=evidence,
        ))
        self.edge_count += 1