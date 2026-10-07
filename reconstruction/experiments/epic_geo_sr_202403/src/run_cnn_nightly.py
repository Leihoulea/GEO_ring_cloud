"""Run the frozen CPU CNN ablation queue and leave machine-readable status."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from common import CODE_ROOT, ROOT, load_config


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def main() -> None:
    cnn = load_config("cnn.yaml")
    models = ["cnn_c", "cnn_g", "cnn_cg", "cnn_cg_closure"]
    logs = ROOT / "logs" / "cnn_nightly"
    logs.mkdir(parents=True, exist_ok=True)
    status_file = logs / "status.json"
    queue = [(seed, model) for seed in cnn["seeds"] for model in models]
    if status_file.exists():
        status = json.loads(status_file.read_text(encoding="utf-8"))
        status["resumed_utc"] = now()
        status["state"] = "running"
        status.pop("current", None)
    else:
        status = {"started_utc": now(), "state": "running", "queue": queue, "completed": [], "failed": []}
    status_file.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for seed, model in queue:
        job = f"{model}_seed{seed}"
        if any(item["job"] == job for item in status["completed"]):
            continue
        log_path = logs / f"{job}.log"
        status["current"] = {"job": job, "started_utc": now(), "log": str(log_path)}
        status_file.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        command = [sys.executable, str(CODE_ROOT / "src" / "train_cnn.py"), model, "--seed", str(seed), "--approve-coarsening"]
        with log_path.open("w", encoding="utf-8") as log:
            log.write(json.dumps({"event": "start", "utc": now(), "command": command}) + "\n")
            run = subprocess.run(command, cwd=ROOT, env=os.environ.copy(), stdout=log, stderr=subprocess.STDOUT, text=True)
            if run.returncode == 0:
                inference = subprocess.run([sys.executable, str(CODE_ROOT / "src" / "infer_cnn.py"), model, "--seed", str(seed)], cwd=ROOT, env=os.environ.copy(), stdout=log, stderr=subprocess.STDOUT, text=True)
                run_code = inference.returncode
            else:
                run_code = run.returncode
            log.write(json.dumps({"event": "finish", "utc": now(), "return_code": run_code}) + "\n")
        if run_code:
            status["failed"].append({"job": job, "return_code": run_code, "finished_utc": now()})
            status["state"] = "failed"
            status_file.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            raise SystemExit(f"Nightly queue stopped at {job}; inspect {log_path}")
        status["completed"].append({"job": job, "finished_utc": now()})
        status.pop("current", None)
        status_file.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    final = [sys.executable, str(CODE_ROOT / "src" / "evaluate_models.py")]
    with (logs / "postprocess.log").open("w", encoding="utf-8") as log:
        for command in [final, [sys.executable, str(CODE_ROOT / "src" / "paired_scene_bootstrap.py")], [sys.executable, str(CODE_ROOT / "src" / "analyze_conditional_gain.py")], [sys.executable, str(CODE_ROOT / "src" / "analyze_information_gain.py")], [sys.executable, str(CODE_ROOT / "src" / "plot_results.py")], [sys.executable, str(CODE_ROOT / "src" / "render_information_gain_maps.py")]]:
            run = subprocess.run(command, cwd=ROOT, env=os.environ.copy(), stdout=log, stderr=subprocess.STDOUT, text=True)
            if run.returncode:
                status["state"] = "postprocess_failed"
                status["postprocess_return_code"] = run.returncode
                status_file.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                raise SystemExit("CNN runs completed but postprocessing failed; inspect postprocess.log")
    status["state"] = "complete"; status["finished_utc"] = now()
    status_file.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
