from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import netCDF4
import numpy as np

import sys

CODE_ROOT = Path(__file__).resolve().parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from path_config import RUNS_ROOT  # noqa: E402


STAGE_ID = "stage_10p"
PROJECT_STAGE_ID = "geo_ring_cloud.stage_10p"
DEFAULT_INPUT = Path(r"F:\DSCOVR_EPIC_L2_COMPOSITE_02_2024.01")
DEFAULT_OUT = RUNS_ROOT / "stage_10p_psf_inventory_202401"
KEYWORDS = [
    "psf",
    "point_spread",
    "fov",
    "field_of_view",
    "weight",
    "weighted",
    "kernel",
    "composite",
    "phase",
    "cloud",
    "height",
    "pressure",
    "optical",
    "fraction",
    "radiance",
]
PSF_KERNEL_TERMS = ["point_spread", "point spread", "kernel"]
WEIGHT_TERMS = ["weight", "weighted"]
CANDIDATE_TERMS = {
    "cloud_fraction": ["cloud", "fraction"],
    "cloud_height": ["cloud", "height"],
    "cloud_pressure": ["cloud", "pressure"],
    "cloud_temperature": ["cloud", "temperature"],
    "cloud_particle_size": ["cloud", "particle"],
    "cloud_optical_thickness": ["cloud", "optical"],
    "cloud_phase": ["cloud", "phase"],
    "radiance_like": ["radiance"],
    "quality_flag": ["quality", "flag", "qa", "dqf"],
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def safe_value(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        if value.size <= 20:
            return [safe_value(v) for v in value.tolist()]
        return {"array_dtype": str(value.dtype), "array_shape": list(value.shape), "preview": [safe_value(v) for v in value.ravel()[:10].tolist()]}
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def attrs_dict(obj: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in obj.ncattrs():
        try:
            out[name] = safe_value(getattr(obj, name))
        except Exception as exc:
            out[name] = f"<unreadable:{type(exc).__name__}:{exc}>"
    return out


def text_blob(*parts: Any) -> str:
    return " ".join(str(p) for p in parts if p is not None).lower()


def snippet(text: str, keyword: str, width: int = 140) -> str:
    low = text.lower()
    i = low.find(keyword.lower())
    if i < 0:
        return text[:width]
    lo = max(0, i - width // 3)
    hi = min(len(text), i + width)
    return text[lo:hi].replace("\n", " ")


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        keys: set[str] = set()
        for row in rows:
            keys.update(row.keys())
        fields = sorted(keys)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            clean = {}
            for key in fields:
                value = row.get(key, "")
                if isinstance(value, (dict, list, tuple, set)):
                    value = json.dumps(value, ensure_ascii=False, sort_keys=True)
                clean[key] = value
            writer.writerow(clean)


def parse_time(path: Path) -> str:
    match = re.search(r"_(\d{8}T\d{6}Z)\.nc$", path.name)
    return match.group(1) if match else ""


def iter_groups(group: netCDF4.Group, prefix: str = ""):
    yield prefix or "/root", group
    for name, child in group.groups.items():
        child_prefix = f"{prefix}/{name}" if prefix else f"/{name}"
        yield from iter_groups(child, child_prefix)


def variable_path(group_path: str, var_name: str) -> str:
    if group_path == "/root":
        return f"/{var_name}"
    return f"{group_path}/{var_name}"


def structure_signature(ds: netCDF4.Dataset) -> str:
    parts: list[str] = []
    for gpath, group in iter_groups(ds):
        dims = sorted((name, len(dim), dim.isunlimited()) for name, dim in group.dimensions.items())
        vars_ = []
        for name, var in sorted(group.variables.items()):
            vars_.append((name, str(var.dtype), tuple(var.dimensions), tuple(var.shape), sorted(var.ncattrs())))
        parts.append(json.dumps({"group": gpath, "dims": dims, "vars": vars_}, sort_keys=True, default=str))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def role_guess(var_path: str, attrs: dict[str, Any]) -> str:
    blob = text_blob(var_path, attrs.get("long_name", ""), attrs.get("standard_name", ""), attrs.get("units", ""), attrs.get("description", ""))
    for role, terms in CANDIDATE_TERMS.items():
        if all(term in blob for term in terms):
            return role
    if "latitude" in blob:
        return "latitude"
    if "longitude" in blob:
        return "longitude"
    if any(term in blob for term in ["radiance", "reflectance", "brightness temperature", "broadband flux", "broadband albedo", "insolation"]):
        return "radiance_like"
    if "solar" in blob or "zenith" in blob or "view" in blob or "sensor" in blob:
        return "geometry_angle"
    return "other"


def candidate_role(var_path: str, attrs: dict[str, Any]) -> str:
    return role_guess(var_path, attrs)


def keyword_hits(text: str) -> list[str]:
    low = text.lower()
    return [k for k in KEYWORDS if k in low]


def is_numeric_var(var: netCDF4.Variable) -> bool:
    try:
        return np.issubdtype(var.dtype, np.number)
    except TypeError:
        return False


def inventory(input_dir: Path, out_dir: Path, max_files: int = 0) -> dict[str, Any]:
    files = sorted(input_dir.glob("*.nc"))
    if not files:
        raise FileNotFoundError(f"no .nc files found in {input_dir}")
    if max_files:
        files = files[:max_files]
    sidecar_files = sorted([p for p in input_dir.iterdir() if p.is_file() and p.suffix.lower() != ".nc"])

    file_rows: list[dict[str, Any]] = []
    global_attr_rows: list[dict[str, Any]] = []
    keyword_rows: list[dict[str, Any]] = []
    dimension_events: list[dict[str, Any]] = []
    group_rows: list[dict[str, Any]] = []
    variable_acc: dict[str, dict[str, Any]] = {}
    candidate_acc: dict[str, dict[str, Any]] = {}
    structure_counts: defaultdict[str, int] = defaultdict(int)
    structure_examples: dict[str, str] = {}
    open_errors: list[dict[str, Any]] = []
    explicit_psf_kernel_vars: set[str] = set()
    explicit_weight_vars: set[str] = set()
    psf_weighted_evidence: list[dict[str, Any]] = []

    for idx, path in enumerate(files, 1):
        file_meta = {
            "file_path": str(path),
            "file_name": path.name,
            "parsed_time_utc": parse_time(path),
            "size_bytes": path.stat().st_size,
            "modified_time": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            "open_status": "pending",
            "structure_signature": "",
            "group_count": 0,
            "variable_count": 0,
            "dimension_count": 0,
            "global_attribute_count": 0,
            "reader": "netCDF4",
        }
        try:
            with netCDF4.Dataset(path) as ds:
                sig = structure_signature(ds)
                structure_counts[sig] += 1
                structure_examples.setdefault(sig, str(path))
                groups = list(iter_groups(ds))
                var_count = sum(len(group.variables) for _, group in groups)
                dim_count = sum(len(group.dimensions) for _, group in groups)
                file_meta.update(
                    {
                        "open_status": "ok",
                        "structure_signature": sig,
                        "group_count": len(groups),
                        "variable_count": var_count,
                        "dimension_count": dim_count,
                        "global_attribute_count": len(ds.ncattrs()),
                    }
                )

                gattrs = attrs_dict(ds)
                for attr_name, attr_value in gattrs.items():
                    value_text = str(attr_value)
                    global_attr_rows.append(
                        {
                            "file_path": str(path),
                            "file_name": path.name,
                            "attribute_name": attr_name,
                            "attribute_value": attr_value,
                            "attribute_value_length": len(value_text),
                        }
                    )
                    for kw in keyword_hits(f"{attr_name} {value_text}"):
                        keyword_rows.append(
                            {
                                "file_path": str(path),
                                "file_name": path.name,
                                "object_type": "global_attribute",
                                "object_path": "/root",
                                "matched_field": attr_name,
                                "keyword": kw,
                                "matched_text_snippet": snippet(value_text, kw),
                                "has_numeric_payload": False,
                            }
                        )
                        if "psf" in kw or "weight" in kw:
                            psf_weighted_evidence.append({"file_path": str(path), "object_type": "global_attribute", "object_path": "/root", "keyword": kw, "matched_field": attr_name, "snippet": snippet(value_text, kw)})

                for gpath, group in groups:
                    group_attr = attrs_dict(group)
                    group_rows.append(
                        {
                            "file_path": str(path),
                            "file_name": path.name,
                            "group_path": gpath,
                            "dimension_count": len(group.dimensions),
                            "variable_count": len(group.variables),
                            "attribute_count": len(group.ncattrs()),
                            "attributes_json": group_attr,
                        }
                    )
                    for dim_name, dim in group.dimensions.items():
                        dimension_events.append(
                            {
                                "file_path": str(path),
                                "file_name": path.name,
                                "group_path": gpath,
                                "dimension_name": dim_name,
                                "dimension_length": len(dim),
                                "is_unlimited": dim.isunlimited(),
                            }
                        )
                        for kw in keyword_hits(dim_name):
                            keyword_rows.append(
                                {
                                    "file_path": str(path),
                                    "file_name": path.name,
                                    "object_type": "dimension",
                                    "object_path": f"{gpath}:{dim_name}",
                                    "matched_field": "dimension_name",
                                    "keyword": kw,
                                    "matched_text_snippet": dim_name,
                                    "has_numeric_payload": False,
                                }
                            )
                    group_text = text_blob(gpath, group_attr)
                    for kw in keyword_hits(group_text):
                        keyword_rows.append(
                            {
                                "file_path": str(path),
                                "file_name": path.name,
                                "object_type": "group",
                                "object_path": gpath,
                                "matched_field": "group_path_or_attributes",
                                "keyword": kw,
                                "matched_text_snippet": snippet(json.dumps(group_attr, ensure_ascii=False), kw),
                                "has_numeric_payload": False,
                            }
                        )

                    for var_name, var in group.variables.items():
                        vpath = variable_path(gpath, var_name)
                        vattrs = attrs_dict(var)
                        vblob = text_blob(vpath, var_name, vattrs)
                        hits = keyword_hits(vblob)
                        numeric = is_numeric_var(var)
                        size = int(np.prod(var.shape)) if var.shape else 1
                        for kw in hits:
                            keyword_rows.append(
                                {
                                    "file_path": str(path),
                                    "file_name": path.name,
                                    "object_type": "variable",
                                    "object_path": vpath,
                                    "matched_field": "variable_name_or_attributes",
                                    "keyword": kw,
                                    "matched_text_snippet": snippet(vblob, kw),
                                    "has_numeric_payload": numeric,
                                    "variable_shape": "x".join(map(str, var.shape)),
                                    "variable_dtype": str(var.dtype),
                                }
                            )
                        if numeric and size > 1 and any(term in vblob for term in PSF_KERNEL_TERMS):
                            explicit_psf_kernel_vars.add(vpath)
                        if numeric and size > 1 and any(term in vblob for term in WEIGHT_TERMS):
                            explicit_weight_vars.add(vpath)
                        if "psf" in vblob or "point spread" in vblob or "point_spread" in vblob:
                            psf_weighted_evidence.append({"file_path": str(path), "object_type": "variable", "object_path": vpath, "keyword": "psf_weighted", "matched_field": "variable_name_or_attributes", "snippet": snippet(vblob, "psf")})

                        acc = variable_acc.setdefault(
                            vpath,
                            {
                                "variable_path": vpath,
                                "group_path": gpath,
                                "variable_name": var_name,
                                "file_count": 0,
                                "first_file": str(path),
                                "last_file": str(path),
                                "dtype_set": set(),
                                "dimension_set": set(),
                                "shape_set": set(),
                                "units_set": set(),
                                "long_name_set": set(),
                                "standard_name_set": set(),
                                "fill_value_set": set(),
                                "valid_min_set": set(),
                                "valid_max_set": set(),
                                "scale_factor_set": set(),
                                "add_offset_set": set(),
                                "keyword_hits": set(),
                                "role_guess": role_guess(vpath, vattrs),
                                "is_numeric": numeric,
                                "sample_attributes_json": vattrs,
                            },
                        )
                        acc["file_count"] += 1
                        acc["last_file"] = str(path)
                        acc["dtype_set"].add(str(var.dtype))
                        acc["dimension_set"].add("|".join(var.dimensions))
                        acc["shape_set"].add("x".join(map(str, var.shape)) if var.shape else "scalar")
                        acc["units_set"].add(str(vattrs.get("units", "")))
                        acc["long_name_set"].add(str(vattrs.get("long_name", "")))
                        acc["standard_name_set"].add(str(vattrs.get("standard_name", "")))
                        acc["fill_value_set"].add(str(vattrs.get("_FillValue", "")))
                        acc["valid_min_set"].add(str(vattrs.get("valid_min", "")))
                        acc["valid_max_set"].add(str(vattrs.get("valid_max", "")))
                        acc["scale_factor_set"].add(str(vattrs.get("scale_factor", "")))
                        acc["add_offset_set"].add(str(vattrs.get("add_offset", "")))
                        acc["keyword_hits"].update(hits)

                        role = candidate_role(vpath, vattrs)
                        if role in CANDIDATE_TERMS:
                            cand = candidate_acc.setdefault(
                                vpath,
                                {
                                    "variable_path": vpath,
                                    "candidate_role": role,
                                    "file_count": 0,
                                    "dtype_set": set(),
                                    "shape_set": set(),
                                    "units_set": set(),
                                    "long_name_set": set(),
                                    "quality_or_mask_evidence": "",
                                    "benchmark_use_note": "",
                                },
                            )
                            cand["file_count"] += 1
                            cand["dtype_set"].add(str(var.dtype))
                            cand["shape_set"].add("x".join(map(str, var.shape)) if var.shape else "scalar")
                            cand["units_set"].add(str(vattrs.get("units", "")))
                            cand["long_name_set"].add(str(vattrs.get("long_name", "")))
        except Exception as exc:
            file_meta["open_status"] = "error"
            file_meta["open_error"] = f"{type(exc).__name__}: {exc}"
            open_errors.append(dict(file_meta))
        file_rows.append(file_meta)
        if idx % 50 == 0:
            print(f"[{STAGE_ID}] metadata scanned {idx}/{len(files)}", flush=True)

    variable_rows: list[dict[str, Any]] = []
    for acc in variable_acc.values():
        row = {}
        for key, value in acc.items():
            if isinstance(value, set):
                row[key] = sorted(value)
            else:
                row[key] = value
        row["shape_consistent"] = len(acc["shape_set"]) == 1
        row["dtype_consistent"] = len(acc["dtype_set"]) == 1
        row["present_in_all_files"] = acc["file_count"] == len(files)
        row["is_explicit_psf_kernel_candidate"] = acc["variable_path"] in explicit_psf_kernel_vars
        row["is_explicit_weight_candidate"] = acc["variable_path"] in explicit_weight_vars
        variable_rows.append(row)

    candidate_rows: list[dict[str, Any]] = []
    for cand in candidate_acc.values():
        role = cand["candidate_role"]
        note = {
            "cloud_fraction": "可作为 Composite cloud fraction / cloud amount 候选基准变量，但需结合质量标志和单位语义。",
            "cloud_height": "可作为 cloud height / effective height 类候选变量；不得默认等同 2024-03 EPIC L2 Cloud A-band CTH-like 变量。",
            "cloud_pressure": "可用于 CTP/CTH/CTT 语义一致性后续审计。",
            "cloud_temperature": "可用于 CTT/CTH/CTP 物理一致性和云高语义审计。",
            "cloud_particle_size": "可用于云微物理分层；不是 CTH 主基准变量。",
            "cloud_optical_thickness": "可用于 COT/光学厚度 benchmark pilot。",
            "cloud_phase": "可用于云相态分层和质量筛选。",
            "radiance_like": "包含 reflectance / brightness temperature / broadband flux / albedo / insolation 等辐射类或通量类变量，可用于几何或 footprint 一致性审计；本产品未必使用变量名 radiance。",
            "quality_flag": "必须作为后续 benchmark 的质量过滤依据。",
        }.get(role, "")
        candidate_rows.append(
            {
                "variable_path": cand["variable_path"],
                "candidate_role": role,
                "file_count": cand["file_count"],
                "dtype_set": sorted(cand["dtype_set"]),
                "shape_set": sorted(cand["shape_set"]),
                "units_set": sorted(cand["units_set"]),
                "long_name_set": sorted(cand["long_name_set"]),
                "benchmark_use_note": note,
            }
        )

    dim_df_rows = summarize_dimensions(dimension_events, file_count=len(files))
    keyword_rows = sorted(keyword_rows, key=lambda r: (r.get("file_name", ""), r.get("object_type", ""), r.get("object_path", ""), r.get("keyword", "")))

    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "file_inventory": out_dir / "stage_10p_composite_file_inventory.csv",
        "variable_inventory": out_dir / "stage_10p_composite_variable_inventory.csv",
        "global_attributes": out_dir / "stage_10p_composite_global_attributes.csv",
        "keyword_search": out_dir / "stage_10p_psf_keyword_search_results.csv",
        "cloud_candidates": out_dir / "stage_10p_cloud_property_variable_candidates.csv",
        "dimension_summary": out_dir / "stage_10p_composite_dimension_summary.csv",
        "report": out_dir / "stage_10p_psf_inventory_report_cn.md",
        "manifest": out_dir / "stage_10p_manifest.json",
    }
    write_csv(paths["file_inventory"], file_rows)
    write_csv(paths["variable_inventory"], sorted(variable_rows, key=lambda r: r["variable_path"]))
    write_csv(paths["global_attributes"], global_attr_rows)
    write_csv(paths["keyword_search"], keyword_rows)
    write_csv(paths["cloud_candidates"], sorted(candidate_rows, key=lambda r: (r["candidate_role"], r["variable_path"])))
    write_csv(paths["dimension_summary"], dim_df_rows)

    summary = build_summary(
        files=files,
        file_rows=file_rows,
        variable_rows=variable_rows,
        keyword_rows=keyword_rows,
        candidate_rows=candidate_rows,
        dimension_rows=dim_df_rows,
        structure_counts=structure_counts,
        structure_examples=structure_examples,
        explicit_psf_kernel_vars=explicit_psf_kernel_vars,
        explicit_weight_vars=explicit_weight_vars,
        psf_weighted_evidence=psf_weighted_evidence,
        open_errors=open_errors,
    )
    write_report(paths["report"], summary, paths, input_dir)
    manifest = {
        "project_stage_id": PROJECT_STAGE_ID,
        "stage_id": STAGE_ID,
        "stage_status": "provisional_inventory_only",
        "generated_utc": utc_now(),
        "input_dir": str(input_dir),
        "output_dir": str(out_dir),
        "reader": "netCDF4",
            "sample_strategy": "all files opened for metadata; no full-array data values read" if not max_files else f"first {max_files} files opened for smoke metadata run; no full-array data values read",
            "sidecar_files": [str(p) for p in sidecar_files],
        "keywords": KEYWORDS,
        "constraints": [
            "No internet download.",
            "No Stage05/06 rerun.",
            "No numerical comparison with 2024-03 Stage09/10 metrics.",
            "Do not claim official PSF kernel unless explicit kernel/weight numeric variables exist in files.",
        ],
        "summary": summary,
        "outputs": {key: str(path) for key, path in paths.items()},
    }
    paths["manifest"].write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8-sig")
    return {"paths": paths, "summary": summary}


def summarize_dimensions(rows: list[dict[str, Any]], file_count: int) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["group_path"], row["dimension_name"])
        acc = grouped.setdefault(
            key,
            {
                "group_path": row["group_path"],
                "dimension_name": row["dimension_name"],
                "file_count": 0,
                "dimension_lengths": set(),
                "is_unlimited_values": set(),
                "example_file": row["file_path"],
            },
        )
        acc["file_count"] += 1
        acc["dimension_lengths"].add(row["dimension_length"])
        acc["is_unlimited_values"].add(row["is_unlimited"])
    out = []
    for acc in grouped.values():
        lengths = sorted(acc["dimension_lengths"])
        out.append(
            {
                "group_path": acc["group_path"],
                "dimension_name": acc["dimension_name"],
                "file_count": acc["file_count"],
                "present_in_all_files": acc["file_count"] == file_count,
                "dimension_lengths": lengths,
                "single_length": lengths[0] if len(lengths) == 1 else "",
                "is_unlimited_values": sorted(acc["is_unlimited_values"]),
                "matches_epic_l2_cloud_2048": 2048 in lengths,
                "matches_common_epic_fov_pair": any(length in {2048, 1024, 512} for length in lengths),
                "example_file": acc["example_file"],
            }
        )
    return sorted(out, key=lambda r: (r["group_path"], r["dimension_name"]))


def build_summary(
    files: list[Path],
    file_rows: list[dict[str, Any]],
    variable_rows: list[dict[str, Any]],
    keyword_rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    dimension_rows: list[dict[str, Any]],
    structure_counts: dict[str, int],
    structure_examples: dict[str, str],
    explicit_psf_kernel_vars: set[str],
    explicit_weight_vars: set[str],
    psf_weighted_evidence: list[dict[str, Any]],
    open_errors: list[dict[str, Any]],
) -> dict[str, Any]:
    dim_lengths = sorted({length for row in dimension_rows for length in (row.get("dimension_lengths") or [])})
    shape_set = sorted({shape for row in variable_rows for shape in row.get("shape_set", [])})
    keyword_counts: defaultdict[str, int] = defaultdict(int)
    for row in keyword_rows:
        keyword_counts[row["keyword"]] += 1
    candidate_counts: defaultdict[str, int] = defaultdict(int)
    for row in candidate_rows:
        candidate_counts[row["candidate_role"]] += 1
    for role in CANDIDATE_TERMS:
        candidate_counts.setdefault(role, 0)
    missing_candidate_roles = sorted([role for role in CANDIDATE_TERMS if candidate_counts.get(role, 0) == 0])
    has_2048 = any(row.get("matches_epic_l2_cloud_2048") for row in dimension_rows)
    has_explicit_kernel = len(explicit_psf_kernel_vars) > 0
    has_explicit_weight = len(explicit_weight_vars) > 0
    has_psf_weighted_text = len(psf_weighted_evidence) > 0
    official_benchmark_status = (
        "official_psf_kernel_available"
        if has_explicit_kernel
        else "official_psf_aware_benchmark_candidate" if has_psf_weighted_text or "composite" in keyword_counts else "composite_product_without_explicit_psf_evidence"
    )
    return {
        "file_count": len(files),
        "opened_file_count": sum(1 for r in file_rows if r.get("open_status") == "ok"),
        "open_error_count": len(open_errors),
        "total_size_bytes": int(sum(p.stat().st_size for p in files)),
        "time_min_utc": min([parse_time(p) for p in files if parse_time(p)] or [""]),
        "time_max_utc": max([parse_time(p) for p in files if parse_time(p)] or [""]),
        "unique_structure_signature_count": len(structure_counts),
        "structure_counts": dict(sorted(structure_counts.items())),
        "structure_examples": structure_examples,
        "variable_count_unique_paths": len(variable_rows),
        "keyword_hit_count": len(keyword_rows),
        "keyword_counts": dict(sorted(keyword_counts.items())),
        "cloud_candidate_counts": dict(sorted(candidate_counts.items())),
        "missing_candidate_roles": missing_candidate_roles,
        "dimension_lengths_observed": dim_lengths,
        "variable_shapes_observed": shape_set,
        "has_dimension_2048": has_2048,
        "has_explicit_psf_kernel_variable": has_explicit_kernel,
        "explicit_psf_kernel_variables": sorted(explicit_psf_kernel_vars),
        "has_explicit_weight_variable": has_explicit_weight,
        "explicit_weight_variables": sorted(explicit_weight_vars),
        "has_psf_weighted_textual_evidence": has_psf_weighted_text,
        "psf_weighted_evidence_count": len(psf_weighted_evidence),
        "official_benchmark_status": official_benchmark_status,
        "can_compare_directly_to_202403_stage10": False,
        "needs_202401_georing_pilot": True,
    }


def fmt_bool(value: bool) -> str:
    return "是" if value else "否"


def fmt_num(value: Any) -> str:
    if isinstance(value, float) and math.isnan(value):
        return "NA"
    return f"{value:,}" if isinstance(value, int) else str(value)


def write_report(path: Path, summary: dict[str, Any], paths: dict[str, Path], input_dir: Path) -> None:
    kernel_vars = summary.get("explicit_psf_kernel_variables", [])
    weight_vars = summary.get("explicit_weight_variables", [])
    status = summary.get("official_benchmark_status", "")
    if status == "official_psf_kernel_available":
        status_cn = "文件内存在可疑显式 PSF kernel 数值变量；后续仍需逐变量确认其是否为官方 kernel。"
    elif status == "official_psf_aware_benchmark_candidate":
        status_cn = "未发现显式 PSF kernel，但 Composite 文件包含 composite/PSF-aware 相关证据，可作为 official PSF-aware benchmark 候选，而不是 official PSF kernel。"
    else:
        status_cn = "未发现显式 PSF kernel，也未发现足够 PSF-weighted 语义证据；当前只能作为官方 Composite 产品结构审计。"

    lines = [
        "# Stage 10P DSCOVR EPIC L2 COMPOSITE PSF Inventory",
        "",
        f"Generated: `{utc_now()}`",
        "",
        "## 阶段定位",
        "",
        "本阶段记为 `geo_ring_cloud.stage_10p` 的 provisional inventory-only audit。输入为本地 2024-01 `DSCOVR_EPIC_L2_COMPOSITE_02` 文件；不联网下载、不重跑 Stage05/06、不把 2024-01 Composite 与 2024-03 Stage09/10 指标做直接数值比较。",
        "",
        "## 文件与结构",
        "",
        f"- 扫描目录：`{input_dir}`",
        f"- 文件数：`{fmt_num(summary.get('file_count'))}`；成功打开：`{fmt_num(summary.get('opened_file_count'))}`；打开失败：`{fmt_num(summary.get('open_error_count'))}`。",
        f"- 时间范围：`{summary.get('time_min_utc')}` 至 `{summary.get('time_max_utc')}`。",
        f"- 唯一结构签名数：`{summary.get('unique_structure_signature_count')}`。",
        f"- 观测到的 dimension lengths：`{summary.get('dimension_lengths_observed')}`。",
        f"- 是否存在 2048 维度：`{fmt_bool(bool(summary.get('has_dimension_2048')))}`。这说明空间维度与 EPIC L2 Cloud 常见 2048 x 2048 网格/FOV 尺度相容；是否完全同一投影和像元定义仍需后续 geolocation 审计。",
        "",
        "## PSF / Composite 结论",
        "",
        f"- 显式 PSF kernel 数值变量：`{fmt_bool(bool(summary.get('has_explicit_psf_kernel_variable')))}`。",
        f"- 显式 weight/weighted 数值变量：`{fmt_bool(bool(summary.get('has_explicit_weight_variable')))}`。",
        f"- PSF kernel 变量候选：`{kernel_vars}`。",
        f"- weight 变量候选：`{weight_vars}`。",
        f"- 判定：{status_cn}",
        "",
        "严格表述规则：除非 `stage_10p_psf_keyword_search_results.csv` 和 `stage_10p_composite_variable_inventory.csv` 中能定位到明确的 kernel/weight 数值变量，否则不能声称本阶段使用了官方 PSF kernel。若只看到 Composite 或 PSF-weighted 语义证据，应写作 official PSF-aware benchmark。",
        "",
        "## 可用于后续 benchmark 的变量",
        "",
        f"- 候选变量角色计数：`{summary.get('cloud_candidate_counts')}`。",
        f"- 未在变量名/属性关键词中识别出的候选角色：`{summary.get('missing_candidate_roles')}`。",
        "- 已识别候选项写入 `stage_10p_cloud_property_variable_candidates.csv`，包括 cloud fraction、cloud height/effective height、cloud pressure、cloud optical depth、cloud temperature、cloud particle size 以及 reflectance/BT/flux/albedo 等 radiance-like 变量。",
        "- 后续 benchmark 必须结合质量标志、单位、有效范围、缺测值和 geolocation/footprint 语义，不应只按变量名直接对齐。",
        "",
        "## 能做与不能做",
        "",
        "- 2024-01 Composite 能做：官方 Composite 产品结构审计、变量可用性审计、PSF/weight/kernel 是否显式存在的证据审计、后续 official PSF-aware benchmark pilot 设计。",
        "- 2024-01 Composite 不能做：直接解释 2024-03 Stage09/10 的数值差异，或替代 2024-03 EPIC L2 Cloud A/B-band effective height 结论。",
        "- 建议后续做 2024-01 小样本 GEO-ring pilot：选取与 Composite 时间相近的 GEO-ring 样本，按 Composite 变量和质量标志做 official Composite benchmark pilot；这应是新 pilot，不是对 2024-03 Stage10 的直接复算。",
        "",
        "## 输出文件",
        "",
    ]
    for label, out_path in paths.items():
        lines.append(f"- {label}: `{out_path}`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage10P inventory for local DSCOVR EPIC L2 COMPOSITE files.")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--max-files", type=int, default=0, help="Optional smoke-test limit; 0 means all files.")
    args = parser.parse_args()
    result = inventory(args.input_dir, args.output_dir, max_files=args.max_files)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
