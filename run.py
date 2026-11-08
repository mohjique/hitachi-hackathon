#!/usr/bin/env python3
"""
run.py — Step 3: the control tower. LangGraph wires the agents into one workflow,
runs every event through it, calls the enterprise server, and writes a trace per event.

The workflow (generated from this code into runs/<run>/agent_graph.mmd)
-----------------------------------------------------------------------
Node names are "<agent>_<what it does>", so the diagram reads as a sentence and the four
agents (coordinator + three specialists) stay visible.

  coordinator_read_message
    ├─ noise only ──────▶ write_audit_summary ─▶ END
    ├─ escalate only ───▶ safety_check_plan
    └─ real incident ───▶ coordinator_find_affected
                            ├─ outage / delay ─▶ planner_reschedule_builds ─▶ dispatcher_notify_and_loan
                            │                      ─▶ inspector_hold_and_recall (only if there is also a quality hold)
                            │                      ─▶ safety_check_plan
                            └─ quality hold ───▶ inspector_hold_and_recall ─▶ safety_check_plan

  safety_check_plan
    ├─ every call OK ───▶ send_to_server ─▶ write_audit_summary ─▶ END
    └─ any call fails ──▶ reject_plan_escalate ─▶ send_to_server ─▶ write_audit_summary ─▶ END

  coordinator_read_message    Coordinator: the LLM reads the message (incident_triage.py) ->
                              ACT / ESCALATE / NO ACTION per incident
  coordinator_find_affected   Coordinator: walks the knowledge graph (graph.py) for every ACT
                              incident and picks the specialists
  planner_reschedule_builds   Supply Chain Planner: reschedule / transfer affected orders (agents.py)
  dispatcher_notify_and_loan  Customer Care Dispatcher: one notice per dealer, loaners by priority
  inspector_hold_and_recall   Quality Inspector: hold lot, stop sale, contact owners
  safety_check_plan           plays the whole plan against a private copy of the enterprise rules
  reject_plan_escalate        if ANY planned call would fail: drop the plan, escalate to a human
  send_to_server              sends the calls to the server (live) or skips (dry mode)
  write_audit_summary         what each agent did, built from the trace; no LLM

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
            --url URL       server address (default http://127.0.0.1:8000)
"""

import json
import operator
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

import agents
import graph as kg
import incident_triage as triage
from enterprise import Enterprise

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")

# Read-only tools: free, never penalised, and not "actions" for rule 9.
READ_TOOLS = {"mes.get_schedule", "crm.loaner_availability"}


# ======================================================================================
# The shared state that flows through the graph (one per event)
# ======================================================================================

class EventState(TypedDict, total=False):
    event: dict                 # the raw event {event_id, received_at, source, text}
    config: dict                # mode ("dry"/"live"), server url, refresh
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
    summary: dict               # one line per agent + "summary" (all of them) + "by"
    visited: Annotated[list, operator.add]   # which nodes ran, in order (for the UI)
    log: Annotated[list, operator.add]       # human-readable trace lines, per node


def _log(node, text):
    return {"node": node, "text": text}


# Node names: "<agent>_<what it does>" so the drawn graph reads at a glance.
READ_MESSAGE = "coordinator_read_message"     # Coordinator, LLM step
FIND_AFFECTED = "coordinator_find_affected"   # Coordinator, knowledge-graph step
RESCHEDULE = "planner_reschedule_builds"      # Supply Chain Planner
NOTIFY_LOAN = "dispatcher_notify_and_loan"          # Customer Care Dispatcher
HOLD_RECALL = "inspector_hold_and_recall"       # Quality Inspector
SAFETY_CHECK = "safety_check_plan"            # guardrail: dry run against enterprise.py
REJECT = "reject_plan_escalate"               # guardrail: failing plan -> human
SEND = "send_to_server"
SUMMARY = "write_audit_summary"               # built from the trace, no LLM


# ======================================================================================
# Nodes
# ======================================================================================

def coordinator_read_message(state: EventState):
    """Coordinator, step 1: the LLM reads the message; code checks every value it returns.
    Each incident ends as ACT, ESCALATE or NO_ACTION (incident_triage.py)."""
    ev = state["event"]
    tri = triage.triage_event(ev, refresh=state["config"].get("refresh", False))  # cached per event
    escalations, notes, log = [], [], []
    for i, inc in enumerate(tri["incidents"], 1):
        log.append(_log(READ_MESSAGE, f"incident {i}: {inc['verdict']} {inc['category']}"
                        + (f" — {inc['verdict_reason']}" if inc.get("verdict_reason") else "")))
        if inc["verdict"] == "NO_ACTION":
            notes.append(f"incident {i}: noise, no action ({inc.get('llm_reason') or 'not about operations'})")
        elif inc["verdict"] == "ESCALATE":
            escalations.append((i, inc.get("verdict_reason") or "unclear report"))
    return {"triage": tri, "escalations": escalations, "notes": notes,
            "graph_results": {}, "disruptions": [], "quality_findings": [],
            "visited": [READ_MESSAGE], "log": log}


def coordinator_find_affected(state: EventState):
    """Coordinator, step 2: walk the knowledge graph for every ACT incident (graph.py)
    and sort the results for the specialists."""
    graph_results, disruptions, quality_findings, log = {}, [], [], []
    escalations = list(state["escalations"])
    for i, inc in enumerate(state["triage"]["incidents"], 1):
        if inc["verdict"] != "ACT":
            continue
        try:
            res = kg.affected(GRAPH, inc)
        except (ValueError, KeyError) as e:
            escalations.append((i, f"graph lookup failed: {e}"))
            log.append(_log(FIND_AFFECTED, f"incident {i}: graph lookup failed, escalating ({e})"))
            continue
        graph_results[i] = res
        (quality_findings if res["kind"] == "quality_hold" else disruptions).append((i, res))
        log.append(_log(FIND_AFFECTED, f"incident {i}: graph path {res['path_schema']}"))
    return {"graph_results": graph_results, "disruptions": disruptions, "quality_findings": quality_findings,
            "escalations": escalations, "shared": agents.SharedState(GRAPH),
            "visited": [FIND_AFFECTED], "log": log}


def planner_reschedule_builds(state: EventState):
    """Supply Chain Planner: move every affected production order to its earliest legal slot."""
    calls, moves, unplaced = agents.supply_chain_planner(state["shared"], state["disruptions"])
    transfers = sum(1 for c in calls if c["tool"] == "mes.transfer")
    escalations = state["escalations"] + [(None, f"no feasible slot for {po} in the planning window")
                                          for po in unplaced]
    return {"calls": calls, "moves": moves, "escalations": escalations, "visited": [RESCHEDULE],
            "log": [_log(RESCHEDULE, f"{len(calls)} orders placed ({transfers} transferred), "
                                     f"{len(unplaced)} could not be placed")]}


def dispatcher_notify_and_loan(state: EventState):
    """Customer Care Dispatcher: one notice per dealer, loaners by priority."""
    calls, info = agents.customer_care_dispatcher(state["shared"], state["disruptions"], state.get("moves", {}))
    n = Counter(c["tool"] for c in calls)
    return {"calls": calls, "loaners": info, "visited": [NOTIFY_LOAN],
            "log": [_log(NOTIFY_LOAN, f"{n['dealer.notify']} dealer notices, {len(info['loaned'])} loaners "
                                      f"assigned, {len(info['pool_empty'])} eligible without a loaner (pool empty)")]}


def inspector_hold_and_recall(state: EventState):
    """Quality Inspector: hold the lot, stop sale of unsold vehicles, contact owners of sold ones."""
    calls = agents.quality_inspector(state["quality_findings"], GRAPH)
    n = Counter(c["tool"] for c in calls)
    return {"calls": calls, "visited": [HOLD_RECALL],
            "log": [_log(HOLD_RECALL, f"{n['quality.hold_lot']} lot hold(s), {n['dealer.stop_sale']} stop-sale calls, "
                                      f"{n['crm.notify_customer']} owners contacted")]}


def safety_check_plan(state: EventState):
    """Add escalations, then play the whole plan against a private copy of enterprise.py."""
    esc = [agents._call("escalate", {"reason": f"{state['event']['event_id']}: {r}"}, "coordinator",
                        [i] if i else [], r) for i, r in state["escalations"]]
    plan = {"event_id": state["event"]["event_id"], "calls": state["calls"] + esc}
    failures = agents.dry_run(ENTERPRISE, plan)
    return {"calls": esc, "failures": failures, "visited": [SAFETY_CHECK],
            "log": [_log(SAFETY_CHECK, f"{len(plan['calls'])} calls checked, {len(failures)} would fail")]}


def reject_plan_escalate(state: EventState):
    """A plan with any failing call is not trusted. Replace it with one escalation."""
    first = state["failures"][0]
    reason = (f"{state['event']['event_id']}: automatic plan rejected — {len(state['failures'])} call(s) "
              f"would fail (first: {first['tool']} {first['error']}). Needs a human.")
    return {"escalations": [(None, reason)], "visited": [REJECT], "log": [_log(REJECT, reason)]}


def send_to_server(state: EventState):
    """Send the calls in order. Dry mode: nothing is sent (the safety check already ran them)."""
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
    return {"results": results, "visited": [SEND], "log": [_log(SEND, note)]}


def write_audit_summary(state: EventState):
    """Coordinator: one line per agent on what it did, assembled from the incident schema and
    the plan (see _build_summary). No LLM call: every fact is already in the trace."""
    summary = _build_summary(state)
    return {"summary": summary, "visited": [SUMMARY], "log": [_log(SUMMARY, summary["summary"])]}


# ---- routing ----------------------------------------------------------------------------

def route_after_read(state: EventState):
    if any(i["verdict"] == "ACT" for i in state["triage"]["incidents"]):
        return FIND_AFFECTED
    if state["escalations"]:
        return SAFETY_CHECK               # escalate only: no graph lookup needed
    return SUMMARY                        # noise only: nothing to do


def route_after_find(state: EventState):
    if state["disruptions"]:
        return RESCHEDULE
    if state["quality_findings"]:
        return HOLD_RECALL
    return SAFETY_CHECK                   # every lookup failed -> escalations only


def route_after_care(state: EventState):
    return HOLD_RECALL if state["quality_findings"] else SAFETY_CHECK


def route_after_check(state: EventState):
    return REJECT if state["failures"] else SEND


def build_app():
    g = StateGraph(EventState)
    for name, fn in [(READ_MESSAGE, coordinator_read_message), (FIND_AFFECTED, coordinator_find_affected),
                     (RESCHEDULE, planner_reschedule_builds), (NOTIFY_LOAN, dispatcher_notify_and_loan),
                     (HOLD_RECALL, inspector_hold_and_recall), (SAFETY_CHECK, safety_check_plan),
                     (REJECT, reject_plan_escalate), (SEND, send_to_server), (SUMMARY, write_audit_summary)]:
        g.add_node(name, fn)
    g.add_edge(START, READ_MESSAGE)
    g.add_conditional_edges(READ_MESSAGE, route_after_read, [FIND_AFFECTED, SAFETY_CHECK, SUMMARY])
    g.add_conditional_edges(FIND_AFFECTED, route_after_find, [RESCHEDULE, HOLD_RECALL, SAFETY_CHECK])
    g.add_edge(RESCHEDULE, NOTIFY_LOAN)
    g.add_conditional_edges(NOTIFY_LOAN, route_after_care, [HOLD_RECALL, SAFETY_CHECK])
    g.add_edge(HOLD_RECALL, SAFETY_CHECK)
    g.add_conditional_edges(SAFETY_CHECK, route_after_check, [SEND, REJECT])
    g.add_edge(REJECT, SEND)
    g.add_edge(SEND, SUMMARY)
    g.add_edge(SUMMARY, END)
    return g.compile()


# ======================================================================================
# Helpers
# ======================================================================================

def _final_calls(state):
    """The calls to execute. After reject_plan_escalate, only the escalation survives."""
    if REJECT in state.get("visited", []):
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


# ---- the audit summary: built from the incident schema and the plan, no LLM ----------------
# Every number, name and date comes from the trace, so the text can't contradict the actions,
# is identical on every run, and costs no tokens.

def _nice(d):
    return agents._nice(str(d)) if d else "?"


def _span(a, b):
    """Mar 19 / Mar 19–22 / Mar 31–Apr 2"""
    a, b = _nice(a), _nice(b)
    if a == b:
        return a
    return f"{a}–{b.split()[1]}" if a.split()[0] == b.split()[0] else f"{a}–{b}"


def _name(kind, key):
    """Readable name from the knowledge graph, e.g. Plant P_KY -> 'Lexington (P_KY)'."""
    node = GRAPH.nodes.get(kg.nid(kind, key)) if key else None
    if not node or "name" not in node:
        return key or "?"
    name = node["name"].replace("Wacky Wheels ", "")
    return f"{name} ({key})" if kind == "Plant" else name


def _n(count, one, many=None):
    return f"{count} {one if count == 1 else (many or one + 's')}"


def _describe(inc, res):
    """One sentence on what an incident is."""
    cat, verdict = inc["category"], inc["verdict"]
    if verdict == "NO_ACTION":
        why = (inc.get("llm_reason") or "").rstrip(".")
        return "The message is not about operations" + (f": {why[0].lower() + why[1:]}." if why else ".")
    if verdict == "ESCALATE":
        return f"Not actionable: {(inc.get('verdict_reason') or 'unclear report').rstrip('.')}."
    if cat == "plant_outage":
        return f"{_name('Plant', inc['plant_id'])} is down {_span(inc['start'], inc['end'])}."
    if cat == "supplier_delay":
        return (f"Shipment {inc['shipment_id']} of {_name('Part', inc['part_id'])} from {_name('Supplier', inc['supplier_id'])} "
                f"to {_name('Plant', inc['plant_id'])} is late: due {_nice(inc['start'])}, now arriving {_nice(inc['end'])}.")
    if cat == "quality_hold" and res:
        return (f"Lot {res['lot_id']} of {_name('Part', res['part_id'])} from {_name('Supplier', inc.get('supplier_id'))} is defective; "
                f"it went into vehicles built at {_name('Plant', res['plant_id'])} {_span(res['window']['first'], res['window']['last'])}.")
    return f"{cat.replace('_', ' ').capitalize()}."


def _build_summary(state):
    """What each agent says about this event, assembled from the trace (no LLM).

    coordinator: what the message reports (or why nothing / a human is needed)
    planner:     where the affected production orders went
    dispatcher:  dealer notices and loaners
    inspector:   lot hold, stop sales, owners contacted
    summary:     all of the above in one paragraph (pop-up header, terminal, trace)
    An agent that did not take part gets None."""
    calls = _final_calls(state)
    incidents = state["triage"]["incidents"]
    graph_results = state.get("graph_results") or {}
    out = {"coordinator": None, "planner": None, "dispatcher": None, "inspector": None}

    # Coordinator: what the message says
    coord = [_describe(inc, graph_results.get(i)) for i, inc in enumerate(incidents, 1)]
    if REJECT in state.get("visited", []):
        coord.append("The plan failed its safety check, so nothing was changed and the event went to a human.")
    else:
        if any(c["tool"] == "escalate" for c in calls):
            coord.append("Escalated to a human.")
        if not calls:
            coord.append("No action taken.")
    out["coordinator"] = " ".join(coord)

    if REJECT not in state.get("visited", []):
        # Supply Chain Planner: where the orders went
        moves = state.get("moves") or {}
        same = defaultdict(list)
        for po, m in moves.items():
            if m["how"] == "reschedule":
                same[m["to"][0]].append(m["to"][1])
        transfers = [(po, m["to"]) for po, m in sorted(moves.items()) if m["how"] == "transfer"]
        parts = [f"{_n(len(days), 'production order') if k == 0 else len(days)} at {_name('Plant', plant)} for {_span(min(days), max(days))}"
                 for k, (plant, days) in enumerate(sorted(same.items()))]
        moved = ("Rescheduled " + "; ".join(parts)) if parts else ""
        if transfers:
            shown = ", ".join(f"{po} to {plant} on {_nice(day)}" for po, (plant, day) in transfers[:3])
            t = f"transferred {_n(len(transfers), 'order')} to another plant ({shown}{', ...' if len(transfers) > 3 else ''})"
            moved = f"{moved}; {t}" if moved else t[0].upper() + t[1:]
        if moved:
            out["planner"] = moved + "."

        # Customer Care Dispatcher: dealer notices and loaners
        notices = [c for c in calls if c["tool"] == "dealer.notify"]
        if notices:
            orders = sum(len(c["args"]["customer_order_ids"]) for c in notices)
            text = f"Notified {_n(len(notices), 'dealer')} about their {_n(orders, 'affected customer order')}."
            loaners = state.get("loaners") or {"loaned": [], "pool_empty": []}
            got, short = len(loaners["loaned"]), len(loaners["pool_empty"])
            if got + short:
                text += (f" Assigned loaners to {got} of {_n(got + short, 'eligible Platinum/Gold customer')}"
                         + (f"; {short} missed out because their dealer's pool was empty." if short else "."))
            else:
                text += " No affected customer is Platinum or Gold, so no loaners were due."
            out["dispatcher"] = text

        # Quality Inspector: lot hold, stop sales, owners
        lots = [c["args"]["lot_id"] for c in calls if c["tool"] == "quality.hold_lot"]
        if lots:
            stops = [c for c in calls if c["tool"] == "dealer.stop_sale"]
            vins = sum(len(c["args"]["vins"]) for c in stops)
            owners = sum(1 for c in calls if c["tool"] == "crm.notify_customer")
            out["inspector"] = (f"Put {', '.join(lots)} on hold, stopped sale of {_n(vins, 'unsold vehicle')} at "
                                f"{_n(len(stops), 'dealer')} and contacted {_n(owners, 'owner')} of sold vehicles.")

    out["summary"] = " ".join(v for v in (out["coordinator"], out["planner"], out["dispatcher"], out["inspector"]) if v)
    out["by"] = "rules (no LLM)"
    return out


# ======================================================================================
# Main
# ======================================================================================

GRAPH = None        # the knowledge graph (built once)
ENTERPRISE = None   # local copy of the enterprise rules, for dry runs


def setup():
    """Build the knowledge graph, the dry-run copy of the enterprise systems, and the
    LangGraph app. Call once; reuse the returned app for every event."""
    global GRAPH, ENTERPRISE
    if GRAPH is None:
        GRAPH = kg.build_graph()
        ENTERPRISE = Enterprise(DATA_DIR)
    return build_app()


def process_event(app, ev, config, trace_dir):
    """Run ONE event through the whole workflow and write its trace file.
    Returns the trace dict (the same thing that is saved to <trace_dir>/<event_id>.json)."""
    t0 = time.time()
    final = app.invoke({"event": ev, "config": config, "calls": [], "visited": [], "log": []})
    calls = _final_calls(final)
    results = final.get("results", [])
    trace = {
        "event": ev,
        "mode": config["mode"],
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
    # Normalise to plain JSON (dates -> "YYYY-MM-DD" strings) so callers get exactly what
    # is saved on disk — the checker and the UI read traces in that form.
    trace = json.loads(json.dumps(trace, default=str))
    os.makedirs(trace_dir, exist_ok=True)
    with open(os.path.join(trace_dir, f"{ev['event_id']}.json"), "w", encoding="utf-8") as f:
        json.dump(trace, f, indent=2)
    return trace


def main(argv):
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

    app = setup()
    os.makedirs(trace_dir, exist_ok=True)
    with open(os.path.join(HERE, out_dir, "agent_graph.mmd"), "w") as f:
        f.write(app.get_graph().draw_mermaid())

    config = {"mode": mode, "url": url, "refresh": "--refresh" in argv}
    print(f"Mode: {mode.upper()}  |  {len(events)} events  |  traces -> {trace_dir}")
    totals = Counter()
    for ev in events:
        trace = process_event(app, ev, config, trace_dir)
        calls = trace["calls"]
        failed = sum(1 for c in calls if not c["result"]["ok"])
        kinds = {c["tool"] for c in calls if c["tool"] not in READ_TOOLS}
        totals.update(calls=len(calls), failed=failed)

        print("=" * 88)
        print(f"{ev['event_id']}  path: {' → '.join(trace['agents_visited'])}")
        print(f"  {len(calls)} calls ({len(kinds)} kinds of action), {failed} failed"
              + ("  [FAIL-SAFE: escalated]" if REJECT in trace["agents_visited"] else ""))
        for agent in ("coordinator", "planner", "dispatcher", "inspector"):
            if trace["summary"].get(agent):
                print(f"  {agent:<11} {trace['summary'][agent]}")

    print("=" * 88)
    print(f"Done: {totals['calls']} calls, {totals['failed']} failed. Agent diagram: {out_dir}/agent_graph.mmd")
    if live:
        print(f"Submit: {out_dir}/action_log.jsonl (written by the server) + {out_dir}/traces/")


if __name__ == "__main__":
    main(sys.argv[1:])
