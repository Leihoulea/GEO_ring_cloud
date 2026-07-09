from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any

import netCDF4
import numpy as np


RUNS_ROOT = Path(r"D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs")
DEFAULT_08H_DIR = RUNS_ROOT / "epic_202403_meteosat_time_offset_control"
DEFAULT_OUT_DIR = DEFAULT_08H_DIR / "source_overlap_diagnostics"

SOURCE_ID_TO_NAME = {
    1: "GOES-16",
    2: "GOES-18",
    3: "FY4B",
    4: "Himawari-9",
    5: "Meteosat-0deg",
    6: "Meteosat-IODC",
}
SOURCE_PRODUCT = {
    "GOES-16": "ACMF",
    "GOES-18": "ACMF",
    "FY4B": "CLM",
    "Himawari-9": "CMSK",
    "Meteosat-0deg": "CLM",
    "Meteosat-IODC": "CLM",
}

POLICIES = {
    "A_inclusive_binary": {
        "epic": {1: 0, 2: 0, 3: 1, 4: 1},
        "geo": {0: 0, 1: 0, 2: 1, 3: 1},
        "kind": "binary",
        "description": "EPIC 1/2=clear, 3/4=cloud; GEO 0/1=clear, 2/3=cloud",
    },
    "B_high_confidence_only": {
        "epic": {1: 0, 4: 1},
        "geo": {0: 0, 3: 1},
        "kind": "binary",
        "description": "EPIC 1=clear, 4=cloud; uncertain/probable classes excluded",
    },
    "C_uncertainty_aware_3class": {
        "epic": {1: 0, 2: 1, 3: 1, 4: 2},
        "geo": {0: 0, 1: 1, 2: 1, 3: 2},
        "kind": "multiclass",
        "description": "clear / uncertain / cloudy three-class diagnostic",
    },
}


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


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


def read_npz_array(path: Path, data_key: str = "data", valid_preference: tuple[str, ...] = ("valid_mask",)) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    with np.load(path, allow_pickle=True) as z:
        data = np.asarray(z[data_key])
        valid = None
        for key in valid_preference:
            if key in z.files:
                valid = np.asarray(z[key]).astype(bool)
                break
        if valid is None:
            valid = np.isfinite(data)
        meta = {}
        if "metadata_json" in z.files:
            try:
                meta = json.loads(str(np.asarray(z["metadata_json"]).item()))
            except Exception:
                meta = {}
    return data, valid, meta


def load_grid(run_root: Path) -> dict[str, Any]:
    p = run_root / "reprojected_grid" / "target_grid_definition.json"
    return json.loads(p.read_text(encoding="utf-8"))


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


def metrics_binary(epic: np.ndarray, geo: np.ndarray, valid: np.ndarray) -> dict[str, Any]:
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
    f1 = 2 * precision * recall / max(precision + recall, 1e-12)
    iou = tp / max(tp + fp + fn, 1)
    return {
        "status": "OK",
        "n": n,
        "agreement": (tp + tn) / n,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "iou": iou,
        "tp": tp,
        "tn": tn,
        "fp": fp,
        "fn": fn,
        "epic_cloud_fraction": float(np.mean(e == 1)),
        "geo_cloud_fraction": float(np.mean(g == 1)),
    }


def metrics_multiclass(epic: np.ndarray, geo: np.ndarray, valid: np.ndarray) -> dict[str, Any]:
    n = int(np.count_nonzero(valid))
    if n == 0:
        return {"status": "NO_VALID_PIXELS", "n": 0}
    e = epic[valid].astype(np.int16)
    g = geo[valid].astype(np.int16)
    return {
        "status": "OK",
        "n": n,
        "agreement": float(np.mean(e == g)),
        "epic_uncertain_fraction": float(np.mean(e == 1)),
        "geo_uncertain_fraction": float(np.mean(g == 1)),
        "either_uncertain_fraction": float(np.mean((e == 1) | (g == 1))),
    }


def compute_metrics(epic_class: np.ndarray, geo_class: np.ndarray, valid: np.ndarray, kind: str) -> dict[str, Any]:
    if kind == "binary":
        return metrics_binary(epic_class, geo_class, valid)
    return metrics_multiclass(epic_class, geo_class, valid)


def find_source_file(run_root: Path, source: str, tag: str) -> Path | None:
    product = SOURCE_PRODUCT[source]
    root = run_root / "reprojected_grid" / source
    hits = list(root.glob(f"{source}_{product}_cloud_mask_grid_{tag}.npz"))
    if hits:
        return hits[0]
    hits = list(root.glob(f"*cloud_mask*{tag}.npz"))
    return hits[0] if hits else None


def analyze_sample(row: dict[str, str], out_dir: Path) -> dict[str, list[dict[str, Any]]]:
    tag = row["time_tag"]
    run_root = RUNS_ROOT / tag
    epic_path = Path(row["epic_file"])
    grid = load_grid(run_root)
    epic = read_epic(epic_path)
    epic_lat, epic_lon, epic_cm = epic["lat"], epic["lon"], epic["cloud_mask"]
    base_epic_valid = np.isfinite(epic_lat) & np.isfinite(epic_lon) & np.isin(epic_cm, [1, 2, 3, 4])

    source_samples: dict[str, dict[str, np.ndarray]] = {}
    for source in SOURCE_PRODUCT:
        p = find_source_file(run_root, source, tag)
        if not p:
            continue
        arr, valid, meta = read_npz_array(p, valid_preference=("fusion_valid_mask", "valid_mask"))
        sampled, sampled_valid = sample_grid_to_points(arr, valid, epic_lat, epic_lon, grid)
        source_samples[source] = {"data": sampled, "valid": sampled_valid, "path": np.array(str(p))}

    fused_arr, fused_valid, _ = read_npz_array(run_root / "fused_best_source" / "fused_cloud_mask.npz", valid_preference=("valid_mask",))
    source_map, source_map_valid, _ = read_npz_array(run_root / "fused_best_source" / "source_map_cloud_mask.npz", valid_preference=("valid_mask",))
    valid_count, valid_count_valid, _ = read_npz_array(run_root / "fused_best_source" / "valid_count_map_cloud_mask.npz", valid_preference=("valid_mask",))
    fused_on_epic, fused_on_epic_valid = sample_grid_to_points(fused_arr, fused_valid, epic_lat, epic_lon, grid)
    srcid_on_epic, srcid_on_epic_valid = sample_grid_to_points(source_map, source_map_valid, epic_lat, epic_lon, grid)
    vcount_on_epic, vcount_on_epic_valid = sample_grid_to_points(valid_count, valid_count_valid, epic_lat, epic_lon, grid)

    source_metric_rows: list[dict[str, Any]] = []
    selected_region_rows: list[dict[str, Any]] = []
    overlap_rows: list[dict[str, Any]] = []
    counter_rows: list[dict[str, Any]] = []
    drag_rows: list[dict[str, Any]] = []

    for policy_name, policy in POLICIES.items():
        epic_class, epic_policy_valid = apply_policy(epic_cm, policy["epic"])
        fused_class, fused_policy_valid = apply_policy(fused_on_epic, policy["geo"])
        fused_base_valid = base_epic_valid & epic_policy_valid & fused_on_epic_valid & fused_policy_valid
        fm = compute_metrics(epic_class, fused_class, fused_base_valid, policy["kind"])
        fm.update(
            {
                "time_tag": tag,
                "target_time": row.get("nearest_hour", ""),
                "epic_time": row.get("epic_time", ""),
                "epic_delta_min": row.get("nearest_hour_delta_min", ""),
                "policy": policy_name,
                "scope": "fused_all_valid",
                "source_name": "FUSED_SELECTED",
            }
        )
        selected_region_rows.append(fm)

        source_classes: dict[str, np.ndarray] = {}
        source_policy_valids: dict[str, np.ndarray] = {}
        for source, sample in source_samples.items():
            geo_class, geo_policy_valid = apply_policy(sample["data"], policy["geo"])
            source_classes[source] = geo_class
            source_policy_valids[source] = sample["valid"] & geo_policy_valid
            valid = base_epic_valid & epic_policy_valid & source_policy_valids[source]
            sm = compute_metrics(epic_class, geo_class, valid, policy["kind"])
            sm.update(
                {
                    "time_tag": tag,
                    "target_time": row.get("nearest_hour", ""),
                    "epic_time": row.get("epic_time", ""),
                    "epic_delta_min": row.get("nearest_hour_delta_min", ""),
                    "policy": policy_name,
                    "source_name": source,
                    "source_family": "Meteosat" if source.startswith("Meteosat") else "non_Meteosat",
                    "sample_source_valid_fraction_of_epic_earth": float(np.mean(valid & base_epic_valid)),
                    "source_file": str(sample["path"].item()),
                }
            )
            source_metric_rows.append(sm)

            sid = [k for k, v in SOURCE_ID_TO_NAME.items() if v == source][0]
            selected_mask = fused_base_valid & srcid_on_epic_valid & (srcid_on_epic == sid)
            sr = compute_metrics(epic_class, fused_class, selected_mask, policy["kind"])
            sr.update(
                {
                    "time_tag": tag,
                    "policy": policy_name,
                    "scope": "fused_pixels_selected_from_source",
                    "source_name": source,
                    "source_id": sid,
                    "selected_pixel_fraction_of_fused_valid": int(np.count_nonzero(selected_mask)) / max(int(np.count_nonzero(fused_base_valid)), 1),
                }
            )
            selected_region_rows.append(sr)

        # overlap count metrics using real sampled source availability, plus fused valid_count map as auxiliary.
        availability = np.zeros(epic_cm.shape, dtype=np.int16)
        for source in source_samples:
            availability += (base_epic_valid & epic_policy_valid & source_policy_valids[source]).astype(np.int16)
        for label, mask in [
            ("all_fused_valid", fused_base_valid),
            ("sampled_source_count_1", fused_base_valid & (availability == 1)),
            ("sampled_source_count_ge2", fused_base_valid & (availability >= 2)),
            ("sampled_source_count_ge3", fused_base_valid & (availability >= 3)),
            ("fused_valid_count_1", fused_base_valid & vcount_on_epic_valid & (vcount_on_epic == 1)),
            ("fused_valid_count_ge2", fused_base_valid & vcount_on_epic_valid & (vcount_on_epic >= 2)),
            ("fused_valid_count_ge3", fused_base_valid & vcount_on_epic_valid & (vcount_on_epic >= 3)),
        ]:
            om = compute_metrics(epic_class, fused_class, mask, policy["kind"])
            om.update({"time_tag": tag, "policy": policy_name, "overlap_scope": label, "pixel_fraction_of_fused_valid": int(np.count_nonzero(mask)) / max(int(np.count_nonzero(fused_base_valid)), 1)})
            overlap_rows.append(om)

        # Counterfactual within overlap: current selected vs each fixed source vs oracle best.
        overlap_mask = fused_base_valid & (availability >= 2)
        if policy["kind"] == "binary":
            current_match = overlap_mask & (fused_class == epic_class)
            oracle_possible = np.zeros(epic_cm.shape, dtype=bool)
            available_any_non_meteosat = np.zeros(epic_cm.shape, dtype=bool)
            available_any_meteosat = np.zeros(epic_cm.shape, dtype=bool)
            source_match_count: dict[str, int] = {}
            source_valid_count: dict[str, int] = {}
            for source in source_samples:
                valid = overlap_mask & source_policy_valids[source]
                match = valid & (source_classes[source] == epic_class)
                oracle_possible |= match
                if source.startswith("Meteosat"):
                    available_any_meteosat |= valid
                else:
                    available_any_non_meteosat |= valid
                source_match_count[source] = int(np.count_nonzero(match))
                source_valid_count[source] = int(np.count_nonzero(valid))
                cm = compute_metrics(epic_class, source_classes[source], valid, policy["kind"])
                cm.update({"time_tag": tag, "policy": policy_name, "counterfactual_scope": "overlap_ge2_fixed_source", "source_name": source})
                counter_rows.append(cm)
            oracle_n = int(np.count_nonzero(overlap_mask))
            counter_rows.append(
                {
                    "time_tag": tag,
                    "policy": policy_name,
                    "counterfactual_scope": "overlap_ge2_oracle_best_available_source",
                    "source_name": "ORACLE_ANY_SOURCE",
                    "n": oracle_n,
                    "agreement": int(np.count_nonzero(overlap_mask & oracle_possible)) / max(oracle_n, 1),
                    "current_selected_agreement": int(np.count_nonzero(current_match)) / max(oracle_n, 1),
                    "oracle_gain_over_current": (int(np.count_nonzero(overlap_mask & oracle_possible)) - int(np.count_nonzero(current_match))) / max(oracle_n, 1),
                    "note": "Diagnostic upper bound only; not a production fusion method.",
                }
            )
            meteosat_selected = overlap_mask & np.isin(srcid_on_epic, [5, 6])
            non_meteosat_oracle = np.zeros(epic_cm.shape, dtype=bool)
            for source in source_samples:
                if source.startswith("Meteosat"):
                    continue
                non_meteosat_oracle |= meteosat_selected & source_policy_valids[source] & (source_classes[source] == epic_class)
            cur_m = meteosat_selected & (fused_class == epic_class)
            drag_rows.append(
                {
                    "time_tag": tag,
                    "policy": policy_name,
                    "scope": "currently_selected_meteosat_pixels_in_overlap_ge2",
                    "n": int(np.count_nonzero(meteosat_selected)),
                    "current_selected_agreement": int(np.count_nonzero(cur_m)) / max(int(np.count_nonzero(meteosat_selected)), 1),
                    "non_meteosat_oracle_possible_agreement": int(np.count_nonzero(non_meteosat_oracle)) / max(int(np.count_nonzero(meteosat_selected)), 1),
                    "potential_gain_if_perfectly_replaced_by_non_meteosat": (int(np.count_nonzero(non_meteosat_oracle)) - int(np.count_nonzero(cur_m))) / max(int(np.count_nonzero(meteosat_selected)), 1),
                    "non_meteosat_available_fraction": int(np.count_nonzero(meteosat_selected & available_any_non_meteosat)) / max(int(np.count_nonzero(meteosat_selected)), 1),
                    "meteosat_available_fraction": int(np.count_nonzero(meteosat_selected & available_any_meteosat)) / max(int(np.count_nonzero(meteosat_selected)), 1),
                    "note": "Oracle replacement checks whether another available non-Meteosat source could match EPIC on the same pixels.",
                }
            )

    return {
        "prefusion_source_metrics": source_metric_rows,
        "selected_region_metrics": selected_region_rows,
        "overlap_count_metrics": overlap_rows,
        "overlap_counterfactual_metrics": counter_rows,
        "meteosat_drag_diagnostics": drag_rows,
    }


def build_report(out_dir: Path, candidates: list[dict[str, str]], rows: dict[str, list[dict[str, Any]]]) -> Path:
    def mean(values: list[float]) -> float:
        return sum(values) / max(len(values), 1)

    lines = [
        "# 08i Meteosat 小时间差样本：单源与重叠区诊断报告",
        "",
        "## 1. 目的",
        "",
        "本报告回答一个更严格的问题：在 08h 的三个小时间差 Meteosat-dominant 样本中，低一致性是否真由 Meteosat-0deg / Meteosat-IODC 拖累，还是其他源卫星、重叠区选择或 cloud mask 语义共同造成。报告不重新融合、不下载数据，只在 EPIC 像元空间复用现有重投影和融合结果做诊断。",
        "",
        "## 2. 样本",
        "",
        "| time_tag | EPIC time | GEO hour | delta min | dominant | Meteosat fraction |",
        "|---|---|---|---:|---|---:|",
    ]
    for r in candidates:
        lines.append(f"| `{r['time_tag']}` | `{r['epic_time']}` | `{r['nearest_hour']}` | {safe_float(r['nearest_hour_delta_min']):.2f} | {r['dominant_satellite_estimate']} | {safe_float(r['meteosat_fraction_estimate']):.3f} |")

    pref = rows["prefusion_source_metrics"]
    for policy in ["A_inclusive_binary", "B_high_confidence_only"]:
        lines.extend(["", f"## 3. 融合前单源 vs EPIC：{policy}", "", "| source | n samples | mean agreement | mean F1 | mean IoU | mean valid pixels |", "|---|---:|---:|---:|---:|---:|"])
        sources = sorted({r["source_name"] for r in pref if r["policy"] == policy})
        for src in sources:
            vals = [r for r in pref if r["policy"] == policy and r["source_name"] == src and r.get("status") == "OK"]
            lines.append(
                f"| {src} | {len(vals)} | {mean([safe_float(v.get('agreement')) for v in vals]):.3f} | "
                f"{mean([safe_float(v.get('f1')) for v in vals]):.3f} | {mean([safe_float(v.get('iou')) for v in vals]):.3f} | "
                f"{mean([safe_float(v.get('n')) for v in vals]):.0f} |"
            )

    sel = rows["selected_region_metrics"]
    lines.extend(["", "## 4. 当前融合 source_map 分区表现", "", "这里检查最终融合结果中，哪些像元由哪个 source 选中，以及这些像元与 EPIC 的一致性。这个结果回答“当前 source_map 中谁贡献了 mismatch”。", ""])
    for policy in ["A_inclusive_binary", "B_high_confidence_only"]:
        lines.extend([f"### {policy}", "", "| selected source | mean selected pixel fraction | mean agreement | mean F1 |", "|---|---:|---:|---:|"])
        for src in sorted(SOURCE_PRODUCT):
            vals = [r for r in sel if r["policy"] == policy and r.get("source_name") == src and r.get("scope") == "fused_pixels_selected_from_source" and r.get("status") == "OK"]
            if not vals:
                continue
            lines.append(
                f"| {src} | {mean([safe_float(v.get('selected_pixel_fraction_of_fused_valid')) for v in vals]):.3f} | "
                f"{mean([safe_float(v.get('agreement')) for v in vals]):.3f} | {mean([safe_float(v.get('f1')) for v in vals]):.3f} |"
            )

    ov = rows["overlap_count_metrics"]
    lines.extend(["", "## 5. 重叠区是否拉低整体指标", "", "| policy | overlap scope | mean pixel fraction | mean agreement | mean F1 |", "|---|---|---:|---:|---:|"])
    for policy in ["A_inclusive_binary", "B_high_confidence_only"]:
        scopes = ["all_fused_valid", "sampled_source_count_1", "sampled_source_count_ge2", "sampled_source_count_ge3"]
        for scope in scopes:
            vals = [r for r in ov if r["policy"] == policy and r["overlap_scope"] == scope and r.get("status") == "OK"]
            lines.append(f"| {policy} | {scope} | {mean([safe_float(v.get('pixel_fraction_of_fused_valid')) for v in vals]):.3f} | {mean([safe_float(v.get('agreement')) for v in vals]):.3f} | {mean([safe_float(v.get('f1')) for v in vals]):.3f} |")

    drag = rows["meteosat_drag_diagnostics"]
    lines.extend(["", "## 6. 如果当前选中了 Meteosat，换成非 Meteosat 是否可能更好？", "", "这是 oracle 诊断，只表示同一像元上若存在其他非 Meteosat 源，且我们事后知道 EPIC 类别，理论上可达到的上限；它不能直接作为生产融合规则。", "", "| policy | n | current Meteosat agreement | non-Meteosat oracle possible | potential gain | non-Meteosat available fraction |", "|---|---:|---:|---:|---:|---:|"])
    for policy in ["A_inclusive_binary", "B_high_confidence_only"]:
        vals = [r for r in drag if r["policy"] == policy]
        lines.append(
            f"| {policy} | {mean([safe_float(v.get('n')) for v in vals]):.0f} | "
            f"{mean([safe_float(v.get('current_selected_agreement')) for v in vals]):.3f} | "
            f"{mean([safe_float(v.get('non_meteosat_oracle_possible_agreement')) for v in vals]):.3f} | "
            f"{mean([safe_float(v.get('potential_gain_if_perfectly_replaced_by_non_meteosat')) for v in vals]):.3f} | "
            f"{mean([safe_float(v.get('non_meteosat_available_fraction')) for v in vals]):.3f} |"
        )

    lines.extend(
        [
            "",
            "## 7. 结论",
            "",
            "1. 这几个新样本已经做了分卫星单源与 EPIC 的比较，并且区分了 Meteosat-0deg 与 Meteosat-IODC。",
            "2. 需要同时看两个层面：融合前单源指标说明源产品本身与 EPIC 的一致性；source_map 分区指标说明当前融合实际选中区域的贡献。",
            "3. 重叠区诊断显示，不能只看整体 agreement；必须区分单源区、多源重叠区和当前选中 Meteosat 的区域。",
            "4. oracle 替换结果只能作为上限诊断。如果 oracle 显示非 Meteosat 可显著提高，说明当前 Meteosat 选择有风险；但生产版仍需要用可预报的质量指标、几何权重或多源共识来实现，而不能使用 EPIC 标签本身。",
            "",
            "## 8. 输出文件",
            "",
            f"- `{out_dir / '08i_prefusion_single_source_metrics.csv'}`",
            f"- `{out_dir / '08i_fused_selected_region_metrics.csv'}`",
            f"- `{out_dir / '08i_overlap_count_metrics.csv'}`",
            f"- `{out_dir / '08i_overlap_counterfactual_metrics.csv'}`",
            f"- `{out_dir / '08i_meteosat_drag_diagnostics.csv'}`",
        ]
    )
    report = out_dir / "08i_meteosat_source_overlap_diagnostics_report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="08i Meteosat source/overlap diagnostics for 08h time-offset controlled samples.")
    p.add_argument("--candidates-csv", default=str(DEFAULT_08H_DIR / "08h_meteosat_time_offset_candidates.csv"))
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    return p


def main() -> int:
    args = build_parser().parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    candidates = read_csv(Path(args.candidates_csv))
    all_rows = {
        "prefusion_source_metrics": [],
        "selected_region_metrics": [],
        "overlap_count_metrics": [],
        "overlap_counterfactual_metrics": [],
        "meteosat_drag_diagnostics": [],
    }
    for row in candidates:
        result = analyze_sample(row, out_dir)
        for key in all_rows:
            all_rows[key].extend(result[key])

    write_csv(
        out_dir / "08i_prefusion_single_source_metrics.csv",
        all_rows["prefusion_source_metrics"],
        ["time_tag", "target_time", "epic_time", "epic_delta_min", "policy", "source_name", "source_family", "status", "n", "agreement", "precision", "recall", "f1", "iou", "tp", "tn", "fp", "fn", "epic_cloud_fraction", "geo_cloud_fraction", "sample_source_valid_fraction_of_epic_earth", "source_file"],
    )
    write_csv(
        out_dir / "08i_fused_selected_region_metrics.csv",
        all_rows["selected_region_metrics"],
        ["time_tag", "target_time", "epic_time", "epic_delta_min", "policy", "scope", "source_name", "source_id", "status", "n", "agreement", "precision", "recall", "f1", "iou", "tp", "tn", "fp", "fn", "epic_cloud_fraction", "geo_cloud_fraction", "selected_pixel_fraction_of_fused_valid"],
    )
    write_csv(
        out_dir / "08i_overlap_count_metrics.csv",
        all_rows["overlap_count_metrics"],
        ["time_tag", "policy", "overlap_scope", "status", "n", "agreement", "precision", "recall", "f1", "iou", "tp", "tn", "fp", "fn", "epic_cloud_fraction", "geo_cloud_fraction", "pixel_fraction_of_fused_valid"],
    )
    write_csv(
        out_dir / "08i_overlap_counterfactual_metrics.csv",
        all_rows["overlap_counterfactual_metrics"],
        ["time_tag", "policy", "counterfactual_scope", "source_name", "status", "n", "agreement", "precision", "recall", "f1", "iou", "tp", "tn", "fp", "fn", "epic_cloud_fraction", "geo_cloud_fraction", "current_selected_agreement", "oracle_gain_over_current", "note"],
    )
    write_csv(
        out_dir / "08i_meteosat_drag_diagnostics.csv",
        all_rows["meteosat_drag_diagnostics"],
        ["time_tag", "policy", "scope", "n", "current_selected_agreement", "non_meteosat_oracle_possible_agreement", "potential_gain_if_perfectly_replaced_by_non_meteosat", "non_meteosat_available_fraction", "meteosat_available_fraction", "note"],
    )
    report = build_report(out_dir, candidates, all_rows)
    print(f"08i report={report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
