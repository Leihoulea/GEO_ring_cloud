"""Run the CNN prototype from an existing, navigation-verified GEO source set.

The source GEO runs are read-only; this script creates only a new experiment
root containing a frozen 77-comparison manifest and downstream CNN products.
"""
from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("EPIC_GEO_SR_DATA_CONFIG", "data_corrected77.yaml")

from common import CODE_ROOT, ROOT, env_path, load_config, sha256, utc_now, write_csv, write_json  # noqa: E402
from nav_provenance import validate_pairing  # noqa: E402

VERSION = "sr_proto_202403_v03_corrected77"
MARKER = ROOT / ".corrected77_root"


def check_root(source_runs: Path) -> None:
    if ROOT == CODE_ROOT or ROOT == source_runs or ROOT in source_runs.parents or source_runs in ROOT.parents:
        raise RuntimeError("Choose a separate NEW output root; never put outputs inside source GEO runs")
    if ROOT.exists() and any(ROOT.iterdir()) and not MARKER.is_file():
        raise RuntimeError(f"Refusing nonempty unmarked output root: {ROOT}")
    ROOT.mkdir(parents=True, exist_ok=True)
    if not MARKER.exists():
        MARKER.write_text(VERSION + "\n", encoding="utf-8")
    if MARKER.read_text(encoding="utf-8").strip() != VERSION:
        raise RuntimeError(f"Unexpected output-root marker: {MARKER}")


def run(command: list[str], log: Path) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"\n# {utc_now()} {' '.join(command)}\n")
        handle.flush()
        result = subprocess.run(command, cwd=CODE_ROOT, env=os.environ.copy(), stdout=handle, stderr=subprocess.STDOUT)
        handle.write(f"# {utc_now()} exit={result.returncode}\n")
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}); inspect {log}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-python", type=Path, default=Path(sys.executable))
    parser.add_argument("--preflight-only", action="store_true", help="Freeze/verify sources but do not process images")
    parser.add_argument("--max-stages", type=int, default=0, help="Run first N non-CNN stages as a smoke test")
    args = parser.parse_args()
    cfg = load_config("data.yaml")
    if cfg["dataset_version"] != VERSION:
        raise RuntimeError("Set EPIC_GEO_SR_DATA_CONFIG=data_corrected77.yaml")
    source_runs = env_path(cfg["geo_runs_root_env"])
    epic_root = env_path(cfg["epic_root_env"])
    source_manifest = source_runs.parent / "00_control" / "stage_10_epic_80_sample_manifest.csv"
    if not source_manifest.is_file() or not args.model_python.is_file():
        raise RuntimeError(f"Missing source manifest or model Python: {source_manifest}, {args.model_python}")
    check_root(source_runs)
    with source_manifest.open(encoding="utf-8-sig", newline="") as handle:
        candidates = list(csv.DictReader(handle))
    if len(candidates) != cfg["source_manifest_expected_count"]:
        raise RuntimeError(f"Expected {cfg['source_manifest_expected_count']} source comparisons, got {len(candidates)}")
    if len({row["sample_id"] for row in candidates}) != len(candidates):
        raise RuntimeError("Duplicate source comparison ID")
    accepted: list[dict[str, str]] = []
    excluded: list[str] = []
    verified_geo: set[str] = set()
    for row in candidates:
        comparison = row["sample_id"]
        geo_scene = row["geo_sample_id"]
        run_dir = source_runs / geo_scene
        if not (run_dir / "fused_best_source/fused_cloud_mask.npz").is_file():
            excluded.append(comparison)
            continue
        if geo_scene not in verified_geo:
            validate_pairing(run_dir, geo_scene, cfg["required_meteosat_navigation"])
            if not (run_dir / "reprojected_grid/target_grid_definition.json").is_file():
                raise RuntimeError(f"Missing target grid: {run_dir}")
            verified_geo.add(geo_scene)
        epic = epic_root / Path(row["epic_file"]).name
        if not epic.is_file():
            raise RuntimeError(f"Missing EPIC L2 source: {epic}")
        accepted.append({**row, "dataset_version": VERSION})
    if set(excluded) != set(cfg["expected_excluded_comparison_ids"]):
        raise RuntimeError(f"Unexpected excluded comparisons: {excluded}")
    if len(accepted) != cfg["expected_scene_count"] or len(verified_geo) != cfg["expected_unique_geo_count"]:
        raise RuntimeError(f"Expected 77 comparisons/76 GEO runs, got {len(accepted)}/{len(verified_geo)}")
    frozen = ROOT / cfg["scene_manifest"]
    if frozen.exists():
        with frozen.open(encoding="utf-8-sig", newline="") as handle:
            existing = list(csv.DictReader(handle))
        if existing != accepted:
            raise RuntimeError(f"Refusing to overwrite a different frozen manifest: {frozen}")
    else:
        write_csv(frozen, accepted, list(accepted[0]))
    status_path = ROOT / "logs/rerun_corrected77_status.json"
    status = {"dataset_version": VERSION, "started_or_resumed_utc": utc_now(),
              "source_runs_root": str(source_runs), "source_manifest": str(source_manifest),
              "source_manifest_sha256": sha256(source_manifest), "accepted_comparisons": len(accepted),
              "verified_geo_runs": len(verified_geo), "excluded_comparison_ids": sorted(excluded),
              "frozen_manifest": str(frozen), "completed_stages": []}
    write_json(status_path, status)
    if args.preflight_only:
        status["state"] = "preflight_pass"
        write_json(status_path, status)
        return
    stages = ["audit_inputs.py", "build_scene_index.py", "build_coarsening_operator.py", "simulate_coarse_epic.py",
              "build_geo_features.py", "build_training_samples.py", "deterministic_baseline.py"]
    selected = stages[:args.max_stages] if args.max_stages else stages
    for script in selected:
        status["current_stage"] = script
        status["state"] = "running"
        write_json(status_path, status)
        run([str(args.model_python), str(CODE_ROOT / "src" / script)], ROOT / "logs/stages" / f"{script}.log")
        status["completed_stages"].append(script)
        write_json(status_path, status)
    if args.max_stages:
        status["state"] = "partial_smoke"
        status.pop("current_stage", None)
        write_json(status_path, status)
        return
    status["current_stage"] = "run_cnn_nightly.py"
    write_json(status_path, status)
    run([str(args.model_python), str(CODE_ROOT / "src/run_cnn_nightly.py")], ROOT / "logs/stages/run_cnn_nightly.log")
    status["state"] = "complete"
    status["finished_utc"] = utc_now()
    status.pop("current_stage", None)
    write_json(status_path, status)


if __name__ == "__main__":
    main()
