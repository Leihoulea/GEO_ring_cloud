from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

import path_config
from geo_ring_cloud_epic_pair_diagnostics import epic_time_from_name, find_prefusion_layer, load_npz, read_csv, time_delta_minutes, write_csv
from geo_ring_cloud_lineage import code_commit, write_manifest
from geo_ring_cloud_time_run_matrix import profile_artifacts_complete


PROJECT_ID = "geo_ring_cloud"
COMPONENT_ROLE = "experiment_runner"
RELATED_STAGE_IDS = ("stage_09d", "stage_10")
SCRIPT_DIR = Path(__file__).resolve().parent
PILOT_TAGS = {
    "20240306_1300",
    "20240313_1300",
    "20240317_1200",
    "20240324_1200",
    "20240329_1300",
}
PROFILES = ("operational_baseline", "claas3_candidate")
REGRESSION_ASSETS = (
    "fused_cloud_mask.npz",
    "fused_cloud_top_height_km.npz",
    "source_map_cloud_mask.npz",
    "source_map_cloud_top_height_km.npz",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def run(command: list[str], cwd: Path = SCRIPT_DIR) -> None:
    completed = subprocess.run(command, cwd=str(cwd), check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"command failed ({completed.returncode}): {' '.join(command)}")


def prepare_parallel_sqlite(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=60.0) as conn:
        conn.execute("PRAGMA busy_timeout=60000")
        mode = str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
        if mode != "wal":
            raise RuntimeError(f"could not enable WAL for parallel run index: {path} ({mode})")


def read_samples(path: Path, phase: str, runs_root: Path) -> list[dict[str, str]]:
    rows = read_csv(path)
    required = {"sample_id", "nearest_georing_time_utc", "epic_file", "time_diff_min", "stage_run_dir"}
    if not rows or not required.issubset(rows[0]):
        raise RuntimeError(f"sample manifest contract mismatch: {path}")
    for row in rows:
        row["selection_origin"] = "stage09d_53_sample_manifest"
    selected = [row for row in rows if phase == "march" or row["sample_id"] in PILOT_TAGS]
    if phase == "pilot":
        missing = PILOT_TAGS - {row["sample_id"] for row in selected}
        for tag in sorted(missing):
            legacy_matrix = runs_root / f"claas3_epic_{tag}" / "geo_ring_cloud_time_run_matrix_manifest.json"
            if not legacy_matrix.exists():
                raise RuntimeError(f"locked pilot sample is absent from the 53-sample manifest and has no legacy matrix: {tag}")
            payload = json.loads(legacy_matrix.read_text(encoding="utf-8"))
            common = payload["common_inputs"]
            epic_path = Path(common["epic_l2"])
            epic_time = epic_time_from_name(epic_path)
            selected.append({
                "sample_id": tag,
                "epic_time_utc": epic_time,
                "nearest_georing_time_utc": common["target_time"],
                "time_diff_min": str(time_delta_minutes(epic_time, common["target_time"])),
                "candidate_group": "LOCKED_PILOT_FALLBACK",
                "dominant_source": "Meteosat-0deg",
                "stage_run_dir": "",
                "epic_file": str(epic_path),
                "selection_origin": str(legacy_matrix),
            })
        if {row["sample_id"] for row in selected} != PILOT_TAGS:
            raise RuntimeError("could not resolve all five locked pilot samples")
    if phase == "march" and len(selected) != 53:
        raise RuntimeError(f"March phase requires exactly 53 samples, found {len(selected)}")
    return sorted(selected, key=lambda row: row["sample_id"])


def matrix_manifest(runs_root: Path, run_id: str) -> Path:
    return runs_root / run_id / "geo_ring_cloud_time_run_matrix_manifest.json"


def resolve_matrix_run_id(runs_root: Path, time_tag: str, checkpoint: dict[str, Any] | None = None) -> str:
    """Reuse legacy Stage0910 runs, but give new runs a component-owned name."""
    if checkpoint and checkpoint.get("run_id"):
        return str(checkpoint["run_id"])
    legacy_run_id = f"claas3_stage0910_{time_tag}"
    if matrix_manifest(runs_root, legacy_run_id).exists():
        return legacy_run_id
    return f"geo_ring_cloud_profile_pair_{time_tag}"


def matrix_pass(path: Path) -> bool:
    if not path.exists():
        return False
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("status") != "PASS" or payload.get("artifact_state") == "PRUNED_AFTER_ACCEPTANCE":
        return False
    roots = {row["source_profile"]: Path(row["profile_root"]) for row in payload.get("profile_runs", [])}
    return set(roots) == set(PROFILES) and all((roots[profile] / "reprojected_grid" / "reprojected_variable_inventory.csv").exists() for profile in PROFILES)


def matrix_profile_roots(path: Path) -> dict[str, Path]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    roots = {row["source_profile"]: Path(row["profile_root"]) for row in payload["profile_runs"]}
    if set(roots) != set(PROFILES):
        raise RuntimeError(f"matrix profile roots are incomplete: {path}")
    return roots


def reusable_operational_baseline(row: dict[str, str]) -> Path | None:
    if not row.get("stage_run_dir"):
        return None
    root = Path(row["stage_run_dir"])
    manifest_path = root / "single_sample_run_manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if manifest.get("target_time") != row["nearest_georing_time_utc"] or manifest.get("time_tag") != row["sample_id"]:
        return None
    return root if profile_artifacts_complete(root, manifest) else None


def run_matrix(args: argparse.Namespace, row: dict[str, str], run_id: str) -> Path:
    path = matrix_manifest(args.runs_root, run_id)
    if matrix_pass(path):
        return path
    command = [
        sys.executable,
        str(SCRIPT_DIR / "geo_ring_cloud_time_run_matrix.py"),
        "--run-id", run_id,
        "--target-time", row["nearest_georing_time_utc"],
        "--time-tag", row["sample_id"],
        "--epic-l2", row["epic_file"],
        "--runs-root", str(args.runs_root),
        "--base-stage-root", str(args.base_stage_root),
        "--claas3-root", str(args.claas3_root),
        "--conda-env", args.conda_env,
        "--resume",
    ]
    reusable_root = reusable_operational_baseline(row) if args.reuse_operational_baseline else None
    if reusable_root:
        command.extend(["--reuse-operational-baseline-root", str(reusable_root)])
    run(command)
    if not matrix_pass(path):
        raise RuntimeError(f"matrix did not satisfy PASS/full-assets contract: {path}")
    return path


def assert_arrays_equal(left_path: Path, right_path: Path) -> None:
    left, left_valid, _ = load_npz(left_path)
    right, right_valid, _ = load_npz(right_path)
    if left.shape != right.shape or not np.array_equal(left_valid, right_valid):
        raise RuntimeError(f"valid-mask regression mismatch: {left_path} vs {right_path}")
    if not np.array_equal(left, right, equal_nan=True):
        mismatch = int(np.count_nonzero(~np.isclose(left, right, equal_nan=True)))
        raise RuntimeError(f"scientific-array regression mismatch ({mismatch} cells): {left_path} vs {right_path}")


def baseline_regression(row: dict[str, str], baseline_root: Path) -> dict[str, Any]:
    if not row.get("stage_run_dir"):
        return {
            "status": "NOT_AVAILABLE",
            "baseline_mode": "fresh_profile_run",
            "reason": "locked pilot fallback has no independent legacy baseline",
        }
    legacy = Path(row["stage_run_dir"]) / "fused_best_source"
    if not legacy.exists():
        return {"status": "NOT_AVAILABLE", "reason": f"legacy fused directory is absent: {legacy}"}
    fresh = baseline_root / "fused_best_source"
    if fresh.resolve() == legacy.resolve():
        return {
            "status": "REUSED_ACCEPTED_LEGACY",
            "baseline_mode": "reused_verified_legacy",
            "baseline_source_root": str(baseline_root.resolve()),
            "reason": "pilot bitwise regression passed; March baseline is the registered legacy asset",
        }
    checked: list[str] = []
    for name in REGRESSION_ASSETS:
        assert_arrays_equal(legacy / name, fresh / name)
        checked.append(name)
    return {
        "status": "PASS",
        "baseline_mode": "fresh_profile_run",
        "baseline_source_root": str(baseline_root.resolve()),
        "checked_assets": checked,
    }


def unchanged_prefusion_regression(baseline_root: Path, candidate_root: Path) -> dict[str, Any]:
    checked: list[str] = []
    specs = (
        ("GOES-16", "ACMF", "cloud_mask"),
        ("GOES-16", "ACHAF", "cloud_top_height_km"),
        ("Meteosat-IODC", "CLM", "cloud_mask"),
        ("Meteosat-IODC", "CTH", "cloud_top_height_km"),
    )
    for source, product, variable in specs:
        left = find_prefusion_layer(baseline_root, source, product, variable)
        right = find_prefusion_layer(candidate_root, source, product, variable)
        assert_arrays_equal(left, right)
        checked.append(f"{source}/{product}/{variable}")
    return {"status": "PASS", "checked_assets": checked}


def diagnostic_acceptance(stage09_dir: Path, stage10_dir: Path, require_replacement_active: bool = True) -> dict[str, Any]:
    stage09 = read_csv(stage09_dir / "stage_09d_claas3_aligned_per_time_source_pair.csv")
    fused09 = read_csv(stage09_dir / "stage_09d_claas3_aligned_per_time_fused.csv")
    stage10 = read_csv(stage10_dir / "stage_10_claas3_aligned_per_time_source_pair.csv")
    requirements = {
        "stage09_operational_claas_pair": any(row.get("source_A") == "Meteosat-0deg" and row.get("source_B") == "CLAAS3-0deg" for row in stage09),
        "stage10_operational_claas_pair": any(row.get("source_A") == "Meteosat-0deg" and row.get("source_B") == "CLAAS3-0deg" for row in stage10),
        "replacement_active_5_to_7": any(row.get("domain") == "replacement_active" and int(float(row.get("common_n", 0))) > 0 for row in fused09),
        "unchanged_control": any(row.get("domain") == "unchanged_control" and int(float(row.get("common_n", 0))) > 0 for row in fused09),
        "nearest_and_box7": all(any(row.get("aggregation") == aggregation for row in fused09) for aggregation in ("nearest", "box_7x7")),
        "fixed_epic_morphology": all((stage09_dir / "stage_09d_claas3_aligned_compact_diagnostic_manifest.json").exists() for _ in (0,)),
    }
    required_keys = set(requirements) if require_replacement_active else {"nearest_and_box7", "fixed_epic_morphology"}
    if not all(requirements[key] for key in required_keys):
        raise RuntimeError(f"diagnostic acceptance failed: {requirements}")
    return {
        "status": "PASS",
        "replacement_active_status": "present" if requirements["replacement_active_5_to_7"] else "unresolved_sparse_no_overlap",
        "operational_claas_source_pair_status": "present" if requirements["stage09_operational_claas_pair"] and requirements["stage10_operational_claas_pair"] else "unresolved_sparse_no_overlap",
        **requirements,
    }


def pilot_reproduction(time_tag: str, stage09_dir: Path, stage10_dir: Path, runs_root: Path, tolerance: float = 1e-12) -> dict[str, Any]:
    old09_path = runs_root / "stage_09d_claas3_epic_5sample_202403" / "stage_09d_claas3_epic_common_domain_samples.csv"
    old10_path = runs_root / "stage_10_claas3_epic_5sample_202403" / "stage_10_claas3_epic_relative_height_samples.csv"
    if not old09_path.exists() or not old10_path.exists():
        return {"status": "NOT_AVAILABLE", "reason": "legacy five-sample pilot tables are missing"}
    old09 = [row for row in read_csv(old09_path) if row.get("time_tag") == time_tag and row.get("domain") == "global_common"]
    new09 = [row for row in read_csv(stage09_dir / "stage_09d_claas3_aligned_per_time_fused.csv") if row.get("stratum") == "all" and row.get("domain") == "global_common" and row.get("policy") in {"A_inclusive_binary", "B_high_confidence_only"} and row.get("aggregation") in {"nearest", "box_7x7"}]
    old09_index = {(row["policy"], row["aggregation"]): row for row in old09}
    for row in new09:
        old = old09_index.get((row["policy"], row["aggregation"]))
        if old is None:
            raise RuntimeError(f"legacy Stage 09 pilot row is missing for {time_tag}/{row['policy']}/{row['aggregation']}")
        checks = {
            "A_f1": "baseline_f1",
            "B_f1": "candidate_f1",
            "B_minus_A_f1": "f1_delta_candidate_minus_baseline",
        }
        for new_key, old_key in checks.items():
            if abs(float(row[new_key]) - float(old[old_key])) > tolerance:
                raise RuntimeError(f"Stage 09 pilot reproduction mismatch: {time_tag}/{row['policy']}/{row['aggregation']}/{new_key}")
    old10 = [row for row in read_csv(old10_path) if row.get("time_tag") == time_tag and row.get("domain") == "global_common"]
    new10 = [row for row in read_csv(stage10_dir / "stage_10_claas3_aligned_per_time_fused.csv") if row.get("stratum") == "all" and row.get("domain") == "D0_common_valid_cth" and row.get("policy") == "A_inclusive_binary" and row.get("aggregation") == "box_7x7"]
    old10_index = {row["epic_band"]: row for row in old10}
    for row in new10:
        old = old10_index.get(row["band"])
        if old is None:
            raise RuntimeError(f"legacy Stage 10 pilot row is missing for {time_tag}/{row['band']}")
        checks = {"A_mae_km": "baseline_mae_epic_relative_km", "B_mae_km": "candidate_mae_epic_relative_km", "B_minus_A_mae_km": "mae_delta_candidate_minus_baseline_km"}
        for new_key, old_key in checks.items():
            if abs(float(row[new_key]) - float(old[old_key])) > tolerance:
                raise RuntimeError(f"Stage 10 pilot reproduction mismatch: {time_tag}/{row['band']}/{new_key}")
    return {"status": "PASS", "stage09_rows_checked": len(new09), "stage10_rows_checked": len(new10), "absolute_tolerance": tolerance}


def prune(args: argparse.Namespace, run_id: str, stage09_dir: Path, stage10_dir: Path) -> None:
    output = args.experiment_root / "pruning" / run_id
    run([
        sys.executable,
        str(SCRIPT_DIR / "geo_ring_cloud_time_run_prune_intermediates.py"),
        "--run-id", run_id,
        "--runs-root", str(args.runs_root),
        "--output-dir", str(output),
        "--require-path", str(stage09_dir / "stage_09d_claas3_aligned_compact_diagnostic_manifest.json"),
        "--require-path", str(stage10_dir / "stage_10_claas3_aligned_compact_diagnostic_manifest.json"),
        "--profile", "claas3_candidate",
        "--execute",
    ])


def write_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def emit_progress(event: str, **payload: Any) -> None:
    print(json.dumps({"event": event, "timestamp_utc": utc_now(), **payload}, ensure_ascii=False), flush=True)


def write_batch_status(args: argparse.Namespace, samples: list[dict[str, str]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for sample in samples:
        path = args.experiment_root / "checkpoints" / f"{sample['sample_id']}.json"
        if not path.exists():
            continue
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    counts = {status: sum(row.get("status") == status for row in rows) for status in ("PASS", "RUNNING", "FAIL")}
    terminal = len(rows) == len(samples) and counts["RUNNING"] == 0
    failures = [
        {
            "sample_index": row.get("sample_index", ""),
            "time_tag": row.get("time_tag", ""),
            "error": row.get("error", ""),
            "completed_utc": row.get("completed_utc", ""),
        }
        for row in rows
        if row.get("status") == "FAIL"
    ]
    status = {
        "experiment_id": args.experiment_id,
        "updated_utc": utc_now(),
        "expected": len(samples),
        "pass": counts["PASS"],
        "running": counts["RUNNING"],
        "fail": counts["FAIL"],
        "pending": len(samples) - len(rows),
        "terminal": terminal,
        "overall_status": "PASS" if terminal and not failures else "COMPLETE_WITH_FAILURES" if terminal else "PARTIAL",
        "failed_time_tags": [row["time_tag"] for row in failures],
    }
    status_path = args.experiment_root / "geo_ring_cloud_profile_pair_batch_status.json"
    status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(args.experiment_root / "geo_ring_cloud_profile_pair_failure_summary.csv", failures)
    return status


def summarize(args: argparse.Namespace) -> None:
    run([
        sys.executable,
        str(SCRIPT_DIR / "stage_09d_claas3_epic_profile_pair_evaluation.py"),
        "--input-root", str(args.stage09_root / "samples"),
        "--output-dir", str(args.stage09_root),
        "--phase", args.phase,
        "--max-primary-time-delta-min", str(args.max_primary_time_delta_min),
    ])
    run([
        sys.executable,
        str(SCRIPT_DIR / "stage_10_claas3_epic_relative_height_evaluation.py"),
        "--input-root", str(args.stage10_root / "samples"),
        "--output-dir", str(args.stage10_root),
        "--phase", args.phase,
        "--max-primary-time-delta-min", str(args.max_primary_time_delta_min),
    ])


def case_atlas(args: argparse.Namespace) -> Path:
    candidates: list[dict[str, Any]] = []
    for path in sorted((args.stage09_root / "samples").rglob("stage_09d_claas3_aligned_per_time_fused.csv")):
        for row in read_csv(path):
            if row.get("policy") == "A_inclusive_binary" and row.get("aggregation") == "box_7x7" and row.get("domain") == "replacement_active" and row.get("stratum") in {"all", "scene_type:broken_cloud", "boundary_class:near_boundary_1cell", "abs_lat_bin:70-80", "abs_lat_bin:>=80"}:
                try:
                    delta = float(row["B_minus_A_f1"])
                    common_n = int(float(row["common_n"]))
                except (KeyError, TypeError, ValueError):
                    continue
                if math.isfinite(delta) and common_n > 0:
                    candidates.append({"variable": "CMA", "time_tag": row["time_tag"], "state": row["stratum"], "delta": delta, "common_n": common_n})
    for path in sorted((args.stage10_root / "samples").rglob("stage_10_claas3_aligned_per_time_fused.csv")):
        for row in read_csv(path):
            if row.get("band") == "A_band" and row.get("policy") == "A_inclusive_binary" and row.get("aggregation") == "box_7x7" and row.get("domain") in {"D1_both_cloud", "D6_boundary_or_broken_cloud", "D7_high_cloud"} and row.get("stratum") == "all":
                try:
                    delta = float(row["B_minus_A_mae_km"])
                    common_n = int(float(row["common_n"]))
                except (KeyError, TypeError, ValueError):
                    continue
                if math.isfinite(delta) and common_n > 0:
                    candidates.append({"variable": "CTX", "time_tag": row["time_tag"], "state": row["domain"], "delta": delta, "common_n": common_n})
    atlas: list[dict[str, Any]] = []
    for variable in ("CMA", "CTX"):
        values = [row for row in candidates if row["variable"] == variable and row["common_n"] > 0]
        if not values:
            continue
        better = min(values, key=lambda row: row["delta"]) if variable == "CTX" else max(values, key=lambda row: row["delta"])
        worse = max(values, key=lambda row: row["delta"]) if variable == "CTX" else min(values, key=lambda row: row["delta"])
        atlas.extend([{**better, "case_role": "largest_improvement"}, {**worse, "case_role": "largest_degradation"}])
    for state_key in ("abs_lat_bin:70-80", "abs_lat_bin:>=80", "scene_type:broken_cloud", "boundary_class:near_boundary_1cell"):
        values = [row for row in candidates if row["state"] == state_key]
        if values:
            atlas.append({**max(values, key=lambda row: row["common_n"]), "case_role": state_key})
    path = args.experiment_root / "geo_ring_cloud_profile_pair_case_atlas.csv"
    write_csv(path, atlas)
    return path


def process_sample(args: argparse.Namespace, index: int, row: dict[str, str]) -> dict[str, Any]:
    started = time.monotonic()
    time_tag = row["sample_id"]
    checkpoint_path = args.experiment_root / "checkpoints" / f"{time_tag}.json"
    if checkpoint_path.exists():
        existing = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    else:
        existing = {}
    run_id = resolve_matrix_run_id(args.runs_root, time_tag, existing)
    if existing.get("status") == "PASS":
        baseline = existing.get("baseline_regression", {})
        if baseline.get("status") == "REUSED_ACCEPTED_LEGACY" and "baseline_mode" not in baseline:
            baseline["baseline_mode"] = "reused_verified_legacy"
            if row.get("stage_run_dir"):
                baseline["baseline_source_root"] = str(Path(row["stage_run_dir"]).resolve())
            existing["baseline_regression"] = baseline
            write_checkpoint(checkpoint_path, existing)
        if args.phase == "pilot" and "legacy_pilot_reproduction" not in existing:
            existing["legacy_pilot_reproduction"] = pilot_reproduction(time_tag, Path(existing["stage09_dir"]), Path(existing["stage10_dir"]), args.runs_root)
            write_checkpoint(checkpoint_path, existing)
        return existing
    pilot_checkpoint_path = args.runs_root / "experiments" / "claas3_stage0910_pilot_202403" / "checkpoints" / f"{time_tag}.json"
    if args.phase == "march" and time_tag in PILOT_TAGS and not existing and pilot_checkpoint_path.exists():
        pilot = json.loads(pilot_checkpoint_path.read_text(encoding="utf-8"))
        if pilot.get("status") == "PASS":
            matrix = Path(pilot["matrix_manifest"])
            stage09_dir = args.stage09_root / "samples" / time_tag
            stage10_dir = args.stage10_root / "samples" / time_tag
            shutil.copytree(Path(pilot["stage09_dir"]), stage09_dir, dirs_exist_ok=True)
            shutil.copytree(Path(pilot["stage10_dir"]), stage10_dir, dirs_exist_ok=True)
            acceptance = diagnostic_acceptance(stage09_dir, stage10_dir, require_replacement_active=False)
            reused = {
                "sample_index": index,
                "time_tag": time_tag,
                "run_id": run_id,
                "worker_concurrency": args.max_workers,
                "status": "PASS",
                "baseline_regression": pilot.get("baseline_regression", {}),
                "unchanged_prefusion_regression": pilot.get("unchanged_prefusion_regression", {}),
                "diagnostic_acceptance": acceptance,
                "matrix_manifest": str(matrix),
                "stage07_dir": pilot.get("stage07_dir", ""),
                "stage09_dir": str(stage09_dir),
                "stage10_dir": str(stage10_dir),
                "diagnostic_cache_state": "NOT_REQUIRED_REUSED_ACCEPTED_PILOT",
                "resume_action": "reused_accepted_pilot_diagnostics",
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "completed_utc": utc_now(),
            }
            write_checkpoint(checkpoint_path, reused)
            if args.prune_after_acceptance:
                prune(args, run_id, stage09_dir, stage10_dir)
                reused["artifact_state"] = "PRUNED_AFTER_ACCEPTANCE"
                write_checkpoint(checkpoint_path, reused)
            return reused
    recovered_matrix = matrix_manifest(args.runs_root, run_id)
    recovered_stage09 = args.stage09_root / "samples" / time_tag
    recovered_stage10 = args.stage10_root / "samples" / time_tag
    recovered_stage09_rows = read_csv(recovered_stage09 / "stage_09d_claas3_aligned_per_time_source_pair.csv")
    recovered_stage10_rows = read_csv(recovered_stage10 / "stage_10_claas3_aligned_per_time_source_pair.csv")
    recovered_outputs = (
        recovered_matrix.exists()
        and (recovered_stage09 / "stage_09d_claas3_aligned_compact_diagnostic_manifest.json").exists()
        and (recovered_stage10 / "stage_10_claas3_aligned_compact_diagnostic_manifest.json").exists()
        and (not recovered_stage09_rows or "status" in recovered_stage09_rows[0])
        and (not recovered_stage10_rows or "status" in recovered_stage10_rows[0])
    )
    if recovered_outputs and existing.get("diagnostic_acceptance", {}).get("status") != "PASS":
        existing["diagnostic_acceptance"] = diagnostic_acceptance(recovered_stage09, recovered_stage10, require_replacement_active=args.phase == "pilot")
        existing.update({"matrix_manifest": str(recovered_matrix), "stage09_dir": str(recovered_stage09), "stage10_dir": str(recovered_stage10)})
        write_checkpoint(checkpoint_path, existing)
    retryable_acceptance = existing.get("diagnostic_acceptance", {}).get("status") == "PASS" and all(Path(existing.get(key, "")).exists() for key in ("matrix_manifest", "stage09_dir", "stage10_dir"))
    if retryable_acceptance and args.phase == "march" and args.prune_after_acceptance:
        prune(args, run_id, Path(existing["stage09_dir"]), Path(existing["stage10_dir"]))
        existing.pop("error", None)
        existing.update({"status": "PASS", "artifact_state": "PRUNED_AFTER_ACCEPTANCE", "resume_action": "prune_only_after_science_acceptance", "completed_utc": utc_now()})
        write_checkpoint(checkpoint_path, existing)
        return existing
    checkpoint: dict[str, Any] = {
        "sample_index": index,
        "time_tag": time_tag,
        "run_id": run_id,
        "worker_concurrency": args.max_workers,
        "started_utc": utc_now(),
        "status": "RUNNING",
    }
    write_checkpoint(checkpoint_path, checkpoint)
    emit_progress("sample_started", sample_index=index, time_tag=time_tag, run_id=run_id)
    try:
        matrix = run_matrix(args, row, run_id)
        run_root = matrix.parent
        profile_roots = matrix_profile_roots(matrix)
        checkpoint["baseline_regression"] = baseline_regression(row, profile_roots["operational_baseline"])
        checkpoint["unchanged_prefusion_regression"] = unchanged_prefusion_regression(profile_roots["operational_baseline"], profile_roots["claas3_candidate"])
        stage07_dir = run_root / "stage_07p_claas3_aligned"
        run([sys.executable, str(SCRIPT_DIR / "stage_07p_claas3_profile_pair_evaluation.py"), "--matrix-manifest", str(matrix), "--output-dir", str(stage07_dir)])
        stage09_dir = args.stage09_root / "samples" / time_tag
        stage10_dir = args.stage10_root / "samples" / time_tag
        diagnostic_cache = args.experiment_root / "diagnostic_cache" / f"{time_tag}_cloud_mask_samples.npz"
        run([sys.executable, str(SCRIPT_DIR / "stage_09d_claas3_epic_profile_pair_evaluation.py"), "--matrix-manifest", str(matrix), "--output-dir", str(stage09_dir), "--phase", args.phase, "--max-primary-time-delta-min", str(args.max_primary_time_delta_min), "--diagnostic-cache", str(diagnostic_cache)])
        run([sys.executable, str(SCRIPT_DIR / "stage_10_claas3_epic_relative_height_evaluation.py"), "--matrix-manifest", str(matrix), "--output-dir", str(stage10_dir), "--phase", args.phase, "--max-primary-time-delta-min", str(args.max_primary_time_delta_min), "--diagnostic-cache", str(diagnostic_cache)])
        checkpoint["diagnostic_acceptance"] = diagnostic_acceptance(stage09_dir, stage10_dir, require_replacement_active=args.phase == "pilot")
        if args.phase == "pilot":
            checkpoint["legacy_pilot_reproduction"] = pilot_reproduction(time_tag, stage09_dir, stage10_dir, args.runs_root)
        checkpoint.update({
            "matrix_manifest": str(matrix),
            "stage07_dir": str(stage07_dir),
            "stage09_dir": str(stage09_dir),
            "stage10_dir": str(stage10_dir),
            "diagnostic_cache": str(diagnostic_cache),
            "status": "PASS",
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "completed_utc": utc_now(),
        })
        write_checkpoint(checkpoint_path, checkpoint)
        if args.phase == "march" and args.prune_after_acceptance:
            prune(args, run_id, stage09_dir, stage10_dir)
            checkpoint["artifact_state"] = "PRUNED_AFTER_ACCEPTANCE"
            write_checkpoint(checkpoint_path, checkpoint)
        if not args.retain_diagnostic_cache and diagnostic_cache.exists():
            diagnostic_cache.unlink()
            checkpoint["diagnostic_cache_state"] = "REMOVED_AFTER_ACCEPTANCE"
            write_checkpoint(checkpoint_path, checkpoint)
    except Exception as exc:
        checkpoint.update({"status": "FAIL", "error": str(exc), "elapsed_seconds": round(time.monotonic() - started, 3), "completed_utc": utc_now()})
        write_checkpoint(checkpoint_path, checkpoint)
        emit_progress("sample_failed", sample_index=index, time_tag=time_tag, error=str(exc))
        if args.failure_policy == "fail-fast":
            raise
    return checkpoint


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the aligned Stage 09d/10 CLAAS-3 experiment matrix")
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--sample-manifest", type=Path, required=True)
    parser.add_argument("--phase", choices=("pilot", "march"), required=True)
    parser.add_argument("--max-primary-time-delta-min", type=float, default=10.0)
    parser.add_argument("--scene-reference", choices=("epic",), default="epic")
    parser.add_argument("--retain-diagnostic-cache", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--prune-after-acceptance", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--reuse-operational-baseline", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--runs-root", type=Path, default=path_config.RUNS_ROOT)
    parser.add_argument("--base-stage-root", type=Path, default=path_config.BASE_STAGE_ROOT)
    parser.add_argument("--claas3-root", type=Path, default=path_config.CLAAS3_ROOT)
    parser.add_argument("--conda-env", default="pytorch")
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-workers", type=int, default=1)
    parser.add_argument("--failure-policy", choices=("continue", "fail-fast"), default="continue")
    args = parser.parse_args()
    args.runs_root = args.runs_root.resolve()
    args.experiment_root = args.runs_root / "experiments" / args.experiment_id
    args.stage09_root = args.runs_root / "stage_09d_claas3_aligned_202403" / args.experiment_id
    args.stage10_root = args.runs_root / "stage_10_claas3_aligned_202403" / args.experiment_id
    samples = read_samples(args.sample_manifest, args.phase, args.runs_root)
    selected = samples[args.start_index:args.start_index + args.limit if args.limit else None]
    if args.max_workers < 1 or args.max_workers > 4:
        parser.error("--max-workers must be between 1 and 4")
    if args.max_workers > 1:
        prepare_parallel_sqlite(args.runs_root / "time_runs.sqlite")
    args.experiment_root.mkdir(parents=True, exist_ok=True)
    manifest_copy = args.experiment_root / "geo_ring_cloud_profile_pair_sample_manifest.csv"
    write_csv(manifest_copy, samples)
    indexed = list(enumerate(selected, start=args.start_index))
    checkpoints: list[dict[str, Any]] = []
    emit_progress("batch_started", selected_count=len(indexed), max_workers=args.max_workers, failure_policy=args.failure_policy)
    write_batch_status(args, samples)

    def record_result(result: dict[str, Any]) -> None:
        checkpoints.append(result)
        status = write_batch_status(args, samples)
        emit_progress(
            "sample_finished",
            sample_index=result.get("sample_index"),
            time_tag=result.get("time_tag"),
            status=result.get("status"),
            pass_count=status["pass"],
            fail_count=status["fail"],
            running_count=status["running"],
            pending_count=status["pending"],
        )

    if args.max_workers == 1:
        for index, row in indexed:
            record_result(process_sample(args, index, row))
    else:
        with ThreadPoolExecutor(max_workers=args.max_workers, thread_name_prefix="grc-profile-pair") as executor:
            futures = {executor.submit(process_sample, args, index, row): (index, row) for index, row in indexed}
            for future in as_completed(futures):
                try:
                    record_result(future.result())
                except Exception as exc:
                    if args.failure_policy == "fail-fast":
                        for pending in futures:
                            pending.cancel()
                        raise
                    index, row = futures[future]
                    checkpoint = {
                        "sample_index": index,
                        "time_tag": row["sample_id"],
                        "run_id": resolve_matrix_run_id(args.runs_root, row["sample_id"]),
                        "status": "FAIL",
                        "error": f"unexpected worker exception: {exc}",
                        "completed_utc": utc_now(),
                    }
                    write_checkpoint(args.experiment_root / "checkpoints" / f"{row['sample_id']}.json", checkpoint)
                    emit_progress("sample_failed", sample_index=index, time_tag=row["sample_id"], error=str(exc))
                    record_result(checkpoint)
        checkpoints.sort(key=lambda row: int(row["sample_index"]))
    checkpoint_paths = [args.experiment_root / "checkpoints" / f"{row['sample_id']}.json" for row in samples]
    all_checkpoint_rows = [json.loads(path.read_text(encoding="utf-8")) for path in checkpoint_paths if path.exists()]
    terminal = len(all_checkpoint_rows) == len(samples) and all(row.get("status") in {"PASS", "FAIL"} for row in all_checkpoint_rows)
    failed_rows = [row for row in all_checkpoint_rows if row.get("status") == "FAIL"]
    complete = terminal and not failed_rows
    if terminal:
        summarize(args)
        atlas_path = case_atlas(args)
    else:
        atlas_path = None
    experiment_manifest = args.experiment_root / "geo_ring_cloud_profile_pair_experiment_manifest.json"
    write_manifest(
        experiment_manifest,
        canonical_stage_id="",
        component_role=COMPONENT_ROLE,
        related_stage_ids=RELATED_STAGE_IDS,
        generating_script=Path(__file__),
        input_paths=[args.sample_manifest],
        output_paths=[args.stage09_root, args.stage10_root, *( [atlas_path] if atlas_path else [])],
        parameters={"experiment_id": args.experiment_id, "phase": args.phase, "max_primary_time_delta_min": args.max_primary_time_delta_min, "scene_reference": args.scene_reference, "retain_diagnostic_cache": args.retain_diagnostic_cache, "prune_after_acceptance": args.prune_after_acceptance, "reuse_operational_baseline": args.reuse_operational_baseline, "max_workers": args.max_workers, "failure_policy": args.failure_policy},
        project_root=path_config.PROJECT_ROOT,
        run_id=args.experiment_id,
        source_profile="profile_matrix",
        extra={"status": "PASS" if complete else "COMPLETE_WITH_FAILURES" if terminal else "PARTIAL", "sample_count_expected": len(samples), "sample_count_this_invocation": len(selected), "failed_sample_count": len(failed_rows), "failed_time_tags": [row.get("time_tag") for row in failed_rows], "checkpoint_paths": [str(path) for path in checkpoint_paths], "code_commit_at_start": code_commit(path_config.PROJECT_ROOT)},
    )
    final_status = write_batch_status(args, samples)
    emit_progress("batch_finished", **final_status)
    return 2 if terminal and failed_rows else 0


if __name__ == "__main__":
    raise SystemExit(main())
