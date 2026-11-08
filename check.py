#!/usr/bin/env python3
"""
check.py — Step 4a: an INDEPENDENT policy checker for a finished run.

It replays each event's calls on a fresh copy of the enterprise systems (enterprise.py),
then compares the final world with what the policy handbook requires — re-deriving the
expected answer straight from the CSVs. It deliberately does NOT import graph.py or
agents.py: if the planner has a bug, a checker built on the same code would share it.

What it checks, per event (handbook rule numbers in brackets)
------------------------------------------------------------
  * no failed calls                                                           [tools]
  * noise -> no calls at all                                                  [7]
  * unconfirmed -> escalate only, no write calls                              [8]
  * outages / delays:
      - exactly the affected production orders were moved, nothing else       [1, 2, 10]
      - each move is legal: same plant on/after the restart, or a plant that
        builds the model; never into a disrupted day                          [3]
      - each move is the EARLIEST feasible slot (no free, usable slot earlier) [3]
      - exactly one dealer.notify per affected dealer, listing exactly
        its affected customer orders                                         [4]
      - loaners went to exactly the right customers (tier, date, id priority) [5]
  * quality holds: the lot held; exactly the unsold affected vehicles stopped;
    exactly the owners of sold affected vehicles contacted                    [6]
  * at least three kinds of action per real incident                          [9]

It checks the run against the system's own reading of each message (the triage in the
trace). Whether the reading itself was right is a separate question — look at the triage.

How to run
----------
  python check.py                  # newest traces: runs/final, else runs/dry
  python check.py runs/dry         # a specific run folder
Writes <run>/checks.json (the UI shows it) and prints a report.
"""

import csv
import glob
import json
import os
import sys
from collections import Counter, defaultdict

from enterprise import Enterprise, ToolError, idx_to_date

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
TODAY = "2027-03-08"
READ_TOOLS = {"mes.get_schedule", "crm.loaner_availability"}
TIER = {"Platinum": 0, "Gold": 1}


def rd(name):
    with open(os.path.join(DATA, name), newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---- raw tables, nothing derived from the planner -----------------------------------
POS = {r["production_order_id"]: r for r in rd("production_orders.csv")}
COS = rd("customer_orders.csv")
TIERS = {r["customer_id"]: r["tier"] for r in rd("customers.csv")}
POOL = {r["dealer_id"]: int(r["loaners_available"]) for r in rd("loaners.csv")}
SHIP = {r["shipment_id"]: r for r in rd("inbound_shipments.csv")}
LOTS = {r["lot_id"]: r for r in rd("part_lots.csv")}
VEH = rd("finished_vehicles.csv")
BOM = defaultdict(set)
for r in rd("bom.csv"):
    BOM[r["model"]].add(r["part_id"])
BUILDS = defaultdict(set)
for r in rd("plant_models.csv"):
    BUILDS[r["plant_id"]].add(r["model"])
WORKDAYS = [r["date"] for r in rd("calendar.csv") if int(r["day_index"]) >= 0]
SLOTS = {r["plant_id"]: int(r["slots_per_day"]) for r in rd("plants.csv")}


def check_event(ent, trace):
    ev = trace["event"]["event_id"]
    calls = trace["calls"]
    problems, passed = [], []

    def ok(cond, rule, msg):
        (passed if cond else problems).append(f"[{rule}] {msg}")

    # ---- replay on a fresh world -----------------------------------------------------
    key = "CHECK-" + ev
    ent._state.pop(key, None)
    replay_fail = 0
    for c in calls:
        try:
            ent.call(key, c["tool"], c["args"])
        except ToolError:
            replay_fail += 1
    s = ent.state(key)
    failed = sum(1 for c in calls if not c.get("result", {}).get("ok", True)) + replay_fail
    ok(failed == 0, "tools", f"{failed} failed calls")

    incs = trace["triage"]["incidents"]
    acts = [i for i in incs if i["verdict"] == "ACT"]
    writes = [c for c in calls if c["tool"] not in READ_TOOLS and c["tool"] != "escalate"]
    tools_used = Counter(c["tool"] for c in calls)

    if not acts:
        if any(i["verdict"] == "ESCALATE" for i in incs):
            ok(not writes and tools_used["escalate"] >= 1, "8", "unconfirmed: escalate only, no write calls")
        else:
            ok(not calls, "7", "noise: no calls")
        return {"event_id": ev, "passed": passed, "problems": problems}

    # ---- expected outcome, from the raw tables ----------------------------------------
    affected_pos, blocks = {}, []   # affected_pos: production order -> earliest allowed same-plant day
    lots_expected, stop_expected, owners_expected = set(), set(), set()
    for inc in acts:
        if inc["category"] == "plant_outage":
            p, a, b = inc["plant_id"], inc["start"], inc["end"]
            blocks.append((p, a, b, None))
            restart = next((d for d in WORKDAYS if d > b), None)
            for po, r in POS.items():
                if r["plant_id"] == p and a <= r["build_date"] <= b and r["build_date"] >= TODAY:
                    affected_pos[po] = max(filter(None, [affected_pos.get(po), restart]), default=None)
        elif inc["category"] == "supplier_delay":
            sh = SHIP[inc["shipment_id"]]
            p, part, a, b = sh["plant_id"], sh["part_id"], inc["start"], inc["end"]
            days = [d for d in WORKDAYS if a <= d < b]
            if days:
                blocks.append((p, days[0], days[-1], part))
            for po, r in POS.items():
                if (r["plant_id"] == p and part in BOM[r["model"]] and a <= r["build_date"] < b
                        and r["build_date"] >= TODAY):
                    affected_pos[po] = max(filter(None, [affected_pos.get(po), b]), default=None)
        elif inc["category"] == "quality_hold":
            lot = LOTS[(inc.get("lots") or [inc.get("lot_id")])[0]]
            lots_expected.add(lot["lot_id"])
            for v in VEH:
                if (v["plant_id"] == lot["plant_id"] and lot["first_build_date"] <= v["build_date"] <= lot["last_build_date"]
                        and lot["part_id"] in BOM[v["model"]]):
                    if v["status"] == "in_stock":
                        stop_expected.add(v["vin"])
                    else:
                        owners_expected.add(v["customer_id"])

    def blocked(plant, day, model):
        return any(plant == p and a <= day <= b and (part is None or part in BOM[model]) for p, a, b, part in blocks)

    # ---- moves [1, 2, 3, 10] ----------------------------------------------------------
    moved = {po for po, loc in s["loc"].items()
             if loc != (ent.orders[po]["plant_id"], ent.orders[po]["idx"])}
    ok(moved == set(affected_pos), "1-3,10",
       f"moved exactly the {len(affected_pos)} affected orders"
       + ("" if moved == set(affected_pos) else
          f" (missing {sorted(set(affected_pos) - moved)[:5]}, extra {sorted(moved - set(affected_pos))[:5]})"))

    free = lambda p, d: SLOTS[p] - len(s["slots"][(p, WORKDAYS.index(d))])
    bad_moves = []
    for po in sorted(moved & set(affected_pos)):
        r = POS[po]
        plant, idx = s["loc"][po]
        day = idx_to_date(idx).isoformat()
        e = affected_pos[po]
        if blocked(plant, day, r["model"]):
            bad_moves.append(f"{po} placed in a disrupted day {plant} {day}")
        if plant == r["plant_id"]:
            if e is None or day < e:
                bad_moves.append(f"{po} rescheduled to {day}, before the restart {e}")
            earlier = [d for d in WORKDAYS if e and e <= d < day and free(plant, d) > 0
                       and not blocked(plant, d, r["model"])]
            if earlier:
                bad_moves.append(f"{po} at {day} but {plant} had a free slot on {earlier[0]}")
        else:
            if r["model"] not in BUILDS[plant]:
                bad_moves.append(f"{po} transferred to {plant}, which does not build {r['model']}")
            same = [d for d in WORKDAYS if e and d >= e and free(r["plant_id"], d) > 0
                    and not blocked(r["plant_id"], d, r["model"])]
            if same:
                bad_moves.append(f"{po} transferred although {r['plant_id']} had a free slot on {same[0]}")
            sooner = [(d, p) for d in WORKDAYS for p in sorted(BUILDS)
                      if p != r["plant_id"] and r["model"] in BUILDS[p] and (d, p) < (day, plant)
                      and free(p, d) > 0 and not blocked(p, d, r["model"])]
            if sooner:
                bad_moves.append(f"{po} transferred to {plant} {day}, but {sooner[0][1]} {sooner[0][0]} was free")
    if affected_pos:
        ok(not bad_moves, "3", "every move is legal and the earliest feasible slot"
           + ("" if not bad_moves else ": " + "; ".join(bad_moves[:4])))

    # ---- dealer notices [4] ----------------------------------------------------------
    aff_cos = [c for c in COS if c["production_order_id"] in affected_pos]
    if affected_pos:
        want = defaultdict(set)
        for c in aff_cos:
            want[c["dealer_id"]].add(c["customer_order_id"])
        n_calls = Counter(c["args"]["dealer_id"] for c in calls if c["tool"] == "dealer.notify")
        got = {d: set(v) for d, v in s["notified"].items()}
        ok(got == dict(want), "4", f"{len(want)} dealers told about exactly their affected orders")
        ok(all(n == 1 for n in n_calls.values()) and set(n_calls) == set(want), "4",
           "exactly one dealer.notify per affected dealer")

        # ---- loaners [5] -------------------------------------------------------------
        eligible = sorted((c for c in aff_cos if TIERS[c["customer_id"]] in TIER),
                          key=lambda c: (TIER[TIERS[c["customer_id"]]], c["promised_date"], c["customer_order_id"]))
        left, want_loan = dict(POOL), set()
        for c in eligible:
            if left[c["dealer_id"]] > 0:
                left[c["dealer_id"]] -= 1
                want_loan.add(c["customer_order_id"])
        ok(s["loaned"] == want_loan, "5", f"{len(want_loan)} loaners to the right customers by priority"
           + ("" if s["loaned"] == want_loan else
              f" (missing {sorted(want_loan - s['loaned'])[:4]}, extra {sorted(s['loaned'] - want_loan)[:4]})"))
    else:
        ok(not s["notified"] and not s["loaned"], "10", "no dealer notices or loaners without affected orders")

    # ---- quality [6] -------------------------------------------------------------------
    ok(s["held"] == lots_expected, "6", f"lots held: {sorted(s['held']) or 'none'}")
    ok(s["stopped"] == stop_expected, "6", f"{len(stop_expected)} unsold affected vehicles stopped from sale")
    ok(s["customers"] == owners_expected, "6", f"{len(owners_expected)} owners of sold affected vehicles contacted")

    # ---- completeness [9] ----------------------------------------------------------------
    kinds = {c["tool"] for c in calls if c["tool"] not in READ_TOOLS and c["tool"] != "escalate"}
    ok(len(kinds) >= 3, "9", f"{len(kinds)} kinds of action")
    return {"event_id": ev, "passed": passed, "problems": problems}


def main(argv):
    run = argv[0] if argv else ("runs/final" if os.path.isdir(os.path.join(HERE, "runs/final/traces")) else "runs/dry")
    files = sorted(glob.glob(os.path.join(HERE, run, "traces", "E-*.json")))
    if not files:
        sys.exit(f"No traces in {run}/traces — run run.py first.")
    ent = Enterprise(DATA)
    results = []
    for path in files:
        with open(path, encoding="utf-8") as f:
            results.append(check_event(ent, json.load(f)))

    bad = [r for r in results if r["problems"]]
    for r in results:
        mark = "PASS" if not r["problems"] else "FAIL"
        print(f"{r['event_id']}  {mark}  ({len(r['passed'])} checks passed)")
        for p in r["problems"]:
            print(f"    ✗ {p}")
    print(f"\n{len(results) - len(bad)}/{len(results)} events pass every check.")
    with open(os.path.join(HERE, run, "checks.json"), "w") as f:
        json.dump(results, f, indent=2)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
