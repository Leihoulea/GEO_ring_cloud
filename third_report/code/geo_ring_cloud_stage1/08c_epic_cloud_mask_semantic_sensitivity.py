from __future__ import annotations

import argparse
import csv
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import netCDF4
import numpy as np
from matplotlib.colors import BoundaryNorm, ListedColormap


SOURCE_NAMES = {
    1: "GOES-16",
    2: "GOES-18",
    3: "FY4B",
    4: "Himawari-9",
    5: "Meteosat-0deg",
    6: "Meteosat-IODC",
    7: "CLAAS3-0deg",
}

EPIC_POLICIES = {
    "A_inclusive_binary": {
        "description": "EPIC 1/2=clear, 3/4=cloud; GEO 0/1=clear, 2/3=cloud",
        "epic": {1: 0, 2: 0, 3: 1, 4: 1},
        "geo": {0: 0, 1: 0, 2: 1, 3: 1},
        "labels": {0: "clear", 1: "cloud"},
    },
    "B_high_confidence_only": {
        "description": "EPIC 1=clear, 4=cloud; GEO 0=clear, 3=cloud; low-confidence/probable classes excluded",
        "epic": {1: 0, 4: 1},
        "geo": {0: 0, 3: 1},
        "labels": {0: "clear", 1: "cloud"},
    },
    "C_uncertainty_aware_3class": {
        "description": "EPIC 1=clear, 2/3=uncertain, 4=cloud; GEO 0=clear, 1/2=uncertain, 3=cloud",
        "epic": {1: 0, 2: 1, 3: 1, 4: 2},
        "geo": {0: 0, 1: 1, 2: 1, 3: 2},
        "labels": {0: "clear", 1: "uncertain", 2: "cloud"},
    },
}

EPIC_CODE_TABLE = {
    0: "non_earth_pixel",
    1: "clear_high_confidence",
    2: "clear_low_confidence",
    3: "cloud_low_confidence",
    4: "cloud_high_confidence",
}

GEORING_CODE_TABLE = {
    0: "clear",
    1: "probably_clear",
    2: "probably_cloudy",
    3: "cloudy",
    -9999: "invalid_or_unfilled",
}


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def parse_time_from_name(path: Path) -> str:
    match = re.search(r"(20\d{12})", path.name)
    if not match:
        return ""
    dt = datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def target_delta_min(epic_time: str, target_time: str) -> float:
    if not epic_time or not target_time:
        return math.nan
    a = datetime.fromisoformat(epic_time.replace("Z", "+00:00"))
    b = datetime.fromisoformat(target_time.replace("Z", "+00:00"))
    return abs((a - b).total_seconds()) / 60.0


def iter_nc_variables(group: netCDF4.Group | netCDF4.Dataset, prefix: str = "") -> list[tuple[str, netCDF4.Variable]]:
    rows: list[tuple[str, netCDF4.Variable]] = []
    for name, var in group.variables.items():
        rows.append((f"{prefix}{name}", var))
    for gname, child in group.groups.items():
        rows.extend(iter_nc_variables(child, f"{prefix}{gname}/"))
    return rows


def find_var_name(ds: netCDF4.Dataset, include: list[str], exclude: list[str] | None = None) -> str | None:
    exclude = exclude or []
    candidates: list[tuple[int, str]] = []
    for name, var in iter_nc_variables(ds):
        low = name.lower()
        attrs = {k: str(getattr(var, k)).lower() for k in var.ncattrs()}
        text = " ".join([low, attrs.get("long_name", ""), attrs.get("standard_name", ""), attrs.get("description", "")])
        if all(term in text for term in include) and not any(term in text for term in exclude):
            candidates.append((len(name), name))
    return sorted(candidates)[0][1] if candidates else None


def read_nc_var(ds: netCDF4.Dataset, name: str) -> np.ndarray:
    arr = np.asarray(ds[name][:])
    if np.ma.isMaskedArray(arr):
        arr = arr.filled(np.nan)
    return arr


def read_epic_l2(path: Path, cloud_var: str | None, lat_var: str | None, lon_var: str | None) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, str]]:
    with netCDF4.Dataset(path) as ds:
        cvar = cloud_var or find_var_name(ds, ["cloud", "mask"])
        yvar = lat_var or find_var_name(ds, ["lat"])
        xvar = lon_var or find_var_name(ds, ["lon"])
        if not cvar or not yvar or not xvar:
            raise RuntimeError(f"Could not identify EPIC variables: cloud={cvar}, lat={yvar}, lon={xvar}")
        cloud = read_nc_var(ds, cvar).astype(np.float32, copy=False)
        lat = read_nc_var(ds, yvar).astype(np.float32, copy=False)
        lon = read_nc_var(ds, xvar).astype(np.float32, copy=False)
    return cloud, lat, lon, {"cloud_mask": cvar, "latitude": yvar, "longitude": xvar}


def load_npz_array(path: Path) -> tuple[np.ndarray, np.ndarray]:
    with np.load(path, allow_pickle=True) as z:
        data = np.asarray(z["data"])
        valid = np.asarray(z["valid_mask"]).astype(bool) if "valid_mask" in z.files else np.isfinite(data)
    return data, valid


def sample_grid_to_points(data: np.ndarray, valid: np.ndarray, lat: np.ndarray, lon: np.ndarray, grid: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    lon_norm = ((lon.astype(np.float32) + 180.0) % 360.0) - 180.0
    res = float(grid["resolution_degree"])
    row = np.rint((lat - (float(grid["lat_min"]) + res / 2.0)) / res).astype(np.int64)
    col = np.rint((lon_norm - (float(grid["lon_min"]) + res / 2.0)) / res).astype(np.int64)
    ok = np.isfinite(lat) & np.isfinite(lon) & (row >= 0) & (row < data.shape[0]) & (col >= 0) & (col < data.shape[1])
    out = np.full(lat.shape, np.nan, dtype=np.float32)
    out_valid = np.zeros(lat.shape, dtype=bool)
    out[ok] = data[row[ok], col[ok]].astype(np.float32)
    out_valid[ok] = valid[row[ok], col[ok]]
    out[~out_valid] = np.nan
    return out, out_valid


def apply_policy(arr: np.ndarray, mapping: dict[int, int]) -> tuple[np.ndarray, np.ndarray]:
    out = np.full(arr.shape, -1, dtype=np.int16)
    valid = np.zeros(arr.shape, dtype=bool)
    for raw_code, mapped_code in mapping.items():
        m = arr == raw_code
        out[m] = mapped_code
        valid |= m
    return out, valid


def binary_metrics(epic: np.ndarray, geo: np.ndarray, valid: np.ndarray) -> dict[str, Any]:
    if np.count_nonzero(valid) == 0:
        return {"status": "NO_OVERLAP", "n": 0}
    e = epic[valid].astype(np.int8)
    g = geo[valid].astype(np.int8)
    tp = int(np.count_nonzero((e == 1) & (g == 1)))
    tn = int(np.count_nonzero((e == 0) & (g == 0)))
    fp = int(np.count_nonzero((e == 0) & (g == 1)))
    fn = int(np.count_nonzero((e == 1) & (g == 0)))
    n = int(e.size)
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-12)
    iou = tp / max(tp + fp + fn, 1)
    return {
        "status": "OK",
        "n": n,
        "agreement": float((tp + tn) / max(n, 1)),
        "precision": float(precision),
        "recall": float(recall),
        "f1": float(f1),
        "iou": float(iou),
        "tp": tp,
        "tn": tn,
        "fp": fp,
        "fn": fn,
        "epic_cloud_fraction": float(np.mean(e == 1)),
        "geo_cloud_fraction": float(np.mean(g == 1)),
    }


def multiclass_agreement(epic: np.ndarray, geo: np.ndarray, valid: np.ndarray, uncertain_code: int = 1) -> dict[str, Any]:
    if np.count_nonzero(valid) == 0:
        return {"status": "NO_OVERLAP", "n": 0}
    e = epic[valid].astype(np.int16)
    g = geo[valid].astype(np.int16)
    definite = (e != uncertain_code) & (g != uncertain_code)
    return {
        "status": "OK",
        "n": int(e.size),
        "agreement": float(np.mean(e == g)),
        "both_definite_n": int(np.count_nonzero(definite)),
        "both_definite_agreement": float(np.mean(e[definite] == g[definite])) if np.any(definite) else math.nan,
        "epic_uncertain_fraction": float(np.mean(e == uncertain_code)),
        "geo_uncertain_fraction": float(np.mean(g == uncertain_code)),
        "either_uncertain_fraction": float(np.mean((e == uncertain_code) | (g == uncertain_code))),
    }


def confusion_rows(policy_name: str, epic: np.ndarray, geo: np.ndarray, valid: np.ndarray, labels: dict[int, str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    total = int(np.count_nonzero(valid))
    for ecode, elabel in labels.items():
        for gcode, glabel in labels.items():
            count = int(np.count_nonzero(valid & (epic == ecode) & (geo == gcode)))
            rows.append(
                {
                    "policy": policy_name,
                    "epic_code": ecode,
                    "epic_label": elabel,
                    "georing_code": gcode,
                    "georing_label": glabel,
                    "count": count,
                    "fraction_of_policy_valid": count / max(total, 1),
                }
            )
    return rows


def code_count_rows(prefix: str, arr: np.ndarray, valid: np.ndarray, table: dict[int, str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    vals, counts = np.unique(arr[valid].astype(np.int32), return_counts=True)
    total = int(np.count_nonzero(valid))
    for value, count in zip(vals, counts):
        rows.append(
            {
                "dataset": prefix,
                "code": int(value),
                "meaning": table.get(int(value), ""),
                "count": int(count),
                "fraction": float(count / max(total, 1)),
            }
        )
    return rows


def source_rows(policy_name: str, scope: str, source: np.ndarray, valid: np.ndarray) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    total = int(np.count_nonzero(valid))
    if total == 0:
        return [{"policy": policy_name, "scope": scope, "source_id": "", "source_name": "NO_PIXELS", "pixel_count": 0, "fraction": 0.0}]
    vals, counts = np.unique(source[valid].astype(np.int16), return_counts=True)
    for value, count in zip(vals, counts):
        if value <= 0:
            continue
        rows.append(
            {
                "policy": policy_name,
                "scope": scope,
                "source_id": int(value),
                "source_name": SOURCE_NAMES.get(int(value), f"source_{int(value)}"),
                "pixel_count": int(count),
                "fraction": float(count / max(total, 1)),
            }
        )
    return rows


def quicklook_policy(path: Path, policy_name: str, epic: np.ndarray, geo: np.ndarray, valid: np.ndarray, labels: dict[int, str]) -> None:
    if len(labels) == 2:
        cmap = ListedColormap(["#2b6cb0", "#f7fafc"])
        norm = BoundaryNorm([-0.5, 0.5, 1.5], cmap.N)
    else:
        cmap = ListedColormap(["#2b6cb0", "#f6ad55", "#f7fafc"])
        norm = BoundaryNorm([-0.5, 0.5, 1.5, 2.5], cmap.N)
    cmap.set_bad("#cfcfcf")
    diff = np.full(epic.shape, np.nan, dtype=np.float32)
    diff[valid] = (geo[valid] != epic[valid]).astype(np.float32)
    fig, axes = plt.subplots(1, 3, figsize=(16, 5), constrained_layout=True)
    for ax, arr, title in zip(axes, [epic.astype(float), geo.astype(float), diff], ["EPIC policy class", "GEO-ring policy class", "Mismatch mask"]):
        show = arr.copy()
        show[~valid] = np.nan
        if title == "Mismatch mask":
            im = ax.imshow(show, origin="upper", cmap="Reds", vmin=0, vmax=1, interpolation="nearest")
        else:
            im = ax.imshow(show, origin="upper", cmap=cmap, norm=norm, interpolation="nearest")
        ax.set_title(title)
        ax.axis("off")
        plt.colorbar(im, ax=ax, shrink=0.75)
    fig.suptitle(policy_name)
    fig.savefig(path, dpi=170)
    plt.close(fig)


def build_policy_tables() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    epic_rows = []
    geo_rows = []
    for policy_name, policy in EPIC_POLICIES.items():
        for raw, mapped in policy["epic"].items():
            epic_rows.append(
                {
                    "policy": policy_name,
                    "raw_code": raw,
                    "raw_meaning": EPIC_CODE_TABLE.get(raw, ""),
                    "mapped_code": mapped,
                    "mapped_meaning": policy["labels"].get(mapped, ""),
                    "included": True,
                }
            )
        for raw in sorted(set(EPIC_CODE_TABLE) - set(policy["epic"])):
            epic_rows.append(
                {
                    "policy": policy_name,
                    "raw_code": raw,
                    "raw_meaning": EPIC_CODE_TABLE.get(raw, ""),
                    "mapped_code": "",
                    "mapped_meaning": "excluded",
                    "included": False,
                }
            )
        for raw, mapped in policy["geo"].items():
            geo_rows.append(
                {
                    "policy": policy_name,
                    "standard_code": raw,
                    "standard_meaning": GEORING_CODE_TABLE.get(raw, ""),
                    "mapped_code": mapped,
                    "mapped_meaning": policy["labels"].get(mapped, ""),
                    "included": True,
                }
            )
        for raw in [0, 1, 2, 3]:
            if raw not in policy["geo"]:
                geo_rows.append(
                    {
                        "policy": policy_name,
                        "standard_code": raw,
                        "standard_meaning": GEORING_CODE_TABLE.get(raw, ""),
                        "mapped_code": "",
                        "mapped_meaning": "excluded",
                        "included": False,
                    }
                )
    return epic_rows, geo_rows


def run(args: argparse.Namespace) -> Path:
    time_run_root = Path(args.time_run_root)
    fused_dir = Path(args.fused_dir) if args.fused_dir else time_run_root / "fused_best_source"
    grid_json = Path(args.grid_json) if args.grid_json else time_run_root / "reprojected_grid" / "target_grid_definition.json"
    time_tag = args.time_tag
    out_dir = Path(args.out_dir) if args.out_dir else time_run_root / f"epic_l2_cloud_mask_semantic_sensitivity_{time_tag}"
    ql_dir = out_dir / "quicklooks"
    report_dir = Path(args.report_dir) if args.report_dir else time_run_root / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    ql_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    grid = json.loads(grid_json.read_text(encoding="utf-8"))
    epic_cloud, epic_lat, epic_lon, epic_names = read_epic_l2(Path(args.epic_l2), args.epic_cloud_mask_var, args.epic_lat_var, args.epic_lon_var)
    geo_std, geo_valid = load_npz_array(fused_dir / "fused_cloud_mask.npz")
    source_map, source_valid = load_npz_array(fused_dir / "source_map_cloud_mask.npz")
    geo_on_epic, geo_on_epic_valid = sample_grid_to_points(geo_std, geo_valid, epic_lat, epic_lon, grid)
    source_on_epic, source_on_epic_valid = sample_grid_to_points(source_map, source_valid, epic_lat, epic_lon, grid)

    base_valid = np.isfinite(epic_lat) & np.isfinite(epic_lon) & np.isfinite(epic_cloud) & geo_on_epic_valid
    count_rows = code_count_rows("EPIC_L2_Cloud_Mask_raw", epic_cloud, np.isfinite(epic_cloud), EPIC_CODE_TABLE)
    count_rows.extend(code_count_rows("GEO_ring_fused_cloud_mask_standard_sampled_to_EPIC", geo_on_epic, geo_on_epic_valid, GEORING_CODE_TABLE))
    write_csv(out_dir / "cloud_mask_code_counts_sampled_to_epic.csv", count_rows, ["dataset", "code", "meaning", "count", "fraction"])

    epic_policy_rows, geo_policy_rows = build_policy_tables()
    write_csv(out_dir / "epic_cloud_mask_semantic_policy_table.csv", epic_policy_rows, ["policy", "raw_code", "raw_meaning", "mapped_code", "mapped_meaning", "included"])
    write_csv(out_dir / "georing_cloud_mask_semantic_policy_table.csv", geo_policy_rows, ["policy", "standard_code", "standard_meaning", "mapped_code", "mapped_meaning", "included"])

    metric_rows: list[dict[str, Any]] = []
    source_metric_rows: list[dict[str, Any]] = []
    conf_rows: list[dict[str, Any]] = []
    src_rows: list[dict[str, Any]] = []
    quicklook_rows: list[dict[str, Any]] = []

    for policy_name, policy in EPIC_POLICIES.items():
        epic_class, epic_policy_valid = apply_policy(epic_cloud, policy["epic"])
        geo_class, geo_policy_valid = apply_policy(geo_on_epic, policy["geo"])
        valid = base_valid & epic_policy_valid & geo_policy_valid
        if len(policy["labels"]) == 2:
            metrics = binary_metrics(epic_class, geo_class, valid)
        else:
            metrics = multiclass_agreement(epic_class, geo_class, valid)
        metrics.update(
            {
                "policy": policy_name,
                "description": policy["description"],
                "valid_fraction_of_epic_earth": float(np.count_nonzero(valid) / max(np.count_nonzero(np.isin(epic_cloud, [1, 2, 3, 4])), 1)),
            }
        )
        metric_rows.append(metrics)
        for source_id, source_name in SOURCE_NAMES.items():
            source_valid = valid & source_on_epic_valid & (source_on_epic == source_id)
            if np.count_nonzero(source_valid) == 0:
                continue
            if len(policy["labels"]) == 2:
                sm = binary_metrics(epic_class, geo_class, source_valid)
            else:
                sm = multiclass_agreement(epic_class, geo_class, source_valid)
            sm.update(
                {
                    "policy": policy_name,
                    "source_id": source_id,
                    "source_name": source_name,
                    "source_pixel_fraction_of_policy_valid": float(np.count_nonzero(source_valid) / max(np.count_nonzero(valid), 1)),
                }
            )
            source_metric_rows.append(sm)
        conf_rows.extend(confusion_rows(policy_name, epic_class, geo_class, valid, policy["labels"]))
        src_rows.extend(source_rows(policy_name, "policy_valid_pixels", source_on_epic, valid & source_on_epic_valid))
        mismatch = valid & (epic_class != geo_class)
        src_rows.extend(source_rows(policy_name, "mismatch_pixels", source_on_epic, mismatch & source_on_epic_valid))
        qpath = ql_dir / f"{policy_name}_epic_vs_georing_cloud_mask.png"
        quicklook_policy(qpath, policy_name, epic_class, geo_class, valid, policy["labels"])
        quicklook_rows.append({"policy": policy_name, "quicklook": str(qpath), "description": policy["description"]})

    metric_fields = sorted({k for row in metric_rows for k in row.keys()})
    write_csv(out_dir / "epic_georing_cloud_mask_sensitivity_metrics.csv", metric_rows, metric_fields)
    source_metric_fields = sorted({k for row in source_metric_rows for k in row.keys()})
    write_csv(out_dir / "epic_georing_cloud_mask_metrics_by_source.csv", source_metric_rows, source_metric_fields)
    write_csv(out_dir / "epic_georing_cloud_mask_confusion_by_policy.csv", conf_rows, ["policy", "epic_code", "epic_label", "georing_code", "georing_label", "count", "fraction_of_policy_valid"])
    write_csv(out_dir / "epic_georing_cloud_mask_source_distribution_by_policy.csv", src_rows, ["policy", "scope", "source_id", "source_name", "pixel_count", "fraction"])
    write_csv(out_dir / "quicklook_manifest.csv", quicklook_rows, ["policy", "quicklook", "description"])

    epic_time = parse_time_from_name(Path(args.epic_l2))
    lines = [
        "# 08c EPIC Cloud Mask Semantic Sensitivity Report",
        "",
        f"- Target time: `{args.target_time}`",
        f"- Time tag: `{time_tag}`",
        f"- EPIC L2 file: `{args.epic_l2}`",
        f"- EPIC file time: `{epic_time}`",
        f"- Time delta: `{target_delta_min(epic_time, args.target_time):.3f}` min",
        f"- GEO-ring time-run root: `{time_run_root}`",
        f"- EPIC variables: `{epic_names}`",
        "",
        "## 1. Why This Check Exists",
        "",
        "Cloud-mask products do not share identical code tables. This report compares several explicit semantic policies instead of treating a single binary agreement as a final truth.",
        "",
        "## 2. Policy Metrics",
        "",
    ]
    for row in metric_rows:
        lines.append(f"### {row['policy']}")
        lines.append(f"- Policy: {row['description']}")
        if row.get("status") == "OK":
            for key in ("n", "valid_fraction_of_epic_earth", "agreement", "f1", "iou", "precision", "recall", "epic_cloud_fraction", "geo_cloud_fraction", "both_definite_agreement", "either_uncertain_fraction"):
                if key in row and row[key] != "":
                    value = row[key]
                    lines.append(f"- {key}: {value}")
        else:
            lines.append(f"- status: {row.get('status')}")
        lines.append("")
    lines.extend(
        [
            "## 3. Interpretation",
            "",
            "- Mode A reproduces the current inclusive binary comparison.",
            "- Mode B removes low-confidence/probable categories on both EPIC and GEO-ring sides; it tests whether disagreement is concentrated in uncertain classes.",
            "- Mode C keeps uncertain classes as a third class; it should be used to diagnose whether apparent binary errors are really uncertainty-policy differences.",
            "- If Mode B improves strongly relative to Mode A, the headline cloud binary agreement is mainly limited by confidence-code harmonization rather than by gross spatial misregistration.",
            "- If mismatch pixels are dominated by one GEO source, inspect that source pair and its cloud-mask code table before changing the fusion algorithm.",
            "",
            "## 4. Outputs",
            "",
            f"- `{out_dir / 'epic_georing_cloud_mask_sensitivity_metrics.csv'}`",
            f"- `{out_dir / 'epic_georing_cloud_mask_metrics_by_source.csv'}`",
            f"- `{out_dir / 'epic_georing_cloud_mask_confusion_by_policy.csv'}`",
            f"- `{out_dir / 'epic_georing_cloud_mask_source_distribution_by_policy.csv'}`",
            f"- `{out_dir / 'epic_cloud_mask_semantic_policy_table.csv'}`",
            f"- `{out_dir / 'georing_cloud_mask_semantic_policy_table.csv'}`",
            f"- `{ql_dir}`",
            "",
            "## Gate",
            "",
            "- EPIC_CLOUD_MASK_SEMANTIC_SENSITIVITY_GATE = PASS",
        ]
    )
    report = out_dir / "epic_cloud_mask_semantic_sensitivity_report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    (report_dir / "epic_cloud_mask_semantic_sensitivity_report.md").write_text("\n".join(lines), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="EPIC L2 cloud-mask semantic sensitivity comparison against GEO-ring fused cloud_mask.")
    parser.add_argument("--time-run-root", required=True, help="Root of a GEO-ring single-time run, e.g. ...\\20240319_1500")
    parser.add_argument("--epic-l2", required=True, help="EPIC L2 cloud NetCDF file")
    parser.add_argument("--target-time", required=True, help="Target GEO-ring UTC time, e.g. 2024-03-19T15:00:00Z")
    parser.add_argument("--time-tag", required=True, help="Time tag used by the run, e.g. 20240319_1500")
    parser.add_argument("--out-dir", default="", help="Optional output directory")
    parser.add_argument("--report-dir", default="", help="Optional report mirror directory")
    parser.add_argument("--fused-dir", default="", help="Optional fused_best_source directory")
    parser.add_argument("--grid-json", default="", help="Optional target_grid_definition.json path")
    parser.add_argument("--epic-cloud-mask-var", default="", help="Optional explicit EPIC cloud mask variable path")
    parser.add_argument("--epic-lat-var", default="", help="Optional explicit EPIC latitude variable path")
    parser.add_argument("--epic-lon-var", default="", help="Optional explicit EPIC longitude variable path")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    for key in ("epic_cloud_mask_var", "epic_lat_var", "epic_lon_var", "out_dir", "report_dir", "fused_dir", "grid_json"):
        if getattr(args, key) == "":
            setattr(args, key, None)
    report = run(args)
    print(f"08c PASS: report={report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
