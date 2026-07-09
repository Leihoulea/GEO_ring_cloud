from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from path_config import BASE_STAGE_ROOT, RUNS_ROOT

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_BASE_STAGE_ROOT = BASE_STAGE_ROOT
DEFAULT_RUNS_ROOT = RUNS_ROOT


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_step(name: str, command: list[str], env: dict[str, str], cwd: Path, log_dir: Path, continue_on_error: bool = False) -> dict[str, Any]:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{name}.log"
    start = time.time()
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        log.write(f"# {name}\n")
        log.write(f"# start_utc={utc_now()}\n")
        log.write(f"# command={' '.join(command)}\n\n")
        proc = subprocess.run(command, cwd=str(cwd), env=env, stdout=log, stderr=subprocess.STDOUT, text=True)
        rc = proc.returncode
        log.write(f"\n# end_utc={utc_now()}\n")
        log.write(f"# returncode={rc}\n")
    elapsed = time.time() - start
    status = "OK" if rc == 0 else "FAILED"
    if rc != 0 and not continue_on_error:
        return {"step": name, "status": status, "returncode": rc, "elapsed_sec": elapsed, "log_path": str(log_path), "stop_reason": "blocking_step_failed"}
    return {"step": name, "status": status, "returncode": rc, "elapsed_sec": elapsed, "log_path": str(log_path), "stop_reason": ""}


def build_report(out_root: Path, args: argparse.Namespace, rows: list[dict[str, Any]]) -> Path:
    report_dir = out_root / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    final_status = "PASS" if all(r["status"] == "OK" for r in rows) else "FAIL"
    lines = [
        "# EPIC GEO-ring Single Sample Run Report",
        "",
        f"- Status: **{final_status}**",
        f"- Target time: `{args.target_time}`",
        f"- Time tag: `{args.time_tag}`",
        f"- EPIC L2: `{args.epic_l2}`",
        f"- Output root: `{out_root}`",
        f"- Base stage root: `{args.base_stage_root}`",
        "",
        "## Steps",
        "",
    ]
    for row in rows:
        lines.append(f"- {row['step']}: {row['status']} returncode={row['returncode']} elapsed={float(row['elapsed_sec']):.1f}s log=`{row['log_path']}`")
    lines.extend(
        [
            "",
            "## Key Outputs",
            "",
            f"- Standardized native: `{out_root / 'standardized_native'}`",
            f"- Reprojected grid: `{out_root / 'reprojected_grid'}`",
            f"- Fused best source: `{out_root / 'fused_best_source'}`",
            f"- 08c semantic sensitivity: `{out_root / f'epic_l2_cloud_mask_semantic_sensitivity_{args.time_tag}'}`",
        ]
    )
    report = report_dir / f"single_sample_run_report_{args.time_tag}.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def run_pipeline(args: argparse.Namespace) -> tuple[str, Path]:
    out_root = Path(args.output_root) if args.output_root else Path(args.runs_root) / args.time_tag
    out_root.mkdir(parents=True, exist_ok=True)
    log_dir = out_root / "logs" / "pipeline"
    status_csv = out_root / "pipeline_run_status.csv"
    env = os.environ.copy()
    env.update(
        {
            "GEO_RING_STAGE_ROOT": str(out_root),
            "GEO_RING_BASE_STAGE_ROOT": str(args.base_stage_root),
            "GEO_RING_RUNS_ROOT": str(args.runs_root),
            "GEO_RING_TARGET_TIME": args.target_time,
            "GEO_RING_TIME_TAG": args.time_tag,
        }
    )

    python_exe = args.python_exe
    if args.use_conda:
        py_prefix = ["conda", "run", "-n", args.conda_env, "python", "-B"]
    else:
        py_prefix = [python_exe, "-B"]

    steps: list[tuple[str, list[str], bool]] = [
        ("02_build_standardized_cloud_native", py_prefix + [str(SCRIPT_DIR / "02_build_standardized_cloud_native.py"), "--target-time", args.target_time], False),
        ("03_validate_standardized_cloud_native", py_prefix + [str(SCRIPT_DIR / "03_validate_standardized_cloud_native.py")], False),
        ("05_reproject_cloud_to_grid", py_prefix + [str(SCRIPT_DIR / "05_reproject_cloud_to_grid.py")], False),
        ("06_fuse_best_source", py_prefix + [str(SCRIPT_DIR / "06_fuse_best_source.py")], False),
        (
            "08c_epic_cloud_mask_semantic_sensitivity",
            py_prefix
            + [
                str(SCRIPT_DIR / "08c_epic_cloud_mask_semantic_sensitivity.py"),
                "--time-run-root",
                str(out_root),
                "--epic-l2",
                args.epic_l2,
                "--target-time",
                args.target_time,
                "--time-tag",
                args.time_tag,
            ],
            False,
        ),
    ]

    rows: list[dict[str, Any]] = []
    for name, cmd, continue_on_error in steps:
        row = run_step(name, cmd, env, SCRIPT_DIR, log_dir, continue_on_error=continue_on_error)
        rows.append(row)
        write_csv(status_csv, rows, ["step", "status", "returncode", "elapsed_sec", "log_path", "stop_reason"])
        if row["status"] != "OK" and not continue_on_error:
            break

    manifest = {
        "created_utc": utc_now(),
        "target_time": args.target_time,
        "time_tag": args.time_tag,
        "epic_l2": args.epic_l2,
        "output_root": str(out_root),
        "base_stage_root": str(args.base_stage_root),
        "steps": rows,
    }
    (out_root / "single_sample_run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    report = build_report(out_root, args, rows)
    final_status = "PASS" if rows and all(r["status"] == "OK" for r in rows) else "FAIL"
    return final_status, report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run parameterized GEO-ring 02/03/05/06 + EPIC 08c semantic comparison for one sample.")
    p.add_argument("--target-time", required=True, help="Nearest GEO target time, e.g. 2024-03-15T04:00:00Z")
    p.add_argument("--time-tag", required=True, help="Run tag, e.g. 20240315_0400")
    p.add_argument("--epic-l2", required=True, help="EPIC L2 cloud NetCDF path")
    p.add_argument("--output-root", default="", help="Output root. Default: runs-root/time-tag")
    p.add_argument("--runs-root", default=str(DEFAULT_RUNS_ROOT), help="Parent directory for time-run outputs")
    p.add_argument("--base-stage-root", default=str(DEFAULT_BASE_STAGE_ROOT), help="Base stage root containing time_index")
    p.add_argument("--use-conda", action="store_true", default=True)
    p.add_argument("--conda-env", default="pytorch")
    p.add_argument("--python-exe", default=sys.executable)
    return p


def main() -> int:
    args = build_parser().parse_args()
    status, report = run_pipeline(args)
    print(f"single-sample {status}: report={report}")
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
