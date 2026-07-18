from __future__ import annotations

import argparse
import json
import math
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import path_config
from geo_ring_cloud_lineage import write_manifest
from geo_ring_cloud_source_registry import REGISTRY_VERSION, validate_profile
from stage1_common import NATIVE_DIR, REPORT_DIR, SCRIPT_DIR, ensure_dirs, utc_now


CATEGORICAL = ["cloud_mask", "cloud_type", "cloud_phase", "quality_flag_raw", "quality_flag_standard"]
SUSPECT_FILL_CODES = {-128, 127, 255, 32767, 65535}
SPACE_OR_FILL_HINTS = ("fill", "missing", "space", "off disk", "off-disk", "nodata", "no data")
OUT_ISSUES = NATIVE_DIR / "standardized_native_semantic_issues.csv"
OUT_CODE_TABLES = NATIVE_DIR / "standardized_native_code_tables.csv"
OUT_REPORT = REPORT_DIR / "standardized_native_semantic_validation_report.md"


def as_scalar_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "|".join(as_scalar_text(v) for v in value)
    return str(value)


def attrs_for(metadata: dict[str, Any], variable: str) -> dict[str, Any]:
    direct = metadata.get("variable_attrs", {}).get(variable, {})
    if isinstance(direct, dict) and direct:
        return direct
    reader_attrs = metadata.get("reader_attrs", {})
    raw = reader_attrs.get(f"attrs_{variable}", {})
    return raw if isinstance(raw, dict) else {}


def source_for(metadata: dict[str, Any], variable: str) -> str:
    src = metadata.get("source_variables", {})
    return str(src.get(variable, ""))


def attr_fill_codes(attrs: dict[str, Any]) -> set[int]:
    codes: set[int] = set()
    for key in ["_FillValue", "FillValue", "fill_value", "missing_value", "MissingValue", "missingValue"]:
        if key not in attrs:
            continue
        value = attrs[key]
        values = value if isinstance(value, list) else [value]
        for item in values:
            try:
                if np.isfinite(float(item)):
                    codes.add(int(float(item)))
            except Exception:
                continue
    valid_range = attrs.get("valid_range")
    if isinstance(valid_range, list) and len(valid_range) == 2:
        try:
            lo, hi = [float(x) for x in valid_range]
            for code in SUSPECT_FILL_CODES:
                if code < lo or code > hi:
                    codes.add(code)
        except Exception:
            pass
    return codes


def description_flag_codes(attrs: dict[str, Any]) -> set[int]:
    codes: set[int] = set()
    text = " ".join(str(attrs.get(k, "")) for k in ["Description", "description", "flag_meanings", "long_name"])
    lowered = text.lower()
    if not any(hint in lowered for hint in SPACE_OR_FILL_HINTS):
        return codes
    import re

    for match in re.finditer(r"(-?\d+)\s*[:=]\s*([^,;|]+)", text):
        try:
            code = int(match.group(1))
        except Exception:
            continue
        meaning = match.group(2).lower()
        if any(hint in meaning for hint in SPACE_OR_FILL_HINTS):
            codes.add(code)
    return codes


def unique_counts(arr: np.ndarray, max_values: int = 256) -> list[tuple[Any, int]]:
    a = np.asarray(arr)
    if a.ndim == 0:
        return []
    if a.dtype.kind == "f":
        finite = np.isfinite(a)
        a = a[finite]
        if a.size == 0:
            return []
        if np.unique(a[: min(a.size, 100_000)]).size > max_values:
            return []
    vals, counts = np.unique(a, return_counts=True)
    order = np.argsort(counts)[::-1]
    return [(vals[i].item() if hasattr(vals[i], "item") else vals[i], int(counts[i])) for i in order[:max_values]]


def code_table_rows(meta: dict[str, Any], variable: str, arr: np.ndarray) -> list[dict[str, Any]]:
    attrs = attrs_for(meta, variable)
    attr_codes = attr_fill_codes(attrs)
    desc_codes = description_flag_codes(attrs)
    rows = []
    for value, count in unique_counts(arr):
        try:
            value_float = float(value)
            value_int = int(value_float) if value_float.is_integer() else None
        except Exception:
            value_float = math.nan
            value_int = None
        is_suspect = value_int in SUSPECT_FILL_CODES if value_int is not None else False
        is_attr_fill = value_int in attr_codes if value_int is not None else False
        is_description_fill = value_int in desc_codes if value_int is not None else False
        rows.append(
            {
                "satellite_group": meta.get("satellite_group", ""),
                "product": meta.get("product", ""),
                "variable": variable,
                "source_variable": source_for(meta, variable),
                "value": value,
                "count": count,
                "is_suspect_fill_code": is_suspect,
                "is_attr_fill_code": is_attr_fill,
                "is_description_fill_or_space_code": is_description_fill,
                "units": attrs.get("units", ""),
                "valid_range": as_scalar_text(attrs.get("valid_range", "")),
                "fill_value": as_scalar_text(attrs.get("_FillValue", attrs.get("FillValue", attrs.get("missing_value", "")))),
                "description": attrs.get("Description", attrs.get("description", attrs.get("flag_meanings", ""))),
            }
        )
    return rows


def issue(severity: str, issue_type: str, meta: dict[str, Any], variable: str, message: str) -> dict[str, Any]:
    return {
        "severity": severity,
        "issue_type": issue_type,
        "satellite_group": meta.get("satellite_group", ""),
        "product": meta.get("product", ""),
        "variable": variable,
        "source_file": meta.get("source_file", ""),
        "message": message,
    }


def valid_mask_fill_issue(meta: dict[str, Any], variable: str, arr: np.ndarray, valid_mask: np.ndarray | None) -> dict[str, Any] | None:
    if valid_mask is None or np.asarray(valid_mask).ndim < 2 or np.asarray(arr).ndim < 2:
        return None
    mask = np.asarray(valid_mask)
    data = np.asarray(arr)
    if mask.shape != data.shape:
        return issue("WARN", "VALID_MASK_SHAPE_MISMATCH", meta, variable, f"valid_mask shape {mask.shape} != {variable} shape {data.shape}")
    attrs = attrs_for(meta, variable)
    fill_codes = set(SUSPECT_FILL_CODES) | attr_fill_codes(attrs) | description_flag_codes(attrs)
    bad_counts: Counter[int] = Counter()
    for code in fill_codes:
        try:
            bad = np.isclose(data.astype(float), float(code)) & (mask > 0)
        except Exception:
            bad = data == code
            bad = bad & (mask > 0)
        count = int(np.count_nonzero(bad))
        if count:
            bad_counts[int(code)] = count
    if bad_counts:
        detail = ", ".join(f"{k}:{v}" for k, v in sorted(bad_counts.items()))
        return issue("FAIL", "FILL_CODE_VALID_IN_VALID_MASK", meta, variable, f"fill/suspect codes remain valid: {detail}")
    return None


def quality_mapping_issue(meta: dict[str, Any], arr: np.ndarray) -> dict[str, Any] | None:
    if meta.get("satellite_group") != "FY4B" or meta.get("product") not in {"CLP", "CLT", "CTH", "CTT", "CTP"}:
        return None
    if np.asarray(arr).ndim < 2:
        return None
    vals = unique_counts(arr, max_values=64)
    numeric_values = []
    for value, _ in vals:
        try:
            numeric_values.append(float(value))
        except Exception:
            pass
    if not numeric_values:
        return issue("WARN", "QUALITY_MAPPING_SUSPECT", meta, "quality_flag_raw", "quality flag is unreadable or nonnumeric")
    max_abs = max(abs(v) for v in numeric_values)
    unique_count = len(numeric_values)
    if max_abs > 255 or unique_count > 32:
        return issue(
            "WARN",
            "QUALITY_MAPPING_SUSPECT",
            meta,
            "quality_flag_raw",
            f"FY4B quality_flag_raw has atypical DQF range; sampled unique_count={unique_count}, max_abs={max_abs:g}",
        )
    return None


def metadata_completeness_issues(meta: dict[str, Any], variable: str) -> list[dict[str, Any]]:
    attrs = attrs_for(meta, variable)
    if not attrs:
        return [issue("WARN", "MISSING_VARIABLE_METADATA", meta, variable, "no per-variable metadata found in metadata_json")]
    missing = []
    if "units" not in attrs:
        missing.append("units")
    if not any(k in attrs for k in ["valid_range", "valid_min", "valid_max"]):
        missing.append("valid_range")
    if not any(k in attrs for k in ["_FillValue", "FillValue", "missing_value"]):
        missing.append("fill_value")
    if missing:
        return [issue("WARN", "INCOMPLETE_VARIABLE_METADATA", meta, variable, "missing " + ", ".join(missing))]
    return []


def longitude_issue(meta: dict[str, Any], lon: np.ndarray) -> dict[str, Any] | None:
    if not str(meta.get("satellite_group", "")).startswith("Meteosat"):
        return None
    a = np.asarray(lon)
    finite = np.isfinite(a)
    if not finite.any():
        return issue("WARN", "METEOSAT_LONGITUDE_UNREADABLE", meta, "longitude", "longitude has no finite values")
    mn = float(np.nanmin(a))
    mx = float(np.nanmax(a))
    if mx > 180.0:
        return issue("WARN", "METEOSAT_LONGITUDE_0_360", meta, "longitude", f"longitude range is {mn:g}..{mx:g}; recommend normalizing to -180..180 before reprojection")
    return None


def shape_issue(meta: dict[str, Any], variable: str, arr: np.ndarray) -> dict[str, Any] | None:
    if not str(meta.get("satellite_group", "")).startswith("Meteosat"):
        return None
    if variable not in {"cloud_mask", "cloud_top_height_km"}:
        return None
    if np.asarray(arr).ndim != 2:
        return issue("FAIL", "METEOSAT_SHAPE_NOT_2D", meta, variable, f"{variable} shape is {np.asarray(arr).shape}, expected 2D native grid")
    return None


def semantic_validate_one(npz_file: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    code_rows: list[dict[str, Any]] = []
    with np.load(npz_file, allow_pickle=False) as npz:
        meta = json.loads(str(npz["metadata_json"]))
        valid_mask = np.asarray(npz["valid_mask"]) if "valid_mask" in npz.files and np.asarray(npz["valid_mask"]).ndim >= 2 else None
        for variable in CATEGORICAL:
            if variable not in npz.files:
                continue
            arr = np.asarray(npz[variable])
            availability = json.loads(str(npz["variable_availability_json"]))
            if not availability.get(f"has_{variable}", False):
                continue
            code_rows.extend(code_table_rows(meta, variable, arr))
            vm_issue = valid_mask_fill_issue(meta, variable, arr, valid_mask)
            if vm_issue:
                issues.append(vm_issue)
            if variable == "quality_flag_raw":
                qm_issue = quality_mapping_issue(meta, arr)
                if qm_issue:
                    issues.append(qm_issue)
            issues.extend(metadata_completeness_issues(meta, variable))
        for variable in ["cloud_top_height_km", "cloud_top_temperature_K", "cloud_top_pressure_hPa", "cloud_optical_thickness", "cloud_effective_radius_um", "cloud_water_path_g_m2", "latitude", "longitude"]:
            if variable not in npz.files:
                continue
            availability = json.loads(str(npz["variable_availability_json"]))
            if not availability.get(f"has_{variable}", False):
                continue
            arr = np.asarray(npz[variable])
            shape = shape_issue(meta, variable, arr)
            if shape:
                issues.append(shape)
            if variable == "longitude":
                lon = longitude_issue(meta, arr)
                if lon:
                    issues.append(lon)
            issues.extend(metadata_completeness_issues(meta, variable))
        # Product-specific layer confirmations.
        if meta.get("satellite_group") == "Himawari-9" and meta.get("product") == "CMSK":
            src = meta.get("source_variables", {})
            issues.append(issue("INFO", "HIMAWARI_CMSK_LAYER_SELECTION", meta, "cloud_mask", f"cloud_mask={src.get('cloud_mask')}; cloud_probability={src.get('cloud_probability')}"))
        if str(meta.get("satellite_group", "")).startswith("GOES") and meta.get("product") in {"ACMF", "ACTPF"}:
            src = meta.get("source_variables", {})
            target = "cloud_mask" if meta.get("product") == "ACMF" else "cloud_phase"
            issues.append(issue("INFO", "GOES_LAYER_SELECTION", meta, target, f"{target}={src.get(target)}; quality_flag_raw={src.get('quality_flag_raw')}"))
        if meta.get("satellite_group") == "CLAAS3-0deg":
            availability = json.loads(str(npz["variable_availability_json"]))
            for variable in [name.replace("has_", "") for name, enabled in availability.items() if enabled]:
                if variable not in npz.files or np.asarray(npz[variable]).ndim != 2:
                    continue
                for kind in ("physical", "fusion", "diagnostic"):
                    mask_name = f"{kind}_valid_mask_{variable}"
                    if mask_name not in npz.files:
                        issues.append(issue("FAIL", "CLAAS3_VARIABLE_MASK_MISSING", meta, variable, mask_name))
                    elif np.asarray(npz[mask_name]).shape != np.asarray(npz[variable]).shape:
                        issues.append(issue("FAIL", "CLAAS3_VARIABLE_MASK_SHAPE", meta, variable, f"{mask_name} shape mismatch"))
            if not meta.get("geostationary_projection_attrs"):
                issues.append(issue("FAIL", "CLAAS3_CF_PROJECTION_MISSING", meta, "projection", "CF geostationary attributes are required"))
            if "exactly once" not in str(meta.get("scale_offset_policy", "")):
                issues.append(issue("FAIL", "CLAAS3_SCALE_PROVENANCE_MISSING", meta, "", "scale/add-offset application count is not documented"))
    return issues, code_rows


def status_from_issues(issues: list[dict[str, Any]]) -> str:
    severities = {row["severity"] for row in issues}
    if "FAIL" in severities:
        return "FAIL"
    if "WARN" in severities:
        return "PASS_WITH_WARNINGS"
    return "PASS"


def write_report(status: str, issues_df: pd.DataFrame, code_df: pd.DataFrame) -> None:
    lines = [
        "# Standardized Native Semantic Validation Report",
        "",
        f"- Generated UTC: {utc_now()}",
        f"- Overall semantic status: **{status}**",
        "- Scope: semantic validation patch after 03; no download, no reprojection, no fusion.",
        "",
        "## Checks",
        "",
        "- Unique value counts for cloud_mask/cloud_type/cloud_phase/quality flags were written to `standardized_native_code_tables.csv`.",
        "- Suspect fill codes checked: `127, 255, -128, 32767, 65535`, plus `_FillValue`, `FillValue`, `missing_value`, `valid_range`, and text descriptions.",
        "- valid_mask was tested against fill/suspect codes.",
        "- Meteosat longitude and 2D shape preservation were checked.",
        "",
        "## Issue Summary",
        "",
    ]
    if issues_df.empty:
        lines.append("- No semantic issues found.")
    else:
        summary = issues_df.groupby(["severity", "issue_type"]).size().reset_index(name="count")
        for _, row in summary.iterrows():
            lines.append(f"- {row['severity']} {row['issue_type']}: {row['count']}")
    lines.extend(["", "## Blocking Decision", ""])
    if status == "FAIL":
        lines.append("- FAIL: fill/suspect code or native shape issue blocks step 04. Fix standardized native output and rerun 03/03.5 first.")
    elif status == "PASS_WITH_WARNINGS":
        lines.append("- PASS_WITH_WARNINGS: basic arrays are readable, but code tables/metadata/quality mapping need interpretation before scientific use.")
    else:
        lines.append("- PASS: semantic checks did not find blocking or warning issues.")
    lines.extend(["", "## Output Files", ""])
    lines.append(f"- `{OUT_ISSUES}`")
    lines.append(f"- `{OUT_CODE_TABLES}`")
    OUT_REPORT.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 03.5 semantic and per-variable mask gate")
    parser.add_argument("--source-profile", default="operational_baseline", choices=["operational_baseline", "claas3_candidate"])
    parser.add_argument("--run-id", default="")
    args = parser.parse_args()
    source_profile = validate_profile(args.source_profile)
    ensure_dirs()
    shutil.copy2(__file__, SCRIPT_DIR / Path(__file__).name)
    inventory = pd.read_csv(NATIVE_DIR / "standardized_native_inventory.csv")
    all_issues: list[dict[str, Any]] = []
    all_code_rows: list[dict[str, Any]] = []
    for _, row in inventory.iterrows():
        npz_file = Path(str(row["npz_file"]))
        issues, code_rows = semantic_validate_one(npz_file)
        all_issues.extend(issues)
        all_code_rows.extend(code_rows)
    issues_df = pd.DataFrame(all_issues)
    if issues_df.empty:
        issues_df = pd.DataFrame(columns=["severity", "issue_type", "satellite_group", "product", "variable", "source_file", "message"])
    code_df = pd.DataFrame(all_code_rows)
    if code_df.empty:
        code_df = pd.DataFrame(
            columns=[
                "satellite_group",
                "product",
                "variable",
                "source_variable",
                "value",
                "count",
                "is_suspect_fill_code",
                "is_attr_fill_code",
                "is_description_fill_or_space_code",
                "units",
                "valid_range",
                "fill_value",
                "description",
            ]
        )
    issues_df.to_csv(OUT_ISSUES, index=False, encoding="utf-8-sig")
    code_df.to_csv(OUT_CODE_TABLES, index=False, encoding="utf-8-sig")
    status = status_from_issues(all_issues)
    write_report(status, issues_df, code_df)
    write_manifest(
        NATIVE_DIR / "stage_03_5_claas3_semantic_manifest.json",
        canonical_stage_id="stage_03_5",
        run_id=args.run_id,
        source_profile=source_profile,
        generating_script=Path(__file__),
        input_paths=inventory["npz_file"].dropna().astype(str).tolist(),
        output_paths=[OUT_ISSUES, OUT_CODE_TABLES, OUT_REPORT],
        parameters={"profile_gate": source_profile},
        project_root=path_config.PROJECT_ROOT,
        extra={"registry_version": REGISTRY_VERSION, "product_versions": {"CLAAS3": "405"} if source_profile == "claas3_candidate" else {}, "status": status},
    )
    print(f"03.5 {status}: issues={len(issues_df)} code_rows={len(code_df)}")
    print(f"report={OUT_REPORT}")
    return 0 if status != "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
