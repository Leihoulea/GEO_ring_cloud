from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

for _env_name in ["OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"]:
    os.environ.setdefault(_env_name, "1")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import netCDF4
import numpy as np
import pandas as pd

from ..adapters.epic import read_epic_cth
from ..paths import RUNS_ROOT


COMPONENT_ROLE = "diagnostics_workflow"
SOURCE_ID = {1: "GOES-16", 2: "GOES-18", 3: "FY4B", 4: "Himawari-9", 5: "Meteosat-0deg", 6: "Meteosat-IODC", 7: "CLAAS3-0deg"}
SOURCE_TO_ID = {v: k for k, v in SOURCE_ID.items()}
SOURCES = ["FY4B", "GOES-16", "GOES-18", "Himawari-9", "Meteosat-0deg", "Meteosat-IODC", "CLAAS3-0deg"]
SOURCE_PAIR_LIST = [
    ("FY4B", "Himawari-9"),
    ("FY4B", "Meteosat-IODC"),
    ("Himawari-9", "Meteosat-IODC"),
    ("Meteosat-0deg", "GOES-16"),
    ("Meteosat-0deg", "Meteosat-IODC"),
    ("GOES-16", "GOES-18"),
]
POLICIES = {
    "A_inclusive_binary": {"epic": {1: 0, 2: 0, 3: 1, 4: 1}, "geo": {0: 0, 1: 0, 2: 1, 3: 1}},
    "B_high_confidence_only": {"epic": {1: 0, 4: 1}, "geo": {0: 0, 3: 1}},
}
EPIC_CTH_CANDIDATES = [
    "geophysical_data/A-band_Effective_Cloud_Height",
    "geophysical_data/B-band_Effective_Cloud_Height",
    "geophysical_data/Cloud_Top_Height",
    "geophysical_data/CloudTopHeight",
    "geophysical_data/Cloud_Effective_Height",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def ensure_dirs(out: Path) -> None:
    for name in [
        "00_inventory",
        "02_fused_cth_metrics",
        "03_fused_cth_by_selected_source",
        "04_prefusion_source_cth",
        "05_source_pair_cth",
        "06_selection_sensitivity_cth",
        "07_geometry_boundary_height",
        "08_case_atlas",
        "09_figures",
        "reports",
    ]:
        (out / name).mkdir(parents=True, exist_ok=True)


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        keys: set[str] = set()
        for row in rows:
            keys.update(row.keys())
        fields = sorted(keys)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})


def read_manifest(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")
    df = df[df["has_epic_file"].astype(str).str.lower().eq("true")].copy()
    df = df[df["stage_run_dir"].map(lambda x: Path(str(x)).exists())].copy()
    return df


def load_npz_array(path: Path) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    with np.load(path, allow_pickle=True) as z:
        data = np.asarray(z["data"])
        valid = np.asarray(z["valid_mask"]).astype(bool) if "valid_mask" in z.files else np.isfinite(data)
        metadata: dict[str, Any] = {}
        if "metadata_json" in z.files:
            try:
                metadata = json.loads(str(np.asarray(z["metadata_json"]).item()))
            except Exception:
                metadata = {"metadata_json_parse_error": True}
    return data, valid, metadata


def load_grid(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "reprojected_grid" / "target_grid_definition.json").read_text(encoding="utf-8"))


def row_col(lat: np.ndarray, lon: np.ndarray, grid: dict[str, Any], shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    res = float(grid["resolution_degree"])
    lat0 = float(grid.get("lat_min", -90.0)) + res / 2.0
    lon0 = float(grid.get("lon_min", -180.0)) + res / 2.0
    if "lat_centers_first_last" in grid:
        lat0 = float(grid["lat_centers_first_last"][0])
    if "lon_centers_first_last" in grid:
        lon0 = float(grid["lon_centers_first_last"][0])
    lon_norm = ((lon.astype(np.float32) + 180.0) % 360.0) - 180.0
    r = np.rint((lat.astype(np.float32) - lat0) / res).astype(np.int64)
    c = np.rint((lon_norm - lon0) / res).astype(np.int64)
    ok = np.isfinite(lat) & np.isfinite(lon_norm) & (r >= 0) & (r < shape[0]) & (c >= 0) & (c < shape[1])
    return r, c, ok


def sample_grid(data: np.ndarray, valid: np.ndarray, lat: np.ndarray, lon: np.ndarray, grid: dict[str, Any], fill: float = np.nan) -> tuple[np.ndarray, np.ndarray]:
    r, c, ok = row_col(lat, lon, grid, data.shape)
    out = np.full(lat.shape, fill, dtype=np.float32)
    out_valid = np.zeros(lat.shape, dtype=bool)
    out[ok] = data[r[ok], c[ok]].astype(np.float32)
    out_valid[ok] = valid[r[ok], c[ok]].astype(bool)
    out[~out_valid] = fill
    return out, out_valid


def nc_var_attrs(var: netCDF4.Variable) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in var.ncattrs():
        value = getattr(var, name)
        if isinstance(value, np.generic):
            value = value.item()
        out[name] = value
    return out


def find_existing_var(ds: netCDF4.Dataset, names: list[str]) -> str | None:
    for name in names:
        group_name, _, var_name = name.rpartition("/")
        group = ds[group_name] if group_name else ds
        if var_name in group.variables:
            return name
    return None


def read_nc_array(ds: netCDF4.Dataset, name: str) -> np.ndarray:
    arr = np.asarray(ds[name][:])
    if np.ma.isMaskedArray(arr):
        arr = arr.filled(np.nan)
    return arr


def read_epic(path: Path, cth_var_name: str | None = None) -> dict[str, Any]:
    return read_epic_cth(path, cth_var_name)


def epic_inventory(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with netCDF4.Dataset(path) as ds:
        for name in EPIC_CTH_CANDIDATES:
            group_name, _, var_name = name.rpartition("/")
            group = ds[group_name] if group_name else ds
            if var_name not in group.variables:
                continue
            var = ds[name]
            attrs = nc_var_attrs(var)
            data = read_nc_array(ds, name).astype(np.float32)
            fill = attrs.get("_FillValue", np.nan)
            raw_valid = np.isfinite(data) & (data != float(fill))
            units = str(attrs.get("units", ""))
            physical = data / 1000.0 if units.lower() == "m" else data
            physical_valid = raw_valid & (physical >= 0) & (physical <= 25)
            rows.append(
                {
                    "product_family": "EPIC_L2_CLOUD",
                    "source_name": "EPIC",
                    "sample_id": "",
                    "file_path": str(path),
                    "cth_variable_name": name,
                    "variable_long_name": attrs.get("long_name", ""),
                    "units_original": units,
                    "units_standardized": "km" if units.lower() in {"m", "km"} else units,
                    "scale_factor": attrs.get("scale_factor", ""),
                    "add_offset": attrs.get("add_offset", ""),
                    "fill_value": attrs.get("_FillValue", ""),
                    "valid_min": attrs.get("valid_min", ""),
                    "valid_max": attrs.get("valid_max", ""),
                    "n_raw_valid": int(np.count_nonzero(raw_valid)),
                    "n_after_physical_filter": int(np.count_nonzero(physical_valid)),
                    "unit_conversion_applied": "m_to_km" if units.lower() == "m" else "none",
                    "unit_inferred": False,
                    "notes": "CTH-like effective cloud height diagnostic reference; not strict cloud top height.",
                }
            )
    return rows


def apply_policy(arr: np.ndarray, mapping: dict[int, int]) -> tuple[np.ndarray, np.ndarray]:
    out = np.full(arr.shape, -1, dtype=np.int16)
    valid = np.zeros(arr.shape, dtype=bool)
    for raw, mapped in mapping.items():
        m = arr == raw
        out[m] = mapped
        valid |= m
    return out, valid


def cth_class(values: np.ndarray) -> np.ndarray:
    out = np.full(values.shape, "missing", dtype=object)
    out[(values >= 0) & (values < 3)] = "low_cloud"
    out[(values >= 3) & (values < 7)] = "mid_cloud"
    out[values >= 7] = "high_cloud"
    return out


def bin_numeric(values: np.ndarray, edges: list[tuple[str, float, float]]) -> np.ndarray:
    out = np.full(values.shape, "missing", dtype=object)
    for label, lo, hi in edges:
        out[(values >= lo) & (values < hi)] = label
    return out


def local_fraction(mask: np.ndarray, radius: int = 2) -> np.ndarray:
    padded = np.pad(mask.astype(np.float32), radius, mode="edge")
    out = np.zeros(mask.shape, dtype=np.float32)
    for dy in range(2 * radius + 1):
        for dx in range(2 * radius + 1):
            out += padded[dy : dy + mask.shape[0], dx : dx + mask.shape[1]]
    return out / float((2 * radius + 1) ** 2)


def corr(x: np.ndarray, y: np.ndarray, method: str) -> float:
    if x.size < 3:
        return math.nan
    if method == "spearman":
        x = pd.Series(x).rank().to_numpy()
        y = pd.Series(y).rank().to_numpy()
    if np.nanstd(x) == 0 or np.nanstd(y) == 0:
        return math.nan
    return float(np.corrcoef(x, y)[0, 1])


def metrics(epic: np.ndarray, fused: np.ndarray, mask: np.ndarray) -> dict[str, Any]:
    n = int(np.count_nonzero(mask))
    if n == 0:
        return {"n_valid_cth": 0}
    e = epic[mask].astype(np.float64)
    f = fused[mask].astype(np.float64)
    d = f - e
    absd = np.abs(d)
    ecls = cth_class(e)
    fcls = cth_class(f)
    return {
        "n_valid_cth": n,
        "epic_cth_mean_km": float(np.mean(e)),
        "fused_cth_mean_km": float(np.mean(f)),
        "bias_km": float(np.mean(d)),
        "median_bias_km": float(np.median(d)),
        "mae_km": float(np.mean(absd)),
        "rmse_km": float(np.sqrt(np.mean(d * d))),
        "median_abs_error_km": float(np.median(absd)),
        "p75_abs_error_km": float(np.percentile(absd, 75)),
        "p90_abs_error_km": float(np.percentile(absd, 90)),
        "within_1km_fraction": float(np.mean(absd <= 1)),
        "within_2km_fraction": float(np.mean(absd <= 2)),
        "within_3km_fraction": float(np.mean(absd <= 3)),
        "pearson_corr": corr(e, f, "pearson"),
        "spearman_corr": corr(e, f, "spearman"),
        "low_mid_high_class_agreement": float(np.mean(ecls == fcls)),
    }


def aggregate_metrics(rows: list[dict[str, Any]], group_cols: list[str]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    metric_cols = [
        "bias_km",
        "mae_km",
        "rmse_km",
        "median_abs_error_km",
        "p90_abs_error_km",
        "within_1km_fraction",
        "within_2km_fraction",
        "within_3km_fraction",
        "pearson_corr",
        "spearman_corr",
        "low_mid_high_class_agreement",
    ]
    for col in ["n_valid_cth", *metric_cols]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    out_rows = []
    for keys, g in df.groupby(group_cols, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        weights = g["n_valid_cth"].fillna(0).to_numpy()
        row = {col: key for col, key in zip(group_cols, keys)}
        row["sample_count"] = int(len(g))
        row["n_valid_cth"] = int(np.nansum(weights))
        for col in metric_cols:
            vals = g[col].to_numpy(dtype=float)
            ok = np.isfinite(vals) & (weights > 0)
            row[col] = float(np.average(vals[ok], weights=weights[ok])) if np.any(ok) else math.nan
        out_rows.append(row)
    return pd.DataFrame(out_rows)


def source_cth_file(run_dir: Path, source: str) -> Path | None:
    files = sorted((run_dir / "reprojected_grid" / source).glob("*cloud_top_height_km_grid_*.npz"))
    return files[0] if files else None


def cloud_mask_file(run_dir: Path, source: str) -> Path | None:
    files = sorted((run_dir / "reprojected_grid" / source).glob("*cloud_mask_grid_*.npz"))
    return files[0] if files else None


def valid_count_bin(v: np.ndarray) -> np.ndarray:
    out = np.full(v.shape, "missing", dtype=object)
    out[v == 1] = "valid_source_count_1"
    out[v == 2] = "valid_source_count_2"
    out[v == 3] = "valid_source_count_3"
    out[v >= 4] = "valid_source_count_ge4"
    return out


def add_group_metrics(
    rows: list[dict[str, Any]],
    sample_meta: dict[str, Any],
    epic_cth: np.ndarray,
    fused_cth: np.ndarray,
    base_mask: np.ndarray,
    policy_name: str,
    domain: str,
    group_name: str,
    group_values: np.ndarray,
) -> None:
    vals = sorted([v for v in pd.unique(group_values[base_mask].ravel()) if str(v) != "missing"])
    for value in vals:
        m = base_mask & (group_values == value)
        if np.count_nonzero(m) == 0:
            continue
        row = dict(sample_meta)
        row.update({"policy": policy_name, "domain": domain, "group_dimension": group_name, "group_value": value})
        row.update(metrics(epic_cth, fused_cth, m))
        rows.append(row)


def source_name_array(source_map: np.ndarray) -> np.ndarray:
    out = np.full(source_map.shape, "missing", dtype=object)
    for sid, name in SOURCE_ID.items():
        out[source_map == sid] = name
    return out


def make_bar(path: Path, df: pd.DataFrame, label_col: str, value_col: str, title: str, source_csv: Path, plot_rows: list[dict[str, Any]]) -> None:
    if df.empty or label_col not in df or value_col not in df:
        return
    plot_df = df.dropna(subset=[value_col]).head(30)
    if plot_df.empty:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    plt.figure(figsize=(max(7, len(plot_df) * 0.55), 4.2))
    plt.bar(plot_df[label_col].astype(str), plot_df[value_col].astype(float), color="#4c78a8")
    plt.xticks(rotation=35, ha="right")
    plt.ylabel(value_col)
    plt.title(title)
    plt.tight_layout()
    plt.savefig(path, dpi=160)
    plt.close()
    plot_rows.append({"plot_path": str(path), "source_csv": str(source_csv), "description": title})


def process_sample(row: pd.Series, out: Path, epic_cth_var: str | None) -> dict[str, Any]:
    sample_id = str(row["sample_id"])
    run_dir = Path(str(row["stage_run_dir"]))
    epic_path = Path(str(row["epic_file"]))
    grid = load_grid(run_dir)
    epic = read_epic(epic_path, epic_cth_var)
    fused_dir = run_dir / "fused_best_source"
    fused_cth, fused_cth_valid, fused_cth_meta = load_npz_array(fused_dir / "fused_cloud_top_height_km.npz")
    fused_cm, fused_cm_valid, _ = load_npz_array(fused_dir / "fused_cloud_mask.npz")
    source_map, source_valid, _ = load_npz_array(fused_dir / "source_map_cloud_top_height_km.npz")
    valid_count, valid_count_valid, _ = load_npz_array(fused_dir / "valid_count_map_cloud_top_height_km.npz")
    fused_on, fused_on_valid = sample_grid(fused_cth, fused_cth_valid & (fused_cth >= 0) & (fused_cth <= 25), epic["lat"], epic["lon"], grid)
    fused_cm_on, fused_cm_on_valid = sample_grid(fused_cm, fused_cm_valid, epic["lat"], epic["lon"], grid)
    src_on, src_on_valid = sample_grid(source_map, source_valid, epic["lat"], epic["lon"], grid)
    vc_on, vc_on_valid = sample_grid(valid_count, valid_count_valid, epic["lat"], epic["lon"], grid)
    sample_meta = {
        "sample_id": sample_id,
        "epic_time_utc": row.get("epic_time_utc", ""),
        "nearest_georing_time_utc": row.get("nearest_georing_time_utc", ""),
        "time_diff_min": row.get("time_diff_min", ""),
        "candidate_group": row.get("candidate_group", ""),
        "dominant_source": row.get("dominant_source", ""),
    }

    epic_policy: dict[str, np.ndarray] = {}
    fused_policy: dict[str, np.ndarray] = {}
    policy_valid: dict[str, np.ndarray] = {}
    domain_rows: list[dict[str, Any]] = []
    group_rows: list[dict[str, Any]] = []
    selected_rows: list[dict[str, Any]] = []
    vc_rows: list[dict[str, Any]] = []
    prefusion_rows: list[dict[str, Any]] = []
    source_pair_rows: list[dict[str, Any]] = []
    regret_rows: list[dict[str, Any]] = []
    height_rows: list[dict[str, Any]] = []
    confusion_rows: list[dict[str, Any]] = []
    case_rows: list[dict[str, Any]] = []

    common_valid = epic["cth_valid"] & fused_on_valid & np.isfinite(fused_on) & (fused_on >= 0) & (fused_on <= 25)
    for policy_name, policy in POLICIES.items():
        ecls, ev = apply_policy(epic["cloud_mask"], policy["epic"])
        gcls, gv = apply_policy(fused_cm_on, policy["geo"])
        epic_policy[policy_name] = ecls
        fused_policy[policy_name] = gcls
        policy_valid[policy_name] = ev & gv & fused_cm_on_valid
        d0 = common_valid & ev & gv & fused_cm_on_valid
        d1 = d0 & (ecls == 1) & (gcls == 1)
        d3 = common_valid & ev & gv & (ecls == 1) & (gcls == 0)
        d4 = common_valid & ev & gv & (ecls == 0) & (gcls == 1)
        local_cloud = local_fraction(d1, 2)
        boundary = d1 & (local_cloud > 0.05) & (local_cloud < 0.95)
        clean_core = d1 & (local_cloud >= 0.95) & (np.abs(epic["lat"]) < 60) & (epic["epic_vza"] < 60) & (epic["sza"] < 70)
        high_cloud = d1 & ((epic["cth_km"] >= 7) | (fused_on >= 7))
        domains = {
            "D0_common_valid_cth": d0,
            "D1_both_cloud": d1,
            "D3_EPIC_cloud_GEO_not_cloud": d3,
            "D4_GEO_cloud_EPIC_not_cloud": d4,
            "D5_clean_core_cloud": clean_core,
            "D6_boundary_or_broken_cloud": boundary,
            "D7_high_cloud": high_cloud,
        }
        for domain, mask in domains.items():
            r = dict(sample_meta)
            r.update({"policy": policy_name, "domain": domain})
            r.update(metrics(epic["cth_km"], fused_on, mask))
            if domain in {"D3_EPIC_cloud_GEO_not_cloud", "D4_GEO_cloud_EPIC_not_cloud"}:
                r["notes"] = "cloud mask disagreement domain; continuous CTH error is diagnostic only."
            domain_rows.append(r)

        src_names = source_name_array(src_on.astype(np.int16))
        vcb = valid_count_bin(vc_on)
        abs_lat_bin = bin_numeric(np.abs(epic["lat"]), [("abs_lat_0_30", 0, 30), ("abs_lat_30_60", 30, 60), ("abs_lat_ge60", 60, 91)])
        vza_bin = bin_numeric(epic["epic_vza"], [("EPIC_VZA_0_30", 0, 30), ("EPIC_VZA_30_60", 30, 60), ("EPIC_VZA_ge60", 60, 181)])
        sza_bin = bin_numeric(epic["sza"], [("SZA_0_50", 0, 50), ("SZA_50_70", 50, 70), ("SZA_ge70", 70, 181)])
        epic_height_class = cth_class(epic["cth_km"])
        fused_height_class = cth_class(fused_on)
        boundary_class = np.where(boundary, "boundary_or_broken_cloud", "non_boundary")
        for group_name, group_values in [
            ("selected_source", src_names),
            ("valid_source_count_bin", vcb),
            ("abs_lat_bin", abs_lat_bin),
            ("EPIC_VZA_bin", vza_bin),
            ("SZA_bin", sza_bin),
            ("epic_cth_class", epic_height_class),
            ("fused_cth_class", fused_height_class),
            ("boundary_class", boundary_class),
        ]:
            add_group_metrics(group_rows, sample_meta, epic["cth_km"], fused_on, d1, policy_name, "D1_both_cloud", group_name, group_values)
        for selected in sorted([v for v in pd.unique(src_names[d1].ravel()) if str(v) != "missing"]):
            m = d1 & (src_names == selected)
            r = dict(sample_meta)
            r.update({"policy": policy_name, "domain": "D1_both_cloud", "selected_source": selected})
            r.update(metrics(epic["cth_km"], fused_on, m))
            selected_rows.append(r)
        for vb in sorted([v for v in pd.unique(vcb[d1].ravel()) if str(v) != "missing"]):
            m = d1 & (vcb == vb)
            r = dict(sample_meta)
            r.update({"policy": policy_name, "domain": "D1_both_cloud", "valid_source_count_bin": vb})
            r.update(metrics(epic["cth_km"], fused_on, m))
            vc_rows.append(r)
        for eclass in ["low_cloud", "mid_cloud", "high_cloud"]:
            for fclass in ["low_cloud", "mid_cloud", "high_cloud"]:
                count = int(np.count_nonzero(d1 & (epic_height_class == eclass) & (fused_height_class == fclass)))
                confusion_rows.append({**sample_meta, "policy": policy_name, "epic_cth_class": eclass, "fused_cth_class": fclass, "count": count})

    prefusion_sampled: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for source in SOURCES:
        path = source_cth_file(run_dir, source)
        if path is None:
            continue
        data, valid, _ = load_npz_array(path)
        sampled, sampled_valid = sample_grid(data, valid & (data >= 0) & (data <= 25), epic["lat"], epic["lon"], grid)
        prefusion_sampled[source] = (sampled, sampled_valid)
        for policy_name in POLICIES:
            d1 = common_valid & policy_valid[policy_name] & (epic_policy[policy_name] == 1) & (fused_policy[policy_name] == 1) & sampled_valid
            r = dict(sample_meta)
            r.update({"source_name": source, "policy": policy_name, "domain": "D1_both_cloud"})
            r.update(metrics(epic["cth_km"], sampled, d1))
            prefusion_rows.append(r)

    for source_a, source_b in SOURCE_PAIR_LIST:
        if source_a not in prefusion_sampled or source_b not in prefusion_sampled:
            continue
        a, av = prefusion_sampled[source_a]
        b, bv = prefusion_sampled[source_b]
        for policy_name in POLICIES:
            d1 = common_valid & policy_valid[policy_name] & (epic_policy[policy_name] == 1) & (fused_policy[policy_name] == 1) & av & bv
            am = metrics(epic["cth_km"], a, d1)
            bm = metrics(epic["cth_km"], b, d1)
            if int(am.get("n_valid_cth", 0)) == 0:
                continue
            ae = np.abs(a[d1] - epic["cth_km"][d1])
            be = np.abs(b[d1] - epic["cth_km"][d1])
            source_pair_rows.append(
                {
                    **sample_meta,
                    "source_A": source_a,
                    "source_B": source_b,
                    "policy": policy_name,
                    "domain": "D1_both_cloud",
                    "n_common_valid_cth": am["n_valid_cth"],
                    "A_bias_km": am.get("bias_km", math.nan),
                    "B_bias_km": bm.get("bias_km", math.nan),
                    "A_mae_km": am.get("mae_km", math.nan),
                    "B_mae_km": bm.get("mae_km", math.nan),
                    "A_rmse_km": am.get("rmse_km", math.nan),
                    "B_rmse_km": bm.get("rmse_km", math.nan),
                    "B_minus_A_mae": bm.get("mae_km", math.nan) - am.get("mae_km", math.nan),
                    "A_within_2km": am.get("within_2km_fraction", math.nan),
                    "B_within_2km": bm.get("within_2km_fraction", math.nan),
                    "A_better_fraction_by_abs_error": float(np.mean(ae < be)),
                    "B_better_fraction_by_abs_error": float(np.mean(be < ae)),
                    "both_bad_fraction_abs_error_gt3km": float(np.mean((ae > 3) & (be > 3))),
                    "source_cth_disagreement_mean_abs_km": float(np.mean(np.abs(a[d1] - b[d1]))),
                }
            )

    if prefusion_sampled:
        stack_values = []
        stack_valids = []
        stack_names = []
        for source, (arr, valid) in prefusion_sampled.items():
            stack_values.append(arr)
            stack_valids.append(valid)
            stack_names.append(source)
        vals = np.stack(stack_values, axis=0)
        valids = np.stack(stack_valids, axis=0)
        errors = np.abs(vals - epic["cth_km"][None, :, :])
        errors[~valids] = np.nan
        best_err = np.nanmin(errors, axis=0)
        current_err = np.abs(fused_on - epic["cth_km"])
        best_idx = np.nanargmin(np.where(np.isfinite(errors), errors, np.inf), axis=0)
        for policy_name in POLICIES:
            d1 = common_valid & policy_valid[policy_name] & (epic_policy[policy_name] == 1) & (fused_policy[policy_name] == 1) & np.isfinite(best_err)
            groups = {
                "ALL_VALID_CTH": d1,
                "valid_source_count_ge4": d1 & (vc_on >= 4),
                "selected_MeteosatIODC": d1 & (src_on == SOURCE_TO_ID["Meteosat-IODC"]),
                "selected_Meteosat0deg": d1 & (src_on == SOURCE_TO_ID["Meteosat-0deg"]),
                "selected_MeteosatIODC_and_valid_count_ge4": d1 & (src_on == SOURCE_TO_ID["Meteosat-IODC"]) & (vc_on >= 4),
                "boundary_or_broken_cloud": d1 & (local_fraction(d1, 2) > 0.05) & (local_fraction(d1, 2) < 0.95),
                "high_cloud": d1 & ((epic["cth_km"] >= 7) | (fused_on >= 7)),
            }
            for group, mask in groups.items():
                n = int(np.count_nonzero(mask))
                if n == 0:
                    continue
                regret_rows.append(
                    {
                        **sample_meta,
                        "policy": policy_name,
                        "pixel_group": group,
                        "n_valid_cth": n,
                        "current_selected_mae_km": float(np.mean(current_err[mask])),
                        "best_available_mae_km": float(np.mean(best_err[mask])),
                        "selection_regret_mae_km": float(np.mean(current_err[mask] - best_err[mask])),
                        "selected_is_best_fraction": float(np.mean(best_idx[mask] == (src_on[mask].astype(int) - 1))),
                    }
                )

    # Case score per sample for atlas index.
    d1a = common_valid & policy_valid["A_inclusive_binary"] & (epic_policy["A_inclusive_binary"] == 1) & (fused_policy["A_inclusive_binary"] == 1)
    m = metrics(epic["cth_km"], fused_on, d1a)
    case_rows.append({**sample_meta, **m, "case_role_hint": "sample_level_candidate"})

    inv_rows = [
        {
            "product_family": "GEO-ring fused",
            "source_name": "GEO-ring",
            "sample_id": sample_id,
            "file_path": str(fused_dir / "fused_cloud_top_height_km.npz"),
            "cth_variable_name": "fused_cloud_top_height_km",
            "variable_long_name": "Stage 06 fused cloud top height",
            "units_original": fused_cth_meta.get("units", "km"),
            "units_standardized": "km",
            "scale_factor": "",
            "add_offset": "",
            "fill_value": "",
            "valid_min": float(np.nanmin(fused_cth[fused_cth_valid])) if np.any(fused_cth_valid) else "",
            "valid_max": float(np.nanmax(fused_cth[fused_cth_valid])) if np.any(fused_cth_valid) else "",
            "n_raw_valid": int(np.count_nonzero(fused_cth_valid)),
            "n_after_physical_filter": int(np.count_nonzero(fused_cth_valid & (fused_cth >= 0) & (fused_cth <= 25))),
            "unit_conversion_applied": "none",
            "unit_inferred": False,
            "notes": "Stage 06 fused CTH product.",
        },
        {
            "product_family": "EPIC_L2_CLOUD",
            "source_name": "EPIC",
            "sample_id": sample_id,
            "file_path": str(epic_path),
            "cth_variable_name": epic["cth_var"],
            "variable_long_name": epic["cth_attrs"].get("long_name", ""),
            "units_original": epic["cth_attrs"].get("units", ""),
            "units_standardized": epic["cth_units_standardized"],
            "scale_factor": epic["cth_attrs"].get("scale_factor", ""),
            "add_offset": epic["cth_attrs"].get("add_offset", ""),
            "fill_value": epic["cth_attrs"].get("_FillValue", ""),
            "valid_min": epic["cth_attrs"].get("valid_min", ""),
            "valid_max": epic["cth_attrs"].get("valid_max", ""),
            "n_raw_valid": int(np.count_nonzero(epic["cth_raw_valid"])),
            "n_after_physical_filter": int(np.count_nonzero(epic["cth_valid"])),
            "unit_conversion_applied": epic["cth_conversion"],
            "unit_inferred": False,
            "notes": "EPIC effective cloud height is used as CTH-like diagnostic reference, not truth.",
        },
    ]
    for source, (arr, valid) in prefusion_sampled.items():
        inv_rows.append(
            {
                "product_family": "GEO prefusion reprojected",
                "source_name": source,
                "sample_id": sample_id,
                "file_path": str(source_cth_file(run_dir, source)),
                "cth_variable_name": "cloud_top_height_km",
                "variable_long_name": "reprojected source cloud top height",
                "units_original": "km",
                "units_standardized": "km",
                "n_raw_valid": int(np.count_nonzero(valid)),
                "n_after_physical_filter": int(np.count_nonzero(valid & (arr >= 0) & (arr <= 25))),
                "unit_conversion_applied": "none",
                "unit_inferred": False,
            }
        )

    return {
        "inventory": inv_rows,
        "domain": domain_rows,
        "group": group_rows,
        "selected": selected_rows,
        "valid_count": vc_rows,
        "prefusion": prefusion_rows,
        "source_pair": source_pair_rows,
        "regret": regret_rows,
        "confusion": confusion_rows,
        "case": case_rows,
    }


def write_report(out: Path, summary: dict[str, Any], output_files: dict[str, Path]) -> None:
    lines = [
        "# Stage 10 GEO-ring fused CTH product validation and mechanism diagnostics",
        "",
        f"Generated: `{utc_now()}`",
        "",
        "## 定位",
        "",
        "本阶段是 `geo_ring_cloud.stage_10` 诊断型分析：读取既有 Stage 06 fused CTH、Stage 09D 样本清单和本地 EPIC L2 Cloud 文件，不新增样本、不重跑 Stage 05/06、不修改 fusion 生产逻辑。EPIC 在本报告中是 independent diagnostic reference，不是绝对真值。",
        "",
        "## 变量与单位",
        "",
        "- GEO-ring 主变量：`fused_cloud_top_height_km`，单位 km，按 0-25 km 物理范围过滤。",
        "- EPIC 主变量：`geophysical_data/A-band_Effective_Cloud_Height`，原单位 m，统一转换为 km。该变量语义是 Oxygen A-band effective cloud height，属于 CTH-like diagnostic reference，不等同严格 cloud top height。",
        "",
        "## 关键结果",
        "",
        f"- 样本数：`{summary.get('sample_count', 0)}`。",
        f"- Policy A / D1 both-cloud overall: bias `{summary.get('bias_km', math.nan):.3f}` km, MAE `{summary.get('mae_km', math.nan):.3f}` km, RMSE `{summary.get('rmse_km', math.nan):.3f}` km, within 2 km `{summary.get('within_2km_fraction', math.nan):.3f}`。",
        f"- selected_Meteosat-IODC MAE：`{summary.get('selected_iodc_mae', math.nan):.3f}` km；valid_source_count>=4 MAE：`{summary.get('valid_ge4_mae', math.nan):.3f}` km。",
        f"- selection regret valid_source_count>=4：`{summary.get('valid_ge4_regret', math.nan):.3f}` km；selected_MeteosatIODC regret：`{summary.get('iodc_regret', math.nan):.3f}` km。",
        "",
        "## 解释原则",
        "",
        "CTH 偏差应解释为 fused CTH 与 EPIC effective cloud height 在 EPIC 像元空间中的相对偏离。source selected 区域、valid source count 高值区、云边界/碎云、高云和高 VZA 是机制诊断分层，不应直接写成某个源的真实性排名。",
        "",
        "## 输出索引",
        "",
    ]
    for label, path in output_files.items():
        lines.append(f"- {label}: `{path}`")
    report = out / "reports" / "stage_10_cth_fused_product_validation_report_cn.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 10 GEO-ring fused CTH validation against EPIC CTH-like product.")
    parser.add_argument("--sample-manifest", type=Path, default=RUNS_ROOT / "stage09d_full_pixel_diagnostics_202403" / "00_sample_manifest" / "stage09d_53_sample_manifest.csv")
    parser.add_argument("--output-dir", type=Path, default=RUNS_ROOT / "stage_10_cth_fused_product_validation_202403")
    parser.add_argument("--epic-cth-var", default="geophysical_data/A-band_Effective_Cloud_Height")
    parser.add_argument("--limit-samples", type=int, default=0)
    args = parser.parse_args()

    out = args.output_dir
    ensure_dirs(out)
    manifest = read_manifest(args.sample_manifest)
    if args.limit_samples:
        manifest = manifest.head(args.limit_samples).copy()

    all_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    epic_inv_done = False
    for _, sample in manifest.iterrows():
        result = process_sample(sample, out, args.epic_cth_var)
        for key, rows in result.items():
            all_rows[key].extend(rows)
        if not epic_inv_done:
            all_rows["epic_variable_inventory"].extend(epic_inventory(Path(str(sample["epic_file"]))))
            epic_inv_done = True

    output_files: dict[str, Path] = {}
    inv_path = out / "00_inventory" / "stage_10_cth_variable_inventory.csv"
    write_csv(inv_path, all_rows["inventory"])
    output_files["variable inventory"] = inv_path
    epic_inv_path = out / "00_inventory" / "stage_10_epic_cth_variable_inventory.csv"
    write_csv(epic_inv_path, all_rows["epic_variable_inventory"])
    output_files["EPIC CTH-like inventory"] = epic_inv_path
    unit_path = out / "00_inventory" / "stage_10_cth_unit_conversion_table.csv"
    write_csv(unit_path, all_rows["inventory"])
    output_files["unit conversion table"] = unit_path

    by_sample_path = out / "02_fused_cth_metrics" / "stage_10_fused_cth_metrics_by_sample.csv"
    write_csv(by_sample_path, all_rows["domain"])
    output_files["fused CTH metrics by sample"] = by_sample_path
    domain_df = aggregate_metrics(all_rows["domain"], ["policy", "domain"])
    domain_path = out / "02_fused_cth_metrics" / "stage_10_fused_cth_metrics_by_domain.csv"
    domain_df.to_csv(domain_path, index=False, encoding="utf-8-sig")
    output_files["fused CTH metrics by domain"] = domain_path
    group_path = out / "02_fused_cth_metrics" / "stage_10_fused_cth_metrics_by_group.csv"
    group_df = aggregate_metrics(all_rows["group"], ["policy", "domain", "group_dimension", "group_value"])
    group_df.to_csv(group_path, index=False, encoding="utf-8-sig")
    output_files["fused CTH metrics by group"] = group_path
    confusion_path = out / "02_fused_cth_metrics" / "stage_10_fused_cth_low_mid_high_confusion.csv"
    confusion_df = pd.DataFrame(all_rows["confusion"]).groupby(["policy", "epic_cth_class", "fused_cth_class"], dropna=False)["count"].sum().reset_index()
    confusion_df.to_csv(confusion_path, index=False, encoding="utf-8-sig")
    output_files["low/mid/high confusion"] = confusion_path

    selected_path = out / "03_fused_cth_by_selected_source" / "stage_10_fused_cth_metrics_by_selected_source.csv"
    selected_df = aggregate_metrics(all_rows["selected"], ["policy", "domain", "selected_source"])
    selected_df.to_csv(selected_path, index=False, encoding="utf-8-sig")
    output_files["fused CTH by selected_source"] = selected_path
    vc_path = out / "03_fused_cth_by_selected_source" / "stage_10_fused_cth_metrics_by_valid_source_count.csv"
    vc_df = aggregate_metrics(all_rows["valid_count"], ["policy", "domain", "valid_source_count_bin"])
    vc_df.to_csv(vc_path, index=False, encoding="utf-8-sig")
    output_files["fused CTH by valid_source_count"] = vc_path
    focus_path = out / "03_fused_cth_by_selected_source" / "stage_10_fused_cth_meteosat_selected_focus.csv"
    selected_df[selected_df["selected_source"].astype(str).str.contains("Meteosat", na=False)].to_csv(focus_path, index=False, encoding="utf-8-sig")
    output_files["Meteosat selected focus"] = focus_path

    pref_path = out / "04_prefusion_source_cth" / "stage_10_prefusion_source_cth_metrics_by_source.csv"
    pref_df = aggregate_metrics(all_rows["prefusion"], ["policy", "domain", "source_name"])
    pref_df.to_csv(pref_path, index=False, encoding="utf-8-sig")
    output_files["prefusion source CTH"] = pref_path

    pair_path = out / "05_source_pair_cth" / "stage_10_cth_source_pair_metrics.csv"
    pair_df = pd.DataFrame(all_rows["source_pair"])
    pair_df.to_csv(pair_path, index=False, encoding="utf-8-sig")
    output_files["source-pair CTH metrics"] = pair_path
    pair_summary_path = out / "05_source_pair_cth" / "stage_10_cth_source_pair_summary.csv"
    if not pair_df.empty:
        pair_df.groupby(["policy", "source_A", "source_B"], dropna=False).mean(numeric_only=True).reset_index().to_csv(pair_summary_path, index=False, encoding="utf-8-sig")
    else:
        write_csv(pair_summary_path, [])
    output_files["source-pair CTH summary"] = pair_summary_path

    regret_path = out / "06_selection_sensitivity_cth" / "stage_10_cth_selection_regret_summary.csv"
    regret_df = pd.DataFrame(all_rows["regret"])
    if not regret_df.empty:
        regret_df.groupby(["policy", "pixel_group"], dropna=False).mean(numeric_only=True).reset_index().to_csv(regret_path, index=False, encoding="utf-8-sig")
    else:
        write_csv(regret_path, [])
    output_files["CTH selection regret"] = regret_path

    height_path = out / "07_geometry_boundary_height" / "stage_10_cth_error_by_height_geometry_boundary.csv"
    group_df.to_csv(height_path, index=False, encoding="utf-8-sig")
    output_files["geometry/boundary/height stratification"] = height_path
    case_path = out / "08_case_atlas" / "stage_10_cth_case_inventory.csv"
    pd.DataFrame(all_rows["case"]).sort_values("mae_km", ascending=False).to_csv(case_path, index=False, encoding="utf-8-sig")
    output_files["case atlas index"] = case_path

    plot_rows: list[dict[str, Any]] = []
    make_bar(out / "09_figures" / "fig01_fused_cth_bias_mae_by_domain.png", domain_df[domain_df["policy"] == "A_inclusive_binary"], "domain", "mae_km", "Policy A fused CTH MAE by domain", domain_path, plot_rows)
    make_bar(out / "09_figures" / "fig03_fused_cth_mae_by_selected_source.png", selected_df[selected_df["policy"] == "A_inclusive_binary"], "selected_source", "mae_km", "Policy A MAE by selected source", selected_path, plot_rows)
    make_bar(out / "09_figures" / "fig04_fused_cth_mae_by_valid_source_count.png", vc_df[vc_df["policy"] == "A_inclusive_binary"], "valid_source_count_bin", "mae_km", "Policy A MAE by valid source count", vc_path, plot_rows)
    make_bar(out / "09_figures" / "fig05_prefusion_source_cth_mae.png", pref_df[pref_df["policy"] == "A_inclusive_binary"], "source_name", "mae_km", "Policy A prefusion source MAE", pref_path, plot_rows)
    plot_index_path = out / "09_figures" / "stage_10_cth_plot_index.csv"
    write_csv(plot_index_path, plot_rows)
    output_files["plot index"] = plot_index_path

    overall = domain_df[(domain_df["policy"] == "A_inclusive_binary") & (domain_df["domain"] == "D1_both_cloud")]
    summary: dict[str, Any] = {"sample_count": int(len(manifest))}
    if not overall.empty:
        summary.update(overall.iloc[0].to_dict())
    iodc = selected_df[(selected_df["policy"] == "A_inclusive_binary") & (selected_df["selected_source"] == "Meteosat-IODC")]
    ge4 = vc_df[(vc_df["policy"] == "A_inclusive_binary") & (vc_df["valid_source_count_bin"] == "valid_source_count_ge4")]
    rsum = pd.read_csv(regret_path, encoding="utf-8-sig") if regret_path.exists() and regret_path.stat().st_size else pd.DataFrame()
    summary["selected_iodc_mae"] = float(iodc["mae_km"].iloc[0]) if not iodc.empty else math.nan
    summary["valid_ge4_mae"] = float(ge4["mae_km"].iloc[0]) if not ge4.empty else math.nan
    summary["valid_ge4_regret"] = float(rsum.loc[(rsum["policy"] == "A_inclusive_binary") & (rsum["pixel_group"] == "valid_source_count_ge4"), "selection_regret_mae_km"].iloc[0]) if not rsum.empty and np.any((rsum["policy"] == "A_inclusive_binary") & (rsum["pixel_group"] == "valid_source_count_ge4")) else math.nan
    summary["iodc_regret"] = float(rsum.loc[(rsum["policy"] == "A_inclusive_binary") & (rsum["pixel_group"] == "selected_MeteosatIODC"), "selection_regret_mae_km"].iloc[0]) if not rsum.empty and np.any((rsum["policy"] == "A_inclusive_binary") & (rsum["pixel_group"] == "selected_MeteosatIODC")) else math.nan
    summary_path = out / "stage_10_cth_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    output_files["summary manifest"] = summary_path
    write_report(out, summary, output_files)


if __name__ == "__main__":
    main()
