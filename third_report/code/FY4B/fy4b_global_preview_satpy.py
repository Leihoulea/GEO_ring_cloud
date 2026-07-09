from __future__ import annotations

"""FY4B 0.05 degree global preview using the Satpy-first route.

This module aligns FY4B preview generation with the preview pattern already used
by GOES, Himawari, and Meteosat:

- read calibrated channels through Satpy
- auto-match the GEO companion file when it exists
- resample to a global 0.05 degree lat/lon grid
- generate one VIS/NIR false-color quicklook, one IR mosaic, and separate
  quicklooks for the remaining reflective channels

The preview is intentionally a visualization product, not a formal L1g output.
"""

import warnings
from pathlib import Path
from typing import Iterable

import matplotlib
import numpy as np
from pyresample.geometry import AreaDefinition
from satpy import Scene

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from fy4b_standardized_l1_source_builder import (
    SATPY_READER,
    find_fy4b_file,
    find_fy4b_geo_file,
    find_project_root,
)


warnings.filterwarnings("ignore", message=".*PROJ database path.*")

GRID_RES_DEG = 0.05
TARGET_AREA = AreaDefinition(
    "global_latlon_005",
    "Global regular lat/lon 0.05 deg",
    "latlon",
    {"proj": "longlat", "datum": "WGS84"},
    int(360 / GRID_RES_DEG),
    int(180 / GRID_RES_DEG),
    (-180.0, -90.0, 180.0, 90.0),
)

VISIBLE_CHANNELS = ["C01", "C02", "C03"]
OTHER_CHANNELS = ["C04", "C05", "C06"]
IR_CHANNELS = ["C07", "C08", "C09", "C10", "C11", "C12", "C13", "C14", "C15"]

CHANNEL_LABELS = {
    "C01": "0.47 um VIS blue",
    "C02": "0.65 um VIS red",
    "C03": "0.825 um NIR vegetation",
    "C04": "1.379 um cirrus",
    "C05": "1.61 um snow/ice NIR",
    "C06": "2.225 um SWIR cloud",
    "C07": "3.75 um SWIR high gain BT",
    "C08": "3.75 um SWIR low gain BT",
    "C09": "6.25 um upper WV BT",
    "C10": "6.95 um mid WV BT",
    "C11": "7.42 um lower WV BT",
    "C12": "8.55 um IR 8.55 BT",
    "C13": "10.8 um IR window BT",
    "C14": "12.0 um split window BT",
    "C15": "13.3 um CO2 BT",
}


def safe_filename_token(text: str) -> str:
    """Convert a human-readable label into a filesystem-safe token."""
    for old, new in [
        (" ", "_"),
        (".", "p"),
        ("/", "_"),
        ("\\", "_"),
        ("(", ""),
        (")", ""),
        (":", "_"),
    ]:
        text = text.replace(old, new)
    return text


def hour_label(hour: str | int) -> str:
    """Format a UTC hour for plot titles."""
    return f"UTC {int(hour):02d}:00"


def file_tag(hour: str | int) -> str:
    """Format the YYYYMMDD_HHMM-style tag used in preview filenames."""
    return f"20240312_{int(hour):02d}00"


def normalize_percentile(arr: np.ndarray, pmin: float = 2, pmax: float = 98, gamma: float = 1.0) -> np.ndarray:
    """Percentile stretch and gamma-adjust one array for display."""
    vals = arr[np.isfinite(arr)]
    if vals.size == 0:
        return np.zeros(arr.shape, dtype=np.float32)
    lo, hi = np.nanpercentile(vals, [pmin, pmax])
    out = np.clip((arr - lo) / max(hi - lo, 1e-6), 0, 1)
    if gamma != 1.0:
        out = np.power(out, gamma)
    return np.nan_to_num(out, nan=0.0).astype(np.float32)


def save_rgb(rgb: np.ndarray, path: Path, title: str) -> None:
    """Save one RGB quicklook image."""
    fig, ax = plt.subplots(figsize=(16, 8), dpi=180)
    ax.imshow(rgb, extent=[-180, 180, -90, 90], origin="upper")
    ax.set_title(title)
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.grid(color="white", alpha=0.25, linewidth=0.4)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(path)


def save_gray(arr: np.ndarray, path: Path, title: str, cmap: str = "viridis", vmin: float | None = None, vmax: float | None = None) -> None:
    """Save one single-channel quicklook image."""
    fig, ax = plt.subplots(figsize=(16, 8), dpi=180)
    im = ax.imshow(arr, extent=[-180, 180, -90, 90], origin="upper", cmap=cmap, vmin=vmin, vmax=vmax)
    ax.set_title(title)
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.grid(color="white", alpha=0.25, linewidth=0.4)
    fig.colorbar(im, ax=ax, shrink=0.78, pad=0.02)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(path)


def save_mosaic(
    arrays: list[np.ndarray],
    labels: list[str],
    path: Path,
    title: str,
    cmap: str = "turbo_r",
    vmin: float = 190,
    vmax: float = 320,
    ncols: int = 3,
) -> None:
    """Save one multi-panel IR brightness-temperature mosaic."""
    n = len(arrays)
    nrows = int(np.ceil(n / ncols))
    fig, axes = plt.subplots(nrows, ncols, figsize=(5.0 * ncols, 3.2 * nrows), dpi=170, constrained_layout=True)
    axes = np.asarray(axes).ravel()
    last_im = None
    for ax, arr, label in zip(axes, arrays, labels):
        last_im = ax.imshow(arr, extent=[-180, 180, -90, 90], origin="upper", cmap=cmap, vmin=vmin, vmax=vmax)
        ax.set_title(label, fontsize=9)
        ax.set_xticks([-120, 0, 120])
        ax.set_yticks([-60, 0, 60])
        ax.grid(color="white", alpha=0.25, linewidth=0.3)
    for ax in axes[n:]:
        ax.axis("off")
    fig.suptitle(title, fontsize=13)
    if last_im is not None:
        cbar = fig.colorbar(last_im, ax=axes.tolist(), shrink=0.82, pad=0.015)
        cbar.set_label("Brightness temperature (K)")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(path)


def _scene_filenames(fy4b_file: Path, geo_file: Path | None) -> list[str]:
    """Build the Satpy input file list, adding GEO when available."""
    files = [str(fy4b_file)]
    if geo_file is not None:
        files.append(str(geo_file))
    return files


def load_resampled(scene_files: Iterable[str], channels: list[str], calibration: str) -> dict[str, np.ndarray]:
    """Load channels with Satpy and resample them to the global 0.05 degree grid."""
    scn = Scene(filenames=list(scene_files), reader=SATPY_READER)
    scn.load(channels, calibration=calibration)
    rs = scn.resample(TARGET_AREA, resampler="nearest", radius_of_influence=60000)
    result = {}
    for ch in channels:
        arr = rs[ch].values.astype(np.float32)
        if calibration == "reflectance":
            units = str(rs[ch].attrs.get("units", "")).strip()
            if units == "%":
                arr = arr / 100.0
        result[ch] = arr
    return result


def run_preview(target_hour_utc: str = "03", project_root: Path | str | None = None) -> dict[str, Path | str]:
    """Generate FY4B global preview products using Satpy."""
    project_root = find_project_root(project_root)
    data_root = project_root / "Satellite_Data_20240312"
    fy4b_root = data_root / "FY4B"
    out_dir = project_root / "code" / "FY4B" / "preview_005deg_outputs"
    out_dir.mkdir(parents=True, exist_ok=True)

    fy4b_file = find_fy4b_file(fy4b_root, target_hour_utc)
    geo_file = find_fy4b_geo_file(fy4b_file, fy4b_root=fy4b_root)
    scene_files = _scene_filenames(fy4b_file, geo_file)

    tag = file_tag(target_hour_utc)
    label = hour_label(target_hour_utc)

    print("FY4B preview route: Satpy-first")
    print("L1:", fy4b_file)
    print("GEO:", geo_file if geo_file is not None else "(not found)")

    # False-color quicklook:
    #   R = C02 (0.65 um red visible)
    #   G = C03 (0.825 um NIR; a false green component)
    #   B = C01 (0.47 um blue visible)
    # This matches the earlier FY4B preview intent, but the data now come from
    # Satpy instead of a hand-written LUT + index pipeline.
    vis = load_resampled(scene_files, VISIBLE_CHANNELS, "reflectance")
    rgb = np.dstack(
        [
            normalize_percentile(vis["C02"], 1, 99, gamma=0.75),
            normalize_percentile(vis["C03"], 1, 99, gamma=0.75),
            normalize_percentile(vis["C01"], 1, 99, gamma=0.75),
        ]
    )
    save_rgb(
        rgb,
        out_dir / f"FY4B_{tag}_global005_VIS_C02C03C01_false_color.png",
        f"FY4B AGRI {label} VIS/NIR false-color on 0.05 deg grid (R=C02, G=C03, B=C01)",
    )

    # Reflective NIR/SWIR channels are plotted individually to separate cirrus,
    # snow/ice/cloud phase, and SWIR cloud-particle sensitivity.
    other = load_resampled(scene_files, OTHER_CHANNELS, "reflectance")
    for ch, arr in other.items():
        vals = arr[np.isfinite(arr)]
        vmin, vmax = np.nanpercentile(vals, [1, 99]) if vals.size else (0.0, 1.0)
        safe_label = safe_filename_token(CHANNEL_LABELS[ch])
        save_gray(
            arr,
            out_dir / f"FY4B_{tag}_global005_{ch}_{safe_label}.png",
            f"FY4B AGRI {label} {ch} {CHANNEL_LABELS[ch]} reflectance on 0.05 deg grid",
            cmap="viridis",
            vmin=vmin,
            vmax=vmax,
        )

    # Thermal IR and water-vapor channels are shown together with one BT color
    # scale so cloud tops, surfaces, and absorption differences can be compared.
    ir = load_resampled(scene_files, IR_CHANNELS, "brightness_temperature")
    save_mosaic(
        [ir[ch] for ch in IR_CHANNELS],
        [f"{ch} {CHANNEL_LABELS[ch]}" for ch in IR_CHANNELS],
        out_dir / f"FY4B_{tag}_global005_IR_BT_C07_C15_mosaic.png",
        f"FY4B AGRI {label} infrared brightness-temperature channels on 0.05 deg grid",
        ncols=3,
    )

    return {
        "fy4b_file": fy4b_file,
        "geo_file": geo_file if geo_file is not None else "",
        "output_dir": out_dir,
    }


if __name__ == "__main__":
    run_preview("03")
