from __future__ import annotations

import csv
import json
import math
import os
import sys
import tempfile
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

import netCDF4
import numpy as np

CORE_CODE_ROOT = Path(__file__).resolve().parents[1] / "geo_ring_cloud_stage1"
if str(CORE_CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_CODE_ROOT))

from geo_ring_cloud.paths import CLAAS3_ROOT, EXTERNAL_GEO_CLOUD_ROOT, THIRD_REPORT_ROOT  # noqa: E402

WORKSPACE = THIRD_REPORT_ROOT
OUT_DIR = WORKSPACE / "reports" / "claas3_vs_operational_meteosat"

CMSAF_ROOT = CLAAS3_ROOT
OP_ROOT = EXTERNAL_GEO_CLOUD_ROOT
TARGET_DAY = "20240312"
TARGET_HHMM = "1200"


def ensure_eccodes() -> None:
    conda_prefix = Path(os.environ.get("CONDA_PREFIX", sys.prefix))
    candidates = [conda_prefix / "Library" / "bin"]
    for candidate in candidates:
        if not candidate.exists():
            continue
        os.environ["PATH"] = str(candidate) + os.pathsep + os.environ.get("PATH", "")
        try:
            os.add_dll_directory(str(candidate))
        except (AttributeError, FileNotFoundError, OSError):
            pass
    lib_root = conda_prefix / "Library"
    if lib_root.exists():
        os.environ.setdefault("ECCODES_DIR", str(lib_root))


ensure_eccodes()
import cfgrib  # noqa: E402


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8-sig")


def find_single(root: Path, pattern: str) -> Path:
    matches = sorted(root.rglob(pattern))
    if not matches:
        raise FileNotFoundError(f"pattern not found: {root} :: {pattern}")
    return matches[0]


def sqrt_shape(size: int) -> tuple[int, int]:
    side = int(round(math.sqrt(size)))
    if side * side != size:
        raise ValueError(f"cannot square-reshape {size}")
    return side, side


def read_claas(path: Path, product: str) -> dict[str, Any]:
    ds = netCDF4.Dataset(path)
    try:
        out: dict[str, Any] = {
            "path": str(path),
            "product": product,
            "dimensions": {name: len(dim) for name, dim in ds.dimensions.items()},
            "global_attrs": {name: _to_python(getattr(ds, name)) for name in ds.ncattrs()},
            "variables": {},
        }
        for name, var in ds.variables.items():
            attrs = {k: _to_python(getattr(var, k)) for k in var.ncattrs()}
            out["variables"][name] = {
                "shape": tuple(int(v) for v in getattr(var, "shape", ())),
                "dtype": str(var.dtype),
                "attrs": attrs,
            }
        return out
    finally:
        ds.close()


def read_claas_array(path: Path, var_name: str) -> tuple[np.ndarray, dict[str, Any]]:
    ds = netCDF4.Dataset(path)
    try:
        try:
            ds.set_auto_mask(False)
            ds.set_auto_scale(False)
        except Exception:
            pass
        var = ds.variables[var_name]
        raw = np.asarray(var[:], dtype=np.float64)
        attrs = {k: _to_python(getattr(var, k)) for k in var.ncattrs()}
        scale = float(attrs.get("scale_factor", 1.0) or 1.0)
        offset = float(attrs.get("add_offset", 0.0) or 0.0)
        fill = attrs.get("_FillValue", attrs.get("missing_value"))
        arr = raw.copy()
        if fill is not None:
            arr[np.isclose(arr, float(fill))] = np.nan
        arr = arr * scale + offset
        arr = np.squeeze(arr)
        return arr, attrs
    finally:
        ds.close()


def read_operational(zip_path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(zip_path) as zf:
        grib_names = [name for name in zf.namelist() if name.lower().endswith((".grb", ".grib", ".grib2"))]
        if not grib_names:
            raise RuntimeError(f"no grib in {zip_path}")
        with tempfile.NamedTemporaryFile(suffix=".grib", delete=False) as tmp:
            tmp.write(zf.read(grib_names[0]))
            tmp_path = Path(tmp.name)
    try:
        dss = cfgrib.open_datasets(str(tmp_path), indexpath="")
        result = {"path": str(zip_path), "grib_member": grib_names[0], "datasets": []}
        for ds in dss:
            try:
                block = {
                    "dims": {k: int(v) for k, v in ds.sizes.items()},
                    "coords": {},
                    "attrs": {k: _to_python(v) for k, v in ds.attrs.items()},
                    "variables": {},
                }
                for name in ds.coords:
                    coord = ds[name]
                    block["coords"][name] = {
                        "shape": tuple(int(v) for v in coord.shape),
                        "dtype": str(coord.dtype),
                        "attrs": {k: _to_python(v) for k, v in coord.attrs.items()},
                    }
                for name in ds.data_vars:
                    da = ds[name]
                    values = np.asarray(da.values)
                    block["variables"][name] = {
                        "shape": tuple(int(v) for v in values.shape),
                        "dtype": str(da.dtype),
                        "attrs": {k: _to_python(v) for k, v in da.attrs.items()},
                        "values": values,
                    }
                result["datasets"].append(block)
            finally:
                ds.close()
        return result
    finally:
        tmp_path.unlink(missing_ok=True)


def value_counts(arr: np.ndarray) -> dict[str, int]:
    a = np.asarray(arr)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return {}
    counter = Counter(a.astype(np.int64).tolist())
    return {str(k): int(v) for k, v in sorted(counter.items())}


def finite_stats(arr: np.ndarray) -> dict[str, Any]:
    a = np.asarray(arr, dtype=np.float64)
    finite = np.isfinite(a)
    if not finite.any():
        return {
            "valid_pixels": 0,
            "valid_fraction": 0.0,
            "min": "",
            "p05": "",
            "median": "",
            "mean": "",
            "p95": "",
            "max": "",
        }
    vals = a[finite]
    return {
        "valid_pixels": int(vals.size),
        "valid_fraction": float(vals.size / a.size),
        "min": float(np.nanmin(vals)),
        "p05": float(np.nanpercentile(vals, 5)),
        "median": float(np.nanmedian(vals)),
        "mean": float(np.nanmean(vals)),
        "p95": float(np.nanpercentile(vals, 95)),
        "max": float(np.nanmax(vals)),
    }


def infer_operational_clm_mapping(op_arr: np.ndarray, claas_arr: np.ndarray) -> dict[str, Any]:
    mask = np.isfinite(op_arr) & np.isfinite(claas_arr) & np.isin(op_arr, [0, 1]) & np.isin(claas_arr, [0, 1])
    if not mask.any():
        return {"usable_pixels": 0}
    op = op_arr[mask].astype(np.int16)
    claas = claas_arr[mask].astype(np.int16)
    direct = float(np.mean(op == claas))
    reverse = float(np.mean((1 - op) == claas))
    if direct >= reverse:
        mapping = "0->clear, 1->cloudy (preferred by agreement)"
        chosen = direct
    else:
        mapping = "0->cloudy, 1->clear (preferred by agreement)"
        chosen = reverse
    return {
        "usable_pixels": int(mask.sum()),
        "agreement_if_direct": direct,
        "agreement_if_reversed": reverse,
        "preferred_binary_interpretation": mapping,
        "preferred_binary_agreement": chosen,
    }


def compare_cth_grids(claas_cth: np.ndarray, op_cth: np.ndarray) -> dict[str, Any]:
    op_shape = op_cth.shape
    best: dict[str, Any] | None = None
    for row_offset in (0, 1, 2):
        for col_offset in (0, 1, 2):
            view = claas_cth[row_offset : row_offset + op_shape[0] * 3 : 3, col_offset : col_offset + op_shape[1] * 3 : 3]
            if view.shape != op_shape:
                continue
            mask = np.isfinite(view) & np.isfinite(op_cth)
            if not mask.any():
                continue
            diff = view[mask] - op_cth[mask]
            row = {
                "row_offset": row_offset,
                "col_offset": col_offset,
                "sample_count": int(mask.sum()),
                "bias_m": float(np.nanmean(diff)),
                "mae_m": float(np.nanmean(np.abs(diff))),
                "median_diff_m": float(np.nanmedian(diff)),
                "p95_abs_diff_m": float(np.nanpercentile(np.abs(diff), 95)),
            }
            if best is None or row["mae_m"] < best["mae_m"]:
                best = row
    return best or {}


def make_report(
    overview_rows: list[dict[str, Any]],
    native_rows: list[dict[str, Any]],
    code_rows: list[dict[str, Any]],
    iodc_rows: list[dict[str, Any]],
) -> str:
    def md_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
        header = "| " + " | ".join(columns) + " |"
        sep = "| " + " | ".join(["---"] * len(columns)) + " |"
        body = []
        for row in rows:
            body.append("| " + " | ".join(str(row.get(col, "")) for col in columns) + " |")
        return "\n".join([header, sep, *body]) if body else "\n".join([header, sep])

    lines = [
        "# CLAAS-3 vs Operational Meteosat 对比",
        "",
        "## 一句话结论",
        "",
        "- `CLAAS-3` 不是 operational `CLM/CTH` 的同一产品流；它是 `CM SAF` 基于 `MSG/SEVIRI` 的再处理/持续扩展云属性数据集。",
        "- 对你现在的工程而言，`CLAAS-3` 更像是对 Meteosat operational baseline 的增强层：`CMA` 对应云掩膜，`CTX` 对应云顶三参数，`CPP` 提供 operational `CLM/CTH` 本身没有的 `phase/COT/CER/CWP` 等物理量。",
        "- `同一时刻同一像元` 不应预期逐值完全相同：产品流不同、算法不同、质量控制不同，且 `CTH vs CTX` 连原生网格分辨率都不同。",
        "- 当前证据支持：`CLAAS-3` 可以作为 `0度 Meteosat` 的高信息量补充，但不能当作 `IODC` 的完整替身。",
        "",
        "## 1. 产品级总览",
        "",
        md_table(
            overview_rows,
            [
                "comparison_item",
                "operational_baseline",
                "claas3",
                "evidence_or_note",
            ],
        ),
        "",
        "## 2. 2024-03-12 12:00 UTC 样本对比",
        "",
        md_table(
            native_rows,
            [
                "pair",
                "metric",
                "operational_value",
                "claas_value",
                "interpretation",
            ],
        ),
        "",
        "## 3. 云掩膜编码/语义对比",
        "",
        md_table(
            code_rows,
            [
                "product_family",
                "variable",
                "value_or_rule",
                "meaning_or_evidence",
                "engineering_use",
            ],
        ),
        "",
        "## 4. IODC 没有 CLAAS-3 时怎么办",
        "",
        md_table(
            iodc_rows,
            [
                "question",
                "answer",
                "engineering_implication",
            ],
        ),
        "",
        "## 5. 工程建议",
        "",
        "1. `Meteosat-0deg` 继续保留 operational `CLM/CTH` 作为 baseline，同时接入 `CLAAS-3 CMA/CTX/CPP` 作为增强层。",
        "2. `IODC` 侧继续用 operational `CLM-IODC/CTH-IODC` 做 v0；不要把 `FY4B` 当作整个 IODC 的替代品。",
        "3. 如果目标是 `v1/v2` 云物理变量，IODC 区域需要明确接受“空间覆盖不均匀”的事实：可以用 `FY4B/Himawari` 在重叠区补充，但中央印度洋不会被完整补齐。",
        "4. 后续标准化时，应把 `CLAAS-3` 视为 `SEVIRI-derived supplement`，而不是 operational `CLM/CTH` 的简单新版覆盖物。",
        "",
    ]
    return "\n".join(lines)


def _to_python(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        if value.ndim == 0:
            return value.item()
        return value.tolist()
    return value


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    claas_stamp = f"{TARGET_DAY}{TARGET_HHMM}00"
    cma_path = find_single(CMSAF_ROOT, f"CMA*{claas_stamp}*SVMSGI1MD.nc")
    ctx_path = find_single(CMSAF_ROOT, f"CTX*{claas_stamp}*SVMSGI1MD.nc")
    cpp_path = find_single(CMSAF_ROOT, f"CPP*{claas_stamp}*SVMSGI1MD.nc")

    op_clm_path = OP_ROOT / "Meteosat-0deg" / "CLM" / TARGET_DAY / "12" / "MSG3-SEVI-MSGCLMK-0100-0100-20240312120000.000000000Z-NA.zip"
    op_cth_path = OP_ROOT / "Meteosat-0deg" / "CTH" / TARGET_DAY / "12" / "MSG3-SEVI-MSGCLTH-0100-0100-20240312120000.000000000Z-NA.zip"

    cma_meta = read_claas(cma_path, "CMA")
    ctx_meta = read_claas(ctx_path, "CTX")
    cpp_meta = read_claas(cpp_path, "CPP")
    op_clm_meta = read_operational(op_clm_path)
    op_cth_meta = read_operational(op_cth_path)

    cma, cma_attrs = read_claas_array(cma_path, "cma")
    cma_prob, cma_prob_attrs = read_claas_array(cma_path, "cma_prob")
    ctx_cth, ctx_cth_attrs = read_claas_array(ctx_path, "cth")
    ctx_ctp, ctx_ctp_attrs = read_claas_array(ctx_path, "ctp")
    ctx_ctt, ctx_ctt_attrs = read_claas_array(ctx_path, "ctt")
    cpp_cph, cpp_cph_attrs = read_claas_array(cpp_path, "cph")
    cpp_cot, cpp_cot_attrs = read_claas_array(cpp_path, "cot")
    cpp_cre, cpp_cre_attrs = read_claas_array(cpp_path, "cre")
    cpp_cwp, cpp_cwp_attrs = read_claas_array(cpp_path, "cwp")

    op_clm_var = op_clm_meta["datasets"][0]["variables"]["p260537"]
    op_cth_var = op_cth_meta["datasets"][0]["variables"]["ctoph"]
    op_cthqi_var = op_cth_meta["datasets"][0]["variables"]["ctophqi"]

    op_clm = np.asarray(op_clm_var["values"], dtype=np.float64).reshape(sqrt_shape(op_clm_var["values"].size))
    op_cth = np.asarray(op_cth_var["values"], dtype=np.float64).reshape(sqrt_shape(op_cth_var["values"].size))
    op_cthqi = np.asarray(op_cthqi_var["values"], dtype=np.float64).reshape(sqrt_shape(op_cthqi_var["values"].size))

    clm_mapping = infer_operational_clm_mapping(op_clm, cma)
    cth_alignment = compare_cth_grids(ctx_cth, op_cth)

    overview_rows = [
        {
            "comparison_item": "产品定位",
            "operational_baseline": "EUMETSAT operational 云产品流；本地已有 0deg/IODC 的 CLM、CTH",
            "claas3": "CM SAF CLAAS-3 / CLAAS_V003，SEVIRI 云数据记录/ICDR 扩展",
            "evidence_or_note": "不是同一生产链，应视为 baseline + supplement 的关系",
        },
        {
            "comparison_item": "平台/卫星",
            "operational_baseline": "本地样本文件名 `MSG3`，对应 Meteosat-10",
            "claas3": "样本全局属性 `platform = METEOSAT-10`",
            "evidence_or_note": "至少在 0deg 样本上，两者来自同一代 MSG/SEVIRI 平台",
        },
        {
            "comparison_item": "云掩膜",
            "operational_baseline": "CLM: GRIB `p260537`, code table 4.217",
            "claas3": "CMA: `cma + cma_prob + quality + status_flag + conditions`",
            "evidence_or_note": "CLAAS-3 明显 richer，不只是一个单层掩膜",
        },
        {
            "comparison_item": "云顶高度",
            "operational_baseline": "CTH: `ctoph + ctophqi`",
            "claas3": "CTX: `cth + ctp + ctt + uncertainties + quality/status/conditions`",
            "evidence_or_note": "CLAAS-3 CTX 是完整 cloud-top triplet；operational CTH 只保证高度主层",
        },
        {
            "comparison_item": "phase / COT / CER / CWP",
            "operational_baseline": "CLM/CTH 本身没有",
            "claas3": "CPP: `cph/cph_ext/cot/cre/cwp/cdnc/cgt + uncertainties + processing_flag`",
            "evidence_or_note": "这正是 CLAAS-3 对 Meteosat operational 的关键补充",
        },
        {
            "comparison_item": "原生网格",
            "operational_baseline": "CLM 3712x3712；CTH 1237x1237",
            "claas3": "CMA/CTX/CPP 都是 3712x3712",
            "evidence_or_note": "CTH 与 CTX 连 native resolution 都不相同，不能简单说“同像元同值”",
        },
        {
            "comparison_item": "几何",
            "operational_baseline": "GRIB 提供 lat/lon coordinate；0deg service",
            "claas3": "NetCDF 提供 geostationary projection + x/y；主文件不直接带 lat/lon",
            "evidence_or_note": "几何表达方式不同，但都可用于后续重投影",
        },
        {
            "comparison_item": "IODC 适用性",
            "operational_baseline": "已有 `CLM-IODC/CTH-IODC` operational baseline",
            "claas3": "当前没有看到专门 IODC 版 CLAAS-3 产品流",
            "evidence_or_note": "CLAAS-3 不能覆盖整个 IODC 盘面需求",
        },
    ]

    native_rows = [
        {
            "pair": "CLM vs CMA",
            "metric": "native shape",
            "operational_value": str(op_clm.shape),
            "claas_value": str(cma.shape),
            "interpretation": "二者在样本上同为 3712x3712，可做同格对照",
        },
        {
            "pair": "CLM vs CMA",
            "metric": "cloud-mask codes",
            "operational_value": json.dumps(value_counts(op_clm), ensure_ascii=False),
            "claas_value": json.dumps(value_counts(cma), ensure_ascii=False),
            "interpretation": "operational 是 4 类编码；CLAAS CMA 是 clear/cloudy 二值主层",
        },
        {
            "pair": "CLM vs CMA",
            "metric": "binary agreement on op codes {0,1}",
            "operational_value": json.dumps({k: v for k, v in clm_mapping.items() if k != "preferred_binary_interpretation"}, ensure_ascii=False),
            "claas_value": clm_mapping.get("preferred_binary_interpretation", ""),
            "interpretation": "这里是经验判别，不是官方 code table 翻译",
        },
        {
            "pair": "CMA",
            "metric": "probability support",
            "operational_value": "CLM 样本未见显式 probability 主变量",
            "claas_value": f"cma_prob; attrs={json.dumps({k: cma_prob_attrs.get(k) for k in ['units', 'scale_factor', '_FillValue', 'valid_range']}, ensure_ascii=False)}",
            "interpretation": "CLAAS-3 可直接给 cloud probability",
        },
        {
            "pair": "CTH vs CTX",
            "metric": "native shape",
            "operational_value": str(op_cth.shape),
            "claas_value": str(ctx_cth.shape),
            "interpretation": "不在同一原生分辨率；不能直接逐像元一一比较",
        },
        {
            "pair": "CTH vs CTX",
            "metric": "cloud-top-height stats (m)",
            "operational_value": json.dumps(finite_stats(op_cth), ensure_ascii=False),
            "claas_value": json.dumps(finite_stats(ctx_cth), ensure_ascii=False),
            "interpretation": "总体高度量级相近，但 retrieval 细节和掩膜不同",
        },
        {
            "pair": "CTH vs CTX",
            "metric": "rough 3x downsample alignment",
            "operational_value": json.dumps(cth_alignment, ensure_ascii=False),
            "claas_value": "CLAAS CTX decimated to 1237x1237 using best offset",
            "interpretation": "只是工程近似，用于说明二者不是天然同像元产品",
        },
        {
            "pair": "CTX",
            "metric": "extra cloud-top variables",
            "operational_value": "CTH 样本只有 ctoph + ctophqi",
            "claas_value": json.dumps(
                {
                    "ctt_stats": finite_stats(ctx_ctt),
                    "ctp_stats": finite_stats(ctx_ctp),
                },
                ensure_ascii=False,
            ),
            "interpretation": "CLAAS-3 CTX 直接补足 CTT/CTP",
        },
        {
            "pair": "CPP",
            "metric": "v1/v2 cloud physics",
            "operational_value": "operational CLM/CTH 不提供",
            "claas_value": json.dumps(
                {
                    "cph": finite_stats(cpp_cph),
                    "cot": finite_stats(cpp_cot),
                    "cre_m": finite_stats(cpp_cre),
                    "cwp_kgm2": finite_stats(cpp_cwp),
                },
                ensure_ascii=False,
            ),
            "interpretation": "白天样本可见 phase/COT/CER/CWP；这部分是 Meteosat operational baseline 没有的",
        },
    ]

    code_rows = [
        {
            "product_family": "Operational CLM",
            "variable": "p260537",
            "value_or_rule": json.dumps(value_counts(op_clm), ensure_ascii=False),
            "meaning_or_evidence": "GRIB name=Cloud mask, units=Code table 4.217；样本出现 0/1/2/3 四类",
            "engineering_use": "建议把 0/1 当作可判云/晴候选，其余先视作 special/off-disc/not-processed 待官方表确认",
        },
        {
            "product_family": "CLAAS-3 CMA",
            "variable": "cma",
            "value_or_rule": json.dumps(value_counts(cma), ensure_ascii=False),
            "meaning_or_evidence": "NetCDF attrs: flag_values=[0,1], flag_meanings='clear cloudy'",
            "engineering_use": "可直接映射到 clear/cloudy",
        },
        {
            "product_family": "CLAAS-3 CMA",
            "variable": "cma_prob",
            "value_or_rule": f"units={cma_prob_attrs.get('units')}, scale_factor={cma_prob_attrs.get('scale_factor')}",
            "meaning_or_evidence": "概率层存在，存储为 packed integer",
            "engineering_use": "可作为 cloud_probability",
        },
        {
            "product_family": "Operational CTH",
            "variable": "ctoph / ctophqi",
            "value_or_rule": json.dumps(
                {
                    "ctoph_stats": finite_stats(op_cth),
                    "ctophqi_counts": value_counts(op_cthqi),
                },
                ensure_ascii=False,
            ),
            "meaning_or_evidence": "高度主层 + 质量指示",
            "engineering_use": "适合 v0 CTH baseline",
        },
        {
            "product_family": "CLAAS-3 CTX",
            "variable": "cth / ctp / ctt",
            "value_or_rule": json.dumps(
                {
                    "cth_attrs": {k: ctx_cth_attrs.get(k) for k in ['units', 'scale_factor', '_FillValue']},
                    "ctp_attrs": {k: ctx_ctp_attrs.get(k) for k in ['units', 'scale_factor', '_FillValue']},
                    "ctt_attrs": {k: ctx_ctt_attrs.get(k) for k in ['units', 'scale_factor', '_FillValue']},
                },
                ensure_ascii=False,
            ),
            "meaning_or_evidence": "同一产品族中直接包含 cloud-top triplet",
            "engineering_use": "可直接支撑 v1 的 CTH/CTP/CTT",
        },
        {
            "product_family": "CLAAS-3 CPP",
            "variable": "cph / cot / cre / cwp",
            "value_or_rule": json.dumps(
                {
                    "cph_attrs": {k: cpp_cph_attrs.get(k) for k in ['flag_values', 'flag_meanings', '_FillValue']},
                    "cot_attrs": {k: cpp_cot_attrs.get(k) for k in ['units', 'scale_factor', '_FillValue']},
                    "cre_attrs": {k: cpp_cre_attrs.get(k) for k in ['units', 'scale_factor', '_FillValue']},
                    "cwp_attrs": {k: cpp_cwp_attrs.get(k) for k in ['units', 'scale_factor', '_FillValue']},
                },
                ensure_ascii=False,
            ),
            "meaning_or_evidence": "CLAAS-3 物理量层，nighttime 某些太阳反演变量可能全 fill",
            "engineering_use": "适合 v1/v2，但需要昼夜可用性区分",
        },
    ]

    iodc_rows = [
        {
            "question": "IODC 没有 CLAAS-3，是不是只能用 FY4B 替代？",
            "answer": "不是。FY4B 不能替代整个 IODC 盘面，只能补它自己覆盖得到的那一部分。",
            "engineering_implication": "IODC baseline 仍应保留 operational CLM-IODC/CTH-IODC；FY4B 只是 overlap 补充，不是整盘替身。",
        },
        {
            "question": "IODC 还能做 v0 吗？",
            "answer": "能。因为你已有 operational `CLM-IODC + CTH-IODC`。",
            "engineering_implication": "cloud_mask + CTH 的 v0 仍可做，只是 richer cloud physics 不足。",
        },
        {
            "question": "IODC 还能做 v1/v2 吗？",
            "answer": "只能部分做。缺少 dedicated phase/COT/CER/CTP/CTT 来源时，只能在重叠区借助 FY4B/Himawari，中央印度洋仍会缺。",
            "engineering_implication": "需要把 IODC 的变量可用性分成 baseline 区域与 enriched overlap 区域两层，不要误报整区完整。",
        },
        {
            "question": "CLAAS-3 能不能至少覆盖部分印度洋？",
            "answer": "能覆盖一部分 0deg Meteosat disk 东侧邻近印度洋，但不是专门 IODC 服务盘。",
            "engineering_implication": "CLAAS-3 更适合作为 0deg SEVIRI supplement，而不是 IODC supplement。",
        },
    ]

    report = make_report(overview_rows, native_rows, code_rows, iodc_rows)

    write_csv(OUT_DIR / "product_comparison_overview.csv", overview_rows)
    write_csv(OUT_DIR / "same_time_native_comparison.csv", native_rows)
    write_csv(OUT_DIR / "code_table_comparison.csv", code_rows)
    write_csv(OUT_DIR / "iodc_strategy_table.csv", iodc_rows)
    write_text(OUT_DIR / "claas3_vs_operational_meteosat_report.md", report)

    print(str(OUT_DIR / "claas3_vs_operational_meteosat_report.md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
