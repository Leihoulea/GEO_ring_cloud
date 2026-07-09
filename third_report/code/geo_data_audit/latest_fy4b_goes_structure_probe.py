from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import h5py
import netCDF4
import numpy as np


OUT_DIR = Path(r"D:\AAAresearch_paper\data_check_report\latest_fy4b_goes_structure_probe")
FY4B_GEO_ROOT = Path(r"D:\AAAresearch_paper\data\FY4B-GEO")
FY4B_CTP_ROOT = Path(r"D:\AAAresearch_paper\data\FY4B-CTP")
GOES_ROOT = Path(r"E:\GEO_Cloud_2024")

FY4B_TIME_RE = re.compile(r"_(20\d{12})_(20\d{12})_([0-9]+M)_V([0-9A-Z.]+)", re.I)
GOES_RE = re.compile(r"OR_ABI-(?P<level>L\d\w*)-(?P<product>[A-Z0-9]+)-.*?_(?P<sat>G1[68])_s(?P<stamp>\d{13})", re.I)

MAX_STATS_VALUES = 250_000
TARGET_GOES_PRODUCTS = {"ACMF", "ACHAF", "ACTPF", "ACHTF", "CTPF", "CODF", "CPSF"}


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = []
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
            writer.writerow({k: row.get(k, "") for k in fields})


def safe_json(value: Any, limit: int = 1600) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return text[:limit]


def norm_goes_product(value: str) -> str:
    value = value.upper().replace("-", "").replace("_", "")
    return {"CTTF": "ACHTF"}.get(value, value)


def goes_time_from_stamp(stamp: str) -> str:
    try:
        year = int(stamp[:4])
        doy = int(stamp[4:7])
        hour = int(stamp[7:9])
        minute = int(stamp[9:11])
        second = int(stamp[11:13])
        dt = datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=doy - 1, hours=hour, minutes=minute, seconds=second)
        return dt.isoformat().replace("+00:00", "Z")
    except Exception:
        return ""


def parse_file(path: Path) -> dict[str, Any]:
    name = path.name
    upper = str(path).upper()
    rec: dict[str, Any] = {
        "file_path": str(path),
        "file_name": name,
        "file_size": path.stat().st_size,
        "suffix": path.suffix.lower(),
        "satellite_family": "",
        "satellite": "",
        "product": "",
        "nominal_time": "",
        "start_time": "",
        "end_time": "",
        "resolution": "",
        "version": "",
        "parse_status": "partial",
    }
    if "FY4B" in upper:
        rec.update({"satellite_family": "FY4B", "satellite": "FY4B", "sensor": "AGRI"})
        if "FY4B-GEO" in upper or "_GEO-" in upper or "_GEO_" in upper:
            rec["product"] = "GEO"
        elif "FY4B-CTP" in upper or "_CTP-" in upper or "_CTP_" in upper:
            rec["product"] = "CTP"
        m = FY4B_TIME_RE.search(name)
        if m:
            rec["start_time"] = m.group(1)
            rec["end_time"] = m.group(2)
            rec["nominal_time"] = m.group(1)[:10]
            rec["resolution"] = m.group(3)
            rec["version"] = m.group(4)
            rec["parse_status"] = "parsed"
        return rec
    m = GOES_RE.search(name)
    if m:
        sat = {"G16": "GOES-16", "G18": "GOES-18"}.get(m.group("sat").upper(), m.group("sat").upper())
        rec.update(
            {
                "satellite_family": "GOES",
                "satellite": sat,
                "sensor": "ABI",
                "product": norm_goes_product(m.group("product")),
                "nominal_time": goes_time_from_stamp(m.group("stamp"))[:13] + ":00:00Z",
                "start_time": goes_time_from_stamp(m.group("stamp")),
                "resolution": "full_disk",
                "parse_status": "parsed",
            }
        )
        return rec
    if "GOES-16" in upper or "GOES-18" in upper:
        sat = "GOES-18" if "GOES-18" in upper else "GOES-16"
        product = ""
        for prod in TARGET_GOES_PRODUCTS:
            if f"\\{prod}\\" in upper or f"/{prod}/" in upper:
                product = prod
                break
        rec.update({"satellite_family": "GOES", "satellite": sat, "sensor": "ABI", "product": product})
    return rec


def iter_target_files() -> list[Path]:
    files: list[Path] = []
    for root in [FY4B_GEO_ROOT, FY4B_CTP_ROOT]:
        if root.exists():
            files.extend([p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in {".hdf", ".h5", ".nc"}])
    for sat in ["GOES-16", "GOES-18"]:
        sat_root = GOES_ROOT / sat
        if not sat_root.exists():
            continue
        for prod in TARGET_GOES_PRODUCTS:
            prod_root = sat_root / prod
            if prod_root.exists():
                files.extend([p for p in prod_root.rglob("*.nc") if p.is_file()])
    return sorted(set(files))


def choose_samples(file_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in file_rows:
        if row.get("product"):
            groups[(row.get("satellite_family", ""), row.get("satellite", ""), row.get("product", ""))].append(row)
    samples = []
    for key, rows in sorted(groups.items()):
        rows = sorted(rows, key=lambda r: (str(r.get("nominal_time")), str(r.get("file_path"))))
        idxs = [0, len(rows) // 2, len(rows) - 1] if len(rows) > 2 else list(range(len(rows)))
        for idx in sorted(set(idxs)):
            sample = dict(rows[idx])
            sample["sample_position"] = "first" if idx == 0 else "last" if idx == len(rows) - 1 else "middle"
            samples.append(sample)
    return samples


def standard_guess(name: str, product: str) -> str:
    low = name.lower()
    prod = product.upper()
    if prod == "GEO":
        if "lat" in low:
            return "latitude"
        if "lon" in low:
            return "longitude"
        if "solarzenith" in low or low in {"sza"}:
            return "solar_zenith_angle"
        if "solarazimuth" in low or low in {"saa"}:
            return "solar_azimuth_angle"
        if "sensorzenith" in low or "satellitezenith" in low or low in {"vza"}:
            return "sensor_zenith_angle"
        if "sensorazimuth" in low or "satelliteazimuth" in low or low in {"vaa"}:
            return "sensor_azimuth_angle"
    if prod == "CTP" and ("ctp" in low or "pressure" in low):
        return "cloud_top_pressure"
    if prod in {"ACMF"} and low in {"acm", "bcm"}:
        return "cloud_mask"
    if prod in {"ACHAF"} and low == "ht":
        return "cloud_top_height"
    if prod in {"ACHTF"} and low == "temp":
        return "cloud_top_temperature"
    if prod == "CTPF" and low == "pres":
        return "cloud_top_pressure"
    if prod == "ACTPF" and "phase" in low:
        return "cloud_phase"
    if prod == "CODF" and low == "cod":
        return "cloud_optical_thickness"
    if prod == "CPSF" and low == "cps":
        return "cloud_effective_radius"
    if low == "dqf" or "quality" in low:
        return "quality_flag"
    if low == "x":
        return "projection_x"
    if low == "y":
        return "projection_y"
    if "projection" in low:
        return "geostationary_projection"
    if low in {"t", "time"}:
        return "observation_time"
    return ""


def sample_array_stats(arr: Any) -> dict[str, Any]:
    out = {"sample_min": "", "sample_max": "", "sample_mean": "", "fill_ratio": "", "invalid_ratio": ""}
    try:
        data = np.asarray(arr)
        if data.size == 0 or data.dtype.kind in {"S", "U", "O"}:
            return out
        flat = data.reshape(-1)
        if flat.size > MAX_STATS_VALUES:
            step = max(1, flat.size // MAX_STATS_VALUES)
            flat = flat[::step][:MAX_STATS_VALUES]
        mask = np.isfinite(flat.astype("float64", copy=False))
        out["invalid_ratio"] = float((~mask).sum() / flat.size) if flat.size else ""
        good = flat[mask]
        if good.size:
            out["sample_min"] = float(np.nanmin(good))
            out["sample_max"] = float(np.nanmax(good))
            out["sample_mean"] = float(np.nanmean(good))
    except Exception as exc:
        out["stats_error"] = f"{type(exc).__name__}: {exc}"
    return out


def inspect_hdf(path: Path, meta: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    with h5py.File(path, "r") as h5:
        def visitor(name: str, obj: Any) -> None:
            if not hasattr(obj, "shape") or not hasattr(obj, "dtype"):
                return
            attrs = {k: obj.attrs.get(k) for k in obj.attrs.keys()}
            row = {
                **meta,
                "group_path": "/" + name,
                "variable_name": name.split("/")[-1],
                "standard_guess": standard_guess(name.split("/")[-1], meta.get("product", "")),
                "shape": "x".join(str(x) for x in obj.shape),
                "dtype": str(obj.dtype),
                "units": attrs.get("units", ""),
                "long_name": attrs.get("long_name", attrs.get("description", "")),
                "fill_value": attrs.get("_FillValue", attrs.get("FillValue", "")),
                "scale_factor": attrs.get("scale_factor", ""),
                "add_offset": attrs.get("add_offset", ""),
                "attributes_json": safe_json(attrs),
                "open_status": "OK",
            }
            try:
                row.update(sample_array_stats(obj[...]))
            except Exception as exc:
                row["stats_error"] = f"{type(exc).__name__}: {exc}"
            rows.append(row)
        h5.visititems(visitor)
        rows.append({**meta, "group_path": "/", "variable_name": "__GLOBAL_ATTRS__", "attributes_json": safe_json({k: h5.attrs.get(k) for k in h5.attrs.keys()}), "open_status": "OK"})
    return rows


def inspect_netcdf(path: Path, meta: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    ds = netCDF4.Dataset(path)
    try:
        rows.append({**meta, "group_path": "/", "variable_name": "__GLOBAL_ATTRS__", "attributes_json": safe_json({k: getattr(ds, k) for k in ds.ncattrs()}), "open_status": "OK"})
        for name, var in ds.variables.items():
            attrs = {k: getattr(var, k) for k in var.ncattrs()}
            row = {
                **meta,
                "group_path": "/",
                "variable_name": name,
                "standard_guess": standard_guess(name, meta.get("product", "")),
                "shape": "x".join(str(x) for x in getattr(var, "shape", ())),
                "dtype": str(getattr(var, "dtype", "")),
                "units": attrs.get("units", ""),
                "long_name": attrs.get("long_name", attrs.get("standard_name", "")),
                "fill_value": attrs.get("_FillValue", attrs.get("missing_value", "")),
                "scale_factor": attrs.get("scale_factor", ""),
                "add_offset": attrs.get("add_offset", ""),
                "coordinates": attrs.get("coordinates", ""),
                "grid_mapping": attrs.get("grid_mapping", ""),
                "attributes_json": safe_json(attrs),
                "open_status": "OK",
            }
            try:
                row.update(sample_array_stats(var[:]))
            except Exception as exc:
                row["stats_error"] = f"{type(exc).__name__}: {exc}"
            rows.append(row)
    finally:
        ds.close()
    return rows


def inspect_samples(samples: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    variables = []
    anomalies = []
    for sample in samples:
        path = Path(sample["file_path"])
        try:
            if path.suffix.lower() in {".hdf", ".h5"}:
                variables.extend(inspect_hdf(path, sample))
            elif path.suffix.lower() == ".nc":
                variables.extend(inspect_netcdf(path, sample))
            else:
                anomalies.append({**sample, "severity": "WARN", "issue": "unsupported_suffix"})
        except Exception as exc:
            anomalies.append({**sample, "severity": "ERROR", "issue": "open_failed", "detail": f"{type(exc).__name__}: {exc}"})
    return variables, anomalies


def summarize(file_rows: list[dict[str, Any]], var_rows: list[dict[str, Any]], anomalies: list[dict[str, Any]]) -> str:
    counts = Counter((r.get("satellite"), r.get("product")) for r in file_rows if r.get("product"))
    variables_by_product: dict[tuple[str, str], set[str]] = defaultdict(set)
    standards_by_product: dict[tuple[str, str], set[str]] = defaultdict(set)
    shapes_by_product: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for row in var_rows:
        if row.get("variable_name", "").startswith("__"):
            continue
        key = (row.get("satellite", ""), row.get("product", ""))
        variables_by_product[key].add(row.get("variable_name", ""))
        if row.get("standard_guess"):
            standards_by_product[key].add(row.get("standard_guess", ""))
        shapes_by_product[(key[0], key[1], row.get("standard_guess") or row.get("variable_name", ""))].add(row.get("shape", ""))
    lines = [
        "# Latest FY4B/GOES Structure Probe",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "Scope: newly supplied FY4B GEO/CTP roots plus GOES cloud products currently under E:\\GEO_Cloud_2024. This probe reads first/middle/last samples per satellite/product and does not modify source data.",
        "",
        "## File Counts",
        "",
        "| satellite | product | files |",
        "| --- | --- | --- |",
    ]
    for (sat, product), count in sorted(counts.items()):
        lines.append(f"| {sat} | {product} | {count} |")
    lines.extend(["", "## Variables And Standard Guesses", "", "| satellite | product | variables | standard guesses |", "| --- | --- | --- | --- |"])
    for key in sorted(variables_by_product):
        lines.append(f"| {key[0]} | {key[1]} | {'; '.join(sorted(variables_by_product[key]))[:500]} | {'; '.join(sorted(standards_by_product[key]))} |")
    lines.extend(["", "## Shape Summary", "", "| satellite | product | variable_or_standard | shapes |", "| --- | --- | --- | --- |"])
    for key, shapes in sorted(shapes_by_product.items()):
        lines.append(f"| {key[0]} | {key[1]} | {key[2]} | {'; '.join(sorted(shapes))} |")
    lines.extend(["", "## Anomalies", ""])
    if anomalies:
        for item in anomalies[:80]:
            lines.append(f"- {item.get('severity')} {item.get('satellite')} {item.get('product')} {item.get('file_path')}: {item.get('issue')} {item.get('detail','')}")
    else:
        lines.append("- No sample open anomalies.")
    lines.extend([
        "",
        "## Practical Interpretation",
        "",
        "- FY4B GEO is expected to provide geolocation/navigation arrays for the 4 km grid; confirm exact variable names and shapes in `latest_fy4b_goes_variable_inventory.csv`.",
        "- FY4B CTP is expected to provide cloud-top pressure; if the sampled files expose CTP-like variables and 4 km-compatible shape, it can fill the FY4B v1 pressure gap.",
        "- GOES ACTPF/ACHTF/CTPF/CODF/CPSF are now treated as real downloaded products and should provide phase, temperature, pressure, optical depth, and particle size respectively, each with DQF and fixed-grid geometry variables.",
    ])
    return "\n".join(lines)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    files = iter_target_files()
    file_rows = [parse_file(p) for p in files]
    samples = choose_samples(file_rows)
    var_rows, anomalies = inspect_samples(samples)
    write_csv(OUT_DIR / "latest_fy4b_goes_file_inventory.csv", file_rows)
    write_csv(OUT_DIR / "latest_fy4b_goes_sample_selection.csv", samples)
    write_csv(OUT_DIR / "latest_fy4b_goes_variable_inventory.csv", var_rows)
    write_csv(OUT_DIR / "latest_fy4b_goes_anomalies.csv", anomalies)
    (OUT_DIR / "latest_fy4b_goes_structure_report.md").write_text(summarize(file_rows, var_rows, anomalies), encoding="utf-8-sig")
    print(f"files={len(file_rows)} samples={len(samples)} variables={len(var_rows)} anomalies={len(anomalies)}")
    print(OUT_DIR / "latest_fy4b_goes_structure_report.md")


if __name__ == "__main__":
    main()
