#!/usr/bin/env python3
"""
run.py — Step 3: the control tower. LangGraph wires the agents into one workflow,
runs every event through it, calls the enterprise server, and writes a trace per event.

The workflow (generated from this code into runs/<run>/agent_graph.mmd)
-----------------------------------------------------------------------
    coordinator ─┬─ outage/delay ─▶ supply_chain_planner ─▶ dealer_customer_care ─┬─▶ quality ─┐
                 ├─ quality ──────────────────────────────────────────────────────┘           │
                 ├─ escalate only ───────────────────────────────────────────────────────────▶ dry_run
                 └─ noise only ──▶ summarise                                                    │
                                                    dry_run ─ ok ─────▶ execute ─▶ summarise ─▶ END
                                                            └ failed ─▶ fail_safe ─▶ execute

  coordinator           reads the message (incident_triage.py) and looks up what each real
                        incident touches (graph.py); decides the route
  supply_chain_planner  reschedules / transfers affected production orders  (agents.py)
  dealer_customer_care  one notice per dealer, loaners by priority          (agents.py)
  quality               hold lot, stop sale, contact owners                 (agents.py)
  dry_run               plays the whole plan against a private copy of the enterprise rules
  fail_safe             if ANY planned call would fail: drop the plan, escalate to a human
  execute               sends the calls to the server (live) or skips (dry mode)
  summarise             Gemini writes a short summary + reason from the facts in the trace

Why LangGraph: the routing above is declared once as a graph (and drawn from the code),
the agents share one typed state (slots + loaner pools for compound events), and every
node's output is recorded step by step, which becomes the audit trail.

How to run
----------
  # rehearsal: no server needed, nothing is logged for grading
  python run.py --dry

  # the graded run (start the server first, in another terminal):
  python enterprise_server.py --data data --events data/events.json --out runs/final --port 8000
  python run.py --live --out runs/final

  Options:  E-01 E-09 ...   only these events
            --no-summary    skip the Gemini summary (template sentence instead)
            --url URL       server address (default http://127.0.0.1:8000)
"""

import hashlib
import json
import operator
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

import agents
import graph as kg
import incident_triage as triage
from enterprise import Enterprise

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
SUMMARY_CACHE = os.path.join(HERE, "runs", "summary_cache")

# Read-only tools: free, never penalised, and not "actions" for rule 9.
READ_TOOLS = {"mes.get_schedule", "crm.loaner_availability"}


# ======================================================================================
# The shared state that flows through the graph (one per event)
# ======================================================================================

class EventState(TypedDict, total=False):
    event: dict                 # the raw event {event_id, received_at, source, text}
    config: dict                # mode ("dry"/"live"), server url, summary on/off
    triage: dict                # incident_triage.triage_event() output
    graph_results: dict         # incident index -> graph.py traversal result
    disruptions: list           # [(index, result)] outages + delays
    quality_findings: list      # [(index, result)] quality holds
    escalations: list           # [(index or None, reason)]
    notes: list                 # e.g. why noise was ignored
    shared: Any                 # agents.SharedState: slots + loaner pools (one per event)
    moves: dict                 # production order -> where the planner put it
    loaners: dict               # {"loaned": [...], "pool_empty": [...]}
    calls: Annotated[list, operator.add]     # planned calls; each agent ADDS its own
    failures: list              # dry-run failures
    results: list               # one result per executed call
    summary: dict               # {"summary": ..., "reason": ..., "by": "gemini"/"template"}
    visited: Annotated[list, operator.add]   # which nodes ran, in order (for the UI)
    log: Annotated[list, operator.add]       # human-readable trace lines, per node


def _log(node, text):
    return {"node": node, "text": text}


# ======================================================================================
# Nodes
# ======================================================================================

def coordinator(state: EventState):
    """Read the message, then look up every real incident in the knowledge graph."""
    ev = state["event"]
    tri = triage.triage_event(ev)                     # LLM + checks (cached per event)
    graph_results, disruptions, quality_findings, escalations, notes, log = {}, [], [], [], [], []
    g = GRAPH

    for i, inc in enumerate(tri["incidents"], 1):
        log.append(_log("coordinator", f"incident {i}: {inc['verdict']} {inc['category']}"
                        + (f" — {inc['verdict_reason']}" if inc.get("verdict_reason") else "")))
        if inc["verdict"] == "NO_ACTION":
            notes.append(f"incident {i}: noise, no action ({inc.get('llm_reason') or 'not about operations'})")
        elif inc["verdict"] == "ESCALATE":
            escalations.append((i, inc.get("verdict_reason") or "unclear report"))
        else:
            try:
                res = kg.affected(g, inc)
            except (ValueError, KeyError) as e:
                escalations.append((i, f"graph lookup failed: {e}"))
                log.append(_log("coordinator", f"incident {i}: graph lookup failed, escalating ({e})"))
                continue
            graph_results[i] = res
            (quality_findings if res["kind"] == "quality_hold" else disruptions).append((i, res))
            log.append(_log("coordinator", f"incident {i}: graph path {res['path_schema']}"))

    return {"triage": tri, "graph_results": graph_results, "disruptions": disruptions,
            "quality_findings": quality_findings, "escalations": escalations, "notes": notes,
            "shared": agents.SharedState(g), "visited": ["coordinator"], "log": log}


def supply_chain_planner(state: EventState):
    calls, moves, unplaced = agents.supply_chain_planner(state["shared"], state["disruptions"])
    transfers = sum(1 for c in calls if c["tool"] == "mes.transfer")
    escalations = state["escalations"] + [(None, f"no feasible slot for {po} in the planning window")
                                          for po in unplaced]
    return {"calls": calls, "moves": moves, "escalations": escalations, "visited": ["supply_chain_planner"],
            "log": [_log("supply_chain_planner", f"{len(calls)} orders placed ({transfers} transferred), "
                                                 f"{len(unplaced)} could not be placed")]}


def dealer_customer_care(state: EventState):
    calls, info = agents.dealer_customer_care(state["shared"], state["disruptions"], state.get("moves", {}))
    n = Counter(c["tool"] for c in calls)
    return {"calls": calls, "loaners": info, "visited": ["dealer_customer_care"],
            "log": [_log("dealer_customer_care", f"{n['dealer.notify']} dealer notices, {len(info['loaned'])} loaners "
                                                 f"assigned, {len(info['pool_empty'])} eligible without a loaner (pool empty)")]}


def quality(state: EventState):
    calls = agents.quality(state["quality_findings"], GRAPH)
    n = Counter(c["tool"] for c in calls)
    return {"calls": calls, "visited": ["quality"],
            "log": [_log("quality", f"{n['quality.hold_lot']} lot hold(s), {n['dealer.stop_sale']} stop-sale calls, "
                                    f"{n['crm.notify_customer']} owners contacted")]}


def dry_run(state: EventState):
    """Add escalations, then play the whole plan against a private copy of enterprise.py."""
    esc = [agents._call("escalate", {"reason": f"{state['event']['event_id']}: {r}"}, "coordinator",
                        [i] if i else [], r) for i, r in state["escalations"]]
    plan = {"event_id": state["event"]["event_id"], "calls": state["calls"] + esc}
    failures = agents.dry_run(ENTERPRISE, plan)
    return {"calls": esc, "failures": failures, "visited": ["dry_run"],
            "log": [_log("dry_run", f"{len(plan['calls'])} calls checked, {len(failures)} would fail")]}


def fail_safe(state: EventState):
    """A plan with any failing call is not trusted. Replace it with one escalation."""
    first = state["failures"][0]
    reason = (f"{state['event']['event_id']}: automatic plan rejected — {len(state['failures'])} call(s) "
              f"would fail (first: {first['tool']} {first['error']}). Needs a human.")
    return {"escalations": [(None, reason)], "visited": ["fail_safe"],
            "log": [_log("fail_safe", reason)]}


def execute(state: EventState):
    """Send the calls in order. Dry mode: nothing is sent (the dry run already checked them)."""
    calls = _final_calls(state)
    cfg = state["config"]
    results = []
    if cfg["mode"] == "dry":
        results = [{"ok": True, "sent": False} for _ in calls]
        note = f"dry mode: {len(calls)} calls not sent"
    else:
        for c in calls:
            res = _post(cfg["url"], state["event"]["event_id"], c["tool"], c["args"])
            results.append(res)
        bad = [r for r in results if not r["ok"]]
        note = f"{len(calls)} calls sent, {len(bad)} failed"
    return {"results": results, "visited": ["execute"], "log": [_log("execute", note)]}


def summarise(state: EventState):
    facts = _facts(state)
    summary = _llm_summary(facts) if state["config"].get("summary", True) else None
    if not summary:
        summary = {"summary": _template_summary(facts), "reason": facts["reason_hint"], "by": "template"}
    return {"summary": summary, "visited": ["summarise"], "log": [_log("summarise", summary["summary"])]}


# ---- routing ----------------------------------------------------------------------------

def route_after_coordinator(state: EventState):
    if state["disruptions"]:
        return "supply_chain_planner"
    if state["quality_findings"]:
        return "quality"
    if state["escalations"]:
        return "dry_run"
    return "summarise"                    # noise only: nothing to do


def route_after_care(state: EventState):
    return "quality" if state["quality_findings"] else "dry_run"


def route_after_dry_run(state: EventState):
    return "fail_safe" if state["failures"] else "execute"


def build_app():
    g = StateGraph(EventState)
    for name, fn in [("coordinator", coordinator), ("supply_chain_planner", supply_chain_planner),
                     ("dealer_customer_care", dealer_customer_care), ("quality", quality),
                     ("dry_run", dry_run), ("fail_safe", fail_safe), ("execute", execute),
                     ("summarise", summarise)]:
        g.add_node(name, fn)
    g.add_edge(START, "coordinator")
    g.add_conditional_edges("coordinator", route_after_coordinator,
                            ["supply_chain_planner", "quality", "dry_run", "summarise"])
    g.add_edge("supply_chain_planner", "dealer_customer_care")
    g.add_conditional_edges("dealer_customer_care", route_after_care, ["quality", "dry_run"])
    g.add_edge("quality", "dry_run")
    g.add_conditional_edges("dry_run", route_after_dry_run, ["execute", "fail_safe"])
    g.add_edge("fail_safe", "execute")
    g.add_edge("execute", "summarise")
    g.add_edge("summarise", END)
    return g.compile()


# ======================================================================================
# Helpers
# ======================================================================================

def _final_calls(state):
    """The calls to execute. After fail_safe, only the escalation survives."""
    if "fail_safe" in state.get("visited", []):
        reason = state["escalations"][-1][1]
        return [agents._call("escalate", {"reason": reason}, "coordinator", [], reason)]
    return state.get("calls", [])


def _post(url, event_id, tool, args):
    body = json.dumps({"event_id": event_id, "tool": tool, "args": args}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/act", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return {"ok": True, "sent": True, "response": json.loads(r.read())}
    except urllib.error.HTTPError as e:      # 400 = the tool refused; logged, never retried blindly
        try:
            payload = json.loads(e.read())
        except ValueError:
            payload = {"error": str(e)}
        return {"ok": False, "sent": True, "response": payload}
    except (urllib.error.URLError, TimeoutError) as e:
        return {"ok": False, "sent": False, "response": {"error": f"server unreachable: {e}"}}


def _facts(state):
    """Everything the summary may say — and nothing else."""
    calls = _final_calls(state)
    tools = Counter(c["tool"] for c in calls)
    incidents = [{"verdict": i["verdict"], "category": i["category"], "plant": i.get("plant_id"),
                  "part": i.get("part_id"), "shipment": i.get("shipment_id"), "lots": i.get("lots"),
                  "dates": [i.get("start"), i.get("end")], "reason": i.get("verdict_reason") or i.get("llm_reason")}
                 for i in state["triage"]["incidents"]]
    moves = state.get("moves") or {}
    transfers = {po: m["to"] for po, m in moves.items() if m["how"] == "transfer"}
    if "fail_safe" in state.get("visited", []):
        hint = "The automatic plan failed its safety check, so it was handed to a human."
    elif all(i["verdict"] == "NO_ACTION" for i in incidents):
        hint = "The message is not about operations, so no action was taken."
    elif all(i["verdict"] == "ESCALATE" for i in incidents):
        hint = "The report is unconfirmed or not specific enough to act on, so it was escalated."
    else:
        hint = "Actions follow the policy handbook for each confirmed incident."
    return {"event_id": state["event"]["event_id"], "message": state["event"]["text"],
            "incidents": incidents, "tool_counts": dict(tools),
            "orders_moved": len(moves), "transfers": transfers,
            "loaners_assigned": len((state.get("loaners") or {}).get("loaned", [])),
            "loaner_shortfall": len((state.get("loaners") or {}).get("pool_empty", [])),
            "failed_calls": sum(1 for r in state.get("results", []) if not r["ok"]),
            "reason_hint": hint}


def _template_summary(f):
    parts = [f"{i['verdict'].replace('_', ' ').lower()} {i['category'].replace('_', ' ')}" for i in f["incidents"]]
    t = f["tool_counts"]
    acts = ", ".join(f"{n} {k}" for k, n in sorted(t.items())) or "no calls"
    return f"{f['event_id']}: {'; '.join(parts)}. Calls: {acts}."


SUMMARY_PROMPT = """You write the audit summary for one event handled by an automotive operations
control tower. Use ONLY the facts below. Do not invent numbers, names or dates.
Return JSON with:
  "summary": 2-3 plain sentences: what the message reported and what was done (with counts).
  "reason": 1 sentence: why this was the right response under the policy handbook
            (e.g. noise -> no action; unconfirmed -> escalate; real incident -> rule-based actions).

FACTS:
{facts}
"""


def _llm_summary(facts):
    """Gemini writes the summary from the facts. Cached by the facts' fingerprint,
    so re-runs are free and identical. Returns None on any failure (template is used)."""
    os.makedirs(SUMMARY_CACHE, exist_ok=True)
    blob = json.dumps(facts, sort_keys=True, default=str)
    key = hashlib.sha256(blob.encode()).hexdigest()[:16]
    path = os.path.join(SUMMARY_CACHE, f"{facts['event_id']}-{key}.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    try:
        from google import genai
        from google.genai import types
        api_key = os.environ.get("GEMINI_API_KEY")       # loaded from .env by incident_triage
        if not api_key:
            return None
        client = genai.Client(api_key=api_key)
        r = client.models.generate_content(
            model=triage.GEMINI_MODEL,
            contents=SUMMARY_PROMPT.format(facts=blob),
            config=types.GenerateContentConfig(
                response_mime_type="application/json", temperature=0,
                response_schema={"type": "OBJECT", "properties": {"summary": {"type": "STRING"},
                                                                  "reason": {"type": "STRING"}},
                                 "required": ["summary", "reason"]}))
        out = json.loads(r.text)
        out["by"] = triage.GEMINI_MODEL
    except Exception as e:  # noqa: BLE001 — a summary must never break the run
        print(f"  (summary fell back to template: {e})")
        return None
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    return out


# ======================================================================================
# Main
# ======================================================================================

GRAPH = None        # the knowledge graph (built once)
ENTERPRISE = None   # local copy of the enterprise rules, for dry runs


def main(argv):
    global GRAPH, ENTERPRISE
    live = "--live" in argv
    mode = "live" if live else "dry"
    url = argv[argv.index("--url") + 1] if "--url" in argv else "http://127.0.0.1:8000"
    out_dir = argv[argv.index("--out") + 1] if "--out" in argv else os.path.join("runs", mode)
    skip = {url, out_dir}
    wanted = {a.upper() for a in argv if not a.startswith("--") and a not in skip}
    trace_dir = os.path.join(HERE, out_dir, "traces")

    if live:
        # Events come from the server, exactly as the judges' run sees them.
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/events", timeout=10) as r:
                events = json.loads(r.read())
        except (urllib.error.URLError, TimeoutError) as e:
            sys.exit(f"Cannot reach the server at {url} ({e}). Start enterprise_server.py first.")
        if os.path.isdir(trace_dir) and os.listdir(trace_dir) and "--overwrite" not in argv:
            sys.exit(f"{trace_dir} already has traces. Use a fresh --out (and a fresh server log).")
    else:
        with open(os.path.join(DATA_DIR, "events.json"), encoding="utf-8") as f:
            events = json.load(f)
    if wanted:
        events = [e for e in events if e["event_id"] in wanted]

    GRAPH = kg.build_graph()
    ENTERPRISE = Enterprise(DATA_DIR)
    app = build_app()
    os.makedirs(trace_dir, exist_ok=True)
    with open(os.path.join(HERE, out_dir, "agent_graph.mmd"), "w") as f:
        f.write(app.get_graph().draw_mermaid())

    config = {"mode": mode, "url": url, "summary": "--no-summary" not in argv}
    print(f"Mode: {mode.upper()}  |  {len(events)} events  |  traces -> {trace_dir}")
    totals = Counter()
    for ev in events:
        t0 = time.time()
        final = app.invoke({"event": ev, "config": config, "calls": [], "visited": [], "log": []})
        calls = _final_calls(final)
        results = final.get("results", [])
        failed = sum(1 for r in results if not r["ok"])
        kinds = {c["tool"] for c in calls if c["tool"] not in READ_TOOLS}
        totals.update(calls=len(calls), failed=failed)

        trace = {
            "event": ev,
            "mode": mode,
            "model": final["triage"].get("model"),
            "agents_visited": final["visited"],
            "triage": final["triage"],
            "graph": {str(i): {k: r[k] for k in ("kind", "path_schema", "notes", "example_path", "edges")}
                      for i, r in final.get("graph_results", {}).items()},
            "notes": final.get("notes", []),
            "calls": [{**c, "result": r} for c, r in zip(calls, results)],
            "dry_run_failures": final.get("failures", []),
            "loaners": final.get("loaners"),
            "summary": final["summary"],
            "log": final["log"],
            "seconds": round(time.time() - t0, 2),
        }
        with open(os.path.join(trace_dir, f"{ev['event_id']}.json"), "w", encoding="utf-8") as f:
            json.dump(trace, f, indent=2, default=str)

        print("=" * 88)
        print(f"{ev['event_id']}  path: {' → '.join(final['visited'])}")
        print(f"  {len(calls)} calls ({len(kinds)} kinds of action), {failed} failed"
              + ("  [FAIL-SAFE: escalated]" if "fail_safe" in final["visited"] else ""))
        print(f"  {final['summary']['summary']}")
        print(f"  why: {final['summary']['reason']}  [{final['summary']['by']}]")

    print("=" * 88)
    print(f"Done: {totals['calls']} calls, {totals['failed']} failed. Agent diagram: {out_dir}/agent_graph.mmd")
    if live:
        print(f"Submit: {out_dir}/action_log.jsonl (written by the server) + {out_dir}/traces/")


if __name__ == "__main__":
    main(sys.argv[1:])
