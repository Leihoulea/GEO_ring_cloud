from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
from typing import Any

import netCDF4
import numpy as np

from geo_ring_cloud.paths import RUNS_ROOT
from geo_ring_cloud.run_discovery import resolve_run_dir


DEFAULT_08H_DIR = RUNS_ROOT / "epic_202403_meteosat_time_offset_control"
DEFAULT_OUT_DIR = DEFAULT_08H_DIR / "prefusion_source_pair_overlap_diagnostics"

SOURCE_PRODUCT = {
    "GOES-16": "ACMF",
    "GOES-18": "ACMF",
    "FY4B": "CLM",
    "Himawari-9": "CMSK",
    "Meteosat-0deg": "CLM",
    "Meteosat-IODC": "CLM",
    "CLAAS3-0deg": "CMA",
}

PAIR_LIST = [
    ("Meteosat-0deg", "GOES-16", "Meteosat-0deg western overlap with GOES-16"),
    ("Meteosat-0deg", "Meteosat-IODC", "MSG 0deg/IODC internal overlap"),
    ("CLAAS3-0deg", "Meteosat-0deg", "CM SAF CLAAS-3 versus operational Meteosat 0deg"),
    ("Meteosat-IODC", "FY4B", "Meteosat-IODC eastern overlap with FY4B"),
    ("FY4B", "Meteosat-IODC", "FY4B western overlap with Meteosat-IODC"),
    ("FY4B", "Himawari-9", "FY4B/Himawari East Asia overlap"),
    ("Himawari-9", "FY4B", "Himawari/FY4B East Asia overlap"),
    ("Meteosat-IODC", "Himawari-9", "Meteosat-IODC/Himawari far-eastern diagnostic overlap"),
]

POLICIES = {
    "A_inclusive_binary": {
        "epic": {1: 0, 2: 0, 3: 1, 4: 1},
        "geo": {0: 0, 1: 0, 2: 1, 3: 1},
        "kind": "binary",
    },
    "B_high_confidence_only": {
        "epic": {1: 0, 4: 1},
        "geo": {0: 0, 3: 1},
        "kind": "binary",
    },
    "C_uncertainty_aware_3class": {
        "epic": {1: 0, 2: 1, 3: 1, 4: 2},
        "geo": {0: 0, 1: 1, 2: 1, 3: 2},
        "kind": "multiclass",
    },
}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def find_var(ds: netCDF4.Dataset, names: list[str]) -> str:
    for name in names:
        group, _, var = name.rpartition("/")
        obj = ds[group] if group else ds
        if var in obj.variables:
            return name
    raise KeyError(names)


def read_epic(path: Path) -> dict[str, np.ndarray]:
    with netCDF4.Dataset(path) as ds:
        lat_name = find_var(ds, ["geolocation_data/latitude", "geolocation_data/Latitude"])
        lon_name = find_var(ds, ["geolocation_data/longitude", "geolocation_data/Longitude"])
        cm_name = find_var(ds, ["geophysical_data/Cloud_Mask", "geophysical_data/cloud_mask"])
        return {
            "lat": np.asarray(ds[lat_name][:], dtype=np.float32),
            "lon": np.asarray(ds[lon_name][:], dtype=np.float32),
            "cloud_mask": np.asarray(ds[cm_name][:]),
        }


def read_npz(path: Path, valid_keys: tuple[str, ...] = ("fusion_valid_mask", "valid_mask")) -> tuple[np.ndarray, np.ndarray]:
    with np.load(path, allow_pickle=True) as z:
        data = np.asarray(z["data"])
        valid = None
        for key in valid_keys:
            if key in z.files:
                valid = np.asarray(z[key]).astype(bool)
                break
        if valid is None:
            valid = np.isfinite(data)
    return data, valid


def load_grid(run_root: Path) -> dict[str, Any]:
    return json.loads((run_root / "reprojected_grid" / "target_grid_definition.json").read_text(encoding="utf-8"))


def sample_grid_to_points(arr: np.ndarray, valid: np.ndarray, lat: np.ndarray, lon: np.ndarray, grid: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    lat0 = float(grid["lat_centers_first_last"][0])
    lon0 = float(grid["lon_centers_first_last"][0])
    res = float(grid["resolution_degree"])
    ny, nx = arr.shape
    lon_norm = ((lon + 180.0) % 360.0) - 180.0
    iy = np.rint((lat - lat0) / res).astype(np.int64)
    ix = np.rint((lon_norm - lon0) / res).astype(np.int64)
    inside = np.isfinite(lat) & np.isfinite(lon_norm) & (iy >= 0) & (iy < ny) & (ix >= 0) & (ix < nx)
    out = np.full(lat.shape, -9999, dtype=arr.dtype)
    out_valid = np.zeros(lat.shape, dtype=bool)
    out[inside] = arr[iy[inside], ix[inside]]
    out_valid[inside] = valid[iy[inside], ix[inside]]
    return out, out_valid


def apply_policy(values: np.ndarray, mapping: dict[int, int]) -> tuple[np.ndarray, np.ndarray]:
    out = np.full(values.shape, -1, dtype=np.int16)
    valid = np.zeros(values.shape, dtype=bool)
    for raw, mapped in mapping.items():
        mask = values == raw
        out[mask] = mapped
        valid[mask] = True
    return out, valid


def binary_metrics(epic: np.ndarray, geo: np.ndarray, valid: np.ndarray) -> dict[str, Any]:
    n = int(np.count_nonzero(valid))
    if n == 0:
        return {"status": "NO_VALID_PIXELS", "n": 0}
    e = epic[valid].astype(np.int8)
    g = geo[valid].astype(np.int8)
    tp = int(np.count_nonzero((e == 1) & (g == 1)))
    tn = int(np.count_nonzero((e == 0) & (g == 0)))
    fp = int(np.count_nonzero((e == 0) & (g == 1)))
    fn = int(np.count_nonzero((e == 1) & (g == 0)))
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    f1 = 2.0 * precision * recall / max(precision + recall, 1e-12)
    return {
        "status": "OK",
        "n": n,
        "agreement": (tp + tn) / n,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "iou": tp / max(tp + fp + fn, 1),
        "tp": tp,
        "tn": tn,
        "fp": fp,
        "fn": fn,
        "cloud_fraction": float(np.mean(g == 1)),
    }


def multiclass_metrics(epic: np.ndarray, geo: np.ndarray, valid: np.ndarray) -> dict[str, Any]:
    n = int(np.count_nonzero(valid))
    if n == 0:
        return {"status": "NO_VALID_PIXELS", "n": 0}
    e = epic[valid].astype(np.int16)
    g = geo[valid].astype(np.int16)
    return {
        "status": "OK",
        "n": n,
        "agreement": float(np.mean(e == g)),
        "uncertain_fraction": float(np.mean(g == 1)),
    }


def find_source_file(run_root: Path, source: str, tag: str) -> Path | None:
    product = SOURCE_PRODUCT[source]
    root = run_root / "reprojected_grid" / source
    hits = list(root.glob(f"{source}_{product}_cloud_mask_grid_{tag}.npz"))
    if hits:
        return hits[0]
    hits = list(root.glob(f"*cloud_mask*{tag}.npz"))
    return hits[0] if hits else None


def analyze_sample(row: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    tag = row["time_tag"]
    run_root = resolve_run_dir(RUNS_ROOT, tag, os.environ.get("GEO_RING_SOURCE_PROFILE", "operational_baseline")) or (RUNS_ROOT / tag)
    grid = load_grid(run_root)
    epic = read_epic(Path(row["epic_file"]))
    epic_lat, epic_lon, epic_cm = epic["lat"], epic["lon"], epic["cloud_mask"]
    epic_geo_valid = np.isfinite(epic_lat) & np.isfinite(epic_lon) & np.isin(epic_cm, [1, 2, 3, 4])

    samples: dict[str, dict[str, np.ndarray]] = {}
    for source in SOURCE_PRODUCT:
        p = find_source_file(run_root, source, tag)
        if p is None:
            continue
        arr, valid = read_npz(p)
        data_on_epic, valid_on_epic = sample_grid_to_points(arr, valid, epic_lat, epic_lon, grid)
        samples[source] = {"data": data_on_epic, "valid": valid_on_epic, "path": np.array(str(p))}

    pair_rows: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    win_rows: list[dict[str, Any]] = []

    for policy_name, policy in POLICIES.items():
        epic_class, epic_policy_valid = apply_policy(epic_cm, policy["epic"])
        source_classes: dict[str, np.ndarray] = {}
        source_valids: dict[str, np.ndarray] = {}
        for source, sample in samples.items():
            cls, pv = apply_policy(sample["data"], policy["geo"])
            source_classes[source] = cls
            source_valids[source] = sample["valid"] & pv

        for source in samples:
            valid = epic_geo_valid & epic_policy_valid & source_valids[source]
            m = binary_metrics(epic_class, source_classes[source], valid) if policy["kind"] == "binary" else multiclass_metrics(epic_class, source_classes[source], valid)
            m.update(
                {
                    "time_tag": tag,
                    "target_time": row.get("nearest_hour", ""),
                    "epic_time": row.get("epic_time", ""),
                    "epic_delta_min": row.get("nearest_hour_delta_min", ""),
                    "policy": policy_name,
                    "source": source,
                    "source_file": str(samples[source]["path"].item()),
                }
            )
            source_rows.append(m)

        for left, right, meaning in PAIR_LIST:
            if left not in samples or right not in samples:
                continue
            both_valid = epic_geo_valid & epic_policy_valid & source_valids[left] & source_valids[right]
            left_m = binary_metrics(epic_class, source_classes[left], both_valid) if policy["kind"] == "binary" else multiclass_metrics(epic_class, source_classes[left], both_valid)
            right_m = binary_metrics(epic_class, source_classes[right], both_valid) if policy["kind"] == "binary" else multiclass_metrics(epic_class, source_classes[right], both_valid)
            n = int(np.count_nonzero(both_valid))
            if n == 0:
                base = {
                    "time_tag": tag,
                    "target_time": row.get("nearest_hour", ""),
                    "epic_time": row.get("epic_time", ""),
                    "epic_delta_min": row.get("nearest_hour_delta_min", ""),
                    "policy": policy_name,
                    "source_a": left,
                    "source_b": right,
                    "pair_meaning": meaning,
                    "status": "NO_VALID_PAIR_PIXELS",
                    "n": 0,
                }
                pair_rows.append(base)
                continue
            left_match = both_valid & (source_classes[left] == epic_class)
            right_match = both_valid & (source_classes[right] == epic_class)
            both_correct = left_match & right_match
            both_wrong = both_valid & (~left_match) & (~right_match)
            a_only = left_match & (~right_match)
            b_only = right_match & (~left_match)
            source_disagree = both_valid & (source_classes[left] != source_classes[right])
            pair_rows.append(
                {
                    "time_tag": tag,
                    "target_time": row.get("nearest_hour", ""),
                    "epic_time": row.get("epic_time", ""),
                    "epic_delta_min": row.get("nearest_hour_delta_min", ""),
                    "policy": policy_name,
                    "source_a": left,
                    "source_b": right,
                    "pair_meaning": meaning,
                    "status": "OK",
                    "n": n,
                    "source_a_agreement": left_m.get("agreement", ""),
                    "source_b_agreement": right_m.get("agreement", ""),
                    "source_a_f1": left_m.get("f1", ""),
                    "source_b_f1": right_m.get("f1", ""),
                    "agreement_b_minus_a": safe_float(right_m.get("agreement")) - safe_float(left_m.get("agreement")),
                    "f1_b_minus_a": safe_float(right_m.get("f1")) - safe_float(left_m.get("f1")),
                    "both_correct_fraction": int(np.count_nonzero(both_correct)) / n,
                    "both_wrong_fraction": int(np.count_nonzero(both_wrong)) / n,
                    "source_a_only_correct_fraction": int(np.count_nonzero(a_only)) / n,
                    "source_b_only_correct_fraction": int(np.count_nonzero(b_only)) / n,
                    "source_disagreement_fraction": int(np.count_nonzero(source_disagree)) / n,
                    "epic_favors_a_over_b_fraction": int(np.count_nonzero(a_only)) / n,
                    "epic_favors_b_over_a_fraction": int(np.count_nonzero(b_only)) / n,
                }
            )
            if policy["kind"] == "binary":
                win_rows.append(
                    {
                        "time_tag": tag,
                        "policy": policy_name,
                        "source_a": left,
                        "source_b": right,
                        "n": n,
                        "winner_by_agreement": left if safe_float(left_m.get("agreement")) >= safe_float(right_m.get("agreement")) else right,
                        "agreement_margin_abs": abs(safe_float(right_m.get("agreement")) - safe_float(left_m.get("agreement"))),
                        "winner_by_f1": left if safe_float(left_m.get("f1")) >= safe_float(right_m.get("f1")) else right,
                        "f1_margin_abs": abs(safe_float(right_m.get("f1")) - safe_float(left_m.get("f1"))),
                    }
                )

    return {"single_source": source_rows, "pair_overlap": pair_rows, "pair_winner": win_rows}


def mean(vals: list[float]) -> float:
    return sum(vals) / max(len(vals), 1)


def build_report(out_dir: Path, candidates: list[dict[str, str]], pair_rows: list[dict[str, Any]], source_rows: list[dict[str, Any]]) -> Path:
    lines = [
        "# 08j 未融合源产品成对重叠区 EPIC 诊断报告",
        "",
        "## 1. 目的",
        "",
        "本报告不使用 fused cloud mask 做归因，而是直接回到各卫星未融合的重投影 cloud_mask，在同一 EPIC 像元位置上比较每个源以及成对重叠源与 EPIC 的一致性。这样可以回答：Meteosat-0deg、Meteosat-IODC、FY4B、Himawari、GOES 在真实重叠区域里到底谁更接近 EPIC。",
        "",
        "## 2. 样本",
        "",
        "| time_tag | EPIC time | GEO hour | delta min | dominant | Meteosat fraction |",
        "|---|---|---|---:|---|---:|",
    ]
    for r in candidates:
        lines.append(f"| `{r['time_tag']}` | `{r['epic_time']}` | `{r['nearest_hour']}` | {safe_float(r['nearest_hour_delta_min']):.2f} | {r['dominant_satellite_estimate']} | {safe_float(r['meteosat_fraction_estimate']):.3f} |")

    lines.extend(["", "## 3. 单源直接对 EPIC 的平均表现", ""])
    for policy in ["A_inclusive_binary", "B_high_confidence_only"]:
        lines.extend([f"### {policy}", "", "| source | n samples | mean valid pixels | mean agreement | mean F1 |", "|---|---:|---:|---:|---:|"])
        for source in sorted({r["source"] for r in source_rows if r["policy"] == policy}):
            vals = [r for r in source_rows if r["policy"] == policy and r["source"] == source and r.get("status") == "OK"]
            if not vals:
                continue
            lines.append(f"| {source} | {len(vals)} | {mean([safe_float(v.get('n')) for v in vals]):.0f} | {mean([safe_float(v.get('agreement')) for v in vals]):.3f} | {mean([safe_float(v.get('f1')) for v in vals]):.3f} |")

    lines.extend(["", "## 4. 成对重叠区：谁更接近 EPIC", ""])
    for policy in ["A_inclusive_binary", "B_high_confidence_only"]:
        lines.extend([f"### {policy}", "", "| source A | source B | pair meaning | mean overlap pixels | A agreement | B agreement | B-A | A only correct | B only correct | both wrong |", "|---|---|---|---:|---:|---:|---:|---:|---:|---:|"])
        seen_pairs = []
        for left, right, meaning in PAIR_LIST:
            vals = [r for r in pair_rows if r["policy"] == policy and r["source_a"] == left and r["source_b"] == right and r.get("status") == "OK"]
            if not vals:
                continue
            seen_pairs.append((left, right))
            lines.append(
                f"| {left} | {right} | {meaning} | {mean([safe_float(v.get('n')) for v in vals]):.0f} | "
                f"{mean([safe_float(v.get('source_a_agreement')) for v in vals]):.3f} | "
                f"{mean([safe_float(v.get('source_b_agreement')) for v in vals]):.3f} | "
                f"{mean([safe_float(v.get('agreement_b_minus_a')) for v in vals]):+.3f} | "
                f"{mean([safe_float(v.get('source_a_only_correct_fraction')) for v in vals]):.3f} | "
                f"{mean([safe_float(v.get('source_b_only_correct_fraction')) for v in vals]):.3f} | "
                f"{mean([safe_float(v.get('both_wrong_fraction')) for v in vals]):.3f} |"
            )

    lines.extend(
        [
            "",
            "## 5. 解读原则",
            "",
            "- `B-A` 为正，表示 source B 在同一重叠像元上比 source A 更接近 EPIC；为负则相反。",
            "- `A only correct` / `B only correct` 表示两个源发生分歧且 EPIC 更支持其中一个源的比例。",
            "- `both wrong` 高，说明该区域不是简单换源能解决，可能存在 EPIC/GEO 语义差异、几何代表性误差、或两类 GEO 源共同偏差。",
            "- 本报告仍使用 08c 的 cloud-mask 语义策略。EPIC 不是绝对真值，结论用于诊断融合策略，而不是直接用 EPIC 训练或替代融合。",
            "",
            "## 6. 输出文件",
            "",
            f"- `{out_dir / '08j_prefusion_single_source_vs_epic.csv'}`",
            f"- `{out_dir / '08j_prefusion_pair_overlap_vs_epic.csv'}`",
            f"- `{out_dir / '08j_prefusion_pair_winner_summary.csv'}`",
        ]
    )
    report = out_dir / "08j_prefusion_source_pair_overlap_diagnostics_report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="08j pairwise overlap diagnostics on prefusion source cloud masks sampled to EPIC pixels.")
    p.add_argument("--candidates-csv", default=str(DEFAULT_08H_DIR / "08h_meteosat_time_offset_candidates.csv"))
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    return p


def main() -> int:
    args = build_parser().parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    candidates = read_csv(Path(args.candidates_csv))
    all_source: list[dict[str, Any]] = []
    all_pair: list[dict[str, Any]] = []
    all_win: list[dict[str, Any]] = []
    for row in candidates:
        result = analyze_sample(row)
        all_source.extend(result["single_source"])
        all_pair.extend(result["pair_overlap"])
        all_win.extend(result["pair_winner"])
    write_csv(
        out_dir / "08j_prefusion_single_source_vs_epic.csv",
        all_source,
        ["time_tag", "target_time", "epic_time", "epic_delta_min", "policy", "source", "status", "n", "agreement", "precision", "recall", "f1", "iou", "tp", "tn", "fp", "fn", "cloud_fraction", "source_file"],
    )
    write_csv(
        out_dir / "08j_prefusion_pair_overlap_vs_epic.csv",
        all_pair,
        [
            "time_tag",
            "target_time",
            "epic_time",
            "epic_delta_min",
            "policy",
            "source_a",
            "source_b",
            "pair_meaning",
            "status",
            "n",
            "source_a_agreement",
            "source_b_agreement",
            "source_a_f1",
            "source_b_f1",
            "agreement_b_minus_a",
            "f1_b_minus_a",
            "both_correct_fraction",
            "both_wrong_fraction",
            "source_a_only_correct_fraction",
            "source_b_only_correct_fraction",
            "source_disagreement_fraction",
            "epic_favors_a_over_b_fraction",
            "epic_favors_b_over_a_fraction",
        ],
    )
    write_csv(
        out_dir / "08j_prefusion_pair_winner_summary.csv",
        all_win,
        ["time_tag", "policy", "source_a", "source_b", "n", "winner_by_agreement", "agreement_margin_abs", "winner_by_f1", "f1_margin_abs"],
    )
    report = build_report(out_dir, candidates, all_pair, all_source)
    print(f"08j report={report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
