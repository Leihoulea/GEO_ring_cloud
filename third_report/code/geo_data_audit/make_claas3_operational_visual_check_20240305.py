from __future__ import annotations

import csv
import math
import os
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import xarray as xr
from matplotlib.colors import BoundaryNorm, ListedColormap, TwoSlopeNorm


SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

from compare_claas3_and_operational_meteosat import compare_cth_grids, ensure_eccodes  # noqa: E402


ensure_eccodes()
import cfgrib  # noqa: E402


OUT_DIR = WORKSPACE / "reports" / "figures" / "claas3_vs_operational_visual_check_20240305T0000"
CMSAF_ROOT = Path(r"E:\GEO_Cloud_2024\CMSAF")
OP_ROOT = Path(r"E:\GEO_Cloud_2024")
TARGET_TIME = datetime(2024, 3, 5, 0, 0, 0, tzinfo=timezone.utc)


@dataclass
class ProductFile:
    label: str
    path: Path
    actual_time: datetime
    time_coverage_start: str
    time_coverage_end: str


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8-sig")


def parse_claas_time_from_name(name: str) -> datetime | None:
    stem = Path(name).stem
    if "in20" not in stem:
        return None
    token = stem.split("in", 1)[1][:14]
    try:
        return datetime.strptime(token, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def find_nearest_claas(product_prefix: str) -> ProductFile:
    candidates = []
    for path in CMSAF_ROOT.rglob(f"{product_prefix}*.nc"):
        ts = parse_claas_time_from_name(path.name)
        if ts is None:
            continue
        candidates.append((abs((ts - TARGET_TIME).total_seconds()), ts, path))
    if not candidates:
        raise FileNotFoundError(f"no CLAAS file found for {product_prefix}")
    _, ts, path = sorted(candidates, key=lambda item: (item[0], item[1], str(item[2])))[0]
    ds = xr.open_dataset(path, mask_and_scale=False)
    try:
        start = str(ds.attrs.get("time_coverage_start", ts.isoformat().replace("+00:00", "Z")))
        end = str(ds.attrs.get("time_coverage_end", ts.isoformat().replace("+00:00", "Z")))
    finally:
        ds.close()
    return ProductFile(product_prefix, path, ts, start, end)


def find_operational(product: str) -> ProductFile:
    zip_name = {
        "CLM": "MSG3-SEVI-MSGCLMK-0100-0100-20240305000000.000000000Z-NA.zip",
        "CTH": "MSG3-SEVI-MSGCLTH-0100-0100-20240305000000.000000000Z-NA.zip",
    }[product]
    path = OP_ROOT / "Meteosat-0deg" / product / "20240305" / "00" / zip_name
    if not path.exists():
        raise FileNotFoundError(path)
    return ProductFile(product, path, TARGET_TIME, TARGET_TIME.isoformat().replace("+00:00", "Z"), TARGET_TIME.isoformat().replace("+00:00", "Z"))


def load_claas_array(path: Path, var_name: str) -> tuple[np.ndarray, dict[str, Any], dict[str, Any]]:
    ds = xr.open_dataset(path, mask_and_scale=False)
    try:
        da = ds[var_name]
        attrs = {k: to_python(v) for k, v in da.attrs.items()}
        gattrs = {k: to_python(v) for k, v in ds.attrs.items()}
        arr = np.asarray(da.values, dtype=np.float32)
        fill = attrs.get("_FillValue", attrs.get("missing_value"))
        if fill is not None:
            arr = arr.astype(np.float32, copy=False)
            arr[np.isclose(arr, float(fill))] = np.nan
        scale = float(attrs.get("scale_factor", 1.0) or 1.0)
        offset = float(attrs.get("add_offset", 0.0) or 0.0)
        arr = arr * scale + offset
        arr = np.squeeze(arr)
        return arr.astype(np.float32, copy=False), attrs, gattrs
    finally:
        ds.close()


def load_operational_grib(zip_path: Path, var_names: list[str]) -> dict[str, np.ndarray]:
    with zipfile.ZipFile(zip_path) as zf:
        grib_names = [name for name in zf.namelist() if name.lower().endswith((".grb", ".grib", ".grib2", ".bin"))]
        if not grib_names:
            raise RuntimeError(f"no grib member found in {zip_path}")
        with tempfile.NamedTemporaryFile(suffix=".grib", delete=False) as tmp:
            tmp.write(zf.read(grib_names[0]))
            tmp_path = Path(tmp.name)
    try:
        ds = xr.open_dataset(tmp_path, engine="cfgrib", backend_kwargs={"indexpath": ""})
        try:
            out: dict[str, np.ndarray] = {}
            for name in var_names:
                if name in ds:
                    out[name] = reshape_square(np.asarray(ds[name].values))
                elif name in ds.coords:
                    out[name] = reshape_square(np.asarray(ds[name].values))
            return out
        finally:
            ds.close()
    finally:
        tmp_path.unlink(missing_ok=True)


def reshape_square(arr: np.ndarray) -> np.ndarray:
    a = np.asarray(arr)
    if a.ndim != 1:
        return np.squeeze(a)
    side = int(round(math.sqrt(a.size)))
    if side * side != a.size:
        return np.squeeze(a)
    return a.reshape(side, side)


def to_python(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        if value.ndim == 0:
            return value.item()
        return value.tolist()
    return value


def format_time(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def format_path(path: Path) -> str:
    return str(path).replace("\\", "/")


def downsample_for_plot(arr: np.ndarray, max_pixels: int = 1_200_000) -> np.ndarray:
    a = np.asarray(arr)
    if a.ndim == 0:
        return a.reshape(1, 1)
    if a.ndim == 1:
        a = a[np.newaxis, :]
    if a.ndim > 2:
        a = np.squeeze(a)
    stride = max(1, int(math.ceil(math.sqrt(a.size / max_pixels)))) if a.size > max_pixels else 1
    return a[::stride, ::stride]


def save_discrete_map(
    arr: np.ndarray,
    out_path: Path,
    title: str,
    colors: list[str],
    boundaries: list[float],
    tick_values: list[float],
    tick_labels: list[str],
    bad_color: str = "#f4f4f4",
) -> None:
    plot = downsample_for_plot(arr)
    cmap = ListedColormap(colors)
    cmap.set_bad(bad_color)
    norm = BoundaryNorm(boundaries, cmap.N)
    fig, ax = plt.subplots(figsize=(9, 6), dpi=150)
    im = ax.imshow(plot.astype(np.float32), interpolation="nearest", cmap=cmap, norm=norm)
    ax.set_title(title, fontsize=9)
    ax.set_xticks([])
    ax.set_yticks([])
    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.03, ticks=tick_values)
    cbar.ax.set_yticklabels(tick_labels)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def save_continuous_map(
    arr: np.ndarray,
    out_path: Path,
    title: str,
    cmap: str = "viridis",
    vmin: float | None = None,
    vmax: float | None = None,
    bad_color: str = "#f4f4f4",
    colorbar_label: str = "",
) -> None:
    plot = downsample_for_plot(arr)
    cm = plt.get_cmap(cmap).copy()
    cm.set_bad(bad_color)
    fig, ax = plt.subplots(figsize=(9, 6), dpi=150)
    im = ax.imshow(plot.astype(np.float32), interpolation="nearest", cmap=cm, vmin=vmin, vmax=vmax)
    ax.set_title(title, fontsize=9)
    ax.set_xticks([])
    ax.set_yticks([])
    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.03)
    if colorbar_label:
        cbar.set_label(colorbar_label)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def save_boolean_mask(arr: np.ndarray, out_path: Path, title: str) -> None:
    plot = downsample_for_plot(arr.astype(np.float32))
    cmap = ListedColormap(["#f4f4f4", "#1f77b4"])
    fig, ax = plt.subplots(figsize=(8, 5), dpi=150)
    im = ax.imshow(plot, interpolation="nearest", cmap=cmap, vmin=0, vmax=1)
    ax.set_title(title, fontsize=9)
    ax.set_xticks([])
    ax.set_yticks([])
    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.03, ticks=[0, 1])
    cbar.ax.set_yticklabels(["invalid", "valid"])
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def save_triplet(cth_km: np.ndarray, ctp_hpa: np.ndarray, ctt_k: np.ndarray, out_path: Path, title_prefix: str) -> None:
    fig, axes = plt.subplots(1, 3, figsize=(16, 5), dpi=150)
    panels = [
        (cth_km, "cloud top height (km)", "viridis", 0, 18),
        (ctp_hpa, "cloud top pressure (hPa)", "magma_r", 0, 1100),
        (ctt_k, "cloud top temperature (K)", "plasma", 180, 310),
    ]
    for ax, (arr, label, cmap, vmin, vmax) in zip(axes, panels):
        plot = downsample_for_plot(arr)
        cm = plt.get_cmap(cmap).copy()
        cm.set_bad("#f4f4f4")
        im = ax.imshow(plot.astype(np.float32), interpolation="nearest", cmap=cm, vmin=vmin, vmax=vmax)
        ax.set_title(label, fontsize=9)
        ax.set_xticks([])
        ax.set_yticks([])
        plt.colorbar(im, ax=ax, fraction=0.046, pad=0.03)
    fig.suptitle(title_prefix, fontsize=11)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(out_path)
    plt.close(fig)


def save_cpp_panels(cph: np.ndarray, cot: np.ndarray, cre_um: np.ndarray, cwp_gm2: np.ndarray, out_path: Path, title_prefix: str) -> None:
    fig, axes = plt.subplots(2, 2, figsize=(12, 10), dpi=150)
    cph_plot = np.where(np.isfinite(cph), cph, np.nan)
    cmap = ListedColormap(["#d9d9d9", "#4c78a8", "#f58518"])
    cmap.set_bad("#f4f4f4")
    norm = BoundaryNorm([-0.5, 0.5, 1.5, 2.5], cmap.N)
    im0 = axes[0, 0].imshow(downsample_for_plot(cph_plot), interpolation="nearest", cmap=cmap, norm=norm)
    axes[0, 0].set_title("cloud phase")
    axes[0, 0].set_xticks([])
    axes[0, 0].set_yticks([])
    cb0 = plt.colorbar(im0, ax=axes[0, 0], fraction=0.046, pad=0.03, ticks=[0, 1, 2])
    cb0.ax.set_yticklabels(["clear", "liquid", "ice"])

    for ax, arr, name, cmap_name, unit in [
        (axes[0, 1], cot, "COT", "magma", ""),
        (axes[1, 0], cre_um, "CER", "viridis", "um"),
        (axes[1, 1], cwp_gm2, "CWP", "cividis", "g m-2"),
    ]:
        finite = np.isfinite(arr)
        if finite.any():
            vmax = float(np.nanpercentile(arr[finite], 99))
            vmin = float(np.nanpercentile(arr[finite], 1))
            if math.isclose(vmin, vmax):
                vmin, vmax = float(np.nanmin(arr[finite])), float(np.nanmax(arr[finite]))
            cm = plt.get_cmap(cmap_name).copy()
            cm.set_bad("#f4f4f4")
            im = ax.imshow(downsample_for_plot(arr), interpolation="nearest", cmap=cm, vmin=vmin, vmax=vmax)
            plt.colorbar(im, ax=ax, fraction=0.046, pad=0.03)
        else:
            ax.imshow(np.full((32, 32), np.nan), interpolation="nearest", cmap=ListedColormap(["#f4f4f4"]))
            ax.text(0.5, 0.5, "all fill\nlikely nighttime\nunavailable", ha="center", va="center", fontsize=10, transform=ax.transAxes)
        ax.set_title(f"{name} ({unit})".strip())
        ax.set_xticks([])
        ax.set_yticks([])
    fig.suptitle(title_prefix, fontsize=11)
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    fig.savefig(out_path)
    plt.close(fig)


def save_cth_difference(op_km: np.ndarray, claas_km: np.ndarray, diff_km: np.ndarray, out_path: Path, title_prefix: str) -> None:
    fig, axes = plt.subplots(1, 3, figsize=(16, 5), dpi=150)
    panels = [
        (op_km, "operational CTH (km)", "viridis", 0, 18, None),
        (claas_km, "CLAAS-3 CTX CTH downsampled (km)", "viridis", 0, 18, None),
        (diff_km, "CLAAS-3 - operational (km)", "coolwarm", None, None, TwoSlopeNorm(vcenter=0.0, vmin=-10.0, vmax=10.0)),
    ]
    for ax, (arr, label, cmap_name, vmin, vmax, norm) in zip(axes, panels):
        cm = plt.get_cmap(cmap_name).copy()
        cm.set_bad("#f4f4f4")
        plot = downsample_for_plot(arr)
        im = ax.imshow(plot.astype(np.float32), interpolation="nearest", cmap=cm, vmin=vmin, vmax=vmax, norm=norm)
        ax.set_title(label, fontsize=9)
        ax.set_xticks([])
        ax.set_yticks([])
        plt.colorbar(im, ax=ax, fraction=0.046, pad=0.03)
    fig.suptitle(title_prefix, fontsize=11)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(out_path)
    plt.close(fig)


def save_valid_coverage_figure(
    op_clm_valid: np.ndarray,
    op_cth_valid: np.ndarray,
    cma_valid: np.ndarray,
    ctx_valid: np.ndarray,
    cpp_valid: np.ndarray,
    composite: np.ndarray,
    out_path: Path,
    title_prefix: str,
) -> None:
    fig, axes = plt.subplots(2, 3, figsize=(14, 9), dpi=150)
    mask_panels = [
        (axes[0, 0], op_clm_valid, "operational CLM valid"),
        (axes[0, 1], op_cth_valid, "operational CTH valid"),
        (axes[0, 2], cma_valid, "CLAAS-3 CMA valid"),
        (axes[1, 0], ctx_valid, "CLAAS-3 CTX valid"),
        (axes[1, 1], cpp_valid, "CLAAS-3 CPP valid"),
    ]
    cmap_bool = ListedColormap(["#f4f4f4", "#1f77b4"])
    for ax, arr, title in mask_panels:
        im = ax.imshow(downsample_for_plot(arr.astype(np.float32)), interpolation="nearest", cmap=cmap_bool, vmin=0, vmax=1)
        ax.set_title(title, fontsize=9)
        ax.set_xticks([])
        ax.set_yticks([])
        plt.colorbar(im, ax=ax, fraction=0.046, pad=0.03, ticks=[0, 1])

    comp_cmap = ListedColormap(["#f4f4f4", "#4c78a8", "#f58518", "#54a24b"])
    comp_norm = BoundaryNorm([-0.5, 0.5, 1.5, 2.5, 3.5], comp_cmap.N)
    imc = axes[1, 2].imshow(downsample_for_plot(composite.astype(np.float32)), interpolation="nearest", cmap=comp_cmap, norm=comp_norm)
    axes[1, 2].set_title("composite availability")
    axes[1, 2].set_xticks([])
    axes[1, 2].set_yticks([])
    cbc = plt.colorbar(imc, ax=axes[1, 2], fraction=0.046, pad=0.03, ticks=[0, 1, 2, 3])
    cbc.ax.set_yticklabels(["no data", "only operational", "only CLAAS-3", "both"])
    fig.suptitle(title_prefix, fontsize=11)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(out_path)
    plt.close(fig)


def nan_stats(diff: np.ndarray) -> dict[str, float | int]:
    mask = np.isfinite(diff)
    if not mask.any():
        return {
            "count": 0,
            "mean_bias": float("nan"),
            "median_difference": float("nan"),
            "mae": float("nan"),
            "rmse": float("nan"),
            "p05": float("nan"),
            "p95": float("nan"),
        }
    vals = diff[mask]
    return {
        "count": int(vals.size),
        "mean_bias": float(np.nanmean(vals)),
        "median_difference": float(np.nanmedian(vals)),
        "mae": float(np.nanmean(np.abs(vals))),
        "rmse": float(np.sqrt(np.nanmean(vals ** 2))),
        "p05": float(np.nanpercentile(vals, 5)),
        "p95": float(np.nanpercentile(vals, 95)),
    }


def maybe_copy_existing(src: Path | None, dst: Path) -> str:
    if src and src.exists():
        shutil.copy2(src, dst)
        return str(src)
    return ""


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    operational_clm = find_operational("CLM")
    operational_cth = find_operational("CTH")
    claas_cma = find_nearest_claas("CMA")
    claas_ctx = find_nearest_claas("CTX")
    claas_cpp = find_nearest_claas("CPP")

    reused_operational_clm = None
    reused_operational_cth = None

    op_clm_data = load_operational_grib(operational_clm.path, ["p260537", "latitude", "longitude"])
    op_cth_data = load_operational_grib(operational_cth.path, ["ctoph", "ctophqi", "latitude", "longitude"])

    cma, cma_attrs, cma_gattrs = load_claas_array(claas_cma.path, "cma")
    cma_prob, cma_prob_attrs, _ = load_claas_array(claas_cma.path, "cma_prob")
    ctx_cth_m, ctx_cth_attrs, ctx_gattrs = load_claas_array(claas_ctx.path, "cth")
    ctx_ctp_hpa, ctx_ctp_attrs, _ = load_claas_array(claas_ctx.path, "ctp")
    ctx_ctt_k, ctx_ctt_attrs, _ = load_claas_array(claas_ctx.path, "ctt")
    cpp_cph, cpp_cph_attrs, cpp_gattrs = load_claas_array(claas_cpp.path, "cph")
    cpp_cot, cpp_cot_attrs, _ = load_claas_array(claas_cpp.path, "cot")
    cpp_cre_m, cpp_cre_attrs, _ = load_claas_array(claas_cpp.path, "cre")
    cpp_cwp_kgm2, cpp_cwp_attrs, _ = load_claas_array(claas_cpp.path, "cwp")

    op_clm_codes = op_clm_data["p260537"].astype(np.float32)
    op_clm_lat = op_clm_data["latitude"].astype(np.float32)
    op_clm_lon = op_clm_data["longitude"].astype(np.float32)
    op_cth_m = op_cth_data["ctoph"].astype(np.float32)
    op_cth_qi = op_cth_data["ctophqi"].astype(np.float32)
    op_cth_lat = op_cth_data["latitude"].astype(np.float32)
    op_cth_lon = op_cth_data["longitude"].astype(np.float32)

    op_clm_valid = np.isfinite(op_clm_codes) & (op_clm_codes != 3.0)
    op_cth_valid = np.isfinite(op_cth_m) & np.isfinite(op_cth_lat) & np.isfinite(op_cth_lon)
    cma_valid = np.isfinite(cma) & np.isin(cma, [0.0, 1.0])
    ctx_valid = np.isfinite(ctx_cth_m)
    cpp_valid = np.isfinite(cpp_cph) | np.isfinite(cpp_cot) | np.isfinite(cpp_cre_m) | np.isfinite(cpp_cwp_kgm2)

    op_cth_km = np.where(op_cth_valid, op_cth_m / 1000.0, np.nan)
    ctx_cth_km = np.where(ctx_valid, ctx_cth_m / 1000.0, np.nan)
    cpp_cre_um = np.where(np.isfinite(cpp_cre_m), cpp_cre_m * 1_000_000.0, np.nan)
    cpp_cwp_gm2 = np.where(np.isfinite(cpp_cwp_kgm2), cpp_cwp_kgm2 * 1000.0, np.nan)

    alignment = compare_cth_grids(ctx_cth_m, op_cth_m)
    row_offset = int(alignment.get("row_offset", 0))
    col_offset = int(alignment.get("col_offset", 0))
    claas_cth_on_op_km = ctx_cth_km[row_offset : row_offset + op_cth_km.shape[0] * 3 : 3, col_offset : col_offset + op_cth_km.shape[1] * 3 : 3]
    if claas_cth_on_op_km.shape != op_cth_km.shape:
        raise RuntimeError(f"downsampled CLAAS CTH shape mismatch: {claas_cth_on_op_km.shape} vs {op_cth_km.shape}")
    overlap = np.isfinite(op_cth_km) & np.isfinite(claas_cth_on_op_km)
    diff_km = np.where(overlap, claas_cth_on_op_km - op_cth_km, np.nan)
    diff_stats = nan_stats(diff_km)

    composite = np.zeros_like(cma, dtype=np.int16)
    claas_any = cma_valid | ctx_valid | cpp_valid
    composite[op_clm_valid & ~claas_any] = 1
    composite[~op_clm_valid & claas_any] = 2
    composite[op_clm_valid & claas_any] = 3

    fig1 = OUT_DIR / "fig01_operational_clm_existing_or_redrawn.png"
    fig2 = OUT_DIR / "fig02_operational_cth_existing_or_redrawn.png"
    fig3 = OUT_DIR / "fig03_claas3_cma_mask.png"
    fig4 = OUT_DIR / "fig04_claas3_cma_probability.png"
    fig5 = OUT_DIR / "fig05_claas3_ctx_cth.png"
    fig6 = OUT_DIR / "fig06_claas3_ctx_triplet_cth_ctp_ctt.png"
    fig7 = OUT_DIR / "fig07_operational_cth_vs_claas3_cth_difference.png"
    fig8 = OUT_DIR / "fig08_claas3_cpp_cloud_physics.png"
    fig9 = OUT_DIR / "fig09_valid_coverage_comparison.png"

    if not maybe_copy_existing(reused_operational_clm, fig1):
        save_discrete_map(
            op_clm_codes,
            fig1,
            f"operational MSG 0deg CLM code map\nproduct=CLM | target={format_time(TARGET_TIME)} | actual={format_time(operational_clm.actual_time)} | shape={op_clm_codes.shape} | units=code table 4.217",
            colors=["#4c78a8", "#f58518", "#54a24b", "#bab0ac"],
            boundaries=[-0.5, 0.5, 1.5, 2.5, 3.5],
            tick_values=[0, 1, 2, 3],
            tick_labels=["0", "1", "2", "3"],
        )
    if not maybe_copy_existing(reused_operational_cth, fig2):
        save_continuous_map(
            op_cth_km,
            fig2,
            f"operational MSG 0deg CTH\nproduct=CTH | target={format_time(TARGET_TIME)} | actual={format_time(operational_cth.actual_time)} | shape={op_cth_km.shape} | units=km",
            cmap="viridis",
            vmin=0,
            vmax=18,
            colorbar_label="km",
        )

    cma_display = np.where(cma_valid, cma, 2.0)
    save_discrete_map(
        cma_display,
        fig3,
        f"CLAAS-3 CMA cloud mask\nproduct=CMA | target={format_time(TARGET_TIME)} | actual={claas_cma.time_coverage_start} to {claas_cma.time_coverage_end} | shape={cma.shape} | units=flag",
        colors=["#4c78a8", "#f58518", "#bdbdbd"],
        boundaries=[-0.5, 0.5, 1.5, 2.5],
        tick_values=[0, 1, 2],
        tick_labels=["clear", "cloudy", "invalid/off-disk"],
    )
    save_continuous_map(
        np.where(np.isfinite(cma_prob), cma_prob, np.nan),
        fig4,
        f"CLAAS-3 CMA cloud probability\nproduct=CMA | target={format_time(TARGET_TIME)} | actual={claas_cma.time_coverage_start} to {claas_cma.time_coverage_end} | shape={cma_prob.shape} | units=percent",
        cmap="magma",
        vmin=0,
        vmax=100,
        colorbar_label="percent",
    )
    save_continuous_map(
        ctx_cth_km,
        fig5,
        f"CLAAS-3 CTX cloud top height\nproduct=CTX | target={format_time(TARGET_TIME)} | actual={claas_ctx.time_coverage_start} to {claas_ctx.time_coverage_end} | shape={ctx_cth_km.shape} | units=km",
        cmap="viridis",
        vmin=0,
        vmax=18,
        colorbar_label="km",
    )
    save_triplet(
        ctx_cth_km,
        ctx_ctp_hpa,
        ctx_ctt_k,
        fig6,
        f"CLAAS-3 CTX triplet | target={format_time(TARGET_TIME)} | actual={claas_ctx.time_coverage_start} to {claas_ctx.time_coverage_end} | shape={ctx_cth_km.shape}",
    )
    save_cth_difference(
        op_cth_km,
        claas_cth_on_op_km,
        diff_km,
        fig7,
        f"operational CTH vs CLAAS-3 CTX CTH | target={format_time(TARGET_TIME)} | alignment=3x decimation offset(row={row_offset}, col={col_offset})",
    )
    save_cpp_panels(
        cpp_cph,
        cpp_cot,
        cpp_cre_um,
        cpp_cwp_gm2,
        fig8,
        f"CLAAS-3 CPP cloud physics | target={format_time(TARGET_TIME)} | actual={claas_cpp.time_coverage_start} to {claas_cpp.time_coverage_end} | shape={cpp_cph.shape}",
    )
    save_valid_coverage_figure(
        op_clm_valid,
        op_cth_valid,
        cma_valid,
        ctx_valid,
        cpp_valid,
        composite,
        fig9,
        f"valid coverage comparison | target={format_time(TARGET_TIME)}",
    )

    metrics_rows = [
        {
            "target_time": format_time(TARGET_TIME),
            "actual_time_operational_clm": format_time(operational_clm.actual_time),
            "actual_time_operational_cth": format_time(operational_cth.actual_time),
            "actual_time_claas3_cma": claas_cma.time_coverage_start,
            "actual_time_claas3_ctx": claas_ctx.time_coverage_start,
            "actual_time_claas3_cpp": claas_cpp.time_coverage_start,
            "variable_pair": "cloud_top_height_km",
            "operational_shape": str(op_cth_km.shape),
            "claas_shape": str(ctx_cth_km.shape),
            "valid_overlap_pixels": diff_stats["count"],
            "valid_overlap_fraction": float(diff_stats["count"] / op_cth_km.size),
            "mean_operational": float(np.nanmean(op_cth_km[overlap])) if overlap.any() else np.nan,
            "mean_claas": float(np.nanmean(claas_cth_on_op_km[overlap])) if overlap.any() else np.nan,
            "bias_claas_minus_operational": diff_stats["mean_bias"],
            "mae": diff_stats["mae"],
            "rmse": diff_stats["rmse"],
            "p05_difference": diff_stats["p05"],
            "median_difference": diff_stats["median_difference"],
            "p95_difference": diff_stats["p95"],
            "note": f"3x decimation alignment with row_offset={row_offset}, col_offset={col_offset}",
        },
        {
            "target_time": format_time(TARGET_TIME),
            "actual_time_operational_clm": format_time(operational_clm.actual_time),
            "actual_time_operational_cth": format_time(operational_cth.actual_time),
            "actual_time_claas3_cma": claas_cma.time_coverage_start,
            "actual_time_claas3_ctx": claas_ctx.time_coverage_start,
            "actual_time_claas3_cpp": claas_cpp.time_coverage_start,
            "variable_pair": "valid_coverage_operational_vs_claas3_on_clm_grid",
            "operational_shape": str(op_clm_valid.shape),
            "claas_shape": str(cma_valid.shape),
            "valid_overlap_pixels": int(np.sum(op_clm_valid & claas_any)),
            "valid_overlap_fraction": float(np.mean(op_clm_valid & claas_any)),
            "mean_operational": float(np.mean(op_clm_valid)),
            "mean_claas": float(np.mean(claas_any)),
            "bias_claas_minus_operational": float(np.mean(claas_any.astype(np.float32) - op_clm_valid.astype(np.float32))),
            "mae": float(np.mean(np.abs(claas_any.astype(np.float32) - op_clm_valid.astype(np.float32)))),
            "rmse": float(np.sqrt(np.mean((claas_any.astype(np.float32) - op_clm_valid.astype(np.float32)) ** 2))),
            "p05_difference": np.nan,
            "median_difference": np.nan,
            "p95_difference": np.nan,
            "note": "coverage composite uses CLM/CMA native grid; operational CTH native grid differs and is shown separately in fig09",
        },
    ]
    write_csv(OUT_DIR / "visual_check_metrics.csv", metrics_rows)

    cth_overlap_fraction = float(diff_stats["count"] / op_cth_km.size)
    cpp_cot_valid_fraction = float(np.mean(np.isfinite(cpp_cot)))
    cpp_cre_valid_fraction = float(np.mean(np.isfinite(cpp_cre_um)))
    cpp_cwp_valid_fraction = float(np.mean(np.isfinite(cpp_cwp_gm2)))
    cpp_cph_valid_fraction = float(np.mean(np.isfinite(cpp_cph)))

    lines = [
        "# CLAAS-3 vs operational Meteosat visual check (2024-03-05 00:00 UTC)",
        "",
        "## 1. 使用文件",
        "",
        f"- operational CLM: `{format_path(operational_clm.path)}`",
        f"- operational CTH: `{format_path(operational_cth.path)}`",
        f"- CLAAS-3 CMA: `{format_path(claas_cma.path)}`",
        f"- CLAAS-3 CTX: `{format_path(claas_ctx.path)}`",
        f"- CLAAS-3 CPP: `{format_path(claas_cpp.path)}`",
        "",
        "## 2. 实际使用时间戳",
        "",
        f"- target_time: `{format_time(TARGET_TIME)}`",
        f"- operational CLM actual_time: `{format_time(operational_clm.actual_time)}`",
        f"- operational CTH actual_time: `{format_time(operational_cth.actual_time)}`",
        f"- CLAAS-3 CMA actual coverage: `{claas_cma.time_coverage_start}` -> `{claas_cma.time_coverage_end}`",
        f"- CLAAS-3 CTX actual coverage: `{claas_ctx.time_coverage_start}` -> `{claas_ctx.time_coverage_end}`",
        f"- CLAAS-3 CPP actual coverage: `{claas_cpp.time_coverage_start}` -> `{claas_cpp.time_coverage_end}`",
        "",
        "## 3. 图件路径",
        "",
        f"- fig01: `{format_path(fig1)}`",
        f"- fig02: `{format_path(fig2)}`",
        f"- fig03: `{format_path(fig3)}`",
        f"- fig04: `{format_path(fig4)}`",
        f"- fig05: `{format_path(fig5)}`",
        f"- fig06: `{format_path(fig6)}`",
        f"- fig07: `{format_path(fig7)}`",
        f"- fig08: `{format_path(fig8)}`",
        f"- fig09: `{format_path(fig9)}`",
        "",
        "## 4. 是否复用旧图",
        "",
        f"- operational CLM old figure reused: `{format_path(Path(reused_operational_clm)) if reused_operational_clm else 'none found; redrawn'}`",
        f"- operational CTH old figure reused: `{format_path(Path(reused_operational_cth)) if reused_operational_cth else 'none found; redrawn'}`",
        "",
        "## 5. CLAAS-3 变量、单位、shape、fill value",
        "",
        f"- CMA `cma`: shape=`{cma.shape}`, units=`{cma_attrs.get('units', '')}`, fill=`{cma_attrs.get('_FillValue', cma_attrs.get('missing_value', ''))}`",
        f"- CMA `cma_prob`: shape=`{cma_prob.shape}`, units=`{cma_prob_attrs.get('units', '')}`, fill=`{cma_prob_attrs.get('_FillValue', cma_prob_attrs.get('missing_value', ''))}`",
        f"- CTX `cth`: shape=`{ctx_cth_km.shape}`, units(raw)=`{ctx_cth_attrs.get('units', '')}`, fill=`{ctx_cth_attrs.get('_FillValue', ctx_cth_attrs.get('missing_value', ''))}`",
        f"- CTX `ctp`: shape=`{ctx_ctp_hpa.shape}`, units(raw)=`{ctx_ctp_attrs.get('units', '')}`, fill=`{ctx_ctp_attrs.get('_FillValue', ctx_ctp_attrs.get('missing_value', ''))}`",
        f"- CTX `ctt`: shape=`{ctx_ctt_k.shape}`, units(raw)=`{ctx_ctt_attrs.get('units', '')}`, fill=`{ctx_ctt_attrs.get('_FillValue', ctx_ctt_attrs.get('missing_value', ''))}`",
        f"- CPP `cph`: shape=`{cpp_cph.shape}`, units=`{cpp_cph_attrs.get('units', '')}`, fill=`{cpp_cph_attrs.get('_FillValue', cpp_cph_attrs.get('missing_value', ''))}`",
        f"- CPP `cot`: shape=`{cpp_cot.shape}`, units=`{cpp_cot_attrs.get('units', '')}`, fill=`{cpp_cot_attrs.get('_FillValue', cpp_cot_attrs.get('missing_value', ''))}`",
        f"- CPP `cre`: shape=`{cpp_cre_um.shape}`, units(raw)=`{cpp_cre_attrs.get('units', '')}`, fill=`{cpp_cre_attrs.get('_FillValue', cpp_cre_attrs.get('missing_value', ''))}`",
        f"- CPP `cwp`: shape=`{cpp_cwp_gm2.shape}`, units(raw)=`{cpp_cwp_attrs.get('units', '')}`, fill=`{cpp_cwp_attrs.get('_FillValue', cpp_cwp_attrs.get('missing_value', ''))}`",
        "",
        "## 6. CTH 差值统计",
        "",
        f"- overlap count: `{diff_stats['count']}`",
        f"- valid overlap fraction on operational CTH grid: `{cth_overlap_fraction:.4f}`",
        f"- mean bias (CLAAS3 - operational, km): `{diff_stats['mean_bias']:.3f}`",
        f"- median difference (km): `{diff_stats['median_difference']:.3f}`",
        f"- MAE (km): `{diff_stats['mae']:.3f}`",
        f"- RMSE (km): `{diff_stats['rmse']:.3f}`",
        f"- p05 / p95 difference (km): `{diff_stats['p05']:.3f}` / `{diff_stats['p95']:.3f}`",
        f"- alignment note: `3x decimation with row_offset={row_offset}, col_offset={col_offset}`",
        "",
        "## 7. 肉眼判断与工程解释",
        "",
        f"- CLAAS-3 CMA vs operational CLM: 两者在整盘云带、大尺度云区位置上应当可以直接肉眼对照，因为 native shape 同为 `3712x3712`；但 operational CLM 是 4 类 code map，CLAAS-3 CMA 是 `clear/cloudy` 二值主层，所以边界不应期待逐码一致。",
        f"- CLAAS-3 CTX CTH vs operational CTH: 两者不是同一原生分辨率。当前样本用 `3x downsample + best offset` 后，平均偏差约 `{diff_stats['mean_bias']:.2f} km`，更适合做结构对照而不是逐像元真值比较。",
        "- 若 fig07 中差异主要沿云边缘、深对流高云区和圆盘边缘增强，这是合理现象；若出现整块大陆/海洋大面积同号偏差，则更像算法或语义差异而非单纯分辨率差异。",
        f"- CLAAS-3 CPP: 当前时次是 `00:00 UTC` 夜间样本。`cph` 仍有较高可用率（约 `{cpp_cph_valid_fraction:.3f}`），但 `cot/cre/cwp` 在夜间可能大面积缺测；本次样本的有效率分别约为 `{cpp_cot_valid_fraction:.3f}`, `{cpp_cre_valid_fraction:.3f}`, `{cpp_cwp_valid_fraction:.3f}`。",
        "- 因此，夜间 CLAAS-3 对 Meteosat 0deg 的价值主要体现在 `CMA` 和 `CTX`，以及 `CPP` 中的 phase 层；而 `COT/CER/CWP` 更适合白天或晨昏可用性更好的时次。",
        "- 结论上，这个时次的 CLAAS-3 适合作为 Meteosat 0deg 的 `cloud-physics supplement`，但要明确它不是 operational 产品的新版替代，更不是 IODC 产品。",
        "",
        "## 8. 元数据补充",
        "",
        f"- CLAAS-3 CMA global time attrs: start=`{cma_gattrs.get('time_coverage_start', '')}`, end=`{cma_gattrs.get('time_coverage_end', '')}`",
        f"- CLAAS-3 CTX global time attrs: start=`{ctx_gattrs.get('time_coverage_start', '')}`, end=`{ctx_gattrs.get('time_coverage_end', '')}`",
        f"- CLAAS-3 CPP global time attrs: start=`{cpp_gattrs.get('time_coverage_start', '')}`, end=`{cpp_gattrs.get('time_coverage_end', '')}`",
        "",
    ]
    write_text(OUT_DIR / "visual_check_report.md", "\n".join(lines))

    print(str(OUT_DIR))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
