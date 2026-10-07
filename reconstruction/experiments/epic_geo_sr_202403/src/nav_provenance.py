"""Fail closed when a GEO pairing predates the Meteosat CLM navigation fixes."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np


def _metadata(path: Path) -> dict:
    if not path.is_file():
        raise RuntimeError(f"Missing required upstream artifact: {path}")
    with np.load(path, allow_pickle=False) as artifact:
        if "metadata_json" not in artifact.files:
            raise RuntimeError(f"Missing metadata_json: {path}")
        return json.loads(str(artifact["metadata_json"].item()))


def validate_pairing(run_dir: Path, scene_id: str, required_versions: dict[str, str]) -> dict:
    """Check native navigation provenance before any GEO array enters a model.

    The fused metadata lists participants but does not carry per-source navigation
    versions.  Therefore the exact native artifacts in the same fresh run are
    mandatory, and a fused artifact must postdate those native artifacts.
    """
    versions = {}
    latest_native = None
    for source, expected in required_versions.items():
        native = run_dir / "standardized_native" / f"{source}_CLM_{scene_id}_native_cloud_v0.npz"
        meta = _metadata(native)
        attrs = meta.get("reader_attrs", {})
        actual = attrs.get("navigation_schema_version")
        if actual != expected:
            raise RuntimeError(
                f"{scene_id} {source} navigation version {actual!r}, expected {expected!r}: {native}"
            )
        if str(meta.get("satellite_group")) != source or str(meta.get("product")) != "CLM":
            raise RuntimeError(f"Native source/product identity mismatch: {native}")
        generated = datetime.fromisoformat(str(meta["generated_utc"]).replace("Z", "+00:00"))
        latest_native = generated if latest_native is None else max(latest_native, generated)
        versions[source] = {"schema": actual, "native_file": str(native), "source_file": meta.get("source_file", "")}

    fused_path = run_dir / "fused_best_source" / "fused_cloud_mask.npz"
    fused = _metadata(fused_path)
    participants = set(fused.get("participants", []))
    for source in required_versions:
        if f"{source}:CLM" not in participants:
            raise RuntimeError(f"{scene_id} missing {source}:CLM in fused participants")
    fused_time = datetime.fromisoformat(str(fused["generated_utc"]).replace("Z", "+00:00"))
    if fused_time < latest_native:
        raise RuntimeError(f"{scene_id} fused cloud mask predates repaired native GEO input")
    for name in ("valid_count_map_cloud_mask.npz", "source_map_cloud_mask.npz"):
        if not (run_dir / "fused_best_source" / name).is_file():
            raise RuntimeError(f"{scene_id} missing fused artifact {name}")
    return {"scene_id": scene_id, "fused_file": str(fused_path), "fused_utc": fused["generated_utc"], "sources": versions}
