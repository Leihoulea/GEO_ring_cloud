"""Run the frozen Stage 09C 80-EPIC experiment with current production readers."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from geo_ring_cloud.lineage import write_manifest
from geo_ring_cloud.paths import BASE_STAGE_ROOT, CODE_ROOT, PROJECT_ROOT, RUNS_ROOT


STAGE_ID = "stage_09c"
COMPONENT_ROLE = "experiment_runner"
RUN_ID = "stage09c_epic80_satpy_navigation_rerun_202403"
DEFAULT_TARGET_LIST = (
    RUNS_ROOT
    / "stage09c_scaled_202403_batch"
    / "00_target_selection"
    / "stage09c_scaled_target_list.csv"
)
DEFAULT_OUTPUT_ROOT = RUNS_ROOT / "stage_09c_epic_80_satpy_navigation_rerun_202403"
DEFAULT_CTH_OUTPUT_ROOT = RUNS_ROOT / "stage_10_cth_epic_80_satpy_navigation_rerun_202403"
EXPECTED_TARGET_COUNT = 80
EXPECTED_UNIQUE_GEO_COUNT = 79
EXPECTED_NAVIGATION = {
    "Meteosat-0deg_CLM": (
        "meteosat_0deg_clm_v2",
        "official_seviri_native_area_definition",
    ),
    "Meteosat-IODC_CLM": ("meteosat_iodc_clm_satpy_v1", "satpy_area_definition"),
    "Meteosat-0deg_CTH": ("meteosat_0deg_cth_v2", "satpy_seviri_l2_grib_cth_area"),
    "Meteosat-IODC_CTH": ("meteosat_iodc_cth_v2", "satpy_seviri_l2_grib_cth_area"),
}
STATUS_FIELDS = [
    "timestamp_utc",
    "scope",
    "sample_id",
    "comparison_id",
    "step",
    "status",
    "returncode",
    "elapsed_sec",
    "log_path",
    "message",
]
SINGLE_SAMPLE_STEPS = [
    "02_build_standardized_cloud_native",
    "03_validate_standardized_cloud_native",
    "03_5_semantic_validation_patch",
    "05_reproject_cloud_to_grid",
    "06_fuse_best_source",
    "08c_epic_cloud_mask_semantic_sensitivity",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def comparison_id(sample_id: str, epic_file: str) -> str:
    match = re.search(r"(20\d{12})", Path(epic_file).name)
    if not match:
        raise ValueError(f"EPIC filename has no 14-digit timestamp: {epic_file}")
    return f"{sample_id}__epic_{match.group(1)}"


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def append_status(path: Path, row: dict[str, Any]) -> None:
    rows: list[dict[str, Any]] = []
    if path.exists():
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
    rows.append({"timestamp_utc": utc_now(), **row})
    write_csv(path, rows, STATUS_FIELDS)


def runtime_environment() -> dict[str, str]:
    env = os.environ.copy()
    library_bin = Path(sys.executable).resolve().parent / "Library" / "bin"
    if library_bin.is_dir():
        env["PATH"] = str(library_bin) + os.pathsep + env.get("PATH", "")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_command(
    command: list[str],
    *,
    name: str,
    log_path: Path,
    status_path: Path,
    scope: str,
    sample_id: str = "",
    comparison: str = "",
    env: dict[str, str] | None = None,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        log.write(f"# start_utc={utc_now()}\n# command={' '.join(command)}\n\n")
        proc = subprocess.run(
            command,
            cwd=str(CODE_ROOT),
            env=env or runtime_environment(),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        log.write(f"\n# end_utc={utc_now()}\n# returncode={proc.returncode}\n")
    elapsed = time.time() - started
    row = {
        "scope": scope,
        "sample_id": sample_id,
        "comparison_id": comparison,
        "step": name,
        "status": "PASS" if proc.returncode == 0 else "FAIL",
        "returncode": proc.returncode,
        "elapsed_sec": f"{elapsed:.3f}",
        "log_path": str(log_path),
        "message": "",
    }
    append_status(status_path, row)
    if proc.returncode != 0:
        raise RuntimeError(f"{name} failed for {sample_id or comparison}; see {log_path}")


def load_targets(path: Path, *, require_full: bool) -> pd.DataFrame:
    targets = pd.read_csv(path, dtype=str).fillna("")
    required = {
        "sample_id",
        "epic_file",
        "epic_time_utc",
        "nearest_georing_time_utc",
        "time_diff_min",
        "candidate_group",
        "estimated_dominant_source",
    }
    missing = sorted(required - set(targets.columns))
    if missing:
        raise RuntimeError(f"target list missing columns: {missing}")
    targets["comparison_id"] = [
        comparison_id(sample_id, epic_file)
        for sample_id, epic_file in zip(targets["sample_id"], targets["epic_file"])
    ]
    problems: list[str] = []
    if targets["comparison_id"].duplicated().any():
        problems.append("comparison_id is not unique")
    missing_files = [path for path in targets["epic_file"] if not Path(path).is_file()]
    if missing_files:
        problems.append(f"{len(missing_files)} EPIC files are missing")
    if require_full and len(targets) != EXPECTED_TARGET_COUNT:
        problems.append(f"expected {EXPECTED_TARGET_COUNT} targets, found {len(targets)}")
    unique_geo = targets["sample_id"].nunique()
    if require_full and unique_geo != EXPECTED_UNIQUE_GEO_COUNT:
        problems.append(f"expected {EXPECTED_UNIQUE_GEO_COUNT} unique GEO times, found {unique_geo}")
    if problems:
        raise RuntimeError("; ".join(problems))
    return targets


def environment_check(python_exe: Path, output_root: Path) -> Path:
    script = (
        "import json, eccodes, netCDF4, satpy; "
        "from satpy import available_readers; "
        "print(json.dumps({'eccodes_python': eccodes.__version__, "
        "'eccodes_c': eccodes.codes_get_api_version(), 'netCDF4': netCDF4.__version__, "
        "'satpy': satpy.__version__, 'seviri_l2_grib': 'seviri_l2_grib' in available_readers()}))"
    )
    checks: dict[str, Any] = {"python_executable": str(python_exe), "checked_utc": utc_now()}
    env = runtime_environment()
    for name, command in (
        ("imports", [str(python_exe), "-B", "-c", script]),
        ("eccodes_selfcheck", [str(python_exe), "-B", "-m", "eccodes", "selfcheck"]),
    ):
        proc = subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        checks[name] = {
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
        if proc.returncode != 0:
            raise RuntimeError(f"environment check failed: {name}: {proc.stderr or proc.stdout}")
    import_info = json.loads(checks["imports"]["stdout"].splitlines()[-1])
    if not import_info["seviri_l2_grib"]:
        raise RuntimeError("environment check failed: seviri_l2_grib is unavailable")
    checks["versions"] = import_info
    path = output_root / "00_control" / "environment_check.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def npz_metadata(path: Path) -> dict[str, Any]:
    with np.load(path, allow_pickle=True) as archive:
        if "metadata_json" not in archive.files:
            raise RuntimeError(f"metadata_json missing: {path}")
        raw = archive["metadata_json"]
        value = raw.item() if raw.shape == () else raw.ravel()[0]
    return json.loads(str(value))


def verify_navigation(sample_dir: Path, sample_id: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for product_key, (schema, source) in EXPECTED_NAVIGATION.items():
        matches = list((sample_dir / "standardized_native").glob(f"{product_key}_{sample_id}_native_cloud_v0.npz"))
        if len(matches) != 1:
            raise RuntimeError(f"expected one {product_key} native NPZ for {sample_id}, found {len(matches)}")
        metadata = npz_metadata(matches[0])
        attrs = metadata.get("reader_attrs", {})
        actual_schema = str(attrs.get("navigation_schema_version", ""))
        actual_source = str(attrs.get("navigation_source", ""))
        if actual_schema != schema or actual_source != source:
            raise RuntimeError(
                f"navigation metadata mismatch for {product_key} {sample_id}: "
                f"schema={actual_schema!r}, source={actual_source!r}"
            )
        rows.append(
            {
                "sample_id": sample_id,
                "product": product_key,
                "navigation_schema_version": actual_schema,
                "navigation_source": actual_source,
                "npz_file": str(matches[0]),
                "status": "PASS",
            }
        )
    return rows


def metric_file_for_primary(sample_dir: Path, sample_id: str) -> Path:
    return (
        sample_dir
        / f"epic_l2_cloud_mask_semantic_sensitivity_{sample_id}"
        / "epic_georing_cloud_mask_sensitivity_metrics.csv"
    )


def single_sample_resume_step(sample_dir: Path, target_time: str) -> str:
    manifest_path = sample_dir / "single_sample_run_manifest.json"
    if not manifest_path.is_file():
        return ""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if manifest.get("target_time") != target_time or manifest.get("source_profile") != "operational_baseline":
        return ""
    by_name = {row.get("step"): row for row in manifest.get("steps", [])}
    for step in SINGLE_SAMPLE_STEPS:
        if by_name.get(step, {}).get("status") != "OK":
            prior = SINGLE_SAMPLE_STEPS[: SINGLE_SAMPLE_STEPS.index(step)]
            if all(by_name.get(name, {}).get("status") == "OK" for name in prior):
                return step
            return ""
    return ""


def run_geo_sample(
    sample: pd.Series,
    *,
    python_exe: Path,
    output_root: Path,
    base_stage_root: Path,
    status_path: Path,
    resume: bool,
) -> Path:
    sample_id = str(sample["sample_id"])
    sample_dir = output_root / "runs" / sample_id
    command = [
        str(python_exe),
        "-B",
        str(CODE_ROOT / "run_epic_georing_single_sample.py"),
        "--target-time",
        str(sample["nearest_georing_time_utc"]),
        "--time-tag",
        sample_id,
        "--epic-l2",
        str(sample["epic_file"]),
        "--output-root",
        str(sample_dir),
        "--runs-root",
        str(output_root / "runs"),
        "--base-stage-root",
        str(base_stage_root),
        "--run-id",
        f"{RUN_ID}__{sample_id}",
        "--source-profile",
        "operational_baseline",
        "--no-use-conda",
        "--python-exe",
        str(python_exe),
    ]
    resume_step = single_sample_resume_step(sample_dir, str(sample["nearest_georing_time_utc"])) if resume else ""
    if resume_step:
        command.extend(["--start-step", resume_step])
    run_command(
        command,
        name="stage_02_to_08c_primary",
        log_path=output_root / "logs" / sample_id / "pipeline.log",
        status_path=status_path,
        scope="geo_sample",
        sample_id=sample_id,
        comparison=str(sample["comparison_id"]),
    )
    return sample_dir


def run_extra_clm_comparison(
    sample: pd.Series,
    *,
    python_exe: Path,
    sample_dir: Path,
    output_root: Path,
    status_path: Path,
) -> Path:
    comp_id = str(sample["comparison_id"])
    out_dir = output_root / "clm_epic_comparisons" / comp_id
    command = [
        str(python_exe),
        "-B",
        str(CODE_ROOT / "08c_epic_cloud_mask_semantic_sensitivity.py"),
        "--time-run-root",
        str(sample_dir),
        "--epic-l2",
        str(sample["epic_file"]),
        "--target-time",
        str(sample["nearest_georing_time_utc"]),
        "--time-tag",
        str(sample["sample_id"]),
        "--out-dir",
        str(out_dir),
        "--report-dir",
        str(out_dir / "reports"),
    ]
    run_command(
        command,
        name="stage_08c_extra_epic",
        log_path=output_root / "logs" / str(sample["sample_id"]) / f"{comp_id}.log",
        status_path=status_path,
        scope="clm_epic_comparison",
        sample_id=str(sample["sample_id"]),
        comparison=comp_id,
    )
    return out_dir / "epic_georing_cloud_mask_sensitivity_metrics.csv"


def build_stage10_manifest(targets: pd.DataFrame, output_root: Path) -> Path:
    rows: list[dict[str, Any]] = []
    for _, target in targets.iterrows():
        rows.append(
            {
                "sample_id": target["comparison_id"],
                "geo_sample_id": target["sample_id"],
                "stage_run_dir": str(output_root / "runs" / target["sample_id"]),
                "epic_file": target["epic_file"],
                "epic_time_utc": target["epic_time_utc"],
                "nearest_georing_time_utc": target["nearest_georing_time_utc"],
                "time_diff_min": target["time_diff_min"],
                "candidate_group": target["candidate_group"],
                "dominant_source": target["estimated_dominant_source"],
            }
        )
    path = output_root / "00_control" / "stage_10_epic_80_sample_manifest.csv"
    write_csv(path, rows, list(rows[0]))
    return path


def aggregate_clm_metrics(metric_records: list[dict[str, str]], output_root: Path) -> Path:
    frames: list[pd.DataFrame] = []
    for record in metric_records:
        frame = pd.read_csv(record["metrics_file"])
        frame.insert(0, "comparison_id", record["comparison_id"])
        frame.insert(0, "sample_id", record["sample_id"])
        frame.insert(2, "epic_file", record["epic_file"])
        frames.append(frame)
    out = output_root / "08_clm_epic" / "epic_80_cloud_mask_sensitivity_metrics.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.concat(frames, ignore_index=True).to_csv(out, index=False, encoding="utf-8-sig")
    return out


def write_progress_report(
    output_root: Path,
    targets: pd.DataFrame,
    completed_geo: int,
    completed_comparisons: int,
    final_status: str,
) -> Path:
    report = output_root / "reports" / "epic_80_rerun_status_cn.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 80 时次 EPIC 对比实验重跑状态",
        "",
        f"- 状态：`{final_status}`",
        f"- 冻结 EPIC 配对：`{len(targets)}`",
        f"- 唯一 GEO 时次：`{targets['sample_id'].nunique()}`",
        f"- 已完成 GEO 重建：`{completed_geo}`",
        f"- 已完成 EPIC 对比：`{completed_comparisons}`",
        "- 处理范围：Stage 02、03、03.5、05、06、08c，以及完成全部样本后的 Stage 10 CTH/QC。",
        "- Meteosat-IODC CLM 导航要求：`meteosat_iodc_clm_satpy_v1`。",
        "- Meteosat-IODC CTH 导航要求：`meteosat_iodc_cth_v2`。",
        "- 旧实验目录未复用、未覆盖。",
    ]
    report.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-list", type=Path, default=DEFAULT_TARGET_LIST)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--cth-output-root", type=Path, default=DEFAULT_CTH_OUTPUT_ROOT)
    parser.add_argument("--base-stage-root", type=Path, default=BASE_STAGE_ROOT)
    parser.add_argument("--python-exe", type=Path, default=Path(sys.executable))
    parser.add_argument("--only-sample", action="append", default=[])
    parser.add_argument("--max-unique-samples", type=int, default=0)
    parser.add_argument("--skip-cth", action="store_true")
    parser.add_argument("--skip-cth-qc", action="store_true")
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    output_root = args.output_root.resolve()
    cth_output_root = args.cth_output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    status_path = output_root / "00_control" / "epic_80_run_status.csv"
    environment_path = environment_check(args.python_exe.resolve(), output_root)
    all_targets = load_targets(args.target_list.resolve(), require_full=True)
    all_targets.to_csv(
        output_root / "00_control" / "frozen_epic_80_target_manifest.csv",
        index=False,
        encoding="utf-8-sig",
    )

    targets = all_targets
    if args.only_sample:
        requested = set(args.only_sample)
        unknown = sorted(requested - set(targets["sample_id"]))
        if unknown:
            raise RuntimeError(f"unknown --only-sample values: {unknown}")
        targets = targets[targets["sample_id"].isin(requested)].copy()
    if args.max_unique_samples:
        keep = list(dict.fromkeys(targets["sample_id"]))[: args.max_unique_samples]
        targets = targets[targets["sample_id"].isin(keep)].copy()

    prior_passed: set[str] = set()
    if args.resume and status_path.exists():
        previous = pd.read_csv(status_path, dtype=str).fillna("")
        prior_passed = set(
            previous.loc[
                (previous["scope"] == "geo_sample")
                & (previous["step"] == "navigation_schema_verification")
                & (previous["status"] == "PASS"),
                "sample_id",
            ]
        )

    navigation_rows: list[dict[str, str]] = []
    metric_records: list[dict[str, str]] = []
    completed_geo = 0
    completed_comparisons = 0
    env = runtime_environment()
    for sample_id, group in targets.groupby("sample_id", sort=False):
        ordered = group.sort_values("epic_time_utc").reset_index(drop=True)
        primary = ordered.iloc[0]
        sample_dir = output_root / "runs" / sample_id
        can_resume = (
            args.resume
            and sample_id in prior_passed
            and metric_file_for_primary(sample_dir, sample_id).is_file()
        )
        if not can_resume:
            sample_dir = run_geo_sample(
                primary,
                python_exe=args.python_exe.resolve(),
                output_root=output_root,
                base_stage_root=args.base_stage_root.resolve(),
                status_path=status_path,
                resume=args.resume,
            )
        verified = verify_navigation(sample_dir, sample_id)
        navigation_rows.extend(verified)
        append_status(
            status_path,
            {
                "scope": "geo_sample",
                "sample_id": sample_id,
                "comparison_id": str(primary["comparison_id"]),
                "step": "navigation_schema_verification",
                "status": "PASS",
                "returncode": 0,
                "elapsed_sec": "0",
                "log_path": "",
                "message": "four Meteosat CLM/CTH products match required navigation schemas",
            },
        )
        completed_geo += 1

        primary_metric = metric_file_for_primary(sample_dir, sample_id)
        if not primary_metric.is_file():
            raise RuntimeError(f"primary CLM metric file missing: {primary_metric}")
        metric_records.append(
            {
                "sample_id": sample_id,
                "comparison_id": str(primary["comparison_id"]),
                "epic_file": str(primary["epic_file"]),
                "metrics_file": str(primary_metric),
            }
        )
        completed_comparisons += 1

        for _, extra in ordered.iloc[1:].iterrows():
            extra_metric = (
                output_root
                / "clm_epic_comparisons"
                / str(extra["comparison_id"])
                / "epic_georing_cloud_mask_sensitivity_metrics.csv"
            )
            if not (args.resume and extra_metric.is_file()):
                extra_metric = run_extra_clm_comparison(
                    extra,
                    python_exe=args.python_exe.resolve(),
                    sample_dir=sample_dir,
                    output_root=output_root,
                    status_path=status_path,
                )
            metric_records.append(
                {
                    "sample_id": sample_id,
                    "comparison_id": str(extra["comparison_id"]),
                    "epic_file": str(extra["epic_file"]),
                    "metrics_file": str(extra_metric),
                }
            )
            completed_comparisons += 1

        write_csv(
            output_root / "00_control" / "navigation_schema_verification.csv",
            navigation_rows,
            [
                "sample_id",
                "product",
                "navigation_schema_version",
                "navigation_source",
                "npz_file",
                "status",
            ],
        )
        write_progress_report(
            output_root,
            targets,
            completed_geo,
            completed_comparisons,
            "RUNNING",
        )

    clm_metrics = aggregate_clm_metrics(metric_records, output_root)
    stage10_manifest = build_stage10_manifest(targets, output_root)
    is_full_run = len(targets) == EXPECTED_TARGET_COUNT and targets["sample_id"].nunique() == EXPECTED_UNIQUE_GEO_COUNT
    cth_outputs: list[Path] = []
    if not args.skip_cth:
        if not is_full_run:
            raise RuntimeError("Stage 10 aggregation is only allowed after all frozen 80 targets complete")
        run_command(
            [
                str(args.python_exe.resolve()),
                "-B",
                str(CODE_ROOT / "stage_10_cth_validation" / "stage_10_run_cth_validation.py"),
                "--sample-manifest",
                str(stage10_manifest),
                "--output-dir",
                str(cth_output_root),
            ],
            name="stage_10_cth_validation",
            log_path=cth_output_root / "logs" / "stage_10_cth_validation.log",
            status_path=status_path,
            scope="cth_epic_aggregate",
            env=env,
        )
        cth_outputs.append(cth_output_root)
        if not args.skip_cth_qc:
            run_command(
                [
                    str(args.python_exe.resolve()),
                    "-B",
                    str(CODE_ROOT / "stage_10_cth_validation" / "stage_10_qc_audit.py"),
                    "--sample-manifest",
                    str(stage10_manifest),
                    "--stage10-output-dir",
                    str(cth_output_root),
                ],
                name="stage_10_cth_qc",
                log_path=cth_output_root / "logs" / "stage_10_cth_qc.log",
                status_path=status_path,
                scope="cth_epic_aggregate",
                env=env,
            )

    final_status = "PASS" if is_full_run and not args.skip_cth else "PREFLIGHT_PASS"
    report = write_progress_report(
        output_root,
        targets,
        completed_geo,
        completed_comparisons,
        final_status,
    )
    manifest_path = write_manifest(
        output_root / "manifest.json",
        canonical_stage_id=STAGE_ID,
        component_role=COMPONENT_ROLE,
        related_stage_ids=("stage_02", "stage_03", "stage_05", "stage_06", "stage_10"),
        generating_script=Path(__file__).resolve(),
        input_paths=(args.target_list.resolve(), environment_path),
        output_paths=(output_root, clm_metrics, stage10_manifest, *cth_outputs),
        parameters={
            "frozen_target_count": len(all_targets),
            "selected_target_count": len(targets),
            "selected_unique_geo_count": targets["sample_id"].nunique(),
            "source_profile": "operational_baseline",
            "resume": args.resume,
            "skip_cth": args.skip_cth,
        },
        project_root=PROJECT_ROOT,
        run_id=RUN_ID,
        source_profile="operational_baseline",
        extra={"final_status": final_status, "report": str(report)},
    )
    if cth_outputs:
        write_manifest(
            cth_output_root / "manifest.json",
            canonical_stage_id="stage_10",
            component_role="validation_runner",
            related_stage_ids=("stage_09c",),
            generating_script=Path(__file__).resolve(),
            input_paths=(stage10_manifest,),
            output_paths=(cth_output_root,),
            parameters={"sample_count": len(targets), "epic_cth_band": "A-band"},
            project_root=PROJECT_ROOT,
            run_id=RUN_ID,
            source_profile="operational_baseline",
            extra={"final_status": final_status},
        )
    print(f"{final_status}: report={report} manifest={manifest_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
