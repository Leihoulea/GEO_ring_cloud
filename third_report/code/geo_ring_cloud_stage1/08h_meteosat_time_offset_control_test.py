from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
RUNS_ROOT = Path(r"D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs")
DEFAULT_INVENTORY = RUNS_ROOT / "epic_202403_target_selection" / "epic_202403_geo_source_candidate_inventory.csv"
DEFAULT_OLD_SUMMARY = RUNS_ROOT / "epic_202403_multisample_summary" / "epic_georing_multisample_summary.csv"
DEFAULT_OUT_DIR = RUNS_ROOT / "epic_202403_meteosat_time_offset_control"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
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


def time_tag_from_hour(value: str) -> str:
    # 2024-03-15T13:00:00Z -> 20240315_1300
    return value[0:13].replace("-", "").replace("T", "_") + "00"


def msg_fraction(row: dict[str, Any]) -> float:
    try:
        d = json.loads(row.get("source_fraction_json") or row.get("estimated_source_fraction_json") or "{}")
    except Exception:
        d = {}
    return safe_float(d.get("Meteosat-0deg")) + safe_float(d.get("Meteosat-IODC"))


def select_candidates(args: argparse.Namespace) -> list[dict[str, Any]]:
    rows = read_csv(Path(args.inventory_csv))
    selected: list[dict[str, Any]] = []
    for row in rows:
        if row.get("nearest_hour_all_27_complete") != "True":
            continue
        if row.get("candidate_class") != "METEOSAT_DOMINANT_CONTROL":
            continue
        delta = safe_float(row.get("nearest_hour_delta_min"), 999.0)
        if delta > args.max_delta_min:
            continue
        mf = msg_fraction(row)
        if mf < args.min_meteosat_fraction:
            continue
        rr = dict(row)
        rr["time_tag"] = time_tag_from_hour(row["nearest_hour"])
        rr["meteosat_fraction_estimate"] = mf
        rr["selection_reason"] = f"Meteosat-dominant; delta={delta:.2f} min; msg_fraction={mf:.3f}; all_27_complete=True"
        selected.append(rr)
    selected.sort(key=lambda r: (safe_float(r["nearest_hour_delta_min"], 999), -safe_float(r["meteosat_fraction_estimate"])))

    if args.diverse_services:
        out: list[dict[str, Any]] = []
        seen_service: set[str] = set()
        for row in selected:
            service = row.get("dominant_satellite_estimate", "")
            if service not in seen_service:
                out.append(row)
                seen_service.add(service)
            if len(out) >= args.max_samples:
                return out
        for row in selected:
            if row["time_tag"] not in {r["time_tag"] for r in out}:
                out.append(row)
            if len(out) >= args.max_samples:
                return out
        return out
    return selected[: args.max_samples]


def run_candidate(row: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    tag = row["time_tag"]
    out_root = Path(args.runs_root) / tag
    report = out_root / "reports" / f"single_sample_run_report_{tag}.md"
    semantic_metrics = out_root / f"epic_l2_cloud_mask_semantic_sensitivity_{tag}" / "epic_georing_cloud_mask_sensitivity_metrics.csv"
    if args.skip_existing and semantic_metrics.exists():
        return {
            "time_tag": tag,
            "target_time": row.get("nearest_hour", ""),
            "epic_time": row.get("epic_time", ""),
            "status": "SKIPPED_EXISTING",
            "returncode": 0,
            "elapsed_sec": 0.0,
            "report": str(report),
            "metrics": str(semantic_metrics),
        }
    cmd = [
        "conda",
        "run",
        "-n",
        args.conda_env,
        "python",
        str(SCRIPT_DIR / "run_epic_georing_single_sample.py"),
        "--target-time",
        row["nearest_hour"],
        "--time-tag",
        tag,
        "--epic-l2",
        row["epic_file"],
        "--output-root",
        str(out_root),
        "--base-stage-root",
        args.base_stage_root,
    ]
    log_dir = Path(args.out_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"run_{tag}.log"
    start = time.time()
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        log.write(f"# 08h run {tag}\n# start={utc_now()}\n# command={' '.join(cmd)}\n\n")
        proc = subprocess.run(cmd, cwd=str(SCRIPT_DIR), text=True, stdout=log, stderr=subprocess.STDOUT)
        log.write(f"\n# end={utc_now()}\n# returncode={proc.returncode}\n")
    return {
        "time_tag": tag,
        "target_time": row.get("nearest_hour", ""),
        "epic_time": row.get("epic_time", ""),
        "status": "OK" if proc.returncode == 0 else "FAILED",
        "returncode": proc.returncode,
        "elapsed_sec": time.time() - start,
        "report": str(report),
        "metrics": str(semantic_metrics),
        "log": str(log_path),
    }


def collect_metrics_for_run(row: dict[str, Any], cohort: str, note: str) -> list[dict[str, Any]]:
    tag = row.get("time_tag") or time_tag_from_hour(row.get("nearest_hour") or row.get("target_time") or "")
    run_root = RUNS_ROOT / tag
    metrics_path = run_root / f"epic_l2_cloud_mask_semantic_sensitivity_{tag}" / "epic_georing_cloud_mask_sensitivity_metrics.csv"
    metrics = read_csv(metrics_path)
    out: list[dict[str, Any]] = []
    for m in metrics:
        policy = m.get("policy", "")
        out.append(
            {
                "cohort": cohort,
                "time_tag": tag,
                "target_time": row.get("nearest_hour") or row.get("target_time", ""),
                "epic_time": row.get("epic_time", ""),
                "epic_delta_min": row.get("nearest_hour_delta_min") or row.get("epic_delta_min", ""),
                "candidate_class": row.get("candidate_class", ""),
                "dominant_satellite": row.get("dominant_satellite_estimate") or row.get("estimated_dominant_satellite", ""),
                "meteosat_fraction_estimate": row.get("meteosat_fraction_estimate", ""),
                "policy": policy,
                "agreement": m.get("agreement", ""),
                "f1": m.get("f1", ""),
                "iou": m.get("iou", ""),
                "precision": m.get("precision", ""),
                "recall": m.get("recall", ""),
                "valid_fraction_of_epic_earth": m.get("valid_fraction_of_epic_earth", ""),
                "epic_cloud_fraction": m.get("epic_cloud_fraction", ""),
                "geo_cloud_fraction": m.get("geo_cloud_fraction", ""),
                "note": note,
                "metrics_path": str(metrics_path),
            }
        )
    return out


def collect_old_baseline(old_summary: Path) -> list[dict[str, Any]]:
    rows = read_csv(old_summary)
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.get("candidate_class") != "METEOSAT_DOMINANT_CONTROL":
            continue
        tag = row.get("time_tag", "")
        for prefix, policy in [("A", "A_inclusive_binary"), ("B", "B_high_confidence_only"), ("C", "C_uncertainty_aware_3class")]:
            if not row.get(f"{prefix}_agreement"):
                continue
            out.append(
                {
                    "cohort": "old_meteosat_large_time_offset",
                    "time_tag": tag,
                    "target_time": row.get("target_time", ""),
                    "epic_time": row.get("epic_time", ""),
                    "epic_delta_min": row.get("epic_delta_min", ""),
                    "candidate_class": row.get("candidate_class", ""),
                    "dominant_satellite": row.get("estimated_dominant_satellite", ""),
                    "meteosat_fraction_estimate": "",
                    "policy": policy,
                    "agreement": row.get(f"{prefix}_agreement", ""),
                    "f1": row.get(f"{prefix}_f1", ""),
                    "iou": row.get(f"{prefix}_iou", ""),
                    "precision": row.get(f"{prefix}_precision", ""),
                    "recall": row.get(f"{prefix}_recall", ""),
                    "valid_fraction_of_epic_earth": row.get(f"{prefix}_valid_fraction_of_epic_earth", ""),
                    "epic_cloud_fraction": row.get(f"{prefix}_epic_cloud_fraction", ""),
                    "geo_cloud_fraction": row.get(f"{prefix}_geo_cloud_fraction", ""),
                    "note": "旧多样本汇总中的 Meteosat-dominant 大时间差样本",
                    "metrics_path": str(old_summary),
                }
            )
    return out


def summarize_by_cohort(metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in metrics:
        if not row.get("agreement"):
            continue
        groups.setdefault((row["cohort"], row["policy"]), []).append(row)
    out: list[dict[str, Any]] = []
    for (cohort, policy), rows in sorted(groups.items()):
        out.append(
            {
                "cohort": cohort,
                "policy": policy,
                "n": len(rows),
                "delta_min_mean": sum(safe_float(r["epic_delta_min"]) for r in rows) / max(len(rows), 1),
                "agreement_mean": sum(safe_float(r["agreement"]) for r in rows) / max(len(rows), 1),
                "f1_mean": sum(safe_float(r["f1"]) for r in rows if r.get("f1")) / max(sum(1 for r in rows if r.get("f1")), 1),
                "iou_mean": sum(safe_float(r["iou"]) for r in rows if r.get("iou")) / max(sum(1 for r in rows if r.get("iou")), 1),
            }
        )
    return out


def build_plot(metrics: list[dict[str, Any]], out_dir: Path) -> str:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return ""
    rows = [r for r in metrics if r.get("policy") in {"A_inclusive_binary", "B_high_confidence_only"} and r.get("agreement")]
    if not rows:
        return ""
    fig, ax = plt.subplots(figsize=(7.5, 4.5), dpi=160)
    colors = {"old_meteosat_large_time_offset": "#b54a3a", "new_meteosat_time_control": "#2f7dbb"}
    markers = {"A_inclusive_binary": "o", "B_high_confidence_only": "s"}
    for r in rows:
        ax.scatter(
            safe_float(r["epic_delta_min"]),
            safe_float(r["agreement"]),
            s=70,
            c=colors.get(r["cohort"], "#555555"),
            marker=markers.get(r["policy"], "o"),
            edgecolor="black",
            linewidth=0.5,
            alpha=0.9,
        )
        ax.text(safe_float(r["epic_delta_min"]) + 0.3, safe_float(r["agreement"]), r["time_tag"], fontsize=7)
    ax.set_xlabel("EPIC-GEO time offset (min)")
    ax.set_ylabel("Cloud-mask agreement")
    ax.set_title("Meteosat-dominant EPIC comparison: time-offset controlled test")
    ax.grid(True, alpha=0.25)
    out = out_dir / "08h_meteosat_time_offset_agreement_scatter.png"
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    return str(out)


def build_report(out_dir: Path, selected: list[dict[str, Any]], status_rows: list[dict[str, Any]], metrics: list[dict[str, Any]], summary: list[dict[str, Any]], plot_path: str, args: argparse.Namespace) -> Path:
    def fmt(v: Any, nd: int = 3) -> str:
        try:
            return f"{float(v):.{nd}f}"
        except Exception:
            return str(v)

    lines: list[str] = [
        "# 08h Meteosat 时间差控制实验报告",
        "",
        f"- 创建时间 UTC：`{utc_now()}`",
        f"- 候选 inventory：`{args.inventory_csv}`",
        f"- 旧多样本汇总：`{args.old_summary}`",
        f"- 最大允许 EPIC-GEO 时间差：`{args.max_delta_min}` 分钟",
        f"- 最小 Meteosat 估计占比：`{args.min_meteosat_fraction}`",
        f"- 计划样本数：`{args.max_samples}`",
        f"- 是否实际运行样本：`{args.run}`",
        "",
        "## 1. 实验目的",
        "",
        "此前两个 Meteosat-dominant 样本与 EPIC 的时间差分别约 22 和 28 分钟，且一致性最低。这个实验专门控制时间差，筛选 EPIC 与 GEO 整点差值较小的 Meteosat-dominant 样本，用同一套 08c 语义对比流程复测，判断 Meteosat 低一致性到底是时间差主导，还是仍存在源产品/语义/融合策略问题。",
        "",
        "## 2. 新筛选的 Meteosat 小时间差候选",
        "",
    ]
    if selected:
        lines.append("| time_tag | EPIC time | GEO hour | delta min | dominant | Meteosat fraction | 说明 |")
        lines.append("|---|---|---|---:|---|---:|---|")
        for r in selected:
            lines.append(
                f"| `{r['time_tag']}` | `{r['epic_time']}` | `{r['nearest_hour']}` | {fmt(r['nearest_hour_delta_min'], 2)} | "
                f"{r['dominant_satellite_estimate']} | {fmt(r['meteosat_fraction_estimate'])} | {r['selection_reason']} |"
            )
    else:
        lines.append("未找到满足条件的候选。建议放宽 `--max-delta-min` 或降低 `--min-meteosat-fraction`。")
    lines.extend(["", "## 3. 运行状态", ""])
    if status_rows:
        lines.append("| time_tag | status | returncode | elapsed sec | report |")
        lines.append("|---|---|---:|---:|---|")
        for r in status_rows:
            lines.append(f"| `{r['time_tag']}` | {r['status']} | {r['returncode']} | {fmt(r['elapsed_sec'], 1)} | `{r.get('report','')}` |")
    else:
        lines.append("本次只做候选筛选，未运行样本。")

    lines.extend(["", "## 4. 大时间差 baseline 与小时间差复测结果", ""])
    if summary:
        lines.append("| cohort | policy | n | mean delta min | mean agreement | mean F1 | mean IoU |")
        lines.append("|---|---|---:|---:|---:|---:|---:|")
        for r in summary:
            lines.append(
                f"| {r['cohort']} | {r['policy']} | {r['n']} | {fmt(r['delta_min_mean'],2)} | "
                f"{fmt(r['agreement_mean'])} | {fmt(r['f1_mean'])} | {fmt(r['iou_mean'])} |"
            )
    else:
        lines.append("暂无可汇总指标。若新样本未运行，只有候选筛选结果。")
    if plot_path:
        lines.extend(["", f"![Meteosat time-offset scatter]({plot_path})", ""])

    lines.extend(
        [
            "",
            "## 5. 判读规则",
            "",
            "- 如果小时间差 Meteosat 样本的 agreement / F1 明显接近 GOES 或 FY4B/Himawari 水平，说明此前 Meteosat 低值主要由时间差放大。",
            "- 如果小时间差样本仍显著低于其他 source-dominant 样本，则 Meteosat CLM 语义、产品质量、边缘几何或当前融合策略仍是主要嫌疑。",
            "- 如果改善有限但存在改善，则应表述为：时间差是重要放大因素，但不是唯一根源。",
            "",
            "## 6. 当前结论",
            "",
        ]
    )
    # Automated cautious conclusion.
    old_a = [r for r in summary if r["cohort"] == "old_meteosat_large_time_offset" and r["policy"] == "A_inclusive_binary"]
    new_a = [r for r in summary if r["cohort"] == "new_meteosat_time_control" and r["policy"] == "A_inclusive_binary"]
    if old_a and new_a:
        diff = safe_float(new_a[0]["agreement_mean"]) - safe_float(old_a[0]["agreement_mean"])
        if diff >= 0.10:
            conclusion = "小时间差复测相对旧 Meteosat baseline 有明显提升，时间差很可能是此前低一致性的主导因素之一；但仍需结合 F1/IoU 和 source-specific 诊断确认是否达到其他卫星水平。"
        elif diff >= 0.03:
            conclusion = "小时间差复测有一定提升，说明时间差会放大 Meteosat mismatch；但提升幅度不足以单独解释全部差异，仍需保留 Meteosat 语义/源产品 warning。"
        else:
            conclusion = "小时间差复测没有明显提升，当前证据更支持 Meteosat CLM 语义、源产品或融合策略问题，而不是单纯时间差问题。"
        lines.append(conclusion)
    else:
        lines.append("当前还不能给出复测结论；请先运行所选样本并重新生成报告。")

    lines.extend(
        [
            "",
            "## 7. 输出文件",
            "",
            f"- 候选清单：`{out_dir / '08h_meteosat_time_offset_candidates.csv'}`",
            f"- 运行状态：`{out_dir / '08h_meteosat_time_offset_run_status.csv'}`",
            f"- 明细指标：`{out_dir / '08h_meteosat_time_offset_metrics.csv'}`",
            f"- 汇总指标：`{out_dir / '08h_meteosat_time_offset_summary.csv'}`",
        ]
    )
    report = out_dir / "08h_meteosat_time_offset_control_report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="08h time-offset controlled Meteosat-dominant EPIC/GEO-ring test.")
    p.add_argument("--inventory-csv", default=str(DEFAULT_INVENTORY))
    p.add_argument("--old-summary", default=str(DEFAULT_OLD_SUMMARY))
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    p.add_argument("--runs-root", default=str(RUNS_ROOT))
    p.add_argument("--base-stage-root", default=r"D:\AAAresearch_paper\geo_ring_cloud_stage1")
    p.add_argument("--max-delta-min", type=float, default=5.0)
    p.add_argument("--min-meteosat-fraction", type=float, default=0.55)
    p.add_argument("--max-samples", type=int, default=3)
    p.add_argument("--diverse-services", action="store_true", help="Prefer at least one Meteosat-0deg and one IODC candidate if available.")
    p.add_argument("--run", action="store_true", help="Actually run the selected samples through the existing single-sample pipeline.")
    p.add_argument("--skip-existing", action="store_true")
    p.add_argument("--conda-env", default="pytorch")
    return p


def main() -> int:
    args = build_parser().parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    selected = select_candidates(args)
    cand_fields = [
        "time_tag",
        "epic_file",
        "epic_time",
        "nearest_hour",
        "nearest_hour_delta_min",
        "candidate_class",
        "dominant_satellite_estimate",
        "dominant_fraction_estimate",
        "meteosat_fraction_estimate",
        "source_fraction_json",
        "nearest_hour_all_27_complete",
        "selection_reason",
    ]
    write_csv(out_dir / "08h_meteosat_time_offset_candidates.csv", selected, cand_fields)

    status_rows: list[dict[str, Any]] = []
    if args.run:
        for row in selected:
            status_rows.append(run_candidate(row, args))
    write_csv(out_dir / "08h_meteosat_time_offset_run_status.csv", status_rows, ["time_tag", "target_time", "epic_time", "status", "returncode", "elapsed_sec", "report", "metrics", "log"])

    metrics: list[dict[str, Any]] = []
    metrics.extend(collect_old_baseline(Path(args.old_summary)))
    for row in selected:
        metrics.extend(collect_metrics_for_run(row, "new_meteosat_time_control", "08h 小时间差 Meteosat-dominant 复测样本"))
    metric_fields = [
        "cohort",
        "time_tag",
        "target_time",
        "epic_time",
        "epic_delta_min",
        "candidate_class",
        "dominant_satellite",
        "meteosat_fraction_estimate",
        "policy",
        "agreement",
        "f1",
        "iou",
        "precision",
        "recall",
        "valid_fraction_of_epic_earth",
        "epic_cloud_fraction",
        "geo_cloud_fraction",
        "note",
        "metrics_path",
    ]
    write_csv(out_dir / "08h_meteosat_time_offset_metrics.csv", metrics, metric_fields)
    summary = summarize_by_cohort(metrics)
    write_csv(out_dir / "08h_meteosat_time_offset_summary.csv", summary, ["cohort", "policy", "n", "delta_min_mean", "agreement_mean", "f1_mean", "iou_mean"])
    plot_path = build_plot(metrics, out_dir)
    report = build_report(out_dir, selected, status_rows, metrics, summary, plot_path, args)
    print(f"08h report={report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
