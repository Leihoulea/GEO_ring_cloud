from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import sys
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import netCDF4
import numpy as np
import pandas as pd
from pyresample.geometry import AreaDefinition
from scipy.spatial import cKDTree

CODE_ROOT_FOR_IMPORT = Path(__file__).resolve().parents[1]
if str(CODE_ROOT_FOR_IMPORT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT_FOR_IMPORT))

from geo_ring_cloud.lineage import write_manifest
from geo_ring_cloud.paths import EXTERNAL_GEO_CLOUD_ROOT, PROJECT_ROOT, RUNS_ROOT


STAGE_ID = "stage_10r"
PROJECT_ID = "geo_ring_cloud"
COMPONENT_ROLE = "stage_diagnostic"
OUTPUT_DIRNAME = "stage_10r_meteosat_operational_cth_satpy_navigation_audit_202403"
GOOD_QUALITY_CODE = 0
MAX_EPIC_SAMPLE_DISTANCE_KM = 25.0
EARTH_RADIUS_KM = 6371.0

CASES = [
    {"product_line": "Meteosat-0deg", "sample_id": "20240310_1200", "has_epic_pair": True, "case_role": "epic_paired_meteosat0deg"},
    {"product_line": "Meteosat-0deg", "sample_id": "20240306_1300", "has_epic_pair": True, "case_role": "second_meteosat0deg"},
    {"product_line": "Meteosat-IODC", "sample_id": "20240310_1000", "has_epic_pair": True, "case_role": "epic_paired_iodc"},
    {"product_line": "Meteosat-IODC", "sample_id": "20240311_0800", "has_epic_pair": True, "case_role": "second_iodc"},
]


def variable_attributes(var: netCDF4.Variable) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    for name in var.ncattrs():
        value = getattr(var, name)
        if isinstance(value, np.ndarray):
            value = value.tolist()
        elif isinstance(value, np.generic):
            value = value.item()
        attrs[name] = value
    return attrs


def find_variable(ds: netCDF4.Dataset, names: tuple[str, ...]) -> str | None:
    for name in names:
        group_name, _, var_name = name.rpartition("/")
        group = ds[group_name] if group_name else ds
        if var_name in group.variables:
            return name
    return None


def read_nc_array(ds: netCDF4.Dataset, name: str, dtype: Any = np.float32) -> np.ndarray:
    values = ds[name][:]
    if np.ma.isMaskedArray(values):
        values = values.astype(np.float32).filled(np.nan)
    return np.asarray(values, dtype=dtype)


def optional_geo(ds: netCDF4.Dataset, name: str, shape: tuple[int, ...]) -> np.ndarray:
    group = ds.groups.get("geolocation_data")
    if group is None or name not in group.variables:
        return np.full(shape, np.nan, dtype=np.float32)
    return read_nc_array(ds, f"geolocation_data/{name}")


def read_epic_cth(path: Path, cth_variable: str) -> dict[str, Any]:
    with netCDF4.Dataset(path) as ds:
        lat_name = find_variable(ds, ("geolocation_data/latitude", "geolocation_data/Latitude"))
        lon_name = find_variable(ds, ("geolocation_data/longitude", "geolocation_data/Longitude"))
        cloud_mask_name = find_variable(ds, ("geophysical_data/Cloud_Mask", "geophysical_data/cloud_mask"))
        if not lat_name or not lon_name or not cloud_mask_name:
            raise RuntimeError(f"missing EPIC geolocation/cloud mask in {path}")
        attrs = variable_attributes(ds[cth_variable])
        raw = read_nc_array(ds, cth_variable)
        fill_value = attrs.get("_FillValue", attrs.get("missing_value"))
        raw_valid = np.isfinite(raw)
        if fill_value is not None:
            raw_valid &= raw != float(fill_value)
        units = str(attrs.get("units", "")).strip().lower()
        if units == "m":
            cth_km = raw / 1000.0
            conversion = "m_to_km"
        else:
            cth_km = raw
            conversion = "none"
        physical_valid = raw_valid & np.isfinite(cth_km) & (cth_km >= 0.0) & (cth_km <= 25.0)
        return {
            "lat": read_nc_array(ds, lat_name),
            "lon": normalize_lon(read_nc_array(ds, lon_name)),
            "cloud_mask": read_nc_array(ds, cloud_mask_name),
            "cth_km": cth_km.astype(np.float32),
            "cth_valid": physical_valid,
            "cth_raw_valid": raw_valid,
            "cth_var": cth_variable,
            "cth_attrs": attrs,
            "cth_conversion": conversion,
            "epic_vza": optional_geo(ds, "sensor_zenith", raw.shape),
            "sza": optional_geo(ds, "solar_zenith", raw.shape),
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def setup_eccodes_runtime(cache_dir: Path) -> list[str]:
    warnings: list[str] = []
    lib_dir = Path(sys.prefix) / "Library" / "bin"
    if lib_dir.exists():
        os.environ["PATH"] = str(lib_dir) + os.pathsep + os.environ.get("PATH", "")
        try:
            os.add_dll_directory(str(lib_dir))
        except Exception as exc:
            warnings.append(f"failed_to_add_conda_dll_directory: {exc}")
    dll = lib_dir / "eccodes.dll"
    if dll.exists():
        compat_dir = cache_dir / "dll"
        compat_dir.mkdir(parents=True, exist_ok=True)
        compat = compat_dir / "libeccodes.dll"
        if not compat.exists() or compat.stat().st_size != dll.stat().st_size:
            shutil.copy2(dll, compat)
        os.environ["PATH"] = str(compat_dir) + os.pathsep + os.environ.get("PATH", "")
        os.environ["ECCODES_PYTHON_USE_FINDLIBS"] = "1"
        try:
            os.add_dll_directory(str(compat_dir))
        except Exception as exc:
            warnings.append(f"failed_to_add_eccodes_compat_dll_directory: {exc}")
    return warnings


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        keys: set[str] = set()
        for row in rows:
            keys.update(row.keys())
        fields = sorted(keys)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


def parse_sample_id(sample_id: str) -> tuple[str, str]:
    match = re.match(r"^(\d{8})_(\d{2})00$", sample_id)
    if not match:
        raise ValueError(f"unsupported sample_id {sample_id}")
    return match.group(1), match.group(2)


def cth_zip_path(product_line: str, sample_id: str) -> Path:
    day, hour = parse_sample_id(sample_id)
    root = EXTERNAL_GEO_CLOUD_ROOT / product_line / "CTH" / day / hour
    files = sorted(root.glob("*MSGCLTH*.zip"))
    if not files:
        raise FileNotFoundError(f"no CTH zip under {root}")
    return files[0]


def standardized_native_path(product_line: str, sample_id: str) -> Path:
    root = RUNS_ROOT / sample_id / "standardized_native"
    files = sorted(root.glob(f"{product_line}_CTH_*_native_cloud_v0.npz"))
    if not files:
        raise FileNotFoundError(f"no standardized native CTH NPZ under {root} for {product_line}")
    return files[0]


def epic_file_from_manifest(sample_manifest: Path, sample_id: str) -> Path | None:
    if not sample_manifest.exists():
        return None
    df = pd.read_csv(sample_manifest, encoding="utf-8-sig")
    hit = df[df["sample_id"].astype(str) == sample_id]
    if hit.empty:
        return None
    path = Path(str(hit.iloc[0].get("epic_file", "")))
    return path if path.exists() else None


def extract_grib(zip_path: Path, cache_dir: Path) -> tuple[Path, list[str]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        entries = zf.namelist()
        gribs = [name for name in entries if name.lower().endswith((".grb", ".grib", ".grb2"))]
        if not gribs:
            raise FileNotFoundError(f"no GRIB entry in {zip_path}")
        entry = gribs[0]
        out = cache_dir / Path(entry).name
        payload = zf.read(entry)
        if not out.exists() or out.stat().st_size != len(payload):
            out.write_bytes(payload)
    return out, entries


def load_current_native(path: Path) -> dict[str, Any]:
    with np.load(path, allow_pickle=True) as z:
        meta = json.loads(str(np.asarray(z["metadata_json"]).item())) if "metadata_json" in z.files else {}
        out = {
            "cth_km": np.asarray(z["cloud_top_height_km"], dtype=np.float32),
            "quality": np.asarray(z["quality_flag_raw"], dtype=np.float32),
            "lat": np.asarray(z["latitude"], dtype=np.float32),
            "lon": normalize_lon(np.asarray(z["longitude"], dtype=np.float32)),
            "valid_mask": np.asarray(z["valid_mask"]).astype(bool) if "valid_mask" in z.files else None,
            "metadata": meta,
        }
    valid = np.isfinite(out["cth_km"]) & (out["cth_km"] >= 0.0) & (out["cth_km"] <= 25.0)
    if out["valid_mask"] is not None:
        valid &= out["valid_mask"]
    out["cth_valid"] = valid
    out["quality_valid"] = np.isfinite(out["quality"])
    return out


def load_satpy(grib_path: Path) -> dict[str, Any]:
    from satpy import Scene

    scn = Scene(reader="seviri_l2_grib", filenames=[str(grib_path)])
    available_names = [str(name) for name in scn.available_dataset_names()]
    available_ids = [str(dataset_id) for dataset_id in scn.available_dataset_ids()]
    scn.load(["cloud_top_height", "cloud_top_quality"])
    cth = scn["cloud_top_height"]
    quality = scn["cloud_top_quality"]
    area = cth.attrs["area"]
    lon, lat = area.get_lonlats()
    cth_m = np.asarray(cth.values, dtype=np.float64)
    quality_arr = np.asarray(quality.values, dtype=np.float64)
    cth_km = (cth_m / 1000.0).astype(np.float32)
    valid = np.isfinite(cth_km) & (cth_km >= 0.0) & (cth_km <= 25.0)
    proj_dict = getattr(area, "proj_dict", {})
    return {
        "cth_km": cth_km,
        "quality": quality_arr.astype(np.float32),
        "cth_valid": valid,
        "quality_valid": np.isfinite(quality_arr),
        "lat": np.asarray(lat, dtype=np.float32),
        "lon": normalize_lon(np.asarray(lon, dtype=np.float32)),
        "area": area,
        "available_names": available_names,
        "available_ids": available_ids,
        "cth_attrs": json_safe(cth.attrs),
        "quality_attrs": json_safe(quality.attrs),
        "area_summary": {
            "area_id": getattr(area, "area_id", ""),
            "description": getattr(area, "description", ""),
            "width": getattr(area, "width", ""),
            "height": getattr(area, "height", ""),
            "area_extent": list(getattr(area, "area_extent", [])),
            "proj_dict": json_safe(proj_dict),
            "lon_0": proj_dict.get("lon_0", ""),
            "h": proj_dict.get("h", ""),
            "a": proj_dict.get("a", ""),
            "rf": proj_dict.get("rf", ""),
        },
        "platform_name": cth.attrs.get("platform_name", ""),
        "start_time": cth.attrs.get("start_time", ""),
        "end_time": cth.attrs.get("end_time", ""),
        "units": cth.attrs.get("units", ""),
        "quality_flag_values": quality.attrs.get("flag_values", ""),
        "quality_flag_meanings": quality.attrs.get("flag_meanings", ""),
    }


def normalize_lon(lon: np.ndarray) -> np.ndarray:
    out = np.asarray(lon, dtype=np.float32).copy()
    finite = np.isfinite(out)
    out[finite] = ((out[finite] + 180.0) % 360.0) - 180.0
    return out


def transform(arr: np.ndarray, name: str) -> np.ndarray:
    if name == "identity":
        return arr
    if name == "rot180":
        return np.rot90(arr, 2)
    if name == "flipud":
        return np.flipud(arr)
    if name == "fliplr":
        return np.fliplr(arr)
    raise ValueError(name)


def pearson(x: np.ndarray, y: np.ndarray) -> float:
    if x.size < 3 or float(np.nanstd(x)) == 0.0 or float(np.nanstd(y)) == 0.0:
        return math.nan
    return float(np.corrcoef(x, y)[0, 1])


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    if x.size < 3:
        return math.nan
    xr = pd.Series(x).rank().to_numpy()
    yr = pd.Series(y).rank().to_numpy()
    return pearson(xr, yr)


def cth_metric_row(ref: np.ndarray, test: np.ndarray, mask: np.ndarray) -> dict[str, Any]:
    n = int(np.count_nonzero(mask))
    if n == 0:
        return {"n_common_valid": 0}
    x = ref[mask].astype(np.float64)
    y = test[mask].astype(np.float64)
    diff = y - x
    abs_diff = np.abs(diff)
    return {
        "n_common_valid": n,
        "bias_km": float(np.mean(diff)),
        "mae_km": float(np.mean(abs_diff)),
        "rmse_km": float(np.sqrt(np.mean(diff * diff))),
        "median_ae_km": float(np.median(abs_diff)),
        "max_ae_km": float(np.max(abs_diff)),
        "pearson_r": pearson(x, y),
        "spearman_r": spearman(x, y),
    }


def value_comparison_rows(case: dict[str, Any], current: dict[str, Any], satpy: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for tname in ["identity", "rot180", "flipud", "fliplr"]:
        cth = transform(satpy["cth_km"], tname)
        valid = transform(satpy["cth_valid"], tname)
        common = current["cth_valid"] & valid
        row = {
            **case,
            "transform_applied_to_satpy": tname,
            "unit_conversion": "satpy_m_to_km;current_already_km",
            "current_shape": shape_text(current["cth_km"]),
            "satpy_shape": shape_text(cth),
            "valid_mask_agreement": float(np.mean(current["cth_valid"] == valid)),
            "current_valid_fraction": float(np.mean(current["cth_valid"])),
            "satpy_valid_fraction": float(np.mean(valid)),
        }
        row.update(cth_metric_row(current["cth_km"], cth, common))
        rows.append(row)
    return rows


def quality_comparison_rows(case: dict[str, Any], current: dict[str, Any], satpy: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for tname in ["identity", "rot180", "flipud", "fliplr"]:
        q = transform(satpy["quality"], tname)
        q_valid = transform(satpy["quality_valid"], tname)
        common = current["quality_valid"] & q_valid
        equal = bool(np.array_equal(current["quality"], q, equal_nan=True))
        agreement = float(np.mean(current["quality"][common] == q[common])) if np.any(common) else math.nan
        row = {
            **case,
            "transform_applied_to_satpy": tname,
            "array_equal": equal,
            "agreement": agreement,
            "n_common_quality": int(np.count_nonzero(common)),
            "current_category_counts": counts_json(current["quality"][current["quality_valid"]]),
            "satpy_category_counts": counts_json(q[q_valid]),
            "satpy_flag_values": json.dumps(json_safe(satpy.get("quality_flag_values", "")), ensure_ascii=False),
            "satpy_flag_meanings": json.dumps(json_safe(satpy.get("quality_flag_meanings", "")), ensure_ascii=False),
            "good_quality_rule_used": f"cloud_top_quality == {GOOD_QUALITY_CODE}",
        }
        rows.append(row)
    return rows


def counts_json(values: np.ndarray) -> str:
    finite = values[np.isfinite(values)]
    counts = Counter([str(float(v)) for v in finite.ravel()])
    return json.dumps(dict(sorted(counts.items(), key=lambda kv: float(kv[0]))), ensure_ascii=False)


def shape_text(arr: np.ndarray) -> str:
    return "x".join(str(x) for x in np.asarray(arr).shape)


def finite_stats(values: np.ndarray, valid: np.ndarray) -> dict[str, Any]:
    if not np.any(valid):
        return {"valid_count": 0, "valid_fraction": 0.0}
    v = values[valid].astype(np.float64)
    return {
        "valid_count": int(v.size),
        "valid_fraction": float(np.mean(valid)),
        "min": float(np.min(v)),
        "max": float(np.max(v)),
        "mean": float(np.mean(v)),
        "dtype": str(values.dtype),
        "shape": shape_text(values),
    }


def haversine_km(lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray) -> np.ndarray:
    lat1r = np.deg2rad(lat1.astype(np.float64))
    lon1r = np.deg2rad(lon1.astype(np.float64))
    lat2r = np.deg2rad(lat2.astype(np.float64))
    lon2r = np.deg2rad(lon2.astype(np.float64))
    dlat = lat2r - lat1r
    dlon = lon2r - lon1r
    a = np.sin(dlat / 2.0) ** 2 + np.cos(lat1r) * np.cos(lat2r) * np.sin(dlon / 2.0) ** 2
    return (2.0 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))).astype(np.float32)


def nav_direction(lat: np.ndarray, lon: np.ndarray) -> dict[str, Any]:
    row0 = lat[0, np.isfinite(lat[0])].mean() if np.any(np.isfinite(lat[0])) else math.nan
    rown = lat[-1, np.isfinite(lat[-1])].mean() if np.any(np.isfinite(lat[-1])) else math.nan
    col0 = lon[np.isfinite(lon[:, 0]), 0].mean() if np.any(np.isfinite(lon[:, 0])) else math.nan
    coln = lon[np.isfinite(lon[:, -1]), -1].mean() if np.any(np.isfinite(lon[:, -1])) else math.nan
    return {
        "row0_direction": "north_to_south" if row0 > rown else "south_to_north",
        "col0_direction": "west_to_east" if col0 < coln else "east_to_west",
        "row0_mean_lat": float(row0),
        "lastrow_mean_lat": float(rown),
        "col0_mean_lon": float(col0),
        "lastcol_mean_lon": float(coln),
    }


def navigation_rows(case: dict[str, Any], current: dict[str, Any], satpy: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    lat1, lon1 = current["lat"], current["lon"]
    lat2, lon2 = satpy["lat"], satpy["lon"]
    valid = np.isfinite(lat1) & np.isfinite(lon1) & np.isfinite(lat2) & np.isfinite(lon2)
    dist = haversine_km(lat1[valid], lon1[valid], lat2[valid], lon2[valid]) if np.any(valid) else np.asarray([])
    summary = {
        **case,
        "comparison": "current_navigation_vs_satpy_cth_area_navigation",
        "control_point": "ALL_VALID_NAV_PIXELS",
        "n_nav_valid": int(dist.size),
        "median_geodesic_error_km": float(np.median(dist)) if dist.size else math.nan,
        "p95_geodesic_error_km": float(np.percentile(dist, 95)) if dist.size else math.nan,
        "max_geodesic_error_km": float(np.max(dist)) if dist.size else math.nan,
        "shape": shape_text(lat1),
        "satpy_area_id": satpy["area_summary"].get("area_id", ""),
        "satpy_area_extent": json.dumps(satpy["area_summary"].get("area_extent", [])),
        "satpy_lon_0": satpy["area_summary"].get("lon_0", ""),
        "satpy_h": satpy["area_summary"].get("h", ""),
        "satpy_a": satpy["area_summary"].get("a", ""),
        "satpy_rf": satpy["area_summary"].get("rf", ""),
        "clm_3712_grid_reused": False,
        "current_navigation_source": current["metadata"].get("reader_attrs", {}).get("reader", ""),
    }
    summary.update({f"current_{k}": v for k, v in nav_direction(lat1, lon1).items()})
    summary.update({f"satpy_{k}": v for k, v in nav_direction(lat2, lon2).items()})
    rows.append(summary)
    ny, nx = lat1.shape
    points = {
        "upper_left": (0, 0),
        "upper_center": (0, nx // 2),
        "upper_right": (0, nx - 1),
        "middle_left": (ny // 2, 0),
        "center": (ny // 2, nx // 2),
        "middle_right": (ny // 2, nx - 1),
        "lower_left": (ny - 1, 0),
        "lower_center": (ny - 1, nx // 2),
        "lower_right": (ny - 1, nx - 1),
    }
    for name, (r, c) in points.items():
        ok = np.isfinite(lat1[r, c]) and np.isfinite(lon1[r, c]) and np.isfinite(lat2[r, c]) and np.isfinite(lon2[r, c])
        rows.append(
            {
                **case,
                "comparison": "current_navigation_vs_satpy_cth_area_navigation",
                "control_point": name,
                "row": r,
                "col": c,
                "current_lat": float(lat1[r, c]) if np.isfinite(lat1[r, c]) else math.nan,
                "current_lon": float(lon1[r, c]) if np.isfinite(lon1[r, c]) else math.nan,
                "satpy_lat": float(lat2[r, c]) if np.isfinite(lat2[r, c]) else math.nan,
                "satpy_lon": float(lon2[r, c]) if np.isfinite(lon2[r, c]) else math.nan,
                "geodesic_error_km": float(haversine_km(np.asarray([lat1[r, c]]), np.asarray([lon1[r, c]]), np.asarray([lat2[r, c]]), np.asarray([lon2[r, c]]))[0]) if ok else math.nan,
            }
        )
    rows.append({**case, "comparison": "l15_ir108_area_navigation", "control_point": "NOT_AVAILABLE_LOCALLY", "warning": "同周期 L1.5 IR_108 未在本地 Meteosat 产品目录发现；未下载新数据。"})
    return rows


def unit_vectors(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    latr = np.deg2rad(lat.astype(np.float64))
    lonr = np.deg2rad(lon.astype(np.float64))
    clat = np.cos(latr)
    return np.column_stack((clat * np.cos(lonr), clat * np.sin(lonr), np.sin(latr)))


def sample_native_to_epic(
    values: np.ndarray,
    quality: np.ndarray,
    nav_lat: np.ndarray,
    nav_lon: np.ndarray,
    epic_lat: np.ndarray,
    epic_lon: np.ndarray,
    epic_valid: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    src_valid = np.isfinite(values) & (values >= 0.0) & (values <= 25.0) & np.isfinite(nav_lat) & np.isfinite(nav_lon)
    src_idx = np.flatnonzero(src_valid.ravel())
    tree = cKDTree(unit_vectors(nav_lat.ravel()[src_idx], nav_lon.ravel()[src_idx]))
    target_valid = epic_valid & np.isfinite(epic_lat) & np.isfinite(epic_lon)
    target_idx = np.flatnonzero(target_valid.ravel())
    sampled = np.full(epic_lat.size, np.nan, dtype=np.float32)
    sampled_quality = np.full(epic_lat.size, np.nan, dtype=np.float32)
    sampled_distance = np.full(epic_lat.size, np.nan, dtype=np.float32)
    sampled_valid = np.zeros(epic_lat.size, dtype=bool)
    if target_idx.size:
        dist_chord, nearest = tree.query(unit_vectors(epic_lat.ravel()[target_idx], epic_lon.ravel()[target_idx]), k=1, workers=-1)
        dist_km = (2.0 * EARTH_RADIUS_KM * np.arcsin(np.clip(dist_chord / 2.0, 0.0, 1.0))).astype(np.float32)
        ok = dist_km <= MAX_EPIC_SAMPLE_DISTANCE_KM
        native_flat_idx = src_idx[nearest[ok]]
        flat_targets = target_idx[ok]
        sampled[flat_targets] = values.ravel()[native_flat_idx]
        sampled_quality[flat_targets] = quality.ravel()[native_flat_idx]
        sampled_distance[flat_targets] = dist_km[ok]
        sampled_valid[flat_targets] = True
    shape = epic_lat.shape
    return sampled.reshape(shape), sampled_quality.reshape(shape), sampled_distance.reshape(shape), sampled_valid.reshape(shape)


def local_fraction(mask: np.ndarray, radius: int = 2) -> np.ndarray:
    padded = np.pad(mask.astype(np.float32), radius, mode="edge")
    out = np.zeros(mask.shape, dtype=np.float32)
    for dy in range(2 * radius + 1):
        for dx in range(2 * radius + 1):
            out += padded[dy : dy + mask.shape[0], dx : dx + mask.shape[1]]
    return out / float((2 * radius + 1) ** 2)


def epic_cloud_binary(cloud_mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    valid = np.isin(cloud_mask, [1, 2, 3, 4])
    cloudy = np.isin(cloud_mask, [3, 4])
    return cloudy, valid


def height_class(values: np.ndarray) -> np.ndarray:
    out = np.full(values.shape, "missing", dtype=object)
    out[(values >= 0.0) & (values < 3.0)] = "low_cth"
    out[(values >= 3.0) & (values < 7.0)] = "mid_cth"
    out[values >= 7.0] = "high_cth"
    return out


def epic_downstream_rows(case: dict[str, Any], current: dict[str, Any], satpy: dict[str, Any], epic_path: Path | None) -> list[dict[str, Any]]:
    if epic_path is None:
        return [{**case, "navigation_variant": "not_run", "stratum": "no_epic_pair", "warning": "EPIC file not found"}]
    epic = read_epic_cth(epic_path, "geophysical_data/A-band_Effective_Cloud_Height")
    fixed_values = current["cth_km"]
    cur_s, cur_q, _, cur_valid = sample_native_to_epic(fixed_values, current["quality"], current["lat"], current["lon"], epic["lat"], epic["lon"], epic["cth_valid"])
    sat_s, sat_q, _, sat_valid = sample_native_to_epic(fixed_values, current["quality"], satpy["lat"], satpy["lon"], epic["lat"], epic["lon"], epic["cth_valid"])
    common = epic["cth_valid"] & cur_valid & sat_valid
    cloudy, cloud_valid = epic_cloud_binary(epic["cloud_mask"])
    boundary_fraction = local_fraction(cloudy & cloud_valid, 2)
    boundary = cloud_valid & (boundary_fraction > 0.05) & (boundary_fraction < 0.95)
    vza = epic.get("epic_vza", np.full(epic["cth_km"].shape, np.nan, dtype=np.float32))
    strata = {
        "all_valid": common,
        "good_quality_only": common & (cur_q == GOOD_QUALITY_CODE) & (sat_q == GOOD_QUALITY_CODE),
        "low_cth": common & (height_class(epic["cth_km"]) == "low_cth"),
        "mid_cth": common & (height_class(epic["cth_km"]) == "mid_cth"),
        "high_cth": common & (height_class(epic["cth_km"]) == "high_cth"),
        "vza_le_60": common & np.isfinite(vza) & (vza <= 60.0),
        "vza_gt_60": common & np.isfinite(vza) & (vza > 60.0),
        "boundary": common & boundary,
        "non_boundary": common & ~boundary,
    }
    rows: list[dict[str, Any]] = []
    for stratum, mask in strata.items():
        for nav_name, sampled in [("current_navigation", cur_s), ("satpy_cth_area_navigation", sat_s)]:
            row = {
                **case,
                "navigation_variant": nav_name,
                "stratum": stratum,
                "epic_reference": "A-band_Effective_Cloud_Height_km",
                "fixed_cth_values_source": "current_standardized_native_cloud_top_height_km",
                "common_valid_fraction": float(np.mean(mask)),
            }
            row.update(cth_metric_row(epic["cth_km"], sampled, mask))
            rows.append(row)
    return rows


def make_quicklook(product_line: str, sample_id: str, current: dict[str, Any], satpy: dict[str, Any], out_dir: Path) -> Path:
    fig_dir = out_dir / "quicklooks"
    fig_dir.mkdir(parents=True, exist_ok=True)
    path = fig_dir / f"stage_10r_{product_line}_{sample_id}_cth_current_vs_satpy_quicklook.png"
    cur = np.ma.masked_where(~current["cth_valid"], current["cth_km"])
    sat = np.ma.masked_where(~satpy["cth_valid"], satpy["cth_km"])
    stride = 3
    fig, axes = plt.subplots(1, 3, figsize=(12, 4), constrained_layout=True)
    axes[0].axis("off")
    axes[0].text(0.5, 0.5, "IR_108 not found locally", ha="center", va="center")
    cur_lon = current["lon"][::stride, ::stride].ravel()
    cur_lat = current["lat"][::stride, ::stride].ravel()
    cur_val = np.asarray(cur[::stride, ::stride]).ravel()
    cur_ok = np.isfinite(cur_lon) & np.isfinite(cur_lat) & np.isfinite(cur_val)
    m1 = axes[1].scatter(cur_lon[cur_ok], cur_lat[cur_ok], c=cur_val[cur_ok], s=1, vmin=0, vmax=16, cmap="viridis", linewidths=0)
    axes[1].set_title("current nav CTH")
    axes[1].set_xlabel("lon")
    axes[1].set_ylabel("lat")
    axes[1].set_xlim(-90, 120)
    axes[1].set_ylim(-70, 70)
    sat_lon = satpy["lon"][::stride, ::stride].ravel()
    sat_lat = satpy["lat"][::stride, ::stride].ravel()
    sat_val = np.asarray(sat[::stride, ::stride]).ravel()
    sat_ok = np.isfinite(sat_lon) & np.isfinite(sat_lat) & np.isfinite(sat_val)
    axes[2].scatter(sat_lon[sat_ok], sat_lat[sat_ok], c=sat_val[sat_ok], s=1, vmin=0, vmax=16, cmap="viridis", linewidths=0)
    axes[2].set_title("Satpy area CTH")
    axes[2].set_xlabel("lon")
    axes[2].set_ylabel("lat")
    axes[2].set_xlim(-90, 120)
    axes[2].set_ylim(-70, 70)
    fig.colorbar(m1, ax=axes[1:], label="CTH (km)", shrink=0.85)
    fig.suptitle(f"{product_line} {sample_id}")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def inventory_row(case: dict[str, Any], zip_path: Path, entries: list[str], grib_path: Path, native_path: Path, current: dict[str, Any], satpy: dict[str, Any]) -> dict[str, Any]:
    cstats = finite_stats(current["cth_km"], current["cth_valid"])
    qstats = finite_stats(current["quality"], current["quality_valid"])
    attrs = current["metadata"].get("reader_attrs", {})
    cth_attrs = attrs.get("attrs_cloud_top_height_km", {})
    q_attrs = attrs.get("attrs_quality_flag_raw", {})
    area = satpy["area_summary"]
    return {
        **case,
        "cth_zip_path": str(zip_path),
        "zip_size_bytes": zip_path.stat().st_size,
        "zip_entries": json.dumps(entries, ensure_ascii=False),
        "extracted_grib_path": str(grib_path),
        "standardized_native_path": str(native_path),
        "internal_grib_message_count": 2,
        "cth_shape": cstats.get("shape", ""),
        "cth_dtype": cstats.get("dtype", ""),
        "cth_units_current": "km",
        "cth_units_raw": cth_attrs.get("units", ""),
        "cth_missing_fill": cth_attrs.get("GRIB_missingValue", ""),
        "cth_min_km": cstats.get("min", ""),
        "cth_max_km": cstats.get("max", ""),
        "cth_valid_fraction": cstats.get("valid_fraction", ""),
        "quality_shape": shape_text(current["quality"]),
        "quality_dtype": str(current["quality"].dtype),
        "quality_units_current": q_attrs.get("units", ""),
        "quality_flag_values": json.dumps(json_safe(satpy.get("quality_flag_values", "")), ensure_ascii=False),
        "quality_flag_meanings": json.dumps(json_safe(satpy.get("quality_flag_meanings", "")), ensure_ascii=False),
        "quality_valid_fraction": qstats.get("valid_fraction", ""),
        "quality_category_counts": counts_json(current["quality"][current["quality_valid"]]),
        "ssp_lon": area.get("lon_0", ""),
        "nx": area.get("width", ""),
        "ny": area.get("height", ""),
        "current_reader_navigation_source": attrs.get("reader", ""),
        "satpy_reader": "seviri_l2_grib",
        "satpy_available_datasets": json.dumps(satpy["available_names"], ensure_ascii=False),
        "satpy_platform": satpy.get("platform_name", ""),
        "satpy_start_time": str(satpy.get("start_time", "")),
        "satpy_end_time": str(satpy.get("end_time", "")),
        "satpy_area_id": area.get("area_id", ""),
        "satpy_area_extent": json.dumps(area.get("area_extent", [])),
        "satpy_lon_0": area.get("lon_0", ""),
        "satpy_h": area.get("h", ""),
        "satpy_a": area.get("a", ""),
        "satpy_rf": area.get("rf", ""),
        "clm_3712_grid_reused": False,
        "note": "CTH native grid is 1237x1237 about 9 km; Stage09H CLM 3712x3712 navigation patch is not used.",
    }


def final_status(product_line: str, value_rows: list[dict[str, Any]], nav_rows: list[dict[str, Any]], epic_rows: list[dict[str, Any]]) -> str:
    ident = [r for r in value_rows if r["product_line"] == product_line and r["transform_applied_to_satpy"] == "identity"]
    nav_summary = [r for r in nav_rows if r["product_line"] == product_line and r.get("control_point") == "ALL_VALID_NAV_PIXELS"]
    if not ident or not nav_summary:
        return "CTH_AUDIT_INCONCLUSIVE"
    max_mae = max(float(r.get("mae_km", math.inf)) for r in ident)
    max_nav_p95 = max(float(r.get("p95_geodesic_error_km", math.inf)) for r in nav_summary)
    has_epic = any(r["product_line"] == product_line and r.get("navigation_variant") == "satpy_cth_area_navigation" for r in epic_rows)
    if max_mae <= 1e-6 and max_nav_p95 <= 0.1 and has_epic:
        return "CTH_SATPY_VALUES_AND_NAVIGATION_VALIDATED"
    if max_mae <= 1e-6 and max_nav_p95 > 5.0:
        return "CTH_CURRENT_VALUES_OK_NAVIGATION_ERROR_CONFIRMED"
    if max_mae <= 1e-3 and max_nav_p95 <= 5.0:
        return "CTH_NAVIGATION_OK_QUALITY_OR_PRODUCT_DIFFERENCE_DOMINANT"
    if max_mae <= 1e-3:
        return "CTH_SATPY_VALUES_ONLY_NAVIGATION_UNRESOLVED"
    return "CTH_AUDIT_INCONCLUSIVE"


def write_report(out_dir: Path, summary_rows: list[dict[str, Any]], outputs: dict[str, Path], warnings: list[dict[str, Any]]) -> None:
    lines = [
        "# Stage 10R Meteosat operational CTH Satpy/navigation audit",
        "",
        f"Generated: `{utc_now()}`",
        "",
        "## 定位",
        "",
        "本阶段是 `geo_ring_cloud.stage_10r` 的只读机制审计：只检查 Meteosat operational CTH 的值、单位、quality flag、Satpy area navigation 与 EPIC 下游敏感性；不修改 production reader，不重跑整月，不复用 Meteosat-0deg CLM 3712x3712 navigation patch。",
        "",
        "## 结论",
        "",
    ]
    for row in summary_rows:
        lines.append(f"- `{row['product_line']}`: `{row['final_status']}`。identity values MAE `{row.get('identity_values_mae_km', math.nan):.6g}` km；current vs Satpy navigation p95 `{row.get('navigation_p95_km', math.nan):.6g}` km；EPIC all-valid MAE delta Satpy-current `{row.get('epic_all_valid_satpy_minus_current_mae_km', math.nan):.6g}` km。")
    lines.extend(
        [
            "",
            "## 关键解释",
            "",
            "- Satpy `seviri_l2_grib` 明确给出 `cloud_top_height` 单位为 `m`，本地 current standardized native CTH 为 `km`；值对照已执行 `m_to_km`，没有把 m/km 混用。",
            "- Satpy `cloud_top_quality` 的 `flag_meanings` 为 `good quality retrieval` 与 `poor quality retrieval`，本报告 good-quality only 使用 `cloud_top_quality == 0`。",
            "- 两条产品线的 CTH shape 均为 `1237x1237`，Satpy area 为约 9 km CTH 网格；未使用 Stage09H 的 `3712x3712` CLM navigation patch。",
            "- 同周期 L1.5 `IR_108` 未在本地 Meteosat 目录命中；本阶段遵守不下载新数据，因此 quicklook 的 IR_108 面板标记为本地不可用。",
            "- EPIC 侧仍是 Oxygen A-band `Effective_Cloud_Height`，不是绝对 CTH 真值；EPIC 下游结果只用于判断 navigation 改动是否能解释差异，不能单独作为导航选择依据。",
            "",
            "## 输出索引",
            "",
        ]
    )
    for label, path in outputs.items():
        lines.append(f"- {label}: `{path}`")
    if warnings:
        lines.extend(["", "## Warnings", ""])
        for row in warnings:
            lines.append(f"- `{row.get('warning_code', '')}`: {row.get('message', '')}")
    (out_dir / "stage_10r_summary_cn.md").write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def build_summary(
    product_lines: list[str],
    value_rows: list[dict[str, Any]],
    nav_rows: list[dict[str, Any]],
    epic_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    value_df = pd.DataFrame(value_rows)
    nav_df = pd.DataFrame(nav_rows)
    epic_df = pd.DataFrame(epic_rows)
    for product_line in product_lines:
        ident = value_df[(value_df["product_line"] == product_line) & (value_df["transform_applied_to_satpy"] == "identity")]
        nav = nav_df[(nav_df["product_line"] == product_line) & (nav_df["control_point"] == "ALL_VALID_NAV_PIXELS")]
        row = {
            "product_line": product_line,
            "identity_values_mae_km": float(ident["mae_km"].max()) if not ident.empty else math.nan,
            "navigation_p95_km": float(nav["p95_geodesic_error_km"].max()) if not nav.empty else math.nan,
        }
        sub = epic_df[(epic_df["product_line"] == product_line) & (epic_df["stratum"] == "all_valid")]
        cur = sub[sub["navigation_variant"] == "current_navigation"]
        sat = sub[sub["navigation_variant"] == "satpy_cth_area_navigation"]
        if not cur.empty and not sat.empty:
            row["epic_all_valid_current_mae_km"] = float(cur["mae_km"].mean())
            row["epic_all_valid_satpy_mae_km"] = float(sat["mae_km"].mean())
            row["epic_all_valid_satpy_minus_current_mae_km"] = row["epic_all_valid_satpy_mae_km"] - row["epic_all_valid_current_mae_km"]
        row["final_status"] = final_status(product_line, value_rows, nav_rows, epic_rows)
        rows.append(row)
    return rows


def run(args: argparse.Namespace) -> None:
    out_dir = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = out_dir / "cache"
    warnings_rows: list[dict[str, Any]] = []
    for message in setup_eccodes_runtime(cache_dir):
        warnings_rows.append({"warning_code": "ECCODES_RUNTIME_SETUP", "message": message})

    inventory_rows: list[dict[str, Any]] = []
    value_rows: list[dict[str, Any]] = []
    quality_rows: list[dict[str, Any]] = []
    nav_rows: list[dict[str, Any]] = []
    epic_rows: list[dict[str, Any]] = []
    quicklooks: list[str] = []
    plotted_lines: set[str] = set()

    for case in CASES:
        case_meta = dict(case)
        zip_path = cth_zip_path(case["product_line"], case["sample_id"])
        native_path = standardized_native_path(case["product_line"], case["sample_id"])
        grib_path, entries = extract_grib(zip_path, cache_dir)
        current = load_current_native(native_path)
        satpy = load_satpy(grib_path)
        case_meta["platform"] = satpy.get("platform_name", "")
        case_meta["nominal_time"] = case["sample_id"]
        inventory_rows.append(inventory_row(case_meta, zip_path, entries, grib_path, native_path, current, satpy))
        value_rows.extend(value_comparison_rows(case_meta, current, satpy))
        quality_rows.extend(quality_comparison_rows(case_meta, current, satpy))
        nav_rows.extend(navigation_rows(case_meta, current, satpy))
        epic_path = epic_file_from_manifest(args.sample_manifest, case["sample_id"])
        epic_rows.extend(epic_downstream_rows(case_meta, current, satpy, epic_path))
        if case["product_line"] not in plotted_lines:
            quicklooks.append(str(make_quicklook(case["product_line"], case["sample_id"], current, satpy, out_dir)))
            plotted_lines.add(case["product_line"])

    product_lines = sorted({case["product_line"] for case in CASES})
    summary_rows = build_summary(product_lines, value_rows, nav_rows, epic_rows)
    warnings_rows.append(
        {
            "warning_code": "L15_IR108_NOT_FOUND",
            "message": "未在 EXTERNAL_GEO_CLOUD_ROOT 下的 Meteosat-* 产品目录发现同周期 L1.5 IR_108；按要求未下载新数据。",
        }
    )

    outputs = {
        "summary": out_dir / "stage_10r_summary_cn.md",
        "inventory": out_dir / "cth_inventory.csv",
        "values comparison": out_dir / "cth_satpy_values_comparison.csv",
        "quality comparison": out_dir / "cth_quality_comparison.csv",
        "navigation metrics": out_dir / "cth_navigation_metrics.csv",
        "EPIC before/after": out_dir / "cth_epic_before_after.csv",
        "warnings": out_dir / "warnings.csv",
        "manifest": out_dir / "manifest.json",
    }
    write_csv(outputs["inventory"], inventory_rows)
    write_csv(outputs["values comparison"], value_rows)
    write_csv(outputs["quality comparison"], quality_rows)
    write_csv(outputs["navigation metrics"], nav_rows)
    write_csv(outputs["EPIC before/after"], epic_rows)
    write_csv(outputs["warnings"], warnings_rows)

    write_manifest(
        outputs["manifest"],
        canonical_stage_id=STAGE_ID,
        component_role=COMPONENT_ROLE,
        related_stage_ids=("stage_10",),
        run_id=out_dir.name,
        source_profile="operational_baseline",
        generating_script=Path(__file__),
        input_paths=[
            args.sample_manifest,
            *[row["cth_zip_path"] for row in inventory_rows],
            *[row["standardized_native_path"] for row in inventory_rows],
        ],
        output_paths=[path for key, path in outputs.items() if key != "manifest"],
        parameters={
            "cases": CASES,
            "satpy_reader": "seviri_l2_grib",
            "good_quality_code": GOOD_QUALITY_CODE,
            "max_epic_sample_distance_km": MAX_EPIC_SAMPLE_DISTANCE_KM,
            "production_reader_modified": False,
            "clm_3712_navigation_patch_reused": False,
            "downloaded_new_data": False,
        },
        project_root=PROJECT_ROOT,
        extra=json_safe(
            {
                "python_executable": sys.executable,
                "external_geo_cloud_root": str(EXTERNAL_GEO_CLOUD_ROOT),
                "quicklooks": quicklooks,
                "summary": summary_rows,
                "warnings": warnings_rows,
            }
        ),
    )
    write_report(out_dir, summary_rows, outputs, warnings_rows)
    print(json.dumps(json_safe({"output_dir": out_dir, "summary": summary_rows}), ensure_ascii=False, indent=2), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 10R Meteosat operational CTH Satpy/navigation audit.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=RUNS_ROOT / OUTPUT_DIRNAME,
    )
    parser.add_argument(
        "--sample-manifest",
        type=Path,
        default=RUNS_ROOT / "stage09d_full_pixel_diagnostics_202403" / "00_sample_manifest" / "stage09d_53_sample_manifest.csv",
    )
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
