from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import matplotlib
import numpy as np
import pandas as pd
import yaml

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, ListedColormap

from geo_ring_cloud.lineage import utc_now
from geo_ring_cloud.pipeline_layout import (
    NATIVE_DIR,
    REPORT_DIR,
    SCRIPT_DIR,
    STAGE_ROOT,
    ensure_pipeline_directories as ensure_dirs,
)


INTRO_DIR = STAGE_ROOT / "FY4B_DATA_INTRO"
OUT_DIR = REPORT_DIR / "fy4b_dqf_bit_decode_quicklooks"
RULES_YAML = REPORT_DIR / "fy4b_quality_flag_rules.yaml"
COUNTS_CSV = REPORT_DIR / "fy4b_dqf_bit_decode_counts.csv"
SUMMARY_CSV = REPORT_DIR / "fy4b_dqf_bit_decode_summary.csv"
DOC_EVIDENCE_CSV = REPORT_DIR / "fy4b_quality_flag_doc_evidence.csv"
REPORT_MD = REPORT_DIR / "fy4b_dqf_bit_decode_diagnostics_report.md"

TIME_TAG = "20240305_0000"
TARGET_TIME = "2024-03-05T00:00:00Z"
PRODUCTS = ["CLM", "CLP", "CLT", "CTH", "CTT", "CTP"]
FILL_CODES = {32767}


RULES: dict[str, Any] = {
    "generated_utc": None,
    "status": "temporary_first_pass_screen_only",
    "notes": [
        "Rules are derived from FY-4B AGRI L2 product introduction documents in FY4B_DATA_INTRO and constrained by actual native DQF dtype/values.",
        "Do not use these decoded fields as continuous quality_weight.",
        "32767 is always treated as fill/suspect.",
        "If document dtype and actual dtype disagree, actual dtype and actual values control diagnostics.",
    ],
    "products": {
        "CLM": {
            "dqf_type": "enum",
            "source_variable": "DQF",
            "fill_codes": [32767],
            "enum": {
                0: "good_or_nominal",
                1: "cloud_mask_main_quality_class_1",
                2: "cloud_mask_main_quality_class_2",
                3: "cloud_mask_main_quality_class_3",
                4: "cloud_mask_main_quality_class_4",
                5: "cloud_mask_main_quality_class_5",
                6: "cloud_mask_main_quality_class_6",
            },
        },
        "CLP": {
            "dqf_type": "uint16_bitfield",
            "source_variable": "DQF",
            "fill_codes": [32767],
            "fields": [
                {"name": "bit0", "start_bit": 0, "bit_count": 1},
                {"name": "bits1_2", "start_bit": 1, "bit_count": 2},
                {"name": "bit7", "start_bit": 7, "bit_count": 1},
                {"name": "bit9", "start_bit": 9, "bit_count": 1},
                {"name": "bit10", "start_bit": 10, "bit_count": 1},
                {"name": "bit11", "start_bit": 11, "bit_count": 1},
                {"name": "bit12", "start_bit": 12, "bit_count": 1},
            ],
        },
        "CLT": {
            "dqf_type": "uint16_bitfield",
            "source_variable": "DQF",
            "fill_codes": [32767],
            "fields": [
                {"name": "bit0", "start_bit": 0, "bit_count": 1},
                {"name": "bits1_2", "start_bit": 1, "bit_count": 2},
                {"name": "bit7", "start_bit": 7, "bit_count": 1},
                {"name": "bit9", "start_bit": 9, "bit_count": 1},
                {"name": "bit10", "start_bit": 10, "bit_count": 1},
                {"name": "bit11", "start_bit": 11, "bit_count": 1},
                {"name": "bit12", "start_bit": 12, "bit_count": 1},
            ],
        },
        "CTH": {
            "dqf_type": "uint16_bitfield",
            "source_variable": "DQF",
            "fill_codes": [32767],
            "fields": [
                {"name": "bits0_1", "start_bit": 0, "bit_count": 2},
                {"name": "bits2_3", "start_bit": 2, "bit_count": 2},
                {"name": "bit4", "start_bit": 4, "bit_count": 1},
                {"name": "bit6", "start_bit": 6, "bit_count": 1},
                {"name": "bits7_8", "start_bit": 7, "bit_count": 2},
                {"name": "bit9", "start_bit": 9, "bit_count": 1},
                {"name": "bit10", "start_bit": 10, "bit_count": 1},
            ],
        },
        "CTT": {
            "dqf_type": "uint16_bitfield",
            "source_variable": "DQF",
            "fill_codes": [32767],
            "fields": [
                {"name": "bits0_1", "start_bit": 0, "bit_count": 2},
                {"name": "bits2_3", "start_bit": 2, "bit_count": 2},
                {"name": "bit4", "start_bit": 4, "bit_count": 1},
                {"name": "bit6", "start_bit": 6, "bit_count": 1},
                {"name": "bits7_8", "start_bit": 7, "bit_count": 2},
                {"name": "bit9", "start_bit": 9, "bit_count": 1},
                {"name": "bit10", "start_bit": 10, "bit_count": 1},
            ],
        },
        "CTP": {
            "dqf_type": "uint16_bitfield",
            "source_variable": "DQF",
            "fill_codes": [32767],
            "fields": [
                {"name": "bits0_1", "start_bit": 0, "bit_count": 2},
                {"name": "bits2_3", "start_bit": 2, "bit_count": 2},
                {"name": "bit4", "start_bit": 4, "bit_count": 1},
                {"name": "bit6", "start_bit": 6, "bit_count": 1},
                {"name": "bits7_8", "start_bit": 7, "bit_count": 2},
                {"name": "bit9", "start_bit": 9, "bit_count": 1},
                {"name": "bit10", "start_bit": 10, "bit_count": 1},
            ],
        },
    },
}


def extract_pdf_evidence() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        import pypdf
    except Exception:
        try:
            import PyPDF2 as pypdf  # type: ignore
        except Exception as exc:
            return [{"file": "", "status": "PDF_TEXT_EXTRACT_UNAVAILABLE", "evidence": str(exc)}]
    for pdf in sorted(INTRO_DIR.glob("*.pdf")):
        snippets: list[str] = []
        status = "OK"
        try:
            reader = pypdf.PdfReader(str(pdf))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
            for pattern in ["DQF", "Quality", "quality", "bit", "FillValue", "32767", "Cloud Mask", "Cloud Phase", "Cloud Type"]:
                idx = text.find(pattern)
                if idx >= 0:
                    snippet = re.sub(r"\s+", " ", text[max(0, idx - 180) : idx + 420]).strip()
                    snippets.append(snippet)
            if not snippets:
                status = "NO_KEYWORD_SNIPPET"
        except Exception as exc:
            status = "PDF_TEXT_EXTRACT_FAILED"
            snippets.append(str(exc))
        rows.append({"file": str(pdf), "status": status, "evidence": " || ".join(snippets[:6])})
    return rows


def npz_path(product: str) -> Path:
    return NATIVE_DIR / f"FY4B_{product}_{TIME_TAG}_native_cloud_v0.npz"


def load_quality(product: str) -> tuple[np.ndarray, dict[str, Any], dict[str, Any]]:
    path = npz_path(product)
    with np.load(path, allow_pickle=False) as npz:
        arr = np.asarray(npz["quality_flag_raw"])
        meta = json.loads(str(npz["metadata_json"]))
        avail = json.loads(str(npz["variable_availability_json"]))
    return arr, meta, avail


def as_uint16_work(arr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    raw = np.asarray(arr)
    finite = np.isfinite(raw) if raw.dtype.kind == "f" else np.ones(raw.shape, dtype=bool)
    fill = ~finite
    for code in FILL_CODES:
        fill |= np.isclose(raw.astype(float), float(code))
    work = np.where(fill, 0, raw).astype(np.uint16, copy=False)
    return work, fill


def decode_field(work: np.ndarray, start_bit: int, bit_count: int) -> np.ndarray:
    mask = (1 << bit_count) - 1
    return ((work >> start_bit) & mask).astype(np.uint8)


def unique_rows(product: str, field: str, values: np.ndarray, fill_mask: np.ndarray, decoded: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    valid = values[~fill_mask] if values.shape == fill_mask.shape else values
    if valid.size:
        vals, counts = np.unique(valid, return_counts=True)
        for value, count in zip(vals, counts):
            rows.append(
                {
                    "product": product,
                    "field": field,
                    "value": int(value) if np.issubdtype(vals.dtype, np.integer) else float(value),
                    "count": int(count),
                    "is_fill": False,
                    "is_decoded_field": decoded,
                }
            )
    fill_count = int(np.count_nonzero(fill_mask))
    if fill_count:
        rows.append(
            {
                "product": product,
                "field": field,
                "value": 32767,
                "count": fill_count,
                "is_fill": True,
                "is_decoded_field": decoded,
            }
        )
    return rows


def make_quicklook(product: str, field: str, values: np.ndarray, fill_mask: np.ndarray) -> Path:
    out = OUT_DIR / f"fy4b_{product.lower()}_dqf_{field}.png"
    arr = np.asarray(values).astype(np.float32, copy=True)
    if arr.shape == fill_mask.shape:
        arr[fill_mask] = np.nan
    stride = max(1, int(np.ceil(np.sqrt(arr.size / 1_200_000)))) if arr.size > 1_200_000 else 1
    plot = arr[::stride, ::stride]
    finite = np.isfinite(plot)
    plt.figure(figsize=(8, 5), dpi=150)
    if finite.any() and np.unique(plot[finite]).size <= 16:
        vals = np.sort(np.unique(plot[finite]))
        bounds = np.concatenate(([vals[0] - 0.5], (vals[:-1] + vals[1:]) / 2, [vals[-1] + 0.5]))
        cmap = ListedColormap(plt.get_cmap("tab20")(np.linspace(0, 1, max(1, vals.size))))
        cmap.set_bad((0.92, 0.92, 0.92, 1.0))
        norm = BoundaryNorm(bounds, cmap.N)
        im = plt.imshow(plot, cmap=cmap, norm=norm, interpolation="nearest")
        cbar = plt.colorbar(im, shrink=0.75, ticks=vals)
        cbar.ax.set_yticklabels([str(int(v)) for v in vals])
    else:
        im = plt.imshow(plot, cmap="viridis", interpolation="nearest")
        plt.colorbar(im, shrink=0.75)
    plt.title(f"FY4B {product} DQF {field} {TARGET_TIME}", fontsize=9)
    plt.axis("off")
    plt.tight_layout()
    plt.savefig(out)
    plt.close()
    return out


def diagnose_product(product: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    arr, meta, avail = load_quality(product)
    work, fill_mask = as_uint16_work(arr)
    rows: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    rule = RULES["products"][product]
    attrs = meta.get("reader_attrs", {}).get("attrs_quality_flag_raw", {})
    summary.append(
        {
            "product": product,
            "npz_file": str(npz_path(product)),
            "nominal_time": meta.get("nominal_time", ""),
            "source_variable": meta.get("source_variables", {}).get("quality_flag_raw", ""),
            "actual_dtype": str(arr.dtype),
            "actual_shape": "x".join(map(str, arr.shape)),
            "rule_type": rule["dqf_type"],
            "fill_count_32767": int(np.count_nonzero(fill_mask)),
            "finite_valid_count": int(arr.size - np.count_nonzero(fill_mask)),
            "doc_or_file_attrs": json.dumps(attrs, ensure_ascii=False, default=str),
            "status": "OK",
            "notes": "first-pass diagnostic only; not quality_weight",
        }
    )
    if product == "CLM":
        rows.extend(unique_rows(product, "enum_0_6", work, fill_mask, False))
        make_quicklook(product, "enum_0_6", work, fill_mask)
    else:
        rows.extend(unique_rows(product, "raw_dqf", work, fill_mask, False))
        for field in rule["fields"]:
            decoded = decode_field(work, int(field["start_bit"]), int(field["bit_count"]))
            rows.extend(unique_rows(product, field["name"], decoded, fill_mask, True))
            make_quicklook(product, field["name"], decoded, fill_mask)
    return rows, summary


def write_report(status: str, count_df: pd.DataFrame, summary_df: pd.DataFrame, doc_df: pd.DataFrame) -> None:
    lines = [
        "# FY4B DQF Bit Decode Diagnostics Report",
        "",
        f"- Generated UTC: {utc_now()}",
        f"- Prototype time: `{TARGET_TIME}`",
        f"- Status: **{status}**",
        "- Scope: FY4B DQF diagnostics only; no download, no reprojection, no fusion.",
        "- Use: first-pass screen only. Decoded fields are not mapped to continuous `quality_weight`.",
        "- Rule: if document dtype and actual dtype disagree, actual dtype and actual values are used; `32767` is always fill/suspect.",
        "",
        "## Rule Sources",
        "",
        f"- Product introduction folder: `{INTRO_DIR}`",
        f"- Temporary rules YAML: `{RULES_YAML}`",
        f"- PDF evidence snippets: `{DOC_EVIDENCE_CSV}`",
        "",
        "## Product Summary",
        "",
    ]
    for _, row in summary_df.iterrows():
        lines.append(
            f"- {row['product']}: dtype={row['actual_dtype']} shape={row['actual_shape']} "
            f"rule={row['rule_type']} fill32767={row['fill_count_32767']}"
        )
    lines.extend(["", "## Diagnostics Outputs", ""])
    lines.append(f"- Counts CSV: `{COUNTS_CSV}`")
    lines.append(f"- Summary CSV: `{SUMMARY_CSV}`")
    lines.append(f"- Quicklook directory: `{OUT_DIR}`")
    lines.extend(["", "## Interpretation", ""])
    lines.append("- CLM DQF is treated as enum 0-6.")
    lines.append("- CLP/CLT DQF is decoded with bit0, bits1-2, bit7, bit9, bit10, bit11, bit12.")
    lines.append("- CTH/CTT/CTP DQF is decoded with bits0-1, bits2-3, bit4, bit6, bits7-8, bit9, bit10.")
    lines.append("- The decoded diagnostics can flag suspicious regions, but must not be used as rating weights until official semantics are fully confirmed.")
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ensure_dirs()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(__file__, SCRIPT_DIR / Path(__file__).name)
    rules = dict(RULES)
    rules["generated_utc"] = utc_now()
    RULES_YAML.write_text(yaml.safe_dump(rules, sort_keys=False, allow_unicode=True), encoding="utf-8")
    doc_rows = extract_pdf_evidence()
    pd.DataFrame(doc_rows).to_csv(DOC_EVIDENCE_CSV, index=False, encoding="utf-8-sig")
    all_counts: list[dict[str, Any]] = []
    all_summary: list[dict[str, Any]] = []
    for product in PRODUCTS:
        rows, summary = diagnose_product(product)
        all_counts.extend(rows)
        all_summary.extend(summary)
    count_df = pd.DataFrame(all_counts)
    summary_df = pd.DataFrame(all_summary)
    count_df.to_csv(COUNTS_CSV, index=False, encoding="utf-8-sig")
    summary_df.to_csv(SUMMARY_CSV, index=False, encoding="utf-8-sig")
    status = "PASS_WITH_WARNINGS"
    write_report(status, count_df, summary_df, pd.DataFrame(doc_rows))
    print(f"04b {status}: products={len(summary_df)} decoded_rows={len(count_df)}")
    print(f"report={REPORT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
