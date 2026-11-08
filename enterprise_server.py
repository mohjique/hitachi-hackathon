#!/usr/bin/env python3
"""Local server for the mock enterprise systems. No cloud, stdlib only.

python enterprise_server.py --data data --events data/events_dev.json --out runs/dev [--port 8000]

GET  /events   the event feed (one JSON list)
GET  /tools    the tools your agents can call (name, description, arguments)
POST /act      {"event_id": "...", "tool": "mes.reschedule", "args": {...}}  ->  result or {"error": "..."}
Every call is appended to <out>/action_log.jsonl. That file is what you submit.
"""
import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from enterprise import TOOLS, Enterprise, ToolError, load_events


class Server(ThreadingHTTPServer):
    def server_bind(self):           # skip HTTPServer's reverse-DNS lookup, which can hang on some networks
        self.socket.bind(self.server_address)
        self.server_name, self.server_port = self.server_address[0], self.server_address[1]


def make_handler(ent, events, log_path, lock):
    known = {e["event_id"] for e in events}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, payload):
            data = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/events":
                return self._send(200, events)
            if self.path == "/tools":
                return self._send(200, [{"tool": k, **{f: v[f] for f in ("desc", "args")}} for k, v in TOOLS.items()])
            self._send(404, {"error": "use /events, /tools or POST /act"})

        def do_POST(self):
            if self.path != "/act":
                return self._send(404, {"error": "use POST /act"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                eid, tool, args = body["event_id"], body["tool"], body.get("args", {})
            except (ValueError, KeyError, TypeError):
                return self._send(400, {"error": 'body must be {"event_id", "tool", "args"}'})
            if eid not in known:
                return self._send(404, {"error": "unknown event_id"})
            with lock:
                try:
                    res, code, err = ent.call(eid, tool, args), 200, None
                except ToolError as e:
                    res, code, err = {"error": str(e)}, 400, str(e)
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"event_id": eid, "tool": tool, "args": args, "ok": err is None}) + "\n")
            self._send(code, res)

        def log_message(self, *a):
            pass

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--events", default=None)
    ap.add_argument("--out", default="runs/dev")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--overwrite", action="store_true", help="start over and replace an existing action_log.jsonl")
    args = ap.parse_args()
    events = load_events(args.events or os.path.join(args.data, "events.json"))
    os.makedirs(args.out, exist_ok=True)
    log_path = os.path.join(args.out, "action_log.jsonl")
    if os.path.exists(log_path) and os.path.getsize(log_path) and not args.overwrite:
        raise SystemExit(f"{log_path} already has calls. The world restarts empty, so use --overwrite or another --out.")
    open(log_path, "w").close()
    srv = Server(("127.0.0.1", args.port), make_handler(Enterprise(args.data), events, log_path, threading.Lock()))
    print(f"http://127.0.0.1:{args.port}/events   ({len(events)} events, log: {log_path})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
