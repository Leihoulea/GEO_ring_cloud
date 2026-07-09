from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "code") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "code"))

from standardized_l1_source_satpy import (
    find_project_root,
    satpy_channel_dataset,
    write_dataset,
)


SEVIRI_CHANNELS = [
    # native_channel_id, standard_channel_slot, channel_group, center_um,
    # physical_quantity_type, standard_units, calibration_type,
    # native_spatial_resolution_km, description
    ("VIS006", "VIS_RED", "core_common", 0.635, "reflectance", "1", "satpy_reflectance", 3.0, "Visible red"),
    ("VIS008", "NIR_VEG", "core_common", 0.81, "reflectance", "1", "satpy_reflectance", 3.0, "Near IR vegetation"),
    ("IR_016", "NIR_SNOW", "core_common", 1.64, "reflectance", "1", "satpy_reflectance", 3.0, "Snow/ice near IR"),
    ("IR_039", "SWIR_FOG", "core_common", 3.92, "brightness_temperature", "K", "satpy_brightness_temperature", 3.0, "Shortwave IR"),
    ("WV_062", "WV_UPPER", "core_common", 6.25, "brightness_temperature", "K", "satpy_brightness_temperature", 3.0, "Upper water vapor"),
    ("WV_073", "WV_LOWER", "core_common", 7.35, "brightness_temperature", "K", "satpy_brightness_temperature", 3.0, "Lower water vapor"),
    ("IR_087", "IR_86", "core_common", 8.70, "brightness_temperature", "K", "satpy_brightness_temperature", 3.0, "IR 8.7"),
    ("IR_097", "OZONE_96", "extension", 9.66, "brightness_temperature", "K", "satpy_brightness_temperature", 3.0, "Ozone"),
    ("IR_108", "IR_WINDOW_MAIN", "core_common", 10.80, "brightness_temperature", "K", "satpy_brightness_temperature", 3.0, "Clean IR window"),
    ("IR_120", "IR_SPLIT_WINDOW", "core_common", 12.00, "brightness_temperature", "K", "satpy_brightness_temperature", 3.0, "Split window"),
    ("IR_134", "CO2_133", "core_common", 13.40, "brightness_temperature", "K", "satpy_brightness_temperature", 3.0, "CO2 longwave IR"),
    ("HRV", "VIS_BROAD_HRV", "extension", float("nan"), "reflectance", "1", "satpy_reflectance", 1.0, "Broadband high resolution visible"),
]


def channel_table() -> pd.DataFrame:
    """Return the SEVIRI channel metadata table used by the standardized writer."""
    channel_df = pd.DataFrame(
        SEVIRI_CHANNELS,
        columns=[
            "native_channel_id",
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
    channel_df["standard_channel_id"] = channel_df["standard_channel_slot"]
    channel_df["central_wavelength_um"] = channel_df["native_channel_center_um"]
    channel_df["raw_data_type"] = "count"
    return channel_df


def find_meteosat_native_file(data_root: Path, satellite: str, hour_utc: str | int) -> list[str]:
    """Find one SEVIRI native full-disk file for a target UTC hour."""
    hour_string = f"{int(hour_utc):02d}"
    file_list = sorted((data_root / satellite).rglob(f"*20240312{hour_string}*.nat"))
    if not file_list:
        raise FileNotFoundError(f"{satellite} hour {hour_utc}")
    return [str(file_list[0])]


def build_and_write(
    satellite: str,
    target_hour_utc: str,
    channels: list[str] | None = None,
    project_root: str | Path | None = None,
) -> list[Path]:
    """Build standardized SEVIRI files for one satellite and one UTC hour."""
    project_root_path = find_project_root(project_root)
    data_root = project_root_path / "Satellite_Data_20240312"
    output_root = data_root / "standardized_L1_source"
    source_files = find_meteosat_native_file(data_root, satellite, target_hour_utc)
    channel_df = channel_table()
    requested_channels = set(channels) if channels is not None else set(channel_df["native_channel_id"])

    outputs = []
    for _, channel_row in channel_df.iterrows():
        native_channel_id = channel_row["native_channel_id"]
        if native_channel_id not in requested_channels:
            continue
        print(f"[{satellite}] {native_channel_id}")
        dataset, output_path = satpy_channel_dataset(
            source_files,
            "seviri_l1b_native",
            satellite,
            "SEVIRI",
            native_channel_id,
            channel_row,
            output_root,
        )
        write_dataset(dataset, output_path)
        print(f"  -> {output_path} ({output_path.stat().st_size / 1024 / 1024:.2f} MB)")
        outputs.append(output_path)
    return outputs


if __name__ == "__main__":
    build_and_write("Meteosat-10", "12", channels=["IR_108"])
