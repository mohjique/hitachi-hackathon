#!/usr/bin/env python3
"""
agents.py — Step 2: the three SPECIALIST AGENTS, plus a dry run that checks every
planned call against the real enterprise rules before anything touches the server.

Where this sits
---------------
  incident_triage.py  "what happened?"           -> runs/triage/triage.json
  graph.py            "what does it affect?"     (called directly from here)
  agents.py (this)    "what exactly do we do?"   -> runs/plan/plans.json
  run.py   (step 3)   LangGraph wiring + calling the real server

The agents
----------
Each agent is a plain, deterministic function. In step 3 these same functions become
LangGraph nodes, unchanged. None of them talks to the server; they only PLAN calls.

  Supply Chain Planner     affected production orders -> mes.reschedule / mes.transfer
  Dealer & Customer Care   affected customer orders   -> dealer.notify, crm.loaner_availability,
                                                         crm.assign_loaner
  Quality                  affected vehicles          -> quality.hold_lot, dealer.stop_sale,
                                                         crm.notify_customer
  (Coordinator)            plan_event() below: routes incidents to agents, merges their
                           plans, adds `escalate` for escalated incidents, runs the dry run.

Every planned call is a dict:
  {"tool", "args", "agent", "incidents": [1, 2], "why": "...", "path": [...graph nodes...]}

Team decisions baked in (see the task list)
-------------------------------------------
  * Reschedule order: original build date, then production order id.
  * Same plant first: the earliest working day on/after the restart with a free slot.
    Otherwise transfer: the earliest day with a free slot (from today), then the lowest
    plant id, at another plant that builds the model. (Handbook rule 3.)
  * Compound events share ONE slot schedule and ONE set of loaner pools.
  * A day that is disrupted in this event is never used as a destination
    (an outage day at that plant, or a delayed-part day for models needing that part).
  * One dealer.notify per dealer per event, listing every affected order with its cause.
  * Loaners ranked across all incidents together: Platinum, Gold, earlier promised date,
    lower customer order id. One loaner per customer order, from the dealer's own pool.

How to run
----------
  python agents.py            # plan + dry-run every event in runs/triage/triage.json
  python agents.py E-01 E-09  # just these
"""

import csv
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date

import graph as kg
from enterprise import Enterprise, ToolError

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
TRIAGE_FILE = os.path.join(HERE, "runs", "triage", "triage.json")
OUT_DIR = os.path.join(HERE, "runs", "plan")

TODAY = date(2027, 3, 8).isoformat()
TIER_RANK = {"Platinum": 0, "Gold": 1}          # handbook rule 5 (Standard gets no loaner)

# Working days in the planning window (today .. 2027-04-02), in order, as ISO strings.
WORKDAYS = [r["date"] for r in csv.DictReader(open(os.path.join(DATA_DIR, "calendar.csv")))
            if int(r["day_index"]) >= 0]


def _call(tool, args, agent, incidents, why, path=None):
    """One planned tool call, with everything the trace and the UI need."""
    return {"tool": tool, "args": args, "agent": agent, "incidents": sorted(set(incidents)),
            "why": why, "path": path or []}


def _nice(d):
    """2027-03-19 -> 'Mar 19' for human-readable messages."""
    return date.fromisoformat(d).strftime("%b %-d")


# ======================================================================================
# Shared state for ONE event (all incidents in a compound event use the same one)
# ======================================================================================

class SharedState:
    """The planner's private copy of the world it can change: slots and loaner pools.

    It mirrors what enterprise.py tracks, so plans stay consistent across incidents
    (two incidents never take the same slot or the same loaner)."""

    def __init__(self, g):
        self.g = g
        self.slots = Counter()     # (plant_id, date) -> production orders scheduled
        self.loc = {}              # production_order_id -> (plant_id, date), future orders only
        for n, a in g.nodes(data=True):
            if a["type"] == "PO" and a["build_date"] >= TODAY:
                self.slots[(a["plant_id"], a["build_date"])] += 1
                self.loc[a["id"]] = (a["plant_id"], a["build_date"])
        self.capacity = {a["id"]: a["slots_per_day"] for _, a in g.nodes(data=True) if a["type"] == "Plant"}
        self.loaners = {a["id"]: a["loaners_available"] for _, a in g.nodes(data=True) if a["type"] == "Dealer"}
        self.blocks = []           # disruptions in this event, see blocked()

    # ---- disruptions --------------------------------------------------------------
    def add_outage(self, plant, first, last):
        self.blocks.append({"plant": plant, "from": first, "to": last, "part": None})

    def add_delay(self, plant, part, first, until_excl):
        # a delay blocks [first, until_excl): store the last blocked day as "to"
        days = [d for d in WORKDAYS if first <= d < until_excl]
        if days:
            self.blocks.append({"plant": plant, "from": days[0], "to": days[-1], "part": part})

    def blocked(self, plant, day, model):
        """Is this plant/day unusable for this model because of a disruption in this event?"""
        for b in self.blocks:
            if b["plant"] == plant and b["from"] <= day <= b["to"]:
                if b["part"] is None:                         # outage: nothing builds
                    return True
                if b["part"] in self.parts_of(model):         # delay: only models needing the part
                    return True
        return False

    def parts_of(self, model):
        return {self.g.nodes[p]["id"] for p in kg.out(self.g, kg.nid("Model", model), "needs")}

    def plants_building(self, model):
        return sorted(self.g.nodes[p]["id"] for p in kg.into(self.g, kg.nid("Model", model), "can_build"))

    # ---- slots ----------------------------------------------------------------------
    def free(self, plant, day):
        return self.capacity[plant] - self.slots[(plant, day)]

    def move(self, po, plant, day):
        self.slots[self.loc[po]] -= 1
        self.slots[(plant, day)] += 1
        self.loc[po] = (plant, day)


# ======================================================================================
# Agent 1 — Supply Chain Planner
# ======================================================================================

def supply_chain_planner(state, findings):
    """findings: list of (incident_index, graph result) for outages and delays.

    For every affected production order, find the earliest feasible slot (rule 3) and
    plan mes.reschedule (same plant) or mes.transfer (another plant).
    Returns (calls, moves, unplaced) where moves = {po: {...where it went and why...}}."""
    # 1) Register every disruption first, so no order is moved INTO a disrupted day.
    for i, res in findings:
        d = res["disruption"]
        if res["kind"] == "plant_outage":
            state.add_outage(res["plant_id"], d["from"], d["through"])
        else:
            state.add_delay(res["plant_id"], res["part_id"], d["from"], d["until_excl"])

    # 2) Collect affected orders. If one order is hit by several incidents, it must wait
    #    for the LATEST restart among them.
    orders = {}
    for i, res in findings:
        if res["kind"] == "plant_outage":
            later = [d for d in WORKDAYS if d > res["disruption"]["through"]]
            earliest = later[0] if later else None          # None: plant never restarts in window
            cause = f"{res['plant_id']} outage {_nice(res['disruption']['from'])}–{_nice(res['disruption']['through'])}"
        else:
            earliest = res["disruption"]["earliest_new_date"]  # the new ETA itself is allowed
            cause = f"{res['part_id']} shipment late, now arriving {_nice(earliest)}"
        for po in res["production_orders"]:
            pid = po["production_order_id"]
            o = orders.setdefault(pid, {**po, "earliest": earliest, "incidents": [], "causes": []})
            if earliest is None or (o["earliest"] is not None and earliest > o["earliest"]):
                o["earliest"] = earliest
            o["incidents"].append(i)
            o["causes"].append(cause)

    # 3) Place them in a fixed order: original date, then id.
    calls, moves, unplaced = [], {}, []
    for o in sorted(orders.values(), key=lambda o: (o["build_date"], o["production_order_id"])):
        pid, home, model = o["production_order_id"], o["plant_id"], o["model"]
        path = [kg.nid("Plant", home), kg.nid("PO", pid)]

        # a) same plant, earliest working day on/after the restart with a free slot
        target = None
        if o["earliest"] is not None:
            for day in WORKDAYS:
                if day >= o["earliest"] and state.free(home, day) > 0 and not state.blocked(home, day, model):
                    target = (home, day)
                    break
        if target:
            state.move(pid, *target)
            why = (f"{'; '.join(o['causes'])}. Earliest {home} day on/after {o['earliest']} "
                   f"with a free slot is {target[1]}.")
            calls.append(_call("mes.reschedule", {"production_order_id": pid, "new_date": target[1]},
                               "supply_chain_planner", o["incidents"], why, path))
            moves[pid] = {"from": (home, o["build_date"]), "to": target, "how": "reschedule", "causes": o["causes"]}
            continue

        # b) transfer: earliest day (from today) with a free slot, then lowest plant id
        for day in WORKDAYS:
            for plant in state.plants_building(model):
                if plant != home and state.free(plant, day) > 0 and not state.blocked(plant, day, model):
                    target = (plant, day)
                    break
            if target:
                break
        if target:
            state.move(pid, *target)
            why = (f"{'; '.join(o['causes'])}. No free {home} slot on/after {o['earliest']} in the "
                   f"planning window; earliest free slot at another {model} plant is {target[0]} on {target[1]}.")
            calls.append(_call("mes.transfer", {"production_order_id": pid, "to_plant_id": target[0],
                                                "new_date": target[1]},
                               "supply_chain_planner", o["incidents"], why,
                               path + [kg.nid("Model", model), kg.nid("Plant", target[0])]))
            moves[pid] = {"from": (home, o["build_date"]), "to": target, "how": "transfer", "causes": o["causes"]}
        else:
            unplaced.append(pid)   # nowhere to put it: the coordinator escalates
    return calls, moves, unplaced


# ======================================================================================
# Agent 2 — Dealer & Customer Care
# ======================================================================================

def dealer_customer_care(state, findings, moves):
    """Rule 4: one dealer.notify per dealer, listing exactly its affected customer orders.
    Rule 5: loaners for Platinum/Gold customers, ranked across ALL incidents together."""
    # Gather affected customer orders once (a CO is listed once even if two incidents hit it).
    cos = {}
    for i, res in findings:
        for c in res["customer_orders"]:
            rec = cos.setdefault(c["customer_order_id"], {**c, "incidents": []})
            rec["incidents"].append(i)

    calls = []
    # ---- dealer notices -----------------------------------------------------------------
    by_dealer = defaultdict(list)
    for c in cos.values():
        by_dealer[c["dealer_id"]].append(c)
    for dealer, items in sorted(by_dealer.items()):
        items.sort(key=lambda c: c["customer_order_id"])
        lines = []
        for c in items:
            m = moves.get(c["production_order_id"])
            if m:
                where = (f"now building {_nice(m['to'][1])}" if m["how"] == "reschedule"
                         else f"transferred to {m['to'][0]}, building {_nice(m['to'][1])}")
                lines.append(f"{c['customer_order_id']} ({'; '.join(m['causes'])}; {where})")
            else:
                lines.append(f"{c['customer_order_id']} (delayed; new date to follow)")
        msg = (f"Delay notice: {len(items)} of your customer order(s) are affected. " + " ".join(lines)
               + " Platinum and Gold customers are being offered a loaner where available.")
        calls.append(_call("dealer.notify",
                           {"dealer_id": dealer, "customer_order_ids": [c["customer_order_id"] for c in items],
                            "message": msg},
                           "dealer_customer_care", [i for c in items for i in c["incidents"]],
                           f"{len(items)} affected customer order(s) at {dealer}",
                           items[0]["path"]))

    # ---- loaners --------------------------------------------------------------------------
    eligible = [c for c in cos.values() if c["tier"] in TIER_RANK]
    eligible.sort(key=lambda c: (TIER_RANK[c["tier"]], c["promised_date"], c["customer_order_id"]))
    loaned, no_loaner, checked = [], [], set()
    for c in eligible:
        dealer = c["dealer_id"]
        if dealer not in checked:   # free read call; shows the pool size in the trace
            checked.add(dealer)
            calls.append(_call("crm.loaner_availability", {"dealer_id": dealer}, "dealer_customer_care",
                               c["incidents"], f"check pool before assigning (planner expects {state.loaners[dealer]})",
                               [kg.nid("Dealer", dealer)]))
        if state.loaners[dealer] > 0:
            state.loaners[dealer] -= 1
            loaned.append(c["customer_order_id"])
            calls.append(_call("crm.assign_loaner", {"customer_order_id": c["customer_order_id"]},
                               "dealer_customer_care", c["incidents"],
                               f"{c['tier']} customer {c['customer_id']}, promised {c['promised_date']}; "
                               f"{dealer} pool now {state.loaners[dealer]}",
                               c["path"]))
        else:
            no_loaner.append(c["customer_order_id"])
    return calls, {"loaned": loaned, "pool_empty": no_loaner}


# ======================================================================================
# Agent 3 — Quality
# ======================================================================================

def quality(findings, g):
    """Rule 6: hold the lot; stop sale of unsold affected vehicles at their dealer;
    contact the owner of each sold affected vehicle (one call per owner)."""
    calls = []
    for i, res in findings:
        part_name = g.nodes[kg.nid("Part", res["part_id"])]["name"]
        lot = res["lot_id"]
        calls.append(_call("quality.hold_lot", {"lot_id": lot}, "quality", [i],
                           f"{lot}: defective {part_name} used at {res['plant_id']} "
                           f"{res['window']['first']}..{res['window']['last']}",
                           [kg.nid("Lot", lot)]))
        by_vin = {v["vin"]: v for v in res["vehicles"]}
        for dealer, vins in sorted(res["stop_sale"].items()):
            calls.append(_call("dealer.stop_sale", {"dealer_id": dealer, "vins": vins}, "quality", [i],
                               f"{len(vins)} unsold vehicle(s) built with {lot}",
                               by_vin[vins[0]]["path"]))
        owners = defaultdict(list)
        for v in res["vehicles"]:
            if v["status"] == "sold":
                owners[v["customer_id"]].append(v)
        for cust, vs in sorted(owners.items()):
            vins = ", ".join(f"{v['vin']} ({v['model'].title()})" for v in vs)
            dealer_name = g.nodes[kg.nid("Dealer", vs[0]["dealer_id"])]["name"]
            msg = (f"Safety notice: your vehicle {vins} may contain a defective {part_name} "
                   f"(lot {lot}). Please contact {dealer_name} to book a free inspection.")
            calls.append(_call("crm.notify_customer", {"customer_id": cust, "message": msg}, "quality", [i],
                               f"owner of {len(vs)} affected sold vehicle(s)", vs[0]["path"]))
    return calls


# ======================================================================================
# Coordinator (planning part) + dry run
# ======================================================================================

def plan_event(g, event):
    """Route each incident of one triaged event to the right agent and merge the plans.

    Returns a dict with the ordered calls and everything the trace needs."""
    state = SharedState(g)
    calls, notes, escalations = [], [], []
    disruptions, quality_findings, graph_results = [], [], {}

    for i, inc in enumerate(event["incidents"], 1):
        if inc["verdict"] == "NO_ACTION":
            notes.append(f"incident {i}: noise — no action ({inc.get('llm_reason') or 'not about operations'})")
        elif inc["verdict"] == "ESCALATE":
            escalations.append((i, inc.get("verdict_reason") or "unclear report"))
        else:
            try:
                res = kg.affected(g, inc)
            except (ValueError, KeyError) as e:
                escalations.append((i, f"graph lookup failed: {e}"))
                continue
            graph_results[i] = res
            (quality_findings if res["kind"] == "quality_hold" else disruptions).append((i, res))

    summary = {}
    if disruptions:
        sc_calls, moves, unplaced = supply_chain_planner(state, disruptions)
        dc_calls, loaner_info = dealer_customer_care(state, disruptions, moves)
        calls += sc_calls + dc_calls     # moves first, then notices, then loaners
        summary.update({"moves": {k: {**v, "from": list(v["from"]), "to": list(v["to"])} for k, v in moves.items()},
                        "unplaced": unplaced, **loaner_info})
        for po in unplaced:
            escalations.append((None, f"no feasible slot anywhere for {po} in the planning window"))
    if quality_findings:
        calls += quality(quality_findings, g)

    for i, reason in escalations:
        calls.append(_call("escalate", {"reason": f"{event['event_id']}: {reason}"}, "coordinator",
                           [i] if i else [], reason))
    return {"event_id": event["event_id"], "calls": calls, "notes": notes, "summary": summary,
            "graph": {i: {k: r[k] for k in ("kind", "path_schema", "notes", "example_path")}
                      for i, r in graph_results.items()}}


def dry_run(ent, plan):
    """Run every planned call against a private copy of the enterprise systems
    (enterprise.py, the same code the graded server uses). Nothing is sent anywhere.
    Returns the list of failures; an empty list means the plan is safe to execute."""
    key = "DRY-" + plan["event_id"]
    ent._state.pop(key, None)          # start from a fresh world every time
    failures = []
    for n, c in enumerate(plan["calls"]):
        try:
            result = ent.call(key, c["tool"], c["args"])
            if c["tool"] == "crm.loaner_availability":
                c["dry_run_result"] = result
        except ToolError as e:
            failures.append({"index": n, "tool": c["tool"], "args": c["args"], "error": str(e)})
    plan["dry_run_state"] = _state_summary(ent, ent._state[key]) if key in ent._state else {}
    return failures


def _state_summary(ent, s):
    """The final world after the dry run — what the grader would compare."""
    moved = [po for po, (p, i) in s["loc"].items()
             if (p, i) != (ent.orders[po]["plant_id"], ent.orders[po]["idx"])]
    return {"moved_orders": sorted(moved),
            "dealers_notified": len(s["notified"]),
            "orders_notified": sum(len(v) for v in s["notified"].values()),
            "loaners_assigned": len(s["loaned"]),
            "lots_held": sorted(s["held"]),
            "vehicles_stopped": len(s["stopped"]),
            "customers_contacted": len(s["customers"]),
            "escalated": s["escalated"]}


# ======================================================================================
# Main
# ======================================================================================

def main(argv):
    wanted = {a.upper() for a in argv if not a.startswith("--")}
    with open(TRIAGE_FILE, encoding="utf-8") as f:
        triage = json.load(f)
    g = kg.build_graph()
    ent = Enterprise(DATA_DIR)

    plans, total_fail = [], 0
    for ev in triage:
        if wanted and ev["event_id"] not in wanted:
            continue
        plan = plan_event(g, ev)
        failures = dry_run(ent, plan)
        plan["dry_run_failures"] = failures
        total_fail += len(failures)
        plans.append(plan)

        counts = Counter(c["tool"] for c in plan["calls"])
        print("=" * 88)
        print(f"{ev['event_id']}  " + ", ".join(f"{i['verdict']} {i['category']}" for i in ev["incidents"]))
        for line in plan["notes"]:
            print(f"  {line}")
        if counts:
            print("  calls: " + ", ".join(f"{t} ×{n}" for t, n in sorted(counts.items())))
            kinds = {t for t in counts if t not in ("crm.loaner_availability", "mes.get_schedule")}
            print(f"  kinds of action: {len(kinds)}")
        else:
            print("  calls: none")
        s = plan["summary"]
        if s.get("moves"):
            for po, m in s["moves"].items():
                arrow = "→" if m["how"] == "reschedule" else "⇒ TRANSFER"
                print(f"    {po}: {m['from'][0]} {m['from'][1]} {arrow} {m['to'][0]} {m['to'][1]}")
            print(f"    loaners: {len(s['loaned'])} assigned, {len(s['pool_empty'])} eligible but pool empty"
                  + (f" ({', '.join(s['pool_empty'])})" if s["pool_empty"] else ""))
        if failures:
            print(f"  DRY-RUN FAILURES: {len(failures)}")
            for f_ in failures[:5]:
                print(f"    {f_['tool']} {f_['args']}: {f_['error']}")
        else:
            print("  dry run: all calls OK")

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "plans.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(plans, f, indent=2, default=str)
    print("=" * 88)
    print(f"{sum(len(p['calls']) for p in plans)} planned calls, {total_fail} dry-run failures. Saved: {out}")


if __name__ == "__main__":
    main(sys.argv[1:])
