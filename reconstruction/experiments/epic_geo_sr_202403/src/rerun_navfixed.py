"""Rebuild March pairings from raw GEO, then rerun the complete prototype.

This never reads the v01 pairing directory. Set EPIC_GEO_SR_ROOT to a new,
dedicated output directory; a marker prevents accidental use of an old root.
"""
from __future__ import annotations

import argparse
import csv
import os
import shutil
import subprocess
import sys
from pathlib import Path

from common import CODE_ROOT, ROOT, load_config, write_json, utc_now
from nav_provenance import validate_pairing

VERSION = "sr_proto_202403_v02_navfixed"
MARKER = ROOT / ".navfixed_v02_root"


def run(command: list[str], log_path: Path, cwd: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n# {utc_now()} {' '.join(command)}\n")
        log.flush()
        result = subprocess.run(command, cwd=cwd, env=os.environ.copy(), stdout=log, stderr=subprocess.STDOUT)
        log.write(f"# {utc_now()} exit={result.returncode}\n")
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}); inspect {log_path}")


def check_root() -> None:
    if ROOT == CODE_ROOT or ROOT.name == "EPIC_GEO_SR_202403":
        raise RuntimeError("Set EPIC_GEO_SR_ROOT to a NEW v02 output directory, never the v01 root")
    if ROOT.exists() and any(ROOT.iterdir()) and not MARKER.is_file():
        raise RuntimeError(f"Refusing nonempty unmarked output root: {ROOT}")
    ROOT.mkdir(parents=True, exist_ok=True)
    if not MARKER.exists():
        MARKER.write_text(VERSION + "\n", encoding="utf-8")
    if MARKER.read_text(encoding="utf-8").strip() != VERSION:
        raise RuntimeError(f"Unexpected output-root marker: {MARKER}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--upstream-python", required=True, type=Path, help="Python with Satpy and GEO pipeline dependencies")
    p.add_argument("--model-python", required=True, type=Path, help="Isolated CPU Python with torch")
    p.add_argument("--base-stage-root", required=True, type=Path, help="GEO-ring time_index parent")
    p.add_argument("--core-code-root", type=Path, default=CODE_ROOT.parents[2] / "third_report/code/geo_ring_cloud_stage1")
    p.add_argument("--pairings-only", action="store_true", help="Stop after rebuilding and checking all GEO pairings")
    p.add_argument("--max-scenes", type=int, default=0, help="Smoke-test first N pairings without downstream steps")
    p.add_argument("--keep-scratch", action="store_true", help="Keep full upstream runs (requires roughly 160 GB for 53 scenes)")
    args = p.parse_args()
    cfg = load_config("data.yaml")
    if cfg["dataset_version"] != VERSION:
        raise RuntimeError("Config version and rerun entry point disagree")
    for path in (args.upstream_python, args.model_python, args.base_stage_root / "time_index/core_time_index.csv", args.core_code_root / "run_epic_georing_single_sample.py"):
        if not path.exists():
            raise RuntimeError(f"Missing prerequisite: {path}")
    epic_root = Path(os.environ["GEO_RING_EXTERNAL_EPIC_L2_ROOT"])
    if not epic_root.is_dir():
        raise RuntimeError(f"Missing EPIC root: {epic_root}")
    check_root()
    frozen = CODE_ROOT / "inputs/scene_manifest.csv"
    if not frozen.is_file():
        raise RuntimeError(f"Missing tracked frozen manifest: {frozen}")
    with frozen.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if len(rows) != cfg["expected_scene_count"] or len({r["sample_id"] for r in rows}) != len(rows):
        raise RuntimeError("Frozen manifest scene count/uniqueness check failed")
    local_manifest = ROOT / "inputs/scene_manifest.csv"
    local_manifest.parent.mkdir(parents=True, exist_ok=True)
    if not local_manifest.exists():
        local_manifest.write_bytes(frozen.read_bytes())
    elif local_manifest.read_bytes() != frozen.read_bytes():
        raise RuntimeError("Output-root scene manifest differs from tracked frozen manifest")
    pairings = ROOT / "inputs/geo_pairings"
    pairings.mkdir(parents=True, exist_ok=True)
    scratch_root = ROOT / "_scratch_pairings"
    scratch_root.mkdir(parents=True, exist_ok=True)
    state_path = ROOT / "logs/rerun_navfixed_status.json"
    status = {"dataset_version": VERSION, "started_or_resumed_utc": utc_now(), "root": str(ROOT), "core_code_root": str(args.core_code_root), "scenes": {}}
    selected = rows[:args.max_scenes] if args.max_scenes else rows
    for row in selected:
        scene = row["sample_id"]
        run_dir = pairings / scene
        try:
            provenance = validate_pairing(run_dir, scene, cfg["required_meteosat_navigation"])
            status["scenes"][scene] = {"state": "verified_existing", "provenance": provenance}
        except RuntimeError:
            epic = epic_root / Path(row["epic_file"]).name
            if not epic.is_file():
                raise RuntimeError(f"Missing raw EPIC: {epic}")
            scratch = scratch_root / scene
            command = [str(args.upstream_python), str(args.core_code_root / "run_epic_georing_single_sample.py"),
                       "--target-time", row["nearest_georing_time_utc"], "--time-tag", scene,
                       "--epic-l2", str(epic), "--output-root", str(scratch), "--runs-root", str(scratch_root),
                       "--base-stage-root", str(args.base_stage_root), "--run-id", f"{VERSION}_{scene}",
                       "--no-use-conda", "--python-exe", str(args.upstream_python)]
            status["scenes"][scene] = {"state": "running", "started_utc": utc_now()}
            write_json(state_path, status)
            run(command, ROOT / "logs/upstream" / f"{scene}.log", args.core_code_root)
            validate_pairing(scratch, scene, cfg["required_meteosat_navigation"])
            wanted = [Path("reprojected_grid/target_grid_definition.json"),
                      Path("fused_best_source/fused_cloud_mask.npz"),
                      Path("fused_best_source/valid_count_map_cloud_mask.npz"),
                      Path("fused_best_source/source_map_cloud_mask.npz"),
                      Path("fused_best_source/fused_cloud_top_height_km.npz")]
            wanted.extend(Path("standardized_native") / f"{source}_CLM_{scene}_native_cloud_v0.npz" for source in cfg["required_meteosat_navigation"])
            for relative in wanted:
                source = scratch / relative
                if not source.is_file():
                    if source.name == "fused_cloud_top_height_km.npz":
                        continue
                    raise RuntimeError(f"Missing freshly rebuilt file: {source}")
                destination = run_dir / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            provenance = validate_pairing(run_dir, scene, cfg["required_meteosat_navigation"])
            if not args.keep_scratch:
                resolved = scratch.resolve()
                if not resolved.is_relative_to(scratch_root.resolve()) or resolved == scratch_root.resolve() or scratch.is_symlink():
                    raise RuntimeError(f"Unsafe scratch cleanup target: {scratch}")
                shutil.rmtree(scratch)
            status["scenes"][scene] = {"state": "verified_new", "provenance": provenance}
        write_json(state_path, status)
    if args.max_scenes or args.pairings_only:
        status["state"] = "partial_smoke" if args.max_scenes else "pairings_complete"
        write_json(state_path, status)
        return
    stages = ["audit_inputs.py", "build_scene_index.py", "build_coarsening_operator.py", "simulate_coarse_epic.py",
              "build_geo_features.py", "build_training_samples.py", "deterministic_baseline.py"]
    for script in stages:
        status["current_stage"] = script
        write_json(state_path, status)
        run([str(args.model_python), str(CODE_ROOT / "src" / script)], ROOT / "logs/stages" / f"{script}.log", CODE_ROOT)
    status["current_stage"] = "run_cnn_nightly.py"
    write_json(state_path, status)
    run([str(args.model_python), str(CODE_ROOT / "src/run_cnn_nightly.py")], ROOT / "logs/stages/run_cnn_nightly.log", CODE_ROOT)
    status["state"] = "complete"
    status["finished_utc"] = utc_now()
    status.pop("current_stage", None)
    write_json(state_path, status)


if __name__ == "__main__":
    main()
