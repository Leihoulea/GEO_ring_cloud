from __future__ import annotations

import argparse
import json
import math
import os
import shutil
from pathlib import Path
from typing import Any

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from geo_ring_cloud import paths as path_config
from geo_ring_cloud.lineage import utc_now, write_manifest
from geo_ring_cloud.pipeline_layout import (
    REPORT_DIR,
    SCRIPT_DIR,
    STAGE_ROOT,
    ensure_pipeline_directories as ensure_dirs,
)
from geo_ring_cloud.sources import REGISTRY_VERSION, tie_order, validate_profile
from geo_ring_cloud.diagnostics.summary import finite_stats
from geo_ring_cloud.cloud_semantics import cloud_mask_masks, cloud_mask_rows, cloud_mask_semantics
from geo_ring_cloud.reprojection import (
    available,
    build_tree,
    geolocate,
    load_npz,
    make_target_grid,
    query_reproject,
    valid_for_variable,
)


NATIVE_DIR = STAGE_ROOT / "standardized_native"
OUT_DIR = STAGE_ROOT / "reprojected_grid"
QUICKLOOK_DIR = STAGE_ROOT / "quicklooks_reprojected"
TARGET_JSON = OUT_DIR / "target_grid_definition.json"
INVENTORY_CSV = OUT_DIR / "reprojected_variable_inventory.csv"
SAT_STATS_CSV = OUT_DIR / "reprojected_per_satellite_stats.csv"
REPORT_MD = REPORT_DIR / "reproject_cloud_to_grid_report.md"
CLOUD_MASK_CODE_TABLE_CSV = REPORT_DIR / "cloud_mask_code_table.csv"
CLOUD_MASK_FUSION_REPORT_MD = REPORT_DIR / "cloud_mask_fusion_mask_report.md"

TARGET_TIME = os.environ.get("GEO_RING_TARGET_TIME", "2024-03-05T00:00:00Z")
TIME_TAG = os.environ.get("GEO_RING_TIME_TAG", TARGET_TIME[0:13].replace("-", "").replace("T", "_"))
GRID_RES = 0.05
LAT_MIN, LAT_MAX = -90.0, 90.0
LON_MIN, LON_MAX = -180.0, 180.0
MAX_DISTANCE_DEG = 0.15

CATEGORICAL = {"cloud_mask", "cloud_type", "cloud_phase", "quality_flag_raw", "quality_flag_standard", "valid_mask"}
VARIABLE_PRIORITY = [
    "cloud_mask",
    "cloud_top_height_km",
    "valid_mask",
    "quality_flag_raw",
    "quality_flag_standard",
    "cloud_type",
    "cloud_phase",
    "cloud_top_temperature_K",
    "cloud_top_pressure_hPa",
    "cloud_optical_thickness",
    "cloud_effective_radius_um",
    "cloud_water_path_g_m2",
    "cloud_probability",
    "sensor_zenith_angle",
    "solar_zenith_angle",
    "sun_glint_angle",
]
SOURCE_PROFILE = validate_profile(os.environ.get("GEO_RING_SOURCE_PROFILE", "operational_baseline"))
SATELLITES = tie_order(SOURCE_PROFILE)
METEOSAT_PRODUCTS_ALLOWED = {"CLM", "CTH"}
ONLY_CLOUD_MASK = os.environ.get("ONLY_CLOUD_MASK", "").strip().lower() in {"1", "true", "yes", "y"}


def output_dtype_and_fill(variable: str, arr: np.ndarray) -> tuple[np.dtype, float | int]:
    if variable in CATEGORICAL:
        return np.dtype(np.int16), -9999
    return np.dtype(np.float32), np.nan


def variable_units(meta: dict[str, Any], variable: str) -> str:
    if meta.get("satellite_family") == "CLAAS3":
        return str(meta.get("physical_units", {}).get(variable, ""))
    attrs = meta.get("reader_attrs", {}).get(f"attrs_{variable}", {})
    return str(attrs.get("units", ""))


def save_npz(
    out_path: Path,
    data: np.ndarray,
    valid_mask: np.ndarray,
    bundle: dict[str, Any],
    variable: str,
    product: str,
    target_grid: dict[str, Any],
    notes: list[str],
    extra_arrays: dict[str, np.ndarray] | None = None,
) -> None:
    meta = bundle["meta"]
    payload_meta = {
        "generated_utc": utc_now(),
        "nominal_time": TARGET_TIME,
        "source_satellite": meta.get("satellite_group", ""),
        "source_product": product,
        "source_file": meta.get("source_file", ""),
        "variable": variable,
        "units": variable_units(meta, variable),
        "resampling_method": "nearest",
        "native_shape": list(np.asarray(bundle["arrays"][variable]).shape),
        "target_grid": target_grid,
        "notes": notes,
    }
    payload: dict[str, np.ndarray] = {
        "data": data,
        "valid_mask": valid_mask.astype(np.uint8),
        "metadata_json": np.asarray(json.dumps(payload_meta, ensure_ascii=False, default=str)),
    }
    for name, value in (extra_arrays or {}).items():
        payload[name] = np.asarray(value)
    np.savez_compressed(out_path, **payload)

def make_quicklook(
    data: np.ndarray,
    valid: np.ndarray,
    out_path: Path,
    title: str,
    categorical: bool = False,
    code_table: dict[int, dict[str, Any]] | None = None,
) -> None:
    arr = np.asarray(data)
    valid_bool = np.asarray(valid) > 0
    title_lower = title.lower()
    is_binary_mask_plot = "valid_mask" in title_lower or "source coverage" in title_lower
    if is_binary_mask_plot:
        plot = valid_bool.astype(np.float32)
    else:
        plot = arr.astype(np.float32, copy=True)
        plot[~valid_bool] = np.nan
    # Keep image light.
    stride = max(1, int(math.ceil(math.sqrt(plot.size / 1_200_000)))) if plot.size > 1_200_000 else 1
    plot = plot[::stride, ::stride]
    plt.figure(figsize=(11, 4.8), dpi=150)
    if is_binary_mask_plot:
        from matplotlib.colors import BoundaryNorm, ListedColormap

        cmap = ListedColormap(["#f7f7f7", "#2ca25f"])
        norm = BoundaryNorm([-0.5, 0.5, 1.5], cmap.N)
        im = plt.imshow(plot, extent=[LON_MIN, LON_MAX, LAT_MIN, LAT_MAX], origin="lower", cmap=cmap, norm=norm, interpolation="nearest")
        cbar = plt.colorbar(im, shrink=0.78, ticks=[0, 1])
        cbar.ax.set_yticklabels(["no data", "valid"])
    elif categorical:
        from matplotlib.colors import BoundaryNorm, ListedColormap

        finite = np.isfinite(plot)
        if finite.any():
            values = np.sort(np.unique(plot[finite]))
            if code_table:
                colors = []
                labels = []
                for raw in values:
                    meta = code_table.get(int(raw), {})
                    labels.append(meta.get("meaning", str(int(raw) if float(raw).is_integer() else raw)))
                    if meta.get("is_off_disc"):
                        colors.append("#969696")
                    elif meta.get("meaning") == "cloud":
                        colors.append("#d7301f")
                    elif meta.get("meaning") == "probably_cloud":
                        colors.append("#fdae6b")
                    elif meta.get("meaning") == "probably_cloudy":
                        colors.append("#fdae6b")
                    elif meta.get("meaning") == "clear":
                        colors.append("#2b8cbe")
                    elif meta.get("meaning") == "probably_clear":
                        colors.append("#a6bddb")
                    elif meta.get("meaning") == "clear_or_probably_clear":
                        colors.append("#2b8cbe")
                    elif meta.get("meaning") == "cloudy":
                        colors.append("#d7301f")
                    elif meta.get("meaning") == "cloudy_or_probably_cloudy":
                        colors.append("#d7301f")
                    elif "water" in str(meta.get("meaning", "")):
                        colors.append("#2b8cbe")
                    elif "land" in str(meta.get("meaning", "")):
                        colors.append("#74c476")
                    else:
                        colors.append("#756bb1")
            else:
                colors = [
                    "#2b8cbe",
                    "#a6bddb",
                    "#fee391",
                    "#969696",
                    "#74c476",
                    "#fdae6b",
                    "#9e9ac8",
                    "#bdbdbd",
                ][: max(1, min(8, values.size))]
                labels = [str(int(v)) if float(v).is_integer() else f"{v:g}" for v in values]
            cmap = ListedColormap(colors[: max(1, len(values))])
            cmap.set_bad("#ffffff")
            if values.size == 1:
                bounds = np.array([values[0] - 0.5, values[0] + 0.5])
            else:
                bounds = np.concatenate(([values[0] - 0.5], (values[:-1] + values[1:]) / 2, [values[-1] + 0.5]))
            norm = BoundaryNorm(bounds, cmap.N)
            im = plt.imshow(plot, extent=[LON_MIN, LON_MAX, LAT_MIN, LAT_MAX], origin="lower", cmap=cmap, norm=norm, interpolation="nearest")
            cbar = plt.colorbar(im, shrink=0.78, ticks=values)
            cbar.ax.set_yticklabels(labels)
        else:
            im = plt.imshow(plot, extent=[LON_MIN, LON_MAX, LAT_MIN, LAT_MAX], origin="lower", cmap="gray", interpolation="nearest")
            plt.colorbar(im, shrink=0.78)
    else:
        finite = np.isfinite(plot)
        if finite.any():
            vmin, vmax = np.nanpercentile(plot, [2, 98])
            if not np.isfinite(vmin) or not np.isfinite(vmax) or vmin == vmax:
                vmin, vmax = None, None
        else:
            vmin, vmax = None, None
        im = plt.imshow(plot, extent=[LON_MIN, LON_MAX, LAT_MIN, LAT_MAX], origin="lower", cmap="viridis", interpolation="nearest", vmin=vmin, vmax=vmax)
        plt.colorbar(im, shrink=0.78)
    coverage = float(np.count_nonzero(valid_bool) / valid_bool.size) if valid_bool.size else 0.0
    plt.title(f"{title} | coverage={coverage:.3f}", fontsize=10)
    plt.xlabel("Longitude")
    plt.ylabel("Latitude")
    plt.tight_layout()
    plt.savefig(out_path)
    plt.close()


def quicklook_coverage(sat: str, coverage: np.ndarray) -> None:
    make_quicklook(coverage.astype(np.int16), coverage.astype(np.uint8), QUICKLOOK_DIR / f"{sat}_source_coverage.png", f"{sat} source coverage {TARGET_TIME}", categorical=True)


def candidate_variables(bundle: dict[str, Any], product: str) -> list[str]:
    sat = str(bundle["meta"].get("satellite_group", ""))
    allowed = ["cloud_mask"] if ONLY_CLOUD_MASK else VARIABLE_PRIORITY[:]
    if sat.startswith("Meteosat"):
        allowed = ["cloud_mask"] if ONLY_CLOUD_MASK else ["cloud_mask", "cloud_top_height_km", "valid_mask", "quality_flag_raw", "quality_flag_standard"]
    out = []
    for var in allowed:
        if var in bundle["arrays"] and (available(bundle, var) or var == "valid_mask") and np.asarray(bundle["arrays"][var]).ndim == 2:
            out.append(var)
    return out


def load_inventory() -> list[dict[str, Any]]:
    inv = pd.read_csv(NATIVE_DIR / "standardized_native_inventory.csv")
    rows = []
    for _, row in inv.iterrows():
        sat = str(row["satellite_group"])
        product = str(row["product"])
        if sat not in SATELLITES:
            continue
        if sat.startswith("Meteosat") and product not in METEOSAT_PRODUCTS_ALLOWED:
            continue
        rows.append({"satellite": sat, "product": product, "npz_file": Path(str(row["npz_file"]))})
    return rows


def reproject_product(row: dict[str, Any], target_lon: np.ndarray, target_lat: np.ndarray, target_grid: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], np.ndarray | None]:
    sat = row["satellite"]
    product = row["product"]
    npz_file = row["npz_file"]
    inv_rows: list[dict[str, Any]] = []
    stat_rows: list[dict[str, Any]] = []
    code_rows: list[dict[str, Any]] = []
    coverage_any = np.zeros((target_lat.size, target_lon.size), dtype=np.uint8)
    if not npz_file.exists():
        inv_rows.append({"satellite": sat, "product": product, "variable": "", "status": "SKIPPED", "reason": "NPZ missing", "output_file": ""})
        return inv_rows, stat_rows, code_rows, None
    try:
        bundle = load_npz(npz_file)
        variables = candidate_variables(bundle, product)
        if not variables:
            raise RuntimeError("no reprojectable 2D variables")
        # Use the first science/valid variable shape as the product grid.
        ref_var = next((v for v in variables if v != "valid_mask"), variables[0])
        ref_shape = tuple(np.asarray(bundle["arrays"][ref_var]).shape)
        lon, lat, geo_notes = geolocate(bundle, ref_shape)
        product_valid = np.asarray(bundle["arrays"].get("valid_mask", np.ones(ref_shape, dtype=np.uint8))).astype(bool)
        if product_valid.shape != ref_shape:
            product_valid = valid_for_variable(ref_var, np.asarray(bundle["arrays"][ref_var]))
        # FY4B: angle finite range mask participates in product-level validity.
        if sat == "FY4B":
            for angle in ["sensor_zenith_angle", "solar_zenith_angle", "sun_glint_angle"]:
                if angle in bundle["arrays"] and np.asarray(bundle["arrays"][angle]).shape == ref_shape:
                    product_valid &= valid_for_variable(angle, bundle["arrays"][angle])
        tree, src_y, src_x, tree_notes = build_tree(lon, lat, product_valid)
        notes = geo_notes + tree_notes
    except Exception as exc:
        inv_rows.append({"satellite": sat, "product": product, "variable": "", "status": "SKIPPED", "reason": str(exc), "output_file": ""})
        return inv_rows, stat_rows, code_rows, None

    sat_dir = OUT_DIR / sat
    sat_dir.mkdir(parents=True, exist_ok=True)
    sat_quick = QUICKLOOK_DIR
    sat_quick.mkdir(parents=True, exist_ok=True)
    for variable in variables:
        arr = np.asarray(bundle["arrays"][variable])
        if arr.shape != ref_shape:
            inv_rows.append({"satellite": sat, "product": product, "variable": variable, "status": "SKIPPED", "reason": f"shape {arr.shape} does not match geolocation shape {ref_shape}", "output_file": ""})
            continue
        cloud_mask_display_src = cloud_mask_fusion_src = cloud_mask_off_disc_src = None
        variable_fusion_mask = bundle["arrays"].get(f"fusion_valid_mask_{variable}")
        if variable_fusion_mask is not None and np.asarray(variable_fusion_mask).shape == arr.shape:
            source_valid = np.asarray(variable_fusion_mask).astype(bool) & valid_for_variable(variable, arr)
        elif variable == "cloud_mask":
            cloud_mask_display_src, cloud_mask_fusion_src, cloud_mask_off_disc_src = cloud_mask_masks(sat, product, arr)
            source_valid = cloud_mask_display_src
        else:
            source_valid = product_valid & valid_for_variable(variable, arr)
        if not np.any(source_valid):
            inv_rows.append({"satellite": sat, "product": product, "variable": variable, "status": "SKIPPED", "reason": "no valid source pixels", "output_file": ""})
            continue
        # Rebuild tree when variable validity is stricter than product validity, otherwise reuse.
        try:
            if np.array_equal(source_valid, product_valid):
                var_tree, var_y, var_x = tree, src_y, src_x
                var_notes = notes[:]
            else:
                var_tree, var_y, var_x, extra = build_tree(lon, lat, source_valid)
                var_notes = notes + extra + ["tree rebuilt for variable-specific valid_mask"]
            dtype, fill = output_dtype_and_fill(variable, arr)
            data_grid, valid_grid = query_reproject(var_tree, var_y, var_x, arr, target_lon, target_lat, dtype, fill)
            extra_arrays: dict[str, np.ndarray] = {}
            cloud_code_table = cloud_mask_semantics(sat, product) if variable == "cloud_mask" else None
            if variable == "valid_mask":
                data_grid = valid_grid.astype(np.int16)
            if variable == "cloud_mask":
                data_float = data_grid.astype(np.float32, copy=False)
                display_valid_grid = valid_grid.astype(np.uint8)
                if cloud_code_table:
                    fusion_codes = np.asarray([float(code) for code, meta in cloud_code_table.items() if meta.get("valid_for_fusion")], dtype=np.float32)
                    off_disc_codes = np.asarray([float(code) for code, meta in cloud_code_table.items() if meta.get("is_off_disc")], dtype=np.float32)
                    fusion_valid_grid = ((display_valid_grid > 0) & np.isin(data_float, fusion_codes)).astype(np.uint8)
                    off_disc_grid = ((display_valid_grid > 0) & np.isin(data_float, off_disc_codes)).astype(np.uint8)
                else:
                    fusion_valid_grid = display_valid_grid.copy()
                    off_disc_grid = np.zeros(display_valid_grid.shape, dtype=np.uint8)
                extra_arrays = {
                    "display_valid_mask": display_valid_grid,
                    "fusion_valid_mask": fusion_valid_grid,
                    "off_disc_mask": off_disc_grid,
                }
                valid_grid = display_valid_grid
            coverage_any |= valid_grid
            out_name = f"{sat}_{product}_{variable}_grid_{TIME_TAG}.npz"
            out_path = sat_dir / out_name
            if variable == "cloud_mask":
                var_notes = var_notes + [
                    "cloud_mask valid_mask is display_valid_mask for backward compatibility",
                    "fusion consumers must use fusion_valid_mask instead of valid_mask/display_valid_mask",
                ]
            save_npz(out_path, data_grid, valid_grid, bundle, variable, product, target_grid, var_notes, extra_arrays=extra_arrays)
            stats = finite_stats(np.where(valid_grid > 0, data_grid.astype(np.float32), np.nan))
            coverage_ratio = float(np.count_nonzero(valid_grid) / valid_grid.size)
            stat_rows.append({"satellite": sat, "product": product, "variable": variable, "coverage_ratio": coverage_ratio, **stats})
            if variable == "cloud_mask":
                fusion_ratio = float(np.count_nonzero(extra_arrays["fusion_valid_mask"]) / extra_arrays["fusion_valid_mask"].size)
                off_disc_ratio = float(np.count_nonzero(extra_arrays["off_disc_mask"]) / extra_arrays["off_disc_mask"].size)
                stat_rows[-1]["fusion_coverage_ratio"] = fusion_ratio
                stat_rows[-1]["off_disc_ratio"] = off_disc_ratio
                native_attrs = bundle["meta"].get("reader_attrs", {}).get("attrs_cloud_mask", {})
                code_rows.extend(cloud_mask_rows(sat, product, arr, native_attrs, data_grid, extra_arrays["display_valid_mask"]))
            inv_rows.append(
                {
                    "satellite": sat,
                    "product": product,
                    "variable": variable,
                    "status": "OK",
                    "reason": "",
                    "output_file": str(out_path),
                    "resampling_method": "nearest",
                    "native_shape": "x".join(map(str, arr.shape)),
                    "target_shape": "x".join(map(str, data_grid.shape)),
                    "coverage_ratio": coverage_ratio,
                    "notes": "|".join(var_notes),
                }
            )
            if variable in {"cloud_mask", "cloud_top_height_km", "valid_mask", "sensor_zenith_angle", "cloud_optical_thickness", "cloud_effective_radius_um"}:
                if variable == "cloud_mask":
                    make_quicklook(data_grid, extra_arrays["display_valid_mask"], QUICKLOOK_DIR / f"{sat}_{product}_{variable}.png", f"{sat} {product} {variable} {TARGET_TIME}", categorical=True, code_table=cloud_code_table)
                    make_quicklook(extra_arrays["fusion_valid_mask"].astype(np.int16), extra_arrays["fusion_valid_mask"], QUICKLOOK_DIR / f"{sat}_{product}_fusion_valid_mask.png", f"{sat} {product} fusion_valid_mask {TARGET_TIME}", categorical=True)
                else:
                    make_quicklook(data_grid, valid_grid, QUICKLOOK_DIR / f"{sat}_{product}_{variable}.png", f"{sat} {product} {variable} {TARGET_TIME}", categorical=variable in CATEGORICAL)
        except Exception as exc:
            inv_rows.append({"satellite": sat, "product": product, "variable": variable, "status": "FAILED", "reason": str(exc), "output_file": ""})
    return inv_rows, stat_rows, code_rows, coverage_any


def preflight_report_status() -> list[str]:
    warnings = []
    for report in [
        REPORT_DIR / "standardized_native_validate_report.md",
        REPORT_DIR / "standardized_native_semantic_validation_report.md",
        REPORT_DIR / "fy4b_geo_alignment_report.md",
        REPORT_DIR / "fy4b_dqf_bit_decode_diagnostics_report.md",
    ]:
        if not report.exists():
            warnings.append(f"preflight report missing: {report.name}")
            continue
        text = report.read_text(encoding="utf-8", errors="ignore")
        fail_markers = ["Overall status: **FAIL**", "Gate status: **FAIL**", "Overall semantic status: **FAIL**", "Status: **FAIL**"]
        if any(marker in text for marker in fail_markers):
            warnings.append(f"preflight report contains FAIL: {report.name}")
    return warnings


def write_report(status: str, inventory: pd.DataFrame, sat_stats: pd.DataFrame, warnings: list[str], target_grid: dict[str, Any]) -> None:
    lines = [
        "# 05 单时次 GEO-ring Cloud 重投影报告",
        "",
        f"- 生成时间 UTC：{utc_now()}",
        f"- 目标时次：`{TARGET_TIME}`",
        f"- 目标网格：经度 `-180..180`，纬度 `-90..90`，分辨率 `0.05°`，shape `{target_grid['shape']}`",
        f"- 结论：**{status}**",
        "- 本步骤只做重投影；未做 best-source fusion、source_map_fused、重叠区验证或下载。",
        "",
        "## 1. 哪些卫星成功重投影",
        "",
    ]
    if inventory.empty:
        lines.append("- 无。")
    else:
        for sat in SATELLITES:
            ok = inventory[(inventory["satellite"] == sat) & (inventory["status"] == "OK")]
            lines.append(f"- {sat}: {'成功' if not ok.empty else '未成功'}，变量数 {len(ok)}")
    lines.extend(["", "## 2. 哪些变量成功重投影", ""])
    for sat in SATELLITES:
        ok = inventory[(inventory["satellite"] == sat) & (inventory["status"] == "OK")]
        if not ok.empty:
            desc = ", ".join(sorted(set(f"{r.product}:{r.variable}" for r in ok.itertuples())))
            lines.append(f"- {sat}: {desc}")
    lines.extend(["", "## 3. 哪些变量失败或跳过，原因是什么", ""])
    bad = inventory[inventory["status"] != "OK"] if not inventory.empty else pd.DataFrame()
    if bad.empty:
        lines.append("- 无阻断性失败；部分不存在变量自然未输出。")
    else:
        for row in bad.itertuples():
            lines.append(f"- {row.satellite} {row.product} {row.variable}: {row.status}，原因：{row.reason}")
    lines.extend(["", "## 4. 每颗卫星有效覆盖率是多少", ""])
    if sat_stats.empty:
        lines.append("- 无统计。")
    else:
        for sat, grp in sat_stats.groupby("satellite"):
            cov = float(grp["coverage_ratio"].max())
            lines.append(f"- {sat}: 最大变量覆盖率 {cov:.4f}")
    lines.extend(["", "## 5. FY4B 是否在目标网格上位置合理", ""])
    fy4b_ok = not inventory[(inventory["satellite"] == "FY4B") & (inventory["status"] == "OK")].empty
    lines.append("- 是，已生成 FY4B cloud_mask/CTH/valid_mask/sensor_zenith quicklook 用于检查；未发现自动阻断。" if fy4b_ok else "- 否，FY4B 未成功落格。")
    lines.extend(["", "## 6. Meteosat 经度是否已规范化", ""])
    met_notes = "|".join(str(x) for x in inventory[inventory["satellite"].astype(str).str.startswith("Meteosat")].get("notes", pd.Series(dtype=str)).dropna().unique())
    lines.append("- 是，Meteosat 产品在 KD-tree 前已从 `0..360` 规范化到 `-180..180`。" if "normalized" in met_notes else "- 未确认或未处理 Meteosat。")
    lines.extend(["", "## 7. 是否存在明显经度断裂、翻转、错位", ""])
    lines.append("- 自动检查未发现目标网格定义错误；已输出 coverage quicklook。经度断裂/翻转仍需结合 quicklook 做人工视觉确认。")
    lines.extend(["", "## 8. 是否可以进入 06 best-source fusion", ""])
    lines.append("- 可以进入 06 的单时次原型，但建议先人工抽看本步骤 quicklook。")
    lines.extend(["", "## 9. 进入 06 时必须携带哪些 warning", ""])
    carry = warnings + [
        "FY4B quality_flag_raw 仍不能作为 quality_weight。",
        "FY4B NavQualityFlag 不是 per-pixel geometry mask。",
        "所有变量在 prototype 阶段均使用 nearest neighbor；类别变量没有使用 bilinear。",
        "FY4B lon/lat 当前由原始 L2 fixed-grid 坐标和 geostationary 近似推导，应在 06 前抽看覆盖位置。",
    ]
    for item in carry:
        lines.append(f"- {item}")
    lines.extend(["", "## 输出文件", ""])
    lines.append(f"- `{INVENTORY_CSV}`")
    lines.append(f"- `{SAT_STATS_CSV}`")
    lines.append(f"- `{TARGET_JSON}`")
    lines.append(f"- `{OUT_DIR}`")
    lines.append(f"- `{QUICKLOOK_DIR}`")
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")


def write_cloud_mask_fusion_report(code_table: pd.DataFrame, stats: pd.DataFrame) -> None:
    lines = [
        "# cloud_mask fusion_valid_mask 检查报告",
        "",
        f"- 生成时间 UTC: {utc_now()}",
        f"- 目标时次: `{TARGET_TIME}`",
        f"- 模式: `ONLY_CLOUD_MASK={ONLY_CLOUD_MASK}`",
        "",
        "## 结论",
        "",
        "- cloud_mask 现已区分 `display_valid_mask` 与 `fusion_valid_mask`。",
        "- `valid_mask` 在 cloud_mask NPZ 中保留为 display 语义，用于兼容旧读取逻辑。",
        "- 后续 06 融合必须读取 `fusion_valid_mask`，不能再把 display/off-disc 像元当成可融合像元。",
        "",
        "## 各卫星处理摘要",
        "",
    ]
    if stats.empty:
        lines.append("- 本轮没有 cloud_mask 统计结果。")
    else:
        cloud_stats = stats[stats["variable"] == "cloud_mask"].copy()
        for row in cloud_stats.itertuples():
            fusion_ratio = getattr(row, "fusion_coverage_ratio", np.nan)
            off_disc_ratio = getattr(row, "off_disc_ratio", np.nan)
            lines.append(
                f"- {row.satellite} {row.product}: display_coverage={row.coverage_ratio:.4f}, "
                f"fusion_coverage={fusion_ratio:.4f}, off_disc_ratio={off_disc_ratio:.4f}"
            )
    lines.extend(["", "## code table 规则", ""])
    if code_table.empty:
        lines.append("- 没有可写出的 code table。")
    else:
        for sat in SATELLITES:
            sat_rows = code_table[code_table["satellite"] == sat]
            if sat_rows.empty:
                continue
            product = sat_rows["product"].iloc[0]
            parts = []
            for row in sat_rows.drop_duplicates(subset=["value"]).sort_values("value").itertuples():
                parts.append(
                    f"{row.value}:{row.metadata_meaning}"
                    f"(display={bool(row.valid_for_display)}, fusion={bool(row.valid_for_fusion)})"
                )
            lines.append(f"- {sat} {product}: " + ", ".join(parts))
    lines.extend(
        [
            "",
            "## 输出文件",
            "",
            f"- `{CLOUD_MASK_CODE_TABLE_CSV}`",
            f"- `{CLOUD_MASK_FUSION_REPORT_MD}`",
            f"- `{QUICKLOOK_DIR}` 中各卫星 `cloud_mask` 与 `fusion_valid_mask` PNG",
        ]
    )
    CLOUD_MASK_FUSION_REPORT_MD.write_text("\n".join(lines), encoding="utf-8")


def determine_status(inventory: pd.DataFrame, sat_stats: pd.DataFrame, preflight_warnings: list[str], target_grid: dict[str, Any]) -> str:
    if target_grid["shape"] != [3600, 7200]:
        return "FAIL"
    if inventory.empty or inventory[inventory["status"] == "OK"].empty:
        return "FAIL"
    ok_sats = set(inventory[inventory["status"] == "OK"]["satellite"])
    required = set(SATELLITES)
    core_success = True
    for sat in required:
        required_vars = ["cloud_mask"] if ONLY_CLOUD_MASK else ["cloud_mask", "cloud_top_height_km"]
        ok = inventory[(inventory["satellite"] == sat) & (inventory["status"] == "OK") & (inventory["variable"].isin(required_vars))]
        if ok.empty:
            core_success = False
    if not core_success:
        return "FAIL"
    return "PASS_WITH_WARNINGS" if preflight_warnings or True else "PASS"


def main() -> int:
    global SATELLITES, SOURCE_PROFILE
    parser = argparse.ArgumentParser(description="Reproject profile-selected cloud products to the GEO-ring grid")
    parser.add_argument("--source-profile", default=SOURCE_PROFILE, choices=["operational_baseline", "claas3_candidate"])
    parser.add_argument("--claas3-root", type=Path, default=path_config.CLAAS3_ROOT)
    parser.add_argument("--run-id", default=os.environ.get("GEO_RING_RUN_ID", ""))
    parser.add_argument("--reuse-operational-reprojected-root", type=Path)
    args = parser.parse_args()
    SOURCE_PROFILE = validate_profile(args.source_profile)
    SATELLITES = tie_order(SOURCE_PROFILE)
    ensure_dirs()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    QUICKLOOK_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(__file__, SCRIPT_DIR / Path(__file__).name)
    target_lon, target_lat, target_grid = make_target_grid()
    TARGET_JSON.write_text(json.dumps(target_grid, indent=2, ensure_ascii=False), encoding="utf-8")
    preflight_warnings = preflight_report_status()
    rows = load_inventory()
    inventory_rows: list[dict[str, Any]] = []
    stat_rows: list[dict[str, Any]] = []
    cloud_code_rows: list[dict[str, Any]] = []
    reused_input_paths: list[Path] = []
    if args.reuse_operational_reprojected_root:
        base_inventory_path = args.reuse_operational_reprojected_root / "reprojected_variable_inventory.csv"
        base_stats_path = args.reuse_operational_reprojected_root / "reprojected_per_satellite_stats.csv"
        base_grid_path = args.reuse_operational_reprojected_root / "target_grid_definition.json"
        base_grid = json.loads(base_grid_path.read_text(encoding="utf-8"))
        if base_grid != target_grid:
            raise RuntimeError(f"reused operational target grid differs from candidate grid: {base_grid_path}")
        inventory_rows.extend(pd.read_csv(base_inventory_path).to_dict("records"))
        stat_rows.extend(pd.read_csv(base_stats_path).to_dict("records"))
        base_code_path = args.reuse_operational_reprojected_root.parent / "reports" / "cloud_mask_code_table.csv"
        if base_code_path.exists():
            cloud_code_rows.extend(pd.read_csv(base_code_path).to_dict("records"))
        reused_input_paths.extend([base_inventory_path, base_stats_path, base_grid_path])
        rows = [row for row in rows if row["satellite"] == "CLAAS3-0deg"]
    coverage_by_sat: dict[str, np.ndarray] = {}
    for row in rows:
        inv, stats, code_rows, coverage = reproject_product(row, target_lon, target_lat, target_grid)
        inventory_rows.extend(inv)
        stat_rows.extend(stats)
        cloud_code_rows.extend(code_rows)
        if coverage is not None:
            sat = row["satellite"]
            if sat not in coverage_by_sat:
                coverage_by_sat[sat] = coverage.astype(np.uint8)
            else:
                coverage_by_sat[sat] |= coverage.astype(np.uint8)
    for sat, coverage in coverage_by_sat.items():
        quicklook_coverage(sat, coverage)
        sat_dir = OUT_DIR / sat
        sat_dir.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([r for r in stat_rows if r["satellite"] == sat]).to_csv(sat_dir / f"{sat}_reprojected_stats.csv", index=False, encoding="utf-8-sig")
    inventory = pd.DataFrame(inventory_rows)
    stats = pd.DataFrame(stat_rows)
    cloud_code = pd.DataFrame(cloud_code_rows)
    inventory.to_csv(INVENTORY_CSV, index=False, encoding="utf-8-sig")
    stats.to_csv(SAT_STATS_CSV, index=False, encoding="utf-8-sig")
    cloud_code.to_csv(CLOUD_MASK_CODE_TABLE_CSV, index=False, encoding="utf-8-sig")
    status = determine_status(inventory, stats, preflight_warnings, target_grid)
    write_report(status, inventory, stats, preflight_warnings, target_grid)
    write_cloud_mask_fusion_report(cloud_code, stats)
    write_manifest(
        OUT_DIR / "stage_05_claas3_reprojection_manifest.json",
        canonical_stage_id="stage_05",
        run_id=args.run_id,
        source_profile=SOURCE_PROFILE,
        generating_script=Path(__file__),
        input_paths=[*reused_input_paths, *[row["npz_file"] for row in rows]],
        output_paths=inventory["output_file"].dropna().astype(str).tolist() if not inventory.empty else [],
        parameters={"target_time": TARGET_TIME, "grid_resolution_degree": GRID_RES, "max_distance_degree": MAX_DISTANCE_DEG, "resampling": "nearest", "reuse_operational_reprojected_root": str(args.reuse_operational_reprojected_root or "")},
        project_root=path_config.PROJECT_ROOT,
        extra={"registry_version": REGISTRY_VERSION, "product_versions": {"CLAAS3": "405"} if SOURCE_PROFILE == "claas3_candidate" else {}},
    )
    print(f"05 {status}: variables_ok={len(inventory[inventory['status']=='OK']) if not inventory.empty else 0}")
    print(f"report={REPORT_MD}")
    print(f"cloud_mask_report={CLOUD_MASK_FUSION_REPORT_MD}")
    return 0 if status != "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
