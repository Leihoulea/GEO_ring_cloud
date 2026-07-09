from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

import netCDF4
import numpy as np
import pandas as pd


RUNS_ROOT = Path(r"D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs")
SUMMARY_CSV = RUNS_ROOT / "epic_202403_multisample_summary" / "epic_georing_multisample_summary.csv"
OUT_DIR = RUNS_ROOT / "epic_202403_multisample_summary" / "geometry_prefusion_diagnostics"

POLICIES = {
    "A_inclusive_binary": {
        "epic": {1: 0, 2: 0, 3: 1, 4: 1},
        "geo": {0: 0, 1: 0, 2: 1, 3: 1},
    },
    "B_high_confidence_only": {
        "epic": {1: 0, 4: 1},
        "geo": {0: 0, 3: 1},
    },
}

SOURCE_PRODUCTS = {
    "FY4B": "CLM",
    "GOES-16": "ACMF",
    "GOES-18": "ACMF",
    "Himawari-9": "CMSK",
    "Meteosat-0deg": "CLM",
    "Meteosat-IODC": "CLM",
}


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def find_var(ds: netCDF4.Dataset, names: list[str]) -> str:
    for name in names:
        group, _, var = name.rpartition("/")
        obj = ds[group] if group else ds
        if var in obj.variables:
            return name
    raise KeyError(names)


def read_epic(path: Path) -> dict[str, np.ndarray]:
    with netCDF4.Dataset(path) as ds:
        lat_name = find_var(ds, ["geolocation_data/latitude", "geolocation_data/Latitude"])
        lon_name = find_var(ds, ["geolocation_data/longitude", "geolocation_data/Longitude"])
        cm_name = find_var(ds, ["geophysical_data/Cloud_Mask", "geophysical_data/cloud_mask"])
        try:
            senz_name = find_var(ds, ["geolocation_data/sensor_zenith", "geolocation_data/SensorZenith", "geolocation_data/ViewAngle"])
            sensor_zenith = np.asarray(ds[senz_name][:], dtype=np.float32)
        except Exception:
            sensor_zenith = np.full(np.asarray(ds[lat_name][:]).shape, np.nan, dtype=np.float32)
        return {
            "lat": np.asarray(ds[lat_name][:], dtype=np.float32),
            "lon": np.asarray(ds[lon_name][:], dtype=np.float32),
            "cloud_mask": np.asarray(ds[cm_name][:], dtype=np.float32),
            "sensor_zenith": sensor_zenith,
        }


def load_npz(path: Path) -> dict[str, np.ndarray]:
    with np.load(path, allow_pickle=True) as z:
        return {k: np.asarray(z[k]) for k in z.files if not k.endswith("json")}


def load_grid(run_root: Path) -> dict[str, Any]:
    return json.loads((run_root / "reprojected_grid" / "target_grid_definition.json").read_text(encoding="utf-8"))


def sample_grid_to_points(data: np.ndarray, valid: np.ndarray, lat: np.ndarray, lon: np.ndarray, grid: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    lon_norm = ((lon.astype(np.float32) + 180.0) % 360.0) - 180.0
    res = float(grid["resolution_degree"])
    row = np.rint((lat - (float(grid["lat_min"]) + res / 2.0)) / res).astype(np.int64)
    col = np.rint((lon_norm - (float(grid["lon_min"]) + res / 2.0)) / res).astype(np.int64)
    ok = np.isfinite(lat) & np.isfinite(lon) & (row >= 0) & (row < data.shape[0]) & (col >= 0) & (col < data.shape[1])
    out = np.full(lat.shape, np.nan, dtype=np.float32)
    out_valid = np.zeros(lat.shape, dtype=bool)
    out[ok] = data[row[ok], col[ok]].astype(np.float32)
    out_valid[ok] = valid[row[ok], col[ok]].astype(bool)
    out[~out_valid] = np.nan
    return out, out_valid


def apply_policy(arr: np.ndarray, mapping: dict[int, int]) -> tuple[np.ndarray, np.ndarray]:
    out = np.full(arr.shape, -1, dtype=np.int16)
    valid = np.zeros(arr.shape, dtype=bool)
    for raw_code, mapped_code in mapping.items():
        m = arr == raw_code
        out[m] = mapped_code
        valid |= m
    return out, valid


def cloud_mask_to_standard(satellite: str, raw: np.ndarray) -> np.ndarray:
    arr = np.asarray(raw)
    out = np.full(arr.shape, -9999, dtype=np.int16)
    if satellite == "FY4B":
        mapping = {0: 3, 1: 2, 2: 1, 3: 0}
    elif satellite.startswith("GOES"):
        mapping = {0: 0, 1: 3}
    elif satellite.startswith("Himawari"):
        mapping = {0: 0, 1: 1, 2: 2, 3: 3}
    elif satellite.startswith("Meteosat"):
        mapping = {0: 0, 1: 0, 2: 3}
    else:
        mapping = {}
    for raw_code, std_code in mapping.items():
        out[arr == raw_code] = std_code
    return out


def binary_metrics(epic: np.ndarray, geo: np.ndarray, valid: np.ndarray) -> dict[str, Any]:
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
        "agreement": (tp + tn) / max(n, 1),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "iou": tp / max(tp + fp + fn, 1),
        "tp": tp,
        "tn": tn,
        "fp": fp,
        "fn": fn,
        "epic_cloud_fraction": float(np.mean(e == 1)),
        "geo_cloud_fraction": float(np.mean(g == 1)),
    }


def bin_masks(epic: dict[str, np.ndarray]) -> list[tuple[str, np.ndarray]]:
    lat = epic["lat"]
    senz = epic["sensor_zenith"]
    abs_lat = np.abs(lat)
    bins: list[tuple[str, np.ndarray]] = [
        ("all", np.isfinite(lat)),
        ("lat_abs_0_30", (abs_lat >= 0) & (abs_lat < 30)),
        ("lat_abs_30_50", (abs_lat >= 30) & (abs_lat < 50)),
        ("lat_abs_50_60", (abs_lat >= 50) & (abs_lat < 60)),
        ("lat_abs_60_70", (abs_lat >= 60) & (abs_lat < 70)),
        ("lat_abs_70_90", (abs_lat >= 70) & (abs_lat <= 90)),
        ("geo_traditional_midlow_lat_abs_lt60", abs_lat < 60),
        ("high_lat_abs_ge60", abs_lat >= 60),
        ("epic_sensor_zenith_0_30", (senz >= 0) & (senz < 30)),
        ("epic_sensor_zenith_30_50", (senz >= 30) & (senz < 50)),
        ("epic_sensor_zenith_50_65", (senz >= 50) & (senz < 65)),
        ("epic_sensor_zenith_65_75", (senz >= 65) & (senz < 75)),
        ("epic_sensor_zenith_75_90", (senz >= 75) & (senz <= 90)),
    ]
    return bins


def find_reprojected_cloud_mask(run_root: Path, satellite: str, tag: str) -> Path | None:
    product = SOURCE_PRODUCTS[satellite]
    matches = list((run_root / "reprojected_grid" / satellite).glob(f"{satellite}_{product}_cloud_mask_grid_{tag}.npz"))
    return matches[0] if matches else None


def run() -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary = pd.read_csv(SUMMARY_CSV).fillna("")
    strat_rows: list[dict[str, Any]] = []
    prefusion_rows: list[dict[str, Any]] = []

    for _, row in summary.iterrows():
        tag = str(row["time_tag"])
        epic_file = Path(str(row["epic_file"]))
        run_root = RUNS_ROOT / tag
        if not epic_file.exists() or not run_root.exists():
            continue
        epic = read_epic(epic_file)
        grid = load_grid(run_root)

        fused = load_npz(run_root / "fused_best_source" / "fused_cloud_mask.npz")
        fused_std, fused_valid = sample_grid_to_points(fused["data"], fused["valid_mask"].astype(bool), epic["lat"], epic["lon"], grid)
        epic_valid_earth = np.isin(epic["cloud_mask"], [1, 2, 3, 4])

        for policy_name, policy in POLICIES.items():
            epic_cls, epic_policy_valid = apply_policy(epic["cloud_mask"], policy["epic"])
            fused_cls, fused_policy_valid = apply_policy(fused_std, policy["geo"])
            base_valid = epic_valid_earth & epic_policy_valid & fused_valid & fused_policy_valid
            for bin_name, bin_mask in bin_masks(epic):
                valid = base_valid & bin_mask
                metrics = binary_metrics(epic_cls, fused_cls, valid)
                strat_rows.append(
                    {
                        "time_tag": tag,
                        "candidate_class": row.get("candidate_class", ""),
                        "estimated_dominant_satellite": row.get("estimated_dominant_satellite", ""),
                        "policy": policy_name,
                        "stratum": bin_name,
                        **metrics,
                    }
                )

        for satellite in SOURCE_PRODUCTS:
            path = find_reprojected_cloud_mask(run_root, satellite, tag)
            if path is None:
                continue
            arrays = load_npz(path)
            raw_on_epic, raw_valid = sample_grid_to_points(arrays["data"], arrays.get("fusion_valid_mask", arrays["valid_mask"]).astype(bool), epic["lat"], epic["lon"], grid)
            std_on_epic = cloud_mask_to_standard(satellite, raw_on_epic)
            std_valid = raw_valid & (std_on_epic >= 0)
            for policy_name, policy in POLICIES.items():
                epic_cls, epic_policy_valid = apply_policy(epic["cloud_mask"], policy["epic"])
                geo_cls, geo_policy_valid = apply_policy(std_on_epic, policy["geo"])
                base_valid = epic_valid_earth & epic_policy_valid & std_valid & geo_policy_valid
                metrics = binary_metrics(epic_cls, geo_cls, base_valid)
                prefusion_rows.append(
                    {
                        "time_tag": tag,
                        "candidate_class": row.get("candidate_class", ""),
                        "estimated_dominant_satellite": row.get("estimated_dominant_satellite", ""),
                        "policy": policy_name,
                        "satellite": satellite,
                        "product": SOURCE_PRODUCTS[satellite],
                        "source_file": str(path),
                        **metrics,
                    }
                )

    strat_fields = sorted({k for r in strat_rows for k in r.keys()})
    pref_fields = sorted({k for r in prefusion_rows for k in r.keys()})
    write_csv(OUT_DIR / "fused_metrics_by_latitude_and_epic_view_angle.csv", strat_rows, strat_fields)
    write_csv(OUT_DIR / "prefusion_source_cloud_mask_vs_epic_metrics.csv", prefusion_rows, pref_fields)

    report = OUT_DIR / "geometry_prefusion_diagnostics_report.md"
    lines = [
        "# 08f Geometry and Pre-fusion EPIC Diagnostics",
        "",
        "## What Was Tested",
        "",
        "- Fused GEO-ring cloud_mask was sampled to EPIC L2 pixel latitude/longitude and stratified by absolute latitude and EPIC sensor zenith angle.",
        "- Each pre-fusion GEO cloud_mask source was also sampled independently to EPIC pixels, using the same semantic policies as 08c.",
        "- This tests whether low agreement is concentrated at high latitude, EPIC disk edge, or a specific pre-fusion satellite source.",
        "",
        "## Important Geometry Caveat",
        "",
        "- This is not a full radiance-level EPIC-view reprojection. It is nearest-neighbor sampling from the 0.05 degree GEO-ring grid to EPIC L2 geolocation points.",
        "- Therefore boundary pixels, high-latitude pixels, and high-view-angle pixels may include representativeness and sampling errors.",
        "",
        "## Outputs",
        "",
        f"- `{OUT_DIR / 'fused_metrics_by_latitude_and_epic_view_angle.csv'}`",
        f"- `{OUT_DIR / 'prefusion_source_cloud_mask_vs_epic_metrics.csv'}`",
    ]
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"08f PASS: stratified_rows={len(strat_rows)} prefusion_rows={len(prefusion_rows)} report={report}")
    return report


if __name__ == "__main__":
    run()
