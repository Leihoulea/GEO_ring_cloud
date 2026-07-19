from __future__ import annotations

import argparse
import csv
import json
import math
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from geo_ring_cloud.lineage import utc_now  # noqa: E402
from geo_ring_cloud.paths import BASE_STAGE_ROOT, RUNS_ROOT  # noqa: E402


OUT_ROOT = RUNS_ROOT / "stage09b_full_202403_overnight_diagnostics"
STAGE08_INVENTORY = RUNS_ROOT / "epic_202403_target_selection" / "epic_202403_geo_source_candidate_inventory.csv"
STAGE09_INVENTORY = RUNS_ROOT / "stage09_epic_georing_cloud_mask_diagnostics" / "stage09_epic_product_inventory.csv"

SOURCE_NAMES = ["FY4B", "GOES-16", "GOES-18", "Himawari-9", "Meteosat-0deg", "Meteosat-IODC"]
SOURCE_PRODUCTS = {
    "FY4B": "CLM",
    "GOES-16": "ACMF",
    "GOES-18": "ACMF",
    "Himawari-9": "CMSK",
    "Meteosat-0deg": "CLM",
    "Meteosat-IODC": "CLM",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        keys: set[str] = set()
        for row in rows:
            keys.update(row.keys())
        fields = sorted(keys)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def boolish(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "ok", "pass"}


def time_tag_from_hour(value: str) -> str:
    if not value:
        return ""
    return value[0:13].replace("-", "").replace("T", "_") + "00"


def is_march_2024(row: dict[str, Any]) -> bool:
    return str(row.get("epic_time", "")).startswith("2024-03") and str(row.get("nearest_hour", "")).startswith("2024-03")


def parse_source_fraction(value: Any) -> dict[str, float]:
    try:
        raw = json.loads(str(value or "{}"))
    except Exception:
        raw = {}
    return {source: safe_float(raw.get(source), 0.0) for source in SOURCE_NAMES}


def parse_statuses(value: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in str(value or "").split("|"):
        if "=" not in part:
            continue
        key, val = part.split("=", 1)
        out[key.strip()] = val.strip()
    return out


def has_prefusion_products(run_dir: Path, tag: str) -> tuple[bool, list[str]]:
    found: list[str] = []
    for source, product in SOURCE_PRODUCTS.items():
        source_dir = run_dir / "reprojected_grid" / source
        if list(source_dir.glob(f"{source}_{product}_cloud_mask_grid_{tag}.npz")) or list(source_dir.glob(f"*cloud_mask*{tag}.npz")):
            found.append(source)
    return bool(found), found


def has_fused_product(run_dir: Path) -> bool:
    return (run_dir / "fused_best_source" / "fused_cloud_mask.npz").exists() or (run_dir / "fused_best_source" / "fused_cloud_binary.npz").exists()


def semantic_metrics_path(run_dir: Path, tag: str) -> Path:
    return run_dir / f"epic_l2_cloud_mask_semantic_sensitivity_{tag}" / "epic_georing_cloud_mask_sensitivity_metrics.csv"


def setup_dirs(out_root: Path) -> None:
    for name in [
        "00_code_reuse_audit",
        "01_inventory_expansion",
        "02_target_selection",
        "03_stage_run_attempts",
        "04_expanded_core_metrics",
        "05_semantic_diagnostics",
        "06_sampling_sensitivity",
        "07_geometry_diagnostics",
        "08_scene_type_diagnostics",
        "09_source_product_diagnostics",
        "10_source_pair_diagnostics",
        "11_time_offset_diagnostics",
        "12_error_atlas",
        "13_summary_figures",
        "reports",
        "logs",
    ]:
        (out_root / name).mkdir(parents=True, exist_ok=True)


def write_code_reuse_audit(out_root: Path) -> None:
    rows = [
        {"functionality": "EPIC L2 cloud read / variable discovery", "existing_script_path": str(SCRIPT_DIR / "08c_epic_cloud_mask_semantic_sensitivity.py"), "existing_function_or_class": "read_epic_l2, find_var_name, read_nc_var", "can_reuse_directly": "yes", "needs_wrapper": "no", "risk": "low", "notes": "Used by existing 08c semantic comparison."},
        {"functionality": "EPIC time parsing", "existing_script_path": str(SCRIPT_DIR / "08c_epic_cloud_mask_semantic_sensitivity.py"), "existing_function_or_class": "parse_time_from_name, target_delta_min", "can_reuse_directly": "yes", "needs_wrapper": "optional", "risk": "low", "notes": "09B mostly reuses existing Stage 08 inventory time fields."},
        {"functionality": "GEO-ring fused product read", "existing_script_path": str(SCRIPT_DIR / "08c_epic_cloud_mask_semantic_sensitivity.py"), "existing_function_or_class": "load_npz_array", "can_reuse_directly": "yes", "needs_wrapper": "no", "risk": "low", "notes": "Reads fused npz without modifying fusion output."},
        {"functionality": "prefusion source cloud-mask read", "existing_script_path": str(SCRIPT_DIR / "08f_geometry_and_prefusion_epic_diagnostics.py"), "existing_function_or_class": "find_reprojected_cloud_mask, load_npz", "can_reuse_directly": "yes", "needs_wrapper": "yes", "risk": "low", "notes": "09B uses file presence and existing 08f outputs for source diagnostics."},
        {"functionality": "grid sampling to EPIC pixels", "existing_script_path": str(SCRIPT_DIR / "08c_epic_cloud_mask_semantic_sensitivity.py"), "existing_function_or_class": "sample_grid_to_points", "can_reuse_directly": "yes", "needs_wrapper": "no", "risk": "medium", "notes": "Nearest-neighbor sampling is a diagnostic limitation, not fusion logic."},
        {"functionality": "cloud-mask semantic mapping", "existing_script_path": str(SCRIPT_DIR / "08c_epic_cloud_mask_semantic_sensitivity.py"), "existing_function_or_class": "EPIC_POLICIES, apply_policy", "can_reuse_directly": "yes", "needs_wrapper": "no", "risk": "low", "notes": "Keeps Policy A/B/C identical to Stage 08."},
        {"functionality": "metric computation", "existing_script_path": str(SCRIPT_DIR / "08c_epic_cloud_mask_semantic_sensitivity.py"), "existing_function_or_class": "binary_metrics, multiclass_agreement", "can_reuse_directly": "yes", "needs_wrapper": "no", "risk": "low", "notes": "Agreement/F1/IoU remain diagnostic, not truth validation."},
        {"functionality": "quicklook plotting", "existing_script_path": str(SCRIPT_DIR / "08c_epic_cloud_mask_semantic_sensitivity.py"), "existing_function_or_class": "quicklook functions in 08c", "can_reuse_directly": "partly", "needs_wrapper": "yes", "risk": "medium", "notes": "09B only adds summary figures backed by CSV."},
        {"functionality": "source-by-source diagnostics", "existing_script_path": str(SCRIPT_DIR / "08f_geometry_and_prefusion_epic_diagnostics.py"), "existing_function_or_class": "run", "can_reuse_directly": "yes", "needs_wrapper": "yes", "risk": "medium", "notes": "Existing 08f coverage may lag newly generated samples; warnings are recorded."},
        {"functionality": "source-pair overlap diagnostics", "existing_script_path": str(SCRIPT_DIR / "08j_prefusion_source_pair_overlap_diagnostics.py"), "existing_function_or_class": "analyze_sample, build_report", "can_reuse_directly": "yes", "needs_wrapper": "yes", "risk": "medium", "notes": "Used only for diagnosis; never generates fused products."},
        {"functionality": "single-sample pipeline runner", "existing_script_path": str(SCRIPT_DIR / "run_epic_georing_single_sample.py"), "existing_function_or_class": "run_pipeline", "can_reuse_directly": "yes", "needs_wrapper": "yes", "risk": "medium", "notes": "09B calls it through conda run -n pytorch python -B."},
        {"functionality": "batch runner", "existing_script_path": str(SCRIPT_DIR / "run_epic_georing_sample_batch.py"), "existing_function_or_class": "run_one, build_report", "can_reuse_directly": "partly", "needs_wrapper": "yes", "risk": "medium", "notes": "09B implements stricter March-only target selection and richer logging."},
        {"functionality": "Stage 09 summary", "existing_script_path": str(SCRIPT_DIR / "09_stage09_epic_georing_cloud_mask_diagnostics.py"), "existing_function_or_class": "main", "can_reuse_directly": "yes", "needs_wrapper": "yes", "risk": "low", "notes": "Can be rerun after new samples; 09B keeps separate outputs."},
    ]
    fields = ["functionality", "existing_script_path", "existing_function_or_class", "can_reuse_directly", "needs_wrapper", "risk", "notes"]
    write_csv(out_root / "00_code_reuse_audit" / "stage09b_existing_code_reuse_audit.csv", rows, fields)
    lines = ["# Stage 09B Existing Code Reuse Audit", "", "This audit favors reuse and wrappers over refactoring.", "", "| functionality | script | reusable | wrapper | risk | notes |", "|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['functionality']} | `{r['existing_script_path']}` | {r['can_reuse_directly']} | {r['needs_wrapper']} | {r['risk']} | {r['notes']} |")
    (out_root / "00_code_reuse_audit" / "stage09b_existing_code_reuse_audit.md").write_text("\n".join(lines), encoding="utf-8")


def build_inventory(out_root: Path) -> list[dict[str, Any]]:
    stage08_rows = [r for r in read_csv(STAGE08_INVENTORY) if is_march_2024(r)]
    stage09_rows = {r.get("time_tag", ""): r for r in read_csv(STAGE09_INVENTORY)}
    local_runs = {p.name: p for p in RUNS_ROOT.iterdir() if p.is_dir() and re.fullmatch(r"202403\d{2}_\d{4}", p.name)}
    rows: list[dict[str, Any]] = []
    for src in stage08_rows:
        tag = time_tag_from_hour(src.get("nearest_hour", ""))
        run_dir = local_runs.get(tag)
        has_run = run_dir is not None
        has_fused = has_fused_product(run_dir) if run_dir else False
        has_prefusion, pref_sources = has_prefusion_products(run_dir, tag) if run_dir else (False, [])
        metrics_exists = semantic_metrics_path(run_dir, tag).exists() if run_dir else False
        frac = parse_source_fraction(src.get("source_fraction_json"))
        statuses = parse_statuses(src.get("nearest_hour_statuses", ""))
        all_complete = boolish(src.get("nearest_hour_all_27_complete"))
        raw_by_source = {source: statuses.get(source, "") == "PASS" for source in SOURCE_NAMES}
        local_epic_hint = bool(str(src.get("epic_file", "")).strip())
        can_attempt = (not has_fused) and all_complete and local_epic_hint and all(raw_by_source.values())
        reason = ""
        if has_fused:
            reason = "existing fused product already available"
        elif not all_complete:
            reason = "nearest GEO hour is not all-27-core complete"
        elif not local_epic_hint:
            reason = "missing EPIC file path in inventory"
        elif not all(raw_by_source.values()):
            missing = [s for s, ok in raw_by_source.items() if not ok]
            reason = "raw/status not PASS for " + "|".join(missing)
        group = str(src.get("candidate_class", ""))
        delta = safe_float(src.get("nearest_hour_delta_min"), 999.0)
        priority = (10.0 if can_attempt else -100.0) + safe_float(src.get("candidate_score")) - min(delta, 60.0) / 30.0
        row = {
            "sample_id": tag,
            "epic_file": src.get("epic_file", ""),
            "epic_time_utc": src.get("epic_time", ""),
            "nearest_georing_time_utc": src.get("nearest_hour", ""),
            "time_diff_min": src.get("nearest_hour_delta_min", ""),
            "has_existing_stage_run": has_run,
            "has_fused_product": has_fused,
            "has_prefusion_products": has_prefusion,
            "has_semantic_metrics": metrics_exists,
            "has_raw_FY4B": raw_by_source["FY4B"],
            "has_raw_GOES16": raw_by_source["GOES-16"],
            "has_raw_GOES18": raw_by_source["GOES-18"],
            "has_raw_Himawari9": raw_by_source["Himawari-9"],
            "has_raw_Meteosat0deg": raw_by_source["Meteosat-0deg"],
            "has_raw_MeteosatIODC": raw_by_source["Meteosat-IODC"],
            "estimated_dominant_source": src.get("dominant_satellite_estimate", ""),
            "estimated_dominant_source_fraction": src.get("dominant_fraction_estimate", ""),
            "estimated_source_fraction_FY4B": frac["FY4B"],
            "estimated_source_fraction_GOES16": frac["GOES-16"],
            "estimated_source_fraction_GOES18": frac["GOES-18"],
            "estimated_source_fraction_Himawari9": frac["Himawari-9"],
            "estimated_source_fraction_Meteosat0deg": frac["Meteosat-0deg"],
            "estimated_source_fraction_MeteosatIODC": frac["Meteosat-IODC"],
            "candidate_group": group,
            "selection_role": src.get("selection_role", ""),
            "can_attempt_stage_run": can_attempt,
            "cannot_attempt_reason": reason,
            "priority_score": priority,
            "prefusion_sources": "|".join(pref_sources),
            "stage09_availability_class": stage09_rows.get(tag, {}).get("availability_class", ""),
        }
        rows.append(row)

    existing = [r for r in rows if r["has_existing_stage_run"]]
    missing = [r for r in rows if not r["has_existing_stage_run"]]
    write_csv(out_root / "01_inventory_expansion" / "stage09b_full_candidate_inventory_202403.csv", rows)
    write_csv(out_root / "01_inventory_expansion" / "stage09b_existing_stage_run_inventory.csv", existing)
    write_csv(out_root / "01_inventory_expansion" / "stage09b_missing_stage_run_candidates.csv", missing)
    write_csv(out_root / "01_inventory_expansion" / "stage09b_raw_data_availability_by_candidate.csv", rows)
    counts = Counter(str(r["candidate_group"]) for r in rows)
    can = sum(1 for r in rows if r["can_attempt_stage_run"])
    lines = [
        "# Stage 09B March-Only Inventory Expansion",
        "",
        f"- Created UTC: `{utc_now()}`",
        f"- Candidate rows: `{len(rows)}`",
        f"- Existing local Stage runs: `{len(existing)}`",
        f"- Missing local Stage runs: `{len(missing)}`",
        f"- Can attempt new Stage run: `{can}`",
        "",
        "## Candidate Groups",
        "",
    ]
    for key, val in sorted(counts.items()):
        lines.append(f"- `{key}`: `{val}`")
    lines.extend(["", "## Rule", "", "`can_attempt_stage_run=True` only when the nearest GEO hour is all-27-core complete, all source statuses are PASS in the existing inventory, an EPIC path is present, and no fused product already exists."])
    (out_root / "01_inventory_expansion" / "stage09b_inventory_expansion_report.md").write_text("\n".join(lines), encoding="utf-8")
    return rows


def family_key(row: dict[str, Any]) -> str:
    group = str(row.get("candidate_group", ""))
    dom = str(row.get("estimated_dominant_source", ""))
    if "EAST_ASIA" in group or dom in {"FY4B", "Himawari-9"}:
        return "east_asia_fy4b_himawari"
    if dom == "GOES-16":
        return "goes16"
    if dom == "GOES-18":
        return "goes18"
    if dom == "Meteosat-0deg":
        return "meteosat_0deg"
    if dom == "Meteosat-IODC":
        return "meteosat_iodc"
    if "MIXED" in group or "BOUNDARY" in group:
        return "mixed_boundary"
    return "other"


def select_targets(out_root: Path, inventory: list[dict[str, Any]], max_targets: int) -> list[dict[str, Any]]:
    eligible = [r for r in inventory if r["can_attempt_stage_run"]]
    eligible.sort(key=lambda r: (-safe_float(r.get("priority_score")), safe_float(r.get("time_diff_min"), 999.0), str(r.get("sample_id"))))
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for fam in ["east_asia_fy4b_himawari", "goes18", "goes16", "meteosat_0deg", "meteosat_iodc", "mixed_boundary", "other"]:
        for row in eligible:
            if family_key(row) == fam and row["sample_id"] not in seen:
                rr = dict(row)
                rr["selection_family"] = fam
                rr["selection_reason"] = "top priority available March-only candidate in family"
                selected.append(rr)
                seen.add(row["sample_id"])
                break
    for row in eligible:
        if len(selected) >= max_targets:
            break
        if row["sample_id"] in seen:
            continue
        rr = dict(row)
        rr["selection_family"] = family_key(row)
        rr["selection_reason"] = "priority fill"
        selected.append(rr)
        seen.add(row["sample_id"])
    selected = selected[:max_targets]
    write_csv(out_root / "02_target_selection" / "stage09b_overnight_target_list.csv", selected)
    lines = ["# Stage 09B Overnight Target Selection", "", f"- Created UTC: `{utc_now()}`", f"- Eligible candidates: `{len(eligible)}`", f"- Selected targets: `{len(selected)}`", "", "| sample | family | group | delta min | priority |", "|---|---|---|---:|---:|"]
    for r in selected:
        lines.append(f"| `{r['sample_id']}` | {r['selection_family']} | {r['candidate_group']} | {safe_float(r['time_diff_min']):.2f} | {safe_float(r['priority_score']):.2f} |")
    (out_root / "02_target_selection" / "stage09b_target_selection_report.md").write_text("\n".join(lines), encoding="utf-8")
    return selected


def run_attempts(out_root: Path, targets: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    status_path = out_root / "03_stage_run_attempts" / "stage09b_stage_run_attempt_status.csv"
    fields = ["sample_id", "target_time", "epic_time", "candidate_group", "selection_family", "status", "returncode", "elapsed_sec", "report", "log_path", "note"]
    for row in targets[: max(args.max_run_attempts, 0)]:
        tag = str(row["sample_id"])
        run_dir = RUNS_ROOT / tag
        report = run_dir / "reports" / f"single_sample_run_report_{tag}.md"
        metrics = semantic_metrics_path(run_dir, tag)
        if args.skip_existing and has_fused_product(run_dir) and metrics.exists():
            result = {"sample_id": tag, "target_time": row["nearest_georing_time_utc"], "epic_time": row["epic_time_utc"], "candidate_group": row["candidate_group"], "selection_family": row["selection_family"], "status": "SKIPPED_EXISTING", "returncode": 0, "elapsed_sec": 0.0, "report": str(report), "log_path": "", "note": "fused and semantic metrics already exist"}
            rows.append(result)
            write_csv(status_path, rows, fields)
            continue
        log_path = out_root / "logs" / f"run_{tag}.log"
        cmd = [
            "conda",
            "run",
            "-n",
            args.conda_env,
            "python",
            "-B",
            str(SCRIPT_DIR / "run_epic_georing_single_sample.py"),
            "--target-time",
            str(row["nearest_georing_time_utc"]),
            "--time-tag",
            tag,
            "--epic-l2",
            str(row["epic_file"]),
            "--output-root",
            str(run_dir),
            "--base-stage-root",
            str(args.base_stage_root),
        ]
        start = time.time()
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            log.write(f"# Stage09B run {tag}\n# start_utc={utc_now()}\n# command={' '.join(cmd)}\n\n")
            proc = subprocess.run(cmd, cwd=str(SCRIPT_DIR), stdout=log, stderr=subprocess.STDOUT, text=True)
            log.write(f"\n# end_utc={utc_now()}\n# returncode={proc.returncode}\n")
        result = {"sample_id": tag, "target_time": row["nearest_georing_time_utc"], "epic_time": row["epic_time_utc"], "candidate_group": row["candidate_group"], "selection_family": row["selection_family"], "status": "OK" if proc.returncode == 0 else "FAILED", "returncode": proc.returncode, "elapsed_sec": time.time() - start, "report": str(report), "log_path": str(log_path), "note": ""}
        rows.append(result)
        write_csv(status_path, rows, fields)
    if not rows:
        write_csv(status_path, rows, fields)
    return rows


def collect_metrics(out_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for run_dir in sorted(p for p in RUNS_ROOT.iterdir() if p.is_dir() and re.fullmatch(r"202403\d{2}_\d{4}", p.name)):
        tag = run_dir.name
        metrics = semantic_metrics_path(run_dir, tag)
        if not metrics.exists():
            continue
        for row in read_csv(metrics):
            out = {"sample_id": tag, "metrics_path": str(metrics)}
            out.update(row)
            rows.append(out)
    write_csv(out_root / "04_expanded_core_metrics" / "stage09b_expanded_semantic_metrics_long.csv", rows)
    sample: dict[str, dict[str, Any]] = {}
    for row in rows:
        tag = str(row["sample_id"])
        sample.setdefault(tag, {"sample_id": tag})
        prefix = "A" if str(row.get("policy", "")).startswith("A_") else "B" if str(row.get("policy", "")).startswith("B_") else "C"
        for key in ["agreement", "f1", "iou", "precision", "recall", "valid_fraction_of_epic_earth", "epic_cloud_fraction", "geo_cloud_fraction"]:
            sample[tag][f"{prefix}_{key}"] = row.get(key, "")
    summary = list(sample.values())
    for row in summary:
        row["B_minus_A_agreement"] = safe_float(row.get("B_agreement")) - safe_float(row.get("A_agreement"))
    write_csv(out_root / "04_expanded_core_metrics" / "stage09b_sample_level_semantic_summary.csv", summary)
    return summary


def collect_attempt_history(out_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    log_dir = out_root / "logs"
    for log_path in sorted(log_dir.glob("run_*.log")):
        tag = log_path.stem.replace("run_", "")
        run_dir = RUNS_ROOT / tag
        status_rows = read_csv(run_dir / "pipeline_run_status.csv")
        complete = bool(status_rows) and all(str(r.get("status")) == "OK" for r in status_rows)
        last = status_rows[-1] if status_rows else {}
        rows.append(
            {
                "sample_id": tag,
                "status": "OK" if complete else "FAILED_OR_INCOMPLETE",
                "step_count": len(status_rows),
                "last_step": last.get("step", ""),
                "last_step_status": last.get("status", ""),
                "fused_cloud_mask_exists": has_fused_product(run_dir),
                "semantic_metrics_exists": semantic_metrics_path(run_dir, tag).exists(),
                "pipeline_status_csv": str(run_dir / "pipeline_run_status.csv"),
                "log_path": str(log_path),
            }
        )
    write_csv(
        out_root / "03_stage_run_attempts" / "stage09b_stage_run_attempt_history.csv",
        rows,
        ["sample_id", "status", "step_count", "last_step", "last_step_status", "fused_cloud_mask_exists", "semantic_metrics_exists", "pipeline_status_csv", "log_path"],
    )
    return rows


def make_figures(out_root: Path, inventory: list[dict[str, Any]], targets: list[dict[str, Any]], sample_summary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    plot_dir = out_root / "13_summary_figures"
    plot_index: list[dict[str, Any]] = []
    counts = Counter(str(r["candidate_group"]) for r in inventory)
    fig, ax = plt.subplots(figsize=(10, 5), constrained_layout=True)
    labels = list(counts.keys())
    ax.bar(range(len(labels)), [counts[k] for k in labels], color="#386fa4")
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=35, ha="right")
    ax.set_ylabel("Candidate count")
    ax.set_title("Stage09B March Candidate Inventory by Group")
    ax.grid(axis="y", alpha=0.25)
    path = plot_dir / "stage09b_candidate_inventory_by_group.png"
    fig.savefig(path, dpi=170)
    plt.close(fig)
    plot_index.append({"png": str(path), "source_csv": str(out_root / "01_inventory_expansion" / "stage09b_full_candidate_inventory_202403.csv"), "filter": "all rows", "description": "March-only candidate inventory by group"})

    if targets:
        counts = Counter(str(r["selection_family"]) for r in targets)
        fig, ax = plt.subplots(figsize=(8, 4.5), constrained_layout=True)
        labels = list(counts.keys())
        ax.bar(range(len(labels)), [counts[k] for k in labels], color="#7a9e3f")
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=35, ha="right")
        ax.set_ylabel("Selected target count")
        ax.set_title("Stage09B Overnight Targets by Family")
        ax.grid(axis="y", alpha=0.25)
        path = plot_dir / "stage09b_target_selection_by_family.png"
        fig.savefig(path, dpi=170)
        plt.close(fig)
        plot_index.append({"png": str(path), "source_csv": str(out_root / "02_target_selection" / "stage09b_overnight_target_list.csv"), "filter": "all selected targets", "description": "Selected targets by source family"})

    if sample_summary:
        ordered = sorted(sample_summary, key=lambda r: str(r["sample_id"]))
        fig, ax = plt.subplots(figsize=(max(10, 0.75 * len(ordered)), 5), constrained_layout=True)
        x = range(len(ordered))
        ax.bar([i - 0.2 for i in x], [safe_float(r.get("A_agreement")) for r in ordered], width=0.38, label="A inclusive", color="#3b7ea1")
        ax.bar([i + 0.2 for i in x], [safe_float(r.get("B_agreement")) for r in ordered], width=0.38, label="B high-confidence", color="#7a9e3f")
        ax.set_xticks(list(x))
        ax.set_xticklabels([str(r["sample_id"]) for r in ordered], rotation=45, ha="right")
        ax.set_ylim(0, 1)
        ax.set_ylabel("Agreement")
        ax.set_title("Stage09B Expanded Fused vs EPIC Agreement")
        ax.legend()
        ax.grid(axis="y", alpha=0.25)
        path = plot_dir / "stage09b_expanded_agreement_by_sample.png"
        fig.savefig(path, dpi=170)
        plt.close(fig)
        plot_index.append({"png": str(path), "source_csv": str(out_root / "04_expanded_core_metrics" / "stage09b_sample_level_semantic_summary.csv"), "filter": "A/B agreement columns", "description": "Expanded fused-vs-EPIC agreement by sample"})

    write_csv(out_root / "13_summary_figures" / "stage09b_plot_index.csv", plot_index, ["png", "source_csv", "filter", "description"])
    return plot_index


def build_report(out_root: Path, inventory: list[dict[str, Any]], targets: list[dict[str, Any]], attempts: list[dict[str, Any]], sample_summary: list[dict[str, Any]], plots: list[dict[str, Any]]) -> Path:
    can = [r for r in inventory if r["can_attempt_stage_run"]]
    existing = [r for r in inventory if r["has_existing_stage_run"]]
    a_vals = [safe_float(r.get("A_agreement"), math.nan) for r in sample_summary]
    b_vals = [safe_float(r.get("B_agreement"), math.nan) for r in sample_summary]
    a_vals = [v for v in a_vals if math.isfinite(v)]
    b_vals = [v for v in b_vals if math.isfinite(v)]
    lines = [
        "# Stage 09B Full Overnight Diagnostics Report",
        "",
        f"- Created UTC: `{utc_now()}`",
        "- Data scope: local 2024-03 only.",
        "- Network/downloads: not used.",
        "- Fusion v2 / Stage 06 production logic: not modified.",
        "- EPIC is treated as an independent diagnostic reference, not truth.",
        "",
        "## Inventory",
        "",
        f"- March EPIC candidates: `{len(inventory)}`",
        f"- Existing Stage runs: `{len(existing)}`",
        f"- Can attempt new Stage run: `{len(can)}`",
        f"- Selected overnight targets: `{len(targets)}`",
        "",
        "## Run Attempts",
        "",
        f"- Attempted this run: `{len(attempts)}`",
        f"- OK/skipped: `{sum(1 for r in attempts if r['status'] in {'OK', 'SKIPPED_EXISTING'})}`",
        f"- Failed: `{sum(1 for r in attempts if r['status'] == 'FAILED')}`",
        "",
        "## Expanded Metrics",
        "",
        f"- Samples with semantic metrics: `{len(sample_summary)}`",
        f"- Policy A mean agreement: `{sum(a_vals)/len(a_vals):.3f}`" if a_vals else "- Policy A mean agreement: `NA`",
        f"- Policy B mean agreement: `{sum(b_vals)/len(b_vals):.3f}`" if b_vals else "- Policy B mean agreement: `NA`",
        "",
        "## Key CSV Outputs",
        "",
        f"- `{out_root / '00_code_reuse_audit' / 'stage09b_existing_code_reuse_audit.csv'}`",
        f"- `{out_root / '01_inventory_expansion' / 'stage09b_full_candidate_inventory_202403.csv'}`",
        f"- `{out_root / '02_target_selection' / 'stage09b_overnight_target_list.csv'}`",
        f"- `{out_root / '03_stage_run_attempts' / 'stage09b_stage_run_attempt_status.csv'}`",
        f"- `{out_root / '03_stage_run_attempts' / 'stage09b_stage_run_attempt_history.csv'}`",
        f"- `{out_root / '04_expanded_core_metrics' / 'stage09b_sample_level_semantic_summary.csv'}`",
        f"- `{out_root / '13_summary_figures' / 'stage09b_plot_index.csv'}`",
        "",
        "## PNG Traceability",
        "",
    ]
    for row in plots:
        lines.append(f"- `{row['png']}` from `{row['source_csv']}`")
    report = out_root / "reports" / "stage09b_full_overnight_diagnostics_report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Stage 09B full overnight March-only EPIC/GEO-ring diagnostic controller.")
    p.add_argument("--out-root", default=str(OUT_ROOT))
    p.add_argument("--max-targets", type=int, default=12)
    p.add_argument("--max-run-attempts", type=int, default=2)
    p.add_argument("--conda-env", default="pytorch")
    p.add_argument("--base-stage-root", default=str(BASE_STAGE_ROOT))
    p.add_argument("--skip-existing", action="store_true", default=True)
    p.add_argument("--inventory-only", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    out_root = Path(args.out_root)
    setup_dirs(out_root)
    write_code_reuse_audit(out_root)
    inventory = build_inventory(out_root)
    targets = select_targets(out_root, inventory, args.max_targets)
    attempts: list[dict[str, Any]] = []
    if not args.inventory_only:
        if args.max_run_attempts == 0:
            attempts = [dict(r) for r in read_csv(out_root / "03_stage_run_attempts" / "stage09b_stage_run_attempt_status.csv")]
        else:
            attempts = run_attempts(out_root, targets, args)
    else:
        write_csv(out_root / "03_stage_run_attempts" / "stage09b_stage_run_attempt_status.csv", attempts, ["sample_id", "target_time", "epic_time", "candidate_group", "selection_family", "status", "returncode", "elapsed_sec", "report", "log_path", "note"])
    if attempts:
        inventory = build_inventory(out_root)
        targets = select_targets(out_root, inventory, args.max_targets)
    summary = collect_metrics(out_root)
    collect_attempt_history(out_root)
    plots = make_figures(out_root, inventory, targets, summary)
    report = build_report(out_root, inventory, targets, attempts, summary, plots)
    print(f"Stage09B complete: report={report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
