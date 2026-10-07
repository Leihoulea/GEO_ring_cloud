"""Audit frozen EPIC and freshly rebuilt, navigation-verified GEO inputs."""
from __future__ import annotations

import collections
import sys
from pathlib import Path

import h5py
import numpy as np

from common import DATA, REPORTS, configured_inputs, ensure_layout, h5_array, resolve_scene_rows, utc_now, write_csv, write_manifest


def main() -> None:
    ensure_layout()
    cfg, epic_root, runs_root, upstream_manifest = configured_inputs()
    rows = resolve_scene_rows()
    audit_rows = []
    variable_rows = []
    for row in rows:
        epic = Path(row["epic_clm_file"])
        mask, mask_attrs = h5_array(epic, cfg["epic_cloud_mask_path"], decode=False)
        earth, _ = h5_array(epic, "geolocation_data/earth_mask", decode=False)
        run = Path(row["stage_run_dir"])
        fused = run / "fused_best_source"
        audit_rows.append({
            "scene_id": row["sample_id"], "scene_time_utc": row["epic_time_utc"], "scene_date": row["scene_date"],
            "epic_clm_file": str(epic), "epic_file_sha256": "deferred_raw_immutable", "epic_shape": "x".join(map(str, mask.shape)),
            "epic_valid_fraction": round(float(np.mean(np.isin(mask, [1, 2, 3, 4]))), 6), "epic_earth_fraction": round(float(np.mean(earth == 1)), 6),
            "geo_matched": True, "geo_time": row["nearest_georing_time_utc"], "geo_time_difference_minutes": row["time_diff_min"],
            "geo_cloud_mask_available": (fused / "fused_cloud_mask.npz").exists(), "geo_cth_available": (fused / "fused_cloud_top_height_km.npz").exists(),
            "geo_source_count_available": (fused / "valid_count_map_cloud_mask.npz").exists(), "geo_valid_fraction": "computed_in_feature_stage",
            "epic_vza_available": True, "epic_sza_available": True, "geo_vza_available": False,
            "land_sea_available": True, "quality_available": "Quality_Assurance" in h5py.File(epic, "r")["geophysical_data"],
            "source_run_dir": str(run), "epic_path_rebound": row["epic_path_rebound"],
        })
        if not variable_rows:
            with h5py.File(epic, "r") as f:
                for group in ["geolocation_data", "geophysical_data"]:
                    for name, ds in f[group].items():
                        if not isinstance(ds, h5py.Dataset):
                            continue
                        variable_rows.append({"group": group, "variable": name, "shape": "x".join(map(str, ds.shape)), "dtype": str(ds.dtype), "units": str(ds.attrs.get("units", "")), "long_name": str(ds.attrs.get("long_name", "")), "fill_value": str(ds.attrs.get("_FillValue", ""))})
    audit_csv = DATA / "index" / "scene_index_202403.csv"
    vars_csv = DATA / "index" / "epic_variable_inventory.csv"
    write_csv(audit_csv, audit_rows)
    write_csv(vars_csv, variable_rows)
    dates = sorted({r["scene_date"] for r in audit_rows})
    codes = collections.Counter(int(v) for r in rows for v in np.unique(h5_array(Path(r["epic_clm_file"]), cfg["epic_cloud_mask_path"], decode=False)[0]))
    report = REPORTS / "01_input_data_audit.md"
    report.write_text("\n".join([
        "# 01 输入数据审计", "", f"生成时间：`{utc_now()}`", "",
        "## 结论", "", f"- 已校验导航版本的配对场景：**{len(audit_rows)}** 个，覆盖 {dates[0]} 至 {dates[-1]}。",
        f"- EPIC 原始路径由历史 `F:` 清单按文件名重绑定至配置的 `GEO_RING_EXTERNAL_EPIC_L2_ROOT`；成功重绑定 {sum(bool(r['epic_path_rebound']) for r in audit_rows)} 个场景。",
        "- EPIC `Cloud_Mask` 语义来自文件属性：1–2 为 clear、3–4 为 cloud；0 是 non-Earth，未被作为训练标签。",
        "- GEO 特征来自已完成且只读引用的 `fused_best_source`；每个场景均检查 Meteosat 导航版本。" if cfg.get("geo_runs_root_env") else "- GEO 特征来自本次从原始产品重建的 `fused_best_source`；每个场景均检查 Meteosat 导航版本。",
        "", "## 限制与门禁", "", f"- 当前为 {len(audit_rows)} 次冻结 EPIC 配对，而不是每个 EPIC 时次的全量配对；所有结论只适用于此样本集。",
        "- `geo_vza` 尚未在既有融合产物中找到，第一版将把它记录为缺失而非伪造数值。",
        "- 完成 `04_coarsening_validation.md` 的图形检查前禁止训练。",
        "", "## 原始 Cloud_Mask 类别计数", "", *[f"- `{k}`: {v}" for k, v in sorted(codes.items())],
        "", "## 机器可读产物", "", f"- `{audit_csv}`", f"- `{vars_csv}`", "",
    ]), encoding="utf-8")
    write_manifest("01_input_data_audit", Path(__file__), [upstream_manifest], [audit_csv, vars_csv, report], {"dataset_version": cfg["dataset_version"], "epic_root": str(epic_root), "runs_root": str(runs_root), "scene_count": len(audit_rows)})
    print(f"Audited {len(audit_rows)} reusable scenes: {report}")


if __name__ == "__main__":
    sys.exit(main())
