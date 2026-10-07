"""Render seed-ensemble coarse-EPIC information-gain maps for frozen tests."""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import yaml

from common import DATA, FIGURES, METRICS, PREDICTIONS, configured_inputs, ensure_layout, resolve_scene_rows, split_for_date, write_csv, write_manifest


def prediction_ensemble(model: str, sample_id: str, seeds: list[int]) -> np.ndarray:
    values = []
    for seed in seeds:
        path = PREDICTIONS / model / f"seed_{seed}" / f"{sample_id}.npz"
        if path.exists():
            with np.load(path, allow_pickle=False) as data:
                values.append(data["prediction"].astype(np.float32))
    if not values:
        raise FileNotFoundError(f"No {model} prediction is available for {sample_id}")
    return np.mean(values, axis=0, dtype=np.float32)


def render(path: Path, values: np.ndarray, valid: np.ndarray, limit: float, title: str) -> None:
    yy, xx = np.where(valid)
    crop = values[yy.min():yy.max() + 1, xx.min():xx.max() + 1]
    fig, axis = plt.subplots(figsize=(6.2, 5.4), constrained_layout=True)
    image = axis.imshow(crop, cmap="RdBu_r", vmin=-limit, vmax=limit, interpolation="nearest")
    axis.set_axis_off(); axis.set_title(title)
    colour_bar = fig.colorbar(image, ax=axis, shrink=0.82)
    colour_bar.set_label("|target − G| − |target − C+G| (positive: coarse EPIC helps)")
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    ensure_layout()
    cfg, _, _, manifest = configured_inputs()
    cnn = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "cnn.yaml").read_text())
    rows = [row for row in resolve_scene_rows() if split_for_date(row["scene_date"], cfg) == "test"]
    out = FIGURES / "information_gain_maps"
    out.mkdir(parents=True, exist_ok=True)
    summary: list[dict[str, object]] = []
    total: np.ndarray | None = None
    count: np.ndarray | None = None
    for row in rows:
        scene = row["sample_id"]
        with np.load(DATA / "prepared" / f"{scene}.npz", allow_pickle=False) as base:
            target, valid = base["target"].astype(np.float32), base["valid_mask"].astype(bool)
        gain = np.abs(target - prediction_ensemble("cnn_g", scene, cnn["seeds"])) - np.abs(target - prediction_ensemble("cnn_cg", scene, cnn["seeds"]))
        gain[~valid] = np.nan
        np.save(out / f"{scene}_gain_epic.npy", gain)
        if total is None:
            total, count = np.zeros_like(gain, dtype=np.float64), np.zeros_like(gain, dtype=np.int32)
        finite = np.isfinite(gain); total[finite] += gain[finite]; count[finite] += 1
        summary.append({"scene_id": scene, "gain_definition": "abs(target-pred_G)-abs(target-pred_CG), seed-ensemble mean", "gain_epic_mae": float(np.nanmean(gain)), "improved_fraction": float(np.nanmean(gain > 0)), "valid_pixels": int(valid.sum())})
    if total is None or count is None:
        raise RuntimeError("No complete test-scene prediction was found")
    aggregate = np.full_like(total, np.nan, dtype=np.float32); use = count > 0; aggregate[use] = total[use] / count[use]
    np.save(out / "test_period_aggregate_gain_epic.npy", aggregate)
    ranked = sorted(summary, key=lambda item: float(item["gain_epic_mae"]))
    selected = []
    for index in (0, len(ranked) // 2, len(ranked) - 1):
        scene = str(ranked[index]["scene_id"])
        if scene not in selected: selected.append(scene)
    limit = float(max(np.nanpercentile(np.abs(np.load(out / f"{scene}_gain_epic.npy", allow_pickle=False)), 99) for scene in selected))
    images = []
    for scene in selected:
        gain = np.load(out / f"{scene}_gain_epic.npy", allow_pickle=False)
        with np.load(DATA / "prepared" / f"{scene}.npz", allow_pickle=False) as base: valid = base["valid_mask"].astype(bool)
        image = out / f"{scene}_gain_epic.png"; render(image, gain, valid, limit, f"{scene}: coarse-EPIC information gain"); images.append(image)
    aggregate_image = out / "test_period_aggregate_gain_epic.png"; render(aggregate_image, aggregate, np.isfinite(aggregate), limit, "Test-period mean coarse-EPIC information gain"); images.append(aggregate_image)
    table = METRICS / "gain_by_scene.csv"; write_csv(table, summary)
    write_manifest("11_information_gain_maps", Path(__file__), [manifest], [table, *images], {"prediction_aggregation": "mean over trained seeds", "selected_map_rule": "lowest, median, highest scene mean gain", "colour_limit": limit})
    print(table)


if __name__ == "__main__": sys.exit(main())
