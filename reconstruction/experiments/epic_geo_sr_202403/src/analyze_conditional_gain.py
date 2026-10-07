"""Describe GEO and coarse-EPIC information gains on frozen test scenes."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from common import DATA, METRICS, PREDICTIONS, REPORTS, configured_inputs, resolve_scene_rows, split_for_date, write_csv, write_manifest


SEEDS = (42, 43, 44)
RNG_SEED = 20240923
N_BOOTSTRAP = 10_000
REGIMES = {
    "geo_boundary_fraction": ("geo_boundary_fraction", [0.0, 0.1, 0.3, 0.5, np.inf], ["0–0.1", "0.1–0.3", "0.3–0.5", ">0.5"]),
    "geo_cloud_fraction": ("geo_cloud_fraction", [0.0, 0.1, 0.3, 0.5, np.inf], ["0–0.1", "0.1–0.3", "0.3–0.5", ">0.5"]),
    "geo_time_difference_minutes": ("geo_time_difference_minutes", [0.0, 5.0, 15.0, 30.0, np.inf], ["0–5", "5–15", "15–30", ">30"]),
    "geo_source_count": ("geo_source_count", [-np.inf, 1.5, 2.5, np.inf], ["1", "2", "3+"]),
}


def mean_prediction(model: str, sample_id: str) -> np.ndarray:
    arrays = []
    for seed in SEEDS:
        path = PREDICTIONS / model / f"seed_{seed}" / f"{sample_id}.npz"
        if path.exists():
            with np.load(path, allow_pickle=False) as data:
                arrays.append(data["prediction"].astype(np.float32))
    if not arrays:
        raise FileNotFoundError(f"No predictions for {model}, {sample_id}")
    return np.mean(arrays, axis=0, dtype=np.float32)


def scene_value(delta: np.ndarray, mask: np.ndarray) -> float:
    return float(np.mean(delta[mask])) if np.any(mask) else np.nan


def main() -> None:
    cfg, _, _, manifest = configured_inputs()
    rows = [row for row in resolve_scene_rows() if split_for_date(row["scene_date"], cfg) == "test"]
    scene_values: dict[tuple[str, str], list[dict[str, object]]] = {}
    for row in rows:
        sample_id = row["sample_id"]
        with np.load(DATA / "prepared" / f"{sample_id}.npz", allow_pickle=False) as base:
            target = base["target"].astype(np.float32)
            valid = base["valid_mask"].astype(bool)
            features = {field: base[key].astype(np.float32) for field, (key, _, _) in REGIMES.items()}
        pred_c = mean_prediction("cnn_c", sample_id)
        pred_g = mean_prediction("cnn_g", sample_id)
        pred_cg = mean_prediction("cnn_cg", sample_id)
        pred_closure = mean_prediction("cnn_cg_closure", sample_id)
        # Negative delta means the GEO-enabled model lowers squared probability error.
        brier_delta_cg = (pred_cg - target) ** 2 - (pred_c - target) ** 2
        brier_delta_epic = (pred_cg - target) ** 2 - (pred_g - target) ** 2
        brier_delta_closure = (pred_closure - target) ** 2 - (pred_cg - target) ** 2
        for regime, (key, edges, labels) in REGIMES.items():
            feature = features[regime]
            for lo, hi, label in zip(edges[:-1], edges[1:], labels):
                mask = valid & np.isfinite(feature) & (feature >= lo) & (feature < hi)
                scene_values.setdefault((regime, label), []).append({
                    "scene_id": sample_id,
                    "n_pixels": int(mask.sum()),
                    "brier_delta_cg_minus_c": scene_value(brier_delta_cg, mask),
                    "brier_delta_cg_minus_g": scene_value(brier_delta_epic, mask),
                    "brier_delta_closure_minus_cg": scene_value(brier_delta_closure, mask),
                })
    rng = np.random.default_rng(RNG_SEED)
    result: list[dict[str, object]] = []
    for (regime, label), values in scene_values.items():
        for metric, direction in (("brier_delta_cg_minus_c", "lower"), ("brier_delta_cg_minus_g", "lower"), ("brier_delta_closure_minus_cg", "lower")):
            finite = [value for value in values if np.isfinite(value[metric]) and value["n_pixels"] > 0]
            delta = np.asarray([value[metric] for value in finite], dtype=float)
            if len(delta) == 0:
                continue
            indices = rng.integers(0, len(delta), size=(N_BOOTSTRAP, len(delta)))
            boot = delta[indices].mean(axis=1)
            result.append({
                "regime": regime,
                "bin": label,
                "comparison": {"brier_delta_cg_minus_c": "C+G versus C", "brier_delta_cg_minus_g": "C+G versus G", "brier_delta_closure_minus_cg": "C+G+closure versus C+G"}[metric],
                "metric": "Brier delta (candidate − reference)",
                "n_scenes": len(delta),
                "total_pixels_across_scene_groups": int(sum(value["n_pixels"] for value in finite)),
                "mean_scene_delta": float(delta.mean()),
                "median_scene_delta": float(np.median(delta)),
                "ci95_low": float(np.quantile(boot, 0.025)),
                "ci95_high": float(np.quantile(boot, 0.975)),
                "candidate_better_probability": float((boot < 0).mean()),
                "bootstrap_replicates": N_BOOTSTRAP,
                "bootstrap_rng_seed": RNG_SEED,
                "interpretation": "negative is better",
            })
    output = METRICS / "conditional_brier_gain.csv"
    write_csv(output, result)
    report = REPORTS / "08_conditional_information_gain.md"
    lines = [
        "# 条件信息增益", "",
        "- 指标为每个完整测试场景内的 Brier 差值（candidate − reference）；负值代表候选模型更好。",
        "- 每个分组以完整场景为重采样单元，进行 10,000 次 bootstrap；像元数仅描述覆盖范围，不作为独立样本数。",
        "- 分组不一定覆盖所有场景，空分组不报告；样本量小的分组应视为探索性结果。", "",
        "| regime | bin | comparison | scenes | mean delta | 95% CI | P(candidate better) |", "|---|---|---|---:|---:|---:|---:|",
    ]
    for row in result:
        lines.append(f"| {row['regime']} | {row['bin']} | {row['comparison']} | {row['n_scenes']} | {row['mean_scene_delta']:.6g} | [{row['ci95_low']:.6g}, {row['ci95_high']:.6g}] | {row['candidate_better_probability']:.3f} |")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_manifest(
        "10_conditional_information_gain", Path(__file__), [manifest], [output, report],
        {"test_scene_count": len(rows), "regimes": {name: {"feature": spec[0], "edges": spec[1], "labels": spec[2]} for name, spec in REGIMES.items()},
         "unit_of_resampling": "complete scene within regime", "bootstrap_replicates": N_BOOTSTRAP, "rng_seed": RNG_SEED},
    )
    print(output)


if __name__ == "__main__":
    sys.exit(main())
