from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

CORE_CODE_ROOT = Path(__file__).resolve().parents[1] / "geo_ring_cloud_stage1"
if str(CORE_CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_CODE_ROOT))

from geo_ring_cloud.paths import CLAAS3_ROOT, THIRD_REPORT_ROOT  # noqa: E402

try:
    import netCDF4
except Exception as exc:  # pragma: no cover
    netCDF4 = exc


ROOT = CLAAS3_ROOT
OUT_DIR = THIRD_REPORT_ROOT / "reports" / "claas3_deep_structure_audit"
MAX_SAMPLE_VALUES = 400_000
MAX_CODE_ROWS = 256
MAX_UNIQUE_PREVIEW = 48
MAX_ATTR_TEXT = 8000

FILE_RE = re.compile(
    r"^(?P<product>CMA|CPP|CTX)in(?P<stamp>\d{14})(?P<version>\d{3})SV(?P<platform>[A-Z0-9]+)\.nc$",
    re.I,
)

STANDARD_MAP = {
    "CMA": {
        "cma": "cloud_mask",
        "cma_prob": "cloud_probability",
        "quality": "quality_flag",
        "status_flag": "status_flag",
        "conditions": "retrieval_conditions",
        "projection": "geostationary_projection",
        "time": "observation_time",
        "x": "projection_x",
        "y": "projection_y",
        "subsatellite_alt": "satellite_height",
        "subsatellite_lon": "satellite_subpoint_longitude",
        "subsatellite_lat": "satellite_subpoint_latitude",
    },
    "CTX": {
        "cth": "cloud_top_height",
        "ctp": "cloud_top_pressure",
        "ctt": "cloud_top_temperature",
        "cth_unc": "cloud_top_height_uncertainty",
        "ctp_unc": "cloud_top_pressure_uncertainty",
        "ctt_unc": "cloud_top_temperature_uncertainty",
        "quality": "quality_flag",
        "status_flag": "status_flag",
        "conditions": "retrieval_conditions",
        "projection": "geostationary_projection",
        "time": "observation_time",
        "x": "projection_x",
        "y": "projection_y",
        "subsatellite_alt": "satellite_height",
        "subsatellite_lon": "satellite_subpoint_longitude",
        "subsatellite_lat": "satellite_subpoint_latitude",
    },
    "CPP": {
        "cph": "cloud_phase",
        "cph_16": "cloud_phase_alt",
        "cph_ext": "cloud_phase_extended",
        "cph_16_ext": "cloud_phase_extended_alt",
        "cot": "cloud_optical_thickness",
        "cot_16": "cloud_optical_thickness_alt",
        "cre": "cloud_effective_radius",
        "cre_16": "cloud_effective_radius_alt",
        "cwp": "cloud_water_path",
        "cwp_16": "cloud_water_path_alt",
        "processing_flag": "processing_flag",
        "processing_flag_16": "processing_flag_alt",
        "cdnc": "cloud_droplet_number_concentration",
        "cgt": "cloud_geometrical_thickness_or_related",
        "projection": "geostationary_projection",
        "time": "observation_time",
        "x": "projection_x",
        "y": "projection_y",
        "subsatellite_alt": "satellite_height",
        "subsatellite_lon": "satellite_subpoint_longitude",
        "subsatellite_lat": "satellite_subpoint_latitude",
    },
}

PRIMARY_PRODUCT_VARIABLES = {
    "CMA": ["cma", "cma_prob", "quality", "status_flag", "conditions"],
    "CTX": ["cth", "ctp", "ctt", "quality", "status_flag", "conditions"],
    "CPP": ["cph", "cot", "cre", "cwp", "processing_flag", "processing_flag_16"],
}


@dataclass(frozen=True)
class ParsedName:
    file_name: str
    product: str
    nominal_time: str
    version_token: str
    platform_token: str


def log(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def ensure_out() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)


def to_builtin(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.generic,)):
        return value.item()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    if isinstance(value, Path):
        return str(value)
    return value


def json_text(value: Any) -> str:
    text = json.dumps(to_builtin(value), ensure_ascii=False, default=str)
    return text[:MAX_ATTR_TEXT]


def parse_name(path: Path) -> ParsedName | None:
    m = FILE_RE.match(path.name)
    if not m:
        return None
    stamp = datetime.strptime(m.group("stamp"), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    return ParsedName(
        file_name=path.name,
        product=m.group("product").upper(),
        nominal_time=stamp.isoformat().replace("+00:00", "Z"),
        version_token=m.group("version"),
        platform_token=m.group("platform"),
    )


def discover_files() -> dict[str, list[Path]]:
    found: dict[str, list[Path]] = defaultdict(list)
    for path in ROOT.rglob("*.nc"):
        parsed = parse_name(path)
        if parsed:
            found[parsed.product].append(path)
    for product in found:
        found[product] = sorted(found[product])
    return dict(found)


def choose_samples(paths: list[Path]) -> list[Path]:
    if not paths:
        return []
    idxs = sorted({0, len(paths) // 2, len(paths) - 1})
    return [paths[i] for i in idxs]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields: list[str] = []
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
            writer.writerow({field: row.get(field, "") for field in fields})


def write_md(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8-sig")


def short_shape(shape: tuple[int, ...]) -> str:
    return "x".join(str(v) for v in shape) if shape else ""


def maybe_fill_values(attrs: dict[str, Any]) -> list[Any]:
    values = []
    for key in ("_FillValue", "missing_value", "fill_value"):
        if key in attrs:
            val = to_builtin(attrs[key])
            if isinstance(val, list):
                values.extend(val)
            else:
                values.append(val)
    dedup: list[Any] = []
    for val in values:
        if val not in dedup:
            dedup.append(val)
    return dedup


def sample_flat(data: np.ndarray) -> np.ndarray:
    flat = np.asarray(data).reshape(-1)
    if flat.size > MAX_SAMPLE_VALUES:
        step = max(1, flat.size // MAX_SAMPLE_VALUES)
        flat = flat[::step][:MAX_SAMPLE_VALUES]
    return flat


def finite_mask_for(values: np.ndarray) -> np.ndarray:
    if values.dtype.kind in {"f", "c"}:
        return np.isfinite(values.astype("float64", copy=False))
    return np.ones(values.shape, dtype=bool)


def normalize_scalar(v: Any) -> Any:
    v = to_builtin(v)
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN"
        if math.isinf(v):
            return "Inf" if v > 0 else "-Inf"
        return float(v)
    return v


def unique_preview(values: np.ndarray, max_items: int = MAX_UNIQUE_PREVIEW) -> tuple[str, int, list[tuple[Any, int]]]:
    flat = sample_flat(values)
    if flat.dtype.kind == "f":
        flat = flat[np.isfinite(flat)]
    if flat.size == 0:
        return "", 0, []
    uniq, counts = np.unique(flat, return_counts=True)
    pairs = [(normalize_scalar(v), int(c)) for v, c in zip(uniq.tolist(), counts.tolist())]
    pairs.sort(key=lambda item: (-item[1], str(item[0])))
    text = "; ".join(f"{v}:{c}" for v, c in pairs[:max_items])
    return text, len(pairs), pairs


def likely_flag_var(name: str, attrs: dict[str, Any], dtype: np.dtype) -> bool:
    low = name.lower()
    if any(token in low for token in ("flag", "quality", "condition", "status", "record_status", "platform_flag")):
        return True
    if "flag_values" in attrs or "flag_meanings" in attrs or "flag_masks" in attrs:
        return True
    return dtype.kind in {"i", "u"} and low in {"cma", "cph", "cph_16", "cph_ext", "cph_16_ext"}


def enum_rows_from_attrs(
    product: str,
    file_name: str,
    var_name: str,
    attrs: dict[str, Any],
    observed_counter: Counter[Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    flag_values = to_builtin(attrs.get("flag_values", []))
    flag_meanings = str(to_builtin(attrs.get("flag_meanings", ""))).split()
    flag_masks = to_builtin(attrs.get("flag_masks", []))
    if flag_values and isinstance(flag_values, list):
        for idx, value in enumerate(flag_values):
            rows.append(
                {
                    "product": product,
                    "file_name": file_name,
                    "variable_name": var_name,
                    "table_type": "explicit_flag_values",
                    "code_value": normalize_scalar(value),
                    "meaning": flag_meanings[idx] if idx < len(flag_meanings) else "",
                    "mask": flag_masks[idx] if isinstance(flag_masks, list) and idx < len(flag_masks) else "",
                    "observed_count_in_sample": observed_counter.get(normalize_scalar(value), 0),
                }
            )
    return rows


def decode_observed_meanings(value: int, flag_values: list[int], flag_masks: list[int], flag_meanings: list[str]) -> str:
    decoded: list[str] = []
    for idx, flag_value in enumerate(flag_values):
        mask = flag_masks[idx] if idx < len(flag_masks) else None
        meaning = flag_meanings[idx] if idx < len(flag_meanings) else ""
        try:
            fv = int(flag_value)
        except Exception:
            continue
        if mask is None:
            if value == fv and meaning:
                decoded.append(meaning)
            continue
        try:
            m = int(mask)
        except Exception:
            continue
        if (value & m) == fv and meaning:
            decoded.append(meaning)
    return " | ".join(decoded)


def flag_mask_rows(
    product: str,
    file_name: str,
    var_name: str,
    attrs: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    flag_values = to_builtin(attrs.get("flag_values", []))
    flag_masks = to_builtin(attrs.get("flag_masks", []))
    flag_meanings = str(to_builtin(attrs.get("flag_meanings", ""))).split()
    if not isinstance(flag_values, list):
        return rows
    if not isinstance(flag_masks, list):
        flag_masks = []
    for idx, value in enumerate(flag_values):
        mask = flag_masks[idx] if idx < len(flag_masks) else ""
        meaning = flag_meanings[idx] if idx < len(flag_meanings) else ""
        try:
            mask_int = int(mask) if mask != "" else None
        except Exception:
            mask_int = None
        bit_indexes = []
        if mask_int is not None:
            bit_indexes = [bit for bit in range(32) if (mask_int & (1 << bit)) != 0]
        rows.append(
            {
                "product": product,
                "file_name": file_name,
                "variable_name": var_name,
                "flag_value": normalize_scalar(value),
                "flag_mask": mask if mask != "" else "",
                "flag_mask_hex": hex(mask_int) if mask_int is not None else "",
                "flag_bits": ",".join(str(bit) for bit in bit_indexes),
                "meaning": meaning,
            }
        )
    return rows


def bit_rows(
    product: str,
    file_name: str,
    var_name: str,
    values: np.ndarray,
    attrs: dict[str, Any],
) -> list[dict[str, Any]]:
    if values.dtype.kind not in {"i", "u"}:
        return []
    flat = sample_flat(values)
    fill_values = {normalize_scalar(v) for v in maybe_fill_values(attrs)}
    valid_mask = np.ones(flat.shape, dtype=bool)
    for fill_val in fill_values:
        valid_mask &= flat != fill_val
    flat = flat[valid_mask]
    if flat.size == 0:
        return []
    max_observed = int(np.max(flat))
    max_bits = max(flat.dtype.itemsize * 8, max_observed.bit_length())
    max_bits = min(max_bits, 32)
    rows: list[dict[str, Any]] = []
    for bit in range(max_bits):
        bit_mask = 1 << bit
        set_count = int(np.count_nonzero((flat.astype("uint64") & bit_mask) != 0))
        if set_count == 0:
            continue
        rows.append(
            {
                "product": product,
                "file_name": file_name,
                "variable_name": var_name,
                "bit_index": bit,
                "bit_mask_decimal": bit_mask,
                "bit_mask_hex": hex(bit_mask),
                "sample_valid_count": int(flat.size),
                "bit_set_count": set_count,
                "bit_set_ratio": float(set_count / flat.size),
                "fill_values": json_text(maybe_fill_values(attrs)),
            }
        )
    return rows


def safe_dataset_attrs(obj: Any) -> dict[str, Any]:
    return {attr: to_builtin(getattr(obj, attr)) for attr in obj.ncattrs()}


def structure_signature(ds: Any) -> str:
    parts: list[str] = []
    for name in sorted(ds.variables):
        var = ds.variables[name]
        parts.append(
            f"{name}|{str(var.dtype)}|{','.join(var.dimensions)}|{short_shape(var.shape)}|{','.join(sorted(var.ncattrs()))}"
        )
    text = "||".join(parts)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def inspect_variable(
    product: str,
    file_name: str,
    var_name: str,
    var: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    attrs = safe_dataset_attrs(var)
    row: dict[str, Any] = {
        "product": product,
        "file_name": file_name,
        "variable_name": var_name,
        "standard_guess": STANDARD_MAP.get(product, {}).get(var_name, ""),
        "dtype": str(var.dtype),
        "dimensions": json_text(list(var.dimensions)),
        "shape": short_shape(var.shape),
        "ndim": len(var.shape),
        "units": attrs.get("units", ""),
        "long_name": attrs.get("long_name", ""),
        "standard_name": attrs.get("standard_name", ""),
        "coordinates": attrs.get("coordinates", ""),
        "grid_mapping": attrs.get("grid_mapping", ""),
        "cell_methods": attrs.get("cell_methods", ""),
        "scale_factor": attrs.get("scale_factor", ""),
        "add_offset": attrs.get("add_offset", ""),
        "valid_min": attrs.get("valid_min", ""),
        "valid_max": attrs.get("valid_max", ""),
        "valid_range": json_text(attrs.get("valid_range", "")),
        "fill_values": json_text(maybe_fill_values(attrs)),
        "flag_values": json_text(attrs.get("flag_values", "")),
        "flag_meanings": attrs.get("flag_meanings", ""),
        "flag_masks": json_text(attrs.get("flag_masks", "")),
        "all_attributes_json": json_text(attrs),
    }
    code_rows: list[dict[str, Any]] = []
    bitfield_rows: list[dict[str, Any]] = []
    attr_rows: list[dict[str, Any]] = []
    mask_meaning_rows: list[dict[str, Any]] = []
    observed_combo_rows: list[dict[str, Any]] = []
    for attr_name, attr_value in attrs.items():
        attr_rows.append(
            {
                "product": product,
                "file_name": file_name,
                "variable_name": var_name,
                "attribute_name": attr_name,
                "attribute_value_json": json_text(attr_value),
            }
        )

    if len(var.shape) == 0 or np.dtype(var.dtype).kind in {"S", "U", "O"}:
        return row, code_rows, bitfield_rows, attr_rows, mask_meaning_rows, observed_combo_rows

    raw_data = np.asarray(var[:])
    flat = sample_flat(raw_data)
    row["sample_count"] = int(flat.size)
    finite_mask = finite_mask_for(flat)
    row["invalid_ratio"] = float(np.mean(~finite_mask)) if flat.size else ""

    fill_values = maybe_fill_values(attrs)
    fill_ratio = ""
    if flat.size and fill_values:
        fill_mask = np.zeros(flat.shape, dtype=bool)
        for fill_val in fill_values:
            try:
                fill_mask |= flat == fill_val
            except Exception:
                pass
        fill_ratio = float(np.mean(fill_mask))
    row["fill_ratio"] = fill_ratio

    if np.dtype(flat.dtype).kind in {"i", "u", "f"}:
        work = flat[finite_mask]
        if work.size:
            row["sample_min"] = float(np.nanmin(work.astype("float64", copy=False)))
            row["sample_max"] = float(np.nanmax(work.astype("float64", copy=False)))
            row["sample_mean"] = float(np.nanmean(work.astype("float64", copy=False)))
            row["sample_std"] = float(np.nanstd(work.astype("float64", copy=False)))
    preview_text, uniq_count, preview_pairs = unique_preview(raw_data)
    row["unique_value_count"] = uniq_count
    row["unique_values_preview"] = preview_text
    observed_counter = Counter()
    for value, count in preview_pairs[:MAX_CODE_ROWS]:
        observed_counter[value] = count

    if likely_flag_var(var_name, attrs, np.dtype(var.dtype)):
        code_rows.extend(enum_rows_from_attrs(product, file_name, var_name, attrs, observed_counter))
        mask_meaning_rows.extend(flag_mask_rows(product, file_name, var_name, attrs))
        if uniq_count and uniq_count <= MAX_CODE_ROWS:
            flag_values = to_builtin(attrs.get("flag_values", []))
            flag_masks = to_builtin(attrs.get("flag_masks", []))
            flag_meanings = str(to_builtin(attrs.get("flag_meanings", ""))).split()
            flag_values = flag_values if isinstance(flag_values, list) else []
            flag_masks = flag_masks if isinstance(flag_masks, list) else []
            for value, count in preview_pairs[:MAX_CODE_ROWS]:
                code_rows.append(
                    {
                        "product": product,
                        "file_name": file_name,
                        "variable_name": var_name,
                        "table_type": "observed_unique_values",
                        "code_value": value,
                        "meaning": "",
                        "mask": "",
                        "observed_count_in_sample": count,
                    }
                )
                if isinstance(value, (int, float)) and flag_values:
                    try:
                        ivalue = int(value)
                    except Exception:
                        ivalue = None
                    if ivalue is not None:
                        observed_combo_rows.append(
                            {
                                "product": product,
                                "file_name": file_name,
                                "variable_name": var_name,
                                "observed_value": ivalue,
                                "observed_count_in_sample": count,
                                "decoded_meanings": decode_observed_meanings(ivalue, [int(v) for v in flag_values], [int(v) for v in flag_masks] if flag_masks else [], flag_meanings),
                            }
                        )
        bitfield_rows.extend(bit_rows(product, file_name, var_name, raw_data, attrs))

    return row, code_rows, bitfield_rows, attr_rows, mask_meaning_rows, observed_combo_rows


def run() -> None:
    if isinstance(netCDF4, Exception):
        raise RuntimeError(f"netCDF4 import failed: {netCDF4}")

    ensure_out()
    files = discover_files()
    if not files:
        raise RuntimeError(f"No CLAAS-3 NetCDF files found under {ROOT}")

    file_inventory: list[dict[str, Any]] = []
    sample_files: list[Path] = []
    structure_rows: list[dict[str, Any]] = []
    global_attr_rows: list[dict[str, Any]] = []
    dimension_rows: list[dict[str, Any]] = []
    variable_rows: list[dict[str, Any]] = []
    code_rows: list[dict[str, Any]] = []
    bitfield_rows: list[dict[str, Any]] = []
    variable_attr_rows: list[dict[str, Any]] = []
    mask_meaning_rows: list[dict[str, Any]] = []
    observed_combo_rows: list[dict[str, Any]] = []
    signature_counter: dict[str, Counter[str]] = defaultdict(Counter)
    sample_paths_by_product: dict[str, list[Path]] = {}

    log("Scanning file inventory")
    for product, product_files in sorted(files.items()):
        chosen = choose_samples(product_files)
        sample_paths_by_product[product] = chosen
        for path in product_files:
            parsed = parse_name(path)
            assert parsed is not None
            file_inventory.append(
                {
                    "product": product,
                    "file_name": path.name,
                    "file_path": str(path),
                    "directory": path.parent.name,
                    "nominal_time": parsed.nominal_time,
                    "version_token": parsed.version_token,
                    "platform_token": parsed.platform_token,
                    "file_size_bytes": path.stat().st_size,
                    "is_sampled_for_deep_audit": str(path in chosen),
                }
            )
        sample_files.extend(chosen)

    log(f"Deep-inspecting {len(sample_files)} sample files")
    for path in sample_files:
        parsed = parse_name(path)
        assert parsed is not None
        with netCDF4.Dataset(path, "r") as ds:
            try:
                ds.set_auto_mask(False)
            except Exception:
                pass
            signature = structure_signature(ds)
            signature_counter[parsed.product][signature] += 1
            structure_rows.append(
                {
                    "product": parsed.product,
                    "file_name": path.name,
                    "file_path": str(path),
                    "nominal_time": parsed.nominal_time,
                    "structure_signature": signature,
                    "variable_count": len(ds.variables),
                    "dimension_count": len(ds.dimensions),
                    "global_attribute_count": len(ds.ncattrs()),
                    "primary_variables_present": ";".join(
                        name for name in PRIMARY_PRODUCT_VARIABLES.get(parsed.product, []) if name in ds.variables
                    ),
                }
            )
            for attr_name in ds.ncattrs():
                global_attr_rows.append(
                    {
                        "product": parsed.product,
                        "file_name": path.name,
                        "attribute_name": attr_name,
                        "attribute_value_json": json_text(getattr(ds, attr_name)),
                    }
                )
            for dim_name, dim in ds.dimensions.items():
                dimension_rows.append(
                    {
                        "product": parsed.product,
                        "file_name": path.name,
                        "dimension_name": dim_name,
                        "length": len(dim),
                        "isunlimited": str(dim.isunlimited()),
                    }
                )
            for var_name in ds.variables:
                row, c_rows, b_rows, a_rows, m_rows, o_rows = inspect_variable(parsed.product, path.name, var_name, ds.variables[var_name])
                variable_rows.append(row)
                code_rows.extend(c_rows)
                bitfield_rows.extend(b_rows)
                variable_attr_rows.extend(a_rows)
                mask_meaning_rows.extend(m_rows)
                observed_combo_rows.extend(o_rows)

    signature_summary_rows: list[dict[str, Any]] = []
    for product, counter in sorted(signature_counter.items()):
        total = sum(counter.values())
        for signature, count in counter.items():
            signature_summary_rows.append(
                {
                    "product": product,
                    "structure_signature": signature,
                    "sample_file_count_with_signature": count,
                    "sample_file_fraction": float(count / total) if total else "",
                }
            )

    product_summary_rows: list[dict[str, Any]] = []
    for product, product_files in sorted(files.items()):
        parsed_times = [parse_name(path).nominal_time for path in product_files if parse_name(path)]
        dirs = sorted({path.parent.name for path in product_files})
        product_summary_rows.append(
            {
                "product": product,
                "downloaded_file_count": len(product_files),
                "downloaded_directory_count": len(dirs),
                "directories": "; ".join(dirs),
                "first_nominal_time": parsed_times[0] if parsed_times else "",
                "last_nominal_time": parsed_times[-1] if parsed_times else "",
                "sampled_file_count": len(sample_paths_by_product.get(product, [])),
                "sampled_file_names": "; ".join(path.name for path in sample_paths_by_product.get(product, [])),
                "sampled_structure_signature_count": len(signature_counter.get(product, {})),
            }
        )

    write_csv(OUT_DIR / "claas3_file_inventory.csv", file_inventory)
    write_csv(OUT_DIR / "claas3_product_summary.csv", product_summary_rows)
    write_csv(OUT_DIR / "claas3_structure_signature_summary.csv", signature_summary_rows)
    write_csv(OUT_DIR / "claas3_sample_structure_rows.csv", structure_rows)
    write_csv(OUT_DIR / "claas3_sample_global_attributes.csv", global_attr_rows)
    write_csv(OUT_DIR / "claas3_sample_dimensions.csv", dimension_rows)
    write_csv(OUT_DIR / "claas3_sample_variable_inventory_full.csv", variable_rows)
    write_csv(OUT_DIR / "claas3_sample_variable_attributes_full.csv", variable_attr_rows)
    write_csv(OUT_DIR / "claas3_flag_code_tables.csv", code_rows)
    write_csv(OUT_DIR / "claas3_flag_mask_meaning_map.csv", mask_meaning_rows)
    write_csv(OUT_DIR / "claas3_flag_observed_combinations.csv", observed_combo_rows)
    write_csv(OUT_DIR / "claas3_flag_bitfield_diagnostics.csv", bitfield_rows)

    product_vars: dict[str, list[str]] = defaultdict(list)
    for row in variable_rows:
        if row["variable_name"] not in product_vars[row["product"]]:
            product_vars[row["product"]].append(row["variable_name"])

    report_lines = [
        "# CLAAS-3 数据结构深度审计报告",
        "",
        f"- 生成时间: {datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}",
        f"- 数据根目录: `{ROOT}`",
        f"- 输出目录: `{OUT_DIR}`",
        "",
        "## 一、当前本地数据概况",
        "",
        "| 产品 | 已下载文件数 | 采样深查文件数 | 首时次 | 末时次 | 采样结构签名数 |",
        "|---|---:|---:|---|---|---:|",
    ]
    for row in product_summary_rows:
        report_lines.append(
            f"| {row['product']} | {row['downloaded_file_count']} | {row['sampled_file_count']} | "
            f"{row['first_nominal_time']} | {row['last_nominal_time']} | {row['sampled_structure_signature_count']} |"
        )
    report_lines.extend(
        [
            "",
            "## 二、每个产品到底包含什么",
            "",
        ]
    )
    for product in sorted(product_vars):
        report_lines.append(f"### {product}")
        report_lines.append("")
        report_lines.append(f"- 原始变量: `{'; '.join(sorted(product_vars[product]))}`")
        mapped = []
        for raw_name, std_name in sorted(STANDARD_MAP.get(product, {}).items()):
            if raw_name in product_vars[product]:
                mapped.append(f"`{raw_name}` -> `{std_name}`")
        if mapped:
            report_lines.append(f"- 标准变量建议映射: {'; '.join(mapped)}")
        key_rows = [r for r in variable_rows if r["product"] == product and r["variable_name"] in PRIMARY_PRODUCT_VARIABLES.get(product, [])]
        for row in key_rows:
            report_lines.append(
                f"- `{row['variable_name']}`: shape `{row['shape']}`, dtype `{row['dtype']}`, units `{row['units']}`, "
                f"fill `{row['fill_values']}`, scale `{row['scale_factor']}`, offset `{row['add_offset']}`, "
                f"unique `{row['unique_values_preview']}`"
            )
        report_lines.append("")
    report_lines.extend(
        [
            "## 三、flag / code / bit 级别发现",
            "",
            "- 详细枚举和显式 code table 请看 `claas3_flag_code_tables.csv`。",
            "- 疑似 bitfield 的逐位统计请看 `claas3_flag_bitfield_diagnostics.csv`。",
            "- 全变量属性全集请看 `claas3_sample_variable_attributes_full.csv`。",
            "",
            "重点提醒：",
            "- `CMA` 的 `cma` / `quality` / `status_flag` / `conditions` 需要结合文件属性中的 `flag_values`、`flag_meanings` 与样本唯一值一起解释。",
            "- `CTX` 的 `quality` / `status_flag` / `conditions` 很可能包含检索状态或质量编码，不能只把它们当普通连续数。",
            "- `CPP` 的 `processing_flag` / `processing_flag_16` 是本次最需要逐位核查的质量变量；这份审计已经把每一位是否出现、出现比例导出来了。",
            "",
            "## 四、投影、时间、平台与几何元数据",
            "",
            "- 全局属性请看 `claas3_sample_global_attributes.csv`。",
            "- 维度表请看 `claas3_sample_dimensions.csv`。",
            "- 变量属性里的 `grid_mapping / coordinates / units / scale_factor / add_offset / valid_range / _FillValue` 已完整展开在 `claas3_sample_variable_inventory_full.csv` 和 `claas3_sample_variable_attributes_full.csv`。",
            "",
            "## 五、你现在最该看的几个文件",
            "",
            "1. `claas3_sample_variable_inventory_full.csv`: 每个样本文件、每个变量的一张总表。",
            "2. `claas3_sample_variable_attributes_full.csv`: 所有属性逐条展开，适合追 `flag_meanings`、`comment`、`ancillary_variables`。",
            "3. `claas3_flag_code_tables.csv`: 显式 code table 和样本唯一值表。",
            "4. `claas3_flag_mask_meaning_map.csv`: `flag_masks + flag_values + flag_meanings` 的逐条拆解表。",
            "5. `claas3_flag_observed_combinations.csv`: 样本里真实出现过的组合值及其 meaning 解码。",
            "6. `claas3_flag_bitfield_diagnostics.csv`: 逐位统计，适合看 `processing_flag` / `quality` / `status_flag`。",
            "7. `claas3_product_summary.csv`: 本地下载覆盖概况和采样范围。",
        ]
    )
    write_md(OUT_DIR / "claas3_deep_structure_audit_report.md", "\n".join(report_lines))
    log(f"Outputs written to {OUT_DIR}")


if __name__ == "__main__":
    run()
