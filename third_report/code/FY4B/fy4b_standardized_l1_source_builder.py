from __future__ import annotations

"""FY4B standardized_L1_source builder.

Current FY4B policy is intentionally hybrid but Satpy-first:

1. Satpy is the default radiometry backend for counts, reflectance, and
   brightness temperature.
2. The builder automatically searches the FY4B root for a time-matched GEO
   companion file. When a GEO file is found, official angle datasets from Satpy
   are used and the output is labeled as official-native geometry.
3. The legacy native LUT path is retained only for regression checks and for
   rare fallback scenarios. It is no longer the default production route.

This module therefore serves two purposes at once:
- production construction of FY4B standardized_L1_source NetCDF files
- regression comparison between the Satpy-first route and the older LUT route
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import h5py
import numpy as np
import pandas as pd
import xarray as xr
from pyproj import CRS, Transformer


PRODUCT_VERSION = "v0.2"
TIME_UNITS = "seconds since 1970-01-01 00:00:00 UTC"
FILL_DN = np.uint16(65535)
SATPY_READER = "agri_fy4b_l1"

Q_INVALID_RAW = np.uint16(1)
Q_INVALID_GEOLOCATION = np.uint16(2)
Q_OFF_DISK = np.uint16(4)
Q_CALIBRATION_FAILED = np.uint16(8)
Q_HIGH_VIEW_ANGLE = np.uint16(16)
Q_SUN_ANGLE_INVALID = np.uint16(32)
Q_SUSPICIOUS_SOURCE_QUALITY = np.uint16(64)


FY4B_CHANNELS = [
    # native, satpy, slot, group, wavelength_um, quantity, units, calibration_type, resolution_km, description
    ("NOMChannel01", "C01", "VIS_BLUE", "extension", 0.47, "reflectance", "1", "reflectance_lookup_table", 4.0, "Blue visible"),
    ("NOMChannel02", "C02", "VIS_RED", "core_common", 0.65, "reflectance", "1", "reflectance_lookup_table", 4.0, "Red visible"),
    ("NOMChannel03", "C03", "NIR_VEG", "core_common", 0.825, "reflectance", "1", "reflectance_lookup_table", 4.0, "Vegetation near IR"),
    ("NOMChannel04", "C04", "CIRRUS_138", "extension", 1.379, "reflectance", "1", "reflectance_lookup_table", 4.0, "Cirrus"),
    ("NOMChannel05", "C05", "NIR_SNOW", "core_common", 1.61, "reflectance", "1", "reflectance_lookup_table", 4.0, "Snow/ice near IR"),
    ("NOMChannel06", "C06", "SWIR_CLOUD", "core_common", 2.225, "reflectance", "1", "reflectance_lookup_table", 4.0, "Cloud particle SWIR"),
    ("NOMChannel07", "C07", "SWIR_FOG", "core_common", 3.75, "brightness_temperature", "K", "brightness_temperature_lookup_table", 4.0, "Shortwave IR high gain"),
    ("NOMChannel08", "C08", "SWIR_FOG", "core_common", 3.75, "brightness_temperature", "K", "brightness_temperature_lookup_table", 4.0, "Shortwave IR low gain"),
    ("NOMChannel09", "C09", "WV_UPPER", "core_common", 6.25, "brightness_temperature", "K", "brightness_temperature_lookup_table", 4.0, "Upper water vapor"),
    ("NOMChannel10", "C10", "WV_MID", "extension", 6.95, "brightness_temperature", "K", "brightness_temperature_lookup_table", 4.0, "Mid water vapor"),
    ("NOMChannel11", "C11", "WV_LOWER", "core_common", 7.42, "brightness_temperature", "K", "brightness_temperature_lookup_table", 4.0, "Lower water vapor"),
    ("NOMChannel12", "C12", "IR_86", "core_common", 8.55, "brightness_temperature", "K", "brightness_temperature_lookup_table", 4.0, "IR 8.6"),
    ("NOMChannel13", "C13", "IR_WINDOW_MAIN", "core_common", 10.8, "brightness_temperature", "K", "brightness_temperature_lookup_table", 4.0, "Clean IR window"),
    ("NOMChannel14", "C14", "IR_SPLIT_WINDOW", "core_common", 12.0, "brightness_temperature", "K", "brightness_temperature_lookup_table", 4.0, "Split window"),
    ("NOMChannel15", "C15", "CO2_133", "extension", 13.3, "brightness_temperature", "K", "brightness_temperature_lookup_table", 4.0, "CO2 longwave IR"),
]


def find_project_root(start: str | Path | None = None) -> Path:
    """Return the project root that contains Satellite_Data_20240312."""
    start_path = Path.cwd() if start is None else Path(start).resolve()
    for path in [start_path, *start_path.parents]:
        if (path / "Satellite_Data_20240312").exists():
            return path
    raise FileNotFoundError("Cannot find Satellite_Data_20240312 from current working directory.")


def build_channel_table() -> pd.DataFrame:
    """Build the standardized FY4B channel metadata table."""
    table = pd.DataFrame(
        FY4B_CHANNELS,
        columns=[
            "native_channel_id",
            "satpy_channel_name",
            "standard_channel_slot",
            "channel_group",
            "native_channel_center_um",
            "physical_quantity_type",
            "standard_units",
            "calibration_type",
            "native_spatial_resolution_km",
            "description",
        ],
    )
    table["standard_channel_id"] = table["standard_channel_slot"]
    table["central_wavelength_um"] = table["native_channel_center_um"]
    table["raw_data_type"] = "DN"
    table["channel_presence_flag"] = np.uint8(1)
    table["channel_data_available_flag"] = np.uint8(0)
    return table


def decode_attr(value: Any) -> Any:
    """Decode HDF5 attributes to plain Python values."""
    arr = np.asarray(value)
    if arr.shape == ():
        item = arr.item()
        return item.decode("utf-8", errors="replace") if isinstance(item, bytes) else item
    if arr.dtype.kind == "S":
        return [x.decode("utf-8", errors="replace") for x in arr.tolist()]
    return arr.tolist()


def attr_scalar(h5: h5py.File, key: str, default: Any = None) -> Any:
    """Read one HDF5 attribute as a scalar when possible."""
    if key not in h5.attrs:
        return default
    value = decode_attr(h5.attrs[key])
    if isinstance(value, list) and len(value) == 1:
        return value[0]
    return value


def find_fy4b_file(fy4b_root: Path, hour_utc: str | int) -> Path:
    """Find the first FY4B full-disk HDF file matching the requested UTC hour."""
    pattern = re.compile(r"_NOM_(?P<start>\d{14})_(?P<end>\d{14})_")
    hour = f"{int(hour_utc):02d}"
    candidates = []
    for path in sorted(fy4b_root.rglob("*.HDF")):
        match = pattern.search(path.name)
        if match and match.group("start")[8:10] == hour:
            candidates.append(path)
    if not candidates:
        raise FileNotFoundError(f"No FY4B file found for UTC hour {hour}.")
    return candidates[0]


def find_fy4b_geo_file(fy4b_file: Path, fy4b_root: Path | None = None) -> Path | None:
    """Find a matching FY4B GEO companion file across the full FY4B root.

    GEO files are often stored in a different acquisition or download folder
    from the nominal L1 radiance files. For that reason we search both the
    local L1 directory and, when provided, the entire FY4B root directory.
    Matching is done by the scene start/end timestamps embedded in the
    filename.
    """
    start_end_match = re.search(r"_(\d{14})_(\d{14})_", fy4b_file.name)
    search_roots = [fy4b_file.parent]
    if fy4b_root is not None and fy4b_root not in search_roots:
        search_roots.append(fy4b_root)
    if start_end_match is None:
        patterns = ["*GEO*.HDF", "*_GEO_*.HDF"]
    else:
        start_tag, end_tag = start_end_match.groups()
        patterns = [
            f"*GEO*{start_tag}*{end_tag}*.HDF",
            f"*{start_tag}*{end_tag}*GEO*.HDF",
        ]
    seen: set[Path] = set()
    for search_root in search_roots:
        for pattern in patterns:
            matches = sorted(search_root.rglob(pattern))
            for match in matches:
                if match == fy4b_file or match in seen:
                    continue
                seen.add(match)
                return match
    return None


def parse_fy4b_time(path: Path) -> tuple[datetime, datetime, datetime]:
    """Parse start/end/mid time from the FY4B filename."""
    match = re.search(r"_NOM_(?P<start>\d{14})_(?P<end>\d{14})_", path.name)
    if not match:
        raise ValueError(path.name)
    start = datetime.strptime(match.group("start"), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    end = datetime.strptime(match.group("end"), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    mid = start + (end - start) / 2
    return start, end, mid


def epoch_seconds(dt: datetime) -> np.float64:
    """Convert a UTC datetime to seconds since Unix epoch."""
    return np.float64(dt.timestamp())


def _to_utc_datetime(dt: Any) -> datetime:
    """Normalize a datetime-like value to a timezone-aware UTC datetime."""
    if isinstance(dt, np.datetime64):
        dt = pd.Timestamp(dt).to_pydatetime()
    if isinstance(dt, pd.Timestamp):
        dt = dt.to_pydatetime()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def build_fy4b_projection_arrays(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    """Legacy FY4B projection approximation path retained for fallback and regression use."""
    with h5py.File(path, "r") as h5:
        height, width = h5["Data/NOMChannel01"].shape
        sub_lon = float(np.asarray(h5.attrs["NOMCenterLon"]).ravel()[0])
        sat_height_from_center = float(np.asarray(h5.attrs["NOMSatHeight"]).ravel()[0])
        a = float(np.asarray(h5.attrs["Semimajor axis of ellipsoid"]).ravel()[0])
        b = float(np.asarray(h5.attrs["Semiminor axis of ellipsoid"]).ravel()[0])

    h = sat_height_from_center - a
    x_extent = 5.5e6
    y_extent = 5.5e6
    dx = 2 * x_extent / width
    dy = 2 * y_extent / height
    projection_x = (-x_extent + (np.arange(width) + 0.5) * dx).astype(np.float32)
    projection_y = (y_extent - (np.arange(height) + 0.5) * dy).astype(np.float32)

    geos = CRS.from_proj4(f"+proj=geos +h={h} +lon_0={sub_lon} +a={a} +b={b} +units=m +sweep=x +no_defs")
    geo = CRS.from_proj4("+proj=longlat +datum=WGS84 +no_defs")
    transformer = Transformer.from_crs(geos, geo, always_xy=True)
    xx, yy = np.meshgrid(projection_x, projection_y)
    lon, lat = transformer.transform(xx, yy)
    lat = lat.astype(np.float32)
    lon = lon.astype(np.float32)
    invalid = ~np.isfinite(lat) | ~np.isfinite(lon) | (lat < -90) | (lat > 90) | (lon < -180) | (lon > 180)
    lat[invalid] = np.nan
    lon[invalid] = np.nan

    proj_meta = {
        "sub_lon": sub_lon,
        "sat_height_from_center": sat_height_from_center,
        "perspective_point_height": h,
        "semi_major_axis": a,
        "semi_minor_axis": b,
        "sweep_angle_axis": "x",
        "projection_x_unit": "m",
        "projection_y_unit": "m",
        "approx_extent_m": x_extent,
    }
    return projection_x, projection_y, lon, lat, proj_meta


def geodetic_to_ecef(lat_deg: np.ndarray, lon_deg: np.ndarray, a: float = 6378137.0, b: float = 6356752.31414) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Convert geodetic coordinates to ECEF."""
    lat = np.deg2rad(lat_deg.astype(np.float64))
    lon = np.deg2rad(lon_deg.astype(np.float64))
    e2 = 1.0 - (b * b) / (a * a)
    n = a / np.sqrt(1.0 - e2 * np.sin(lat) ** 2)
    x = n * np.cos(lat) * np.cos(lon)
    y = n * np.cos(lat) * np.sin(lon)
    z = (b * b / (a * a) * n) * np.sin(lat)
    return x, y, z


def ecef_to_enu_components(vx: np.ndarray, vy: np.ndarray, vz: np.ndarray, lat_deg: np.ndarray, lon_deg: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Project an ECEF vector onto local ENU components."""
    lat = np.deg2rad(lat_deg.astype(np.float64))
    lon = np.deg2rad(lon_deg.astype(np.float64))
    east = -np.sin(lon) * vx + np.cos(lon) * vy
    north = -np.sin(lat) * np.cos(lon) * vx - np.sin(lat) * np.sin(lon) * vy + np.cos(lat) * vz
    up = np.cos(lat) * np.cos(lon) * vx + np.cos(lat) * np.sin(lon) * vy + np.sin(lat) * vz
    return east, north, up


def sensor_angles(lat: np.ndarray, lon: np.ndarray, sub_lon: float, sat_height_from_center: float, a: float = 6378137.0, b: float = 6356752.31414) -> tuple[np.ndarray, np.ndarray]:
    """Approximate sensor zenith/azimuth from satellite and surface geometry."""
    sat_lon = np.deg2rad(sub_lon)
    sx = sat_height_from_center * np.cos(sat_lon)
    sy = sat_height_from_center * np.sin(sat_lon)
    sz = 0.0
    gx, gy, gz = geodetic_to_ecef(lat, lon, a=a, b=b)
    vx = sx - gx
    vy = sy - gy
    vz = sz - gz
    east, north, up = ecef_to_enu_components(vx, vy, vz, lat, lon)
    horiz = np.sqrt(east**2 + north**2)
    zenith = np.rad2deg(np.arctan2(horiz, up))
    azimuth = (np.rad2deg(np.arctan2(east, north)) + 360.0) % 360.0
    bad = ~np.isfinite(lat) | ~np.isfinite(lon) | (up <= 0)
    zenith[bad] = np.nan
    azimuth[bad] = np.nan
    return zenith.astype(np.float32), azimuth.astype(np.float32)


def solar_position_approx(dt_utc: datetime) -> tuple[float, float]:
    """Approximate solar declination and equation of time."""
    doy = dt_utc.timetuple().tm_yday
    hour = dt_utc.hour + dt_utc.minute / 60 + dt_utc.second / 3600
    gamma = 2 * np.pi / 365 * (doy - 1 + (hour - 12) / 24)
    decl = (
        0.006918
        - 0.399912 * np.cos(gamma)
        + 0.070257 * np.sin(gamma)
        - 0.006758 * np.cos(2 * gamma)
        + 0.000907 * np.sin(2 * gamma)
        - 0.002697 * np.cos(3 * gamma)
        + 0.00148 * np.sin(3 * gamma)
    )
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * np.cos(gamma)
        - 0.032077 * np.sin(gamma)
        - 0.014615 * np.cos(2 * gamma)
        - 0.040849 * np.sin(2 * gamma)
    )
    return decl, eqtime


def solar_angles(lat: np.ndarray, lon: np.ndarray, dt_utc: datetime) -> tuple[np.ndarray, np.ndarray]:
    """Approximate solar zenith/azimuth using scene midpoint and lon/lat."""
    decl, eqtime = solar_position_approx(dt_utc)
    minutes = dt_utc.hour * 60 + dt_utc.minute + dt_utc.second / 60
    true_solar_time = (minutes + eqtime + 4 * lon) % 1440
    hour_angle = np.deg2rad(true_solar_time / 4 - 180)
    lat_rad = np.deg2rad(lat.astype(np.float64))
    cosz = np.sin(lat_rad) * np.sin(decl) + np.cos(lat_rad) * np.cos(decl) * np.cos(hour_angle)
    cosz = np.clip(cosz, -1, 1)
    zenith = np.rad2deg(np.arccos(cosz))
    az = np.rad2deg(np.arctan2(np.sin(hour_angle), np.cos(hour_angle) * np.sin(lat_rad) - np.tan(decl) * np.cos(lat_rad)))
    azimuth = (az + 180) % 360
    bad = ~np.isfinite(lat) | ~np.isfinite(lon)
    zenith[bad] = np.nan
    azimuth[bad] = np.nan
    return zenith.astype(np.float32), azimuth.astype(np.float32)


def _load_scene(filenames: list[str]):
    """Create a Satpy scene for FY4B."""
    from satpy import Scene

    return Scene(filenames=filenames, reader=SATPY_READER)


def _extract_projection_from_area(area: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    """Convert a Satpy/pyresample area definition to projection vectors and lon/lat grids."""
    projection_x, projection_y = area.get_proj_vectors()
    lon, lat = area.get_lonlats()
    projection_x = np.asarray(projection_x, dtype=np.float32)
    projection_y = np.asarray(projection_y, dtype=np.float32)
    longitude = np.asarray(lon, dtype=np.float32)
    latitude = np.asarray(lat, dtype=np.float32)
    invalid = ~np.isfinite(latitude) | ~np.isfinite(longitude) | (latitude < -90) | (latitude > 90)
    latitude[invalid] = np.nan
    longitude[invalid] = np.nan

    proj_dict = dict(getattr(area, "proj_dict", {}) or {})
    semi_major_axis = float(proj_dict.get("a") or proj_dict.get("semi_major_axis") or 6378137.0)
    semi_minor_axis = float(proj_dict.get("b") or proj_dict.get("semi_minor_axis") or 6356752.31414)
    perspective_point_height = float(proj_dict.get("h") or proj_dict.get("perspective_point_height"))
    sub_lon = float(proj_dict.get("lon_0") or proj_dict.get("longitude_of_projection_origin"))
    proj_meta = {
        "sub_lon": sub_lon,
        "sat_height_from_center": perspective_point_height + semi_major_axis,
        "perspective_point_height": perspective_point_height,
        "semi_major_axis": semi_major_axis,
        "semi_minor_axis": semi_minor_axis,
        "sweep_angle_axis": str(proj_dict.get("sweep") or proj_dict.get("sweep_angle_axis") or "x"),
        "projection_x_unit": str(proj_dict.get("units") or "m"),
        "projection_y_unit": str(proj_dict.get("units") or "m"),
        "area_extent": [float(v) for v in area.area_extent],
        "area_id": getattr(area, "area_id", None),
    }
    return projection_x, projection_y, longitude, latitude, proj_meta


def _build_satpy_core_context(path: Path) -> dict[str, Any]:
    """Load the Satpy area/timing context used as the FY4B production baseline.

    This is the geometry backbone of the Satpy-first route:
    - projection vectors come from Satpy's AreaDefinition
    - lon/lat come from Satpy's area.get_lonlats()
    - scene timing comes from Satpy dataset attrs
    """
    scene = _load_scene([str(path)])
    scene.load(["C02"], calibration="reflectance")
    data_array = scene["C02"]
    projection_x, projection_y, longitude, latitude, proj_meta = _extract_projection_from_area(data_array.attrs["area"])
    start_time = _to_utc_datetime(data_array.attrs["start_time"])
    end_time = _to_utc_datetime(data_array.attrs["end_time"])
    mid_time = start_time + (end_time - start_time) / 2
    return {
        "projection_x": projection_x,
        "projection_y": projection_y,
        "longitude": longitude,
        "latitude": latitude,
        "proj_meta": proj_meta,
        "start_time": start_time,
        "end_time": end_time,
        "mid_time": mid_time,
    }


def _load_satpy_angles(path: Path, geo_file: Path | None) -> dict[str, np.ndarray] | None:
    """Load official FY4B angle datasets through Satpy when a GEO file exists.

    If this succeeds, sensor/solar angle fields should be treated as official
    native geometry rather than approximate fallback geometry.
    """
    if geo_file is None:
        return None
    try:
        scene = _load_scene([str(path), str(geo_file)])
        angle_names = [
            "satellite_zenith_angle",
            "satellite_azimuth_angle",
            "solar_zenith_angle",
            "solar_azimuth_angle",
        ]
        scene.load(angle_names)
        return {
            "sensor_zenith_angle": scene["satellite_zenith_angle"].values.astype(np.float32),
            "sensor_azimuth_angle": scene["satellite_azimuth_angle"].values.astype(np.float32),
            "solar_zenith_angle": scene["solar_zenith_angle"].values.astype(np.float32),
            "solar_azimuth_angle": scene["solar_azimuth_angle"].values.astype(np.float32),
        }
    except Exception:
        return None


def read_fy4b_all_channels(path: Path, channel_table: pd.DataFrame, latitude: np.ndarray, sensor_zenith: np.ndarray, solar_zenith: np.ndarray, high_view_angle_deg: float = 70.0) -> dict[str, np.ndarray]:
    """Legacy native LUT implementation retained for regression and fallback comparisons."""
    shape = latitude.shape
    raw_list = []
    radiance_list = []
    reflectance_list = []
    bt_list = []
    cal_list = []
    valid_list = []
    qf_list = []
    available = []

    geolocation_bad = ~np.isfinite(latitude)
    high_view = np.isfinite(sensor_zenith) & (sensor_zenith > high_view_angle_deg)
    sun_bad = ~np.isfinite(solar_zenith)

    with h5py.File(path, "r") as h5:
        for _, row in channel_table.iterrows():
            native = row["native_channel_id"]
            ch_num = int(native[-2:])
            data_name = f"Data/{native}"
            cal_name = f"Calibration/CALChannel{ch_num:02d}"
            if data_name not in h5 or cal_name not in h5:
                raw = np.full(shape, FILL_DN, dtype=np.uint16)
                calibrated = np.full(shape, np.nan, dtype=np.float32)
                raw_bad = np.ones(shape, dtype=bool)
                cal_bad = np.ones(shape, dtype=bool)
                available.append(np.uint8(0))
            else:
                dn_ds = h5[data_name]
                raw = dn_ds[...].astype(np.uint16)
                fill = int(np.asarray(dn_ds.attrs.get("FillValue", [int(FILL_DN)])).ravel()[0])
                valid_range = np.asarray(dn_ds.attrs.get("valid_range", [0, 4095])).astype(np.int32).ravel()
                lut = h5[cal_name][...].astype(np.float32)
                raw_bad = (raw == fill) | (raw < valid_range[0]) | (raw > valid_range[1]) | (raw >= lut.size)
                calibrated = np.full(raw.shape, np.nan, dtype=np.float32)
                ok_for_lut = ~raw_bad
                calibrated[ok_for_lut] = lut[raw[ok_for_lut]]
                cal_bad = ok_for_lut & ~np.isfinite(calibrated)
                available.append(np.uint8(1))

            quality = np.zeros(shape, dtype=np.uint16)
            quality[raw_bad] |= Q_INVALID_RAW
            quality[geolocation_bad] |= Q_INVALID_GEOLOCATION | Q_OFF_DISK
            quality[cal_bad] |= Q_CALIBRATION_FAILED
            quality[high_view] |= Q_HIGH_VIEW_ANGLE
            quality[sun_bad] |= Q_SUN_ANGLE_INVALID
            valid = quality == 0

            radiance = np.full(shape, np.nan, dtype=np.float32)
            reflectance = np.full(shape, np.nan, dtype=np.float32)
            bt = np.full(shape, np.nan, dtype=np.float32)
            if row["physical_quantity_type"] == "reflectance":
                reflectance[valid] = calibrated[valid]
            elif row["physical_quantity_type"] == "brightness_temperature":
                bt[valid] = calibrated[valid]

            raw_list.append(raw)
            radiance_list.append(radiance)
            reflectance_list.append(reflectance)
            bt_list.append(bt)
            cal_list.append(np.where(valid, calibrated, np.nan).astype(np.float32))
            valid_list.append(valid.astype(np.uint8))
            qf_list.append(quality)

    return {
        "raw_count": np.stack(raw_list, axis=0),
        "radiance": np.stack(radiance_list, axis=0),
        "reflectance": np.stack(reflectance_list, axis=0),
        "brightness_temperature": np.stack(bt_list, axis=0),
        "calibrated_value": np.stack(cal_list, axis=0),
        "valid_mask": np.stack(valid_list, axis=0),
        "quality_flag": np.stack(qf_list, axis=0),
        "channel_data_available_flag": np.asarray(available, dtype=np.uint8),
    }


def read_fy4b_all_channels_satpy(path: Path, channel_table: pd.DataFrame, latitude: np.ndarray, sensor_zenith: np.ndarray, solar_zenith: np.ndarray, high_view_angle_deg: float = 70.0) -> dict[str, np.ndarray]:
    """Read FY4B channels through Satpy and organize them into standardized arrays.

    Important conventions:
    - raw_count keeps the original FY4B integer DN layer
    - reflectance channels are converted from Satpy's percent units to 0-1
      fraction units to match the standardized convention
    - brightness temperature channels stay in kelvin
    - radiance remains NaN because the current FY4B Satpy reader does not
      expose a stable radiance product for this source configuration
    """
    shape = latitude.shape
    channel_count = channel_table.shape[0]
    raw_count = np.full((channel_count, *shape), FILL_DN, dtype=np.uint16)
    radiance = np.full((channel_count, *shape), np.nan, dtype=np.float32)
    reflectance = np.full((channel_count, *shape), np.nan, dtype=np.float32)
    brightness_temperature = np.full((channel_count, *shape), np.nan, dtype=np.float32)
    calibrated_value = np.full((channel_count, *shape), np.nan, dtype=np.float32)
    valid_mask = np.zeros((channel_count, *shape), dtype=np.uint8)
    quality_flag = np.zeros((channel_count, *shape), dtype=np.uint16)
    channel_available = np.zeros(channel_count, dtype=np.uint8)

    geolocation_bad = ~np.isfinite(latitude)
    high_view = np.isfinite(sensor_zenith) & (sensor_zenith > high_view_angle_deg)
    sun_bad = ~np.isfinite(solar_zenith)

    satpy_names = channel_table["satpy_channel_name"].tolist()
    reflectance_channels = channel_table.loc[channel_table["physical_quantity_type"] == "reflectance", "satpy_channel_name"].tolist()
    bt_channels = channel_table.loc[channel_table["physical_quantity_type"] == "brightness_temperature", "satpy_channel_name"].tolist()

    counts_scene = _load_scene([str(path)])
    counts_scene.load(satpy_names, calibration="counts")

    reflectance_scene = None
    if reflectance_channels:
        reflectance_scene = _load_scene([str(path)])
        reflectance_scene.load(reflectance_channels, calibration="reflectance")

    bt_scene = None
    if bt_channels:
        bt_scene = _load_scene([str(path)])
        bt_scene.load(bt_channels, calibration="brightness_temperature")

    for idx, row in channel_table.iterrows():
        satpy_name = row["satpy_channel_name"]
        quantity_type = row["physical_quantity_type"]

        counts = counts_scene[satpy_name].values.astype(np.uint16)
        raw_count[idx] = counts

        raw_bad = counts == FILL_DN
        cal_bad = np.zeros(shape, dtype=bool)

        if quantity_type == "reflectance":
            assert reflectance_scene is not None
            calibrated = reflectance_scene[satpy_name].values.astype(np.float32)
            units = str(reflectance_scene[satpy_name].attrs.get("units", ""))
            if units.strip() == "%":
                calibrated = calibrated / 100.0
            reflectance[idx] = calibrated
            calibrated_value[idx] = calibrated
            cal_bad = ~np.isfinite(calibrated)
        elif quantity_type == "brightness_temperature":
            assert bt_scene is not None
            calibrated = bt_scene[satpy_name].values.astype(np.float32)
            brightness_temperature[idx] = calibrated
            calibrated_value[idx] = calibrated
            cal_bad = ~np.isfinite(calibrated)
        else:
            calibrated = np.full(shape, np.nan, dtype=np.float32)
            cal_bad = np.ones(shape, dtype=bool)

        quality = np.zeros(shape, dtype=np.uint16)
        quality[raw_bad] |= Q_INVALID_RAW
        quality[geolocation_bad] |= Q_INVALID_GEOLOCATION | Q_OFF_DISK
        quality[cal_bad] |= Q_CALIBRATION_FAILED
        quality[high_view] |= Q_HIGH_VIEW_ANGLE
        quality[sun_bad] |= Q_SUN_ANGLE_INVALID

        valid = quality == 0
        valid_mask[idx] = valid.astype(np.uint8)
        quality_flag[idx] = quality
        channel_available[idx] = np.uint8(1)

        if quantity_type == "reflectance":
            reflectance[idx, ~valid] = np.nan
            calibrated_value[idx, ~valid] = np.nan
        elif quantity_type == "brightness_temperature":
            brightness_temperature[idx, ~valid] = np.nan
            calibrated_value[idx, ~valid] = np.nan

    return {
        "raw_count": raw_count,
        "radiance": radiance,
        "reflectance": reflectance,
        "brightness_temperature": brightness_temperature,
        "calibrated_value": calibrated_value,
        "valid_mask": valid_mask,
        "quality_flag": quality_flag,
        "channel_data_available_flag": channel_available,
    }


def _build_channel_data(path: Path, channel_df: pd.DataFrame, latitude: np.ndarray, sensor_zenith: np.ndarray, solar_zenith: np.ndarray, backend: str) -> dict[str, np.ndarray]:
    """Dispatch channel loading to the requested backend."""
    if backend == "satpy":
        return read_fy4b_all_channels_satpy(path, channel_df, latitude, sensor_zenith, solar_zenith)
    if backend == "native_lut":
        return read_fy4b_all_channels(path, channel_df, latitude, sensor_zenith, solar_zenith)
    raise ValueError(f"Unsupported FY4B backend: {backend}")


def _array_diff_summary(lhs: np.ndarray, rhs: np.ndarray, normalize_rhs_factor: float = 1.0) -> dict[str, Any]:
    """Summarize shared-mask numeric differences between two arrays."""
    rhs_scaled = rhs * normalize_rhs_factor
    shared = np.isfinite(lhs) & np.isfinite(rhs_scaled)
    result = {
        "shared_fraction": float(shared.mean()),
        "shared_count": int(shared.sum()),
    }
    if not shared.any():
        result.update({"max_abs_diff": None, "mean_abs_diff": None})
        return result
    diff = lhs[shared] - rhs_scaled[shared]
    result.update(
        {
            "max_abs_diff": float(np.max(np.abs(diff))),
            "mean_abs_diff": float(np.mean(np.abs(diff))),
        }
    )
    return result


def build_backend_comparison_summary(path: Path, target_hour_utc: str) -> dict[str, Any]:
    """Compare the Satpy-first backend against the legacy native LUT backend.

    This summary is intended for migration confidence, not for routine science
    reading. It verifies that the new production route reproduces the older
    LUT-based implementation on representative reflective and thermal channels.
    """
    satpy_ds, _ = build_dataset(project_root=find_project_root(path.parent), target_hour_utc=target_hour_utc, backend="satpy", comparison_mode=False)
    native_ds, _ = build_dataset(project_root=find_project_root(path.parent), target_hour_utc=target_hour_utc, backend="native_lut", comparison_mode=False)
    comparison_rows = {}
    channel_names = ["NOMChannel01", "NOMChannel02", "NOMChannel06", "NOMChannel07", "NOMChannel09", "NOMChannel13", "NOMChannel15"]
    satpy_index = {name: idx for idx, name in enumerate(satpy_ds["native_channel_id"].values.astype(str))}
    for channel_name in channel_names:
        idx = satpy_index[channel_name]
        quantity_type = str(satpy_ds["physical_quantity_type"].values[idx])
        if quantity_type == "reflectance":
            quantity_name = "reflectance"
        else:
            quantity_name = "brightness_temperature"
        comparison_rows[channel_name] = {
            "counts": _array_diff_summary(
                satpy_ds["raw_count"].values[idx].astype(np.float32),
                native_ds["raw_count"].values[idx].astype(np.float32),
            ),
            quantity_name: _array_diff_summary(
                satpy_ds[quantity_name].values[idx].astype(np.float32),
                native_ds[quantity_name].values[idx].astype(np.float32),
            ),
        }
    return comparison_rows


def make_output_path(out_root: Path, start_time: datetime) -> Path:
    """Return the standardized output path for one FY4B scene."""
    tag = start_time.strftime("%Y%m%d_%H%M")
    return out_root / f"FY4B_AGRI_FD_{tag}_standardized_L1_source_{PRODUCT_VERSION}.nc"


def build_dataset(
    project_root: Path | str | None = None,
    target_hour_utc: str = "03",
    backend: str = "satpy",
    comparison_mode: bool = False,
) -> tuple[xr.Dataset, Path]:
    """Build one FY4B standardized_L1_source dataset using the requested backend."""
    project_root = find_project_root(project_root)
    data_root = project_root / "Satellite_Data_20240312"
    fy4b_root = data_root / "FY4B"
    out_root = data_root / "standardized_L1_source" / "FY4B"
    out_root.mkdir(parents=True, exist_ok=True)

    fy4b_file = find_fy4b_file(fy4b_root, target_hour_utc)
    geo_file = find_fy4b_geo_file(fy4b_file, fy4b_root=fy4b_root)
    fallback_start, fallback_end, fallback_mid = parse_fy4b_time(fy4b_file)
    channel_df = build_channel_table()

    if backend == "satpy":
        satpy_context = _build_satpy_core_context(fy4b_file)
        projection_x = satpy_context["projection_x"]
        projection_y = satpy_context["projection_y"]
        longitude = satpy_context["longitude"]
        latitude = satpy_context["latitude"]
        proj_meta = satpy_context["proj_meta"]
        start_time = satpy_context["start_time"]
        end_time = satpy_context["end_time"]
        mid_time = satpy_context["mid_time"]
    elif backend == "native_lut":
        projection_x, projection_y, longitude, latitude, proj_meta = build_fy4b_projection_arrays(fy4b_file)
        start_time, end_time, mid_time = fallback_start, fallback_end, fallback_mid
    else:
        raise ValueError(f"Unsupported FY4B backend: {backend}")

    satpy_angles = _load_satpy_angles(fy4b_file, geo_file) if backend == "satpy" else None
    if satpy_angles is not None:
        sensor_zenith = satpy_angles["sensor_zenith_angle"]
        sensor_azimuth = satpy_angles["sensor_azimuth_angle"]
        solar_zenith = satpy_angles["solar_zenith_angle"]
        solar_azimuth = satpy_angles["solar_azimuth_angle"]
        geolocation_method = "official_native"
        angle_computation_method = "official_native"
    else:
        sensor_zenith, sensor_azimuth = sensor_angles(
            latitude,
            longitude,
            proj_meta["sub_lon"],
            proj_meta["sat_height_from_center"],
            proj_meta["semi_major_axis"],
            proj_meta["semi_minor_axis"],
        )
        solar_zenith, solar_azimuth = solar_angles(latitude, longitude, mid_time)
        if backend == "satpy":
            geolocation_method = "approximate_inverse_geostationary_from_satpy_area"
        else:
            geolocation_method = "approximate_inverse_geostationary"
        angle_computation_method = "approximate_geometry"

    channel_data = _build_channel_data(fy4b_file, channel_df, latitude, sensor_zenith, solar_zenith, backend=backend)
    channel_df["channel_data_available_flag"] = channel_data.pop("channel_data_available_flag")

    channel_coord = np.arange(channel_df.shape[0], dtype=np.int16)
    y_coord = np.arange(latitude.shape[0], dtype=np.int32)
    x_coord = np.arange(latitude.shape[1], dtype=np.int32)
    scan_duration_seconds = (end_time - start_time).total_seconds()
    line_time_offset = np.linspace(0, scan_duration_seconds, latitude.shape[0], dtype=np.float32)

    nominal_time = start_time + (end_time - start_time) / 2
    data_vars = {
        **{name: (("channel", "y", "x"), value) for name, value in channel_data.items()},
        "latitude": (("y", "x"), latitude),
        "longitude": (("y", "x"), longitude),
        "sensor_zenith_angle": (("y", "x"), sensor_zenith),
        "sensor_azimuth_angle": (("y", "x"), sensor_azimuth),
        "solar_zenith_angle": (("y", "x"), solar_zenith),
        "solar_azimuth_angle": (("y", "x"), solar_azimuth),
        "projection_x": (("x",), projection_x),
        "projection_y": (("y",), projection_y),
        "time": ((), epoch_seconds(nominal_time)),
        "observation_start_time": ((), epoch_seconds(start_time)),
        "observation_end_time": ((), epoch_seconds(end_time)),
        "line_time_offset": (("y",), line_time_offset),
        "goes_imager_projection": ((), np.int32(0)),
    }
    for column in [
        "native_channel_id",
        "standard_channel_slot",
        "standard_channel_id",
        "channel_group",
        "physical_quantity_type",
        "standard_units",
        "raw_data_type",
        "calibration_type",
        "description",
    ]:
        data_vars[column] = (("channel",), channel_df[column].astype(str).values)
    for column in [
        "native_channel_center_um",
        "central_wavelength_um",
        "native_spatial_resolution_km",
    ]:
        data_vars[column] = (("channel",), channel_df[column].astype(np.float32).values)
    for column in ["channel_presence_flag", "channel_data_available_flag"]:
        data_vars[column] = (("channel",), channel_df[column].astype(np.uint8).values)

    ds = xr.Dataset(data_vars=data_vars, coords={"channel": channel_coord, "y": y_coord, "x": x_coord})

    with h5py.File(fy4b_file, "r") as h5:
        processing_software_version = f"FY4B standardized_L1_source builder {PRODUCT_VERSION}"
        calibration_reference = "FY4B AGRI CALChannelXX lookup tables in source L1B file"
        notes = (
            "FY4B v0.2: raw_count is DN; reflectance/brightness_temperature come from CALChannelXX lookup tables; "
            "radiance is not available from this source file and is NaN."
        )
        if backend == "satpy":
            import satpy

            processing_software_version = f"{processing_software_version}; satpy {satpy.__version__}"
            calibration_reference = f"satpy/{SATPY_READER}; raw counts retained from source file"
            notes = (
                "FY4B v0.2 Satpy-first path: raw_count comes from Satpy counts; reflectance and brightness_temperature "
                "come from Satpy calibration; radiance is not currently supported by Satpy for this FY4B reader and is NaN. "
                "When a matching GEO companion file is found, official FY4B angle datasets are loaded through Satpy and "
                "the geometry path is treated as official_native. When no GEO companion file is present, geolocation and "
                "angle fields fall back to a mixed path: projection/lonlat come from Satpy AreaDefinition and "
                "viewing/solar angles remain approximate."
            )

        ds.attrs.update(
            {
                "Conventions": "CF-1.8",
                "product_name": "standardized_L1_source",
                "product_version": PRODUCT_VERSION,
                "institution": "AAAresearch_paper third_report",
                "history": f"Created {datetime.now(timezone.utc).isoformat()} by code/FY4B/fy4b_standardized_l1_source_builder.py using backend={backend}",
                "processing_software_version": processing_software_version,
                "satellite_id": "FY4B",
                "platform_name": "FY-4B",
                "sensor_id": "AGRI",
                "scene_type": "FD",
                "source_file": str(fy4b_file),
                "source_file_format": "FY4B AGRI L1 HDF5",
                "source_l1b_version": str(attr_scalar(h5, "Product Version", "")),
                "calibration_reference": calibration_reference,
                "spectral_response_reference": "Native AGRI channel metadata; SRF file not attached in v0.2",
                "observation_start_time_utc": start_time.isoformat(),
                "observation_end_time_utc": end_time.isoformat(),
                "nominal_time_utc": nominal_time.isoformat(),
                "geolocation_method": geolocation_method,
                "angle_computation_method": angle_computation_method,
                "projection": json.dumps(proj_meta),
                "sub_satellite_longitude_degree": proj_meta["sub_lon"],
                "source_dataset_name": str(attr_scalar(h5, "Dataset Name", "")),
                "source_product_id": str(attr_scalar(h5, "ProductID", "")),
                "source_auxiliary_geo_file": "" if geo_file is None else str(geo_file),
                "backend": backend,
                "notes": notes,
            }
        )

    if comparison_mode:
        comparison_summary = build_backend_comparison_summary(fy4b_file, target_hour_utc)
        ds.attrs["backend_comparison_summary_json"] = json.dumps(comparison_summary, ensure_ascii=False)

    for name in ["time", "observation_start_time", "observation_end_time"]:
        ds[name].attrs.update({"units": TIME_UNITS, "calendar": "standard"})
    ds["line_time_offset"].attrs.update(
        {
            "units": "seconds",
            "long_name": "line time offset from observation start",
            "reference_time": "observation_start_time",
        }
    )

    ds["raw_count"].attrs.update(
        {
            "long_name": "original FY4B AGRI digital number",
            "units": "DN",
            "raw_fill_value_code": FILL_DN,
            "comment": "Use valid_mask to screen invalid pixels. No CF _FillValue is set so xarray preserves integer DN.",
        }
    )
    ds["radiance"].attrs.update({"long_name": "spectral radiance", "units": "channel-dependent", "grid_mapping": "goes_imager_projection"})
    ds["reflectance"].attrs.update({"long_name": "reflectance", "units": "1", "grid_mapping": "goes_imager_projection"})
    ds["brightness_temperature"].attrs.update({"long_name": "brightness temperature", "units": "K", "grid_mapping": "goes_imager_projection"})
    ds["calibrated_value"].attrs.update(
        {
            "long_name": "preferred calibrated value for this channel",
            "units": "channel-dependent; see physical_quantity_type and standard_units",
            "grid_mapping": "goes_imager_projection",
        }
    )
    ds["valid_mask"].attrs.update({"long_name": "valid pixel mask", "flag_values": "0, 1", "flag_meanings": "invalid valid"})
    ds["quality_flag"].attrs.update(
        {
            "long_name": "quality bit field",
            "flag_masks": "1, 2, 4, 8, 16, 32, 64, 128",
            "flag_meanings": "invalid_raw invalid_geolocation off_disk calibration_failed high_view_angle sun_angle_invalid suspicious_source_quality reserved",
        }
    )
    ds["latitude"].attrs.update({"units": "degree_north", "long_name": "pixel center latitude", "grid_mapping": "goes_imager_projection"})
    ds["longitude"].attrs.update({"units": "degree_east", "long_name": "pixel center longitude", "grid_mapping": "goes_imager_projection"})
    for name in ["sensor_zenith_angle", "sensor_azimuth_angle", "solar_zenith_angle", "solar_azimuth_angle"]:
        ds[name].attrs.update({"units": "degree", "long_name": name.replace("_", " "), "grid_mapping": "goes_imager_projection"})
    ds["projection_x"].attrs.update({"units": "m", "long_name": "FY4B geostationary projection x coordinate"})
    ds["projection_y"].attrs.update({"units": "m", "long_name": "FY4B geostationary projection y coordinate"})
    ds["goes_imager_projection"].attrs.update(
        {
            "grid_mapping_name": "geostationary",
            "longitude_of_projection_origin": proj_meta["sub_lon"],
            "perspective_point_height": proj_meta["perspective_point_height"],
            "semi_major_axis": proj_meta["semi_major_axis"],
            "semi_minor_axis": proj_meta["semi_minor_axis"],
            "sweep_angle_axis": proj_meta["sweep_angle_axis"],
        }
    )

    return ds, make_output_path(out_root, start_time)


def write_dataset(ds: xr.Dataset, out_path: Path) -> Path:
    """Write the standardized dataset to NetCDF."""
    encoding: dict[str, dict[str, Any]] = {}
    for name in ds.data_vars:
        if ds[name].dtype.kind in "fiu":
            encoding[name] = {"zlib": True, "complevel": 4, "shuffle": True}
    for name in [
        "radiance",
        "reflectance",
        "brightness_temperature",
        "calibrated_value",
        "latitude",
        "longitude",
        "sensor_zenith_angle",
        "sensor_azimuth_angle",
        "solar_zenith_angle",
        "solar_azimuth_angle",
    ]:
        encoding[name].update({"_FillValue": np.float32(np.nan)})
    for name in ["time", "observation_start_time", "observation_end_time", "line_time_offset", "goes_imager_projection"]:
        encoding[name] = {"_FillValue": None}

    ds.to_netcdf(out_path, engine="netcdf4", encoding=encoding)
    return out_path


def build_and_write(
    project_root: str | Path | None = None,
    target_hour_utc: str = "03",
    backend: str = "satpy",
    comparison_mode: bool = False,
) -> Path:
    """Build and write one FY4B standardized file."""
    ds, out_path = build_dataset(
        Path(project_root) if project_root is not None else None,
        target_hour_utc=target_hour_utc,
        backend=backend,
        comparison_mode=comparison_mode,
    )
    write_dataset(ds, out_path)
    print(out_path)
    print(f"size MB = {out_path.stat().st_size / 1024 / 1024:.2f}")
    return out_path


if __name__ == "__main__":
    build_and_write(target_hour_utc="03", backend="satpy")
