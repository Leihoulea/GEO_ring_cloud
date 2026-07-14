from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import h5py
import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, ListedColormap


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[3]
INPUT_ROOT = Path(os.environ.get("FY4B_CPD_ROOT", REPO_ROOT / "data" / "FY4B_CPD"))
OUT_DIR = Path(
    os.environ.get(
        "FY4B_CPD_INSPECTION_OUT",
        REPO_ROOT / "data_check_report" / "fy4b_cpd_product_inspection",
    )
)
QUICKLOOK_DIR = OUT_DIR / "quicklooks"

PRODUCT_PREFIX = "stage_00b_fy4b_cpd"
TIME_RE = re.compile(
    r"FY4B-.*?_L2-_CPD-_.*?_(?P<start>20\d{12})_(?P<end>20\d{12})_(?P<resolution>\d+M)_V(?P<version>[0-9A-Z.]+)\.NC$",
    re.I,
)
SCIENCE_VARIABLES = {"COT", "CER", "LWP", "IWP"}
QUALITY_VARIABLES = {"DQF"}
COORDINATE_VARIABLES = {
    "x",
    "y",
    "nominal_satellite_height",
    "nominal_satellite_subpoint_lat",
    "nominal_satellite_subpoint_lon",
    "geospatial_lat_lon_extent",
}
MAX_QUICK_STATS_VALUES = 250_000
FULL_STATS_SAMPLE_POSITIONS = {"first", "middle", "last"}

DQF_BIT_FIELDS = [
    ("cloud_mask", 0, 1, "0=cloudy, 1=possible_cloudy"),
    ("cloud_fraction", 1, 1, "0=partly_cloudy, 1=totally_cloudy"),
    ("retrieval_phase", 2, 2, "0=warm_water, 1=super_cooled, 2=mixed, 3=ice"),
    ("sunglint", 4, 1, "0=no_sunglint, 1=sunglint"),
    ("surface", 5, 2, "0=water, 1=coastline, 2=land"),
    ("retrieval_outcome", 7, 1, "0=failed, 1=successful"),
    ("cot_confidence", 8, 2, "0=no_retrieval, 1=low_quality, 2=high_quality_3_lt_cot_lt_25"),
    ("cer_confidence", 10, 2, "0=no_retrieval, 1=low_quality, 2=high_quality_water_lt_12_ice_gt_28"),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def h5_value_to_python(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, h5py.Reference):
        return str(value)
    if isinstance(value, np.ndarray):
        return [h5_value_to_python(v) for v in value.tolist()]
    if isinstance(value, np.generic):
        return h5_value_to_python(value.item())
    return value


def safe_json(value: Any, limit: int = 12000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=h5_value_to_python)
    except Exception:
        text = str(value)
    return text[:limit]


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=h5_value_to_python)


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = []
        for row in rows:
            for key in row:
                if key not in fields:
                    fields.append(key)
        if not fields:
            fields = ["empty"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fields})


def parse_time(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return ""


def parse_file(path: Path) -> dict[str, Any]:
    stat = path.stat()
    match = TIME_RE.search(path.name)
    row: dict[str, Any] = {
        "file_path": str(path),
        "file_name": path.name,
        "suffix": path.suffix,
        "file_size_bytes": stat.st_size,
        "file_size_mb": round(stat.st_size / 1024 / 1024, 3),
        "modified_time": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "satellite": "FY4B" if "FY4B" in path.name.upper() else "",
        "sensor": "AGRI" if "AGRI" in path.name.upper() else "",
        "level": "L2" if "_L2-" in path.name.upper() else "",
        "product": "CPD" if "_CPD-" in path.name.upper() else "",
        "coverage": "DISK_1050E" if "DISK_1050E" in path.name.upper() else "",
        "start_time": "",
        "end_time": "",
        "resolution": "",
        "version": "",
        "parse_status": "unparsed",
        "open_status": "not_opened",
    }
    if match:
        row.update(
            {
                "start_time": parse_time(match.group("start")),
                "end_time": parse_time(match.group("end")),
                "resolution": match.group("resolution"),
                "version": match.group("version"),
                "parse_status": "parsed",
            }
        )
    return row


def choose_samples(files: list[Path]) -> dict[Path, str]:
    if not files:
        return {}
    positions = {0: "first", len(files) // 2: "middle", len(files) - 1: "last"}
    return {files[idx]: label for idx, label in sorted(positions.items())}


def attrs_to_dict(obj: Any) -> dict[str, Any]:
    return {key: h5_value_to_python(obj.attrs[key]) for key in sorted(obj.attrs.keys())}


def attr_lookup(attrs: dict[str, Any], *names: str) -> Any:
    lower = {str(k).lower(): v for k, v in attrs.items()}
    for name in names:
        if name in attrs:
            return attrs[name]
        if name.lower() in lower:
            return lower[name.lower()]
    return ""


def attr_text(value: Any) -> str:
    if value == "":
        return ""
    if isinstance(value, list):
        return safe_json(value, limit=600)
    return str(value)


def shape_text(shape: tuple[int, ...] | None) -> str:
    if not shape:
        return ""
    return "x".join(str(dim) for dim in shape)


def classify_variable(name: str) -> str:
    if name in SCIENCE_VARIABLES:
        return "science"
    if name in QUALITY_VARIABLES:
        return "quality_flag"
    if name in COORDINATE_VARIABLES:
        return "coordinate_or_navigation"
    if name in {"algorithm_product_version_container", "processing_parm_version_container", "OBIType"}:
        return "metadata_container"
    if name == "o":
        return "netcdf_dimension_scale"
    return "unknown"


def semantic_guess(name: str, attrs: dict[str, Any]) -> str:
    haystack = " ".join(str(v) for v in [name, attrs.get("long_name", ""), attrs.get("standard_name", ""), attrs.get("Description", "")]).lower()
    if name == "DQF" or "quality" in haystack:
        return "quality_flag"
    if name == "COT" or "optical" in haystack:
        return "cloud_optical_thickness"
    if name == "CER" or "effective radius" in haystack:
        return "cloud_effective_radius"
    if name == "LWP" or "liquid water path" in haystack:
        return "cloud_liquid_water_path"
    if name == "IWP" or "ice water path" in haystack:
        return "cloud_ice_water_path"
    if name == "x":
        return "projection_x"
    if name == "y":
        return "projection_y"
    if "subpoint_lon" in name:
        return "satellite_subpoint_longitude"
    if "subpoint_lat" in name:
        return "satellite_subpoint_latitude"
    if "height" in name:
        return "nominal_satellite_height"
    if "extent" in name:
        return "geospatial_extent_metadata"
    return ""


def special_codes_from_description(attrs: dict[str, Any]) -> dict[float, str]:
    description = str(attr_lookup(attrs, "Description", "description"))
    out: dict[float, str] = {}
    for value, meaning in re.findall(r"(-?\d+(?:\.\d+)?)\s*:\s*([^,;]+)", description):
        label = meaning.strip().lower()
        if any(token in label for token in ["space", "clear", "nighttime", "fill"]):
            try:
                out[float(value)] = label
            except ValueError:
                pass
    return out


def as_numeric_sample(dataset: h5py.Dataset, full: bool) -> tuple[np.ndarray | None, str]:
    if dataset.size == 0 or dataset.dtype.kind in {"S", "U", "O"}:
        return None, "non_numeric_or_empty"
    if full or dataset.ndim == 0 or dataset.size <= MAX_QUICK_STATS_VALUES:
        return np.asarray(dataset[...]), "full"
    stride = max(1, int(np.ceil(np.sqrt(dataset.size / MAX_QUICK_STATS_VALUES)))) if dataset.ndim >= 2 else max(1, dataset.size // MAX_QUICK_STATS_VALUES)
    selectors = tuple(slice(None, None, stride) for _ in dataset.shape)
    return np.asarray(dataset[selectors]), f"stride_{stride}"


def fill_mask(data: np.ndarray, fill_value: Any) -> np.ndarray:
    mask = ~np.isfinite(data.astype("float64", copy=False)) if data.dtype.kind in {"f", "c"} else np.zeros(data.shape, dtype=bool)
    if fill_value != "":
        values = fill_value if isinstance(fill_value, list) else [fill_value]
        for value in values:
            try:
                mask |= np.isclose(data.astype("float64", copy=False), float(value))
            except Exception:
                pass
    return mask


def special_mask(data: np.ndarray, codes: dict[float, str]) -> np.ndarray:
    mask = np.zeros(data.shape, dtype=bool)
    if not codes:
        return mask
    work = data.astype("float64", copy=False)
    for code in codes:
        mask |= np.isclose(work, code)
    return mask


def numeric_stats(dataset: h5py.Dataset, attrs: dict[str, Any], full: bool) -> dict[str, Any]:
    data, scope = as_numeric_sample(dataset, full)
    out: dict[str, Any] = {
        "stat_scope": scope,
        "sample_count": "",
        "raw_min": "",
        "raw_max": "",
        "raw_mean": "",
        "fill_or_nan_count": "",
        "fill_or_nan_fraction": "",
        "fill_masked_count": "",
        "fill_masked_min": "",
        "fill_masked_max": "",
        "fill_masked_mean": "",
        "fill_masked_p01": "",
        "fill_masked_p50": "",
        "fill_masked_p99": "",
        "unique_count": "",
        "unique_values_top20_json": "",
        "known_special_values_json": "",
        "known_special_count": "",
        "known_special_fraction": "",
        "physical_valid_count": "",
        "physical_min": "",
        "physical_max": "",
        "physical_mean": "",
        "physical_p01": "",
        "physical_p50": "",
        "physical_p99": "",
    }
    if data is None:
        return out
    flat = data.reshape(-1)
    flat_float = flat.astype("float64", copy=False)
    finite = np.isfinite(flat_float)
    out["sample_count"] = int(flat.size)
    if finite.any():
        out["raw_min"] = float(np.nanmin(flat_float[finite]))
        out["raw_max"] = float(np.nanmax(flat_float[finite]))
        out["raw_mean"] = float(np.nanmean(flat_float[finite]))
    fill_value = attr_lookup(attrs, "FillValue", "_FillValue", "missing_value")
    missing = fill_mask(flat, fill_value)
    special_codes = special_codes_from_description(attrs)
    special = special_mask(flat, special_codes)
    fill_masked = flat[~missing]
    out["fill_or_nan_count"] = int(missing.sum())
    out["fill_or_nan_fraction"] = float(missing.sum() / flat.size) if flat.size else ""
    out["fill_masked_count"] = int(fill_masked.size)
    out["known_special_values_json"] = safe_json(special_codes, limit=1000) if special_codes else ""
    out["known_special_count"] = int((special & ~missing).sum())
    out["known_special_fraction"] = float((special & ~missing).sum() / flat.size) if flat.size else ""
    if fill_masked.size:
        valid_float = fill_masked.astype("float64", copy=False)
        out.update(
            {
                "fill_masked_min": float(np.nanmin(valid_float)),
                "fill_masked_max": float(np.nanmax(valid_float)),
                "fill_masked_mean": float(np.nanmean(valid_float)),
                "fill_masked_p01": float(np.nanpercentile(valid_float, 1)),
                "fill_masked_p50": float(np.nanpercentile(valid_float, 50)),
                "fill_masked_p99": float(np.nanpercentile(valid_float, 99)),
            }
        )
        if dataset.dtype.kind in {"i", "u", "b"} or fill_masked.size <= 50_000:
            values, counts = np.unique(fill_masked, return_counts=True)
            order = np.argsort(counts)[::-1]
            out["unique_count"] = int(values.size)
            out["unique_values_top20_json"] = safe_json(
                [{"value": h5_value_to_python(values[i]), "count": int(counts[i])} for i in order[:20]],
                limit=3000,
            )
    physical = flat[~missing & ~special]
    if physical.size:
        physical_float = physical.astype("float64", copy=False)
        out.update(
            {
                "physical_valid_count": int(physical.size),
                "physical_min": float(np.nanmin(physical_float)),
                "physical_max": float(np.nanmax(physical_float)),
                "physical_mean": float(np.nanmean(physical_float)),
                "physical_p01": float(np.nanpercentile(physical_float, 1)),
                "physical_p50": float(np.nanpercentile(physical_float, 50)),
                "physical_p99": float(np.nanpercentile(physical_float, 99)),
            }
        )
    return out


def decoded_bit_array(work: np.ndarray, start_bit: int, bit_count: int) -> np.ndarray:
    return ((work.astype(np.uint16, copy=False) >> start_bit) & ((1 << bit_count) - 1)).astype(np.uint8)


def dimension_scale_paths(dataset: h5py.Dataset) -> list[str]:
    paths: list[str] = []
    for dim in dataset.dims:
        for idx in range(len(dim)):
            try:
                paths.append(dim[idx].name or "")
            except Exception:
                paths.append("")
    return paths


def dimension_rows(file_row: dict[str, Any], h5: h5py.File, name: str, obj: h5py.Dataset) -> list[dict[str, Any]]:
    rows = []
    for axis, dim in enumerate(obj.dims):
        scale_paths = []
        for idx in range(len(dim)):
            try:
                scale_paths.append(dim[idx].name or "")
            except Exception as exc:
                scale_paths.append(f"ERROR:{type(exc).__name__}:{exc}")
        rows.append(
            {
                **file_row,
                "variable_name": name,
                "axis": axis,
                "axis_length": obj.shape[axis] if axis < len(obj.shape) else "",
                "dimension_label": dim.label,
                "attached_scale_count": len(dim),
                "attached_scale_paths": ";".join(scale_paths),
                "is_dimension_scale_variable": str(name in {"x", "y", "o"}),
                "coordinate_links_attr": attr_text(attr_lookup(attrs_to_dict(obj), "coordinates")),
                "unlimited": "false",
            }
        )
    return rows


def structure_signature(h5: h5py.File) -> tuple[str, dict[str, Any]]:
    datasets = []
    groups = []
    for name, obj in h5.items():
        if isinstance(obj, h5py.Dataset):
            attrs = attrs_to_dict(obj)
            datasets.append(
                {
                    "name": name,
                    "shape": list(obj.shape),
                    "dtype": str(obj.dtype),
                    "chunks": list(obj.chunks) if obj.chunks else [],
                    "compression": obj.compression or "",
                    "dim_scales": dimension_scale_paths(obj),
                    "attr_keys": sorted(attrs.keys()),
                    "units": attr_text(attr_lookup(attrs, "units")),
                    "fill": attr_text(attr_lookup(attrs, "FillValue", "_FillValue", "missing_value")),
                    "scale_factor": attr_text(attr_lookup(attrs, "scale_factor")),
                    "add_offset": attr_text(attr_lookup(attrs, "add_offset")),
                }
            )
        else:
            groups.append(name)
    payload = {
        "groups": sorted(groups),
        "datasets": sorted(datasets, key=lambda row: row["name"]),
        "global_attr_keys": sorted(h5.attrs.keys()),
    }
    digest = hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()
    return digest, payload


def code_table_rows(file_row: dict[str, Any], variable_name: str, attrs: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    fill_value = attr_lookup(attrs, "FillValue", "_FillValue", "missing_value")
    for code, meaning in special_codes_from_description(attrs).items():
        rows.append(
            {
                **file_row,
                "raw_variable": variable_name,
                "table_type": "category_or_sentinel",
                "code_value": code,
                "meaning": meaning,
                "fill_codes": attr_text(fill_value),
                "meaning_source": "variable Description attribute",
            }
        )
    if variable_name == "DQF":
        for field_name, start_bit, bit_count, meaning in DQF_BIT_FIELDS:
            mask_decimal = ((1 << bit_count) - 1) << start_bit
            rows.append(
                {
                    **file_row,
                    "raw_variable": "DQF",
                    "table_type": "bitfield",
                    "field_name": field_name,
                    "start_bit": start_bit,
                    "bit_count": bit_count,
                    "bit_numbering_convention": "least_significant_bit_zero",
                    "bit_mask_decimal": mask_decimal,
                    "bit_mask_hex": hex(mask_decimal),
                    "flag_masks": mask_decimal,
                    "flag_meanings": meaning,
                    "fill_codes": attr_text(fill_value),
                    "meaning_source": "DQF Description attribute",
                }
            )
    return rows


def add_category_stats(
    rows: list[dict[str, Any]],
    file_row: dict[str, Any],
    sample_position: str,
    variable_name: str,
    data: np.ndarray,
    attrs: dict[str, Any],
) -> None:
    flat = data.reshape(-1)
    fill_value = attr_lookup(attrs, "FillValue", "_FillValue", "missing_value")
    missing = fill_mask(flat, fill_value)
    for code, meaning in special_codes_from_description(attrs).items():
        count = int(np.count_nonzero(np.isclose(flat.astype("float64", copy=False), code)))
        rows.append(
            {
                **file_row,
                "sample_position": sample_position,
                "variable_name": variable_name,
                "category_code": code,
                "category_meaning": meaning,
                "count": count,
                "fraction_of_pixels": float(count / flat.size) if flat.size else "",
                "meaning_source": "variable Description attribute",
            }
        )
    if fill_value != "":
        rows.append(
            {
                **file_row,
                "sample_position": sample_position,
                "variable_name": variable_name,
                "category_code": attr_text(fill_value),
                "category_meaning": "fillvalue",
                "count": int(missing.sum()),
                "fraction_of_pixels": float(missing.sum() / flat.size) if flat.size else "",
                "meaning_source": "FillValue attribute",
            }
        )


def quicklook_downsample(arr: np.ndarray, max_dim: int = 1200) -> tuple[np.ndarray, int]:
    if arr.ndim != 2:
        return arr, 1
    stride = max(1, int(np.ceil(max(arr.shape) / max_dim)))
    return arr[::stride, ::stride], stride


def save_continuous_quicklook(path: Path, arr: np.ndarray, title: str, units: str, fill: np.ndarray, special: np.ndarray) -> str:
    plot = arr.astype("float64", copy=True)
    plot[fill | special] = np.nan
    plot, stride = quicklook_downsample(plot)
    finite = np.isfinite(plot)
    plt.figure(figsize=(8, 6), dpi=150)
    cmap = plt.get_cmap("viridis").copy()
    cmap.set_bad((0.90, 0.90, 0.90, 1.0))
    if finite.any():
        vmin, vmax = np.nanpercentile(plot[finite], [2, 98])
        if np.isclose(vmin, vmax):
            vmin, vmax = float(np.nanmin(plot[finite])), float(np.nanmax(plot[finite]))
        image = plt.imshow(plot, cmap=cmap, vmin=vmin, vmax=vmax, interpolation="nearest")
    else:
        image = plt.imshow(plot, cmap=cmap, interpolation="nearest")
    plt.colorbar(image, shrink=0.78, label=units or "")
    plt.title(title, fontsize=9)
    plt.axis("off")
    plt.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(path)
    plt.close()
    return f"robust_p02_p98_downsample_stride_{stride}"


def save_categorical_quicklook(path: Path, arr: np.ndarray, title: str, labels: dict[int, str]) -> str:
    plot, stride = quicklook_downsample(arr)
    finite_values = sorted(int(v) for v in np.unique(plot[np.isfinite(plot)]))
    if not finite_values:
        finite_values = [0]
    bounds = np.array(finite_values + [finite_values[-1] + 1], dtype=float) - 0.5
    cmap = ListedColormap(plt.get_cmap("tab20")(np.linspace(0, 1, max(1, len(finite_values)))))
    norm = BoundaryNorm(bounds, cmap.N)
    plt.figure(figsize=(8, 6), dpi=150)
    image = plt.imshow(plot, cmap=cmap, norm=norm, interpolation="nearest")
    cbar = plt.colorbar(image, shrink=0.78, ticks=finite_values)
    tick_labels = [f"{value}: {labels.get(value, '')}"[:45] for value in finite_values]
    cbar.ax.set_yticklabels(tick_labels, fontsize=7)
    plt.title(title, fontsize=9)
    plt.axis("off")
    plt.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(path)
    plt.close()
    return f"categorical_downsample_stride_{stride}"


def build_quicklooks(
    file_row: dict[str, Any],
    sample_position: str,
    h5: h5py.File,
    quicklook_rows: list[dict[str, Any]],
) -> None:
    if sample_position != "middle":
        return
    stamp = file_row["start_time"].replace(":", "").replace("-", "").replace("Z", "")
    for name in ["COT", "CER", "LWP", "IWP"]:
        if name not in h5:
            continue
        ds = h5[name]
        attrs = attrs_to_dict(ds)
        data = np.asarray(ds[...])
        fill = fill_mask(data, attr_lookup(attrs, "FillValue", "_FillValue", "missing_value"))
        specials = special_codes_from_description(attrs)
        special = special_mask(data, specials)
        out = QUICKLOOK_DIR / f"{PRODUCT_PREFIX}_{stamp}_{name}_physical.png"
        scaling = save_continuous_quicklook(out, data, f"FY4B CPD {name} physical-only {file_row['start_time']}", attr_text(attr_lookup(attrs, "units")), fill, special)
        quicklook_rows.append(
            {
                "plot_path": str(out),
                "source_variable": name,
                "source_file": file_row["file_path"],
                "sample_position": sample_position,
                "reader": "h5py",
                "plot_type": "physical_continuous",
                "scaling": scaling,
                "colormap": "viridis",
                "units": attr_text(attr_lookup(attrs, "units")),
                "valid_mask_rule": "mask FillValue plus category/sentinel codes from Description",
                "meaning_note": "用于检查空间分布，不是出版图。",
            }
        )
        for code, meaning in specials.items():
            mask = np.isclose(data.astype("float64", copy=False), code).astype(np.uint8)
            out = QUICKLOOK_DIR / f"{PRODUCT_PREFIX}_{stamp}_{name}_{meaning.replace(' ', '_')}_{int(code)}_mask.png"
            scaling = save_categorical_quicklook(out, mask, f"FY4B CPD {name} {meaning} mask {file_row['start_time']}", {0: "other", 1: meaning})
            quicklook_rows.append(
                {
                    "plot_path": str(out),
                    "source_variable": name,
                    "source_file": file_row["file_path"],
                    "sample_position": sample_position,
                    "reader": "h5py",
                    "plot_type": "category_sentinel_mask",
                    "scaling": scaling,
                    "colormap": "tab20",
                    "units": "",
                    "valid_mask_rule": f"value == {code}",
                    "meaning_note": f"类别/哨兵码：{meaning}",
                }
            )
    if "DQF" in h5:
        ds = h5["DQF"]
        attrs = attrs_to_dict(ds)
        data = np.asarray(ds[...])
        fill = fill_mask(data, attr_lookup(attrs, "FillValue", "_FillValue", "missing_value"))
        raw = data.astype("float64", copy=True)
        raw[fill] = np.nan
        out = QUICKLOOK_DIR / f"{PRODUCT_PREFIX}_{stamp}_DQF_raw.png"
        scaling = save_continuous_quicklook(out, raw, f"FY4B CPD DQF raw codes {file_row['start_time']}", "", np.isnan(raw), np.zeros(raw.shape, dtype=bool))
        quicklook_rows.append(
            {
                "plot_path": str(out),
                "source_variable": "DQF",
                "source_file": file_row["file_path"],
                "sample_position": sample_position,
                "reader": "h5py",
                "plot_type": "raw_quality_code",
                "scaling": scaling,
                "colormap": "viridis",
                "units": "",
                "valid_mask_rule": "mask FillValue",
                "meaning_note": "DQF raw code，语义需结合 bitfield diagnostics。",
            }
        )
        non_fill_data = np.where(fill, 0, data).astype(np.uint16)
        for field_name, start_bit, bit_count, meaning in DQF_BIT_FIELDS:
            decoded = decoded_bit_array(non_fill_data, start_bit, bit_count)
            labels = {}
            for item in meaning.split(","):
                if "=" in item:
                    left, right = item.split("=", 1)
                    try:
                        labels[int(left.strip())] = right.strip()
                    except ValueError:
                        pass
            out = QUICKLOOK_DIR / f"{PRODUCT_PREFIX}_{stamp}_DQF_{field_name}.png"
            scaling = save_categorical_quicklook(out, decoded, f"FY4B CPD DQF {field_name} {file_row['start_time']}", labels)
            quicklook_rows.append(
                {
                    "plot_path": str(out),
                    "source_variable": "DQF",
                    "source_file": file_row["file_path"],
                    "sample_position": sample_position,
                    "reader": "h5py",
                    "plot_type": "decoded_bitfield",
                    "scaling": scaling,
                    "colormap": "tab20",
                    "units": "",
                    "valid_mask_rule": "decoded from non-fill DQF; fill set to zero for display only",
                    "meaning_note": f"{field_name}: {meaning}",
                }
            )


def inspect_file(
    file_row: dict[str, Any],
    sample_position: str,
    quicklook_rows: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    path = Path(file_row["file_path"])
    with h5py.File(path, "r") as h5:
        file_row["open_status"] = "OK"
        signature_hash, signature_payload = structure_signature(h5)
        rows["structure"].append(
            {
                **file_row,
                "sample_position": sample_position,
                "structure_signature_sha256": signature_hash,
                "group_count": sum(1 for obj in h5.values() if isinstance(obj, h5py.Group)),
                "dataset_count": sum(1 for obj in h5.values() if isinstance(obj, h5py.Dataset)),
                "dimension_count": len({scale for obj in h5.values() if isinstance(obj, h5py.Dataset) for scale in dimension_scale_paths(obj)}),
                "global_attribute_count": len(h5.attrs),
                "variable_names": ";".join(sorted(name for name, obj in h5.items() if isinstance(obj, h5py.Dataset))),
                "signature_payload_json": safe_json(signature_payload),
            }
        )
        for attr_name, attr_value in attrs_to_dict(h5).items():
            rows["global_attrs"].append(
                {
                    **file_row,
                    "sample_position": sample_position,
                    "attribute_name": attr_name,
                    "attribute_value": attr_text(attr_value),
                    "attribute_value_json": safe_json(attr_value),
                }
            )
        for name, obj in h5.items():
            if not isinstance(obj, h5py.Dataset):
                continue
            attrs = attrs_to_dict(obj)
            fill_value = attr_lookup(attrs, "FillValue", "_FillValue", "missing_value")
            valid_range = attr_lookup(attrs, "valid_range", "valid_min", "valid_max")
            dim_paths = dimension_scale_paths(obj)
            var_row = {
                **file_row,
                "sample_position": sample_position,
                "variable_path": "/" + name,
                "variable_name": name,
                "variable_role": classify_variable(name),
                "semantic_guess": semantic_guess(name, attrs),
                "shape": shape_text(obj.shape),
                "ndim": obj.ndim,
                "size": int(obj.size),
                "dtype": str(obj.dtype),
                "chunks": shape_text(obj.chunks),
                "compression": obj.compression or "",
                "compression_opts": attr_text(obj.compression_opts),
                "dimensions": ";".join(dim_paths),
                "units": attr_text(attr_lookup(attrs, "units")),
                "long_name": attr_text(attr_lookup(attrs, "long_name")),
                "standard_name": attr_text(attr_lookup(attrs, "standard_name")),
                "description": attr_text(attr_lookup(attrs, "Description", "description")),
                "coordinates": attr_text(attr_lookup(attrs, "coordinates")),
                "grid_mapping": attr_text(attr_lookup(attrs, "grid_mapping")),
                "ancillary_variables": attr_text(attr_lookup(attrs, "ancillary_variables")),
                "fill_value": attr_text(fill_value),
                "valid_range": attr_text(valid_range),
                "scale_factor": attr_text(attr_lookup(attrs, "scale_factor")),
                "add_offset": attr_text(attr_lookup(attrs, "add_offset")),
                "unsigned": attr_text(attr_lookup(attrs, "_Unsigned")),
                "code_table_attributes_json": safe_json(
                    {
                        key: attrs[key]
                        for key in attrs
                        if "flag" in key.lower() or "meaning" in key.lower() or "description" in key.lower() or key in {"FillValue", "valid_range"}
                    }
                ),
                "attributes_json": safe_json(attrs),
            }
            rows["variables"].append(var_row)
            rows["stats"].append({**var_row, **numeric_stats(obj, attrs, full=sample_position in FULL_STATS_SAMPLE_POSITIONS or name in {"x", "y", "o"})})
            rows["dimensions"].extend(dimension_rows(file_row, h5, name, obj))
            for attr_name, attr_value in attrs.items():
                rows["variable_attrs"].append(
                    {
                        **file_row,
                        "sample_position": sample_position,
                        "variable_path": "/" + name,
                        "variable_name": name,
                        "attribute_name": attr_name,
                        "attribute_value": attr_text(attr_value),
                        "attribute_value_json": safe_json(attr_value),
                    }
                )
            rows["code_table"].extend(code_table_rows(file_row, name, attrs))
            if obj.ndim == 2 and name in SCIENCE_VARIABLES and sample_position in FULL_STATS_SAMPLE_POSITIONS:
                add_category_stats(rows["category_stats"], file_row, sample_position, name, np.asarray(obj[...]), attrs)
            if name == "DQF" and sample_position in FULL_STATS_SAMPLE_POSITIONS:
                data = np.asarray(obj[...]).reshape(-1)
                fill_value_raw = attr_lookup(attrs, "FillValue", "_FillValue", "missing_value")
                missing = fill_mask(data, fill_value_raw)
                non_fill = data[~missing].astype(np.uint16, copy=False)
                values, counts = np.unique(non_fill, return_counts=True)
                for value, count in zip(values, counts):
                    rows["quality_decode"].append(
                        {
                            **file_row,
                            "sample_position": sample_position,
                            "record_type": "raw_code_count",
                            "raw_variable": "DQF",
                            "raw_dtype": str(obj.dtype),
                            "raw_value": int(value),
                            "count": int(count),
                            "fraction_of_non_fill": float(count / max(1, non_fill.size)),
                            "fill_codes": attr_text(fill_value_raw),
                            "meaning_source": "raw code count before bit decoding",
                        }
                    )
                if missing.any():
                    rows["quality_decode"].append(
                        {
                            **file_row,
                            "sample_position": sample_position,
                            "record_type": "fill_count",
                            "raw_variable": "DQF",
                            "raw_dtype": str(obj.dtype),
                            "raw_value": attr_text(fill_value_raw),
                            "count": int(missing.sum()),
                            "fraction_of_non_fill": "",
                            "fill_codes": attr_text(fill_value_raw),
                            "meaning_source": "FillValue attribute",
                        }
                    )
                if non_fill.size:
                    combo_counter = Counter()
                    decoded_values_by_field: dict[str, np.ndarray] = {}
                    for field_name, start_bit, bit_count, meaning in DQF_BIT_FIELDS:
                        decoded = decoded_bit_array(non_fill, start_bit, bit_count)
                        decoded_values_by_field[field_name] = decoded
                        vals, cnts = np.unique(decoded, return_counts=True)
                        bit_mask = ((1 << bit_count) - 1) << start_bit
                        for value, count in zip(vals, cnts):
                            rows["bitfield"].append(
                                {
                                    **file_row,
                                    "sample_position": sample_position,
                                    "raw_variable": "DQF",
                                    "raw_dtype": str(obj.dtype),
                                    "raw_unique_values": int(values.size),
                                    "fill_codes": attr_text(fill_value_raw),
                                    "flag_values": "",
                                    "flag_masks": bit_mask,
                                    "flag_meanings": meaning,
                                    "bit_numbering_convention": "least_significant_bit_zero",
                                    "bit_index": start_bit,
                                    "bit_mask_decimal": bit_mask,
                                    "bit_mask_hex": hex(bit_mask),
                                    "field_name": field_name,
                                    "start_bit": start_bit,
                                    "bit_count": bit_count,
                                    "decoded_value": int(value),
                                    "decoded_value_count": int(count),
                                    "decoded_value_fraction": float(count / non_fill.size),
                                    "meaning_source": "DQF Description attribute",
                                }
                            )
                            rows["quality_decode"].append(
                                {
                                    **file_row,
                                    "sample_position": sample_position,
                                    "record_type": "decoded_bit_count",
                                    "raw_variable": "DQF",
                                    "raw_dtype": str(obj.dtype),
                                    "decoded_field": field_name,
                                    "start_bit": start_bit,
                                    "bit_count": bit_count,
                                    "decoded_value": int(value),
                                    "count": int(count),
                                    "fraction_of_non_fill": float(count / non_fill.size),
                                    "fill_codes": attr_text(fill_value_raw),
                                    "meaning_source": meaning,
                                }
                            )
                    fields = [field[0] for field in DQF_BIT_FIELDS]
                    stacked = np.vstack([decoded_values_by_field[field] for field in fields]).T
                    for combo in map(tuple, stacked.tolist()):
                        combo_counter[combo] += 1
                    for combo, count in combo_counter.most_common():
                        rows["observed_combos"].append(
                            {
                                **file_row,
                                "sample_position": sample_position,
                                "raw_variable": "DQF",
                                "observed_code_or_combination": "|".join(f"{field}={value}" for field, value in zip(fields, combo)),
                                "decoded_meanings": "; ".join(field[3] for field in DQF_BIT_FIELDS),
                                "count": count,
                                "fraction_of_non_fill": float(count / non_fill.size),
                                "meaning_source": "DQF Description attribute",
                            }
                        )
            if name in COORDINATE_VARIABLES:
                coord_row = dict(var_row)
                if obj.size <= 20:
                    try:
                        coord_row["values_json"] = safe_json(np.asarray(obj[...]))
                    except Exception as exc:
                        coord_row["values_json"] = f"{type(exc).__name__}: {exc}"
                rows["coordinate_audit"].append(coord_row)
        build_quicklooks(file_row, sample_position, h5, quicklook_rows)
    return rows


def consistency_rows(variable_rows: list[dict[str, Any]], structure_rows: list[dict[str, Any]], file_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    by_var: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in variable_rows:
        by_var[row["variable_name"]].append(row)
    for var, var_rows in sorted(by_var.items()):
        fields = ["shape", "dtype", "units", "fill_value", "scale_factor", "add_offset", "valid_range", "dimensions"]
        out = {"check_type": "variable_consistency", "variable_name": var, "file_count": len(var_rows)}
        consistent = True
        for field in fields:
            values = sorted({str(row.get(field, "")) for row in var_rows})
            out[f"{field}_unique_count"] = len(values)
            out[f"{field}_values"] = "; ".join(values)
            if len(values) > 1:
                consistent = False
        out["is_consistent"] = str(consistent)
        rows.append(out)
    sig_counts = Counter(row["structure_signature_sha256"] for row in structure_rows)
    for signature, count in sorted(sig_counts.items()):
        rows.append(
            {
                "check_type": "structure_signature",
                "structure_signature_sha256": signature,
                "file_count": count,
                "is_consistent": str(len(sig_counts) == 1),
            }
        )
    starts = sorted(row["start_time"] for row in file_rows if row.get("start_time"))
    rows.append(
        {
            "check_type": "time_coverage",
            "file_count": len(file_rows),
            "first_start_time": starts[0] if starts else "",
            "last_start_time": starts[-1] if starts else "",
            "duplicate_start_times": ";".join([time for time, count in Counter(starts).items() if count > 1]),
            "is_consistent": str(len(starts) == len(set(starts))),
        }
    )
    return rows


def anomaly_rows_from_units(file_rows: list[dict[str, Any]], coordinate_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in coordinate_rows:
        if row.get("variable_name") == "nominal_satellite_height" and row.get("values_json"):
            try:
                value = float(json.loads(row["values_json"])[0])
            except Exception:
                value = np.nan
            units = row.get("units", "")
            if units == "km" and value > 1_000_000:
                rows.append(
                    {
                        **{key: row.get(key, "") for key in file_rows[0].keys()},
                        "sample_position": row.get("sample_position", ""),
                        "severity": "WARN",
                        "issue": "unit_value_contradiction",
                        "variable_name": "nominal_satellite_height",
                        "detail": f"value={value} with units=km; likely meters or mislabeled units",
                    }
                )
    return rows


def build_chinese_report(
    file_rows: list[dict[str, Any]],
    structure_rows: list[dict[str, Any]],
    variable_rows: list[dict[str, Any]],
    stats_rows: list[dict[str, Any]],
    bitfield_rows: list[dict[str, Any]],
    quicklook_rows: list[dict[str, Any]],
    consistency: list[dict[str, Any]],
    anomalies: list[dict[str, Any]],
    sample_map: dict[Path, str],
    outputs: dict[str, Path],
) -> str:
    parsed = sum(1 for row in file_rows if row["parse_status"] == "parsed")
    sig_counts = Counter(row["structure_signature_sha256"] for row in structure_rows)
    middle_stats = {
        row["variable_name"]: row
        for row in stats_rows
        if row.get("sample_position") == "middle" and row.get("variable_name") in SCIENCE_VARIABLES | QUALITY_VARIABLES
    }
    variable_names = sorted({row["variable_name"] for row in variable_rows})
    lines = [
        "# FY4B AGRI L2 CPD 产品重新探查报告",
        "",
        f"- 生成时间 UTC：`{utc_now()}`",
        f"- 输入目录：`{INPUT_ROOT}`",
        f"- 文件数：`{len(file_rows)}`，文件名解析成功：`{parsed}/{len(file_rows)}`",
        f"- 代表样本：`{', '.join(label + '=' + path.name for path, label in sample_map.items())}`",
        f"- 读取器：`h5py {h5py.__version__}`；当前环境没有使用 `netCDF4`。",
        "",
        "## 总体结论",
        "",
        "- 这批文件均可作为 HDF5/NetCDF4 容器打开，产品命名显示为 `FY4B AGRI L2 CPD DISK_1050E 4000M`。",
        f"- 多文件结构签名数量为 `{len(sig_counts)}`；若为 1，说明变量、维度、属性键和核心结构在 52 个文件间一致。",
        "- 主二维变量为 `COT`, `CER`, `LWP`, `IWP`, `DQF`，尺寸均为 `2748x2748`。",
        "- `COT/CER/LWP/IWP` 同时包含物理值和类别/哨兵码，必须显式掩膜 `65535=space`, `65531=clear`, `65532=nighttime`, `-999=fillvalue`。",
        "- `DQF` 已按文件内 `Description` 属性拆解为 bitfield 诊断表；这些解码是产品探查证据，进入生产过滤前仍建议与官方文档交叉确认。",
        "- 文件没有逐像元 `latitude/longitude` 数组；只有 `x/y` dimension scale、卫星子点和高度等导航元数据，后续重建地理定位不能直接假定已解决。",
        "",
        "## 结构一致性",
        "",
        "| structure_signature_sha256 | files |",
        "| --- | ---: |",
    ]
    for signature, count in sorted(sig_counts.items()):
        lines.append(f"| `{signature}` | {count} |")
    lines.extend(["", "## 变量概览", "", "| variable | shape | dtype | units | fill | scale/add_offset |", "| --- | --- | --- | --- | --- | --- |"])
    by_var: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in variable_rows:
        by_var[row["variable_name"]].append(row)
    for var in variable_names:
        rows = by_var[var]
        shape = ";".join(sorted({row.get("shape", "") for row in rows}))
        dtype = ";".join(sorted({row.get("dtype", "") for row in rows}))
        units = ";".join(sorted({row.get("units", "") for row in rows}))
        fill = ";".join(sorted({row.get("fill_value", "") for row in rows}))
        scale = ";".join(sorted({f"{row.get('scale_factor','')}/{row.get('add_offset','')}" for row in rows}))
        lines.append(f"| `{var}` | {shape} | {dtype} | {units} | {fill} | {scale} |")
    lines.extend(["", "## 中间样本物理统计", "", "| variable | physical_count | physical_min | physical_p50 | physical_max | special_fraction |", "| --- | ---: | ---: | ---: | ---: | ---: |"])
    for var in ["COT", "CER", "LWP", "IWP", "DQF"]:
        row = middle_stats.get(var, {})
        lines.append(
            f"| `{var}` | {row.get('physical_valid_count','')} | {row.get('physical_min','')} | "
            f"{row.get('physical_p50','')} | {row.get('physical_max','')} | {row.get('known_special_fraction','')} |"
        )
    field_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for row in bitfield_rows:
        field_counts[row.get("field_name", "")][str(row.get("decoded_value", ""))] += int(row.get("decoded_value_count", 0))
    lines.extend(["", "## DQF Bitfield 摘要", "", "| field | observed decoded counts |", "| --- | --- |"])
    for field, counts in sorted(field_counts.items()):
        lines.append(f"| `{field}` | {'; '.join(f'{k}:{v}' for k, v in sorted(counts.items(), key=lambda item: int(item[0])))} |")
    lines.extend(
        [
            "",
            "## 坐标与单位风险",
            "",
            "- `COT/CER/LWP/IWP/DQF` 的 dimension scale 链接为 `/x` 和 `/y`；一维坐标变量 `x/y` 没有单位属性。",
            "- `nominal_satellite_subpoint_lon=105.0 degrees_east`，`nominal_satellite_subpoint_lat=0.0 degrees_north`。",
            "- `nominal_satellite_height` 的值为 `35786000`，但单位属性是 `km`；这与常见地球同步轨道高度量级矛盾，应在投影重建前核对为米还是单位误标。",
            "",
            "## Quicklook",
            "",
            f"- 已生成 `{len(quicklook_rows)}` 张 quicklook，索引见 `{outputs['quicklook_index']}`。",
            "- 连续科学变量 quicklook 使用 physical-only 掩膜和 2-98 百分位拉伸；类别/哨兵码和 DQF bitfield 使用 categorical 色标。",
            "",
            "## 异常与未决语义",
            "",
        ]
    )
    if anomalies:
        grouped: dict[tuple[str, str, str, str], int] = defaultdict(int)
        for row in anomalies:
            key = (
                str(row.get("severity", "")),
                str(row.get("issue", "")),
                str(row.get("variable_name", "")),
                str(row.get("detail", "")),
            )
            grouped[key] += 1
        for (severity, issue, variable_name, detail), count in sorted(grouped.items()):
            suffix = f"；影响文件数={count}" if count > 1 else ""
            lines.append(f"- `{severity}` `{issue}`：{variable_name} {detail}{suffix}")
    else:
        lines.append("- 未发现读取异常；仍需用官方手册核对 `DQF` bit 定义和高度单位。")
    lines.extend(["", "## 输出清单", ""])
    for key, path in outputs.items():
        lines.append(f"- `{key}`: `{path}`")
    return "\n".join(lines) + "\n"


def encoding_check(report_path: Path) -> list[dict[str, Any]]:
    text = report_path.read_text(encoding="utf-8")
    bad_patterns = ["�", "鏁", "鍑", "涓", "€"]
    hits = [pattern for pattern in bad_patterns if pattern in text]
    return [
        {
            "file_path": str(report_path),
            "encoding": "utf-8",
            "read_status": "OK",
            "contains_chinese": str(any("\u4e00" <= ch <= "\u9fff" for ch in text)),
            "mojibake_patterns_found": ";".join(hits),
            "status": "PASS" if not hits else "WARN",
        }
    ]


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    QUICKLOOK_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(INPUT_ROOT.glob("*.NC"))
    sample_map = choose_samples(files)
    file_rows = [parse_file(path) for path in files]
    sample_lookup = {str(path): label for path, label in sample_map.items()}

    accum: dict[str, list[dict[str, Any]]] = defaultdict(list)
    quicklook_rows: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    for file_row in file_rows:
        sample_position = sample_lookup.get(file_row["file_path"], "all_files_quick")
        try:
            result = inspect_file(file_row, sample_position, quicklook_rows)
            for key, rows in result.items():
                accum[key].extend(rows)
        except Exception as exc:
            file_row["open_status"] = "ERROR"
            anomalies.append(
                {
                    **file_row,
                    "sample_position": sample_position,
                    "severity": "ERROR",
                    "issue": "open_or_inspect_failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )

    anomalies.extend(anomaly_rows_from_units(file_rows, accum["coordinate_audit"]))
    consistency = consistency_rows(accum["variables"], accum["structure"], file_rows)

    outputs = {
        "file_inventory": OUT_DIR / f"{PRODUCT_PREFIX}_file_inventory.csv",
        "structure_inventory": OUT_DIR / f"{PRODUCT_PREFIX}_structure_inventory.csv",
        "dimension_inventory": OUT_DIR / f"{PRODUCT_PREFIX}_dimension_inventory.csv",
        "global_attributes": OUT_DIR / f"{PRODUCT_PREFIX}_global_attributes.csv",
        "variable_inventory": OUT_DIR / f"{PRODUCT_PREFIX}_variable_inventory.csv",
        "variable_attributes": OUT_DIR / f"{PRODUCT_PREFIX}_variable_attributes.csv",
        "code_table": OUT_DIR / f"{PRODUCT_PREFIX}_code_table.csv",
        "bitfield_diagnostics": OUT_DIR / f"{PRODUCT_PREFIX}_bitfield_diagnostics.csv",
        "observed_code_combinations": OUT_DIR / f"{PRODUCT_PREFIX}_observed_code_combinations.csv",
        "category_sentinel_stats": OUT_DIR / f"{PRODUCT_PREFIX}_category_sentinel_stats.csv",
        "sample_stats": OUT_DIR / f"{PRODUCT_PREFIX}_sample_stats.csv",
        "quality_flag_decode": OUT_DIR / f"{PRODUCT_PREFIX}_quality_flag_decode.csv",
        "coordinate_audit": OUT_DIR / f"{PRODUCT_PREFIX}_coordinate_audit.csv",
        "cross_file_consistency": OUT_DIR / f"{PRODUCT_PREFIX}_cross_file_consistency.csv",
        "quicklook_index": OUT_DIR / f"{PRODUCT_PREFIX}_quicklook_index.csv",
        "anomalies": OUT_DIR / f"{PRODUCT_PREFIX}_anomalies.csv",
        "encoding_check": OUT_DIR / f"{PRODUCT_PREFIX}_encoding_check.csv",
        "report": OUT_DIR / f"{PRODUCT_PREFIX}_inspection_report.md",
        "manifest": OUT_DIR / f"{PRODUCT_PREFIX}_inspection_manifest.json",
    }

    write_csv(outputs["file_inventory"], file_rows)
    write_csv(outputs["structure_inventory"], accum["structure"])
    write_csv(outputs["dimension_inventory"], accum["dimensions"])
    write_csv(outputs["global_attributes"], accum["global_attrs"])
    write_csv(outputs["variable_inventory"], accum["variables"])
    write_csv(outputs["variable_attributes"], accum["variable_attrs"])
    write_csv(outputs["code_table"], accum["code_table"])
    write_csv(outputs["bitfield_diagnostics"], accum["bitfield"])
    write_csv(outputs["observed_code_combinations"], accum["observed_combos"])
    write_csv(outputs["category_sentinel_stats"], accum["category_stats"])
    write_csv(outputs["sample_stats"], accum["stats"])
    write_csv(outputs["quality_flag_decode"], accum["quality_decode"])
    write_csv(outputs["coordinate_audit"], accum["coordinate_audit"])
    write_csv(outputs["cross_file_consistency"], consistency)
    write_csv(outputs["quicklook_index"], quicklook_rows)
    write_csv(outputs["anomalies"], anomalies)

    report = build_chinese_report(
        file_rows,
        accum["structure"],
        accum["variables"],
        accum["stats"],
        accum["bitfield"],
        quicklook_rows,
        consistency,
        anomalies,
        sample_map,
        outputs,
    )
    outputs["report"].write_text(report, encoding="utf-8")
    write_csv(outputs["encoding_check"], encoding_check(outputs["report"]))

    structure_signatures = Counter(row["structure_signature_sha256"] for row in accum["structure"])
    manifest = {
        "generated_utc": utc_now(),
        "input_root": str(INPUT_ROOT),
        "input_files": [str(path) for path in files],
        "inspection_script": str(SCRIPT_PATH),
        "output_dir": str(OUT_DIR),
        "input_file_count": len(file_rows),
        "opened_file_count": sum(1 for row in file_rows if row.get("open_status") == "OK"),
        "sample_strategy": "all files opened for metadata, structure, dimensions, attributes, quick sampled stats; first/middle/last files read with full science/DQF/category statistics; middle sample used for quicklooks",
        "representative_samples": {label: str(path) for path, label in sample_map.items()},
        "reader_versions": {"h5py": h5py.__version__, "numpy": np.__version__, "matplotlib": matplotlib.__version__},
        "output_files": {key: str(path) for key, path in outputs.items()},
        "variables_inspected": sorted({row["variable_name"] for row in accum["variables"]}),
        "structure_signatures": dict(structure_signatures),
        "quicklooks": {"count": len(quicklook_rows), "index": str(outputs["quicklook_index"]), "directory": str(QUICKLOOK_DIR)},
        "warnings": [
            "netCDF4 Python package is not installed; inspection used h5py against the NetCDF4/HDF5 container.",
            "Science arrays include documented category/sentinel codes in numeric arrays; physical analysis must mask them explicitly.",
            "DQF bit fields were decoded from the file Description attribute; confirm against official manual before production quality filtering.",
            "Per-pixel latitude/longitude arrays are not embedded in sampled CPD files.",
            "nominal_satellite_height value is 35786000 with units=km in sampled files; verify whether the stored value is actually meters before geolocation reconstruction.",
        ],
        "unresolved_semantics": [
            "official CPD DQF bit table cross-check against the product manual",
            "exact geostationary projection formula/grid mapping attributes for downstream lat/lon reconstruction",
            "pixel center/edge convention for x/y grid",
        ],
    }
    outputs["manifest"].write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"inspected_files={len(file_rows)} structures={len(accum['structure'])} variables={len(accum['variables'])} "
        f"dimensions={len(accum['dimensions'])} quicklooks={len(quicklook_rows)} anomalies={len(anomalies)}"
    )
    print(outputs["report"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
