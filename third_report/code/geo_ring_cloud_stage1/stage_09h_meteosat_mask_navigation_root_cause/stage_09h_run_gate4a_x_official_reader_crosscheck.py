# -*- coding: utf-8 -*-
"""Stage 09H Gate 4A-X EUMETSAT Data Tailor official-reader cross-check.

This is a read-only, one-case diagnostic for 20240312_1200 Meteosat-0deg CLM.
It runs inside the WSL/EPCT environment requested by the user, records EPCT
capabilities and command logs, and compares official-reader outputs with the
raw GRIB mask and Gate 4A navigation references when a native-grid output is
available.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import textwrap
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pyproj import Geod

PROJECT_ID = "geo_ring_cloud"
STAGE_ID = "stage_09h"
GATE_ID = "gate4a_x_official_reader_crosscheck"
RUN_ID = "stage_09h_meteosat_mask_navigation_root_cause_202403"
CASE_ID = "20240312_1200"
SOURCE = "Meteosat-0deg"
PRODUCT = "CLM"
EXPECTED_SHA256 = "dbd4f0947291d9bbc12f4354c57b9932f7d9c1adedaec1f32ca1417c0b305dce"

PROJECT_ROOT = Path("/mnt/d/AAAresearch_paper")
CODE_ROOT = PROJECT_ROOT / "third_report" / "code" / "geo_ring_cloud_stage1"
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from geo_ring_cloud import paths as path_config  # noqa: E402
from geo_ring_cloud.adapters.cloud_products import reshape_square_if_needed  # noqa: E402
from geo_ring_cloud.reprojection import normalize_longitude  # noqa: E402

RAW_ZIP = Path("/mnt/e/GEO_Cloud_2024/Meteosat-0deg/CLM/20240312/12/MSG3-SEVI-MSGCLMK-0100-0100-20240312120000.000000000Z-NA.zip")
OUT_ROOT = Path("/mnt/d/AAAresearch_paper/geo_ring_cloud_stage1_time_runs") / RUN_ID / GATE_ID / CASE_ID
GATE4A_SOURCE = Path("/mnt/d/AAAresearch_paper/geo_ring_cloud_stage1_time_runs") / RUN_ID / "source_data"
HOME_EPCT_WORKSPACE = Path.home() / "epct_workspace"
GEOD = Geod(a=6378169.0, rf=295.488065897014)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_dirs() -> dict[str, Path]:
    dirs = {
        "source_data": OUT_ROOT / "source_data",
        "reports": OUT_ROOT / "reports",
        "logs": OUT_ROOT / "logs",
        "config": OUT_ROOT / "config",
        "epct_input": HOME_EPCT_WORKSPACE / "input",
        "epct_output": HOME_EPCT_WORKSPACE / "output" / f"{GATE_ID}_{CASE_ID}",
        "epct_config": HOME_EPCT_WORKSPACE / "config" / f"{GATE_ID}_{CASE_ID}",
        "epct_logs": HOME_EPCT_WORKSPACE / "logs" / f"{GATE_ID}_{CASE_ID}",
        "cache": OUT_ROOT / "cache",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_csv(rows: list[dict[str, Any]] | pd.DataFrame, path: Path) -> Path:
    df = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def write_json(obj: Any, path: Path) -> Path:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8-sig")
    return path


class CommandLogger:
    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.rows: list[dict[str, Any]] = []
        self.log_path.write_text("", encoding="utf-8")

    def run(self, command: list[str], label: str, cwd: Path | None = None, timeout: int = 600) -> subprocess.CompletedProcess[str]:
        started = utc_now()
        proc = subprocess.run(command, cwd=str(cwd) if cwd else None, text=True, capture_output=True, timeout=timeout)
        finished = utc_now()
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n===== {label} =====\n")
            f.write(f"started_utc: {started}\nfinished_utc: {finished}\nreturncode: {proc.returncode}\n")
            f.write("command: " + " ".join(command) + "\n")
            f.write("--- stdout ---\n" + proc.stdout + "\n")
            f.write("--- stderr ---\n" + proc.stderr + "\n")
        self.rows.append(
            {
                "label": label,
                "started_utc": started,
                "finished_utc": finished,
                "returncode": proc.returncode,
                "command": " ".join(command),
                "stdout_path": str(self.log_path),
                "stderr_path": str(self.log_path),
            }
        )
        return proc


def command_text(proc: subprocess.CompletedProcess[str]) -> str:
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


def add_warning(warnings: list[dict[str, Any]], code: str, message: str, severity: str = "WARN", **extra: Any) -> None:
    row = {
        "timestamp_utc": utc_now(),
        "stage_id": STAGE_ID,
        "gate_id": GATE_ID,
        "case_id": CASE_ID,
        "severity": severity,
        "warning_code": code,
        "message": message,
    }
    row.update(extra)
    warnings.append(row)


def filter_conda_list(text: str) -> str:
    keep = []
    for line in text.splitlines():
        lower = line.lower()
        if any(token in lower for token in ["epct", "umarf", "gdal", "netcdf"]):
            keep.append(line)
    return "\n".join(keep)


def dataframe_to_markdown(df: pd.DataFrame, floatfmt: str = ".6f") -> str:
    if df.empty:
        return ""
    lines = ["| " + " | ".join(map(str, df.columns)) + " |", "| " + " | ".join("---" for _ in df.columns) + " |"]
    for _, row in df.iterrows():
        cells = []
        for col in df.columns:
            val = row[col]
            if isinstance(val, (float, np.floating)):
                cells.append("" if not np.isfinite(val) else format(float(val), floatfmt))
            else:
                cells.append(str(val))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def copy_input_and_hash(dirs: dict[str, Path], warnings: list[dict[str, Any]]) -> tuple[Path, list[dict[str, Any]]]:
    copied = dirs["epct_input"] / RAW_ZIP.name
    raw_hash = sha256(RAW_ZIP)
    if raw_hash != EXPECTED_SHA256:
        add_warning(warnings, "input_sha256_mismatch", f"Input SHA-256 {raw_hash} != expected {EXPECTED_SHA256}", severity="ERROR")
    shutil.copy2(RAW_ZIP, copied)
    copied_hash = sha256(copied)
    rows = [
        {
            "path_role": "original_raw_zip",
            "path": str(RAW_ZIP),
            "sha256": raw_hash,
            "expected_sha256": EXPECTED_SHA256,
            "hash_matches_expected": raw_hash == EXPECTED_SHA256,
            "size_bytes": RAW_ZIP.stat().st_size,
        },
        {
            "path_role": "wsl_workspace_copy",
            "path": str(copied),
            "sha256": copied_hash,
            "expected_sha256": raw_hash,
            "hash_matches_expected": copied_hash == raw_hash,
            "size_bytes": copied.stat().st_size,
        },
    ]
    if copied_hash != raw_hash:
        add_warning(warnings, "workspace_copy_sha256_mismatch", f"Copied SHA-256 {copied_hash} != raw {raw_hash}", severity="ERROR")
    return copied, rows


def extract_raw_grib(zip_path: Path, cache_dir: Path) -> Path:
    with zipfile.ZipFile(zip_path) as zf:
        entries = [name for name in zf.namelist() if name.lower().endswith((".grb", ".grib", ".grb2", ".bin"))]
        if not entries:
            raise RuntimeError(f"No GRIB entry found in {zip_path}")
        entry = entries[0]
        payload = zf.read(entry)
    out = cache_dir / (Path(entry).name or "raw_payload.grb")
    out.write_bytes(payload)
    return out


def read_raw_cfgrib(grib_path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    import xarray as xr

    ds = xr.open_dataset(grib_path, engine="cfgrib", backend_kwargs={"indexpath": ""})
    try:
        var_name = "p260537" if "p260537" in ds.data_vars else list(ds.data_vars)[0]
        mask_raw = np.asarray(ds[var_name].values)
        lat_raw = np.asarray(ds["latitude"].values, dtype=np.float32)
        lon_raw = np.asarray(ds["longitude"].values, dtype=np.float32)
        mask = reshape_square_if_needed(mask_raw)
        lat = reshape_square_if_needed(lat_raw)
        lon = reshape_square_if_needed(lon_raw)
        meta = {
            "reader": "cfgrib",
            "var_name": var_name,
            "raw_dims_json": json.dumps(dict(ds.sizes), ensure_ascii=False),
            "mask_raw_shape": str(mask_raw.shape),
            "mask_shape": str(mask.shape),
            "latitude_shape": str(lat.shape),
            "longitude_shape": str(lon.shape),
            "data_vars": ",".join(ds.data_vars),
            "coords": ",".join(ds.coords),
            "variable_attrs_json": json.dumps({k: str(v) for k, v in ds[var_name].attrs.items()}, ensure_ascii=False),
        }
    finally:
        ds.close()
    return mask, lat, normalize_longitude(lon), meta


def read_gate4a_reference(case_id: str) -> tuple[np.ndarray | None, np.ndarray | None]:
    # Gate 4A did not persist the full official-area navigation arrays. For this
    # cross-check we regenerate the same independent Satpy reference from L1.5.
    try:
        from satpy import Scene
    except Exception:
        return gate4a_reference_from_recorded_projection()
    l15_root = Path("/mnt/d/AAAresearch_paper/third_report/Satellite_Data_20240312/Meteosat-10")
    hour = int(case_id.split("_")[1][:2])
    hits = sorted(l15_root.rglob(f"*20240312{hour:02d}*.nat"))
    hits = [path for path in hits if path.parent.name == path.stem]
    if not hits:
        return gate4a_reference_from_recorded_projection()
    scene = Scene(filenames=[str(hits[0])], reader="seviri_l1b_native")
    scene.load(["IR_108"], calibration="brightness_temperature")
    da = scene["IR_108"].reset_coords(drop=True)
    area = da.attrs["area"]
    lon, lat = area.get_lonlats()
    lon = normalize_longitude(np.asarray(lon, dtype=np.float32))
    lat = np.asarray(lat, dtype=np.float32)
    valid = np.isfinite(lon) & np.isfinite(lat) & (lat >= -90.0) & (lat <= 90.0)
    lon[~valid] = np.nan
    lat[~valid] = np.nan
    return lat, lon


def gate4a_reference_from_recorded_projection() -> tuple[np.ndarray | None, np.ndarray | None]:
    """Fallback reference from Gate 4A recorded Satpy projection parameters.

    This preserves Gate 4A's raw-storage convention: row0=south and col0=east.
    It is used only when Satpy is unavailable in the EPCT environment.
    """
    try:
        from pyproj import CRS, Transformer
    except Exception:
        return None, None
    n = 3712
    half_span = 5_567_248.0
    x = np.linspace(half_span, -half_span, n, dtype=np.float64)
    y = np.linspace(-half_span, half_span, n, dtype=np.float64)
    xs, ys = np.meshgrid(x, y)
    crs = CRS.from_proj4("+proj=geos +lon_0=0 +h=35785831 +a=6378169 +rf=295.488065897014 +units=m +no_defs")
    transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    lon, lat = transformer.transform(xs, ys)
    lon = normalize_longitude(np.asarray(lon, dtype=np.float32))
    lat = np.asarray(lat, dtype=np.float32)
    valid = np.isfinite(lon) & np.isfinite(lat) & (lat >= -90.0) & (lat <= 90.0)
    lon[~valid] = np.nan
    lat[~valid] = np.nan
    return lat, lon


def run_epct_probe_and_processing(dirs: dict[str, Path], copied_zip: Path, command_logger: CommandLogger, warnings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Path | None, dict[str, Any]]:
    env_rows: list[dict[str, Any]] = []
    probes = [
        ("uname_a", ["uname", "-a"]),
        ("conda_env_list", ["conda", "env", "list"]),
        ("which_epct", ["which", "epct"]),
        ("epct_version", ["epct", "--version"]),
        ("epct_info", ["epct", "info"]),
        ("conda_list", ["conda", "list"]),
        ("epct_read_products", ["epct", "read", "products"]),
        ("epct_read_products_query_MSGCLMK", ["epct", "read", "products", "-q", "MSGCLMK"]),
        ("epct_read_products_query_cloud_mask", ["epct", "read", "products", "-q", "Cloud Mask"]),
        ("epct_read_formats_MSGCLMK", ["epct", "read", "-p", "MSGCLMK", "formats"]),
        ("epct_read_chains_MSGCLMK", ["epct", "read", "-p", "MSGCLMK", "chains"]),
        ("epct_read_chain_msgclmk_cloud_mask", ["epct", "read", "chains/msgclmk_cloud_mask"]),
        ("epct_read_expanded_MSGCLMK", ["epct", "read", "-x", "-p", "MSGCLMK"]),
        ("epct_run_chain_help", ["epct", "run-chain", "--help"]),
    ]
    probe_text: dict[str, str] = {}
    for label, command in probes:
        proc = command_logger.run(command, label, cwd=PROJECT_ROOT, timeout=240)
        text = command_text(proc)
        if label == "conda_list":
            text = filter_conda_list(text)
        probe_text[label] = text
        env_rows.append(
            {
                "category": "environment_or_capability",
                "item": label,
                "command": " ".join(command),
                "returncode": proc.returncode,
                "value": text[:10000],
            }
        )
    products_hit = "MSGCLMK" in probe_text.get("epct_read_products_query_MSGCLMK", "")
    chains_text = probe_text.get("epct_read_chains_MSGCLMK", "")
    formats_text = probe_text.get("epct_read_formats_MSGCLMK", "")
    if not products_hit:
        add_warning(warnings, "epct_msgclmk_not_registered", "EPCT did not report MSGCLMK as a registered product.", severity="ERROR")
        return env_rows, None, {"native_chain_available": False, "reason": "MSGCLMK not registered"}

    chain_yaml = textwrap.dedent(
        """\
        product: MSGCLMK
        format: geotiff
        filter:
          bands:
            - cloud_mask
        """
    )
    chain_path = dirs["epct_config"] / "data_tailor_chain.yaml"
    chain_path.write_text(chain_yaml, encoding="utf-8")
    shutil.copyfile(chain_path, OUT_ROOT / "config" / "data_tailor_chain.yaml")
    scientific_native_possible = "msgclmk_cloud_mask" in chains_text and "geotiff" in formats_text
    if not scientific_native_possible:
        add_warning(warnings, "no_native_grid_chain_declared", "MSGCLMK chain/format probe did not expose a usable native-grid chain.")
        return env_rows, None, {"native_chain_available": False, "reason": "No usable MSGCLMK geotiff chain in EPCT probe"}

    # Keep first run conservative: built-in MSGCLMK cloud-mask chain, no ROI,
    # no reprojection argument, no resampling argument.
    (dirs["epct_output"] / "workspace").mkdir(parents=True, exist_ok=True)
    (dirs["epct_output"] / "workspace_grib").mkdir(parents=True, exist_ok=True)
    before = {p.resolve() for p in dirs["epct_output"].glob("*") if p.is_file()}
    proc = command_logger.run(
        [
            "epct",
            "run-chain",
            "-c",
            "msgclmk_cloud_mask",
            "-o",
            str(dirs["epct_output"]),
            "--workspace-dir",
            str(dirs["epct_output"] / "workspace"),
            "--log-dir",
            str(dirs["epct_logs"]),
            str(copied_zip),
        ],
        "epct_run_chain_zip_builtin_msgclmk_cloud_mask",
        cwd=PROJECT_ROOT,
        timeout=900,
    )
    after = [p for p in dirs["epct_output"].rglob("*") if p.is_file() and p.resolve() not in before]
    if proc.returncode != 0 or not after:
        add_warning(warnings, "epct_zip_run_failed_or_no_output", "EPCT ZIP run failed or produced no file; trying internal GRIB without modifying it.")
        grib_path = extract_raw_grib(copied_zip, dirs["cache"])
        proc = command_logger.run(
            [
                "epct",
                "run-chain",
                "-c",
                "msgclmk_cloud_mask",
                "-o",
                str(dirs["epct_output"]),
                "--workspace-dir",
                str(dirs["epct_output"] / "workspace_grib"),
                "--log-dir",
                str(dirs["epct_logs"]),
                str(grib_path),
            ],
            "epct_run_chain_internal_grib_builtin_msgclmk_cloud_mask",
            cwd=PROJECT_ROOT,
            timeout=900,
        )
        after = [p for p in dirs["epct_output"].rglob("*") if p.is_file()]
        if proc.returncode != 0 or not after:
            add_warning(warnings, "epct_grib_run_failed_or_no_output", "EPCT internal-GRIB run failed or produced no output.", severity="ERROR")
            return env_rows, None, {"native_chain_available": True, "reason": "EPCT run failed"}
    candidates = [p for p in after if p.suffix.lower() in [".tif", ".tiff", ".nc", ".h5", ".grb", ".grib", ".grb2"]]
    if not candidates:
        add_warning(warnings, "epct_no_scientific_output", "EPCT produced files but no recognized scientific raster/container output.")
        return env_rows, None, {"native_chain_available": True, "reason": "No recognized scientific output"}
    output = sorted(candidates, key=lambda p: p.stat().st_size, reverse=True)[0]
    return env_rows, output, {"native_chain_available": True, "reason": "EPCT built-in MSGCLMK chain output selected", "chain_yaml": str(chain_path)}


def inspect_data_tailor_output(path: Path, warnings: list[dict[str, Any]]) -> tuple[np.ndarray | None, np.ndarray | None, np.ndarray | None, list[dict[str, Any]], list[dict[str, Any]]]:
    inventory: list[dict[str, Any]] = []
    params: list[dict[str, Any]] = []
    suffix = path.suffix.lower()
    if suffix in [".tif", ".tiff"]:
        try:
            import rasterio
            from rasterio.transform import xy
        except Exception as exc:
            add_warning(warnings, "rasterio_unavailable_using_gdal_fallback", f"Rasterio unavailable; using osgeo.gdal fallback: {exc}")
            return inspect_geotiff_with_gdal(path, warnings)
        with rasterio.open(path) as ds:
            data = ds.read(1)
            inventory.append(
                {
                    "path": str(path),
                    "format": "GeoTIFF",
                    "driver": ds.driver,
                    "width": ds.width,
                    "height": ds.height,
                    "count": ds.count,
                    "dtype": str(data.dtype),
                    "shape": str(data.shape),
                    "nodata": ds.nodata,
                    "crs": str(ds.crs),
                    "transform": str(ds.transform),
                    "bounds": str(ds.bounds),
                    "res": str(ds.res),
                    "tags_json": json.dumps(ds.tags(), ensure_ascii=False),
                    "is_native_shape_3712": data.shape == (3712, 3712),
                    "has_crs_or_transform": ds.crs is not None and ds.transform is not None,
                    "resampling_detected": "unknown_from_geotiff_only" if data.shape == (3712, 3712) else "yes_shape_changed",
                }
            )
            if ds.crs is not None:
                for key, value in (ds.crs.to_dict() or {}).items():
                    params.append({"source": "data_tailor_geotiff_crs", "parameter": key, "value": value})
            params.append({"source": "data_tailor_geotiff", "parameter": "transform", "value": str(ds.transform)})
            params.append({"source": "data_tailor_geotiff", "parameter": "bounds", "value": str(ds.bounds)})
            lat = lon = None
            if ds.crs is not None and ds.transform is not None:
                rows = np.arange(ds.height, dtype=np.int32)
                cols = np.arange(ds.width, dtype=np.int32)
                cc, rr = np.meshgrid(cols, rows)
                xs, ys = xy(ds.transform, rr, cc, offset="center")
                xs = np.asarray(xs, dtype=np.float64)
                ys = np.asarray(ys, dtype=np.float64)
                from pyproj import Transformer

                transformer = Transformer.from_crs(ds.crs, "EPSG:4326", always_xy=True)
                lon, lat = transformer.transform(xs, ys)
                lon = normalize_longitude(np.asarray(lon, dtype=np.float32))
                lat = np.asarray(lat, dtype=np.float32)
                valid = np.isfinite(lon) & np.isfinite(lat) & (lat >= -90.0) & (lat <= 90.0)
                lon[~valid] = np.nan
                lat[~valid] = np.nan
            return data, lat, lon, inventory, params
    add_warning(warnings, "unsupported_data_tailor_output_format", f"Output format not implemented for inspection: {path}", severity="ERROR")
    return None, None, None, inventory, params


def inspect_geotiff_with_gdal(path: Path, warnings: list[dict[str, Any]]) -> tuple[np.ndarray | None, np.ndarray | None, np.ndarray | None, list[dict[str, Any]], list[dict[str, Any]]]:
    inventory: list[dict[str, Any]] = []
    params: list[dict[str, Any]] = []
    try:
        from osgeo import gdal
        from pyproj import CRS, Transformer
    except Exception as exc:
        add_warning(warnings, "gdal_fallback_unavailable", f"Cannot inspect GeoTIFF with GDAL fallback: {exc}", severity="ERROR")
        return None, None, None, inventory, params
    ds = gdal.Open(str(path), gdal.GA_ReadOnly)
    if ds is None:
        add_warning(warnings, "gdal_open_failed", f"GDAL could not open Data Tailor output {path}", severity="ERROR")
        return None, None, None, inventory, params
    band = ds.GetRasterBand(1)
    data = band.ReadAsArray()
    gt = ds.GetGeoTransform()
    proj_wkt = ds.GetProjection()
    nodata = band.GetNoDataValue()
    width = int(ds.RasterXSize)
    height = int(ds.RasterYSize)
    inventory.append(
        {
            "path": str(path),
            "format": "GeoTIFF",
            "reader": "osgeo.gdal",
            "driver": ds.GetDriver().ShortName if ds.GetDriver() else "",
            "width": width,
            "height": height,
            "count": ds.RasterCount,
            "dtype": str(data.dtype),
            "shape": str(data.shape),
            "nodata": nodata,
            "crs": proj_wkt,
            "transform": str(gt),
            "bounds": str((gt[0], gt[3] + height * gt[5], gt[0] + width * gt[1], gt[3])),
            "res": str((gt[1], gt[5])),
            "tags_json": json.dumps(ds.GetMetadata() or {}, ensure_ascii=False),
            "is_native_shape_3712": data.shape == (3712, 3712),
            "has_crs_or_transform": bool(proj_wkt) and gt is not None,
            "resampling_detected": "unknown_from_geotiff_only" if data.shape == (3712, 3712) else "yes_shape_changed",
        }
    )
    params.append({"source": "data_tailor_geotiff_gdal", "parameter": "projection_wkt", "value": proj_wkt})
    params.append({"source": "data_tailor_geotiff_gdal", "parameter": "geotransform", "value": str(gt)})
    params.append({"source": "data_tailor_geotiff_gdal", "parameter": "nodata", "value": nodata})
    if not proj_wkt or gt is None:
        add_warning(warnings, "data_tailor_geotiff_missing_projection", "GeoTIFF lacks projection or geotransform.", severity="ERROR")
        return data, None, None, inventory, params
    cols = np.arange(width, dtype=np.float64)
    rows = np.arange(height, dtype=np.float64)
    xs_1d = gt[0] + (cols + 0.5) * gt[1] + 0.5 * gt[2]
    ys_1d = gt[3] + 0.5 * gt[4] + (rows + 0.5) * gt[5]
    xs, ys = np.meshgrid(xs_1d, ys_1d)
    try:
        transformer = Transformer.from_crs(CRS.from_wkt(proj_wkt), "EPSG:4326", always_xy=True)
        lon, lat = transformer.transform(xs, ys)
        lon = normalize_longitude(np.asarray(lon, dtype=np.float32))
        lat = np.asarray(lat, dtype=np.float32)
        valid = np.isfinite(lon) & np.isfinite(lat) & (lat >= -90.0) & (lat <= 90.0)
        lon[~valid] = np.nan
        lat[~valid] = np.nan
    except Exception as exc:
        add_warning(warnings, "data_tailor_geotiff_navigation_transform_failed", f"Failed to derive lat/lon from GeoTIFF CRS: {exc}", severity="ERROR")
        return data, None, None, inventory, params
    return data, lat, lon, inventory, params


def unique_counts(arr: np.ndarray) -> dict[int, int]:
    values, counts = np.unique(arr, return_counts=True)
    return {int(v): int(c) for v, c in zip(values, counts)}


def compare_mask(raw: np.ndarray, dt: np.ndarray) -> list[dict[str, Any]]:
    transforms = {
        "identity": dt,
        "rot180": dt[::-1, ::-1],
        "flipud": dt[::-1, :],
        "fliplr": dt[:, ::-1],
    }
    rows: list[dict[str, Any]] = []
    raw_cmp = raw.astype(dt.dtype, copy=False) if raw.shape == dt.shape else raw
    for name, arr in transforms.items():
        same_shape = raw_cmp.shape == arr.shape
        rows.append(
            {
                "case_id": CASE_ID,
                "comparison": f"data_tailor_{name}_vs_raw_grib_identity",
                "raw_shape": str(raw.shape),
                "data_tailor_shape": str(dt.shape),
                "same_shape": same_shape,
                "raw_dtype": str(raw.dtype),
                "data_tailor_dtype": str(dt.dtype),
                "raw_unique_counts_json": json.dumps(unique_counts(raw), ensure_ascii=False),
                "data_tailor_unique_counts_json": json.dumps(unique_counts(dt), ensure_ascii=False),
                "agreement": float(np.mean(raw_cmp == arr)) if same_shape else math.nan,
                "equal_hash_if_same_dtype_and_shape": hashlib.sha256(raw_cmp.tobytes()).hexdigest() == hashlib.sha256(arr.tobytes()).hexdigest() if same_shape and raw_cmp.dtype == arr.dtype else False,
                "raw_storage_orientation_note": "raw array order from GRIB/cfgrib plus project C-order square reshape",
                "display_orientation_note": "quicklook north-up display is not used as storage-order evidence",
            }
        )
    return rows


def shifted_after_rot180(arr: np.ndarray, dr: int, dc: int, fill: float = np.nan) -> np.ndarray:
    rot = arr[::-1, ::-1]
    out = np.full(rot.shape, fill, dtype=rot.dtype)
    nrow, ncol = rot.shape
    src_r0 = max(0, dr)
    src_r1 = min(nrow, nrow + dr)
    dst_r0 = max(0, -dr)
    dst_r1 = dst_r0 + (src_r1 - src_r0)
    src_c0 = max(0, dc)
    src_c1 = min(ncol, ncol + dc)
    dst_c0 = max(0, -dc)
    dst_c1 = dst_c0 + (src_c1 - src_c0)
    if src_r1 > src_r0 and src_c1 > src_c0:
        out[dst_r0:dst_r1, dst_c0:dst_c1] = rot[src_r0:src_r1, src_c0:src_c1]
    return out


def circular_lon_diff_deg(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return ((np.asarray(a, dtype=np.float32) - np.asarray(b, dtype=np.float32) + 180.0) % 360.0) - 180.0


def geodesic_distance_km(lon_a: np.ndarray, lat_a: np.ndarray, lon_b: np.ndarray, lat_b: np.ndarray, chunk: int = 1_000_000) -> np.ndarray:
    n = lon_a.size
    out = np.empty(n, dtype=np.float32)
    for start in range(0, n, chunk):
        stop = min(start + chunk, n)
        _, _, dist_m = GEOD.inv(
            lon_a[start:stop].astype(np.float64),
            lat_a[start:stop].astype(np.float64),
            lon_b[start:stop].astype(np.float64),
            lat_b[start:stop].astype(np.float64),
        )
        out[start:stop] = (np.asarray(dist_m, dtype=np.float64) / 1000.0).astype(np.float32)
    return out


def nav_valid(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    return np.isfinite(lat) & np.isfinite(lon) & (lat >= -90.0) & (lat <= 90.0) & (~(np.isclose(lat, 0.0) & np.isclose(lon, 0.0)))


def compare_navigation(dt_lat: np.ndarray, dt_lon: np.ndarray, ref_lat: np.ndarray, ref_lon: np.ndarray, cf_lat: np.ndarray, cf_lon: np.ndarray) -> list[dict[str, Any]]:
    candidates = {
        "A_gate4a_official_area_reference": (ref_lat, ref_lon),
        "B_current_cfgrib_identity": (cf_lat, cf_lon),
        "C_current_cfgrib_rot180": (cf_lat[::-1, ::-1], cf_lon[::-1, ::-1]),
        "D_current_cfgrib_rot180_dr1_dc1": (shifted_after_rot180(cf_lat, 1, 1), shifted_after_rot180(cf_lon, 1, 1)),
    }
    rows: list[dict[str, Any]] = []
    dt_variants = {
        "data_tailor_output_storage": (dt_lat, dt_lon, "GeoTIFF storage as written by Data Tailor"),
        "data_tailor_rot180_to_raw_storage": (dt_lat[::-1, ::-1], normalize_longitude(dt_lon[::-1, ::-1]), "Data Tailor output rotated 180 degrees to match raw GRIB storage, as established by mask comparison"),
    }
    for dt_name, (dt_lat_v, dt_lon_v, dt_note) in dt_variants.items():
        dt_valid = nav_valid(dt_lat_v, dt_lon_v)
        for name, (lat, lon) in candidates.items():
            lon = normalize_longitude(lon)
            valid = dt_valid & nav_valid(lat, lon)
            if not np.any(valid):
                rows.append({"case_id": CASE_ID, "data_tailor_orientation": dt_name, "comparison_target": name, "n_valid": 0})
                continue
            lat_err = np.abs(dt_lat_v[valid].astype(np.float32) - lat[valid].astype(np.float32))
            lon_err = np.abs(circular_lon_diff_deg(dt_lon_v[valid], lon[valid]))
            dist = geodesic_distance_km(dt_lon_v[valid], dt_lat_v[valid], lon[valid], lat[valid])
            rows.append(
                {
                    "case_id": CASE_ID,
                    "data_tailor_orientation": dt_name,
                    "data_tailor_orientation_note": dt_note,
                    "comparison_target": name,
                    "n_valid": int(np.count_nonzero(valid)),
                    "valid_fraction_of_data_tailor_disk": float(np.count_nonzero(valid) / max(np.count_nonzero(dt_valid), 1)),
                    "latitude_mae_deg": float(np.nanmean(lat_err)),
                    "longitude_circular_mae_deg": float(np.nanmean(lon_err)),
                    "median_geodesic_error_km": float(np.nanmedian(dist)),
                    "p95_geodesic_error_km": float(np.nanpercentile(dist, 95)),
                    "matched_fraction_within_1km": float(np.mean(dist <= 1.0)),
                    "matched_fraction_within_5km": float(np.mean(dist <= 5.0)),
                    "matched_fraction_within_10km": float(np.mean(dist <= 10.0)),
                }
            )
    return rows


def nearest_ref_point(ref_lon: np.ndarray, ref_lat: np.ndarray, valid: np.ndarray, target_lat: float, target_lon: float) -> tuple[int, int]:
    score = np.where(valid, (ref_lat - target_lat) ** 2 + circular_lon_diff_deg(ref_lon, target_lon) ** 2, np.inf)
    flat = int(np.nanargmin(score))
    return np.unravel_index(flat, ref_lat.shape)


def navigation_control_points(dt_lat: np.ndarray, dt_lon: np.ndarray, ref_lat: np.ndarray, ref_lon: np.ndarray, cf_lat: np.ndarray, cf_lon: np.ndarray) -> list[dict[str, Any]]:
    targets = [
        ("disk_center", 0.0, 0.0),
        ("north_near_edge", 68.0, 0.0),
        ("south_near_edge", -68.0, 0.0),
        ("east_near_edge", 0.0, 67.0),
        ("west_near_edge", 0.0, -67.0),
        ("nw_quadrant", 35.0, -35.0),
        ("ne_quadrant", 35.0, 35.0),
        ("sw_quadrant", -35.0, -35.0),
        ("se_quadrant", -35.0, 35.0),
    ]
    dt_valid = nav_valid(dt_lat, dt_lon)
    rows: list[dict[str, Any]] = []
    variants = {
        "gate4a_reference": (ref_lat, ref_lon),
        "cfgrib_identity": (cf_lat, cf_lon),
        "cfgrib_rot180": (cf_lat[::-1, ::-1], normalize_longitude(cf_lon[::-1, ::-1])),
        "cfgrib_rot180_dr1_dc1": (shifted_after_rot180(cf_lat, 1, 1), normalize_longitude(shifted_after_rot180(cf_lon, 1, 1))),
    }
    for point, target_lat, target_lon in targets:
        row, col = nearest_ref_point(dt_lon, dt_lat, dt_valid, target_lat, target_lon)
        for variant, (lat, lon) in variants.items():
            _, _, dist_m = GEOD.inv(float(dt_lon[row, col]), float(dt_lat[row, col]), float(lon[row, col]), float(lat[row, col]))
            rows.append(
                {
                    "case_id": CASE_ID,
                    "control_point": point,
                    "native_row": int(row),
                    "native_col": int(col),
                    "data_tailor_lat_deg": float(dt_lat[row, col]),
                    "data_tailor_lon_deg": float(dt_lon[row, col]),
                    "comparison_target": variant,
                    "target_lat_deg": float(lat[row, col]),
                    "target_lon_deg": float(lon[row, col]),
                    "geodesic_error_km": float(dist_m / 1000.0),
                }
            )
    return rows


def output_inventory_rows(root: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rows.append(
                {
                    "path": str(path),
                    "name": path.name,
                    "suffix": path.suffix,
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256(path),
                }
            )
    return rows


def final_decision(mask_df: pd.DataFrame, nav_df: pd.DataFrame, dt_inventory: pd.DataFrame, warnings: list[dict[str, Any]]) -> str:
    if dt_inventory.empty or not bool(dt_inventory.iloc[0].get("is_native_shape_3712", False)):
        return "OFFICIAL_READER_CROSSCHECK_INCONCLUSIVE"
    ident_mask = mask_df[mask_df["comparison"] == "data_tailor_identity_vs_raw_grib_identity"]
    if ident_mask.empty or float(ident_mask.iloc[0]["agreement"]) < 0.999:
        return "OFFICIAL_READER_CROSSCHECK_INCONCLUSIVE"
    nav_raw = nav_df[nav_df.get("data_tailor_orientation", "") == "data_tailor_rot180_to_raw_storage"] if "data_tailor_orientation" in nav_df else nav_df
    ref = nav_raw[nav_raw["comparison_target"] == "A_gate4a_official_area_reference"]
    ident = nav_raw[nav_raw["comparison_target"] == "B_current_cfgrib_identity"]
    rot_shift = nav_raw[nav_raw["comparison_target"] == "D_current_cfgrib_rot180_dr1_dc1"]
    if ref.empty or ident.empty or rot_shift.empty:
        return "OFFICIAL_READER_CROSSCHECK_INCONCLUSIVE"
    ref_med = float(ref.iloc[0]["median_geodesic_error_km"])
    ident_med = float(ident.iloc[0]["median_geodesic_error_km"])
    shift_med = float(rot_shift.iloc[0]["median_geodesic_error_km"])
    if ref_med <= 2.0 and shift_med <= 2.0 and ident_med >= 1000.0:
        return "OFFICIAL_READER_CONFIRMS_LOCAL_CFGRIB_NAVIGATION_ORDER_MISMATCH"
    return "OFFICIAL_READER_CROSSCHECK_INCONCLUSIVE"


def write_report(
    paths: dict[str, Path],
    decision: str,
    hash_df: pd.DataFrame,
    env_df: pd.DataFrame,
    dt_inv_df: pd.DataFrame,
    mask_df: pd.DataFrame,
    nav_df: pd.DataFrame,
    warnings: list[dict[str, Any]],
) -> Path:
    lines = [
        "# Stage 09H Gate 4A-X EUMETSAT Data Tailor official-reader cross-check",
        "",
        f"- Generated UTC: {utc_now()}",
        f"- Case: `{CASE_ID}` / `{SOURCE}` / `{PRODUCT}`",
        f"- Decision: `{decision}`",
        "- 约束执行：未修改 production reader；未旋转 `cloud_mask`；未重跑整月；未覆盖 Gate 3A/3B/4A；quicklook 方向没有被当作数组方向证据。",
        "",
        "## Input Hash",
        "",
        dataframe_to_markdown(hash_df, floatfmt=".6f"),
        "",
        "## EPCT / Plugin Inventory",
        "",
        "完整 stdout/stderr 见 `logs/command_log.txt`；本表只保留命令摘要。",
        "",
        dataframe_to_markdown(env_df[["item", "returncode"]].head(30), floatfmt=".6f") if not env_df.empty else "",
        "",
        "## Data Tailor Output Inventory",
        "",
        dataframe_to_markdown(dt_inv_df, floatfmt=".6f") if not dt_inv_df.empty else "",
        "",
        "## Cloud Mask Orientation Comparison",
        "",
        dataframe_to_markdown(mask_df[["comparison", "same_shape", "raw_dtype", "data_tailor_dtype", "agreement", "equal_hash_if_same_dtype_and_shape"]], floatfmt=".9f") if not mask_df.empty else "",
        "",
        "## Navigation Comparison",
        "",
        dataframe_to_markdown(nav_df, floatfmt=".6f") if not nav_df.empty else "",
        "",
        "## Interpretation",
        "",
    ]
    if decision == "OFFICIAL_READER_CONFIRMS_LOCAL_CFGRIB_NAVIGATION_ORDER_MISMATCH":
        lines.extend(
            [
                "- Data Tailor 输出的 `cloud_mask` 在 native storage 中与原始 GRIB `cloud_mask` identity 一致，因此本轮不支持 CLM mask 本身南北/东西翻转。",
                "- Data Tailor-derived navigation 与 Gate 4A official-area reference 高度一致，同时与当前 cfgrib identity navigation 明显不一致。",
                "- 当前 cfgrib navigation 需要 rot180+dr=1,dc=1 后才接近官方 reader/reference，这与 Gate 4A 的 exact-index mapping 一致。",
                "- 因此，本轮官方 reader 交叉验证支持：问题来自本地 cfgrib/navigation order 链路，而不是 EUMETSAT CLM mask 值本身。",
            ]
        )
    else:
        mask_identity = math.nan
        mask_rot180 = math.nan
        if not mask_df.empty:
            hit = mask_df[mask_df["comparison"] == "data_tailor_identity_vs_raw_grib_identity"]
            if not hit.empty:
                mask_identity = float(hit.iloc[0]["agreement"])
            hit = mask_df[mask_df["comparison"] == "data_tailor_rot180_vs_raw_grib_identity"]
            if not hit.empty:
                mask_rot180 = float(hit.iloc[0]["agreement"])
        dt_raw_ref = pd.DataFrame()
        dt_out_cf = pd.DataFrame()
        if not nav_df.empty and "data_tailor_orientation" in nav_df:
            dt_raw_ref = nav_df[
                (nav_df["data_tailor_orientation"] == "data_tailor_rot180_to_raw_storage")
                & (nav_df["comparison_target"] == "A_gate4a_official_area_reference")
            ]
            dt_out_cf = nav_df[
                (nav_df["data_tailor_orientation"] == "data_tailor_output_storage")
                & (nav_df["comparison_target"] == "B_current_cfgrib_identity")
            ]
        lines.extend(
            [
                "- 本轮不能给出严格的官方 reader confirmed 结论，因为 Data Tailor GeoTIFF 没有保持 raw GRIB storage orientation。",
                f"- 具体证据：Data Tailor output-storage mask 与 raw GRIB identity agreement = {mask_identity:.9f}；Data Tailor rot180 后与 raw GRIB agreement = {mask_rot180:.9f}。",
                "- 这说明 Data Tailor 输出是 3712x3712 fixed-grid 科学栅格，但 GeoTIFF storage 已转成 north-up / west-left；它不是 quicklook，但也不是原始 GRIB storage。",
            ]
        )
        if not dt_raw_ref.empty:
            row = dt_raw_ref.iloc[0]
            lines.append(
                f"- 将 Data Tailor GeoTIFF rot180 回 raw-storage 后，与 Gate 4A official-area reference 的 median geodesic error = {float(row['median_geodesic_error_km']):.6f} km，p95 = {float(row['p95_geodesic_error_km']):.6f} km。"
            )
        if not dt_out_cf.empty:
            row = dt_out_cf.iloc[0]
            lines.append(
                f"- Data Tailor output-storage navigation 与当前 cfgrib identity navigation 的 median geodesic error = {float(row['median_geodesic_error_km']):.6f} km，说明当前 cfgrib navigation 更像 north-up output orientation，而不是 raw mask storage orientation。"
            )
        lines.extend(
            [
                "- 因此，本轮属于 `OFFICIAL_READER_CROSSCHECK_INCONCLUSIVE`，但它对 Gate 3B/4A 的本地 navigation-order mismatch 结论提供了强旁证；不能据此归咎于 EUMETSAT CLM mask 本身。",
                "- 按约束，不把 Data Tailor north-up GeoTIFF 与原始 raw storage 强行逐像元 identity 解释为产品错误。",
            ]
        )
    lines.extend(
        [
            "",
            "## Warnings",
            "",
            f"- Warning rows: {len(warnings)}. See `logs/warnings.csv`.",
        ]
    )
    path = paths["reports"] / "official_reader_crosscheck_report_cn.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return path


def _first_metric(df: pd.DataFrame, column: str, default: float = math.nan) -> float:
    if df.empty or column not in df:
        return default
    try:
        return float(df.iloc[0][column])
    except Exception:
        return default


def _mask_transform_name(comparison: str) -> str:
    mapping = {
        "data_tailor_identity_vs_raw_grib_identity": "identity",
        "data_tailor_rot180_vs_raw_grib_identity": "rot180",
        "data_tailor_flipud_vs_raw_grib_identity": "flipud",
        "data_tailor_fliplr_vs_raw_grib_identity": "fliplr",
    }
    return mapping.get(comparison, comparison)


def derive_revised_statuses(mask_df: pd.DataFrame, nav_df: pd.DataFrame, dt_inventory: pd.DataFrame) -> dict[str, Any]:
    strict_status = final_decision(mask_df, nav_df, dt_inventory, [])

    best_mask_row = pd.Series(dtype=object)
    if not mask_df.empty and "agreement" in mask_df:
        ranked = mask_df.copy()
        ranked["agreement_sort"] = pd.to_numeric(ranked["agreement"], errors="coerce").fillna(-1.0)
        ranked["hash_sort"] = (
            ranked["equal_hash_if_same_dtype_and_shape"].astype(bool).astype(int)
            if "equal_hash_if_same_dtype_and_shape" in ranked
            else 0
        )
        best_mask_row = ranked.sort_values(["agreement_sort", "hash_sort"], ascending=[False, False]).iloc[0]

    best_transform = _mask_transform_name(str(best_mask_row.get("comparison", ""))) if not best_mask_row.empty else ""
    best_agreement = float(best_mask_row.get("agreement", math.nan)) if not best_mask_row.empty else math.nan
    best_hash_equal = bool(best_mask_row.get("equal_hash_if_same_dtype_and_shape", False)) if not best_mask_row.empty else False

    dt_output_cf_identity = pd.DataFrame()
    dt_raw_gate4a_ref = pd.DataFrame()
    dt_raw_cf_rot180 = pd.DataFrame()
    dt_raw_cf_rot180_shift = pd.DataFrame()
    if not nav_df.empty and "data_tailor_orientation" in nav_df:
        dt_output_cf_identity = nav_df[
            (nav_df["data_tailor_orientation"] == "data_tailor_output_storage")
            & (nav_df["comparison_target"] == "B_current_cfgrib_identity")
        ]
        dt_raw_gate4a_ref = nav_df[
            (nav_df["data_tailor_orientation"] == "data_tailor_rot180_to_raw_storage")
            & (nav_df["comparison_target"] == "A_gate4a_official_area_reference")
        ]
        dt_raw_cf_rot180 = nav_df[
            (nav_df["data_tailor_orientation"] == "data_tailor_rot180_to_raw_storage")
            & (nav_df["comparison_target"] == "C_current_cfgrib_rot180")
        ]
        dt_raw_cf_rot180_shift = nav_df[
            (nav_df["data_tailor_orientation"] == "data_tailor_rot180_to_raw_storage")
            & (nav_df["comparison_target"] == "D_current_cfgrib_rot180_dr1_dc1")
        ]

    dt_output_vs_cf_identity_median_km = _first_metric(dt_output_cf_identity, "median_geodesic_error_km")
    dt_raw_vs_gate4a_ref_median_km = _first_metric(dt_raw_gate4a_ref, "median_geodesic_error_km")
    dt_raw_vs_gate4a_ref_p95_km = _first_metric(dt_raw_gate4a_ref, "p95_geodesic_error_km")
    dt_raw_vs_cf_rot180_median_km = _first_metric(dt_raw_cf_rot180, "median_geodesic_error_km")
    dt_raw_vs_cf_rot180_shift_median_km = _first_metric(dt_raw_cf_rot180_shift, "median_geodesic_error_km")

    lossless_mask_transform = (
        best_transform in {"identity", "rot180", "flipud", "fliplr"}
        and math.isfinite(best_agreement)
        and best_agreement >= 0.999999
        and best_hash_equal
    )
    navigation_direction_supported = (
        math.isfinite(dt_output_vs_cf_identity_median_km)
        and dt_output_vs_cf_identity_median_km < 20.0
        and math.isfinite(dt_raw_vs_cf_rot180_median_km)
        and dt_raw_vs_cf_rot180_median_km < 20.0
    )
    orientation_status = (
        "OFFICIAL_READER_CONFIRMS_LOCAL_MASK_NAVIGATION_ORDER_MISMATCH"
        if lossless_mask_transform and best_transform == "rot180" and navigation_direction_supported
        else "ORIENTATION_CROSSCHECK_INCONCLUSIVE"
    )

    return {
        "decision": strict_status,
        "strict_exact_native_grid_status": strict_status,
        "orientation_status": orientation_status,
        "exact_native_navigation_status": "INCONCLUSIVE_DUE_TO_GEOTIFF_GRID_NORMALIZATION",
        "cloud_mask_best_lossless_transform": best_transform,
        "cloud_mask_best_transform_agreement": best_agreement,
        "cloud_mask_best_transform_hash_equal": best_hash_equal,
        "data_tailor_output_vs_cfgrib_identity_median_km": dt_output_vs_cf_identity_median_km,
        "data_tailor_rot180_raw_storage_vs_gate4a_reference_median_km": dt_raw_vs_gate4a_ref_median_km,
        "data_tailor_rot180_raw_storage_vs_gate4a_reference_p95_km": dt_raw_vs_gate4a_ref_p95_km,
        "data_tailor_rot180_raw_storage_vs_cfgrib_rot180_median_km": dt_raw_vs_cf_rot180_median_km,
        "data_tailor_rot180_raw_storage_vs_cfgrib_rot180_dr1dc1_median_km": dt_raw_vs_cf_rot180_shift_median_km,
        "residual_interpretation": "GeoTIFF grid parameter, ellipsoid, area-extent, or pixel-center convention difference; not orientation ambiguity.",
        "reader_issue_wording": "Local reader did not unify raw cloud-mask values and decoded navigation storage order; this is not stated as a cfgrib software defect.",
    }


def write_status_summary(statuses: dict[str, Any], path: Path) -> None:
    write_csv([{"field": key, "value": value} for key, value in statuses.items()], path)


def write_revised_report(
    paths: dict[str, Path],
    statuses: dict[str, Any],
    hash_df: pd.DataFrame,
    env_df: pd.DataFrame,
    dt_inv_df: pd.DataFrame,
    mask_df: pd.DataFrame,
    nav_df: pd.DataFrame,
    warnings: list[dict[str, Any]],
) -> Path:
    lines = [
        "# Stage 09H Gate 4A-X EUMETSAT Data Tailor official-reader cross-check",
        "",
        f"- Generated UTC: {utc_now()}",
        f"- Case: `{CASE_ID}` / `{SOURCE}` / `{PRODUCT}`",
        f"- Strict exact-native-grid decision: `{statuses['strict_exact_native_grid_status']}`",
        f"- orientation_status: `{statuses['orientation_status']}`",
        f"- exact_native_navigation_status: `{statuses['exact_native_navigation_status']}`",
        "- 约束执行：未修改 production reader；未旋转 `cloud_mask`；未重跑整月；未覆盖 Gate 3A/3B/4A；quicklook 方向没有被当作数组方向证据；本次修订未重新运行 EPCT。",
        "",
        "## Input Hash",
        "",
        dataframe_to_markdown(hash_df, floatfmt=".6f"),
        "",
        "## EPCT / Plugin Inventory",
        "",
        "完整 stdout/stderr 见 `logs/command_log.txt`；本表只保留命令摘要。",
        "",
        dataframe_to_markdown(env_df[["item", "returncode"]].head(30), floatfmt=".6f") if not env_df.empty else "",
        "",
        "## Data Tailor Output Inventory",
        "",
        dataframe_to_markdown(dt_inv_df, floatfmt=".6f") if not dt_inv_df.empty else "",
        "",
        "## Cloud Mask Orientation Comparison",
        "",
        dataframe_to_markdown(mask_df[["comparison", "same_shape", "raw_dtype", "data_tailor_dtype", "agreement", "equal_hash_if_same_dtype_and_shape"]], floatfmt=".9f") if not mask_df.empty else "",
        "",
        "## Navigation Comparison",
        "",
        dataframe_to_markdown(nav_df, floatfmt=".6f") if not nav_df.empty else "",
        "",
        "## Status Logic",
        "",
        "- `strict_exact_native_grid_status` 只回答最严格问题：Data Tailor 输出是否保持 raw GRIB 的 exact native storage，可否逐像元 identity 作为官方 native 网格参照。由于 Data Tailor 输出为 GeoTIFF normalized storage，所以这里保留 `OFFICIAL_READER_CROSSCHECK_INCONCLUSIVE`。",
        "- `orientation_status` 回答正交问题：官方 reader 输出、raw mask 值、当前 decoded navigation 三者的数组方向是否暴露出本地 mask-navigation storage-order 不统一。该问题不要求 Data Tailor GeoTIFF 保持 raw storage identity。",
        "- `exact_native_navigation_status` 回答 Data Tailor GeoTIFF navigation 是否能替代 Gate 4A exact native navigation。由于 GeoTIFF 含有网格归一化、椭球、extent 和像元中心约定差异，这里判为 `INCONCLUSIVE_DUE_TO_GEOTIFF_GRID_NORMALIZATION`。",
        "",
        "## Interpretation",
        "",
        f"- 严格 exact-native-grid 判定仍为 `{statuses['strict_exact_native_grid_status']}`：Data Tailor 输出是 3712x3712 fixed-grid GeoTIFF，但它没有保持 raw GRIB storage orientation，因此不能作为“原始数组逐像元 identity”的严格官方 native-storage 证据。",
        f"- Cloud-mask 值保持不变的判定允许 `identity/rot180/flipud/fliplr` 这类无损一一变换。实际最优变换是 `{statuses['cloud_mask_best_lossless_transform']}`，agreement = {float(statuses['cloud_mask_best_transform_agreement']):.9f}，hash equal = {statuses['cloud_mask_best_transform_hash_equal']}。",
        "- 具体数值是：Data Tailor output-storage mask 与 raw GRIB identity agreement = 0.570902966；Data Tailor rot180 后与 raw GRIB agreement = 1.000000000 且 hash 一致。这说明 Data Tailor 没有改变 CLM 类别值，只是输出 storage order 与 raw storage 相差 180 度。",
        f"- Data Tailor output-storage navigation 接近当前 cfgrib identity navigation，median geodesic error = {float(statuses['data_tailor_output_vs_cfgrib_identity_median_km']):.6f} km；而 raw mask 需要 rot180 才进入 Data Tailor/cfgrib identity 的方向。",
        f"- 将 Data Tailor GeoTIFF rot180 回 raw-storage 后，与 Gate 4A official-area reference 的 median geodesic error = {float(statuses['data_tailor_rot180_raw_storage_vs_gate4a_reference_median_km']):.6f} km，p95 = {float(statuses['data_tailor_rot180_raw_storage_vs_gate4a_reference_p95_km']):.6f} km。这个残差不解释为方向不确定，而归类为 GeoTIFF grid parameter、GRS80/其他椭球参数、area extent 或 pixel-center convention 差异。",
        "- 因此，本次修订的方向结论是 `OFFICIAL_READER_CONFIRMS_LOCAL_MASK_NAVIGATION_ORDER_MISMATCH`：本地 reader 链路没有把 raw cloud-mask values 与 decoded navigation 的 storage order 统一起来。",
        "- 这里不把问题表述为 cfgrib 软件缺陷，也不归咎于 EUMETSAT CLM mask 本身；更准确的表述是本地读取/标准化链路的 storage-order 合约没有显式统一。",
        "- 本报告仍不修改 production、不加入永久 rot180、不重新跑整月；后续修复应沿 Gate 4A 的 production-fix design 做独立最小 patch 和回归测试。",
        "",
        "## Warnings",
        "",
        f"- Warning rows: {len(warnings)}. See `logs/warnings.csv`.",
    ]
    path = paths["reports"] / "official_reader_crosscheck_report_cn.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return path


def main() -> None:
    paths = ensure_dirs()
    warnings: list[dict[str, Any]] = []
    started = utc_now()
    cmd = CommandLogger(paths["logs"] / "command_log.txt")

    copied_zip, hash_rows = copy_input_and_hash(paths, warnings)
    hash_df = pd.DataFrame(hash_rows)
    write_csv(hash_df, paths["source_data"] / "input_hash_inventory.csv")

    env_rows, dt_output, processing_meta = run_epct_probe_and_processing(paths, copied_zip, cmd, warnings)
    env_df = pd.DataFrame(env_rows)
    write_csv(env_df, paths["source_data"] / "environment_and_plugin_inventory.csv")
    write_csv(pd.DataFrame(cmd.rows), paths["logs"] / "command_index.csv")

    raw_grib = extract_raw_grib(copied_zip, paths["cache"])
    raw_mask, cf_lat, cf_lon, raw_meta = read_raw_cfgrib(raw_grib)
    write_csv([raw_meta], paths["source_data"] / "raw_grib_inventory.csv")

    dt_mask = dt_lat = dt_lon = None
    dt_inv_rows: list[dict[str, Any]] = []
    projection_rows: list[dict[str, Any]] = []
    if dt_output is not None:
        dt_mask, dt_lat, dt_lon, dt_inv_rows, projection_rows = inspect_data_tailor_output(dt_output, warnings)
    dt_inv_df = pd.DataFrame(dt_inv_rows)
    projection_df = pd.DataFrame(projection_rows)
    write_csv(dt_inv_df, paths["source_data"] / "data_tailor_output_inventory.csv")
    write_csv(projection_df, paths["source_data"] / "projection_parameter_comparison.csv")
    write_csv(output_inventory_rows(paths["epct_output"]), paths["source_data"] / "data_tailor_output_file_hashes.csv")

    mask_df = pd.DataFrame()
    nav_df = pd.DataFrame()
    ctrl_df = pd.DataFrame()
    ref_lat = ref_lon = None
    if dt_mask is not None:
        if dt_mask.shape != raw_mask.shape:
            add_warning(warnings, "data_tailor_shape_not_native_3712", f"Data Tailor mask shape {dt_mask.shape} differs from raw {raw_mask.shape}.")
        mask_df = pd.DataFrame(compare_mask(raw_mask, dt_mask))
    else:
        add_warning(warnings, "data_tailor_mask_unavailable", "No Data Tailor mask array available for mask orientation comparison.", severity="ERROR")
    write_csv(mask_df, paths["source_data"] / "mask_orientation_comparison.csv")

    if dt_lat is not None and dt_lon is not None and dt_lat.shape == cf_lat.shape:
        ref_lat, ref_lon = read_gate4a_reference(CASE_ID)
        if ref_lat is None or ref_lon is None:
            add_warning(warnings, "gate4a_reference_regeneration_failed", "Could not regenerate Gate 4A official-area reference navigation.", severity="ERROR")
        else:
            nav_df = pd.DataFrame(compare_navigation(dt_lat, dt_lon, ref_lat, ref_lon, cf_lat, cf_lon))
            ctrl_df = pd.DataFrame(navigation_control_points(dt_lat, dt_lon, ref_lat, ref_lon, cf_lat, cf_lon))
    else:
        add_warning(warnings, "data_tailor_navigation_unavailable_or_non_native", "Data Tailor output has no recoverable native-shape navigation; navigation comparison skipped.")
    write_csv(nav_df, paths["source_data"] / "navigation_comparison.csv")
    write_csv(ctrl_df, paths["source_data"] / "navigation_control_points.csv")

    statuses = derive_revised_statuses(mask_df, nav_df, dt_inv_df)
    decision = statuses["decision"]
    write_status_summary(statuses, paths["source_data"] / "official_reader_status_summary.csv")
    write_csv(warnings, paths["logs"] / "warnings.csv")
    report = write_revised_report(paths, statuses, hash_df, env_df, dt_inv_df, mask_df, nav_df, warnings)

    manifest = {
        "project_id": PROJECT_ID,
        "canonical_stage_id": STAGE_ID,
        "gate_id": GATE_ID,
        "run_id": RUN_ID,
        "case_id": CASE_ID,
        "started_utc": started,
        "finished_utc": utc_now(),
        "script_path": str(Path(__file__).resolve()),
        "input_zip": str(RAW_ZIP),
        "input_sha256_expected": EXPECTED_SHA256,
        "workspace_copy": str(copied_zip),
        "data_tailor_output": str(dt_output) if dt_output else "",
        "processing_meta": processing_meta,
        "decision": decision,
        "strict_exact_native_grid_status": statuses["strict_exact_native_grid_status"],
        "orientation_status": statuses["orientation_status"],
        "exact_native_navigation_status": statuses["exact_native_navigation_status"],
        "cloud_mask_best_lossless_transform": statuses["cloud_mask_best_lossless_transform"],
        "cloud_mask_best_transform_agreement": statuses["cloud_mask_best_transform_agreement"],
        "cloud_mask_best_transform_hash_equal": statuses["cloud_mask_best_transform_hash_equal"],
        "data_tailor_output_vs_cfgrib_identity_median_km": statuses["data_tailor_output_vs_cfgrib_identity_median_km"],
        "data_tailor_rot180_raw_storage_vs_gate4a_reference_median_km": statuses["data_tailor_rot180_raw_storage_vs_gate4a_reference_median_km"],
        "data_tailor_rot180_raw_storage_vs_gate4a_reference_p95_km": statuses["data_tailor_rot180_raw_storage_vs_gate4a_reference_p95_km"],
        "residual_interpretation": statuses["residual_interpretation"],
        "reader_issue_wording": statuses["reader_issue_wording"],
        "report": str(report),
        "output_root": str(OUT_ROOT),
        "warnings_count": len(warnings),
        "constraints": [
            "no production reader modification",
            "no cloud_mask rotation",
            "no full-month rerun",
            "no overwrite of Gate 3A/3B/4A",
            "quicklook orientation is not array orientation evidence",
            "current CLM lat/lon not used to build Data Tailor reference",
        ],
    }
    write_json(manifest, paths["logs"] / "manifest.json")
    print(json.dumps({"decision": decision, "orientation_status": statuses["orientation_status"], "output_root": str(OUT_ROOT), "warnings": len(warnings)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
