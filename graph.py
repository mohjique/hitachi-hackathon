#!/usr/bin/env python3
"""
graph.py — Step 1: the company KNOWLEDGE GRAPH, and the traversals that find what an
incident touches.

Where this sits
---------------
  incident_triage.py   "what happened?"        -> runs/triage/triage.json  (ACT incidents)
  graph.py  (this)     "what does it affect?"  -> runs/graph/affected.json
  agents / planner     "what do we do about it?"  (next step)

This file never calls the enterprise server and never decides on actions. It only
answers questions about the data, and records the PATH it walked to get each answer —
that path is the "graph path" evidence the judges ask for.

The graph
---------
One networkx MultiDiGraph built from the CSVs in data/.

Nodes are typed records. The node id is "<Type>:<id>" so ids from different tables can
never collide (e.g. "Plant:P_TX", "PO:PO-0058", "CO:CO-00249", "Vehicle:AUR0000001").
Every node carries `type`, `id`, and the columns of its CSV row as attributes.

Edges are named relationships (the edge `rel` attribute, also used as the edge key):

    Supplier ─supplies→ Shipment ─carries→ Part        Shipment ─delivered_to→ Plant
    Supplier ─made→ Lot ─batch_of→ Part                Lot ─used_at→ Plant
    Model ─needs→ Part            (bill of materials)  Plant ─can_build→ Model
    Plant ─runs→ PO ─builds→ Model                     PO ─fulfils→ CO
    CO ─placed_at→ Dealer         CO ─ordered_by→ Customer
    PO ─produced→ Vehicle ─at_dealer→ Dealer           Vehicle ─owned_by→ Customer (if sold)

Traversals (one per incident type), each returning the affected records AND their paths:

    affected_by_delay    Shipment → Part ← Model (BOM) ← PO (plant, date range) → CO → Dealer / Customer
    affected_by_outage   Plant → PO (date range) → CO → Dealer / Customer
    affected_by_quality  Lot → Part ← Model (BOM) ← PO (plant, lot window) → Vehicle → Dealer / Customer

How to run
----------
  python graph.py              # every ACT incident in runs/triage/triage.json
  python graph.py E-01 E-14    # just these events
  python graph.py --ontology   # also write the ontology diagram (node + edge types)

Only the standard library plus networkx (pip install networkx).
"""

import csv
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date

import networkx as nx

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
TRIAGE_FILE = os.path.join(HERE, "runs", "triage", "triage.json")
OUT_DIR = os.path.join(HERE, "runs", "graph")

TODAY = date(2027, 3, 8)               # the challenge's "today" (working-day index 0)
LOANER_TIERS = ("Platinum", "Gold")    # handbook rule 5: only these tiers get loaners


# ======================================================================================
# Building the graph
# ======================================================================================

def _read(name):
    with open(os.path.join(DATA_DIR, name), newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def nid(kind, key):
    """Node id helper: nid("Plant", "P_TX") -> "Plant:P_TX"."""
    return f"{kind}:{key}"


def build_graph():
    """Load every CSV and return the knowledge graph (about 17,000 nodes)."""
    g = nx.MultiDiGraph()

    def node(kind, key, **attrs):
        g.add_node(nid(kind, key), type=kind, id=key, **attrs)

    def edge(src, rel, dst):
        # key=rel means the same relationship can't be added twice between two nodes
        g.add_edge(src, dst, key=rel, rel=rel)

    # ---- the "things" -----------------------------------------------------------------
    for r in _read("plants.csv"):
        node("Plant", r["plant_id"], name=r["name"], city=r["city"], slots_per_day=int(r["slots_per_day"]))
    for r in _read("models.csv"):
        node("Model", r["model"], segment=r["segment"])
    for r in _read("parts.csv"):
        node("Part", r["part_id"], name=r["name"], category=r["category"])
    for r in _read("suppliers.csv"):
        node("Supplier", r["supplier_id"], name=r["name"], city=r["city"])
    loaners = {r["dealer_id"]: int(r["loaners_available"]) for r in _read("loaners.csv")}
    for r in _read("dealers.csv"):
        node("Dealer", r["dealer_id"], name=r["name"], city=r["city"], region=r["region"],
             transit_days=int(r["transit_days"]), loaners_available=loaners.get(r["dealer_id"], 0))
    for r in _read("customers.csv"):
        node("Customer", r["customer_id"], name=r["name"], tier=r["tier"])

    # ---- static relationships ---------------------------------------------------------
    for r in _read("bom.csv"):                      # which parts each model needs
        edge(nid("Model", r["model"]), "needs", nid("Part", r["part_id"]))
    for r in _read("plant_models.csv"):             # which plants can build which models
        edge(nid("Plant", r["plant_id"]), "can_build", nid("Model", r["model"]))

    # ---- supply side ------------------------------------------------------------------
    for r in _read("inbound_shipments.csv"):
        s = nid("Shipment", r["shipment_id"])
        node("Shipment", r["shipment_id"], eta_date=r["eta_date"], covers_days=int(r["covers_days"]))
        edge(nid("Supplier", r["supplier_id"]), "supplies", s)
        edge(s, "carries", nid("Part", r["part_id"]))
        edge(s, "delivered_to", nid("Plant", r["plant_id"]))
    for r in _read("part_lots.csv"):
        l = nid("Lot", r["lot_id"])
        node("Lot", r["lot_id"], first_build_date=r["first_build_date"], last_build_date=r["last_build_date"])
        edge(nid("Supplier", r["supplier_id"]), "made", l)
        edge(l, "batch_of", nid("Part", r["part_id"]))
        edge(l, "used_at", nid("Plant", r["plant_id"]))

    # ---- production and demand --------------------------------------------------------
    for r in _read("production_orders.csv"):
        p = nid("PO", r["production_order_id"])
        node("PO", r["production_order_id"], build_date=r["build_date"], model=r["model"],
             plant_id=r["plant_id"], units=int(r["units"]))
        edge(nid("Plant", r["plant_id"]), "runs", p)
        edge(p, "builds", nid("Model", r["model"]))
    for r in _read("customer_orders.csv"):
        c = nid("CO", r["customer_order_id"])
        node("CO", r["customer_order_id"], promised_date=r["promised_date"], model=r["model"],
             dealer_id=r["dealer_id"], customer_id=r["customer_id"], production_order_id=r["production_order_id"])
        edge(nid("PO", r["production_order_id"]), "fulfils", c)
        edge(c, "placed_at", nid("Dealer", r["dealer_id"]))
        edge(c, "ordered_by", nid("Customer", r["customer_id"]))
    for r in _read("finished_vehicles.csv"):
        v = nid("Vehicle", r["vin"])
        node("Vehicle", r["vin"], build_date=r["build_date"], model=r["model"], plant_id=r["plant_id"],
             status=r["status"], dealer_id=r["dealer_id"], customer_id=r["customer_id"] or None)
        edge(nid("PO", r["production_order_id"]), "produced", v)
        edge(v, "at_dealer", nid("Dealer", r["dealer_id"]))
        if r["customer_id"]:
            edge(v, "owned_by", nid("Customer", r["customer_id"]))

    return g


# ======================================================================================
# Small traversal helpers
# ======================================================================================

def out(g, n, rel):
    """Nodes reached from n along edges named `rel`  (n ─rel→ ?)."""
    return [v for _, v, k in g.out_edges(n, keys=True) if k == rel]


def into(g, n, rel):
    """Nodes that point at n along edges named `rel`  (? ─rel→ n)."""
    return [u for u, _, k in g.in_edges(n, keys=True) if k == rel]


def one(nodes, what):
    """Exactly one node expected (e.g. a shipment carries exactly one part)."""
    if len(nodes) != 1:
        raise ValueError(f"expected exactly one {what}, found {len(nodes)}")
    return nodes[0]


def _as_iso(d):
    """Accept a date or an ISO string (triage.json stores dates as strings)."""
    return d.isoformat() if isinstance(d, date) else d


class Trace:
    """Collects what a traversal walked: the edges (for drawing the subgraph) and
    readable lines (for the reasoning trace)."""

    def __init__(self):
        self.edges = []       # (source, rel, target) — every edge actually used
        self.notes = []       # human-readable steps

    def walk(self, u, rel, v):
        self.edges.append((u, rel, v))

    def note(self, text):
        self.notes.append(text)


# ======================================================================================
# The shared tail: production orders -> customer orders -> dealers / customers
# (used by both supplier delays and plant outages)
# ======================================================================================

def _orders_downstream(g, pos, head_path, t):
    """For affected production orders, find their customer orders, dealers and customers.

    head_path: the path from the incident's start node to each PO, e.g.
               {"PO:PO-0058": ["Plant:P_TX", "PO:PO-0058"]}
    Returns a list of customer-order records, each with the full path to it.
    """
    records = []
    for po in pos:
        for co in sorted(out(g, po, "fulfils")):
            t.walk(po, "fulfils", co)
            dealer = one(out(g, co, "placed_at"), "dealer")
            customer = one(out(g, co, "ordered_by"), "customer")
            t.walk(co, "placed_at", dealer)
            t.walk(co, "ordered_by", customer)
            a = g.nodes[co]
            tier = g.nodes[customer]["tier"]
            records.append({
                "customer_order_id": a["id"],
                "production_order_id": g.nodes[po]["id"],
                "dealer_id": g.nodes[dealer]["id"],
                "customer_id": g.nodes[customer]["id"],
                "tier": tier,
                "loaner_eligible": tier in LOANER_TIERS,
                "promised_date": a["promised_date"],
                "path": head_path[po] + [co, dealer, customer],
            })
    return records


def _summarise_orders(pos, records, g):
    """Group the customer orders the way the agents will need them."""
    by_dealer = defaultdict(list)
    for r in records:
        by_dealer[r["dealer_id"]].append(r["customer_order_id"])
    return {
        "production_orders": [
            {"production_order_id": g.nodes[p]["id"], "plant_id": g.nodes[p]["plant_id"],
             "model": g.nodes[p]["model"], "build_date": g.nodes[p]["build_date"]}
            for p in pos],
        "customer_orders": records,
        "dealers": {d: sorted(cos) for d, cos in sorted(by_dealer.items())},
        "loaner_candidates": sorted(r["customer_order_id"] for r in records if r["loaner_eligible"]),
    }


def _sort_pos(g, pos):
    """Production orders in a stable order: build date, then id."""
    return sorted(set(pos), key=lambda p: (g.nodes[p]["build_date"], g.nodes[p]["id"]))


# ======================================================================================
# 1) Supplier delay
#    Shipment → Part ← Model (BOM) ← PO (at the receiving plant, original ETA ≤ date < new ETA)
#    → CO → Dealer / Customer
# ======================================================================================

def affected_by_delay(g, inc):
    """Handbook rule 1: builds that need the delayed part cannot run from the original ETA
    up to (but not including) the new ETA — only at the receiving plant."""
    t = Trace()
    sh = nid("Shipment", inc["shipment_id"])
    part = one(out(g, sh, "carries"), "part")
    plant = one(out(g, sh, "delivered_to"), "plant")
    t.walk(sh, "carries", part)
    t.walk(sh, "delivered_to", plant)
    start, end = _as_iso(inc["start"]), _as_iso(inc["end"])
    t.note(f"{sh} carries {part} to {plant}; blocked from {start} up to (not incl.) {end}")

    # Which models need this part? (walk the BOM edges backwards)
    models = sorted(into(g, part, "needs"))
    for m in models:
        t.walk(m, "needs", part)
    t.note(f"models needing {part}: {', '.join(g.nodes[m]['id'] for m in models)}")

    # The plant's production orders of those models inside the blocked range
    pos, head_path = [], {}
    for po in out(g, plant, "runs"):
        a = g.nodes[po]
        model = nid("Model", a["model"])
        if model in models and start <= a["build_date"] < end:
            pos.append(po)
            t.walk(plant, "runs", po)
            t.walk(po, "builds", model)
            head_path[po] = [sh, part, model, po]
    pos = _sort_pos(g, pos)
    skipped = _drop_already_built(g, pos, t)
    pos = [p for p in pos if p not in skipped]
    t.note(f"{len(pos)} production orders affected at {plant}")

    records = _orders_downstream(g, pos, head_path, t)
    result = _summarise_orders(pos, records, g)
    result.update({
        "kind": "supplier_delay",
        "path_schema": "Shipment ─carries→ Part ←needs─ Model ←builds─ PO ─fulfils→ CO ─placed_at→ Dealer / ─ordered_by→ Customer",
        "plant_id": g.nodes[plant]["id"], "part_id": g.nodes[part]["id"],
        "disruption": {"from": start, "until_excl": end, "earliest_new_date": end},
    })
    return _finish(result, t, records)


# ======================================================================================
# 2) Plant outage
#    Plant → PO (start ≤ date ≤ end) → CO → Dealer / Customer
# ======================================================================================

def affected_by_outage(g, inc):
    """Handbook rule 2: every production order at the plant during the outage (inclusive)
    is affected; the plant restarts the next working day."""
    t = Trace()
    plant = nid("Plant", inc["plant_id"])
    start, end = _as_iso(inc["start"]), _as_iso(inc["end"])
    t.note(f"{plant} down {start} through {end} (inclusive)")

    pos, head_path = [], {}
    for po in out(g, plant, "runs"):
        if start <= g.nodes[po]["build_date"] <= end:
            pos.append(po)
            t.walk(plant, "runs", po)
            head_path[po] = [plant, po]
    pos = _sort_pos(g, pos)
    skipped = _drop_already_built(g, pos, t)
    pos = [p for p in pos if p not in skipped]
    t.note(f"{len(pos)} production orders affected at {plant}")

    records = _orders_downstream(g, pos, head_path, t)
    result = _summarise_orders(pos, records, g)
    result.update({
        "kind": "plant_outage",
        "path_schema": "Plant ─runs→ PO ─fulfils→ CO ─placed_at→ Dealer / ─ordered_by→ Customer",
        "plant_id": g.nodes[plant]["id"],
        # The planner needs "the next working day after the outage"; it uses the
        # calendar for that, so we only pass the outage itself here.
        "disruption": {"from": start, "through": end},
    })
    return _finish(result, t, records)


def _drop_already_built(g, pos, t):
    """Orders before today are already built and can't be moved by the tools."""
    past = {p for p in pos if g.nodes[p]["build_date"] < TODAY.isoformat()}
    if past:
        t.note(f"ignored {len(past)} already-built orders: {', '.join(sorted(g.nodes[p]['id'] for p in past))}")
    return past


# ======================================================================================
# 3) Quality hold
#    Lot → Part ← Model (BOM) ← PO (at the lot's plant, in the lot's build window)
#    → Vehicle → Dealer (unsold) / Customer (sold)
# ======================================================================================

def affected_by_quality(g, inc):
    """Handbook rule 6: affected vehicles are the finished vehicles built at the lot's
    plant, on the lot's build dates, from production orders of models that use the part.
    Unsold (in_stock) -> stop sale at their dealer. Sold -> contact the owner."""
    t = Trace()
    lot_id = inc.get("lot_id") or one(inc.get("lots") or [], "lot")
    lot = nid("Lot", lot_id)
    part = one(out(g, lot, "batch_of"), "part")
    plant = one(out(g, lot, "used_at"), "plant")
    t.walk(lot, "batch_of", part)
    t.walk(lot, "used_at", plant)
    first, last = g.nodes[lot]["first_build_date"], g.nodes[lot]["last_build_date"]
    t.note(f"{lot} is {part} used at {plant} for builds {first} through {last}")

    models = sorted(into(g, part, "needs"))
    for m in models:
        t.walk(m, "needs", part)
    t.note(f"models using {part}: {', '.join(g.nodes[m]['id'] for m in models)}")

    pos, vehicles = [], []
    for po in out(g, plant, "runs"):
        a = g.nodes[po]
        model = nid("Model", a["model"])
        if model not in models or not (first <= a["build_date"] <= last):
            continue
        pos.append(po)
        t.walk(plant, "runs", po)
        t.walk(po, "builds", model)
        for v in sorted(out(g, po, "produced")):
            va = g.nodes[v]
            # The rule is about the vehicle's own plant and build date, so check them too.
            if va["plant_id"] != g.nodes[plant]["id"] or not (first <= va["build_date"] <= last):
                continue
            t.walk(po, "produced", v)
            dealer = one(out(g, v, "at_dealer"), "dealer")
            t.walk(v, "at_dealer", dealer)
            rec = {"vin": va["id"], "production_order_id": a["id"], "model": va["model"],
                   "build_date": va["build_date"], "status": va["status"],
                   "dealer_id": g.nodes[dealer]["id"], "customer_id": va["customer_id"],
                   "path": [lot, part, model, po, v, dealer]}
            if va["status"] == "sold":
                owner = one(out(g, v, "owned_by"), "owner")
                t.walk(v, "owned_by", owner)
                rec["path"].append(owner)
            vehicles.append(rec)
    pos = _sort_pos(g, pos)

    unsold = [v for v in vehicles if v["status"] == "in_stock"]
    sold = [v for v in vehicles if v["status"] == "sold"]
    stop_sale = defaultdict(list)
    for v in unsold:
        stop_sale[v["dealer_id"]].append(v["vin"])
    owners = sorted({v["customer_id"] for v in sold})
    t.note(f"{len(pos)} production orders, {len(vehicles)} vehicles: "
           f"{len(unsold)} unsold at {len(stop_sale)} dealers, {len(sold)} sold to {len(owners)} owners")

    result = {
        "kind": "quality_hold",
        "path_schema": "Lot ─batch_of→ Part ←needs─ Model ←builds─ PO ─produced→ Vehicle ─at_dealer→ Dealer / ─owned_by→ Customer",
        "lot_id": lot_id, "plant_id": g.nodes[plant]["id"], "part_id": g.nodes[part]["id"],
        "window": {"first": first, "last": last},
        "production_orders": [{"production_order_id": g.nodes[p]["id"], "model": g.nodes[p]["model"],
                               "build_date": g.nodes[p]["build_date"]} for p in pos],
        "vehicles": vehicles,
        "stop_sale": {d: sorted(v) for d, v in sorted(stop_sale.items())},
        "owners_to_notify": owners,
    }
    return _finish(result, t, vehicles)


# ======================================================================================
# Shared finishing + dispatcher
# ======================================================================================

def _finish(result, t, leaf_records):
    """Attach the trace: readable notes, every edge walked, and one example full path."""
    result["notes"] = t.notes
    result["edges"] = sorted(set(t.edges))
    result["example_path"] = leaf_records[0]["path"] if leaf_records else None
    return result


TRAVERSALS = {
    "supplier_delay": affected_by_delay,
    "plant_outage": affected_by_outage,
    "quality_hold": affected_by_quality,
}


def affected(g, incident):
    """Run the right traversal for one ACT incident from triage.json."""
    fn = TRAVERSALS.get(incident["category"])
    if fn is None:
        raise ValueError(f"no traversal for category {incident['category']!r}")
    return fn(g, incident)


# ======================================================================================
# Ontology export (a submission item): node types, edge types, counts
# ======================================================================================

def ontology(g):
    node_counts = Counter(a["type"] for _, a in g.nodes(data=True))
    edge_counts = Counter((g.nodes[u]["type"], k, g.nodes[v]["type"]) for u, v, k in g.edges(keys=True))
    return node_counts, edge_counts


def write_ontology(g, out_dir=OUT_DIR):
    """Writes <out_dir>/ontology.json and ontology.mmd (a Mermaid diagram — paste it into
    any Mermaid viewer, GitHub, or a Markdown preview to see the picture).
    run.py and app.py also call this, so every run folder contains the ontology diagram."""
    node_counts, edge_counts = ontology(g)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "ontology.json"), "w") as f:
        json.dump({"node_types": dict(node_counts),
                   "edge_types": [{"from": a, "rel": r, "to": b, "count": c}
                                  for (a, r, b), c in sorted(edge_counts.items())]}, f, indent=2)
    lines = ["flowchart LR"]
    for t, c in sorted(node_counts.items()):
        lines.append(f'    {t}["{t}<br/>{c:,}"]')
    for (a, r, b), c in sorted(edge_counts.items()):
        lines.append(f"    {a} -->|{r}| {b}")
    with open(os.path.join(out_dir, "ontology.mmd"), "w") as f:
        f.write("\n".join(lines) + "\n")
    write_knowledge_graph(g, out_dir)
    return node_counts, edge_counts


def write_knowledge_graph(g, out_dir=OUT_DIR):
    """Writes the FULL knowledge graph (every record, not just the types) to
    <out_dir>/knowledge_graph.graphml (opens in Gephi, Cytoscape, yEd) and
    knowledge_graph.json (networkx node-link format). Each node keeps its type, id and
    attributes; each edge keeps its relationship name in "rel". Dates become "YYYY-MM-DD"
    strings and empty values are dropped, because GraphML only stores plain scalars.
    Edge ids must be unique across the whole file (Gephi / graphology reject duplicates),
    so each edge gets "e0", "e1", ... instead of the in-memory key, which is just the rel
    name and only unique per node pair."""
    def plain(d):
        return {k: (v if isinstance(v, (str, int, float, bool)) else str(v)) for k, v in d.items() if v is not None}

    h = nx.MultiDiGraph()
    h.add_nodes_from((n, plain(a)) for n, a in g.nodes(data=True))
    h.add_edges_from((u, v, f"e{i}", {**plain(a), "rel": k})
                     for i, (u, v, k, a) in enumerate(g.edges(keys=True, data=True)))
    os.makedirs(out_dir, exist_ok=True)
    nx.write_graphml(h, os.path.join(out_dir, "knowledge_graph.graphml"))
    with open(os.path.join(out_dir, "knowledge_graph.json"), "w") as f:
        json.dump(nx.node_link_data(h, edges="edges"), f)


# ======================================================================================
# Main: run every ACT incident from triage.json and print what it affects
# ======================================================================================

def print_result(event_id, i, inc, res):
    print(f"  [{i}] {res['kind']}   {res['path_schema']}")
    for line in res["notes"]:
        print(f"      - {line}")
    if res["kind"] == "quality_hold":
        print(f"      lot {res['lot_id']}: hold it; stop sale at {len(res['stop_sale'])} dealers "
              f"({sum(len(v) for v in res['stop_sale'].values())} vehicles); "
              f"notify {len(res['owners_to_notify'])} owners")
    else:
        pos = ", ".join(p["production_order_id"] for p in res["production_orders"])
        print(f"      POs: {pos}")
        print(f"      {len(res['customer_orders'])} customer orders at {len(res['dealers'])} dealers; "
              f"{len(res['loaner_candidates'])} Platinum/Gold customers eligible for a loaner")
    if res["example_path"]:
        print("      example path: " + " → ".join(res["example_path"]))


def main(argv):
    wanted = {a.upper() for a in argv if not a.startswith("--")}
    g = build_graph()
    print(f"Knowledge graph: {g.number_of_nodes():,} nodes, {g.number_of_edges():,} edges")

    if "--ontology" in argv:
        nodes, edges = write_ontology(g)
        print(f"Ontology: {len(nodes)} node types, {len(edges)} edge types -> {OUT_DIR}/ontology.mmd"
              f" (full graph -> {OUT_DIR}/knowledge_graph.graphml)")

    if not os.path.exists(TRIAGE_FILE):
        sys.exit(f"{TRIAGE_FILE} not found — run incident_triage.py first.")
    with open(TRIAGE_FILE, encoding="utf-8") as f:
        triage = json.load(f)

    output = []
    for ev in triage:
        if wanted and ev["event_id"] not in wanted:
            continue
        acts = [inc for inc in ev["incidents"] if inc["verdict"] == "ACT"]
        print("=" * 88)
        print(f"{ev['event_id']}  " + ", ".join(f"{i['verdict']} {i['category']}" for i in ev["incidents"]))
        results = []
        for i, inc in enumerate(ev["incidents"], 1):
            if inc["verdict"] != "ACT":
                continue  # NO_ACTION / ESCALATE: nothing to look up in the graph
            try:
                res = affected(g, inc)
            except (ValueError, KeyError) as e:
                # A traversal failure means the data didn't line up with the incident.
                # The coordinator should escalate rather than act on a partial answer.
                res = {"kind": inc["category"], "error": str(e)}
                print(f"  [{i}] ERROR {e}")
            else:
                print_result(ev["event_id"], i, inc, res)
            res["incident_index"] = i
            results.append(res)
        if not acts:
            print("  nothing to look up (no ACT incidents)")
        output.append({"event_id": ev["event_id"], "affected": results})

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, "affected.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    print("=" * 88)
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main(sys.argv[1:])
