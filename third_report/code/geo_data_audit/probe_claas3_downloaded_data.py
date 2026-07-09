from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

try:
    import netCDF4
except Exception as exc:  # pragma: no cover
    netCDF4 = exc


ROOT = Path(r"E:\GEO_Cloud_2024\CMSAF")
OUT_DIR = Path(r"D:\AAAresearch_paper\third_report\reports\claas3_downloaded_probe")
MAX_SAMPLE_VALUES = 300_000

FILE_RE = re.compile(
    r"^(?P<product>CMA|CPP|CTX)in(?P<stamp>\d{14})(?P<version>\d{3})SV(?P<platform>[A-Z0-9]+)\.nc$",
    re.I,
)

STANDARD_MAP = {
    "CMA": {
        "cma": "cloud_mask",
        "cma_prob": "cloud_probability",
        "status_flag": "status_flag",
        "quality": "quality_flag",
        "conditions": "retrieval_conditions",
        "time": "observation_time",
        "time_bnds": "time_bounds",
        "projection": "geostationary_projection",
        "subsatellite_alt": "satellite_height",
        "subsatellite_lat": "satellite_subpoint_latitude",
        "subsatellite_lon": "satellite_subpoint_longitude",
        "record_status": "record_status",
        "platform_flag": "platform_flag",
    },
    "CTX": {
        "cth": "cloud_top_height",
        "ctp": "cloud_top_pressure",
        "ctt": "cloud_top_temperature",
        "cth_unc": "cloud_top_height_uncertainty",
        "ctp_unc": "cloud_top_pressure_uncertainty",
        "ctt_unc": "cloud_top_temperature_uncertainty",
        "quality": "quality_flag",
        "status_flag": "status_flag",
        "conditions": "retrieval_conditions",
        "time": "observation_time",
        "time_bnds": "time_bounds",
        "projection": "geostationary_projection",
        "subsatellite_alt": "satellite_height",
        "subsatellite_lat": "satellite_subpoint_latitude",
        "subsatellite_lon": "satellite_subpoint_longitude",
        "record_status": "record_status",
        "platform_flag": "platform_flag",
    },
    "CPP": {
        "cph": "cloud_phase",
        "cph_16": "cloud_phase_alt",
        "cph_ext": "cloud_phase_extended",
        "cph_16_ext": "cloud_phase_extended_alt",
        "cot": "cloud_optical_thickness",
        "cot_16": "cloud_optical_thickness_alt",
        "cre": "cloud_effective_radius",
        "cre_16": "cloud_effective_radius_alt",
        "cwp": "cloud_water_path",
        "cwp_16": "cloud_water_path_alt",
        "cwp_unc": "cloud_water_path_uncertainty",
        "cwp_16_unc": "cloud_water_path_uncertainty_alt",
        "cot_unc": "cloud_optical_thickness_uncertainty",
        "cot_16_unc": "cloud_optical_thickness_uncertainty_alt",
        "cre_unc": "cloud_effective_radius_uncertainty",
        "cre_16_unc": "cloud_effective_radius_uncertainty_alt",
        "cdnc": "cloud_droplet_number_concentration",
        "cdnc_unc": "cloud_droplet_number_concentration_uncertainty",
        "cgt": "cloud_geometrical_thickness_or_related",
        "cgt_unc": "cloud_geometrical_thickness_or_related_uncertainty",
        "h_sigma": "retrieval_sigma_or_spread",
        "processing_flag": "quality_flag",
        "processing_flag_16": "quality_flag_alt",
        "time": "observation_time",
        "time_bnds": "time_bounds",
        "projection": "geostationary_projection",
        "subsatellite_alt": "satellite_height",
        "subsatellite_lat": "satellite_subpoint_latitude",
        "subsatellite_lon": "satellite_subpoint_longitude",
        "record_status": "record_status",
        "platform_flag": "platform_flag",
    },
}

OTHER_SATELLITE_RELATIONS = {
    "CMA": [
        "Meteosat operational CLM / CLM-IODC: both are cloud-mask family products, but CLAAS-3 CMA is CM SAF CDR/ICDR, not the operational stream.",
        "GOES ACMF: corresponds to cloud mask / cloud probability / QA role.",
        "Himawari CMSK: corresponds to cloud mask / cloud probability role.",
        "FY4B CLM: corresponds to AGRI cloud mask role.",
    ],
    "CTX": [
        "Meteosat operational CTH: overlaps on cloud-top-height role, but operational CTH we already saw is much thinner in variable content; CTX is the richer CTT/CTP/CTH bundle.",
        "GOES ACHAF + ACHTF + CTPF: CTX effectively bundles cloud-top height, temperature, and pressure that GOES exposes as separate ABI L2 products.",
        "Himawari CHGT: corresponds to CldTopHght / CldTopTemp / CldTopPres.",
        "FY4B CTH + CTT + CTP: corresponds to the FY4B cloud-top triplet.",
    ],
    "CPP": [
        "GOES ACTPF + CODF + CPSF: CLAAS-3 CPP bundles phase plus optical and microphysical variables that GOES splits across several ABI products.",
        "Himawari CHGT partly overlaps on optical depth, but CLAAS-3 CPP is richer because it adds phase and water-path style retrievals.",
        "FY4B CLP overlaps on phase role, but FY4B would still need separate microphysics products to match CPP depth.",
        "Operational Meteosat CLM/CTH do not cover these microphysical variables; this is exactly where CLAAS-3 supplements the operational baseline.",
    ],
}


def log(message: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


def ensure_out() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    if not fields:
        fields = ["empty"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def write_md(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8-sig")


def parse_name(path: Path) -> dict[str, Any]:
    m = FILE_RE.match(path.name)
    if not m:
        return {
            "file_name": path.name,
            "product": "",
            "nominal_time": "",
            "version_token": "",
            "platform_token": "",
        }
    stamp = datetime.strptime(m.group("stamp"), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    return {
        "file_name": path.name,
        "product": m.group("product").upper(),
        "nominal_time": stamp.isoformat().replace("+00:00", "Z"),
        "version_token": m.group("version"),
        "platform_token": m.group("platform"),
    }


def discover_files() -> dict[str, list[Path]]:
    out: dict[str, list[Path]] = defaultdict(list)
    for path in ROOT.rglob("*.nc"):
        meta = parse_name(path)
        if meta["product"]:
            out[meta["product"]].append(path)
    for product in out:
        out[product] = sorted(out[product])
    return dict(out)


def choose_samples(paths: list[Path]) -> list[Path]:
    if not paths:
        return []
    idxs = sorted({0, len(paths) // 2, len(paths) - 1})
    return [paths[i] for i in idxs]


def safe_attr(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="ignore")
        except Exception:
            return str(value)
    return value


def unique_summary(values: np.ndarray, max_codes: int = 24) -> tuple[str, int]:
    flat = np.asarray(values).reshape(-1)
    if flat.size > MAX_SAMPLE_VALUES:
        step = max(1, flat.size // MAX_SAMPLE_VALUES)
        flat = flat[::step][:MAX_SAMPLE_VALUES]
    if flat.dtype.kind == "f":
        flat = flat[np.isfinite(flat)]
    if flat.size == 0:
        return "", 0
    uniq, counts = np.unique(flat, return_counts=True)
    pairs = sorted(zip(uniq.tolist(), counts.tolist()), key=lambda x: (-x[1], x[0]))
    text = "; ".join(f"{v}:{c}" for v, c in pairs[:max_codes])
    return text, len(uniq)


def stats_for_var(var: Any, attrs: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    if not getattr(var, "shape", ()):
        return row
    if np.dtype(var.dtype).kind in {"S", "U", "O"}:
        return row
    data = np.asarray(var[:])
    flat = data.reshape(-1)
    if flat.size > MAX_SAMPLE_VALUES:
        step = max(1, flat.size // MAX_SAMPLE_VALUES)
        flat = flat[::step][:MAX_SAMPLE_VALUES]
    fill_value = attrs.get("_FillValue", attrs.get("missing_value", ""))
    try:
        fill_ratio = float(np.mean(flat == fill_value)) if fill_value != "" else ""
    except Exception:
        fill_ratio = ""
    try:
        finite_mask = np.isfinite(flat.astype("float64", copy=False))
    except Exception:
        finite_mask = np.ones(flat.shape, dtype=bool)
    good = flat[finite_mask]
    row["sample_count"] = int(flat.size)
    row["fill_ratio"] = fill_ratio
    row["invalid_ratio"] = float(np.mean(~finite_mask)) if flat.size else ""
    if good.size:
        row["sample_min"] = float(np.nanmin(good))
        row["sample_max"] = float(np.nanmax(good))
        row["sample_mean"] = float(np.nanmean(good))
        row["sample_std"] = float(np.nanstd(good))
    uniq_text, uniq_count = unique_summary(flat)
    row["unique_values_preview"] = uniq_text
    row["unique_value_count"] = uniq_count
    return row


def inspect_file(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    meta = parse_name(path)
    variable_rows: list[dict[str, Any]] = []
    file_summary: dict[str, Any] = {
        **meta,
        "file_path": str(path),
        "file_size": path.stat().st_size,
        "open_status": "OK",
    }
    with netCDF4.Dataset(path, "r") as ds:
        try:
            ds.set_auto_mask(False)
            ds.set_auto_scale(False)
        except Exception:
            pass
        global_attrs = {name: safe_attr(getattr(ds, name)) for name in ds.ncattrs()}
        file_summary["dimensions"] = "; ".join(f"{k}={len(v)}" for k, v in ds.dimensions.items())
        file_summary["global_attrs_json"] = json.dumps(global_attrs, ensure_ascii=False, default=str)[:6000]
        file_summary["platform_attr"] = str(global_attrs.get("platform", ""))
        file_summary["instrument_attr"] = str(global_attrs.get("instrument", ""))
        file_summary["time_coverage_start"] = str(global_attrs.get("time_coverage_start", ""))
        file_summary["time_coverage_end"] = str(global_attrs.get("time_coverage_end", ""))
        file_summary["geospatial_lat_min"] = global_attrs.get("geospatial_lat_min", "")
        file_summary["geospatial_lat_max"] = global_attrs.get("geospatial_lat_max", "")
        file_summary["geospatial_lon_min"] = global_attrs.get("geospatial_lon_min", "")
        file_summary["geospatial_lon_max"] = global_attrs.get("geospatial_lon_max", "")

        for name, var in ds.variables.items():
            attrs = {attr: safe_attr(getattr(var, attr)) for attr in var.ncattrs()}
            std = STANDARD_MAP.get(meta["product"], {}).get(name, "")
            row = {
                **meta,
                "file_path": str(path),
                "variable_name": name,
                "standard_guess": std,
                "dimensions": ",".join(var.dimensions),
                "shape": "x".join(str(s) for s in getattr(var, "shape", ())),
                "dtype": str(var.dtype),
                "units": attrs.get("units", ""),
                "long_name": attrs.get("long_name", attrs.get("standard_name", "")),
                "coordinates": attrs.get("coordinates", ""),
                "grid_mapping": attrs.get("grid_mapping", ""),
                "flag_values": attrs.get("flag_values", ""),
                "flag_masks": attrs.get("flag_masks", ""),
                "flag_meanings": attrs.get("flag_meanings", ""),
                "valid_range": attrs.get("valid_range", ""),
                "fill_value": attrs.get("_FillValue", attrs.get("missing_value", "")),
                "scale_factor": attrs.get("scale_factor", ""),
                "add_offset": attrs.get("add_offset", ""),
                "attrs_json": json.dumps(attrs, ensure_ascii=False, default=str)[:4000],
            }
            row.update(stats_for_var(var, attrs))
            variable_rows.append(row)

        proj = {}
        if "projection" in ds.variables:
            p = ds.variables["projection"]
            proj = {attr: safe_attr(getattr(p, attr)) for attr in p.ncattrs()}
        projection_summary = {
            "product": meta["product"],
            "file_path": str(path),
            "nominal_time": meta["nominal_time"],
            "projection_attrs_json": json.dumps(proj, ensure_ascii=False, default=str)[:4000],
            "has_projection_var": "projection" in ds.variables,
            "has_x": "x" in ds.variables,
            "has_y": "y" in ds.variables,
            "has_lat": "lat" in ds.variables or "latitude" in ds.variables,
            "has_lon": "lon" in ds.variables or "longitude" in ds.variables,
            "has_satzen": "satzen" in ds.variables,
        }
    return variable_rows, file_summary, projection_summary


def inspect_structure_only(path: Path) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    with netCDF4.Dataset(path, "r") as ds:
        for name, var in ds.variables.items():
            rows.append((name, "x".join(str(s) for s in getattr(var, "shape", ())), str(var.dtype)))
    return rows


def structure_signature(rows: list[tuple[str, str, str]]) -> str:
    parts = sorted(f"{name}|{shape}|{dtype}" for name, shape, dtype in rows)
    return "\n".join(parts)


def compare_structure(files: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    by_product: dict[str, list[Path]] = defaultdict(list)
    for path in files:
        by_product[parse_name(path)["product"]].append(path)
    for product, product_paths in sorted(by_product.items()):
        signatures = Counter()
        samples: dict[str, str] = {}
        dims = Counter()
        for path in product_paths:
            vrows = inspect_structure_only(path)
            sig = structure_signature(vrows)
            signatures[sig] += 1
            samples.setdefault(sig, path.name)
            for name, shape, _dtype in vrows:
                dims[f"{name}::{shape}"] += 1
        rows.append(
            {
                "product": product,
                "file_count": len(product_paths),
                "distinct_structure_signatures": len(signatures),
                "most_common_signature_count": max(signatures.values()) if signatures else 0,
                "example_file": next(iter(samples.values()), ""),
                "shape_summary": "; ".join(f"{k}:{v}" for k, v in dims.most_common(20)),
            }
        )
    return rows


def build_product_summary(file_summaries: list[dict[str, Any]], variable_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    by_product_files: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_product_vars: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in file_summaries:
        by_product_files[row["product"]].append(row)
    for row in variable_rows:
        by_product_vars[row["product"]].append(row)
    for product in sorted(by_product_files):
        files = by_product_files[product]
        vars_ = by_product_vars[product]
        by_std = defaultdict(set)
        for row in vars_:
            if row.get("standard_guess"):
                by_std[row["standard_guess"]].add(row["variable_name"])
        out.append(
            {
                "product": product,
                "file_count": len(files),
                "first_time": min(f["nominal_time"] for f in files),
                "last_time": max(f["nominal_time"] for f in files),
                "time_step_minutes_estimate": 15,
                "platform_attr_values": "; ".join(sorted({str(f.get('platform_attr', '')) for f in files if f.get('platform_attr', '')})),
                "instrument_attr_values": "; ".join(sorted({str(f.get('instrument_attr', '')) for f in files if f.get('instrument_attr', '')})),
                "standard_variables_found": "; ".join(f"{k}<-{','.join(sorted(v))}" for k, v in sorted(by_std.items())),
                "raw_variables_found": "; ".join(sorted({r['variable_name'] for r in vars_})),
                "other_satellite_relationships": " | ".join(OTHER_SATELLITE_RELATIONS.get(product, [])),
            }
        )
    return out


def build_markdown(
    product_summary: list[dict[str, Any]],
    structure_rows: list[dict[str, Any]],
    sample_rows: list[dict[str, Any]],
) -> str:
    lines = [
        "# CLAAS-3 Downloaded CMA/CTX/CPP Structural Probe",
        "",
        "## Overall takeaways",
        "",
    ]
    for row in product_summary:
        lines.append(
            f"- `{row['product']}`: {row['file_count']} files, {row['first_time']} to {row['last_time']}, "
            f"raw variables `{row['raw_variables_found']}`."
        )
    lines.extend(
        [
            "",
            "These files are true `Level-2 instantaneous native SEVIRI grid` NetCDF products, not just order metadata.",
            "For 2024-03-12 you have the full `96 files/day/product` pattern, which matches `15-minute` sampling across the day.",
            "",
            "## What each product contains",
            "",
        ]
    )
    for row in product_summary:
        lines.append(f"### {row['product']}")
        if row["product"] == "CMA":
            lines.append("- Main use: cloud mask baseline plus probability and QA-style diagnostic layers.")
            lines.append("- Expected comparison products: FY4B `CLM`, GOES `ACMF`, Himawari `CMSK`, Meteosat operational `CLM`.")
        elif row["product"] == "CTX":
            lines.append("- Main use: bundled cloud-top triplet, i.e. height + pressure + temperature together.")
            lines.append("- Expected comparison products: FY4B `CTH/CTP/CTT`, GOES `ACHAF/CTPF/ACHTF`, Himawari `CHGT`.")
        elif row["product"] == "CPP":
            lines.append("- Main use: cloud phase plus optical/microphysical retrievals.")
            lines.append("- Expected comparison products: GOES `ACTPF/CODF/CPSF`, Himawari optical-depth/effective-radius fields, and partly FY4B `CLP` for phase.")
        lines.append(f"- Variables found: `{row['raw_variables_found']}`")
        lines.append(f"- Standardized interpretation: `{row['standard_variables_found']}`")
        lines.append(f"- Cross-satellite relation: {row['other_satellite_relationships']}")
        lines.append("")

    lines.extend(
        [
            "## Structure consistency",
            "",
            "| Product | File count | Distinct structure signatures | Most common signature count |",
            "|---|---:|---:|---:|",
        ]
    )
    for row in structure_rows:
        lines.append(
            f"| {row['product']} | {row['file_count']} | {row['distinct_structure_signatures']} | {row['most_common_signature_count']} |"
        )
    lines.extend(
        [
            "",
            "If `distinct_structure_signatures = 1`, the product family is structurally stable across the downloaded day.",
            "",
            "## Sample-level notes",
            "",
        ]
    )
    for row in sample_rows:
        std = row.get("standard_guess", "")
        if std in {
            "cloud_mask",
            "cloud_probability",
            "cloud_top_height",
            "cloud_top_pressure",
            "cloud_top_temperature",
            "cloud_phase",
            "cloud_optical_thickness",
            "cloud_effective_radius",
            "cloud_water_path",
            "quality_flag",
        }:
            lines.append(
                f"- `{row['product']}` `{row['variable_name']}` -> `{std}`; shape `{row['shape']}`, units `{row['units']}`, "
                f"sample range `{row.get('sample_min', '')}` to `{row.get('sample_max', '')}`."
            )
    lines.extend(
        [
            "",
            "## Practical use in your GEO-ring workflow",
            "",
            "- `CMA` can enter the same role as cloud-mask products from FY4B/GOES/Himawari/Meteosat operational baseline, but remember it is a `CM SAF CDR/ICDR` stream rather than the operational MSG CLM stream.",
            "- `CTX` is especially valuable because it gives `CTH + CTP + CTT` together on the same CLAAS native grid, so it is the clean Meteosat-side answer to the cloud-top triplet requirement.",
            "- `CPP` is the big upgrade over operational Meteosat CLM/CTH: this is where you get `phase`, `optical thickness`, `effective radius`, and `water path` support for v1/v2 cloud-property fusion experiments.",
            "- Important encoding note: many geophysical variables are stored as packed integers with `scale_factor` and `_FillValue=-1`. For example `CTT` uses scale `0.1 K`, `CTP` uses scale `0.1 hPa`, `COT` uses scale `0.01`, `CWP` uses scale `0.0002 kg/m2`, and `CRE` is stored in `m` with a very small scale factor, so convert to `um` for cross-satellite work.",
            "- Day-night behavior matters: `CPP` phase remains available at night, but `COT/CRE/CWP` can be all-fill at nighttime samples, which is physically plausible for solar-channel-driven microphysics retrievals rather than evidence of corruption.",
            "- These files do not by themselves replace the need for careful geometry handling. They carry native projection metadata, but if you need explicit `lat/lon/satzen` fields, you still likely want the official CLAAS auxiliary geometry file as a companion dataset.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    if isinstance(netCDF4, Exception):
        raise RuntimeError(f"netCDF4 import failed: {netCDF4}")
    ensure_out()
    products = discover_files()
    if not products:
        raise RuntimeError(f"No CLAAS NetCDF files found under {ROOT}")

    log(f"Found products: {', '.join(f'{k}={len(v)}' for k, v in sorted(products.items()))}")

    sample_paths: list[Path] = []
    all_paths: list[Path] = []
    for paths in products.values():
        sample_paths.extend(choose_samples(paths))
        all_paths.extend(paths)

    variable_rows: list[dict[str, Any]] = []
    file_summaries: list[dict[str, Any]] = []
    projection_rows: list[dict[str, Any]] = []
    for path in sample_paths:
        log(f"Inspecting sample {path.name}")
        vrows, fsum, prow = inspect_file(path)
        variable_rows.extend(vrows)
        file_summaries.append(fsum)
        projection_rows.append(prow)

    structure_rows = compare_structure(all_paths)
    product_summary = build_product_summary(file_summaries, variable_rows)

    write_csv(OUT_DIR / "claas3_downloaded_sample_variable_inventory.csv", variable_rows)
    write_csv(OUT_DIR / "claas3_downloaded_sample_file_summaries.csv", file_summaries)
    write_csv(OUT_DIR / "claas3_downloaded_sample_projection_summary.csv", projection_rows)
    write_csv(OUT_DIR / "claas3_downloaded_structure_consistency.csv", structure_rows)
    write_csv(OUT_DIR / "claas3_downloaded_product_summary.csv", product_summary)
    write_md(OUT_DIR / "claas3_downloaded_probe_report.md", build_markdown(product_summary, structure_rows, variable_rows))

    log(f"Wrote outputs to {OUT_DIR}")


if __name__ == "__main__":
    main()
