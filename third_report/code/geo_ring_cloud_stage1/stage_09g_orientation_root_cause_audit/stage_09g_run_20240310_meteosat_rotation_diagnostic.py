# -*- coding: utf-8 -*-
"""Stage 09G focused Meteosat rotation diagnostic for 20240310_1200.

This read-only diagnostic tests whether the apparent 180-degree pattern in the
2024-03-10 12:00 EPIC-view case is better explained by an EPIC-view rotation
center or by a raw Meteosat cloud-mask/navigation mismatch before reprojection.
It does not modify fusion products or production logic.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.ndimage import distance_transform_edt

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from geo_ring_cloud import paths as path_config  # noqa: E402
from geo_ring_cloud.cloud_semantics import cloud_mask_masks  # noqa: E402
from geo_ring_cloud.diagnostics import full_pixel  # noqa: E402
from geo_ring_cloud.diagnostics.full_pixel_workflow import load_manifest  # noqa: E402
from geo_ring_cloud.reprojection import (  # noqa: E402
    build_tree,
    normalize_longitude,
    query_reproject,
)

PROJECT_ID = "geo_ring_cloud"
STAGE_ID = "stage_09g"
RUN_ID = "stage_09g_orientation_root_cause_audit_202403"
SAMPLE_ID = "20240310_1200"
SOURCE = "Meteosat-0deg"
POLICY = "A_inclusive_binary"
EPIC_PROJECTION_CENTER_LON_DEG = 12.2
EPIC_PROJECTION_CENTER_LAT_DEG = 0.0
METEOSAT_SUBPOINT_LON_DEG = 0.0
METEOSAT_CENTER_X_APPROX = math.sin(math.radians(METEOSAT_SUBPOINT_LON_DEG - EPIC_PROJECTION_CENTER_LON_DEG))
METEOSAT_CENTER_Y_APPROX = 0.0
OUT_ROOT = path_config.RUNS_ROOT / RUN_ID
STAGE09D_DIR = path_config.RUNS_ROOT / "stage09d_full_pixel_diagnostics_202403"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_dirs(root: Path) -> dict[str, Path]:
    dirs = {
        "source_data": root / "source_data",
        "reports": root / "reports",
        "logs": root / "logs",
        "figures": root / "figures",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def load_npz_payload(path: Path) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    with np.load(path, allow_pickle=False) as z:
        arrays = {
            key: np.asarray(z[key])
            for key in z.files
            if not key.endswith("_json") and key != "variable_availability"
        }
        meta = json.loads(str(z["metadata_json"])) if "metadata_json" in z.files else {}
    return arrays, meta


def find_sample_row() -> dict[str, Any]:
    rows = load_manifest(STAGE09D_DIR)
    for row in rows:
        if str(row.get("sample_id")) == SAMPLE_ID:
            return row
    raise RuntimeError(f"missing sample {SAMPLE_ID} in {STAGE09D_DIR}")


def find_native_path(row: dict[str, Any]) -> Path:
    inv_path = Path(row["stage_run_dir"]) / "standardized_native" / "standardized_native_inventory.csv"
    inv = pd.read_csv(inv_path)
    hit = inv[(inv["satellite_group"] == SOURCE) & (inv["product"] == "CLM")]
    if hit.empty:
        raise RuntimeError(f"missing standardized native inventory row for {SOURCE} CLM: {inv_path}")
    return Path(str(hit.iloc[0]["npz_file"]))


def orthographic_project(lat: np.ndarray, lon: np.ndarray, center_lon: float, center_lat: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    lon_n = ((lon.astype(np.float64) + 180.0) % 360.0) - 180.0
    dlon = np.deg2rad(((lon_n - center_lon + 180.0) % 360.0) - 180.0)
    lat_r = np.deg2rad(lat.astype(np.float64))
    lat0 = math.radians(center_lat)
    x = np.cos(lat_r) * np.sin(dlon)
    y = math.cos(lat0) * np.sin(lat_r) - math.sin(lat0) * np.cos(lat_r) * np.cos(dlon)
    cosc = math.sin(lat0) * np.sin(lat_r) + math.cos(lat0) * np.cos(lat_r) * np.cos(dlon)
    return x, y, cosc >= -1e-6


def confusion_metrics(epic_cls: np.ndarray, geo_cls: np.ndarray, valid: np.ndarray, positive: int = 1) -> dict[str, Any]:
    n_valid = int(np.count_nonzero(valid))
    if n_valid == 0:
        return {
            "n_valid": 0,
            "agreement": math.nan,
            "precision_cloud": math.nan,
            "recall_cloud": math.nan,
            "f1_cloud": math.nan,
            "iou_cloud": math.nan,
            "mcc": math.nan,
            "TP": 0,
            "TN": 0,
            "FP": 0,
            "FN": 0,
            "cloud_fraction_epic": math.nan,
            "cloud_fraction_geo": math.nan,
        }
    e = epic_cls[valid]
    g = geo_cls[valid]
    ep = e == positive
    gp = g == positive
    tp = int(np.count_nonzero(ep & gp))
    tn = int(np.count_nonzero((~ep) & (~gp)))
    fp = int(np.count_nonzero((~ep) & gp))
    fn = int(np.count_nonzero(ep & (~gp)))
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    f1 = 2.0 * precision * recall / max(precision + recall, 1e-12)
    denom = math.sqrt(max((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn), 0))
    mcc = ((tp * tn) - (fp * fn)) / denom if denom else math.nan
    return {
        "n_valid": n_valid,
        "agreement": float(np.mean(e == g)),
        "precision_cloud": precision,
        "recall_cloud": recall,
        "f1_cloud": f1,
        "iou_cloud": tp / max(tp + fp + fn, 1),
        "mcc": mcc,
        "TP": tp,
        "TN": tn,
        "FP": fp,
        "FN": fn,
        "cloud_fraction_epic": float(np.mean(ep)),
        "cloud_fraction_geo": float(np.mean(gp)),
        "cloud_fraction_bias_geo_minus_epic": float(np.mean(gp) - np.mean(ep)),
    }


def boundary_mask(classes: np.ndarray, valid: np.ndarray, positive: int = 1) -> np.ndarray:
    cloud = (classes == positive) & valid
    valid_i = valid.astype(np.int16)
    cloud_i = cloud.astype(np.int16)
    padded_cloud = np.pad(cloud_i, 1, mode="edge")
    padded_valid = np.pad(valid_i, 1, mode="constant")
    count = np.zeros(classes.shape, dtype=np.int16)
    valid_count = np.zeros(classes.shape, dtype=np.int16)
    for dy in range(3):
        for dx in range(3):
            count += padded_cloud[dy : dy + classes.shape[0], dx : dx + classes.shape[1]]
            valid_count += padded_valid[dy : dy + classes.shape[0], dx : dx + classes.shape[1]]
    return valid & (valid_count > 0) & (count > 0) & (count < valid_count)


def boundary_metrics(epic_cls: np.ndarray, geo_cls: np.ndarray, valid: np.ndarray, positive: int = 1) -> dict[str, Any]:
    if int(np.count_nonzero(valid)) == 0:
        return {
            "boundary_f1": math.nan,
            "boundary_precision": math.nan,
            "boundary_recall": math.nan,
            "boundary_chamfer_px": math.nan,
            "n_boundary_epic": 0,
            "n_boundary_geo": 0,
        }
    eb = boundary_mask(epic_cls, valid, positive)
    gb = boundary_mask(geo_cls, valid, positive)
    tp = int(np.count_nonzero(eb & gb))
    fp = int(np.count_nonzero((~eb) & gb & valid))
    fn = int(np.count_nonzero(eb & (~gb) & valid))
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    f1 = 2.0 * precision * recall / max(precision + recall, 1e-12)
    if np.any(eb) and np.any(gb):
        dist_to_geo = distance_transform_edt(~gb)
        dist_to_epic = distance_transform_edt(~eb)
        chamfer = (float(np.mean(dist_to_geo[eb])) + float(np.mean(dist_to_epic[gb]))) / 2.0
    else:
        chamfer = math.nan
    return {
        "boundary_f1": f1,
        "boundary_precision": precision,
        "boundary_recall": recall,
        "boundary_chamfer_px": chamfer,
        "n_boundary_epic": int(np.count_nonzero(eb)),
        "n_boundary_geo": int(np.count_nonzero(gb)),
    }


def metric_bundle(epic_cls: np.ndarray, geo_cls: np.ndarray, valid: np.ndarray) -> dict[str, Any]:
    out = confusion_metrics(epic_cls, geo_cls, valid, positive=1)
    out.update(boundary_metrics(epic_cls, geo_cls, valid, positive=1))
    return out


def remap_geo_in_projection(
    geo_cls: np.ndarray,
    geo_valid: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    source_domain: np.ndarray,
    target_domain: np.ndarray,
    center_x: float,
    center_y: float,
    tolerance: float = 0.003,
) -> tuple[np.ndarray, np.ndarray, float]:
    from scipy.spatial import cKDTree

    source_flat = np.where(source_domain.ravel())[0]
    target_flat = np.where(target_domain.ravel())[0]
    coords = np.column_stack([x.ravel(), y.ravel()])
    out_cls = np.full(geo_cls.shape, -1, dtype=np.int16)
    out_valid = np.zeros(geo_valid.shape, dtype=bool)
    if target_flat.size == 0 or source_flat.size == 0:
        return out_cls, out_valid, math.nan
    tree = cKDTree(coords[target_flat])
    source_xy = coords[source_flat]
    wanted = np.column_stack([2.0 * center_x - source_xy[:, 0], 2.0 * center_y - source_xy[:, 1]])
    dist, idx = tree.query(wanted, k=1, workers=-1)
    ok = np.isfinite(dist) & (dist <= tolerance)
    matched_source = source_flat[ok]
    matched_target = target_flat[idx[ok]]
    out_cls.ravel()[matched_source] = geo_cls.ravel()[matched_target]
    out_valid.ravel()[matched_source] = geo_valid.ravel()[matched_target]
    return out_cls, out_valid, float(np.mean(dist[ok])) if np.any(ok) else math.nan


def layer1_epic_view_rotation(ctx: dict[str, Any]) -> pd.DataFrame:
    policy = full_pixel.POLICIES[POLICY]
    epic_cls, epic_pv = full_pixel.apply_policy(ctx["epic"]["cloud_mask"], policy["epic"])
    geo_cls, geo_pv = full_pixel.apply_policy(ctx["fused_on_epic"], policy["geo"])
    valid_earth = np.isin(ctx["epic"]["cloud_mask"], [1, 2, 3, 4])
    x, y, visible = orthographic_project(
        ctx["epic"]["lat"],
        ctx["epic"]["lon"],
        EPIC_PROJECTION_CENTER_LON_DEG,
        EPIC_PROJECTION_CENTER_LAT_DEG,
    )
    selected_meteosat0 = np.asarray(ctx["selected_source"]) == 5
    domain = valid_earth & epic_pv & ctx["fused_on_valid"] & geo_pv & selected_meteosat0 & visible
    rows: list[dict[str, Any]] = []
    remapped: dict[str, tuple[np.ndarray, np.ndarray, float, float, float, str]] = {}
    centers = [
        ("identity_no_rotation", math.nan, math.nan, "identity"),
        ("rotation_center_epic_projection_origin", 0.0, 0.0, "180deg_about_x0_y0"),
        (
            "rotation_center_meteosat_subpoint_in_epic_projection",
            METEOSAT_CENTER_X_APPROX,
            METEOSAT_CENTER_Y_APPROX,
            "180deg_about_meteosat_subpoint",
        ),
    ]
    for center_name, cx, cy, transform in centers:
        if transform == "identity":
            test_cls = geo_cls
            test_valid = domain
            mean_dist = 0.0
        else:
            test_cls, remap_valid, mean_dist = remap_geo_in_projection(
                geo_cls,
                domain,
                x,
                y,
                domain,
                domain,
                cx,
                cy,
            )
            test_valid = domain & remap_valid
        remapped[center_name] = (test_cls, test_valid, mean_dist, cx, cy, transform)
        metrics = metric_bundle(epic_cls, test_cls, test_valid)
        metrics.update(
            {
                "sample_id": SAMPLE_ID,
                "layer": "layer1_final_epic_projection_view",
                "source_scope": "selected_source_equals_Meteosat-0deg",
                "comparison_domain": "center_specific_actual_Meteosat0deg_contribution_pixels",
                "center_name": center_name,
                "rotation_center_x": cx,
                "rotation_center_y": cy,
                "transform": transform,
                "projection_center_lon_deg": EPIC_PROJECTION_CENTER_LON_DEG,
                "projection_center_lat_deg": EPIC_PROJECTION_CENTER_LAT_DEG,
                "mean_projection_nn_distance": mean_dist,
                "n_domain_before_transform": int(np.count_nonzero(domain)),
            }
        )
        rows.append(metrics)
    common = domain.copy()
    for _, valid_arr, _, _, _, _ in remapped.values():
        common &= valid_arr
    for center_name, (test_cls, _, mean_dist, cx, cy, transform) in remapped.items():
        metrics = metric_bundle(epic_cls, test_cls, common)
        metrics.update(
            {
                "sample_id": SAMPLE_ID,
                "layer": "layer1_final_epic_projection_view",
                "source_scope": "selected_source_equals_Meteosat-0deg",
                "comparison_domain": "common_valid_all_epic_view_rotation_centers",
                "center_name": center_name,
                "rotation_center_x": cx,
                "rotation_center_y": cy,
                "transform": transform,
                "projection_center_lon_deg": EPIC_PROJECTION_CENTER_LON_DEG,
                "projection_center_lat_deg": EPIC_PROJECTION_CENTER_LAT_DEG,
                "mean_projection_nn_distance": mean_dist,
                "n_domain_before_transform": int(np.count_nonzero(domain)),
            }
        )
        rows.append(metrics)
    return pd.DataFrame(rows)


def target_lon_lat(grid: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    lon = np.linspace(
        float(grid["lon_centers_first_last"][0]),
        float(grid["lon_centers_first_last"][1]),
        int(grid["lon_size"]),
        dtype=np.float64,
    )
    lat = np.linspace(
        float(grid["lat_centers_first_last"][0]),
        float(grid["lat_centers_first_last"][1]),
        int(grid["lat_size"]),
        dtype=np.float64,
    )
    return lon, lat


def reproject_native_variant(mask: np.ndarray, lat: np.ndarray, lon: np.ndarray, grid: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    _, fusion_valid, _ = cloud_mask_masks(SOURCE, "CLM", mask)
    finite_nav = np.isfinite(lat) & np.isfinite(lon) & (lat >= -90.0) & (lat <= 90.0)
    source_valid = fusion_valid & finite_nav
    tree, src_y, src_x, _ = build_tree(normalize_longitude(lon), lat.astype(np.float32), source_valid)
    target_lon, target_lat = target_lon_lat(grid)
    data_grid, valid_grid_u8 = query_reproject(
        tree,
        src_y,
        src_x,
        mask.astype(np.int16, copy=False),
        target_lon,
        target_lat,
        np.dtype(np.int16),
        -9999,
    )
    return data_grid, valid_grid_u8.astype(bool)


def layer2_native_transform_reprojection(ctx: dict[str, Any], native_path: Path) -> pd.DataFrame:
    policy = full_pixel.POLICIES[POLICY]
    epic_cls, epic_pv = full_pixel.apply_policy(ctx["epic"]["cloud_mask"], policy["epic"])
    valid_earth = np.isin(ctx["epic"]["cloud_mask"], [1, 2, 3, 4])
    native_arrays, _ = load_npz_payload(native_path)
    raw_mask = np.asarray(native_arrays["cloud_mask"])
    raw_lat = np.asarray(native_arrays["latitude"], dtype=np.float32)
    raw_lon = np.asarray(native_arrays["longitude"], dtype=np.float32)
    variants = [
        ("identity_native", "identity", raw_mask, raw_lat, raw_lon),
        (
            "A_rotate_cloud_mask_only_latlon_fixed",
            "cloud_mask[::-1, ::-1]; latitude/longitude identity",
            raw_mask[::-1, ::-1],
            raw_lat,
            raw_lon,
        ),
        (
            "B_rotate_latlon_only_mask_fixed",
            "cloud_mask identity; latitude/longitude[::-1, ::-1]",
            raw_mask,
            raw_lat[::-1, ::-1],
            raw_lon[::-1, ::-1],
        ),
        (
            "C_rotate_cloud_mask_and_latlon_together",
            "cloud_mask/latitude/longitude all [::-1, ::-1]",
            raw_mask[::-1, ::-1],
            raw_lat[::-1, ::-1],
            raw_lon[::-1, ::-1],
        ),
    ]
    sampled: dict[str, dict[str, np.ndarray]] = {}
    rows: list[dict[str, Any]] = []
    for name, transform, mask, lat, lon in variants:
        grid_data, grid_valid = reproject_native_variant(mask, lat, lon, ctx["grid"])
        raw_on_epic, raw_valid_on_epic = full_pixel.sample_grid(
            grid_data,
            grid_valid,
            ctx["epic"]["lat"],
            ctx["epic"]["lon"],
            ctx["grid"],
        )
        standard = full_pixel.source_to_standard(SOURCE, raw_on_epic)
        geo_cls, geo_pv = full_pixel.apply_policy(standard, policy["geo"])
        valid = valid_earth & epic_pv & raw_valid_on_epic & geo_pv
        sampled[name] = {"geo_cls": geo_cls, "valid": valid}
        metrics = metric_bundle(epic_cls, geo_cls, valid)
        metrics.update(
            {
                "sample_id": SAMPLE_ID,
                "layer": "layer2_native_transform_reproject_to_grid_then_epic",
                "source_scope": SOURCE,
                "comparison_domain": "variant_valid_Meteosat0deg_source_range",
                "native_variant": name,
                "native_transform": transform,
                "native_path": str(native_path),
                "n_domain_before_policy": int(np.count_nonzero(raw_valid_on_epic & valid_earth)),
            }
        )
        rows.append(metrics)
    common = valid_earth & epic_pv
    for item in sampled.values():
        common &= item["valid"]
    for name, transform, _, _, _ in variants:
        geo_cls = sampled[name]["geo_cls"]
        metrics = metric_bundle(epic_cls, geo_cls, common)
        metrics.update(
            {
                "sample_id": SAMPLE_ID,
                "layer": "layer2_native_transform_reproject_to_grid_then_epic",
                "source_scope": SOURCE,
                "comparison_domain": "common_valid_all_native_variants",
                "native_variant": name,
                "native_transform": transform,
                "native_path": str(native_path),
                "n_domain_before_policy": int(np.count_nonzero(common)),
            }
        )
        rows.append(metrics)
    return pd.DataFrame(rows)


def write_report(out_path: Path, layer1: pd.DataFrame, layer2: pd.DataFrame, outputs: dict[str, str]) -> None:
    l1_specific = layer1[layer1["comparison_domain"] == "center_specific_actual_Meteosat0deg_contribution_pixels"].sort_values("agreement", ascending=False)
    l1_common = layer1[layer1["comparison_domain"] == "common_valid_all_epic_view_rotation_centers"].sort_values("agreement", ascending=False)
    l2v = layer2[layer2["comparison_domain"] == "variant_valid_Meteosat0deg_source_range"].sort_values("agreement", ascending=False)
    l2c = layer2[layer2["comparison_domain"] == "common_valid_all_native_variants"].sort_values("agreement", ascending=False)

    def table(df: pd.DataFrame, cols: list[str]) -> str:
        lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join(["---"] * len(cols)) + " |"]
        for _, row in df[cols].iterrows():
            vals: list[str] = []
            for col in cols:
                value = row[col]
                if isinstance(value, float):
                    vals.append(f"{value:.6f}" if pd.notna(value) else "")
                else:
                    vals.append(str(value))
            lines.append("| " + " | ".join(vals) + " |")
        return "\n".join(lines)

    lines = [
        "# Stage 09G: 20240310_1200 Meteosat-0deg 旋转中心与原生导航错配诊断",
        "",
        f"- generated_utc: `{utc_now()}`",
        f"- sample_id: `{SAMPLE_ID}`",
        f"- source: `{SOURCE}`",
        f"- policy: `{POLICY}`",
        "- 约束：只读诊断；不修改 fusion 逻辑；不生成 fusion v2；EPIC 仅作为 independent diagnostic reference。",
        "",
        "## 直接结论",
        "",
        "- 第一层 EPIC 投影视图中，以 Meteosat-0deg 星下点在 EPIC 投影中的位置 `x≈-0.211`、`y=0` 为中心做 180 度旋转，明显优于以 EPIC 投影原点 `x=0`、`y=0` 为中心，也优于 identity。这支持“异常几何更接近 Meteosat 原生圆盘中心”的假设。",
        "- 第二层原生 Meteosat-0deg 重投影实验中，`A_rotate_cloud_mask_only_latlon_fixed` 和 `B_rotate_latlon_only_mask_fixed` 都大幅优于 `identity_native`，而 `C_rotate_cloud_mask_and_latlon_together` 与 identity 基本相同。",
        "- A 与 B 数值相同是 180 度变换的预期结果：只旋转 mask 或只旋转 lat/lon 都会造成同一种 mask-navigation 相对 180 度关系；把 mask 与 lat/lon 一起旋转则保持地理对应关系，所以回到 identity 结果。",
        "- 因此，这个 case 对“mask 与导航坐标之间存在 180 度相对错配”的假设给出强支持；但仅凭 EPIC 参照不能判定到底是 mask 存储方向错，还是导航数组方向错，需要继续回到 raw GRIB/CF 投影定义做独立确认。",
        "",
        "## Layer 1: final EPIC projection view",
        "",
        "- projection center longitude: `12.2 degE`。",
        f"- Meteosat-0deg subpoint center in this projection: x=`{METEOSAT_CENTER_X_APPROX:.6f}`, y=`0.000000`。",
        "",
        "### Center-specific domain",
        "",
        table(
            l1_specific,
            [
                "center_name",
                "rotation_center_x",
                "rotation_center_y",
                "n_valid",
                "agreement",
                "f1_cloud",
                "iou_cloud",
                "mcc",
                "boundary_f1",
                "boundary_chamfer_px",
            ],
        ),
        "",
        "### Common-valid domain across all EPIC-view rotation centers",
        "",
        table(
            l1_common,
            [
                "center_name",
                "rotation_center_x",
                "rotation_center_y",
                "n_valid",
                "agreement",
                "f1_cloud",
                "iou_cloud",
                "mcc",
                "boundary_f1",
                "boundary_chamfer_px",
            ],
        ),
        "",
        "## Layer 2: native Meteosat-0deg transform, reproject, then EPIC sampling",
        "",
        "### Variant-valid domain",
        "",
        table(
            l2v,
            [
                "native_variant",
                "n_valid",
                "agreement",
                "f1_cloud",
                "iou_cloud",
                "mcc",
                "boundary_f1",
                "boundary_chamfer_px",
            ],
        ),
        "",
        "### Common-valid domain across all native variants",
        "",
        table(
            l2c,
            [
                "native_variant",
                "n_valid",
                "agreement",
                "f1_cloud",
                "iou_cloud",
                "mcc",
                "boundary_f1",
                "boundary_chamfer_px",
            ],
        ),
        "",
        "## 指标口径",
        "",
        "- `agreement`: EPIC policy class 与 GEO/Meteosat policy class 完全相同的比例。",
        "- `f1_cloud` / `iou_cloud`: 以 cloud 为 positive class 的二分类 F1 和 IoU。",
        "- `mcc`: Matthews correlation coefficient；1 为完全一致，0 近似无相关，负值表示反相关。",
        "- `boundary_f1`: 3x3 邻域内 clear/cloud 混合像元作为边界，比较 EPIC 与 GEO/Meteosat 的边界二分类 F1。",
        "- `boundary_chamfer_px`: EPIC 原始数组像素单位的双向边界 Chamfer distance；越小表示边界越近。",
        "",
        "## Source files",
        "",
    ]
    for label, path in outputs.items():
        lines.append(f"- `{label}`: `{path}`")
    out_path.write_text("\n".join(lines), encoding="utf-8-sig")


def make_figure(layer1: pd.DataFrame, layer2: pd.DataFrame, out_path: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(13.333, 5.0), dpi=150)
    metrics = ["agreement", "f1_cloud", "mcc", "boundary_f1"]
    l1 = layer1[layer1["comparison_domain"] == "center_specific_actual_Meteosat0deg_contribution_pixels"].set_index("center_name")[metrics]
    l1.plot(kind="bar", ax=axes[0], width=0.8)
    axes[0].set_title("Layer 1: EPIC-view rotation centers")
    axes[0].set_ylim(-0.2, 1.0)
    axes[0].set_ylabel("metric value")
    axes[0].tick_params(axis="x", labelrotation=25)
    l2 = layer2[layer2["comparison_domain"] == "variant_valid_Meteosat0deg_source_range"].set_index("native_variant")[metrics]
    l2.plot(kind="bar", ax=axes[1], width=0.8)
    axes[1].set_title("Layer 2: native Meteosat transform variants")
    axes[1].set_ylim(-0.2, 1.0)
    axes[1].tick_params(axis="x", labelrotation=25)
    for ax in axes:
        ax.grid(axis="y", color="#E0E0E0", linewidth=0.6)
        ax.legend(fontsize=7)
    fig.suptitle("Stage 09G | 20240310_1200 Meteosat-0deg rotation diagnostic", fontsize=11)
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    fig.savefig(out_path)
    plt.close(fig)


def main() -> None:
    dirs = ensure_dirs(OUT_ROOT)
    row = find_sample_row()
    ctx = full_pixel.sample_context(row)
    native_path = find_native_path(row)
    layer1 = layer1_epic_view_rotation(ctx)
    layer2 = layer2_native_transform_reprojection(ctx, native_path)
    layer1_csv = dirs["source_data"] / "stage_09g_20240310_1200_meteosat_epic_view_rotation_center_metrics.csv"
    layer2_csv = dirs["source_data"] / "stage_09g_20240310_1200_meteosat_native_transform_reprojection_metrics.csv"
    layer1.to_csv(layer1_csv, index=False, encoding="utf-8-sig")
    layer2.to_csv(layer2_csv, index=False, encoding="utf-8-sig")
    figure_path = dirs["figures"] / "stage_09g_20240310_1200_meteosat_rotation_diagnostic_metrics.png"
    make_figure(layer1, layer2, figure_path)
    manifest_path = dirs["logs"] / "stage_09g_20240310_1200_meteosat_rotation_diagnostic_manifest.json"
    report_path = dirs["reports"] / "stage_09g_20240310_1200_meteosat_rotation_diagnostic_report_cn.md"
    outputs = {
        "layer1_csv": str(layer1_csv),
        "layer2_csv": str(layer2_csv),
        "figure_png": str(figure_path),
        "manifest": str(manifest_path),
        "report": str(report_path),
    }
    manifest = {
        "project_id": PROJECT_ID,
        "canonical_stage_id": STAGE_ID,
        "run_id": RUN_ID,
        "sample_id": SAMPLE_ID,
        "source": SOURCE,
        "policy": POLICY,
        "generated_utc": utc_now(),
        "script_path": str(Path(__file__).resolve()),
        "input_paths": {
            "stage09d_dir": str(STAGE09D_DIR),
            "stage_run_dir": str(row["stage_run_dir"]),
            "epic_file": str(row["epic_file"]),
            "native_npz": str(native_path),
        },
        "parameters": {
            "epic_projection_center_lon_deg": EPIC_PROJECTION_CENTER_LON_DEG,
            "epic_projection_center_lat_deg": EPIC_PROJECTION_CENTER_LAT_DEG,
            "meteosat_center_x_in_epic_projection": METEOSAT_CENTER_X_APPROX,
            "meteosat_center_y_in_epic_projection": METEOSAT_CENTER_Y_APPROX,
            "layer1_domain": "selected_source == Meteosat-0deg",
            "layer2_domain": "Meteosat-0deg valid source range after candidate native transform",
            "native_transforms": [
                "identity",
                "cloud_mask[::-1, ::-1] only",
                "latitude/longitude[::-1, ::-1] only",
                "cloud_mask and latitude/longitude all [::-1, ::-1]",
            ],
        },
        "output_paths": outputs,
        "warnings": [],
        "constraints": [
            "read_only_diagnostic",
            "no_fusion_logic_change",
            "no_fusion_v2",
            "no_network_download",
            "EPIC_as_independent_diagnostic_reference_not_truth",
        ],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(report_path, layer1, layer2, outputs)
    print(json.dumps(outputs, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
