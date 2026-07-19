from __future__ import annotations

import csv
import json
import math
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from geo_ring_cloud.paths import CODE_ROOT, DATA_CHECK_ROOT, EVIDENCE_ROOT, GEOMETRY_ROOT, STAGE_ROOT


COMPONENT_ROLE = "evidence_pack_builder"

STAGE1_ROOT = STAGE_ROOT

LATEST_ROOT = EVIDENCE_ROOT / "latest"
SNAPSHOTS_ROOT = EVIDENCE_ROOT / "snapshots"

REPORTS_ROOT = STAGE1_ROOT / "reports"
TIME_INDEX_ROOT = STAGE1_ROOT / "time_index"
STANDARDIZED_ROOT = STAGE1_ROOT / "standardized_native"
REPROJECTED_ROOT = STAGE1_ROOT / "reprojected_grid"
FUSED_ROOT = STAGE1_ROOT / "fused_best_source"
SOURCE_DIAG_ROOT = STAGE1_ROOT / "source_selection_diagnostics"
GEOM06E_ROOT = STAGE1_ROOT / "geometry_angle_sync_06e"
AUDIT06F_ROOT = STAGE1_ROOT / "data_asset_audit_06f"
OVERLAP07_ROOT = STAGE1_ROOT / "overlap_validation"
OVERLAP07P_ROOT = STAGE1_ROOT / "overlap_validation_07p"

PATH_TOKENS = {
    "@DATA_CHECK_ROOT@": str(DATA_CHECK_ROOT),
    "@GEOMETRY_ROOT@": str(GEOMETRY_ROOT),
    "@STAGE_ROOT@": str(STAGE1_ROOT),
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_stamp(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def snapshot_stamp(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def render_path_tokens(text: str) -> str:
    for token, path in PATH_TOKENS.items():
        text = text.replace(token, path)
    return text


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_path_tokens(text).rstrip() + "\n", encoding="utf-8-sig")


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8-sig")


def remove_tree_contents(path: Path) -> None:
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        return
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def copy_latest_to_snapshot(snapshot_root: Path) -> None:
    if snapshot_root.exists():
        raise RuntimeError(f"snapshot already exists: {snapshot_root}")
    shutil.copytree(LATEST_ROOT, snapshot_root)


def md_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def list_files(root: Path, pattern: str = "*") -> list[Path]:
    if not root.exists():
        return []
    return sorted([p for p in root.rglob(pattern) if p.is_file()])


def file_count_and_size(root: Path) -> tuple[int, int]:
    count = 0
    size = 0
    for p in list_files(root):
        count += 1
        size += p.stat().st_size
    return count, size


def human_size(num: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(num)
    idx = 0
    while value >= 1024.0 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    return f"{value:.2f} {units[idx]}"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def normalized_suffix(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if not suffix:
        return None
    if len(suffix) > 12:
        return None
    if suffix[1:].isdigit():
        return None
    return suffix


def standardized_products() -> dict[str, list[str]]:
    products: dict[str, list[str]] = defaultdict(list)
    for path in list_files(STANDARDIZED_ROOT, "*_native_cloud_v0.npz"):
        stem = path.stem.replace("_native_cloud_v0", "")
        parts = stem.split("_")
        if len(parts) < 4:
            continue
        satellite = "_".join(parts[:-3])
        product = parts[-3]
        products[satellite].append(product)
    return {k: sorted(v) for k, v in sorted(products.items())}


def reprojected_products() -> dict[str, dict[str, list[str]]]:
    out: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for sat_dir in sorted([p for p in REPROJECTED_ROOT.iterdir() if p.is_dir()]):
        for path in sat_dir.glob("*.npz"):
            name = path.stem.replace("_grid_20240305_0000", "")
            prefix = sat_dir.name + "_"
            if not name.startswith(prefix):
                continue
            tail = name[len(prefix):]
            parts = tail.split("_")
            if len(parts) < 2:
                continue
            product = parts[0]
            variable = "_".join(parts[1:])
            out[sat_dir.name][product].append(variable)
    return {
        sat: {prod: sorted(vars_) for prod, vars_ in sorted(prod_map.items())}
        for sat, prod_map in sorted(out.items())
    }


def fused_variables() -> list[str]:
    out = []
    for path in sorted(FUSED_ROOT.glob("fused_*.npz")):
        name = path.stem
        if name == "fused_geo_ring_cloud_20240305_0000":
            continue
        out.append(name.replace("fused_", ""))
    return out






def script_manifest_rows() -> list[list[str]]:
    script_status = {
        "01_build_core_time_index.py": ("01", "executed"),
        "02_build_standardized_cloud_native.py": ("02", "executed"),
        "03_validate_standardized_cloud_native.py": ("03", "executed"),
        "03_5_semantic_validation_patch.py": ("03.5", "executed"),
        "04_check_fy4b_geo_alignment.py": ("04", "executed"),
        "04b_fy4b_dqf_bit_decode_diagnostics.py": ("04b", "executed"),
        "05_reproject_cloud_to_grid.py": ("05", "executed"),
        "06_fuse_best_source.py": ("06", "executed"),
        "06_5_source_selection_diagnostics.py": ("06.5", "executed"),
        "stage_06c_geometry_audit/stage_06c_geometry_parameter_audit.py": ("06c-legacy", "executed canonical"),
        "stage_06c_geometry_audit/stage_06c_multi_satellite_geometry_metadata_audit.py": ("06c-final", "executed canonical"),
        "stage_06c_geometry_audit/stage_06c_claas3_geometry_angle_lineage.py": ("06c", "present canonical gate"),
        "06c_geometry_parameter_audit.py": ("06c-legacy", "compatibility entrypoint"),
        "06c_multi_satellite_geometry_metadata_audit.py": ("06c-final", "compatibility entrypoint"),
        "stage_06c_claas3_geometry_angle_lineage.py": ("06c", "compatibility entrypoint"),
        "06d_himawari_full_disk_geometry_validation.py": ("06d", "executed"),
        "stage_06e_geometry_angle_sync/stage_06e_full_geometry_angle_source_sync.py": ("06e", "executed canonical"),
        "stage_06e_geometry_angle_sync/stage_06e_vza_ecef_final_audit.py": ("06e", "present canonical audit"),
        "06e_full_geometry_angle_source_sync_patch.py": ("06e", "compatibility entrypoint"),
        "06e_vza_ecef_final_audit.py": ("06e", "compatibility entrypoint"),
        "stage_06f_data_asset_audit/stage_06f_unknown_aware_data_asset_audit.py": ("06f", "executed canonical"),
        "stage_06f_data_asset_audit/stage_06f_reexport_with_obitype_patch.py": ("06f", "executed canonical patch"),
        "stage_06f_data_asset_audit/stage_06f_report_sync.py": ("06f", "executed canonical patch"),
        "06f_unknown_aware_data_asset_audit.py": ("06f", "compatibility entrypoint"),
        "06f_reexport_with_obitype_patch.py": ("06f", "compatibility entrypoint"),
        "06f_report_sync_patch.py": ("06f", "compatibility entrypoint"),
        "07_overlap_consistency_validation.py": ("07-original", "executed"),
        "07p_overlap_validator_hotfix.py": ("07p", "executed"),
        "07p_b_source_boundary_magnitude_review.py": ("07p-b", "executed"),
        "07v2_formal_single_time_report.py": ("07v2", "executed"),
        "download_geo_geometry_samples.py": ("06c-support", "executed"),
        "stage1_common.py": ("shared", "support library"),
    }
    rows = []
    paths = sorted(CODE_ROOT.glob("*.py")) + sorted(CODE_ROOT.glob("stage_*/*.py"))
    for path in paths:
        if path.name == "__init__.py":
            continue
        relative = path.relative_to(CODE_ROOT).as_posix()
        stage, status = script_status.get(relative, ("unclassified", "present"))
        rows.append([relative, stage, status, str(path)])
    return rows








def build_06e_status() -> str:
    return """# 06e 状态

- 报告：`@STAGE_ROOT@\\reports\\06e_full_geometry_angle_source_sync_patch_report.md`
- 当前状态：`COMPLETE`
- Gate 摘要：
  - `ANGLE_PROVENANCE_GATE = PASS`
  - `OFFICIAL_ANGLE_INGEST_GATE = PASS`
  - `GEOMETRY_SYNC_GATE = PASS`
  - `SOURCE_MAP_STABILITY_GATE = PASS`
  - `ANGLE_LAYER_GATE = PASS`
- 决策影响：
  - 06e 已允许进入 Stage 07。
  - 07 必须沿用 06e 产出的同步角度层和 provenance inventory。
"""


def build_07_status() -> str:
    return """# 07 状态

## 当前状态

- 07 original overlap validator：`completed`，但由于后续发现 validator implementation issue，已不再是当前 authority。
- 07p hotfix：`completed`，`PASS`
- 07p-b boundary review：`completed`，并放行正式 07v2
- 07v2 formal single-time report：`completed`，`PASS_WITH_WARNINGS`
- 当前 acceptance 状态：`CLOSED_LOOP_PASS_WITH_WARNINGS`
- 多时次扩展状态：`HOLD`

## 当前 authoritative files

- `@STAGE_ROOT@\\reports\\07v2_overlap_consistency_validation_report.md`
- `@STAGE_ROOT@\\reports\\stage1_single_time_acceptance_decision.md`
- `@STAGE_ROOT@\\reports\\overlap_validation_07p_report.md`
- `@STAGE_ROOT@\\reports\\07p_boundary_magnitude_review.md`

## 继续携带的 warning

1. Meteosat 相关 CTH 一致性偏弱。
2. FY4B-Himawari 的 CTT/CTP 存在不连续性。
3. COT/CER 仍是高不确定性变量。
4. 连续变量在 source boundary 上仍存在跳变。
5. 当前还不是 production-grade blending。
"""


def build_core_product_config() -> str:
    return """# 核心产品配置

- 主原型时次：`2024-03-05T00:00:00Z`
- 代表时次：
  - `2024-03-05T04:00:00Z`
  - `2024-03-05T05:00:00Z`
  - `2024-03-05T06:00:00Z`
  - `2024-03-05T07:00:00Z`
  - `2024-03-05T12:00:00Z`
  - `2024-03-05T18:00:00Z`

## 核心产品

- FY4B: `CLM, CLP, CLT, CTH, CTT, CTP, GEO`; optional `FDI`
- GOES-16: `ACMF, ACHAF, ACHTF, CTPF, ACTPF, CODF, CPSF`
- GOES-18: `ACMF, ACHAF, ACHTF, CTPF, ACTPF, CODF, CPSF`
- Himawari-9: `CMSK, CHGT`
- Meteosat-0deg: `CLM, CTH`
- Meteosat-IODC: `CLM, CTH`
"""




def build_product_inventory() -> str:
    std = standardized_products()
    reproj = reprojected_products()
    fused = fused_variables()
    lines = ["# 产品索引", "", "## Standardized native 产品", ""]
    rows = [[sat, ", ".join(products)] for sat, products in std.items()]
    lines.append(md_table(["卫星", "产品"], rows))
    lines.extend(["", "## 重投影产品与变量", ""])
    for sat, prod_map in reproj.items():
        lines.append(f"### {sat}")
        rows = [[prod, ", ".join(vars_)] for prod, vars_ in prod_map.items()]
        lines.append(md_table(["产品", "reprojected_grid 下发现的变量"], rows))
        lines.append("")
    lines.extend(["## 融合变量", "", "- " + ", ".join(fused)])
    return "\n".join(lines)


def build_quicklook_inventory() -> str:
    rows = quicklook_inventory_rows()
    return "# Quicklook 索引\n\n" + md_table(["位置", "PNG 数量", "路径"], rows)


def build_report_inventory() -> str:
    rows = report_inventory_rows()
    return "# 报告与统计索引\n\n" + md_table(["文件", "阶段", "源路径"], rows)


def build_script_manifest() -> str:
    rows = script_manifest_rows()
    return "# 脚本清单\n\n" + md_table(["脚本", "阶段", "状态", "路径"], rows)






def build_next_steps() -> str:
    return """# 后续建议

## 当前建议路径

1. 保持当前单时次原型链作为已接受 baseline。
2. 以 07v2 作为正式单时次 overlap 报告。
3. 不要把当前输出提升为 production-grade blending。
4. 在当前 warnings 被明确处置或接受之前，继续保持 six-time / batch expansion 为 HOLD。

## 最高优先级科学 warning

1. Meteosat 相关 CTH 一致性偏弱。
2. FY4B-Himawari 的 CTT/CTP 不连续。
3. COT/CER 仍属于高不确定性诊断变量。
4. 连续变量 source boundary 跳变仍然显著。

## 建议的近期动作

- 以当前 evidence pack 作为审计基线。
- 若继续推进，重点应是削减 warning，而不是重新证明已经通过 acceptance 的单时次闭环。
"""


def build_stage01() -> str:
    return """# Stage 01 - 核心时次索引

- 状态：`COMPLETE`
- Gate：`PASS`
- 脚本：`01_build_core_time_index.py`
- 当前 authority：
  - `@STAGE_ROOT@\\time_index\\core_time_index_report.md`
  - `@STAGE_ROOT@\\time_index\\core_time_index.csv`
  - `@STAGE_ROOT@\\time_index\\usable_times_ranked.csv`

## 当前事实

- 主原型时次：`2024-03-05T00:00:00Z`
- 代表时次：`04:00, 05:00, 06:00, 07:00, 12:00, 18:00 UTC`
- FY4B `2024-03-01` 至 `2024-03-04` 视为 `OFFICIAL_UNAVAILABLE`
- FY4B `FDI` 属于 optional
"""


def build_stage02() -> str:
    std_count = len(list_files(STANDARDIZED_ROOT, "*_native_cloud_v0.npz"))
    return f"""# Stage 02 - Native 标准化

- 状态：`COMPLETE`
- Gate：`PASS`
- 脚本：`02_build_standardized_cloud_native.py`
- 当前 authority：
  - `{REPORTS_ROOT / "standardized_native_build_report.md"}`
  - `{STANDARDIZED_ROOT / "standardized_native_inventory.csv"}`
  - `{STANDARDIZED_ROOT}`

## 当前事实

- 当前发现的 native standardized 样本文件数：`{std_count}`
- 覆盖卫星组：FY4B、GOES-16、GOES-18、Himawari-9、Meteosat-0deg、Meteosat-IODC
- 本阶段不涉及下载、不涉及重投影、不涉及融合
"""


def build_stage03() -> str:
    return f"""# Stage 03 - Native 结构校验

- 状态：`COMPLETE`
- Gate：结构校验为 `PASS`，语义跟踪为 `PASS_WITH_WARNINGS`
- 脚本：
  - `03_validate_standardized_cloud_native.py`
  - `03_5_semantic_validation_patch.py`
- 当前 authority：
  - `{REPORTS_ROOT / "standardized_native_validate_report.md"}`
  - `{REPORTS_ROOT / "standardized_native_semantic_validation_report.md"}`
  - `{STANDARDIZED_ROOT / "standardized_native_variable_stats_validated.csv"}`
  - `{STANDARDIZED_ROOT / "standardized_native_semantic_issues.csv"}`

## 当前事实

- 6 个卫星组的结构性校验均已通过。
- 语义跟踪保留了 metadata 完整性、Meteosat 经度规范化和质量标志解释方面的 warning。
"""


def build_stage03_5() -> str:
    return f"""# Stage 03.5 - 语义校验补丁

- 状态：`COMPLETE`
- Gate：`PASS_WITH_WARNINGS`
- 脚本：`03_5_semantic_validation_patch.py`
- 当前 authority：
  - `{REPORTS_ROOT / "standardized_native_semantic_validation_report.md"}`
  - `{STANDARDIZED_ROOT / "standardized_native_semantic_issues.csv"}`
  - `{STANDARDIZED_ROOT / "standardized_native_code_tables.csv"}`

## 当前事实

- 已对 cloud-mask、phase/type、quality 等字段进行了语义 code-table 检查。
- 在进入 Stage 04 之前，fill 和 suspect code 已被专项检查。
- 剩余 warning 主要是质量语义和 metadata 完整性，不是数据无法读取。
"""


def build_stage04() -> str:
    return f"""# Stage 04 - FY4B GEO 同格验证

- 状态：`COMPLETE`
- Gate：`PASS_WITH_WARNINGS`
- 脚本：
  - `04_check_fy4b_geo_alignment.py`
  - `04b_fy4b_dqf_bit_decode_diagnostics.py`
- 当前 authority：
  - `{REPORTS_ROOT / "fy4b_geo_alignment_report.md"}`
  - `{REPORTS_ROOT / "fy4b_dqf_bit_decode_diagnostics_report.md"}`
  - `{REPORTS_ROOT / "fy4b_geo_alignment_shape_check.csv"}`
  - `{REPORTS_ROOT / "fy4b_geo_angle_stats.csv"}`
  - `{REPORTS_ROOT / "fy4b_dqf_bit_decode_summary.csv"}`

## 当前事实

- FY4B L2 云产品与 GEO 角度场在 `2748 x 2748` 上同格。
- 没有发现明显翻转或圆盘边界错位。
- `NavQualityFlag` 不是 per-pixel 变量，不作为几何像元掩膜使用。
- FY4B 质量标志目前仍是诊断层，不作为成熟的 rating weight。
"""


def build_stage04b() -> str:
    return f"""# Stage 04b - FY4B DQF 位级诊断

- 状态：`COMPLETE`
- Gate：`PASS_WITH_WARNINGS`
- 脚本：`04b_fy4b_dqf_bit_decode_diagnostics.py`
- 当前 authority：
  - `{REPORTS_ROOT / "fy4b_dqf_bit_decode_diagnostics_report.md"}`
  - `{REPORTS_ROOT / "fy4b_dqf_bit_decode_summary.csv"}`
  - `{REPORTS_ROOT / "fy4b_quality_flag_rules.yaml"}`

## 当前事实

- CLM DQF 按枚举 code table 做了检查。
- CLP、CLT、CTH、CTT、CTP 的 DQF 按 bitfield 做了 first-pass screen。
- 该产物仅用于诊断，不把 FY4B 质量转换成连续融合权重。
"""


def build_stage05() -> str:
    target = read_json(REPROJECTED_ROOT / "target_grid_definition.json")
    return f"""# Stage 05 - 重投影到统一网格

- 状态：`COMPLETE`
- Gate：`PASS_WITH_WARNINGS`
- 脚本：`05_reproject_cloud_to_grid.py`
- 当前 authority：
  - `{REPORTS_ROOT / "reproject_cloud_to_grid_report.md"}`
  - `{REPROJECTED_ROOT / "target_grid_definition.json"}`
  - `{REPROJECTED_ROOT}`

## 当前事实

- 目标网格 CRS：`{target["crs"]}`
- 经度范围：`{target["lon_min"]} .. {target["lon_max"]}`
- 纬度范围：`{target["lat_min"]} .. {target["lat_max"]}`
- 分辨率：`{target["resolution_degree"]}`
- 网格形状：`{target["shape"][0]} x {target["shape"][1]}`

## 重要证据说明

- 早期 05 摘要和 `reprojected_variable_inventory.csv` 不能完整枚举实际重投影产物。
- 当前 authoritative evidence 以 `reprojected_grid/` 实际文件集，以及其被 Stage 06 成功消费这一事实为准。
"""


def build_stage06() -> str:
    return """# Stage 06 - Best-source 融合

- 状态：`COMPLETE`
- Gate：`PASS_WITH_WARNINGS`
- 脚本：`06_fuse_best_source.py`
- 当前 authority：
  - `@STAGE_ROOT@\\reports\\fuse_best_source_report.md`
  - `@STAGE_ROOT@\\fused_best_source\\fusion_stats.csv`
  - `@STAGE_ROOT@\\fused_best_source\\fusion_source_frequency.csv`
  - `@STAGE_ROOT@\\fused_best_source\\fused_geo_ring_cloud_20240305_0000.npz`

## 当前覆盖率

- `cloud_mask = 0.8255`
- `cloud_top_height_km = 0.7127`
- `cloud_top_temperature_K = 0.6136`
- `cloud_top_pressure_hPa = 0.6081`
- `cloud_phase = 0.7664`
- `cloud_type = 0.3439`
- `cloud_optical_thickness = 0.4029`
- `cloud_effective_radius_um = 0.2353`

## 当前 warning

- `cloud_type` 目前只由 FY4B 参与。
- `cloud_effective_radius_um` 目前只由 GOES 参与。
- 当前结果仍是 best-source selection，不是 production-grade blending。
"""


def build_stage06_5() -> str:
    return f"""# Stage 06.5 - Source-selection 诊断

- 状态：`COMPLETE`
- Gate：`PASS`
- 脚本：`06_5_source_selection_diagnostics.py`
- 当前 authority：
  - `{REPORTS_ROOT / "source_selection_diagnostics_report.md"}`
  - `{SOURCE_DIAG_ROOT / "selected_vs_min_vza_agreement.csv"}`
  - `{SOURCE_DIAG_ROOT / "geometry_driver_summary.csv"}`
  - `{SOURCE_DIAG_ROOT / "rating_margin_summary.csv"}`

## 当前事实

- 已检查 selected source 与 minimum-VZA source 的一致性。
- 已区分哪些 source 区域由真实几何驱动，哪些受近似 view-weight 影响。
- 在 06b/06c/06d 几何补强之前，已量化 rating margin 和 source boundary 敏感性。
"""


def build_stage06c() -> str:
    return """# Stage 06c - 多星几何元数据审计

- 状态：`COMPLETE`
- Gate：`PASS_WITH_WARNINGS`
- canonical 脚本：
  - `stage_06c_geometry_audit/stage_06c_geometry_parameter_audit.py`（baseline audit）
  - `stage_06c_geometry_audit/stage_06c_multi_satellite_geometry_metadata_audit.py`（增强版 authoritative audit）
  - `stage_06c_geometry_audit/stage_06c_claas3_geometry_angle_lineage.py`（CLAAS-3 lineage gate）
- 历史顶层路径仅作为 compatibility entrypoint。
- 当前 authority：
  - `@GEOMETRY_ROOT@\\vza_method_comparison_report.md`
  - `@GEOMETRY_ROOT@\\goes_l1b_vs_l2_projection_check.csv`
  - `@GEOMETRY_ROOT@\\himawari_geometry_metadata_audit.csv`
  - `@GEOMETRY_ROOT@\\meteosat_geometry_metadata_audit.csv`
  - `@GEOMETRY_ROOT@\\vza_method_comparison_by_satellite.csv`

## 当前事实

- GOES 的 L1b 与 L2 projection metadata 已对齐。
- Meteosat 0deg 与 IODC 的 native metadata 审计已成功。
- FY4B 的 official VZA 已能与 computed VZA 对比。
- 06c 剩余的主要 warning 是 Himawari full-disk segment 还未验证，而这一点已由 06d 补强。
"""


def build_stage06d() -> str:
    return """# Stage 06d - Himawari 全圆盘几何验证

- 状态：`COMPLETE`
- Gate：`PASS_WITH_WARNINGS`
- 脚本：`06d_himawari_full_disk_geometry_validation.py`
- 当前 authority：
  - `@GEOMETRY_ROOT@\\Himawari-9\\himawari_full_disk_geometry_report.md`
  - `@GEOMETRY_ROOT@\\Himawari-9\\himawari_full_segment_inventory.csv`
  - `@GEOMETRY_ROOT@\\Himawari-9\\himawari_full_disk_geometry_audit.csv`
  - `@GEOMETRY_ROOT@\\Himawari-9\\himawari_vza_method_comparison.csv`

## 当前事实

- 10 个 B13 full-disk segments（`S0110` 至 `S1010`）已全部到位。
- full-disk area shape：`5500 x 5500`
- 06c 的单 segment header 参数与 full-disk 参数一致。
- 当前 06b/current Himawari subpoint longitude 与 full-disk SSP longitude 相差 `0.065604 deg`。
- 尽管存在这个差值，current 与 full-segment 的 VZA 差异仍足够小，仍可接受。

## 剩余 warning

- 仍缺少 JAXA official gridded VZA 对照。
"""


def build_stage06e() -> str:
    return """# Stage 06e - 全链几何与角度源同步

- 状态：`COMPLETE`
- Gate：`PASS`
- canonical 脚本：`stage_06e_geometry_angle_sync/stage_06e_full_geometry_angle_source_sync.py`
- 历史兼容入口：`06e_full_geometry_angle_source_sync_patch.py`
- 当前 authority：
  - `@STAGE_ROOT@\\reports\\06e_full_geometry_angle_source_sync_patch_report.md`
  - `@STAGE_ROOT@\\geometry_angle_sync_06e\\angle_provenance_inventory.csv`
  - `@STAGE_ROOT@\\geometry_angle_sync_06e\\geometry_angle_source_policy.yaml`
  - `@STAGE_ROOT@\\geometry_angle_sync_06e\\source_map_change_summary.csv`

## 当前事实

- 5 个 gate 全部通过。
- 06e 已在多星之间同步 angle provenance 与 angle-layer usage。
- 06e 已明确允许进入 Stage 07。
"""


def build_stage06f() -> str:
    summary = read_json(AUDIT06F_ROOT / "audit_summary.json")
    gate = summary["gate_status"]
    file_counts = {row["read_status"]: row["count"] for row in summary["file_counts"]}
    unknown_counts = {row["priority"]: row["count"] for row in summary["unknown_counts_by_priority"]}
    return f"""# Stage 06f - 面向 unknown 的数据资产审计

- 状态：`COMPLETE`
- Gate：`PASS_WITH_WARNINGS`
- 脚本：
  - `06f_unknown_aware_data_asset_audit.py`
  - `06f_reexport_with_obitype_patch.py`
  - `06f_report_sync_patch.py`
- 当前 authority：
  - `{REPORTS_ROOT / "06f_unknown_aware_data_asset_audit_report.md"}`
  - `{AUDIT06F_ROOT / "audit_summary.json"}`
  - `{AUDIT06F_ROOT / "exports" / "high_priority_unknowns.csv"}`
  - `{AUDIT06F_ROOT / "exports" / "recommendation_matrix.csv"}`

## 当前事实

- Files read OK: `{file_counts.get("OK", 0)}`
- Files read ERROR: `{file_counts.get("ERROR", 0)}`
- HIGH unknown count: `{unknown_counts.get("HIGH", 0)}`
- MEDIUM unknown count: `{unknown_counts.get("MEDIUM", 0)}`
- LOW unknown count: `{unknown_counts.get("LOW", 0)}`
- `DISCOVERY_GATE = {gate["DISCOVERY_GATE"]}`
- `SEMANTIC_GATE = {gate["SEMANTIC_GATE"]}`
- `FUSION_READINESS_GATE = {gate["FUSION_READINESS_GATE"]}`
- `UNKNOWN_RISK_GATE = {gate["UNKNOWN_RISK_GATE"]}`
- `allow_enter_07 = {summary["allow_enter_07"]}`

## 解释

- 06f 的当前结论以最终报告和最终 summary JSON 为准。
- 不应再用更早的中间态 06f 结果代表当前 evidence-pack 口径。
"""


DATA_CHECK_GEOMETRY_ROOT = DATA_CHECK_ROOT / "geometry_variable_audit"
DATA_CHECK_LATEST_PROBE_ROOT = DATA_CHECK_ROOT / "latest_fy4b_goes_structure_probe"
DATA_CHECK_METEOSAT_DISCOVERY_ROOT = DATA_CHECK_ROOT / "meteosat_catalogue_discovery"
DATA_CHECK_METEOSAT_SERIES_ROOT = DATA_CHECK_ROOT / "meteosat_product_series_audit"
DATA_CHECK_STD_V0_ROOT = DATA_CHECK_ROOT / "standardized_cloud_v0_samples"
DATA_CHECK_GOES_METEOSAT_RUN_ROOT = DATA_CHECK_ROOT / "priority_download_run_goes_meteosat"
DATA_CHECK_METEOSAT_PRIORITY_ROOT = DATA_CHECK_ROOT / "meteosat_priority_cloud_download"


def _existing(paths: list[Path]) -> list[str]:
    return [str(path) for path in paths if path.exists()]


def quicklook_inventory_rows() -> list[list[str]]:
    dirs = [
        ("data_check_report/quicklooks", DATA_CHECK_ROOT / "quicklooks"),
        ("data_check_report/standardized_cloud_v0_samples", DATA_CHECK_STD_V0_ROOT),
        ("quicklooks_native", STAGE1_ROOT / "quicklooks_native"),
        ("quicklooks_reprojected", STAGE1_ROOT / "quicklooks_reprojected"),
        ("fused_best_source/quicklooks", FUSED_ROOT / "quicklooks"),
        ("source_selection_diagnostics/quicklooks", SOURCE_DIAG_ROOT / "quicklooks"),
        ("geometry_angle_sync_06e/quicklooks_angle", GEOM06E_ROOT / "quicklooks_angle"),
        ("geo_geometry_check/vza_difference_quicklooks", GEOMETRY_ROOT / "vza_difference_quicklooks"),
        ("overlap_validation/quicklooks", OVERLAP07_ROOT / "quicklooks"),
        ("overlap_validation_07p/quicklooks", OVERLAP07P_ROOT / "quicklooks"),
    ]
    rows: list[list[str]] = []
    for label, root in dirs:
        rows.append([label, str(len(list_files(root, "*.png"))), str(root)])
    rows.append(["reports/fy4b_cloud_geo_overlay_*.png", str(len(list(REPORTS_ROOT.glob("fy4b_cloud_geo_overlay_*.png")))), str(REPORTS_ROOT)])
    rows.append(["reports/fy4b_*_quicklook.png", str(len(list(REPORTS_ROOT.glob("fy4b_*_quicklook.png")))), str(REPORTS_ROOT)])
    return rows


def report_inventory_rows() -> list[list[str]]:
    rows: list[list[str]] = []
    known_stage_map = {
        "data_download_audit_report.md": "00",
        "one_sample_each_product_read_report.md": "00b",
        "standardized_cloud_v0_report.md": "00e",
        "variable_availability_report.md": "00",
        "06c_geometry_audit_report.md": "06c-legacy",
        "06e_full_geometry_angle_source_sync_patch_report.md": "06e",
        "06f_unknown_aware_data_asset_audit_report.md": "06f",
        "07p_boundary_magnitude_review.md": "07p-b",
        "07v2_overlap_consistency_validation_report.md": "07v2",
        "fuse_best_source_report.md": "06",
        "fy4b_geo_alignment_report.md": "04",
        "fy4b_dqf_bit_decode_diagnostics_report.md": "04b",
        "overlap_validation_07p_report.md": "07p",
        "overlap_validation_report.md": "07-original",
        "reproject_cloud_to_grid_report.md": "05",
        "source_selection_diagnostics_report.md": "06.5",
        "stage1_single_time_acceptance_decision.md": "07-acceptance",
        "standardized_native_build_report.md": "02",
        "standardized_native_semantic_validation_report.md": "03.5",
        "standardized_native_validate_report.md": "03",
    }
    roots = [
        REPORTS_ROOT,
        DATA_CHECK_ROOT,
        DATA_CHECK_GEOMETRY_ROOT,
        DATA_CHECK_LATEST_PROBE_ROOT,
        DATA_CHECK_METEOSAT_DISCOVERY_ROOT,
        DATA_CHECK_METEOSAT_SERIES_ROOT,
        DATA_CHECK_GOES_METEOSAT_RUN_ROOT,
        DATA_CHECK_METEOSAT_PRIORITY_ROOT,
    ]
    for root in roots:
        for path in sorted(root.glob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".md", ".csv", ".json", ".yaml"}:
                continue
            rows.append([path.name, known_stage_map.get(path.name, "supporting"), str(path)])
    rows.extend(
        [
            ["geometry_variable_audit_report.md", "00c", str(DATA_CHECK_GEOMETRY_ROOT / "geometry_variable_audit_report.md")],
            ["latest_fy4b_goes_structure_report.md", "00e-support", str(DATA_CHECK_LATEST_PROBE_ROOT / "latest_fy4b_goes_structure_report.md")],
            ["meteosat_catalogue_discovery_report.md", "00d", str(DATA_CHECK_METEOSAT_DISCOVERY_ROOT / "meteosat_catalogue_discovery_report.md")],
            ["meteosat_product_series_audit.md", "00d", str(DATA_CHECK_METEOSAT_SERIES_ROOT / "meteosat_product_series_audit.md")],
            ["priority_download_goes_meteosat_report.md", "00f", str(DATA_CHECK_GOES_METEOSAT_RUN_ROOT / "priority_download_goes_meteosat_report.md")],
            ["meteosat_priority_download_report.md", "00f", str(DATA_CHECK_METEOSAT_PRIORITY_ROOT / "meteosat_priority_download_report.md")],
            ["vza_method_comparison_report.md", "06c-final", str(GEOMETRY_ROOT / "vza_method_comparison_report.md")],
            ["himawari_full_disk_geometry_report.md", "06d", str(GEOMETRY_ROOT / "Himawari-9" / "himawari_full_disk_geometry_report.md")],
        ]
    )
    dedup: list[list[str]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = (row[0], row[2])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(row)
    return dedup


def evidence_sources_by_stage() -> dict[str, list[str]]:
    return {
        "00": _existing(
            [
                DATA_CHECK_ROOT / "data_download_audit_report.md",
                DATA_CHECK_ROOT / "goes_cross_product_match.csv",
                DATA_CHECK_ROOT / "himawari_cross_product_match.csv",
                DATA_CHECK_ROOT / "meteosat_cross_product_match.csv",
                DATA_CHECK_ROOT / "fy4b_cross_product_match.csv",
            ]
        ),
        "00b": _existing(
            [
                DATA_CHECK_ROOT / "one_sample_each_product_read_report.md",
                DATA_CHECK_ROOT / "one_sample_each_product_selection.csv",
                DATA_CHECK_ROOT / "one_sample_each_product_variables.csv",
                DATA_CHECK_ROOT / "one_sample_each_product_variable_statistics.csv",
                DATA_CHECK_ROOT / "manual_variable_mapping_by_product.yaml",
                DATA_CHECK_ROOT / "meteosat_grib_deep_check.csv",
            ]
        ),
        "00c": _existing(
            [
                DATA_CHECK_GEOMETRY_ROOT / "geometry_variable_audit_report.md",
                DATA_CHECK_GEOMETRY_ROOT / "product_variable_inventory_full.csv",
                DATA_CHECK_GEOMETRY_ROOT / "geometry_capability_matrix.csv",
                DATA_CHECK_GEOMETRY_ROOT / "quality_flag_capability_matrix.csv",
                DATA_CHECK_GEOMETRY_ROOT / "cloud_variable_capability_matrix.csv",
                DATA_CHECK_GEOMETRY_ROOT / "recommended_minimal_download_list.md",
            ]
        ),
        "00d": _existing(
            [
                DATA_CHECK_METEOSAT_SERIES_ROOT / "meteosat_product_series_audit.md",
                DATA_CHECK_METEOSAT_DISCOVERY_ROOT / "meteosat_catalogue_discovery_report.md",
                DATA_CHECK_METEOSAT_DISCOVERY_ROOT / "candidate_collections_discovered.csv",
                DATA_CHECK_METEOSAT_DISCOVERY_ROOT / "candidate_collection_availability_202403.csv",
                DATA_CHECK_METEOSAT_DISCOVERY_ROOT / "candidate_sample_variable_inventory.csv",
            ]
        ),
        "00e": _existing(
            [
                DATA_CHECK_ROOT / "standardized_cloud_v0_report.md",
                DATA_CHECK_LATEST_PROBE_ROOT / "latest_fy4b_goes_structure_report.md",
                DATA_CHECK_STD_V0_ROOT / "FY4B_CLM.npz",
                DATA_CHECK_STD_V0_ROOT / "GOES-16_ACMF.npz",
                DATA_CHECK_STD_V0_ROOT / "Himawari-9_CMSK.npz",
            ]
        ),
        "00f": _existing(
            [
                DATA_CHECK_GOES_METEOSAT_RUN_ROOT / "priority_download_goes_meteosat_report.md",
                DATA_CHECK_GOES_METEOSAT_RUN_ROOT / "priority_download_manifest_all.csv",
                DATA_CHECK_GOES_METEOSAT_RUN_ROOT / "priority_download_status.csv",
                DATA_CHECK_GOES_METEOSAT_RUN_ROOT / "priority_download_verification.csv",
                DATA_CHECK_METEOSAT_PRIORITY_ROOT / "meteosat_priority_download_report.md",
                DATA_CHECK_METEOSAT_PRIORITY_ROOT / "meteosat_priority_download_status.csv",
            ]
        ),
        "01": _existing(
            [
                TIME_INDEX_ROOT / "core_time_index_report.md",
                TIME_INDEX_ROOT / "core_time_index.csv",
                TIME_INDEX_ROOT / "usable_times_ranked.csv",
            ]
        ),
        "02": _existing(
            [
                REPORTS_ROOT / "standardized_native_build_report.md",
                STANDARDIZED_ROOT / "standardized_native_inventory.csv",
                STANDARDIZED_ROOT / "standardized_native_variable_stats.csv",
            ]
        ),
        "03": _existing(
            [
                REPORTS_ROOT / "standardized_native_validate_report.md",
                STANDARDIZED_ROOT / "standardized_native_variable_stats_validated.csv",
                STANDARDIZED_ROOT / "standardized_native_file_validation.csv",
            ]
        ),
        "03.5": _existing(
            [
                REPORTS_ROOT / "standardized_native_semantic_validation_report.md",
                STANDARDIZED_ROOT / "standardized_native_semantic_issues.csv",
                STANDARDIZED_ROOT / "standardized_native_code_tables.csv",
            ]
        ),
        "04": _existing(
            [
                REPORTS_ROOT / "fy4b_geo_alignment_report.md",
                REPORTS_ROOT / "fy4b_geo_alignment_shape_check.csv",
                REPORTS_ROOT / "fy4b_geo_angle_stats.csv",
            ]
        ),
        "04b": _existing(
            [
                REPORTS_ROOT / "fy4b_dqf_bit_decode_diagnostics_report.md",
                REPORTS_ROOT / "fy4b_dqf_bit_decode_summary.csv",
                REPORTS_ROOT / "fy4b_quality_flag_rules.yaml",
            ]
        ),
        "05": _existing(
            [
                REPORTS_ROOT / "reproject_cloud_to_grid_report.md",
                REPROJECTED_ROOT / "target_grid_definition.json",
                REPROJECTED_ROOT,
            ]
        ),
        "06": _existing(
            [
                REPORTS_ROOT / "fuse_best_source_report.md",
                FUSED_ROOT / "fusion_stats.csv",
                FUSED_ROOT / "fusion_source_frequency.csv",
            ]
        ),
        "06.5": _existing(
            [
                REPORTS_ROOT / "source_selection_diagnostics_report.md",
                SOURCE_DIAG_ROOT / "selected_vs_min_vza_agreement.csv",
                SOURCE_DIAG_ROOT / "geometry_driver_summary.csv",
                SOURCE_DIAG_ROOT / "rating_margin_summary.csv",
            ]
        ),
        "06c": _existing(
            [
                GEOMETRY_ROOT / "vza_method_comparison_report.md",
                GEOMETRY_ROOT / "goes_l1b_vs_l2_projection_check.csv",
                GEOMETRY_ROOT / "himawari_geometry_metadata_audit.csv",
                GEOMETRY_ROOT / "meteosat_geometry_metadata_audit.csv",
                GEOMETRY_ROOT / "vza_method_comparison_by_satellite.csv",
            ]
        ),
        "06d": _existing(
            [
                GEOMETRY_ROOT / "Himawari-9" / "himawari_full_disk_geometry_report.md",
                GEOMETRY_ROOT / "Himawari-9" / "himawari_full_segment_inventory.csv",
                GEOMETRY_ROOT / "Himawari-9" / "himawari_full_disk_geometry_audit.csv",
                GEOMETRY_ROOT / "Himawari-9" / "himawari_vza_method_comparison.csv",
            ]
        ),
        "06e": _existing(
            [
                REPORTS_ROOT / "06e_full_geometry_angle_source_sync_patch_report.md",
                GEOM06E_ROOT / "angle_provenance_inventory.csv",
                GEOM06E_ROOT / "geometry_angle_source_policy.yaml",
                GEOM06E_ROOT / "official_vs_computed_vza_stats.csv",
            ]
        ),
        "06f": _existing(
            [
                REPORTS_ROOT / "06f_unknown_aware_data_asset_audit_report.md",
                AUDIT06F_ROOT / "audit_summary.json",
                AUDIT06F_ROOT / "exports" / "high_priority_unknowns.csv",
                AUDIT06F_ROOT / "exports" / "recommendation_matrix.csv",
            ]
        ),
        "07": _existing(
            [
                REPORTS_ROOT / "overlap_validation_report.md",
                REPORTS_ROOT / "overlap_validation_07p_report.md",
                REPORTS_ROOT / "07p_boundary_magnitude_review.md",
                REPORTS_ROOT / "07v2_overlap_consistency_validation_report.md",
                REPORTS_ROOT / "stage1_single_time_acceptance_decision.md",
                OVERLAP07P_ROOT / "source_boundary_transition_matrix.csv",
            ]
        ),
    }


def build_data_check_report_inventory() -> str:
    rows = [
        ["00", "数据下载与完整性审计", str(DATA_CHECK_ROOT / "data_download_audit_report.md")],
        ["00b", "一样本一产品读取与变量映射", str(DATA_CHECK_ROOT / "one_sample_each_product_read_report.md")],
        ["00c", "几何与变量能力审计", str(DATA_CHECK_GEOMETRY_ROOT / "geometry_variable_audit_report.md")],
        ["00d", "Meteosat 产品体系与 catalogue discovery", str(DATA_CHECK_METEOSAT_SERIES_ROOT / "meteosat_product_series_audit.md")],
        ["00d", "Meteosat catalogue discovery", str(DATA_CHECK_METEOSAT_DISCOVERY_ROOT / "meteosat_catalogue_discovery_report.md")],
        ["00e", "单时次 v0 标准化原型", str(DATA_CHECK_ROOT / "standardized_cloud_v0_report.md")],
        ["00e", "FY4B/GOES 最新结构探查", str(DATA_CHECK_LATEST_PROBE_ROOT / "latest_fy4b_goes_structure_report.md")],
        ["00f", "GOES/Meteosat 优先补下载运行", str(DATA_CHECK_GOES_METEOSAT_RUN_ROOT / "priority_download_goes_meteosat_report.md")],
        ["00f", "Meteosat 优先补下载运行", str(DATA_CHECK_METEOSAT_PRIORITY_ROOT / "meteosat_priority_download_report.md")],
    ]
    return "# data_check_report 索引\n\n" + md_table(["阶段", "主题", "主文件"], rows)


def build_data_check_report_lineage() -> str:
    return """# data_check_report 到 Stage1 主链的谱系说明

## 定位

`@DATA_CHECK_ROOT@` 记录的是进入 `01–07` 主链之前的大量前序工作，包括：

- 数据下载与完整性审计
- 一样本一产品真实读取
- 变量映射与 GRIB 深度确认
- 几何与变量能力探查
- Meteosat 产品体系与 catalogue discovery
- v0 native-grid 标准化原型
- 优先补下载运行证据

## 与 01–07 的关系

- `00` 系列负责说明“为什么可以开始做 Stage1 主链”
- `01–07` 负责说明“基于这些前提，单时次闭环实际上做到了什么”
- `00` 系列补全 provenance，但不改写 `01–07` 的 acceptance 结论

## 冲突处理

如果 `data_check_report` 的较早诊断与后续 `01–07` 的最终结论冲突，应采用以下顺序：

1. 当前最终 `01–07` 阶段报告
2. 当前最终 CSV / JSON 统计
3. 当前实际输出目录文件集
4. `data_check_report` 中较早的诊断性结论

## 典型例子

- `00c` 中对几何能力的早期判断，后来被 `06c/06d/06e` 进一步补强
- `00e` 中的 v0 native-grid 原型，不等于后续 `02/03/05` 的正式主链产物
- `00f` 中的下载运行证据说明了后续数据补齐过程，但它不是主链 acceptance 本体
"""


def build_readme(ts: str, snapshot_id: str) -> str:
    return f"""# GEO-ring Cloud Stage1 证据包

生成时间（UTC）：`{ts}`

本证据包是针对当前 Stage1 实际状态重建的审计索引包。它**不会**复制大体积 `.npz`、`.png` 或下载数据本体，而是明确当前 authoritative evidence 的位置，并给出与当前工程状态一致的摘要。

## 当前顶层结论

- 前序审计阶段：`00–00f 已正式纳入`
- 单时次闭环：`PASS_WITH_WARNINGS`
- 正式单时次 overlap 报告：`07v2 已完成`
- 多时次扩展：`HOLD`
- 当前流程**不是** production-closed，也**没有**放行 batch 扩展。

## 覆盖范围

本证据包现在覆盖两层证据链：

- `00–00f`：来源于 `@DATA_CHECK_ROOT@` 的前序审计、产品识别、几何能力与补下载运行证据
- `01–07`：Stage1 单时次标准化、重投影、融合与 overlap 主链

## 编码说明

- 本次重建生成的 `.md`、`.json`、`.csv`、`.yaml` 统一写为 `UTF-8 with BOM`
- 文本统一采用中文口径，保留必要的文件名、变量名和少量英文术语

## 目录说明

{md_table(
    ["路径", "用途"],
    [
        ["stage_registry.md", "当前阶段状态、gate 结果和决策状态总表，包含 00–00f 与 01–07 两层链条。"],
        ["pipeline_stages/", "按阶段或子阶段组织的摘要页。"],
        ["cross_cutting/data_check_report_inventory.md", "data_check_report 体系的主题索引。"],
        ["cross_cutting/data_check_report_lineage.md", "data_check_report 与 01–07 主链之间的谱系关系与 authority 规则。"],
        ["cross_cutting/evidence_sources_by_stage.md", "按阶段组织的 authoritative source 路径。"],
        ["cross_cutting/report_inventory.md", "作为证据源使用的报告/统计文件索引。"],
        ["cross_cutting/quicklook_inventory.md", "quicklook 目录与数量统计。"],
        ["cross_cutting/product_inventory.md", "standardized / reprojected / fused 输出的产品级索引。"],
        ["cross_cutting/dataset_summary.md", "关键证据目录的文件数和体积摘要。"],
        ["cross_cutting/known_mismatches_and_current_authority.md", "已知文档/输出不一致与当前 authority 顺序。"],
    ],
)}

## Snapshot 策略

- `latest/` 会被刷新到当前状态
- 同时会新建一个不可变 snapshot：`snapshots/{snapshot_id}/`
- 旧 snapshots 保留，作为历史记录，不做覆盖
"""


def build_stage_registry() -> str:
    rows = [
        ["00", "Complete", "historical evidence", "数据下载与完整性审计，形成后续工作范围与缺口判断。"],
        ["00b", "Complete", "historical evidence", "一样本一产品真实读取与变量映射，确认关键变量真实可读。"],
        ["00c", "Complete", "historical evidence", "早期几何与变量能力审计，给出可推导/需补下载判断。"],
        ["00d", "Complete", "historical evidence", "Meteosat 产品体系与 catalogue discovery，澄清 collection 与可用性。"],
        ["00e", "Complete", "historical evidence", "单时次 v0 native-grid 标准化原型，验证跨星样本可读。"],
        ["00f", "Complete", "operational evidence", "优先补下载运行证据，支撑后续主链数据补齐。"],
        ["01", "Complete", "PASS", "主原型时次固定为 2024-03-05T00:00:00Z。"],
        ["02", "Complete", "PASS", "6 个卫星组的 native standardized NPZ 已生成。"],
        ["03", "Complete", "PASS", "结构性校验已通过。"],
        ["03.5", "Complete", "PASS_WITH_WARNINGS", "语义问题已记录，但数组可正常读取。"],
        ["04", "Complete", "PASS_WITH_WARNINGS", "FY4B GEO/L2 同格验证通过，但保留质量相关 warning。"],
        ["04b", "Complete", "PASS_WITH_WARNINGS", "FY4B DQF 位级诊断已生成，但未作为 rating weight。"],
        ["05", "Complete", "PASS_WITH_WARNINGS", "重投影产物已存在，但早期 inventory/report 不完整。"],
        ["06", "Complete", "PASS_WITH_WARNINGS", "主原型时次的 best-source 融合已完成。"],
        ["06.5", "Complete", "PASS", "source-selection 诊断确认其总体受 min-VZA 逻辑驱动。"],
        ["06c", "Complete", "PASS_WITH_WARNINGS", "增强版多星几何元数据审计已完成。"],
        ["06d", "Complete", "PASS_WITH_WARNINGS", "Himawari 全圆盘几何验证已完成。"],
        ["06e", "Complete", "PASS", "几何与角度源同步已完成，5/5 gate 通过。"],
        ["06f", "Complete", "PASS_WITH_WARNINGS", "unknown-aware 数据资产审计允许带 warning 进入 07。"],
        ["07 original", "Complete", "Historical only", "原始 overlap validator 跑通过，但存在实现问题，只保留历史意义。"],
        ["07p", "Complete", "PASS", "hotfix validator 已完成，5/5 gate 通过。"],
        ["07p-b", "Complete", "PASS", "边界跳变幅度 review 已完成，并放行正式 07v2。"],
        ["07v2", "Complete", "PASS_WITH_WARNINGS", "正式单时次 overlap 报告已完成。"],
        ["Stage 1 acceptance", "Current", "CLOSED_LOOP_PASS_WITH_WARNINGS", "单时次链条已接受，但多时次扩展仍保持 HOLD。"],
    ]
    return "# 阶段注册表\n\n" + md_table(["阶段", "状态", "Gate", "当前解释"], rows) + "\n\n## 当前决策状态\n\n- 前序审计阶段：`00–00f 已纳入`\n- 单时次正式验证：`completed`\n- 单时次 acceptance：`PASS_WITH_WARNINGS`\n- 多时次扩展：`HOLD`\n- production-grade blending：`尚不可用`\n"


def build_dataset_summary() -> str:
    dirs = [
        ("data_check_report", DATA_CHECK_ROOT),
        ("data_check_report/geometry_variable_audit", DATA_CHECK_GEOMETRY_ROOT),
        ("data_check_report/standardized_cloud_v0_samples", DATA_CHECK_STD_V0_ROOT),
        ("data_check_report/priority_download_run_goes_meteosat", DATA_CHECK_GOES_METEOSAT_RUN_ROOT),
        ("data_check_report/meteosat_priority_cloud_download", DATA_CHECK_METEOSAT_PRIORITY_ROOT),
        ("time_index", TIME_INDEX_ROOT),
        ("standardized_native", STANDARDIZED_ROOT),
        ("reprojected_grid", REPROJECTED_ROOT),
        ("fused_best_source", FUSED_ROOT),
        ("source_selection_diagnostics", SOURCE_DIAG_ROOT),
        ("geometry_angle_sync_06e", GEOM06E_ROOT),
        ("data_asset_audit_06f", AUDIT06F_ROOT),
        ("overlap_validation", OVERLAP07_ROOT),
        ("overlap_validation_07p", OVERLAP07P_ROOT),
        ("geo_geometry_check", GEOMETRY_ROOT),
    ]
    rows = []
    for label, root in dirs:
        count, size = file_count_and_size(root)
        rows.append([label, str(count), human_size(size), str(root)])
    return "# 数据集摘要\n\n" + md_table(["目录", "文件数", "总体积", "路径"], rows)


def build_known_mismatches() -> str:
    return """# 已知不一致与当前 Authority

## Authority 顺序

当不同 artifact 彼此冲突时，采用以下 authority 顺序：

1. 当前最终 `01–07` 阶段报告
2. 当前最终 CSV / JSON 统计文件
3. 当前实际输出目录文件集
4. `data_check_report` 中较早的诊断性结论

## data_check_report 与主链的时序关系

- `data_check_report` 记录的是进入 `01–07` 主链之前的大量前序工作
- 其中有些判断属于早期能力评估，后续已被 `06c/06d/06e` 等主链阶段补强或修正
- 因此 `00` 系列用于补全 provenance，不用于改写当前单时次 acceptance

## 05 重投影阶段

- 早期 05 摘要文档和 `reprojected_variable_inventory.csv` 不能完整代表实际重投影产物集合
- 当前 authority 以 `reprojected_grid/` 实际文件集和后续被 Stage 06 成功消费这一事实为准

## 06c 几何审计

- 早期 `00c` 中存在几何能力的初步判断
- 当前几何 authority 已由 `06c/06d/06e` 和 `geo_geometry_check` 进一步补强

## 06f 数据资产审计

- 06f 经历过额外同步和 patch 脚本修订
- 当前 authority 以最终报告和最终 summary JSON 为准

## 较旧 snapshots 中的 07 状态

- 较旧 evidence-pack snapshot 早于当前 07v2 正式单时次报告
- 当前 authority 以 `07v2_overlap_consistency_validation_report.md` 和 `stage1_single_time_acceptance_decision.md` 为准
"""


def build_evidence_sources_by_stage() -> str:
    lines = ["# 分阶段证据源索引", ""]
    for stage, paths in evidence_sources_by_stage().items():
        lines.append(f"## {stage}")
        for path in paths:
            lines.append(f"- `{path}`")
        lines.append("")
    return "\n".join(lines)


def build_stage00() -> str:
    return f"""# Stage 00 - 数据下载与完整性审计

- 状态：`COMPLETE`
- 证据类型：`historical evidence`
- 当前 authority：
  - `{DATA_CHECK_ROOT / "data_download_audit_report.md"}`
  - `{DATA_CHECK_ROOT / "goes_cross_product_match.csv"}`
  - `{DATA_CHECK_ROOT / "himawari_cross_product_match.csv"}`
  - `{DATA_CHECK_ROOT / "meteosat_cross_product_match.csv"}`
  - `{DATA_CHECK_ROOT / "fy4b_cross_product_match.csv"}`

## 关键结论

- 这是进入 Stage1 主链前的全量下载与完整性盘点。
- 它回答了“当时哪些产品已齐、哪些缺失、哪些需要补下载”。
- 该阶段不覆盖后续 `01–07` 的正式 acceptance，只提供最初的数据基线与缺口判断。

## 对后续阶段的输入

- 为后续变量映射、几何能力判断和优先补下载提供范围基础。
- 为 `00f` 运行证据和 `01–07` 主链提供最早的数据完整性背景。
"""


def build_stage00b() -> str:
    return f"""# Stage 00b - 一样本一产品真实读取与变量映射

- 状态：`COMPLETE`
- 证据类型：`historical evidence`
- 当前 authority：
  - `{DATA_CHECK_ROOT / "one_sample_each_product_read_report.md"}`
  - `{DATA_CHECK_ROOT / "one_sample_each_product_selection.csv"}`
  - `{DATA_CHECK_ROOT / "one_sample_each_product_variables.csv"}`
  - `{DATA_CHECK_ROOT / "one_sample_each_product_variable_statistics.csv"}`
  - `{DATA_CHECK_ROOT / "manual_variable_mapping_by_product.yaml"}`
  - `{DATA_CHECK_ROOT / "meteosat_grib_deep_check.csv"}`

## 关键结论

- 该阶段确认了 NetCDF/HDF/ZIP-GRIB 样本可以被真实读取，而不是只看文件名。
- Meteosat 的 ZIP 内 GRIB 已做深读，确认了 cloud mask / cloud top height 等数组存在性。
- `manual_variable_mapping_by_product.yaml` 成为后续标准化变量映射的重要基础。

## 对后续阶段的输入

- 为 `00e` 的 v0 标准化原型提供样本选择和变量映射依据。
- 为 `02/03` 的正式 standardized native 主链提供早期可读性与命名约束。
"""


def build_stage00c() -> str:
    return f"""# Stage 00c - 几何与变量能力审计

- 状态：`COMPLETE`
- 证据类型：`historical evidence`
- 当前 authority：
  - `{DATA_CHECK_GEOMETRY_ROOT / "geometry_variable_audit_report.md"}`
  - `{DATA_CHECK_GEOMETRY_ROOT / "product_variable_inventory_full.csv"}`
  - `{DATA_CHECK_GEOMETRY_ROOT / "geometry_capability_matrix.csv"}`
  - `{DATA_CHECK_GEOMETRY_ROOT / "quality_flag_capability_matrix.csv"}`
  - `{DATA_CHECK_GEOMETRY_ROOT / "cloud_variable_capability_matrix.csv"}`
  - `{DATA_CHECK_GEOMETRY_ROOT / "recommended_minimal_download_list.md"}`

## 关键结论

- 该阶段给出了各星/各产品的变量存在性、几何直接可得/可推导/需补下载判断。
- 它是早期几何能力判定，不等于后续最终几何 authority。
- 后续 `06c/06d/06e` 已对其中的关键几何结论做进一步补强，尤其是 Himawari 和多星 VZA 一致性。

## 对后续阶段的输入

- 为是否能开始 v0 原型、需要补哪些产品、各星几何风险在哪里提供依据。
- 为 `05/06` 的重投影与融合设计提供早期几何约束。
"""


def build_stage00d() -> str:
    return f"""# Stage 00d - Meteosat 产品体系与 catalogue discovery

- 状态：`COMPLETE`
- 证据类型：`historical evidence`
- 当前 authority：
  - `{DATA_CHECK_METEOSAT_SERIES_ROOT / "meteosat_product_series_audit.md"}`
  - `{DATA_CHECK_METEOSAT_DISCOVERY_ROOT / "meteosat_catalogue_discovery_report.md"}`
  - `{DATA_CHECK_METEOSAT_DISCOVERY_ROOT / "candidate_collections_discovered.csv"}`
  - `{DATA_CHECK_METEOSAT_DISCOVERY_ROOT / "candidate_collection_availability_202403.csv"}`
  - `{DATA_CHECK_METEOSAT_DISCOVERY_ROOT / "candidate_sample_variable_inventory.csv"}`

## 关键结论

- 该阶段澄清了当前本地 CLM/CTH/CTTH 目录与实际 EUMETSAT collection 的关系。
- 对 2024-03 的 collection 可用性做了 catalogue discovery，而不是只靠硬编码猜测。
- 结论用于界定：哪些 Meteosat collection 是当前可下载/可用的，哪些只是候选、CDR、regional 或 software-only 线索。

## 对后续阶段的输入

- 为 Meteosat 数据解释、补下载优先级和后续主链中 Meteosat 的变量边界提供依据。
- 不直接覆盖 `06c/06d/06e` 的几何 authority，也不代表 `07` 的科学验证结论。
"""


def build_stage00e() -> str:
    return f"""# Stage 00e - 单时次 v0 标准化原型

- 状态：`COMPLETE`
- 证据类型：`historical evidence`
- 当前 authority：
  - `{DATA_CHECK_ROOT / "standardized_cloud_v0_report.md"}`
  - `{DATA_CHECK_LATEST_PROBE_ROOT / "latest_fy4b_goes_structure_report.md"}`
  - `{DATA_CHECK_STD_V0_ROOT}`

## 关键结论

- 该阶段是在各产品 native grid 上做的早期 v0 原型，不做全月拼接，也不等于后续正式主链标准化。
- 它证明了 FY4B、GOES、Himawari、Meteosat 的代表性样本能够被统一成可分析的单时次原型对象。
- 它尤其回答了：哪些卫星已经具备 `cloud_mask + CTH` 的 v0 起步条件，哪些变量仍缺失。

## 对后续阶段的输入

- 为 `02/03` 的正式 standardized native 主链提供变量语义和样本结构先验。
- 不应把这个阶段的原型输出与后续 `02/03/05` 的正式主链产物混为一谈。
"""


def build_stage00f() -> str:
    return f"""# Stage 00f - 优先补下载运行证据

- 状态：`COMPLETE`
- 证据类型：`operational run evidence`
- 当前 authority：
  - `{DATA_CHECK_GOES_METEOSAT_RUN_ROOT / "priority_download_goes_meteosat_report.md"}`
  - `{DATA_CHECK_GOES_METEOSAT_RUN_ROOT / "priority_download_manifest_all.csv"}`
  - `{DATA_CHECK_GOES_METEOSAT_RUN_ROOT / "priority_download_status.csv"}`
  - `{DATA_CHECK_GOES_METEOSAT_RUN_ROOT / "priority_download_verification.csv"}`
  - `{DATA_CHECK_METEOSAT_PRIORITY_ROOT / "meteosat_priority_download_report.md"}`
  - `{DATA_CHECK_METEOSAT_PRIORITY_ROOT / "meteosat_priority_download_status.csv"}`

## 关键结论

- 这部分记录的是补下载运行、manifest、verification 和 smoke test 证据。
- 它说明后续数据补齐是如何执行和验证的。
- 它不是 `01–07` 主链 acceptance 本体，只是 operational provenance。

## 对后续阶段的输入

- 为后续主链为何能拿到补齐后的 GOES/Meteosat 数据提供运行层证据。
- 不改写 `01–07` 的科学验证或单时次闭环结论。
"""





def build_stage07() -> str:
    return """# Stage 07 - Overlap 验证

- 状态：`FORMAL_SINGLE_TIME_COMPLETE_WITH_WARNINGS`
- 当前 authority：
  - `@STAGE_ROOT@\\reports\\07v2_overlap_consistency_validation_report.md`
  - `@STAGE_ROOT@\\reports\\stage1_single_time_acceptance_decision.md`
  - `@STAGE_ROOT@\\reports\\overlap_validation_07p_report.md`
  - `@STAGE_ROOT@\\reports\\07p_boundary_magnitude_review.md`

## 子阶段

1. `07 original`
   - 已执行。
   - 仅保留历史意义。
   - 由于后续识别到 validator implementation issue，它不再代表当前最终口径。

2. `07p hotfix`
   - 已执行。
   - 修复了 cloud-mask mapping、angle-layer usage、rating diagnostic reconstruction、stratification execution 和 report logic。

3. `07p-b`
   - 已执行。
   - 完成了 source-boundary jump magnitude review。
   - 放行了正式 07v2 报告生成。

4. `07v2`
   - 已执行。
   - 正式单时次报告已完成，结论为 `PASS_WITH_WARNINGS`。

## 当前 acceptance

- Single-time chain status: `CLOSED_LOOP_PASS_WITH_WARNINGS`
- Validator status: `PASS`
- Scientific validation status: `PASS_WITH_WARNINGS`
- Multi-time expansion: `HOLD`

## 主要 warning

1. Meteosat 相关 CTH 一致性偏弱。
2. FY4B-Himawari 的 CTT/CTP 不连续。
3. COT/CER 高不确定性。
4. 连续变量 source-boundary 跳变仍然存在。
5. 当前还没有 production-grade blending。
"""






def build_evidence_manifest(ts: str, snapshot_id: str) -> dict:
    source_roots = [STAGE1_ROOT, GEOMETRY_ROOT, CODE_ROOT, DATA_CHECK_ROOT]
    ext_counts = defaultdict(int)
    for root in source_roots:
        for path in list_files(root):
            suffix = normalized_suffix(path)
            if suffix is not None:
                ext_counts[suffix] += 1
    return {
        "evidence_pack": {
            "name": "GEO-ring Cloud Stage1 Evidence Pack",
            "version": "3.0",
            "generated_utc": ts,
            "target_datetime": "2024-03-05T00:00:00Z",
            "single_time_status": "PASS_WITH_WARNINGS",
            "multi_time_status": "HOLD",
            "note": "This pack now covers both pre-stage data_check_report evidence (00-00f) and the Stage1 single-time chain (01-07).",
            "snapshot_id": snapshot_id,
        },
        "stages_covered": [
            "00_data_download_audit",
            "00b_one_sample_product_probe",
            "00c_geometry_variable_audit",
            "00d_meteosat_catalogue_and_series_audit",
            "00e_standardized_cloud_v0_prototype",
            "00f_priority_download_runs",
            "01_core_time_index",
            "02_standardized_native",
            "03_validate_native",
            "03_5_semantic_validation",
            "04_fy4b_geo_alignment",
            "04b_fy4b_dqf_bit_decode",
            "05_reproject_to_grid",
            "06_fuse_best_source",
            "06_5_source_selection_diagnostics",
            "06c_geometry_audit",
            "06d_himawari_full_disk_geometry_validation",
            "06e_geometry_angle_sync",
            "06f_unknown_aware_data_asset_audit",
            "07_original_overlap_validation",
            "07p_hotfix_overlap_validation",
            "07p_b_boundary_review",
            "07v2_formal_single_time_overlap_validation",
            "stage1_single_time_acceptance_decision",
        ],
        "input_directories": [str(STAGE1_ROOT), str(GEOMETRY_ROOT), str(CODE_ROOT), str(DATA_CHECK_ROOT)],
        "output_directories": {
            "latest": str(LATEST_ROOT),
            "new_snapshot": str(SNAPSHOTS_ROOT / snapshot_id),
        },
        "source_extension_counts": dict(sorted(ext_counts.items())),
    }


def build_pipeline_and_crosscutting(root: Path, ts: str, snapshot_id: str) -> None:
    write_text(root / "README.md", build_readme(ts, snapshot_id))
    write_text(root / "stage_registry.md", build_stage_registry())
    write_json(root / "evidence_manifest.json", build_evidence_manifest(ts, snapshot_id))
    write_text(root / "06e_status.md", build_06e_status())
    write_text(root / "07_status.md", build_07_status())

    write_text(root / "config" / "core_product_config.md", build_core_product_config())

    write_text(root / "cross_cutting" / "dataset_summary.md", build_dataset_summary())
    write_text(root / "cross_cutting" / "product_inventory.md", build_product_inventory())
    write_text(root / "cross_cutting" / "quicklook_inventory.md", build_quicklook_inventory())
    write_text(root / "cross_cutting" / "report_inventory.md", build_report_inventory())
    write_text(root / "cross_cutting" / "script_manifest.md", build_script_manifest())
    write_text(root / "cross_cutting" / "known_mismatches_and_current_authority.md", build_known_mismatches())
    write_text(root / "cross_cutting" / "evidence_sources_by_stage.md", build_evidence_sources_by_stage())
    write_text(root / "cross_cutting" / "data_check_report_inventory.md", build_data_check_report_inventory())
    write_text(root / "cross_cutting" / "data_check_report_lineage.md", build_data_check_report_lineage())
    write_text(root / "cross_cutting" / "08_next_steps.md", build_next_steps())

    stage_dir = root / "pipeline_stages"
    write_text(stage_dir / "00_data_download_audit.md", build_stage00())
    write_text(stage_dir / "00b_one_sample_product_probe.md", build_stage00b())
    write_text(stage_dir / "00c_geometry_variable_audit.md", build_stage00c())
    write_text(stage_dir / "00d_meteosat_catalogue_and_series_audit.md", build_stage00d())
    write_text(stage_dir / "00e_standardized_cloud_v0_prototype.md", build_stage00e())
    write_text(stage_dir / "00f_priority_download_runs.md", build_stage00f())
    write_text(stage_dir / "01_core_time_index.md", build_stage01())
    write_text(stage_dir / "02_standardized_native.md", build_stage02())
    write_text(stage_dir / "03_validate_native.md", build_stage03())
    write_text(stage_dir / "03_5_semantic_validation.md", build_stage03_5())
    write_text(stage_dir / "04_fy4b_geo_alignment.md", build_stage04())
    write_text(stage_dir / "04b_fy4b_dqf_bit_decode.md", build_stage04b())
    write_text(stage_dir / "05_reproject_to_grid.md", build_stage05())
    write_text(stage_dir / "06_fuse_best_source.md", build_stage06())
    write_text(stage_dir / "06_5_source_selection_diagnostics.md", build_stage06_5())
    write_text(stage_dir / "06c_geometry_audit.md", build_stage06c())
    write_text(stage_dir / "06d_himawari_full_disk_geometry_validation.md", build_stage06d())
    write_text(stage_dir / "06e_geometry_angle_sync.md", build_stage06e())
    write_text(stage_dir / "06e_status.md", build_06e_status())
    write_text(stage_dir / "06f_unknown_aware_data_asset_audit.md", build_stage06f())
    write_text(stage_dir / "07_overlap_validation.md", build_stage07())
    write_text(stage_dir / "07_status.md", build_07_status())


def main() -> int:
    now = utc_now()
    ts = utc_stamp(now)
    snap_id = snapshot_stamp(now)

    LATEST_ROOT.mkdir(parents=True, exist_ok=True)
    SNAPSHOTS_ROOT.mkdir(parents=True, exist_ok=True)

    remove_tree_contents(LATEST_ROOT)
    build_pipeline_and_crosscutting(LATEST_ROOT, ts, snap_id)

    snapshot_root = SNAPSHOTS_ROOT / snap_id
    copy_latest_to_snapshot(snapshot_root)

    print(f"rebuilt latest={LATEST_ROOT}")
    print(f"snapshot={snapshot_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
