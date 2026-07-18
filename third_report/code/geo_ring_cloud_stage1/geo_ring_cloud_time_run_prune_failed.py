from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from geo_ring_cloud.lineage import write_manifest
from geo_ring_cloud.paths import PROJECT_ROOT, RUNS_ROOT


COMPONENT_ROLE = "time_run_pruning"
RUN_ID_PATTERN = re.compile(r"^\d{8}_\d{4}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def directory_stats(path: Path) -> tuple[int, int, dict[str, dict[str, int]]]:
    files = [item for item in path.rglob("*") if item.is_file()]
    extensions: dict[str, dict[str, int]] = {}
    for item in files:
        suffix = item.suffix.lower() or "<none>"
        row = extensions.setdefault(suffix, {"file_count": 0, "size_bytes": 0})
        row["file_count"] += 1
        row["size_bytes"] += item.stat().st_size
    return len(files), sum(item.stat().st_size for item in files), extensions


def validate_failed_run(runs_root: Path, run_id: str) -> tuple[Path, Path, dict[str, Any]]:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise RuntimeError(f"invalid legacy run id: {run_id}")
    root = runs_root.resolve()
    run_root = (runs_root / run_id).resolve()
    if run_root != root / run_id or run_root.parent != root:
        raise RuntimeError(f"unsafe failed-run target: {run_root}")
    manifest_path = run_root / "single_sample_run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("time_tag") != run_id:
        raise RuntimeError(f"manifest time tag mismatch: {manifest_path}")
    steps = manifest.get("steps", [])
    failed = [row for row in steps if row.get("status") != "OK"]
    if not failed:
        raise RuntimeError(f"run is not failed and cannot be pruned with this tool: {run_id}")
    if (run_root / "fused_best_source").exists():
        raise RuntimeError(f"failed run unexpectedly contains fused evidence: {run_root}")
    return run_root, manifest_path, manifest


def preserve_non_npz(run_root: Path, archive_root: Path) -> tuple[int, int]:
    copied_count = 0
    copied_bytes = 0
    for source in sorted(item for item in run_root.rglob("*") if item.is_file() and item.suffix.lower() != ".npz"):
        destination = archive_root / source.relative_to(run_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied_count += 1
        copied_bytes += source.stat().st_size
    return copied_count, copied_bytes


def build_report(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# GEO-ring Cloud 失败 time run 精简报告",
        "",
        f"- 执行时间（UTC）：{utc_now()}",
        f"- 模式：`{payload['mode']}`",
        f"- Run：`{payload['run_id']}`",
        f"- 失败步骤：`{payload['failed_step']}`",
        f"- 原始占用：{payload['size_before_bytes'] / 1024 ** 3:.3f} GiB",
        f"- NPZ 占用：{payload['npz_bytes'] / 1024 ** 3:.3f} GiB",
        f"- 保留的非 NPZ 文件：{payload['preserved_file_count']} 个，{payload['preserved_bytes'] / 1024 ** 2:.2f} MiB",
        f"- 实际磁盘空间增量：{payload['actual_gain_bytes'] / 1024 ** 3:.3f} GiB",
        "- Quicklook 保留策略：全部 PNG 按原相对路径复制到 `preserved_failed_run`。",
        "- 科学状态：该运行在 Stage 05 失败，未生成 `fused_best_source`，不属于有效科学样本。",
        "",
        f"- 删除路径：`{payload['deleted_path']}`",
        f"- 保留路径：`{payload['archive_path']}`",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive non-NPZ evidence and prune one failed legacy time run")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runs-root", type=Path, default=RUNS_ROOT)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    args.runs_root = args.runs_root.resolve()
    args.output_dir = args.output_dir.resolve()
    run_root, manifest_path, run_manifest = validate_failed_run(args.runs_root, args.run_id)
    if args.output_dir == run_root or run_root in args.output_dir.parents:
        raise RuntimeError("pruning output directory cannot be inside the failed run")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    file_count, size_before, extensions = directory_stats(run_root)
    npz_stats = extensions.get(".npz", {"file_count": 0, "size_bytes": 0})
    failed_rows = [row for row in run_manifest["steps"] if row.get("status") != "OK"]
    archive_root = args.output_dir / "preserved_failed_run" / args.run_id
    journal_path = args.output_dir / "geo_ring_cloud_time_run_failed_pruning_journal.json"
    report_path = args.output_dir / "geo_ring_cloud_time_run_failed_pruning_report.md"
    final_manifest_path = args.output_dir / "geo_ring_cloud_time_run_failed_pruning_manifest.json"
    free_before = shutil.disk_usage(args.runs_root).free
    journal: dict[str, Any] = {
        "project_id": "geo_ring_cloud",
        "component_role": "time_run_pruning",
        "status": "PLANNED",
        "timestamp_utc": utc_now(),
        "run_id": args.run_id,
        "run_root": str(run_root),
        "source_manifest": str(manifest_path),
        "file_count": file_count,
        "size_before_bytes": size_before,
        "extension_summary": extensions,
        "failed_steps": failed_rows,
    }
    write_json(journal_path, journal)
    preserved_count = 0
    preserved_bytes = 0
    if args.execute:
        journal["status"] = "ARCHIVING"
        write_json(journal_path, journal)
        preserved_count, preserved_bytes = preserve_non_npz(run_root, archive_root)
        journal.update({"status": "DELETING", "preserved_file_count": preserved_count, "preserved_bytes": preserved_bytes})
        write_json(journal_path, journal)
        shutil.rmtree(run_root)
        if run_root.exists():
            raise RuntimeError(f"failed run still exists after pruning: {run_root}")
        journal.update({"status": "COMPLETE", "completed_utc": utc_now()})
        write_json(journal_path, journal)
    free_after = shutil.disk_usage(args.runs_root).free
    report_payload = {
        "mode": "execute" if args.execute else "dry-run",
        "run_id": args.run_id,
        "failed_step": failed_rows[0].get("step", "unknown"),
        "size_before_bytes": size_before,
        "npz_bytes": int(npz_stats["size_bytes"]),
        "preserved_file_count": preserved_count if args.execute else file_count - int(npz_stats["file_count"]),
        "preserved_bytes": preserved_bytes if args.execute else size_before - int(npz_stats["size_bytes"]),
        "actual_gain_bytes": free_after - free_before,
        "deleted_path": str(run_root),
        "archive_path": str(archive_root),
    }
    build_report(report_path, report_payload)
    write_manifest(
        final_manifest_path,
        canonical_stage_id="",
        component_role=COMPONENT_ROLE,
        generating_script=Path(__file__),
        input_paths=[manifest_path],
        output_paths=[archive_root, journal_path, report_path],
        parameters={"execute": args.execute, "run_id": args.run_id, "preserve_non_npz": True},
        project_root=PROJECT_ROOT,
        run_id=args.run_id,
        source_profile="legacy_operational",
        extra={
            "status": "COMPLETE" if args.execute else "DRY_RUN",
            "artifact_state": "FAILED_RUN_PRUNED" if args.execute else "PLANNED",
            **report_payload,
            "extension_summary": extensions,
            "failed_steps": failed_rows,
        },
    )
    print(f"failed-run pruning {'complete' if args.execute else 'dry-run complete'}: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
