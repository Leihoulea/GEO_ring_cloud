from __future__ import annotations

import csv
import json
import math
import re
import tempfile
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import h5py
import matplotlib
import netCDF4
import numpy as np
import pandas as pd
import yaml

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, ListedColormap


BASE_DIR = Path(r"D:\AAAresearch_paper\data_check_report")
SELECTION_CSV = BASE_DIR / "one_sample_each_product_selection.csv"
MAPPING_YAML = BASE_DIR / "manual_variable_mapping_by_product.yaml"
OUT_DIR = BASE_DIR / "standardized_cloud_v0_samples"
REPORT_MD = BASE_DIR / "standardized_cloud_v0_report.md"

STANDARD_VARS = [
    "cloud_mask",
    "cloud_mask_acm",
    "cloud_mask_bcm",
    "cloud_probability",
    "cloud_top_height",
    "cloud_top_temperature",
    "cloud_top_pressure",
    "cloud_optical_thickness",
    "quality_flag",
    "latitude",
    "longitude",
    "projection_x",
    "projection_y",
    "geostationary_projection",
    "observation_time",
]

QUICKLOOK_PRIORITY = [
    "cloud_mask",
    "cloud_top_height",
    "cloud_top_temperature",
    "cloud_top_pressure",
    "cloud_probability",
    "cloud_optical_thickness",
]

MAX_PLOT_PIXELS = 1_200_000


def log(message: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


def read_mapping() -> dict[str, dict[str, list[str]]]:
    mapping = yaml.safe_load(MAPPING_YAML.read_text(encoding="utf-8-sig"))
    # Hard fix for the GOES ambiguity: y is projection_y only. The resolver below
    # also prevents one-character variables from matching inside words like quality.
    for key in ["GOES_ACMF", "GOES_ACHAF"]:
        mapping.setdefault(key, {})
        mapping[key]["projection_y"] = ["y"]
        mapping[key]["projection_x"] = ["x"]
        mapping[key]["quality_flag"] = ["DQF", "quality_flag"]
    return mapping


def product_key(row: dict[str, Any]) -> str:
    family = str(row.get("satellite_family", ""))
    product = str(row.get("product", ""))
    return f"{family}_{product}"


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def resolve_variables(names: list[str], product_map: dict[str, list[str]]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    name_norm = {name: normalize(name) for name in names}
    for std, candidates in product_map.items():
        for candidate in candidates or []:
            cand = normalize(str(candidate))
            if not cand or "tobeconfirmed" in cand or "ifpresent" in cand:
                continue
            for name, norm in name_norm.items():
                if name in resolved:
                    continue
                if len(cand) <= 2 or len(norm) <= 2:
                    matched = cand == norm
                else:
                    matched = cand == norm or cand in norm or norm in cand
                if matched:
                    resolved[name] = std
    return resolved


def special_std(row: dict[str, Any], name: str) -> str | None:
    family = str(row.get("satellite_family", ""))
    product = str(row.get("product", ""))
    if family == "GOES" and product == "ACMF":
        return {
            "BCM": "cloud_mask",
            "ACM": "cloud_mask_acm",
            "Cloud_Probabilities": "cloud_probability",
            "DQF": "quality_flag",
            "x": "projection_x",
            "y": "projection_y",
            "t": "observation_time",
            "time_bounds": "observation_time",
            "goes_imager_projection": "geostationary_projection",
        }.get(name)
    if family == "GOES" and product == "ACHAF":
        return {
            "HT": "cloud_top_height",
            "DQF": "quality_flag",
            "x": "projection_x",
            "y": "projection_y",
            "t": "observation_time",
            "time_bounds": "observation_time",
            "goes_imager_projection": "geostationary_projection",
        }.get(name)
    return None


def should_replace(existing: np.ndarray | None, new: np.ndarray, existing_name: str, new_name: str) -> bool:
    if existing is None:
        return True
    existing_shape = tuple(np.asarray(existing).shape)
    new_shape = tuple(np.asarray(new).shape)
    if len(new_shape) > len(existing_shape):
        return True
    if len(new_shape) < len(existing_shape):
        return False
    if np.asarray(new).size > np.asarray(existing).size:
        return True
    reject_tokens = ("total_number", "minimum_", "maximum_", "mean_", "std_dev_", "granule_level")
    if any(token in existing_name for token in reject_tokens) and not any(token in new_name for token in reject_tokens):
        return True
    return False


def attr_to_python(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    return value


def attrs_to_dict(attrs: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if hasattr(attrs, "items"):
        iterator = attrs.items()
    else:
        iterator = []
    for key, value in iterator:
        try:
            out[str(key)] = attr_to_python(value)
        except Exception:
            out[str(key)] = str(value)
    return out


def nc_attrs(var: Any) -> dict[str, Any]:
    return {name: attr_to_python(getattr(var, name)) for name in var.ncattrs()}


def apply_hdf_scale(arr: np.ndarray, attrs: dict[str, Any]) -> np.ndarray:
    out = np.asarray(arr)
    scale = first_attr(attrs, ["scale_factor", "Slope", "slope"])
    offset = first_attr(attrs, ["add_offset", "Intercept", "intercept"])
    if scale is not None or offset is not None:
        out = out.astype("float32", copy=False)
        if scale is not None:
            out = out * float(np.asarray(scale).ravel()[0])
        if offset is not None:
            out = out + float(np.asarray(offset).ravel()[0])
    return out


def first_attr(attrs: dict[str, Any], names: list[str]) -> Any | None:
    lowered = {str(k).lower(): v for k, v in attrs.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def sanitize_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value)[:180]


def choose_selection_rows(selection: pd.DataFrame) -> list[dict[str, Any]]:
    wanted = {
        ("FY4B", "FY4B", "CLM"),
        ("FY4B", "FY4B", "CTH"),
        ("FY4B", "FY4B", "CTT"),
        ("FY4B", "FY4B", "CLT"),
        ("GOES", "GOES-16", "ACMF"),
        ("GOES", "GOES-16", "ACHAF"),
        ("GOES", "GOES-18", "ACMF"),
        ("GOES", "GOES-18", "ACHAF"),
        ("Himawari", "Himawari-9", "CMSK"),
        ("Himawari", "Himawari-9", "CHGT"),
        ("Meteosat", "Meteosat-0deg", "CLM"),
        ("Meteosat", "Meteosat-0deg", "CTH"),
        ("Meteosat", "Meteosat-IODC", "CLM"),
        ("Meteosat", "Meteosat-IODC", "CTH"),
    }
    rows = []
    for row in selection.to_dict("records"):
        key = (str(row.get("satellite_family")), str(row.get("satellite")), str(row.get("product")))
        path = Path(str(row.get("file_path", "")))
        if key in wanted and path.exists():
            rows.append(row)
    return rows


def read_sample(row: dict[str, Any], mapping: dict[str, dict[str, list[str]]]) -> dict[str, Any]:
    path = Path(str(row["file_path"]))
    family = str(row["satellite_family"])
    suffix = "".join(path.suffixes).lower()
    if family == "Meteosat" and ".zip" in suffix:
        return read_meteosat_zip(row, mapping)
    if ".nc" in suffix:
        return read_netcdf(row, mapping)
    if any(ext in suffix for ext in [".hdf", ".h5"]):
        return read_hdf(row, mapping)
    raise ValueError(f"Unsupported sample file: {path}")


def read_netcdf(row: dict[str, Any], mapping: dict[str, dict[str, list[str]]]) -> dict[str, Any]:
    path = Path(str(row["file_path"]))
    key = product_key(row)
    arrays: dict[str, np.ndarray] = {}
    source_names: dict[str, str] = {}
    attrs_by_std: dict[str, dict[str, Any]] = {}
    all_shapes: dict[str, tuple[int, ...]] = {}
    global_attrs: dict[str, Any] = {}
    with netCDF4.Dataset(path, "r") as ds:
        try:
            ds.set_auto_mask(False)
        except Exception:
            pass
        names = list(ds.variables)
        resolved = resolve_variables(names, mapping.get(key, {}))
        global_attrs = {name: attr_to_python(getattr(ds, name)) for name in ds.ncattrs()}
        for name in names:
            var = ds.variables[name]
            all_shapes[name] = tuple(getattr(var, "shape", ()))
            std = special_std(row, name) or resolved.get(name)
            if std not in STANDARD_VARS:
                continue
            arr = np.asarray(var[:])
            arr = normalize_masked(arr)
            if should_replace(arrays.get(std), arr, source_names.get(std, ""), name):
                arrays[std] = arr
                source_names[std] = name
                attrs_by_std[std] = nc_attrs(var)
    return build_product_result(row, "netcdf", arrays, source_names, attrs_by_std, global_attrs, all_shapes)


def read_hdf(row: dict[str, Any], mapping: dict[str, dict[str, list[str]]]) -> dict[str, Any]:
    path = Path(str(row["file_path"]))
    key = product_key(row)
    arrays: dict[str, np.ndarray] = {}
    source_names: dict[str, str] = {}
    attrs_by_std: dict[str, dict[str, Any]] = {}
    all_shapes: dict[str, tuple[int, ...]] = {}
    global_attrs: dict[str, Any] = {}
    with h5py.File(path, "r") as h5:
        global_attrs = attrs_to_dict(h5.attrs)
        datasets: dict[str, Any] = {}

        def visitor(name: str, obj: Any) -> None:
            if hasattr(obj, "shape") and hasattr(obj, "dtype"):
                datasets[name] = obj

        h5.visititems(visitor)
        resolved = resolve_variables(list(datasets), mapping.get(key, {}))
        for name, obj in datasets.items():
            all_shapes[name] = tuple(obj.shape)
            std = special_std(row, name) or resolved.get(name)
            if std not in STANDARD_VARS:
                continue
            attrs = attrs_to_dict(obj.attrs)
            arr = apply_hdf_scale(np.asarray(obj[()]), attrs)
            arr = normalize_masked(arr)
            if should_replace(arrays.get(std), arr, source_names.get(std, ""), name):
                arrays[std] = arr
                source_names[std] = name
                attrs_by_std[std] = attrs
    return build_product_result(row, "hdf5", arrays, source_names, attrs_by_std, global_attrs, all_shapes)


def read_meteosat_zip(row: dict[str, Any], mapping: dict[str, dict[str, list[str]]]) -> dict[str, Any]:
    import cfgrib

    path = Path(str(row["file_path"]))
    arrays: dict[str, np.ndarray] = {}
    source_names: dict[str, str] = {}
    attrs_by_std: dict[str, dict[str, Any]] = {}
    all_shapes: dict[str, tuple[int, ...]] = {}
    projection: dict[str, Any] = {"zip_entries": []}
    with zipfile.ZipFile(path) as zf:
        projection["zip_entries"] = zf.namelist()
        grib_names = [n for n in zf.namelist() if n.lower().endswith((".grb", ".grib", ".grib2"))]
        for member in grib_names:
            data = zf.read(member)
            records = parse_grib_messages(row, member, data)
            if records:
                projection.update({f"grib_{k}": v for k, v in records[0].items() if k not in {"zip_file"}})
            with tempfile.NamedTemporaryFile(suffix=".grib", delete=False) as tmp:
                tmp.write(data)
                tmp_path = Path(tmp.name)
            try:
                dss = cfgrib.open_datasets(str(tmp_path), indexpath="")
                for ds_index, ds in enumerate(dss):
                    try:
                        grid_shape = grib_grid_shape(records, ds)
                        for name in ds.data_vars:
                            da = ds[name]
                            attrs = {str(k): attr_to_python(v) for k, v in da.attrs.items()}
                            std = infer_meteosat_std(row, name, attrs, mapping)
                            arr = reshape_grib_array(np.asarray(da.values), grid_shape)
                            all_shapes[name] = tuple(arr.shape)
                            if std not in STANDARD_VARS:
                                continue
                            arrays[std] = normalize_masked(arr)
                            source_names[std] = name
                            attrs["cfgrib_dataset_index"] = ds_index
                            attrs["grib_member"] = member
                            attrs_by_std[std] = attrs
                    finally:
                        ds.close()
            finally:
                tmp_path.unlink(missing_ok=True)
    return build_product_result(row, "zip_cfgrib", arrays, source_names, attrs_by_std, projection, all_shapes)


def normalize_masked(arr: np.ndarray) -> np.ndarray:
    if np.ma.isMaskedArray(arr):
        arr = arr.filled(np.nan if np.issubdtype(arr.dtype, np.floating) else 0)
    return np.asarray(arr)


def build_product_result(
    row: dict[str, Any],
    reader: str,
    arrays: dict[str, np.ndarray],
    source_names: dict[str, str],
    attrs_by_std: dict[str, dict[str, Any]],
    global_attrs: dict[str, Any],
    all_shapes: dict[str, tuple[int, ...]],
) -> dict[str, Any]:
    meta = {
        "satellite_family": row.get("satellite_family", ""),
        "satellite": row.get("satellite", ""),
        "product": row.get("product", ""),
        "file_path": row.get("file_path", ""),
        "nominal_time": row.get("nominal_time", ""),
        "reader": reader,
        "source_names": source_names,
        "variable_attrs": attrs_by_std,
        "global_attrs": global_attrs,
        "all_variable_shapes": {k: list(v) for k, v in all_shapes.items()},
    }
    add_time_metadata(meta)
    return {"row": row, "reader": reader, "arrays": arrays, "meta": meta, "all_shapes": all_shapes}


def add_time_metadata(meta: dict[str, Any]) -> None:
    attrs = meta.get("global_attrs", {})
    for out_name, candidates in {
        "start_time": ["time_coverage_start", "start_time", "date_created", "GRIB_valid_time"],
        "end_time": ["time_coverage_end", "end_time"],
        "nominal_time": ["nominal_time", "time_coverage_start"],
    }.items():
        if meta.get(out_name):
            continue
        for key in candidates:
            if key in attrs and attrs[key] not in {"", None}:
                meta[out_name] = attrs[key]
                break


def infer_meteosat_std(row: dict[str, Any], var_name: str, attrs: dict[str, Any], mapping: dict[str, dict[str, list[str]]]) -> str:
    text = normalize(" ".join([var_name, str(attrs.get("GRIB_shortName", "")), str(attrs.get("long_name", "")), str(attrs.get("GRIB_name", ""))]))
    product = str(row.get("product", "")).upper()
    if "quality" in text or normalize(var_name).endswith("qi"):
        return "quality_flag"
    if product == "CLM" and (var_name == "p260537" or "cloudmask" in text):
        return "cloud_mask"
    if product == "CTH":
        if normalize(var_name) == "ctoph" or "cloudtopheight" in text:
            return "cloud_top_height"
        if normalize(var_name) in {"ctt", "cttoph"} or "cloudtoptemperature" in text:
            return "cloud_top_temperature"
        if normalize(var_name) in {"ctp", "ctopp"} or "cloudtoppressure" in text:
            return "cloud_top_pressure"
    resolved = resolve_variables([var_name], mapping.get(product_key(row), {}))
    return resolved.get(var_name, "unknown")


def parse_grib_messages(row: dict[str, Any], member_name: str, data: bytes) -> list[dict[str, Any]]:
    rows = []
    pos = 0
    index = 0
    while True:
        idx = data.find(b"GRIB", pos)
        if idx < 0 or idx + 16 > len(data):
            break
        edition = data[idx + 7]
        total_length = int.from_bytes(data[idx + 8 : idx + 16], "big", signed=False) if edition == 2 else 0
        if total_length <= 0 or idx + total_length > len(data):
            total_length = len(data) - idx
        payload = data[idx : idx + total_length]
        index += 1
        rec = {
            "satellite_family": row.get("satellite_family", ""),
            "satellite": row.get("satellite", ""),
            "product": row.get("product", ""),
            "grib_member": member_name,
            "message_index": index,
            "edition": edition,
            "discipline": data[idx + 6],
            "total_length": total_length,
            "grid_template": "",
            "nx": "",
            "ny": "",
            "grid_metadata_note": "",
        }
        if edition == 2:
            rec.update(parse_grib2_sections(payload))
        rows.append(rec)
        pos = idx + total_length
    return rows


def parse_grib2_sections(payload: bytes) -> dict[str, Any]:
    pos = 16
    out: dict[str, Any] = {"sections": ""}
    sections = []
    while pos + 5 <= len(payload):
        if payload[pos : pos + 4] == b"7777":
            break
        sec_len = int.from_bytes(payload[pos : pos + 4], "big", signed=False)
        sec_no = payload[pos + 4]
        if sec_len < 5 or pos + sec_len > len(payload):
            break
        sec = payload[pos : pos + sec_len]
        sections.append(str(sec_no))
        if sec_no == 3 and sec_len >= 20:
            out.update(parse_grib2_grid_section(sec))
        elif sec_no == 4 and sec_len >= 11:
            out["product_template"] = int.from_bytes(sec[7:9], "big", signed=False)
            if sec_len >= 13:
                out["parameter_category"] = sec[9]
                out["parameter_number"] = sec[10]
        pos += sec_len
    out["sections"] = ",".join(sections)
    return out


def parse_grib2_grid_section(sec: bytes) -> dict[str, Any]:
    out: dict[str, Any] = {}
    out["number_of_data_points"] = int.from_bytes(sec[6:10], "big", signed=False)
    out["grid_template"] = int.from_bytes(sec[12:14], "big", signed=False)
    body = sec[14:]
    if out["grid_template"] == 90:
        values = [int.from_bytes(body[i : i + 4], "big", signed=False) for i in range(0, min(len(body) - 3, 64), 4)]
        candidates = [v for v in values if 100 <= v <= 20000]
        if len(candidates) >= 2:
            out["nx"] = candidates[0]
            out["ny"] = candidates[1]
        out["grid_metadata_note"] = f"GRIB2 template 3.90 geostationary; raw_4byte_values={values[:12]}"
    return out


def grib_grid_shape(records: list[dict[str, Any]], ds: Any) -> tuple[int, int] | None:
    for rec in records:
        try:
            nx = int(rec.get("nx") or 0)
            ny = int(rec.get("ny") or 0)
            if nx > 0 and ny > 0:
                return ny, nx
        except Exception:
            pass
    sizes = dict(getattr(ds, "sizes", {}))
    if sizes.get("values"):
        root = int(math.sqrt(int(sizes["values"])))
        if root * root == int(sizes["values"]):
            return root, root
    return None


def reshape_grib_array(arr: np.ndarray, grid_shape: tuple[int, int] | None) -> np.ndarray:
    arr = np.asarray(arr)
    if grid_shape and arr.ndim == 1 and arr.size == grid_shape[0] * grid_shape[1]:
        return arr.reshape(grid_shape)
    return arr


def numeric_stats(arr: np.ndarray) -> dict[str, Any]:
    arr = np.asarray(arr)
    row: dict[str, Any] = {
        "shape": str(tuple(arr.shape)),
        "dtype": str(arr.dtype),
        "count": int(arr.size),
        "finite_ratio": "",
        "min": "",
        "max": "",
        "mean": "",
        "p01": "",
        "p50": "",
        "p99": "",
        "unique_count_sample": "",
        "unique_values_sample": "",
    }
    if arr.size == 0 or not np.issubdtype(arr.dtype, np.number):
        return row
    sample = downsample_for_stats(arr)
    values = sample.astype("float64", copy=False).ravel()
    finite = np.isfinite(values)
    row["finite_ratio"] = round(float(finite.mean()), 6) if values.size else ""
    values = values[finite]
    if values.size:
        row.update(
            {
                "min": float(np.nanmin(values)),
                "max": float(np.nanmax(values)),
                "mean": float(np.nanmean(values)),
                "p01": float(np.nanpercentile(values, 1)),
                "p50": float(np.nanpercentile(values, 50)),
                "p99": float(np.nanpercentile(values, 99)),
            }
        )
        uniques = np.unique(values[: min(values.size, 300_000)])
        row["unique_count_sample"] = int(uniques.size)
        row["unique_values_sample"] = ",".join(str(float(x)) for x in uniques[:20])
    return row


def downsample_for_stats(arr: np.ndarray, max_values: int = 500_000) -> np.ndarray:
    arr = np.asarray(arr)
    if arr.size <= max_values or arr.ndim == 0:
        return arr
    factor = (arr.size / max_values) ** (1 / arr.ndim)
    slices = tuple(slice(None, None, max(1, int(math.ceil(factor)))) for _ in arr.shape)
    return arr[slices]


def downsample_for_plot(arr: np.ndarray) -> np.ndarray:
    arr = np.asarray(arr)
    while arr.ndim > 2:
        arr = arr[0]
    if arr.ndim != 2:
        return arr
    if arr.size <= MAX_PLOT_PIXELS:
        return arr
    factor = int(math.ceil(math.sqrt(arr.size / MAX_PLOT_PIXELS)))
    return arr[::factor, ::factor]


def make_quicklook(result: dict[str, Any], out_path: Path) -> str | None:
    arrays = result["arrays"]
    chosen = None
    for std in QUICKLOOK_PRIORITY:
        if std in arrays and np.asarray(arrays[std]).ndim >= 2:
            chosen = std
            break
    if not chosen:
        return None
    arr = downsample_for_plot(np.asarray(arrays[chosen]))
    if arr.ndim != 2 or arr.size == 0:
        return None
    plot_arr = arr.astype("float64", copy=True) if np.issubdtype(arr.dtype, np.number) else arr
    if chosen in {"cloud_mask", "cloud_mask_acm", "cloud_mask_bcm", "quality_flag"} and np.issubdtype(arr.dtype, np.number):
        # Many cloud products use large category values such as 126/127/255 as
        # fill/background. Keep NPZ values raw, but mask them for readable plots.
        plot_arr[plot_arr > 20] = np.nan
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(7, 6), dpi=140)
    finite = np.isfinite(plot_arr.astype("float64", copy=False)) if np.issubdtype(plot_arr.dtype, np.number) else np.ones(plot_arr.shape, dtype=bool)
    title = f"{result['meta']['satellite']} {result['meta']['product']} {chosen}"
    if chosen in {"cloud_mask", "cloud_mask_acm", "cloud_mask_bcm", "quality_flag"}:
        values = plot_arr[finite]
        unique = np.unique(values[: min(values.size, 300_000)]) if values.size else np.asarray([])
        if unique.size and unique.size <= 32:
            colors = plt.get_cmap("tab20", max(2, unique.size)).colors
            cmap = ListedColormap(colors)
            cmap.set_bad("#f2f2f2")
            bounds = np.r_[unique - 0.5, unique[-1] + 0.5]
            norm = BoundaryNorm(bounds, cmap.N)
            im = ax.imshow(np.ma.masked_invalid(plot_arr), origin="upper", cmap=cmap, norm=norm, interpolation="nearest")
        else:
            im = ax.imshow(np.ma.masked_invalid(plot_arr), origin="upper", cmap="viridis", interpolation="nearest")
    else:
        vals = plot_arr.astype("float64", copy=False)[finite]
        if vals.size:
            vmin, vmax = np.nanpercentile(vals, [1, 99])
            if vmin == vmax:
                vmin, vmax = np.nanmin(vals), np.nanmax(vals)
        else:
            vmin, vmax = None, None
        im = ax.imshow(plot_arr, origin="upper", cmap="turbo", vmin=vmin, vmax=vmax)
    ax.set_title(title, fontsize=10)
    ax.set_xticks([])
    ax.set_yticks([])
    fig.colorbar(im, ax=ax, shrink=0.75)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return str(out_path)


def save_npz(result: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {std: arr for std, arr in result["arrays"].items()}
    payload["metadata_json"] = np.asarray(json.dumps(result["meta"], ensure_ascii=False, default=str))
    np.savez_compressed(out_path, **payload)


def write_stats_csv(result: dict[str, Any], out_path: Path) -> None:
    rows = []
    for std, arr in result["arrays"].items():
        stats = numeric_stats(arr)
        rows.append(
            {
                "satellite": result["meta"]["satellite"],
                "product": result["meta"]["product"],
                "standard_variable": std,
                "source_variable": result["meta"]["source_names"].get(std, ""),
                **stats,
            }
        )
    write_csv(out_path, rows)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    if not fields:
        fields = ["empty"]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def summarize_satellites(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        grouped[str(result["meta"]["satellite"])].append(result)
    summary: dict[str, dict[str, Any]] = {}
    for sat, items in grouped.items():
        present = set()
        shapes = {}
        products = []
        for item in items:
            products.append(str(item["meta"]["product"]))
            for std, arr in item["arrays"].items():
                present.add(std)
                shapes[f"{item['meta']['product']}:{std}"] = tuple(np.asarray(arr).shape)
        summary[sat] = {
            "products": sorted(set(products)),
            "present": sorted(present),
            "shapes": shapes,
            "v0_ready": "cloud_mask" in present and "cloud_top_height" in present,
            "v1_cth_ctt_ctp_ready": all(v in present for v in ["cloud_top_height", "cloud_top_temperature", "cloud_top_pressure"]),
        }
    return summary


def fy4b_diagnostics(results: list[dict[str, Any]]) -> list[str]:
    fy4b = [r for r in results if r["meta"]["satellite"] == "FY4B"]
    lines = ["## FY4B Resolution Diagnostics", ""]
    if not fy4b:
        return lines + ["- No FY4B sample was standardized."]
    any_458 = False
    any_2748 = False
    core_shapes = []
    for item in fy4b:
        product = item["meta"]["product"]
        lines.append(f"- FY4B {product}:")
        for std, arr in item["arrays"].items():
            shape = tuple(np.asarray(arr).shape)
            if std in {"cloud_mask", "cloud_top_height", "cloud_top_temperature", "quality_flag"}:
                core_shapes.append(shape)
            lines.append(f"  - {std}: shape {shape}, source `{item['meta']['source_names'].get(std, '')}`")
        large = [f"{name}={shape}" for name, shape in item["all_shapes"].items() if tuple(shape) == (2748, 2748)]
        medium = [f"{name}={shape}" for name, shape in item["all_shapes"].items() if tuple(shape) == (458, 458)]
        any_458 = any_458 or bool(medium)
        any_2748 = any_2748 or bool(large)
        lines.append(f"  - 458x458 variables: {', '.join(medium) if medium else 'none'}")
        lines.append(f"  - 2748x2748 variables: {', '.join(large) if large else 'none in this L2 product'}")
    unique_core_shapes = sorted(set(core_shapes))
    lines.append("")
    if any_458:
        lines.append("Interpretation: at least one selected FY4B file contains 458x458 variables. Any 458x458 field must be treated as a separate native grid until a matching GEO/navigation file explains its relation to 2748x2748.")
    else:
        lines.append("Interpretation: this v0 run did not find 458x458 variables in the selected FY4B CLM/CLT/CTH/CTT files. The core readable cloud variables are on these shapes: " + ", ".join(str(s) for s in unique_core_shapes) + ".")
    if any_2748:
        lines.append("The selected FY4B L2 products do contain 2748x2748 core cloud variables, so these samples are usable as native-grid FY4B cloud inputs for a 4 km GEO-ring prototype, assuming the matching FY4B navigation/GEO convention is used in the later reprojection step.")
    else:
        lines.append("The selected FY4B L2 products do not expose 2748x2748 core cloud variables, so FY4B should not enter a 4 km GEO-ring grid until the correct GEO/L2 pairing is found.")
    return lines


def generate_report(results: list[dict[str, Any]], outputs: list[dict[str, str]]) -> str:
    summary = summarize_satellites(results)
    lines = [
        "# Standardized Cloud v0 Sample Report",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "Scope: no downloading, no full mosaicking, no cross-sensor reprojection. Each product is saved on its native grid.",
        "",
        "## Per-Satellite Readiness",
        "",
        "| Satellite | Products | v0 cloud_mask+CTH | v1 CTH/CTT/CTP | Present variables | Resolution notes |",
        "|---|---|---|---|---|---|",
    ]
    for sat in ["FY4B", "GOES-16", "GOES-18", "Himawari-9", "Meteosat-0deg", "Meteosat-IODC"]:
        info = summary.get(sat, {"products": [], "present": [], "shapes": {}, "v0_ready": False, "v1_cth_ctt_ctp_ready": False})
        shape_bits = [f"{k}={v}" for k, v in info["shapes"].items() if any(token in k for token in ["cloud_mask", "cloud_top_height", "cloud_top_temperature", "cloud_top_pressure"])]
        lines.append(
            f"| {sat} | {', '.join(info['products']) or 'none'} | "
            f"{'YES' if info['v0_ready'] else 'NO'} | "
            f"{'YES' if info['v1_cth_ctt_ctp_ready'] else 'NO'} | "
            f"{', '.join(info['present']) or 'none'} | "
            f"{'; '.join(shape_bits) or 'none'} |"
        )
    lines.extend(
        [
            "",
            "## Direct Answers",
            "",
            "- Satellites that can enter cloud_mask + CTH v0 from these samples: "
            + ", ".join([sat for sat, info in summary.items() if info["v0_ready"]])
            + ".",
            "- Satellites that can enter CTH/CTT/CTP v1 from these samples: "
            + (", ".join([sat for sat, info in summary.items() if info["v1_cth_ctt_ctp_ready"]]) or "none with all three variables confirmed in the selected samples")
            + ".",
            "- Missing-variable pattern: GOES ACMF supplies BCM/ACM/Cloud_Probabilities/DQF and ACHAF supplies HT/DQF; GOES CTT/CTP are not confirmed in the selected ACHAF sample unless separate CTTF/CTPF or bundled variables are present. Meteosat CTH supplies height and quality only, not CTT/CTP. Meteosat CLM has no separate quality flag in the decoded sample. FY4B CTP/CLP/COT/CER are not part of this selected v0 sample set.",
            "- Resolution mismatch: Meteosat CLM is 3712x3712 while Meteosat CTH is 1237x1237, so they are saved separately and are not pixel-wise merged. FY4B shape findings are listed in the dedicated diagnostics section below. GOES ACMF/ACHAF and Himawari CMSK/CHGT should still be treated product-by-product until their grids are explicitly compared.",
            "- Next step: unified-grid reprojection can start as a separate prototype after choosing target projection/resolution and writing explicit resampling rules. It should not start from this script's native-grid outputs as if all arrays were already co-registered.",
            "",
        ]
    )
    lines.extend(fy4b_diagnostics(results))
    lines.extend(["", "## Output Files", ""])
    for out in outputs:
        lines.append(f"- {out['satellite']} {out['product']}: `{out['npz']}`, `{out['quicklook']}`, `{out['stats']}`")
    lines.extend(["", "## Notes By Sensor", ""])
    lines.extend(
        [
            "- GOES: y is explicitly mapped to projection_y and DQF is explicitly mapped to quality_flag. The script records x, y, and goes_imager_projection metadata when present.",
            "- Himawari: CMSK reads CloudMask/CloudProbability/Latitude/Longitude when present; CHGT reads CldTopHght/CldTopTemp/CldTopPres/CldOptDpth/Latitude/Longitude when present.",
            "- Meteosat: GRIB template 90 projection metadata is recorded in each NPZ metadata_json; CLM and CTH are emitted as separate native-grid products.",
            "- Quicklooks use adaptive categorical coloring for cloud masks and robust percentile scaling for continuous fields. Satpy is available in the environment, but this script uses direct decoded arrays so that every quicklook corresponds exactly to the standardized NPZ variable.",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    mapping = read_mapping()
    selection = pd.read_csv(SELECTION_CSV, encoding="utf-8-sig")
    rows = choose_selection_rows(selection)
    results: list[dict[str, Any]] = []
    outputs: list[dict[str, str]] = []
    for row in rows:
        log(f"Reading {row['satellite']} {row['product']}: {row['file_path']}")
        result = read_sample(row, mapping)
        results.append(result)
        stem = sanitize_name(f"{row['satellite']}_{row['product']}")
        npz_path = OUT_DIR / f"{stem}.npz"
        png_path = OUT_DIR / f"{stem}_quicklook.png"
        stats_path = OUT_DIR / f"{stem}_variable_statistics.csv"
        save_npz(result, npz_path)
        quicklook = make_quicklook(result, png_path) or ""
        write_stats_csv(result, stats_path)
        outputs.append({"satellite": row["satellite"], "product": row["product"], "npz": str(npz_path), "quicklook": quicklook, "stats": str(stats_path)})
    REPORT_MD.write_text(generate_report(results, outputs), encoding="utf-8-sig")
    log(f"Finished. products={len(results)} out={OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
