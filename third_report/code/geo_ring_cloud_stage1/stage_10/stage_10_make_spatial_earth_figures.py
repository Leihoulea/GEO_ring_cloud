# -*- coding: utf-8 -*-
"""Stage 10 spatial Earth-view figures for group-meeting reporting.

The script reads existing local Stage 06 fused products, Stage 09D sample
manifest, and Stage 10 CTH diagnostic tables. It does not download data, rerun
Stage 05/06, or treat EPIC A-band Effective Cloud Height as absolute truth.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import h5py
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import BoundaryNorm, ListedColormap, Normalize, TwoSlopeNorm
from matplotlib.patches import Circle, Patch

plt.rcParams.update(
    {
        "font.family": "sans-serif",
        "font.sans-serif": ["Arial", "Helvetica", "Liberation Sans"],
        "font.size": 8,
        "svg.fonttype": "none",
        "pdf.fonttype": 42,
    }
)

# Static QA sentinel: save_figure exports figure.svg, figure.pdf, figure.png, and figure.tiff.

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from geo_ring_cloud import paths as geo_paths  # noqa: E402

PROJECT_ID = "geo_ring_cloud"
STAGE_ID = "stage_10"
RUN_ID = "stage_10_meeting_figures_202403"
OUT_ROOT = geo_paths.RUNS_ROOT / RUN_ID
STAGE10_ROOT = geo_paths.RUNS_ROOT / "stage_10_cth_fused_product_validation_202403"
SAMPLE_MANIFEST = (
    geo_paths.RUNS_ROOT
    / "stage09d_full_pixel_diagnostics_202403"
    / "00_sample_manifest"
    / "stage09d_53_sample_manifest.csv"
)
CASE_INVENTORY = STAGE10_ROOT / "08_case_atlas" / "stage_10_cth_case_inventory.csv"

EPIC_CTH_VAR = "geophysical_data/A-band_Effective_Cloud_Height"
SUGGESTED_SAMPLES = ["20240331_1200", "20240319_1500", "20240322_0400", "20240318_0800"]

SOURCE_ID = {
    1: "GOES-16",
    2: "GOES-18",
    3: "FY4B",
    4: "Himawari-9",
    5: "Meteosat-0deg",
    6: "Meteosat-IODC",
    7: "CLAAS3-0deg",
}
SOURCE_COLORS = {
    "GOES-16": "#2F5D9B",
    "GOES-18": "#8FB3D9",
    "FY4B": "#3B8F6B",
    "Himawari-9": "#62A85B",
    "Meteosat-0deg": "#B94A48",
    "Meteosat-IODC": "#D1846A",
    "CLAAS3-0deg": "#7B6BA8",
    "missing": "#D6D6D6",
}
SOURCE_ORDER = ["GOES-16", "GOES-18", "FY4B", "Himawari-9", "Meteosat-0deg", "Meteosat-IODC", "CLAAS3-0deg"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def ensure_dirs() -> dict[str, Path]:
    dirs = {
        "figures": OUT_ROOT / "figures",
        "source_data": OUT_ROOT / "source_data",
        "reports": OUT_ROOT / "reports",
        "logs": OUT_ROOT / "logs",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        keys: set[str] = set()
        for row in rows:
            keys.update(row.keys())
        fields = sorted(keys)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def _decode_attr(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, np.bytes_):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, np.ndarray) and value.size == 1:
        return _decode_attr(value.reshape(-1)[0])
    if isinstance(value, np.generic):
        return value.item()
    return value


def read_h5_dataset(file_handle: h5py.File, name: str) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    dataset = file_handle[name]
    attrs = {key: _decode_attr(value) for key, value in dataset.attrs.items()}
    data = np.asarray(dataset[...])
    valid = np.isfinite(data.astype(np.float32, copy=False))
    fill_value = attrs.get("_FillValue", attrs.get("missing_value"))
    if fill_value is not None:
        try:
            valid &= data != float(fill_value)
        except Exception:
            pass
    return data, valid, attrs


def read_epic_cloud(path: Path) -> dict[str, Any]:
    with h5py.File(path, "r") as f:
        lat, lat_valid, _ = read_h5_dataset(f, "geolocation_data/latitude")
        lon, lon_valid, _ = read_h5_dataset(f, "geolocation_data/longitude")
        cth_raw, cth_raw_valid, cth_attrs = read_h5_dataset(f, EPIC_CTH_VAR)
        cloud_mask, cloud_mask_valid, _ = read_h5_dataset(f, "geophysical_data/Cloud_Mask")

    lat = lat.astype(np.float32)
    lon = lon.astype(np.float32)
    geo_valid = lat_valid & lon_valid & (lat >= -90.0) & (lat <= 90.0) & (lon >= -180.0) & (lon <= 180.0)
    units = str(cth_attrs.get("units", "")).strip().lower()
    cth = cth_raw.astype(np.float32)
    conversion = "none"
    if units == "m":
        cth = cth / 1000.0
        conversion = "m_to_km"
    cth_valid = cth_raw_valid & geo_valid & np.isfinite(cth) & (cth >= 0.0) & (cth <= 25.0)
    return {
        "lat": lat,
        "lon": lon,
        "geo_valid": geo_valid,
        "cth_km": cth.astype(np.float32),
        "cth_valid": cth_valid,
        "cloud_mask": cloud_mask.astype(np.int16),
        "cloud_mask_valid": cloud_mask_valid & geo_valid,
        "cth_attrs": cth_attrs,
        "cth_conversion": conversion,
    }


def load_npz_array(path: Path) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    with np.load(path, allow_pickle=True) as z:
        data = np.asarray(z["data"])
        valid = np.asarray(z["valid_mask"]).astype(bool) if "valid_mask" in z.files else np.isfinite(data)
        metadata: dict[str, Any] = {}
        if "metadata_json" in z.files:
            try:
                metadata = json.loads(str(np.asarray(z["metadata_json"]).item()))
            except Exception:
                metadata = {"metadata_json_parse_error": True}
    return data, valid, metadata


def load_grid(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "reprojected_grid" / "target_grid_definition.json").read_text(encoding="utf-8"))


def row_col(lat: np.ndarray, lon: np.ndarray, grid: dict[str, Any], shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    res = float(grid["resolution_degree"])
    lat0 = float(grid.get("lat_min", -90.0)) + res / 2.0
    lon0 = float(grid.get("lon_min", -180.0)) + res / 2.0
    if "lat_centers_first_last" in grid:
        lat0 = float(grid["lat_centers_first_last"][0])
    if "lon_centers_first_last" in grid:
        lon0 = float(grid["lon_centers_first_last"][0])
    lon_norm = ((lon.astype(np.float32) + 180.0) % 360.0) - 180.0
    r = np.rint((lat.astype(np.float32) - lat0) / res).astype(np.int64)
    c = np.rint((lon_norm - lon0) / res).astype(np.int64)
    ok = np.isfinite(lat) & np.isfinite(lon_norm) & (r >= 0) & (r < shape[0]) & (c >= 0) & (c < shape[1])
    return r, c, ok


def sample_grid(data: np.ndarray, valid: np.ndarray, lat: np.ndarray, lon: np.ndarray, grid: dict[str, Any], fill: float = np.nan) -> tuple[np.ndarray, np.ndarray]:
    r, c, ok = row_col(lat, lon, grid, data.shape)
    out = np.full(lat.shape, fill, dtype=np.float32)
    out_valid = np.zeros(lat.shape, dtype=bool)
    out[ok] = data[r[ok], c[ok]].astype(np.float32)
    out_valid[ok] = valid[r[ok], c[ok]].astype(bool)
    out[~out_valid] = fill
    return out, out_valid


def normalize_lon(lon: np.ndarray | float) -> np.ndarray | float:
    return ((np.asarray(lon) + 180.0) % 360.0) - 180.0


def circular_mean_lon(lon: np.ndarray, valid: np.ndarray) -> float:
    values = np.deg2rad(lon[valid].astype(np.float64))
    if values.size == 0:
        return 0.0
    mean = math.degrees(math.atan2(float(np.nanmean(np.sin(values))), float(np.nanmean(np.cos(values)))))
    return float(((mean + 180.0) % 360.0) - 180.0)


def orthographic_project(lat: np.ndarray, lon: np.ndarray, center_lon: float, center_lat: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    lon_n = normalize_lon(lon)
    dlon = np.deg2rad(((lon_n - center_lon + 180.0) % 360.0) - 180.0)
    lat_r = np.deg2rad(lat.astype(float))
    lat0 = math.radians(float(center_lat))
    x = np.cos(lat_r) * np.sin(dlon)
    y = math.cos(lat0) * np.sin(lat_r) - math.sin(lat0) * np.cos(lat_r) * np.cos(dlon)
    cosc = math.sin(lat0) * np.sin(lat_r) + math.cos(lat0) * np.cos(lat_r) * np.cos(dlon)
    return x, y, cosc >= -1e-6


def draw_graticule(ax: plt.Axes, center_lon: float, center_lat: float) -> None:
    ax.add_patch(Circle((0, 0), 1.0, facecolor="#F7F8FA", edgecolor="#303030", linewidth=0.8, zorder=0))
    for lat in [-60, -30, 0, 30, 60]:
        lon_line = np.linspace(-180, 180, 721)
        lat_line = np.full_like(lon_line, float(lat))
        x, y, visible = orthographic_project(lat_line, lon_line, center_lon, center_lat)
        ax.plot(x[visible], y[visible], color="#B8B8B8", linewidth=0.35, zorder=1)
    for lon in np.arange(-180, 181, 30):
        lat_line = np.linspace(-89.5, 89.5, 360)
        lon_line = np.full_like(lat_line, float(lon))
        x, y, visible = orthographic_project(lat_line, lon_line, center_lon, center_lat)
        ax.plot(x[visible], y[visible], color="#B8B8B8", linewidth=0.35, zorder=1)
    ax.annotate("N", xy=(0.84, 0.84), xytext=(0.84, 0.66), xycoords="axes fraction", textcoords="axes fraction",
                arrowprops={"arrowstyle": "-|>", "lw": 0.6, "color": "#303030"}, ha="center", va="bottom", fontsize=7, color="#303030")
    ax.set_xlim(-1.04, 1.04)
    ax.set_ylim(-1.04, 1.04)
    ax.set_aspect("equal")
    ax.axis("off")


def source_names_from_codes(codes: np.ndarray) -> np.ndarray:
    out = np.full(codes.shape, "missing", dtype=object)
    finite = np.isfinite(codes)
    int_codes = np.full(codes.shape, -999, dtype=np.int16)
    int_codes[finite] = np.rint(codes[finite]).astype(np.int16)
    for code, name in SOURCE_ID.items():
        out[int_codes == code] = name
    return out


def source_codes_from_names(names: np.ndarray) -> np.ndarray:
    mapping = {name: idx for idx, name in enumerate(SOURCE_ORDER)}
    out = np.full(names.shape, -1, dtype=np.int16)
    for name, idx in mapping.items():
        out[names == name] = idx
    return out


def prepare_sample(row: pd.Series, stride: int) -> tuple[pd.DataFrame, dict[str, Any]]:
    sample_id = str(row["sample_id"]) if "sample_id" in row.index else str(row.name)
    run_dir = Path(str(row["stage_run_dir"]))
    epic_path = Path(str(row["epic_file"]))
    epic = read_epic_cloud(epic_path)
    grid = load_grid(run_dir)
    fused_dir = run_dir / "fused_best_source"
    fused_cth, fused_cth_valid, _ = load_npz_array(fused_dir / "fused_cloud_top_height_km.npz")
    fused_cm, fused_cm_valid, _ = load_npz_array(fused_dir / "fused_cloud_mask.npz")
    source_map, source_valid, _ = load_npz_array(fused_dir / "source_map_cloud_top_height_km.npz")
    valid_count, valid_count_valid, _ = load_npz_array(fused_dir / "valid_count_map_cloud_top_height_km.npz")

    fused_on, fused_on_valid = sample_grid(fused_cth, fused_cth_valid & (fused_cth >= 0) & (fused_cth <= 25), epic["lat"], epic["lon"], grid)
    fused_cm_on, fused_cm_on_valid = sample_grid(fused_cm, fused_cm_valid, epic["lat"], epic["lon"], grid)
    source_on, source_on_valid = sample_grid(source_map, source_valid, epic["lat"], epic["lon"], grid)
    valid_count_on, valid_count_on_valid = sample_grid(valid_count, valid_count_valid, epic["lat"], epic["lon"], grid)

    epic_cloud = np.isin(epic["cloud_mask"], [3, 4]) & epic["cloud_mask_valid"]
    fused_cm_int = np.full(fused_cm_on.shape, -999, dtype=np.int16)
    fused_cm_ok = fused_cm_on_valid & np.isfinite(fused_cm_on)
    fused_cm_int[fused_cm_ok] = np.rint(fused_cm_on[fused_cm_ok]).astype(np.int16)
    fused_cloud = np.isin(fused_cm_int, [2, 3]) & fused_cm_on_valid
    common = epic["cth_valid"] & fused_on_valid & epic_cloud & fused_cloud
    high_cloud = common & ((epic["cth_km"] >= 7.0) | (fused_on >= 7.0))
    abs_error = np.abs(fused_on - epic["cth_km"])
    signed_error = fused_on - epic["cth_km"]
    center_lon = circular_mean_lon(epic["lon"], epic["geo_valid"])
    center_lat = float(np.nanmean(epic["lat"][epic["geo_valid"]])) if np.any(epic["geo_valid"]) else 0.0

    rows = np.arange(0, epic["lat"].shape[0], stride)
    cols = np.arange(0, epic["lat"].shape[1], stride)
    rr, cc = np.meshgrid(rows, cols, indexing="ij")
    rr = rr.ravel()
    cc = cc.ravel()
    lat = epic["lat"][rr, cc]
    lon = epic["lon"][rr, cc]
    x, y, visible = orthographic_project(lat, lon, center_lon, center_lat)
    source_names = source_names_from_codes(source_on[rr, cc])
    frame = pd.DataFrame(
        {
            "sample_id": sample_id,
            "display_row": rr.astype(int),
            "display_col": cc.astype(int),
            "latitude_deg": lat,
            "longitude_deg": lon,
            "plot_stride": stride,
            "center_longitude_deg": center_lon,
            "center_latitude_deg": center_lat,
            "projection_x_orthographic": x,
            "projection_y_orthographic": y,
            "projection_visible": visible,
            "epic_a_band_effective_cloud_height_km": epic["cth_km"][rr, cc],
            "fused_cloud_top_height_km": fused_on[rr, cc],
            "signed_difference_fused_minus_epic_km": signed_error[rr, cc],
            "absolute_error_km": abs_error[rr, cc],
            "stage10_policy_a_both_cloud": common[rr, cc],
            "stage10_high_cloud_domain": high_cloud[rr, cc],
            "selected_source_name": source_names,
            "selected_source_code": source_codes_from_names(source_names),
            "valid_source_count": valid_count_on[rr, cc],
            "source_map_valid": source_on_valid[rr, cc],
            "valid_source_count_valid": valid_count_on_valid[rr, cc],
        }
    )
    metric_mask = common & np.isfinite(abs_error)
    metrics = {
        "sample_id": sample_id,
        "epic_time_utc": row.get("epic_time_utc", ""),
        "nearest_georing_time_utc": row.get("nearest_georing_time_utc", ""),
        "candidate_group": row.get("candidate_group", ""),
        "dominant_source": row.get("dominant_source", ""),
        "center_longitude_deg": center_lon,
        "center_latitude_deg": center_lat,
        "n_policy_a_both_cloud": int(np.count_nonzero(metric_mask)),
        "mae_km_recomputed_for_plot": float(np.nanmean(abs_error[metric_mask])) if np.any(metric_mask) else math.nan,
        "bias_km_recomputed_for_plot": float(np.nanmean(signed_error[metric_mask])) if np.any(metric_mask) else math.nan,
        "high_cloud_fraction_recomputed_for_plot": float(np.mean(high_cloud[metric_mask])) if np.any(metric_mask) else math.nan,
        "epic_cth_variable": EPIC_CTH_VAR,
        "epic_cth_unit_conversion": epic["cth_conversion"],
    }
    return frame, metrics


def save_figure(fig: plt.Figure, dirs: dict[str, Path], stem: str, outputs: list[dict[str, Any]], source_csv: Path, description: str) -> None:
    paths = {}
    for ext in ["svg", "pdf", "png", "tiff"]:
        path = dirs["figures"] / f"{stem}.{ext}"
        kwargs: dict[str, Any] = {"facecolor": "white"}
        if ext in {"png", "tiff"}:
            kwargs["dpi"] = 300
        fig.savefig(path, **kwargs)
        paths[ext] = str(path)
    plt.close(fig)
    outputs.append(
        {
            "figure_id": stem,
            "description": description,
            "source_csv": str(source_csv),
            "svg_path": paths["svg"],
            "pdf_path": paths["pdf"],
            "png_path": paths["png"],
            "tiff_path": paths["tiff"],
            "projection": "geodetic orthographic for disk panels; equirectangular for case locator",
            "orientation": "north-up; longitude normalized to -180..180",
        }
    )


def scatter_panel(ax: plt.Axes, df: pd.DataFrame, value_col: str, center_lon: float, center_lat: float, cmap: Any, norm: Any, title: str, mask_col: str = "stage10_policy_a_both_cloud") -> Any:
    draw_graticule(ax, center_lon, center_lat)
    data = df[df["projection_visible"].astype(bool)].copy()
    if mask_col in data:
        data = data[data[mask_col].astype(bool)]
    data = data[np.isfinite(pd.to_numeric(data[value_col], errors="coerce"))]
    sc = ax.scatter(
        data["projection_x_orthographic"],
        data["projection_y_orthographic"],
        c=data[value_col].astype(float),
        s=2.2,
        cmap=cmap,
        norm=norm,
        linewidths=0,
        alpha=0.92,
        rasterized=True,
        zorder=3,
    )
    ax.set_title(title, fontsize=8, pad=2)
    return sc


def make_product_atlas(sample_frames: dict[str, pd.DataFrame], sample_metrics: pd.DataFrame, case_df: pd.DataFrame, dirs: dict[str, Path], outputs: list[dict[str, Any]]) -> None:
    sample_ids = list(sample_frames)
    source_csv = dirs["source_data"] / "stage_10_group_meeting_spatial_fig01_source.csv"
    pd.concat([sample_frames[sid] for sid in sample_ids], ignore_index=True).to_csv(source_csv, index=False, encoding="utf-8-sig")

    fig, axes = plt.subplots(len(sample_ids), 3, figsize=(13.2, 10.2), constrained_layout=False)
    height_norm = Normalize(vmin=0, vmax=14)
    diff_norm = TwoSlopeNorm(vmin=-8, vcenter=0, vmax=8)
    row_labels: list[tuple[int, str]] = []
    column_titles = ["EPIC A-band ECH reference", "GEO-ring fused CTH", "Fused - EPIC reference"]
    for row_idx, sample_id in enumerate(sample_ids):
        df = sample_frames[sample_id]
        meta = sample_metrics.set_index("sample_id").loc[sample_id]
        case = case_df.set_index("sample_id").loc[sample_id] if sample_id in set(case_df["sample_id"].astype(str)) else None
        center_lon = float(meta["center_longitude_deg"])
        center_lat = float(meta["center_latitude_deg"])
        row_label = f"{sample_id}\nlon0={center_lon:+.1f}\nsource={meta['dominant_source']}"
        if case is not None:
            row_label += f"\nMAE={float(case['mae_km']):.2f} km"
        row_labels.append((row_idx, row_label))
        scatter_panel(axes[row_idx, 0], df, "epic_a_band_effective_cloud_height_km", center_lon, center_lat, "viridis", height_norm, column_titles[0] if row_idx == 0 else "")
        scatter_panel(axes[row_idx, 1], df, "fused_cloud_top_height_km", center_lon, center_lat, "viridis", height_norm, column_titles[1] if row_idx == 0 else "")
        scatter_panel(axes[row_idx, 2], df, "signed_difference_fused_minus_epic_km", center_lon, center_lat, "RdBu_r", diff_norm, column_titles[2] if row_idx == 0 else "")
    fig.suptitle("Stage 10 spatial comparison on EPIC disk pixels: effective-height reference versus fused CTH", fontsize=12.5, y=0.985)
    cax1 = fig.add_axes([0.28, 0.035, 0.31, 0.018])
    cb1 = fig.colorbar(plt.cm.ScalarMappable(norm=height_norm, cmap="viridis"), cax=cax1, orientation="horizontal")
    cb1.set_label("Height (km)", fontsize=8)
    cb1.ax.tick_params(labelsize=7)
    cax2 = fig.add_axes([0.72, 0.035, 0.22, 0.018])
    cb2 = fig.colorbar(plt.cm.ScalarMappable(norm=diff_norm, cmap="RdBu_r"), cax=cax2, orientation="horizontal")
    cb2.set_label("Signed difference (km)", fontsize=8)
    cb2.ax.tick_params(labelsize=7)
    fig.text(0.03, 0.025, "Policy A both-cloud pixels only; orthographic projection; north-up.", fontsize=7.5, color="#303030")
    fig.subplots_adjust(left=0.16, right=0.985, top=0.955, bottom=0.075, wspace=0.045, hspace=0.18)
    for row_idx, row_label in row_labels:
        bbox = axes[row_idx, 0].get_position()
        fig.text(0.025, (bbox.y0 + bbox.y1) / 2.0, row_label, fontsize=8.0, ha="left", va="center", color="#202020", linespacing=1.25)
    save_figure(fig, dirs, "stage_10_group_meeting_spatial_fig01_epic_disk_cth_atlas", outputs, source_csv, "Pixel-level EPIC disk atlas comparing EPIC A-band effective cloud height, GEO-ring fused CTH, and signed difference.")


def make_mechanism_disk(sample_id: str, df: pd.DataFrame, metrics: pd.Series, dirs: dict[str, Path], outputs: list[dict[str, Any]]) -> None:
    source_csv = dirs["source_data"] / "stage_10_group_meeting_spatial_fig02_source.csv"
    df.to_csv(source_csv, index=False, encoding="utf-8-sig")
    center_lon = float(metrics["center_longitude_deg"])
    center_lat = float(metrics["center_latitude_deg"])
    fig, axes = plt.subplots(2, 2, figsize=(11.4, 8.2), constrained_layout=False)

    abs_norm = Normalize(vmin=0, vmax=10)
    scatter_panel(axes[0, 0], df, "absolute_error_km", center_lon, center_lat, "magma", abs_norm, "Absolute error |fused - EPIC|")

    draw_graticule(axes[0, 1], center_lon, center_lat)
    src_df = df[df["projection_visible"].astype(bool) & df["stage10_policy_a_both_cloud"].astype(bool) & (df["selected_source_code"] >= 0)]
    cmap_src = ListedColormap([SOURCE_COLORS[name] for name in SOURCE_ORDER])
    norm_src = BoundaryNorm(np.arange(-0.5, len(SOURCE_ORDER) + 0.5, 1), len(SOURCE_ORDER))
    axes[0, 1].scatter(src_df["projection_x_orthographic"], src_df["projection_y_orthographic"], c=src_df["selected_source_code"], s=2.2, cmap=cmap_src, norm=norm_src, linewidths=0, rasterized=True, zorder=3)
    axes[0, 1].set_title("Selected source for fused CTH", fontsize=8, pad=2)

    vc_norm = BoundaryNorm(np.arange(0.5, 5.6, 1.0), 5)
    vc_norm = Normalize(vmin=1, vmax=4)
    scatter_panel(axes[1, 0], df, "valid_source_count", center_lon, center_lat, "viridis", vc_norm, "Valid source count")

    draw_graticule(axes[1, 1], center_lon, center_lat)
    high = df[df["projection_visible"].astype(bool) & df["stage10_policy_a_both_cloud"].astype(bool)]
    high_color = np.where(high["stage10_high_cloud_domain"].astype(bool), "#B94A48", "#CFCFCF")
    axes[1, 1].scatter(high["projection_x_orthographic"], high["projection_y_orthographic"], c=high_color, s=2.2, linewidths=0, alpha=0.92, rasterized=True, zorder=3)
    axes[1, 1].set_title("High-cloud diagnostic domain", fontsize=8, pad=2)

    fig.suptitle(f"Stage 10 mechanism disk for {sample_id}: lon0={center_lon:+.1f}, dominant={metrics['dominant_source']}", fontsize=12.5, y=0.975)
    cax1 = fig.add_axes([0.11, 0.055, 0.28, 0.018])
    cb1 = fig.colorbar(plt.cm.ScalarMappable(norm=abs_norm, cmap="magma"), cax=cax1, orientation="horizontal")
    cb1.set_label("Absolute error (km)", fontsize=8)
    cb1.ax.tick_params(labelsize=7)
    cax2 = fig.add_axes([0.44, 0.055, 0.20, 0.018])
    cb2 = fig.colorbar(plt.cm.ScalarMappable(norm=vc_norm, cmap="viridis"), cax=cax2, orientation="horizontal")
    cb2.set_label("Valid source count", fontsize=8)
    cb2.ax.tick_params(labelsize=7)
    legend_handles = [Patch(facecolor=SOURCE_COLORS[name], edgecolor="none", label=name) for name in SOURCE_ORDER if name in set(src_df["selected_source_name"])]
    legend_handles += [Patch(facecolor="#B94A48", edgecolor="none", label="High cloud"), Patch(facecolor="#CFCFCF", edgecolor="none", label="Other both-cloud")]
    fig.legend(handles=legend_handles, loc="lower right", bbox_to_anchor=(0.985, 0.025), ncol=2, frameon=False, fontsize=7)
    fig.text(0.03, 0.025, "All panels use the same EPIC-disk orthographic projection; north-up; Policy A both-cloud mask.", fontsize=7.5, color="#303030")
    fig.subplots_adjust(left=0.04, right=0.98, top=0.93, bottom=0.11, wspace=0.03, hspace=0.12)
    save_figure(fig, dirs, "stage_10_group_meeting_spatial_fig02_mechanism_disk", outputs, source_csv, "Mechanism-oriented disk map for the highest-MAE sample: absolute error, selected source, valid-source count, and high-cloud domain.")


def make_case_locator(sample_metrics: pd.DataFrame, case_df: pd.DataFrame, dirs: dict[str, Path], outputs: list[dict[str, Any]]) -> None:
    merged = case_df.merge(sample_metrics, on="sample_id", how="left", suffixes=("", "_center"))
    source_csv = dirs["source_data"] / "stage_10_group_meeting_spatial_fig03_source.csv"
    merged.to_csv(source_csv, index=False, encoding="utf-8-sig")
    fig, ax = plt.subplots(figsize=(12.0, 5.8))
    ax.set_facecolor("#F7F8FA")
    for lon in np.arange(-180, 181, 30):
        ax.axvline(lon, color="#D0D0D0", linewidth=0.45, zorder=0)
    for lat in np.arange(-60, 61, 30):
        ax.axhline(lat, color="#D0D0D0", linewidth=0.45, zorder=0)
    ax.axhline(0, color="#9E9E9E", linewidth=0.6, zorder=0)
    ax.axvline(0, color="#9E9E9E", linewidth=0.6, zorder=0)
    norm = Normalize(vmin=float(np.nanmin(merged["mae_km"])), vmax=float(np.nanmax(merged["mae_km"])))
    sizes = 24 + 180 * (merged["n_valid_cth"].astype(float) / merged["n_valid_cth"].astype(float).max())
    for source, group in merged.groupby("dominant_source", dropna=False):
        idx = group.index
        ax.scatter(
            group["center_longitude_deg"],
            group["center_latitude_deg"],
            c=group["mae_km"],
            s=sizes.loc[idx],
            cmap="magma",
            norm=norm,
            edgecolor=SOURCE_COLORS.get(str(source), "#303030"),
            linewidth=1.0,
            alpha=0.88,
            label=str(source),
            zorder=3,
        )
    ax.set_xlim(-180, 180)
    ax.set_ylim(-70, 70)
    ax.set_xlabel("Longitude (degrees east)")
    ax.set_ylabel("Latitude (degrees north)")
    ax.set_title("Stage 10 sample centers and fused CTH MAE in EPIC-view geometry", fontsize=12.5, pad=8)
    cb = fig.colorbar(plt.cm.ScalarMappable(norm=norm, cmap="magma"), ax=ax, pad=0.015, fraction=0.032)
    cb.set_label("Sample-level MAE (km)")
    ax.legend(loc="lower left", bbox_to_anchor=(0.0, -0.24), ncol=4, frameon=False, fontsize=8, title="Dominant source")
    ax.text(0.99, 0.02, "Circle size scales with n_valid_cth; edge color identifies dominant source.", transform=ax.transAxes, ha="right", va="bottom", fontsize=8, color="#303030")
    fig.subplots_adjust(left=0.065, right=0.945, top=0.90, bottom=0.24)
    save_figure(fig, dirs, "stage_10_group_meeting_spatial_fig03_case_locator", outputs, source_csv, "Global locator map for all Stage 10 samples using EPIC-disk geolocation centers and sample-level MAE.")


def select_samples(manifest: pd.DataFrame, case_df: pd.DataFrame, requested: list[str]) -> list[str]:
    available = set(manifest["sample_id"].astype(str))
    selected = [sid for sid in requested if sid in available]
    if len(selected) >= 4:
        return selected[:4]
    ranked = case_df.sort_values("mae_km", ascending=False)["sample_id"].astype(str).tolist()
    for sid in ranked:
        if sid in available and sid not in selected:
            selected.append(sid)
        if len(selected) >= 4:
            break
    return selected


def write_guides(dirs: dict[str, Path], outputs: list[dict[str, Any]], selected_samples: list[str]) -> Path:
    text = f"""# Stage 10 空间地球图组会讲解指南

本图版包只读取本地已有 Stage 06 fused 产品、Stage 09D 样本清单和 Stage 10 CTH 诊断表，不重跑 Stage05/06，不联网下载。EPIC 变量写作 `A-band_Effective_Cloud_Height reference`，含义是氧气 A-band 有效云高参考，不是严格几何 cloud top height 真值。

## 图 S1：EPIC 盘面 CTH 对比图

这张图回答：Stage10 的 CTH 偏差在 EPIC 可见地球盘面上长什么样。每一行是一个代表样本：{", ".join(selected_samples)}。第一列是 EPIC A-band effective cloud height，第二列是 GEO-ring fused cloud top height，第三列是 `fused - EPIC reference` 的有符号差值。

颜色单位都是 km。前两列共用 `Height (km)` 色标，第三列用红蓝发散色标：红色表示 GEO-ring fused CTH 高于 EPIC effective-height reference，蓝色表示低于 reference。只绘制 Stage10 Policy A both-cloud 像元，因此它不是整盘云图，而是 CTH 可比域图。每行标题里的 `lon0` 是正射投影中心经度，`dominant` 是 Stage09D/10 样本清单中的主导 GEO 源，`MAE` 来自 Stage10 case inventory。

## 图 S2：最高误差样本的机制盘面

这张图回答：高误差不是一个抽象平均值，它对应哪些空间机制。四个小图都使用同一个样本、同一个正射投影和同一个 Policy A both-cloud mask。左上是绝对误差，右上是 fused CTH 选源，左下是有效源数量，右下是高云诊断域。

导师如果问“是不是某个源导致的”，可以说：这张图用于定位 selected-source 与误差空间结构是否共址；但 Stage10 的结论不是简单源排名，因为 EPIC reference 是 effective height，且高云/视角/选源机制共同作用。

## 图 S3：53 个样本的全球位置与样本级 MAE

这张图回答：Stage10 样本不是随机条形图，而是有明确 EPIC 可见盘面几何。每个圆是一个 Stage10 样本的 EPIC 地理中心；颜色是 case-level MAE，圆大小按 `n_valid_cth` 缩放，边框颜色表示 dominant source。横轴是东经，纵轴是北纬，方向未翻转。

## 讲述边界

- 不说 EPIC 是绝对真值；说 independent effective-height reference。
- 不说图 S1/S2 是月平均；它们是代表样本的像素级直观图。
- 不说 Composite 或近似 PSF 已经改变 Stage10 结论；本包只服务 Stage10 CTH 空间解释。
- 所有中心经度、投影坐标和采样 stride 都写入对应 source CSV，可追溯。
"""
    path = dirs["reports"] / "stage_10_group_meeting_spatial_earth_figure_guide_cn.md"
    path.write_text(text, encoding="utf-8-sig")
    return path


def write_manifest(dirs: dict[str, Path], outputs: list[dict[str, Any]], selected_samples: list[str], warnings: list[dict[str, Any]], guide_path: Path) -> Path:
    manifest = {
        "project_id": PROJECT_ID,
        "stage_id": STAGE_ID,
        "run_id": RUN_ID,
        "artifact_type": "group_meeting_spatial_earth_figures",
        "created_utc": utc_now(),
        "input_paths": {
            "sample_manifest": str(SAMPLE_MANIFEST),
            "case_inventory": str(CASE_INVENTORY),
            "stage10_root": str(STAGE10_ROOT),
        },
        "selected_samples": selected_samples,
        "epic_reference_variable": EPIC_CTH_VAR,
        "scientific_scope": "visualization only; reads existing local products; does not rerun Stage05/06",
        "figure_outputs": outputs,
        "guide_path": str(guide_path),
        "warnings": warnings,
    }
    path = dirs["logs"] / "stage_10_group_meeting_spatial_manifest.json"
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8-sig")
    return path


def write_qa(dirs: dict[str, Path], outputs: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> Path:
    checks = []
    for row in outputs:
        for key in ["svg_path", "pdf_path", "png_path", "tiff_path", "source_csv"]:
            path = Path(row[key])
            checks.append({"path": str(path), "exists": path.exists(), "size_bytes": path.stat().st_size if path.exists() else 0})
    report = {
        "created_utc": utc_now(),
        "status": "PASS" if all(c["exists"] and c["size_bytes"] > 0 for c in checks) and not any(w.get("level") == "ERROR" for w in warnings) else "WARN",
        "checks": checks,
        "warnings": warnings,
    }
    path = dirs["logs"] / "stage_10_group_meeting_spatial_qa_report.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8-sig")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Make Stage 10 spatial Earth-view figures for group meeting.")
    parser.add_argument("--plot-stride", type=int, default=10)
    parser.add_argument("--samples", nargs="*", default=SUGGESTED_SAMPLES)
    args = parser.parse_args()

    dirs = ensure_dirs()
    warnings: list[dict[str, Any]] = []
    manifest = pd.read_csv(SAMPLE_MANIFEST, encoding="utf-8-sig")
    case_df = pd.read_csv(CASE_INVENTORY, encoding="utf-8-sig")
    manifest = manifest[manifest["has_epic_file"].astype(str).str.lower().eq("true")].copy()
    selected_samples = select_samples(manifest, case_df, [str(s) for s in args.samples])
    if len(selected_samples) < 4:
        warnings.append({"level": "WARN", "message": f"Only {len(selected_samples)} samples available for product atlas."})

    sample_frames: dict[str, pd.DataFrame] = {}
    metric_rows: list[dict[str, Any]] = []
    manifest_index = manifest.set_index("sample_id")
    for sample_id in selected_samples:
        frame, metrics = prepare_sample(manifest_index.loc[sample_id], args.plot_stride)
        sample_frames[sample_id] = frame
        metric_rows.append(metrics)

    center_rows: list[dict[str, Any]] = []
    for _, row in manifest.iterrows():
        sample_id = str(row["sample_id"])
        try:
            epic = read_epic_cloud(Path(str(row["epic_file"])))
            center_rows.append(
                {
                    "sample_id": sample_id,
                    "center_longitude_deg": circular_mean_lon(epic["lon"], epic["geo_valid"]),
                    "center_latitude_deg": float(np.nanmean(epic["lat"][epic["geo_valid"]])) if np.any(epic["geo_valid"]) else math.nan,
                }
            )
        except Exception as exc:
            warnings.append({"level": "WARN", "sample_id": sample_id, "message": f"Cannot compute EPIC center: {exc}"})

    sample_metrics = pd.DataFrame(metric_rows)
    center_df = pd.DataFrame(center_rows)
    for _, row in sample_metrics.iterrows():
        center_df.loc[center_df["sample_id"] == row["sample_id"], ["center_longitude_deg", "center_latitude_deg"]] = [
            row["center_longitude_deg"],
            row["center_latitude_deg"],
        ]

    outputs: list[dict[str, Any]] = []
    make_product_atlas(sample_frames, sample_metrics, case_df, dirs, outputs)
    make_mechanism_disk(selected_samples[0], sample_frames[selected_samples[0]], sample_metrics.set_index("sample_id").loc[selected_samples[0]], dirs, outputs)
    make_case_locator(center_df, case_df, dirs, outputs)

    plot_index = dirs["logs"] / "stage_10_group_meeting_spatial_figure_index.csv"
    write_csv(plot_index, outputs)
    guide_path = write_guides(dirs, outputs, selected_samples)
    manifest_path = write_manifest(dirs, outputs, selected_samples, warnings, guide_path)
    qa_path = write_qa(dirs, outputs, warnings)
    print(json.dumps({"status": "ok", "figures": len(outputs), "plot_index": str(plot_index), "manifest": str(manifest_path), "qa": str(qa_path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
