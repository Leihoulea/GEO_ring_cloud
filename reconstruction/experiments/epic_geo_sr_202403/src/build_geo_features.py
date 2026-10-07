"""Sample existing GEO-ring products to native EPIC pixels without target leakage."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from scipy import ndimage

from common import DATA, REPORTS, configured_inputs, ensure_layout, load_npz, read_epic, resolve_scene_rows, sample_geo_grid, utc_now, write_csv, write_manifest


def geo_binary_probability(raw: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    # Stage 09D Policy A: GEO 0,1 clear; 2,3 cloud.  This is a GEO-only feature.
    valid = np.isin(raw, [0, 1, 2, 3])
    probability = np.full(raw.shape, np.nan, dtype=np.float32)
    probability[np.isin(raw, [0, 1])] = 0.0
    probability[np.isin(raw, [2, 3])] = 1.0
    return probability, valid


def local_features(probability: np.ndarray, valid: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    weight = valid.astype(np.float32)
    numerator = ndimage.uniform_filter(np.where(valid, probability, 0.0), size=3, mode="constant")
    denominator = ndimage.uniform_filter(weight, size=3, mode="constant")
    mean = np.full(probability.shape, np.nan, dtype=np.float32); good = denominator > 0
    mean[good] = numerator[good] / denominator[good]
    variance = ndimage.uniform_filter(np.where(valid, probability ** 2, 0.0), size=3, mode="constant")
    variance[good] = variance[good] / denominator[good] - mean[good] ** 2
    boundary = np.sqrt(np.maximum(variance, 0)).astype(np.float32)
    return np.sqrt(np.maximum(variance, 0)).astype(np.float32), boundary


def main() -> None:
    ensure_layout(); cfg, _, _, manifest = configured_inputs(); rows = resolve_scene_rows(); outputs=[]; inventory=[]
    for row in rows:
        out = DATA / "geo_features" / f"{row['sample_id']}_geo_features.npz"
        if out.exists():
            with np.load(out, allow_pickle=False) as z:
                geo_valid = z["geo_valid_fraction"].astype(bool)
                geo_sources = z["geo_source_count"]
            outputs.append(out)
            inventory.append({"scene_id": row["sample_id"], "feature_file": str(out), "geo_valid_fraction": float(np.mean(geo_valid)), "geo_source_count_mean": float(np.nanmean(geo_sources)), "geo_time_difference_minutes": row["time_diff_min"], "geo_source_disagreement_available": False, "geo_vza_available": False, "resumed_existing": True})
            continue
        run = Path(row["stage_run_dir"]); fused = run / "fused_best_source"
        grid = json.loads((run / "reprojected_grid" / "target_grid_definition.json").read_text(encoding="utf-8"))
        raw, raw_ok, _ = load_npz(fused / "fused_cloud_mask.npz")
        probability, codes_ok = geo_binary_probability(raw)
        valid = raw_ok & codes_ok
        source_count, source_count_ok, _ = load_npz(fused / "valid_count_map_cloud_mask.npz")
        # All following derived quantities depend exclusively on GEO values/masks.
        std_grid, boundary_grid = local_features(probability, valid)
        epic = read_epic(Path(row["epic_clm_file"]), cfg)
        geo_probability, geo_valid = sample_geo_grid(probability, valid, epic["lat"], epic["lon"], grid)
        geo_std, _ = sample_geo_grid(std_grid, valid, epic["lat"], epic["lon"], grid)
        geo_boundary, _ = sample_geo_grid(boundary_grid, valid, epic["lat"], epic["lon"], grid)
        geo_sources, source_ok = sample_geo_grid(source_count, source_count_ok, epic["lat"], epic["lon"], grid)
        np.savez_compressed(out,
            geo_cloud_fraction=geo_probability, geo_cloud_probability=geo_probability,
            geo_cloud_fraction_std=geo_std, geo_boundary_fraction=geo_boundary,
            geo_source_count=geo_sources, geo_source_disagreement=np.full(geo_probability.shape, np.nan, dtype=np.float32),
            geo_valid_fraction=geo_valid.astype(np.float32), geo_time_difference_minutes=np.full(geo_probability.shape, float(row["time_diff_min"]), dtype=np.float32),
            geo_vza_mean=np.full(geo_probability.shape, np.nan, dtype=np.float32),
            geo_missing_disagreement_mask=np.ones(geo_probability.shape, dtype=np.uint8), geo_missing_vza_mask=np.ones(geo_probability.shape, dtype=np.uint8),
        )
        outputs.append(out)
        inventory.append({"scene_id": row["sample_id"], "feature_file": str(out), "geo_valid_fraction": float(np.mean(geo_valid)), "geo_source_count_mean": float(np.nanmean(geo_sources)), "geo_time_difference_minutes": row["time_diff_min"], "geo_source_disagreement_available": False, "geo_vza_available": False})
    table = DATA / "geo_features" / "geo_feature_inventory.csv"; write_csv(table, inventory); outputs.append(table)
    report = REPORTS / "05_geo_feature_inventory.md"
    report.write_text("\n".join(["# 05 GEO 特征清单", "", f"生成时间：`{utc_now()}`", "", "- 输入仅为本次重建的 `fused_best_source` GEO cloud mask、valid mask 和 source-count map；没有使用 EPIC target 生成 GEO feature。", "- `geo_boundary_fraction` 与 `geo_cloud_fraction_std` 均由 GEO cloud probability 的 3×3 局部方差产生。", "- 融合产物不含可审计的 per-pixel source-disagreement 或 GEO VZA；这两项保留为 NaN 并提供 missing-mask channel，不会被伪造为 0。", "- `geo_cloud_probability` 当前是 GEO Policy-A binary mask 的 0/1 表示，不是校准的云概率；若以后提供正式 GEO probability 产品，须创建新 dataset version。", "" ]), encoding="utf-8")
    outputs.append(report); write_manifest("05_geo_feature_inventory", Path(__file__), [manifest], outputs, {"scene_count": len(rows), "target_leakage_check": "GEO features derive only from GEO products"})
    print(f"Built GEO feature files for {len(rows)} scenes")


if __name__ == "__main__":
    sys.exit(main())
