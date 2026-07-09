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


AHI_CHANNELS = [
    # native_channel_id, standard_channel_slot, channel_group, center_um,
    # physical_quantity_type, standard_units, calibration_type,
    # native_spatial_resolution_km, description
    ("B01", "VIS_BLUE", "extension", 0.47, "reflectance", "1", "satpy_reflectance", 1.0, "Blue visible"),
    ("B02", "VIS_GREEN", "extension", 0.51, "reflectance", "1", "satpy_reflectance", 1.0, "Green visible"),
    ("B03", "VIS_RED", "core_common", 0.64, "reflectance", "1", "satpy_reflectance", 0.5, "Red visible"),
    ("B04", "NIR_VEG", "core_common", 0.86, "reflectance", "1", "satpy_reflectance", 1.0, "Vegetation near IR"),
    ("B05", "NIR_SNOW", "core_common", 1.61, "reflectance", "1", "satpy_reflectance", 2.0, "Snow/ice near IR"),
    ("B06", "SWIR_CLOUD", "extension", 2.30, "reflectance", "1", "satpy_reflectance", 2.0, "Cloud particle SWIR"),
    ("B07", "SWIR_FOG", "core_common", 3.90, "brightness_temperature", "K", "satpy_brightness_temperature", 2.0, "Shortwave IR"),
    ("B08", "WV_UPPER", "core_common", 6.20, "brightness_temperature", "K", "satpy_brightness_temperature", 2.0, "Upper water vapor"),
    ("B09", "WV_MID", "extension", 6.90, "brightness_temperature", "K", "satpy_brightness_temperature", 2.0, "Mid water vapor"),
    ("B10", "WV_LOWER", "core_common", 7.30, "brightness_temperature", "K", "satpy_brightness_temperature", 2.0, "Lower water vapor"),
    ("B11", "IR_86", "core_common", 8.60, "brightness_temperature", "K", "satpy_brightness_temperature", 2.0, "IR 8.6"),
    ("B12", "OZONE_96", "extension", 9.60, "brightness_temperature", "K", "satpy_brightness_temperature", 2.0, "Ozone"),
    ("B13", "IR_WINDOW_LOWER", "core_common", 10.40, "brightness_temperature", "K", "satpy_brightness_temperature", 2.0, "Clean IR window"),
    ("B14", "IR_WINDOW_MAIN", "extension", 11.20, "brightness_temperature", "K", "satpy_brightness_temperature", 2.0, "Longwave IR window"),
    ("B15", "IR_SPLIT_WINDOW", "core_common", 12.40, "brightness_temperature", "K", "satpy_brightness_temperature", 2.0, "Split window"),
    ("B16", "CO2_133", "core_common", 13.30, "brightness_temperature", "K", "satpy_brightness_temperature", 2.0, "CO2 longwave IR"),
]


def channel_table() -> pd.DataFrame:
    """Return the AHI channel metadata table used by the standardized writer."""
    channel_df = pd.DataFrame(
        AHI_CHANNELS,
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


def find_himawari_files(data_root: Path, hour_utc: str | int) -> list[str]:
    """Find all Himawari Standard Data files for one UTC hour."""
    hour_folder = f"{int(hour_utc):02d}"
    hour_root = data_root / "Himawari-9" / hour_folder
    dat_files = sorted(str(path) for path in hour_root.rglob("*.DAT") if path.is_file())
    if dat_files:
        return dat_files

    compressed_files = sorted(str(path) for path in hour_root.rglob("*.DAT.bz2") if path.is_file())
    if compressed_files:
        return compressed_files

    raise FileNotFoundError(hour_root)


def build_and_write(
    target_hour_utc: str = "03",
    channels: list[str] | None = None,
    project_root: str | Path | None = None,
) -> list[Path]:
    """Build standardized AHI files for one UTC hour."""
    project_root_path = find_project_root(project_root)
    data_root = project_root_path / "Satellite_Data_20240312"
    output_root = data_root / "standardized_L1_source"
    source_files = find_himawari_files(data_root, target_hour_utc)
    channel_df = channel_table()
    requested_channels = set(channels) if channels is not None else set(channel_df["native_channel_id"])

    outputs = []
    for _, channel_row in channel_df.iterrows():
        native_channel_id = channel_row["native_channel_id"]
        if native_channel_id not in requested_channels:
            continue
        print(f"[Himawari-9] {native_channel_id}")
        dataset, output_path = satpy_channel_dataset(
            source_files,
            "ahi_hsd",
            "Himawari-9",
            "AHI",
            native_channel_id,
            channel_row,
            output_root,
        )
        write_dataset(dataset, output_path)
        print(f"  -> {output_path} ({output_path.stat().st_size / 1024 / 1024:.2f} MB)")
        outputs.append(output_path)
    return outputs


if __name__ == "__main__":
    build_and_write("03", channels=["B13"])
