"""Small deterministic summaries for scientific arrays."""

from __future__ import annotations

from typing import Any

import numpy as np


COMPONENT_ROLE = "diagnostics_library"
__all__ = ["finite_stats"]


def finite_stats(arr: np.ndarray) -> dict[str, Any]:
    """Return shape, dtype, size, range, mean, and invalid-value ratio."""
    values = np.asarray(arr)
    row: dict[str, Any] = {
        "shape": "x".join(map(str, values.shape)),
        "dtype": str(values.dtype),
        "size": int(values.size),
    }
    if values.size == 0:
        row.update({"min": np.nan, "max": np.nan, "mean": np.nan, "nan_ratio": np.nan})
        return row
    if values.dtype.kind in "f":
        finite = np.isfinite(values)
        row["nan_ratio"] = float((~finite).sum() / values.size)
        if finite.any():
            row["min"] = float(np.nanmin(values))
            row["max"] = float(np.nanmax(values))
            row["mean"] = float(np.nanmean(values))
        else:
            row["min"] = row["max"] = row["mean"] = np.nan
    elif values.dtype.kind in "iu":
        row["nan_ratio"] = 0.0
        row["min"] = int(np.min(values))
        row["max"] = int(np.max(values))
        row["mean"] = float(np.mean(values))
    else:
        row["nan_ratio"] = np.nan
        row["min"] = row["max"] = row["mean"] = np.nan
    return row
