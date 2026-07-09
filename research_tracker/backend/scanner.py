"""目录扫描器 - 递归扫描指定根目录，排除常见非源码目录"""

import hashlib
import os
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

from db import Database, FileRecord

# 默认排除目录
EXCLUDE_DIRS = {
    ".git", "__pycache__", "venv", ".venv", "node_modules",
    ".idea", ".vscode", "*.egg-info", ".mypy_cache",
    ".pytest_cache", ".tox", "build", "dist",
    "_NON_GEO_ARCHIVE", "_GEO_RING_CLOUD_WORKSPACE",
}

# 默认排除扩展名
EXCLUDE_EXTENSIONS = {
    ".pyc", ".pyo", ".exe", ".dll", ".so", ".bin",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".ico",
    ".pdf", ".pptx", ".ppt", ".docx", ".doc", ".xlsx",
    ".mp4", ".avi", ".mov", ".flv",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
}

# 文件类型映射
FILE_TYPE_MAP = {
    ".py": "script",
    ".md": "report",
    ".csv": "data_file",
    ".json": "config",
    ".yaml": "config",
    ".yml": "config",
    ".txt": "report",
    ".xml": "data_file",
    ".html": "report",
    ".css": "script",
    ".js": "script",
    ".ts": "script",
    ".vue": "script",
    ".ipynb": "notebook",
    ".nc": "data_file",
    ".hdf": "data_file",
    ".h5": "data_file",
    ".grib": "data_file",
    ".npz": "data_file",
    ".npy": "data_file",
}

# 根据目录名推断阶段
STAGE_PATTERNS = [
    (r'stage(\d+)', lambda m: f'Stage{m.group(1)}'),
    (r'(\d+)_build_.*', lambda m: f'BuildStep{m.group(1)}'),
    (r'(\d+)_[a-z].*', lambda m: f'Step{m.group(1)}'),
]

# 根据目录名推断子项目
SUBPROJECT_PATTERNS = [
    (r'^epic_ceres', 'epic_ceres'),
    (r'^geo_data_audit', 'geo_data_audit'),
    (r'^geo_cloud_download', 'geo_cloud_download'),
    (r'^geo_ring_cloud_stage1', 'geo_ring_cloud_stage1'),
    (r'^priority_download', 'priority_download'),
    (r'^paper_notion_manager', 'paper_notion_manager'),
    (r'^(FY4B|GOES|Himawari|Meteosat)', r'\1'),
]


def infer_stage(file_path: str, rel_path: str) -> str:
    """从路径推断阶段"""
    for pattern, repl in STAGE_PATTERNS:
        import re
        m = re.search(pattern, rel_path)
        if m:
            if callable(repl):
                return repl(m)
            return repl
    return ""


def infer_subproject(file_path: str, rel_path: str) -> str:
    """从路径推断子项目"""
    import re
    for pattern, repl in SUBPROJECT_PATTERNS:
        m = re.search(pattern, rel_path)
        if m:
            if callable(repl):
                r = repl(m)
                if not isinstance(r, str):
                    r = m.group(1) if m.groups() else ""
                return r
            return repl
    return ""


def compute_hash(path: str) -> str:
    """计算文件 SHA-256 哈希"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def file_type(path: str) -> str:
    ext = Path(path).suffix.lower()
    return FILE_TYPE_MAP.get(ext, "data_file")


def should_exclude(name: str, root: str) -> bool:
    """检查是否应排除"""
    if name in EXCLUDE_DIRS:
        return True
    for pat in EXCLUDE_DIRS:
        if pat.startswith("*") and name.endswith(pat[1:]):
            return True
    ext = Path(name).suffix.lower()
    if ext in EXCLUDE_EXTENSIONS:
        return True
    return False


class Scanner:
    def __init__(self, db: Database, root_dir: str, incremental: bool = True):
        self.db = db
        self.root = Path(root_dir).resolve()
        self.incremental = incremental
        self.now = datetime.now(timezone.utc).isoformat()
        self.scanned = 0
        self.skipped = 0
        self.deleted = 0

    def scan(self):
        """执行扫描"""
        root_str = str(self.root)
        known_paths = set()

        for dirpath, dirnames, filenames in os.walk(self.root):
            # 过滤排除目录
            dirnames[:] = [d for d in dirnames if not should_exclude(d, dirpath)]

            rel_dir = Path(dirpath).relative_to(self.root)

            for filename in filenames:
                filepath = os.path.join(dirpath, filename)
                rel_path = str(Path(rel_dir) / filename) if str(rel_dir) != "." else filename
                known_paths.add(filepath)

                if should_exclude(filename, dirpath):
                    continue

                rel_path_lower = rel_path.lower()
                ext = Path(filename).suffix.lower()

                # 只处理关键文件类型
                if ext not in FILE_TYPE_MAP and ext not in ('.py', '.md', '.csv', '.json', '.yaml', '.yml', '.txt'):
                    # 对于大型二进制数据文件，只记录目录，不解析
                    if ext in ('.nc', '.hdf', '.h5', '.grib', '.npz', '.npy'):
                        pass  # 继续记录
                    else:
                        continue

                try:
                    stat = os.stat(filepath)
                    size = stat.st_size
                    mtime = stat.st_mtime
                except OSError:
                    continue

                # 检查是否需要跳过（hash 未变）
                file_hash = None
                existing = self.db.file_exists(filepath)

                if self.incremental and existing:
                    # 快速检查 mtime 是否一致
                    if abs(existing.mtime - mtime) < 60:
                        self.skipped += 1
                        continue

                # 计算 hash
                file_hash = compute_hash(filepath)

                if self.incremental and existing and existing.hash == file_hash:
                    self.skipped += 1
                    continue

                # 记录文件
                ft = file_type(filepath)
                rec = FileRecord(
                    path=filepath,
                    file_type=ft,
                    size=size,
                    mtime=mtime,
                    hash=file_hash,
                    stage=infer_stage(filepath, rel_path),
                    subproject=infer_subproject(filepath, rel_path),
                    last_scanned=self.now,
                )
                self.db.upsert_file(rec)
                self.scanned += 1

        # 标记移除的文件
        if self.incremental:
            for f in self.db.all_files():
                if f.path not in known_paths:
                    self.db.delete_file(f.path)
                    self.deleted += 1

        self.db.commit()

        return {
            "scanned": self.scanned,
            "skipped": self.skipped,
            "deleted": self.deleted,
        }
