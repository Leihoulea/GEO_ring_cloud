from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from geo_ring_cloud_source_registry import REGISTRY_VERSION, SOURCE_PROFILES
from geo_ring_cloud_lineage import code_commit, write_manifest
from path_config import BASE_STAGE_ROOT, CLAAS3_ROOT, PROJECT_ROOT, RUNS_ROOT


SCRIPT_DIR = Path(__file__).resolve().parent
REQUIRED_PROFILE_STEPS = (
    "02_build_standardized_cloud_native",
    "03_validate_standardized_cloud_native",
    "03_5_semantic_validation_patch",
    "05_reproject_cloud_to_grid",
    "06_fuse_best_source",
    "08c_epic_cloud_mask_semantic_sensitivity",
)
REQUIRED_PROFILE_ARTIFACTS = (
    Path("standardized_native"),
    Path("reprojected_grid"),
    Path("fused_best_source") / "fused_cloud_mask.npz",
    Path("fused_best_source") / "fused_cloud_top_height_km.npz",
    Path("fused_best_source") / "source_map_cloud_mask.npz",
    Path("fused_best_source") / "source_map_cloud_top_height_km.npz",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def ensure_claas_catalog(base_stage_root: Path, target_time: str) -> None:
    index_path = base_stage_root / "time_index" / "core_time_index.csv"
    if not index_path.exists():
        raise RuntimeError(f"Stage 01 index missing: {index_path}")
    index = pd.read_csv(index_path)
    rows = index[(index["nominal_time"] == target_time) & (index["satellite_group"] == "CLAAS3-0deg")]
    if rows.empty or str(rows.iloc[0].get("status", "")) not in {"PASS", "PARTIAL"}:
        raise RuntimeError("CLAAS-3 Stage 01 catalog row is missing; rerun 01_build_core_time_index.py first")


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS time_runs (
            run_id TEXT NOT NULL,
            parent_run_id TEXT NOT NULL,
            source_profile TEXT NOT NULL,
            target_utc TEXT NOT NULL,
            status TEXT NOT NULL,
            manifest_path TEXT NOT NULL,
            updated_utc TEXT NOT NULL,
            PRIMARY KEY (run_id, source_profile)
        )
        """
    )
    return conn


def upsert_run(conn: sqlite3.Connection, row: dict[str, str]) -> None:
    conn.execute(
        """
        INSERT INTO time_runs(run_id,parent_run_id,source_profile,target_utc,status,manifest_path,updated_utc)
        VALUES(:run_id,:parent_run_id,:source_profile,:target_utc,:status,:manifest_path,:updated_utc)
        ON CONFLICT(run_id,source_profile) DO UPDATE SET
            parent_run_id=excluded.parent_run_id,
            target_utc=excluded.target_utc,
            status=excluded.status,
            manifest_path=excluded.manifest_path,
            updated_utc=excluded.updated_utc
        """,
        row,
    )
    conn.commit()


def profile_artifacts_complete(profile_root: Path, manifest: dict[str, Any]) -> bool:
    if manifest.get("artifact_state") == "PRUNED_AFTER_ACCEPTANCE":
        return False
    if not all((profile_root / relative).exists() for relative in REQUIRED_PROFILE_ARTIFACTS):
        return False
    time_tag = str(manifest.get("time_tag", ""))
    return bool(time_tag) and (profile_root / f"epic_l2_cloud_mask_semantic_sensitivity_{time_tag}").is_dir()


def run_profile(args: argparse.Namespace, profile: str, profile_root: Path) -> dict[str, Any]:
    profile_root.mkdir(parents=True, exist_ok=True)
    manifest_path = profile_root / "single_sample_run_manifest.json"
    if args.resume and manifest_path.exists():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            step_status = {row["step"]: row.get("status") for row in previous.get("steps", [])}
            reusable = (
                previous.get("source_profile") == profile
                and previous.get("target_time") == args.target_time
                and previous.get("epic_l2") == str(args.epic_l2)
                and all(step_status.get(name) == "OK" for name in REQUIRED_PROFILE_STEPS)
                and profile_artifacts_complete(profile_root, previous)
            )
            if reusable:
                return {
                    "source_profile": profile,
                    "status": "PASS",
                    "returncode": 0,
                    "profile_root": str(profile_root),
                    "manifest_path": str(manifest_path),
                    "reused_complete_profile": True,
                }
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            pass
    command = [
        sys.executable,
        str(SCRIPT_DIR / "run_epic_georing_single_sample.py"),
        "--target-time", args.target_time,
        "--time-tag", args.time_tag,
        "--epic-l2", str(args.epic_l2),
        "--output-root", str(profile_root),
        "--runs-root", str(args.runs_root),
        "--base-stage-root", str(args.base_stage_root),
        "--run-id", args.run_id,
        "--parent-run-id", args.run_id,
        "--source-profile", profile,
        "--claas3-root", str(args.claas3_root),
        "--conda-env", args.conda_env,
    ]
    completed = subprocess.run(command, cwd=str(SCRIPT_DIR), check=False)
    status = "PASS" if completed.returncode == 0 and manifest_path.exists() else "FAIL"
    return {
        "source_profile": profile,
        "status": status,
        "returncode": completed.returncode,
        "profile_root": str(profile_root),
        "manifest_path": str(manifest_path),
        "reused_complete_profile": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run matched operational-baseline and CLAAS-3-candidate profiles")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--target-time", required=True)
    parser.add_argument("--time-tag", required=True)
    parser.add_argument("--epic-l2", type=Path, required=True)
    parser.add_argument("--runs-root", type=Path, default=RUNS_ROOT)
    parser.add_argument("--base-stage-root", type=Path, default=BASE_STAGE_ROOT)
    parser.add_argument("--claas3-root", type=Path, default=CLAAS3_ROOT)
    parser.add_argument("--conda-env", default="pytorch")
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    ensure_claas_catalog(args.base_stage_root, args.target_time)
    parent_root = args.runs_root / args.run_id
    parent_root.mkdir(parents=True, exist_ok=True)
    db_path = args.runs_root / "time_runs.sqlite"
    results: list[dict[str, Any]] = []
    with init_db(db_path) as conn:
        for profile in SOURCE_PROFILES:
            profile_root = parent_root / profile
            upsert_run(conn, {
                "run_id": args.run_id,
                "parent_run_id": args.run_id,
                "source_profile": profile,
                "target_utc": args.target_time,
                "status": "RUNNING",
                "manifest_path": str(profile_root / "single_sample_run_manifest.json"),
                "updated_utc": utc_now(),
            })
            result = run_profile(args, profile, profile_root)
            results.append(result)
            upsert_run(conn, {
                "run_id": args.run_id,
                "parent_run_id": args.run_id,
                "source_profile": profile,
                "target_utc": args.target_time,
                "status": result["status"],
                "manifest_path": result["manifest_path"],
                "updated_utc": utc_now(),
            })
    common_inputs = {
        "target_time": args.target_time,
        "time_tag": args.time_tag,
        "epic_l2": str(args.epic_l2),
        "base_stage_root": str(args.base_stage_root),
        "non_meteosat_inputs_policy": "identical Stage 01 catalog rows for both profiles",
    }
    payload = {
        "project_id": "geo_ring_cloud",
        "canonical_stage_id": "time_run_matrix",
        "run_id": args.run_id,
        "parent_run_id": args.run_id,
        "source_profile": "profile_matrix",
        "generated_utc": utc_now(),
        "timestamp_utc": utc_now(),
        "generating_script": str(Path(__file__)),
        "input_paths": [str(args.epic_l2), str(args.base_stage_root / "time_index" / "core_time_index.csv"), str(args.claas3_root)],
        "output_paths": [row["profile_root"] for row in results],
        "parameter_summary": common_inputs,
        "code_commit": code_commit(PROJECT_ROOT),
        "source_registry_version": REGISTRY_VERSION,
        "product_versions": {"CLAAS3": "405"},
        "common_inputs": common_inputs,
        "profile_runs": results,
        "status": "PASS" if all(row["status"] == "PASS" for row in results) else "FAIL",
        "sqlite_index": str(db_path),
    }
    manifest_path = parent_root / "geo_ring_cloud_time_run_matrix_manifest.json"
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_manifest(
        parent_root / "stage_08_profile_pair_sample_manifest.json",
        canonical_stage_id="stage_08",
        run_id=args.run_id,
        source_profile="profile_pair",
        generating_script=Path(__file__),
        input_paths=[args.epic_l2, *(Path(row["manifest_path"]) for row in results)],
        output_paths=[Path(row["profile_root"]) for row in results],
        parameters={"target_time": args.target_time, "time_tag": args.time_tag, "common_domain_required": True},
        project_root=PROJECT_ROOT,
        extra={
            "registry_version": REGISTRY_VERSION,
            "product_versions": {"CLAAS3": "405"},
            "profiles": {row["source_profile"]: row["profile_root"] for row in results},
            "epic_role": "relative diagnostic for cloud mask and effective cloud height only",
        },
    )
    print(f"time-run matrix {payload['status']}: {manifest_path}")
    return 0 if payload["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
