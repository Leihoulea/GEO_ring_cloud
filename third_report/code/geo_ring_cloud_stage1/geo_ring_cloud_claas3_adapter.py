from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import netCDF4
import numpy as np

from geo_ring_cloud.sources import REGISTRY_VERSION, SOURCE_BY_KEY


FILE_RE = re.compile(r"^(?P<product>CMA|CTX|CPP)in(?P<stamp>\d{14})(?P<version>\d{3})SV(?P<platform>[A-Z0-9]+)\.nc$", re.I)
SCIENCE_VARIABLES = {
    "CMA": {"cma": "cloud_mask", "cma_prob": "cloud_probability"},
    "CTX": {"cth": "cloud_top_height_km", "ctt": "cloud_top_temperature_K", "ctp": "cloud_top_pressure_hPa"},
    "CPP": {"cph": "cloud_phase", "cot": "cloud_optical_thickness", "cre": "cloud_effective_radius_um", "cwp": "cloud_water_path_g_m2"},
}
UNCERTAINTY_VARIABLES = {
    "cth_unc": "cloud_top_height_uncertainty_km",
    "ctt_unc": "cloud_top_temperature_uncertainty_K",
    "ctp_unc": "cloud_top_pressure_uncertainty_hPa",
    "cot_unc": "cloud_optical_thickness_uncertainty",
    "cre_unc": "cloud_effective_radius_uncertainty_um",
    "cwp_unc": "cloud_water_path_uncertainty_g_m2",
}


@dataclass(frozen=True)
class Claas3FileRecord:
    product: str
    nominal_time: str
    product_version: str
    platform_token: str
    path: str
    size_bytes: int
    modified_utc: str


@dataclass
class Claas3ReadResult:
    arrays: dict[str, np.ndarray]
    metadata: dict[str, Any]
    availability: dict[str, bool]
    source_variables: dict[str, str]
    warnings: list[str]


def parse_filename(path: str | Path) -> Claas3FileRecord | None:
    p = Path(path)
    match = FILE_RE.match(p.name)
    if not match:
        return None
    stamp = datetime.strptime(match.group("stamp"), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    stat = p.stat()
    modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return Claas3FileRecord(
        product=match.group("product").upper(),
        nominal_time=stamp.isoformat(timespec="seconds").replace("+00:00", "Z"),
        product_version=match.group("version"),
        platform_token=match.group("platform"),
        path=str(p),
        size_bytes=int(stat.st_size),
        modified_utc=modified,
    )


def discover_files(root: str | Path) -> tuple[list[Claas3FileRecord], list[dict[str, Any]]]:
    candidates: dict[tuple[str, str], list[Claas3FileRecord]] = {}
    for path in Path(root).rglob("*.nc"):
        record = parse_filename(path)
        if record is not None:
            candidates.setdefault((record.product, record.nominal_time), []).append(record)
    selected: list[Claas3FileRecord] = []
    duplicates: list[dict[str, Any]] = []
    for key, records in sorted(candidates.items()):
        ordered = sorted(records, key=lambda item: (int(item.product_version), item.size_bytes, item.path), reverse=True)
        selected.append(ordered[0])
        if len(ordered) > 1:
            duplicates.append({
                "product": key[0],
                "nominal_time": key[1],
                "selected_path": ordered[0].path,
                "candidate_count": len(ordered),
                "candidate_paths": "|".join(item.path for item in ordered),
            })
    return selected, duplicates


def select_for_time(records: Iterable[Claas3FileRecord], product: str, target_time: str) -> tuple[Claas3FileRecord | None, float]:
    product = product.upper()
    target = datetime.fromisoformat(target_time.replace("Z", "+00:00"))
    choices = [item for item in records if item.product == product]
    if not choices:
        return None, float("nan")
    ranked = sorted(
        ((abs((datetime.fromisoformat(item.nominal_time.replace("Z", "+00:00")) - target).total_seconds()) / 60.0, item) for item in choices),
        key=lambda pair: (pair[0], -int(pair[1].product_version), pair[1].path),
    )
    delta, selected = ranked[0]
    tolerance = 7.5 if product == "CMA" else SOURCE_BY_KEY["CLAAS3-0deg"].time_tolerance_minutes
    return (selected, delta) if delta <= tolerance else (None, delta)


def _attrs(var: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in var.ncattrs():
        value = getattr(var, name)
        if isinstance(value, np.ndarray):
            value = value.tolist()
        elif isinstance(value, np.generic):
            value = value.item()
        out[name] = value
    return out


def _raw_and_physical(var: Any) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    attrs = _attrs(var)
    raw = np.asarray(var[:])
    raw = np.squeeze(raw)
    fill = attrs.get("_FillValue", attrs.get("missing_value"))
    valid = np.ones(raw.shape, dtype=bool)
    if fill is not None:
        valid &= raw != np.asarray(fill, dtype=raw.dtype)
    scale = float(attrs.get("scale_factor", 1.0) or 1.0)
    offset = float(attrs.get("add_offset", 0.0) or 0.0)
    physical = raw.astype(np.float64) * scale + offset
    physical[~valid] = np.nan
    return raw, physical.astype(np.float32), attrs


def _convert(standard: str, values: np.ndarray, attrs: dict[str, Any]) -> tuple[np.ndarray, str]:
    units = str(attrs.get("units", "")).strip()
    out = values.astype(np.float32, copy=True)
    target_units = units
    if standard == "cloud_mask":
        result = np.full(out.shape, -9999, dtype=np.int16)
        result[np.isclose(out, 0)] = 0
        result[np.isclose(out, 1)] = 3
        return result, "canonical_code_0_clear_3_cloudy"
    if standard == "cloud_probability":
        out /= 100.0
        target_units = "1"
    elif standard.endswith("height_km") or standard.endswith("uncertainty_km"):
        if units.lower() in {"m", "meter", "meters"}:
            out /= 1000.0
        target_units = "km"
    elif standard.endswith("radius_um") or standard.endswith("uncertainty_um"):
        if units.lower() in {"m", "meter", "meters"}:
            out *= 1_000_000.0
        target_units = "um"
    elif standard.endswith("water_path_g_m2") or standard.endswith("uncertainty_g_m2"):
        if units.lower().replace(" ", "") in {"kg/m2", "kgm-2", "kgm^-2"}:
            out *= 1000.0
        target_units = "g m-2"
    return out, target_units


def _quality_masks(product: str, standard: str, physical: np.ndarray, qc: dict[str, np.ndarray], cloud_phase: np.ndarray | None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if standard in {"cloud_mask", "cloud_phase"}:
        physical_valid = np.isin(physical, [0, 3] if standard == "cloud_mask" else [0, 1, 2])
    else:
        physical_valid = np.isfinite(physical)
    if standard == "cloud_probability":
        physical_valid &= (physical >= 0.0) & (physical <= 1.0)
    elif standard == "cloud_top_height_km":
        physical_valid &= (physical >= 0.0) & (physical <= 25.0)
    elif standard == "cloud_top_temperature_K":
        physical_valid &= (physical >= 150.0) & (physical <= 350.0)
    elif standard == "cloud_top_pressure_hPa":
        physical_valid &= (physical >= 0.0) & (physical <= 1100.0)
    elif standard == "cloud_optical_thickness":
        physical_valid &= (physical >= 0.0) & (physical <= 200.0)
    elif standard == "cloud_effective_radius_um":
        physical_valid &= (physical >= 0.0) & (physical <= 200.0)
    elif standard == "cloud_water_path_g_m2":
        physical_valid &= (physical >= 0.0) & (physical <= 10000.0)

    if product in {"CMA", "CTX"}:
        quality = qc.get("quality")
        if quality is None:
            return physical_valid, np.zeros_like(physical_valid), physical_valid.copy()
        quality_class = np.asarray(quality, dtype=np.uint16) & np.uint16(56)
        fusion = physical_valid & (quality_class == 8)
        diagnostic = physical_valid & np.isin(quality_class, [8, 16, 32])
        return physical_valid, fusion, diagnostic

    processing = qc.get("processing_flag")
    if processing is None:
        return physical_valid, np.zeros_like(physical_valid), physical_valid.copy()
    flags = np.asarray(processing, dtype=np.uint16)
    diagnostic = physical_valid.copy()
    fusion = physical_valid.copy()
    if standard in {"cloud_optical_thickness", "cloud_effective_radius_um", "cloud_water_path_g_m2"} and cloud_phase is not None:
        cloudy = np.isin(cloud_phase, [1, 2])
        retrieval_failure = (flags & np.uint16(256 | 512 | 4096)) != 0
        physical_valid &= cloudy
        diagnostic &= cloudy
        fusion &= cloudy & ~retrieval_failure
    return physical_valid, fusion, diagnostic


def structure_signature(path: str | Path) -> str:
    with netCDF4.Dataset(path) as ds:
        parts = [f"D:{name}:{len(dim)}" for name, dim in ds.dimensions.items()]
        for name, var in ds.variables.items():
            parts.append(f"V:{name}:{var.dtype}:{','.join(var.dimensions)}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def read_product(path: str | Path) -> Claas3ReadResult:
    record = parse_filename(path)
    if record is None:
        raise ValueError(f"not a CLAAS-3 CMA/CTX/CPP file: {path}")
    arrays: dict[str, np.ndarray] = {}
    source_variables: dict[str, str] = {}
    warnings: list[str] = []
    variable_attrs: dict[str, Any] = {}
    original_units: dict[str, str] = {}
    physical_units: dict[str, str] = {}
    qc: dict[str, np.ndarray] = {}

    with netCDF4.Dataset(path) as ds:
        ds.set_auto_mask(False)
        ds.set_auto_scale(False)
        global_attrs = {name: getattr(ds, name) for name in ds.ncattrs()}
        for name in ("quality", "status_flag", "conditions", "processing_flag", "processing_flag_16"):
            if name in ds.variables:
                raw, _, attrs = _raw_and_physical(ds.variables[name])
                qc[name] = raw
                arrays[f"claas3_{name}_raw"] = raw
                variable_attrs[f"claas3_{name}_raw"] = attrs
        for name in ("x", "y"):
            if name in ds.variables:
                _, values, attrs = _raw_and_physical(ds.variables[name])
                standard = f"projection_{name}"
                arrays[standard] = values
                source_variables[standard] = name
                variable_attrs[standard] = attrs
        projection_attrs: dict[str, Any] = {}
        if "projection" in ds.variables:
            projection_attrs = _attrs(ds.variables["projection"])
            arrays["geostationary_projection"] = np.asarray(0, dtype=np.int16)
            source_variables["geostationary_projection"] = "projection"

        science: dict[str, np.ndarray] = {}
        for source_name, standard in SCIENCE_VARIABLES[record.product].items():
            if source_name not in ds.variables:
                warnings.append(f"missing expected variable {source_name}")
                continue
            _, values, attrs = _raw_and_physical(ds.variables[source_name])
            converted, units = _convert(standard, values, attrs)
            science[standard] = converted
            arrays[standard] = converted
            source_variables[standard] = source_name
            variable_attrs[standard] = attrs
            original_units[standard] = str(attrs.get("units", ""))
            physical_units[standard] = units
        for source_name, standard in UNCERTAINTY_VARIABLES.items():
            if source_name not in ds.variables:
                continue
            _, values, attrs = _raw_and_physical(ds.variables[source_name])
            converted, units = _convert(standard, values, attrs)
            arrays[standard] = converted
            source_variables[standard] = source_name
            variable_attrs[standard] = attrs
            original_units[standard] = str(attrs.get("units", ""))
            physical_units[standard] = units

    phase = science.get("cloud_phase")
    for standard, values in science.items():
        physical, fusion, diagnostic = _quality_masks(record.product, standard, values, qc, phase)
        arrays[f"physical_valid_mask_{standard}"] = physical.astype(np.uint8)
        arrays[f"fusion_valid_mask_{standard}"] = fusion.astype(np.uint8)
        arrays[f"diagnostic_valid_mask_{standard}"] = diagnostic.astype(np.uint8)

    availability = {f"has_{name}": True for name in science}
    metadata = {
        "source_key": "CLAAS3-0deg",
        "source_id": 7,
        "satellite_group": "CLAAS3-0deg",
        "satellite_family": "CLAAS3",
        "platform": "METEOSAT-10",
        "processing_stream": "CM_SAF_CLAAS_V003_ICDR",
        "product": record.product,
        "product_version": record.product_version,
        "nominal_time": record.nominal_time,
        "source_file": record.path,
        "source_variables": source_variables,
        "variable_attrs": variable_attrs,
        "original_units": original_units,
        "physical_units": physical_units,
        "geostationary_projection_attrs": projection_attrs,
        "scale_offset_policy": "netCDF auto mask/scale disabled; fill masked in raw storage; scale_factor/add_offset applied exactly once",
        "quality_policy": "per-variable physical/fusion/diagnostic masks; flags are gates, never continuous weights",
        "registry_version": REGISTRY_VERSION,
        "warnings": warnings,
    }
    return Claas3ReadResult(arrays, metadata, availability, source_variables, warnings)


def records_as_dicts(records: Iterable[Claas3FileRecord]) -> list[dict[str, Any]]:
    return [asdict(item) for item in records]
