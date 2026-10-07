"""Shared data, tiling and closure helpers for the fixed CNN ablation."""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

import numpy as np

from common import DATA

C = ["coarse_cloud_fraction_up", "coarse_valid_fraction_up"]
G = ["geo_cloud_fraction", "geo_cloud_fraction_std", "geo_boundary_fraction", "geo_source_count", "geo_valid_fraction", "geo_time_difference_minutes", "geo_missing_disagreement_mask", "geo_missing_vza_mask"]
Z = ["epic_vza", "epic_sza", "land_ocean", "valid_mask"]


def require_torch():
    try:
        import torch
        return torch
    except BaseException as exc:
        raise RuntimeError("PyTorch is unavailable in this project's .venv. Install requirements.txt with the CPU PyTorch index.") from exc


def feature_names(groups: list[str]) -> list[str]:
    return sum(({"C": C, "G": G, "Z": Z}[group] for group in groups), [])


def load_scene(scene: Path, names: list[str], stats: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return normalized channels and the frozen 50 km cell operator fields."""
    with np.load(scene, allow_pickle=False) as z:
        valid = z["valid_mask"].astype(bool)
        target = z["target"].astype(np.float32)
        channels = []
        for name in names:
            value = z[name].astype(np.float32)
            info = stats.get(name)
            if info:
                value = (value - info["mean"]) / info["std"]
            channels.append(np.nan_to_num(value, nan=0.0, posinf=0.0, neginf=0.0))
    with np.load(DATA / "coarse50" / f"{scene.stem}_coarse50.npz", allow_pickle=False) as z:
        coarse_up = z["coarse_cloud_fraction_up"].astype(np.float32)
        cell_id = z["cell_id"].astype(np.int32)
        coarse_by_cell = z["cloud_fraction_by_cell"].astype(np.float32)
    return np.stack(channels), target, valid, coarse_up, cell_id, coarse_by_cell


class ScenePatchDataset:
    """Scene-grouped, LRU-cached patch reader for CPU training.

    Each compressed prepared scene is decoded at most once while its patch group
    is traversed. Patch order is random within each scene and scene order is
    random each epoch, so this changes I/O only—not the frozen sample set.
    """
    def __init__(self, index: np.ndarray, names: list[str], stats: dict, cache_scenes: int = 2):
        self.rows = list(index)
        self.names, self.stats, self.cache_scenes = names, stats, cache_scenes
        self.by_scene: dict[str, list[int]] = {}
        for i, row in enumerate(self.rows):
            self.by_scene.setdefault(Path(row["scene_file"]).name, []).append(i)
        self.scene_ordinal = {name: i for i, name in enumerate(sorted(self.by_scene))}
        self.order = list(range(len(self.rows)))
        self.cache: OrderedDict[str, tuple[np.ndarray, ...]] = OrderedDict()

    def set_epoch(self, seed: int) -> None:
        rng = np.random.default_rng(seed)
        groups = list(self.by_scene.values())
        rng.shuffle(groups)
        self.order = [i for group in groups for i in rng.permutation(group)]

    def __len__(self) -> int:
        return len(self.rows)

    def _scene(self, name: str) -> tuple[np.ndarray, ...]:
        if name not in self.cache:
            x, y, valid, _, cell, coarse_cell = load_scene(DATA / "prepared" / name, self.names, self.stats)
            key = np.where(cell >= 0, cell + self.scene_ordinal[name] * 1_000_000, -1).astype(np.int64)
            target_coarse = np.full(cell.shape, np.nan, dtype=np.float32)
            usable = cell >= 0
            target_coarse[usable] = coarse_cell[cell[usable]]
            self.cache[name] = (x, y, valid, key, target_coarse)
            if len(self.cache) > self.cache_scenes:
                self.cache.popitem(last=False)
        else:
            self.cache.move_to_end(name)
        return self.cache[name]

    def __getitem__(self, i: int):
        row = self.rows[self.order[i]]
        x, y, valid, key, target_coarse = self._scene(Path(row["scene_file"]).name)
        r, c, size = int(row["row"]), int(row["col"]), int(row["size"])
        return (x[:, r:r + size, c:c + size], y[r:r + size, c:c + size], valid[r:r + size, c:c + size].astype(np.float32), key[r:r + size, c:c + size], target_coarse[r:r + size, c:c + size])


def closure_mse(probability, valid, cell_key, target_coarse):
    """Cell-mean closure loss using the frozen native→50 km cell membership.

    A training batch is a stochastic subset of full 50 km cells; each retained
    cell mean is an unbiased patch-level estimator of D(prediction). Exact
    full-scene closure is always recomputed during final evaluation.
    """
    import torch
    use = valid.bool() & (cell_key >= 0) & torch.isfinite(target_coarse)
    if not bool(use.any()):
        return probability.new_zeros(())
    keys = cell_key[use].long()
    _, inverse = torch.unique(keys, return_inverse=True)
    count = torch.bincount(inverse).to(probability.dtype)
    p_sum = torch.zeros(len(count), dtype=probability.dtype, device=probability.device).scatter_add_(0, inverse, probability[use])
    t_sum = torch.zeros(len(count), dtype=probability.dtype, device=probability.device).scatter_add_(0, inverse, target_coarse[use])
    return torch.mean(((p_sum / count) - (t_sum / count)) ** 2)


def predict_tiled(model, channels: np.ndarray, device: str, tile_size: int, halo: int) -> np.ndarray:
    """Predict a full native scene in tiles while preserving the CNN receptive field."""
    torch = require_torch()
    _, height, width = channels.shape
    output = np.empty((height, width), dtype=np.float32)
    model.eval()
    with torch.no_grad():
        for top in range(0, height, tile_size):
            bottom = min(height, top + tile_size)
            for left in range(0, width, tile_size):
                right = min(width, left + tile_size)
                top_h, bottom_h = max(0, top - halo), min(height, bottom + halo)
                left_h, right_h = max(0, left - halo), min(width, right + halo)
                tensor = torch.from_numpy(channels[:, top_h:bottom_h, left_h:right_h][None]).to(device)
                prediction = torch.sigmoid(model(tensor)).squeeze().cpu().numpy()
                output[top:bottom, left:right] = prediction[top - top_h:bottom - top_h, left - left_h:right - left_h]
    return output
