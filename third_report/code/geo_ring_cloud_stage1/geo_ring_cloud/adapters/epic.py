"""Stable DSCOVR EPIC cloud-height product reader."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import netCDF4
import numpy as np


COMPONENT_ROLE = "product_adapter"
EPIC_CTH_CANDIDATES = (
    "geophysical_data/A-band_Effective_Cloud_Height",
    "geophysical_data/B-band_Effective_Cloud_Height",
    "geophysical_data/Cloud_Top_Height",
    "geophysical_data/CloudTopHeight",
    "geophysical_data/Cloud_Effective_Height",
)

__all__ = ["EPIC_CTH_CANDIDATES", "read_epic_cth"]


def _variable_attributes(var: netCDF4.Variable) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    for name in var.ncattrs():
        value = getattr(var, name)
        if isinstance(value, np.ndarray):
            value = value.tolist()
        elif isinstance(value, np.generic):
            value = value.item()
        attributes[name] = value
    return attributes


def _find_existing_variable(ds: netCDF4.Dataset, names: tuple[str, ...]) -> str | None:
    for name in names:
        group_name, _, variable_name = name.rpartition("/")
        group = ds[group_name] if group_name else ds
        if variable_name in group.variables:
            return name
    return None


def _read_array(ds: netCDF4.Dataset, name: str) -> np.ndarray:
    values = ds[name][:]
    if np.ma.isMaskedArray(values):
        values = values.filled(np.nan)
    return np.asarray(values)


def _optional_geolocation(ds: netCDF4.Dataset, name: str, shape: tuple[int, ...]) -> np.ndarray:
    group = ds.groups.get("geolocation_data")
    if group is None or name not in group.variables:
        return np.full(shape, np.nan, dtype=np.float32)
    return _read_array(ds, f"geolocation_data/{name}").astype(np.float32)


def read_epic_cth(path: str | Path, cth_variable: str | None = None) -> dict[str, Any]:
    """Read one EPIC cloud-height field with common geolocation and validity metadata."""
    source_path = Path(path)
    with netCDF4.Dataset(source_path) as ds:
        latitude = _find_existing_variable(
            ds, ("geolocation_data/latitude", "geolocation_data/Latitude")
        )
        longitude = _find_existing_variable(
            ds, ("geolocation_data/longitude", "geolocation_data/Longitude")
        )
        cloud_mask = _find_existing_variable(
            ds, ("geophysical_data/Cloud_Mask", "geophysical_data/cloud_mask")
        )
        cth_name = cth_variable or _find_existing_variable(ds, EPIC_CTH_CANDIDATES)
        if not latitude or not longitude or not cloud_mask or not cth_name:
            raise RuntimeError(f"missing required EPIC variable in {source_path}")

        cth_attributes = _variable_attributes(ds[cth_name])
        cth_raw = _read_array(ds, cth_name).astype(np.float32)
        fill_value = cth_attributes.get("_FillValue", cth_attributes.get("missing_value"))
        raw_valid = np.isfinite(cth_raw)
        if fill_value is not None:
            raw_valid &= cth_raw != float(fill_value)

        source_units = str(cth_attributes.get("units", "")).strip().lower()
        if source_units == "m":
            cth_km = cth_raw / 1000.0
            conversion = "m_to_km"
            standardized_units = "km"
        elif source_units == "km":
            cth_km = cth_raw
            conversion = "none"
            standardized_units = "km"
        else:
            cth_km = cth_raw
            conversion = "none_or_unknown"
            standardized_units = source_units or "unknown"

        physical_valid = raw_valid & (cth_km >= 0) & (cth_km <= 25)
        return {
            "lat": _read_array(ds, latitude).astype(np.float32),
            "lon": _read_array(ds, longitude).astype(np.float32),
            "cloud_mask": _read_array(ds, cloud_mask).astype(np.float32),
            "cth_km": cth_km.astype(np.float32),
            "cth_valid": physical_valid,
            "cth_raw_valid": raw_valid,
            "cth_var": cth_name,
            "cth_attrs": cth_attributes,
            "cth_units_standardized": standardized_units,
            "cth_conversion": conversion,
            "epic_vza": _optional_geolocation(ds, "sensor_zenith", cth_raw.shape),
            "sza": _optional_geolocation(ds, "solar_zenith", cth_raw.shape),
        }
