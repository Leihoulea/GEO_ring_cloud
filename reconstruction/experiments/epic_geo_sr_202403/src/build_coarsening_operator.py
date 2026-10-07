"""Build a metric, footprint-mean native-EPIC → 50 km operator per scene."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from common import DATA, REPORTS, configured_inputs, ensure_layout, haversine_km, load_config, read_epic, resolve_scene_rows, utc_now, write_csv, write_manifest


def circular_center(lon: np.ndarray) -> float:
    radians = np.deg2rad(lon[np.isfinite(lon)])
    return float(np.rad2deg(np.arctan2(np.mean(np.sin(radians)), np.mean(np.cos(radians)))))


def orthographic_xy(lat: np.ndarray, lon: np.ndarray, lat0: float, lon0: float) -> tuple[np.ndarray, np.ndarray]:
    p, q, p0 = np.deg2rad(lat), np.deg2rad(lon), np.deg2rad(lat0)
    dl = np.deg2rad(((lon - lon0 + 180.0) % 360.0) - 180.0)
    x = 6371.0088 * np.cos(p) * np.sin(dl)
    y = 6371.0088 * (np.cos(p0) * np.sin(p) - np.sin(p0) * np.cos(p) * np.cos(dl))
    return x.astype(np.float32), y.astype(np.float32)


def neighbor_resolution(epic: dict[str, np.ndarray]) -> float:
    valid = epic["valid"]
    lat, lon = epic["lat"], epic["lon"]
    right = valid[:, 1:] & valid[:, :-1]
    down = valid[1:, :] & valid[:-1, :]
    distances = [haversine_km(lat[:, :-1][right], lon[:, :-1][right], lat[:, 1:][right], lon[:, 1:][right]), haversine_km(lat[:-1, :][down], lon[:-1, :][down], lat[1:, :][down], lon[1:, :][down])]
    merged = np.concatenate([d[np.isfinite(d) & (d > 0) & (d < 100)] for d in distances])
    if len(merged) == 0:
        raise RuntimeError("Cannot estimate native EPIC pixel spacing from valid geolocation.")
    return float(np.median(merged))


def main() -> None:
    ensure_layout()
    cfg, _, _, manifest = configured_inputs()
    coarse_cfg = load_config("coarsening.yaml")
    rows = resolve_scene_rows()
    results = []
    outputs: list[Path] = []
    target = float(coarse_cfg["target_resolution_km"])
    for row in rows:
        output = DATA / "coarse50" / f"{row['sample_id']}_operator.npz"
        # A long local run may be interrupted by the desktop supervisor.  The
        # operator is immutable once written; resume rather than overwrite it.
        if output.exists():
            with np.load(output, allow_pickle=False) as z:
                finite = z["valid_mask"].astype(bool)
                native_km = float(z["native_resolution_km"])
                lat0, lon0 = float(z["lat0"]), float(z["lon0"])
                cell = z["cell_id"]
            outputs.append(output)
            results.append({"scene_id": row["sample_id"], "target_resolution_km": target, "native_resolution_median_km": native_km, "native_pixels_per_50km": target / native_km, "orthographic_center_lat": lat0, "orthographic_center_lon": lon0, "valid_pixels": int(np.count_nonzero(finite)), "coarse_cells": int(np.unique(cell[finite]).size), "operator_path": str(output), "resumed_existing": True})
            continue
        epic = read_epic(Path(row["epic_clm_file"]), cfg)
        valid = epic["valid"]
        lat0 = float(np.nanmedian(epic["lat"][valid]))
        lon0 = circular_center(epic["lon"][valid])
        x, y = orthographic_xy(epic["lat"], epic["lon"], lat0, lon0)
        finite = valid & np.isfinite(x) & np.isfinite(y)
        x0, y0 = float(np.nanmin(x[finite])), float(np.nanmin(y[finite]))
        col = np.floor((x - x0) / target).astype(np.int32)
        rix = np.floor((y - y0) / target).astype(np.int32)
        width = int(np.nanmax(col[finite])) + 1
        cell = np.full(valid.shape, -1, dtype=np.int32)
        cell[finite] = rix[finite] * width + col[finite]
        native_km = neighbor_resolution(epic)
        np.savez_compressed(output, cell_id=cell, x_km=x, y_km=y, valid_mask=finite, lat0=np.float32(lat0), lon0=np.float32(lon0), cell_width=np.int32(width), target_resolution_km=np.float32(target), native_resolution_km=np.float32(native_km))
        outputs.append(output)
        results.append({"scene_id": row["sample_id"], "target_resolution_km": target, "native_resolution_median_km": native_km, "native_pixels_per_50km": target / native_km, "orthographic_center_lat": lat0, "orthographic_center_lon": lon0, "valid_pixels": int(np.count_nonzero(finite)), "coarse_cells": int(np.unique(cell[finite]).size), "operator_path": str(output)})
    table = DATA / "coarse50" / "coarsening_operator_inventory.csv"
    write_csv(table, results)
    median_native = float(np.median([r["native_resolution_median_km"] for r in results]))
    report = REPORTS / "03_epic_grid_geometry.md"
    report.write_text("\n".join([
        "# 03 EPIC 原生网格与退化算子几何", "", f"生成时间：`{utc_now()}`", "",
        f"- 以相邻有效 EPIC 像元的 Haversine 距离中位数估计原生间距；场景中位数为 **{median_native:.3f} km**。",
        f"- 50 km 退化不是在经纬度上 Gaussian blur：每一场景首先以其有效地球像元的中心建立正射（orthographic）km 坐标，再按 50 km × 50 km footprint cell 汇总。",
        "- 每个 operator 保留 native→cell 映射、投影坐标及有效掩膜；`simulate_coarse_epic.py` 使用相同权重计算 cloud fraction 与 valid fraction。",
        "- 这是基于 L2 geolocation 反演的近似 plane-of-sky metric operator，而非官方 EPIC PSF；该限制将在最终报告中保留。", "",
    ]), encoding="utf-8")
    outputs += [table, report]
    write_manifest("03_epic_grid_geometry", Path(__file__), [manifest], outputs, {"coarsening": coarse_cfg, "scene_count": len(rows)})
    print(f"Built {len(rows)} operators; median spacing = {median_native:.3f} km")


if __name__ == "__main__":
    sys.exit(main())
