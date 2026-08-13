#!/usr/bin/env python3
"""
strm-sync-monitor — Web dashboard for the strm-pull sync.

Shows:
  - Live tail of the rclone sync log
  - Run history (start time, duration, exit code)
  - Current sync status (running/idle via lock file)
  - Next estimated run
  - "Run now" button to trigger a sync immediately
  - "Stop" button to kill a running sync

Triggers and stops the systemd service on the host via nsenter.
Only reads the tail of the log file to avoid issues with large logs.
"""

import os
import re
import subprocess
import threading
import time
from collections import deque
from flask import Flask, jsonify, Response

app = Flask(__name__)

LOG_FILE = os.environ.get("LOG_FILE", "/app/pull.log")
LOCK_FILE = os.environ.get("LOCK_FILE", "/tmp/strm-pull.lock")
START_SCRIPT = os.environ.get("START_SCRIPT", "/app/start-sync.sh")
STOP_SCRIPT = os.environ.get("STOP_SCRIPT", "/app/stop-sync.sh")
SYNC_INTERVAL_HOURS = 6
TAIL_LINES = 20000

log_buffer = deque(maxlen=500)
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
    """Start the strm-pull systemd service on the host via the start script."""
    if is_sync_running():
        return False, "Sync is already running"
    try:
        result = subprocess.run(
            ["bash", START_SCRIPT],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            return True, "Sync triggered successfully"
        else:
            return False, result.stderr.strip() or "Failed to start sync"
    except Exception as e:
        return False, str(e)


def stop_sync():
    """Stop the strm-pull systemd service on the host via the stop script."""
    if not is_sync_running():
        return False, "No sync is currently running"
    try:
        result = subprocess.run(
            ["bash", STOP_SCRIPT],
            capture_output=True, text=True, timeout=30
        )
        # Wait for lock to release
        time.sleep(3)
        if is_sync_running():
            return True, "Stop signal sent (lock may take a moment to release)"
        return True, "Sync stopped successfully"
    except Exception as e:
        return False, str(e)


@app.route("/")
def index():
    history = parse_run_history()
    running = is_sync_running()

    history_html = ""
    for run in history:
        if run["status"] == "running":
            badge = '<span class="badge badge-running">RUNNING</span>'
        elif run["status"] == "success":
            badge = '<span class="badge badge-success">SUCCESS</span>'
        else:
            badge = f'<span class="badge badge-failed">FAILED (rc={run["exit_code"]})</span>'
        history_html += f'<tr><td>{run["start"]}</td><td>{run.get("end", "-")}</td><td>{run.get("duration", "-")}</td><td>{badge}</td></tr>'

    next_run = "Unknown"
    if history and history[0].get("end"):
        try:
            from datetime import datetime, timedelta
            last_end = datetime.fromisoformat(history[0]["end"])
            next_dt = last_end + timedelta(hours=SYNC_INTERVAL_HOURS)
            next_run = next_dt.strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            pass

    status_html = '<span class="badge badge-running">RUNNING NOW</span>' if running else '<span class="badge badge-inactive">IDLE</span>'
    run_btn_disabled = "disabled" if running else ""
    run_btn_text = "Sync running..." if running else "Run Sync Now"
    stop_btn_display = "inline-block" if running else "none"

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>STRM Sync Monitor</title><style>
:root{{--bg:#1a1a2e;--card:#16213e;--text:#e0e0e0;--accent:#0f3460;--green:#2ecc71;--red:#e74c3c;--yellow:#f39c12;--blue:#3498db}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,monospace;background:var(--bg);color:var(--text);padding:20px;max-width:1200px;margin:0 auto}}
h1{{font-size:1.8rem;margin-bottom:20px;color:var(--blue)}}
.grid{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:15px;margin-bottom:20px}}
.card{{background:var(--card);border-radius:8px;padding:20px;text-align:center}}
.card h2{{font-size:0.85rem;text-transform:uppercase;color:#888;margin-bottom:10px}}
.card .value{{font-size:1.2rem;font-weight:bold}}
.badge{{display:inline-block;padding:4px 12px;border-radius:4px;font-size:0.85rem;font-weight:bold;text-transform:uppercase}}
.badge-running{{background:var(--yellow);color:#000}}.badge-success{{background:var(--green);color:#000}}
.badge-failed{{background:var(--red);color:#fff}}.badge-inactive{{background:#555;color:#fff}}
.btn{{background:var(--blue);color:#fff;border:none;padding:12px 30px;border-radius:6px;font-size:1rem;cursor:pointer;margin:10px 5px 20px 0}}
.btn:hover{{background:#2980b9}}.btn:disabled{{background:#555;cursor:not-allowed}}
.btn-stop{{background:var(--red)}}.btn-stop:hover{{background:#c0392b}}
table{{width:100%;border-collapse:collapse;margin-bottom:20px;background:var(--card);border-radius:8px;overflow:hidden}}
th,td{{padding:10px 15px;text-align:left;border-bottom:1px solid #2a2a4a}}
th{{background:var(--accent);font-size:0.85rem;text-transform:uppercase;color:#aaa}}
#log{{background:#0d1117;border-radius:8px;padding:15px;font-family:'Courier New',monospace;font-size:0.8rem;height:400px;overflow-y:auto;white-space:pre-wrap;word-break:break-all}}
.log-line{{padding:1px 0}}.log-error{{color:var(--red)}}.log-notice{{color:var(--yellow)}}
.log-info{{color:var(--green)}}.log-start{{color:var(--blue);font-weight:bold}}
h2.section{{font-size:1.1rem;margin:20px 0 10px;color:#aaa}}
#toast{{position:fixed;top:20px;right:20px;padding:15px 25px;border-radius:6px;display:none;z-index:1000}}
.toast-success{{background:var(--green);color:#000}}.toast-error{{background:var(--red);color:#fff}}
</style></head><body>
<h1>STRM Sync Monitor</h1>
<div class="grid">
<div class="card"><h2>Status</h2><div class="value">{status_html}</div></div>
<div class="card"><h2>Next Estimated Run</h2><div class="value">{next_run}</div></div>
<div class="card"><h2>Last Run</h2><div class="value">{history[0]['start'] if history else '-'}</div></div>
</div>
<div>
<button class="btn" id="runBtn" onclick="triggerRun()" {run_btn_disabled}>{run_btn_text}</button>
<button class="btn btn-stop" id="stopBtn" onclick="stopSync()" style="display:{stop_btn_display}">Stop Sync</button>
</div>
<h2 class="section">Run History</h2>
<table><thead><tr><th>Started</th><th>Finished</th><th>Duration</th><th>Status</th></tr></thead>
<tbody>{history_html or '<tr><td colspan="4">No runs found</td></tr>'}</tbody></table>
<h2 class="section">Live Log</h2>
<div id="log"></div><div id="toast"></div>
<script>
function triggerRun(){{const b=document.getElementById('runBtn');b.disabled=true;b.textContent='Triggering...';
fetch('/api/trigger',{{method:'POST'}}).then(r=>r.json()).then(d=>{{showToast(d.message,d.success);
if(d.success){{b.textContent='Sync running...';setTimeout(()=>location.reload(),5000)}}else{{b.disabled=false;b.textContent='Run Sync Now'}}}})
.catch(()=>{{showToast('Request failed',false);b.disabled=false;b.textContent='Run Sync Now'}})}}
function stopSync(){{const b=document.getElementById('stopBtn');b.disabled=true;b.textContent='Stopping...';
if(!confirm('Are you sure you want to stop the running sync?')){{b.disabled=false;b.textContent='Stop Sync';return}}
fetch('/api/stop',{{method:'POST'}}).then(r=>r.json()).then(d=>{{showToast(d.message,d.success);
b.disabled=false;b.textContent='Stop Sync';if(d.success)setTimeout(()=>location.reload(),3000)}})
.catch(()=>{{showToast('Request failed',false);b.disabled=false;b.textContent='Stop Sync'}})}}
function showToast(m,s){{const t=document.getElementById('toast');t.textContent=m;t.className='toast-'+(s?'success':'error');t.style.display='block';setTimeout(()=>t.style.display='none',4000)}}
const es=new EventSource('/api/log/stream');const ld=document.getElementById('log');
es.onmessage=function(e){{const l=document.createElement('div');l.className='log-line';const t=e.data;
if(t.includes('ERROR'))l.classList.add('log-error');else if(t.includes('NOTICE'))l.classList.add('log-notice');
else if(t.includes('START pull')||t.includes('END pull'))l.classList.add('log-start');
else if(t.includes('Copied')||t.includes('Transferred'))l.classList.add('log-info');
l.textContent=t;ld.appendChild(l);while(ld.children.length>300)ld.removeChild(ld.firstChild);ld.scrollTop=ld.scrollHeight}};
setTimeout(()=>location.reload(),30000);
</script></body></html>"""


@app.route("/api/status")
def api_status():
    return jsonify({"running": is_sync_running(), "history": parse_run_history()})


@app.route("/api/trigger", methods=["POST"])
def api_trigger():
    success, message = trigger_run()
    return jsonify({"success": success, "message": message})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    success, message = stop_sync()
    return jsonify({"success": success, "message": message})


@app.route("/api/log/stream")
def api_log_stream():
    def generate():
        with log_lock:
            for line in list(log_buffer):
                yield f"data: {line}\n\n"
        last_count = len(log_buffer)
        while True:
            with log_lock:
                current = list(log_buffer)
            if len(current) > last_count:
                for line in current[last_count:]:
                    yield f"data: {line}\n\n"
                last_count = len(current)
            time.sleep(0.5)
    return Response(generate(), mimetype="text/event-stream")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    t = threading.Thread(target=tail_log_file, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=8080, threaded=True)
