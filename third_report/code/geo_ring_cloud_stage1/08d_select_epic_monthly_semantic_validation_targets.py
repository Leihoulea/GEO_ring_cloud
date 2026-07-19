from __future__ import annotations

import argparse
import csv
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import netCDF4
import numpy as np

from geo_ring_cloud.paths import RUNS_ROOT
from geo_ring_cloud.pipeline_layout import TIME_INDEX_DIR


SATELLITES = {
    "GOES-16": -75.2,
    "GOES-18": -137.0,
    "FY4B": 105.0,
    "Himawari-9": 140.7,
    "Meteosat-0deg": 0.0,
    "Meteosat-IODC": 41.5,
}

EARTH_RADIUS_KM = 6378.137
SATELLITE_RADIUS_KM = EARTH_RADIUS_KM + 35786.023


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def parse_time_from_name(path: Path) -> datetime:
    match = re.search(r"(20\d{12})", path.name)
    if not match:
        raise ValueError(f"cannot parse time: {path.name}")
    return datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def round_to_nearest_hour(dt: datetime) -> datetime:
    base = dt.replace(minute=0, second=0, microsecond=0)
    if (dt - base) >= timedelta(minutes=30):
        base += timedelta(hours=1)
    return base


def iso_z(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def normalize_lon(lon: np.ndarray | float) -> np.ndarray | float:
    return ((np.asarray(lon) + 180.0) % 360.0) - 180.0


def approximate_vza(lat: np.ndarray, lon: np.ndarray, subpoint_lon: float) -> np.ndarray:
    lat_rad = np.deg2rad(lat.astype(np.float64, copy=False))
    dlon = np.deg2rad(normalize_lon(lon.astype(np.float64, copy=False) - subpoint_lon))
    cos_psi = np.cos(lat_rad) * np.cos(dlon)
    visible = cos_psi > (EARTH_RADIUS_KM / SATELLITE_RADIUS_KM)
    numer = SATELLITE_RADIUS_KM * cos_psi - EARTH_RADIUS_KM
    denom = np.sqrt(
        SATELLITE_RADIUS_KM * SATELLITE_RADIUS_KM
        + EARTH_RADIUS_KM * EARTH_RADIUS_KM
        - 2.0 * SATELLITE_RADIUS_KM * EARTH_RADIUS_KM * cos_psi
    )
    cos_vza = np.clip(numer / denom, -1.0, 1.0)
    out = np.full(lat.shape, np.inf, dtype=np.float32)
    out[visible] = np.rad2deg(np.arccos(cos_vza[visible])).astype(np.float32)
    return out


def read_core_time_index(path: Path) -> dict[str, dict[str, Any]]:
    by_time: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            t = row["nominal_time"]
            entry = by_time.setdefault(
                t,
                {
                    "nominal_time": t,
                    "total_present_core": 0,
                    "total_core": 0,
                    "complete_satellite_groups": 0,
                    "satellite_groups": [],
                    "statuses": [],
                },
            )
            present = int(row.get("present_core_count") or 0)
            total = int(row.get("total_core_count") or 0)
            entry["total_present_core"] += present
            entry["total_core"] += total
            if str(row.get("complete_core", "")).lower() == "true":
                entry["complete_satellite_groups"] += 1
            entry["satellite_groups"].append(row.get("satellite_group", ""))
            entry["statuses"].append(f"{row.get('satellite_group')}={row.get('status')}")
    for entry in by_time.values():
        entry["core_completeness_ratio"] = entry["total_present_core"] / max(entry["total_core"], 1)
        entry["all_27_complete"] = entry["total_present_core"] == 27 and entry["total_core"] == 27
    return by_time


def find_var(ds: netCDF4.Dataset, names: list[str]) -> str:
    for candidate in names:
        group, _, var = candidate.rpartition("/")
        obj = ds[group] if group else ds
        if var in obj.variables:
            return candidate
    raise KeyError(names)


def sample_source_fractions(path: Path, stride: int) -> dict[str, Any]:
    with netCDF4.Dataset(path) as ds:
        attrs = {k: getattr(ds, k) for k in ds.ncattrs()}
        lat_name = find_var(ds, ["geolocation_data/latitude", "geolocation_data/Latitude"])
        lon_name = find_var(ds, ["geolocation_data/longitude", "geolocation_data/Longitude"])
        cm_name = find_var(ds, ["geophysical_data/Cloud_Mask", "geophysical_data/cloud_mask"])
        lat = np.asarray(ds[lat_name][::stride, ::stride], dtype=np.float32)
        lon = np.asarray(ds[lon_name][::stride, ::stride], dtype=np.float32)
        cm = np.asarray(ds[cm_name][::stride, ::stride])
    valid = np.isfinite(lat) & np.isfinite(lon) & np.isin(cm, [1, 2, 3, 4])
    if not np.any(valid):
        return {
            "valid_sample_count": 0,
            "dominant_satellite_estimate": "",
            "dominant_fraction_estimate": 0.0,
            "second_satellite_estimate": "",
            "second_fraction_estimate": 0.0,
            "source_fraction_json": "{}",
            "centroid_mean_longitude": attrs.get("centroid_mean_longitude", ""),
            "centroid_mean_latitude": attrs.get("centroid_mean_latitude", ""),
        }
    stack = []
    names = []
    for sat, lon0 in SATELLITES.items():
        stack.append(approximate_vza(lat, lon, lon0))
        names.append(sat)
    vza = np.stack(stack, axis=0)
    best = np.argmin(vza, axis=0)
    best_vza = np.min(vza, axis=0)
    best_valid = valid & np.isfinite(best_vza)
    total = int(np.count_nonzero(best_valid))
    fractions: dict[str, float] = {}
    for idx, sat in enumerate(names):
        fractions[sat] = float(np.count_nonzero(best_valid & (best == idx)) / max(total, 1))
    ranked = sorted(fractions.items(), key=lambda kv: kv[1], reverse=True)
    return {
        "valid_sample_count": total,
        "dominant_satellite_estimate": ranked[0][0],
        "dominant_fraction_estimate": ranked[0][1],
        "second_satellite_estimate": ranked[1][0],
        "second_fraction_estimate": ranked[1][1],
        "source_fraction_json": json.dumps(fractions, ensure_ascii=False, sort_keys=True),
        "centroid_mean_longitude": attrs.get("centroid_mean_longitude", ""),
        "centroid_mean_latitude": attrs.get("centroid_mean_latitude", ""),
        "minimum_longitude": attrs.get("minimum_longitude", ""),
        "maximum_longitude": attrs.get("maximum_longitude", ""),
        "minimum_latitude": attrs.get("minimum_latitude", ""),
        "maximum_latitude": attrs.get("maximum_latitude", ""),
    }


def classify_candidate(row: dict[str, Any]) -> str:
    fractions = json.loads(row["source_fraction_json"])
    east = fractions.get("FY4B", 0.0) + fractions.get("Himawari-9", 0.0)
    west = fractions.get("GOES-16", 0.0) + fractions.get("GOES-18", 0.0)
    msg = fractions.get("Meteosat-0deg", 0.0) + fractions.get("Meteosat-IODC", 0.0)
    dom = row["dominant_satellite_estimate"]
    if east >= 0.55 and row.get("nearest_hour_all_27_complete") == "True":
        return "EAST_ASIA_FY4B_HIMAWARI_PRIORITY"
    if dom in {"FY4B", "Himawari-9"} and row.get("nearest_hour_all_27_complete") == "True":
        return "EAST_ASIA_DOMINANT_SINGLE_SAT"
    if msg >= 0.55 and row.get("nearest_hour_all_27_complete") == "True":
        return "METEOSAT_DOMINANT_CONTROL"
    if west >= 0.55 and row.get("nearest_hour_all_27_complete") == "True":
        return "GOES_DOMINANT_CONTROL"
    if row.get("nearest_hour_all_27_complete") != "True":
        return "GEO_CORE_INCOMPLETE_OR_OUT_OF_INDEX"
    return "MIXED_OR_BOUNDARY"


def score_row(row: dict[str, Any]) -> float:
    fractions = json.loads(row["source_fraction_json"])
    east = fractions.get("FY4B", 0.0) + fractions.get("Himawari-9", 0.0)
    time_penalty = float(row["nearest_hour_delta_min"]) / 60.0
    complete_bonus = 1.0 if row.get("nearest_hour_all_27_complete") == "True" else -10.0
    return east * 10.0 + complete_bonus - time_penalty


def run(args: argparse.Namespace) -> Path:
    epic_dir = Path(args.epic_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    core = read_core_time_index(Path(args.core_time_index))
    rows: list[dict[str, Any]] = []
    files = sorted(epic_dir.glob("*.nc4"))
    if args.max_files > 0:
        files = files[: args.max_files]
    for idx, path in enumerate(files, start=1):
        dt = parse_time_from_name(path)
        nearest = round_to_nearest_hour(dt)
        nearest_iso = iso_z(nearest)
        core_entry = core.get(nearest_iso)
        sample = sample_source_fractions(path, args.stride)
        row = {
            "epic_file": str(path),
            "epic_filename": path.name,
            "epic_time": iso_z(dt),
            "nearest_hour": nearest_iso,
            "nearest_hour_delta_min": abs((dt - nearest).total_seconds()) / 60.0,
            "nearest_hour_total_present_core": core_entry["total_present_core"] if core_entry else "",
            "nearest_hour_total_core": core_entry["total_core"] if core_entry else "",
            "nearest_hour_complete_groups": core_entry["complete_satellite_groups"] if core_entry else "",
            "nearest_hour_all_27_complete": str(bool(core_entry and core_entry["all_27_complete"])),
            "nearest_hour_statuses": "|".join(core_entry["statuses"]) if core_entry else "",
            "file_size": path.stat().st_size,
            **sample,
        }
        row["candidate_class"] = classify_candidate(row)
        row["east_asia_fraction_estimate"] = json.loads(row["source_fraction_json"]).get("FY4B", 0.0) + json.loads(row["source_fraction_json"]).get("Himawari-9", 0.0)
        row["candidate_score"] = score_row(row)
        rows.append(row)
        if idx % 50 == 0:
            print(f"scanned {idx}/{len(files)}")

    fields = [
        "epic_file",
        "epic_filename",
        "epic_time",
        "nearest_hour",
        "nearest_hour_delta_min",
        "nearest_hour_total_present_core",
        "nearest_hour_total_core",
        "nearest_hour_complete_groups",
        "nearest_hour_all_27_complete",
        "candidate_class",
        "candidate_score",
        "east_asia_fraction_estimate",
        "dominant_satellite_estimate",
        "dominant_fraction_estimate",
        "second_satellite_estimate",
        "second_fraction_estimate",
        "source_fraction_json",
        "valid_sample_count",
        "centroid_mean_longitude",
        "centroid_mean_latitude",
        "minimum_longitude",
        "maximum_longitude",
        "minimum_latitude",
        "maximum_latitude",
        "file_size",
        "nearest_hour_statuses",
    ]
    write_csv(out_dir / "epic_202403_geo_source_candidate_inventory.csv", rows, fields)

    eligible = [r for r in rows if r["nearest_hour_all_27_complete"] == "True"]
    east = sorted(
        [r for r in eligible if r["candidate_class"] in {"EAST_ASIA_FY4B_HIMAWARI_PRIORITY", "EAST_ASIA_DOMINANT_SINGLE_SAT", "MIXED_OR_BOUNDARY"}],
        key=score_row,
        reverse=True,
    )[:20]
    controls = []
    for klass in ["GOES_DOMINANT_CONTROL", "METEOSAT_DOMINANT_CONTROL", "MIXED_OR_BOUNDARY"]:
        controls.extend(sorted([r for r in eligible if r["candidate_class"] == klass], key=lambda r: float(r["dominant_fraction_estimate"]), reverse=True)[:5])
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for role, group in [("east_asia_priority", east[:10]), ("control_or_boundary", controls)]:
        for r in group:
            key = r["epic_filename"]
            if key in seen:
                continue
            seen.add(key)
            rr = dict(r)
            rr["selection_role"] = role
            selected.append(rr)
    write_csv(out_dir / "recommended_epic_georing_validation_targets.csv", selected, ["selection_role", *fields])

    lines = [
        "# 08d EPIC Monthly GEO-sector Target Selection Report",
        "",
        f"- EPIC directory: `{epic_dir}`",
        f"- EPIC files scanned: `{len(rows)}`",
        f"- Core time index: `{args.core_time_index}`",
        f"- Stride used for EPIC geolocation sampling: `{args.stride}`",
        "",
        "## Main Point",
        "",
        "Do not tune 06 cloud_mask fusion v2 from a single GOES/Meteosat-looking EPIC scene. This selection identifies EPIC scenes whose sampled disk is expected to be dominated by FY4B/Himawari, plus GOES/Meteosat controls.",
        "",
        "## Recommended East-Asia / FY4B-Himawari Candidates",
        "",
    ]
    for r in east[:10]:
        lines.append(
            f"- `{r['epic_time']}` nearest `{r['nearest_hour']}` delta={float(r['nearest_hour_delta_min']):.2f} min, "
            f"dominant={r['dominant_satellite_estimate']} {float(r['dominant_fraction_estimate']):.3f}, "
            f"FY4B+Himawari={float(r['east_asia_fraction_estimate']):.3f}, class={r['candidate_class']}"
        )
    lines.extend(
        [
            "",
            "## Controls",
            "",
        ]
    )
    for r in controls[:15]:
        lines.append(
            f"- `{r['epic_time']}` nearest `{r['nearest_hour']}` delta={float(r['nearest_hour_delta_min']):.2f} min, "
            f"dominant={r['dominant_satellite_estimate']} {float(r['dominant_fraction_estimate']):.3f}, class={r['candidate_class']}"
        )
    lines.extend(
        [
            "",
            "## Suggested Next Step",
            "",
            "Run the existing parameterized 01/02/05/06/08b/08c pipeline for 2-3 east-Asia candidates and 1-2 controls. Compare semantic-sensitive metrics by source sector before changing the 06 fusion algorithm.",
            "",
            "## Outputs",
            "",
            f"- `{out_dir / 'epic_202403_geo_source_candidate_inventory.csv'}`",
            f"- `{out_dir / 'recommended_epic_georing_validation_targets.csv'}`",
        ]
    )
    report = out_dir / "epic_202403_geo_source_target_selection_report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Select EPIC March 2024 L2 cloud scenes for GEO-ring source-sector semantic validation.")
    p.add_argument("--epic-dir", required=True)
    p.add_argument("--core-time-index", default=str(TIME_INDEX_DIR / "core_time_index.csv"))
    p.add_argument("--out-dir", default=str(RUNS_ROOT / "epic_202403_target_selection"))
    p.add_argument("--stride", type=int, default=32)
    p.add_argument("--max-files", type=int, default=0)
    return p


def main() -> int:
    args = build_parser().parse_args()
    report = run(args)
    print(f"08d PASS: report={report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
