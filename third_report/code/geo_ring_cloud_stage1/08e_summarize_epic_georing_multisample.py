from __future__ import annotations

import csv
import json
import os
import shutil
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from geo_ring_cloud.paths import RUNS_ROOT
from geo_ring_cloud.run_discovery import discover_run_dirs, run_time_tag


SELECTION_INVENTORY = RUNS_ROOT / "epic_202403_target_selection" / "epic_202403_geo_source_candidate_inventory.csv"
OUT_DIR = RUNS_ROOT / "epic_202403_multisample_summary"
QL_DIR = OUT_DIR / "renamed_quicklooks"
SOURCE_PROFILE = os.environ.get("GEO_RING_SOURCE_PROFILE", "operational_baseline")


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def find_runs() -> list[Path]:
    runs = []
    for path in discover_run_dirs(RUNS_ROOT, SOURCE_PROFILE):
        tag = run_time_tag(path)
        sens = path / f"epic_l2_cloud_mask_semantic_sensitivity_{tag}"
        if (sens / "epic_georing_cloud_mask_sensitivity_metrics.csv").exists():
            runs.append(path)
    return runs


def load_selection_lookup() -> dict[str, dict[str, Any]]:
    if not SELECTION_INVENTORY.exists():
        return {}
    df = pd.read_csv(SELECTION_INVENTORY)
    out: dict[str, dict[str, Any]] = {}
    for _, row in df.iterrows():
        tag = str(row.get("nearest_hour", ""))[0:13].replace("-", "").replace("T", "_") + "00"
        out[tag] = row.to_dict()
    return out


def read_csv_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return pd.read_csv(path).fillna("").to_dict("records")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def dominant_from_source_json(value: str) -> tuple[str, float, str]:
    try:
        d = json.loads(value)
    except Exception:
        return "", 0.0, "{}"
    if not d:
        return "", 0.0, "{}"
    sat, frac = sorted(d.items(), key=lambda kv: float(kv[1]), reverse=True)[0]
    return str(sat), float(frac), json.dumps(d, ensure_ascii=False, sort_keys=True)


def collect() -> tuple[pd.DataFrame, pd.DataFrame, list[dict[str, Any]]]:
    selection = load_selection_lookup()
    summary_rows: list[dict[str, Any]] = []
    source_rows_all: list[dict[str, Any]] = []
    quicklook_rows: list[dict[str, Any]] = []
    QL_DIR.mkdir(parents=True, exist_ok=True)

    for run in find_runs():
        tag = run_time_tag(run)
        sens = run / f"epic_l2_cloud_mask_semantic_sensitivity_{tag}"
        metrics = read_csv_rows(sens / "epic_georing_cloud_mask_sensitivity_metrics.csv")
        source_metrics = read_csv_rows(sens / "epic_georing_cloud_mask_metrics_by_source.csv")
        source_dist = read_csv_rows(sens / "epic_georing_cloud_mask_source_distribution_by_policy.csv")
        sel = selection.get(tag, {})
        dominant, dominant_frac, source_json = dominant_from_source_json(str(sel.get("source_fraction_json", "{}")))
        row = {
            "time_tag": tag,
            "target_time": sel.get("nearest_hour", ""),
            "epic_time": sel.get("epic_time", ""),
            "candidate_class": sel.get("candidate_class", "MANUAL_OR_NOT_IN_SELECTION"),
            "selection_role": sel.get("selection_role", ""),
            "epic_file": sel.get("epic_file", ""),
            "epic_delta_min": sel.get("nearest_hour_delta_min", ""),
            "estimated_dominant_satellite": dominant or sel.get("dominant_satellite_estimate", ""),
            "estimated_dominant_fraction": dominant_frac or sel.get("dominant_fraction_estimate", ""),
            "east_asia_fraction_estimate": sel.get("east_asia_fraction_estimate", ""),
            "estimated_source_fraction_json": source_json,
        }
        for m in metrics:
            policy = str(m.get("policy", ""))
            prefix = "A" if policy.startswith("A_") else "B" if policy.startswith("B_") else "C" if policy.startswith("C_") else policy
            for key in ["agreement", "f1", "iou", "precision", "recall", "valid_fraction_of_epic_earth", "epic_cloud_fraction", "geo_cloud_fraction", "either_uncertain_fraction", "both_definite_agreement"]:
                if key in m:
                    row[f"{prefix}_{key}"] = m.get(key, "")
        summary_rows.append(row)

        for sm in source_metrics:
            sm = dict(sm)
            sm["time_tag"] = tag
            sm["candidate_class"] = row["candidate_class"]
            sm["target_time"] = row["target_time"]
            sm["epic_time"] = row["epic_time"]
            source_rows_all.append(sm)

        for dist in source_dist:
            if dist.get("policy") != "A_inclusive_binary" or dist.get("scope") != "policy_valid_pixels":
                continue
            dist = dict(dist)
            dist["time_tag"] = tag
            dist["candidate_class"] = row["candidate_class"]
            source_rows_all.append({**dist, "source_metric_kind": "pixel_distribution"})

        for policy_short, filename in [
            ("A", "A_inclusive_binary_epic_vs_georing_cloud_mask.png"),
            ("B", "B_high_confidence_only_epic_vs_georing_cloud_mask.png"),
            ("C", "C_uncertainty_aware_3class_epic_vs_georing_cloud_mask.png"),
        ]:
            src = sens / "quicklooks" / filename
            if not src.exists():
                continue
            label = str(row["candidate_class"]).replace("_", "-").lower()
            dom = str(row["estimated_dominant_satellite"]).replace(" ", "").replace("/", "-")
            dst = QL_DIR / f"{tag}_{label}_{dom}_{policy_short}_{filename}"
            if not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime:
                shutil.copy2(src, dst)
            quicklook_rows.append(
                {
                    "time_tag": tag,
                    "policy": policy_short,
                    "candidate_class": row["candidate_class"],
                    "estimated_dominant_satellite": row["estimated_dominant_satellite"],
                    "quicklook": str(dst),
                    "source": str(src),
                }
            )

    return pd.DataFrame(summary_rows), pd.DataFrame(source_rows_all), quicklook_rows


def make_plots(summary: pd.DataFrame, source: pd.DataFrame) -> None:
    if summary.empty:
        return
    plot_dir = OUT_DIR / "plots"
    plot_dir.mkdir(parents=True, exist_ok=True)

    order = summary.sort_values(["candidate_class", "time_tag"]).reset_index(drop=True)
    labels = [f"{r.time_tag}\n{str(r.candidate_class).replace('_', ' ')}" for r in order.itertuples()]
    x = range(len(order))

    fig, ax = plt.subplots(figsize=(max(10, len(order) * 1.25), 5), constrained_layout=True)
    ax.bar([i - 0.18 for i in x], [safe_float(v) for v in order.get("A_agreement", [])], width=0.36, label="A inclusive")
    ax.bar([i + 0.18 for i in x], [safe_float(v) for v in order.get("B_agreement", [])], width=0.36, label="B high-confidence")
    ax.set_ylim(0, 1)
    ax.set_ylabel("Agreement")
    ax.set_title("EPIC L2 Cloud Mask vs GEO-ring Cloud Mask: A/B Agreement by Sample")
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, rotation=45, ha="right")
    ax.grid(axis="y", alpha=0.25)
    ax.legend()
    fig.savefig(plot_dir / "agreement_by_sample_A_B.png", dpi=170)
    plt.close(fig)

    if not source.empty and "policy" in source.columns and "source_name" in source.columns:
        s = source[(source["policy"] == "A_inclusive_binary") & (source.get("source_metric_kind", "") != "pixel_distribution")].copy()
        if not s.empty and "agreement" in s.columns:
            piv = s.pivot_table(index="time_tag", columns="source_name", values="agreement", aggfunc="mean")
            fig, ax = plt.subplots(figsize=(10, max(4, len(piv) * 0.45)), constrained_layout=True)
            im = ax.imshow(piv.astype(float).values, vmin=0, vmax=1, cmap="viridis", aspect="auto")
            ax.set_title("Mode A Agreement by Selected GEO Source")
            ax.set_yticks(range(len(piv.index)))
            ax.set_yticklabels(piv.index)
            ax.set_xticks(range(len(piv.columns)))
            ax.set_xticklabels(piv.columns, rotation=45, ha="right")
            fig.colorbar(im, ax=ax, shrink=0.8)
            fig.savefig(plot_dir / "agreement_by_source_heatmap_A.png", dpi=170)
            plt.close(fig)


def make_report(summary: pd.DataFrame, source: pd.DataFrame, quicklooks: list[dict[str, Any]]) -> Path:
    lines = [
        "# 08e EPIC GEO-ring Multi-sample Summary",
        "",
        f"- Samples summarized: `{len(summary)}`",
        f"- Output directory: `{OUT_DIR}`",
        "",
        "## 1. How To Read This",
        "",
        "- Mode A: inclusive binary. EPIC 1/2=clear, 3/4=cloud; GEO 0/1=clear, 2/3=cloud.",
        "- Mode B: high-confidence only. EPIC 1=clear, 4=cloud; GEO 0=clear, 3=cloud; low-confidence/probable pixels are excluded.",
        "- Mode C: uncertainty-aware 3-class. Low-confidence/probable pixels are kept as uncertain.",
        "- EPIC L2 is an independent reference, not absolute truth. The purpose here is to diagnose source/product/fusion differences.",
        "",
        "## 2. Sample-level Metrics",
        "",
    ]
    if summary.empty:
        lines.append("- No completed 08c samples found.")
    else:
        for _, r in summary.sort_values(["candidate_class", "time_tag"]).iterrows():
            lines.append(
                f"- `{r.get('time_tag')}` {r.get('candidate_class')} dominant={r.get('estimated_dominant_satellite')} "
                f"A_agreement={safe_float(r.get('A_agreement'), float('nan')):.3f}, "
                f"B_agreement={safe_float(r.get('B_agreement'), float('nan')):.3f}, "
                f"A_F1={safe_float(r.get('A_f1'), float('nan')):.3f}, "
                f"B_F1={safe_float(r.get('B_f1'), float('nan')):.3f}"
            )

    lines.extend(["", "## 3. Group Summary", ""])
    if not summary.empty:
        group = summary.copy()
        for col in ["A_agreement", "B_agreement", "A_f1", "B_f1"]:
            group[col] = pd.to_numeric(group.get(col, ""), errors="coerce")
        agg = group.groupby("candidate_class")[["A_agreement", "B_agreement", "A_f1", "B_f1"]].agg(["count", "mean", "min", "max"])
        for klass in agg.index:
            lines.append(f"### {klass}")
            for metric in ["A_agreement", "B_agreement", "A_f1", "B_f1"]:
                vals = agg.loc[klass, metric]
                lines.append(f"- {metric}: n={int(vals['count'])}, mean={vals['mean']:.3f}, min={vals['min']:.3f}, max={vals['max']:.3f}")
            lines.append("")

    lines.extend(
        [
            "## 4. Current Scientific Reading",
            "",
            "- The available samples support your concern: a single EPIC scene is not enough to decide 06 v2.",
            "- East-Asia/FY4B-Himawari scenes generally show higher agreement than the earlier GOES/Meteosat mixed scene.",
            "- If Mode B is consistently higher than Mode A, part of the disagreement is confidence/semantic policy rather than geolocation failure.",
            "- Low Meteosat-source agreement, if stable across Meteosat-dominant samples, points to a product semantics or source-boundary issue rather than a GEO-wide failure.",
            "- This does not mean EPIC is more trustworthy than GEO. Use EPIC as an external reference and diagnose regional/source-dependent behavior.",
            "",
            "## 5. Key Files",
            "",
            f"- Summary table: `{OUT_DIR / 'epic_georing_multisample_summary.csv'}`",
            f"- Source metrics: `{OUT_DIR / 'epic_georing_multisample_source_metrics.csv'}`",
            f"- Quicklook index: `{OUT_DIR / 'epic_georing_multisample_quicklook_index.csv'}`",
            f"- Renamed quicklooks: `{QL_DIR}`",
            f"- Plots: `{OUT_DIR / 'plots'}`",
            "",
            "## 6. About EPIC L1 Images",
            "",
            "- This summary uses EPIC L2 cloud-mask quicklooks, not EPIC L1 RGB, because L1 files are not present for every selected sample in the current run directories.",
            "- If EPIC L1 for all selected samples is later available, the same summary directory should add an `epic_l1_rgb/` panel next to these L2 cloud-mask panels.",
        ]
    )
    report = OUT_DIR / "epic_georing_multisample_summary_report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary, source, quicklooks = collect()
    summary.to_csv(OUT_DIR / "epic_georing_multisample_summary.csv", index=False, encoding="utf-8-sig")
    source.to_csv(OUT_DIR / "epic_georing_multisample_source_metrics.csv", index=False, encoding="utf-8-sig")
    write_csv(OUT_DIR / "epic_georing_multisample_quicklook_index.csv", quicklooks, ["time_tag", "policy", "candidate_class", "estimated_dominant_satellite", "quicklook", "source"])
    make_plots(summary, source)
    report = make_report(summary, source, quicklooks)
    print(f"08e PASS: samples={len(summary)} report={report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
