"""Wait for the frozen EPIC-80 run, then analyze and build the report deck."""

from __future__ import annotations

import argparse
import ctypes
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageStat

from geo_ring_cloud.paths import CODE_ROOT, RUNS_ROOT


COMPONENT_ROLE = "experiment_runner"
DEFAULT_STAGE09C_ROOT = RUNS_ROOT / "stage_09c_epic_80_satpy_navigation_rerun_202403"
DEFAULT_STAGE10_ROOT = RUNS_ROOT / "stage_10_cth_epic_80_satpy_navigation_rerun_202403"
DEFAULT_ANALYSIS_ROOT = RUNS_ROOT / "stage_10_epic_80_satpy_navigation_analysis_202403"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(temporary, path)


def manifest_status(path: Path) -> str:
    if not path.is_file():
        return "MISSING"
    try:
        return str(json.loads(path.read_text(encoding="utf-8-sig")).get("final_status", "UNKNOWN"))
    except (OSError, json.JSONDecodeError):
        return "UNREADABLE"


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_main_pid(stage09c: Path) -> int:
    path = stage09c / "00_control" / "background_process.json"
    if not path.is_file():
        return 0
    try:
        return int(json.loads(path.read_text(encoding="utf-8-sig")).get("pid", 0))
    except (OSError, ValueError, json.JSONDecodeError):
        return 0


def progress_counts(stage09c: Path) -> dict[str, int]:
    path = stage09c / "00_control" / "epic_80_run_status.csv"
    if not path.is_file():
        return {"geo_pass": 0, "clm_pass": 0, "fail_rows": 0}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    latest: dict[tuple[str, str, str, str], dict[str, str]] = {}
    for row in rows:
        key = (
            row.get("scope", ""),
            row.get("sample_id", ""),
            row.get("comparison_id", ""),
            row.get("step", ""),
        )
        latest[key] = row
    geo_pass = {
        row.get("sample_id", "")
        for row in rows
        if row.get("step") == "navigation_schema_verification" and row.get("status") == "PASS"
    }
    clm_pass = {
        row.get("comparison_id", "")
        for row in rows
        if row.get("status") == "PASS" and row.get("comparison_id") and row.get("scope") in {"geo_sample", "clm_epic_comparison"}
    }
    return {
        "geo_pass": len(geo_pass),
        "clm_pass": len(clm_pass),
        "fail_rows": sum(row.get("status") == "FAIL" for row in latest.values()),
    }


def write_status_report(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# EPIC 80 自动分析与汇报接力状态",
        "",
        f"- 状态：`{payload['status']}`",
        f"- 更新时间：`{payload['updated_utc']}`",
        f"- Stage 09C manifest：`{payload.get('stage09c_status', '')}`",
        f"- Stage 10 manifest：`{payload.get('stage10_status', '')}`",
        f"- 已通过 GEO 导航检查：`{payload.get('geo_pass', 0)}/79`",
        f"- 已完成 CLM/EPIC 配对：`{payload.get('clm_pass', 0)}/80`",
        f"- 主实验 PID：`{payload.get('main_pid', 0)}`（alive={payload.get('main_process_alive', False)}）",
        f"- 分析结果目录：`{payload.get('analysis_root', '')}`",
    ]
    if payload.get("message"):
        lines.extend(["", f"- 说明：{payload['message']}"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def runtime_env(python_exe: Path) -> dict[str, str]:
    env = os.environ.copy()
    library_bin = python_exe.resolve().parent / "Library" / "bin"
    if library_bin.is_dir():
        env["PATH"] = str(library_bin) + os.pathsep + env.get("PATH", "")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_logged(
    command: list[str],
    log_path: Path,
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    success_artifact: Path | None = None,
    success_marker: str = "",
) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        log.write(f"# start_utc={utc_now()}\n# command={' '.join(command)}\n\n")
        proc = subprocess.run(command, cwd=str(cwd), env=env, stdout=log, stderr=subprocess.STDOUT, text=True)
        log.write(f"\n# end_utc={utc_now()}\n# returncode={proc.returncode}\n")
    accepted_nonzero = bool(
        proc.returncode != 0
        and success_artifact is not None
        and success_artifact.is_file()
        and success_marker
        and success_marker in log_path.read_text(encoding="utf-8", errors="replace")
    )
    if proc.returncode != 0 and not accepted_nonzero:
        raise RuntimeError(f"command failed ({proc.returncode}); see {log_path}")
    return proc.returncode


def image_qa(render_dir: Path, output_path: Path, expected: int = 10) -> None:
    images = sorted(render_dir.glob("*.png"))
    if len(images) != expected:
        raise RuntimeError(f"expected {expected} rendered slides, found {len(images)}")
    rows: list[dict[str, Any]] = []
    for image_path in images:
        with Image.open(image_path) as image:
            rgb = image.convert("RGB")
            stat = ImageStat.Stat(rgb)
            extrema = rgb.getextrema()
            dynamic = max(high - low for low, high in extrema)
            if dynamic < 12:
                raise RuntimeError(f"rendered slide is nearly blank: {image_path}")
            rows.append(
                {
                    "slide": image_path.name,
                    "width": rgb.width,
                    "height": rgb.height,
                    "mean_r": stat.mean[0],
                    "mean_g": stat.mean[1],
                    "mean_b": stat.mean[2],
                    "dynamic_range": dynamic,
                    "status": "PASS",
                }
            )
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def layout_qa(layout_dir: Path, output_path: Path, expected: int = 10) -> None:
    paths = sorted(layout_dir.glob("slide-*.layout.json"))
    if len(paths) != expected:
        raise RuntimeError(f"expected {expected} layout JSON files, found {len(paths)}")
    rows: list[dict[str, Any]] = []
    for layout_path in paths:
        payload = json.loads(layout_path.read_text(encoding="utf-8-sig"))
        frame = payload["slide"]["frame"]
        width = float(frame["width"])
        height = float(frame["height"])
        for element in payload.get("elements", []):
            bbox = element.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            left, top, item_width, item_height = map(float, bbox)
            inside = left >= -0.5 and top >= -0.5 and left + item_width <= width + 0.5 and top + item_height <= height + 0.5
            rows.append(
                {
                    "slide": layout_path.stem,
                    "element_id": element.get("aid", element.get("id", "")),
                    "kind": element.get("kind", ""),
                    "left": left,
                    "top": top,
                    "width": item_width,
                    "height": item_height,
                    "inside_slide": inside,
                    "status": "PASS" if inside else "FAIL",
                }
            )
    if not rows or any(row["status"] == "FAIL" for row in rows):
        raise RuntimeError("artifact-tool layout QA found an element outside the slide frame")
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def build_presentation(
    *,
    analysis_root: Path,
    node_exe: Path,
    presentation_python: Path,
    presentation_skill: Path,
) -> Path:
    ppt_dir = analysis_root / "pptx"
    workspace = analysis_root / "00_control" / "presentation_workspace"
    pptx = ppt_dir / "geo_ring_cloud_epic_80_clm_cth_report_cn.pptx"
    logs = analysis_root / "00_control" / "logs"
    workspace.mkdir(parents=True, exist_ok=True)
    ppt_dir.mkdir(parents=True, exist_ok=True)
    node_env = os.environ.copy()
    if not node_env.get("HOME") and node_env.get("USERPROFILE"):
        node_env["HOME"] = node_env["USERPROFILE"]

    setup = presentation_skill / "container_tools" / "setup_artifact_tool_workspace.mjs"
    run_logged(
        [str(node_exe), str(setup), "--workspace", str(workspace)],
        logs / "presentation_workspace_setup.log",
        cwd=workspace,
        env=node_env,
    )
    source_builder = CODE_ROOT / "geo_ring_cloud_presentation_builder_epic_80.mjs"
    builder = workspace / "build_epic_80_deck.mjs"
    shutil.copy2(source_builder, builder)
    run_logged(
        [str(node_exe), str(builder), "--analysis-root", str(analysis_root), "--output", str(pptx)],
        logs / "presentation_build.log",
        cwd=workspace,
        env=node_env,
        success_artifact=pptx,
        success_marker="PRESENTATION_BUILD_PASS",
    )

    tools = presentation_skill / "container_tools"
    render_dir = ppt_dir / "rendered_slides"
    render_rc = run_logged(
        [str(presentation_python), str(tools / "render_slides.py"), str(pptx), "--output_dir", str(render_dir)],
        logs / "presentation_render.log",
        cwd=workspace,
        env=node_env,
        success_artifact=render_dir / "slide-10.png",
        success_marker='"slideCount": 10',
    )
    slides_test_rc = run_logged(
        [str(presentation_python), str(tools / "slides_test.py"), str(pptx)],
        logs / "presentation_overflow_test.log",
        cwd=workspace,
        env=node_env,
        success_artifact=pptx,
        success_marker='"slideCount": 10',
    )
    montage = ppt_dir / "geo_ring_cloud_epic_80_clm_cth_report_cn_montage.png"
    run_logged(
        [str(presentation_python), str(tools / "create_montage.py"), "--input_dir", str(render_dir), "--output_file", str(montage), "--num_col", "2", "--label_mode", "filename", "--fail_on_image_error"],
        logs / "presentation_montage.log",
        cwd=workspace,
        env=node_env,
    )
    image_qa(render_dir, ppt_dir / "presentation_render_qa.csv")
    layout_qa(ppt_dir / "artifact_tool_previews", ppt_dir / "presentation_layout_qa.csv")
    qa_note = {
        "render_command_returncode": render_rc,
        "slides_test_returncode": slides_test_rc,
        "artifact_tool_windows_exit_note": (
            "The bundled artifact-tool can return a Windows native cleanup error after all slide images are written. "
            "Non-zero status is accepted only when the expected 10-slide artifact and slideCount marker exist; "
            "layout-frame and rendered-image QA must still pass."
        ),
        "layout_qa": "PASS",
        "rendered_image_qa": "PASS",
        "montage": str(montage),
    }
    (ppt_dir / "presentation_qa_summary.json").write_text(
        json.dumps(qa_note, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return pptx


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage09c-root", type=Path, default=DEFAULT_STAGE09C_ROOT)
    parser.add_argument("--stage10-root", type=Path, default=DEFAULT_STAGE10_ROOT)
    parser.add_argument("--analysis-root", type=Path, default=DEFAULT_ANALYSIS_ROOT)
    parser.add_argument("--python-exe", type=Path, default=Path(sys.executable))
    parser.add_argument("--node-exe", type=Path, required=True)
    parser.add_argument("--presentation-python", type=Path, required=True)
    parser.add_argument("--presentation-skill", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=int, default=120)
    parser.add_argument("--timeout-hours", type=float, default=168.0)
    parser.add_argument("--dead-polls-before-fail", type=int, default=3)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    stage09c = args.stage09c_root.resolve()
    stage10 = args.stage10_root.resolve()
    analysis_root = args.analysis_root.resolve()
    control = analysis_root / "00_control"
    status_path = control / "automatic_followup_status.json"
    report_path = analysis_root / "reports" / "automatic_followup_status_cn.md"
    started = time.time()
    main_pid = read_main_pid(stage09c)
    dead_polls = 0
    payload: dict[str, Any] = {
        "status": "WAITING_FOR_EXPERIMENT",
        "started_utc": utc_now(),
        "analysis_root": str(analysis_root),
        "main_pid": main_pid,
    }

    try:
        for _ in iter(int, 1):
            s09 = manifest_status(stage09c / "manifest.json")
            s10 = manifest_status(stage10 / "manifest.json")
            alive = process_alive(main_pid)
            counts = progress_counts(stage09c)
            payload.update(
                {
                    "updated_utc": utc_now(),
                    "stage09c_status": s09,
                    "stage10_status": s10,
                    "main_process_alive": alive,
                    **counts,
                }
            )
            atomic_json(status_path, payload)
            write_status_report(report_path, payload)
            if s09 == "PASS" and s10 == "PASS":
                break
            if counts["fail_rows"]:
                raise RuntimeError("main experiment status log contains FAIL")
            dead_polls = 0 if alive else dead_polls + 1
            if dead_polls >= args.dead_polls_before_fail:
                raise RuntimeError("main experiment process ended before both final manifests reached PASS")
            if time.time() - started > args.timeout_hours * 3600:
                raise TimeoutError(f"follow-up watcher exceeded {args.timeout_hours} hours")
            time.sleep(max(5, args.poll_seconds))

        payload.update({"status": "RUNNING_ANALYSIS", "updated_utc": utc_now()})
        atomic_json(status_path, payload)
        write_status_report(report_path, payload)
        run_logged(
            [
                str(args.python_exe.resolve()),
                "-B",
                str(CODE_ROOT / "geo_ring_cloud_epic_80_analysis.py"),
                "--stage09c-root",
                str(stage09c),
                "--stage10-root",
                str(stage10),
                "--output-root",
                str(analysis_root),
            ],
            control / "logs" / "epic_80_analysis.log",
            cwd=CODE_ROOT,
            env=runtime_env(args.python_exe.resolve()),
        )

        payload.update({"status": "BUILDING_PRESENTATION", "updated_utc": utc_now()})
        atomic_json(status_path, payload)
        write_status_report(report_path, payload)
        pptx = build_presentation(
            analysis_root=analysis_root,
            node_exe=args.node_exe.resolve(),
            presentation_python=args.presentation_python.resolve(),
            presentation_skill=args.presentation_skill.resolve(),
        )
        payload.update(
            {
                "status": "EPIC80_ANALYSIS_AND_PRESENTATION_PASS",
                "updated_utc": utc_now(),
                "completed_utc": utc_now(),
                "presentation": str(pptx),
                "message": "CLM/CTH analysis, figures, quicklooks, rendered Chinese PPT and QA completed.",
            }
        )
        atomic_json(status_path, payload)
        write_status_report(report_path, payload)
        print(f"EPIC80_ANALYSIS_AND_PRESENTATION_PASS: {pptx}", flush=True)
        return 0
    except Exception as exc:
        payload.update(
            {
                "status": "EPIC80_AUTOMATIC_FOLLOWUP_FAILED",
                "updated_utc": utc_now(),
                "failed_utc": utc_now(),
                "message": f"{type(exc).__name__}: {exc}",
            }
        )
        atomic_json(status_path, payload)
        write_status_report(report_path, payload)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
