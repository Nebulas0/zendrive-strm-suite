#!/usr/bin/env python3
"""
strm-bridge.py — Webhook bridge between ZenLocalPoller and Emby for STRM libraries.

Receives autoscan-style trigger requests from ZenLocalPoller with UNIONFS paths,
converts them to STRM paths, runs a targeted rclone sync/copy to sync just the
changed directory from zendrive:strm-tree to the local STRM library, then
notifies autoscan to scan the STRM path.

For small directories (< SYNC_FILE_THRESHOLD files), uses rclone sync with
--max-delete 500 to also clean up stale files. For larger directories, uses
rclone copy to avoid hitting --max-delete limits caused by duplicate directory
entries on ZenDRIVE.

Deduplication ensures only one sync runs per directory at a time.
Autoscan is only triggered AFTER the rclone completes successfully.
If a sync is already running for a directory, duplicate events wait
and autoscan is triggered once after the running sync finishes.
"""

import json
import os
import subprocess
import threading
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "3031"))

LOCAL_LIBRARY = os.environ.get("LOCAL_LIBRARY", "/mnt/local/strm/library")
RCLONE_REMOTE = os.environ.get("RCLONE_REMOTE", "zendrive:strm-tree")
AUTOSCAN_URL = os.environ.get("AUTOSCAN_URL", "http://127.0.0.1:3030")
AUTH_USER = os.environ.get("AUTH_USER", "plexuser")
AUTH_PASS = os.environ.get("AUTH_PASS", "8XpETOmC6JyKXeNZ")
RCLONE_TRANSFERS = os.environ.get("RCLONE_TRANSFERS", "8")
RCLONE_CHECKERS = os.environ.get("RCLONE_CHECKERS", "8")

# Use sync for dirs with fewer than this many files, copy for larger dirs.
# ZenDRIVE has duplicate directory entries that cause sync to try deleting
# files from the "other" copy — with --max-delete 500, a dir with many
# seasons can exceed the limit. 500 files is safe for most single-season shows.
SYNC_FILE_THRESHOLD = int(os.environ.get("SYNC_FILE_THRESHOLD", "500"))

LOG_FILE = os.environ.get("LOG_FILE", "")

handlers = [logging.StreamHandler()]
if LOG_FILE:
    handlers.insert(0, logging.FileHandler(LOG_FILE))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=handlers,
)
log = logging.getLogger("strm-bridge")

# Path mappings for unionfs -> strm conversion
UNIONFS_TO_STRM_MAPPINGS = [
    ("/mnt/unionfs/movies-int/", "/mnt/local/strm/library/movies/int/"),
    ("/mnt/unionfs/television-int/", "/mnt/local/strm/library/television/int/"),
    ("/mnt/unionfs/", "/mnt/local/strm/library/"),
]

# Track which directories are currently being synced
active_syncs = {}  # rclone_subpath -> threading.Event (set when sync is done)
active_syncs_lock = threading.Lock()


def unionfs_to_strm(path: str) -> str:
    """Convert a unionfs path to the corresponding strm path."""
    for unionfs_prefix, strm_prefix in UNIONFS_TO_STRM_MAPPINGS:
        if path.startswith(unionfs_prefix):
            return strm_prefix + path[len(unionfs_prefix):]
    return path


def count_remote_files(rclone_subpath: str) -> int:
    """Count files in the remote directory using rclone size --fast-list."""
    remote_src = f"{RCLONE_REMOTE}/{rclone_subpath}"
    cmd = [
        "/usr/bin/rclone", "size", remote_src,
        "--fast-list",
        "--json",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=120)
        if result.returncode == 0 and result.stdout:
            data = json.loads(result.stdout)
            return data.get("count", 0)
    except Exception as e:
        log.warning(f"Could not count files for {rclone_subpath}: {e}")
    return -1  # unknown


def run_rclone(rclone_subpath: str, local_dest: str) -> bool:
    """Run a targeted rclone sync or copy for a single directory.

    Uses sync (with --max-delete 500) for small directories to also clean up
    stale files. Uses copy for large directories to avoid hitting --max-delete
    limits from ZenDRIVE's duplicate directory entries.
    """
    remote_src = f"{RCLONE_REMOTE}/{rclone_subpath}"

    file_count = count_remote_files(rclone_subpath)
    use_sync = 0 <= file_count < SYNC_FILE_THRESHOLD

    base_cmd = [
        "/usr/bin/rclone",
        "sync" if use_sync else "copy",
        remote_src, local_dest,
        "--ignore-size",
        "--use-server-modtime",
        "--ignore-errors",
        "--exclude", "*.partial",
        "--exclude", "*.partial.*",
    ]
    if use_sync:
        base_cmd += ["--max-delete", "500"]
    base_cmd += [
        "--transfers", RCLONE_TRANSFERS,
        "--checkers", RCLONE_CHECKERS,
        "--retries", "3",
        "--low-level-retries", "10",
        "--stats", "30s",
        "--stats-one-line",
        "--log-level", "INFO",
    ]

    mode = "sync" if use_sync else "copy"
    log.info(f"rclone {mode} ({file_count} files): {remote_src} -> {local_dest}")

    try:
        result = subprocess.run(base_cmd, capture_output=True, text=True, timeout=600)
        if result.returncode == 0:
            log.info(f"rclone {mode} OK: {rclone_subpath}")
            if result.stderr:
                for line in result.stderr.strip().splitlines()[-3:]:
                    log.info(f"  rclone: {line}")
            return True
        else:
            log.error(f"rclone {mode} FAILED (rc={result.returncode}): {rclone_subpath}")
            if result.stderr:
                for line in result.stderr.strip().splitlines()[-5:]:
                    log.error(f"  rclone: {line}")

            # If sync failed due to --max-delete, retry with copy as fallback
            if use_sync and result.stderr and "max-delete" in result.stderr:
                log.warning(f"sync hit --max-delete limit, retrying with copy: {rclone_subpath}")
                # Rebuild as copy command without --max-delete
                copy_cmd = [
                    "/usr/bin/rclone", "copy", remote_src, local_dest,
                    "--ignore-size",
                    "--use-server-modtime",
                    "--ignore-errors",
                    "--exclude", "*.partial",
                    "--exclude", "*.partial.*",
                    "--transfers", RCLONE_TRANSFERS,
                    "--checkers", RCLONE_CHECKERS,
                    "--retries", "3",
                    "--low-level-retries", "10",
                    "--stats", "30s",
                    "--stats-one-line",
                    "--log-level", "INFO",
                ]
                result2 = subprocess.run(copy_cmd, capture_output=True, text=True, errors="replace", timeout=600)
                if result2.returncode == 0:
                    log.info(f"rclone copy fallback OK: {rclone_subpath}")
                    if result2.stderr:
                        for line in result2.stderr.strip().splitlines()[-3:]:
                            log.info(f"  rclone: {line}")
                    return True
                else:
                    log.error(f"rclone copy fallback FAILED (rc={result2.returncode}): {rclone_subpath}")
                    if result2.stderr:
                        for line in result2.stderr.strip().splitlines()[-5:]:
                            log.error(f"  rclone: {line}")
            return False
    except subprocess.TimeoutExpired:
        log.error(f"rclone {mode} TIMEOUT: {rclone_subpath}")
        return False
    except Exception as e:
        log.error(f"rclone {mode} ERROR: {rclone_subpath}: {e}")
        return False


def trigger_autoscan_scan(scan_path: str, trigger: str) -> bool:
    """Tell autoscan to scan a specific path via the manual webhook trigger."""
    import base64
    from urllib.parse import quote
    encoded_path = quote(scan_path, safe="")
    url = f"{AUTOSCAN_URL}/triggers/manual?dir={encoded_path}"
    req = Request(url, data=b"", method="POST")
    credentials = base64.b64encode(f"{AUTH_USER}:{AUTH_PASS}".encode()).decode()
    req.add_header("Authorization", f"Basic {credentials}")
    try:
        with urlopen(req, timeout=30) as resp:
            if resp.status in (200, 202, 204):
                log.info(f"Autoscan triggered: {scan_path}")
                return True
            else:
                log.error(f"Autoscan returned {resp.status}: {scan_path}")
                return False
    except (URLError, HTTPError) as e:
        log.error(f"Autoscan failed: {scan_path}: {e}")
        return False


def process_event(path: str, trigger: str):
    """Process a single incoming event: convert path, rclone, autoscan."""

    strm_path = unionfs_to_strm(path)
    if os.path.splitext(strm_path)[1]:
        strm_dir = os.path.dirname(strm_path)
    else:
        strm_dir = strm_path
    log.info(f"Path conversion: {path} -> {strm_dir}")

    if not strm_path.startswith(LOCAL_LIBRARY):
        if not path.startswith("/mnt/unionfs/"):
            log.warning(f"Path not a unionfs path (likely unmapped), skipping: {path}")
        else:
            log.warning(f"Converted path outside library root, skipping: {strm_path}")
        return

    subpath = strm_path[len(LOCAL_LIBRARY):].lstrip("/")

    if os.path.splitext(subpath)[1]:
        subpath = os.path.dirname(subpath)

    if not subpath:
        log.warning("Empty subpath after processing, skipping")
        return

    local_dest = os.path.join(LOCAL_LIBRARY, subpath)
    rclone_subpath = subpath

    def _do_sync():
        # Check if this directory is already being synced
        with active_syncs_lock:
            if rclone_subpath in active_syncs:
                # A sync is already running for this directory.
                # Wait for it to finish, then trigger autoscan (don't start a new sync).
                log.info(f"Sync already in progress for {rclone_subpath}, waiting for it to finish")
                done_event = active_syncs[rclone_subpath]
            else:
                # We're the first sync for this directory - register and run
                done_event = threading.Event()
                active_syncs[rclone_subpath] = done_event
                # Start the sync in this thread
                threading.Thread(target=_run_and_signal, args=(rclone_subpath, local_dest, trigger, done_event), daemon=True).start()
                return

        # Wait for the running sync to complete, then trigger autoscan
        done_event.wait(timeout=600)
        log.info(f"Sync for {rclone_subpath} finished, triggering autoscan for queued event")
        trigger_autoscan_scan(local_dest, trigger)

    def _run_and_signal(subpath, dest, trig, done_evt):
        """Run the actual rclone and signal completion."""
        try:
            ok = run_rclone(subpath, dest)
            if ok:
                trigger_autoscan_scan(dest, trig)
        finally:
            with active_syncs_lock:
                active_syncs.pop(subpath, None)
            done_evt.set()

    threading.Thread(target=_do_sync, daemon=True).start()


class BridgeHandler(BaseHTTPRequestHandler):
    def _check_auth(self):
        import base64
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode()
                user, _, pw = decoded.partition(":")
                if user == AUTH_USER and pw == AUTH_PASS:
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="strm-bridge"')
        self.end_headers()
        return False

    def do_POST(self):
        if not self._check_auth():
            return

        trigger = self.path.strip("/").split("/")[-1] if "/triggers/" in self.path else "unknown"

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""
        content_type = self.headers.get("Content-Type", "")

        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            from urllib.parse import parse_qs
            parsed = parse_qs(body.decode())
            data = {"path": parsed.get("path", [""])[0]}

        path = data.get("path", "")
        # If path is just a bare filename (not a full unionfs path), try
        # extracting the full path from series/episodeFile or movie/movieFile
        if path and not path.startswith("/mnt/unionfs/"):
            log.info(f"Path '{path}' is not a unionfs path, trying movie/series fields")
            path = ""
        if not path:
            if "series" in data:
                base = data["series"].get("path", "")
                rel = data.get("episodeFile", {}).get("relativePath", "")
                path = os.path.join(base, rel) if rel else base
            elif "movie" in data:
                base = data["movie"].get("path", "")
                rel = data.get("movieFile", {}).get("relativePath", "")
                path = os.path.join(base, rel) if rel else base
            elif "remotePath" in data:
                path = data["remotePath"]
            elif "folderPath" in data:
                path = data["folderPath"]

        if not path:
            log.warning(f"400 Bad Request: trigger={trigger} content_type={content_type} body={body[:1000]}")
            log.warning(f"  data keys: {list(data.keys())}")
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error": "missing path"}')
            return

        log.info(f"Resolved path: {path}")

        log.info(f"Event received trigger={trigger} path={path}")
        process_event(path, trigger)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "accepted"}')

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass


def main():
    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), BridgeHandler)
    log.info(f"strm-bridge listening on {LISTEN_HOST}:{LISTEN_PORT}")
    log.info(f"  library: {LOCAL_LIBRARY}")
    log.info(f"  rclone:  {RCLONE_REMOTE}")
    log.info(f"  autoscan: {AUTOSCAN_URL}")
    log.info(f"  sync threshold: {SYNC_FILE_THRESHOLD} files")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
