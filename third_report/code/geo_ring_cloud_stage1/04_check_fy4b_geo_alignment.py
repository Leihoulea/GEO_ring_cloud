from __future__ import annotations

import json
import math
import shutil
from pathlib import Path
from typing import Any

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from geo_ring_cloud.diagnostics.summary import finite_stats
from geo_ring_cloud.lineage import utc_now
from geo_ring_cloud.pipeline_layout import REPORT_DIR, SCRIPT_DIR, ensure_pipeline_directories as ensure_dirs


NATIVE_DIR = Path(r"D:\AAAresearch_paper\geo_ring_cloud_stage1\standardized_native")
TARGET_TIME = "2024-03-05T00:00:00Z"
TIME_TAG = "20240305_0000"
EXPECTED_SHAPE = (2748, 2748)

PRODUCTS = ["CLM", "CLP", "CLT", "CTH", "CTT", "CTP", "GEO"]
CORE_VAR_BY_PRODUCT = {
    "CLM": "cloud_mask",
    "CLP": "cloud_phase",
    "CLT": "cloud_type",
    "CTH": "cloud_top_height_km",
    "CTT": "cloud_top_temperature_K",
    "CTP": "cloud_top_pressure_hPa",
}
GEO_ANGLE_VARS = [
    "sensor_zenith_angle",
    "sensor_azimuth_angle",
    "solar_zenith_angle",
    "solar_azimuth_angle",
    "sun_glint_angle",
]
SHAPE_CHECK_VARS = list(CORE_VAR_BY_PRODUCT.values()) + GEO_ANGLE_VARS + ["nav_quality_flag"]
SUSPECT_CODES = {-128, 126, 127, 255, 32767, 65535}


def npz_path(product: str) -> Path:
    return NATIVE_DIR / f"FY4B_{product}_{TIME_TAG}_native_cloud_v0.npz"


def load_npz(path: Path) -> dict[str, Any]:
    z = np.load(path, allow_pickle=False)
    meta = json.loads(str(z["metadata_json"]))
    avail = json.loads(str(z["variable_availability_json"]))
    arrays = {name: np.asarray(z[name]) for name in z.files if not name.endswith("_json")}
    z.close()
    return {"metadata": meta, "availability": avail, "arrays": arrays}


def is_available(bundle: dict[str, Any], name: str) -> bool:
    return bool(bundle["availability"].get(f"has_{name}", False))


def shape_text(shape: tuple[int, ...] | None) -> str:
    if shape is None:
        return ""
    return "x".join(str(x) for x in shape)


def variable_shape(bundle: dict[str, Any], name: str) -> tuple[int, ...] | None:
    if not is_available(bundle, name):
        return None
    return tuple(np.asarray(bundle["arrays"][name]).shape)


def finite_mask(arr: np.ndarray) -> np.ndarray:
    a = np.asarray(arr)
    if a.dtype.kind == "f":
        mask = np.isfinite(a)
        for code in SUSPECT_CODES:
            mask &= ~np.isclose(a, float(code))
        return mask
    if a.dtype.kind in "iu":
        mask = np.ones(a.shape, dtype=bool)
        for code in SUSPECT_CODES:
            mask &= a != code
        return mask
    return np.ones(a.shape, dtype=bool)


def downsample(arr: np.ndarray, max_pixels: int = 1_200_000) -> np.ndarray:
    a = np.asarray(arr)
    if a.ndim != 2:
        return a
    stride = max(1, int(math.ceil(math.sqrt(a.size / max_pixels)))) if a.size > max_pixels else 1
    return a[::stride, ::stride]


def masked_for_plot(arr: np.ndarray, categorical: bool = False) -> np.ndarray:
    a = np.asarray(arr).astype(np.float32, copy=True)
    if categorical:
        for code in SUSPECT_CODES:
            a[np.isclose(a, float(code))] = np.nan
    else:
        a[~np.isfinite(a)] = np.nan
        for code in SUSPECT_CODES:
            a[np.isclose(a, float(code))] = np.nan
    return a


def quicklook_continuous(arr: np.ndarray, out: Path, title: str, cmap: str = "viridis") -> None:
    a = downsample(masked_for_plot(arr, categorical=False))
    finite = np.isfinite(a)
    if finite.any():
        vmin, vmax = np.nanpercentile(a, [2, 98])
        if not np.isfinite(vmin) or not np.isfinite(vmax) or vmin == vmax:
            vmin, vmax = None, None
    else:
        vmin, vmax = None, None
    plt.figure(figsize=(8, 5), dpi=150)
    im = plt.imshow(a, cmap=cmap, interpolation="nearest", vmin=vmin, vmax=vmax)
    plt.title(title, fontsize=10)
    plt.axis("off")
    plt.colorbar(im, shrink=0.75)
    plt.tight_layout()
    plt.savefig(out)
    plt.close()


def quicklook_categorical(arr: np.ndarray, out: Path, title: str) -> None:
    a = downsample(masked_for_plot(arr, categorical=True))
    plt.figure(figsize=(8, 5), dpi=150)
    im = plt.imshow(a, cmap="tab20", interpolation="nearest")
    plt.title(title, fontsize=10)
    plt.axis("off")
    plt.colorbar(im, shrink=0.75)
    plt.tight_layout()
    plt.savefig(out)
    plt.close()


def quicklook_nav_quality(nav: np.ndarray | None, out: Path) -> str:
    if nav is None:
        plt.figure(figsize=(7, 4), dpi=150)
        plt.text(0.5, 0.5, "NavQualityFlag not available", ha="center", va="center")
        plt.axis("off")
        plt.tight_layout()
        plt.savefig(out)
        plt.close()
        return "NavQualityFlag not available"
    arr = np.asarray(nav)
    if arr.ndim == 2:
        quicklook_categorical(arr, out, "FY4B NavQualityFlag")
        return "NavQualityFlag is 2D"
    values, counts = np.unique(arr, return_counts=True)
    plt.figure(figsize=(7, 4), dpi=150)
    plt.bar([str(v) for v in values], counts)
    plt.title(f"FY4B NavQualityFlag shape={arr.shape}")
    plt.xlabel("code")
    plt.ylabel("count")
    plt.tight_layout()
    plt.savefig(out)
    plt.close()
    return f"NavQualityFlag is not per-pixel; shape={arr.shape}"


def overlay_contours(base: np.ndarray, contour: np.ndarray, out: Path, title: str, categorical: bool = False, cmap: str = "viridis") -> None:
    b = downsample(masked_for_plot(base, categorical=categorical))
    c = downsample(masked_for_plot(contour, categorical=False))
    plt.figure(figsize=(8, 5), dpi=150)
    finite = np.isfinite(b)
    if finite.any() and not categorical:
        vmin, vmax = np.nanpercentile(b, [2, 98])
    else:
        vmin, vmax = None, None
    im = plt.imshow(b, cmap=("tab20" if categorical else cmap), interpolation="nearest", vmin=vmin, vmax=vmax)
    if c.shape == b.shape and np.isfinite(c).any():
        levels = np.nanpercentile(c, [20, 40, 60, 80])
        if np.unique(levels).size > 1:
            plt.contour(c, levels=levels, colors="black", linewidths=0.45, alpha=0.75)
    plt.title(title, fontsize=10)
    plt.axis("off")
    plt.colorbar(im, shrink=0.75)
    plt.tight_layout()
    plt.savefig(out)
    plt.close()


def phase_type_quicklook(phase: np.ndarray, cloud_type: np.ndarray, valid: np.ndarray, out: Path) -> None:
    p = downsample(masked_for_plot(phase, categorical=True))
    t = downsample(masked_for_plot(cloud_type, categorical=True))
    v = downsample(np.asarray(valid).astype(np.float32))
    fig, axes = plt.subplots(1, 2, figsize=(12, 5), dpi=150)
    for ax, arr, title in [(axes[0], p, "Cloud phase + valid boundary"), (axes[1], t, "Cloud type + valid boundary")]:
        im = ax.imshow(arr, cmap="tab20", interpolation="nearest")
        if v.shape == arr.shape:
            ax.contour(v, levels=[0.5], colors="black", linewidths=0.5)
        ax.set_title(title, fontsize=9)
        ax.axis("off")
        fig.colorbar(im, ax=ax, shrink=0.7)
    plt.tight_layout()
    plt.savefig(out)
    plt.close()


def load_all_products() -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    bundles: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    for product in PRODUCTS:
        path = npz_path(product)
        status = "OK"
        notes: list[str] = []
        native_shape = ""
        available_vars: list[str] = []
        nominal_time = ""
        if not path.exists():
            status = "MISSING"
            notes.append("NPZ file missing")
        else:
            try:
                bundle = load_npz(path)
                bundles[product] = bundle
                nominal_time = str(bundle["metadata"].get("nominal_time", ""))
                available_vars = sorted(k.replace("has_", "") for k, v in bundle["availability"].items() if v)
                shapes = [np.asarray(bundle["arrays"][v]).shape for v in available_vars if v in bundle["arrays"] and np.asarray(bundle["arrays"][v]).ndim >= 2]
                native_shape = shape_text(tuple(shapes[0])) if shapes else ""
                if nominal_time != TARGET_TIME:
                    status = "TIME_MISMATCH"
                    notes.append(f"expected {TARGET_TIME}")
            except Exception as exc:
                status = "READ_FAILED"
                notes.append(str(exc))
        rows.append(
            {
                "source_product": product,
                "file_path": str(path),
                "nominal_time": nominal_time,
                "native_shape": native_shape,
                "available_variables": "|".join(available_vars),
                "status": status,
                "notes": "|".join(notes),
            }
        )
    return bundles, rows


def shape_checks(bundles: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    ok = True
    for product, variable in CORE_VAR_BY_PRODUCT.items():
        bundle = bundles.get(product)
        shp = variable_shape(bundle, variable) if bundle else None
        status = "PASS" if shp == EXPECTED_SHAPE else "FAIL"
        if status == "FAIL":
            ok = False
        rows.append(
            {
                "source_product": product,
                "variable": variable,
                "shape": shape_text(shp),
                "expected_shape": shape_text(EXPECTED_SHAPE),
                "status": status,
                "notes": "core cloud variable",
            }
        )
    geo = bundles.get("GEO")
    for variable in GEO_ANGLE_VARS:
        shp = variable_shape(geo, variable) if geo else None
        status = "PASS" if shp == EXPECTED_SHAPE else "FAIL"
        if status == "FAIL":
            ok = False
        rows.append(
            {
                "source_product": "GEO",
                "variable": variable,
                "shape": shape_text(shp),
                "expected_shape": shape_text(EXPECTED_SHAPE),
                "status": status,
                "notes": "GEO angle variable",
            }
        )
    nav = None
    if geo and is_available(geo, "quality_flag_raw"):
        nav = np.asarray(geo["arrays"]["quality_flag_raw"])
    if nav is not None:
        rows.append(
            {
                "source_product": "GEO",
                "variable": "NavQualityFlag",
                "shape": shape_text(tuple(nav.shape)),
                "expected_shape": shape_text(EXPECTED_SHAPE),
                "status": "WARN" if nav.shape != EXPECTED_SHAPE else "PASS",
                "notes": "not required for grid alignment; per-pixel use only if 2D",
            }
        )
    return rows, ok


def angle_stats_and_tests(geo: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    tests: dict[str, Any] = {}
    for variable in GEO_ANGLE_VARS:
        arr = np.asarray(geo["arrays"][variable])
        valid = finite_mask(arr)
        stats = finite_stats(np.where(valid, arr, np.nan).astype(np.float32))
        center = arr[arr.shape[0] // 2 - 100 : arr.shape[0] // 2 + 100, arr.shape[1] // 2 - 100 : arr.shape[1] // 2 + 100]
        yy, xx = np.indices(arr.shape)
        cy = (arr.shape[0] - 1) / 2.0
        cx = (arr.shape[1] - 1) / 2.0
        radius = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
        rnorm = radius / np.nanmax(radius)
        edge = arr[(rnorm > 0.62) & (rnorm < 0.72) & valid]
        center_valid = center[finite_mask(center)]
        center_median = float(np.nanmedian(center_valid)) if center_valid.size else np.nan
        edge_median = float(np.nanmedian(edge)) if edge.size else np.nan
        rows.append(
            {
                "variable": variable,
                **stats,
                "finite_ratio": float(np.count_nonzero(valid) / valid.size),
                "center_median": center_median,
                "outer_disk_median": edge_median,
            }
        )
    szen = np.asarray(geo["arrays"]["sensor_zenith_angle"])
    szen_valid = finite_mask(szen)
    yy, xx = np.indices(szen.shape)
    min_idx = np.nanargmin(np.where(szen_valid, szen, np.nan))
    min_y, min_x = np.unravel_index(min_idx, szen.shape)
    cy = (szen.shape[0] - 1) / 2.0
    cx = (szen.shape[1] - 1) / 2.0
    min_dist_px = float(np.sqrt((min_y - cy) ** 2 + (min_x - cx) ** 2))
    center_med = next(row["center_median"] for row in rows if row["variable"] == "sensor_zenith_angle")
    edge_med = next(row["outer_disk_median"] for row in rows if row["variable"] == "sensor_zenith_angle")
    tests["sensor_zenith_min_near_center"] = min_dist_px < 0.08 * szen.shape[0]
    tests["sensor_zenith_edge_larger_than_center"] = np.isfinite(center_med) and np.isfinite(edge_med) and edge_med > center_med + 15
    tests["sensor_zenith_min_location"] = {"y": int(min_y), "x": int(min_x), "distance_px": min_dist_px}
    solz = np.asarray(geo["arrays"]["solar_zenith_angle"])
    solz_valid = finite_mask(solz)
    solz_finite = solz[solz_valid]
    tests["solar_zenith_has_day_night_gradient"] = bool(solz_finite.size and np.nanmin(solz_finite) < 80 and np.nanmax(solz_finite) > 100)
    glint = np.asarray(geo["arrays"]["sun_glint_angle"])
    glint_valid = finite_mask(glint)
    gy, gx = np.gradient(np.where(glint_valid, glint, np.nan).astype(np.float32))
    grad = np.sqrt(gx * gx + gy * gy)
    tests["sun_glint_spatially_continuous"] = bool(np.isfinite(grad).any() and np.nanmedian(grad) < 2.0)
    return rows, tests


def quality_counts(bundles: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for product, bundle in bundles.items():
        if not is_available(bundle, "quality_flag_raw"):
            continue
        arr = np.asarray(bundle["arrays"]["quality_flag_raw"])
        if arr.ndim == 0:
            continue
        vals, counts = np.unique(arr, return_counts=True)
        order = np.argsort(counts)[::-1]
        for idx in order[:256]:
            value = vals[idx].item() if hasattr(vals[idx], "item") else vals[idx]
            try:
                is_suspect = bool(np.isfinite(float(value)) and int(float(value)) in SUSPECT_CODES)
            except Exception:
                is_suspect = False
            rows.append(
                {
                    "product": product,
                    "variable": "quality_flag_raw" if product != "GEO" else "NavQualityFlag",
                    "value": value,
                    "count": int(counts[idx]),
                    "is_suspect_code": is_suspect,
                    "note": "QUALITY_MAPPING_UNVERIFIED" if product != "GEO" else "NAV_QUALITY_RAW",
                }
            )
    return rows


def write_quality_note() -> None:
    text = """# FY4B Quality Mapping Note

- Status: `QUALITY_MAPPING_UNVERIFIED`.
- FY4B `quality_flag_raw` is preserved as the raw source value in step 04.
- It is not mapped to `quality_weight` and must not participate in rating yet.
- The current samples show non-DQF-like ranges for several FY4B products, so official product documentation or a data dictionary is required before semantic quality weighting.
- `NavQualityFlag`, when present in the standardized GEO product, is checked as raw navigation metadata; it is only usable as a per-pixel geometry mask if it is present as a 2D field on the native cloud grid.
"""
    (REPORT_DIR / "fy4b_quality_mapping_note.md").write_text(text, encoding="utf-8")


def make_outputs(bundles: dict[str, dict[str, Any]]) -> None:
    geo = bundles["GEO"]
    for variable, filename in [
        ("sensor_zenith_angle", "fy4b_sensor_zenith_quicklook.png"),
        ("solar_zenith_angle", "fy4b_solar_zenith_quicklook.png"),
        ("sun_glint_angle", "fy4b_sun_glint_quicklook.png"),
    ]:
        quicklook_continuous(np.asarray(geo["arrays"][variable]), REPORT_DIR / filename, f"FY4B {variable} {TARGET_TIME}")
    nav = np.asarray(geo["arrays"]["quality_flag_raw"]) if is_available(geo, "quality_flag_raw") else None
    quicklook_nav_quality(nav, REPORT_DIR / "fy4b_nav_quality_quicklook.png")
    overlay_contours(
        bundles["CLM"]["arrays"]["cloud_mask"],
        geo["arrays"]["sensor_zenith_angle"],
        REPORT_DIR / "fy4b_cloud_geo_overlay_cloud_mask.png",
        "FY4B cloud_mask + sensor_zenith contours",
        categorical=True,
    )
    overlay_contours(
        bundles["CTH"]["arrays"]["cloud_top_height_km"],
        geo["arrays"]["sensor_zenith_angle"],
        REPORT_DIR / "fy4b_cloud_geo_overlay_cth.png",
        "FY4B CTH + sensor_zenith contours",
    )
    overlay_contours(
        bundles["CTP"]["arrays"]["cloud_top_pressure_hPa"],
        geo["arrays"]["solar_zenith_angle"],
        REPORT_DIR / "fy4b_cloud_geo_overlay_ctp.png",
        "FY4B CTP + solar_zenith contours",
    )
    phase_type_quicklook(
        bundles["CLP"]["arrays"]["cloud_phase"],
        bundles["CLT"]["arrays"]["cloud_type"],
        bundles["CLP"]["arrays"]["valid_mask"],
        REPORT_DIR / "fy4b_cloud_geo_overlay_phase_type.png",
    )


def report_status(shape_ok: bool, tests: dict[str, Any], nav_note: str) -> tuple[str, list[str], list[str]]:
    fail_reasons: list[str] = []
    warnings: list[str] = []
    if not shape_ok:
        fail_reasons.append("At least one FY4B cloud/GEO core variable shape is not 2748x2748.")
    for key in ["sensor_zenith_min_near_center", "sensor_zenith_edge_larger_than_center", "solar_zenith_has_day_night_gradient", "sun_glint_spatially_continuous"]:
        if not tests.get(key, False):
            fail_reasons.append(f"Angle physical check failed: {key}.")
    warnings.append("FY4B quality_flag_raw semantic mapping is unverified; keep raw values only and do not use for rating.")
    if "not per-pixel" in nav_note or "not available" in nav_note:
        warnings.append(nav_note + "; it cannot be used as a per-pixel geometry valid mask in step 05.")
    status = "FAIL" if fail_reasons else ("PASS_WITH_WARNINGS" if warnings else "PASS")
    return status, fail_reasons, warnings


def write_report(status: str, fail_reasons: list[str], warnings: list[str], tests: dict[str, Any], nav_note: str) -> None:
    lines = [
        "# FY4B GEO Alignment Report",
        "",
        f"- Generated UTC: {utc_now()}",
        f"- Prototype time: `{TARGET_TIME}`",
        f"- Gate status: **{status}**",
        "- Scope: FY4B only; no download, no reprojection, no fusion.",
        "",
        "## File And Shape Decision",
        "",
        "- FY4B L2 cloud products and GEO angle fields are required to be `2748x2748`.",
        "- See `fy4b_geo_alignment_file_match.csv` and `fy4b_geo_alignment_shape_check.csv`.",
        "",
        "## Physical Angle Checks",
        "",
        f"- sensor_zenith_min_near_center: `{tests.get('sensor_zenith_min_near_center')}`; min_location={tests.get('sensor_zenith_min_location')}",
        f"- sensor_zenith_edge_larger_than_center: `{tests.get('sensor_zenith_edge_larger_than_center')}`",
        f"- solar_zenith_has_day_night_gradient: `{tests.get('solar_zenith_has_day_night_gradient')}`",
        f"- sun_glint_spatially_continuous: `{tests.get('sun_glint_spatially_continuous')}`",
        f"- NavQualityFlag: {nav_note}",
        "",
        "## Direction / Offset Assessment",
        "",
    ]
    if status == "FAIL":
        lines.append("- Not cleared because at least one blocking check failed.")
    else:
        lines.append("- No automatic evidence of gross up/down flip, left/right flip, or cloud/GEO disk-boundary mismatch was found.")
        lines.append("- Overlay quicklooks were generated for manual visual confirmation.")
    lines.extend(["", "## Quality Flag Handling", ""])
    lines.append("- `QUALITY_MAPPING_UNVERIFIED`: FY4B `quality_flag_raw` is preserved but not mapped to mature `quality_weight`.")
    lines.append("- See `fy4b_quality_raw_code_counts.csv` and `fy4b_quality_mapping_note.md`.")
    lines.extend(["", "## Gate To 05", ""])
    if status == "FAIL":
        lines.append("- FY4B must not enter 05 reproject until blocking reasons are fixed.")
    else:
        lines.append("- FY4B can enter 05 reproject for the prototype time.")
        lines.append("- Carry warnings: quality_flag_raw cannot be used for rating; NavQualityFlag is not per-pixel unless documented otherwise.")
    if fail_reasons:
        lines.extend(["", "## Blocking Reasons", ""])
        lines.extend(f"- {item}" for item in fail_reasons)
    if warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {item}" for item in warnings)
    lines.extend(
        [
            "",
            "## Output Files",
            "",
            "- `fy4b_geo_alignment_file_match.csv`",
            "- `fy4b_geo_alignment_shape_check.csv`",
            "- `fy4b_geo_angle_stats.csv`",
            "- `fy4b_quality_raw_code_counts.csv`",
            "- `fy4b_quality_mapping_note.md`",
            "- `fy4b_*_quicklook.png` and `fy4b_cloud_geo_overlay_*.png`",
        ]
    )
    (REPORT_DIR / "fy4b_geo_alignment_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ensure_dirs()
    shutil.copy2(__file__, SCRIPT_DIR / Path(__file__).name)
    bundles, file_rows = load_all_products()
    pd.DataFrame(file_rows).to_csv(REPORT_DIR / "fy4b_geo_alignment_file_match.csv", index=False, encoding="utf-8-sig")
    if any(row["status"] != "OK" for row in file_rows):
        shape_rows: list[dict[str, Any]] = []
        angle_rows: list[dict[str, Any]] = []
        quality_rows: list[dict[str, Any]] = []
        status = "FAIL"
        fail_reasons = ["One or more FY4B input NPZ files are missing, unreadable, or time-mismatched."]
        warnings: list[str] = []
        tests: dict[str, Any] = {}
        nav_note = "not checked"
    else:
        shape_rows, shape_ok = shape_checks(bundles)
        angle_rows, tests = angle_stats_and_tests(bundles["GEO"])
        quality_rows = quality_counts(bundles)
        make_outputs(bundles)
        nav = np.asarray(bundles["GEO"]["arrays"]["quality_flag_raw"]) if is_available(bundles["GEO"], "quality_flag_raw") else None
        nav_note = "NavQualityFlag not available" if nav is None else (f"NavQualityFlag shape={nav.shape}; not per-pixel" if nav.ndim != 2 else "NavQualityFlag is 2D")
        status, fail_reasons, warnings = report_status(shape_ok, tests, nav_note)
    pd.DataFrame(shape_rows).to_csv(REPORT_DIR / "fy4b_geo_alignment_shape_check.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(angle_rows).to_csv(REPORT_DIR / "fy4b_geo_angle_stats.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(quality_rows).to_csv(REPORT_DIR / "fy4b_quality_raw_code_counts.csv", index=False, encoding="utf-8-sig")
    write_quality_note()
    write_report(status, fail_reasons, warnings, tests, nav_note)
    print(f"04 {status}: report={REPORT_DIR / 'fy4b_geo_alignment_report.md'}")
    return 0 if status != "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
