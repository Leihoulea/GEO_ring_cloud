from __future__ import annotations

import gc
import hashlib
import json
import math
import os
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib
import numpy as np
import pandas as pd
import yaml

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, ListedColormap

from path_config import DATA_CHECK_GEOMETRY_ROOT, DATA_CHECK_ROOT, HIMAWARI_R21_DIR, STAGE_ROOT

REPORT_ROOT = DATA_CHECK_ROOT
GEOM_AUDIT_ROOT = DATA_CHECK_GEOMETRY_ROOT

PARSED_METADATA = REPORT_ROOT / "parsed_file_metadata.csv"
VARIABLE_INVENTORY = GEOM_AUDIT_ROOT / "product_variable_inventory_full.csv"
MAPPING_YAML = REPORT_ROOT / "manual_variable_mapping_by_product.yaml"

SCRIPT_DIR = STAGE_ROOT / "scripts"
CONFIG_DIR = STAGE_ROOT / "config"
TIME_INDEX_DIR = STAGE_ROOT / "time_index"
NATIVE_DIR = STAGE_ROOT / "standardized_native"
QUICKLOOK_DIR = STAGE_ROOT / "quicklooks_native"
REPORT_DIR = STAGE_ROOT / "reports"

STANDARD_VARS = [
    "cloud_mask",
    "cloud_probability",
    "cloud_type",
    "cloud_phase",
    "cloud_top_height_km",
    "cloud_top_temperature_K",
    "cloud_top_pressure_hPa",
    "cloud_optical_thickness",
    "cloud_effective_radius_um",
    "cloud_water_path_g_m2",
    "quality_flag_raw",
    "quality_flag_standard",
    "latitude",
    "longitude",
    "projection_x",
    "projection_y",
    "sensor_zenith_angle",
    "sensor_azimuth_angle",
    "solar_zenith_angle",
    "solar_azimuth_angle",
    "relative_azimuth_angle",
    "sun_glint_angle",
    "valid_mask",
    "variable_availability",
]

QUICKLOOK_PRIORITY = [
    "cloud_mask",
    "cloud_top_height_km",
    "cloud_top_temperature_K",
    "cloud_top_pressure_hPa",
    "cloud_probability",
    "cloud_phase",
    "cloud_type",
    "cloud_optical_thickness",
    "cloud_effective_radius_um",
    "cloud_water_path_g_m2",
    "sensor_zenith_angle",
    "solar_zenith_angle",
]

CATEGORICAL_VARS = {
    "cloud_mask",
    "cloud_type",
    "cloud_phase",
    "quality_flag_raw",
    "quality_flag_standard",
    "valid_mask",
}

CATEGORY_FILL_VALUES = {
    "cloud_mask": {-128, 126, 127, 255},
    "cloud_type": {-128, 126, 127, 255},
    "cloud_phase": {-128, 126, 127, 255},
    "quality_flag_raw": {-128, 126, 127, 255, 32767, 65535},
    "quality_flag_standard": {-128, 126, 127, 255},
    "valid_mask": {-128, 126, 127, 255},
}


CLOUD_MASK_CODE_TABLES: dict[tuple[str, str], dict[int, dict[str, Any]]] = {
    ("FY4B", "CLM"): {
        0: {"meaning": "cloud", "is_cloudy": True, "is_clear": False, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "FY4B CLM PDF Description"},
        1: {"meaning": "probably_cloud", "is_cloudy": False, "is_clear": False, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "FY4B CLM PDF Description"},
        2: {"meaning": "probably_clear", "is_cloudy": False, "is_clear": False, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "FY4B CLM PDF Description"},
        3: {"meaning": "clear", "is_cloudy": False, "is_clear": True, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "FY4B CLM PDF Description"},
        126: {"meaning": "space", "is_cloudy": False, "is_clear": False, "is_off_disc": True, "valid_for_display": True, "valid_for_fusion": False, "meaning_source": "FY4B CLM PDF Description"},
        127: {"meaning": "fillvalue", "is_cloudy": False, "is_clear": False, "is_off_disc": False, "valid_for_display": False, "valid_for_fusion": False, "meaning_source": "FY4B CLM PDF Description"},
    },
    ("GOES", "ACMF"): {
        0: {"meaning": "clear_or_probably_clear", "is_cloudy": False, "is_clear": True, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "GOES ACMF flag_meanings"},
        1: {"meaning": "cloudy_or_probably_cloudy", "is_cloudy": True, "is_clear": False, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "GOES ACMF flag_meanings"},
        255: {"meaning": "fill_or_off_disc", "is_cloudy": False, "is_clear": False, "is_off_disc": True, "valid_for_display": True, "valid_for_fusion": False, "meaning_source": "GOES ACMF _FillValue plus disk-edge spatial pattern"},
    },
    ("Himawari", "CMSK"): {
        0: {"meaning": "clear", "is_cloudy": False, "is_clear": True, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "Himawari CMSK flag_meanings"},
        1: {"meaning": "probably_clear", "is_cloudy": False, "is_clear": False, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "Himawari CMSK flag_meanings"},
        2: {"meaning": "probably_cloudy", "is_cloudy": False, "is_clear": False, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "Himawari CMSK flag_meanings"},
        3: {"meaning": "cloudy", "is_cloudy": True, "is_clear": False, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "Himawari CMSK flag_meanings"},
        -128: {"meaning": "fill_or_off_earth", "is_cloudy": False, "is_clear": False, "is_off_disc": True, "valid_for_display": True, "valid_for_fusion": False, "meaning_source": "Himawari CMSK _FillValue plus radial off-earth pattern"},
    },
    ("Meteosat", "CLM"): {
        0: {"meaning": "clear_sky_over_water_inferred", "is_cloudy": False, "is_clear": True, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "EUMETSAT CLM abstract plus spatial inference"},
        1: {"meaning": "clear_sky_over_land_inferred", "is_cloudy": False, "is_clear": True, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "EUMETSAT CLM abstract plus spatial inference"},
        2: {"meaning": "cloud_inferred", "is_cloudy": True, "is_clear": False, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "EUMETSAT CLM abstract plus spatial inference"},
        3: {"meaning": "not_processed_off_earth_disc_inferred", "is_cloudy": False, "is_clear": False, "is_off_disc": True, "valid_for_display": True, "valid_for_fusion": False, "meaning_source": "EUMETSAT CLM abstract plus radial edge pattern"},
    },
    ("CLAAS3", "CMA"): {
        0: {"meaning": "clear", "is_cloudy": False, "is_clear": True, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "CLAAS-3 CMA flag_values/flag_meanings"},
        3: {"meaning": "cloudy", "is_cloudy": True, "is_clear": False, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "Canonicalized from CLAAS-3 CMA cloudy code 1"},
        -9999: {"meaning": "fill_or_off_disc", "is_cloudy": False, "is_clear": False, "is_off_disc": True, "valid_for_display": False, "valid_for_fusion": False, "meaning_source": "CLAAS-3 adapter fill policy"},
    },
}


def cloud_mask_semantics(satellite: str, product: str) -> dict[int, dict[str, Any]]:
    sat = str(satellite)
    prod = str(product)
    if sat.startswith("GOES") and ("GOES", prod) in CLOUD_MASK_CODE_TABLES:
        return CLOUD_MASK_CODE_TABLES[("GOES", prod)]
    if sat.startswith("Himawari") and ("Himawari", prod) in CLOUD_MASK_CODE_TABLES:
        return CLOUD_MASK_CODE_TABLES[("Himawari", prod)]
    if sat.startswith("Meteosat") and ("Meteosat", prod) in CLOUD_MASK_CODE_TABLES:
        return CLOUD_MASK_CODE_TABLES[("Meteosat", prod)]
    if sat.startswith("CLAAS3") and ("CLAAS3", prod) in CLOUD_MASK_CODE_TABLES:
        return CLOUD_MASK_CODE_TABLES[("CLAAS3", prod)]
    return CLOUD_MASK_CODE_TABLES.get((sat, prod), {})


def cloud_mask_masks(satellite: str, product: str, arr: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    a = np.asarray(arr)
    table = cloud_mask_semantics(satellite, product)
    if not table:
        base = array_valid_mask("cloud_mask", a)
        if base is None:
            base = np.zeros(a.shape, dtype=bool)
        return base, base, np.zeros(a.shape, dtype=bool)
    display_codes = np.asarray(sorted(code for code, meta in table.items() if meta.get("valid_for_display")), dtype=np.float32)
    fusion_codes = np.asarray(sorted(code for code, meta in table.items() if meta.get("valid_for_fusion")), dtype=np.float32)
    off_disc_codes = np.asarray(sorted(code for code, meta in table.items() if meta.get("is_off_disc")), dtype=np.float32)
    finite = np.isfinite(a) if a.dtype.kind == "f" else np.ones(a.shape, dtype=bool)
    display = finite & np.isin(a.astype(np.float32, copy=False), display_codes)
    fusion = finite & np.isin(a.astype(np.float32, copy=False), fusion_codes)
    off_disc = finite & np.isin(a.astype(np.float32, copy=False), off_disc_codes)
    return display, fusion, off_disc


def cloud_mask_rows(
    satellite: str,
    product: str,
    native_arr: np.ndarray,
    native_attrs: dict[str, Any] | None,
    grid_arr: np.ndarray,
    display_valid_mask: np.ndarray,
) -> list[dict[str, Any]]:
    attrs = native_attrs or {}
    table = cloud_mask_semantics(satellite, product)
    native_vals, native_counts = np.unique(np.asarray(native_arr), return_counts=True)
    native_count_map = {float(v): int(c) for v, c in zip(native_vals, native_counts)}
    grid_vals, grid_counts = np.unique(np.asarray(grid_arr)[np.asarray(display_valid_mask).astype(bool)], return_counts=True)
    grid_count_map = {float(v): int(c) for v, c in zip(grid_vals, grid_counts)}
    all_values = sorted(set(native_count_map) | set(grid_count_map) | {float(k) for k in table})
    rows: list[dict[str, Any]] = []
    for value in all_values:
        meta = table.get(int(value), {})
        rows.append(
            {
                "satellite": satellite,
                "product": product,
                "value": int(value) if float(value).is_integer() else float(value),
                "count_native": native_count_map.get(value, 0),
                "count_reprojected_display": grid_count_map.get(value, 0),
                "metadata_meaning": meta.get("meaning", ""),
                "meaning_source": meta.get("meaning_source", ""),
                "flag_meanings_attr": attrs.get("flag_meanings", ""),
                "flag_values_attr": attrs.get("flag_values", ""),
                "description_attr": attrs.get("Description", attrs.get("description", "")),
                "fill_value_attr": attrs.get("_FillValue", attrs.get("missing_value", "")),
                "is_cloudy": bool(meta.get("is_cloudy", False)),
                "is_clear": bool(meta.get("is_clear", False)),
                "is_off_disc": bool(meta.get("is_off_disc", False)),
                "valid_for_display": bool(meta.get("valid_for_display", False)),
                "valid_for_fusion": bool(meta.get("valid_for_fusion", False)),
            }
        )
    return rows


def reproject_mask_for_use(arrays: dict[str, np.ndarray], variable: str, use: str = "fusion") -> np.ndarray:
    if variable == "cloud_mask":
        if use == "fusion" and "fusion_valid_mask" in arrays:
            return np.asarray(arrays["fusion_valid_mask"]).astype(bool)
        if use == "display" and "display_valid_mask" in arrays:
            return np.asarray(arrays["display_valid_mask"]).astype(bool)
    if "valid_mask" in arrays:
        return np.asarray(arrays["valid_mask"]).astype(bool)
    ref = arrays.get(variable)
    if ref is None:
        return np.zeros((0, 0), dtype=bool)
    return np.ones(np.asarray(ref).shape, dtype=bool)


CORE_PRODUCTS = {
    "FY4B": {
        "family": "FY4B",
        "satellite": "FY4B",
        "core": ["CLM", "CLP", "CLT", "CTH", "CTT", "CTP", "GEO"],
        "optional": ["FDI"],
        "official_unavailable_before": "2024-03-05T00:00:00Z",
    },
    "GOES-16": {
        "family": "GOES",
        "satellite": "GOES-16",
        "core": ["ACMF", "ACHAF", "ACHTF", "CTPF", "ACTPF", "CODF", "CPSF"],
        "optional": [],
    },
    "GOES-18": {
        "family": "GOES",
        "satellite": "GOES-18",
        "core": ["ACMF", "ACHAF", "ACHTF", "CTPF", "ACTPF", "CODF", "CPSF"],
        "optional": [],
    },
    "Himawari-9": {
        "family": "Himawari",
        "satellite": "Himawari-9",
        "core": ["CMSK", "CHGT"],
        "optional": [],
    },
    "Meteosat-0deg": {
        "family": "Meteosat",
        "satellite": "Meteosat-0deg",
        "core": ["CLM", "CTH"],
        "optional": [],
    },
    "Meteosat-IODC": {
        "family": "Meteosat",
        "satellite": "Meteosat-IODC",
        "core": ["CLM", "CTH"],
        "optional": [],
    },
}


PRODUCT_MAPPING_KEYS = {
    ("FY4B", "CLM"): "FY4B_CLM",
    ("FY4B", "CLP"): "FY4B_CLP",
    ("FY4B", "CLT"): "FY4B_CLT",
    ("FY4B", "CTH"): "FY4B_CTH",
    ("FY4B", "CTT"): "FY4B_CTT",
    ("FY4B", "CTP"): "FY4B_CTP",
    ("FY4B", "GEO"): "FY4B_GEO",
    ("GOES", "ACMF"): "GOES_ACMF",
    ("GOES", "ACHAF"): "GOES_ACHAF",
    ("GOES", "ACHTF"): "GOES_ACHTF",
    ("GOES", "CTPF"): "GOES_CTPF",
    ("GOES", "ACTPF"): "GOES_ACTPF",
    ("GOES", "CODF"): "GOES_CODF",
    ("GOES", "CPSF"): "GOES_CPSF",
    ("Himawari", "CMSK"): "Himawari_CMSK",
    ("Himawari", "CHGT"): "Himawari_CHGT",
    ("Meteosat", "CLM"): "Meteosat_CLM",
    ("Meteosat", "CTH"): "Meteosat_CTH",
}


UNIT_TARGETS = {
    "cloud_top_height": "cloud_top_height_km",
    "cloud_top_temperature": "cloud_top_temperature_K",
    "cloud_top_pressure": "cloud_top_pressure_hPa",
    "cloud_effective_radius": "cloud_effective_radius_um",
    "quality_flag": "quality_flag_raw",
}


@dataclass
class ReadResult:
    arrays: dict[str, np.ndarray]
    attrs: dict[str, Any]
    source_variables: dict[str, str]
    warnings: list[str]


def ensure_dirs() -> None:
    for path in [STAGE_ROOT, SCRIPT_DIR, CONFIG_DIR, TIME_INDEX_DIR, NATIVE_DIR, QUICKLOOK_DIR, REPORT_DIR]:
        path.mkdir(parents=True, exist_ok=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_")


def parse_time(value: Any) -> pd.Timestamp | pd.NaT:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return pd.NaT
    text = str(value).strip()
    if not text:
        return pd.NaT
    return pd.to_datetime(text, utc=True, errors="coerce")


def iso_z(ts: pd.Timestamp | datetime) -> str:
    return pd.Timestamp(ts).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def read_mapping() -> dict[str, dict[str, list[str]]]:
    mapping = yaml.safe_load(MAPPING_YAML.read_text(encoding="utf-8-sig"))
    mapping.setdefault("GOES_ACMF", {})
    mapping["GOES_ACMF"]["cloud_mask"] = ["BCM", "ACM", "Cloud_Mask", "cloud_mask"]
    mapping["GOES_ACMF"]["quality_flag"] = ["DQF", "quality_flag"]
    mapping["GOES_ACMF"]["projection_x"] = ["x"]
    mapping["GOES_ACMF"]["projection_y"] = ["y"]
    mapping["GOES_ACMF"]["geostationary_projection"] = ["goes_imager_projection"]
    for key in ["GOES_ACHAF", "GOES_ACHTF", "GOES_CTPF", "GOES_ACTPF", "GOES_CODF", "GOES_CPSF"]:
        mapping.setdefault(key, {})
        mapping[key]["quality_flag"] = ["DQF", "quality_flag"]
        mapping[key]["projection_x"] = ["x"]
        mapping[key]["projection_y"] = ["y"]
        mapping[key]["geostationary_projection"] = ["goes_imager_projection"]
    mapping.setdefault("FY4B_GEO", {})
    mapping["FY4B_GEO"].update(
        {
            "sensor_zenith_angle": ["NOMSatelliteZenith", "SatelliteZenith", "SensorZenith", "VZA"],
            "sensor_azimuth_angle": ["NOMSatelliteAzimuth", "SatelliteAzimuth", "SensorAzimuth", "VAA"],
            "solar_zenith_angle": ["NOMSunZenith", "SolarZenith", "SZA"],
            "solar_azimuth_angle": ["NOMSunAzimuth", "SolarAzimuth", "SAA"],
            "sun_glint_angle": ["NOMSunGlintAngle", "SunGlintAngle"],
            "quality_flag": ["NavQualityFlag", "DQF", "QA", "QualityFlag"],
        }
    )
    mapping.setdefault("Himawari_R21_FLDK", {})
    mapping["Himawari_R21_FLDK"].update(
        {
            "latitude": ["latitude"],
            "longitude": ["longitude"],
            "sensor_zenith_angle": ["SAZ"],
            "sensor_azimuth_angle": ["SAA"],
            "solar_zenith_angle": ["SOZ"],
            "solar_azimuth_angle": ["SOA"],
        }
    )
    return mapping


def product_mapping_key(family: str, product: str) -> str:
    return PRODUCT_MAPPING_KEYS.get((family, product), f"{family}_{product}")


def resolve_variable_names(names: list[str], product_map: dict[str, list[str]]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    norm_names = {name: normalize_name(name.split("/")[-1]) for name in names}
    for standard, candidates in product_map.items():
        target = UNIT_TARGETS.get(standard, standard)
        for candidate in candidates or []:
            cand = normalize_name(str(candidate))
            if not cand or "ifpresent" in cand or "need" in cand:
                continue
            for name, norm in norm_names.items():
                if name in resolved:
                    continue
                if target in resolved.values() and target not in {"projection_x", "projection_y"}:
                    continue
                if len(cand) <= 2 or len(norm) <= 2:
                    matched = cand == norm
                else:
                    matched = cand == norm or cand in norm or norm in cand
                if matched:
                    resolved[name] = target
                    break
    return resolved


def attr_to_python(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    if isinstance(value, np.generic):
        return value.item()
    if hasattr(value, "tolist"):
        return value.tolist()
    return value


def attrs_to_dict(attrs: Any) -> dict[str, Any]:
    if not hasattr(attrs, "items"):
        return {}
    out: dict[str, Any] = {}
    for key, value in attrs.items():
        try:
            out[str(key)] = attr_to_python(value)
        except Exception:
            out[str(key)] = str(value)
    return out


def parse_himawari_r21_time(path: Path) -> pd.Timestamp | pd.NaT:
    match = re.search(r"NC_H\d{2}_(\d{8})_(\d{4})_R21_FLDK", path.name)
    if not match:
        return pd.NaT
    return pd.to_datetime(f"{match.group(1)} {match.group(2)}", format="%Y%m%d %H%M", utc=True, errors="coerce")


def find_himawari_r21_geometry_file(target_time: str | pd.Timestamp) -> tuple[Path | None, dict[str, Any]]:
    target_ts = parse_time(target_time)
    if pd.isna(target_ts):
        return None, {"status": "invalid_target_time"}
    files = sorted(HIMAWARI_R21_DIR.glob("NC_H09_*_R21_FLDK*.nc"))
    if not files:
        return None, {"status": "no_r21_files"}
    candidates: list[dict[str, Any]] = []
    for path in files:
        ts = parse_himawari_r21_time(path)
        if pd.isna(ts):
            continue
        dt_minutes = abs((ts - target_ts).total_seconds()) / 60.0
        same_minute = int(ts.hour == target_ts.hour and ts.minute == target_ts.minute)
        same_day = int(ts.date() == target_ts.date())
        candidates.append(
            {
                "path": path,
                "time": ts,
                "dt_minutes": dt_minutes,
                "same_minute": same_minute,
                "same_day": same_day,
            }
        )
    if not candidates:
        return None, {"status": "no_parseable_r21_files"}
    candidates.sort(key=lambda x: (-x["same_minute"], -x["same_day"], x["dt_minutes"], str(x["path"])))
    best = candidates[0]
    return best["path"], {
        "status": "ok",
        "target_time": iso_z(target_ts),
        "selected_time": iso_z(best["time"]),
        "same_day": bool(best["same_day"]),
        "same_minute": bool(best["same_minute"]),
        "dt_minutes": float(best["dt_minutes"]),
        "file_count": len(candidates),
    }


def read_himawari_r21_geometry(path: Path, mapping: dict[str, dict[str, list[str]]]) -> ReadResult:
    result = read_netcdf_product(path, "Himawari", "R21_FLDK", mapping)
    try:
        import netCDF4

        with netCDF4.Dataset(path) as ds:
            if "geometry_parameters" in ds.variables:
                gp = np.asarray(ds.variables["geometry_parameters"][:], dtype=np.float64)
                result.attrs["r21_geometry_parameters"] = gp.tolist()
                result.attrs["r21_geometry_parameters_long_name"] = str(getattr(ds.variables["geometry_parameters"], "long_name", ""))
            if "Hour" in ds.variables:
                result.attrs["r21_hour_attrs"] = {k: attr_to_python(getattr(ds.variables["Hour"], k)) for k in ds.variables["Hour"].ncattrs()}
    except Exception as exc:
        result.warnings.append(f"failed to inspect Himawari R21 geometry extras: {exc}")
    return result


def variable_to_array(var: Any) -> np.ndarray:
    try:
        arr = np.asarray(var[:])
    except Exception:
        if hasattr(var, "set_auto_maskandscale"):
            var.set_auto_maskandscale(False)
        elif hasattr(var, "set_auto_mask"):
            var.set_auto_mask(False)
        arr = np.asarray(var[:])
    if np.ma.isMaskedArray(arr):
        arr = arr.astype(np.float32).filled(np.nan)
    if arr.dtype.kind in "iu":
        return arr
    return arr.astype(np.float32, copy=False)


def convert_units(name: str, arr: np.ndarray, attrs: dict[str, Any]) -> np.ndarray:
    units = str(attrs.get("units", "")).strip().lower()
    out = arr
    if name == "cloud_top_height_km":
        finite = np.isfinite(out) if out.dtype.kind == "f" else np.ones(out.shape, dtype=bool)
        if "m" == units or units in {"meter", "meters"} or (finite.any() and np.nanmax(out.astype(float)) > 1000):
            out = out.astype(np.float32) / 1000.0
        else:
            out = out.astype(np.float32)
    elif name in {"cloud_top_temperature_K", "cloud_top_pressure_hPa", "cloud_optical_thickness", "cloud_effective_radius_um"}:
        out = out.astype(np.float32, copy=False)
    return out


def mask_sentinel_values(arr: np.ndarray) -> np.ndarray:
    if arr.dtype.kind not in "fc":
        return arr
    out = arr.astype(np.float32, copy=True)
    out[np.isclose(out, -999.0) | np.isclose(out, -9999.0) | np.isclose(out, 65535.0)] = np.nan
    return out


def array_valid_mask(name: str, arr: np.ndarray) -> np.ndarray | None:
    a = np.asarray(arr)
    if a.ndim < 2:
        return None
    if a.dtype.kind in "f":
        valid = np.isfinite(a)
        for fill in CATEGORY_FILL_VALUES.get(name, set()):
            valid &= ~np.isclose(a, float(fill))
        return valid
    if a.dtype.kind in "iu":
        valid = np.ones(a.shape, dtype=bool)
        for fill in CATEGORY_FILL_VALUES.get(name, set()):
            valid &= a != fill
        return valid
    return None


def add_valid_and_quality(arrays: dict[str, np.ndarray]) -> None:
    data_candidates = [
        name
        for name in QUICKLOOK_PRIORITY
        if name in arrays and np.asarray(arrays[name]).ndim >= 2 and name not in {"quality_flag_raw"}
    ]
    shape = None
    if data_candidates:
        shape = np.asarray(arrays[data_candidates[0]]).shape
    elif "quality_flag_raw" in arrays and np.asarray(arrays["quality_flag_raw"]).ndim >= 2:
        shape = np.asarray(arrays["quality_flag_raw"]).shape
    if shape is not None:
        base = np.ones(shape, dtype=bool)
        mask_inputs = data_candidates[:]
        if "quality_flag_raw" in arrays and np.asarray(arrays["quality_flag_raw"]).shape == shape:
            mask_inputs.append("quality_flag_raw")
        if not mask_inputs:
            mask_inputs = [name for name, value in arrays.items() if np.asarray(value).shape == shape and np.asarray(value).ndim >= 2]
        for name in mask_inputs:
            mask = array_valid_mask(name, np.asarray(arrays[name]))
            if mask is not None and mask.shape == shape:
                base &= mask
        arrays["valid_mask"] = base.astype(np.uint8)
    if "quality_flag_raw" in arrays and "quality_flag_standard" not in arrays:
        q = np.asarray(arrays["quality_flag_raw"])
        if q.ndim >= 2:
            std = np.where(q == 0, 3, np.where(np.isfinite(q), 1, 0)).astype(np.uint8)
            arrays["quality_flag_standard"] = std


def read_netcdf_product(path: Path, family: str, product: str, mapping: dict[str, dict[str, list[str]]]) -> ReadResult:
    import netCDF4

    arrays: dict[str, np.ndarray] = {}
    source_variables: dict[str, str] = {}
    warnings: list[str] = []
    attrs: dict[str, Any] = {"source_file": str(path), "reader": "netCDF4"}
    key = product_mapping_key(family, product)
    product_map = mapping.get(key, {})
    ds = netCDF4.Dataset(path)
    try:
        attrs.update({f"global_{k}": attr_to_python(getattr(ds, k)) for k in ds.ncattrs()})
        variables: dict[str, Any] = {}

        def walk(group: Any, prefix: str = "") -> None:
            for name, var in group.variables.items():
                variables[f"{prefix}{name}"] = var
            for group_name, child in group.groups.items():
                walk(child, f"{prefix}{group_name}/")

        walk(ds)
        resolved = resolve_variable_names(list(variables), product_map)
        for var_name, standard in resolved.items():
            var = variables[var_name]
            if standard == "geostationary_projection":
                attrs["geostationary_projection_attrs"] = attrs_to_dict(var.__dict__)
                arrays[standard] = np.asarray(0, dtype=np.int32)
                source_variables[standard] = var_name
                continue
            try:
                arr = variable_to_array(var)
                var_attrs = {name: attr_to_python(getattr(var, name)) for name in var.ncattrs()}
                arr = convert_units(standard, mask_sentinel_values(arr), var_attrs)
                arrays[standard] = arr
                source_variables[standard] = var_name
                attrs[f"attrs_{standard}"] = var_attrs
            except Exception as exc:
                warnings.append(f"failed to read {var_name}: {exc}")
    finally:
        ds.close()
    add_valid_and_quality(arrays)
    return ReadResult(arrays=arrays, attrs=attrs, source_variables=source_variables, warnings=warnings)


def read_hdf_product(path: Path, family: str, product: str, mapping: dict[str, dict[str, list[str]]]) -> ReadResult:
    import h5py

    arrays: dict[str, np.ndarray] = {}
    source_variables: dict[str, str] = {}
    warnings: list[str] = []
    attrs: dict[str, Any] = {"source_file": str(path), "reader": "h5py"}
    key = product_mapping_key(family, product)
    product_map = mapping.get(key, {})
    with h5py.File(path, "r") as handle:
        attrs.update({f"global_{k}": attr_to_python(v) for k, v in handle.attrs.items()})
        datasets: dict[str, Any] = {}

        def visitor(name: str, obj: Any) -> None:
            if isinstance(obj, h5py.Dataset):
                datasets[name] = obj

        handle.visititems(visitor)
        resolved = resolve_variable_names(list(datasets), product_map)
        for var_name, standard in resolved.items():
            ds = datasets[var_name]
            try:
                arr = np.asarray(ds[()])
                var_attrs = attrs_to_dict(ds.attrs)
                arr = convert_units(standard, mask_sentinel_values(arr), var_attrs)
                arrays[standard] = arr
                source_variables[standard] = var_name
                attrs[f"attrs_{standard}"] = var_attrs
            except Exception as exc:
                warnings.append(f"failed to read {var_name}: {exc}")
    add_valid_and_quality(arrays)
    return ReadResult(arrays=arrays, attrs=attrs, source_variables=source_variables, warnings=warnings)


def read_meteosat_zip(path: Path, product: str, mapping: dict[str, dict[str, list[str]]]) -> ReadResult:
    arrays: dict[str, np.ndarray] = {}
    source_variables: dict[str, str] = {}
    warnings: list[str] = []
    attrs: dict[str, Any] = {"source_file": str(path), "reader": "zip+cfgrib_cached_extract", "zip_entries": []}
    key = product_mapping_key("Meteosat", product)
    product_map = mapping.get(key, {})
    try:
        import xarray as xr
    except Exception as exc:
        return ReadResult(arrays, attrs, source_variables, [f"xarray/cfgrib unavailable: {exc}"])

    extract_cache = STAGE_ROOT / "cache" / "meteosat_extract"
    extract_cache.mkdir(parents=True, exist_ok=True)
    attrs["extract_cache"] = str(extract_cache)
    with zipfile.ZipFile(path) as zf:
        entries = zf.namelist()
        attrs["zip_entries"] = entries
        grib_entries = [e for e in entries if e.lower().endswith((".grb", ".grib", ".grb2", ".bin"))]
        if not grib_entries:
            warnings.append("no GRIB entry found in ZIP")
            return ReadResult(arrays, attrs, source_variables, warnings)
        for entry in grib_entries:
            suffix = Path(entry).suffix or ".grb"
            cache_key = hashlib.sha1(f"{path.resolve()}|{entry}".encode("utf-8")).hexdigest()
            extracted = extract_cache / f"{cache_key}{suffix}"
            payload = zf.read(entry)
            if not extracted.exists() or extracted.stat().st_size != len(payload):
                extracted.write_bytes(payload)
            try:
                ds = xr.open_dataset(extracted, engine="cfgrib", backend_kwargs={"indexpath": ""})
            except Exception as exc:
                warnings.append(f"cfgrib open failed for {entry}: {exc}")
                continue
            try:
                attrs["cfgrib_attrs"] = {k: attr_to_python(v) for k, v in ds.attrs.items()}
                names = list(ds.data_vars)
                resolved = resolve_variable_names(names, product_map)
                for var_name, standard in resolved.items():
                    data = ds[var_name]
                    arr = np.array(data.values, copy=True)
                    arr = reshape_square_if_needed(arr)
                    if arr.dtype.kind in "fc":
                        arr = mask_sentinel_values(arr.astype(np.float32))
                    arrays[standard] = convert_units(standard, arr, dict(data.attrs))
                    source_variables[standard] = var_name
                    attrs[f"attrs_{standard}"] = {k: attr_to_python(v) for k, v in data.attrs.items()}
                if "latitude" not in arrays and "latitude" in ds.coords:
                    arrays["latitude"] = reshape_square_if_needed(np.array(ds["latitude"].values, dtype=np.float32, copy=True))
                    source_variables["latitude"] = "latitude"
                if "longitude" not in arrays and "longitude" in ds.coords:
                    arrays["longitude"] = reshape_square_if_needed(np.array(ds["longitude"].values, dtype=np.float32, copy=True))
                    source_variables["longitude"] = "longitude"
            finally:
                ds.close()
                del ds
                gc.collect()
    add_valid_and_quality(arrays)
    return ReadResult(arrays=arrays, attrs=attrs, source_variables=source_variables, warnings=warnings)


def reshape_square_if_needed(arr: np.ndarray) -> np.ndarray:
    a = np.asarray(arr)
    if a.ndim != 1:
        return a
    side = int(round(math.sqrt(a.size)))
    if side * side == a.size and side > 32:
        return a.reshape(side, side)
    return a


def read_product(path: Path, family: str, product: str, mapping: dict[str, dict[str, list[str]]]) -> ReadResult:
    suffix = path.suffix.lower()
    if family == "Meteosat" and suffix == ".zip":
        return read_meteosat_zip(path, product, mapping)
    if suffix in {".nc", ".nc4"} or path.name.lower().endswith((".nc", ".nc4")):
        return read_netcdf_product(path, family, product, mapping)
    if suffix in {".hdf", ".h5", ".hdf5"} or path.name.lower().endswith((".hdf", ".h5", ".hdf5")):
        return read_hdf_product(path, family, product, mapping)
    return ReadResult({}, {"source_file": str(path), "reader": "unsupported"}, {}, [f"unsupported suffix {suffix}"])


def finite_stats(arr: np.ndarray) -> dict[str, Any]:
    a = np.asarray(arr)
    row: dict[str, Any] = {"shape": "x".join(map(str, a.shape)), "dtype": str(a.dtype), "size": int(a.size)}
    if a.size == 0:
        row.update({"min": np.nan, "max": np.nan, "mean": np.nan, "nan_ratio": np.nan})
        return row
    if a.dtype.kind in "f":
        finite = np.isfinite(a)
        row["nan_ratio"] = float((~finite).sum() / a.size)
        if finite.any():
            row["min"] = float(np.nanmin(a))
            row["max"] = float(np.nanmax(a))
            row["mean"] = float(np.nanmean(a))
        else:
            row["min"] = row["max"] = row["mean"] = np.nan
    elif a.dtype.kind in "iu":
        row["nan_ratio"] = 0.0
        row["min"] = int(np.min(a))
        row["max"] = int(np.max(a))
        row["mean"] = float(np.mean(a))
    else:
        row["nan_ratio"] = np.nan
        row["min"] = row["max"] = row["mean"] = np.nan
    return row


def make_quicklook(arr: np.ndarray, out_path: Path, title: str, variable_name: str) -> None:
    a = np.asarray(arr)
    if a.ndim == 0:
        return
    if a.ndim == 1:
        a = np.tile(a[np.newaxis, :], (64, 1))
    if a.ndim > 2:
        a = np.squeeze(a)
        if a.ndim > 2:
            a = a.reshape(a.shape[-2], a.shape[-1])
    max_pixels = 1_200_000
    stride = max(1, int(math.ceil(math.sqrt(a.size / max_pixels)))) if a.size > max_pixels else 1
    plot = a[::stride, ::stride]
    plt.figure(figsize=(8, 5), dpi=140)
    if variable_name in CATEGORICAL_VARS:
        plot_float = plot.astype(np.float32, copy=True)
        for fill in CATEGORY_FILL_VALUES.get(variable_name, set()):
            plot_float[np.isclose(plot_float, fill)] = np.nan
        finite = np.isfinite(plot_float)
        if finite.any():
            values = np.unique(plot_float[finite])
            if values.size <= 32:
                values = np.sort(values)
                boundaries = np.concatenate(([values[0] - 0.5], (values[:-1] + values[1:]) / 2.0, [values[-1] + 0.5]))
                base_colors = plt.get_cmap("tab20")(np.linspace(0, 1, max(1, values.size)))
                cmap = ListedColormap(base_colors)
                cmap.set_bad((0.92, 0.92, 0.92, 1.0))
                norm = BoundaryNorm(boundaries, cmap.N)
                im = plt.imshow(plot_float, interpolation="nearest", cmap=cmap, norm=norm)
                cbar = plt.colorbar(im, shrink=0.75, ticks=values)
                cbar.ax.set_yticklabels([str(int(v)) if float(v).is_integer() else f"{v:g}" for v in values])
            else:
                im = plt.imshow(plot_float, interpolation="nearest", cmap="tab20")
                plt.colorbar(im, shrink=0.75)
        else:
            im = plt.imshow(plot_float, interpolation="nearest", cmap="gray")
            plt.colorbar(im, shrink=0.75)
    else:
        finite = np.isfinite(plot) if plot.dtype.kind == "f" else np.ones(plot.shape, dtype=bool)
        if finite.any():
            vmin, vmax = np.nanpercentile(plot.astype(float), [2, 98])
            if not np.isfinite(vmin) or not np.isfinite(vmax) or vmin == vmax:
                vmin, vmax = None, None
        else:
            vmin, vmax = None, None
        im = plt.imshow(plot, interpolation="nearest", cmap="viridis", vmin=vmin, vmax=vmax)
        plt.colorbar(im, shrink=0.75)
    plt.title(title, fontsize=9)
    plt.axis("off")
    plt.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_path)
    plt.close()


def write_json_npz(path: Path, arrays: dict[str, np.ndarray], metadata: dict[str, Any], availability: dict[str, bool]) -> None:
    payload = {name: np.asarray(value) for name, value in arrays.items()}
    payload["metadata_json"] = np.asarray(json.dumps(metadata, ensure_ascii=False, default=str))
    payload["variable_availability_json"] = np.asarray(json.dumps(availability, ensure_ascii=False, default=str))
    np.savez_compressed(path, **payload)
