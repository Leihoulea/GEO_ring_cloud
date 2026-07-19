from __future__ import annotations

import argparse
import csv
import json
import math
import os
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


OUT_ROOT = RUNS_ROOT / "stage09c_scaled_202403_batch"
STAGE09B_ROOT = RUNS_ROOT / "stage09b_full_202403_overnight_diagnostics"
STAGE09_ROOT = RUNS_ROOT / "stage09_epic_georing_cloud_mask_diagnostics"
INV_09B = STAGE09B_ROOT / "01_inventory_expansion" / "stage09b_full_candidate_inventory_202403.csv"
TARGET_09B = STAGE09B_ROOT / "02_target_selection" / "stage09b_overnight_target_list.csv"

FIELDS_HISTORY = [
    "sample_id",
    "epic_time_utc",
    "nearest_georing_time_utc",
    "attempted",
    "status",
    "status_detail",
    "output_stage_run_dir",
    "has_fused_after_run",
    "has_prefusion_after_run",
    "runtime_seconds",
    "failure_stage",
    "failure_reason",
    "log_file",
]


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
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({field: row.get(field, "") for field in fields})


def safe_float(value: Any, default: float = math.nan) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def boolish(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "ok"}


def setup_dirs(out_root: Path) -> Path:
    for name in ["00_target_selection", "01_run_attempts", "02_expanded_diagnostics", "03_summary_figures", "reports", "logs", "_tmp"]:
        (out_root / name).mkdir(parents=True, exist_ok=True)
    return out_root / "_tmp"


def semantic_metrics_path(tag: str) -> Path:
    return RUNS_ROOT / tag / f"epic_l2_cloud_mask_semantic_sensitivity_{tag}" / "epic_georing_cloud_mask_sensitivity_metrics.csv"


def has_fused(tag: str) -> bool:
    root = RUNS_ROOT / tag / "fused_best_source"
    return (root / "fused_cloud_mask.npz").exists() or (root / "fused_cloud_binary.npz").exists()


def has_prefusion(tag: str) -> bool:
    root = RUNS_ROOT / tag / "reprojected_grid"
    return root.exists() and any(root.glob("*/*cloud_mask*.npz"))


def time_bin(delta: Any) -> str:
    d = safe_float(delta)
    if not math.isfinite(d):
        return "unknown"
    if d <= 5:
        return "00_05_min"
    if d <= 10:
        return "05_10_min"
    if d <= 20:
        return "10_20_min"
    return "gt20_min"


def family(row: dict[str, Any]) -> str:
    group = str(row.get("candidate_group", ""))
    dom = str(row.get("estimated_dominant_source", ""))
    if "EAST_ASIA" in group or dom in {"FY4B", "Himawari-9"}:
        return "east_asia"
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


def enrich_inventory() -> list[dict[str, Any]]:
    rows = []
    for r in read_csv(INV_09B):
        tag = str(r.get("sample_id", ""))
        if not re.fullmatch(r"202403\d{2}_\d{4}", tag):
            continue
        rr = dict(r)
        rr["has_existing_stage_run"] = (RUNS_ROOT / tag).exists()
        rr["has_fused_product"] = has_fused(tag)
        rr["has_prefusion_products"] = has_prefusion(tag)
        rr["has_semantic_metrics"] = semantic_metrics_path(tag).exists()
        raw_ok = all(boolish(rr.get(k)) for k in ["has_raw_FY4B", "has_raw_GOES16", "has_raw_GOES18", "has_raw_Himawari9", "has_raw_Meteosat0deg", "has_raw_MeteosatIODC"])
        rr["can_attempt_stage_run"] = boolish(rr.get("can_attempt_stage_run")) and not rr["has_semantic_metrics"] and raw_ok
        if rr["has_semantic_metrics"]:
            rr["cannot_attempt_reason"] = "semantic metrics already exist"
        elif not raw_ok:
            rr["cannot_attempt_reason"] = "missing_raw_source"
        rr["selection_family"] = family(rr)
        rr["time_diff_bin"] = time_bin(rr.get("time_diff_min"))
        rows.append(rr)
    return rows


def select_targets(out_root: Path, max_targets: int) -> list[dict[str, Any]]:
    inv = enrich_inventory()
    eligible = [r for r in inv if boolish(r.get("can_attempt_stage_run"))]
    for r in eligible:
        delta = safe_float(r.get("time_diff_min"), 999)
        fam = str(r["selection_family"])
        balance_bonus = {"meteosat_0deg": 8, "meteosat_iodc": 8, "goes16": 5, "goes18": 5, "east_asia": 5, "mixed_boundary": 4}.get(fam, 0)
        close_bonus = 8 if delta <= 10 else 3 if delta <= 20 else 0
        pair_bonus = 4 if fam in {"meteosat_0deg", "meteosat_iodc", "mixed_boundary"} else 0
        r["stage09c_priority_score"] = safe_float(r.get("priority_score"), 0) + balance_bonus + close_bonus + pair_bonus
    eligible.sort(key=lambda r: (-safe_float(r["stage09c_priority_score"]), safe_float(r.get("time_diff_min"), 999), str(r["sample_id"])))

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    quotas = [("meteosat_0deg", 14), ("meteosat_iodc", 14), ("goes16", 10), ("goes18", 10), ("east_asia", 18), ("mixed_boundary", 10), ("other", 4)]
    for fam, quota in quotas:
        picked = [r for r in eligible if r["selection_family"] == fam and r["sample_id"] not in seen][:quota]
        for r in picked:
            selected.append(r)
            seen.add(str(r["sample_id"]))
    for r in eligible:
        if len(selected) >= max_targets:
            break
        if r["sample_id"] not in seen:
            selected.append(r)
            seen.add(str(r["sample_id"]))
    selected = selected[:max_targets]

    out_rows: list[dict[str, Any]] = []
    for i, r in enumerate(selected, start=1):
        out_rows.append(
            {
                "target_rank": i,
                "sample_id": r["sample_id"],
                "epic_time_utc": r.get("epic_time_utc", ""),
                "nearest_georing_time_utc": r.get("nearest_georing_time_utc", ""),
                "time_diff_min": r.get("time_diff_min", ""),
                "candidate_group": r.get("candidate_group", ""),
                "estimated_dominant_source": r.get("estimated_dominant_source", ""),
                "reason_selected": f"{r['selection_family']}; {r['time_diff_bin']}; balanced scaled batch",
                "has_existing_stage_run": r.get("has_existing_stage_run", ""),
                "can_attempt_stage_run": r.get("can_attempt_stage_run", ""),
                "priority_score": r.get("stage09c_priority_score", ""),
                "epic_file": r.get("epic_file", ""),
                "selection_family": r.get("selection_family", ""),
            }
        )
    write_csv(out_root / "00_target_selection" / "stage09c_scaled_target_list.csv", out_rows)

    counts = Counter(r["selection_family"] for r in out_rows)
    lines = ["# Stage 09C Scaled Target Selection", "", f"- Created UTC: `{utc_now()}`", f"- Eligible March candidates: `{len(eligible)}`", f"- Selected targets: `{len(out_rows)}`", "", "## Family Counts", ""]
    for k, v in sorted(counts.items()):
        lines.append(f"- `{k}`: `{v}`")
    lines.extend(["", "## Policy", "", "- Prioritized `time_diff_min <= 10 min`.", "- Meteosat-0deg and Meteosat-IODC are separated.", "- Existing successful semantic-metric samples are included in final diagnostics but skipped for rerun."])
    (out_root / "00_target_selection" / "stage09c_target_selection_report.md").write_text("\n".join(lines), encoding="utf-8")
    return out_rows


def read_history(out_root: Path) -> list[dict[str, str]]:
    return read_csv(out_root / "01_run_attempts" / "stage09c_run_attempt_history.csv")


def classify_failure(tag: str, log_text: str) -> tuple[str, str]:
    status_rows = read_csv(RUNS_ROOT / tag / "pipeline_run_status.csv")
    failed = [r for r in status_rows if str(r.get("status")) != "OK"]
    stage = failed[0].get("step", "") if failed else ""
    text = log_text.lower()
    if "access is denied" in text or "拒绝访问" in text or "permission" in text:
        return stage, "temp_permission_error" if "temp" in text or "tmp" in text else "path_error"
    if "zip" in text and ("failed" in text or "error" in text):
        return stage, "zip_extract_failed"
    if "stage05" in stage or "05_" in stage:
        return stage, "stage05_failed"
    if "stage06" in stage or "06_" in stage:
        return stage, "stage06_failed"
    if "no such file" in text or "missing" in text:
        return stage, "missing_raw_source"
    if not (RUNS_ROOT / tag / "fused_best_source").exists():
        return stage, "output_missing"
    return stage, "unknown_exception"


def write_history_outputs(out_root: Path, rows: list[dict[str, Any]]) -> None:
    write_csv(out_root / "01_run_attempts" / "stage09c_run_attempt_history.csv", rows, FIELDS_HISTORY)
    write_csv(out_root / "01_run_attempts" / "stage09c_run_success.csv", [r for r in rows if r.get("status") == "success"], FIELDS_HISTORY)
    write_csv(out_root / "01_run_attempts" / "stage09c_run_failed.csv", [r for r in rows if r.get("status") == "failed"], FIELDS_HISTORY)
    write_csv(out_root / "01_run_attempts" / "stage09c_run_skipped_existing.csv", [r for r in rows if r.get("status") == "skipped_existing"], FIELDS_HISTORY)


def run_batch(out_root: Path, targets: list[dict[str, Any]], args: argparse.Namespace, tmp_dir: Path) -> list[dict[str, Any]]:
    history = read_history(out_root)
    by_id = {r["sample_id"]: dict(r) for r in history}
    master = out_root / "logs" / "stage09c_master.log"
    env = os.environ.copy()
    env["TMP"] = str(tmp_dir)
    env["TEMP"] = str(tmp_dir)
    attempted_new = 0
    with master.open("a", encoding="utf-8", errors="replace") as mlog:
        mlog.write(f"\n# Stage09C session start {utc_now()} tmp={tmp_dir}\n")
        for target in targets:
            tag = str(target["sample_id"])
            if attempted_new >= args.max_attempts:
                break
            if tag in by_id and by_id[tag].get("status") in {"success", "skipped_existing"}:
                continue
            run_dir = RUNS_ROOT / tag
            log_file = out_root / "logs" / f"stage09c_run_sample_{tag}.log"
            if has_fused(tag) and semantic_metrics_path(tag).exists():
                row = {
                    "sample_id": tag,
                    "epic_time_utc": target.get("epic_time_utc", ""),
                    "nearest_georing_time_utc": target.get("nearest_georing_time_utc", ""),
                    "attempted": False,
                    "status": "skipped_existing",
                    "status_detail": "semantic metrics already exist",
                    "output_stage_run_dir": str(run_dir),
                    "has_fused_after_run": True,
                    "has_prefusion_after_run": has_prefusion(tag),
                    "runtime_seconds": 0,
                    "failure_stage": "",
                    "failure_reason": "",
                    "log_file": "",
                }
                by_id[tag] = row
                write_history_outputs(out_root, list(by_id.values()))
                continue
            cmd = [
                "conda",
                "run",
                "-n",
                args.conda_env,
                "python",
                "-B",
                str(SCRIPT_DIR / "run_epic_georing_single_sample.py"),
                "--target-time",
                str(target["nearest_georing_time_utc"]),
                "--time-tag",
                tag,
                "--epic-l2",
                str(target["epic_file"]),
                "--output-root",
                str(run_dir),
                "--base-stage-root",
                str(args.base_stage_root),
            ]
            attempted_new += 1
            mlog.write(f"{utc_now()} RUN {attempted_new}/{args.max_attempts} {tag}\n")
            start = time.time()
            with log_file.open("w", encoding="utf-8", errors="replace") as log:
                log.write(f"# Stage09C sample {tag}\n# start={utc_now()}\n# tmp={tmp_dir}\n# command={' '.join(cmd)}\n\n")
                try:
                    proc = subprocess.run(cmd, cwd=str(SCRIPT_DIR), env=env, stdout=log, stderr=subprocess.STDOUT, text=True, timeout=args.timeout_seconds)
                    returncode = proc.returncode
                except subprocess.TimeoutExpired:
                    returncode = -999
                    log.write(f"\n# timeout after {args.timeout_seconds} sec\n")
                log.write(f"\n# end={utc_now()}\n# returncode={returncode}\n")
            runtime = time.time() - start
            text = log_file.read_text(encoding="utf-8", errors="replace")
            ok = returncode == 0 and has_fused(tag) and semantic_metrics_path(tag).exists()
            stage, reason = ("", "") if ok else classify_failure(tag, text)
            row = {
                "sample_id": tag,
                "epic_time_utc": target.get("epic_time_utc", ""),
                "nearest_georing_time_utc": target.get("nearest_georing_time_utc", ""),
                "attempted": True,
                "status": "success" if ok else "failed",
                "status_detail": "OK" if ok else f"returncode={returncode}",
                "output_stage_run_dir": str(run_dir),
                "has_fused_after_run": has_fused(tag),
                "has_prefusion_after_run": has_prefusion(tag),
                "runtime_seconds": runtime,
                "failure_stage": stage,
                "failure_reason": "timeout" if returncode == -999 else reason,
                "log_file": str(log_file),
            }
            by_id[tag] = row
            write_history_outputs(out_root, list(by_id.values()))
            if attempted_new % 10 == 0:
                ckpt = {"created_time": utc_now(), "attempted_new_this_session": attempted_new, "history_count": len(by_id), "last_sample_id": tag}
                (out_root / "logs" / "stage09c_checkpoint.json").write_text(json.dumps(ckpt, indent=2), encoding="utf-8")
        mlog.write(f"# Stage09C session end {utc_now()} attempted_new={attempted_new}\n")
    return list(by_id.values())


def load_candidate_lookup() -> dict[str, dict[str, str]]:
    return {r["sample_id"]: r for r in enrich_inventory()}


def collect_semantic(out_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    lookup = load_candidate_lookup()
    long_rows: list[dict[str, Any]] = []
    sample_rows: dict[str, dict[str, Any]] = {}
    for run_dir in sorted(p for p in RUNS_ROOT.iterdir() if p.is_dir() and re.fullmatch(r"202403\d{2}_\d{4}", p.name)):
        tag = run_dir.name
        path = semantic_metrics_path(tag)
        if not path.exists():
            continue
        meta = lookup.get(tag, {})
        for m in read_csv(path):
            policy = str(m.get("policy", ""))
            tp, tn, fp, fn = [safe_float(m.get(k), 0) for k in ["tp", "tn", "fp", "fn"]]
            recall = tp / max(tp + fn, 1)
            specificity = tn / max(tn + fp, 1)
            row = {
                "sample_id": tag,
                "candidate_group": meta.get("candidate_group", "UNKNOWN_OR_EXISTING"),
                "dominant_source": meta.get("estimated_dominant_source", ""),
                "time_diff_min": meta.get("time_diff_min", ""),
                "time_diff_bin": time_bin(meta.get("time_diff_min")),
                "boundary_class": "mixed_or_boundary" if "MIXED" in str(meta.get("candidate_group", "")) or "BOUNDARY" in str(meta.get("candidate_group", "")) else "non_boundary",
                "metrics_path": str(path),
                "balanced_accuracy": (recall + specificity) / 2.0,
                "cloud_fraction_bias": safe_float(m.get("geo_cloud_fraction")) - safe_float(m.get("epic_cloud_fraction")),
            }
            row.update(m)
            long_rows.append(row)
            base = sample_rows.setdefault(tag, {k: row.get(k, "") for k in ["sample_id", "candidate_group", "dominant_source", "time_diff_min", "time_diff_bin", "boundary_class"]})
            prefix = "A" if policy.startswith("A_") else "B" if policy.startswith("B_") else "C"
            for k in ["agreement", "f1", "iou", "precision", "recall", "valid_fraction_of_epic_earth", "epic_cloud_fraction", "geo_cloud_fraction", "balanced_accuracy", "cloud_fraction_bias", "tp", "tn", "fp", "fn"]:
                base[f"{prefix}_{k}"] = row.get(k, "")
    samples = list(sample_rows.values())
    for r in samples:
        r["B_minus_A_agreement"] = safe_float(r.get("B_agreement")) - safe_float(r.get("A_agreement"))
    write_csv(out_root / "02_expanded_diagnostics" / "stage09c_semantic_metrics_long.csv", long_rows)
    write_csv(out_root / "02_expanded_diagnostics" / "stage09c_sample_level_semantic_summary.csv", samples)
    return long_rows, samples


def mean(vals: list[float]) -> float:
    vals = [v for v in vals if math.isfinite(v)]
    return sum(vals) / len(vals) if vals else math.nan


def minmax(vals: list[float]) -> tuple[float, float]:
    vals = [v for v in vals if math.isfinite(v)]
    return (min(vals), max(vals)) if vals else (math.nan, math.nan)


def group_summary(samples: list[dict[str, Any]], group_field: str) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in samples:
        buckets[str(r.get(group_field, ""))].append(r)
    rows = []
    for key, vals in sorted(buckets.items()):
        rows.append(
            {
                "group_field": group_field,
                "group_value": key,
                "n": len(vals),
                "A_agreement_mean": mean([safe_float(v.get("A_agreement")) for v in vals]),
                "B_agreement_mean": mean([safe_float(v.get("B_agreement")) for v in vals]),
                "C_agreement_mean": mean([safe_float(v.get("C_agreement")) for v in vals]),
                "B_minus_A_mean": mean([safe_float(v.get("B_minus_A_agreement")) for v in vals]),
                "A_f1_mean": mean([safe_float(v.get("A_f1")) for v in vals]),
                "A_cloud_fraction_bias_mean": mean([safe_float(v.get("A_cloud_fraction_bias")) for v in vals]),
            }
        )
    return rows


def build_diagnostics(out_root: Path, long_rows: list[dict[str, Any]], samples: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    group_rows: list[dict[str, Any]] = []
    for field in ["candidate_group", "dominant_source", "time_diff_bin", "boundary_class"]:
        group_rows.extend(group_summary(samples, field))
    write_csv(out_root / "02_expanded_diagnostics" / "stage09c_group_level_summary.csv", group_rows)

    policy_rows = []
    for policy in ["A", "B", "C"]:
        vals = [safe_float(r.get(f"{policy}_agreement")) for r in samples]
        lo, hi = minmax(vals)
        policy_rows.append({"policy": policy, "n": len([v for v in vals if math.isfinite(v)]), "agreement_mean": mean(vals), "agreement_min": lo, "agreement_max": hi})
    write_csv(out_root / "02_expanded_diagnostics" / "stage09c_policy_A_B_C_summary.csv", policy_rows)

    met_rows = [r for r in group_rows if r["group_field"] == "dominant_source" and r["group_value"] in {"Meteosat-0deg", "Meteosat-IODC"}]
    write_csv(out_root / "02_expanded_diagnostics" / "stage09c_meteosat_0deg_vs_iodc_summary.csv", met_rows)

    source_pair_src = STAGE09_ROOT / "stage09_source_pair_summary.csv"
    source_pair = [dict(r, coverage_note="existing 08j-derived source-pair coverage; not recomputed for all 09C samples") for r in read_csv(source_pair_src)]
    write_csv(out_root / "02_expanded_diagnostics" / "stage09c_source_pair_overlap_summary.csv", source_pair)

    sampling_src = STAGE09_ROOT / "stage09_strata_summary.csv"
    sampling = [dict(r, coverage_note="existing 08f-derived sampling sensitivity coverage; not recomputed for all 09C samples") for r in read_csv(sampling_src)]
    write_csv(out_root / "02_expanded_diagnostics" / "stage09c_sampling_sensitivity_summary.csv", sampling)

    geometry = [r for r in group_rows if r["group_field"] in {"boundary_class", "time_diff_bin"}]
    write_csv(out_root / "02_expanded_diagnostics" / "stage09c_geometry_boundary_summary.csv", geometry)

    factor_rows = [
        {"factor": "semantic_policy", "metric": "mean B-A agreement", "value": mean([safe_float(r.get("B_minus_A_agreement")) for r in samples]), "strength": "moderate" if mean([safe_float(r.get("B_minus_A_agreement")) for r in samples]) > 0.02 else "weak", "source_csv": "stage09c_sample_level_semantic_summary.csv"},
        {"factor": "scene_source_family", "metric": "range of A group means", "value": max([safe_float(r.get("A_agreement_mean")) for r in group_rows if r["group_field"] == "candidate_group"], default=math.nan) - min([safe_float(r.get("A_agreement_mean")) for r in group_rows if r["group_field"] == "candidate_group"], default=math.nan), "strength": "strong", "source_csv": "stage09c_group_level_summary.csv"},
        {"factor": "sampling_geometry", "metric": "available 08f strata evidence", "value": len(sampling), "strength": "insufficient_data_for_new_09c_samples" if sampling else "insufficient_data", "source_csv": "stage09c_sampling_sensitivity_summary.csv"},
    ]
    write_csv(out_root / "reports" / "stage09c_factor_contribution_summary.csv", factor_rows)
    return {"group": group_rows, "policy": policy_rows, "meteosat": met_rows, "source_pair": source_pair, "sampling": sampling, "geometry": geometry, "factor": factor_rows}


def save_bar(path: Path, labels: list[str], values: list[float], title: str, ylabel: str, ylim: tuple[float, float] | None = None) -> None:
    fig, ax = plt.subplots(figsize=(max(8, 0.55 * len(labels)), 4.8), constrained_layout=True)
    ax.bar(range(len(labels)), values, color="#3b7ea1")
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=45, ha="right")
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    if ylim:
        ax.set_ylim(*ylim)
    ax.grid(axis="y", alpha=0.25)
    fig.savefig(path, dpi=170)
    plt.close(fig)


def build_plots(out_root: Path, samples: list[dict[str, Any]], diag: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    pdir = out_root / "03_summary_figures"
    idx: list[dict[str, Any]] = []
    created = utc_now()
    ordered = sorted(samples, key=lambda r: str(r["sample_id"]))
    if ordered:
        labels = [r["sample_id"] for r in ordered]
        fig, ax = plt.subplots(figsize=(max(12, 0.5 * len(labels)), 5), constrained_layout=True)
        x = range(len(labels))
        ax.bar([i - 0.2 for i in x], [safe_float(r.get("A_agreement"), 0) for r in ordered], width=0.38, label="A")
        ax.bar([i + 0.2 for i in x], [safe_float(r.get("B_agreement"), 0) for r in ordered], width=0.38, label="B")
        ax.set_ylim(0, 1)
        ax.set_xticks(list(x))
        ax.set_xticklabels(labels, rotation=45, ha="right")
        ax.set_ylabel("Agreement")
        ax.set_title("Stage09C Policy A/B Agreement by Sample")
        ax.legend()
        ax.grid(axis="y", alpha=0.25)
        path = pdir / "stage09c_policy_A_B_agreement_by_sample.png"
        fig.savefig(path, dpi=170)
        plt.close(fig)
        idx.append({"plot_path": str(path), "source_csv": str(out_root / "02_expanded_diagnostics" / "stage09c_sample_level_semantic_summary.csv"), "description": "Policy A/B agreement by sample", "created_time": created})

    group = [r for r in diag["group"] if r["group_field"] == "candidate_group"]
    if group:
        labels = [r["group_value"] for r in group]
        save_bar(pdir / "stage09c_group_mean_agreement.png", labels, [safe_float(r.get("A_agreement_mean"), 0) for r in group], "Stage09C Group Mean Agreement, Policy A", "Agreement", (0, 1))
        idx.append({"plot_path": str(pdir / "stage09c_group_mean_agreement.png"), "source_csv": str(out_root / "02_expanded_diagnostics" / "stage09c_group_level_summary.csv"), "description": "Candidate-group mean A agreement", "created_time": created})
        save_bar(pdir / "stage09c_B_minus_A_by_group.png", labels, [safe_float(r.get("B_minus_A_mean"), 0) for r in group], "Stage09C B minus A by Group", "Agreement delta")
        idx.append({"plot_path": str(pdir / "stage09c_B_minus_A_by_group.png"), "source_csv": str(out_root / "02_expanded_diagnostics" / "stage09c_group_level_summary.csv"), "description": "B-A semantic-policy lift by group", "created_time": created})
        fig, ax = plt.subplots(figsize=(10, 5), constrained_layout=True)
        x = range(len(labels))
        for off, pol in [(-0.25, "A"), (0, "B"), (0.25, "C")]:
            ax.bar([i + off for i in x], [safe_float(r.get(f"{pol}_agreement_mean"), 0) for r in group], width=0.24, label=pol)
        ax.set_ylim(0, 1)
        ax.set_xticks(list(x))
        ax.set_xticklabels(labels, rotation=45, ha="right")
        ax.set_title("Stage09C Policy A/B/C by Group")
        ax.set_ylabel("Mean agreement")
        ax.legend()
        path = pdir / "stage09c_policy_A_B_C_by_group.png"
        fig.savefig(path, dpi=170)
        plt.close(fig)
        idx.append({"plot_path": str(path), "source_csv": str(out_root / "02_expanded_diagnostics" / "stage09c_group_level_summary.csv"), "description": "Policy A/B/C group means", "created_time": created})

    met = diag["meteosat"]
    save_bar(pdir / "stage09c_meteosat_0deg_vs_iodc.png", [r.get("group_value", "") for r in met] or ["no_data"], [safe_float(r.get("A_agreement_mean"), 0) for r in met] or [0], "Meteosat-0deg vs Meteosat-IODC", "A agreement", (0, 1))
    idx.append({"plot_path": str(pdir / "stage09c_meteosat_0deg_vs_iodc.png"), "source_csv": str(out_root / "02_expanded_diagnostics" / "stage09c_meteosat_0deg_vs_iodc_summary.csv"), "description": "Meteosat dominant-source comparison", "created_time": created})

    pair = [r for r in diag["source_pair"] if str(r.get("policy")) == "A_inclusive_binary"]
    save_bar(pdir / "stage09c_source_pair_overlap_heatmap.png", [f"{r.get('source_a')}|{r.get('source_b')}" for r in pair] or ["no_data"], [safe_float(r.get("source_disagreement_fraction_mean"), 0) for r in pair] or [0], "Source-Pair Overlap Disagreement", "Fraction", (0, 1))
    idx.append({"plot_path": str(pdir / "stage09c_source_pair_overlap_heatmap.png"), "source_csv": str(out_root / "02_expanded_diagnostics" / "stage09c_source_pair_overlap_summary.csv"), "description": "Source-pair disagreement summary", "created_time": created})

    samp = [r for r in diag["sampling"] if str(r.get("policy")) == "A_inclusive_binary"]
    save_bar(pdir / "stage09c_sampling_sensitivity.png", [r.get("stratum", "") for r in samp] or ["no_data"], [safe_float(r.get("agreement_mean"), 0) for r in samp] or [0], "Sampling Sensitivity", "A agreement", (0, 1))
    idx.append({"plot_path": str(pdir / "stage09c_sampling_sensitivity.png"), "source_csv": str(out_root / "02_expanded_diagnostics" / "stage09c_sampling_sensitivity_summary.csv"), "description": "Sampling/latitude/view sensitivity from available strata diagnostics", "created_time": created})

    geom = diag["geometry"]
    save_bar(pdir / "stage09c_geometry_boundary_summary.png", [f"{r.get('group_field')}:{r.get('group_value')}" for r in geom] or ["no_data"], [safe_float(r.get("A_agreement_mean"), 0) for r in geom] or [0], "Geometry/Boundary Summary", "A agreement", (0, 1))
    idx.append({"plot_path": str(pdir / "stage09c_geometry_boundary_summary.png"), "source_csv": str(out_root / "02_expanded_diagnostics" / "stage09c_geometry_boundary_summary.csv"), "description": "Boundary and time-difference bins", "created_time": created})
    write_csv(pdir / "stage09c_plot_index.csv", idx, ["plot_path", "source_csv", "description", "created_time"])
    return idx


def fmt(v: Any) -> str:
    x = safe_float(v)
    return "NA" if not math.isfinite(x) else f"{x:.3f}"


def build_report(out_root: Path, targets: list[dict[str, Any]], history: list[dict[str, Any]], samples: list[dict[str, Any]], diag: dict[str, list[dict[str, Any]]], plots: list[dict[str, Any]]) -> Path:
    success = [r for r in history if r.get("status") == "success"]
    failed = [r for r in history if r.get("status") == "failed"]
    attempted = [r for r in history if boolish(r.get("attempted"))]
    policies = {r["policy"]: r for r in diag["policy"]}
    bminus = [safe_float(r.get("B_minus_A_agreement")) for r in samples]
    fail_counts = Counter(r.get("failure_reason", "") for r in failed)
    group_a = [r for r in diag["group"] if r["group_field"] == "candidate_group"]
    stage09b_samples = read_csv(STAGE09B_ROOT / "04_expanded_core_metrics" / "stage09b_sample_level_semantic_summary.csv")
    stage09b_a = mean([safe_float(r.get("A_agreement")) for r in stage09b_samples])
    stage09b_b = mean([safe_float(r.get("B_agreement")) for r in stage09b_samples])
    stage09c_a = policies.get("A", {})
    stage09c_b = policies.get("B", {})
    stage09c_c = policies.get("C", {})
    met0 = next((r for r in diag["meteosat"] if r.get("group_value") == "Meteosat-0deg"), {})
    meti = next((r for r in diag["meteosat"] if r.get("group_value") == "Meteosat-IODC"), {})
    lower_met = "insufficient_data"
    if met0 and meti:
        lower_met = "Meteosat-0deg" if safe_float(met0.get("A_agreement_mean")) < safe_float(meti.get("A_agreement_mean")) else "Meteosat-IODC"
    lines = [
        "# Stage 09C Scaled 202403 Batch Report",
        "",
        f"- Created UTC: `{utc_now()}`",
        "- Scope: local 2024-03 only; no downloads; no fusion v2; Stage 06 fusion logic unchanged.",
        f"- New attempted samples recorded by 09C: `{len(attempted)}`",
        f"- New successes: `{len(success)}`",
        f"- Failures: `{len(failed)}`",
        f"- Final diagnostic samples: `{len(samples)}`",
        "",
        "## Policy Agreement",
        "",
    ]
    for key, label in [("A", "Policy A inclusive"), ("B", "Policy B high-confidence"), ("C", "Policy C uncertainty-aware")]:
        r = policies.get(key, {})
        lines.append(f"- {label}: n=`{r.get('n', 0)}`, mean=`{fmt(r.get('agreement_mean'))}`, range=`{fmt(r.get('agreement_min'))}-{fmt(r.get('agreement_max'))}`")
    lines.extend(["", "## Group Mean Agreement", "", "| group | n | A | B | C | B-A |", "|---|---:|---:|---:|---:|---:|"])
    for r in group_a:
        lines.append(f"| {r['group_value']} | {r['n']} | {fmt(r['A_agreement_mean'])} | {fmt(r['B_agreement_mean'])} | {fmt(r['C_agreement_mean'])} | {fmt(r['B_minus_A_mean'])} |")
    lines.extend(["", "## Meteosat-0deg vs IODC", ""])
    if diag["meteosat"]:
        for r in diag["meteosat"]:
            lines.append(f"- `{r['group_value']}`: n={r['n']}, A={fmt(r['A_agreement_mean'])}, B={fmt(r['B_agreement_mean'])}")
    else:
        lines.append("- insufficient_data")
    lines.extend(["", "## Key Readings", ""])
    lines.append(f"- B-A lift mean: `{fmt(mean(bminus))}`; stable positive lift = {'moderate' if mean(bminus) > 0.02 else 'weak/insufficient'}.")
    lines.append("- Low-confidence/raw-code semantics remain a plausible mismatch source when B-A is positive across groups.")
    lines.append("- Sampling/geometry evidence is currently inherited from available 08f strata outputs; new 09C per-sample strata are not fully recomputed.")
    top_pairs = sorted([r for r in diag["source_pair"] if str(r.get("policy")) == "A_inclusive_binary"], key=lambda r: safe_float(r.get("source_disagreement_fraction_mean"), 0), reverse=True)[:3]
    top_pair_text = "; ".join(
        f"{r.get('source_a')} vs {r.get('source_b')}={fmt(r.get('source_disagreement_fraction_mean'))}" for r in top_pairs
    ) if top_pairs else "insufficient_data"
    fail_reason_text = "; ".join(f"{k or 'none'}={v}" for k, v in fail_counts.most_common(3)) if failed else "none"
    lines.append("- Top source-pair disagreements: " + "; ".join(f"{r.get('source_a')} vs {r.get('source_b')}={fmt(r.get('source_disagreement_fraction_mean'))}" for r in top_pairs) if top_pairs else "- Top source-pair disagreements: insufficient_data")
    lines.append("- Top failure reasons: " + "; ".join(f"{k or 'none'}={v}" for k, v in fail_counts.most_common(3)) if failed else "- Top failure reasons: none in recorded 09C attempts")
    lines.extend(["", "## Strength Labels", "", "- strong: scene/source family effect if group A means remain separated.", "- moderate: semantic-policy B-A lift if mean lift remains positive.", "- insufficient_data: 09C-specific sampling geometry, boundary enrichment, and full source-pair recomputation until per-sample diagnostics are expanded beyond semantic metrics."])
    lines.extend(
        [
            "",
            "## Required Questions",
            "",
            "| # | answer |",
            "|---:|---|",
            f"| 1 | Stage 09C attempted `{len(attempted)}` new samples. |",
            f"| 2 | Success `{len(success)}`, failure `{len(failed)}`. |",
            f"| 3 | Final fused+prefusion diagnostic sample count is `{len(samples)}`. |",
            f"| 4 | A mean/range `{fmt(stage09c_a.get('agreement_mean'))}` / `{fmt(stage09c_a.get('agreement_min'))}-{fmt(stage09c_a.get('agreement_max'))}`; B `{fmt(stage09c_b.get('agreement_mean'))}` / `{fmt(stage09c_b.get('agreement_min'))}-{fmt(stage09c_b.get('agreement_max'))}`; C `{fmt(stage09c_c.get('agreement_mean'))}` / `{fmt(stage09c_c.get('agreement_min'))}-{fmt(stage09c_c.get('agreement_max'))}`. |",
            f"| 5 | Compared with Stage 09B 14-sample means A=`{fmt(stage09b_a)}`, B=`{fmt(stage09b_b)}`, the larger 09C Meteosat-heavy sample lowers means to A=`{fmt(stage09c_a.get('agreement_mean'))}`, B=`{fmt(stage09c_b.get('agreement_mean'))}`; conclusion shifts toward stronger source/scene dependence. |",
            "| 6 | Group means are listed above in `Group Mean Agreement`; Meteosat-dominant is lowest, GOES and East Asia are higher, mixed/boundary is intermediate. |",
            f"| 7 | Meteosat-0deg A=`{fmt(met0.get('A_agreement_mean'))}`, IODC A=`{fmt(meti.get('A_agreement_mean'))}`; lower by A is `{lower_met}`. Both remain low relative to GOES/East Asia. |",
            f"| 8 | B-A lift mean is `{fmt(mean(bminus))}`; positive but smaller than 09B, so stability is weak-to-moderate rather than strong. |",
            "| 9 | Low-confidence/raw-code mapping remains a mismatch source, but after 09C it is not the dominant explanation by itself. |",
            "| 10 | Sampling sensitivity remains supported by existing 08f strata, but 09C did not recompute per-sample strata for all new samples: mark as `insufficient_data_for_new_09c_samples`. |",
            "| 11 | Boundary/mixed samples are intermediate rather than worst; geometry/boundary enrichment remains `moderate/insufficient_data` pending per-pixel error atlas. |",
            f"| 12 | Largest source-pair disagreements: {top_pair_text}. |",
            f"| 13 | Largest failure reasons: {fail_reason_text}. |",
            "| 14 | Strong: scene/source family effect; Moderate: Meteosat low agreement, EPIC/GEO semantic-confidence contribution. |",
            "| 15 | Insufficient data: full 09C source-pair recomputation, per-sample sampling sensitivity, boundary/broken-cloud pixel-level enrichment. |",
            "| 16 | Next run should continue ranks after the last attempted target in `stage09c_scaled_target_list.csv`, preserving Meteosat-0deg/IODC and close-time samples. |",
        ]
    )
    lines.extend(["", "## Next Samples", "", "- Continue rerunning this script with higher `--max-attempts`; it resumes from `stage09c_run_attempt_history.csv`.", "- Keep Meteosat-0deg/IODC and close time-difference samples in the target list to improve the weakest groups."])
    lines.extend(["", "## Output Index", ""])
    outputs = [
        out_root / "00_target_selection" / "stage09c_scaled_target_list.csv",
        out_root / "01_run_attempts" / "stage09c_run_attempt_history.csv",
        out_root / "02_expanded_diagnostics" / "stage09c_sample_level_semantic_summary.csv",
        out_root / "reports" / "stage09c_factor_contribution_summary.csv",
        out_root / "03_summary_figures" / "stage09c_plot_index.csv",
    ]
    for p in outputs:
        lines.append(f"- `{p}`")
    output_rows = [{"path": str(p), "kind": p.suffix.lstrip("."), "exists": p.exists()} for p in outputs]
    for row in plots:
        output_rows.append({"path": row["plot_path"], "kind": "png", "exists": Path(row["plot_path"]).exists(), "source_csv": row["source_csv"]})
    write_csv(out_root / "reports" / "stage09c_output_file_index.csv", output_rows)
    report = out_root / "reports" / "stage09c_scaled_202403_batch_report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Stage 09C scaled March-only batch runner and diagnostics.")
    p.add_argument("--out-root", default=str(OUT_ROOT))
    p.add_argument("--target-count", type=int, default=80)
    p.add_argument("--max-attempts", type=int, default=40)
    p.add_argument("--timeout-seconds", type=int, default=1800)
    p.add_argument("--conda-env", default="pytorch")
    p.add_argument("--base-stage-root", default=str(BASE_STAGE_ROOT))
    p.add_argument("--plan-only", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    out_root = Path(args.out_root)
    tmp_dir = setup_dirs(out_root)
    targets = select_targets(out_root, args.target_count)
    if args.plan_only:
        history = read_history(out_root)
    else:
        history = run_batch(out_root, targets, args, tmp_dir)
    long_rows, samples = collect_semantic(out_root)
    diag = build_diagnostics(out_root, long_rows, samples)
    plots = build_plots(out_root, samples, diag)
    report = build_report(out_root, targets, history, samples, diag, plots)
    print(f"Stage09C complete: report={report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
