#!/usr/bin/env python3
"""research_tracker 扫描入口 - 使用: python run_scan.py <root_dir> [--db <db_path>] [--rebuild-graph]"""

import argparse, json, os, sys, time
from pathlib import Path

# 将 backend 目录加入路径
sys.path.insert(0, str(Path(__file__).parent))

# Windows 下强制 stdout 用 UTF-8，避免 emoji/中文触发 GBK 编码错误
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from db import Database
from scanner import Scanner
from ast_parser import ASTParser
from md_parser import MDParser
from csv_parser import CSVParser
from graph_builder import GraphBuilder
from annotations import AnnotationProcessor
from report_generator import ReportGenerator


def main():
    parser = argparse.ArgumentParser(description="Research Tracker - 科研项目扫描与知识图谱构建")
    parser.add_argument("root_dir", help="要扫描的科研项目根目录")
    parser.add_argument("--db", default="research_tracker.db", help="SQLite 数据库路径")
    parser.add_argument("--rebuild-graph", action="store_true", help="跳过增量扫描，仅重建图谱和报告")
    parser.add_argument("--no-incremental", action="store_true", help="关闭增量扫描，重新扫描所有文件")
    parser.add_argument("--json-out", default="", help="图数据 JSON 输出路径（默认输出到 frontend/src/data/）")
    parser.add_argument("--annotations", default="", help="人工标注 YAML 文件路径")
    parser.add_argument("--gen-reports", action="store_true", help="生成分析报告")
    args = parser.parse_args()

    root = Path(args.root_dir).resolve()
    if not root.exists():
        print(f"错误: 根目录不存在: {root}")
        sys.exit(1)

    db_path = Path(args.db).resolve()
    db = Database(str(db_path))
    t_start = time.time()

    print(f"\n{'='*60}")
    print(f"  Research Tracker — 科研项目知识图谱构建")
    print(f"  根目录: {root}")
    print(f"  数据库: {db_path}")
    print(f"{'='*60}\n")

    try:
        # Step 1: 扫描目录
        if args.rebuild_graph:
            print("[skip] 跳过扫描，使用已有数据库重建图谱...")
        else:
            print("[scan] 扫描目录文件...")
            scanner = Scanner(db, str(root), incremental=not args.no_incremental)
            result = scanner.scan()
            print(f"   [ok] 新增/修改: {result['scanned']}, 跳过: {result['skipped']}, 移除: {result['deleted']}")

        # Step 2: 解析 Python AST
        print("\n[ast]  解析 Python AST...")
        ast_parser = ASTParser(db)
        ast_count = 0
        for f in db.all_files():
            if not f.path.endswith('.py'):
                continue
            nodes = ast_parser.parse(f.path)
            if nodes:
                for n in nodes:
                    db.upsert_ast_node(n)
                ast_count += len(nodes)
            else:
                # 对于解析失败的文件，标记旧的AST节点
                pass
        db.commit()
        print(f"   [ok] 解析了 {ast_count} 个 AST 节点")

        # Step 3: 解析 Markdown 报告
        print("[md]   解析 Markdown 报告...")
        md_parser = MDParser(db)
        md_count = 0
        for f in db.all_files():
            if not f.path.endswith('.md'):
                continue
            if md_parser.parse_and_store(f.path):
                md_count += 1
        print(f"   [ok] 解析了 {md_count} 个 Markdown 报告")

        # Step 4: 解析 CSV/JSON 数据文件
        print("[data] 解析 CSV/JSON 数据文件...")
        csv_parser = CSVParser(db)
        csv_count = 0
        for f in db.all_files():
            ext = Path(f.path).suffix.lower()
            if ext not in ('.csv', '.json'):
                continue
            if csv_parser.parse_and_store(f.path):
                csv_count += 1
        print(f"   [ok] 解析了 {csv_count} 个数据文件")

        # Step 5: 构建图谱边
        print("[graph]构建知识图谱边...")
        gb = GraphBuilder(db)
        gb.build_all_edges()
        edge_count = len(db.all_edges())
        print(f"   [ok] 构建了 {edge_count} 条边")

        # Step 6: 应用人工标注
        annotation_path = args.annotations or str(root / "research_tracker" / "research_tracker_annotations.yml")
        if Path(annotation_path).exists():
            print("[ann]  应用人工标注...")
            ap = AnnotationProcessor(db)
            ann_count = ap.load_and_apply(annotation_path)
            print(f"   [ok] 应用了 {ann_count} 条标注")

        # Step 7: 导出图数据 JSON
        print("[json] 导出图数据 JSON...")
        json_out = args.json_out or str(root / "research_tracker" / "frontend" / "src" / "data" / "graph.json")
        graph = db.export_graph_json(json_out)
        print(f"   [ok] 导出 {len(graph['nodes'])} 节点, {len(graph['edges'])} 边 -> {json_out}")

        # Step 8: 生成报告
        if args.gen_reports:
            print("[rpt]  生成分析报告...")
            rg = ReportGenerator(db, str(root))
            rg.generate_all()
            report_dir = root / "research_tracker" / "reports"
            print(f"   [ok] 报告已生成 -> {report_dir}")

        elapsed = time.time() - t_start
        print(f"\n{'='*60}")
        print(f"  [done] 完成! 耗时 {elapsed:.1f} 秒")
        print(f"  节点数: {len(graph['nodes'])}, 边数: {len(graph['edges'])}")
        print(f"{'='*60}\n")

    finally:
        db.close()


if __name__ == "__main__":
    main()