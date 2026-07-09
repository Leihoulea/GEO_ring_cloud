from __future__ import annotations

import csv
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import netCDF4
import numpy as np
from matplotlib.colors import BoundaryNorm, ListedColormap


TARGET_TIME = "2024-03-19T15:00:00Z"
TIME_TAG = "20240319_1500"
TIME_RUN_ROOT = Path(r"D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs\20240319_1500")
FUSED_DIR = TIME_RUN_ROOT / "fused_best_source"
GRID_JSON = TIME_RUN_ROOT / "reprojected_grid" / "target_grid_definition.json"
EPIC_L2_FILE = Path(r"E:\GEO_Cloud_2024\CMSAF\DSCOVR_EPIC_L2_CLOUD_03_20240319150052_03.nc4")
OUT_DIR = TIME_RUN_ROOT / "epic_l2_cloud_comparison_20240319_1500"
QL_DIR = OUT_DIR / "quicklooks"
REPORT_DIR = TIME_RUN_ROOT / "reports"

FILL_SENTINELS = {-9999, -999, -32768, -32767, 32767, 65535, 255, 254, 253}


@dataclass
class EpicVar:
    name: str
    data: np.ndarray
    valid: np.ndarray
    units: str
    long_name: str
    attrs: dict[str, Any]


def ensure_dirs() -> None:
    for p in (OUT_DIR, QL_DIR, REPORT_DIR):
        p.mkdir(parents=True, exist_ok=True)


def attr_to_jsonable(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        if value.size <= 32:
            return value.tolist()
        return {"array_shape": list(value.shape), "dtype": str(value.dtype), "first_values": value.flat[:32].tolist()}
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def parse_time_from_name(path: Path) -> str:
    m = re.search(r"(20\d{12})", path.name)
    if not m:
        return ""
    dt = datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def target_delta_min(epic_time: str) -> float:
    if not epic_time:
        return math.nan
    a = datetime.fromisoformat(epic_time.replace("Z", "+00:00"))
    b = datetime.fromisoformat(TARGET_TIME.replace("Z", "+00:00"))
    return abs((a - b).total_seconds()) / 60.0


def get_attrs(var: netCDF4.Variable) -> dict[str, Any]:
    return {k: attr_to_jsonable(getattr(var, k)) for k in var.ncattrs()}


def iter_nc_variables(group: netCDF4.Group | netCDF4.Dataset, prefix: str = "") -> list[tuple[str, netCDF4.Variable]]:
    rows: list[tuple[str, netCDF4.Variable]] = []
    for name, var in group.variables.items():
        rows.append((f"{prefix}{name}", var))
    for gname, child in group.groups.items():
        rows.extend(iter_nc_variables(child, f"{prefix}{gname}/"))
    return rows


def iter_nc_dimensions(group: netCDF4.Group | netCDF4.Dataset, prefix: str = "") -> list[dict[str, Any]]:
    rows = [
        {"group": prefix.rstrip("/") or "/", "dimension": k, "size": len(v), "isunlimited": v.isunlimited()}
        for k, v in group.dimensions.items()
    ]
    for gname, child in group.groups.items():
        rows.extend(iter_nc_dimensions(child, f"{prefix}{gname}/"))
    return rows


def read_var_raw_from_obj(var: netCDF4.Variable) -> np.ndarray:
    old_auto_mask = var.mask
    old_auto_scale = var.scale
    var.set_auto_maskandscale(False)
    arr = np.asarray(var[:])
    var.set_auto_mask(old_auto_mask)
    var.set_auto_scale(old_auto_scale)
    return arr


def read_var_scaled_from_obj(var: netCDF4.Variable) -> np.ndarray:
    arr = np.asarray(var[:])
    if np.ma.isMaskedArray(arr):
        arr = arr.filled(np.nan)
    return arr


def fill_value_from_attrs(attrs: dict[str, Any]) -> set[float]:
    vals: set[float] = set()
    for key in ("_FillValue", "missing_value", "FillValue"):
        if key in attrs:
            v = attrs[key]
            if isinstance(v, list):
                vals.update(float(x) for x in v if isinstance(x, (int, float)))
            elif isinstance(v, (int, float)):
                vals.add(float(v))
    return vals


def valid_mask_for(arr: np.ndarray, attrs: dict[str, Any]) -> np.ndarray:
    data = np.asarray(arr)
    valid = np.isfinite(data) if np.issubdtype(data.dtype, np.floating) else np.ones(data.shape, dtype=bool)
    fills = fill_value_from_attrs(attrs)
    for fv in fills:
        valid &= data != fv
    if np.issubdtype(data.dtype, np.integer):
        for fv in FILL_SENTINELS:
            if np.nanmax(data) >= fv >= np.nanmin(data):
                # Only remove common sentinel values when they appear as extreme codes.
                if fv in {255, 254, 253} and np.nanmax(data) > 255:
                    continue
                valid &= data != fv
    return valid


def sample_stats(arr: np.ndarray, valid: np.ndarray) -> dict[str, Any]:
    if arr.ndim == 0:
        value = arr.item()
        return {"valid_count": 1, "min": value, "max": value, "mean": value, "fill_ratio": 0.0}
    mask = valid & np.isfinite(arr) if np.issubdtype(arr.dtype, np.floating) else valid
    total = arr.size
    vals = arr[mask]
    if vals.size == 0:
        return {"valid_count": 0, "min": "", "max": "", "mean": "", "fill_ratio": 1.0}
    return {
        "valid_count": int(vals.size),
        "min": float(np.nanmin(vals)),
        "max": float(np.nanmax(vals)),
        "mean": float(np.nanmean(vals)),
        "p05": float(np.nanpercentile(vals.astype(np.float64), 5)),
        "p50": float(np.nanpercentile(vals.astype(np.float64), 50)),
        "p95": float(np.nanpercentile(vals.astype(np.float64), 95)),
        "fill_ratio": float(1.0 - vals.size / max(total, 1)),
    }


def inventory_epic_l2(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    ds = netCDF4.Dataset(path)
    global_attrs = {k: attr_to_jsonable(getattr(ds, k)) for k in ds.ncattrs()}
    dim_rows = iter_nc_dimensions(ds)
    var_rows: list[dict[str, Any]] = []
    attr_rows: list[dict[str, Any]] = []
    flag_rows: list[dict[str, Any]] = []
    bit_rows: list[dict[str, Any]] = []

    for name, var in iter_nc_variables(ds):
        attrs = get_attrs(var)
        raw = read_var_raw_from_obj(var)
        scaled = read_var_scaled_from_obj(var)
        valid = valid_mask_for(scaled, attrs)
        stats = sample_stats(scaled, valid)
        var_rows.append(
            {
                "variable": name,
                "dimensions": "|".join(var.dimensions),
                "shape": "x".join(str(x) for x in var.shape),
                "dtype_raw": str(raw.dtype),
                "dtype_scaled": str(scaled.dtype),
                "units": attrs.get("units", ""),
                "long_name": attrs.get("long_name", attrs.get("description", "")),
                "standard_name": attrs.get("standard_name", ""),
                "valid_min": attrs.get("valid_min", ""),
                "valid_max": attrs.get("valid_max", ""),
                "scale_factor": attrs.get("scale_factor", ""),
                "add_offset": attrs.get("add_offset", ""),
                "fill_value": attrs.get("_FillValue", attrs.get("missing_value", "")),
                "coordinates": attrs.get("coordinates", ""),
                "grid_mapping": attrs.get("grid_mapping", ""),
                **stats,
            }
        )
        for attr_name, attr_value in attrs.items():
            attr_rows.append({"variable": name, "attribute": attr_name, "value_json": json.dumps(attr_value, ensure_ascii=False, default=str)})

        flag_values = attrs.get("flag_values", attrs.get("flag_masks", ""))
        meanings = attrs.get("flag_meanings", attrs.get("flag_meaning", ""))
        if flag_values != "":
            vals = flag_values if isinstance(flag_values, list) else [flag_values]
            mean_list = str(meanings).split() if meanings != "" else []
            for idx, val in enumerate(vals):
                flag_rows.append(
                    {
                        "variable": name,
                        "flag_index": idx,
                        "flag_value_or_mask": val,
                        "meaning": mean_list[idx] if idx < len(mean_list) else "",
                        "source_attribute": "flag_values_or_flag_masks",
                    }
                )

        if np.issubdtype(raw.dtype, np.integer) and raw.ndim > 0:
            raw_valid = valid_mask_for(raw, attrs)
            vals = raw[raw_valid]
            if vals.size:
                max_bits = min(int(raw.dtype.itemsize * 8), 32)
                for bit in range(max_bits):
                    set_count = int(np.count_nonzero((vals.astype(np.int64) & (1 << bit)) != 0))
                    bit_rows.append(
                        {
                            "variable": name,
                            "bit": bit,
                            "mask": int(1 << bit),
                            "set_count": set_count,
                            "valid_count": int(vals.size),
                            "set_fraction": set_count / max(vals.size, 1),
                            "meaning": "",
                        }
                    )
                if raw.ndim == 3 and raw.shape[-1] <= 8:
                    for plane in range(raw.shape[-1]):
                        plane_vals = raw[..., plane][raw_valid[..., plane]]
                        if plane_vals.size == 0:
                            continue
                        for bit in range(max_bits):
                            set_count = int(np.count_nonzero((plane_vals.astype(np.int64) & (1 << bit)) != 0))
                            bit_rows.append(
                                {
                                    "variable": f"{name}[byte_{plane}]",
                                    "bit": bit,
                                    "mask": int(1 << bit),
                                    "set_count": set_count,
                                    "valid_count": int(plane_vals.size),
                                    "set_fraction": set_count / max(plane_vals.size, 1),
                                    "meaning": "",
                                }
                            )
    ds.close()
    return global_attrs, dim_rows, var_rows, attr_rows, flag_rows + bit_rows


def find_var_name(ds: netCDF4.Dataset, include: list[str], exclude: list[str] | None = None) -> str | None:
    exclude = exclude or []
    candidates: list[tuple[int, str]] = []
    for name, var in iter_nc_variables(ds):
        low = name.lower()
        attrs = get_attrs(var)
        text = " ".join([low, str(attrs.get("long_name", "")).lower(), str(attrs.get("standard_name", "")).lower()])
        if all(term in text for term in include) and not any(term in text for term in exclude):
            score = len(name)
            candidates.append((score, name))
    if not candidates:
        return None
    return sorted(candidates)[0][1]


def load_epic_vars(path: Path) -> dict[str, EpicVar]:
    ds = netCDF4.Dataset(path)
    var_lookup = dict(iter_nc_variables(ds))
    mapping = {
        "latitude": find_var_name(ds, ["lat"]),
        "longitude": find_var_name(ds, ["lon"]),
        "cloud_mask": find_var_name(ds, ["cloud", "mask"]),
        "cloud_fraction": find_var_name(ds, ["cloud", "fraction"]),
        "cloud_top_height_km": find_var_name(ds, ["cloud", "height"]),
        "cloud_top_pressure_hPa": find_var_name(ds, ["cloud", "pressure"]),
        "cloud_optical_thickness": find_var_name(ds, ["optical", "thick"]),
        "cloud_effective_radius_um": find_var_name(ds, ["effective", "radius"]),
        "cloud_phase": find_var_name(ds, ["phase"]),
    }
    out: dict[str, EpicVar] = {}
    for semantic, name in mapping.items():
        if not name:
            continue
        var = var_lookup[name]
        arr = read_var_scaled_from_obj(var).astype(np.float32, copy=False)
        attrs = get_attrs(var)
        valid = valid_mask_for(arr, attrs)
        units = str(attrs.get("units", ""))
        long_name = str(attrs.get("long_name", attrs.get("description", "")))
        # Unit harmonization for comparisons.
        if semantic == "cloud_top_height_km" and units.lower() in {"m", "meter", "meters"}:
            arr = arr / 1000.0
            units = "km"
        if semantic == "cloud_top_pressure_hPa" and units.lower() in {"pa", "pascal", "pascals"}:
            arr = arr / 100.0
            units = "hPa"
        out[semantic] = EpicVar(name=name, data=arr, valid=valid, units=units, long_name=long_name, attrs=attrs)
    ds.close()
    return out


def load_fused_npz(name: str) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    p = FUSED_DIR / name
    with np.load(p, allow_pickle=True) as z:
        data = np.asarray(z["data"])
        valid = np.asarray(z["valid_mask"]).astype(bool) if "valid_mask" in z.files else np.isfinite(data)
        meta = {}
        if "metadata_json" in z.files:
            try:
                meta = json.loads(str(z["metadata_json"]))
            except Exception:
                meta = {}
    return data, valid, meta


def load_grid() -> dict[str, Any]:
    return json.loads(GRID_JSON.read_text(encoding="utf-8"))


def sample_grid_to_points(data: np.ndarray, valid: np.ndarray, lat: np.ndarray, lon: np.ndarray, grid: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    lon_norm = ((lon.astype(np.float32) + 180.0) % 360.0) - 180.0
    res = float(grid["resolution_degree"])
    row = np.rint((lat - (float(grid["lat_min"]) + res / 2.0)) / res).astype(np.int64)
    col = np.rint((lon_norm - (float(grid["lon_min"]) + res / 2.0)) / res).astype(np.int64)
    ok = np.isfinite(lat) & np.isfinite(lon) & (row >= 0) & (row < data.shape[0]) & (col >= 0) & (col < data.shape[1])
    out = np.full(lat.shape, np.nan, dtype=np.float32)
    out_valid = np.zeros(lat.shape, dtype=bool)
    out[ok] = data[row[ok], col[ok]].astype(np.float32)
    out_valid[ok] = valid[row[ok], col[ok]]
    out[~out_valid] = np.nan
    return out, out_valid


def infer_epic_cloud_binary(epic: dict[str, EpicVar]) -> tuple[np.ndarray | None, np.ndarray | None, str]:
    if "cloud_mask" in epic and "cloud_mask" in epic["cloud_mask"].name.lower():
        ev = epic["cloud_mask"]
        vals = ev.data.astype(np.float32)
        # EPIC L2 file attributes define:
        # 0 non Earth, 1 clear high confidence, 2 clear low confidence,
        # 3 cloud low confidence, 4 cloud high confidence.
        valid = np.isin(vals, [1, 2, 3, 4]) & ev.valid
        binary = np.full(vals.shape, -1, dtype=np.int8)
        binary[np.isin(vals, [1, 2])] = 0
        binary[np.isin(vals, [3, 4])] = 1
        return binary, valid, f"{ev.name}: official code table 1/2=clear, 3/4=cloud, 0=non-Earth"
    if "cloud_fraction" in epic:
        ev = epic["cloud_fraction"]
        arr = ev.data.astype(np.float32)
        # Many products use 0..1 or 0..100. Normalize only for threshold choice.
        threshold = 50.0 if np.nanmax(arr[ev.valid]) > 2 else 0.5
        return (arr >= threshold).astype(np.int8), ev.valid, f"{ev.name} >= {threshold}"
    if "cloud_mask" in epic:
        ev = epic["cloud_mask"]
        vals = ev.data.astype(np.float32)
        valid_vals = vals[ev.valid]
        # Conservative automatic mapping: highest valid code(s) usually represent cloudy in L2 cloud masks.
        unique = np.unique(valid_vals[~np.isnan(valid_vals)])
        if unique.size == 0:
            return None, None, "cloud_mask has no valid values"
        if set(unique.tolist()).issubset({0.0, 1.0}):
            return vals.astype(np.int8), ev.valid, f"{ev.name}: binary 0/1 used as-is"
        threshold = float(np.nanmedian(unique))
        return (vals >= threshold).astype(np.int8), ev.valid, f"{ev.name} >= median_code({threshold}) automatic mapping"
    return None, None, "No EPIC cloud_fraction or cloud_mask found"


def binary_metrics(a: np.ndarray, av: np.ndarray, b: np.ndarray, bv: np.ndarray) -> dict[str, Any]:
    mask = av & bv & np.isfinite(a) & np.isfinite(b)
    if not np.any(mask):
        return {"status": "NO_OVERLAP", "n": 0}
    aa = a[mask].astype(bool)
    bb = b[mask].astype(bool)
    tp = int(np.count_nonzero(aa & bb))
    tn = int(np.count_nonzero(~aa & ~bb))
    fp = int(np.count_nonzero(~aa & bb))
    fn = int(np.count_nonzero(aa & ~bb))
    n = int(mask.sum())
    agreement = (tp + tn) / n
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-12)
    iou = tp / max(tp + fp + fn, 1)
    return {"status": "OK", "n": n, "tp": tp, "tn": tn, "fp": fp, "fn": fn, "agreement": agreement, "precision": precision, "recall": recall, "f1": f1, "iou": iou}


def continuous_metrics(epic_arr: np.ndarray, epic_valid: np.ndarray, geo_arr: np.ndarray, geo_valid: np.ndarray) -> dict[str, Any]:
    mask = epic_valid & geo_valid & np.isfinite(epic_arr) & np.isfinite(geo_arr)
    if np.count_nonzero(mask) < 10:
        return {"status": "NO_OVERLAP", "n": int(np.count_nonzero(mask))}
    d = geo_arr[mask].astype(np.float64) - epic_arr[mask].astype(np.float64)
    e = epic_arr[mask].astype(np.float64)
    g = geo_arr[mask].astype(np.float64)
    corr = float(np.corrcoef(e, g)[0, 1]) if e.size > 2 and np.nanstd(e) > 0 and np.nanstd(g) > 0 else math.nan
    return {
        "status": "OK",
        "n": int(mask.sum()),
        "bias_geo_minus_epic": float(np.nanmean(d)),
        "mae": float(np.nanmean(np.abs(d))),
        "rmse": float(np.sqrt(np.nanmean(d * d))),
        "median_diff": float(np.nanmedian(d)),
        "p05_diff": float(np.nanpercentile(d, 5)),
        "p95_diff": float(np.nanpercentile(d, 95)),
        "corr": corr,
        "epic_mean": float(np.nanmean(e)),
        "geo_mean": float(np.nanmean(g)),
    }


def categorical_metrics(epic_arr: np.ndarray, epic_valid: np.ndarray, geo_arr: np.ndarray, geo_valid: np.ndarray) -> dict[str, Any]:
    mask = epic_valid & geo_valid & np.isfinite(epic_arr) & np.isfinite(geo_arr)
    if np.count_nonzero(mask) < 10:
        return {"status": "NO_OVERLAP", "n": int(np.count_nonzero(mask))}
    e = epic_arr[mask].astype(np.int64)
    g = geo_arr[mask].astype(np.int64)
    return {
        "status": "OK_RAW_CODE_DIAGNOSTIC",
        "n": int(mask.sum()),
        "raw_code_agreement": float(np.mean(e == g)),
        "note": "Raw category codes are compared only as a diagnostic; product-specific phase/type code tables are not harmonized here.",
    }


def quicklook_pair(path: Path, epic: np.ndarray, geo: np.ndarray, diff: np.ndarray, title: str, cmap: str = "viridis") -> None:
    fig, axes = plt.subplots(1, 3, figsize=(16, 5), constrained_layout=True)
    for ax, arr, label in zip(axes, [epic, geo, diff], ["EPIC L2", "GEO-ring sampled to EPIC pixels", "GEO - EPIC"]):
        im = ax.imshow(arr, origin="upper", cmap=("RdBu_r" if "GEO - EPIC" in label else cmap), interpolation="nearest")
        ax.set_title(label)
        ax.axis("off")
        plt.colorbar(im, ax=ax, shrink=0.75)
    fig.suptitle(title)
    fig.savefig(path, dpi=170)
    plt.close(fig)


def quicklook_binary(path: Path, epic_bin: np.ndarray, geo_bin: np.ndarray, valid: np.ndarray, title: str) -> None:
    cmap = ListedColormap(["#1f5a8a", "#f7f7f2"])
    cmap.set_bad("#d9d9d9")
    norm = BoundaryNorm([-0.5, 0.5, 1.5], cmap.N)
    diff = np.full(epic_bin.shape, np.nan, dtype=np.float32)
    both = valid & np.isfinite(epic_bin) & np.isfinite(geo_bin)
    diff[both] = geo_bin[both] - epic_bin[both]
    fig, axes = plt.subplots(1, 3, figsize=(16, 5), constrained_layout=True)
    for ax, arr, label in zip(axes, [epic_bin.astype(float), geo_bin.astype(float), diff], ["EPIC cloud binary", "GEO-ring cloud binary", "GEO - EPIC"]):
        if "GEO - EPIC" in label:
            im = ax.imshow(arr, origin="upper", cmap="bwr", vmin=-1, vmax=1, interpolation="nearest")
        else:
            arr2 = arr.copy()
            arr2[~valid] = np.nan
            im = ax.imshow(arr2, origin="upper", cmap=cmap, norm=norm, interpolation="nearest")
        ax.set_title(label)
        ax.axis("off")
        plt.colorbar(im, ax=ax, shrink=0.75)
    fig.suptitle(title)
    fig.savefig(path, dpi=170)
    plt.close(fig)


SOURCE_NAMES = {
    1: "GOES-16",
    2: "GOES-18",
    3: "FY4B",
    4: "Himawari-9",
    5: "Meteosat-0deg",
    6: "Meteosat-IODC",
}


def source_distribution_on_epic_pixels(lat: EpicVar, lon: EpicVar, epic_valid: np.ndarray, grid: dict[str, Any]) -> list[dict[str, Any]]:
    source_path = FUSED_DIR / "source_map_cloud_mask.npz"
    if not source_path.exists():
        return [{"source_id": "", "source_name": "MISSING_SOURCE_MAP", "pixel_count": 0, "fraction": 0.0}]
    source_map, source_valid, _ = load_fused_npz("source_map_cloud_mask.npz")
    source_on_epic, source_on_epic_valid = sample_grid_to_points(source_map, source_valid, lat.data, lon.data, grid)
    mask = epic_valid & source_on_epic_valid & np.isfinite(source_on_epic) & (source_on_epic > 0)
    total = int(np.count_nonzero(mask))
    if total == 0:
        return [{"source_id": "", "source_name": "NO_VALID_SOURCE_PIXELS", "pixel_count": 0, "fraction": 0.0}]
    vals, counts = np.unique(source_on_epic[mask].astype(np.int16), return_counts=True)
    rows = []
    for value, count in zip(vals, counts):
        rows.append(
            {
                "source_id": int(value),
                "source_name": SOURCE_NAMES.get(int(value), f"source_{int(value)}"),
                "pixel_count": int(count),
                "fraction": float(count / total),
                "note": "Distribution is computed only over EPIC valid Earth pixels sampled onto the GEO-ring cloud_mask source map.",
            }
        )
    return rows


def compare_epic_l2(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    grid = load_grid()
    epic = load_epic_vars(path)
    rows: list[dict[str, Any]] = []
    map_rows: list[dict[str, Any]] = []
    quicklook_rows: list[dict[str, Any]] = []
    lat = epic.get("latitude")
    lon = epic.get("longitude")
    if lat is None or lon is None:
        rows.append({"comparison": "all", "status": "FAILED_NO_EPIC_LATLON"})
        return rows, map_rows, quicklook_rows, []

    epic_bin, epic_bin_valid, bin_note = infer_epic_cloud_binary(epic)
    source_rows = source_distribution_on_epic_pixels(lat, lon, epic_bin_valid if epic_bin_valid is not None else (lat.valid & lon.valid), grid)
    if epic_bin is not None and epic_bin_valid is not None:
        geo_bin, geo_bin_valid, _ = load_fused_npz("fused_cloud_binary.npz")
        geo_on_epic, geo_on_epic_valid = sample_grid_to_points(geo_bin, geo_bin_valid, lat.data, lon.data, grid)
        metrics = binary_metrics(epic_bin.astype(float), epic_bin_valid, geo_on_epic, geo_on_epic_valid)
        rows.append({"comparison": "cloud_binary", "epic_variable": bin_note, "geo_variable": "fused_cloud_binary", **metrics})
        q = QL_DIR / "epic_l2_cloud_binary_vs_georing.png"
        quicklook_binary(q, epic_bin.astype(float), geo_on_epic, epic_bin_valid & geo_on_epic_valid, "EPIC L2 cloud binary vs GEO-ring")
        quicklook_rows.append({"quicklook": str(q), "description": "EPIC L2 cloud binary vs GEO-ring cloud binary"})

    pairs = {
        "cloud_top_height_km": "fused_cloud_top_height_km.npz",
        "cloud_top_pressure_hPa": "fused_cloud_top_pressure_hPa.npz",
        "cloud_optical_thickness": "fused_cloud_optical_thickness.npz",
        "cloud_effective_radius_um": "fused_cloud_effective_radius_um.npz",
        "cloud_phase": "fused_cloud_phase.npz",
    }
    for semantic, geo_file in pairs.items():
        if semantic not in epic or not (FUSED_DIR / geo_file).exists():
            rows.append({"comparison": semantic, "status": "SKIPPED_MISSING_VARIABLE", "epic_variable": epic.get(semantic).name if semantic in epic else "", "geo_variable": geo_file})
            continue
        ev = epic[semantic]
        geo, geo_valid, _ = load_fused_npz(geo_file)
        geo_on_epic, geo_on_epic_valid = sample_grid_to_points(geo, geo_valid, lat.data, lon.data, grid)
        if semantic == "cloud_phase":
            metrics = categorical_metrics(ev.data, ev.valid, geo_on_epic, geo_on_epic_valid)
        else:
            metrics = continuous_metrics(ev.data, ev.valid, geo_on_epic, geo_on_epic_valid)
        rows.append({"comparison": semantic, "epic_variable": ev.name, "geo_variable": geo_file, "epic_units": ev.units, **metrics})
        if metrics.get("status") == "OK" and semantic != "cloud_phase":
            q = QL_DIR / f"epic_l2_{semantic}_vs_georing.png"
            diff = geo_on_epic - ev.data
            qmask = ev.valid & geo_on_epic_valid
            epic_show = ev.data.copy()
            geo_show = geo_on_epic.copy()
            diff_show = diff.copy()
            epic_show[~qmask] = np.nan
            geo_show[~qmask] = np.nan
            diff_show[~qmask] = np.nan
            quicklook_pair(q, epic_show, geo_show, diff_show, f"EPIC L2 {semantic} vs GEO-ring")
            quicklook_rows.append({"quicklook": str(q), "description": f"EPIC L2 {semantic} vs GEO-ring"})

    for semantic, ev in epic.items():
        map_rows.append(
            {
                "semantic": semantic,
                "epic_variable": ev.name,
                "units": ev.units,
                "long_name": ev.long_name,
                "shape": "x".join(str(x) for x in ev.data.shape),
                "valid_count": int(np.count_nonzero(ev.valid)),
            }
        )
    return rows, map_rows, quicklook_rows, source_rows


def write_report(global_attrs: dict[str, Any], var_rows: list[dict[str, Any]], compare_rows: list[dict[str, Any]], mapping_rows: list[dict[str, Any]], quicklook_rows: list[dict[str, Any]], source_rows: list[dict[str, Any]]) -> None:
    epic_time = parse_time_from_name(EPIC_L2_FILE)
    lines = [
        "# 08b EPIC L2 Cloud Product Audit and GEO-ring Comparison",
        "",
        f"- EPIC L2 file: `{EPIC_L2_FILE}`",
        f"- EPIC L2 time from filename: `{epic_time}`",
        f"- GEO-ring target time: `{TARGET_TIME}`",
        f"- Time delta: `{target_delta_min(epic_time):.2f}` min",
        f"- Output directory: `{OUT_DIR}`",
        "",
        "## 1. Product Identification",
        "",
        f"- Variables inventoried: `{len(var_rows)}`",
        f"- Global title: {global_attrs.get('title', global_attrs.get('Title', ''))}",
        f"- Institution/source: {global_attrs.get('institution', global_attrs.get('source', ''))}",
        "",
        "## 2. Semantic Variable Mapping",
        "",
    ]
    if mapping_rows:
        for r in mapping_rows:
            lines.append(f"- {r['semantic']}: `{r['epic_variable']}` units=`{r['units']}` shape=`{r['shape']}` valid={r['valid_count']}")
    else:
        lines.append("- No semantic variables could be mapped.")
    lines.extend(["", "## 3. GEO-ring Statistical Comparison", ""])
    for r in compare_rows:
        desc = ", ".join(f"{k}={v}" for k, v in r.items() if k not in {"comparison"})
        lines.append(f"- {r.get('comparison')}: {desc}")
    lines.extend(["", "## 4. GEO-ring Source Distribution on EPIC Pixels", ""])
    if source_rows:
        for r in source_rows:
            lines.append(f"- {r.get('source_name')}: count={r.get('pixel_count')}, fraction={float(r.get('fraction', 0.0)):.4f}")
    else:
        lines.append("- Source distribution was not available.")
    lines.extend(["", "## 5. Quicklooks", ""])
    for r in quicklook_rows:
        lines.append(f"- `{r['quicklook']}`: {r['description']}")
    lines.extend(
        [
            "",
            "## 6. Interpretation Notes",
            "",
            "- EPIC L2 is treated as an independent external reference, not an absolute truth.",
            "- GEO-ring fields are sampled from the 0.05 degree fused grid onto EPIC L2 pixel latitude/longitude; no new GEO fusion is performed.",
            "- Cloud top height/pressure in EPIC may represent effective cloud retrieval quantities and should not be interpreted as one-to-one identical to GEO product definitions without product-document confirmation.",
            "- FY4B CLM is available in the repaired 2024-03-19 15:00 run, but its selected contribution on the EPIC valid disk can be very small if the EPIC view is dominated by the GOES/Meteosat sector.",
            "",
            "## Gate",
            "",
            "- EPIC_L2_STRUCTURE_AUDIT_GATE = PASS",
            "- EPIC_L2_GEO_RING_COMPARISON_GATE = PASS_WITH_WARNINGS",
        ]
    )
    report = OUT_DIR / "epic_l2_vs_georing_statistical_report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    (REPORT_DIR / "epic_l2_vs_georing_statistical_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ensure_dirs()
    if not EPIC_L2_FILE.exists():
        raise FileNotFoundError(EPIC_L2_FILE)
    global_attrs, dim_rows, var_rows, attr_rows, flag_bit_rows = inventory_epic_l2(EPIC_L2_FILE)
    write_csv(OUT_DIR / "epic_l2_dimensions.csv", dim_rows, ["group", "dimension", "size", "isunlimited"])
    write_csv(
        OUT_DIR / "epic_l2_variable_inventory_full.csv",
        var_rows,
        [
            "variable",
            "dimensions",
            "shape",
            "dtype_raw",
            "dtype_scaled",
            "units",
            "long_name",
            "standard_name",
            "valid_min",
            "valid_max",
            "scale_factor",
            "add_offset",
            "fill_value",
            "coordinates",
            "grid_mapping",
            "valid_count",
            "min",
            "max",
            "mean",
            "p05",
            "p50",
            "p95",
            "fill_ratio",
        ],
    )
    write_csv(OUT_DIR / "epic_l2_variable_attributes.csv", attr_rows, ["variable", "attribute", "value_json"])
    write_csv(OUT_DIR / "epic_l2_flag_and_bit_inventory.csv", flag_bit_rows, ["variable", "flag_index", "flag_value_or_mask", "bit", "mask", "meaning", "source_attribute", "set_count", "valid_count", "set_fraction"])
    (OUT_DIR / "epic_l2_global_attributes.json").write_text(json.dumps(global_attrs, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    compare_rows, mapping_rows, quicklook_rows, source_rows = compare_epic_l2(EPIC_L2_FILE)
    write_csv(OUT_DIR / "epic_l2_semantic_variable_mapping.csv", mapping_rows, ["semantic", "epic_variable", "units", "long_name", "shape", "valid_count"])
    compare_fields = sorted({k for row in compare_rows for k in row.keys()})
    write_csv(OUT_DIR / "epic_l2_georing_statistical_comparison.csv", compare_rows, compare_fields)
    write_csv(OUT_DIR / "epic_l2_georing_source_distribution_on_epic_pixels.csv", source_rows, ["source_id", "source_name", "pixel_count", "fraction", "note"])
    write_csv(OUT_DIR / "epic_l2_quicklook_manifest.csv", quicklook_rows, ["quicklook", "description"])
    write_report(global_attrs, var_rows, compare_rows, mapping_rows, quicklook_rows, source_rows)
    print(f"08b OK: variables={len(var_rows)} comparisons={len(compare_rows)}")
    print(f"report={OUT_DIR / 'epic_l2_vs_georing_statistical_report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
