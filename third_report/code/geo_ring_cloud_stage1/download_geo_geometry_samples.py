from __future__ import annotations

import bz2
import csv
import json
import math
import re
import shutil
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import botocore
import netCDF4
from botocore import UNSIGNED
from botocore.config import Config


OUT_ROOT = Path(r"D:\AAAresearch_paper\geo_geometry_check")
REPORT_PATH = OUT_ROOT / "geometry_sample_download_report.md"
MANIFEST_PATH = OUT_ROOT / "geometry_sample_download_manifest.csv"
SCRIPT_COPY_DIR = Path(r"D:\AAAresearch_paper\third_report\code\geo_ring_cloud_stage1")

TARGET_DT = datetime(2024, 3, 5, 0, 0, tzinfo=timezone.utc)
TARGET_YJJJHH = "2024/065/00"

GOES_BUCKETS = {
    "GOES-16": "noaa-goes16",
    "GOES-18": "noaa-goes18",
}

S3 = boto3.client("s3", config=Config(signature_version=UNSIGNED))


@dataclass
class SampleResult:
    satellite: str
    product: str
    bucket: str
    search_prefix: str
    downloaded_files: list[str]
    local_files: list[str]
    status: str
    file_head_readable: bool
    projection_extracted: bool
    subpoint_lon_extracted: bool
    satellite_height_extracted: bool
    notes: list[str]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def ensure_dirs() -> None:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    for sat in ["GOES-16", "GOES-18", "Himawari-9"]:
        (OUT_ROOT / sat).mkdir(parents=True, exist_ok=True)


def list_common_prefixes(bucket: str, prefix: str) -> list[str]:
    paginator = S3.get_paginator("list_objects_v2")
    prefixes: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        prefixes.extend(cp["Prefix"] for cp in page.get("CommonPrefixes", []))
    return sorted(prefixes)


def list_objects(bucket: str, prefix: str) -> list[dict[str, Any]]:
    paginator = S3.get_paginator("list_objects_v2")
    items: list[dict[str, Any]] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        items.extend(page.get("Contents", []))
    return items


def parse_goes_start_dt(key: str) -> datetime | None:
    m = re.search(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})", key)
    if not m:
        return None
    year = int(m.group(1))
    jday = int(m.group(2))
    hour = int(m.group(3))
    minute = int(m.group(4))
    second = int(m.group(5))
    return datetime.strptime(f"{year} {jday:03d} {hour:02d}:{minute:02d}:{second:02d}", "%Y %j %H:%M:%S").replace(tzinfo=timezone.utc)


def parse_himawari_start_dt(key: str) -> datetime | None:
    for pat in [
        r"_(\d{14})_",
        r"_s(\d{14})_",
        r"/(\d{14})/",
    ]:
        m = re.search(pat, key)
        if m:
            return datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    m = re.search(r"(\d{4})/(\d{2})/(\d{2})/(\d{4})/", key.replace("\\", "/"))
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)[:2]), int(m.group(4)[2:]), tzinfo=timezone.utc)
    return None


def nearest_by_time(keys: list[dict[str, Any]], parser) -> dict[str, Any] | None:
    best = None
    best_delta = None
    for item in keys:
        dt = parser(item["Key"])
        if dt is None:
            continue
        delta = abs((dt - TARGET_DT).total_seconds())
        if best is None or delta < best_delta or (delta == best_delta and item["Key"] < best["Key"]):
            best = item
            best_delta = delta
    return best


def download_s3_object(bucket: str, key: str, local_path: Path) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    S3.download_file(bucket, key, str(local_path))


def inspect_goes_nc(path: Path) -> tuple[bool, bool, bool, bool, list[str]]:
    notes: list[str] = []
    try:
        with netCDF4.Dataset(path) as ds:
            if "goes_imager_projection" not in ds.variables:
                return True, False, False, False, ["goes_imager_projection missing"]
            proj = ds.variables["goes_imager_projection"]
            lon0 = getattr(proj, "longitude_of_projection_origin", None)
            h = getattr(proj, "perspective_point_height", None)
            a = getattr(proj, "semi_major_axis", None)
            b = getattr(proj, "semi_minor_axis", None)
            notes.append(f"longitude_of_projection_origin={lon0}")
            notes.append(f"perspective_point_height={h}")
            notes.append(f"semi_major_axis={a}")
            notes.append(f"semi_minor_axis={b}")
            return True, True, lon0 is not None, h is not None, notes
    except Exception as exc:
        return False, False, False, False, [f"netcdf_read_failed:{type(exc).__name__}:{exc}"]


def extract_hsd_meta_from_bytes(raw: bytes) -> tuple[bool, bool, bool, list[str]]:
    notes: list[str] = []
    projection = False
    subpoint = False
    sat_height = False

    # Minimal binary header heuristic. HSD is little-endian and contains
    # navigation-related doubles/floats in the first few header blocks.
    # We don't claim full HSD decoding here; this is a header sanity check.
    try:
        if len(raw) < 512:
            return False, False, False, ["hsd_header_too_short"]
        floats = []
        for off in range(0, min(len(raw) - 8, 4096), 8):
            try:
                val = struct.unpack("<d", raw[off : off + 8])[0]
                if math.isfinite(val):
                    floats.append((off, val))
            except Exception:
                pass
        lon_hits = [(off, val) for off, val in floats if 120.0 <= val <= 160.0]
        height_hits = [(off, val) for off, val in floats if 4.0e7 <= val <= 4.4e7]
        if lon_hits:
            projection = True
            subpoint = True
            notes.append(f"header_double_lon_candidate={lon_hits[0][1]:.6f}@{lon_hits[0][0]}")
        if height_hits:
            projection = True
            sat_height = True
            notes.append(f"header_double_height_candidate={height_hits[0][1]:.1f}@{height_hits[0][0]}")
        return True, projection, subpoint, sat_height, notes
    except Exception as exc:
        return False, False, False, False, [f"hsd_header_parse_failed:{type(exc).__name__}:{exc}"]


def inspect_himawari_sample(path: Path) -> tuple[bool, bool, bool, bool, list[str]]:
    notes: list[str] = []
    suffix = "".join(path.suffixes).lower()
    try:
        if suffix.endswith(".bz2"):
            with bz2.open(path, "rb") as fh:
                raw = fh.read(8192)
        else:
            raw = path.read_bytes()[:8192]
        header_ok, proj_ok, sub_ok, sat_ok, hdr_notes = extract_hsd_meta_from_bytes(raw)
        notes.extend(hdr_notes)
        return header_ok, proj_ok, sub_ok, sat_ok, notes
    except Exception as exc:
        return False, False, False, False, [f"himawari_read_failed:{type(exc).__name__}:{exc}"]


def select_goes_samples() -> list[SampleResult]:
    results: list[SampleResult] = []
    for satellite, bucket in GOES_BUCKETS.items():
        prefix = f"ABI-L1b-RadF/{TARGET_YJJJHH}/"
        listing = list_objects(bucket, prefix)
        candidates = [obj for obj in listing if "M6C13" in obj["Key"] and obj["Key"].lower().endswith(".nc")]
        if not candidates:
            results.append(
                SampleResult(satellite, "ABI-L1b-RadF M6C13", bucket, prefix, [], [], "NO_MATCH", False, False, False, False, ["no M6C13 file under target prefix"])
            )
            continue
        picked = nearest_by_time(candidates, parse_goes_start_dt)
        if picked is None:
            results.append(
                SampleResult(satellite, "ABI-L1b-RadF M6C13", bucket, prefix, [], [], "NO_MATCH", False, False, False, False, ["M6C13 candidates found but no parsable time"])
            )
            continue
        local_path = OUT_ROOT / satellite / Path(picked["Key"]).name
        download_s3_object(bucket, picked["Key"], local_path)
        head_ok, proj_ok, sub_ok, sat_ok, notes = inspect_goes_nc(local_path)
        results.append(
            SampleResult(
                satellite=satellite,
                product="ABI-L1b-RadF M6C13",
                bucket=bucket,
                search_prefix=prefix,
                downloaded_files=[picked["Key"]],
                local_files=[str(local_path)],
                status="OK",
                file_head_readable=head_ok,
                projection_extracted=proj_ok,
                subpoint_lon_extracted=sub_ok,
                satellite_height_extracted=sat_ok,
                notes=notes,
            )
        )
    return results


def discover_himawari_hour_prefix(bucket: str) -> tuple[str | None, list[str]]:
    notes: list[str] = []
    root = "AHI-L1b-FLDK/"
    lvl1 = list_common_prefixes(bucket, root)
    notes.append(f"root_prefixes={lvl1[:12]}")
    year_prefix = next((p for p in lvl1 if p.rstrip("/").endswith("/2024") or p == "AHI-L1b-FLDK/2024/"), None)
    if year_prefix is None:
        year_prefix = root + "2024/"
    lvl2 = list_common_prefixes(bucket, year_prefix)
    notes.append(f"year_prefixes={lvl2[:12]}")
    month_prefix = next((p for p in lvl2 if p.rstrip("/").endswith("/03")), None)
    if month_prefix is None:
        month_prefix = year_prefix + "03/"
    lvl3 = list_common_prefixes(bucket, month_prefix)
    notes.append(f"month_prefixes={lvl3[:12]}")
    day_prefix = next((p for p in lvl3 if p.rstrip("/").endswith("/05")), None)
    if day_prefix is None:
        day_prefix = month_prefix + "05/"
    lvl4 = list_common_prefixes(bucket, day_prefix)
    notes.append(f"day_prefixes={lvl4[:12]}")
    hour_prefix = next((p for p in lvl4 if p.rstrip("/").endswith("/0000") or p.rstrip("/").endswith("/00")), None)
    if hour_prefix is None:
        # fall back to the first prefix that contains 00
        hour_prefix = next((p for p in lvl4 if "/00" in p), None)
    return hour_prefix, notes


def select_himawari_sample() -> SampleResult:
    bucket = "noaa-himawari9"
    hour_prefix, notes = discover_himawari_hour_prefix(bucket)
    if hour_prefix is None:
        return SampleResult("Himawari-9", "AHI-L1b-FLDK", bucket, "AHI-L1b-FLDK/", [], [], "NO_PREFIX", False, False, False, False, notes + ["could not resolve target hour prefix"])

    listing = list_objects(bucket, hour_prefix)
    keys = [obj for obj in listing if obj["Key"] != hour_prefix]
    notes.append(f"hour_prefix={hour_prefix}")
    notes.append(f"object_count={len(keys)}")
    if not keys:
        return SampleResult("Himawari-9", "AHI-L1b-FLDK", bucket, hour_prefix, [], [], "EMPTY", False, False, False, False, notes + ["no objects under target hour prefix"])

    # Prefer B13 full-disk segment if available. For navigation/header audit one segment is enough.
    b13 = [obj for obj in keys if re.search(r"(B13|C13|_13_)", obj["Key"], re.IGNORECASE)]
    pool = b13 or keys
    picked = nearest_by_time(pool, parse_himawari_start_dt) or pool[0]
    local_path = OUT_ROOT / "Himawari-9" / Path(picked["Key"]).name
    download_s3_object(bucket, picked["Key"], local_path)
    head_ok, proj_ok, sub_ok, sat_ok, inspect_notes = inspect_himawari_sample(local_path)
    notes.extend(inspect_notes)
    notes.append("Single segment/file downloaded because header/navigation audit does not require full stitched radiance mosaic.")
    return SampleResult(
        satellite="Himawari-9",
        product="AHI-L1b-FLDK minimal header sample",
        bucket=bucket,
        search_prefix=hour_prefix,
        downloaded_files=[picked["Key"]],
        local_files=[str(local_path)],
        status="OK",
        file_head_readable=head_ok,
        projection_extracted=proj_ok,
        subpoint_lon_extracted=sub_ok,
        satellite_height_extracted=sat_ok,
        notes=notes,
    )


def write_manifest(results: list[SampleResult]) -> None:
    fieldnames = [
        "satellite",
        "product",
        "bucket",
        "search_prefix",
        "status",
        "downloaded_files",
        "local_files",
        "file_head_readable",
        "projection_extracted",
        "subpoint_lon_extracted",
        "satellite_height_extracted",
        "notes",
    ]
    with MANIFEST_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow(
                {
                    "satellite": r.satellite,
                    "product": r.product,
                    "bucket": r.bucket,
                    "search_prefix": r.search_prefix,
                    "status": r.status,
                    "downloaded_files": " | ".join(r.downloaded_files),
                    "local_files": " | ".join(r.local_files),
                    "file_head_readable": r.file_head_readable,
                    "projection_extracted": r.projection_extracted,
                    "subpoint_lon_extracted": r.subpoint_lon_extracted,
                    "satellite_height_extracted": r.satellite_height_extracted,
                    "notes": " | ".join(r.notes),
                }
            )


def write_report(results: list[SampleResult]) -> None:
    by_sat = {r.satellite: r for r in results}
    lines = [
        "# GEO geometry/navigation 最小样本下载报告",
        "",
        f"- 生成时间 UTC: {utc_now()}",
        "- 访问方式: `boto3` 匿名 S3 (`Config(signature_version=UNSIGNED)`)，原因是当前系统未安装 `aws` CLI。",
        "- 目标时次: `2024-03-05 00:00 UTC`",
        "",
        "## 下载结果",
        "",
        f"- GOES-16 下载文件路径: {', '.join(by_sat['GOES-16'].local_files) if 'GOES-16' in by_sat else 'N/A'}",
        f"- GOES-18 下载文件路径: {', '.join(by_sat['GOES-18'].local_files) if 'GOES-18' in by_sat else 'N/A'}",
        f"- Himawari-9 下载文件路径: {', '.join(by_sat['Himawari-9'].local_files) if 'Himawari-9' in by_sat else 'N/A'}",
        "",
        "## 文件头与几何字段可读性",
        "",
    ]
    for r in results:
        lines.append(
            f"- {r.satellite}: status={r.status}, file_head_readable={r.file_head_readable}, "
            f"projection_extracted={r.projection_extracted}, "
            f"subpoint_lon_extracted={r.subpoint_lon_extracted}, "
            f"satellite_height_extracted={r.satellite_height_extracted}"
        )
    lines.extend(
        [
            "",
            "## 下一步判断",
            "",
        ]
    )
    him = by_sat.get("Himawari-9")
    if him and him.projection_extracted and him.subpoint_lon_extracted and him.satellite_height_extracted:
        lines.append("- 当前最小样本已足以继续本轮 GOES/Himawari geometry 审计，不必立即追加 JAXA P-Tree。")
    elif him and him.file_head_readable:
        lines.append("- Himawari 文件头可读，但当前最小样本未稳定提取完整 projection/subpoint/height。下一步优先考虑 JAXA P-Tree 或更完整的官方 HSD 说明，而不是先用 EUMDAC。")
    else:
        lines.append("- Himawari 最小样本未形成足够稳定的 navigation 解析。下一步需要 JAXA P-Tree 或官方 HSD 资料。EUMDAC 不适用于 Himawari。")
    lines.append("- 本任务不涉及 Meteosat 下载，因此当前不需要 EUMDAC。")
    lines.extend(["", "## 备注", ""])
    for r in results:
        lines.append(f"- {r.satellite}: " + " | ".join(r.notes))
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ensure_dirs()
    dst = SCRIPT_COPY_DIR / Path(__file__).name
    if Path(__file__).resolve() != dst.resolve():
        shutil.copy2(__file__, dst)
    results = []
    results.extend(select_goes_samples())
    results.append(select_himawari_sample())
    write_manifest(results)
    write_report(results)
    print(f"manifest={MANIFEST_PATH}")
    print(f"report={REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
