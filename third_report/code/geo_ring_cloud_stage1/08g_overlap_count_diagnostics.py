from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import netCDF4
import numpy as np
import pandas as pd


ROOT = Path(r"D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs")
SUMMARY = ROOT / "epic_202403_multisample_summary" / "epic_georing_multisample_summary.csv"
OUT = ROOT / "epic_202403_multisample_summary" / "geometry_prefusion_diagnostics" / "fused_metrics_by_overlap_count.csv"

EPIC_A = {1: 0, 2: 0, 3: 1, 4: 1}
GEO_A = {0: 0, 1: 0, 2: 1, 3: 1}


def find_var(ds: netCDF4.Dataset, names: list[str]) -> str:
    for name in names:
        group, _, var = name.rpartition("/")
        obj = ds[group] if group else ds
        if var in obj.variables:
            return name
    raise KeyError(names)


def read_epic(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    with netCDF4.Dataset(path) as ds:
        lat = np.asarray(ds[find_var(ds, ["geolocation_data/latitude", "geolocation_data/Latitude"])][:], dtype=np.float32)
        lon = np.asarray(ds[find_var(ds, ["geolocation_data/longitude", "geolocation_data/Longitude"])][:], dtype=np.float32)
        cm = np.asarray(ds[find_var(ds, ["geophysical_data/Cloud_Mask", "geophysical_data/cloud_mask"])][:], dtype=np.float32)
    return lat, lon, cm


def load_npz(path: Path) -> dict[str, np.ndarray]:
    with np.load(path, allow_pickle=True) as z:
        return {k: np.asarray(z[k]) for k in z.files if not k.endswith("json")}


def apply_map(arr: np.ndarray, mapping: dict[int, int]) -> tuple[np.ndarray, np.ndarray]:
    out = np.full(arr.shape, -1, dtype=np.int16)
    valid = np.zeros(arr.shape, dtype=bool)
    for raw, mapped in mapping.items():
        m = arr == raw
        out[m] = mapped
        valid |= m
    return out, valid


def sample(data: np.ndarray, valid: np.ndarray, lat: np.ndarray, lon: np.ndarray, grid: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    lon_norm = ((lon.astype(np.float32) + 180) % 360) - 180
    res = float(grid["resolution_degree"])
    row = np.rint((lat - (float(grid["lat_min"]) + res / 2)) / res).astype(np.int64)
    col = np.rint((lon_norm - (float(grid["lon_min"]) + res / 2)) / res).astype(np.int64)
    ok = np.isfinite(lat) & np.isfinite(lon) & (row >= 0) & (row < data.shape[0]) & (col >= 0) & (col < data.shape[1])
    out = np.full(lat.shape, np.nan, dtype=np.float32)
    out_valid = np.zeros(lat.shape, dtype=bool)
    out[ok] = data[row[ok], col[ok]].astype(np.float32)
    out_valid[ok] = valid[row[ok], col[ok]].astype(bool)
    out[~out_valid] = np.nan
    return out, out_valid


def metrics(epic: np.ndarray, geo: np.ndarray, valid: np.ndarray) -> dict[str, Any]:
    n = int(np.count_nonzero(valid))
    if n == 0:
        return {"status": "NO_OVERLAP", "n": 0}
    e = epic[valid].astype(np.int8)
    g = geo[valid].astype(np.int8)
    tp = int(np.count_nonzero((e == 1) & (g == 1)))
    tn = int(np.count_nonzero((e == 0) & (g == 0)))
    fp = int(np.count_nonzero((e == 0) & (g == 1)))
    fn = int(np.count_nonzero((e == 1) & (g == 0)))
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-12)
    return {
        "status": "OK",
        "n": n,
        "agreement": (tp + tn) / n,
        "f1": f1,
        "precision": precision,
        "recall": recall,
        "iou": tp / max(tp + fp + fn, 1),
        "tp": tp,
        "tn": tn,
        "fp": fp,
        "fn": fn,
    }


def main() -> int:
    rows = []
    df = pd.read_csv(SUMMARY).fillna("")
    for _, r in df.iterrows():
        tag = str(r["time_tag"])
        run = ROOT / tag
        epic_file = Path(str(r["epic_file"]))
        if not epic_file.exists():
            continue
        lat, lon, epic_cm = read_epic(epic_file)
        grid = json.loads((run / "reprojected_grid" / "target_grid_definition.json").read_text(encoding="utf-8"))
        fused = load_npz(run / "fused_best_source" / "fused_cloud_mask.npz")
        valid_count = load_npz(run / "fused_best_source" / "valid_count_map_cloud_mask.npz")
        geo_std, geo_valid = sample(fused["data"], fused["valid_mask"].astype(bool), lat, lon, grid)
        vcnt, vcnt_valid = sample(valid_count["data"], valid_count["valid_mask"].astype(bool), lat, lon, grid)
        epic_bin, epic_valid = apply_map(epic_cm, EPIC_A)
        geo_bin, geo_policy_valid = apply_map(geo_std, GEO_A)
        base = np.isin(epic_cm, [1, 2, 3, 4]) & epic_valid & geo_valid & geo_policy_valid & vcnt_valid
        strata = {
            "all": np.isfinite(vcnt),
            "valid_count_1": vcnt == 1,
            "valid_count_2": vcnt == 2,
            "valid_count_3plus": vcnt >= 3,
            "overlap_2plus": vcnt >= 2,
        }
        for name, mask in strata.items():
            rows.append(
                {
                    "time_tag": tag,
                    "candidate_class": r.get("candidate_class", ""),
                    "estimated_dominant_satellite": r.get("estimated_dominant_satellite", ""),
                    "stratum": name,
                    **metrics(epic_bin, geo_bin, base & mask),
                }
            )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({k for row in rows for k in row.keys()})
    with OUT.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"08g PASS: rows={len(rows)} out={OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
