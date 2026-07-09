from __future__ import annotations

"""Self-check script for GOES ABI / Satpy backend readiness.

This script verifies four things:

1. Satpy version and available reader registration.
2. Whether the `abi_l1b` reader is discoverable.
3. Whether representative GOES ABI files can be loaded by `Scene`.
4. Whether Satpy outputs match the current direct official-variable route for:
   - raw packed counts (`Rad`)
   - radiance
   - reflectance (visible channel, after unit normalization)
   - brightness temperature (IR channel)

The goal is not to replace the production builder by itself. Instead, it
provides an evidence-based regression tool for deciding whether GOES can be
confidently run through a Satpy-first backend in the future.
"""

import argparse
import json
from pathlib import Path

import numpy as np
import xarray as xr
from satpy import Scene, available_readers
import satpy


def find_project_root(start: str | Path | None = None) -> Path:
    """Return the project root that contains Satellite_Data_20240312."""
    start_path = Path.cwd() if start is None else Path(start).resolve()
    for path in [start_path, *start_path.parents]:
        if (path / "Satellite_Data_20240312").exists():
            return path
    raise FileNotFoundError("Cannot find Satellite_Data_20240312.")


def representative_files(project_root: Path) -> dict[str, Path]:
    """Return one visible and one thermal representative GOES-16 full-disk file."""
    goes_root = project_root / "Satellite_Data_20240312" / "GOES-16" / "18"
    return {
        "C02": goes_root / "OR_ABI-L1b-RadF-M6C02_G16_s20240721800205_e20240721809513_c20240721809546.nc",
        "C13": goes_root / "OR_ABI-L1b-RadF-M6C13_G16_s20240721800205_e20240721809525_c20240721809572.nc",
    }


def max_abs_diff(a: np.ndarray, b: np.ndarray) -> float:
    """Return the maximum absolute difference between two arrays."""
    diff = np.abs(a.astype(np.float64) - b.astype(np.float64))
    return float(np.nanmax(diff))


def scene_dataset_report(path: Path, channel_id: str) -> dict:
    """Compare Satpy outputs against direct official-variable decoding for one channel."""
    source_ds = xr.open_dataset(path, decode_times=False, mask_and_scale=False)
    raw = np.asarray(source_ds["Rad"].values)
    scale = np.float32(source_ds["Rad"].attrs.get("scale_factor", 1.0))
    offset = np.float32(source_ds["Rad"].attrs.get("add_offset", 0.0))
    direct_radiance = raw.astype(np.float32) * scale + offset

    if channel_id == "C02":
        direct_reflectance = direct_radiance * np.float32(source_ds["kappa0"].item())
    else:
        direct_reflectance = None

    if channel_id == "C13":
        fk1 = np.float32(source_ds["planck_fk1"].item())
        fk2 = np.float32(source_ds["planck_fk2"].item())
        bc1 = np.float32(source_ds["planck_bc1"].item())
        bc2 = np.float32(source_ds["planck_bc2"].item())
        direct_bt = ((fk2 / np.log((fk1 / direct_radiance) + 1.0) - bc1) / bc2).astype(np.float32)
        direct_bt = np.where(direct_radiance > 0, direct_bt, np.nan)
    else:
        direct_bt = None

    report = {
        "source_file": str(path),
        "raw_dtype": str(raw.dtype),
        "raw_shape": list(raw.shape),
        "satpy": {},
    }

    for calibration in ["counts", "radiance", "reflectance", "brightness_temperature"]:
        try:
            scene = Scene(filenames=[str(path)], reader="abi_l1b")
            scene.load([channel_id], calibration=calibration)
            da = scene[channel_id]
            array = np.asarray(da.data)
            dataset_report = {
                "dtype": str(array.dtype),
                "shape": list(array.shape),
                "units": str(da.attrs.get("units", "")),
                "has_area": "area" in da.attrs,
            }
            if calibration == "counts":
                dataset_report["equal_to_source_rad"] = bool(np.array_equal(array, raw))
                dataset_report["max_abs_diff_vs_source_rad"] = max_abs_diff(array, raw)
            elif calibration == "radiance":
                dataset_report["max_abs_diff_vs_direct_decode"] = max_abs_diff(array, direct_radiance)
            elif calibration == "reflectance" and direct_reflectance is not None:
                normalized = array / np.float32(100.0) if dataset_report["units"] == "%" else array
                dataset_report["max_abs_diff_vs_direct_reflectance"] = max_abs_diff(normalized, direct_reflectance)
            elif calibration == "brightness_temperature" and direct_bt is not None:
                dataset_report["max_abs_diff_vs_direct_bt"] = max_abs_diff(array, direct_bt)
            report["satpy"][calibration] = dataset_report
        except Exception as exc:
            report["satpy"][calibration] = {"error": f"{type(exc).__name__}: {exc}"}

    return report


def main() -> None:
    """Run the GOES / Satpy self-check and optionally write a JSON report."""
    parser = argparse.ArgumentParser(description="GOES ABI / Satpy backend self-check.")
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Optional path for JSON report output.",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Project root containing Satellite_Data_20240312.",
    )
    args = parser.parse_args()

    project_root = find_project_root(args.project_root)
    readers = available_readers()
    reader_names = [reader if isinstance(reader, str) else reader.get("name", "") for reader in readers]

    report = {
        "satpy_version": satpy.__version__,
        "reader_count": len(reader_names),
        "abi_l1b_reader_present": "abi_l1b" in reader_names,
        "checked_files": {},
    }

    for channel_id, path in representative_files(project_root).items():
        report["checked_files"][channel_id] = scene_dataset_report(path, channel_id)

    report_text = json.dumps(report, indent=2)
    print(report_text)

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(report_text, encoding="utf-8")


if __name__ == "__main__":
    main()
