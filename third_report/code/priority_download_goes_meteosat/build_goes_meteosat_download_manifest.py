from __future__ import annotations

import base64
import argparse
import csv
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
import pandas as pd
import requests
from botocore import UNSIGNED
from botocore.config import Config

CORE_CODE_ROOT = Path(__file__).resolve().parents[1] / "geo_ring_cloud_stage1"
if str(CORE_CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_CODE_ROOT))

from geo_ring_cloud.paths import (  # noqa: E402
    DATA_CHECK_ROOT,
    EUMETSAT_CREDENTIALS_FILE,
    EXTERNAL_GEO_CLOUD_ROOT,
)


OUT_DIR = DATA_CHECK_ROOT / "priority_download_run_goes_meteosat"
ROOT = EXTERNAL_GEO_CLOUD_ROOT
PARSED = DATA_CHECK_ROOT / "parsed_file_metadata.csv"
CRED_FILE = EUMETSAT_CREDENTIALS_FILE
EUM_SEARCH_URL = "https://api.eumetsat.int/data/search-products/1.0.0/os"
EUM_TOKEN_URL = "https://api.eumetsat.int/token"
FIELDS = [
    "priority","target_time_utc","satellite","product","source_product","provider","collection_id",
    "remote_type","bucket","remote_key_or_product_id","remote_name","actual_start_time","actual_end_time",
    "size_bytes","status","local_path","note",
]
GOES_PRODUCTS = {
    "ACTPF": "ABI-L2-ACTPF",
    "CTPF": "ABI-L2-CTPF",
    "ACHTF": "ABI-L2-ACHTF",
    "CODF": "ABI-L2-CODF",
    "CPSF": "ABI-L2-CPSF",
}
GOES_PRIORITY = {"ACTPF": 1, "CTPF": 1, "ACHTF": 1, "CODF": 2, "CPSF": 2}
MET_PRODUCTS = {"CTTH": 1, "CT": 1, "OCA": 2, "CMIC": 2}
MET_KEYWORDS = {
    "CTTH": ["ctth", "cloud top temperature", "cloud top pressure", "cloud top height"],
    "CT": ["cloud type", " ct ", "phase"],
    "OCA": ["oca", "optical", "effective radius"],
    "CMIC": ["cmic", "microphysics", "optical", "effective radius"],
}


def log(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] = FIELDS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with tmp.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    os.replace(tmp, path)


def read_existing() -> pd.DataFrame:
    df = pd.read_csv(PARSED, encoding="utf-8-sig", low_memory=False)
    df = df[df["file_path"].astype(str).map(lambda p: Path(p).exists())].copy()
    return df


def parse_time(v: Any) -> datetime | None:
    if pd.isna(v) or not str(v).strip():
        return None
    s = str(v).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        m = re.search(r"(20\d{2})(\d{2})(\d{2})(\d{2})?", s)
        if not m:
            return None
        hour = int(m.group(4) or "0")
        dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), hour)
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def target_intersection(df: pd.DataFrame, satellite: str, products: list[str]) -> list[datetime]:
    sets = []
    for prod in products:
        sub = df[(df["satellite"].astype(str) == satellite) & (df["product"].astype(str) == prod)]
        times = set()
        for v in sub["nominal_time"].tolist():
            dt = parse_time(v)
            if dt:
                times.add(dt.replace(minute=0, second=0, microsecond=0))
        sets.append(times)
    return sorted(set.intersection(*sets)) if sets else []


def local_path(sat: str, product: str, target: datetime, name: str) -> str:
    return str(ROOT / sat / product / target.strftime("%Y%m%d") / target.strftime("%H") / name)


def s3_client():
    os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""
    proxies = {"https": proxy, "http": proxy} if proxy else None
    return boto3.client("s3", config=Config(signature_version=UNSIGNED, proxies=proxies, retries={"max_attempts": 5}, connect_timeout=20, read_timeout=60))


def goes_bucket(sat: str) -> str:
    return "noaa-goes16" if sat == "GOES-16" else "noaa-goes18"


def goes_gnum(sat: str) -> str:
    return "G16" if sat == "GOES-16" else "G18"


def pick_goes_key(client, sat: str, product: str, target: datetime) -> dict[str, Any]:
    bucket = goes_bucket(sat)
    full = GOES_PRODUCTS[product]
    prefix = f"{full}/{target.year}/{target.timetuple().tm_yday:03d}/{target.hour:02d}/"
    try:
        resp = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    except Exception as exc:
        return {"status": "FAILED_NETWORK", "note": f"s3_list_failed:{type(exc).__name__}:{exc}"}
    candidates = []
    for obj in resp.get("Contents", []):
        key = obj["Key"]
        if f"_{goes_gnum(sat)}_" not in key or not key.endswith(".nc"):
            continue
        m = re.search(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})", key)
        if not m:
            continue
        st = datetime(int(m.group(1)), 1, 1, tzinfo=timezone.utc) + timedelta(days=int(m.group(2))-1, hours=int(m.group(3)), minutes=int(m.group(4)), seconds=int(m.group(5)))
        diff = abs((st.replace(minute=0, second=0, microsecond=0) - target).total_seconds())
        candidates.append((diff, st, obj))
    if not candidates:
        return {"status": "FAILED_REMOTE_MISSING", "note": f"no_s3_object:{prefix}"}
    _, st, obj = sorted(candidates, key=lambda x: (x[0], x[2]["Key"]))[0]
    return {
        "status": "FOUND",
        "bucket": bucket,
        "remote_key_or_product_id": obj["Key"],
        "remote_name": Path(obj["Key"]).name,
        "actual_start_time": iso(st),
        "size_bytes": obj.get("Size", ""),
    }


def list_goes_day(client, sat: str, product: str, day: datetime) -> list[dict[str, Any]]:
    bucket = goes_bucket(sat)
    full = GOES_PRODUCTS[product]
    prefix = f"{full}/{day.year}/{day.timetuple().tm_yday:03d}/"
    out = []
    token = None
    try:
        while True:
            kwargs = {"Bucket": bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            resp = client.list_objects_v2(**kwargs)
            out.extend(resp.get("Contents", []))
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
    except Exception as exc:
        return [{"_error": f"s3_list_failed:{type(exc).__name__}:{exc}", "_prefix": prefix}]
    return out


def goes_day_index(client, sat: str, product: str, targets: list[datetime]) -> dict[datetime, dict[str, Any]]:
    by_day: dict[str, list[datetime]] = {}
    for t in targets:
        by_day.setdefault(t.strftime("%Y%m%d"), []).append(t)
    picked = {}
    for day_key, day_targets in by_day.items():
        day = day_targets[0]
        objects = list_goes_day(client, sat, product, day)
        if objects and "_error" in objects[0]:
            for t in day_targets:
                picked[t] = {"status": "FAILED_NETWORK", "note": objects[0]["_error"]}
            continue
        parsed = []
        for obj in objects:
            key = obj["Key"]
            if f"_{goes_gnum(sat)}_" not in key or not key.endswith(".nc"):
                continue
            m = re.search(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})", key)
            if not m:
                continue
            st = datetime(int(m.group(1)), 1, 1, tzinfo=timezone.utc) + timedelta(days=int(m.group(2))-1, hours=int(m.group(3)), minutes=int(m.group(4)), seconds=int(m.group(5)))
            parsed.append((st, obj))
        for t in day_targets:
            candidates = []
            for st, obj in parsed:
                if st.hour != t.hour:
                    continue
                diff = abs((st.replace(minute=0, second=0, microsecond=0) - t).total_seconds())
                candidates.append((diff, st, obj))
            if not candidates:
                picked[t] = {"status": "FAILED_REMOTE_MISSING", "note": f"no_s3_object_day:{GOES_PRODUCTS[product]}/{t:%Y}/{t.timetuple().tm_yday:03d}/{t:%H}"}
            else:
                _, st, obj = sorted(candidates, key=lambda x: (x[0], x[2]["Key"]))[0]
                picked[t] = {"status": "FOUND", "bucket": goes_bucket(sat), "remote_key_or_product_id": obj["Key"], "remote_name": Path(obj["Key"]).name, "actual_start_time": iso(st), "size_bytes": obj.get("Size", "")}
    return picked


def load_eum_creds() -> tuple[str, str, str]:
    key = os.environ.get("EUMETSAT_CONSUMER_KEY", "").strip()
    secret = os.environ.get("EUMETSAT_CONSUMER_SECRET", "").strip()
    token = os.environ.get("EUMETSAT_API_TOKEN", "").strip()
    if CRED_FILE.exists():
        text = CRED_FILE.read_text(encoding="utf-8", errors="ignore")
        def find(*names):
            for name in names:
                m = re.search(rf"{name}\s*[:=]\s*(.+)", text, re.I)
                if m:
                    return m.group(1).strip().strip("'\"")
            return ""
        key = key or find("consumer\\s*key", "key")
        secret = secret or find("consumer\\s*secret", "secret")
        token = token or find("api\\s*token", "token")
    return key, secret, token


_bearer = ""
def bearer() -> str:
    global _bearer
    if _bearer:
        return _bearer
    key, secret, token = load_eum_creds()
    if token and token.count(".") >= 1:
        _bearer = token
        return _bearer
    if not key or not secret:
        raise RuntimeError("missing_eumetsat_credentials")
    basic = base64.b64encode(f"{key}:{secret}".encode()).decode()
    r = requests.post(EUM_TOKEN_URL, headers={"Authorization": f"Basic {basic}"}, data={"grant_type": "client_credentials"}, timeout=60)
    r.raise_for_status()
    _bearer = r.json()["access_token"]
    return _bearer


def eum_search(collection_id: str, target: datetime) -> list[dict[str, Any]]:
    params = {
        "format": "json", "pi": collection_id, "si": 0, "c": 100,
        "dtstart": iso(target - timedelta(minutes=10)), "dtend": iso(target + timedelta(minutes=25)),
    }
    r = requests.get(EUM_SEARCH_URL, headers={"Authorization": f"Bearer {bearer()}"}, params=params, timeout=90)
    if r.status_code in (401, 403):
        raise RuntimeError(f"auth_failed:{r.status_code}")
    r.raise_for_status()
    return list((r.json() or {}).get("features") or [])


def eum_search_window(collection_id: str, start: datetime, end: datetime, count: int = 1000) -> list[dict[str, Any]]:
    params = {"format": "json", "pi": collection_id, "si": 0, "c": count, "dtstart": iso(start), "dtend": iso(end)}
    r = requests.get(EUM_SEARCH_URL, headers={"Authorization": f"Bearer {bearer()}"}, params=params, timeout=120)
    if r.status_code in (401, 403):
        raise RuntimeError(f"auth_failed:{r.status_code}")
    r.raise_for_status()
    return list((r.json() or {}).get("features") or [])


def eum_feature_id(f: dict[str, Any]) -> str:
    p = f.get("properties") or {}
    return str(f.get("id") or p.get("identifier") or p.get("title") or "")


def eum_feature_time(f: dict[str, Any], which: str) -> datetime | None:
    p = f.get("properties") or {}
    date = p.get("date")
    if isinstance(date, str) and "/" in date:
        part = date.split("/")[0 if which == "start" else 1]
        return parse_time(part)
    for k in ([which, "beginposition", "sensing_start"] if which == "start" else [which, "endposition", "sensing_end"]):
        if p.get(k):
            return parse_time(p[k])
    return None


def eum_size(f: dict[str, Any]) -> Any:
    p = f.get("properties") or {}
    info = p.get("productInformation") or {}
    return info.get("size") or p.get("size") or ""


def discover_meteosat_collections() -> dict[tuple[str, str], list[str]]:
    # Verified historical IDs are used as seeds; each seed is tested by inventory search before use.
    seeds = {}
    for sat, suffix in [("Meteosat-0deg", ""), ("Meteosat-IODC", "-IODC")]:
        seeds[(sat, "CTTH")] = [f"EO:EUM:DAT:MSG:CTTH{suffix}", f"EO:EUM:DAT:MSG:CTH{suffix}"]
        seeds[(sat, "CT")] = [f"EO:EUM:DAT:MSG:CT{suffix}"]
        seeds[(sat, "OCA")] = [f"EO:EUM:DAT:MSG:OCA{suffix}"]
        seeds[(sat, "CMIC")] = [f"EO:EUM:DAT:MSG:CMIC{suffix}"]
    return seeds


def pick_meteosat_feature(collections: list[str], target: datetime) -> dict[str, Any]:
    last_note = ""
    for cid in collections:
        try:
            feats = eum_search(cid, target)
        except Exception as exc:
            last_note = f"{cid}:{type(exc).__name__}:{exc}"
            continue
        if not feats:
            last_note = f"{cid}:empty"
            continue
        enriched = []
        for f in feats:
            st, en = eum_feature_time(f, "start"), eum_feature_time(f, "end")
            diff = abs(((st or target) - target).total_seconds())
            covers = bool(st and en and st <= target <= en + timedelta(minutes=20))
            enriched.append((not covers, diff, f, st, en, cid))
        _, _, f, st, en, cid = sorted(enriched, key=lambda x: (x[0], x[1], eum_feature_id(x[2])))[0]
        return {"status": "FOUND", "collection_id": cid, "remote_key_or_product_id": eum_feature_id(f), "remote_name": eum_feature_id(f), "actual_start_time": iso(st) if st else "", "actual_end_time": iso(en) if en else "", "size_bytes": eum_size(f)}
    return {"status": "NOT_FOUND_IN_COLLECTION_SEARCH", "note": last_note}


def pick_from_features(features: list[dict[str, Any]], target: datetime, cid: str) -> dict[str, Any] | None:
    enriched = []
    for f in features:
        st, en = eum_feature_time(f, "start"), eum_feature_time(f, "end")
        if st and st.date() != target.date() and abs((st - target).total_seconds()) > 7200:
            continue
        diff = abs(((st or target) - target).total_seconds())
        covers = bool(st and en and st <= target <= en + timedelta(minutes=20))
        enriched.append((not covers, diff, f, st, en))
    if not enriched:
        return None
    _, _, f, st, en = sorted(enriched, key=lambda x: (x[0], x[1], eum_feature_id(x[2])))[0]
    return {"status": "FOUND", "collection_id": cid, "remote_key_or_product_id": eum_feature_id(f), "remote_name": eum_feature_id(f), "actual_start_time": iso(st) if st else "", "actual_end_time": iso(en) if en else "", "size_bytes": eum_size(f)}


def meteosat_day_index(collections: list[str], targets: list[datetime]) -> dict[datetime, dict[str, Any]]:
    by_day: dict[str, list[datetime]] = {}
    for t in targets:
        by_day.setdefault(t.strftime("%Y%m%d"), []).append(t)
    out: dict[datetime, dict[str, Any]] = {}
    for day_key, day_targets in by_day.items():
        day0 = day_targets[0].replace(hour=0, minute=0, second=0, microsecond=0)
        day1 = day0 + timedelta(days=1, minutes=20)
        day_features = []
        found_cid = ""
        notes = []
        for cid in collections:
            try:
                feats = eum_search_window(cid, day0 - timedelta(minutes=10), day1)
            except Exception as exc:
                notes.append(f"{cid}:{type(exc).__name__}:{exc}")
                continue
            if feats:
                day_features = feats
                found_cid = cid
                break
            notes.append(f"{cid}:empty")
        for t in day_targets:
            picked = pick_from_features(day_features, t, found_cid) if day_features else None
            out[t] = picked or {"status": "NOT_FOUND_IN_COLLECTION_SEARCH", "note": ";".join(notes[-3:])}
    return out


def build_goes(df: pd.DataFrame) -> list[dict[str, Any]]:
    rows = []
    client = s3_client()
    for sat in ["GOES-16", "GOES-18"]:
        times = target_intersection(df, sat, ["ACMF", "ACHAF"])
        log(f"{sat}: {len(times)} target hours from ACMF/ACHAF intersection")
        for product in GOES_PRODUCTS:
            indexed = goes_day_index(client, sat, product, times)
            for target in times:
                found = indexed.get(target) or {"status": "FAILED_REMOTE_MISSING", "note": "not_indexed"}
                name = found.get("remote_name") or f"{sat}_{product}_{target:%Y%m%d%H}.nc"
                rows.append({"priority": GOES_PRIORITY[product], "target_time_utc": iso(target), "satellite": sat, "product": product, "provider": "NOAA", "remote_type": "s3", "collection_id": GOES_PRODUCTS[product], "status": found["status"], "local_path": local_path(sat, product, target, name), **found})
            log(f"{sat} {product}: indexed {len(indexed)} hours")
    return rows


def build_meteosat(df: pd.DataFrame) -> list[dict[str, Any]]:
    rows = []
    collections = discover_meteosat_collections()
    for sat in ["Meteosat-0deg", "Meteosat-IODC"]:
        times = target_intersection(df, sat, ["CLM", "CTH"])
        log(f"{sat}: {len(times)} target hours from CLM/CTH intersection")
        for product, pri in MET_PRODUCTS.items():
            indexed = meteosat_day_index(collections[(sat, product)], times)
            for target in times:
                found = indexed.get(target) or {"status": "NOT_FOUND_IN_COLLECTION_SEARCH", "note": "not_indexed"}
                name = (found.get("remote_name") or f"{sat}_{product}_{target:%Y%m%d%H}").replace("/", "_")
                if not name.lower().endswith(".zip"):
                    name += ".zip"
                rows.append({"priority": pri, "target_time_utc": iso(target), "satellite": sat, "product": product, "provider": "EUMETSAT", "remote_type": "eumetsat", "status": found["status"], "local_path": local_path(sat, product, target, name), **found})
            log(f"{sat} {product}: indexed {len(indexed)} hours")
    return rows


def merge_and_write(goes: list[dict[str, Any]], met: list[dict[str, Any]]) -> None:
    all_rows = sorted(goes + met, key=lambda r: (int(r.get("priority") or 9), r.get("satellite",""), r.get("product",""), r.get("target_time_utc","")))
    write_csv(OUT_DIR / "priority_download_manifest_all.csv", all_rows)
    write_csv(OUT_DIR / "goes_v1_download_manifest.csv", [r for r in all_rows if str(r.get("satellite","")).startswith("GOES")])
    write_csv(OUT_DIR / "meteosat_v1_download_manifest.csv", [r for r in all_rows if str(r.get("satellite","")).startswith("Meteosat")])
    if all_rows:
        summary = pd.DataFrame(all_rows).groupby(["satellite","product","status"], dropna=False).size().reset_index(name="count")
        summary.to_csv(OUT_DIR / "manifest_summary.csv", index=False, encoding="utf-8-sig")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-meteosat", action="store_true")
    ap.add_argument("--skip-goes", action="store_true")
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = read_existing()
    goes = []
    met = []
    if not args.skip_goes:
        goes = build_goes(df)
        merge_and_write(goes, met)
        log(f"wrote GOES manifest rows={len(goes)}")
    elif (OUT_DIR / "goes_v1_download_manifest.csv").exists():
        goes = pd.read_csv(OUT_DIR / "goes_v1_download_manifest.csv", encoding="utf-8-sig").to_dict("records")
    if not args.skip_meteosat:
        try:
            met = build_meteosat(df)
        except Exception as exc:
            met = [{"priority": 1, "satellite": "Meteosat", "product": "ALL", "provider": "EUMETSAT", "remote_type": "eumetsat", "status": "FAILED_AUTH" if "credential" in str(exc).lower() or "auth" in str(exc).lower() else "FAILED_NETWORK", "note": f"{type(exc).__name__}: {exc}"}]
    elif (OUT_DIR / "meteosat_v1_download_manifest.csv").exists():
        met = pd.read_csv(OUT_DIR / "meteosat_v1_download_manifest.csv", encoding="utf-8-sig").to_dict("records")
    merge_and_write(goes, met)
    print(OUT_DIR / "priority_download_manifest_all.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
