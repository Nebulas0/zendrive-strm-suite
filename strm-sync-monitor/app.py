#!/usr/bin/env python3
"""
strm-sync-monitor — Web dashboard for the strm-pull sync.
"""

import os
import re
import subprocess
import threading
import time
import glob
from collections import deque, defaultdict
from flask import Flask, jsonify, Response

app = Flask(__name__)

LOG_FILE = os.environ.get("LOG_FILE", "/app/pull.log")
LOCK_FILE = os.environ.get("LOCK_FILE", "/tmp/strm-pull.lock")
START_SCRIPT = os.environ.get("START_SCRIPT", "/app/start-sync.sh")
STOP_SCRIPT = os.environ.get("STOP_SCRIPT", "/app/stop-sync.sh")
SYNC_INTERVAL_HOURS = 6
TAIL_LINES = 20000

KNOWN_SECTIONS = ["television", "movies", "xxx", "sports", "courses"]

SECTION_COLORS = {
    "television": "#e11d48",
    "movies": "#2563eb",
    "xxx": "#9333ea",
    "sports": "#16a34a",
    "courses": "#ea580c",
}

SECTION_LABELS = {
    "television": "Television",
    "movies": "Movies",
    "xxx": "XXX",
    "sports": "Sports",
    "courses": "Courses",
}

log_buffer = deque(maxlen=2000)
log_lock = threading.Lock()


def read_log_tail(n=TAIL_LINES):
    try:
        result = subprocess.run(
            ["tail", "-n", str(n), LOG_FILE],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return result.stdout.splitlines()
    except Exception:
        pass
    return []


def tail_log_file():
    try:
        proc = subprocess.Popen(
            ["tail", "-n", "100", "-f", LOG_FILE],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True
        )
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line:
                with log_lock:
                    log_buffer.append(line)
    except Exception:
        time.sleep(5)


def parse_section_from_line(line):
    m = re.match(r"^\[([\w/]+)\]", line)
    if m:
        raw = m.group(1)
        # Map split labels like "television/anime" -> "television"
        if "/" in raw:
            return raw.split("/")[0]
        return raw
    return None


def is_stats_line(line):
    """Check if a line is a repeated rclone stats line (e.g. '20.876 MiB / 20.876 MiB, 100%...')."""
    return bool(re.search(r"\d+\.?\d*\s*(KiB|MiB|GiB|BiB)\s*/\s*\d+\.?\d*\s*(KiB|MiB|GiB|BiB).*ETA", line))


def get_running_rclone_sections():
    running = set()
    for cmdline_path in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(cmdline_path, "r") as f:
                cmd = f.read().replace("\x00", " ")
            if "rclone sync" in cmd and "zendrive:strm-tree/" in cmd:
                m = re.search(r"zendrive:strm-tree/([\w/]+)", cmd)
                if m:
                    raw = m.group(1)
                    # Map split paths like "television/anime" -> "television"
                    if "/" in raw:
                        running.add(raw.split("/")[0])
                    else:
                        running.add(raw)
        except (IOError, OSError):
            continue
    return running


def parse_section_status():
    lines = read_log_tail(5000)
    sections = {}
    started_sections = set()
    for line in lines:
        start_match = re.match(r"\S+\s+Starting(?: section)?: ([\w/]+)", line)
        if start_match:
            raw = start_match.group(1)
            started_sections.add(raw.split("/")[0] if "/" in raw else raw)
        ok_match = re.match(r"\S+\s+\[([\w/]+)\] (?:Section )?OK", line)
        fail_match = re.match(r"\S+\s+\[([\w/]+)\] (?:Section )?FAILED", line)
        if ok_match:
            raw = ok_match.group(1)
            sec = raw.split("/")[0] if "/" in raw else raw
            sections[sec] = {"status": "ok"}
        elif fail_match:
            raw = fail_match.group(1)
            sec = raw.split("/")[0] if "/" in raw else raw
            sections[sec] = {"status": "failed"}
    running_procs = get_running_rclone_sections()
    sync_active = is_sync_running()
    # /proc check is primary - any running process means section is running
    for sec in running_procs:
        if sec not in sections or sections[sec]["status"] not in ("ok", "failed"):
            sections[sec] = {"status": "running"}
    # Sections that were started but not running and not marked OK/FAILED
    for sec in started_sections:
        if sec not in sections:
            sections[sec] = {"status": "done"}
    # If sync is running, sections without a process and without OK/FAILED = done
    # If sync is not running, they are idle
    for sec in KNOWN_SECTIONS:
        if sec not in sections:
            if sync_active and sec not in running_procs:
                sections[sec] = {"status": "done"}
            else:
                sections[sec] = {"status": "idle"}
    return sections


def parse_section_stats():
    lines = read_log_tail(5000)
    stats = defaultdict(lambda: {"copied": 0, "deleted": 0, "errors": 0})
    for line in lines:
        sec = parse_section_from_line(line)
        if sec:
            if "Copied (new)" in line or "Copied (replaced" in line:
                stats[sec]["copied"] += 1
            elif "Deleted" in line or "Removing directory" in line:
                stats[sec]["deleted"] += 1
            elif "ERROR" in line:
                stats[sec]["errors"] += 1
    return dict(stats)


def parse_run_history():
    lines = []
    try:
        result = subprocess.run(
            ["bash", "-c", f"tac {LOG_FILE} | grep -m 40 -E 'START pull|END pull' | tac"],
            capture_output=True, text=True, timeout=30
        )
        if result.stdout:
            lines = result.stdout.splitlines()
    except Exception:
        lines = read_log_tail(TAIL_LINES)
    history = []
    current_run = None
    for line in lines:
        start_match = re.match(r"(\S+)\s+START pull", line)
        end_match = re.match(r"(\S+)\s+END pull rc=(\d+) dur=(\d+)s", line)
        if start_match:
            current_run = {"start": start_match.group(1), "end": None, "exit_code": None, "duration_seconds": None}
        elif end_match and current_run:
            current_run["end"] = end_match.group(1)
            current_run["exit_code"] = int(end_match.group(2))
            dur = int(end_match.group(3))
            current_run["duration"] = f"{dur // 3600}h {(dur % 3600) // 60}m" if dur >= 3600 else f"{dur // 60}m {dur % 60}s"
            current_run["status"] = "success" if current_run["exit_code"] == 0 else "failed"
            history.append(current_run)
            current_run = None
    if current_run:
        current_run.update({"status": "running", "end": None, "exit_code": None, "duration": "in progress"})
        history.append(current_run)
    return list(reversed(history))[:20]


def filter_section_lines(lines, max_lines=80):
    """Filter log lines: collapse consecutive duplicate stats lines, prioritize file operations."""
    result = []
    last_stats_line = None
    stats_count = 0

    for line in lines:
        if is_stats_line(line):
            # Collapse consecutive stats lines - keep only the latest
            if last_stats_line is not None:
                # We had a previous stats line, replace it with this one
                # But first check if we already added it
                if result and result[-1] == last_stats_line:
                    result[-1] = line
                else:
                    result.append(line)
            else:
                result.append(line)
            last_stats_line = line
            stats_count += 1
        else:
            # Non-stats line (file operation, error, etc.) - always keep
            result.append(line)
            last_stats_line = None

    return result[-max_lines:]


def get_section_logs():
    lines = read_log_tail(3000)
    section_logs = defaultdict(list)
    general_logs = []
    for line in lines:
        sec = parse_section_from_line(line)
        if sec:
            section_logs[sec].append(line)
        else:
            general_logs.append(line)
    for sec in section_logs:
        section_logs[sec] = filter_section_lines(section_logs[sec], max_lines=80)
    return section_logs, general_logs[-50:]


def is_sync_running():
    try:
        import fcntl
        f = open(LOCK_FILE, "r")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
            return False
        except (IOError, OSError):
            return True
        finally:
            f.close()
    except Exception:
        return False


def trigger_run():
    if is_sync_running():
        return False, "Sync is already running"
    try:
        result = subprocess.run(["bash", START_SCRIPT], capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            return True, "Sync triggered successfully"
        return False, result.stderr.strip() or "Failed to start sync"
    except Exception as e:
        return False, str(e)


def stop_sync():
    if not is_sync_running():
        return False, "No sync is currently running"
    try:
        result = subprocess.run(["bash", STOP_SCRIPT], capture_output=True, text=True, timeout=30)
        time.sleep(3)
        if is_sync_running():
            return True, "Stop signal sent"
        return True, "Sync stopped successfully"
    except Exception as e:
        return False, str(e)


def classify_log_line(line):
    if "ERROR" in line:
        return "ll-error"
    if "NOTICE" in line:
        return "ll-notice"
    if "Deleted" in line or "Removing" in line:
        return "ll-delete"
    if "Copied" in line or "Transferred" in line:
        return "ll-copy"
    if "0 B / 0 B" in line:
        return "ll-idle"
    if "START pull" in line or "END pull" in line:
        return "ll-start"
    return ""


@app.route("/")
def index():
    history = parse_run_history()
    running = is_sync_running()
    section_status = parse_section_status()
    section_stats = parse_section_stats()
    section_logs, general_logs = get_section_logs()

    cards_html = ""
    for sec in KNOWN_SECTIONS:
        info = section_status.get(sec, {"status": "idle"})
        status = info["status"]
        color = SECTION_COLORS.get(sec, "#64748b")
        label = SECTION_LABELS.get(sec, sec)
        stats = section_stats.get(sec, {"copied": 0, "deleted": 0, "errors": 0})
        status_map = {
            "running": ("Running", "st-running"),
            "ok": ("Done", "st-done"),
            "done": ("Done", "st-done"),
            "failed": ("Failed", "st-failed"),
            "idle": ("Idle", "st-idle"),
        }
        status_label, status_class = status_map.get(status, ("Idle", "st-idle"))
        cards_html += f'''
        <div class="sec-card {status_class}" onclick="toggle('{sec}')" style="--c:{color}">
          <div class="sec-top">
            <div class="sec-dot" style="background:{color}"></div>
            <span class="sec-name">{label}</span>
            <span class="sec-badge {status_class}">{status_label}</span>
          </div>
          <div class="sec-stats">
            <span class="s-stat"><b>{stats['copied']}</b> copied</span>
            <span class="s-stat"><b>{stats['deleted']}</b> deleted</span>
            <span class="s-stat {'err' if stats['errors'] else ''}"><b>{stats['errors']}</b> errors</span>
          </div>
        </div>'''

    panels_html = ""
    for sec in KNOWN_SECTIONS:
        logs = section_logs.get(sec, [])
        color = SECTION_COLORS.get(sec, "#64748b")
        status = section_status.get(sec, {}).get("status", "idle")
        label = SECTION_LABELS.get(sec, sec)
        log_html = ""
        for line in logs:
            esc = line.replace("<", "&lt;").replace(">", "&gt;")
            cls = classify_log_line(line)
            log_html += f'<div class="ll {cls}">{esc}</div>'
        if not log_html:
            log_html = '<div class="ll ll-empty">No output yet</div>'
        expanded = status in ("running", "failed")
        style = "" if expanded else "display:none;"
        chev = "open" if expanded else ""
        panels_html += f'''
        <div class="panel" id="p-{sec}" style="{style}">
          <div class="p-bar" onclick="toggle('{sec}')">
            <span class="p-dot" style="background:{color}"></span>
            <span class="p-name">{label}</span>
            <svg class="chev {chev}" id="ch-{sec}" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>
          </div>
          <div class="p-log" id="lg-{sec}">{log_html}</div>
        </div>'''

    gen_html = ""
    for line in general_logs:
        esc = line.replace("<", "&lt;").replace(">", "&gt;")
        cls = classify_log_line(line)
        gen_html += f'<div class="ll {cls}">{esc}</div>'
    if not gen_html:
        gen_html = '<div class="ll ll-empty">No general output</div>'

    hist_html = ""
    for run in history:
        if run["status"] == "running":
            badge = '<span class="pill pill-run">Running</span>'
        elif run["status"] == "success":
            badge = '<span class="pill pill-ok">Success</span>'
        else:
            badge = f'<span class="pill pill-fail">Failed (rc={run["exit_code"]})</span>'
        hist_html += f'<tr><td>{run["start"][:19]}</td><td>{run.get("end","-")[:19] if run.get("end") else "-"}</td><td>{run.get("duration","-")}</td><td>{badge}</td></tr>'

    next_run = "Unknown"
    if history and history[0].get("end"):
        try:
            from datetime import datetime, timedelta
            last_end = datetime.fromisoformat(history[0]["end"])
            next_dt = last_end + timedelta(hours=SYNC_INTERVAL_HOURS)
            next_run = next_dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass

    status_pill = '<span class="pill pill-run">Running</span>' if running else '<span class="pill pill-idle">Idle</span>'
    run_dis = "disabled" if running else ""
    run_txt = "Running..." if running else "Run Now"
    stop_show = "inline-flex" if running else "none"

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>STRM Sync Monitor</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
:root{{--bg:#f1f5f9;--card:#fff;--border:#e2e8f0;--text:#1e293b;--dim:#64748b;--blue:#2563eb;--green:#16a34a;--red:#dc2626;--yellow:#ca8a04;--r:14px}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Inter','Segoe UI',sans-serif;background:var(--bg);color:var(--text);padding:24px;max-width:1100px;margin:0 auto;-webkit-font-smoothing:antialiased}}
.head{{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;flex-wrap:wrap;gap:12px}}
.head-l{{display:flex;align-items:center;gap:12px}}
.logo{{width:38px;height:38px;border-radius:10px;background:linear-gradient(135deg,var(--blue),#7c3aed);display:flex;align-items:center;justify-content:center;color:#fff;font-size:17px;font-weight:800}}
.head h1{{font-size:1.2rem;font-weight:700;letter-spacing:-0.02em}}
.head h1 small{{display:block;font-size:0.72rem;font-weight:400;color:var(--dim);margin-top:1px}}
.btn{{display:inline-flex;align-items:center;gap:6px;padding:8px 18px;border-radius:8px;font-size:0.83rem;font-weight:600;cursor:pointer;border:none;transition:all 0.15s}}
.btn-go{{background:var(--blue);color:#fff}}.btn-go:hover{{background:#1d4ed8}}
.btn-go:disabled{{background:#cbd5e1;color:#94a3b8;cursor:not-allowed}}
.btn-stop{{background:#fff;border:1px solid var(--red);color:var(--red)}}.btn-stop:hover{{background:var(--red);color:#fff}}
.top{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:20px}}
.tcard{{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:14px 16px}}
.tcard .lbl{{font-size:0.68rem;text-transform:uppercase;letter-spacing:0.08em;color:var(--dim);margin-bottom:4px;font-weight:600}}
.tcard .val{{font-size:1rem;font-weight:700}}
.sec-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px;margin-bottom:20px}}
.sec-card{{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:14px;cursor:pointer;transition:all 0.15s;border-top:3px solid var(--c)}}
.sec-card:hover{{box-shadow:0 4px 12px rgba(0,0,0,0.08);transform:translateY(-1px)}}
.sec-card.st-running{{box-shadow:0 0 0 2px var(--yellow),0 4px 12px rgba(202,138,4,0.15)}}
.sec-card.st-failed{{box-shadow:0 0 0 2px var(--red),0 4px 12px rgba(220,38,38,0.1)}}
.sec-top{{display:flex;align-items:center;gap:8px;margin-bottom:10px}}
.sec-dot{{width:10px;height:10px;border-radius:50%;flex-shrink:0}}
.sec-name{{font-size:0.88rem;font-weight:700;flex:1}}
.sec-badge{{font-size:0.62rem;font-weight:700;text-transform:uppercase;letter-spacing:0.04em;padding:2px 8px;border-radius:20px}}
.st-running{{background:#fef3c7;color:var(--yellow)}}
.st-done{{background:#dcfce7;color:var(--green)}}
.st-failed{{background:#fee2e2;color:var(--red)}}
.st-idle{{background:#f1f5f9;color:var(--dim)}}
.sec-stats{{display:flex;gap:12px;font-size:0.72rem;color:var(--dim)}}
.sec-stats .s-stat b{{color:var(--text)}}
.sec-stats .s-stat.err b{{color:var(--red)}}
.stitle{{font-size:0.72rem;text-transform:uppercase;letter-spacing:0.1em;color:var(--dim);margin:20px 0 10px;font-weight:700}}
.panel{{background:var(--card);border:1px solid var(--border);border-radius:var(--r);margin-bottom:8px;overflow:hidden}}
.p-bar{{display:flex;align-items:center;gap:10px;padding:9px 14px;cursor:pointer;user-select:none;border-bottom:1px solid var(--border);transition:background 0.1s}}
.p-bar:hover{{background:#f8fafc}}
.p-dot{{width:8px;height:8px;border-radius:50%;flex-shrink:0}}
.p-name{{font-size:0.82rem;font-weight:600;flex:1}}
.chev{{transition:transform 0.2s;color:var(--dim)}}
.chev.open{{transform:rotate(180deg)}}
.p-log{{padding:10px 14px;font-family:'SF Mono','Fira Code',Consolas,monospace;font-size:0.74rem;max-height:320px;overflow-y:auto;white-space:pre-wrap;word-break:break-all;line-height:1.5;background:#fafbfc}}
.ll{{padding:1px 0}}
.ll-error{{color:var(--red)}}
.ll-notice{{color:var(--yellow)}}
.ll-delete{{color:#ea580c}}
.ll-copy{{color:var(--green)}}
.ll-idle{{color:#94a3b8}}
.ll-start{{color:var(--blue);font-weight:600}}
.ll-empty{{color:var(--dim);font-style:italic;padding:6px 0}}
.gen-log{{background:#fafbfc;border:1px solid var(--border);border-radius:var(--r);padding:10px 14px;font-family:'SF Mono','Fira Code',Consolas,monospace;font-size:0.74rem;max-height:160px;overflow-y:auto;white-space:pre-wrap;word-break:break-all;line-height:1.5;margin-bottom:20px}}
.tw{{background:var(--card);border:1px solid var(--border);border-radius:var(--r);overflow:hidden;margin-bottom:20px}}
table{{width:100%;border-collapse:collapse}}
th{{padding:9px 14px;text-align:left;font-size:0.68rem;text-transform:uppercase;letter-spacing:0.08em;color:var(--dim);font-weight:700;background:#f8fafc}}
td{{padding:9px 14px;border-top:1px solid var(--border);font-size:0.8rem}}
.pill{{display:inline-block;padding:2px 10px;border-radius:20px;font-size:0.68rem;font-weight:600}}
.pill-run{{background:#fef3c7;color:var(--yellow)}}
.pill-ok{{background:#dcfce7;color:var(--green)}}
.pill-fail{{background:#fee2e2;color:var(--red)}}
.pill-idle{{background:#f1f5f9;color:var(--dim)}}
#toast{{position:fixed;top:20px;right:20px;padding:11px 18px;border-radius:8px;font-size:0.83rem;display:none;z-index:1000;box-shadow:0 4px 12px rgba(0,0,0,0.15)}}
.t-ok{{background:var(--green);color:#fff}}.t-err{{background:var(--red);color:#fff}}
::-webkit-scrollbar{{width:6px;height:6px}}
::-webkit-scrollbar-track{{background:transparent}}
::-webkit-scrollbar-thumb{{background:#cbd5e1;border-radius:3px}}
::-webkit-scrollbar-thumb:hover{{background:#94a3b8}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:0.5}}}}
.st-running{{animation:pulse 2s infinite}}
</style></head><body>
<div class="head">
  <div class="head-l">
    <div class="logo">S</div>
    <h1>STRM Sync Monitor<small>zendrive-strm-suite</small></h1>
  </div>
  <div style="display:flex;gap:8px">
    <button class="btn btn-go" id="runBtn" onclick="triggerRun()" {run_dis}>{run_txt}</button>
    <button class="btn btn-stop" id="stopBtn" onclick="stopSync()" style="display:{stop_show}">Stop</button>
  </div>
</div>
<div class="top">
  <div class="tcard"><div class="lbl">Status</div><div class="val">{status_pill}</div></div>
  <div class="tcard"><div class="lbl">Next Run</div><div class="val">{next_run}</div></div>
  <div class="tcard"><div class="lbl">Last Started</div><div class="val" style="font-size:0.9rem">{history[0]['start'][:19] if history else '-'}</div></div>
</div>
<div class="stitle">Sections</div>
<div class="sec-grid">{cards_html}</div>
<div class="stitle">Logs &mdash; click to expand</div>
{panels_html}
<div class="stitle">General</div>
<div class="gen-log" id="genLog">{gen_html}</div>
<div class="stitle">Run History</div>
<div class="tw">
  <table><thead><tr><th>Started</th><th>Finished</th><th>Duration</th><th>Status</th></tr></thead>
  <tbody>{hist_html or '<tr><td colspan="4" style="color:var(--dim);text-align:center;padding:16px">No runs yet</td></tr>'}</tbody></table>
</div>
<div id="toast"></div>
<script>
function toggle(s){{
  const p=document.getElementById('p-'+s);const c=document.getElementById('ch-'+s);
  if(p.style.display==='none'){{p.style.display='block';c.classList.add('open')}}
  else{{p.style.display='none';c.classList.remove('open')}}
}}
function triggerRun(){{
  const b=document.getElementById('runBtn');b.disabled=true;b.textContent='Starting...';
  fetch('/api/trigger',{{method:'POST'}}).then(r=>r.json()).then(d=>{{
    toast(d.message,d.success);
    if(d.success){{b.textContent='Running...';setTimeout(()=>location.reload(),5000)}}
    else{{b.disabled=false;b.textContent='Run Now'}}
  }}).catch(()=>{{toast('Request failed',false);b.disabled=false;b.textContent='Run Now'}});
}}
function stopSync(){{
  const b=document.getElementById('stopBtn');b.disabled=true;b.textContent='Stopping...';
  if(!confirm('Stop the running sync?')){{b.disabled=false;b.textContent='Stop';return}}
  fetch('/api/stop',{{method:'POST'}}).then(r=>r.json()).then(d=>{{
    toast(d.message,d.success);b.disabled=false;b.textContent='Stop';
    if(d.success)setTimeout(()=>location.reload(),3000);
  }}).catch(()=>{{toast('Request failed',false);b.disabled=false;b.textContent='Stop'}});
}}
function toast(m,s){{
  const t=document.getElementById('toast');t.textContent=m;
  t.className=s?'t-ok':'t-err';t.style.display='block';
  setTimeout(()=>t.style.display='none',4000);
}}
// SSE: only listen for NEW lines (initial logs already in HTML)
const es=new EventSource('/api/log/stream');
es.onmessage=function(e){{
  const line=e.data;const m=line.match(/^\\[(\\w+)\\]/);
  if(m){{
    const sec=m[1];const lg=document.getElementById('lg-'+sec);
    if(lg){{
      // Skip duplicate stats lines
      if(isStats(line)&&lg.lastChild&&isStats(lg.lastChild.textContent)){{
        lg.lastChild.textContent=line;
        lg.lastChild.className="ll "+cls(line);
      }}else{{
        const el=document.createElement('div');el.className='ll '+cls(line);
        el.textContent=line;lg.appendChild(el);
        while(lg.children.length>120)lg.removeChild(lg.firstChild);
      }}
      lg.scrollTop=lg.scrollHeight;
    }}
  }}else{{
    const g=document.getElementById('genLog');
    if(g){{
      const el=document.createElement('div');el.className='ll '+cls(line);
      el.textContent=line;g.appendChild(el);
      while(g.children.length>100)g.removeChild(g.firstChild);
      g.scrollTop=g.scrollHeight;
    }}
  }}
}};
function isStats(l){{return /\\d+\\.?\\d*\\s*(KiB|MiB|GiB)\\s*\\/\\s*\\d+\\.?\\d*\\s*(KiB|MiB|GiB).*ETA/.test(l)}}
function cls(l){{
  if(l.includes('ERROR'))return' ll-error';
  if(l.includes('NOTICE'))return' ll-notice';
  if(l.includes('Deleted')||l.includes('Removing'))return' ll-delete';
  if(l.includes('Copied')||l.includes('Transferred'))return' ll-copy';
  if(l.includes('0 B / 0 B'))return' ll-idle';
  if(l.includes('START pull')||l.includes('END pull'))return' ll-start';
  return'';
}}
setTimeout(()=>location.reload(),60000);
</script></body></html>"""


@app.route("/api/status")
def api_status():
    return jsonify({"running": is_sync_running(), "history": parse_run_history(), "sections": parse_section_status()})


@app.route("/api/trigger", methods=["POST"])
def api_trigger():
    s, m = trigger_run()
    return jsonify({"success": s, "message": m})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    s, m = stop_sync()
    return jsonify({"success": s, "message": m})


@app.route("/api/sections")
def api_sections():
    sl, gl = get_section_logs()
    return jsonify({"sections": parse_section_status(), "stats": parse_section_stats(), "section_logs": dict(sl), "general_logs": gl})


@app.route("/api/log/stream")
def api_log_stream():
    def gen():
        # Only send NEW lines - initial logs are already in the HTML
        last = len(log_buffer)
        while True:
            with log_lock:
                cur = list(log_buffer)
            if len(cur) > last:
                for line in cur[last:]:
                    yield f"data: {line}\n\n"
                last = len(cur)
            elif len(cur) < last:
                # Buffer wrapped around
                last = 0
            time.sleep(0.5)
    return Response(gen(), mimetype="text/event-stream")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    t = threading.Thread(target=tail_log_file, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=8080, threaded=True)
