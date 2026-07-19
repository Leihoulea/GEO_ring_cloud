from __future__ import annotations

import csv
import json
import math
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import h5py
import numpy as np
from matplotlib.colors import BoundaryNorm, ListedColormap
from PIL import Image

from geo_ring_cloud.paths import STAGE_ROOT, env_path


def parse_target_time() -> datetime:
    text = os.environ.get("GEO_RING_TARGET_TIME", "2024-03-05T00:00:00Z").replace("Z", "+00:00")
    return datetime.fromisoformat(text).astimezone(timezone.utc)


TARGET_TIME = parse_target_time()
TARGET_DATE = TARGET_TIME.strftime("%Y-%m-%d")
TIME_TAG = os.environ.get("GEO_RING_TIME_TAG", TARGET_TIME.strftime("%Y%m%d_%H%M"))

FUSED_DIR = STAGE_ROOT / "fused_best_source"
GRID_JSON = STAGE_ROOT / "reprojected_grid" / "target_grid_definition.json"
OUT_DIR = env_path("EPIC_VISUAL_OUT_DIR", STAGE_ROOT / f"epic_visual_comparison_{TIME_TAG}")
IMG_DIR = OUT_DIR / "epic_images"
QL_DIR = OUT_DIR / "quicklooks_epic_compare"
REPORT_DIR = STAGE_ROOT / "reports"
EPIC_L1_H5 = os.environ.get("EPIC_L1_H5", "").strip()

EPIC_API = "https://epic.gsfc.nasa.gov/api/{product}/date/{date}"
EPIC_ARCHIVE = "https://epic.gsfc.nasa.gov/archive/{product}/{yyyy}/{mm}/{dd}/png/{image}.png"
PRODUCTS = ("natural", "enhanced", "cloud")


@dataclass
class EpicRecord:
    product: str
    image: str
    caption: str
    date: datetime
    delta_minutes: float
    centroid_lat: float | None
    centroid_lon: float | None
    api_url: str
    image_url: str
    local_path: Path | None
    status: str
    notes: str


def ensure_dirs() -> None:
    for path in (OUT_DIR, IMG_DIR, QL_DIR, REPORT_DIR):
        path.mkdir(parents=True, exist_ok=True)


def robust_scale(arr: np.ndarray, mask: np.ndarray | None = None) -> np.ndarray:
    data = arr.astype(np.float32, copy=True)
    data[~np.isfinite(data)] = np.nan
    if mask is not None:
        data[~mask] = np.nan
    vals = data[np.isfinite(data)]
    if vals.size == 0:
        return np.zeros(data.shape, dtype=np.float32)
    lo, hi = np.nanpercentile(vals, [1, 99.5])
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        lo, hi = float(np.nanmin(vals)), float(np.nanmax(vals))
    out = np.clip((data - lo) / max(hi - lo, 1e-6), 0, 1)
    out[~np.isfinite(out)] = 0
    return out.astype(np.float32)


def read_scalar_attr(obj: Any, key: str) -> float | None:
    if key not in obj.attrs:
        return None
    value = obj.attrs[key]
    try:
        if isinstance(value, np.ndarray):
            value = value.item() if value.size == 1 else value.flat[0]
        return float(value)
    except Exception:
        return None


def parse_time_from_epic_h5_path(path: Path) -> datetime:
    match = re.search(r"(20\d{12})", path.name)
    if match:
        return datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    return TARGET_TIME


def make_epic_l1_rgb_record(h5_path: Path) -> EpicRecord:
    out_png = IMG_DIR / f"epic_l1_rgb_{h5_path.stem}.png"
    with h5py.File(h5_path, "r") as f:
        red = f["Band680nm/Image"][()]
        green = f["Band551nm/Image"][()]
        blue = f["Band443nm/Image"][()]
        mask = None
        if "Band443nm/Geolocation/Earth/Mask" in f:
            mask = f["Band443nm/Geolocation/Earth/Mask"][()] > 0
        rgb = np.dstack([
            robust_scale(red, mask),
            robust_scale(green, mask),
            robust_scale(blue, mask),
        ])
        if mask is not None:
            rgb[~mask] = 0
        Image.fromarray((np.clip(rgb, 0, 1) * 255).astype(np.uint8)).save(out_png)
        img_obj = f["Band680nm/Image"]
        lon = read_scalar_attr(img_obj, "centroid_center_longitude")
        lat = read_scalar_attr(img_obj, "centroid_center_latitude")
        if lon is None or lat is None:
            earth = f["Band680nm/Geolocation/Earth"]
            lon = lon if lon is not None else read_scalar_attr(earth, "centroid_center_longitude")
            lat = lat if lat is not None else read_scalar_attr(earth, "centroid_center_latitude")
    item_time = parse_time_from_epic_h5_path(h5_path)
    delta = abs((item_time - TARGET_TIME).total_seconds()) / 60.0
    return EpicRecord(
        product="natural",
        image=h5_path.stem,
        caption="local EPIC L1 RGB from Band680/Band551/Band443",
        date=item_time,
        delta_minutes=delta,
        centroid_lat=lat,
        centroid_lon=lon,
        api_url="",
        image_url="",
        local_path=out_png,
        status="LOCAL_EPIC_L1_H5",
        notes=f"source_h5={h5_path}; RGB=Band680nm/Band551nm/Band443nm",
    )


def fetch_json(url: str, retries: int = 3) -> Any:
    last_error: Exception | None = None
    contexts: list[ssl.SSLContext | None] = [None, ssl._create_unverified_context()]
    for context in contexts:
        for attempt in range(1, retries + 1):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "geo-ring-cloud-epic-compare/1.0"})
                with urllib.request.urlopen(req, timeout=45, context=context) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception as exc:  # noqa: BLE001 - preserve original network failure in report.
                last_error = exc
                time.sleep(min(2 * attempt, 8))
    raise RuntimeError(f"failed to fetch JSON from {url}: {last_error}")


def download_file(url: str, out_path: Path, retries: int = 3) -> tuple[str, str]:
    if out_path.exists() and out_path.stat().st_size > 0:
        return "SKIPPED_EXISTING", "local file already exists"
    last_error: Exception | None = None
    part = out_path.with_suffix(out_path.suffix + ".part")
    contexts: list[ssl.SSLContext | None] = [None, ssl._create_unverified_context()]
    for context in contexts:
        context_note = "verified TLS" if context is None else "unverified TLS fallback"
        for attempt in range(1, retries + 1):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "geo-ring-cloud-epic-compare/1.0"})
                with urllib.request.urlopen(req, timeout=90, context=context) as resp:
                    data = resp.read()
                if not data:
                    raise RuntimeError("empty response")
                part.write_bytes(data)
                part.replace(out_path)
                return "OK", f"downloaded {len(data)} bytes using {context_note}"
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if part.exists():
                    part.unlink(missing_ok=True)
                time.sleep(min(2 * attempt, 8))
    return "FAILED", str(last_error)


def parse_epic_time(text: str) -> datetime:
    # EPIC API date strings are normally "YYYY-MM-DD HH:MM:SS".
    return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def epic_image_url(product: str, image: str, date: datetime) -> str:
    return EPIC_ARCHIVE.format(
        product=product,
        yyyy=date.strftime("%Y"),
        mm=date.strftime("%m"),
        dd=date.strftime("%d"),
        image=image,
    )


def infer_time_from_image_name(image: str) -> datetime | None:
    digits = "".join(ch for ch in image if ch.isdigit())
    if len(digits) >= 14:
        try:
            return datetime.strptime(digits[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def local_fallback_record(product: str, api_url: str, note: str) -> EpicRecord | None:
    matches = sorted(IMG_DIR.glob(f"{product}_*20240305*.png"))
    if not matches:
        return None
    local = matches[0]
    image = local.stem.removeprefix(product + "_")
    item_time = infer_time_from_image_name(image) or TARGET_TIME
    delta = abs((item_time - TARGET_TIME).total_seconds()) / 60.0
    return EpicRecord(
        product=product,
        image=image,
        caption="local fallback from previous EPIC download",
        date=item_time,
        delta_minutes=delta,
        centroid_lat=None,
        centroid_lon=None,
        api_url=api_url,
        image_url="",
        local_path=local,
        status="SKIPPED_EXISTING_LOCAL_FALLBACK",
        notes=note,
    )


def centroid_from_record(item: dict[str, Any]) -> tuple[float | None, float | None]:
    c = item.get("centroid_coordinates") or {}
    lat = c.get("lat")
    lon = c.get("lon")
    try:
        return (float(lat), float(lon))
    except (TypeError, ValueError):
        return (None, None)


def select_epic_records() -> list[EpicRecord]:
    if EPIC_L1_H5:
        return [make_epic_l1_rgb_record(Path(EPIC_L1_H5))]
    selected: list[EpicRecord] = []
    for product in PRODUCTS:
        api_url = EPIC_API.format(product=product, date=TARGET_DATE)
        try:
            rows = fetch_json(api_url)
            if not rows:
                selected.append(
                    EpicRecord(
                        product,
                        "",
                        "",
                        TARGET_TIME,
                        math.nan,
                        None,
                        None,
                        api_url,
                        "",
                        None,
                        "NO_RECORDS",
                        "EPIC API returned no records",
                    )
                )
                continue
            ranked = []
            for item in rows:
                item_time = parse_epic_time(item["date"])
                delta = abs((item_time - TARGET_TIME).total_seconds()) / 60.0
                ranked.append((delta, item_time, item))
            delta, item_time, item = sorted(ranked, key=lambda x: x[0])[0]
            image = str(item["image"])
            lat, lon = centroid_from_record(item)
            url = epic_image_url(product, image, item_time)
            local = IMG_DIR / f"{product}_{image}.png"
            status, notes = download_file(url, local)
            selected.append(
                EpicRecord(
                    product=product,
                    image=image,
                    caption=str(item.get("caption", "")),
                    date=item_time,
                    delta_minutes=delta,
                    centroid_lat=lat,
                    centroid_lon=lon,
                    api_url=api_url,
                    image_url=url,
                    local_path=local if local.exists() else None,
                    status=status,
                    notes=notes,
                )
            )
        except Exception as exc:  # noqa: BLE001
            fallback = local_fallback_record(product, api_url, f"API failed but local image was reused: {exc}")
            if fallback:
                selected.append(fallback)
            else:
                selected.append(
                    EpicRecord(
                        product,
                        "",
                        "",
                        TARGET_TIME,
                        math.nan,
                        None,
                        None,
                        api_url,
                        "",
                        None,
                        "FAILED_API",
                        str(exc),
                    )
                )
    return selected


def load_grid_definition() -> dict[str, Any]:
    with GRID_JSON.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_npz_data(name: str) -> tuple[np.ndarray, np.ndarray | None, dict[str, Any]]:
    p = FUSED_DIR / name
    with np.load(p, allow_pickle=True) as z:
        data = z["data"].astype(np.float32, copy=False)
        valid = z["valid_mask"].astype(bool, copy=False) if "valid_mask" in z.files else None
        meta = {}
        if "metadata_json" in z.files:
            try:
                meta = json.loads(str(z["metadata_json"]))
            except json.JSONDecodeError:
                meta = {"metadata_json_parse_error": str(z["metadata_json"])[:200]}
        return data, valid, meta


def load_source_map(name: str) -> tuple[np.ndarray, dict[str, Any]]:
    p = FUSED_DIR / name
    with np.load(p, allow_pickle=True) as z:
        data = z["data"].astype(np.float32, copy=False)
        meta = {}
        if "metadata_json" in z.files:
            try:
                meta = json.loads(str(z["metadata_json"]))
            except json.JSONDecodeError:
                meta = {}
        return data, meta


def lon_lat_vectors(grid: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    lon = np.linspace(
        float(grid["lon_centers_first_last"][0]),
        float(grid["lon_centers_first_last"][1]),
        int(grid["lon_size"]),
        dtype=np.float32,
    )
    lat = np.linspace(
        float(grid["lat_centers_first_last"][0]),
        float(grid["lat_centers_first_last"][1]),
        int(grid["lat_size"]),
        dtype=np.float32,
    )
    return lon, lat


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def record_rows(records: list[EpicRecord]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    time_rows = []
    manifest_rows = []
    for rec in records:
        row = {
            "target_time_utc": TARGET_TIME.isoformat().replace("+00:00", "Z"),
            "epic_product": rec.product,
            "epic_time_utc": rec.date.isoformat().replace("+00:00", "Z") if rec.image else "",
            "delta_minutes": f"{rec.delta_minutes:.2f}" if math.isfinite(rec.delta_minutes) else "",
            "image": rec.image,
            "status": rec.status,
            "centroid_lat": rec.centroid_lat if rec.centroid_lat is not None else "",
            "centroid_lon": rec.centroid_lon if rec.centroid_lon is not None else "",
            "notes": rec.notes,
        }
        time_rows.append(row)
        manifest_rows.append(
            {
                **row,
                "api_url": rec.api_url,
                "image_url": rec.image_url,
                "local_path": str(rec.local_path) if rec.local_path else "",
                "caption": rec.caption,
            }
        )
    return time_rows, manifest_rows


def normalize_for_display(data: np.ndarray, valid: np.ndarray | None, pct: tuple[float, float]) -> np.ndarray:
    arr = data.astype(np.float32, copy=True)
    if valid is not None:
        arr[~valid] = np.nan
    vals = arr[np.isfinite(arr)]
    if vals.size == 0:
        return arr
    lo, hi = np.nanpercentile(vals, pct)
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        lo, hi = float(np.nanmin(vals)), float(np.nanmax(vals))
        if hi <= lo:
            hi = lo + 1.0
    return np.clip((arr - lo) / (hi - lo), 0.0, 1.0)


def centered_extent(center_lon: float | None) -> tuple[float, float]:
    if center_lon is None:
        return (-180.0, 180.0)
    return (center_lon - 180.0, center_lon + 180.0)


def roll_to_center(data: np.ndarray, center_lon: float | None) -> np.ndarray:
    if center_lon is None:
        return data
    nlon = data.shape[1]
    lons = np.linspace(-179.975, 179.975, nlon, dtype=np.float32)
    wrapped = ((lons - center_lon + 180.0) % 360.0) - 180.0 + center_lon
    order = np.argsort(wrapped)
    return data[:, order]


def render_lonlat_panel(
    ax: plt.Axes,
    data: np.ndarray,
    title: str,
    cmap: str,
    valid: np.ndarray | None = None,
    vmin: float | None = None,
    vmax: float | None = None,
    center_lon: float | None = None,
) -> None:
    arr = data.astype(np.float32, copy=True)
    if valid is not None:
        arr[~valid] = np.nan
    arr = roll_to_center(arr, center_lon)
    x0, x1 = centered_extent(center_lon)
    im = ax.imshow(
        arr,
        origin="lower",
        extent=(x0, x1, -90, 90),
        cmap=cmap,
        vmin=vmin,
        vmax=vmax,
        interpolation="nearest",
        aspect="auto",
    )
    ax.set_title(title)
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_xlim(x0, x1)
    ax.set_ylim(-90, 90)
    ax.grid(color="white", alpha=0.18, linewidth=0.4)
    plt.colorbar(im, ax=ax, shrink=0.78, pad=0.015)


def render_cloud_binary_panel(
    ax: plt.Axes,
    data: np.ndarray,
    valid: np.ndarray | None,
    title: str,
    center_lon: float | None = None,
) -> None:
    arr = data.astype(np.float32, copy=True)
    if valid is not None:
        arr[~valid] = np.nan
    arr = roll_to_center(arr, center_lon)
    x0, x1 = centered_extent(center_lon)
    cmap = ListedColormap(["#1f5a8a", "#f7f7f2"])
    cmap.set_bad("#d9d9d9")
    norm = BoundaryNorm([-0.5, 0.5, 1.5], cmap.N)
    im = ax.imshow(
        arr,
        origin="lower",
        extent=(x0, x1, -90, 90),
        cmap=cmap,
        norm=norm,
        interpolation="nearest",
        aspect="auto",
    )
    ax.set_title(title + "\n0=clear, 1=cloud, gray=no data")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_xlim(x0, x1)
    ax.set_ylim(-90, 90)
    ax.grid(color="white", alpha=0.18, linewidth=0.4)
    cbar = plt.colorbar(im, ax=ax, ticks=[0, 1], shrink=0.78, pad=0.015)
    cbar.ax.set_yticklabels(["clear", "cloud"])


def render_epic_image(ax: plt.Axes, image_path: Path | None, title: str) -> None:
    ax.set_title(title)
    ax.axis("off")
    if image_path and image_path.exists():
        img = Image.open(image_path).convert("RGB")
        ax.imshow(img)
    else:
        ax.text(0.5, 0.5, "EPIC image unavailable", ha="center", va="center", transform=ax.transAxes)


def make_lonlat_comparisons(records: list[EpicRecord], cloud_binary: np.ndarray, cloud_valid: np.ndarray | None, cth: np.ndarray, cth_valid: np.ndarray | None, source_map: np.ndarray, valid_count: np.ndarray) -> None:
    natural = next((r for r in records if r.product == "natural" and r.local_path), None)
    enhanced = next((r for r in records if r.product == "enhanced" and r.local_path), None)
    cloud = next((r for r in records if r.product == "cloud" and r.local_path), None)
    rgb = natural or enhanced
    center_lat, center_lon, center_note = epic_center(records)
    center_title = f"centered at EPIC lon {center_lon:.2f} deg"

    fig, axes = plt.subplots(1, 3, figsize=(17, 5.2), constrained_layout=True)
    render_epic_image(axes[0], rgb.local_path if rgb else None, f"EPIC RGB nearest to {TARGET_TIME:%Y-%m-%d %H:%MZ}")
    render_cloud_binary_panel(axes[1], cloud_binary, cloud_valid, f"GEO-ring fused cloud binary\n{center_title}", center_lon=center_lon)
    render_lonlat_panel(axes[2], source_map, f"GEO-ring source map (cloud mask)\n{center_title}", "tab20", cloud_valid, center_lon=center_lon)
    fig.savefig(QL_DIR / "epic_rgb_vs_georing_cloud_mask.png", dpi=180)
    plt.close(fig)

    fig, axes = plt.subplots(1, 3, figsize=(17, 5.2), constrained_layout=True)
    render_epic_image(axes[0], rgb.local_path if rgb else None, "EPIC RGB")
    render_lonlat_panel(axes[1], cth, f"GEO-ring fused CTH (km)\n{center_title}", "viridis", cth_valid, center_lon=center_lon)
    render_lonlat_panel(axes[2], valid_count, f"GEO-ring valid count (cloud mask)\n{center_title}", "magma", center_lon=center_lon)
    fig.savefig(QL_DIR / "epic_rgb_vs_georing_cth.png", dpi=180)
    plt.close(fig)

    fig, axes = plt.subplots(1, 3, figsize=(17, 5.2), constrained_layout=True)
    render_epic_image(axes[0], cloud.local_path if cloud else None, "EPIC Cloud Fraction")
    render_cloud_binary_panel(axes[1], cloud_binary, cloud_valid, f"GEO-ring fused cloud binary\n{center_title}", center_lon=center_lon)
    render_lonlat_panel(axes[2], valid_count, f"GEO-ring valid count\n{center_title}", "magma", center_lon=center_lon)
    fig.savefig(QL_DIR / "epic_cloud_fraction_vs_georing_cloud_binary.png", dpi=180)
    plt.close(fig)


def epic_center(records: list[EpicRecord]) -> tuple[float, float, str]:
    for product in ("natural", "enhanced", "cloud"):
        rec = next((r for r in records if r.product == product), None)
        if rec and rec.centroid_lat is not None and rec.centroid_lon is not None:
            lon = ((rec.centroid_lon + 180.0) % 360.0) - 180.0
            return rec.centroid_lat, lon, f"EPIC {product} centroid_coordinates"
    return 0.0, 0.0, "fallback 0N/0E because EPIC centroid was unavailable"


def orthographic_resample(
    data: np.ndarray,
    valid: np.ndarray | None,
    grid: dict[str, Any],
    center_lat: float,
    center_lon: float,
    out_size: int = 1000,
) -> tuple[np.ndarray, np.ndarray]:
    x = np.linspace(-1.0, 1.0, out_size, dtype=np.float32)
    y = np.linspace(1.0, -1.0, out_size, dtype=np.float32)
    xx, yy = np.meshgrid(x, y)
    rho = np.sqrt(xx * xx + yy * yy)
    visible = rho <= 1.0
    rho_safe = np.where(rho == 0, 1.0, rho)
    c = np.arcsin(np.clip(rho, 0, 1))
    lat0 = np.deg2rad(center_lat)
    lon0 = np.deg2rad(center_lon)
    lat = np.arcsin(np.cos(c) * np.sin(lat0) + (yy * np.sin(c) * np.cos(lat0) / rho_safe))
    lon = lon0 + np.arctan2(
        xx * np.sin(c),
        rho_safe * np.cos(lat0) * np.cos(c) - yy * np.sin(lat0) * np.sin(c),
    )
    lat = np.rad2deg(lat)
    lon = ((np.rad2deg(lon) + 180.0) % 360.0) - 180.0

    lat_min = float(grid["lat_min"])
    lon_min = float(grid["lon_min"])
    res = float(grid["resolution_degree"])
    row = np.rint((lat - (lat_min + res / 2.0)) / res).astype(np.int64)
    col = np.rint((lon - (lon_min + res / 2.0)) / res).astype(np.int64)
    ok = visible & (row >= 0) & (row < data.shape[0]) & (col >= 0) & (col < data.shape[1])
    if valid is not None:
        ok &= valid[row.clip(0, data.shape[0] - 1), col.clip(0, data.shape[1] - 1)]
    out = np.full((out_size, out_size), np.nan, dtype=np.float32)
    out[ok] = data[row[ok], col[ok]]
    return out, ok


def make_epic_view(records: list[EpicRecord], grid: dict[str, Any], cloud_binary: np.ndarray, cloud_valid: np.ndarray | None, cth: np.ndarray, cth_valid: np.ndarray | None, source_map: np.ndarray, valid_count: np.ndarray) -> str:
    center_lat, center_lon, center_note = epic_center(records)
    cloud_disk, cloud_ok = orthographic_resample(cloud_binary, cloud_valid, grid, center_lat, center_lon)
    cth_disk, cth_ok = orthographic_resample(cth, cth_valid, grid, center_lat, center_lon)
    src_disk, src_ok = orthographic_resample(source_map, cloud_valid, grid, center_lat, center_lon)
    vc_disk, _ = orthographic_resample(valid_count, None, grid, center_lat, center_lon)
    natural = next((r for r in records if r.product == "natural" and r.local_path), None)
    enhanced = next((r for r in records if r.product == "enhanced" and r.local_path), None)
    rgb = natural or enhanced

    fig, axes = plt.subplots(2, 2, figsize=(12, 12), constrained_layout=True)
    render_epic_image(axes[0, 0], rgb.local_path if rgb else None, f"EPIC RGB\ncenter={center_lat:.2f}, {center_lon:.2f}")
    cloud_cmap = ListedColormap(["#1f5a8a", "#f7f7f2"])
    cloud_cmap.set_bad("#d9d9d9")
    cloud_norm = BoundaryNorm([-0.5, 0.5, 1.5], cloud_cmap.N)
    axes[0, 1].imshow(cloud_disk, cmap=cloud_cmap, norm=cloud_norm, interpolation="nearest")
    axes[0, 1].set_title("GEO-ring cloud binary in approximate EPIC view\n0=clear, 1=cloud")
    axes[0, 1].axis("off")
    im = axes[1, 0].imshow(cth_disk, cmap="viridis", interpolation="nearest")
    axes[1, 0].set_title("GEO-ring CTH in approximate EPIC view")
    axes[1, 0].axis("off")
    plt.colorbar(im, ax=axes[1, 0], shrink=0.78)
    im2 = axes[1, 1].imshow(src_disk, cmap="tab20", interpolation="nearest")
    axes[1, 1].contour(np.isfinite(vc_disk), levels=[0.5], colors="white", linewidths=0.5)
    axes[1, 1].set_title("GEO-ring source map in approximate EPIC view")
    axes[1, 1].axis("off")
    plt.colorbar(im2, ax=axes[1, 1], shrink=0.78)
    fig.savefig(QL_DIR / "epic_view_source_map.png", dpi=180)
    plt.close(fig)

    return (
        f"{center_note}; center_lat={center_lat:.3f}; center_lon={center_lon:.3f}; "
        f"cloud_disk_valid_pixels={int(cloud_ok.sum())}; cth_disk_valid_pixels={int(cth_ok.sum())}; "
        f"source_disk_valid_pixels={int(src_ok.sum())}"
    )


def summarize_array(data: np.ndarray, valid: np.ndarray | None, name: str) -> dict[str, Any]:
    arr = data.astype(np.float32, copy=False)
    mask = np.isfinite(arr)
    if valid is not None:
        mask &= valid
    vals = arr[mask]
    if vals.size == 0:
        return {"name": name, "valid_pixels": 0}
    return {
        "name": name,
        "valid_pixels": int(vals.size),
        "min": float(np.nanmin(vals)),
        "p05": float(np.nanpercentile(vals, 5)),
        "mean": float(np.nanmean(vals)),
        "p95": float(np.nanpercentile(vals, 95)),
        "max": float(np.nanmax(vals)),
    }


def write_report(records: list[EpicRecord], epic_view_note: str, stats: list[dict[str, Any]]) -> None:
    successful = [r for r in records if r.local_path and r.local_path.exists()]
    nearest_text = "\n".join(
        [
            f"- {r.product}: {r.date.isoformat().replace('+00:00', 'Z') if r.image else 'N/A'}, "
            f"delta={r.delta_minutes:.2f} min, status={r.status}, file={r.local_path or ''}"
            for r in records
        ]
    )
    warning = "YES" if any(math.isfinite(r.delta_minutes) and r.delta_minutes > 60 for r in records) else "NO"
    stat_text = "\n".join(
        [
            "- "
            + ", ".join(f"{k}={v:.4g}" if isinstance(v, float) else f"{k}={v}" for k, v in s.items())
            for s in stats
        ]
    )
    l2_note = (
        "本轮未批量下载 EPIC L2 云产品。原因是当前导师问题首先是视觉差异；"
        "EPIC Cloud Effective Height/Pressure 与 GEO-ring CTH/CTP 不完全等价，"
        "后续若做定量参照，应只下载少量同一时次样本并单独标注不等价风险。"
    )
    text = f"""# EPIC 与 GEO-ring 单时次视觉对比报告

- 目标 GEO-ring 时次: {TARGET_TIME.isoformat().replace('+00:00', 'Z')}
- 输出目录: `{OUT_DIR}`
- quicklook 目录: `{QL_DIR}`
- 时间差超过 60 分钟 warning: {warning}

## EPIC 时间匹配

{nearest_text}

## GEO-ring 输入

- 使用 fused best-source 结果，不重新融合、不重投影。
- cloud mask: `fused_cloud_binary.npz`
- CTH: `fused_cloud_top_height_km.npz`
- source map: `source_map_cloud_mask.npz`
- valid count: `valid_count_map_cloud_mask.npz`

## GEO-ring 数值概览

{stat_text}

## 输出图像

- `quicklooks_epic_compare/epic_rgb_vs_georing_cloud_mask.png`
- `quicklooks_epic_compare/epic_rgb_vs_georing_cth.png`
- `quicklooks_epic_compare/epic_cloud_fraction_vs_georing_cloud_binary.png`
- `quicklooks_epic_compare/epic_view_source_map.png`

## EPIC 近似视角处理

{epic_view_note}

这里采用 EPIC `centroid_coordinates` 作为近似正射中心，把 GEO-ring 经纬网采样到正射盘面。它适合做视觉解释，不等价于严格 EPIC 相机几何反投影。

## 初步解释建议

- EPIC RGB/Cloud Fraction 主要用于判断大尺度云系形态、日照半球边界、热带对流和锋面云带是否与 GEO-ring 一致。
- GEO-ring source_map 用于解释拼接边界；如果视觉突变与 source boundary 同位，应归入融合源切换 warning，而不是 EPIC 观测差异。
- EPIC 只能看日照半球，不能评价 GEO-ring 夜半球云产品。
- 若 EPIC 与 GEO-ring 的云边界不一致，优先检查时间差、视角差、日夜边界和 source boundary，再考虑产品算法差异。

## EPIC L2 云产品处理结论

{l2_note}

## Gate

- EPIC_VISUAL_MATCH_GATE = {'PASS' if successful else 'FAIL'}
- EPIC_L2_QUANTITATIVE_GATE = NOT_ATTEMPTED
"""
    (OUT_DIR / "epic_visual_comparison_report.md").write_text(text, encoding="utf-8")
    (REPORT_DIR / "epic_visual_comparison_report.md").write_text(text, encoding="utf-8")


def main() -> int:
    ensure_dirs()
    records = select_epic_records()
    time_rows, manifest_rows = record_rows(records)
    write_csv(
        OUT_DIR / "epic_time_match_summary.csv",
        time_rows,
        [
            "target_time_utc",
            "epic_product",
            "epic_time_utc",
            "delta_minutes",
            "image",
            "status",
            "centroid_lat",
            "centroid_lon",
            "notes",
        ],
    )
    write_csv(
        OUT_DIR / "epic_visual_comparison_manifest.csv",
        manifest_rows,
        [
            "target_time_utc",
            "epic_product",
            "epic_time_utc",
            "delta_minutes",
            "image",
            "status",
            "centroid_lat",
            "centroid_lon",
            "api_url",
            "image_url",
            "local_path",
            "caption",
            "notes",
        ],
    )

    grid = load_grid_definition()
    cloud_binary, cloud_valid, _ = load_npz_data("fused_cloud_binary.npz")
    cth, cth_valid, _ = load_npz_data("fused_cloud_top_height_km.npz")
    source_map, _ = load_source_map("source_map_cloud_mask.npz")
    valid_count, _, _ = load_npz_data("valid_count_map_cloud_mask.npz")

    make_lonlat_comparisons(records, cloud_binary, cloud_valid, cth, cth_valid, source_map, valid_count)
    epic_view_note = make_epic_view(records, grid, cloud_binary, cloud_valid, cth, cth_valid, source_map, valid_count)
    stats = [
        summarize_array(cloud_binary, cloud_valid, "fused_cloud_binary"),
        summarize_array(cth, cth_valid, "fused_cloud_top_height_km"),
        summarize_array(valid_count, None, "valid_count_cloud_mask"),
    ]
    write_report(records, epic_view_note, stats)
    print(f"EPIC visual report: {OUT_DIR / 'epic_visual_comparison_report.md'}")
    print(f"Quicklooks: {QL_DIR}")
    print(f"Downloaded/available EPIC images: {sum(1 for r in records if r.local_path and r.local_path.exists())}/{len(records)}")
    return 0 if any(r.local_path and r.local_path.exists() for r in records) else 2


if __name__ == "__main__":
    raise SystemExit(main())
