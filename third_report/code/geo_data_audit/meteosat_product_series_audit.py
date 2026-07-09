from __future__ import annotations

import csv
import json
import os
import re
import tempfile
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

try:
    import eccodes  # type: ignore
except Exception:
    eccodes = None

try:
    import eumdac  # type: ignore
except Exception:
    eumdac = None


ROOT = Path(r"E:\GEO_Cloud_2024")
REPORT_DIR = Path(r"D:\AAAresearch_paper\data_check_report\meteosat_product_series_audit")
PRIORITY_DIR = Path(r"D:\AAAresearch_paper\data_check_report\priority_download_run_goes_meteosat")
MANIFEST_DIR = ROOT / "manifests"
CRED_FILE = Path(r"D:\AAAresearch_paper\third_report\eumetsat_dataservices_API.txt")

SEARCH_URL = "https://api.eumetsat.int/data/search-products/1.0.0/os"
COLLECTION_URL = "https://api.eumetsat.int/data/browse/collections/1.0.0/collections"

PRODUCT_FAMILIES = ["CLM", "CMA", "CTH", "CTTH", "CT", "OCA", "CMIC"]
BASE_CANDIDATES = [
    ("Cloud Mask", "CLM", ["EO:EUM:DAT:MSG:CLM", "EO:EUM:DAT:MSG:CLM-IODC", "EO:EUM:DAT:MSG:CMA", "EO:EUM:DAT:MSG:CMA-IODC"]),
    ("Cloud Type", "CT", ["EO:EUM:DAT:MSG:CT", "EO:EUM:DAT:MSG:CT-IODC"]),
    ("Cloud Top Height", "CTH", ["EO:EUM:DAT:MSG:CTH", "EO:EUM:DAT:MSG:CTH-IODC"]),
    ("Cloud Top Temperature and Height", "CTTH", ["EO:EUM:DAT:MSG:CTTH", "EO:EUM:DAT:MSG:CTTH-IODC"]),
    ("Optimal Cloud Analysis", "OCA", ["EO:EUM:DAT:MSG:OCA", "EO:EUM:DAT:MSG:OCA-IODC"]),
    ("Cloud Microphysics", "CMIC", ["EO:EUM:DAT:MSG:CMIC", "EO:EUM:DAT:MSG:CMIC-IODC"]),
]


def read_credentials() -> tuple[str | None, str | None]:
    key = os.environ.get("EUMETSAT_CONSUMER_KEY")
    secret = os.environ.get("EUMETSAT_CONSUMER_SECRET")
    if key and secret:
        return key, secret
    if not CRED_FILE.exists():
        return None, None
    text = CRED_FILE.read_text(encoding="utf-8", errors="ignore")
    found: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
        elif "=" in line:
            k, v = line.split("=", 1)
        else:
            continue
        lk = k.strip().lower().replace(" ", "_")
        if "consumer" in lk and "key" in lk:
            found["key"] = v.strip().strip('"').strip("'")
        if "consumer" in lk and ("secret" in lk or "secert" in lk):
            found["secret"] = v.strip().strip('"').strip("'")
    return found.get("key"), found.get("secret")


def token() -> Any:
    key, secret = read_credentials()
    if key and secret and eumdac:
        try:
            return eumdac.AccessToken((key, secret))
        except Exception as exc:
            return f"EUMDAC token failed: {exc}"
    return "No EUMETSAT credentials found"


def bearer_token() -> str | None:
    key, secret = read_credentials()
    if not key or not secret:
        return None
    proxies = proxy_dict()
    try:
        response = requests.post(
            "https://api.eumetsat.int/token",
            auth=(key, secret),
            data={"grant_type": "client_credentials"},
            timeout=60,
            proxies=proxies,
        )
        response.raise_for_status()
        value = response.json().get("access_token")
        return str(value) if value else None
    except Exception:
        return None


def proxy_dict() -> dict[str, str]:
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "http://127.0.0.1:7897"
    return {"http": proxy, "https": proxy} if proxy else {}


def load_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def infer_product(path: Path) -> str:
    parts = [p.upper() for p in path.parts]
    for prod in PRODUCT_FAMILIES:
        if prod in parts:
            return prod
    name = path.name.upper()
    mapping = {
        "MSGCLMK": "CLM",
        "MSGCLTH": "CTH",
        "MSGCTTH": "CTTH",
        "MSGCT": "CT",
        "MSGOCA": "OCA",
        "CMIC": "CMIC",
        "CMA": "CMA",
    }
    for token_, prod in mapping.items():
        if token_ in name:
            return prod
    return "UNKNOWN"


def sample_files() -> list[Path]:
    files: list[Path] = []
    for sat in ["Meteosat-0deg", "Meteosat-IODC"]:
        base = ROOT / sat
        if base.exists():
            files.extend(base.rglob("*.zip"))
    by_key: dict[tuple[str, str], list[Path]] = defaultdict(list)
    for p in files:
        by_key[(p.parts[2] if len(p.parts) > 2 else "Meteosat", infer_product(p))].append(p)
    out: list[Path] = []
    for values in by_key.values():
        values = sorted(values)
        if len(values) <= 3:
            out.extend(values)
        else:
            out.extend([values[0], values[len(values) // 2], values[-1]])
    return sorted(set(out))


def zip_members(path: Path) -> list[dict[str, Any]]:
    rows = []
    try:
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                rows.append({"member": info.filename, "size": info.file_size, "compress_size": info.compress_size})
    except Exception as exc:
        rows.append({"member": "ZIP_ERROR", "size": 0, "compress_size": 0, "error": str(exc)})
    return rows


def grib_messages_from_zip(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if eccodes is None:
        return [{"file": str(path), "error": "eccodes unavailable"}]
    try:
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if not re.search(r"\.(grb|grib|grib2)$", info.filename, re.I):
                    continue
                data = zf.read(info)
                with tempfile.NamedTemporaryFile(delete=False, suffix=".grb") as tmp:
                    tmp.write(data)
                    tmp_path = tmp.name
                try:
                    with open(tmp_path, "rb") as fh:
                        idx = 0
                        while True:
                            gid = eccodes.codes_grib_new_from_file(fh)
                            if gid is None:
                                break
                            idx += 1
                            row: dict[str, Any] = {"file": str(path), "member": info.filename, "message": idx}
                            for key in [
                                "shortName",
                                "name",
                                "paramId",
                                "typeOfLevel",
                                "level",
                                "gridType",
                                "Nx",
                                "Ny",
                                "numberOfPoints",
                                "units",
                                "dataDate",
                                "dataTime",
                            ]:
                                try:
                                    row[key] = eccodes.codes_get(gid, key)
                                except Exception:
                                    row[key] = ""
                            row["shape"] = f"{row.get('Ny','')}x{row.get('Nx','')}"
                            try:
                                vals = eccodes.codes_get_values(gid)
                                row["min"] = float(vals.min())
                                row["max"] = float(vals.max())
                            except Exception as exc:
                                row["value_error"] = str(exc)
                            eccodes.codes_release(gid)
                            rows.append(row)
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
    except Exception as exc:
        rows.append({"file": str(path), "error": str(exc)})
    return rows


def local_inventory() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    files = sample_files()
    local_rows = []
    grib_rows = []
    for path in files:
        sat = "Meteosat-IODC" if "Meteosat-IODC" in str(path) else "Meteosat-0deg"
        product = infer_product(path)
        collection = ""
        if product == "CLM":
            collection = "EO:EUM:DAT:MSG:CLM-IODC" if sat.endswith("IODC") else "EO:EUM:DAT:MSG:CLM"
        elif product in {"CTH", "CTTH"}:
            collection = "EO:EUM:DAT:MSG:CTH-IODC" if sat.endswith("IODC") else "EO:EUM:DAT:MSG:CTH"
        members = zip_members(path)
        gribs = grib_messages_from_zip(path)
        grib_rows.extend(gribs)
        local_rows.append({
            "satellite": sat,
            "folder_product": product,
            "inferred_collection": collection,
            "file": str(path),
            "filename_pattern": re.sub(r"\d{14}", "YYYYMMDDHHMMSS", path.name),
            "size_bytes": path.stat().st_size,
            "zip_members": "; ".join(f"{m.get('member')}({m.get('size')})" for m in members[:10]),
            "grib_shortnames": ",".join(sorted({str(g.get("shortName")) for g in gribs if g.get("shortName")})),
            "xml_members": "; ".join(m.get("member", "") for m in members if str(m.get("member", "")).lower().endswith(".xml")),
        })
    return local_rows, grib_rows


def collection_metadata(ids: list[str]) -> list[dict[str, Any]]:
    t = token()
    out = []
    store = None
    if eumdac and not isinstance(t, str):
        try:
            store = eumdac.DataStore(t)
        except Exception:
            store = None
    for cid in ids:
        row = {"collection_id": cid}
        if store:
            try:
                col = store.get_collection(cid)
                row["exists"] = True
                row["title"] = getattr(col, "title", "") or getattr(col, "abstract", "")
                row["abstract"] = getattr(col, "abstract", "")
            except Exception as exc:
                row["exists"] = False
                row["notes"] = f"get_collection failed: {type(exc).__name__}: {exc}"
        else:
            row["exists"] = False
            row["notes"] = str(t)
        out.append(row)
    return out


def search_product_count(cid: str, start: str = "2024-03-12T00:00:00Z", end: str = "2024-03-12T01:00:00Z") -> dict[str, Any]:
    params = {"format": "json", "pi": cid, "si": 0, "c": 5, "dtstart": start, "dtend": end}
    proxies = proxy_dict()
    headers = {}
    bearer = bearer_token()
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    try:
        r = requests.get(SEARCH_URL, params=params, timeout=40, proxies=proxies, headers=headers)
        row = {"http_status": r.status_code, "url": r.url}
        if r.ok:
            js = r.json()
            feats = js.get("features", []) if isinstance(js, dict) else []
            row["can_download_2024_03"] = bool(feats)
            row["sample_product_id"] = feats[0].get("id", "") if feats else ""
            row["sample_title"] = feats[0].get("properties", {}).get("title", "") if feats else ""
            row["count_returned"] = len(feats)
        else:
            row["can_download_2024_03"] = False
            row["error"] = r.text[:300]
        return row
    except Exception as exc:
        return {"can_download_2024_03": False, "error": f"{type(exc).__name__}: {exc}"}


def candidate_collections() -> list[dict[str, Any]]:
    candidate_ids: list[str] = []
    for _, _, ids in BASE_CANDIDATES:
        candidate_ids.extend(ids)
    for csv_name in ["meteosat_v1_download_manifest.csv", "priority_download_status.csv", "priority_download_verification.csv"]:
        for row in load_csv(PRIORITY_DIR / csv_name):
            cid = (row.get("collection_id") or "").strip()
            if cid.startswith("EO:EUM:DAT:MSG:"):
                candidate_ids.append(cid)
            note = row.get("note") or ""
            for match in re.findall(r"EO:EUM:DAT:MSG:[A-Z0-9-]+", note):
                candidate_ids.append(match)
    try:
        options = json.loads((MANIFEST_DIR / "meteosat_collection_options.json").read_text(encoding="utf-8"))
        for item in options if isinstance(options, list) else options.values():
            if isinstance(item, str) and item.startswith("EO:EUM"):
                candidate_ids.append(item)
            if isinstance(item, dict):
                for v in item.values():
                    if isinstance(v, str) and v.startswith("EO:EUM"):
                        candidate_ids.append(v)
    except Exception:
        pass
    candidate_ids = sorted(set(candidate_ids))
    local_ok: set[str] = set()
    for p in (MANIFEST_DIR / "manifest_meteosat_downloaded.csv", MANIFEST_DIR / "manifest_downloaded.csv"):
        for old in load_csv(p):
            cid = (old.get("collection_id") or "").strip()
            status = (old.get("status") or "").strip().upper()
            if cid.startswith("EO:EUM:DAT:MSG:") and status in {"OK", "DOWNLOADED", "SKIPPED_EXISTING"}:
                local_ok.add(cid)
    for p in (PRIORITY_DIR / "priority_download_status.csv", PRIORITY_DIR / "priority_download_verification.csv"):
        for old in load_csv(p):
            cid = (old.get("collection_id") or "").strip()
            status = (old.get("verification_status") or old.get("status") or "").strip().upper()
            if cid.startswith("EO:EUM:DAT:MSG:") and status in {"OK", "SKIPPED_EXISTING"}:
                local_ok.add(cid)
    meta_by_id = {r["collection_id"]: r for r in collection_metadata(candidate_ids)}
    rows = []
    product_hints = {
        "CLM": "cloud mask",
        "CMA": "cloud mask",
        "CTH": "cloud top height only in current GRIB evidence",
        "CTTH": "cloud top temperature/pressure/height if such collection exists",
        "CT": "cloud type / cloud phase if such collection exists",
        "OCA": "cloud optical thickness/effective radius if such collection exists",
        "CMIC": "cloud microphysics COT/CER if such collection exists",
    }
    for cid in candidate_ids:
        prod = cid.rsplit(":", 1)[-1].replace("-IODC", "")
        fam = next((label for _, label, ids in BASE_CANDIDATES if cid in ids), prod)
        row = {
            "collection_id": cid,
            "service_position": "IODC" if cid.endswith("-IODC") else "0deg/nominal",
            "family": fam,
            "variables_expected": product_hints.get(prod, ""),
            "access_method": "EUMETSAT Data Store search-products API/eumdac",
        }
        row.update(meta_by_id.get(cid, {}))
        search = search_product_count(cid)
        row.update(search)
        if cid in local_ok:
            row["can_download_2024_03"] = True
            row["notes"] = ((row.get("notes") or "") + "; " if row.get("notes") else "") + "confirmed by local downloaded/verified 2024-03 files"
        if not row.get("title"):
            row["title"] = row.get("sample_title", "")
        notes = row.get("notes", "")
        if row.get("http_status") == 404:
            notes = (notes + "; " if notes else "") + "search-products returned 404 for 2024-03 query"
        row["notes"] = notes
        rows.append(row)
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def md_table(rows: list[dict[str, Any]], keys: list[str], limit: int = 50) -> str:
    if not rows:
        return "_No rows._"
    shown = rows[:limit]
    lines = ["| " + " | ".join(keys) + " |", "| " + " | ".join(["---"] * len(keys)) + " |"]
    for row in shown:
        vals = []
        for key in keys:
            val = str(row.get(key, ""))
            val = val.replace("|", "\\|").replace("\n", " ")
            if len(val) > 120:
                val = val[:117] + "..."
            vals.append(val)
        lines.append("| " + " | ".join(vals) + " |")
    if len(rows) > limit:
        lines.append(f"\n_Showing {limit} of {len(rows)} rows._")
    return "\n".join(lines)


def write_report(local_rows: list[dict[str, Any]], grib_rows: list[dict[str, Any]], coll_rows: list[dict[str, Any]]) -> Path:
    c = Counter((r.get("satellite"), r.get("folder_product"), r.get("inferred_collection")) for r in local_rows)
    grib_by_short = Counter(str(r.get("shortName")) for r in grib_rows if r.get("shortName"))
    found = {r["collection_id"]: r for r in coll_rows}
    lines = [
        "# Meteosat Product Series Audit",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "Scope: local Meteosat ZIP/GRIB inspection plus small EUMETSAT collection/product-search checks for March 2024. No bulk download was performed.",
        "",
        "## Local Product Identity",
        "",
        md_table([{"satellite": k[0], "folder_product": k[1], "inferred_collection": k[2], "sample_count": v} for k, v in c.items()], ["satellite", "folder_product", "inferred_collection", "sample_count"]),
        "",
        "## Local ZIP / GRIB Evidence",
        "",
        md_table(local_rows, ["satellite", "folder_product", "inferred_collection", "file", "filename_pattern", "zip_members", "grib_shortnames"], 20),
        "",
        "## GRIB Message Short Names",
        "",
        md_table([{"shortName": k, "messages": v} for k, v in grib_by_short.items()], ["shortName", "messages"]),
        "",
        "## GRIB Message Detail",
        "",
        md_table(grib_rows, ["file", "member", "message", "shortName", "name", "paramId", "typeOfLevel", "level", "gridType", "shape", "units", "min", "max"], 80),
        "",
        "## Candidate Collections",
        "",
        md_table(coll_rows, ["collection_id", "title", "service_position", "family", "variables_expected", "access_method", "can_download_2024_03", "http_status", "sample_product_id", "notes"], 80),
        "",
        "## Conclusions",
        "",
    ]
    local_short = set(grib_by_short)
    has_ctt = any(x.lower() in {"t", "ctt", "cttgrd", "ctot", "cltt", "ctth"} or "temperature" in x.lower() for x in local_short)
    has_ctp = any("pres" in x.lower() or x.lower() in {"pres", "ctop"} for x in local_short)
    lines.extend([
        f"- 当前已有 CLM: 本地 `CLM` ZIP 文件名为 `MSG*-SEVI-MSGCLMK-...zip`，collection 推断为 `EO:EUM:DAT:MSG:CLM` / `EO:EUM:DAT:MSG:CLM-IODC`。",
        f"- 当前已有 CTH/CTTH 目录: 本地文件名均为 `MSG*-SEVI-MSGCLTH-...zip`，实际 collection 来自 `EO:EUM:DAT:MSG:CTH` / `EO:EUM:DAT:MSG:CTH-IODC`；上一轮 `CTTH` 目录只是回退保存位置，不代表真正 CTTH collection。",
        f"- 当前 CTH GRIB 是否包含 CTT/CTP: {'有温度证据' if has_ctt else '未发现 CTT'}；{'有压力证据' if has_ctp else '未发现 CTP'}。本次 shortName 集合为 `{', '.join(sorted(local_short))}`。",
        "- 当前 CT/cloud type: 本地未发现独立 `CT` ZIP；候选 `EO:EUM:DAT:MSG:CT` / `CT-IODC` 若 2024-03 查询为 404 或无样本，则不能视为已确认可下载。",
        "- 当前 OCA/CMIC: 本地未发现独立 `OCA`/`CMIC` ZIP；候选 collection 是否能用于 2024-03 以表中 `can_download_2024_03` 为准。",
        "- 气候数据集或软件包: 本报告只把 Data Store search-products 在 2024-03 返回产品的 collection 视为可用于当前下载；只有说明页、软件包或气候再处理数据不能直接替代 MSG 2024-03 近实时/归档 L2 下载。",
    ])
    report = REPORT_DIR / "meteosat_product_series_audit.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    local_rows, grib_rows = local_inventory()
    coll_rows = candidate_collections()
    write_csv(REPORT_DIR / "local_meteosat_zip_inventory.csv", local_rows)
    write_csv(REPORT_DIR / "local_meteosat_grib_messages.csv", grib_rows)
    write_csv(REPORT_DIR / "meteosat_candidate_collections.csv", coll_rows)
    report = write_report(local_rows, grib_rows, coll_rows)
    print(f"report: {report}")


if __name__ == "__main__":
    main()
