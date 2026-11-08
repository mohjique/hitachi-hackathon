#!/usr/bin/env python3
"""
ui/build_report.py — Step 4b: builds the CONTROL TOWER page from a finished run.

It reads the run's traces (written by run.py) and policy checks (written by check.py),
looks up display details for every graph node, and writes ONE self-contained HTML file:

    <run>/control_tower.html      open it in any browser; works offline

The page (design lives in ui/template.html, data is injected below):
  * Data model    the ontology: 11 node types, 16 relationships; the ones the selected
                  incident walks are drawn in dark ink
  * Event feed    all 15 events with their verdicts and summaries
  * Event detail  message, triage, summary + reason, policy checks, agent flow (LangGraph
                  path), graph path explorer (every affected record), action log, audit trail

How to run
----------
  python ui/build_report.py              # runs/final if it exists, else runs/dry
  python ui/build_report.py runs/dry     # a specific run
"""

import glob
import json
import os
import sys
from collections import Counter
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import graph as kg  # noqa: E402  (needs the sys.path line above)

READ_TOOLS = {"mes.get_schedule", "crm.loaner_availability"}
KIND_LABEL = {"plant_outage": "Plant outage", "supplier_delay": "Supplier delay",
              "quality_hold": "Quality hold", "noise": "Noise", "unconfirmed": "Unconfirmed report"}


def node_info(g, n):
    """One line of human detail per node type, for tooltips."""
    a = g.nodes[n]
    t = a["type"]
    text = {
        "Plant": lambda: f'{a["name"]} · {a["slots_per_day"]} slots/day',
        "Part": lambda: a["name"],
        "Model": lambda: a["segment"],
        "Supplier": lambda: a["name"],
        "Shipment": lambda: f'due {a["eta_date"]}',
        "Lot": lambda: f'built {a["first_build_date"]} → {a["last_build_date"]}',
        "PO": lambda: f'{a["model"]} · {a["plant_id"]} · {a["build_date"]} · {a["units"]} units',
        "CO": lambda: f'{a["model"]} · promised {a["promised_date"]}',
        "Customer": lambda: f'{a["name"]} · {a["tier"]}',
        "Dealer": lambda: f'{a["name"]}, {a["city"]} · {a["loaners_available"]} loaners',
        "Vehicle": lambda: f'{a["model"]} · built {a["build_date"]} · {a["status"].replace("_", " ")}',
    }[t]()
    d = {"id": n, "t": t, "k": a["id"], "i": text}
    if t == "Customer":
        d["tier"] = a["tier"]
    if t == "Vehicle":
        d["st"] = a["status"]
    return d


def short_args(tool, a):
    """A compact one-liner of a call's arguments for the action table."""
    if tool == "mes.reschedule":
        return f'{a["production_order_id"]} → {a["new_date"]}'
    if tool == "mes.transfer":
        return f'{a["production_order_id"]} → {a["to_plant_id"]} {a["new_date"]}'
    if tool == "dealer.notify":
        return f'{a["dealer_id"]} · {len(a["customer_order_ids"])} order(s)'
    if tool == "dealer.stop_sale":
        return f'{a["dealer_id"]} · {len(a["vins"])} vehicle(s)'
    if tool == "crm.assign_loaner":
        return a["customer_order_id"]
    if tool == "crm.loaner_availability":
        return a["dealer_id"]
    if tool == "crm.notify_customer":
        return a["customer_id"]
    if tool == "quality.hold_lot":
        return a["lot_id"]
    if tool == "escalate":
        return a["reason"][:90]
    return json.dumps(a)[:90]


def build(run_dir):
    files = sorted(glob.glob(os.path.join(run_dir, "traces", "E-*.json")))
    if not files:
        sys.exit(f"No traces in {run_dir}/traces — run run.py first.")
    checks = {}
    cpath = os.path.join(run_dir, "checks.json")
    if os.path.exists(cpath):
        checks = {c["event_id"]: c for c in json.load(open(cpath))}

    g = kg.build_graph()
    node_counts, edge_counts = kg.ontology(g)
    events, totals = [], Counter()
    mode = model = None

    for path in files:
        t = json.load(open(path, encoding="utf-8"))
        mode, model = t.get("mode"), t.get("model") or model
        ev = t["event"]
        incidents = []
        for inc in t["triage"]["incidents"]:
            incidents.append({k: inc.get(k) for k in ("verdict", "category", "plant_id", "part_id", "supplier_id",
                                                      "shipment_id", "lots", "start", "end", "verdict_reason",
                                                      "llm_reason", "trace", "quote")})
        graphs = []
        for idx, gr in sorted(t.get("graph", {}).items(), key=lambda kv: int(kv[0])):
            nodes = sorted({u for u, _, v in gr["edges"]} | {v for u, _, v in gr["edges"]})
            types = Counter(g.nodes[n]["type"] for n in nodes)
            graphs.append({"idx": int(idx), "kind": gr["kind"], "label": KIND_LABEL[gr["kind"]],
                           "schema": gr["path_schema"], "notes": gr["notes"],
                           "counts": dict(types), "rels": sorted({r for _, r, _ in gr["edges"]}),
                           "nodes": [node_info(g, n) for n in nodes],
                           "edges": [[u, r, v] for u, r, v in gr["edges"]]})
        calls = []
        for c in t["calls"]:
            res = c.get("result", {})
            calls.append({"tool": c["tool"], "agent": c["agent"], "short": short_args(c["tool"], c["args"]),
                          "args": c["args"], "why": c.get("why", ""), "path": c.get("path", []),
                          "incidents": c.get("incidents", []), "ok": res.get("ok", True),
                          "sent": res.get("sent", False), "response": res.get("response")})
        n_calls = len(calls)
        failed = sum(1 for c in calls if not c["ok"])
        totals.update(events=1, calls=n_calls, failed=failed,
                      escalated=int(any(c["tool"] == "escalate" for c in calls)))
        chk = checks.get(ev["event_id"])
        if chk:
            totals.update(checked=1, passed=int(not chk["problems"]))
        events.append({
            "id": ev["event_id"], "received_at": ev.get("received_at"), "source": ev.get("source"),
            "text": ev["text"], "visited": t["agents_visited"], "incidents": incidents,
            "summary": t["summary"], "checks": chk, "graphs": graphs, "calls": calls,
            "log": t.get("log", []), "loaners": t.get("loaners"), "notes": t.get("notes", []),
            "kinds": sorted({c["tool"] for c in calls if c["tool"] not in READ_TOOLS}),
            "seconds": t.get("seconds"),
        })

    data = {
        "run": {"dir": os.path.relpath(run_dir, ROOT), "mode": mode, "model": model,
                "built": datetime.now().strftime("%Y-%m-%d %H:%M"), **totals},
        "ontology": {"nodes": dict(node_counts),
                     "edges": [[a, r, b] for (a, r, b) in sorted(edge_counts)],
                     "graph_nodes": g.number_of_nodes(), "graph_edges": g.number_of_edges()},
        "events": events,
    }
    with open(os.path.join(HERE, "template.html"), encoding="utf-8") as f:
        html = f.read()
    blob = json.dumps(data, separators=(",", ":"), default=str).replace("</", "<\\/")
    out = os.path.join(run_dir, "control_tower.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html.replace("__DATA__", blob))
    return out, totals


def main(argv):
    run = argv[0] if argv else ("runs/final" if os.path.isdir(os.path.join(ROOT, "runs/final/traces")) else "runs/dry")
    run_dir = run if os.path.isabs(run) else os.path.join(ROOT, run)
    out, totals = build(run_dir)
    print(f"{totals['events']} events, {totals['calls']} calls, {totals['failed']} failed"
          + (f", checks {totals['passed']}/{totals['checked']} passed" if totals["checked"] else ", no checks.json yet"))
    print(f"Open: {out}")


if __name__ == "__main__":
    main(sys.argv[1:])
