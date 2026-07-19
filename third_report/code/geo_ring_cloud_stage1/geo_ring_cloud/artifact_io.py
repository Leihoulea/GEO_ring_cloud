"""Small, deterministic serializers for generated Geo Ring Cloud artifacts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import numpy as np


COMPONENT_ROLE = "artifact_io"


def safe_name(value: str) -> str:
    """Convert a free-form identifier to a portable artifact-name segment."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_")


def write_json_npz(
    path: Path,
    arrays: dict[str, np.ndarray],
    metadata: dict[str, Any],
    availability: dict[str, bool],
) -> None:
    """Write arrays plus JSON metadata using the established Stage 1 NPZ schema."""
    payload = {name: np.asarray(value) for name, value in arrays.items()}
    payload["metadata_json"] = np.asarray(json.dumps(metadata, ensure_ascii=False, default=str))
    payload["variable_availability_json"] = np.asarray(
        json.dumps(availability, ensure_ascii=False, default=str)
    )
    np.savez_compressed(path, **payload)


__all__ = ["safe_name", "write_json_npz"]
