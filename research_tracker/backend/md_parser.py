"""Markdown 报告解析器 - 提取标题、结果、路径、警告等信息"""

import re
from pathlib import Path

from db import Database, MDReport


class MDParser:
    """解析 .md 报告文件，提取结构化信息"""

    def __init__(self, db: Database):
        self.db = db

    def parse(self, file_path: str) -> MDReport | None:
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except Exception:
            return None

        report_id = file_path

        # 提取标题（第一个 # 行）
        title = ""
        title_match = re.search(r'^#\s+(.+)$', text, re.MULTILINE)
        if title_match:
            title = title_match.group(1).strip()

        # 提取小标题（## 行）
        headings = re.findall(r'^#{2,4}\s+(.+)$', text, re.MULTILINE)
        headings = [h.strip() for h in headings]

        # 提取结果状态
        results = []
        # PASS / PASS_WITH_WARNINGS / FAIL / OK / ERROR
        for m in re.finditer(
            r'(PASS(?:_WITH_WARNINGS)?|FAIL|ERROR|OK)\s*(?::\s*(.+))?',
            text, re.IGNORECASE
        ):
            results.append({
                "status": m.group(1).upper(),
                "detail": (m.group(2) or "").strip(),
            })

        # 提取脚本路径
        script_paths = []
        # 支持格式: `xxx.py` [xxx.py](path/to/xxx.py) 或 xxx.py
        for m in re.finditer(r'\[([^\]]+\.py)\]\(([^)]+)\)', text):
            script_paths.append(m.group(2))
        for m in re.finditer(r'(?<!\[)([^\s()]+\.py)(?!\])', text):
            p = m.group(1).strip('`').strip(' ')
            # 常见 Python 路径格式
            if '\\' in p or '/' in p:
                script_paths.append(p)

        # 提取输出路径（.nc .csv .png .npz .json .md 等）
        output_paths = []
        for m in re.finditer(r'\[([^\]]+\.(?:nc|csv|png|npz|json|md|pptx))\]\(([^)]+)\)', text):
            output_paths.append(m.group(2))
        for m in re.finditer(r'(?<!\[)([^\s()]+\.(?:nc|csv|png|npz|json|md))(?!"|\'])', text):
            p = m.group(1).strip('`').strip(' ')
            if '\\' in p or '/' in p:
                output_paths.append(p)

        # 去重
        script_paths = list(set(script_paths))
        output_paths = list(set(output_paths))

        # 提取 warning
        warnings = []
        for m in re.finditer(
            r'(?:warning|WARNING|warn)\s*[:：]?\s*(.+?)[\.\n]',
            text, re.IGNORECASE
        ):
            w = m.group(1).strip()
            if len(w) > 5:
                warnings.append(w)

        # 提取 blocking issue
        blocking_issues = []
        for m in re.finditer(
            r'(?:blocking|BLOCKING|blocked by|failed|FAILED)\s*[:：]?\s*(.+?)[\.\n]',
            text, re.IGNORECASE
        ):
            b = m.group(1).strip()
            if len(b) > 5:
                blocking_issues.append(b)

        # summary: 前 500 字
        summary = text[:500].strip()

        report = MDReport(
            id=report_id,
            file_path=file_path,
            title=title,
            headings=headings,
            results=results,
            script_paths=script_paths,
            output_paths=output_paths,
            warnings=warnings,
            blocking_issues=blocking_issues,
            summary=summary,
        )
        return report

    def parse_and_store(self, file_path: str):
        report = self.parse(file_path)
        if report:
            self.db.upsert_md_report(report)
            self.db.commit()
            return True
        return False