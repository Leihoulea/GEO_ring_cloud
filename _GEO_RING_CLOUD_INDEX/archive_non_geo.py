# -*- coding: utf-8 -*-
"""Archive directories that are outside the GEO-ring Cloud workstream.

Default mode is dry-run. Use --execute after reviewing the manifest.
"""
from __future__ import annotations

import argparse
import csv
import shutil
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(r"D:\AAAresearch_paper")
ARCHIVE_ROOT = ROOT / "_NON_GEO_ARCHIVE"
WORKSPACE_ROOT = ROOT / "_GEO_RING_CLOUD_WORKSPACE"
CODE_ROOT = ROOT / "third_report" / "code" / "geo_ring_cloud_stage1"

CANDIDATES = [
    ROOT / "forth",
    ROOT / "second_report",
    ROOT / "third_report" / "code" / "epic_ceres",
    ROOT / "third_report" / "paper_notion_manager",
    ROOT / "third_report" / "outputs" / "epic_ceres",
    ROOT / "third_report" / "outputs" / "epic_ceres_fullmonth_v2",
    ROOT / "third_report" / "outputs" / "epic_ceres_ppt_assets",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def count_tree(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    if path.is_file():
        return 1, path.stat().st_size
    count = 0
    total = 0
    for fp in path.rglob("*"):
        if not fp.is_file():
            continue
        count += 1
        try:
            total += fp.stat().st_size
        except OSError:
            pass
    return count, total


def code_reference_count(path: Path) -> int:
    if not CODE_ROOT.exists():
        return 0
    tokens = {
        str(path),
        str(path.relative_to(ROOT)).replace("/", "\\"),
        str(path.relative_to(ROOT)).replace("\\", "/"),
        path.name,
    }
    count = 0
    for fp in CODE_ROOT.rglob("*"):
        if fp.suffix.lower() not in {".py", ".ps1", ".json", ".yaml", ".yml", ".md", ".txt"}:
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(token and token in text for token in tokens):
            count += 1
    return count


def archive_target(path: Path) -> Path:
    return ARCHIVE_ROOT / path.relative_to(ROOT)


def build_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in CANDIDATES:
        exists = path.exists()
        files, size = count_tree(path)
        refs = code_reference_count(path) if exists else 0
        target = archive_target(path)
        rows.append(
            {
                "generated_at": utc_now(),
                "source_path": str(path),
                "archive_path": str(target),
                "exists_now": "yes" if exists else "no",
                "file_count": files,
                "size_bytes": size,
                "geo_code_reference_count": refs,
                "action": "move" if exists and refs == 0 else "skip",
                "status": "planned" if exists and refs == 0 else "not_movable",
            }
        )
    return rows


def write_manifest(rows: list[dict[str, object]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "generated_at",
        "source_path",
        "archive_path",
        "exists_now",
        "file_count",
        "size_bytes",
        "geo_code_reference_count",
        "action",
        "status",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def execute(rows: list[dict[str, object]]) -> None:
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    for row in rows:
        if row["action"] != "move":
            continue
        src = Path(str(row["source_path"]))
        dst = Path(str(row["archive_path"]))
        if not src.exists():
            row["status"] = "missing_at_execute"
            continue
        resolved_archive = ARCHIVE_ROOT.resolve()
        resolved_dst_parent = dst.parent.resolve() if dst.parent.exists() else dst.parent
        if resolved_archive not in [resolved_dst_parent, *resolved_dst_parent.parents]:
            raise RuntimeError(f"refusing archive target outside archive root: {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            row["status"] = "target_exists_skip"
            continue
        shutil.move(str(src), str(dst))
        row["status"] = "moved"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="move zero-reference candidates")
    args = parser.parse_args()

    rows = build_rows()
    workspace_manifest = WORKSPACE_ROOT / "archive_manifest_dry_run.csv"
    archive_manifest = ARCHIVE_ROOT / "_move_manifest.csv"
    write_manifest(rows, workspace_manifest)
    if args.execute:
        execute(rows)
        write_manifest(rows, archive_manifest)
        log = ARCHIVE_ROOT / "_move_log.md"
        moved = [r for r in rows if r["status"] == "moved"]
        log.write_text(
            "# Non-GEO Archive Move Log\n\n"
            f"- Generated at: `{utc_now()}`\n"
            f"- Moved entries: {len(moved)}\n"
            f"- Manifest: `{archive_manifest}`\n",
            encoding="utf-8",
        )
    print(f"manifest={workspace_manifest}")
    if args.execute:
        print(f"archive_manifest={archive_manifest}")


if __name__ == "__main__":
    main()

