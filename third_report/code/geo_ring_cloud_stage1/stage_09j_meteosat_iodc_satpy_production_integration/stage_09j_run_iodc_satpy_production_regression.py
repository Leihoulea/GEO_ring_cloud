from __future__ import annotations

import csv
import hashlib
import json
import math
import sys
import zipfile
from pathlib import Path
from typing import Any

import numpy as np

CODE_ROOT_FOR_IMPORT = Path(__file__).resolve().parents[1]
if str(CODE_ROOT_FOR_IMPORT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT_FOR_IMPORT))

from geo_ring_cloud.adapters.cloud_products import (  # noqa: E402
    IODC_CLM_AREA_ID,
    IODC_CLM_NAVIGATION_SCHEMA_VERSION,
    IODC_CLM_NAVIGATION_SOURCE,
    IODC_CLM_READER_BACKEND,
    IODC_CLM_SHAPE,
    IODC_CLM_SUBSATELLITE_LONGITUDE,
    attr_to_python,
    mask_sentinel_values,
    product_mapping_key,
    read_mapping,
    read_product,
    reshape_square_if_needed,
    resolve_variable_names,
)
from geo_ring_cloud.lineage import write_manifest  # noqa: E402
from geo_ring_cloud.paths import EXTERNAL_EPIC_L2_ROOT, EXTERNAL_GEO_CLOUD_ROOT, PROJECT_ROOT, RUNS_ROOT  # noqa: E402
from geo_ring_cloud.pipeline_layout import STAGE_ROOT  # noqa: E402


PROJECT_ID = "geo_ring_cloud"
STAGE_ID = "stage_09j"
COMPONENT_ROLE = "production_regression"
OUTPUT_ROOT = RUNS_ROOT / "stage_09j_meteosat_iodc_satpy_production_integration_202403"
IODC_ROOT = EXTERNAL_GEO_CLOUD_ROOT / "Meteosat-IODC" / "CLM"
EPIC_ROOT = EXTERNAL_EPIC_L2_ROOT
STAGE09I_BASELINE = RUNS_ROOT / "stage_09i_remaining_cloud_reader_audit" / "iodc_epic_comparison.csv"
ROI_METERS = 6000
CASES = [
    ("20240306_1300", "20240306130000", "DSCOVR_EPIC_L2_CLOUD_03_20240306125407_03.nc4", True),
    ("20240306_1100", "20240306110000", "DSCOVR_EPIC_L2_CLOUD_03_20240306110605_03.nc4", False),
    ("20240306_1400", "20240306140000", "DSCOVR_EPIC_L2_CLOUD_03_20240306144209_03.nc4", False),
]

def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def case_zip(timestamp: str) -> Path:
    day = timestamp[:8]
    hour = timestamp[8:10]
    name = f"MSG2-SEVI-MSGCLMK-0100-0100-{timestamp}.000000000Z-NA.zip"
    return IODC_ROOT / day / hour / name


def category_hash(values: np.ndarray) -> str:
    arr = np.asarray(values)
    norm = np.full(arr.shape, -32768, dtype=np.int16)
    finite = np.isfinite(arr)
    norm[finite] = np.rint(arr[finite]).astype(np.int16)
    return hashlib.sha256(norm.tobytes()).hexdigest()


def class_counts(values: np.ndarray) -> dict[str, int]:
    arr = np.asarray(values)
    finite = np.isfinite(arr)
    unique, counts = np.unique(np.rint(arr[finite]).astype(np.int16), return_counts=True)
    return {str(int(k)): int(v) for k, v in zip(unique, counts)}


def read_legacy_cfgrib_mask_and_navigation(path: Path, mapping: dict[str, dict[str, list[str]]]) -> dict[str, np.ndarray]:
    import xarray as xr

    product_map = mapping.get(product_mapping_key("Meteosat", "CLM"), {})
    extract_cache = STAGE_ROOT / "cache" / "stage09j_legacy_cfgrib_negative_control"
    extract_cache.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as zf:
        entries = [e for e in zf.namelist() if e.lower().endswith((".grb", ".grib", ".grb2", ".bin"))]
        if not entries:
            raise RuntimeError(f"no GRIB entry found in {path}")
        entry = entries[0]
        payload = zf.read(entry)
        key = hashlib.sha1(f"{path.resolve()}|{entry}|legacy".encode("utf-8")).hexdigest()
        extracted = extract_cache / key / Path(entry).name
        extracted.parent.mkdir(parents=True, exist_ok=True)
        if not extracted.exists() or extracted.stat().st_size != len(payload):
            extracted.write_bytes(payload)
    ds = xr.open_dataset(extracted, engine="cfgrib", backend_kwargs={"indexpath": ""})
    try:
        resolved = resolve_variable_names(list(ds.data_vars), product_map)
        cloud_var = next((name for name, standard in resolved.items() if standard == "cloud_mask"), None)
        if cloud_var is None:
            raise RuntimeError(f"cloud_mask not resolved from {path}")
        mask = reshape_square_if_needed(np.array(ds[cloud_var].values, copy=True))
        if mask.dtype.kind in "fc":
            mask = mask_sentinel_values(mask.astype(np.float32))
        lat = reshape_square_if_needed(np.array(ds["latitude"].values, dtype=np.float32, copy=True))
        lon = reshape_square_if_needed(np.array(ds["longitude"].values, dtype=np.float32, copy=True))
        return {
            "cloud_mask": mask,
            "latitude": lat,
            "longitude": lon,
            "cfgrib_attrs": {k: attr_to_python(v) for k, v in ds.attrs.items()},
        }
    finally:
        ds.close()


def epic_mask(path: Path) -> dict[str, np.ndarray]:
    import netCDF4

    def read_array(ds: netCDF4.Dataset, name: str, dtype: Any) -> np.ndarray:
        values = ds[name][:]
        if np.ma.isMaskedArray(values):
            values = values.astype(np.float32).filled(np.nan)
        return np.asarray(values, dtype=dtype)

    with netCDF4.Dataset(path) as ds:
        return {
            "lat": read_array(ds, "geolocation_data/latitude", np.float32),
            "lon": read_array(ds, "geolocation_data/longitude", np.float32),
            "cloud_mask": read_array(ds, "geophysical_data/Cloud_Mask", np.float32),
        }


def map_geo_classes(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    out = np.full(values.shape, -1, dtype=np.int8)
    valid = np.zeros(values.shape, dtype=bool)
    rounded = np.zeros(values.shape, dtype=np.int16)
    finite = np.isfinite(values)
    rounded[finite] = np.rint(values[finite]).astype(np.int16)
    for raw, mapped in {0: 0, 1: 0, 2: 1, 3: 1}.items():
        hit = finite & (rounded == raw)
        out[hit] = mapped
        valid |= hit
    return out, valid


def map_epic_classes(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    out = np.full(values.shape, -1, dtype=np.int8)
    valid = np.zeros(values.shape, dtype=bool)
    rounded = np.zeros(values.shape, dtype=np.int16)
    finite = np.isfinite(values)
    rounded[finite] = np.rint(values[finite]).astype(np.int16)
    for raw, mapped in {1: 0, 2: 0, 3: 1, 4: 1}.items():
        hit = finite & (rounded == raw)
        out[hit] = mapped
        valid |= hit
    return out, valid


def resample_to_epic(mask: np.ndarray, lat: np.ndarray, lon: np.ndarray, epic: dict[str, np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    from pyresample import geometry, kd_tree

    classes, source_valid = map_geo_classes(mask)
    source = geometry.SwathDefinition(lons=lon, lats=lat)
    target = geometry.SwathDefinition(lons=epic["lon"], lats=epic["lat"])
    sampled = kd_tree.resample_nearest(source, classes, target, radius_of_influence=ROI_METERS, fill_value=-1)
    sampled_valid = kd_tree.resample_nearest(
        source,
        source_valid.astype(np.uint8),
        target,
        radius_of_influence=ROI_METERS,
        fill_value=0,
    ).astype(bool)
    return sampled.astype(np.int8), sampled_valid & (sampled >= 0)


def binary_metrics(epic: dict[str, np.ndarray], sampled: np.ndarray, sampled_valid: np.ndarray) -> dict[str, Any]:
    ref, ref_valid = map_epic_classes(epic["cloud_mask"])
    common = ref_valid & sampled_valid
    n = int(np.count_nonzero(common))
    if n == 0:
        return {"valid_pixel_count": 0}
    r = ref[common] == 1
    s = sampled[common] == 1
    tp = int(np.count_nonzero(r & s))
    tn = int(np.count_nonzero(~r & ~s))
    fp = int(np.count_nonzero(~r & s))
    fn = int(np.count_nonzero(r & ~s))
    agreement = float((tp + tn) / n)
    f1 = float(2 * tp / (2 * tp + fp + fn)) if (2 * tp + fp + fn) else math.nan
    iou = float(tp / (tp + fp + fn)) if (tp + fp + fn) else math.nan
    denom = math.sqrt((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
    mcc = float(((tp * tn) - (fp * fn)) / denom) if denom else math.nan
    return {
        "valid_pixel_count": n,
        "agreement": agreement,
        "F1": f1,
        "IoU": iou,
        "MCC": mcc,
        "TP": tp,
        "TN": tn,
        "FP": fp,
        "FN": fn,
    }


def area_check(case: str, attrs: dict[str, Any], lat: np.ndarray, lon: np.ndarray) -> dict[str, Any]:
    mid_y = lat.shape[0] // 2
    mid_x = lat.shape[1] // 2
    center_lon = float(lon[mid_y, mid_x])
    center_lat = float(lat[mid_y, mid_x])
    eastward = float(lon[mid_y, mid_x + 100] - lon[mid_y, mid_x - 100])
    north_to_south = float(lat[mid_y - 100, mid_x] - lat[mid_y + 100, mid_x])
    finite_fraction = float(np.mean(np.isfinite(lat) & np.isfinite(lon)))
    ok = (
        attrs.get("navigation_area_id") == IODC_CLM_AREA_ID
        and abs(float(attrs.get("navigation_lon_0", math.nan)) - IODC_CLM_SUBSATELLITE_LONGITUDE) <= 1e-3
        and tuple(lat.shape) == IODC_CLM_SHAPE
        and abs(center_lon - IODC_CLM_SUBSATELLITE_LONGITUDE) < 0.1
        and abs(center_lat) < 0.1
        and finite_fraction > 0.25
        and abs(eastward) > 1.0
        and abs(north_to_south) > 1.0
    )
    return {
        "time_tag": case,
        "area_id": attrs.get("navigation_area_id", ""),
        "lon_0": attrs.get("navigation_lon_0", ""),
        "shape": "x".join(map(str, lat.shape)),
        "center_lon": center_lon,
        "center_lat": center_lat,
        "eastward_lon_delta": eastward,
        "north_to_south_lat_delta": north_to_south,
        "finite_lonlat_fraction": finite_fraction,
        "orientation_note": "Satpy native AreaDefinition scan order; deltas recorded, no manual flip/rotation applied.",
        "status": "PASS" if ok else "FAIL",
    }


def main() -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    mapping = read_mapping()
    mask_rows: list[dict[str, Any]] = []
    area_rows: list[dict[str, Any]] = []
    epic_rows: list[dict[str, Any]] = []
    warning_rows: list[dict[str, Any]] = []

    baseline_rows = read_csv(STAGE09I_BASELINE)
    baseline_satpy = next(r for r in baseline_rows if r["time_tag"] == "20240306_1300" and r["reader_navigation"] == "satpy_area_navigation")

    for time_tag, timestamp, epic_name, fixed_case in CASES:
        zpath = case_zip(timestamp)
        epath = EPIC_ROOT / epic_name
        if not zpath.exists() or not epath.exists():
            warning_rows.append({"time_tag": time_tag, "warning": "missing_input", "zip": str(zpath), "epic": str(epath)})
            continue
        patched = read_product(zpath, "Meteosat", "CLM", mapping)
        legacy = read_legacy_cfgrib_mask_and_navigation(zpath, mapping)
        for warning in patched.warnings:
            warning_rows.append({"time_tag": time_tag, "warning": warning, "zip": str(zpath)})

        patched_mask = np.asarray(patched.arrays["cloud_mask"])
        legacy_mask = np.asarray(legacy["cloud_mask"])
        array_equal = bool(np.array_equal(np.rint(patched_mask), np.rint(legacy_mask)))
        hash_equal = category_hash(patched_mask) == category_hash(legacy_mask)
        counts_patched = class_counts(patched_mask)
        counts_legacy = class_counts(legacy_mask)
        mask_rows.append(
            {
                "time_tag": time_tag,
                "array_equal": array_equal,
                "hash_equal": hash_equal,
                "hash_dtype": "int16_category_normalized",
                "patched_unique_counts": json.dumps(counts_patched, sort_keys=True),
                "legacy_unique_counts": json.dumps(counts_legacy, sort_keys=True),
                "counts_equal": counts_patched == counts_legacy,
                "mask_transform": patched.attrs.get("mask_transform", ""),
                "navigation_schema_version": patched.attrs.get("navigation_schema_version", ""),
                "reader_backend": patched.attrs.get("reader_backend", ""),
                "navigation_source": patched.attrs.get("navigation_source", ""),
                "status": "PASS" if array_equal and hash_equal and counts_patched == counts_legacy else "FAIL",
            }
        )
        area_rows.append(
            area_check(
                time_tag,
                patched.attrs,
                np.asarray(patched.arrays["latitude"]),
                np.asarray(patched.arrays["longitude"]),
            )
        )

        epic = epic_mask(epath)
        legacy_sampled, legacy_valid = resample_to_epic(
            legacy_mask,
            np.asarray(legacy["latitude"]),
            np.asarray(legacy["longitude"]),
            epic,
        )
        patched_sampled, patched_valid = resample_to_epic(
            patched_mask,
            np.asarray(patched.arrays["latitude"]),
            np.asarray(patched.arrays["longitude"]),
            epic,
        )
        common = legacy_valid & patched_valid
        for label, sampled, sampled_valid in [
            ("legacy_current_navigation", legacy_sampled, legacy_valid & common),
            ("patched_satpy_navigation", patched_sampled, patched_valid & common),
        ]:
            metrics = binary_metrics(epic, sampled, sampled_valid)
            row = {"time_tag": time_tag, "reader_navigation": label, "common_valid_domain": "epic_valid_and_legacy_valid_and_patched_valid"}
            row.update(metrics)
            if fixed_case and label == "patched_satpy_navigation":
                row["stage09i_baseline_agreement"] = baseline_satpy["agreement"]
                row["stage09i_baseline_F1"] = baseline_satpy["F1"]
                row["stage09i_baseline_IoU"] = baseline_satpy["IoU"]
                row["stage09i_baseline_MCC"] = baseline_satpy["MCC"]
                row["baseline_reproduced"] = (
                    abs(float(metrics["agreement"]) - float(baseline_satpy["agreement"])) < 1e-9
                    and abs(float(metrics["F1"]) - float(baseline_satpy["F1"])) < 1e-9
                    and abs(float(metrics["IoU"]) - float(baseline_satpy["IoU"])) < 1e-9
                    and abs(float(metrics["MCC"]) - float(baseline_satpy["MCC"])) < 1e-9
                )
            epic_rows.append(row)

    case_metric = {(r["time_tag"], r["reader_navigation"]): r for r in epic_rows}
    fixed_patched = case_metric.get(("20240306_1300", "patched_satpy_navigation"), {})
    regression_ok = True
    for time_tag, _, _, _ in CASES:
        legacy = case_metric.get((time_tag, "legacy_current_navigation"), {})
        patched = case_metric.get((time_tag, "patched_satpy_navigation"), {})
        if not legacy or not patched:
            regression_ok = False
            continue
        regression_ok &= float(patched.get("agreement", 0.0)) > float(legacy.get("agreement", 1.0))
        regression_ok &= float(patched.get("MCC", 0.0)) > float(legacy.get("MCC", 1.0))

    gate_rows = [
        {"gate": "IODC_MASK_PRESERVATION", "status": "PASS" if mask_rows and all(r["status"] == "PASS" for r in mask_rows) else "FAIL"},
        {"gate": "IODC_SCOPE_GUARD", "status": "PASS" if mask_rows and all(r["mask_transform"] == "identity" for r in mask_rows) else "FAIL"},
        {"gate": "IODC_SATPY_AREA", "status": "PASS" if area_rows and all(r["status"] == "PASS" for r in area_rows) else "FAIL"},
        {"gate": "IODC_EPIC_BASELINE_REPRODUCTION", "status": "PASS" if fixed_patched.get("baseline_reproduced") is True else "FAIL"},
        {
            "gate": "IODC_CACHE_VERSIONING",
            "status": "PASS"
            if mask_rows
            and all(
                r["navigation_schema_version"] == IODC_CLM_NAVIGATION_SCHEMA_VERSION
                and r["reader_backend"] == IODC_CLM_READER_BACKEND
                and r["navigation_source"] == IODC_CLM_NAVIGATION_SOURCE
                for r in mask_rows
            )
            else "FAIL",
        },
        {"gate": "IODC_POST_PATCH_REGRESSION", "status": "PASS" if regression_ok else "FAIL"},
    ]
    final_status = (
        "IODC_SATPY_PRODUCTION_PATCH_VALIDATED"
        if all(row["status"] == "PASS" for row in gate_rows)
        else "IODC_PRODUCTION_PATCH_FAILED"
    )

    write_csv(OUTPUT_ROOT / "iodc_mask_preservation.csv", mask_rows)
    write_csv(OUTPUT_ROOT / "iodc_satpy_area_check.csv", area_rows)
    write_csv(OUTPUT_ROOT / "iodc_epic_comparison.csv", epic_rows)
    write_csv(OUTPUT_ROOT / "stage_09j_gate_status.csv", gate_rows)
    write_csv(OUTPUT_ROOT / "warnings.csv", warning_rows)

    summary = [
        "# Stage 09J：Meteosat-IODC CLM Satpy production integration",
        "",
        "## 做了什么",
        "",
        "- 仅对 `Meteosat-IODC/CLM/MSG2-SEVI-MSGCLMK-0100-0100-*`、`3712x3712`、`lon_0=45.5` 文件族启用 Satpy `seviri_l2_grib`。",
        "- production mask 保持 identity；navigation 来源改为 Satpy `AreaDefinition`。",
        "- 固定首例 `20240306_1300` 后，又检查 `20240306_1100` 和 `20240306_1400` 两个已有 EPIC 配对时次。",
        "",
        "## 关键数值",
        "",
    ]
    for time_tag, _, _, _ in CASES:
        legacy = case_metric.get((time_tag, "legacy_current_navigation"), {})
        patched = case_metric.get((time_tag, "patched_satpy_navigation"), {})
        if legacy and patched:
            summary.append(
                f"- `{time_tag}`：legacy agreement `{float(legacy['agreement']):.6f}` / MCC `{float(legacy['MCC']):.6f}`；"
                f"patched agreement `{float(patched['agreement']):.6f}` / MCC `{float(patched['MCC']):.6f}`。"
            )
    summary.extend(
        [
            "",
            "## 最终状态",
            "",
            f"`{final_status}`",
            "",
            "## 是否需要修改代码",
            "",
            "需要，且本阶段已将 IODC CLM production navigation 接入 Satpy AreaDefinition。未修改 Meteosat-0deg Gate4B 或 CLAAS-3 reader。",
            "",
        ]
    )
    (OUTPUT_ROOT / "stage_09j_summary_cn.md").write_text("\n".join(summary), encoding="utf-8-sig")

    output_paths = [
        OUTPUT_ROOT / "stage_09j_summary_cn.md",
        OUTPUT_ROOT / "iodc_mask_preservation.csv",
        OUTPUT_ROOT / "iodc_satpy_area_check.csv",
        OUTPUT_ROOT / "iodc_epic_comparison.csv",
        OUTPUT_ROOT / "stage_09j_gate_status.csv",
        OUTPUT_ROOT / "warnings.csv",
    ]
    write_manifest(
        OUTPUT_ROOT / "manifest.json",
        canonical_stage_id=STAGE_ID,
        component_role=COMPONENT_ROLE,
        related_stage_ids=("stage_09i",),
        run_id=OUTPUT_ROOT.name,
        source_profile="operational_baseline",
        generating_script=Path(__file__),
        input_paths=[
            STAGE09I_BASELINE,
            *[case_zip(timestamp) for _, timestamp, _, _ in CASES],
            *[EPIC_ROOT / epic_name for _, _, epic_name, _ in CASES],
        ],
        output_paths=output_paths,
        parameters={
            "scope": {
            "source": "Meteosat-IODC",
            "product": "CLM",
            "filename_family": "MSG2-SEVI-MSGCLMK-0100-0100-*",
            "shape": list(IODC_CLM_SHAPE),
            "lon_0": IODC_CLM_SUBSATELLITE_LONGITUDE,
            "reader_backend": IODC_CLM_READER_BACKEND,
            "navigation_source": IODC_CLM_NAVIGATION_SOURCE,
            "navigation_schema_version": IODC_CLM_NAVIGATION_SCHEMA_VERSION,
            },
            "cases": [case[0] for case in CASES],
            "no_monthly_run": True,
        },
        project_root=PROJECT_ROOT,
        extra={
            "gates": {row["gate"]: row["status"] for row in gate_rows},
            "final_status": final_status,
            "production_code_modified": True,
        },
    )
    print(final_status)


if __name__ == "__main__":
    main()
