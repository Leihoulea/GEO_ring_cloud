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
Q_INVALID_GEOLOCATION = np.uint16(2)
Q_OFF_DISK = np.uint16(4)
Q_CALIBRATION_FAILED = np.uint16(8)


ABI_CHANNELS = [
    # native_channel_id, standard_channel_slot, channel_group, center_um,
    # physical_quantity_type, standard_units, native_spatial_resolution_km, description
    ("C01", "VIS_BLUE", "extension", 0.47, "reflectance", "1", 1.0, "Blue visible"),
    ("C02", "VIS_RED", "core_common", 0.64, "reflectance", "1", 0.5, "Red visible"),
    ("C03", "NIR_VEG", "core_common", 0.86, "reflectance", "1", 1.0, "Vegetation near IR"),
    ("C04", "CIRRUS_138", "extension", 1.37, "reflectance", "1", 2.0, "Cirrus"),
    ("C05", "NIR_SNOW", "core_common", 1.61, "reflectance", "1", 1.0, "Snow/ice near IR"),
    ("C06", "SWIR_CLOUD", "extension", 2.25, "reflectance", "1", 2.0, "Cloud particle SWIR"),
    ("C07", "SWIR_FOG", "core_common", 3.90, "brightness_temperature", "K", 2.0, "Shortwave IR"),
    ("C08", "WV_UPPER", "core_common", 6.20, "brightness_temperature", "K", 2.0, "Upper water vapor"),
    ("C09", "WV_MID", "extension", 6.90, "brightness_temperature", "K", 2.0, "Mid water vapor"),
    ("C10", "WV_LOWER", "core_common", 7.30, "brightness_temperature", "K", 2.0, "Lower water vapor"),
    ("C11", "IR_86", "core_common", 8.40, "brightness_temperature", "K", 2.0, "IR 8.4"),
    ("C12", "OZONE_96", "extension", 9.60, "brightness_temperature", "K", 2.0, "Ozone"),
    ("C13", "IR_WINDOW_LOWER", "core_common", 10.35, "brightness_temperature", "K", 2.0, "Clean IR window"),
    ("C14", "IR_WINDOW_MAIN", "extension", 11.20, "brightness_temperature", "K", 2.0, "Longwave IR window"),
    ("C15", "IR_SPLIT_WINDOW", "core_common", 12.30, "brightness_temperature", "K", 2.0, "Split window"),
    ("C16", "CO2_133", "core_common", 13.30, "brightness_temperature", "K", 2.0, "CO2 longwave IR"),
]


def find_project_root(start: str | Path | None = None) -> Path:
    """Return the project root that contains Satellite_Data_20240312."""
    start_path = Path.cwd() if start is None else Path(start).resolve()
    for path in [start_path, *start_path.parents]:
        if (path / "Satellite_Data_20240312").exists():
            return path
    raise FileNotFoundError("Cannot find Satellite_Data_20240312.")


def channel_table() -> pd.DataFrame:
    """Return the ABI channel metadata table used by the standardized writer."""
    channel_df = pd.DataFrame(
        ABI_CHANNELS,
        columns=[
            "native_channel_id",
            "standard_channel_slot",
            "channel_group",
            "native_channel_center_um",
            "physical_quantity_type",
            "standard_units",
            "native_spatial_resolution_km",
            "description",
        ],
    )
    channel_df["standard_channel_id"] = channel_df["standard_channel_slot"]
    channel_df["central_wavelength_um"] = channel_df["native_channel_center_um"]
    channel_df["raw_data_type"] = "packed_radiance"
    channel_df["calibration_type"] = "scale_factor_add_offset"
    channel_df["channel_presence_flag"] = np.uint8(1)
    return channel_df


def find_goes_files(data_root: Path, satellite: str, hour_utc: str | int) -> list[Path]:
    """Find all ABI full-disk L1b files for one satellite and one UTC hour."""
    hour_folder = f"{int(hour_utc):02d}"
    file_list = sorted((data_root / satellite / hour_folder).glob("OR_ABI-L1b-RadF-*.nc"))
    if not file_list:
        raise FileNotFoundError(data_root / satellite / hour_folder)
    return file_list


def channel_from_filename(path: Path) -> str:
    """Extract the ABI channel ID such as C13 from a standard GOES filename."""
    match = re.search(r"M\dC(?P<channel>\d{2})_", path.name)
    if not match:
        raise ValueError(path.name)
    return f"C{match.group('channel')}"


def parse_abi_time_value(value: str) -> datetime:
    """Parse a GOES time string such as 2024-03-12T18:00:20.5Z."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def epoch_seconds(dt: datetime) -> np.float64:
    """Convert a timezone-aware datetime to Unix epoch seconds."""
    return np.float64(dt.timestamp())


def scalar(dataset: xr.Dataset, variable_name: str, default=np.nan):
    """Read a scalar variable from a dataset and fall back to a default value."""
    if variable_name not in dataset:
        return default
    value = dataset[variable_name].values
    return value.item() if np.asarray(value).shape == () else value


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


def infer_satpy_times(da) -> tuple[datetime, datetime]:
    """Extract start and end time from a Satpy DataArray."""
    start = da.attrs.get("start_time")
    end = da.attrs.get("end_time")
    if start is None:
        start = da.attrs.get("time_parameters", {}).get("nominal_start_time")
    if end is None:
        end = da.attrs.get("time_parameters", {}).get("nominal_end_time")
    if start is None:
        raise ValueError("Satpy dataset has no start_time.")
    if end is None:
        end = start
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    else:
        start = start.astimezone(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    else:
        end = end.astimezone(timezone.utc)
    return start, end


def build_channel_dataset_direct(
    path: Path,
    satellite: str,
    output_root: Path,
    chunks="auto",
) -> tuple[xr.Dataset, Path]:
    """Build one standardized_L1_source file for one ABI native channel."""
    channel_id = channel_from_filename(path)
    channel_row = channel_table().loc[lambda df: df["native_channel_id"].eq(channel_id)].iloc[0]

    source_ds = xr.open_dataset(path, decode_times=False, mask_and_scale=False, chunks=chunks)

    # ABI stores Rad as packed integer values. Keep that packed layer in raw_count and
    # decode radiance explicitly so the source layer and calibrated layer stay separate.
    raw_count = source_ds["Rad"].astype(np.int16).expand_dims(channel=[0])
    radiance_scale = np.float32(source_ds["Rad"].attrs.get("scale_factor", 1.0))
    radiance_offset = np.float32(source_ds["Rad"].attrs.get("add_offset", 0.0))
    radiance_2d = (source_ds["Rad"].astype(np.float32) * radiance_scale + radiance_offset).astype(np.float32)

    valid_min, valid_max = [
        int(v) for v in np.asarray(source_ds["Rad"].attrs.get("valid_range", [-32768, 32767])).ravel()
    ]
    raw_fill_value = int(source_ds["Rad"].attrs.get("_FillValue", -9999))
    raw_invalid_mask = (
        (source_ds["Rad"] == raw_fill_value)
        | (source_ds["Rad"] < valid_min)
        | (source_ds["Rad"] > valid_max)
    )
    calibrated_invalid_mask = ~np.isfinite(radiance_2d)

    reflectance_2d = xr.full_like(radiance_2d, np.nan, dtype=np.float32)
    brightness_temperature_2d = xr.full_like(radiance_2d, np.nan, dtype=np.float32)

    if channel_row["physical_quantity_type"] == "reflectance":
        kappa0 = np.float32(scalar(source_ds, "kappa0", np.nan))
        reflectance_2d = (radiance_2d * kappa0).astype(np.float32)
    else:
        fk1 = np.float32(scalar(source_ds, "planck_fk1", np.nan))
        fk2 = np.float32(scalar(source_ds, "planck_fk2", np.nan))
        bc1 = np.float32(scalar(source_ds, "planck_bc1", np.nan))
        bc2 = np.float32(scalar(source_ds, "planck_bc2", np.nan))
        brightness_temperature_2d = (
            (fk2 / np.log((fk1 / radiance_2d) + 1.0) - bc1) / bc2
        ).astype(np.float32)
        brightness_temperature_2d = brightness_temperature_2d.where(radiance_2d > 0)

    quality_flag_2d = xr.zeros_like(source_ds["Rad"], dtype=np.uint16)
    quality_flag_2d = quality_flag_2d.where(~raw_invalid_mask, quality_flag_2d | Q_INVALID_RAW)
    quality_flag_2d = quality_flag_2d.where(
        ~calibrated_invalid_mask,
        quality_flag_2d | Q_CALIBRATION_FAILED,
    )
    valid_mask_2d = (quality_flag_2d == 0).astype(np.uint8)

    preferred_calibrated_2d = (
        reflectance_2d
        if channel_row["physical_quantity_type"] == "reflectance"
        else brightness_temperature_2d
    )
    radiance_2d = radiance_2d.where(valid_mask_2d == 1)
    reflectance_2d = reflectance_2d.where(valid_mask_2d == 1)
    brightness_temperature_2d = brightness_temperature_2d.where(valid_mask_2d == 1)
    preferred_calibrated_2d = preferred_calibrated_2d.where(valid_mask_2d == 1)

    start_time = parse_abi_time_value(source_ds.attrs["time_coverage_start"])
    end_time = parse_abi_time_value(source_ds.attrs["time_coverage_end"])
    line_time_offset = np.linspace(
        0,
        (end_time - start_time).total_seconds(),
        source_ds.sizes["y"],
        dtype=np.float32,
    )

    channel_attrs = {
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
    perspective_point_height = np.float32(
        source_ds["goes_imager_projection"].attrs.get("perspective_point_height")
    )
    projection_x_values = (source_ds["x"].values.astype(np.float32) * perspective_point_height).astype(np.float32)
    projection_y_values = (source_ds["y"].values.astype(np.float32) * perspective_point_height).astype(np.float32)

    dataset = xr.Dataset(
        data_vars={
            "raw_count": raw_count,
            "radiance": radiance_2d.expand_dims(channel=[0]),
            "reflectance": reflectance_2d.expand_dims(channel=[0]),
            "brightness_temperature": brightness_temperature_2d.expand_dims(channel=[0]),
            "calibrated_value": preferred_calibrated_2d.expand_dims(channel=[0]),
            "valid_mask": valid_mask_2d.expand_dims(channel=[0]),
            "quality_flag": quality_flag_2d.expand_dims(channel=[0]),
            "projection_x": (("x",), projection_x_values),
            "projection_y": (("y",), projection_y_values),
            "time": ((), epoch_seconds(start_time)),
            "observation_start_time": ((), epoch_seconds(start_time)),
            "observation_end_time": ((), epoch_seconds(end_time)),
            "line_time_offset": (("y",), line_time_offset),
            "goes_imager_projection": source_ds["goes_imager_projection"],
            **{name: (("channel",), values) for name, values in channel_attrs.items()},
        },
        coords={
            "channel": np.asarray([0], dtype=np.int16),
            "y": projection_y_values,
            "x": projection_x_values,
        },
    )

    satellite_id_for_filename = satellite.replace("-", "")
    dataset.attrs.update(
        {
            "Conventions": "CF-1.8",
            "product_name": "standardized_L1_source",
            "product_version": PRODUCT_VERSION,
            "institution": "AAAresearch_paper third_report",
            "processing_software_version": f"GOES ABI standardized_L1_source builder {PRODUCT_VERSION}",
            "satellite_id": satellite,
            "platform_name": satellite,
            "sensor_id": "ABI",
            "scene_type": "FD",
            "source_file": str(path),
            "source_file_format": "GOES-R ABI L1b NetCDF",
            "source_l1b_version": str(source_ds.attrs.get("production_site", "")),
            "calibration_reference": "GOES ABI L1b Rad scale_factor/add_offset and Planck/kappa0 variables",
            "spectral_response_reference": "Native ABI band metadata; SRF file not attached in v0.2",
            "observation_start_time_utc": start_time.isoformat(),
            "observation_end_time_utc": end_time.isoformat(),
            "nominal_time_utc": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "geolocation_method": "official_fixed_grid_projection",
            "angle_computation_method": "not_written_projection_geometry_available",
            "notes": "GOES v0.2 is written per native channel to preserve native resolution. Latitude/longitude and angles are derivable from projection_x/projection_y plus goes_imager_projection and are deferred to the L1g mapper.",
        }
    )

    for variable_name in [
        "raw_count",
        "radiance",
        "reflectance",
        "brightness_temperature",
        "calibrated_value",
        "valid_mask",
        "quality_flag",
    ]:
        dataset[variable_name].attrs = {}

    for variable_name in ["time", "observation_start_time", "observation_end_time"]:
        dataset[variable_name].attrs.update({"units": TIME_UNITS, "calendar": "standard"})

    dataset["line_time_offset"].attrs.update(
        {
            "units": "seconds",
            "long_name": "line time offset from observation start",
            "reference_time": "observation_start_time",
        }
    )
    dataset["raw_count"].attrs.update(
        {
            "long_name": "GOES ABI packed Rad count",
            "units": "packed_radiance",
            "raw_fill_value_code": np.int16(raw_fill_value),
        }
    )
    dataset["radiance"].attrs.update(
        {
            "long_name": "spectral radiance",
            "units": source_ds["Rad"].attrs.get("units", ""),
            "grid_mapping": "goes_imager_projection",
        }
    )
    dataset["reflectance"].attrs.update(
        {"long_name": "reflectance", "units": "1", "grid_mapping": "goes_imager_projection"}
    )
    dataset["brightness_temperature"].attrs.update(
        {
            "long_name": "brightness temperature",
            "units": "K",
            "grid_mapping": "goes_imager_projection",
        }
    )
    dataset["calibrated_value"].attrs.update(
        {
            "long_name": "preferred calibrated value for this channel",
            "units": "channel-dependent; see physical_quantity_type and standard_units",
            "grid_mapping": "goes_imager_projection",
        }
    )
    dataset["valid_mask"].attrs.update(
        {"long_name": "valid pixel mask", "flag_values": "0, 1", "flag_meanings": "invalid valid"}
    )
    dataset["quality_flag"].attrs.update(
        {
            "long_name": "quality bit field",
            "flag_masks": "1, 2, 4, 8",
            "flag_meanings": "invalid_raw invalid_geolocation off_disk calibration_failed",
        }
    )
    dataset["projection_x"].attrs.update(
        {
            "units": "m",
            "long_name": "ABI geostationary projection x coordinate axis",
        }
    )
    dataset["projection_y"].attrs.update(
        {
            "units": "m",
            "long_name": "ABI geostationary projection y coordinate axis",
        }
    )

    output_dir = output_root / satellite / channel_id
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / (
        f"{satellite_id_for_filename}_ABI_FD_{channel_id}_{start_time.strftime('%Y%m%d_%H%M')}"
        f"_standardized_L1_source_{PRODUCT_VERSION}.nc"
    )
    return dataset, output_path


def build_channel_dataset_satpy(
    path: Path,
    satellite: str,
    output_root: Path,
) -> tuple[xr.Dataset, Path]:
    """Build one GOES standardized file using Satpy as the reader/calibration backend."""
    channel_id = channel_from_filename(path)
    channel_row = channel_table().loc[lambda df: df["native_channel_id"].eq(channel_id)].iloc[0]
    source_ds = xr.open_dataset(path, decode_times=False, mask_and_scale=False)
    raw = source_ds["Rad"]
    raw_fill_value = int(raw.attrs.get("_FillValue", -9999))

    requested_calibrations = ["counts", "radiance"]
    if channel_row["physical_quantity_type"] == "reflectance":
        requested_calibrations.append("reflectance")
    else:
        requested_calibrations.append("brightness_temperature")

    loaded: dict[str, xr.DataArray] = {}
    attrs_da = None
    for calibration in requested_calibrations:
        scene = Scene(filenames=[str(path)], reader="abi_l1b")
        scene.load([channel_id], calibration=calibration)
        da = scene[channel_id].reset_coords(drop=True)
        loaded[calibration] = xr.DataArray(da.data, dims=("y", "x"), attrs=dict(da.attrs))
        attrs_da = da

    assert attrs_da is not None
    counts = loaded["counts"].astype(np.int16)
    radiance = loaded["radiance"].astype(np.float32)
    reflectance = xr.full_like(radiance, np.nan, dtype=np.float32)
    brightness_temperature = xr.full_like(radiance, np.nan, dtype=np.float32)

    if "reflectance" in loaded:
        reflectance = loaded["reflectance"].astype(np.float32)
        if str(loaded["reflectance"].attrs.get("units", "")) == "%":
            reflectance = reflectance / np.float32(100.0)
    if "brightness_temperature" in loaded:
        brightness_temperature = loaded["brightness_temperature"].astype(np.float32)

    raw_invalid_mask = counts == np.int16(raw_fill_value)
    preferred = (
        reflectance
        if channel_row["physical_quantity_type"] == "reflectance"
        else brightness_temperature
    )
    calibrated_invalid_mask = ~np.isfinite(preferred)

    quality_flag_2d = xr.zeros_like(counts, dtype=np.uint16)
    quality_flag_2d = quality_flag_2d.where(~raw_invalid_mask, quality_flag_2d | Q_INVALID_RAW)
    quality_flag_2d = quality_flag_2d.where(~calibrated_invalid_mask, quality_flag_2d | Q_CALIBRATION_FAILED)
    valid_mask_2d = (quality_flag_2d == 0).astype(np.uint8)

    radiance = radiance.where(valid_mask_2d == 1)
    reflectance = reflectance.where(valid_mask_2d == 1)
    brightness_temperature = brightness_temperature.where(valid_mask_2d == 1)
    preferred = preferred.where(valid_mask_2d == 1)

    start_time, end_time = infer_satpy_times(attrs_da)
    line_time_offset = np.linspace(
        0,
        (end_time - start_time).total_seconds(),
        counts.sizes["y"],
        dtype=np.float32,
    )

    channel_attrs = {
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
    area = attrs_da.attrs.get("area")
    if area is None:
        raise RuntimeError(f"{satellite} {channel_id} missing Satpy area definition.")

    dataset = xr.Dataset(
        data_vars={
            "raw_count": counts.expand_dims(channel=[0]),
            "radiance": radiance.expand_dims(channel=[0]),
            "reflectance": reflectance.expand_dims(channel=[0]),
            "brightness_temperature": brightness_temperature.expand_dims(channel=[0]),
            "calibrated_value": preferred.expand_dims(channel=[0]),
            "valid_mask": valid_mask_2d.expand_dims(channel=[0]),
            "quality_flag": quality_flag_2d.expand_dims(channel=[0]),
            "projection_x": (("x",), projection_x_values),
            "projection_y": (("y",), projection_y_values),
            "time": ((), epoch_seconds(start_time)),
            "observation_start_time": ((), epoch_seconds(start_time)),
            "observation_end_time": ((), epoch_seconds(end_time)),
            "line_time_offset": (("y",), line_time_offset),
            "goes_imager_projection": ((), np.int32(0)),
            **{name: (("channel",), values) for name, values in channel_attrs.items()},
        },
        coords={
            "channel": np.asarray([0], dtype=np.int16),
            "y": projection_y_values,
            "x": projection_x_values,
        },
    )

    satellite_id_for_filename = satellite.replace("-", "")
    dataset.attrs.update(
        {
            "Conventions": "CF-1.8",
            "product_name": "standardized_L1_source",
            "product_version": PRODUCT_VERSION,
            "institution": "AAAresearch_paper third_report",
            "processing_software_version": f"GOES ABI standardized_L1_source builder {PRODUCT_VERSION}; backend=satpy",
            "satellite_id": satellite,
            "platform_name": satellite,
            "sensor_id": "ABI",
            "scene_type": "FD",
            "source_file": str(path),
            "source_file_format": "GOES-R ABI L1b NetCDF",
            "source_l1b_version": str(source_ds.attrs.get("production_site", "")),
            "calibration_reference": "Satpy reader=abi_l1b with GOES-R official source variables",
            "spectral_response_reference": "Native ABI band metadata; SRF file not attached in v0.2",
            "observation_start_time_utc": start_time.isoformat(),
            "observation_end_time_utc": end_time.isoformat(),
            "nominal_time_utc": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "geolocation_method": "satpy_native_area_definition",
            "angle_computation_method": "not_written_projection_geometry_available",
            "notes": "GOES v0.2 is written per native channel to preserve native resolution. Latitude/longitude and angles are derivable from projection_x/projection_y plus goes_imager_projection and are deferred to the L1g mapper.",
        }
    )

    for variable_name in [
        "raw_count",
        "radiance",
        "reflectance",
        "brightness_temperature",
        "calibrated_value",
        "valid_mask",
        "quality_flag",
    ]:
        dataset[variable_name].attrs = {}

    for variable_name in ["time", "observation_start_time", "observation_end_time"]:
        dataset[variable_name].attrs.update({"units": TIME_UNITS, "calendar": "standard"})

    dataset["line_time_offset"].attrs.update(
        {
            "units": "seconds",
            "long_name": "line time offset from observation start",
            "reference_time": "observation_start_time",
        }
    )
    dataset["raw_count"].attrs.update(
        {
            "long_name": "GOES ABI packed Rad count",
            "units": "packed_radiance",
            "raw_fill_value_code": np.int16(raw_fill_value),
        }
    )
    dataset["radiance"].attrs.update(
        {
            "long_name": "spectral radiance",
            "units": str(loaded["radiance"].attrs.get("units", "")),
            "grid_mapping": "goes_imager_projection",
        }
    )
    dataset["reflectance"].attrs.update(
        {"long_name": "reflectance", "units": "1", "grid_mapping": "goes_imager_projection"}
    )
    dataset["brightness_temperature"].attrs.update(
        {
            "long_name": "brightness temperature",
            "units": "K",
            "grid_mapping": "goes_imager_projection",
        }
    )
    dataset["calibrated_value"].attrs.update(
        {
            "long_name": "preferred calibrated value for this channel",
            "units": "channel-dependent; see physical_quantity_type and standard_units",
            "grid_mapping": "goes_imager_projection",
        }
    )
    dataset["valid_mask"].attrs.update(
        {"long_name": "valid pixel mask", "flag_values": "0, 1", "flag_meanings": "invalid valid"}
    )
    dataset["quality_flag"].attrs.update(
        {
            "long_name": "quality bit field",
            "flag_masks": "1, 2, 4, 8",
            "flag_meanings": "invalid_raw invalid_geolocation off_disk calibration_failed",
        }
    )
    dataset["projection_x"].attrs.update(
        {"units": "m", "long_name": "ABI geostationary projection x coordinate axis"}
    )
    dataset["projection_y"].attrs.update(
        {"units": "m", "long_name": "ABI geostationary projection y coordinate axis"}
    )
    dataset["goes_imager_projection"].attrs.update(area_grid_mapping_attrs(area))

    output_dir = output_root / satellite / channel_id
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / (
        f"{satellite_id_for_filename}_ABI_FD_{channel_id}_{start_time.strftime('%Y%m%d_%H%M')}"
        f"_standardized_L1_source_{PRODUCT_VERSION}.nc"
    )
    return dataset, output_path


def build_channel_dataset(
    path: Path,
    satellite: str,
    output_root: Path,
    *,
    backend: str = "direct",
    chunks="auto",
) -> tuple[xr.Dataset, Path]:
    """Build one standardized GOES channel dataset using the requested backend."""
    if backend == "direct":
        return build_channel_dataset_direct(path, satellite, output_root, chunks=chunks)
    if backend == "satpy":
        return build_channel_dataset_satpy(path, satellite, output_root)
    raise ValueError(f"Unsupported backend: {backend}")


def write_dataset(dataset: xr.Dataset, out_path: Path) -> Path:
    """Write a single-channel GOES dataset with compressed NetCDF encoding."""
    encoding = {}
    for variable_name in dataset.data_vars:
        if dataset[variable_name].dtype.kind in "fiu":
            encoding[variable_name] = {"zlib": True, "complevel": 4, "shuffle": True}
    for variable_name in ["radiance", "reflectance", "brightness_temperature", "calibrated_value"]:
        encoding[variable_name].update({"_FillValue": np.float32(np.nan)})
    for variable_name in [
        "time",
        "observation_start_time",
        "observation_end_time",
        "line_time_offset",
        "goes_imager_projection",
    ]:
        encoding[variable_name] = {"_FillValue": None}
    dataset.to_netcdf(out_path, engine="netcdf4", encoding=encoding, compute=True)
    return out_path


def build_and_write(
    satellite: str,
    target_hour_utc: str = "18",
    channels: list[str] | None = None,
    project_root: str | Path | None = None,
    backend: str = "satpy",
) -> list[Path]:
    """Build standardized files for one GOES satellite and one UTC hour."""
    project_root_path = find_project_root(project_root)
    data_root = project_root_path / "Satellite_Data_20240312"
    output_root = data_root / "standardized_L1_source"
    source_files = find_goes_files(data_root, satellite, target_hour_utc)
    requested_channels = set(channels) if channels is not None else None

    outputs = []
    for path in source_files:
        channel_id = channel_from_filename(path)
        if requested_channels is not None and channel_id not in requested_channels:
            continue
        print(f"[{satellite}] {channel_id}: {path.name} (backend={backend})")
        dataset, output_path = build_channel_dataset(path, satellite, output_root, backend=backend)
        write_dataset(dataset, output_path)
        print(f"  -> {output_path} ({output_path.stat().st_size / 1024 / 1024:.2f} MB)")
        outputs.append(output_path)
    return outputs


if __name__ == "__main__":
    build_and_write("GOES-16", "18", channels=["C13"])
