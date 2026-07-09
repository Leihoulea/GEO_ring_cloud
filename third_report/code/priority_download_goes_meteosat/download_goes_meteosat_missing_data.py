from __future__ import annotations

import base64
import csv
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import boto3
import eumdac
from botocore import UNSIGNED
from botocore.config import Config


OUT_DIR = Path(r"D:\AAAresearch_paper\data_check_report\priority_download_run_goes_meteosat")
MANIFEST = OUT_DIR / "priority_download_manifest_all.csv"
STATUS_CSV = OUT_DIR / "priority_download_status.csv"
CRED_FILE = Path(r"D:\AAAresearch_paper\third_report\eumetsat_dataservices_API.txt")
CHUNK = 1024 * 1024
MAX_WORKERS = int(os.environ.get("PRIORITY_DOWNLOAD_WORKERS", "6"))
RETRIES = int(os.environ.get("PRIORITY_DOWNLOAD_RETRIES", "5"))
MIN_SIZE = 1024
STATUS_FIELDS = [
    "priority","target_time_utc","satellite","product","provider","collection_id","remote_type",
    "bucket","remote_key_or_product_id","local_path","status","attempts","bytes","note",
]


def read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with tmp.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=STATUS_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in STATUS_FIELDS})
    os.replace(tmp, path)


def load_eum_creds() -> tuple[str, str]:
    key = os.environ.get("EUMETSAT_CONSUMER_KEY", "").strip()
    secret = os.environ.get("EUMETSAT_CONSUMER_SECRET", "").strip()
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
    return key, secret


def s3_client():
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""
    proxies = {"https": proxy, "http": proxy} if proxy else None
    return boto3.client("s3", config=Config(signature_version=UNSIGNED, proxies=proxies, retries={"max_attempts": 5}))


def datastore():
    key, secret = load_eum_creds()
    if not key or not secret:
        raise RuntimeError("missing_eumetsat_consumer_key_secret")
    return eumdac.DataStore(token=eumdac.AccessToken((key, secret)))


def base_result(row: dict[str, Any], status: str, attempts: int = 0, note: str = "") -> dict[str, Any]:
    p = Path(row.get("local_path", ""))
    return {**row, "status": status, "attempts": attempts, "bytes": p.stat().st_size if p.exists() else "", "note": note}


def existing_ok(row: dict[str, Any]) -> bool:
    p = Path(row["local_path"])
    if not p.exists() or p.stat().st_size < MIN_SIZE:
        return False
    expected = str(row.get("size_bytes") or "")
    if expected.isdigit() and int(expected) > MIN_SIZE and p.stat().st_size < int(expected) * 0.5:
        return False
    return True


def download_s3(row: dict[str, Any]) -> dict[str, Any]:
    if existing_ok(row):
        return base_result(row, "SKIPPED_EXISTING", 0, "existing_size_ok")
    target = Path(row["local_path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    client = s3_client()
    last = ""
    for attempt in range(1, RETRIES + 1):
        try:
            if tmp.exists():
                tmp.unlink()
            obj = client.get_object(Bucket=row["bucket"], Key=row["remote_key_or_product_id"])
            with tmp.open("wb") as f:
                for chunk in iter(lambda: obj["Body"].read(CHUNK), b""):
                    f.write(chunk)
            if tmp.stat().st_size < MIN_SIZE:
                tmp.unlink(missing_ok=True)
                return base_result(row, "FAILED_SIZE_TOO_SMALL", attempt, "downloaded file too small")
            os.replace(tmp, target)
            return base_result(row, "OK", attempt, "downloaded")
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            tmp.unlink(missing_ok=True)
            time.sleep(min(60, 2 ** attempt))
    status = "FAILED_NETWORK" if any(t in last.lower() for t in ["ssl", "timeout", "connection", "network"]) else "FAILED_UNKNOWN"
    return base_result(row, status, RETRIES, last)


def download_eum(row: dict[str, Any]) -> dict[str, Any]:
    if existing_ok(row):
        return base_result(row, "SKIPPED_EXISTING", 0, "existing_size_ok")
    target = Path(row["local_path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    last = ""
    for attempt in range(1, RETRIES + 1):
        try:
            if tmp.exists():
                tmp.unlink()
            ds = datastore()
            try:
                product = ds.get_product(row["collection_id"], row["remote_key_or_product_id"])
            except Exception:
                collection = ds.get_collection(row["collection_id"])
                found = list(collection.search(query=row["remote_key_or_product_id"]))
                if not found:
                    return base_result(row, "FAILED_REMOTE_MISSING", attempt, "product not found by id")
                product = found[0]
            with product.open() as src, tmp.open("wb") as dst:
                while True:
                    b = src.read(CHUNK)
                    if not b:
                        break
                    dst.write(b)
            if tmp.stat().st_size < MIN_SIZE:
                tmp.unlink(missing_ok=True)
                return base_result(row, "FAILED_SIZE_TOO_SMALL", attempt, "downloaded file too small")
            os.replace(tmp, target)
            return base_result(row, "OK", attempt, "downloaded")
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            tmp.unlink(missing_ok=True)
            if any(t in last.lower() for t in ["401", "403", "auth", "token", "unauthorized"]):
                return base_result(row, "FAILED_AUTH", attempt, last)
            time.sleep(min(60, 2 ** attempt))
    status = "FAILED_NETWORK" if any(t in last.lower() for t in ["ssl", "timeout", "connection", "network"]) else "FAILED_UNKNOWN"
    return base_result(row, status, RETRIES, last)


def handle(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("status") not in {"FOUND", "found"}:
        return base_result(row, row.get("status") or "NOT_FOUND_IN_COLLECTION_SEARCH", 0, row.get("note", "not in manifest as FOUND"))
    if row["remote_type"] == "s3":
        return download_s3(row)
    if row["remote_type"] == "eumetsat":
        return download_eum(row)
    return base_result(row, "FAILED_UNKNOWN", 0, "unknown remote_type")


def main() -> int:
    rows = sorted(read_csv(MANIFEST), key=lambda r: (int(r.get("priority") or 9), r.get("satellite",""), r.get("product",""), r.get("target_time_utc","")))
    results: list[dict[str, Any]] = []
    done = 0
    for priority in sorted({int(r.get("priority") or 9) for r in rows}):
        batch = [r for r in rows if int(r.get("priority") or 9) == priority]
        print(f"priority {priority} start rows={len(batch)}", flush=True)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futs = [ex.submit(handle, r) for r in batch]
            for fut in as_completed(futs):
                res = fut.result()
                results.append(res)
                done += 1
                if done % 20 == 0:
                    write_csv(STATUS_CSV, results)
                    print(f"{done}/{len(rows)} P{priority} {res.get('satellite')} {res.get('product')} {res.get('status')}", flush=True)
        write_csv(STATUS_CSV, results)
        print(f"priority {priority} done", flush=True)
    write_csv(STATUS_CSV, sorted(results, key=lambda r: (r.get("satellite",""), r.get("product",""), r.get("target_time_utc",""))))
    print(STATUS_CSV)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
