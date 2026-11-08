"""Hack the Highway: the company data and the mock enterprise systems (MES, dealer portal, CRM, quality). Stdlib only.

Each event is handled in its own fresh copy of the world, so what you do for one event never changes another.
"""
import csv
import json
import os
from collections import defaultdict
from datetime import date, timedelta

START = date(2027, 3, 8)        # working-day index 0 (a Monday); events arrive on this day
FIRST_IDX, LAST_IDX = -10, 19   # past two weeks (already built) and four future weeks, Mon-Fri only
TIER_RANK = {"Platinum": 0, "Gold": 1, "Standard": 2}
LOANER_TIERS = ("Platinum", "Gold")


def idx_to_date(i):
    return START + timedelta(days=7 * (i // 5) + i % 5)


def date_to_idx(d):
    days = (d - START).days
    if days % 7 > 4:
        raise ValueError("weekend")
    return 5 * (days // 7) + days % 7


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path, fields, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


class ToolError(Exception):
    pass


TOOLS = {
    "mes.get_schedule": {"desc": "Orders built at a plant on a date, with free slots (live view).",
                         "args": {"plant_id": "str", "date": "YYYY-MM-DD"}, "writes": False},
    "mes.reschedule": {"desc": "Move a future production order to another working day at the same plant.",
                       "args": {"production_order_id": "str", "new_date": "YYYY-MM-DD"}, "writes": True},
    "mes.transfer": {"desc": "Move a future production order to another plant that builds the model.",
                     "args": {"production_order_id": "str", "to_plant_id": "str", "new_date": "YYYY-MM-DD"},
                     "writes": True},
    "dealer.notify": {"desc": "Tell a dealer about delayed customer orders.",
                      "args": {"dealer_id": "str", "customer_order_ids": "list[str]", "message": "str"},
                      "writes": True},
    "dealer.stop_sale": {"desc": "Stop sale of unsold vehicles (by VIN) at a dealer.",
                         "args": {"dealer_id": "str", "vins": "list[str]"}, "writes": True},
    "crm.loaner_availability": {"desc": "Loaner cars still free at a dealer.",
                                "args": {"dealer_id": "str"}, "writes": False},
    "crm.assign_loaner": {"desc": "Give the customer of a delayed order a loaner car from their dealer's pool.",
                          "args": {"customer_order_id": "str"}, "writes": True},
    "crm.notify_customer": {"desc": "Contact a customer (owner or buyer).",
                            "args": {"customer_id": "str", "message": "str"}, "writes": True},
    "quality.hold_lot": {"desc": "Place a part lot on quality hold.", "args": {"lot_id": "str"}, "writes": True},
    "escalate": {"desc": "Hand an unclear or unconfirmed report to a human.", "args": {"reason": "str"},
                 "writes": False},
}
WRITE_TOOLS = {k for k, v in TOOLS.items() if v["writes"]}


class Enterprise:
    def __init__(self, data_dir):
        rd = lambda n: read_csv(os.path.join(data_dir, n))
        self.plants = {r["plant_id"]: {"name": r["name"], "slots": int(r["slots_per_day"]), "models": set()}
                       for r in rd("plants.csv")}
        for r in rd("plant_models.csv"):
            self.plants[r["plant_id"]]["models"].add(r["model"])
        self.orders = {r["production_order_id"]: {"plant_id": r["plant_id"], "idx": date_to_idx(date.fromisoformat(r["build_date"])),
                                                  "model": r["model"], "units": int(r["units"])}
                       for r in rd("production_orders.csv")}
        self.dealers = {r["dealer_id"]: r for r in rd("dealers.csv")}
        self.customers = {r["customer_id"]: r for r in rd("customers.csv")}
        self.cos = {r["customer_order_id"]: r for r in rd("customer_orders.csv")}
        self.vehicles = {r["vin"]: r for r in rd("finished_vehicles.csv")}
        self.lots = {r["lot_id"]: r for r in rd("part_lots.csv")}
        self.loaner_pool = {r["dealer_id"]: int(r["loaners_available"]) for r in rd("loaners.csv")}
        self.bom = defaultdict(set)
        for r in rd("bom.csv"):
            self.bom[r["model"]].add(r["part_id"])
        self._state = {}

    def state(self, event_id):
        if event_id not in self._state:
            s = {"loc": {}, "slots": defaultdict(set), "loaners": dict(self.loaner_pool), "loaned": set(),
                 "held": set(), "stopped": set(), "notified": defaultdict(set), "customers": set(), "escalated": False}
            for po, o in self.orders.items():
                if o["idx"] >= 0:
                    s["loc"][po] = (o["plant_id"], o["idx"])
                    s["slots"][(o["plant_id"], o["idx"])].add(po)
            self._state[event_id] = s
        return self._state[event_id]

    # ---- helpers
    @staticmethod
    def _need(args, name, kind):
        v = args.get(name)
        if kind == "str" and not (isinstance(v, str) and v):
            raise ToolError(f"{name} must be a non-empty string")
        if kind == "list" and not (isinstance(v, list) and v and all(isinstance(x, str) for x in v)):
            raise ToolError(f"{name} must be a non-empty list of strings")
        return v

    def _day(self, text):
        try:
            i = date_to_idx(date.fromisoformat(text))
        except (TypeError, ValueError):
            raise ToolError("date must be a weekday formatted YYYY-MM-DD") from None
        if not 0 <= i <= LAST_IDX:
            raise ToolError("date must be a working day from today through the end of the planning window")
        return i

    def _move(self, s, po, plant, idx):
        if po not in s["loc"]:
            raise ToolError("unknown or already built production order")
        cur = s["loc"][po]
        if (plant, idx) == cur:
            raise ToolError("order is already there")
        if len(s["slots"][(plant, idx)]) >= self.plants[plant]["slots"]:
            raise ToolError(f"no free slot at {plant} on that day")
        s["slots"][cur].discard(po)
        s["slots"][(plant, idx)].add(po)
        s["loc"][po] = (plant, idx)
        return {"ok": True, "production_order_id": po, "plant_id": plant, "date": idx_to_date(idx).isoformat()}

    # ---- the single entry point
    def call(self, event_id, tool, args):
        if tool not in TOOLS:
            raise ToolError("unknown tool")
        if not isinstance(args, dict):
            raise ToolError("args must be an object")
        s = self.state(event_id)
        need = lambda n, k="str": self._need(args, n, k)
        if tool == "mes.get_schedule":
            plant, idx = need("plant_id"), self._day(need("date"))
            if plant not in self.plants:
                raise ToolError("unknown plant_id")
            here = sorted(s["slots"][(plant, idx)])
            return {"plant_id": plant, "date": args["date"], "orders": [{"production_order_id": p, "model": self.orders[p]["model"]} for p in here],
                    "free_slots": self.plants[plant]["slots"] - len(here)}
        if tool == "mes.reschedule":
            po = need("production_order_id")
            if po not in s["loc"]:
                raise ToolError("unknown or already built production order")
            return self._move(s, po, s["loc"][po][0], self._day(need("new_date")))
        if tool == "mes.transfer":
            po, plant = need("production_order_id"), need("to_plant_id")
            if po not in s["loc"]:
                raise ToolError("unknown or already built production order")
            if plant not in self.plants or self.orders[po]["model"] not in self.plants[plant]["models"]:
                raise ToolError("that plant does not build this model")
            if plant == s["loc"][po][0]:
                raise ToolError("use mes.reschedule inside the same plant")
            return self._move(s, po, plant, self._day(need("new_date")))
        if tool == "dealer.notify":
            dealer, ids = need("dealer_id"), need("customer_order_ids", "list")
            need("message")
            if dealer not in self.dealers:
                raise ToolError("unknown dealer_id")
            bad = [i for i in ids if i not in self.cos or self.cos[i]["dealer_id"] != dealer]
            if bad:
                raise ToolError(f"not customer orders of {dealer}: {bad[:3]}")
            s["notified"][dealer].update(ids)
            return {"ok": True}
        if tool == "dealer.stop_sale":
            dealer, vins = need("dealer_id"), need("vins", "list")
            if dealer not in self.dealers:
                raise ToolError("unknown dealer_id")
            bad = [v for v in vins if v not in self.vehicles or self.vehicles[v]["dealer_id"] != dealer
                   or self.vehicles[v]["status"] != "in_stock"]
            if bad:
                raise ToolError(f"not unsold vehicles at {dealer}: {bad[:3]}")
            s["stopped"].update(vins)
            return {"ok": True}
        if tool == "crm.loaner_availability":
            dealer = need("dealer_id")
            if dealer not in s["loaners"]:
                raise ToolError("unknown dealer_id")
            return {"dealer_id": dealer, "available": s["loaners"][dealer]}
        if tool == "crm.assign_loaner":
            co = need("customer_order_id")
            if co not in self.cos:
                raise ToolError("unknown customer_order_id")
            dealer = self.cos[co]["dealer_id"]
            if co in s["loaned"]:
                raise ToolError("already has a loaner")
            if s["loaners"][dealer] <= 0:
                raise ToolError(f"no loaners left at {dealer}")
            s["loaners"][dealer] -= 1
            s["loaned"].add(co)
            return {"ok": True, "dealer_id": dealer}
        if tool == "crm.notify_customer":
            cid = need("customer_id")
            need("message")
            if cid not in self.customers:
                raise ToolError("unknown customer_id")
            s["customers"].add(cid)
            return {"ok": True}
        if tool == "quality.hold_lot":
            lot = need("lot_id")
            if lot not in self.lots:
                raise ToolError("unknown lot_id")
            s["held"].add(lot)
            return {"ok": True}
        need("reason")
        s["escalated"] = True
        return {"ok": True}


def load_events(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)
