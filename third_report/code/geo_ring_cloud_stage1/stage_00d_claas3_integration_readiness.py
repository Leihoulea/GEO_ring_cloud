from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

import netCDF4

import path_config
from geo_ring_cloud.adapters.claas3 import discover_files, records_as_dicts, structure_signature
from geo_ring_cloud_lineage import write_manifest
from geo_ring_cloud_source_registry import REGISTRY_VERSION


SCRIPT_PATH = Path(__file__).resolve()
STAGE_ID = "stage_00d"
EXPECTED_COUNTS = {"CMA": 2592, "CTX": 648, "CPP": 648}
START = "2024-03-05T00:00:00Z"
END = "2024-03-31T23:59:59Z"
EXPECTED_SCIENCE = {
    "CMA": {"cma", "cma_prob"},
    "CTX": {"cth", "ctt", "ctp"},
    "CPP": {"cph", "cot", "cre", "cwp"},
}


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields or ["empty"])
        writer.writeheader()
        writer.writerows(rows)


def inspect_header(path: Path, product: str, reason: str) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    with netCDF4.Dataset(path) as ds:
        variable_names = set(ds.variables)
        missing_science = EXPECTED_SCIENCE[product] - variable_names
        if missing_science:
            problems.append(f"missing science variables: {sorted(missing_science)}")
        if not {"x", "y", "projection"}.issubset(variable_names):
            problems.append("missing CF geostationary x/y/projection variables")
        projection = ds.variables.get("projection")
        if projection is None or getattr(projection, "grid_mapping_name", "") != "geostationary":
            problems.append("projection grid_mapping_name is not geostationary")
        flag_vars = [name for name in ("quality", "status_flag", "conditions", "processing_flag", "processing_flag_16") if name in ds.variables]
        if not flag_vars:
            problems.append("no documented QA/status/processing flag variable")
        for name in flag_vars:
            attrs = set(ds.variables[name].ncattrs())
            if not ({"flag_masks", "flag_values"} & attrs) or "flag_meanings" not in attrs:
                problems.append(f"{name} lacks flag masks/values or flag_meanings")
        for name, var in ds.variables.items():
            attrs = {key: getattr(var, key) for key in var.ncattrs()}
            rows.append({
                "product": product,
                "file_name": path.name,
                "representative_reason": reason,
                "variable_name": name,
                "dimensions": "|".join(var.dimensions),
                "shape": "x".join(map(str, var.shape)),
                "dtype": str(var.dtype),
                "units": attrs.get("units", ""),
                "fill_value": attrs.get("_FillValue", attrs.get("missing_value", "")),
                "scale_factor": attrs.get("scale_factor", ""),
                "add_offset": attrs.get("add_offset", ""),
                "flag_values": json.dumps(attrs.get("flag_values", ""), default=str),
                "flag_masks": json.dumps(attrs.get("flag_masks", ""), default=str),
                "flag_meanings": attrs.get("flag_meanings", ""),
                "grid_mapping": attrs.get("grid_mapping", ""),
            })
        summary = {
            "product": product,
            "file_name": path.name,
            "representative_reason": reason,
            "dimension_count": len(ds.dimensions),
            "variable_count": len(ds.variables),
            "global_attribute_count": len(ds.ncattrs()),
            "structure_signature": structure_signature(path),
            "semantic_status": "PASS" if not problems else "FAIL",
        }
    return rows, summary, problems


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 00d CLAAS-3 integration readiness gate")
    parser.add_argument("--claas3-root", type=Path, default=path_config.CLAAS3_ROOT)
    parser.add_argument("--output-dir", type=Path, default=path_config.DATA_CHECK_ROOT / "claas3_integration_readiness")
    parser.add_argument("--run-id", default="claas3_202403_readiness")
    parser.add_argument("--source-profile", default="claas3_candidate")
    args = parser.parse_args()
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    records, duplicates = discover_files(args.claas3_root)
    scoped = [item for item in records if START <= item.nominal_time <= END]
    counts = Counter(item.product for item in scoped)
    summary_rows: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    for product, expected in EXPECTED_COUNTS.items():
        actual = counts.get(product, 0)
        status = "PASS" if actual == expected else "FAIL"
        times = [item.nominal_time for item in scoped if item.product == product]
        summary_rows.append({
            "product": product,
            "expected_count": expected,
            "actual_count": actual,
            "first_time": min(times) if times else "",
            "last_time": max(times) if times else "",
            "status": status,
        })
        if status == "FAIL":
            anomalies.append({"severity": "FAIL", "issue_type": "TIME_COVERAGE_COUNT", "product": product, "message": f"expected {expected}, found {actual}"})

    variable_rows: list[dict[str, Any]] = []
    structure_rows: list[dict[str, Any]] = []
    for product in EXPECTED_COUNTS:
        items = [item for item in scoped if item.product == product]
        if not items:
            continue
        representatives = [(0, "night/first"), (len(items) // 2, "middle"), (len(items) - 1, "day-or-last")]
        for index, reason in representatives:
            rows, structure, problems = inspect_header(Path(items[index].path), product, reason)
            variable_rows.extend(rows)
            structure_rows.append(structure)
            for problem in problems:
                anomalies.append({"severity": "FAIL", "issue_type": "SEMANTIC_CONTRACT", "product": product, "message": f"{items[index].path}: {problem}"})
    for product in EXPECTED_COUNTS:
        signatures = {row["structure_signature"] for row in structure_rows if row["product"] == product}
        if len(signatures) > 1:
            anomalies.append({"severity": "FAIL", "issue_type": "STRUCTURE_SIGNATURE_DIVERGENCE", "product": product, "message": f"sampled signatures: {sorted(signatures)}"})
    for duplicate in duplicates:
        anomalies.append({"severity": "WARN", "issue_type": "DUPLICATE_TIME", **duplicate})

    inventory_path = output / "stage_00d_claas3_file_inventory.csv"
    summary_path = output / "stage_00d_claas3_product_summary.csv"
    structure_path = output / "stage_00d_claas3_structure_inventory.csv"
    variables_path = output / "stage_00d_claas3_variable_inventory.csv"
    anomalies_path = output / "stage_00d_claas3_anomalies.csv"
    quicklook_path = output / "stage_00d_claas3_quicklook_index.csv"
    report_path = output / "stage_00d_claas3_inspection_report.md"
    manifest_path = output / "stage_00d_claas3_inspection_manifest.json"
    write_csv(inventory_path, records_as_dicts(scoped))
    write_csv(summary_path, summary_rows)
    write_csv(structure_path, structure_rows)
    write_csv(variables_path, variable_rows)
    write_csv(anomalies_path, anomalies)
    write_csv(quicklook_path, [{
        "selected_for_plot": False,
        "not_plotted_reason": "Reuse the existing CLAAS-3 visual check; this gate verifies structure, time, and semantic contracts.",
    }])

    overall = "FAIL" if any(row["status"] == "FAIL" for row in summary_rows) or any(row.get("severity") == "FAIL" for row in anomalies) else ("PASS_WITH_WARNINGS" if anomalies else "PASS")
    lines = [
        "# Stage 00d CLAAS-3 integration readiness report",
        "",
        f"- Status: **{overall}**",
        f"- Data root: `{args.claas3_root}`",
        f"- Time range: `{START}` to `{END}`",
        f"- Source registry: `{REGISTRY_VERSION}`",
        "",
        "## Product cadence",
        "",
    ]
    for row in summary_rows:
        lines.append(f"- {row['product']}: {row['actual_count']}/{row['expected_count']} ({row['status']})")
    lines.extend([
        "",
        "## Scientific boundary",
        "",
        "- CLAAS-3 is an independent CM SAF processing stream and does not replace an operational Meteosat source ID.",
        "- Uncertainty and flags remain QC/lineage fields; unresolved quality semantics are never treated as fusion-valid.",
        "- The gate reuses the existing visual check, so no duplicate quicklook is generated; the reason is recorded in the quicklook index.",
    ])
    report_path.write_text("\n".join(lines), encoding="utf-8")
    outputs = [inventory_path, summary_path, structure_path, variables_path, anomalies_path, quicklook_path, report_path]
    write_manifest(
        manifest_path,
        canonical_stage_id="stage_00d",
        run_id=args.run_id,
        source_profile=args.source_profile,
        generating_script=SCRIPT_PATH,
        input_paths=[args.claas3_root],
        output_paths=outputs,
        parameters={"start": START, "end": END, "expected_counts": EXPECTED_COUNTS},
        project_root=path_config.PROJECT_ROOT,
        extra={
            "registry_version": REGISTRY_VERSION,
            "product_versions": sorted({item.product_version for item in scoped}),
            "status": overall,
            "unresolved_semantics": [],
        },
    )
    print(f"stage_00d {overall}: {report_path}")
    return 0 if overall != "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
