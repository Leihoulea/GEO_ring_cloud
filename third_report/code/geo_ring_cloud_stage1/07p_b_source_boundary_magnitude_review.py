from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from geo_ring_cloud.lineage import utc_now
from geo_ring_cloud.pipeline_layout import (
    REPORT_DIR,
    SCRIPT_DIR,
    STAGE_ROOT,
    ensure_pipeline_directories as ensure_dirs,
)


FUSED_DIR = STAGE_ROOT / "fused_best_source"
REPROJECT_DIR = STAGE_ROOT / "reprojected_grid"
ANGLE06E_DIR = STAGE_ROOT / "geometry_angle_sync_06e"
INPUT_07P_DIR = STAGE_ROOT / "overlap_validation_07p"
OUT_DIR = STAGE_ROOT / "overlap_validation_07p"

TIME_TAG = "20240305_0000"

TRANSITION_IN_CSV = INPUT_07P_DIR / "source_boundary_transition_matrix.csv"
REVIEW_CSV = OUT_DIR / "source_boundary_magnitude_review.csv"
REPORT_MD = REPORT_DIR / "07p_boundary_magnitude_review.md"

UNITS_MAP = {
    "cloud_mask": "binary_code",
    "cloud_top_height_km": "km",
    "cloud_top_temperature_K": "K",
    "cloud_top_pressure_hPa": "hPa",
    "cloud_optical_thickness": "1",
    "cloud_effective_radius_um": "um",
    "cloud_phase": "category_code",
    "cloud_type": "category_code",
}

RECOMMEND_THRESHOLDS = {
    "cloud_top_height_km": {"p95": 8.0, "margin_max": 0.08, "edge_min": 10000},
    "cloud_top_temperature_K": {"p95": 18.0, "margin_max": 0.08, "edge_min": 10000},
    "cloud_top_pressure_hPa": {"p95": 180.0, "margin_max": 0.08, "edge_min": 10000},
    "cloud_optical_thickness": {"p95": 10.0, "margin_max": 0.08, "edge_min": 5000},
    "cloud_effective_radius_um": {"p95": 20.0, "margin_max": 0.08, "edge_min": 5000},
}


def load_module(script_name: str, module_name: str):
    path = SCRIPT_DIR / script_name
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module {script_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


F06 = load_module("06_fuse_best_source.py", "stage1_f06_for_07pb")


def load_npz_payload(path: Path) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    with np.load(path, allow_pickle=False) as z:
        meta = json.loads(str(z["metadata_json"]))
        arrays = {name: np.asarray(z[name]) for name in z.files if not name.endswith("_json")}
    return meta, arrays


def load_fused_array(name: str) -> tuple[dict[str, Any], np.ndarray, np.ndarray]:
    meta, arrays = load_npz_payload(FUSED_DIR / f"{name}.npz")
    return meta, np.asarray(arrays["data"]), np.asarray(arrays["valid_mask"], dtype=bool)


def build_candidate_cache() -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    catalog = F06.load_catalog()
    target_grid = F06.target_grid_from_any(catalog)
    target_lon, target_lat = F06.build_target_lon_lat(target_grid)
    lon2d, lat2d = np.meshgrid(target_lon, target_lat)
    subpoints = F06.build_subpoint_longitude_map(catalog)
    cache: dict[str, list[dict[str, Any]]] = {}
    for variable, rules in F06.VARIABLE_RULES.items():
        items: list[dict[str, Any]] = []
        for rule in rules:
            cand = F06.build_candidate(variable, rule, catalog, lon2d, lat2d, subpoints)
            if cand is not None:
                cand = dict(cand)
                cand["source_id"] = F06.SOURCE_ID_MAP[cand["satellite"]]
                cand["rating"] = (
                    np.asarray(cand["view_weight"], dtype=np.float32)
                    * np.float32(cand["time_weight"])
                    * np.float32(cand["product_level_weight"])
                )
                items.append(cand)
        cache[variable] = items
    return cache, catalog


def compute_margin_and_selected_vza(variable: str, candidates: list[dict[str, Any]], shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    if not candidates:
        return np.full(shape, np.nan, dtype=np.float32), np.full(shape, np.nan, dtype=np.float32)
    best = np.full(shape, -np.inf, dtype=np.float32)
    second = np.full(shape, -np.inf, dtype=np.float32)
    selected_vza = np.full(shape, np.nan, dtype=np.float32)
    best_src = np.zeros(shape, dtype=np.int16)
    for cand in candidates:
        valid = np.asarray(cand["valid"], dtype=bool)
        rating = np.asarray(cand["rating"], dtype=np.float32)
        vza = np.asarray(cand["vza_for_rating"], dtype=np.float32)
        source_id = np.int16(cand["source_id"])
        replace_best = valid & (rating > best)
        replace_second = valid & ~replace_best & (rating > second)
        if np.any(replace_second):
            second[replace_second] = rating[replace_second]
        if np.any(replace_best):
            second[replace_best] = best[replace_best]
            best[replace_best] = rating[replace_best]
            selected_vza[replace_best] = vza[replace_best]
            best_src[replace_best] = source_id
        tie = valid & np.isclose(rating, best, atol=1e-12, rtol=0.0) & (best_src == source_id)
        if np.any(tie):
            selected_vza[tie] = vza[tie]
    margin = best - second
    invalid = (~np.isfinite(best)) | (~np.isfinite(second)) | (best <= -np.inf / 2) | (second <= -np.inf / 2)
    margin = margin.astype(np.float32)
    margin[invalid] = np.nan
    return margin, selected_vza


def edge_pair_arrays(source_map: np.ndarray, valid: np.ndarray, values: np.ndarray, source_a_id: int, source_b_id: int) -> tuple[np.ndarray, np.ndarray]:
    edge_diffs: list[np.ndarray] = []
    edge_masks: list[np.ndarray] = []
    for axis in [0, 1]:
        s1 = [slice(None), slice(None)]
        s2 = [slice(None), slice(None)]
        s1[axis] = slice(1, None)
        s2[axis] = slice(None, -1)
        src1 = source_map[tuple(s1)]
        src2 = source_map[tuple(s2)]
        ok = valid[tuple(s1)] & valid[tuple(s2)] & np.isfinite(values[tuple(s1)]) & np.isfinite(values[tuple(s2)])
        pair = ok & (
            ((src1 == source_a_id) & (src2 == source_b_id))
            | ((src1 == source_b_id) & (src2 == source_a_id))
        )
        if not np.any(pair):
            continue
        edge_diffs.append(np.abs(values[tuple(s1)][pair] - values[tuple(s2)][pair]).astype(np.float32))
        mask_full = np.zeros(source_map.shape, dtype=bool)
        idx = np.argwhere(pair)
        if axis == 0:
            mask_full[idx[:, 0] + 1, idx[:, 1]] = True
            mask_full[idx[:, 0], idx[:, 1]] = True
        else:
            mask_full[idx[:, 0], idx[:, 1] + 1] = True
            mask_full[idx[:, 0], idx[:, 1]] = True
        edge_masks.append(mask_full)
    diff = np.concatenate(edge_diffs) if edge_diffs else np.asarray([], dtype=np.float32)
    mask = np.logical_or.reduce(edge_masks) if edge_masks else np.zeros(source_map.shape, dtype=bool)
    return diff, mask


def top_row(df: pd.DataFrame, variable: str) -> pd.Series | None:
    subset = df[df["variable"] == variable].copy()
    if subset.empty:
        return None
    subset = subset.sort_values(["edge_jump_p95_abs", "edge_count"], ascending=[False, False])
    return subset.iloc[0]


def main() -> int:
    ensure_dirs()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(__file__, SCRIPT_DIR / Path(__file__).name)

    if not TRANSITION_IN_CSV.exists():
        raise RuntimeError(f"missing input transition matrix: {TRANSITION_IN_CSV}")

    transitions = pd.read_csv(TRANSITION_IN_CSV, encoding="utf-8-sig")
    candidates_by_var, _catalog = build_candidate_cache()

    source_maps: dict[str, np.ndarray] = {}
    source_valids: dict[str, np.ndarray] = {}
    fused_values: dict[str, np.ndarray] = {}
    margins: dict[str, np.ndarray] = {}
    selected_vzas: dict[str, np.ndarray] = {}

    for variable in sorted(transitions["variable"].unique()):
        source_meta, source_map, source_valid = load_fused_array(f"source_map_{variable}")
        _ = source_meta
        source_maps[variable] = source_map.astype(np.int16)
        source_valids[variable] = source_valid.astype(bool)
        if variable == "cloud_mask":
            _, fused_data, fused_valid = load_fused_array("fused_cloud_binary")
            fused_values[variable] = np.where(fused_valid, fused_data.astype(np.float32), np.nan)
        else:
            _, fused_data, fused_valid = load_fused_array(f"fused_{variable}")
            fused_values[variable] = np.where(fused_valid, fused_data.astype(np.float32), np.nan)
        margins[variable], selected_vzas[variable] = compute_margin_and_selected_vza(
            variable,
            candidates_by_var.get(variable, []),
            source_map.shape,
        )

    rows: list[dict[str, Any]] = []
    for rec in transitions.itertuples():
        variable = str(rec.variable)
        source_a = str(rec.source_a)
        source_b = str(rec.source_b)
        source_a_id = int(rec.source_a_id)
        source_b_id = int(rec.source_b_id)
        diff, edge_mask = edge_pair_arrays(
            source_maps[variable],
            source_valids[variable],
            fused_values[variable],
            source_a_id,
            source_b_id,
        )
        if diff.size == 0:
            row = {
                "variable": variable,
                "source_a": source_a,
                "source_b": source_b,
                "edge_count": int(rec.edge_count),
                "edge_jump_mean_abs": np.nan,
                "edge_jump_median_abs": np.nan,
                "edge_jump_p95_abs": np.nan,
                "edge_jump_max_abs": np.nan,
                "rating_margin_mean": np.nan,
                "selected_vza_mean": np.nan,
                "variable_units": UNITS_MAP.get(variable, ""),
            }
        else:
            margin_vals = margins[variable][edge_mask]
            vza_vals = selected_vzas[variable][edge_mask]
            row = {
                "variable": variable,
                "source_a": source_a,
                "source_b": source_b,
                "edge_count": int(diff.size),
                "edge_jump_mean_abs": float(np.mean(diff)),
                "edge_jump_median_abs": float(np.median(diff)),
                "edge_jump_p95_abs": float(np.percentile(diff, 95)),
                "edge_jump_max_abs": float(np.max(diff)),
                "rating_margin_mean": float(np.nanmean(margin_vals)) if np.isfinite(margin_vals).any() else np.nan,
                "selected_vza_mean": float(np.nanmean(vza_vals)) if np.isfinite(vza_vals).any() else np.nan,
                "variable_units": UNITS_MAP.get(variable, ""),
            }
        rows.append(row)

    review_df = pd.DataFrame(rows).sort_values(["variable", "edge_jump_p95_abs", "edge_count"], ascending=[True, False, False])
    review_df.to_csv(REVIEW_CSV, index=False, encoding="utf-8-sig")

    recommendations: list[str] = []
    for variable, thresh in RECOMMEND_THRESHOLDS.items():
        subset = review_df[review_df["variable"] == variable]
        if subset.empty:
            continue
        top = subset.iloc[0]
        if (
            pd.notna(top["edge_jump_p95_abs"])
            and float(top["edge_jump_p95_abs"]) >= float(thresh["p95"])
            and pd.notna(top["rating_margin_mean"])
            and float(top["rating_margin_mean"]) <= float(thresh["margin_max"])
            and int(top["edge_count"]) >= int(thresh["edge_min"])
        ):
            recommendations.append(
                f"{variable}: {top['source_a']} vs {top['source_b']} "
                f"(p95={float(top['edge_jump_p95_abs']):.3f}, margin={float(top['rating_margin_mean']):.3f}, edges={int(top['edge_count'])})"
            )

    cth_top = top_row(review_df, "cloud_top_height_km")
    ctt_top = top_row(review_df, "cloud_top_temperature_K")
    ctp_top = top_row(review_df, "cloud_top_pressure_hPa")
    cot_top = top_row(review_df, "cloud_optical_thickness")
    cer_top = top_row(review_df, "cloud_effective_radius_um")

    allow_07v2 = "YES" if not recommendations else "NO"
    allow_6times = "YES" if not recommendations else "NO"

    lines = [
        "# 07p-b Source Boundary Magnitude Review",
        "",
        f"- 生成时间 UTC: {utc_now()}",
        f"- 输入矩阵: `{TRANSITION_IN_CSV}`",
        f"- 输出表: `{REVIEW_CSV}`",
        "",
        "## 1. CTH 最大跳变来自哪个 source pair",
        "",
        (
            f"- {cth_top['source_a']} vs {cth_top['source_b']}, "
            f"p95={float(cth_top['edge_jump_p95_abs']):.3f} {cth_top['variable_units']}, "
            f"mean={float(cth_top['edge_jump_mean_abs']):.3f}, edges={int(cth_top['edge_count'])}"
            if cth_top is not None
            else "- 无。"
        ),
        "",
        "## 2. CTT/CTP 最大跳变来自哪个 source pair",
        "",
        (
            f"- CTT: {ctt_top['source_a']} vs {ctt_top['source_b']}, "
            f"p95={float(ctt_top['edge_jump_p95_abs']):.3f} {ctt_top['variable_units']}, "
            f"mean={float(ctt_top['edge_jump_mean_abs']):.3f}, edges={int(ctt_top['edge_count'])}"
            if ctt_top is not None
            else "- CTT: 无。"
        ),
        (
            f"- CTP: {ctp_top['source_a']} vs {ctp_top['source_b']}, "
            f"p95={float(ctp_top['edge_jump_p95_abs']):.3f} {ctp_top['variable_units']}, "
            f"mean={float(ctp_top['edge_jump_mean_abs']):.3f}, edges={int(ctp_top['edge_count'])}"
            if ctp_top is not None
            else "- CTP: 无。"
        ),
        "",
        "## 3. COT/CER 最大跳变来自哪个 source pair",
        "",
        (
            f"- COT: {cot_top['source_a']} vs {cot_top['source_b']}, "
            f"p95={float(cot_top['edge_jump_p95_abs']):.3f} {cot_top['variable_units']}, "
            f"mean={float(cot_top['edge_jump_mean_abs']):.3f}, edges={int(cot_top['edge_count'])}"
            if cot_top is not None
            else "- COT: 无。"
        ),
        (
            f"- CER: {cer_top['source_a']} vs {cer_top['source_b']}, "
            f"p95={float(cer_top['edge_jump_p95_abs']):.3f} {cer_top['variable_units']}, "
            f"mean={float(cer_top['edge_jump_mean_abs']):.3f}, edges={int(cer_top['edge_count'])}"
            if cer_top is not None
            else "- CER: 无。"
        ),
        "",
        "## 4. 是否存在某个 source pair 应在 06 rating 中降权",
        "",
    ]
    if recommendations:
        lines.append("- 是，建议重点评估以下 source pair 是否需要在 06 rating 中增加边界相关降权或后续平滑约束：")
        for item in recommendations:
            lines.append(f"- {item}")
    else:
        lines.append("- 当前没有足够证据要求直接修改 06 rating。高跳变 pair 存在，但在现有阈值下尚未同时满足“大跳变 + 低 margin + 大样本”的降权触发条件。")

    lines.extend(
        [
            "",
            "## 5. 是否允许生成正式 07v2",
            "",
            f"- {allow_07v2}",
            "",
            "## 6. 是否允许进入 6 个代表时次",
            "",
            f"- {allow_6times}",
            "",
            "## 说明",
            "",
            "- `rating_margin_mean` 定义为当前 06 逻辑下 best-vs-second-best rating margin 在该 source pair 边界像元并集上的均值。",
            "- `selected_vza_mean` 定义为该 source pair 边界像元并集上，被选中 source 的 VZA 均值。",
            "- `edge_jump_*` 统计基于实际相邻边界像元对的绝对差值，不是边界缓冲区统计。",
        ]
    )

    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"07p-b review rows={len(review_df)}")
    print(f"report={REPORT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
