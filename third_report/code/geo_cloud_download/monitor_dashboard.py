from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

CORE_CODE_ROOT = Path(__file__).resolve().parents[1] / "geo_ring_cloud_stage1"
if str(CORE_CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_CODE_ROOT))

from geo_ring_cloud.paths import EXTERNAL_GEO_CLOUD_ROOT, THIRD_REPORT_ROOT  # noqa: E402


PROJECT_ROOT = THIRD_REPORT_ROOT
DOWNLOAD_ROOT = EXTERNAL_GEO_CLOUD_ROOT
CODE_DIR = PROJECT_ROOT / "code" / "geo_cloud_download"
MANIFEST_DIR = DOWNLOAD_ROOT / "manifests"
LOG_DIR = DOWNLOAD_ROOT / "logs"

S3_LOG = LOG_DIR / "download_s3_range.log"
METEOSAT_INVENTORY_LOG = LOG_DIR / "meteosat_inventory.log"
METEOSAT_DOWNLOAD_LOG = LOG_DIR / "download_meteosat_range.log"
S3_STATUS = CODE_DIR / "goes_himawari_march2024_repair_status.json"
METEOSAT_STATUS = CODE_DIR / "meteosat_march2024_status.json"
METEOSAT_DIRECT_RUN_LOG = CODE_DIR / "meteosat_march2024_direct_run.log"

METEOSAT_EXPECTED_ROWS = 31 * 24 * 4
METEOSAT_EXPECTED_DAYS = 31
_PART_SNAPSHOT: dict[str, tuple[int, float]] = {}

S3_EVENT_RE = re.compile(
    r"^(?P<ts>\S+)\s+(?P<num>\d+)/(?P<total>\d+)\s+(?P<status>\w+)\s+"
    r"(?P<platform>GOES-\d+|Himawari-9)\s+(?P<product>\S+)\s+"
    r"(?P<target>\S+)\s+(?P<note>.*)$"
)
S3_START_RE = re.compile(
    r"download_s3_range_start .*rows=(?P<rows>\d+)\s+skipped_existing=(?P<skipped>\d+)\s+pending=(?P<pending>\d+)"
)
MET_DOWNLOAD_RE = re.compile(
    r"^(?P<ts>\S+)\s+(?P<num>\d+)/(?P<total>\d+)\s+(?P<status>\w+)\s+"
    r"(?P<platform>Meteosat-\S+)\s+(?P<product>\S+)\s+"
    r"(?P<target>\S+)\s+(?P<note>.*)$"
)
MET_INV_RE = re.compile(r"^(?P<ts>\S+)\s+inventoried_through=(?P<target>\S+)")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def tail_lines(path: Path, max_lines: int = 5000) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 1024 * 1024))
            text = handle.read().decode("utf-8", errors="ignore")
        return text.splitlines()[-max_lines:]
    except Exception:
        return []


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def read_manifest_sizes(path: Path) -> dict[tuple[str, str, str], int]:
    sizes: dict[tuple[str, str, str], int] = {}
    if not path.exists():
        return sizes
    try:
        with path.open("r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                size = row.get("size_bytes", "")
                if str(size).isdigit():
                    key = (row.get("platform", ""), row.get("product", ""), row.get("target_time_utc", ""))
                    sizes[key] = int(size)
    except Exception:
        pass
    return sizes


def format_bytes(value: float | int | None) -> str:
    if value is None:
        return ""
    value = float(value)
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    return f"{value:.1f} {units[idx]}"


def summarize_events(events: list[dict], total: int | None, size_map: dict[tuple[str, str, str], int]) -> dict:
    now = utc_now()
    ok_events = [event for event in events if event["status"] == "downloaded"]
    bad_events = [event for event in events if event["status"] == "corrupt"]
    completed = max([event["num"] for event in events], default=0)
    if total is None:
        total = max([event["total"] for event in events], default=0)

    recent_1h = []
    recent_10m = []
    for event in ok_events:
        age = (now - event["time"]).total_seconds()
        if age <= 3600:
            recent_1h.append(event)
        if age <= 600:
            recent_10m.append(event)

    def bytes_for(items: list[dict]) -> int:
        return sum(size_map.get((item["platform"], item["product"], item["target"]), 0) for item in items)

    completed_bytes = bytes_for(ok_events)
    last_hour_bytes = bytes_for(recent_1h)
    last_10m_bytes = bytes_for(recent_10m)
    last_event = events[-1] if events else None
    by_platform = Counter(event["platform"] for event in ok_events)
    by_product = Counter(f"{event['platform']} {event['product']}" for event in ok_events)

    remaining = max(total - completed, 0) if total else 0
    per_hour = len(recent_1h)
    eta_hours = remaining / per_hour if per_hour > 0 else None

    return {
        "total": total,
        "completed": completed,
        "failed": len(bad_events),
        "percent": round((completed / total * 100), 2) if total else 0,
        "recent_10m_files": len(recent_10m),
        "recent_1h_files": len(recent_1h),
        "files_per_hour": round(per_hour, 2),
        "mbps_10m": round(last_10m_bytes / 600 / (1024 * 1024), 3),
        "mbps_1h": round(last_hour_bytes / 3600 / (1024 * 1024), 3),
        "completed_bytes": completed_bytes,
        "completed_bytes_label": format_bytes(completed_bytes),
        "eta_hours": round(eta_hours, 2) if eta_hours is not None else None,
        "last_event": last_event,
        "by_platform": dict(by_platform),
        "by_product": dict(by_product),
    }


def parse_download_log(path: Path, pattern: re.Pattern, after_last_start: bool = False) -> list[dict]:
    lines = tail_lines(path, 10000)
    if after_last_start:
        start_indexes = [idx for idx, line in enumerate(lines) if "download_s3_range_start" in line]
        if start_indexes:
            lines = lines[start_indexes[-1] + 1 :]
    events = []
    for line in lines:
        match = pattern.match(line)
        if not match:
            continue
        when = parse_time(match.group("ts"))
        if not when:
            continue
        events.append(
            {
                "time": when,
                "ts": match.group("ts"),
                "num": int(match.group("num")),
                "total": int(match.group("total")),
                "status": match.group("status"),
                "platform": match.group("platform"),
                "product": match.group("product"),
                "target": match.group("target"),
                "note": match.group("note"),
            }
        )
    events.sort(key=lambda item: item["time"])
    return events


def parse_s3_start() -> dict:
    lines = tail_lines(S3_LOG, 10000)
    for line in reversed(lines):
        if "download_s3_range_start" not in line:
            continue
        match = S3_START_RE.search(line)
        if not match:
            continue
        return {
            "total_rows": int(match.group("rows")),
            "skipped_existing": int(match.group("skipped")),
            "pending": int(match.group("pending")),
            "line": line,
        }
    return {"total_rows": 0, "skipped_existing": 0, "pending": 0, "line": ""}


def parse_meteosat_inventory() -> dict:
    lines = tail_lines(METEOSAT_INVENTORY_LOG, 2000)
    start_indexes = [idx for idx, line in enumerate(lines) if "meteosat_inventory_start" in line]
    starts = [lines[idx] for idx in start_indexes]
    if start_indexes:
        lines = lines[start_indexes[-1] + 1 :]
    events = []
    for line in lines:
        match = MET_INV_RE.match(line)
        if not match:
            continue
        when = parse_time(match.group("ts"))
        if not when:
            continue
        events.append({"time": when, "ts": match.group("ts"), "target": match.group("target")})
    events.sort(key=lambda item: item["time"])
    march_events = [event for event in events if event["target"].startswith("2024-03-")]
    last = march_events[-1] if march_events else None
    days_done = len({event["target"][:10] for event in march_events})
    recent = [event for event in march_events if (utc_now() - event["time"]).total_seconds() <= 3600]
    days_per_hour = len(recent)
    remaining_days = max(METEOSAT_EXPECTED_DAYS - days_done, 0)
    eta_hours = remaining_days / days_per_hour if days_per_hour else None
    return {
        "started": bool(starts),
        "days_done": days_done,
        "expected_days": METEOSAT_EXPECTED_DAYS,
        "rows_done_estimate": days_done * 24 * 4,
        "expected_rows": METEOSAT_EXPECTED_ROWS,
        "percent": round(days_done / METEOSAT_EXPECTED_DAYS * 100, 2),
        "days_per_hour": days_per_hour,
        "eta_hours": round(eta_hours, 2) if eta_hours is not None else None,
        "last_event": last,
    }


def process_snapshot() -> dict:
    script = (
        "Get-Process -Id 30312,50000,39848 -ErrorAction SilentlyContinue | "
        "Select-Object Id,ProcessName,CPU,StartTime | ConvertTo-Json -Compress"
    )
    try:
        output = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-Command", script],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
        if not output:
            return {"known": []}
        data = json.loads(output)
        if isinstance(data, dict):
            data = [data]
        return {"known": data}
    except Exception:
        return {"known": []}


def disk_snapshot() -> dict:
    try:
        usage = shutil.disk_usage(str(DOWNLOAD_ROOT.anchor or DOWNLOAD_ROOT))
        return {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "total_label": format_bytes(usage.total),
            "used_label": format_bytes(usage.used),
            "free_label": format_bytes(usage.free),
            "used_percent": round(usage.used / usage.total * 100, 2),
        }
    except Exception:
        return {}


def active_part_snapshot() -> dict:
    global _PART_SNAPSHOT
    now = time.time()
    current: dict[str, tuple[int, float]] = {}
    items = []
    try:
        files = sorted(
            DOWNLOAD_ROOT.rglob("*.part"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:20]
    except Exception:
        files = []

    total_rate = 0.0
    for path in files:
        try:
            stat = path.stat()
        except Exception:
            continue
        key = str(path)
        current[key] = (stat.st_size, now)
        prev = _PART_SNAPSHOT.get(key)
        rate = None
        if prev:
            prev_size, prev_time = prev
            elapsed = max(now - prev_time, 0.001)
            rate = max(stat.st_size - prev_size, 0) / elapsed
            total_rate += rate
        items.append(
            {
                "path": key,
                "name": path.name,
                "size": stat.st_size,
                "size_label": format_bytes(stat.st_size),
                "rate_bps": rate,
                "rate_label": f"{format_bytes(rate)}/s" if rate is not None else "measuring",
                "last_write": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        )
    _PART_SNAPSHOT = current
    return {
        "count": len(items),
        "total_rate_bps": total_rate if any(item["rate_bps"] is not None for item in items) else None,
        "total_rate_label": f"{format_bytes(total_rate)}/s"
        if any(item["rate_bps"] is not None for item in items)
        else "measuring",
        "items": items,
    }


def current_status() -> dict:
    s3_sizes = read_manifest_sizes(MANIFEST_DIR / "manifest_inventory.csv")
    met_sizes = read_manifest_sizes(MANIFEST_DIR / "manifest_meteosat_inventory.csv")
    s3_start = parse_s3_start()
    s3_events = parse_download_log(S3_LOG, S3_EVENT_RE, after_last_start=True)
    met_events = parse_download_log(METEOSAT_DOWNLOAD_LOG, MET_DOWNLOAD_RE)
    s3_summary = summarize_events(s3_events, s3_start.get("pending") or None, s3_sizes)
    if s3_start.get("total_rows"):
        s3_summary["current_batch_completed"] = s3_summary["completed"]
        s3_summary["skipped_existing"] = s3_start["skipped_existing"]
        s3_summary["overall_completed"] = s3_start["skipped_existing"] + s3_summary["completed"]
        s3_summary["overall_total"] = s3_start["total_rows"]
        s3_summary["overall_percent"] = round(
            s3_summary["overall_completed"] / s3_start["total_rows"] * 100, 2
        )
    met_download_summary = summarize_events(met_events, None, met_sizes)

    status = {
        "generated_at": utc_now().isoformat().replace("+00:00", "Z"),
        "disk": disk_snapshot(),
        "active_parts": active_part_snapshot(),
        "processes": process_snapshot(),
        "s3": {
            "status": read_json(S3_STATUS),
            "log": str(S3_LOG),
            "summary": s3_summary,
            "recent": s3_events[-12:],
        },
        "meteosat": {
            "status": read_json(METEOSAT_STATUS),
            "inventory": parse_meteosat_inventory(),
            "download": met_download_summary,
            "inventory_log": str(METEOSAT_INVENTORY_LOG),
            "download_log": str(METEOSAT_DOWNLOAD_LOG),
            "recent_download": met_events[-12:],
            "direct_run_log_tail": tail_lines(METEOSAT_DIRECT_RUN_LOG, 20)[-6:],
        },
    }
    return status


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GEO Cloud Download Monitor</title>
  <style>
    :root { color-scheme: light; --ink:#172126; --muted:#66747d; --line:#d8e0e4; --panel:#ffffff; --bg:#f4f7f8; --blue:#1f77b4; --green:#26845b; --amber:#b56b00; --red:#b64242; }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: "Segoe UI", Arial, sans-serif; background: var(--bg); color: var(--ink); }
    header { padding: 18px 24px 12px; border-bottom: 1px solid var(--line); background: #fff; display:flex; justify-content:space-between; gap:16px; align-items:flex-end; }
    h1 { margin: 0; font-size: 22px; font-weight: 650; letter-spacing: 0; }
    .sub { color: var(--muted); font-size: 13px; margin-top: 4px; }
    main { padding: 18px 24px 28px; display: grid; gap: 16px; }
    .grid { display:grid; grid-template-columns: repeat(12, minmax(0,1fr)); gap: 16px; }
    .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; min-width:0; }
    .span-3 { grid-column: span 3; } .span-4 { grid-column: span 4; } .span-6 { grid-column: span 6; } .span-8 { grid-column: span 8; } .span-12 { grid-column: span 12; }
    .metric { font-size: 28px; font-weight: 700; line-height: 1.1; }
    .label { color: var(--muted); font-size: 12px; margin-top: 4px; }
    .row { display:flex; align-items:center; justify-content:space-between; gap:12px; }
    .bar { height: 10px; background:#e7edf0; border-radius: 999px; overflow:hidden; margin-top:10px; }
    .fill { height:100%; border-radius:999px; transition: width .3s ease; }
    .blue { background: var(--blue); } .green { background: var(--green); } .amber { background: var(--amber); }
    table { width:100%; border-collapse:collapse; font-size: 13px; }
    th, td { text-align:left; padding:7px 6px; border-bottom:1px solid #edf1f3; white-space:nowrap; }
    th { color:var(--muted); font-weight:600; }
    code { font-family: Consolas, monospace; font-size: 12px; color:#28424d; }
    .pill { display:inline-flex; padding:3px 8px; border:1px solid var(--line); border-radius:999px; font-size:12px; color:#42515a; background:#fafcfd; }
    .ok { color: var(--green); } .warn { color: var(--amber); } .bad { color: var(--red); }
    .mini-chart { display:grid; gap:9px; margin-top:12px; }
    .mini-row { display:grid; grid-template-columns: 150px 1fr 70px; gap:10px; align-items:center; font-size:13px; }
    .mini-track { height:14px; background:#e7edf0; border-radius:999px; overflow:hidden; }
    .mini-fill { height:100%; border-radius:999px; background:var(--blue); }
    @media (max-width: 900px) { .span-3,.span-4,.span-6,.span-8 { grid-column: span 12; } header { display:block; } }
  </style>
</head>
<body>
<header>
  <div><h1>GEO Cloud Download Monitor</h1><div class="sub">GOES / Himawari / Meteosat 下载过程监控</div></div>
  <div class="sub">自动刷新：<span id="updated">--</span></div>
</header>
<main>
  <section class="grid">
    <div class="panel span-3"><div class="metric" id="s3Pct">--</div><div class="label">GOES + Himawari 当前批次进度</div><div class="bar"><div id="s3Bar" class="fill blue"></div></div></div>
    <div class="panel span-3"><div class="metric" id="metInvPct">--</div><div class="label">Meteosat inventory 进度</div><div class="bar"><div id="metInvBar" class="fill amber"></div></div></div>
    <div class="panel span-3"><div class="metric" id="metDlPct">--</div><div class="label">Meteosat 下载进度</div><div class="bar"><div id="metDlBar" class="fill green"></div></div></div>
    <div class="panel span-3"><div class="metric" id="activeSpeed">--</div><div class="label">正在写入 .part 实时速度</div><div class="bar"><div id="diskBar" class="fill green"></div></div></div>
  </section>
  <section class="grid">
    <div class="panel span-4"><div class="row"><b>GOES/Himawari</b><span class="pill" id="s3Stage">--</span></div><table id="s3Table"></table></div>
    <div class="panel span-4"><div class="row"><b>Meteosat</b><span class="pill" id="metStage">--</span></div><table id="metTable"></table></div>
    <div class="panel span-4"><div class="row"><b>进程和磁盘</b><span class="pill" id="procCount">--</span></div><table id="procTable"></table></div>
  </section>
  <section class="panel span-12"><div class="row"><b>正在下载的临时文件</b><span class="pill" id="partCount">--</span></div><table id="partTable"></table></section>
  <section class="grid">
    <div class="panel span-6"><b>最近一小时文件完成数</b><div id="fileChart" class="mini-chart"></div></div>
    <div class="panel span-6"><b>已完成文件分布</b><div id="productChart" class="mini-chart"></div></div>
  </section>
  <section class="grid">
    <div class="panel span-6"><b>GOES/Himawari 最近完成</b><table id="recentS3"></table></div>
    <div class="panel span-6"><b>Meteosat 最近下载</b><table id="recentMet"></table></div>
  </section>
  <section class="panel span-12"><b>日志位置</b><div class="sub" id="paths"></div></section>
</main>
<script>
const fmt = new Intl.NumberFormat("zh-CN");
function pct(x){ return (x || 0).toFixed(1) + "%"; }
function eta(hours){ if(hours === null || hours === undefined) return "估算中"; if(hours < 1) return Math.round(hours*60)+" 分钟"; return hours.toFixed(1)+" 小时"; }
function table(rows){ return rows.map(r=>"<tr><th>"+r[0]+"</th><td>"+r[1]+"</td></tr>").join(""); }
function setBar(id, value){ document.getElementById(id).style.width = Math.max(0, Math.min(100, value || 0)) + "%"; }
function recentRows(items){
  if(!items || !items.length) return "<tr><td class='sub'>暂无下载记录</td></tr>";
  return "<tr><th>UTC</th><th>平台</th><th>产品</th><th>目标时次</th></tr>" + items.slice().reverse().map(x=>`<tr><td>${x.ts}</td><td>${x.platform}</td><td>${x.product}</td><td>${x.target}</td></tr>`).join("");
}
function miniBars(id, labels, values, colors){
  const max = Math.max(...values, 1);
  document.getElementById(id).innerHTML = labels.map((label, i)=>{
    const w = Math.round((values[i] || 0) / max * 100);
    const color = colors[i % colors.length];
    return `<div class="mini-row"><div>${label}</div><div class="mini-track"><div class="mini-fill" style="width:${w}%;background:${color}"></div></div><div>${fmt.format(values[i] || 0)}</div></div>`;
  }).join("");
}
function upsertCharts(data){
  miniBars("fileChart", ["GOES/Himawari", "Meteosat"], [data.s3.summary.recent_1h_files || 0, data.meteosat.download.recent_1h_files || 0], ["#1f77b4","#26845b"]);
  const products = Object.assign({}, data.s3.summary.by_product || {}, data.meteosat.download.by_product || {});
  const productLabels = Object.keys(products).slice(-12);
  miniBars("productChart", productLabels, productLabels.map(k=>products[k]), ["#1f77b4","#4c9ed9","#26845b","#74b88a","#b56b00","#d19a3f"]);
}
async function refresh(){
  const data = await fetch("/api/status", {cache:"no-store"}).then(r=>r.json());
  document.getElementById("updated").textContent = data.generated_at;
  const s3 = data.s3.summary, mi = data.meteosat.inventory, md = data.meteosat.download, disk = data.disk || {};
  const parts = data.active_parts || {items:[]};
  const s3MainPct = s3.overall_percent !== undefined ? s3.overall_percent : s3.percent;
  document.getElementById("s3Pct").textContent = pct(s3MainPct); setBar("s3Bar", s3MainPct);
  document.getElementById("metInvPct").textContent = pct(mi.percent); setBar("metInvBar", mi.percent);
  document.getElementById("metDlPct").textContent = md.total ? pct(md.percent) : "等待"; setBar("metDlBar", md.percent);
  document.getElementById("activeSpeed").textContent = parts.total_rate_label || "--"; setBar("diskBar", 100-(disk.used_percent || 0));
  document.getElementById("s3Stage").textContent = (data.s3.status && data.s3.status.phase) || "running";
  document.getElementById("metStage").textContent = (data.meteosat.status && data.meteosat.status.phase) || "inventory";
  document.getElementById("s3Table").innerHTML = table([
    ["总完成", s3.overall_total ? `${fmt.format(s3.overall_completed)} / ${fmt.format(s3.overall_total)} 个文件` : `${fmt.format(s3.completed)} / ${fmt.format(s3.total || 0)} 个文件`],
    ["当前批次", `${fmt.format(s3.current_batch_completed || s3.completed)} / ${fmt.format(s3.total || 0)} 个文件`],
    ["已跳过有效文件", fmt.format(s3.skipped_existing || 0)],
    ["失败", fmt.format(s3.failed || 0)],
    ["近 10 分钟", `${fmt.format(s3.recent_10m_files)} 个，${s3.mbps_10m} MB/s`],
    ["近 1 小时", `${fmt.format(s3.recent_1h_files)} 个，${s3.mbps_1h} MB/s`],
    ["已完成体量", s3.completed_bytes_label || "--"],
    ["ETA", eta(s3.eta_hours)],
    ["最后时次", s3.last_event ? `${s3.last_event.platform} ${s3.last_event.product} ${s3.last_event.target}` : "--"]
  ]);
  document.getElementById("metTable").innerHTML = table([
    ["Inventory", `${fmt.format(mi.rows_done_estimate)} / ${fmt.format(mi.expected_rows)} 行，${mi.days_done}/${mi.expected_days} 天`],
    ["Inventory 速度", `${fmt.format(mi.days_per_hour || 0)} 天/小时`],
    ["Inventory ETA", eta(mi.eta_hours)],
    ["下载", md.total ? `${fmt.format(md.completed)} / ${fmt.format(md.total)} 个文件` : "等待 inventory 完成"],
    ["下载速度", `${md.mbps_1h || 0} MB/s`],
    ["下载 ETA", eta(md.eta_hours)],
    ["最后 inventory", mi.last_event ? mi.last_event.target : "--"]
  ]);
  const procs = (data.processes && data.processes.known) || [];
  document.getElementById("procCount").textContent = `${procs.length} known`;
  document.getElementById("procTable").innerHTML = table([
    ["E: 已用", `${disk.used_label || "--"} / ${disk.total_label || "--"} (${disk.used_percent || 0}%)`],
    ["E: 剩余", disk.free_label || "--"],
    ["进程", procs.map(p=>`${p.Id}:${p.ProcessName}`).join("<br>") || "未检测到固定 PID"],
    ["状态文件", data.meteosat.status ? data.meteosat.status.status : "--"]
  ]);
  document.getElementById("partCount").textContent = `${parts.count || 0} active`;
  document.getElementById("partTable").innerHTML = parts.items && parts.items.length
    ? "<tr><th>文件</th><th>当前大小</th><th>增长速度</th><th>更新时间 UTC</th></tr>" + parts.items.slice(0,8).map(x=>`<tr><td><code>${x.name}</code></td><td>${x.size_label}</td><td>${x.rate_label}</td><td>${x.last_write}</td></tr>`).join("")
    : "<tr><td class='sub'>当前没有 .part 文件；可能在列目录、校验、重试，或等待下一阶段。</td></tr>";
  document.getElementById("recentS3").innerHTML = recentRows(data.s3.recent);
  document.getElementById("recentMet").innerHTML = recentRows(data.meteosat.recent_download);
  document.getElementById("paths").innerHTML = `<code>${data.s3.log}</code><br><code>${data.meteosat.inventory_log}</code><br><code>${data.meteosat.download_log}</code>`;
  upsertCharts(data);
}
refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            payload = json.dumps(current_status(), default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path in {"/", "/index.html"}:
            payload = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_error(404)

    def log_message(self, fmt, *args):
        return


def main() -> int:
    port = int(os.environ.get("GEO_MONITOR_PORT", "8765"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"http://127.0.0.1:{port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
