#!/usr/bin/env python3
"""
app.py — the control tower as a local web app with a Play button.

    python app.py              # opens http://localhost:8080
    python app.py --refresh    # make Gemini re-read every message (ignore cached answers)
    python app.py --port 9000  # use another port for the page

What Play does (every time, from scratch)
-----------------------------------------
  1. Deletes runs/live/ completely, so nothing from an earlier run survives.
  2. Starts the ORGANISERS' server, unchanged, exactly as the challenge describes:
        python enterprise_server.py --data data --events data/events.json --out runs/live --port <free port> --overwrite
     A fresh server = a fresh, empty world, and a fresh runs/live/action_log.jsonl.
  3. Reads the events from that server (GET /events) and feeds them through the agents
     (run.py's LangGraph workflow, live mode), ONE EVENT EVERY 5 SECONDS. Every tool call
     goes to the server's POST /act, which writes it to action_log.jsonl.
  4. After each event: checks it against the handbook (check.py) and shows it on the page.
  5. At the end: writes runs/live/checks.json and a static copy of the page
     (runs/live/control_tower.html), then stops the server.

Press Play again (or restart this app and press Play) and all of runs/live/ is replaced.
Stop the app with Ctrl+C in the terminal; it also stops the enterprise server.

Only the standard library is used here, on top of the project's own modules.
"""

import atexit
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import check
import run as pipeline
from enterprise import Enterprise
from ui.build_report import build, event_payload, ontology_payload, render

HERE = os.path.dirname(os.path.abspath(__file__))

OUT_DIR = os.path.join(HERE, "runs", "live")
LOG_PATH = os.path.join(OUT_DIR, "action_log.jsonl")
# Tab icon: the coordinator's column-5 ("working") pose, cut by ui/make_sprites.py
FAVICON_PATH = os.path.join(HERE, "ui", "sprites", "coordinator_working.png")
SECONDS_PER_EVENT = 5

# ---- everything the page polls for; guarded by LOCK ------------------------------------
LOCK = threading.Lock()
STATE = {"status": "idle", "run_id": None, "total": 0, "events": [], "current": None,
         "next_id": None, "next_at": None, "error": None, "run": {}}
SERVER_PROC = None   # the enterprise_server.py subprocess while a run is going


# ======================================================================================
# The run (in a background thread, so the page stays responsive)
# ======================================================================================

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_enterprise_server():
    """Start the organisers' server as a separate process with a brand-new world and log."""
    global SERVER_PROC
    port = free_port()
    log = open(os.path.join(OUT_DIR, "server_console.txt"), "w")
    SERVER_PROC = subprocess.Popen(
        [sys.executable, "enterprise_server.py", "--data", "data", "--events", "data/events.json",
         "--out", os.path.relpath(OUT_DIR, HERE), "--port", str(port), "--overwrite"],
        cwd=HERE, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):                       # wait up to ~10 s for it to answer
        if SERVER_PROC.poll() is not None:
            raise RuntimeError("enterprise_server.py exited early; see runs/live/server_console.txt")
        try:
            with urllib.request.urlopen(url + "/events", timeout=1) as r:
                return url, json.loads(r.read())
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("enterprise_server.py did not start within 10 seconds")


def stop_enterprise_server():
    global SERVER_PROC
    if SERVER_PROC and SERVER_PROC.poll() is None:
        SERVER_PROC.terminate()
        try:
            SERVER_PROC.wait(timeout=5)
        except subprocess.TimeoutExpired:
            SERVER_PROC.kill()
    SERVER_PROC = None


atexit.register(stop_enterprise_server)


def _stop_on_signal(signum, frame):
    """Closing the terminal (SIGHUP) or `kill` (SIGTERM) behaves like Ctrl+C, so the
    enterprise server is always stopped together with this app."""
    raise KeyboardInterrupt


for _sig in (signal.SIGTERM, signal.SIGHUP):
    signal.signal(_sig, _stop_on_signal)


def run_all(options):
    """Play: wipe runs/live, start a fresh server, process the 15 events on a 5-second beat."""
    try:
        shutil.rmtree(OUT_DIR, ignore_errors=True)
        os.makedirs(OUT_DIR)
        url, events = start_enterprise_server()
        with LOCK:
            STATE["total"] = len(events)
        config = {"mode": "live", "url": url, "refresh": options["refresh"]}
        checker_world = Enterprise(os.path.join(HERE, "data"))
        results = []

        t0 = time.time()
        for i, ev in enumerate(events):
            due = t0 + i * SECONDS_PER_EVENT            # event i "arrives" at 5 s × i
            with LOCK:
                STATE.update(next_id=ev["event_id"], next_at=due, current=None)
            time.sleep(max(0.0, due - time.time()))
            with LOCK:
                STATE.update(current=ev["event_id"], next_id=None, next_at=None)

            trace = pipeline.process_event(APP, ev, config, os.path.join(OUT_DIR, "traces"))
            chk = check.check_event(checker_world, trace)
            results.append(chk)
            payload = event_payload(pipeline.GRAPH, trace, chk)
            with LOCK:
                STATE["events"].append(payload)
                STATE["current"] = None
                STATE["run"]["model"] = trace.get("model") or STATE["run"].get("model")

        with open(os.path.join(OUT_DIR, "checks.json"), "w") as f:
            json.dump(results, f, indent=2)
        build(OUT_DIR, g=pipeline.GRAPH)                # static copy of the page for this run
        pipeline.write_diagrams(APP, OUT_DIR)           # agent_graph.mmd + ontology.mmd for the submission
        with LOCK:
            STATE.update(status="done", next_id=None, next_at=None)
    except Exception as e:  # noqa: BLE001 — show any failure on the page instead of dying silently
        traceback.print_exc()
        with LOCK:
            STATE.update(status="error", error=f"{type(e).__name__}: {e}", current=None, next_id=None, next_at=None)
    finally:
        stop_enterprise_server()


# ======================================================================================
# The web page and its small API
# ======================================================================================

def snapshot(since):
    with LOCK:
        s = dict(STATE)
        s["events"] = STATE["events"][since:]
    next_at = s.pop("next_at")
    s["next_in"] = None if next_at is None else max(0.0, next_at - time.time())   # seconds until the next event
    s["log_ready"] = os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 0
    s["log_path"] = os.path.relpath(LOG_PATH, HERE)
    return s


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            page = render({"live": True, "run_id": None, "events": [],
                           "run": {"dir": "runs/live", "mode": "live", "model": None, "built": None},
                           "ontology": ONTOLOGY})
            return self._send(200, page, "text/html; charset=utf-8")
        if u.path == "/favicon.ico":
            if not os.path.exists(FAVICON_PATH):
                return self._send(404, json.dumps({"error": "run ui/make_sprites.py first"}))
            with open(FAVICON_PATH, "rb") as f:
                return self._send(200, f.read(), "image/png")
        if u.path == "/api/state":
            since = int(parse_qs(u.query).get("since", ["0"])[0])
            return self._send(200, json.dumps(snapshot(since), default=str))
        if u.path == "/api/action_log":
            if not os.path.exists(LOG_PATH):
                return self._send(404, json.dumps({"error": "no action_log.jsonl yet — press Play"}))
            with open(LOG_PATH, "rb") as f:
                return self._send(200, f.read(), "application/x-ndjson",
                                  {"Content-Disposition": 'attachment; filename="action_log.jsonl"'})
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if urlparse(self.path).path != "/api/play":
            return self._send(404, json.dumps({"error": "not found"}))
        with LOCK:
            if STATE["status"] == "running":
                return self._send(409, json.dumps({"error": "A run is already in progress."}))
            STATE.update(status="running", run_id=datetime.now().strftime("%Y%m%d-%H%M%S"), total=0, events=[],
                         current=None, next_id=None, next_at=None, error=None,
                         run={"dir": "runs/live", "mode": "live", "model": None,
                              "built": "started " + datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        threading.Thread(target=run_all, args=(OPTIONS,), daemon=True).start()
        self._send(200, json.dumps({"ok": True}))

    def log_message(self, *a):
        pass


# ======================================================================================
# Main
# ======================================================================================

APP = ONTOLOGY = OPTIONS = None


def main(argv):
    global APP, ONTOLOGY, OPTIONS
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else 8080
    OPTIONS = {"refresh": "--refresh" in argv}

    print("Building the knowledge graph and the agent workflow…")
    APP = pipeline.setup()
    ONTOLOGY = ontology_payload(pipeline.GRAPH)

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}"
    print(f"Control tower: {url}   (Ctrl+C to stop)")
    if "--no-browser" not in argv:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        stop_enterprise_server()
        srv.server_close()


if __name__ == "__main__":
    main(sys.argv[1:])
