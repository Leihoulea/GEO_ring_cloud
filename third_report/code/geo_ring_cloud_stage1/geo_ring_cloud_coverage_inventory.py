"""Build an auditable CLM/CTH yearly coverage and gap inventory from Stage 01."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


COMPONENT_ROLE = "coverage_inventory"

CORE_PRODUCTS = {
    "FY4B": ("CLM", "CTH", "GEO"),
    "GOES-16": ("ACMF", "ACHAF"),
    "GOES-18": ("ACMF", "ACHAF"),
    "Himawari-9": ("CMSK", "CHGT"),
    "Meteosat-0deg": ("CLM", "CTH"),
    "Meteosat-IODC": ("CLM", "CTH"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize yearly CLM/CTH source coverage from Stage 01 index")
    parser.add_argument("--time-index", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    df = pd.read_csv(args.time_index)
    required = {"nominal_time", "satellite_group", "product_files_json"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"time index missing columns: {sorted(missing)}")
    rows = []
    for _, item in df.iterrows():
        group = str(item["satellite_group"])
        if group not in CORE_PRODUCTS:
            continue
        products = json.loads(str(item["product_files_json"])) if pd.notna(item["product_files_json"]) else {}
        absent = [name for name in CORE_PRODUCTS[group] if name not in products or not products[name].get("file_path")]
        rows.append({
            "nominal_time": item["nominal_time"], "satellite_group": group,
            "required_products": "|".join(CORE_PRODUCTS[group]),
            "available": not absent,
            "availability_status": "AVAILABLE" if not absent else "SOURCE_UNAVAILABLE_AT_TIME",
            "missing_core_products": "|".join(absent),
            "stage01_status": item.get("status", ""),
        })
    out = pd.DataFrame(rows).sort_values(["nominal_time", "satellite_group"])
    args.output_dir.mkdir(parents=True, exist_ok=True)
    out.to_csv(args.output_dir / "clm_cth_core_source_coverage.csv", index=False, encoding="utf-8-sig")
    gaps = out.loc[~out["available"]].copy()
    gaps.to_csv(args.output_dir / "clm_cth_core_source_gaps.csv", index=False, encoding="utf-8-sig")
    summary = out.groupby("satellite_group", as_index=False).agg(time_slots=("nominal_time", "count"), available_slots=("available", "sum"))
    summary["coverage_fraction"] = summary["available_slots"] / summary["time_slots"]
    summary.to_csv(args.output_dir / "clm_cth_core_coverage_summary.csv", index=False, encoding="utf-8-sig")
    print(f"coverage_rows={len(out)} gaps={len(gaps)} output={args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
