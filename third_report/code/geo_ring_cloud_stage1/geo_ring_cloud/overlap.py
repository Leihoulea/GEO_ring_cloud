"""Reusable overlap-validation metrics, boundaries, and quicklook helpers."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, ListedColormap

from .fusion_support import VARIABLE_RULES


COMPONENT_ROLE = "overlap_metrics"


def build_variable_to_product() -> dict[tuple[str, str], str]:
    """Map each source/variable pair to the configured upstream product."""
    mapping: dict[tuple[str, str], str] = {}
    for variable, rules in VARIABLE_RULES.items():
        for rule in rules:
            mapping[(rule["satellite"], variable)] = rule["product"]
    return mapping


def confusion_from_binary(a: np.ndarray, b: np.ndarray) -> dict[str, int]:
    """Return a binary confusion matrix using ``a`` as reference."""
    return {
        "tn": int(np.count_nonzero((a == 0) & (b == 0))),
        "fp": int(np.count_nonzero((a == 0) & (b == 1))),
        "fn": int(np.count_nonzero((a == 1) & (b == 0))),
        "tp": int(np.count_nonzero((a == 1) & (b == 1))),
    }


def make_simple_quicklook(
    data: np.ndarray,
    out_path: Path,
    title: str,
    cmap: str = "viridis",
    vmin: float | None = None,
    vmax: float | None = None,
    categorical_labels: dict[int, str] | None = None,
) -> None:
    """Render the established geographic overlap quicklook."""
    arr = np.asarray(data)
    stride = max(1, int(math.ceil(math.sqrt(arr.size / 1_200_000)))) if arr.size > 1_200_000 else 1
    plot = arr[::stride, ::stride]
    plt.figure(figsize=(11, 4.8), dpi=150)
    if categorical_labels is not None:
        finite = np.isfinite(plot)
        values = np.sort(np.unique(plot[finite])) if finite.any() else np.asarray([])
        colors = plt.get_cmap("tab10")(np.linspace(0, 1, max(2, values.size if values.size else 2)))
        cmap_obj = ListedColormap(colors)
        cmap_obj.set_bad("#ffffff")
        if values.size == 0:
            im = plt.imshow(
                plot,
                extent=[-180, 180, -90, 90],
                origin="lower",
                cmap=cmap_obj,
                interpolation="nearest",
            )
        else:
            bounds = (
                np.concatenate(
                    ([values[0] - 0.5], (values[:-1] + values[1:]) / 2.0, [values[-1] + 0.5])
                )
                if values.size > 1
                else np.array([values[0] - 0.5, values[0] + 0.5])
            )
            norm = BoundaryNorm(bounds, cmap_obj.N)
            im = plt.imshow(
                plot,
                extent=[-180, 180, -90, 90],
                origin="lower",
                cmap=cmap_obj,
                norm=norm,
                interpolation="nearest",
            )
            cbar = plt.colorbar(im, shrink=0.78, ticks=values)
            cbar.ax.set_yticklabels([categorical_labels.get(int(v), str(int(v))) for v in values])
            plt.title(title, fontsize=10)
            plt.xlabel("Longitude")
            plt.ylabel("Latitude")
            plt.tight_layout()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            plt.savefig(out_path)
            plt.close()
            return
    else:
        im = plt.imshow(
            plot,
            extent=[-180, 180, -90, 90],
            origin="lower",
            cmap=cmap,
            interpolation="nearest",
            vmin=vmin,
            vmax=vmax,
        )
        plt.colorbar(im, shrink=0.78)
    plt.title(title, fontsize=10)
    plt.xlabel("Longitude")
    plt.ylabel("Latitude")
    plt.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_path)
    plt.close()


def neighbor_edge_arrays(
    values: np.ndarray,
    valid: np.ndarray,
    source_map: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Collect absolute neighbor differences across and within source boundaries."""
    value = np.asarray(values, dtype=np.float32)
    valid = np.asarray(valid, dtype=bool)
    source_map = np.asarray(source_map, dtype=np.int16)
    diffs_boundary: list[np.ndarray] = []
    diffs_same: list[np.ndarray] = []
    for axis in [0, 1]:
        s1 = [slice(None), slice(None)]
        s2 = [slice(None), slice(None)]
        s1[axis] = slice(1, None)
        s2[axis] = slice(None, -1)
        v1 = value[tuple(s1)]
        v2 = value[tuple(s2)]
        ok = valid[tuple(s1)] & valid[tuple(s2)] & np.isfinite(v1) & np.isfinite(v2)
        same = ok & (source_map[tuple(s1)] == source_map[tuple(s2)])
        boundary = ok & (source_map[tuple(s1)] != source_map[tuple(s2)])
        if np.any(boundary):
            diffs_boundary.append(np.abs(v1[boundary] - v2[boundary]))
        if np.any(same):
            diffs_same.append(np.abs(v1[same] - v2[same]))
    return (
        np.concatenate(diffs_boundary) if diffs_boundary else np.asarray([], dtype=np.float32),
        np.concatenate(diffs_same) if diffs_same else np.asarray([], dtype=np.float32),
    )


def compute_boundary_mask(source_map: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Mark valid pixels adjacent to a different selected source."""
    src = np.asarray(source_map, dtype=np.int16)
    valid = np.asarray(valid, dtype=bool)
    out = np.zeros(src.shape, dtype=bool)
    out[1:, :] |= valid[1:, :] & valid[:-1, :] & (src[1:, :] != src[:-1, :])
    out[:-1, :] |= valid[:-1, :] & valid[1:, :] & (src[:-1, :] != src[1:, :])
    out[:, 1:] |= valid[:, 1:] & valid[:, :-1] & (src[:, 1:] != src[:, :-1])
    out[:, :-1] |= valid[:, :-1] & valid[:, 1:] & (src[:, :-1] != src[:, 1:])
    return out


__all__ = [
    "build_variable_to_product",
    "compute_boundary_mask",
    "confusion_from_binary",
    "make_simple_quicklook",
    "neighbor_edge_arrays",
]
