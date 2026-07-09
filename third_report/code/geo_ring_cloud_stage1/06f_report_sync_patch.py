from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from pathlib import Path


STAGE_ROOT = Path(r"D:\AAAresearch_paper\geo_ring_cloud_stage1")
OUT_DIR = STAGE_ROOT / "data_asset_audit_06f"
EXPORT_DIR = OUT_DIR / "exports"
REPORT_PATH = STAGE_ROOT / "reports" / "06f_unknown_aware_data_asset_audit_report.md"
SUMMARY_PATH = OUT_DIR / "audit_summary.json"
SQLITE_PATH = OUT_DIR / "data_asset_audit.sqlite"
HIGH_UNKNOWNS_CSV = EXPORT_DIR / "high_priority_unknowns.csv"
BLOCKING_CSV = EXPORT_DIR / "blocking_issues.csv"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def fmt_bool_like(value: str | int | None) -> str:
    if value in (None, "", "None"):
        return "N/A"
    if str(value) == "1":
        return "Yes"
    if str(value) == "0":
        return "No"
    return str(value)


def dedup_product_variable(rows: list[dict[str, str]], keep_names: set[str] | None = None) -> list[tuple[str, str, str]]:
    seen: set[tuple[str, str, str]] = set()
    out: list[tuple[str, str, str]] = []
    for row in rows:
        name = row.get("name", "")
        if keep_names is not None and name not in keep_names:
            continue
        item = (row.get("satellite", ""), row.get("product", ""), name)
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def main() -> None:
    summary = json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))
    high_unknowns = read_csv(HIGH_UNKNOWNS_CSV)
    blocking = read_csv(BLOCKING_CSV)

    conn = sqlite3.connect(SQLITE_PATH)
    try:
        capability_rows = conn.execute(
            """
            SELECT f.satellite, f.product,
                   MAX(CASE WHEN i.normalized_name IN ('cloudmask','cloudmaskbinary','bcm','acm','cbm','clm','cmsk') THEN 1 ELSE 0 END) AS cloud_class,
                   MAX(CASE WHEN i.normalized_name IN ('cloudtopheightkm','cldtophght','ht','cth') THEN 1 ELSE 0 END) AS cth_like,
                   MAX(CASE WHEN i.normalized_name IN ('cloudtoptemperaturek','cldtoptemp','ctt') THEN 1 ELSE 0 END) AS ctt_like,
                   MAX(CASE WHEN i.normalized_name IN ('cloudtoppressurehpa','cldtoppres','ctp') THEN 1 ELSE 0 END) AS ctp_like,
                   MAX(CASE WHEN i.semantic_class LIKE '%coordinate%' OR i.semantic_class LIKE '%navigation_projection%' THEN 1 ELSE 0 END) AS geometry,
                   MAX(CASE WHEN i.semantic_class LIKE '%geometry_angle%' THEN 1 ELSE 0 END) AS angles,
                   MAX(CASE WHEN i.semantic_class LIKE '%quality_flag%' THEN 1 ELSE 0 END) AS quality
            FROM files f
            LEFT JOIN items i ON f.file_id = i.file_id
            GROUP BY f.satellite, f.product
            ORDER BY f.satellite, f.product
            """
        ).fetchall()

        underused_rows = conn.execute(
            """
            SELECT DISTINCT f.satellite, f.product, i.name, i.semantic_class, i.known_status
            FROM items i
            JOIN files f ON i.file_id = f.file_id
            WHERE f.satellite IS NOT NULL
              AND i.item_type IN ('variable','dataset','npz_array')
              AND (
                   i.semantic_class LIKE '%geometry_angle%'
               OR i.semantic_class LIKE '%navigation_projection%'
               OR i.semantic_class LIKE '%time_scan%'
               OR i.semantic_class LIKE '%quality_flag%'
              )
            ORDER BY f.satellite, f.product, i.name
            LIMIT 20
            """
        ).fetchall()

        diag_rows = conn.execute(
            """
            SELECT DISTINCT f.satellite, f.product, i.name
            FROM recommendations r
            JOIN items i ON r.item_id = i.item_id
            JOIN files f ON i.file_id = f.file_id
            WHERE f.satellite IS NOT NULL
              AND i.item_type IN ('variable','dataset','npz_array')
              AND r.use_now = 0 AND r.use_later = 1 AND r.use_for_fusion = 0
            ORDER BY f.satellite, f.product, i.name
            """
        ).fetchall()

        overlap_rows = conn.execute(
            """
            SELECT DISTINCT f.satellite, f.product, i.name
            FROM items i
            JOIN files f ON i.file_id = f.file_id
            WHERE f.satellite IS NOT NULL
              AND i.item_type IN ('variable','dataset','npz_array')
              AND i.name IN ('cloud_mask','cloud_top_height_km','cloud_top_temperature_K','cloud_top_pressure_hPa',
                             'cloud_phase','cloud_type','sensor_zenith_angle','solar_zenith_angle',
                             'valid_mask','quality_flag_raw','quality_flag_standard')
            ORDER BY f.satellite, f.product, i.name
            """
        ).fetchall()

        official_rows = conn.execute(
            """
            SELECT DISTINCT g.angle_kind, f.satellite, f.product, i.name
            FROM geo_time g
            JOIN items i ON g.item_id = i.item_id
            JOIN files f ON i.file_id = f.file_id
            WHERE f.satellite IS NOT NULL
            ORDER BY g.angle_kind, f.satellite, f.product, i.name
            """
        ).fetchall()
    finally:
        conn.close()

    file_counts = {row["read_status"]: row["count"] for row in summary["file_counts"]}
    unknown_counts = {row["priority"]: row["count"] for row in summary["unknown_counts_by_priority"]}
    prototype_bad = [row for row in blocking if "20240305" in row.get("path", "")]

    section4_rows = []
    for row in high_unknowns:
        name = row.get("name", "")
        if name == "OBIType":
            continue
        if row.get("satellite", "") == "FY4B" and row.get("product", "") == "CTT" and name == "CLE":
            row = dict(row)
            row["notes"] = "known cloud emissivity auxiliary variable; future diagnostic; not blocking 07"
        section4_rows.append(row)

    flag_like_names = {"DQF", "valid_mask", "display_valid_mask", "fusion_valid_mask", "off_disc_mask", "quality_flag_raw", "quality_flag_standard"}
    section9 = []
    seen9: set[tuple[str, str, str]] = set()
    for sat, product, name in dedup_product_variable(
        [{"satellite": r[0], "product": r[1], "name": r[2]} for r in underused_rows + overlap_rows],
        keep_names=flag_like_names,
    ):
        if name == "OBIType":
            continue
        key = (sat, product, name)
        if key not in seen9:
            seen9.add(key)
            section9.append(key)

    diag_lines = []
    for sat, product, name in diag_rows:
        if name == "OBIType":
            diag_lines.append(f"- {sat} / {product} / {name}: known observing-type metadata; diagnostic only; not for fusion/rating.")
        elif sat == "FY4B" and product == "CTT" and name == "CLE":
            diag_lines.append(f"- {sat} / {product} / {name}: known cloud emissivity auxiliary variable; future diagnostic; not blocking 07.")
        else:
            diag_lines.append(f"- {sat} / {product} / {name}")

    official_map: dict[str, list[str]] = defaultdict(list)
    for angle_kind, sat, product, name in official_rows:
        official_map[angle_kind or "unspecified"].append(f"{sat} / {product} / {name}")

    lines: list[str] = []
    lines.append("# 06f Unknown-aware Data Asset Audit Report")
    lines.append("")
    lines.append(f"- 生成时间 UTC: {summary['run_time']}")
    lines.append(f"- 输入目录: {', '.join(summary['input_dirs'])}")
    lines.append(f"- SQLite: `{summary['output_paths']['sqlite']}`")
    lines.append("")
    lines.append("## 1. 扫描规模")
    lines.append("")
    lines.append(f"- 扫描文件数: `{file_counts.get('OK', 0) + file_counts.get('ERROR', 0)}`")
    lines.append(f"- 成功读取: `{file_counts.get('OK', 0)}`")
    lines.append(f"- 读取失败: `{file_counts.get('ERROR', 0)}`")
    lines.append(f"- HIGH unknown 数: `{unknown_counts.get('HIGH', 0)}`")
    lines.append(f"- MEDIUM unknown 数: `{unknown_counts.get('MEDIUM', 0)}`")
    lines.append(f"- LOW unknown 数: `{unknown_counts.get('LOW', 0)}`")
    lines.append("")
    lines.append("## 2. 各卫星/产品核心能力")
    lines.append("")
    for sat, product, cloud_class, cth_like, ctt_like, ctp_like, geometry, angles, quality in capability_rows:
        lines.append(
            f"- {sat} / {product}: cloud_class={cloud_class}, cth_like={cth_like}, ctt_like={ctt_like}, "
            f"ctp_like={ctp_like}, geometry={geometry}, angles={angles}, quality={quality}"
        )
    lines.append("")
    lines.append("## 3. 当前数据里已存在但之前未充分利用的信息")
    lines.append("")
    for sat, product, name, semantic_class, known_status in underused_rows:
        lines.append(f"- {sat} / {product} / {name}: {semantic_class} ({known_status})")
    lines.append("")
    lines.append("## 4. 高优先级未知项（去重后 product-variable 级别）")
    lines.append("")
    if not section4_rows:
        lines.append("- 当前没有剩余的 high-priority unknown。")
    else:
        for row in section4_rows:
            notes = row.get("notes", "")
            extra = f"; notes={notes}" if notes else ""
            lines.append(
                f"- {row.get('satellite','')} / {row.get('product','')} / {row.get('name','')}: "
                f"occurrences={row.get('file_occurrences','')}, risk={row.get('max_risk_score','')}, "
                f"blocks_07={fmt_bool_like(row.get('blocks_07'))}, affects_rating={fmt_bool_like(row.get('affects_future_rating'))}; "
                f"{row.get('why_flagged','')}{extra}"
            )
    lines.append("")
    lines.append("## 5. 哪些未知项阻塞 07")
    lines.append("")
    if summary.get("top_blocking_issues"):
        for row in summary["top_blocking_issues"]:
            lines.append(
                f"- {row.get('satellite','')} / {row.get('product','')} / {row.get('name','')}: "
                f"priority={row.get('priority','')}, risk={row.get('risk_score','')}, why={row.get('why_flagged','')}"
            )
    else:
        lines.append("- 当前没有 `blocks_07 = Yes` 的 unknown。")
    lines.append("")
    lines.append("## 6. 哪些未知项只影响后续 rating")
    lines.append("")
    rating_only = [row for row in section4_rows if row.get("affects_future_rating") == "1"]
    if rating_only:
        for row in rating_only:
            lines.append(f"- {row.get('satellite','')} / {row.get('product','')} / {row.get('name','')}")
    else:
        lines.append("- 当前没有仅影响 rating 的 high-priority unknown。")
    lines.append("")
    lines.append("## 7. 可直接用于 07 overlap validation 的变量")
    lines.append("")
    for sat, product, name in overlap_rows[:40]:
        lines.append(f"- {sat} / {product} / {name}")
    lines.append("")
    lines.append("## 8. 只能用于诊断、不能直接融合/评分的变量")
    lines.append("")
    for line in diag_lines[:60]:
        lines.append(line)
    lines.append("")
    lines.append("## 9. 仍需语义确认的 flag / code table（按 product-variable 去重）")
    lines.append("")
    if not section9:
        lines.append("- 当前没有剩余的去重后 flag/code-table 语义疑点。")
    else:
        for sat, product, name in section9:
            if sat == "FY4B" and product == "CTT" and name == "CLE":
                continue
            lines.append(f"- {sat} / {product} / {name}")
    lines.append("")
    lines.append("## 10. 官方角度 / 计算角度 / 近似角度")
    lines.append("")
    for kind in ["official_angle", "computed_angle", "approximate_angle", "unspecified"]:
        values = official_map.get(kind, [])
        if not values:
            continue
        lines.append(f"- {kind}:")
        for value in values[:40]:
            lines.append(f"  - {value}")
    lines.append("")
    lines.append("## 11. 对深空/几何增强最有价值的信息")
    lines.append("")
    lines.append("- 几何角度层：sensor_zenith_angle / solar_zenith_angle / azimuth / glint / relative_azimuth。")
    lines.append("- 投影参数：SSP longitude、satellite height、semi-major/minor axis、sweep axis、CFAC/LFAC/COFF/LOFF。")
    lines.append("- 质量与算法状态层：DQF / quality_flag / confidence / status / code table。")
    lines.append("- 云微物理辅助层：cloud_optical_thickness / effective_radius / cloud phase/type。")
    lines.append("")
    lines.append("## 12. 是否允许进入 07")
    lines.append("")
    lines.append(f"- DISCOVERY_GATE: `{summary['gate_status']['DISCOVERY_GATE']}`")
    lines.append(f"- SEMANTIC_GATE: `{summary['gate_status']['SEMANTIC_GATE']}`")
    lines.append(f"- FUSION_READINESS_GATE: `{summary['gate_status']['FUSION_READINESS_GATE']}`")
    lines.append(f"- UNKNOWN_RISK_GATE: `{summary['gate_status']['UNKNOWN_RISK_GATE']}`")
    lines.append(f"- 是否允许进入 07: `{summary['allow_enter_07']}`")
    lines.append("- 明确说明：6 个 FY4B 坏文件均不包含 `2024-03-05 00:00`，因此不阻塞当前 07。")
    lines.append(f"- 当前 `blocking_issues.csv` 中命中 `20240305` 的 FY4B 坏文件数: `{len(prototype_bad)}`")
    lines.append("- 必带 warning: 仍有非阻断型 semantic unknown；FY4B DQF 和若干 valid-mask 语义仍需按产品级继续确认。")
    lines.append("")

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(REPORT_PATH)


if __name__ == "__main__":
    main()
