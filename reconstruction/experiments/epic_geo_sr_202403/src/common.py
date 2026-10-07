"""Shared, deterministic utilities for the EPIC–GEO 2024-03 prototype."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import h5py
import numpy as np
import yaml
from nav_provenance import validate_pairing

CODE_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("EPIC_GEO_SR_ROOT", str(CODE_ROOT))).resolve()
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
FIGURES = ROOT / "figures"
METRICS = ROOT / "metrics"
PREDICTIONS = ROOT / "predictions"
CONFIG = CODE_ROOT / "config"
EARTH_RADIUS_KM = 6371.0088


def load_config(name: str) -> dict[str, Any]:
    with (CONFIG / name).open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def ensure_layout() -> None:
    for path in [DATA / "index", DATA / "prepared", DATA / "coarse50", DATA / "geo_features", DATA / "patches", REPORTS, FIGURES, METRICS, PREDICTIONS]:
        path.mkdir(parents=True, exist_ok=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def git_state() -> dict[str, str]:
    try:
        repo = subprocess.check_output(["git", "-C", str(CODE_ROOT), "rev-parse", "--show-toplevel"], text=True, stderr=subprocess.DEVNULL).strip()
        commit = subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
        status = subprocess.check_output(["git", "-C", repo, "status", "--porcelain"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return {"code_commit": "unavailable", "git_state": "unavailable"}
    return {"code_commit": commit, "git_state": "dirty" if status else "clean"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str] | None = None) -> None:
    values = list(rows)
    if fields is None:
        fields = sorted({key for row in values for key in row})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows([{key: row.get(key, "") for key in fields} for row in values])


def write_manifest(stage: str, script: Path, inputs: list[Path], outputs: list[Path], parameters: dict[str, Any]) -> Path:
    state = git_state()
    manifest = {
        "project_id": "epic_geo_sr_202403",
        "component_role": "experimental_prototype",
        "canonical_stage_id": "",
        "related_stage_ids": ["source_stage09d"],
        "prototype": "epic_geo_sr_202403",
        "stage": stage,
        "timestamp_utc": utc_now(),
        "generating_script": str(script.relative_to(CODE_ROOT)),
        "generating_script_sha256": sha256(script),
        "inputs": [str(p) for p in inputs],
        "outputs": [str(p) for p in outputs],
        "parameters": parameters,
        **state,
        "commit_represents_script": False,
        "lineage_warning": "The repository worktree was not assumed clean; script SHA-256 is the execution identity.",
    }
    path = ROOT / "logs" / f"{stage}_manifest.json"
    write_json(path, manifest)
    return path


def env_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable is unset: {name}")
    path = Path(value)
    if not path.exists():
        raise RuntimeError(f"Configured path does not exist: {name}={path}")
    return path


def configured_inputs() -> tuple[dict[str, Any], Path, Path, Path]:
    cfg = load_config("data.yaml")
    epic_root = env_path(cfg["epic_root_env"])
    geo_pairings_root = ROOT / cfg["geo_pairings_root"]
    manifest = ROOT / cfg["scene_manifest"]
    if not manifest.exists():
        raise RuntimeError(f"Local scene manifest is unavailable: {manifest}")
    if not geo_pairings_root.exists():
        raise RuntimeError(f"Local GEO pairings are unavailable: {geo_pairings_root}")
    return cfg, epic_root, geo_pairings_root, manifest


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def resolve_scene_rows() -> list[dict[str, Any]]:
    cfg, epic_root, geo_pairings_root, manifest_path = configured_inputs()
    with manifest_path.open(encoding="utf-8-sig", newline="") as f:
        raw_rows = list(csv.DictReader(f))
    rows: list[dict[str, Any]] = []
    scene_ids = [row["sample_id"] for row in raw_rows]
    if len(scene_ids) != len(set(scene_ids)):
        raise RuntimeError("Duplicate scene_id in frozen manifest")
    expected_count = int(cfg.get("expected_scene_count", len(raw_rows)))
    if len(raw_rows) != expected_count:
        raise RuntimeError(f"Expected {expected_count} manifest scenes, found {len(raw_rows)}")
    for row in raw_rows:
        filename = Path(row["epic_file"]).name
        epic_path = epic_root / filename
        run_dir = geo_pairings_root / row["sample_id"]
        required = run_dir / "fused_best_source" / "fused_cloud_mask.npz"
        if not epic_path.is_file():
            raise RuntimeError(f"Missing frozen EPIC scene: {epic_path}")
        if not required.is_file():
            raise RuntimeError(f"Missing fresh GEO pairing for {row['sample_id']}: {required}")
        provenance = validate_pairing(run_dir, row["sample_id"], cfg["required_meteosat_navigation"])
        dt = parse_time(row["epic_time_utc"])
        row = dict(row)
        row.update({
            "scene_date": dt.date().isoformat(),
            "epic_clm_file": str(epic_path),
            "epic_path_rebound": str(Path(row["epic_file"])) != str(epic_path),
            "stage_run_dir": str(run_dir),
            "dataset_version": cfg["dataset_version"],
            "navigation_provenance_json": json.dumps(provenance, ensure_ascii=False, sort_keys=True),
        })
        rows.append(row)
    return rows


def h5_array(path: Path, dataset: str, decode: bool = True) -> tuple[np.ndarray, dict[str, Any]]:
    with h5py.File(path, "r") as f:
        ds = f[dataset]
        values = np.asarray(ds[...])
        attrs = {key: _scalar(value) for key, value in ds.attrs.items()}
    if decode and values.dtype.kind in "iu":
        fill = attrs.get("_FillValue")
        values = values.astype(np.float32)
        if fill is not None:
            values[values == float(np.asarray(fill).flat[0])] = np.nan
        scale = float(np.asarray(attrs.get("scale_factor", 1.0)).flat[0])
        offset = float(np.asarray(attrs.get("add_offset", 0.0)).flat[0])
        values = values * scale + offset
    return values, attrs


def _scalar(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    return value


def read_epic(path: Path, cfg: dict[str, Any]) -> dict[str, np.ndarray]:
    raw_mask, _ = h5_array(path, cfg["epic_cloud_mask_path"], decode=False)
    raw_mask = raw_mask.astype(np.int16)
    clear = np.isin(raw_mask, cfg["cloud_translation"]["clear_codes"])
    cloudy = np.isin(raw_mask, cfg["cloud_translation"]["cloudy_codes"])
    valid = clear | cloudy
    lat, _ = h5_array(path, cfg["epic_latitude_path"])
    lon, _ = h5_array(path, cfg["epic_longitude_path"])
    vza, _ = h5_array(path, cfg["epic_vza_path"])
    sza, _ = h5_array(path, cfg["epic_sza_path"])
    surface, _ = h5_array(path, cfg["epic_surface_type_path"], decode=False)
    valid &= np.isfinite(lat) & np.isfinite(lon)
    cloud = np.where(valid, cloudy.astype(np.float32), np.nan)
    return {"cloud": cloud, "valid": valid, "lat": lat, "lon": lon, "epic_vza": vza, "epic_sza": sza, "surface_type": surface.astype(np.float32), "raw_mask": raw_mask}


def load_npz(path: Path) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    with np.load(path, allow_pickle=False) as data:
        values = np.asarray(data["data"])
        valid = np.asarray(data["valid_mask"]).astype(bool) if "valid_mask" in data.files else np.isfinite(values)
        meta = json.loads(str(data["metadata_json"].item())) if "metadata_json" in data.files else {}
    return values, valid, meta


def sample_geo_grid(values: np.ndarray, valid: np.ndarray, lat: np.ndarray, lon: np.ndarray, grid: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    res = float(grid["resolution_degree"])
    lat0 = float(grid.get("lat_centers_first_last", [grid["lat_min"] + res / 2])[0])
    lon0 = float(grid.get("lon_centers_first_last", [grid["lon_min"] + res / 2])[0])
    lon = ((lon + 180.0) % 360.0) - 180.0
    rr = np.rint((lat - lat0) / res).astype(np.int64)
    cc = np.rint((lon - lon0) / res).astype(np.int64)
    ok = np.isfinite(lat) & np.isfinite(lon) & (rr >= 0) & (rr < values.shape[0]) & (cc >= 0) & (cc < values.shape[1])
    out = np.full(lat.shape, np.nan, dtype=np.float32)
    out_ok = np.zeros(lat.shape, dtype=bool)
    out[ok] = values[rr[ok], cc[ok]].astype(np.float32)
    out_ok[ok] = valid[rr[ok], cc[ok]]
    out[~out_ok] = np.nan
    return out, out_ok


def haversine_km(lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray) -> np.ndarray:
    p1, p2 = np.deg2rad(lat1), np.deg2rad(lat2)
    dp = p2 - p1
    dl = np.deg2rad(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return (2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))).astype(np.float32)


def date_in_range(date: str, limits: list[str]) -> bool:
    return limits[0] <= date <= limits[1]


def split_for_date(date: str, cfg: dict[str, Any]) -> str:
    for label, limits in cfg["splits"].items():
        if date_in_range(date, limits):
            return label
    return "excluded"
