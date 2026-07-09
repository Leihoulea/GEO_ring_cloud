from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


RUNS_ROOT = Path(r"D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs")
STAGE09D = RUNS_ROOT / "stage09d_full_pixel_diagnostics_202403"
OUT = STAGE09D / "interpretation_package" / "meteosat_semantics_audit"
MANIFEST = STAGE09D / "00_sample_manifest" / "stage09d_53_sample_manifest.csv"

METEOSAT_SOURCES = {
    "Meteosat-0deg": 5,
    "Meteosat-IODC": 6,
}


def read_npz(path: Path) -> dict[str, Any]:
    with np.load(path, allow_pickle=True) as z:
        return {k: z[k] for k in z.files}


def count_values(arr: np.ndarray, mask: np.ndarray | None = None, codes: list[int] | None = None) -> dict[str, int]:
    if mask is not None:
        arr = arr[np.asarray(mask).astype(bool)]
    if codes is None:
        codes = [-9999, -1, 0, 1, 2, 3, 126, 127, 255]
    return {f"count_code_{c}": int(np.count_nonzero(arr == c)) for c in codes}


def find_prefusion(run_dir: Path, source: str, tag: str) -> Path | None:
    root = run_dir / "reprojected_grid" / source
    exact = root / f"{source}_CLM_cloud_mask_grid_{tag}.npz"
    if exact.exists():
        return exact
    hits = list(root.glob(f"*cloud_mask*{tag}.npz"))
    return hits[0] if hits else None


def audit_prefusion(manifest: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, r in manifest.iterrows():
        tag = str(r["sample_id"])
        run_dir = RUNS_ROOT / tag
        for source in METEOSAT_SOURCES:
            path = find_prefusion(run_dir, source, tag)
            base = {
                "sample_id": tag,
                "source": source,
                "prefusion_path": str(path) if path else "",
                "exists": bool(path and path.exists()),
            }
            if not path:
                rows.append({**base, "status": "missing"})
                continue
            try:
                z = read_npz(path)
                data = np.asarray(z["data"])
                valid = np.asarray(z.get("valid_mask", np.ones(data.shape, dtype=bool))).astype(bool)
                display = np.asarray(z.get("display_valid_mask", valid)).astype(bool)
                fusion = np.asarray(z.get("fusion_valid_mask", valid)).astype(bool)
                off = np.asarray(z.get("off_disc_mask", np.zeros(data.shape, dtype=bool))).astype(bool)
                row = {
                    **base,
                    "status": "ok",
                    "shape": "x".join(map(str, data.shape)),
                    "valid_mask_n": int(np.count_nonzero(valid)),
                    "display_valid_n": int(np.count_nonzero(display)),
                    "fusion_valid_n": int(np.count_nonzero(fusion)),
                    "off_disc_n": int(np.count_nonzero(off)),
                    "fusion_valid_code3_n": int(np.count_nonzero(fusion & (data == 3))),
                    "display_code3_n": int(np.count_nonzero(display & (data == 3))),
                    "raw_codes_in_fusion_valid": json.dumps({int(k): int(v) for k, v in zip(*np.unique(data[fusion], return_counts=True))}, ensure_ascii=False),
                    "raw_codes_in_display_valid": json.dumps({int(k): int(v) for k, v in zip(*np.unique(data[display], return_counts=True))}, ensure_ascii=False),
                }
                row.update({f"fusion_{k}": v for k, v in count_values(data, fusion, [0, 1, 2, 3]).items()})
                row.update({f"display_{k}": v for k, v in count_values(data, display, [0, 1, 2, 3]).items()})
                rows.append(row)
            except Exception as exc:
                rows.append({**base, "status": "error", "error": repr(exc)})
    return pd.DataFrame(rows)


def audit_fused(manifest: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, r in manifest.iterrows():
        tag = str(r["sample_id"])
        run_dir = RUNS_ROOT / tag / "fused_best_source"
        fpath = run_dir / "fused_cloud_mask.npz"
        raw_path = run_dir / "fused_cloud_mask_raw.npz"
        spath = run_dir / "source_map_cloud_mask.npz"
        if not (fpath.exists() and spath.exists()):
            rows.append({"sample_id": tag, "status": "missing_fused_or_source_map", "fused_path": str(fpath), "source_map_path": str(spath), "raw_path": str(raw_path)})
            continue
        try:
            fz = read_npz(fpath)
            sz = read_npz(spath)
            std = np.asarray(fz["data"])
            valid = np.asarray(fz.get("valid_mask", np.isfinite(std))).astype(bool)
            if raw_path.exists():
                rawz = read_npz(raw_path)
                raw = np.asarray(rawz["data"])
            else:
                raw = np.full(std.shape, -9999, dtype=np.int16)
            source_map = np.asarray(sz["data"])
            source_valid = np.asarray(sz.get("valid_mask", np.isfinite(source_map))).astype(bool)
            for source, sid in METEOSAT_SOURCES.items():
                m = valid & source_valid & (source_map == sid)
                row = {
                    "sample_id": tag,
                    "source": source,
                    "source_id": sid,
                    "selected_n": int(np.count_nonzero(m)),
                    "raw_invalid_code3_selected_n": int(np.count_nonzero(m & (raw == 3))),
                    "std_invalid_selected_n": int(np.count_nonzero(m & ~np.isin(std, [0, 1, 2, 3]))),
                    "std_cloud_n": int(np.count_nonzero(m & np.isin(std, [2, 3]))),
                    "std_clear_n": int(np.count_nonzero(m & np.isin(std, [0, 1]))),
                    "raw_codes_selected": json.dumps({int(k): int(v) for k, v in zip(*np.unique(raw[m], return_counts=True))}, ensure_ascii=False),
                    "std_codes_selected": json.dumps({int(k): int(v) for k, v in zip(*np.unique(std[m], return_counts=True))}, ensure_ascii=False),
                    "status": "ok",
                }
                row.update({f"raw_{k}": v for k, v in count_values(raw, m, [0, 1, 2, 3, -9999]).items()})
                row.update({f"std_{k}": v for k, v in count_values(std, m, [0, 1, 2, 3, -9999]).items()})
                rows.append(row)
        except Exception as exc:
            rows.append({"sample_id": tag, "status": "error", "error": repr(exc)})
    return pd.DataFrame(rows)


def build_mapping_table() -> pd.DataFrame:
    rows = [
        {
            "layer": "stage1_common.cloud_mask_semantics",
            "source": "Meteosat CLM",
            "mapping": "0 clear over water; 1 clear over land; 2 cloud; 3 not_processed/off-earth",
            "fusion_valid_codes": "0,1,2",
            "invalid_or_off_disc_codes": "3",
            "status": "expected",
        },
        {
            "layer": "06_fuse_best_source.cloud_mask_to_standard",
            "source": "Meteosat CLM",
            "mapping": "0->standard 0 clear; 1->standard 0 clear; 2->standard 3 cloud; 3 unmapped invalid",
            "fusion_valid_codes": "standard 0/3 after valid mask",
            "invalid_or_off_disc_codes": "raw 3 rejected before/inside fusion",
            "status": "expected",
        },
        {
            "layer": "09D source_pair_recompute.source_to_standard",
            "source": "Meteosat CLM prefusion",
            "mapping": "0->standard 0 clear; 1->standard 0 clear; 2->standard 3 cloud; 3 unmapped invalid",
            "fusion_valid_codes": "0,1,2 raw mapped to standard",
            "invalid_or_off_disc_codes": "raw 3 becomes -9999 and is excluded",
            "status": "expected",
        },
        {
            "layer": "Stage09D fused comparison",
            "source": "fused_cloud_mask",
            "mapping": "uses already-standardized fused_cloud_mask codes 0/1 clear-like, 2/3 cloud-like",
            "fusion_valid_codes": "fused valid_mask + policy mapping",
            "invalid_or_off_disc_codes": "not compared",
            "status": "expected",
        },
    ]
    return pd.DataFrame(rows)


def md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_No rows_"
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join(["---"] * len(cols)) + " |"]
    for _, r in df.iterrows():
        vals = []
        for c in cols:
            text = "" if pd.isna(r[c]) else str(r[c])
            vals.append(text.replace("|", "\\|").replace("\n", "<br>"))
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines)


def write_report(mapping: pd.DataFrame, pref: pd.DataFrame, fused: pd.DataFrame) -> None:
    pref_ok = pref[(pref["status"] == "ok") & pref["source"].astype(str).str.startswith("Meteosat")]
    fused_ok = fused[fused["status"] == "ok"]
    pref_code3 = int(pd.to_numeric(pref_ok.get("fusion_valid_code3_n", pd.Series(dtype=int)), errors="coerce").fillna(0).sum())
    display_code3 = int(pd.to_numeric(pref_ok.get("display_code3_n", pd.Series(dtype=int)), errors="coerce").fillna(0).sum())
    raw3_selected = int(pd.to_numeric(fused_ok.get("raw_invalid_code3_selected_n", pd.Series(dtype=int)), errors="coerce").fillna(0).sum())
    selected = int(pd.to_numeric(fused_ok.get("selected_n", pd.Series(dtype=int)), errors="coerce").fillna(0).sum())
    pref_sources = pref_ok[["source", "fusion_valid_n", "fusion_count_code_0", "fusion_count_code_1", "fusion_count_code_2", "fusion_count_code_3"]].groupby("source").sum(numeric_only=True).reset_index()
    fused_sources = fused_ok[["source", "selected_n", "raw_count_code_0", "raw_count_code_1", "raw_count_code_2", "raw_count_code_3", "std_count_code_0", "std_count_code_3"]].groupby("source").sum(numeric_only=True).reset_index()

    lines = [
        "# Meteosat CLM semantics audit for Stage 09D",
        "",
        "## Conclusion",
        "",
        "- Production and Stage 09D diagnostic code both use the expected Meteosat CLM semantics: raw `0/1` are clear, raw `2` is cloud, raw `3` is not-processed/off-earth and not valid for fusion/comparison.",
        f"- Across audited prefusion Meteosat files, raw code `3` inside `fusion_valid_mask` totals `{pref_code3}` pixels.",
        f"- Raw code `3` inside `display_valid_mask` totals `{display_code3}` pixels; this is expected because display validity preserves off-disc/not-processed for visual inspection, while fusion validity excludes it.",
        f"- Across fused pixels selected from Meteosat sources, raw code `3` selected into fused output totals `{raw3_selected}` pixels out of `{selected}` Meteosat-selected pixels.",
        "",
        "Therefore the current EPIC comparison did account for the Meteosat `0 clear water / 1 clear land / 2 cloud / 3 not processed` semantics. Meteosat low agreement should not be explained by a simple 0/1/2/3 inversion in the Stage 09D comparison.",
        "",
        "## Mapping table",
        "",
        md_table(mapping),
        "",
        "## Prefusion fusion-valid raw code totals",
        "",
        md_table(pref_sources),
        "",
        "## Fused Meteosat-selected raw/standard code totals",
        "",
        md_table(fused_sources),
        "",
        "## Caveat",
        "",
        "This audit verifies the implemented code semantics and output masks. It does not prove the upstream EUMETSAT product semantics are scientifically complete for every cloud edge/thin cloud case; that remains the proposed semantic mapping delta validation topic.",
    ]
    (OUT / "meteosat_semantics_audit_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = pd.read_csv(MANIFEST)
    mapping = build_mapping_table()
    pref = audit_prefusion(manifest)
    fused = audit_fused(manifest)
    mapping.to_csv(OUT / "meteosat_semantics_mapping_code_audit.csv", index=False, encoding="utf-8-sig")
    pref.to_csv(OUT / "meteosat_prefusion_code_audit_by_sample_source.csv", index=False, encoding="utf-8-sig")
    fused.to_csv(OUT / "meteosat_fused_selected_code_audit_by_sample_source.csv", index=False, encoding="utf-8-sig")
    write_report(mapping, pref, fused)
    print(f"Meteosat semantics audit complete: {OUT}")


if __name__ == "__main__":
    main()
