"""Verified Meteosat-0deg CLM native-grid navigation.

This module intentionally fixes only the navigation paired with the raw
MSGCLMK cloud-mask storage order. Cloud-mask values are not transformed here.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np


COMPONENT_ROLE = "product_adapter_navigation"

NAVIGATION_SCHEMA_VERSION = "meteosat_0deg_clm_v2"
VALIDATION_GATE = "stage_09h_gate4a"
OFFICIAL_READER_CROSSCHECK = "orientation_confirmed_exact_native_grid_inconclusive"


@dataclass(frozen=True)
class MeteosatNativeGridSpec:
    area_id: str = "msg_seviri_fes_3km"
    description: str = "Stage 09H Gate 4A verified Meteosat-0deg CLM SEVIRI native area"
    proj_id: str = "geosmsg"
    shape_y: int = 3712
    shape_x: int = 3712
    proj: str = "geos"
    lon_0: float = 0.0
    h: float = 35785831.0
    x_0: float = 0.0
    y_0: float = 0.0
    a: float = 6378169.0
    rf: float = 295.488065897014
    units: str = "m"
    area_extent_m: tuple[float, float, float, float] = (
        5.567248e6,
        5.5702485e6,
        -5.5702485e6,
        -5.567248e6,
    )
    row_order: str = "south_to_north"
    column_order: str = "east_to_west"
    pixel_center_convention: str = "pyresample AreaDefinition pixel centers from Gate 4A SEVIRI native area"
    validation_gate: str = VALIDATION_GATE

    @property
    def shape(self) -> tuple[int, int]:
        return (self.shape_y, self.shape_x)

    @property
    def proj_dict(self) -> dict[str, Any]:
        return {
            "proj": self.proj,
            "lon_0": self.lon_0,
            "h": self.h,
            "x_0": self.x_0,
            "y_0": self.y_0,
            "a": self.a,
            "rf": self.rf,
            "units": self.units,
            "no_defs": None,
            "type": "crs",
        }


METEOSAT_0DEG_CLM_GRID_SPEC = MeteosatNativeGridSpec()


def grid_spec_sha256(spec: MeteosatNativeGridSpec = METEOSAT_0DEG_CLM_GRID_SPEC) -> str:
    payload = json.dumps(asdict(spec), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_longitude_array(lon: np.ndarray) -> np.ndarray:
    values = np.asarray(lon, dtype=np.float32)
    out = np.full(values.shape, np.nan, dtype=np.float32)
    finite = np.isfinite(values)
    out[finite] = ((values[finite] + 180.0) % 360.0) - 180.0
    return out


def matches_meteosat_0deg_clm_scope(
    path: str | Path,
    product: str,
    shape: tuple[int, int],
    attrs: dict[str, Any] | None = None,
) -> bool:
    """Return True only for the audited MSG3 Meteosat-0deg CLM 3712x3712 family."""
    p = Path(path)
    name = p.name
    path_text = str(p).replace("\\", "/")
    if product.upper() != "CLM":
        return False
    if tuple(shape) != METEOSAT_0DEG_CLM_GRID_SPEC.shape:
        return False
    if not re.match(r"^MSG3-SEVI-MSGCLMK-0100-0100-\d{14}\.\d+Z-NA\.zip$", name):
        return False
    if "Meteosat-0deg" not in path_text:
        return False
    if "Meteosat-IODC" in path_text:
        return False
    if attrs:
        lon0_candidates = [
            attrs.get("GRIB_subSatellitePointLongitudeInDegrees"),
            attrs.get("GRIB_subSatellitePointLongitude"),
            attrs.get("subSatellitePointLongitude"),
            attrs.get("longitudeOfSubSatellitePointInDegrees"),
        ]
        for value in lon0_candidates:
            if value in (None, ""):
                continue
            try:
                return abs(float(value)) < 1e-6
            except (TypeError, ValueError):
                continue
    return True


def build_meteosat_0deg_clm_raw_navigation(
    shape: tuple[int, int] = METEOSAT_0DEG_CLM_GRID_SPEC.shape,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Build latitude/longitude matching raw MSGCLMK mask storage order."""
    spec = METEOSAT_0DEG_CLM_GRID_SPEC
    if tuple(shape) != spec.shape:
        raise ValueError(f"Meteosat-0deg CLM navigation supports shape {spec.shape}, got {shape}")
    lat, lon = _cached_navigation(spec.shape_y, spec.shape_x, grid_spec_sha256(spec))
    meta = navigation_metadata(spec)
    return lat, lon, meta


@lru_cache(maxsize=2)
def _cached_navigation(shape_y: int, shape_x: int, spec_hash: str) -> tuple[np.ndarray, np.ndarray]:
    del spec_hash
    spec = METEOSAT_0DEG_CLM_GRID_SPEC
    from pyresample.geometry import AreaDefinition

    area = AreaDefinition(
        spec.area_id,
        spec.description,
        spec.proj_id,
        spec.proj_dict,
        shape_x,
        shape_y,
        spec.area_extent_m,
    )
    lon, lat = area.get_lonlats()
    lat = np.asarray(lat, dtype=np.float32)
    lon = normalize_longitude_array(np.asarray(lon, dtype=np.float32))
    valid = np.isfinite(lon) & np.isfinite(lat) & (lat >= -90.0) & (lat <= 90.0)
    lat = lat.copy()
    lon = lon.copy()
    lat[~valid] = np.nan
    lon[~valid] = np.nan
    lat.setflags(write=False)
    lon.setflags(write=False)
    return lat, lon


def navigation_metadata(spec: MeteosatNativeGridSpec = METEOSAT_0DEG_CLM_GRID_SPEC) -> dict[str, Any]:
    return {
        "navigation_schema_version": NAVIGATION_SCHEMA_VERSION,
        "navigation_source": "official_seviri_native_area_definition",
        "navigation_storage_order": "raw_grib_mask_storage_order",
        "navigation_row_order": spec.row_order,
        "navigation_column_order": spec.column_order,
        "navigation_validation_gate": spec.validation_gate,
        "navigation_official_reader_crosscheck": OFFICIAL_READER_CROSSCHECK,
        "decoded_cfgrib_navigation_usage": "diagnostic_only",
        "mask_transform": "identity",
        "navigation_grid_spec_sha256": grid_spec_sha256(spec),
        "navigation_area_id": spec.area_id,
        "navigation_area_extent_m_json": json.dumps(spec.area_extent_m),
        "navigation_proj_dict_json": json.dumps(spec.proj_dict, sort_keys=True, default=str),
        "navigation_pixel_center_convention": spec.pixel_center_convention,
    }


__all__ = [
    "COMPONENT_ROLE",
    "METEOSAT_0DEG_CLM_GRID_SPEC",
    "NAVIGATION_SCHEMA_VERSION",
    "MeteosatNativeGridSpec",
    "build_meteosat_0deg_clm_raw_navigation",
    "grid_spec_sha256",
    "matches_meteosat_0deg_clm_scope",
    "navigation_metadata",
]
