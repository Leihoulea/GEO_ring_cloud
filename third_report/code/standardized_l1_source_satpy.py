from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr
from satpy import Scene


PRODUCT_VERSION = "v0.2"
TIME_UNITS = "seconds since 1970-01-01 00:00:00 UTC"

Q_INVALID_RAW = np.uint16(1)
Q_CALIBRATION_FAILED = np.uint16(8)


def find_project_root(start: str | Path | None = None) -> Path:
    """Return the project root that contains Satellite_Data_20240312."""
    start_path = Path.cwd() if start is None else Path(start).resolve()
    for path in [start_path, *start_path.parents]:
        if (path / "Satellite_Data_20240312").exists():
            return path
    raise FileNotFoundError("Cannot find Satellite_Data_20240312.")


def epoch_seconds(dt: datetime) -> np.float64:
    """Convert a timezone-aware datetime to Unix epoch seconds."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return np.float64(dt.timestamp())


def area_grid_mapping_attrs(area) -> dict:
    """Translate a Satpy area definition into CF-style geostationary grid_mapping attrs."""
    proj = {k: v for k, v in area.proj_dict.items()}
    attrs = {"grid_mapping_name": "geostationary"}
    if "lon_0" in proj:
        attrs["longitude_of_projection_origin"] = float(proj["lon_0"])
    if "h" in proj:
        attrs["perspective_point_height"] = float(proj["h"])
    if "a" in proj:
        attrs["semi_major_axis"] = float(proj["a"])
    if "b" in proj:
        attrs["semi_minor_axis"] = float(proj["b"])
    if "rf" in proj and "semi_minor_axis" not in attrs and "semi_major_axis" in attrs:
        attrs["inverse_flattening"] = float(proj["rf"])
    attrs["sweep_angle_axis"] = str(proj.get("sweep", "x"))
    return attrs


def infer_times(da) -> tuple[datetime, datetime, datetime]:
    """Extract start, end, and midpoint time from a Satpy DataArray."""
    start = da.attrs.get("start_time")
    end = da.attrs.get("end_time")
    if start is None:
        start = da.attrs.get("time_parameters", {}).get("nominal_start_time")
    if end is None:
        end = da.attrs.get("time_parameters", {}).get("nominal_end_time")
    if start is None:
        start = datetime(1970, 1, 1, tzinfo=timezone.utc)
    if end is None:
        end = start
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    mid = start + (end - start) / 2
    return start, end, mid


def normalize_projection_unit(unit: str | None) -> str:
    """Normalize projection-axis unit labels to the project convention."""
    if unit is None:
        return "m"
    lowered = str(unit).strip().lower()
    if lowered in {"meter", "meters", "metre", "metres", "m"}:
        return "m"
    return str(unit)


def normalize_raw_count_unit(unit: str | None) -> str:
    """Normalize raw count unit labels to project-friendly wording."""
    if unit is None:
        return "count"
    lowered = str(unit).strip().lower()
    if lowered in {"1", "count", "counts", "dn"}:
        return "count" if lowered != "dn" else "DN"
    return str(unit)


def prepare_raw_count(counts: xr.DataArray) -> tuple[xr.DataArray, dict]:
    """Preserve integer-valued count layers as integer raw_count when possible.

    Satpy may expose count datasets as floating-point arrays even when the values
    are semantically discrete raw counts. For standardized_L1_source we prefer
    to keep raw_count as an integer layer when:
    - all finite values are integer-valued
    - a safe integer dtype and fill code can be chosen
    """
    values = np.asarray(counts.values)
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        raw_fill_value_code = np.uint16(65535)
        raw_array = np.full(values.shape, raw_fill_value_code, dtype=np.uint16)
        return xr.DataArray(raw_array, dims=counts.dims), {
            "units": normalize_raw_count_unit(counts.attrs.get("units", "count")),
            "raw_fill_value_code": raw_fill_value_code,
        }

    is_integer_valued = np.allclose(finite, np.round(finite), atol=0.0, rtol=0.0)
    if is_integer_valued:
        finite_min = float(finite.min())
        finite_max = float(finite.max())
        if finite_min >= 0 and finite_max < np.iinfo(np.uint16).max:
            fill_value = np.uint16(np.iinfo(np.uint16).max)
            raw_array = np.where(np.isfinite(values), np.round(values), fill_value).astype(np.uint16)
            return xr.DataArray(raw_array, dims=counts.dims), {
                "units": normalize_raw_count_unit(counts.attrs.get("units", "count")),
                "raw_fill_value_code": fill_value,
            }
        if finite_min > np.iinfo(np.int16).min and finite_max < np.iinfo(np.int16).max:
            fill_value = np.int16(np.iinfo(np.int16).min)
            raw_array = np.where(np.isfinite(values), np.round(values), fill_value).astype(np.int16)
            return xr.DataArray(raw_array, dims=counts.dims), {
                "units": normalize_raw_count_unit(counts.attrs.get("units", "count")),
                "raw_fill_value_code": fill_value,
            }

    return counts.astype(np.float32), {"units": normalize_raw_count_unit(counts.attrs.get("units", "count"))}


def satpy_channel_dataset(
    filenames: list[str],
    reader: str,
    satellite: str,
    sensor: str,
    native_channel: str,
    channel_row: pd.Series,
    out_root: Path,
) -> tuple[xr.Dataset, Path]:
    """Build a single-channel standardized_L1_source dataset from a Satpy reader."""
    quantity = channel_row["physical_quantity_type"]
    calibrations = ["counts", "radiance"]
    if quantity == "reflectance":
        calibrations.append("reflectance")
    elif quantity == "brightness_temperature":
        calibrations.append("brightness_temperature")

    loaded = {}
    attrs_da = None
    for calibration in calibrations:
        try:
            scene = Scene(filenames=filenames, reader=reader)
            scene.load([native_channel], calibration=calibration)
            da = scene[native_channel].reset_coords(drop=True)
            # Satpy may attach slightly different floating-point x/y coordinate labels to
            # different calibration views of the same native channel. We drop those labels
            # here and keep the raw array shape so all derived fields stay aligned by pixel
            # position on the native grid.
            loaded[calibration] = xr.DataArray(da.data, dims=("y", "x"), attrs=dict(da.attrs)).astype(np.float32)
            attrs_da = da
        except Exception as exc:
            print(f"[WARN] {satellite} {native_channel} calibration={calibration} unavailable: {exc}")

    if attrs_da is None:
        raise RuntimeError(f"No calibration could be loaded for {satellite} {native_channel}.")

    counts = loaded.get("counts")
    if counts is None:
        counts = xr.full_like(next(iter(loaded.values())), np.nan, dtype=np.float32)
    raw_count, raw_count_meta = prepare_raw_count(counts)
    radiance = loaded.get("radiance")
    if radiance is None:
        radiance = xr.full_like(counts, np.nan, dtype=np.float32)
    reflectance = loaded.get("reflectance")
    if reflectance is None:
        reflectance = xr.full_like(counts, np.nan, dtype=np.float32)
    bt = loaded.get("brightness_temperature")
    if bt is None:
        bt = xr.full_like(counts, np.nan, dtype=np.float32)

    preferred = (
        reflectance
        if quantity == "reflectance"
        else bt
        if quantity == "brightness_temperature"
        else radiance
    )
    raw_bad = ~np.isfinite(counts)
    cal_bad = ~np.isfinite(preferred)
    quality = xr.zeros_like(counts, dtype=np.uint16)
    quality = quality.where(~raw_bad, quality | Q_INVALID_RAW)
    quality = quality.where(~cal_bad, quality | Q_CALIBRATION_FAILED)
    valid = (quality == 0).astype(np.uint8)

    start, end, _ = infer_times(attrs_da)
    line_time_offset = np.linspace(0, (end - start).total_seconds(), counts.sizes["y"], dtype=np.float32)
    area = attrs_da.attrs.get("area")
    if area is None:
        raise RuntimeError(f"{satellite} {native_channel} has no Satpy area definition.")

    channel_vars = {
        "native_channel_id": [channel_row["native_channel_id"]],
        "standard_channel_slot": [channel_row["standard_channel_slot"]],
        "standard_channel_id": [channel_row["standard_channel_id"]],
        "channel_group": [channel_row["channel_group"]],
        "physical_quantity_type": [channel_row["physical_quantity_type"]],
        "standard_units": [channel_row["standard_units"]],
        "raw_data_type": [channel_row["raw_data_type"]],
        "calibration_type": [channel_row["calibration_type"]],
        "channel_description": [channel_row["description"]],
        "native_channel_center_um": np.asarray([channel_row["native_channel_center_um"]], dtype=np.float32),
        "central_wavelength_um": np.asarray([channel_row["central_wavelength_um"]], dtype=np.float32),
        "native_spatial_resolution_km": np.asarray([channel_row["native_spatial_resolution_km"]], dtype=np.float32),
        "channel_presence_flag": np.asarray([1], dtype=np.uint8),
        "channel_data_available_flag": np.asarray([1], dtype=np.uint8),
    }

    projection_x_values = np.asarray(attrs_da["x"].values, dtype=np.float32)
    projection_y_values = np.asarray(attrs_da["y"].values, dtype=np.float32)

    ds = xr.Dataset(
        data_vars={
            "raw_count": raw_count.expand_dims(channel=[0]),
            "radiance": radiance.where(valid == 1).expand_dims(channel=[0]),
            "reflectance": reflectance.where(valid == 1).expand_dims(channel=[0]),
            "brightness_temperature": bt.where(valid == 1).expand_dims(channel=[0]),
            "calibrated_value": preferred.where(valid == 1).expand_dims(channel=[0]),
            "valid_mask": valid.expand_dims(channel=[0]),
            "quality_flag": quality.expand_dims(channel=[0]),
            "projection_x": (("x",), projection_x_values),
            "projection_y": (("y",), projection_y_values),
            "time": ((), epoch_seconds(start)),
            "observation_start_time": ((), epoch_seconds(start)),
            "observation_end_time": ((), epoch_seconds(end)),
            "line_time_offset": (("y",), line_time_offset),
            "goes_imager_projection": ((), np.int32(0)),
            **{k: (("channel",), v) for k, v in channel_vars.items()},
        },
        coords={"channel": np.asarray([0], dtype=np.int16), "y": projection_y_values, "x": projection_x_values},
    )

    ds.attrs.update(
        {
            "Conventions": "CF-1.8",
            "product_name": "standardized_L1_source",
            "product_version": PRODUCT_VERSION,
            "institution": "AAAresearch_paper third_report",
            "processing_software_version": f"Satpy standardized_L1_source builder {PRODUCT_VERSION}",
            "satellite_id": satellite,
            "platform_name": str(attrs_da.attrs.get("platform_name", satellite)),
            "sensor_id": sensor,
            "scene_type": "FD",
            "source_file": ";".join(filenames[:5]) + (";..." if len(filenames) > 5 else ""),
            "source_file_format": reader,
            "source_l1b_version": "",
            "calibration_reference": f"Satpy reader={reader}; calibrations={','.join(loaded.keys())}",
            "spectral_response_reference": "Native channel metadata; SRF file not attached in v0.2",
            "observation_start_time_utc": start.isoformat(),
            "observation_end_time_utc": end.isoformat(),
            "nominal_time_utc": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "geolocation_method": "satpy_native_area_definition",
            "angle_computation_method": "not_written_projection_geometry_available",
            "notes": "Written per native channel to preserve native resolution. Latitude/longitude and angles are derivable from projection_x/projection_y plus grid_mapping and are deferred to the L1g mapper.",
        }
    )

    for name in ["raw_count", "radiance", "reflectance", "brightness_temperature", "calibrated_value", "valid_mask", "quality_flag"]:
        ds[name].attrs = {}
    for name in ["time", "observation_start_time", "observation_end_time"]:
        ds[name].attrs.update({"units": TIME_UNITS, "calendar": "standard"})
    ds["line_time_offset"].attrs.update(
        {
            "units": "seconds",
            "long_name": "line time offset from observation start",
            "reference_time": "observation_start_time",
        }
    )
    ds["raw_count"].attrs.update({"long_name": f"{satellite} {native_channel} native counts", "units": raw_count_meta["units"]})
    if "raw_fill_value_code" in raw_count_meta:
        ds["raw_count"].attrs["raw_fill_value_code"] = raw_count_meta["raw_fill_value_code"]
    ds["radiance"].attrs.update({"long_name": "spectral radiance", "units": str(radiance.attrs.get("units", "")), "grid_mapping": "goes_imager_projection"})
    ds["reflectance"].attrs.update({"long_name": "reflectance", "units": "1", "grid_mapping": "goes_imager_projection"})
    ds["brightness_temperature"].attrs.update({"long_name": "brightness temperature", "units": "K", "grid_mapping": "goes_imager_projection"})
    ds["calibrated_value"].attrs.update({"long_name": "preferred calibrated value for this channel", "units": "channel-dependent; see physical_quantity_type and standard_units", "grid_mapping": "goes_imager_projection"})
    ds["valid_mask"].attrs.update({"long_name": "valid pixel mask", "flag_values": "0, 1", "flag_meanings": "invalid valid"})
    ds["quality_flag"].attrs.update({"long_name": "quality bit field", "flag_masks": "1, 8", "flag_meanings": "invalid_raw calibration_failed"})
    ds["projection_x"].attrs.update({"units": normalize_projection_unit(attrs_da["x"].attrs.get("units", "m")), "long_name": "native geostationary projection x coordinate axis"})
    ds["projection_y"].attrs.update({"units": normalize_projection_unit(attrs_da["y"].attrs.get("units", "m")), "long_name": "native geostationary projection y coordinate axis"})
    ds["goes_imager_projection"].attrs.update(area_grid_mapping_attrs(area))

    safe_sat = satellite.replace("-", "")
    safe_ch = re.sub(r"[^A-Za-z0-9_]+", "_", native_channel)
    out_dir = out_root / satellite / safe_ch
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{safe_sat}_{sensor}_FD_{safe_ch}_{start.strftime('%Y%m%d_%H%M')}_standardized_L1_source_{PRODUCT_VERSION}.nc"
    return ds, out_path


def write_dataset(ds: xr.Dataset, out_path: Path) -> Path:
    """Write a standardized_L1_source dataset using compressed NetCDF4 encoding."""
    encoding = {}
    for name in ds.data_vars:
        if ds[name].dtype.kind in "fiu":
            encoding[name] = {"zlib": True, "complevel": 4, "shuffle": True}
    for name in ["radiance", "reflectance", "brightness_temperature", "calibrated_value"]:
        encoding[name].update({"_FillValue": np.float32(np.nan)})
    if ds["raw_count"].dtype.kind == "f":
        encoding["raw_count"].update({"_FillValue": np.float32(np.nan)})
    for name in ["time", "observation_start_time", "observation_end_time", "line_time_offset", "goes_imager_projection"]:
        encoding[name] = {"_FillValue": None}
    ds.to_netcdf(out_path, engine="netcdf4", encoding=encoding, compute=True)
    return out_path
