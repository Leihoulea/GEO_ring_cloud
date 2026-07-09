from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from path_config import BASE_STAGE_ROOT, RUNS_ROOT

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SELECTION_CSV = RUNS_ROOT / "epic_202403_target_selection" / "recommended_epic_georing_validation_targets.csv"
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


def time_tag_from_nearest_hour(value: str) -> str:
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):", value)
    if not match:
        raise ValueError(f"bad nearest_hour: {value}")
    y, m, d, h = match.groups()
    return f"{y}{m}{d}_{h}00"


def read_selection(args: argparse.Namespace) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(args.selection_csv).open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if args.role and row.get("selection_role") != args.role:
                continue
            if args.candidate_class and row.get("candidate_class") != args.candidate_class:
                continue
            rows.append(row)
    if args.start_index > 0:
        rows = rows[args.start_index :]
    if args.max_samples > 0:
        rows = rows[: args.max_samples]
    return rows


def base_result_row(row: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    time_tag = args.time_tag_override or time_tag_from_nearest_hour(row["nearest_hour"])
    return {
        "selection_role": row.get("selection_role", ""),
        "candidate_class": row.get("candidate_class", ""),
        "epic_time": row.get("epic_time", ""),
        "target_time": row.get("nearest_hour", ""),
        "time_tag": time_tag,
        "epic_l2": row.get("epic_file", ""),
        "status": "",
        "returncode": "",
        "elapsed_sec": "",
        "report": "",
    }


def run_one(row: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    base = base_result_row(row, args)
    time_tag = base["time_tag"]
    out_root = Path(args.runs_root) / time_tag
    report = out_root / "reports" / f"single_sample_run_report_{time_tag}.md"
    fused_bundle = out_root / "fused_best_source" / f"fused_geo_ring_cloud_{time_tag}.npz"
    semantic_report = out_root / f"epic_l2_cloud_mask_semantic_sensitivity_{time_tag}" / "epic_cloud_mask_semantic_sensitivity_report.md"
    legacy_done = fused_bundle.exists() and semantic_report.exists()
    if args.skip_existing and (report.exists() or legacy_done):
        base.update({"status": "SKIPPED_EXISTING", "returncode": 0, "elapsed_sec": 0.0, "report": str(report if report.exists() else semantic_report)})
        return base

    cmd = [
        "conda",
        "run",
        "-n",
        args.conda_env,
        "python",
        "-B",
        str(SCRIPT_DIR / "run_epic_georing_single_sample.py"),
        "--target-time",
        row["nearest_hour"],
        "--time-tag",
        time_tag,
        "--epic-l2",
        row["epic_file"],
        "--output-root",
        str(out_root),
        "--base-stage-root",
        args.base_stage_root,
    ]
    start = time.time()
    proc = subprocess.run(cmd, cwd=str(SCRIPT_DIR), text=True)
    elapsed = time.time() - start
    base.update({"status": "OK" if proc.returncode == 0 else "FAILED", "returncode": proc.returncode, "elapsed_sec": elapsed, "report": str(report)})
    return base


def build_report(out_dir: Path, rows: list[dict[str, Any]], args: argparse.Namespace) -> Path:
    ok = sum(1 for r in rows if r["status"] in {"OK", "SKIPPED_EXISTING"})
    failed = sum(1 for r in rows if r["status"] == "FAILED")
    lines = [
        "# EPIC GEO-ring Sample Batch Report",
        "",
        f"- Created UTC: `{utc_now()}`",
        f"- Selection CSV: `{args.selection_csv}`",
        f"- Role filter: `{args.role}`",
        f"- Candidate class filter: `{args.candidate_class}`",
        f"- Samples attempted: `{len(rows)}`",
        f"- OK/skipped: `{ok}`",
        f"- Failed: `{failed}`",
        "",
        "## Runs",
        "",
    ]
    for r in rows:
        lines.append(f"- {r['time_tag']} {r['status']} target=`{r['target_time']}` epic=`{r['epic_time']}` report=`{r['report']}`")
    report = out_dir / "epic_georing_sample_batch_report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Batch-run parameterized GEO-ring + EPIC 08c validation samples from an 08d selection CSV.")
    p.add_argument("--selection-csv", default=str(DEFAULT_SELECTION_CSV))
    p.add_argument("--runs-root", default=str(DEFAULT_RUNS_ROOT))
    p.add_argument("--base-stage-root", default=str(BASE_STAGE_ROOT))
    p.add_argument("--role", default="", help="Optional selection_role filter, e.g. east_asia_priority")
    p.add_argument("--candidate-class", default="", help="Optional candidate_class filter")
    p.add_argument("--max-samples", type=int, default=0)
    p.add_argument("--start-index", type=int, default=0)
    p.add_argument("--conda-env", default="pytorch")
    p.add_argument("--skip-existing", action="store_true")
    p.add_argument("--time-tag-override", default="", help="Only for single-row debugging; normally leave blank")
    p.add_argument("--batch-out-dir", default=str(RUNS_ROOT / "epic_202403_batch_runs"))
    return p


def main() -> int:
    args = build_parser().parse_args()
    out_dir = Path(args.batch_out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    selected = read_selection(args)
    rows: list[dict[str, Any]] = []
    status_csv = out_dir / "epic_georing_sample_batch_status.csv"
    fields = ["selection_role", "candidate_class", "epic_time", "target_time", "time_tag", "epic_l2", "status", "returncode", "elapsed_sec", "report"]
    for row in selected:
        pending = base_result_row(row, args)
        pending.update({"status": "RUNNING", "returncode": "", "elapsed_sec": "", "report": str(Path(args.runs_root) / pending["time_tag"])})
        rows.append(pending)
        write_csv(status_csv, rows, fields)
        result = run_one(row, args)
        rows[-1] = result
        write_csv(status_csv, rows, fields)
        if result["status"] == "FAILED":
            print(f"FAILED {result['time_tag']}; continuing with next sample")
    report = build_report(out_dir, rows, args)
    failed = [r for r in rows if r["status"] == "FAILED"]
    print(f"batch {'PASS_WITH_FAILURES' if failed else 'PASS'}: report={report}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
