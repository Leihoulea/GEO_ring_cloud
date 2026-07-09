"""CSV/JSON 数据文件解析器 - 提取列名、行数、关键字段、时间范围、产品/卫星/状态字段"""

import csv
import json
import re
from pathlib import Path
from typing import Any

from db import Database, CSVTable


CSV_PRODUCT_KEYWORDS = [
    "product", "satellite", "platform", "mission", "sensor",
    "collection", "dataset", "variable", "parameter",
    "product_type", "source", "type",
]
CSV_STATUS_KEYWORDS = [
    "status", "state", "flag", "quality", "pass", "fail",
    "check", "valid", "availability", "confidence", "error",
    "warning", "result",
]
CSV_TIME_KEYWORDS = [
    "time", "date", "datetime", "timestamp", "utc", "start_time",
    "end_time", "scene_time", "obs_time", "year", "month", "day",
    "hour", "minute", "second",
]


class CSVParser:
    def __init__(self, db: Database):
        self.db = db

    def parse_csv(self, file_path: str) -> CSVTable | None:
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                sample = f.read(8192)
                f.seek(0)
                # 检测分隔符
                dialect = csv.Sniffer().sniff(sample[:4096])
                reader = csv.DictReader(f, dialect=dialect)
                columns = reader.fieldnames or []
                rows = list(reader)
        except Exception:
            return None

        return self._build_table(file_path, "csv", columns, rows)

    def parse_json(self, file_path: str) -> CSVTable | None:
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                data = json.load(f)
        except Exception:
            return None

        if isinstance(data, dict):
            columns = list(data.keys())
            rows = [data]
        elif isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
            columns = list(data[0].keys())
            rows = data
        else:
            columns = []
            rows = []

        return self._build_table(file_path, "json", columns, rows)

    def _build_table(self, file_path: str, fmt: str,
                     columns: list[str] | None, rows: list[dict]) -> CSVTable:
        if not columns:
            return None

        row_count = len(rows)

        # 检测关键字段
        key_fields = []
        for col in columns:
            col_lower = col.lower().strip()
            # 识别 ID/name/type 类字段
            if any(kw in col_lower for kw in ["id", "name", "key", "label", "type", "code", "path"]):
                key_fields.append(col)

        # 提取卫星/产品字段（取唯一值）
        products = []
        satellites = []
        for col in columns:
            col_lower = col.lower().strip()
            if any(kw in col_lower for kw in ["product", "dataset", "collection", "variable"]):
                vals = list(set(
                    str(r[col]).strip() for r in rows if col in r and r[col]
                ))
                products.extend(v[:50] for v in vals if len(v) < 100)
            if any(kw in col_lower for kw in ["satellite", "platform", "mission", "sensor"]):
                vals = list(set(
                    str(r[col]).strip() for r in rows if col in r and r[col]
                ))
                satellites.extend(v[:50] for v in vals if len(v) < 100)

        # 时间范围
        time_range = ""
        for col in columns:
            col_lower = col.lower().strip()
            if any(kw in col_lower for kw in CSV_TIME_KEYWORDS):
                times = sorted(set(
                    str(r[col]).strip()[:19] for r in rows
                    if col in r and r[col] and str(r[col]).strip()
                ))
                if len(times) >= 2:
                    time_range = f"{times[0]} ~ {times[-1]}"
                    break
                elif len(times) == 1:
                    time_range = times[0]
                    break

        # 状态字段
        status_fields = []
        for col in columns:
            col_lower = col.lower().strip()
            if any(kw in col_lower for kw in CSV_STATUS_KEYWORDS):
                status_fields.append(col)

        table_id = file_path
        table = CSVTable(
            id=table_id,
            file_path=file_path,
            format=fmt,
            columns=columns,
            row_count=row_count,
            key_fields=key_fields[:10],
            time_range=time_range,
            products=list(set(products))[:20],
            satellites=list(set(satellites))[:20],
            status_fields=status_fields[:10],
        )
        return table

    def parse_and_store(self, file_path: str):
        ext = Path(file_path).suffix.lower()
        table = None
        if ext == '.csv':
            table = self.parse_csv(file_path)
        elif ext == '.json':
            table = self.parse_json(file_path)
        if table:
            self.db.upsert_csv_table(table)
            self.db.commit()
            return True
        return False