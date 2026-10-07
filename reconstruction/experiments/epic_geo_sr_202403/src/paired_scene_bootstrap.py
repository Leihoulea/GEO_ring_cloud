"""Quantify CNN ablation differences with a paired, scene-level bootstrap."""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np

from common import METRICS, REPORTS, configured_inputs, resolve_scene_rows, split_for_date, write_csv, write_manifest


RNG_SEED = 20240923
N_BOOTSTRAP = 10_000
COMPARISONS = [
    ("cnn_cg", "cnn_c", "C+G versus C"),
    ("cnn_cg", "cnn_g", "C+G versus G"),
    ("cnn_cg_closure", "cnn_cg", "C+G+closure versus C+G"),
]
METRICS_TO_COMPARE = {
    "Brier": "lower",
    "BCE": "lower",
    "SSIM": "higher",
    "boundary_F1": "higher",
    "highpass_RMSE": "lower",
    "highpass_corr": "higher",
    "gradient_corr": "higher",
    "closure_MAE": "lower",
}


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    cfg, _, _, manifest = configured_inputs()
    expected_scenes = sorted(
        row["sample_id"] for row in resolve_scene_rows() if split_for_date(row["scene_date"], cfg) == "test"
    )
    source = METRICS / "scene_metrics.csv"
    raw = read_rows(source)
    # Average the three trained seed results within a scene first.  Each bootstrap
    # resample then draws whole scenes, retaining the paired spatial dependence.
    values: dict[tuple[str, str], dict[str, list[float]]] = {}
    for row in raw:
        key = (row["model"], row["scene_id"])
        bucket = values.setdefault(key, {metric: [] for metric in METRICS_TO_COMPARE})
        for metric in METRICS_TO_COMPARE:
            value = float(row[metric])
            if np.isfinite(value):
                bucket[metric].append(value)
    per_scene = {
        key: {metric: float(np.mean(series)) if series else np.nan for metric, series in bucket.items()}
        for key, bucket in values.items()
    }
    rng = np.random.default_rng(RNG_SEED)
    result: list[dict[str, object]] = []
    for candidate, reference, label in COMPARISONS:
        observed = sorted(set(scene for model, scene in per_scene if model == candidate) & set(scene for model, scene in per_scene if model == reference))
        if observed != expected_scenes:
            raise RuntimeError(f"{label}: paired test scenes do not match frozen test split")
        for metric, direction in METRICS_TO_COMPARE.items():
            candidate_values = np.asarray([per_scene[(candidate, scene)][metric] for scene in observed])
            reference_values = np.asarray([per_scene[(reference, scene)][metric] for scene in observed])
            keep = np.isfinite(candidate_values) & np.isfinite(reference_values)
            delta = candidate_values[keep] - reference_values[keep]
            if len(delta) < 2:
                raise RuntimeError(f"{label}, {metric}: fewer than two finite paired scenes")
            indices = rng.integers(0, len(delta), size=(N_BOOTSTRAP, len(delta)))
            boot_mean = delta[indices].mean(axis=1)
            better_scene = delta < 0 if direction == "lower" else delta > 0
            better_bootstrap = boot_mean < 0 if direction == "lower" else boot_mean > 0
            result.append({
                "comparison": label,
                "candidate": candidate,
                "reference": reference,
                "metric": metric,
                "better_direction": direction,
                "n_paired_test_scenes": int(len(delta)),
                "seed_aggregation": "mean within scene across available trained seeds",
                "mean_delta_candidate_minus_reference": float(delta.mean()),
                "median_delta_candidate_minus_reference": float(np.median(delta)),
                "candidate_better_scene_fraction": float(better_scene.mean()),
                "bootstrap_mean_delta_ci95_low": float(np.quantile(boot_mean, 0.025)),
                "bootstrap_mean_delta_ci95_high": float(np.quantile(boot_mean, 0.975)),
                "bootstrap_candidate_better_probability": float(better_bootstrap.mean()),
                "bootstrap_replicates": N_BOOTSTRAP,
                "bootstrap_rng_seed": RNG_SEED,
            })
    output = METRICS / "paired_scene_bootstrap.csv"
    write_csv(output, result)
    report = REPORTS / "07_paired_scene_bootstrap.md"
    lines = [
        "# CNN 消融：按场景配对 bootstrap", "",
        f"- 冻结测试集：{len(expected_scenes)} 个完整场景。",
        f"- 每景先对可用训练种子求均值，再对场景进行 {N_BOOTSTRAP:,} 次有放回配对重采样（随机种子 {RNG_SEED}）。",
        "- 差值定义为 candidate − reference；对低值更优指标，负值代表 candidate 更好；对高值更优指标，正值代表 candidate 更好。",
        "- 此不确定性反映测试场景抽样，不应解释为独立像元的置信区间。",
        "",
        "| comparison | metric | mean delta | 95% CI | candidate-better probability |", "|---|---:|---:|---:|---:|",
    ]
    for row in result:
        lines.append(
            f"| {row['comparison']} | {row['metric']} | {row['mean_delta_candidate_minus_reference']:.6g} | "
            f"[{row['bootstrap_mean_delta_ci95_low']:.6g}, {row['bootstrap_mean_delta_ci95_high']:.6g}] | "
            f"{row['bootstrap_candidate_better_probability']:.3f} |"
        )
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_manifest(
        "09_paired_scene_bootstrap", Path(__file__), [manifest, source], [output, report],
        {"test_scenes": expected_scenes, "bootstrap_replicates": N_BOOTSTRAP, "rng_seed": RNG_SEED,
         "unit_of_resampling": "complete test scene", "seed_aggregation": "within-scene mean"},
    )
    print(output)


if __name__ == "__main__":
    sys.exit(main())
