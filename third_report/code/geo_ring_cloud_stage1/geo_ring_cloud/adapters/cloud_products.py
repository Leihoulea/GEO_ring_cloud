from __future__ import annotations

import gc
import hashlib
import functools
import json
import math
import os
import re
import subprocess
import shutil
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from ..cloud_semantics import add_valid_and_quality
from ..paths import HIMAWARI_R21_DIR
from ..pipeline_layout import MAPPING_YAML, STAGE_ROOT
from .meteosat_native_navigation import (
    NAVIGATION_SCHEMA_VERSION,
    build_meteosat_0deg_clm_raw_navigation,
    matches_meteosat_0deg_clm_scope,
)


COMPONENT_ROLE = "product_adapter"

METEOSAT_CTH_SHAPE = (1237, 1237)
METEOSAT_CTH_NAVIGATION_SOURCE = "satpy_seviri_l2_grib_cth_area"
METEOSAT_CTH_NAVIGATION_GRID = "cth_native_1237x1237_9km"
METEOSAT_0DEG_CTH_NAVIGATION_SCHEMA_VERSION = "meteosat_0deg_cth_v2"
METEOSAT_IODC_CTH_NAVIGATION_SCHEMA_VERSION = "meteosat_iodc_cth_v2"
METEOSAT_CTH_AREA_TOLERANCE_DEG = 1e-6

IODC_CLM_NAVIGATION_SCHEMA_VERSION = "meteosat_iodc_clm_satpy_v1"
IODC_CLM_CACHE_NAMESPACE = "m09j_iodc_clm_v1"
IODC_CLM_READER_BACKEND = "satpy_seviri_l2_grib"
IODC_CLM_NAVIGATION_SOURCE = "satpy_area_definition"
IODC_CLM_MASK_TRANSFORM = "identity"
IODC_CLM_AREA_ID = "msg_seviri_iodc_3km"
IODC_CLM_SUBSATELLITE_LONGITUDE = 45.5
IODC_CLM_SHAPE = (3712, 3712)
IODC_CLM_AREA_TOLERANCE_DEG = 1e-3
IODC_CLM_FILENAME_RE = re.compile(r"^MSG2-SEVI-MSGCLMK-0100-0100-\d{14}\.\d+Z-NA\.zip$", re.IGNORECASE)

_DLL_DIRECTORY_HANDLES: list[Any] = []

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


def normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


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


def normalize_meteosat_longitude_array(lon: np.ndarray) -> np.ndarray:
    values = np.asarray(lon, dtype=np.float32)
    out = np.full(values.shape, np.nan, dtype=np.float32)
    finite = np.isfinite(values)
    out[finite] = ((values[finite] + 180.0) % 360.0) - 180.0
    return out


def _json_sha256(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _setup_eccodes_runtime(extract_cache: Path, warnings: list[str]) -> None:
    """Expose ecCodes DLLs for direct interpreter runs on Windows."""
    if os.name != "nt":
        return
    conda_bin = Path(sys.prefix) / "Library" / "bin"
    if not conda_bin.exists():
        return
    try:
        handle = os.add_dll_directory(str(conda_bin))
        _DLL_DIRECTORY_HANDLES.append(handle)
    except (AttributeError, OSError) as exc:
        warnings.append(f"ecCodes DLL directory registration skipped: {exc}")
    source = conda_bin / "eccodes.dll"
    if not source.exists():
        return
    dll_cache = extract_cache / "dll"
    dll_cache.mkdir(parents=True, exist_ok=True)
    target = dll_cache / "libeccodes.dll"
    try:
        if not target.exists() or target.stat().st_size != source.stat().st_size:
            shutil.copy2(source, target)
        os.environ.setdefault("ECCODES_PYTHON_USE_FINDLIBS", "1")
        handle = os.add_dll_directory(str(dll_cache))
        _DLL_DIRECTORY_HANDLES.append(handle)
    except (AttributeError, OSError, shutil.Error) as exc:
        warnings.append(f"ecCodes compatibility DLL setup skipped: {exc}")


def _area_metadata(area: Any) -> dict[str, Any]:
    proj_dict = dict(getattr(area, "proj_dict", {}) or {})
    area_extent = tuple(float(x) for x in getattr(area, "area_extent", ()))
    width = int(getattr(area, "width", 0))
    height = int(getattr(area, "height", 0))
    payload = {
        "area_id": str(getattr(area, "area_id", "")),
        "description": str(getattr(area, "description", "")),
        "proj_id": str(getattr(area, "proj_id", "")),
        "shape": [height, width],
        "proj_dict": proj_dict,
        "area_extent_m": list(area_extent),
    }
    return {
        **payload,
        "lon_0": _to_float(proj_dict.get("lon_0")),
        "grid_spec_sha256": _json_sha256(payload),
    }


class IodcSatpyEnvironmentNotReady(RuntimeError):
    """Raised when the verified IODC CLM Satpy reader stack is unavailable."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"IODC_SATPY_ENVIRONMENT_NOT_READY: {reason}")


class IodcSatpyAreaInvalid(RuntimeError):
    """Raised when Satpy does not expose the verified IODC CLM AreaDefinition."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"IODC_SATPY_AREA_INVALID: {reason}")


def _path_contains_part(path: str | Path, name: str) -> bool:
    lowered = name.lower()
    return any(str(part).lower() == lowered for part in Path(path).parts)


def is_meteosat_iodc_clm_path(path: str | Path, product: str) -> bool:
    return _path_contains_part(path, "Meteosat-IODC") and product.upper() == "CLM"


def matches_meteosat_iodc_clm_candidate_scope(path: str | Path, product: str) -> tuple[bool, str]:
    if product.upper() != "CLM":
        return False, "product_is_not_clm"
    if not _path_contains_part(path, "Meteosat-IODC"):
        return False, "path_does_not_contain_meteosat_iodc"
    if not IODC_CLM_FILENAME_RE.match(Path(path).name):
        return False, "filename_is_not_msg2_msgclmk_0100_0100"
    return True, "candidate_msg2_iodc_clm"


def validate_meteosat_iodc_clm_area(area_meta: dict[str, Any] | None) -> tuple[bool, str]:
    if not area_meta:
        return False, "missing_satpy_area_metadata"
    area_id = str(area_meta.get("area_id", ""))
    if area_id != IODC_CLM_AREA_ID:
        return False, f"area_id_is_{area_id}"
    area_shape = tuple(area_meta.get("shape", ()))
    if area_shape != IODC_CLM_SHAPE:
        return False, f"area_shape_is_{area_shape}"
    lon0 = _to_float(area_meta.get("lon_0"))
    if lon0 is None:
        return False, "missing_satpy_lon_0"
    if abs(lon0 - IODC_CLM_SUBSATELLITE_LONGITUDE) > IODC_CLM_AREA_TOLERANCE_DEG:
        return False, f"satpy_lon_0_is_{lon0}"
    return True, "matched_msg2_iodc_clm_satpy_area"


@functools.lru_cache(maxsize=1)
def _check_iodc_satpy_environment_cached() -> dict[str, Any]:
    try:
        import eccodes  # noqa: F401
    except Exception as exc:
        raise IodcSatpyEnvironmentNotReady(f"import eccodes failed: {exc}") from exc
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "eccodes", "selfcheck"],
            capture_output=True,
            check=False,
            text=True,
            timeout=60,
        )
    except Exception as exc:
        raise IodcSatpyEnvironmentNotReady(f"eccodes selfcheck failed to run: {exc}") from exc
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "").strip()
        raise IodcSatpyEnvironmentNotReady(f"eccodes selfcheck failed: {message}")
    try:
        import satpy
        from satpy import available_readers
    except Exception as exc:
        raise IodcSatpyEnvironmentNotReady(f"import satpy failed: {exc}") from exc
    try:
        readers = set(available_readers())
    except Exception as exc:
        raise IodcSatpyEnvironmentNotReady(f"satpy available_readers failed: {exc}") from exc
    if "seviri_l2_grib" not in readers:
        raise IodcSatpyEnvironmentNotReady("seviri_l2_grib missing from satpy available_readers")
    return {
        "satpy_version": getattr(satpy, "__version__", ""),
        "eccodes_selfcheck": (proc.stdout or "").strip(),
        "seviri_l2_grib_available": True,
    }


def _check_iodc_satpy_environment(extract_cache: Path, warnings: list[str]) -> dict[str, Any]:
    _setup_eccodes_runtime(extract_cache, warnings)
    return _check_iodc_satpy_environment_cached()


def _iodc_clm_navigation_metadata(area_meta: dict[str, Any], dataset_attrs: dict[str, Any], env_meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "navigation_schema_version": IODC_CLM_NAVIGATION_SCHEMA_VERSION,
        "reader_backend": IODC_CLM_READER_BACKEND,
        "navigation_source": IODC_CLM_NAVIGATION_SOURCE,
        "mask_transform": IODC_CLM_MASK_TRANSFORM,
        "navigation_area_id": area_meta.get("area_id", ""),
        "navigation_area_extent_m_json": json.dumps(area_meta.get("area_extent_m", []), default=str),
        "navigation_proj_dict_json": json.dumps(area_meta.get("proj_dict", {}), sort_keys=True, default=str),
        "navigation_grid_spec_sha256": area_meta.get("grid_spec_sha256", ""),
        "navigation_lon_0": area_meta.get("lon_0"),
        "navigation_shape": "3712x3712",
        "navigation_platform_name": dataset_attrs.get("platform_name", ""),
        "navigation_start_time": str(dataset_attrs.get("start_time", "")),
        "navigation_reader": IODC_CLM_READER_BACKEND,
        "navigation_patch_scope": "Meteosat-IODC CLM MSG2-SEVI-MSGCLMK-0100-0100 3712x3712 lon_0=45.5",
        "navigation_validation_gate": "stage_09j",
        "satpy_version": env_meta.get("satpy_version", ""),
        "satpy_dataset_name": "cloud_mask",
        "satpy_area_description": area_meta.get("description", ""),
    }


def iodc_clm_extracted_path(extract_cache: Path, source_zip: Path, entry: str) -> Path:
    """Keep Satpy's recognizable filename below the Windows legacy path limit."""
    cache_key = hashlib.sha1(
        f"{source_zip.resolve()}|{entry}|{IODC_CLM_NAVIGATION_SCHEMA_VERSION}".encode("utf-8")
    ).hexdigest()
    suffix = Path(entry).suffix or ".grb"
    entry_name = Path(entry).name or f"{cache_key}{suffix}"
    return extract_cache / cache_key[:20] / entry_name


def read_meteosat_iodc_clm_satpy_zip(path: Path) -> ReadResult:
    arrays: dict[str, np.ndarray] = {}
    source_variables: dict[str, str] = {}
    warnings: list[str] = []
    attrs: dict[str, Any] = {
        "source_file": str(path),
        "reader": f"zip+{IODC_CLM_READER_BACKEND}+{IODC_CLM_NAVIGATION_SCHEMA_VERSION}",
        "zip_entries": [],
    }
    extract_cache = STAGE_ROOT / "cache" / IODC_CLM_CACHE_NAMESPACE
    extract_cache.mkdir(parents=True, exist_ok=True)
    attrs["extract_cache"] = str(extract_cache)
    attrs["extract_cache_schema_version"] = IODC_CLM_NAVIGATION_SCHEMA_VERSION
    env_meta = _check_iodc_satpy_environment(extract_cache, warnings)
    from satpy import Scene

    with zipfile.ZipFile(path) as zf:
        entries = zf.namelist()
        attrs["zip_entries"] = entries
        grib_entries = [e for e in entries if e.lower().endswith((".grb", ".grib", ".grb2", ".bin"))]
        if not grib_entries:
            raise IodcSatpyEnvironmentNotReady("no GRIB entry found in ZIP")
        for entry in grib_entries:
            extracted = iodc_clm_extracted_path(extract_cache, path, entry)
            extracted_dir = extracted.parent
            extracted_dir.mkdir(parents=True, exist_ok=True)
            payload = zf.read(entry)
            if not extracted.exists() or extracted.stat().st_size != len(payload):
                extracted.write_bytes(payload)
            scene = Scene(filenames=[str(extracted)], reader="seviri_l2_grib")
            scene.load(["cloud_mask"])
            dataset = scene["cloud_mask"]
            mask = reshape_square_if_needed(np.array(dataset.values, copy=True))
            if tuple(mask.shape) != IODC_CLM_SHAPE:
                raise IodcSatpyAreaInvalid(f"cloud_mask shape is {tuple(mask.shape)}")
            area = dataset.attrs.get("area")
            area_meta = _area_metadata(area) if area is not None else None
            area_ok, area_reason = validate_meteosat_iodc_clm_area(area_meta)
            attrs["meteosat_iodc_clm_scope_reason"] = area_reason
            if not area_ok:
                raise IodcSatpyAreaInvalid(area_reason)
            lon, lat = area.get_lonlats()
            lat = np.asarray(lat, dtype=np.float64)
            lon = np.asarray(lon, dtype=np.float64)
            finite_lon = np.isfinite(lon)
            lon = lon.copy()
            lon[finite_lon] = ((lon[finite_lon] + 180.0) % 360.0) - 180.0
            if lat.shape != mask.shape or lon.shape != mask.shape:
                raise IodcSatpyAreaInvalid(f"area lon/lat shape {lon.shape}/{lat.shape} does not match mask {mask.shape}")
            valid = np.isfinite(lon) & np.isfinite(lat) & (lat >= -90.0) & (lat <= 90.0)
            lat = lat.copy()
            lon = lon.copy()
            lat[~valid] = np.nan
            lon[~valid] = np.nan
            arrays["cloud_mask"] = mask
            arrays["latitude"] = lat
            arrays["longitude"] = lon
            dataset_attrs = attrs_to_dict(dataset.attrs)
            attrs["attrs_cloud_mask"] = dataset_attrs
            attrs.update(_iodc_clm_navigation_metadata(area_meta or {}, dataset_attrs, env_meta))
            source_variables["cloud_mask"] = "satpy:cloud_mask"
            source_variables["latitude"] = IODC_CLM_NAVIGATION_SOURCE
            source_variables["longitude"] = IODC_CLM_NAVIGATION_SOURCE
            warnings.append(
                f"applied {IODC_CLM_NAVIGATION_SCHEMA_VERSION} Meteosat-IODC CLM Satpy area navigation; "
                "cloud_mask unchanged"
            )
            break
    add_valid_and_quality(arrays)
    return ReadResult(arrays=arrays, attrs=attrs, source_variables=source_variables, warnings=warnings)


def read_satpy_meteosat_cth_area(
    extracted: Path,
    extract_cache: Path,
    warnings: list[str] | None = None,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    local_warnings = warnings if warnings is not None else []
    _setup_eccodes_runtime(extract_cache, local_warnings)
    from satpy import Scene

    scene = Scene(filenames=[str(extracted)], reader="seviri_l2_grib")
    scene.load(["cloud_top_height", "cloud_top_quality"])
    cth = scene["cloud_top_height"]
    area = cth.attrs.get("area")
    if area is None:
        raise RuntimeError("Satpy seviri_l2_grib did not return a CTH AreaDefinition")
    lon, lat = area.get_lonlats()
    lat = np.asarray(lat, dtype=np.float32)
    lon = normalize_meteosat_longitude_array(np.asarray(lon, dtype=np.float32))
    valid = np.isfinite(lon) & np.isfinite(lat) & (lat >= -90.0) & (lat <= 90.0)
    lat = lat.copy()
    lon = lon.copy()
    lat[~valid] = np.nan
    lon[~valid] = np.nan
    meta = _area_metadata(area)
    meta["satpy_platform_name"] = str(cth.attrs.get("platform_name", scene.attrs.get("platform_name", "")))
    meta["satpy_start_time"] = str(cth.attrs.get("start_time", scene.attrs.get("start_time", "")))
    meta["satpy_end_time"] = str(cth.attrs.get("end_time", scene.attrs.get("end_time", "")))
    return lat, lon, meta


def matches_meteosat_cth_scope(
    path: str | Path,
    product: str,
    value_shape: tuple[int, int],
    quality_shape: tuple[int, int] | None,
    area_meta: dict[str, Any] | None,
) -> tuple[bool, str, str]:
    """Return CTH navigation eligibility, product line, and reason."""
    p = Path(path)
    if product.upper() != "CTH":
        return False, "", "product_is_not_cth"
    if not re.match(r"^MSG\d*-SEVI-MSGCLTH-0100-0100-\d{14}\.\d+Z-NA\.zip$", p.name):
        return False, "", "filename_is_not_msgclth_operational_cth"
    if tuple(value_shape) != METEOSAT_CTH_SHAPE:
        return False, "", f"value_shape_is_{value_shape}"
    if quality_shape is not None and tuple(quality_shape) != METEOSAT_CTH_SHAPE:
        return False, "", f"quality_shape_is_{quality_shape}"
    if not area_meta:
        return False, "", "missing_satpy_area_metadata"
    area_shape = tuple(area_meta.get("shape", ()))
    if area_shape != METEOSAT_CTH_SHAPE:
        return False, "", f"satpy_area_shape_is_{area_shape}"
    lon0 = _to_float(area_meta.get("lon_0"))
    if lon0 is None:
        return False, "", "missing_satpy_lon_0"
    if abs(lon0) <= METEOSAT_CTH_AREA_TOLERANCE_DEG:
        return True, "Meteosat-0deg", "matched_msgclth_1237_satpy_lon0_0"
    if 1.0 < abs(lon0) <= 90.0:
        return True, "Meteosat-IODC", "matched_msgclth_1237_satpy_iodc_lon0"
    return False, "", f"unsupported_satpy_lon_0_{lon0}"


def meteosat_cth_navigation_metadata(product_line: str, area_meta: dict[str, Any]) -> dict[str, Any]:
    lon0 = _to_float(area_meta.get("lon_0"))
    schema = (
        METEOSAT_0DEG_CTH_NAVIGATION_SCHEMA_VERSION
        if product_line == "Meteosat-0deg"
        else METEOSAT_IODC_CTH_NAVIGATION_SCHEMA_VERSION
    )
    return {
        "navigation_schema_version": schema,
        "navigation_source": METEOSAT_CTH_NAVIGATION_SOURCE,
        "navigation_grid": METEOSAT_CTH_NAVIGATION_GRID,
        "navigation_grid_spec_sha256": area_meta.get("grid_spec_sha256", ""),
        "navigation_area_id": area_meta.get("area_id", ""),
        "navigation_area_extent_m_json": json.dumps(area_meta.get("area_extent_m", []), default=str),
        "navigation_proj_dict_json": json.dumps(area_meta.get("proj_dict", {}), sort_keys=True, default=str),
        "navigation_platform_name": area_meta.get("satpy_platform_name", ""),
        "navigation_lon_0": lon0,
        "navigation_shape": "1237x1237",
        "navigation_reader": "satpy_seviri_l2_grib",
        "navigation_patch_scope": f"{product_line} CTH MSGCLTH-0100-0100 1237x1237",
        "navigation_validation_gate": "stage_10s",
        "cth_transform": "identity",
        "quality_transform": "identity",
    }


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
    iodc_candidate, iodc_scope_reason = matches_meteosat_iodc_clm_candidate_scope(path, product)
    if iodc_candidate:
        return read_meteosat_iodc_clm_satpy_zip(path)
    if is_meteosat_iodc_clm_path(path, product):
        warnings.append(f"{IODC_CLM_NAVIGATION_SCHEMA_VERSION} not applied: {iodc_scope_reason}; using legacy reader path")
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
            entry_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(entry).name) or f"meteosat{suffix}"
            extracted_dir = extract_cache / cache_key
            extracted_dir.mkdir(parents=True, exist_ok=True)
            extracted = extracted_dir / entry_name
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
                if "cloud_top_height_km" in arrays and product.upper() == "CTH":
                    cf_lat_shape = tuple(np.asarray(arrays.get("latitude", np.asarray([]))).shape)
                    cf_lon_shape = tuple(np.asarray(arrays.get("longitude", np.asarray([]))).shape)
                    try:
                        satpy_lat, satpy_lon, area_meta = read_satpy_meteosat_cth_area(extracted, extract_cache, warnings)
                        q_shape = tuple(np.asarray(arrays["quality_flag_raw"]).shape) if "quality_flag_raw" in arrays else None
                        matched, product_line, scope_reason = matches_meteosat_cth_scope(
                            path,
                            product,
                            tuple(np.asarray(arrays["cloud_top_height_km"]).shape),
                            q_shape,
                            area_meta,
                        )
                        attrs["meteosat_cth_scope_reason"] = scope_reason
                        if matched:
                            if satpy_lat.shape != tuple(np.asarray(arrays["cloud_top_height_km"]).shape):
                                raise RuntimeError(
                                    f"Satpy CTH area shape {satpy_lat.shape} does not match CTH values "
                                    f"{np.asarray(arrays['cloud_top_height_km']).shape}"
                                )
                            arrays["latitude"] = np.array(satpy_lat, dtype=np.float32, copy=True)
                            arrays["longitude"] = np.array(satpy_lon, dtype=np.float32, copy=True)
                            attrs.update(meteosat_cth_navigation_metadata(product_line, area_meta))
                            attrs["reader"] = "zip+cfgrib_cached_extract+satpy_seviri_l2_grib_cth_navigation"
                            attrs["legacy_cfgrib_latitude_shape"] = cf_lat_shape
                            attrs["legacy_cfgrib_longitude_shape"] = cf_lon_shape
                            attrs["legacy_cfgrib_navigation_usage"] = "legacy_negative_control_only"
                            source_variables["latitude"] = METEOSAT_CTH_NAVIGATION_SOURCE
                            source_variables["longitude"] = METEOSAT_CTH_NAVIGATION_SOURCE
                            warnings.append(
                                f"applied {attrs['navigation_schema_version']} Meteosat CTH Satpy area navigation; "
                                "cloud_top_height and cloud_top_quality unchanged"
                            )
                    except Exception as exc:
                        warnings.append(f"Meteosat CTH Satpy area navigation unavailable: {exc}")
                if "cloud_mask" in arrays and matches_meteosat_0deg_clm_scope(path, product, tuple(np.asarray(arrays["cloud_mask"]).shape), attrs["cfgrib_attrs"]):
                    cf_lat_shape = tuple(np.asarray(arrays.get("latitude", np.asarray([]))).shape)
                    cf_lon_shape = tuple(np.asarray(arrays.get("longitude", np.asarray([]))).shape)
                    lat, lon, nav_meta = build_meteosat_0deg_clm_raw_navigation(tuple(np.asarray(arrays["cloud_mask"]).shape))
                    arrays["latitude"] = np.array(lat, dtype=np.float32, copy=True)
                    arrays["longitude"] = np.array(lon, dtype=np.float32, copy=True)
                    attrs.update(nav_meta)
                    attrs["reader"] = "zip+cfgrib_cached_extract+stage09h_verified_meteosat_navigation"
                    attrs["legacy_cfgrib_latitude_shape"] = cf_lat_shape
                    attrs["legacy_cfgrib_longitude_shape"] = cf_lon_shape
                    attrs["navigation_patch_scope"] = "Meteosat-0deg CLM MSG3-SEVI-MSGCLMK-0100-0100 3712x3712"
                    attrs["navigation_patch_final_status_required"] = "PRODUCTION_NAVIGATION_PATCH_VALIDATED"
                    source_variables["latitude"] = "stage09h_verified_seviri_native_area"
                    source_variables["longitude"] = "stage09h_verified_seviri_native_area"
                    warnings.append(f"applied {NAVIGATION_SCHEMA_VERSION} verified Meteosat-0deg CLM navigation; cloud_mask unchanged")
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


__all__ = [
    "METEOSAT_CTH_SHAPE", "METEOSAT_CTH_NAVIGATION_SOURCE", "METEOSAT_CTH_NAVIGATION_GRID",
    "METEOSAT_0DEG_CTH_NAVIGATION_SCHEMA_VERSION", "METEOSAT_IODC_CTH_NAVIGATION_SCHEMA_VERSION",
    "IODC_CLM_NAVIGATION_SCHEMA_VERSION", "IODC_CLM_READER_BACKEND", "IODC_CLM_NAVIGATION_SOURCE",
    "IODC_CLM_MASK_TRANSFORM", "IODC_CLM_AREA_ID", "IODC_CLM_SUBSATELLITE_LONGITUDE",
    "IODC_CLM_SHAPE", "IodcSatpyEnvironmentNotReady", "IodcSatpyAreaInvalid",
    "STANDARD_VARS", "CORE_PRODUCTS", "PRODUCT_MAPPING_KEYS", "UNIT_TARGETS",
    "ReadResult", "normalize_name", "parse_time", "iso_z", "read_mapping",
    "product_mapping_key", "resolve_variable_names",
    "attr_to_python", "attrs_to_dict", "parse_himawari_r21_time",
    "find_himawari_r21_geometry_file", "read_himawari_r21_geometry", "variable_to_array",
    "convert_units", "mask_sentinel_values", "add_valid_and_quality",
    "normalize_meteosat_longitude_array", "matches_meteosat_cth_scope",
    "meteosat_cth_navigation_metadata", "read_satpy_meteosat_cth_area",
    "is_meteosat_iodc_clm_path", "matches_meteosat_iodc_clm_candidate_scope",
    "validate_meteosat_iodc_clm_area", "read_meteosat_iodc_clm_satpy_zip",
    "read_netcdf_product", "read_hdf_product", "read_meteosat_zip",
    "reshape_square_if_needed", "read_product",
]
