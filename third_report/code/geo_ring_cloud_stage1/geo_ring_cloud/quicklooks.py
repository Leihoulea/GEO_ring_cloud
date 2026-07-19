"""Representative quicklook rendering for normalized cloud products."""

from __future__ import annotations

import math
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, ListedColormap

from .cloud_semantics import CATEGORICAL_VARS, CATEGORY_FILL_VALUES


COMPONENT_ROLE = "quicklook_renderer"


def make_quicklook(arr: np.ndarray, out_path: Path, title: str, variable_name: str) -> None:
    """Render a bounded-memory categorical or continuous product quicklook."""
    a = np.asarray(arr)
    if a.ndim == 0:
        return
    if a.ndim == 1:
        a = np.tile(a[np.newaxis, :], (64, 1))
    if a.ndim > 2:
        a = np.squeeze(a)
        if a.ndim > 2:
            a = a.reshape(a.shape[-2], a.shape[-1])
    max_pixels = 1_200_000
    stride = max(1, int(math.ceil(math.sqrt(a.size / max_pixels)))) if a.size > max_pixels else 1
    plot = a[::stride, ::stride]
    plt.figure(figsize=(8, 5), dpi=140)
    if variable_name in CATEGORICAL_VARS:
        plot_float = plot.astype(np.float32, copy=True)
        for fill in CATEGORY_FILL_VALUES.get(variable_name, set()):
            plot_float[np.isclose(plot_float, fill)] = np.nan
        finite = np.isfinite(plot_float)
        if finite.any():
            values = np.unique(plot_float[finite])
            if values.size <= 32:
                values = np.sort(values)
                boundaries = np.concatenate(
                    ([values[0] - 0.5], (values[:-1] + values[1:]) / 2.0, [values[-1] + 0.5])
                )
                base_colors = plt.get_cmap("tab20")(np.linspace(0, 1, max(1, values.size)))
                cmap = ListedColormap(base_colors)
                cmap.set_bad((0.92, 0.92, 0.92, 1.0))
                norm = BoundaryNorm(boundaries, cmap.N)
                im = plt.imshow(plot_float, interpolation="nearest", cmap=cmap, norm=norm)
                cbar = plt.colorbar(im, shrink=0.75, ticks=values)
                cbar.ax.set_yticklabels(
                    [str(int(v)) if float(v).is_integer() else f"{v:g}" for v in values]
                )
            else:
                im = plt.imshow(plot_float, interpolation="nearest", cmap="tab20")
                plt.colorbar(im, shrink=0.75)
        else:
            im = plt.imshow(plot_float, interpolation="nearest", cmap="gray")
            plt.colorbar(im, shrink=0.75)
    else:
        finite = np.isfinite(plot) if plot.dtype.kind == "f" else np.ones(plot.shape, dtype=bool)
        if finite.any():
            vmin, vmax = np.nanpercentile(plot.astype(float), [2, 98])
            if not np.isfinite(vmin) or not np.isfinite(vmax) or vmin == vmax:
                vmin, vmax = None, None
        else:
            vmin, vmax = None, None
        im = plt.imshow(plot, interpolation="nearest", cmap="viridis", vmin=vmin, vmax=vmax)
        plt.colorbar(im, shrink=0.75)
    plt.title(title, fontsize=9)
    plt.axis("off")
    plt.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_path)
    plt.close()


__all__ = ["make_quicklook"]
