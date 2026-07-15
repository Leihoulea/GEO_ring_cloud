from __future__ import annotations

import argparse
import csv
import json
import math
import re
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from geo_ring_cloud_run_discovery import discover_run_dirs as discover_profile_run_dirs, run_time_tag

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


RUNS_ROOT = Path(r"D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs")
DEFAULT_OUT_DIR = RUNS_ROOT / "stage09_epic_georing_cloud_mask_diagnostics"
SELECTION_INVENTORY = RUNS_ROOT / "epic_202403_target_selection" / "epic_202403_geo_source_candidate_inventory.csv"
MULTISAMPLE_DIR = RUNS_ROOT / "epic_202403_multisample_summary"
GEOM_PREFUSION_DIR = MULTISAMPLE_DIR / "geometry_prefusion_diagnostics"
TIME_OFFSET_PAIR_DIR = RUNS_ROOT / "epic_202403_meteosat_time_offset_control" / "prefusion_source_pair_overlap_diagnostics"

SOURCE_PRODUCTS = {
    "FY4B": "CLM",
    "GOES-16": "ACMF",
    "GOES-18": "ACMF",
    "Himawari-9": "CMSK",
    "Meteosat-0deg": "CLM",
    "Meteosat-IODC": "CLM",
}

POLICY_LABELS = {
    "A_inclusive_binary": "A inclusive binary",
    "B_high_confidence_only": "B high confidence",
    "C_uncertainty_aware_3class": "C uncertainty-aware",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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
            w.writerow({k: row.get(k, "") for k in fields})


def safe_float(value: Any, default: float = math.nan) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def boolish(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "ok"}


def time_tag_from_hour(value: str) -> str:
    if not value:
        return ""
    return value[0:13].replace("-", "").replace("T", "_") + "00"


def tag_to_target_time(tag: str) -> str:
    m = re.fullmatch(r"(20\d{6})_(\d{4})", tag)
    if not m:
        return ""
    dt = datetime.strptime("".join(m.groups()), "%Y%m%d%H%M")
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def parse_source_fraction(value: Any) -> dict[str, float]:
    try:
        data = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return {str(k): safe_float(v, 0.0) for k, v in data.items()}


def discover_run_dirs(runs_root: Path) -> list[Path]:
    return discover_profile_run_dirs(runs_root, os.environ.get("GEO_RING_SOURCE_PROFILE", "operational_baseline"))


def prefusion_sources_for_run(run_dir: Path, tag: str) -> tuple[list[str], list[str]]:
    sources: list[str] = []
    files: list[str] = []
    for source, product in SOURCE_PRODUCTS.items():
        source_dir = run_dir / "reprojected_grid" / source
        hits = list(source_dir.glob(f"{source}_{product}_cloud_mask_grid_{tag}.npz"))
        if not hits:
            hits = list(source_dir.glob(f"*cloud_mask*{tag}.npz"))
        if hits:
            sources.append(source)
            files.append(str(hits[0]))
    return sources, files


def build_inventory(runs_root: Path, out_dir: Path, warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selection_rows = read_csv(SELECTION_INVENTORY)
    selection_by_tag: dict[str, dict[str, str]] = {}
    for row in selection_rows:
        tag = time_tag_from_hour(row.get("nearest_hour", ""))
        if tag and tag not in selection_by_tag:
            selection_by_tag[tag] = row

    run_dirs = {run_time_tag(p): p for p in discover_run_dirs(runs_root)}
    all_tags = sorted(set(selection_by_tag) | set(run_dirs))
    rows: list[dict[str, Any]] = []

    if not selection_rows:
        warnings.append({"severity": "WARNING", "scope": "inventory", "message": f"Missing selection inventory: {SELECTION_INVENTORY}"})
    if not run_dirs:
        warnings.append({"severity": "WARNING", "scope": "inventory", "message": f"No local time-run directories found under {runs_root}"})

    for tag in all_tags:
        sel = selection_by_tag.get(tag, {})
        run_dir = run_dirs.get(tag)
        fused_file = run_dir / "fused_best_source" / "fused_cloud_mask.npz" if run_dir else Path()
        fused_binary = run_dir / "fused_best_source" / "fused_cloud_binary.npz" if run_dir else Path()
        semantic_metrics = run_dir / f"epic_l2_cloud_mask_semantic_sensitivity_{tag}" / "epic_georing_cloud_mask_sensitivity_metrics.csv" if run_dir else Path()
        pref_sources, pref_files = prefusion_sources_for_run(run_dir, tag) if run_dir else ([], [])
        has_fused = bool(run_dir and (fused_file.exists() or fused_binary.exists()))
        has_prefusion = len(pref_sources) > 0
        if has_fused and has_prefusion:
            availability = "fused_and_prefusion_available"
        elif has_fused:
            availability = "fused_only_available"
        elif has_prefusion:
            availability = "prefusion_only_available"
        elif run_dir:
            availability = "run_dir_without_cloud_mask_products"
        else:
            availability = "no_local_stage_run"

        row_warnings: list[str] = []
        if not run_dir:
            row_warnings.append("no local Stage run directory")
        if run_dir and not has_fused:
            row_warnings.append("missing fused_cloud_mask/fused_cloud_binary")
        if run_dir and not has_prefusion:
            row_warnings.append("missing prefusion reprojected cloud_mask")
        if run_dir and not semantic_metrics.exists():
            row_warnings.append("missing 08c semantic metrics")
        if row_warnings and run_dir:
            warnings.append({"severity": "WARNING", "scope": tag, "message": "; ".join(row_warnings)})

        source_fraction = parse_source_fraction(sel.get("source_fraction_json"))
        rows.append(
            {
                "time_tag": tag,
                "target_time": sel.get("nearest_hour") or tag_to_target_time(tag),
                "epic_time": sel.get("epic_time", ""),
                "epic_file": sel.get("epic_file", ""),
                "candidate_class": sel.get("candidate_class", "RUN_NOT_IN_SELECTION_INVENTORY" if run_dir else ""),
                "nearest_hour_delta_min": sel.get("nearest_hour_delta_min", ""),
                "nearest_hour_all_27_complete": sel.get("nearest_hour_all_27_complete", ""),
                "dominant_satellite_estimate": sel.get("dominant_satellite_estimate", ""),
                "dominant_fraction_estimate": sel.get("dominant_fraction_estimate", ""),
                "source_fraction_json": json.dumps(source_fraction, ensure_ascii=False, sort_keys=True),
                "run_dir": str(run_dir) if run_dir else "",
                "run_dir_exists": bool(run_dir),
                "fused_cloud_mask_exists": bool(run_dir and fused_file.exists()),
                "fused_cloud_binary_exists": bool(run_dir and fused_binary.exists()),
                "semantic_metrics_exists": bool(run_dir and semantic_metrics.exists()),
                "prefusion_source_count": len(pref_sources),
                "prefusion_sources": "|".join(pref_sources),
                "prefusion_cloud_mask_files": "|".join(pref_files),
                "availability_class": availability,
                "warning": "; ".join(row_warnings),
            }
        )

    fields = [
        "time_tag",
        "target_time",
        "epic_time",
        "epic_file",
        "candidate_class",
        "nearest_hour_delta_min",
        "nearest_hour_all_27_complete",
        "dominant_satellite_estimate",
        "dominant_fraction_estimate",
        "source_fraction_json",
        "run_dir",
        "run_dir_exists",
        "fused_cloud_mask_exists",
        "fused_cloud_binary_exists",
        "semantic_metrics_exists",
        "prefusion_source_count",
        "prefusion_sources",
        "prefusion_cloud_mask_files",
        "availability_class",
        "warning",
    ]
    write_csv(out_dir / "stage09_epic_product_inventory.csv", rows, fields)
    counts = Counter(r["availability_class"] for r in rows)
    if counts.get("no_local_stage_run", 0):
        warnings.append(
            {
                "severity": "WARNING",
                "scope": "inventory",
                "message": f"{counts['no_local_stage_run']} EPIC nearest-hour records have no local Stage run directory; kept in inventory and skipped for metric diagnostics.",
            }
        )
    if counts.get("prefusion_only_available", 0):
        warnings.append(
            {
                "severity": "WARNING",
                "scope": "inventory",
                "message": f"{counts['prefusion_only_available']} local run(s) have prefusion cloud-mask products but no fused cloud mask.",
            }
        )
    if counts.get("run_dir_without_cloud_mask_products", 0):
        warnings.append(
            {
                "severity": "WARNING",
                "scope": "inventory",
                "message": f"{counts['run_dir_without_cloud_mask_products']} local run(s) exist but have neither fused nor prefusion cloud-mask products.",
            }
        )
    return rows


def collect_semantic_metrics(inventory: list[dict[str, Any]], out_dir: Path, warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_tag = {r["time_tag"]: r for r in inventory}
    rows: list[dict[str, Any]] = []
    for inv in inventory:
        if not inv.get("run_dir_exists"):
            continue
        tag = str(inv["time_tag"])
        metrics_path = Path(str(inv["run_dir"])) / f"epic_l2_cloud_mask_semantic_sensitivity_{tag}" / "epic_georing_cloud_mask_sensitivity_metrics.csv"
        metrics = read_csv(metrics_path)
        if not metrics:
            continue
        for m in metrics:
            row = {
                "time_tag": tag,
                "target_time": inv.get("target_time", ""),
                "epic_time": inv.get("epic_time", ""),
                "epic_delta_min": inv.get("nearest_hour_delta_min", ""),
                "candidate_class": inv.get("candidate_class", ""),
                "dominant_satellite_estimate": inv.get("dominant_satellite_estimate", ""),
                "availability_class": inv.get("availability_class", ""),
                "metrics_path": str(metrics_path),
            }
            row.update(m)
            rows.append(row)

    if not rows:
        warnings.append({"severity": "WARNING", "scope": "semantic_metrics", "message": "No 08c semantic metrics were found; fused-vs-EPIC diagnosis will be inventory-only."})
    write_csv(out_dir / "stage09_fused_vs_epic_semantic_metrics_long.csv", rows)
    return rows


def load_existing_csv(src: Path, dst: Path, warnings: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    rows = [dict(r) for r in read_csv(src)]
    if rows:
        write_csv(dst, rows)
    else:
        warnings.append({"severity": "WARNING", "scope": name, "message": f"Missing or empty CSV: {src}"})
        write_csv(dst, [])
    return rows


def mean(values: list[float]) -> float:
    vals = [v for v in values if math.isfinite(v)]
    return sum(vals) / len(vals) if vals else math.nan


def pearson(x: list[float], y: list[float]) -> float:
    pairs = [(a, b) for a, b in zip(x, y) if math.isfinite(a) and math.isfinite(b)]
    if len(pairs) < 3:
        return math.nan
    xs, ys = zip(*pairs)
    mx, my = mean(list(xs)), mean(list(ys))
    sx = math.sqrt(sum((v - mx) ** 2 for v in xs))
    sy = math.sqrt(sum((v - my) ** 2 for v in ys))
    if sx == 0 or sy == 0:
        return math.nan
    return sum((a - mx) * (b - my) for a, b in pairs) / (sx * sy)


def summarize_semantic(metrics: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_sample: dict[str, dict[str, Any]] = {}
    for row in metrics:
        tag = row.get("time_tag", "")
        by_sample.setdefault(
            tag,
            {
                "time_tag": tag,
                "target_time": row.get("target_time", ""),
                "epic_time": row.get("epic_time", ""),
                "epic_delta_min": row.get("epic_delta_min", ""),
                "candidate_class": row.get("candidate_class", ""),
                "dominant_satellite_estimate": row.get("dominant_satellite_estimate", ""),
                "availability_class": row.get("availability_class", ""),
            },
        )
        policy = str(row.get("policy", ""))
        prefix = "A" if policy.startswith("A_") else "B" if policy.startswith("B_") else "C" if policy.startswith("C_") else policy[:1]
        for field in ["status", "n", "agreement", "f1", "iou", "precision", "recall", "valid_fraction_of_epic_earth", "epic_cloud_fraction", "geo_cloud_fraction", "either_uncertain_fraction", "both_definite_agreement"]:
            if field in row:
                by_sample[tag][f"{prefix}_{field}"] = row.get(field, "")
    sample_rows = list(by_sample.values())
    for row in sample_rows:
        row["semantic_B_minus_A_agreement"] = safe_float(row.get("B_agreement")) - safe_float(row.get("A_agreement"))
        row["semantic_B_minus_A_f1"] = safe_float(row.get("B_f1")) - safe_float(row.get("A_f1"))
        row["geo_minus_epic_cloud_fraction_A"] = safe_float(row.get("A_geo_cloud_fraction")) - safe_float(row.get("A_epic_cloud_fraction"))
        row["geo_minus_epic_cloud_fraction_B"] = safe_float(row.get("B_geo_cloud_fraction")) - safe_float(row.get("B_epic_cloud_fraction"))

    group_rows: list[dict[str, Any]] = []
    for group_field in ["candidate_class", "dominant_satellite_estimate", "availability_class"]:
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in sample_rows:
            buckets[str(row.get(group_field, ""))].append(row)
        for key, vals in sorted(buckets.items()):
            if not key:
                continue
            group_rows.append(
                {
                    "group_field": group_field,
                    "group_value": key,
                    "sample_count": len(vals),
                    "A_agreement_mean": mean([safe_float(v.get("A_agreement")) for v in vals]),
                    "B_agreement_mean": mean([safe_float(v.get("B_agreement")) for v in vals]),
                    "B_minus_A_agreement_mean": mean([safe_float(v.get("semantic_B_minus_A_agreement")) for v in vals]),
                    "A_f1_mean": mean([safe_float(v.get("A_f1")) for v in vals]),
                    "B_f1_mean": mean([safe_float(v.get("B_f1")) for v in vals]),
                    "delta_min_mean": mean([safe_float(v.get("epic_delta_min")) for v in vals]),
                }
            )
    return sample_rows, group_rows


def summarize_strata(strata_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in strata_rows:
        if row.get("status") == "OK":
            buckets[(str(row.get("policy", "")), str(row.get("stratum", "")))].append(row)
    out: list[dict[str, Any]] = []
    for (policy, stratum), vals in sorted(buckets.items()):
        out.append(
            {
                "policy": policy,
                "stratum": stratum,
                "sample_count": len({v.get("time_tag") for v in vals}),
                "row_count": len(vals),
                "n_mean": mean([safe_float(v.get("n")) for v in vals]),
                "agreement_mean": mean([safe_float(v.get("agreement")) for v in vals]),
                "f1_mean": mean([safe_float(v.get("f1")) for v in vals]),
                "geo_minus_epic_cloud_fraction_mean": mean([safe_float(v.get("geo_cloud_fraction")) - safe_float(v.get("epic_cloud_fraction")) for v in vals]),
            }
        )
    return out


def summarize_prefusion(prefusion_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in prefusion_rows:
        if row.get("status") == "OK":
            source = str(row.get("satellite") or row.get("source") or row.get("source_name") or "")
            buckets[(str(row.get("policy", "")), source)].append(row)
    out: list[dict[str, Any]] = []
    for (policy, source), vals in sorted(buckets.items()):
        if not source:
            continue
        out.append(
            {
                "policy": policy,
                "source": source,
                "sample_count": len({v.get("time_tag") for v in vals}),
                "row_count": len(vals),
                "n_mean": mean([safe_float(v.get("n")) for v in vals]),
                "agreement_mean": mean([safe_float(v.get("agreement")) for v in vals]),
                "f1_mean": mean([safe_float(v.get("f1")) for v in vals]),
                "geo_minus_epic_cloud_fraction_mean": mean([safe_float(v.get("geo_cloud_fraction", v.get("cloud_fraction"))) - safe_float(v.get("epic_cloud_fraction")) for v in vals]),
            }
        )
    return out


def summarize_pairs(pair_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in pair_rows:
        if row.get("status") == "OK":
            buckets[(str(row.get("policy", "")), str(row.get("source_a", "")), str(row.get("source_b", "")))].append(row)
    out: list[dict[str, Any]] = []
    for (policy, a, b), vals in sorted(buckets.items()):
        out.append(
            {
                "policy": policy,
                "source_a": a,
                "source_b": b,
                "sample_count": len({v.get("time_tag") for v in vals}),
                "row_count": len(vals),
                "n_mean": mean([safe_float(v.get("n")) for v in vals]),
                "source_a_agreement_mean": mean([safe_float(v.get("source_a_agreement")) for v in vals]),
                "source_b_agreement_mean": mean([safe_float(v.get("source_b_agreement")) for v in vals]),
                "agreement_b_minus_a_mean": mean([safe_float(v.get("agreement_b_minus_a")) for v in vals]),
                "source_disagreement_fraction_mean": mean([safe_float(v.get("source_disagreement_fraction")) for v in vals]),
                "both_wrong_fraction_mean": mean([safe_float(v.get("both_wrong_fraction")) for v in vals]),
                "source_a_only_correct_fraction_mean": mean([safe_float(v.get("source_a_only_correct_fraction")) for v in vals]),
                "source_b_only_correct_fraction_mean": mean([safe_float(v.get("source_b_only_correct_fraction")) for v in vals]),
            }
        )
    return out


def build_factor_rows(
    sample_rows: list[dict[str, Any]],
    group_rows: list[dict[str, Any]],
    strata_summary: list[dict[str, Any]],
    prefusion_summary: list[dict[str, Any]],
    pair_summary: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    factors: list[dict[str, Any]] = []
    a_rows = [r for r in sample_rows if math.isfinite(safe_float(r.get("A_agreement")))]
    b_minus_a = [safe_float(r.get("semantic_B_minus_A_agreement")) for r in a_rows]
    factors.append(
        {
            "factor": "semantic_policy_difference",
            "evidence_metric": "mean(B_agreement - A_agreement)",
            "value": mean(b_minus_a),
            "sample_count": len(b_minus_a),
            "interpretation": "Positive values mean excluding low-confidence/probable classes improves agreement, supporting a semantic-confidence contribution.",
            "source_csv": "stage09_sample_level_semantic_summary.csv",
        }
    )
    factors.append(
        {
            "factor": "time_difference",
            "evidence_metric": "Pearson corr(delta_min, A_agreement)",
            "value": pearson([safe_float(r.get("epic_delta_min")) for r in a_rows], [safe_float(r.get("A_agreement")) for r in a_rows]),
            "sample_count": len(a_rows),
            "interpretation": "Negative values suggest larger EPIC-GEO time offsets reduce agreement; weak values mean time is not the only explanation in current samples.",
            "source_csv": "stage09_sample_level_semantic_summary.csv",
        }
    )
    strata = {(r["policy"], r["stratum"]): r for r in strata_summary}
    mid = safe_float(strata.get(("A_inclusive_binary", "geo_traditional_midlow_lat_abs_lt60"), {}).get("agreement_mean"))
    high = safe_float(strata.get(("A_inclusive_binary", "high_lat_abs_ge60"), {}).get("agreement_mean"))
    edge = safe_float(strata.get(("A_inclusive_binary", "lat_abs_70_90"), {}).get("agreement_mean"))
    factors.append(
        {
            "factor": "sampling_geometry_high_latitude",
            "evidence_metric": "A_agreement(high_lat_abs_ge60 - abs_lat_lt60)",
            "value": high - mid,
            "sample_count": safe_int(strata.get(("A_inclusive_binary", "high_lat_abs_ge60"), {}).get("sample_count")),
            "interpretation": "Negative values indicate GEO-grid/EPIC sampling and geometry differences are concentrated toward high latitudes or disk edges.",
            "source_csv": "stage09_strata_summary.csv",
        }
    )
    factors.append(
        {
            "factor": "sampling_geometry_extreme_latitude",
            "evidence_metric": "A_agreement(lat_abs_70_90 - abs_lat_lt60)",
            "value": edge - mid,
            "sample_count": safe_int(strata.get(("A_inclusive_binary", "lat_abs_70_90"), {}).get("sample_count")),
            "interpretation": "A large negative value flags extreme-latitude representativeness error, not necessarily cloud-mask algorithm failure.",
            "source_csv": "stage09_strata_summary.csv",
        }
    )
    for row in group_rows:
        if row.get("group_field") == "candidate_class":
            factors.append(
                {
                    "factor": "scene_type_difference",
                    "evidence_metric": f"A_agreement_mean for {row.get('group_value')}",
                    "value": row.get("A_agreement_mean"),
                    "sample_count": row.get("sample_count"),
                    "interpretation": "Scene/source-region class changes agreement, so the 60-80 percent range should not be treated as one homogeneous population.",
                    "source_csv": "stage09_group_semantic_summary.csv",
                }
            )
    for row in prefusion_summary:
        if row.get("policy") == "A_inclusive_binary":
            factors.append(
                {
                    "factor": "source_product_difference",
                    "evidence_metric": f"A_agreement_mean for prefusion {row.get('source')}",
                    "value": row.get("agreement_mean"),
                    "sample_count": row.get("sample_count"),
                    "interpretation": "Single-source prefusion behavior identifies whether disagreement is source-specific before fusion selection.",
                    "source_csv": "stage09_prefusion_source_summary.csv",
                }
            )
    pair_a = [r for r in pair_summary if r.get("policy") == "A_inclusive_binary"]
    factors.append(
        {
            "factor": "source_pair_difference",
            "evidence_metric": "mean pair source_disagreement_fraction",
            "value": mean([safe_float(r.get("source_disagreement_fraction_mean")) for r in pair_a]),
            "sample_count": len(pair_a),
            "interpretation": "Large source-pair disagreement in overlaps means source boundary/product semantics can explain part of fused-vs-EPIC disagreement.",
            "source_csv": "stage09_source_pair_summary.csv",
        }
    )
    return factors


def save_bar(labels: list[str], values: list[float], path: Path, title: str, ylabel: str, ylim: tuple[float, float] | None = None, rotation: int = 35) -> None:
    fig, ax = plt.subplots(figsize=(max(8, len(labels) * 0.75), 4.8), constrained_layout=True)
    ax.bar(range(len(labels)), values, color="#386fa4")
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=rotation, ha="right")
    if ylim:
        ax.set_ylim(*ylim)
    ax.grid(axis="y", alpha=0.25)
    fig.savefig(path, dpi=170)
    plt.close(fig)


def build_plots(
    out_dir: Path,
    inventory: list[dict[str, Any]],
    sample_rows: list[dict[str, Any]],
    strata_summary: list[dict[str, Any]],
    prefusion_summary: list[dict[str, Any]],
    pair_summary: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    plot_dir = out_dir / "plots"
    plot_dir.mkdir(parents=True, exist_ok=True)
    plot_index: list[dict[str, Any]] = []

    try:
        counts = Counter(r["availability_class"] for r in inventory)
        labels = list(counts.keys())
        values = [counts[k] for k in labels]
        path = plot_dir / "stage09_inventory_availability_counts.png"
        save_bar(labels, values, path, "Stage 09 EPIC-Time Product Availability", "EPIC time count")
        plot_index.append({"png": str(path), "source_csv": str(out_dir / "stage09_epic_product_inventory.csv"), "filter": "all rows", "description": "Inventory availability class counts"})

        ordered = sorted(sample_rows, key=lambda r: (str(r.get("candidate_class", "")), str(r.get("time_tag", ""))))
        if ordered:
            labels = [f"{r.get('time_tag')}\n{r.get('candidate_class')}" for r in ordered]
            x = range(len(ordered))
            fig, ax = plt.subplots(figsize=(max(10, len(ordered) * 1.1), 5), constrained_layout=True)
            ax.bar([i - 0.2 for i in x], [safe_float(r.get("A_agreement"), 0.0) for r in ordered], width=0.38, label="A inclusive", color="#3b7ea1")
            ax.bar([i + 0.2 for i in x], [safe_float(r.get("B_agreement"), 0.0) for r in ordered], width=0.38, label="B high-confidence", color="#7a9e3f")
            ax.set_title("Fused GEO-ring vs EPIC Cloud Mask Agreement by Sample")
            ax.set_ylabel("Agreement")
            ax.set_ylim(0, 1)
            ax.set_xticks(list(x))
            ax.set_xticklabels(labels, rotation=45, ha="right")
            ax.grid(axis="y", alpha=0.25)
            ax.legend()
            path = plot_dir / "stage09_fused_agreement_by_sample.png"
            fig.savefig(path, dpi=170)
            plt.close(fig)
            plot_index.append({"png": str(path), "source_csv": str(out_dir / "stage09_sample_level_semantic_summary.csv"), "filter": "A/B agreement columns", "description": "Fused-vs-EPIC agreement by sample and semantic policy"})

            labels = [str(r.get("time_tag")) for r in ordered]
            values = [safe_float(r.get("semantic_B_minus_A_agreement"), 0.0) for r in ordered]
            path = plot_dir / "stage09_semantic_policy_delta_B_minus_A.png"
            save_bar(labels, values, path, "Semantic Policy Sensitivity: B minus A Agreement", "Agreement delta", None, 45)
            plot_index.append({"png": str(path), "source_csv": str(out_dir / "stage09_sample_level_semantic_summary.csv"), "filter": "semantic_B_minus_A_agreement", "description": "Agreement gain/loss when low-confidence/probable classes are excluded"})

        strata_a = [r for r in strata_summary if r.get("policy") == "A_inclusive_binary" and str(r.get("stratum", "")).startswith(("lat_", "geo_", "high_"))]
        if strata_a:
            order = ["lat_abs_0_30", "lat_abs_30_50", "lat_abs_50_60", "lat_abs_60_70", "lat_abs_70_90", "geo_traditional_midlow_lat_abs_lt60", "high_lat_abs_ge60"]
            picked = [next((r for r in strata_a if r.get("stratum") == s), None) for s in order]
            picked = [r for r in picked if r]
            path = plot_dir / "stage09_agreement_by_latitude_stratum_A.png"
            save_bar([str(r["stratum"]) for r in picked], [safe_float(r.get("agreement_mean"), 0.0) for r in picked], path, "Fused Agreement by Latitude Stratum, Policy A", "Mean agreement", (0, 1), 35)
            plot_index.append({"png": str(path), "source_csv": str(out_dir / "stage09_strata_summary.csv"), "filter": "policy=A_inclusive_binary, latitude strata", "description": "Latitude/geometry sampling diagnostic"})

        pref_a = [r for r in prefusion_summary if r.get("policy") == "A_inclusive_binary"]
        if pref_a:
            path = plot_dir / "stage09_prefusion_source_agreement_A.png"
            save_bar([str(r["source"]) for r in pref_a], [safe_float(r.get("agreement_mean"), 0.0) for r in pref_a], path, "Prefusion Source vs EPIC Agreement, Policy A", "Mean agreement", (0, 1), 35)
            plot_index.append({"png": str(path), "source_csv": str(out_dir / "stage09_prefusion_source_summary.csv"), "filter": "policy=A_inclusive_binary", "description": "Single-source prefusion product diagnostic"})

        pair_a = [r for r in pair_summary if r.get("policy") == "A_inclusive_binary"]
        if pair_a:
            labels = [f"{r['source_a']} vs\n{r['source_b']}" for r in pair_a]
            values = [safe_float(r.get("source_disagreement_fraction_mean"), 0.0) for r in pair_a]
            path = plot_dir / "stage09_source_pair_disagreement_A.png"
            save_bar(labels, values, path, "Prefusion Source-Pair Disagreement in Overlap, Policy A", "Mean source disagreement fraction", (0, 1), 35)
            plot_index.append({"png": str(path), "source_csv": str(out_dir / "stage09_source_pair_summary.csv"), "filter": "policy=A_inclusive_binary", "description": "Overlap source-pair diagnostic"})
    except Exception as exc:
        warnings.append({"severity": "WARNING", "scope": "plots", "message": f"Plot generation skipped after error: {exc}"})

    write_csv(out_dir / "stage09_plot_index.csv", plot_index, ["png", "source_csv", "filter", "description"])
    return plot_index


def fmt(value: Any, digits: int = 3) -> str:
    v = safe_float(value)
    if not math.isfinite(v):
        return "NA"
    return f"{v:.{digits}f}"


def build_report(
    out_dir: Path,
    inventory: list[dict[str, Any]],
    sample_rows: list[dict[str, Any]],
    group_rows: list[dict[str, Any]],
    factor_rows: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    plot_index: list[dict[str, Any]],
) -> Path:
    counts = Counter(r["availability_class"] for r in inventory)
    fused_available = [r for r in inventory if str(r["availability_class"]).startswith("fused") or r["availability_class"] == "fused_and_prefusion_available"]
    prefusion_only = [r for r in inventory if r["availability_class"] == "prefusion_only_available"]
    no_run = [r for r in inventory if r["availability_class"] == "no_local_stage_run"]
    a_ag = [safe_float(r.get("A_agreement")) for r in sample_rows]
    b_ag = [safe_float(r.get("B_agreement")) for r in sample_rows]

    lines = [
        "# Stage 09 GEO-ring vs DSCOVR EPIC Cloud Mask Diagnostics",
        "",
        f"- Generated UTC: `{utc_now()}`",
        f"- Output directory: `{out_dir}`",
        "- Scope: diagnostic only. No fusion v2 was implemented, no fused production logic was modified, and no source-pair result was used to create a new fused product.",
        "- EPIC is used as an independent reference/diagnostic view, not as absolute truth.",
        "",
        "## 1. Inventory First",
        "",
        f"- Inventory rows: `{len(inventory)}` EPIC nearest-hour records/local run tags.",
        f"- Fused + prefusion available: `{counts.get('fused_and_prefusion_available', 0)}`.",
        f"- Prefusion only available: `{counts.get('prefusion_only_available', 0)}`.",
        f"- No local Stage run: `{counts.get('no_local_stage_run', 0)}`.",
        f"- Run directory without cloud-mask products: `{counts.get('run_dir_without_cloud_mask_products', 0)}`.",
        "",
        "Key inventory CSV:",
        "",
        f"- `{out_dir / 'stage09_epic_product_inventory.csv'}`",
        "",
        "Fused-capable local EPIC/GEO times:",
        "",
    ]
    if fused_available:
        for r in sorted(fused_available, key=lambda x: str(x["time_tag"])):
            lines.append(f"- `{r['time_tag']}` EPIC `{r.get('epic_time')}` class `{r.get('candidate_class')}` prefusion sources `{r.get('prefusion_sources')}`")
    else:
        lines.append("- None found in local runs.")

    lines.extend(["", "Prefusion-only local EPIC/GEO times:", ""])
    if prefusion_only:
        for r in sorted(prefusion_only, key=lambda x: str(x["time_tag"])):
            lines.append(f"- `{r['time_tag']}` EPIC `{r.get('epic_time')}` sources `{r.get('prefusion_sources')}` warning `{r.get('warning')}`")
    else:
        lines.append("- None found in local runs.")

    lines.extend(
        [
            "",
            "## 2. Agreement Range",
            "",
            f"- Policy A inclusive agreement: n=`{len([v for v in a_ag if math.isfinite(v)])}`, mean=`{fmt(mean(a_ag))}`, min=`{fmt(min([v for v in a_ag if math.isfinite(v)], default=math.nan))}`, max=`{fmt(max([v for v in a_ag if math.isfinite(v)], default=math.nan))}`.",
            f"- Policy B high-confidence agreement: n=`{len([v for v in b_ag if math.isfinite(v)])}`, mean=`{fmt(mean(b_ag))}`, min=`{fmt(min([v for v in b_ag if math.isfinite(v)], default=math.nan))}`, max=`{fmt(max([v for v in b_ag if math.isfinite(v)], default=math.nan))}`.",
            "",
            "This is consistent with the observed roughly 60%-80% band: the band is not one failure mode. It changes with semantic policy, source region, source products, sampling geometry, and time offset.",
            "",
            "## 3. Diagnostic Factors",
            "",
            "| factor | evidence metric | value | n | reading |",
            "|---|---|---:|---:|---|",
        ]
    )
    for row in factor_rows:
        lines.append(f"| {row.get('factor')} | {row.get('evidence_metric')} | {fmt(row.get('value'))} | {row.get('sample_count', '')} | {row.get('interpretation')} |")

    lines.extend(["", "## 4. Scene / Source Group Means", ""])
    if group_rows:
        lines.extend(["| group field | group value | n | A agreement | B agreement | B-A |", "|---|---|---:|---:|---:|---:|"])
        for row in group_rows:
            lines.append(f"| {row.get('group_field')} | {row.get('group_value')} | {row.get('sample_count')} | {fmt(row.get('A_agreement_mean'))} | {fmt(row.get('B_agreement_mean'))} | {fmt(row.get('B_minus_A_agreement_mean'))} |")
    else:
        lines.append("- No group metrics available.")

    lines.extend(
        [
            "",
            "## 5. Outputs",
            "",
            f"- Inventory: `{out_dir / 'stage09_epic_product_inventory.csv'}`",
            f"- Fused semantic long metrics: `{out_dir / 'stage09_fused_vs_epic_semantic_metrics_long.csv'}`",
            f"- Sample semantic summary: `{out_dir / 'stage09_sample_level_semantic_summary.csv'}`",
            f"- Group semantic summary: `{out_dir / 'stage09_group_semantic_summary.csv'}`",
            f"- Geometry strata summary: `{out_dir / 'stage09_strata_summary.csv'}`",
            f"- Prefusion source summary: `{out_dir / 'stage09_prefusion_source_summary.csv'}`",
            f"- Source-pair summary: `{out_dir / 'stage09_source_pair_summary.csv'}`",
            f"- Factor table: `{out_dir / 'stage09_diagnostic_factor_table.csv'}`",
            f"- Plot traceability index: `{out_dir / 'stage09_plot_index.csv'}`",
            "",
            "PNG outputs, all traceable through `stage09_plot_index.csv`:",
            "",
        ]
    )
    for row in plot_index:
        lines.append(f"- `{row['png']}` from `{row['source_csv']}`")

    lines.extend(["", "## 6. Warnings", ""])
    if warnings:
        for w in warnings[:80]:
            lines.append(f"- `{w.get('severity')}` `{w.get('scope')}`: {w.get('message')}")
        if len(warnings) > 80:
            lines.append(f"- Additional warnings omitted from report view; see `{out_dir / 'stage09_warnings.csv'}`.")
    else:
        lines.append("- No warnings recorded.")

    if no_run:
        lines.extend(["", "## 7. Note On Missing Local Runs", ""])
        lines.append(f"`{len(no_run)}` inventory records do not have local Stage run directories. They were kept in the inventory so sample scarcity is explicit rather than silently hidden.")

    report = out_dir / "stage09_epic_georing_cloud_mask_diagnostics_report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Stage 09 diagnostic inventory and attribution for GEO-ring vs EPIC cloud-mask agreement.")
    p.add_argument("--runs-root", default=str(RUNS_ROOT))
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    return p


def main() -> int:
    args = build_parser().parse_args()
    runs_root = Path(args.runs_root)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[dict[str, Any]] = []

    inventory = build_inventory(runs_root, out_dir, warnings)
    semantic_long = collect_semantic_metrics(inventory, out_dir, warnings)
    sample_rows, group_rows = summarize_semantic(semantic_long)
    write_csv(out_dir / "stage09_sample_level_semantic_summary.csv", sample_rows)
    write_csv(out_dir / "stage09_group_semantic_summary.csv", group_rows)

    strata_rows = load_existing_csv(
        GEOM_PREFUSION_DIR / "fused_metrics_by_latitude_and_epic_view_angle.csv",
        out_dir / "stage09_existing_08f_fused_metrics_by_latitude_and_epic_view_angle.csv",
        warnings,
        "08f_strata",
    )
    prefusion_rows = load_existing_csv(
        GEOM_PREFUSION_DIR / "prefusion_source_cloud_mask_vs_epic_metrics.csv",
        out_dir / "stage09_existing_08f_prefusion_source_cloud_mask_vs_epic_metrics.csv",
        warnings,
        "08f_prefusion",
    )
    pair_rows = load_existing_csv(
        TIME_OFFSET_PAIR_DIR / "08j_prefusion_pair_overlap_vs_epic.csv",
        out_dir / "stage09_existing_08j_prefusion_pair_overlap_vs_epic.csv",
        warnings,
        "08j_pair_overlap",
    )

    strata_summary = summarize_strata(strata_rows)
    prefusion_summary = summarize_prefusion(prefusion_rows)
    pair_summary = summarize_pairs(pair_rows)
    semantic_tags = {str(r.get("time_tag")) for r in sample_rows if r.get("time_tag")}
    strata_tags = {str(r.get("time_tag")) for r in strata_rows if r.get("time_tag")}
    prefusion_tags = {str(r.get("time_tag")) for r in prefusion_rows if r.get("time_tag")}
    pair_tags = {str(r.get("time_tag")) for r in pair_rows if r.get("time_tag")}
    if semantic_tags and strata_tags and strata_tags != semantic_tags:
        warnings.append(
            {
                "severity": "WARNING",
                "scope": "08f_strata_coverage",
                "message": f"08f strata diagnostics cover {len(strata_tags)} sample(s), while Stage 09 semantic metrics cover {len(semantic_tags)} fused sample(s); missing tags are {','.join(sorted(semantic_tags - strata_tags)) or 'none'}.",
            }
        )
    if semantic_tags and prefusion_tags and prefusion_tags != semantic_tags:
        warnings.append(
            {
                "severity": "WARNING",
                "scope": "08f_prefusion_coverage",
                "message": f"08f prefusion source diagnostics cover {len(prefusion_tags)} sample(s), while Stage 09 semantic metrics cover {len(semantic_tags)} fused sample(s); missing tags are {','.join(sorted(semantic_tags - prefusion_tags)) or 'none'}.",
            }
        )
    if semantic_tags and pair_tags and pair_tags != semantic_tags:
        warnings.append(
            {
                "severity": "WARNING",
                "scope": "08j_pair_coverage",
                "message": f"08j source-pair diagnostics cover {len(pair_tags)} sample(s), while Stage 09 semantic metrics cover {len(semantic_tags)} fused sample(s); missing tags are {','.join(sorted(semantic_tags - pair_tags)) or 'none'}.",
            }
        )
    write_csv(out_dir / "stage09_strata_summary.csv", strata_summary)
    write_csv(out_dir / "stage09_prefusion_source_summary.csv", prefusion_summary)
    write_csv(out_dir / "stage09_source_pair_summary.csv", pair_summary)

    factor_rows = build_factor_rows(sample_rows, group_rows, strata_summary, prefusion_summary, pair_summary)
    write_csv(out_dir / "stage09_diagnostic_factor_table.csv", factor_rows)

    plot_index = build_plots(out_dir, inventory, sample_rows, strata_summary, prefusion_summary, pair_summary, warnings)
    write_csv(out_dir / "stage09_warnings.csv", warnings, ["severity", "scope", "message"])
    report = build_report(out_dir, inventory, sample_rows, group_rows, factor_rows, warnings, plot_index)
    print(f"Stage 09 complete: report={report}")
    print(f"CSV/PNG outputs={out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
