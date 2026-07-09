from __future__ import annotations

import csv
import json
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any

import netCDF4
import numpy as np
import pandas as pd


OUT_DIR = Path(r"D:\AAAresearch_paper\data_check_report\priority_download_run_goes_meteosat")
MANIFEST = OUT_DIR / "priority_download_manifest_all.csv"
STATUS_CSV = OUT_DIR / "priority_download_status.csv"
VERIFY_CSV = OUT_DIR / "priority_download_verification.csv"
SMOKE_CSV = OUT_DIR / "priority_download_variable_smoke_test.csv"
REPORT_MD = OUT_DIR / "priority_download_goes_meteosat_report.md"
MIN_SIZE = 1024

TARGETS = {
    "ACTPF": {"cloud_phase": ["Phase", "ACTP", "cloud_phase"], "quality_flag": ["DQF"], "projection_x": ["x"], "projection_y": ["y"], "projection": ["goes_imager_projection"]},
    "CTPF": {"cloud_top_pressure": ["PRES", "CTP"], "quality_flag": ["DQF"], "projection_x": ["x"], "projection_y": ["y"], "projection": ["goes_imager_projection"]},
    "ACHTF": {"cloud_top_temperature": ["TEMP", "ACHT", "CTT"], "quality_flag": ["DQF"], "projection_x": ["x"], "projection_y": ["y"], "projection": ["goes_imager_projection"]},
    "CODF": {"cloud_optical_depth": ["COD", "COT"], "quality_flag": ["DQF"], "projection_x": ["x"], "projection_y": ["y"], "projection": ["goes_imager_projection"]},
    "CPSF": {"cloud_particle_size": ["CPS", "CER", "PSD"], "quality_flag": ["DQF"], "projection_x": ["x"], "projection_y": ["y"], "projection": ["goes_imager_projection"]},
    "CTTH": {"cloud_top_height": ["ctoph", "height"], "cloud_top_temperature": ["ctt", "temperature"], "cloud_top_pressure": ["ctp", "pressure"], "quality_flag": ["qi", "quality"]},
    "CT": {"cloud_type": ["type", "phase", "ct"], "quality_flag": ["quality", "flag", "qi"]},
    "OCA": {"cloud_optical_thickness": ["cot", "optical"], "cloud_effective_radius": ["cer", "effective", "radius"], "quality_flag": ["quality", "flag"]},
    "CMIC": {"cloud_optical_thickness": ["cot", "optical"], "cloud_effective_radius": ["cer", "effective", "radius"], "quality_flag": ["quality", "flag"]},
}


def read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = []
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    if not fields:
        fields = ["empty"]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def verify_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        p = Path(r.get("local_path", ""))
        if r.get("status") in {"FOUND", "found"}:
            status = "MISSING_NOT_DOWNLOADED"
        else:
            status = r.get("status", "")
        ok = p.exists() and p.stat().st_size >= MIN_SIZE
        out.append({**r, "file_exists": ok, "actual_size": p.stat().st_size if p.exists() else 0, "verification_status": "OK" if ok else status})
    return out


def norm(s: str) -> str:
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def match_vars(names: list[str], product: str) -> dict[str, str]:
    out = {}
    normed = {n: norm(n) for n in names}
    for std, candidates in TARGETS.get(product, {}).items():
        for cand in candidates:
            c = norm(cand)
            for n, nn in normed.items():
                if n in out.values():
                    continue
                if c == nn or (len(c) > 2 and c in nn) or (len(nn) > 2 and nn in c):
                    out[std] = n
                    break
            if std in out:
                break
    return out


def smoke_netcdf(row: dict[str, Any]) -> dict[str, Any]:
    p = Path(row["local_path"])
    try:
        with netCDF4.Dataset(p) as ds:
            names = list(ds.variables)
            matched = match_vars(names, row["product"])
            shapes = {std: str(tuple(ds.variables[name].shape)) for std, name in matched.items()}
            return {**row, "open_status": "OK", "matched_variables": json.dumps(matched), "matched_shapes": json.dumps(shapes), "missing_targets": ",".join(sorted(set(TARGETS.get(row["product"], {})) - set(matched)))}
    except Exception as exc:
        return {**row, "open_status": "FAILED_OPEN", "error": f"{type(exc).__name__}: {exc}"}


def smoke_meteosat(row: dict[str, Any]) -> dict[str, Any]:
    import cfgrib
    p = Path(row["local_path"])
    try:
        names = []
        shapes = {}
        with zipfile.ZipFile(p) as zf:
            gribs = [n for n in zf.namelist() if n.lower().endswith((".grb", ".grib", ".grib2"))]
            if not gribs:
                return {**row, "open_status": "FAILED_OPEN", "error": "no_grib_inside_zip"}
            import tempfile
            for member in gribs[:2]:
                with tempfile.NamedTemporaryFile(suffix=".grib", delete=False) as tmp:
                    tmp.write(zf.read(member))
                    tmp_path = Path(tmp.name)
                try:
                    for ds in cfgrib.open_datasets(str(tmp_path), indexpath=""):
                        try:
                            for v in ds.data_vars:
                                names.append(v)
                                shapes[v] = str(tuple(ds[v].shape))
                                attrs = ds[v].attrs
                                if attrs.get("long_name"):
                                    names.append(str(attrs["long_name"]))
                                if attrs.get("GRIB_shortName"):
                                    names.append(str(attrs["GRIB_shortName"]))
                        finally:
                            ds.close()
                finally:
                    tmp_path.unlink(missing_ok=True)
        matched = match_vars(names, row["product"])
        return {**row, "open_status": "OK", "matched_variables": json.dumps(matched), "matched_shapes": json.dumps(shapes), "missing_targets": ",".join(sorted(set(TARGETS.get(row["product"], {})) - set(matched)))}
    except Exception as exc:
        return {**row, "open_status": "FAILED_OPEN", "error": f"{type(exc).__name__}: {exc}"}


def choose_samples(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ok = [r for r in rows if Path(r.get("local_path", "")).exists() and Path(r.get("local_path", "")).stat().st_size >= MIN_SIZE]
    by = defaultdict(list)
    for r in ok:
        by[(r.get("satellite"), r.get("product"))].append(r)
    samples = []
    for key, vals in by.items():
        vals = sorted(vals, key=lambda r: r.get("target_time_utc", ""))
        idxs = [0, len(vals)//2, len(vals)-1] if len(vals) >= 3 else list(range(len(vals)))
        for i in sorted(set(idxs)):
            samples.append(vals[i])
    return samples


def summarize_status(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(rows).groupby(["satellite","product","verification_status"], dropna=False).size().reset_index(name="count")

def md_table_df(df: pd.DataFrame, cols: list[str] | None = None, limit: int = 500) -> str:
    if df is None or len(df) == 0:
        return "No rows."
    if cols is None:
        cols = list(df.columns)
    rows = df[cols].head(limit).fillna("").astype(str).to_dict("records")
    lines = ["|" + "|".join(cols) + "|", "|" + "|".join(["---"] * len(cols)) + "|"]
    for r in rows:
        lines.append("|" + "|".join(str(r.get(c, "")).replace("|", "/").replace("\n", " ") for c in cols) + "|")
    if len(df) > limit:
        lines.append(f"\nShowing first {limit} of {len(df)} rows.")
    return "\n".join(lines)


def class_for_products(verify: list[dict[str, Any]], products: list[str], satellites: list[str]) -> str:
    sub = [r for r in verify if r.get("satellite") in satellites and r.get("product") in products]
    if not sub:
        return "FAIL"
    ok = [r for r in sub if r.get("verification_status") == "OK"]
    found_or_missing = [r for r in sub if r.get("status") not in {"NOT_FOUND_IN_COLLECTION_SEARCH", "FAILED_REMOTE_MISSING"}]
    if len(ok) == len(sub):
        return "OK"
    if ok:
        return "PARTIAL"
    return "FAIL"


def make_report(manifest: list[dict[str, Any]], verify: list[dict[str, Any]], smoke: list[dict[str, Any]]) -> str:
    vdf = summarize_status(verify)
    lines = ["# GOES + Meteosat Priority Download Report", "", "## Planned Products", "- GOES P1: ACTPF, CTPF, ACHTF. P2: CODF, CPSF.", "- Meteosat P1: CTTH, CT. P2: OCA, CMIC.", ""]
    lines.append("## Counts By Product")
    lines.append(md_table_df(vdf))
    lines.append("")
    lines.append("## Smoke Test")
    if smoke:
        sdf = pd.DataFrame(smoke)
        cols = ["satellite","product","target_time_utc","open_status","matched_variables","missing_targets"]
        lines.append(md_table_df(sdf, cols))
    else:
        lines.append("No downloaded/existing files available for smoke test.")
    lines.append("")
    lines.append("## Readiness")
    goes = class_for_products(verify, ["ACTPF","CTPF","ACHTF"], ["GOES-16","GOES-18"])
    met = class_for_products(verify, ["CTTH","CT"], ["Meteosat-0deg","Meteosat-IODC"])
    micro = class_for_products(verify, ["OCA","CMIC","CODF","CPSF"], ["GOES-16","GOES-18","Meteosat-0deg","Meteosat-IODC"])
    if not any(r.get("product") in {"OCA","CMIC","CODF","CPSF"} for r in verify):
        micro = "NOT_ATTEMPTED"
    lines.append(f"- GOES v1: {goes}")
    lines.append(f"- Meteosat v1: {met}")
    lines.append(f"- microphysics: {micro}")
    lines.append("- Recommendation: start GOES/Meteosat v1 standardization tomorrow only for products marked OK or PARTIAL with successful smoke-test variables; keep missing products in the retry queue.")
    return "\n".join(lines)


def main() -> int:
    manifest = read_csv(MANIFEST)
    status = read_csv(STATUS_CSV)
    rows = status if status else manifest
    verify = verify_rows(rows)
    write_csv(VERIFY_CSV, verify)
    smoke = []
    for r in choose_samples(verify):
        if r.get("remote_type") == "s3":
            smoke.append(smoke_netcdf(r))
        elif r.get("remote_type") == "eumetsat":
            smoke.append(smoke_meteosat(r))
    write_csv(SMOKE_CSV, smoke)
    REPORT_MD.write_text(make_report(manifest, verify, smoke), encoding="utf-8-sig")
    goes = class_for_products(verify, ["ACTPF","CTPF","ACHTF"], ["GOES-16","GOES-18"])
    met = class_for_products(verify, ["CTTH","CT"], ["Meteosat-0deg","Meteosat-IODC"])
    micro = class_for_products(verify, ["OCA","CMIC","CODF","CPSF"], ["GOES-16","GOES-18","Meteosat-0deg","Meteosat-IODC"])
    if not any(r.get("product") in {"OCA","CMIC","CODF","CPSF"} for r in verify):
        micro = "NOT_ATTEMPTED"
    print(f"GOES v1: {goes}")
    print(f"Meteosat v1: {met}")
    print(f"microphysics: {micro}")
    print(f"final_report: {REPORT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
