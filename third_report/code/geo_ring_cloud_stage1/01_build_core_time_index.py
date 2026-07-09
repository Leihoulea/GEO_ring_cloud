from __future__ import annotations

import json
import shutil
from pathlib import Path

import pandas as pd
import yaml

from stage1_common import (
    CONFIG_DIR,
    CORE_PRODUCTS,
    PARSED_METADATA,
    REPORT_DIR,
    SCRIPT_DIR,
    STAGE_ROOT,
    TIME_INDEX_DIR,
    ensure_dirs,
    iso_z,
    parse_time,
    utc_now,
)


START = pd.Timestamp("2024-03-05T00:00:00Z")
END = pd.Timestamp("2024-03-31T23:00:00Z")


def load_metadata() -> pd.DataFrame:
    df = pd.read_csv(PARSED_METADATA)
    df["nominal_dt"] = df["nominal_time"].map(parse_time)
    df["file_size_num"] = pd.to_numeric(df["file_size"], errors="coerce").fillna(0)
    mask = (df["nominal_dt"] >= START) & (df["nominal_dt"] <= END)
    df = df.loc[mask].copy()
    df["nominal_hour"] = df["nominal_dt"].dt.floor("h")
    df = df[df["nominal_dt"] == df["nominal_hour"]]
    return df


def select_file(rows: pd.DataFrame) -> dict[str, object] | None:
    if rows.empty:
        return None
    ok = rows.copy()
    ok["parse_ok"] = ok["parse_status"].astype(str).str.upper().isin(["OK", "PARSED", ""])
    ok = ok.sort_values(["parse_ok", "file_size_num"], ascending=[False, False])
    row = ok.iloc[0]
    return {
        "file_path": row.get("file_path", ""),
        "file_size": int(row.get("file_size_num", 0)),
        "parse_status": row.get("parse_status", ""),
        "suffix": row.get("suffix", ""),
    }


def build_index(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    hours = pd.date_range(START, END, freq="h", tz="UTC")
    rows = []
    summary_rows = []
    for hour in hours:
        hour_iso = iso_z(hour)
        total_core = 0
        total_present = 0
        complete_satellites = 0
        satellite_with_any = 0
        missing_all: list[str] = []
        for group_name, spec in CORE_PRODUCTS.items():
            group_df = df[
                (df["satellite_family"] == spec["family"])
                & (df["satellite"] == spec["satellite"])
                & (df["nominal_hour"] == hour)
            ]
            product_files: dict[str, dict[str, object]] = {}
            missing: list[str] = []
            present_count = 0
            for product in spec["core"]:
                product_df = group_df[group_df["product"] == product]
                selected = select_file(product_df)
                if selected and selected["file_size"] > 0:
                    product_files[product] = selected
                    present_count += 1
                else:
                    missing.append(product)
            total_core += len(spec["core"])
            total_present += present_count
            if present_count > 0:
                satellite_with_any += 1
            if not missing:
                complete_satellites += 1
            missing_all.extend([f"{group_name}:{p}" for p in missing])
            score = present_count * 10 + (100 if not missing else 0)
            rows.append(
                {
                    "nominal_time": hour_iso,
                    "satellite_group": group_name,
                    "satellite_family": spec["family"],
                    "satellite": spec["satellite"],
                    "core_products": "|".join(spec["core"]),
                    "optional_products": "|".join(spec.get("optional", [])),
                    "present_core_count": present_count,
                    "total_core_count": len(spec["core"]),
                    "complete_core": not missing,
                    "missing_core_products": "|".join(missing),
                    "product_files_json": json.dumps(product_files, ensure_ascii=False),
                    "satellite_score": score,
                    "status": "PASS" if not missing else ("PARTIAL" if present_count else "MISSING"),
                }
            )
        completeness_ratio = total_present / total_core if total_core else 0.0
        time_score = total_present * 10 + complete_satellites * 100 + satellite_with_any * 20
        summary_rows.append(
            {
                "rank": 0,
                "nominal_time": hour_iso,
                "total_present_core": total_present,
                "total_core": total_core,
                "core_completeness_ratio": completeness_ratio,
                "complete_satellite_groups": complete_satellites,
                "satellite_groups_with_any_data": satellite_with_any,
                "missing_core_items": "|".join(missing_all),
                "time_score": time_score,
                "selection_role": "",
            }
        )
    ranked = pd.DataFrame(summary_rows).sort_values(
        ["time_score", "complete_satellite_groups", "core_completeness_ratio", "nominal_time"],
        ascending=[False, False, False, True],
    )
    ranked["rank"] = range(1, len(ranked) + 1)
    if not ranked.empty:
        ranked.loc[ranked.index[0], "selection_role"] = "prototype"
    selected = [ranked.index[0]] if not ranked.empty else []
    buckets = [(0, 5), (6, 11), (12, 17), (18, 23)]
    for lo, hi in buckets:
        candidates = ranked[ranked["nominal_time"].str[11:13].astype(int).between(lo, hi)]
        for idx in candidates.index:
            if idx not in selected:
                selected.append(idx)
                break
    for idx in ranked.index:
        if len(selected) >= 7:
            break
        if idx not in selected:
            selected.append(idx)
    for idx in selected[1:7]:
        ranked.loc[idx, "selection_role"] = "representative"
    return pd.DataFrame(rows), ranked


def write_report(index_df: pd.DataFrame, ranked: pd.DataFrame) -> None:
    prototype = ranked[ranked["selection_role"] == "prototype"].head(1)
    lines = [
        "# Core Time Index Report",
        "",
        f"- Generated UTC: {utc_now()}",
        f"- Source metadata: `{PARSED_METADATA}`",
        f"- Time range: `{START.strftime('%Y-%m-%dT%H:%M:%SZ')}` to `{END.strftime('%Y-%m-%dT%H:%M:%SZ')}`",
        "- FY4B 2024-03-01 to 2024-03-04 is treated as `OFFICIAL_UNAVAILABLE` and excluded from ranking.",
        "- FY4B FDI is optional and does not affect cloud-product core PASS/FAIL.",
        "",
        "## Prototype Time",
        "",
    ]
    if prototype.empty:
        lines.append("- No prototype time could be selected.")
    else:
        row = prototype.iloc[0]
        lines.append(
            f"- `{row['nominal_time']}` rank={row['rank']} score={row['time_score']} "
            f"complete_satellite_groups={row['complete_satellite_groups']} "
            f"core_ratio={row['core_completeness_ratio']:.3f}"
        )
    lines.extend(["", "## Representative Times", ""])
    reps = ranked[ranked["selection_role"].isin(["prototype", "representative"])].sort_values("rank")
    for _, row in reps.iterrows():
        lines.append(
            f"- {row['selection_role']}: `{row['nominal_time']}` "
            f"score={row['time_score']} complete_groups={row['complete_satellite_groups']}"
        )
    lines.extend(["", "## Core Definitions", ""])
    for group, spec in CORE_PRODUCTS.items():
        lines.append(f"- {group}: core={', '.join(spec['core'])}; optional={', '.join(spec.get('optional', [])) or 'none'}")
    lines.extend(["", "## Output Files", ""])
    lines.append(f"- `{TIME_INDEX_DIR / 'core_time_index.csv'}`")
    lines.append(f"- `{TIME_INDEX_DIR / 'usable_times_ranked.csv'}`")
    (TIME_INDEX_DIR / "core_time_index_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ensure_dirs()
    shutil.copy2(__file__, SCRIPT_DIR / Path(__file__).name)
    core_yaml = {
        "generated_utc": utc_now(),
        "time_range": {"start": "2024-03-05T00:00:00Z", "end": "2024-03-31T23:00:00Z"},
        "fy4b_official_unavailable": {"start": "2024-03-01T00:00:00Z", "end": "2024-03-04T23:00:00Z"},
        "satellite_groups": CORE_PRODUCTS,
    }
    (CONFIG_DIR / "core_product_definition.yaml").write_text(yaml.safe_dump(core_yaml, sort_keys=False, allow_unicode=True), encoding="utf-8")
    df = load_metadata()
    index_df, ranked = build_index(df)
    index_df.to_csv(TIME_INDEX_DIR / "core_time_index.csv", index=False, encoding="utf-8-sig")
    ranked.to_csv(TIME_INDEX_DIR / "usable_times_ranked.csv", index=False, encoding="utf-8-sig")
    write_report(index_df, ranked)
    (REPORT_DIR / "geo_ring_cloud_next_stage_report.md").write_text(
        "# GEO-ring Cloud Next Stage Report\n\n"
        f"- Generated UTC: {utc_now()}\n"
        "- Stage status: 01 completed; 02 and 03 not run yet.\n",
        encoding="utf-8",
    )
    print(f"01 OK: prototype={ranked.loc[ranked['selection_role'].eq('prototype'), 'nominal_time'].iloc[0]}")
    print(f"report={TIME_INDEX_DIR / 'core_time_index_report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

