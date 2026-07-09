from __future__ import annotations

"""Validate sample standardized_L1_source files against core project rules.

This validator checks the sample outputs currently used in the project for:

- presence of required core variables
- key dtype expectations
- non-NaN projection axes
- CF-style time variables
- projection-axis unit and semantics consistency

It is intentionally narrower than a full schema validator, but it targets the
exact class of regressions we have already encountered in this project.
"""

from pathlib import Path
import sys

import numpy as np
import xarray as xr


def find_project_root(start: str | Path | None = None) -> Path:
    """Return the project root that contains Satellite_Data_20240312."""
    start_path = Path.cwd() if start is None else Path(start).resolve()
    for path in [start_path, *start_path.parents]:
        if (path / "Satellite_Data_20240312").exists():
            return path
    raise FileNotFoundError("Cannot find Satellite_Data_20240312.")


def sample_files(project_root: Path) -> dict[str, Path]:
    """Return the standardized sample files used for cross-satellite validation."""
    root = project_root / "Satellite_Data_20240312" / "standardized_L1_source"
    return {
        "FY4B": root / "FY4B" / "FY4B_AGRI_FD_20240312_0300_standardized_L1_source_v0.2.nc",
        "GOES-18": root / "GOES-18" / "C13" / "GOES18_ABI_FD_C13_20240312_2100_standardized_L1_source_v0.2.nc",
        "Himawari-9": root / "Himawari-9" / "B13" / "Himawari9_AHI_FD_B13_20240312_0300_standardized_L1_source_v0.2.nc",
        "Meteosat-9": root / "Meteosat-9" / "IR_108" / "Meteosat9_SEVIRI_FD_IR_108_20240312_0900_standardized_L1_source_v0.2.nc",
    }


def require(condition: bool, message: str) -> None:
    """Raise a descriptive validation error when a condition is false."""
    if not condition:
        raise AssertionError(message)


def validate_file(name: str, path: Path) -> list[str]:
    """Validate one standardized sample file and return human-readable notes."""
    ds = xr.open_dataset(path, decode_times=False, mask_and_scale=False)
    notes: list[str] = []

    required_vars = [
        "raw_count",
        "radiance",
        "reflectance",
        "brightness_temperature",
        "calibrated_value",
        "valid_mask",
        "quality_flag",
        "projection_x",
        "projection_y",
        "time",
        "observation_start_time",
        "observation_end_time",
        "line_time_offset",
    ]
    for var in required_vars:
        require(var in ds, f"{name}: missing required variable `{var}`")

    require(ds["raw_count"].dtype.kind in {"u", "i"}, f"{name}: raw_count must preserve integer raw layer")
    require(ds["valid_mask"].dtype == np.uint8, f"{name}: valid_mask must be uint8")
    require(ds["quality_flag"].dtype == np.uint16, f"{name}: quality_flag must be uint16")
    require(ds["projection_x"].dtype == np.float32, f"{name}: projection_x must be float32")
    require(ds["projection_y"].dtype == np.float32, f"{name}: projection_y must be float32")

    for var in ["projection_x", "projection_y"]:
        units = ds[var].attrs.get("units")
        require(units == "m", f"{name}: {var} units must be `m`, got `{units}`")
        long_name = str(ds[var].attrs.get("long_name", ""))
        require("projection" in long_name.lower(), f"{name}: {var} long_name should describe projection coordinates")
        values = np.asarray(ds[var].values)
        require(np.isfinite(values).all(), f"{name}: {var} contains NaN/Inf")

    for var in ["time", "observation_start_time", "observation_end_time"]:
        require(
            ds[var].attrs.get("units") == "seconds since 1970-01-01 00:00:00 UTC",
            f"{name}: {var} must use the project CF time unit",
        )

    line_units = ds["line_time_offset"].attrs.get("units")
    require(line_units == "seconds", f"{name}: line_time_offset units must be `seconds`")

    require("goes_imager_projection" in ds, f"{name}: grid_mapping variable missing")
    grid_mapping_name = ds["goes_imager_projection"].attrs.get("grid_mapping_name")
    require(grid_mapping_name == "geostationary", f"{name}: grid_mapping_name must be geostationary")

    notes.append(f"{name}: OK")
    notes.append(
        f"  raw_count={ds['raw_count'].dtype}, projection_x={ds['projection_x'].attrs.get('units')}, "
        f"geolocation_method={ds.attrs.get('geolocation_method')}"
    )
    return notes


def main() -> None:
    """Run validation on all configured sample outputs."""
    project_root = find_project_root()
    notes: list[str] = []
    failures: list[str] = []

    for name, path in sample_files(project_root).items():
        try:
            notes.extend(validate_file(name, path))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    print("\n".join(notes))
    if failures:
        print("\nVALIDATION FAILURES:")
        print("\n".join(failures))
        sys.exit(1)
    print("\nAll sample standardized_L1_source files passed validation.")


if __name__ == "__main__":
    main()
