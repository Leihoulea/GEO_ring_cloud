from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from geo_ring_cloud_lineage import write_manifest
from path_config import PROJECT_ROOT, RUNS_ROOT


PROFILES = ("operational_baseline", "claas3_candidate")
PRUNABLE_SECTIONS = ("standardized_native", "reprojected_grid")
REQUIRED_FUSED = (
    "fused_cloud_mask.npz",
    "fused_cloud_top_height_km.npz",
    "source_map_cloud_mask.npz",
    "source_map_cloud_top_height_km.npz",
    "stage_06_claas3_fusion_manifest.json",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def assert_safe_target(runs_root: Path, run_id: str, profile: str, section: str) -> Path:
    root = runs_root.resolve()
    target = (runs_root / run_id / profile / section).resolve()
    expected = root / run_id / profile / section
    if target != expected or target.parent.name != profile or target.parent.parent.name != run_id:
        raise RuntimeError(f"unsafe pruning target: {target}")
    if section not in PRUNABLE_SECTIONS or profile not in PROFILES:
        raise RuntimeError(f"target is outside pruning allowlist: {target}")
    return target


def directory_stats(path: Path) -> tuple[int, int]:
    files = [item for item in path.rglob("*") if item.is_file()]
    return len(files), sum(item.stat().st_size for item in files)


def validate_run(runs_root: Path, run_id: str) -> tuple[Path, dict[str, Any]]:
    matrix_path = runs_root / run_id / "geo_ring_cloud_time_run_matrix_manifest.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    if matrix.get("run_id") != run_id or matrix.get("status") != "PASS":
        raise RuntimeError(f"matrix is not an accepted PASS run: {matrix_path}")
    profiles = {row.get("source_profile"): row for row in matrix.get("profile_runs", [])}
    if set(profiles) != set(PROFILES) or any(profiles[name].get("status") != "PASS" for name in PROFILES):
        raise RuntimeError(f"profile matrix is incomplete: {matrix_path}")
    for profile in PROFILES:
        profile_root = runs_root / run_id / profile
        profile_manifest = profile_root / "single_sample_run_manifest.json"
        payload = json.loads(profile_manifest.read_text(encoding="utf-8"))
        if payload.get("source_profile") != profile:
            raise RuntimeError(f"profile manifest mismatch: {profile_manifest}")
        for name in REQUIRED_FUSED:
            required = profile_root / "fused_best_source" / name
            if not required.exists():
                raise RuntimeError(f"required fused evidence is missing: {required}")
    return matrix_path, matrix


def update_run_lineage(matrix_path: Path, matrix: dict[str, Any], rows: list[dict[str, Any]], pruning_manifest: Path) -> None:
    timestamp = utc_now()
    run_rows = [row for row in rows if row["run_id"] == matrix["run_id"]]
    for profile in PROFILES:
        profile_root = matrix_path.parent / profile
        profile_manifest_path = profile_root / "single_sample_run_manifest.json"
        profile_manifest = json.loads(profile_manifest_path.read_text(encoding="utf-8"))
        profile_manifest.update({
            "artifact_state": "PRUNED_AFTER_ACCEPTANCE",
            "pruned_utc": timestamp,
            "pruning_manifest_path": str(pruning_manifest),
            "pruned_paths": [row["path"] for row in run_rows if row["profile"] == profile],
        })
        write_json(profile_manifest_path, profile_manifest)
    matrix.update({
        "artifact_state": "PRUNED_AFTER_ACCEPTANCE",
        "pruned_utc": timestamp,
        "pruning_manifest_path": str(pruning_manifest),
        "pruned_paths": [row["path"] for row in run_rows],
    })
    write_json(matrix_path, matrix)


def build_report(path: Path, rows: list[dict[str, Any]], executed: bool, free_before: int, free_after: int) -> None:
    total = sum(int(row["bytes_before"]) for row in rows)
    lines = [
        "# GEO-ring Cloud 双轨验收中间产物精简报告",
        "",
        f"- 执行时间（UTC）：{utc_now()}",
        f"- 模式：`{'execute' if executed else 'dry-run'}`",
        f"- 目录数：{len(rows)}",
        f"- 清单字节数：{total}（{total / 1024 ** 3:.3f} GiB）",
    ]
    if executed:
        lines.append(f"- 磁盘可用空间变化：{free_before / 1024 ** 3:.3f} -> {free_after / 1024 ** 3:.3f} GiB")
    lines.extend([
        "- 保留：全部 `fused_best_source`、source map、运行 manifest、日志以及 Stage 07p/09d/10 结果。",
        "- 科学状态：原双轨验收结论仍有效；这些 profile 已标记 `PRUNED_AFTER_ACCEPTANCE`，不得作为完整 Stage 02-06 resume 命中。",
        "",
        "| Run | Profile | Section | GiB | 状态 | 路径 |",
        "| --- | --- | --- | ---: | --- | --- |",
    ])
    for row in rows:
        lines.append(
            f"| {row['run_id']} | {row['profile']} | {row['section']} | "
            f"{int(row['bytes_before']) / 1024 ** 3:.3f} | {row['status']} | `{row['path']}` |"
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Prune accepted profile intermediates while preserving fused evidence")
    parser.add_argument("--run-id", action="append", required=True)
    parser.add_argument("--runs-root", type=Path, default=RUNS_ROOT)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--require-path", type=Path, action="append", default=[])
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    args.runs_root = args.runs_root.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for required in args.require_path:
        if not required.exists():
            raise RuntimeError(f"acceptance evidence missing: {required}")

    matrices: list[tuple[Path, dict[str, Any]]] = []
    rows: list[dict[str, Any]] = []
    for run_id in args.run_id:
        matrices.append(validate_run(args.runs_root, run_id))
        for profile in PROFILES:
            for section in PRUNABLE_SECTIONS:
                target = assert_safe_target(args.runs_root, run_id, profile, section)
                if not target.is_dir():
                    raise RuntimeError(f"pruning target is missing: {target}")
                file_count, byte_count = directory_stats(target)
                rows.append({
                    "run_id": run_id,
                    "profile": profile,
                    "section": section,
                    "path": str(target),
                    "file_count": file_count,
                    "bytes_before": byte_count,
                    "status": "PLANNED",
                })

    journal = args.output_dir / "geo_ring_cloud_time_run_pruning_journal.json"
    report = args.output_dir / "geo_ring_cloud_time_run_pruning_report.md"
    manifest = args.output_dir / "geo_ring_cloud_time_run_pruning_manifest.json"
    journal_payload = {"status": "PLANNED", "timestamp_utc": utc_now(), "rows": rows}
    write_json(journal, journal_payload)
    free_before = shutil.disk_usage(args.runs_root).free
    if args.execute:
        journal_payload["status"] = "RUNNING"
        write_json(journal, journal_payload)
        for row in rows:
            target = Path(row["path"])
            shutil.rmtree(target)
            if target.exists():
                raise RuntimeError(f"failed to remove pruning target: {target}")
            row["status"] = "DELETED"
            write_json(journal, journal_payload)
        for matrix_path, matrix_payload in matrices:
            update_run_lineage(matrix_path, matrix_payload, rows, manifest)
        journal_payload["status"] = "COMPLETE"
        journal_payload["completed_utc"] = utc_now()
        write_json(journal, journal_payload)
    free_after = shutil.disk_usage(args.runs_root).free
    build_report(report, rows, args.execute, free_before, free_after)
    write_manifest(
        manifest,
        canonical_stage_id="time_run_matrix",
        generating_script=Path(__file__),
        input_paths=[path for path, _ in matrices] + list(args.require_path),
        output_paths=[journal, report],
        parameters={"execute": args.execute, "run_ids": args.run_id, "sections": PRUNABLE_SECTIONS},
        project_root=PROJECT_ROOT,
        run_id="claas3_epic_202403_five_sample_pruning",
        source_profile="profile_matrix",
        extra={
            "component_role": "time_run_pruning",
            "status": "COMPLETE" if args.execute else "DRY_RUN",
            "artifact_state": "PRUNED_AFTER_ACCEPTANCE" if args.execute else "PLANNED",
            "planned_bytes": sum(int(row["bytes_before"]) for row in rows),
            "free_space_before_bytes": free_before,
            "free_space_after_bytes": free_after,
            "deletions": rows,
            "retention_policy": "preserve fused_best_source, manifests, logs, and stage_07p/stage_09d/stage_10 evidence",
        },
    )
    print(f"pruning {'complete' if args.execute else 'dry-run complete'}: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
