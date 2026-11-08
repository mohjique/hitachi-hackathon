#!/usr/bin/env python3
"""
incident_triage.py — Stage 1 of the control tower: READ each event and turn it into
clean, checked "incidents" before anything touches the knowledge graph or the tools.

What it does, per event in data/events.json
-------------------------------------------
  1. LLM extract   Gemini reads the message, splits it into incidents, labels each one
                   (supplier_delay / plant_outage / quality_hold / noise / unconfirmed),
                   and copies out the EXACT words used for plants, parts, suppliers, dates.
  2. Quote check   Every phrase the LLM returned must really appear in the message.
                   (Catches the model inventing things.)
  3. Resolve names "the Texas plant" -> P_TX, "Quanta" -> SUP-12   (aliases.csv + official names)
  4. Resolve dates "next Friday" -> 2027-03-19, "last Monday" -> 2027-03-01   (plain code)
  5. Completeness  Does the incident have every field its category needs?
  6. Data check    Does the shipment / lot / plant really exist and match?

Every incident ends with one VERDICT:
  ACT        -> fully resolved and checked; safe to hand to the planner (next stage)
  NO_ACTION  -> noise; do nothing
  ESCALATE   -> vague, unconfirmed, or something failed a check; a human should look

The LLM only does step 1. Steps 2-6 are deterministic code, so they are testable and
give the same answer every time. If ANY check fails, we escalate instead of guessing.

How to run
----------
  pip install google-genai
  # put your key in a file called .env next to this script:   GEMINI_API_KEY=your-key
  python incident_triage.py            # all 15 events
  python incident_triage.py E-01 E-14  # just some events
  python incident_triage.py --refresh  # ignore cached LLM answers and call Gemini again

Outputs
-------
  * A readable report printed to the terminal.
  * runs/triage/triage.json   — the same results as JSON, for the next stage to read.
  * runs/triage/llm_cache/    — the raw LLM answer per event. Re-runs read from here,
                                so they are free and repeatable. Delete it (or --refresh)
                                after you change the prompt.

Only the standard library is used apart from the `google-genai` package.
"""

import csv
import json
import os
import re
import sys
from datetime import date, timedelta

# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
OUT_DIR = os.path.join(HERE, "runs", "triage")
CACHE_DIR = os.path.join(OUT_DIR, "llm_cache")
ENV_FILE = os.path.join(HERE, ".env")


def load_env_file(path=ENV_FILE):
    """Read KEY=value lines from a .env file into os.environ.

    A tiny stand-in for the python-dotenv package, so no extra install is needed.
    Rules:
      * blank lines and lines starting with # are skipped
      * an optional leading "export " is allowed      (export GEMINI_API_KEY=...)
      * values may be wrapped in "double" or 'single' quotes
      * a variable ALREADY set in your shell wins over the file
        (handy for trying a different key without editing .env)
    Keep .env out of git and out of your submission: it holds your secret key.
    """
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key, value)


load_env_file()  # runs once, when the script starts

# Which Gemini model to use. A small, cheap "Flash-Lite" / "Flash" model is enough because
# the LLM only reads text. Check Google's current model list and put the exact id here
# (or set GEMINI_MODEL in your shell). Cite this id in your submission.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

# "Today" in the challenge world (handbook: Monday 2027-03-08 is working-day index 0).
TODAY = date(2027, 3, 8)
# The planning window: tools only accept dates from today through this day.
WINDOW_END = date(2027, 4, 2)


# --------------------------------------------------------------------------------------
# Load the company data we need for checking (plain csv, nothing fancy)
# --------------------------------------------------------------------------------------

def read_csv(name):
    with open(os.path.join(DATA_DIR, name), newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


PLANTS = {r["plant_id"]: r for r in read_csv("plants.csv")}
PARTS = {r["part_id"]: r for r in read_csv("parts.csv")}
SUPPLIERS = {r["supplier_id"]: r for r in read_csv("suppliers.csv")}
SHIPMENTS = {r["shipment_id"]: r for r in read_csv("inbound_shipments.csv")}
LOTS = {r["lot_id"]: r for r in read_csv("part_lots.csv")}
ALIASES = read_csv("aliases.csv")


# --------------------------------------------------------------------------------------
# STEP 1 — the LLM call
# --------------------------------------------------------------------------------------

# The prompt. Note what we ask for: EXACT phrases, not ids and not converted dates.
# Small models make mistakes with date arithmetic and id lookups; code does those better.
# If you add worked examples here, write your own — never paste the 15 test events in
# (that would count as hard-coding answers).
PROMPT = """You read operations messages for a car maker. Today is Monday 2027-03-08.

Split the message into separate incidents (one message can contain several).
Give each incident exactly one category:
- supplier_delay: a shipment of a part will arrive later than planned
- plant_outage: a plant cannot build for a range of days
- quality_hold: a batch (lot) of a part is defective; vehicles built in a date window are suspect
- noise: not about our operations at all (questions about brochures, far-away news, etc.)
- unconfirmed: possibly about our operations, but a rumour, vague, or explicitly not confirmed

Fill the fields by COPYING the exact words from the message. Do NOT convert dates,
do NOT translate names into codes, do NOT guess. Use null when the message does not say.
- plant, part, supplier: the words used for them (e.g. "the Texas plant", "harness B")
- shipment_id, lot_id: only if an id like SH-0123 or LOT-0456 is written in the message
- start_date / end_date mean:
    supplier_delay -> start_date = ORIGINAL arrival date, end_date = NEW arrival date
    plant_outage   -> start_date = first day down,        end_date = last day down
    quality_hold   -> start_date = first suspect build day, end_date = last suspect build day
- quote: the sentence(s) of the message this incident comes from, copied exactly
- confirmed: false for rumours, hearsay, "maybe", "possibly", "not confirmed"; true otherwise
- reason: one short sentence explaining your category choice

Message source: {source}
Message:
{text}
"""

# The JSON shape Gemini must answer in. This is Gemini's "response schema" format
# (an OpenAPI-style subset). Gemini is forced to return JSON matching it, so we never
# have to scrape fields out of free text.
_NULLABLE_STR = {"type": "STRING", "nullable": True}
INCIDENT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "category": {"type": "STRING",
                     "enum": ["supplier_delay", "plant_outage", "quality_hold", "noise", "unconfirmed"]},
        "quote": {"type": "STRING"},
        "plant": _NULLABLE_STR,
        "part": _NULLABLE_STR,
        "supplier": _NULLABLE_STR,
        "shipment_id": _NULLABLE_STR,
        "lot_id": _NULLABLE_STR,
        "start_date": _NULLABLE_STR,
        "end_date": _NULLABLE_STR,
        "confirmed": {"type": "BOOLEAN"},
        "reason": {"type": "STRING"},
    },
    "required": ["category", "quote", "plant", "part", "supplier", "shipment_id", "lot_id",
                 "start_date", "end_date", "confirmed", "reason"],
}
EXTRACTION_SCHEMA = {
    "type": "OBJECT",
    "properties": {"incidents": {"type": "ARRAY", "items": INCIDENT_SCHEMA}},
    "required": ["incidents"],
}

_client = None  # created on first use so the rest of the file works without the package


def llm_extract(event, refresh=False):
    """Ask Gemini to read one event. Returns {"incidents": [...]} as a plain dict.

    Answers are cached per event in runs/triage/llm_cache/<event_id>.json:
      - re-runs cost nothing and give identical results (important for a graded run)
      - your trace can show exactly what the model said
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, f"{event['event_id']}.json")
    if not refresh and os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            return json.load(f)

    global _client
    if _client is None:
        try:
            from google import genai  # pip install google-genai
        except ImportError:
            sys.exit("The 'google-genai' package is missing. Run:  pip install google-genai")
        # The key was loaded from .env by load_env_file() at startup.
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            sys.exit(f"No API key found. Add this line to {ENV_FILE}:\n  GEMINI_API_KEY=your-key")
        _client = genai.Client(api_key=api_key)  # pass the key explicitly

    from google.genai import types
    response = _client.models.generate_content(
        model=GEMINI_MODEL,
        contents=PROMPT.format(source=event.get("source", "unknown"), text=event["text"]),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",  # answer must be JSON...
            response_schema=EXTRACTION_SCHEMA,       # ...in exactly this shape
            temperature=0,                           # as repeatable as possible
        ),
    )
    result = json.loads(response.text)
    result["_model"] = GEMINI_MODEL  # remember which model produced this (for the write-up)

    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    return result


# --------------------------------------------------------------------------------------
# STEP 2 — quote check: did the model copy words that are really in the message?
# --------------------------------------------------------------------------------------

def _squash(s):
    """Lower-case and collapse whitespace so small spacing differences don't matter."""
    return re.sub(r"\s+", " ", s.lower()).strip()


def appears_in(phrase, text):
    return phrase is None or _squash(phrase) in _squash(text)


# --------------------------------------------------------------------------------------
# STEP 3 — names -> ids
# --------------------------------------------------------------------------------------
#
# We build one lookup list per kind (plant / part / supplier) from:
#   * aliases.csv            ("the Texas plant" -> P_TX, "Quanta" -> SUP-12)
#   * the official names     ("Wacky Wheels Lexington", "Kestrel Electronics", "brake kit")
#   * the ids themselves     ("P_KY", "CHIP-IVI")
#   * plant cities           ("Lexington" from "Lexington, KY")
# Then a phrase is matched in three increasingly loose ways (see resolve_name).

def _norm_name(s):
    """Normalise a name for matching: lower case, hyphens -> spaces, drop filler words."""
    s = s.lower().replace("-", " ")
    s = re.sub(r"[^\w\s]", " ", s)                           # drop punctuation like ( ) ,
    s = re.sub(r"\b(the|plant|line|factory|site)\b", " ", s)  # "the Texas plant" -> "texas"
    return re.sub(r"\s+", " ", s).strip()


def _build_name_index():
    index = {"plant": [], "part": [], "supplier": []}

    def add(kind, name, entity_id):
        n = _norm_name(name)
        if n:
            index[kind].append((n, entity_id))

    for r in ALIASES:
        add(r["entity_type"], r["alias"], r["entity_id"])
    for pid, r in PLANTS.items():
        add("plant", pid, pid)
        add("plant", r["name"], pid)
        add("plant", r["city"].split(",")[0], pid)
    for pid, r in PARTS.items():
        add("part", pid, pid)
        add("part", r["name"], pid)
    for sid, r in SUPPLIERS.items():
        add("supplier", sid, sid)
        add("supplier", r["name"], sid)
    return index


NAME_INDEX = _build_name_index()


def resolve_name(phrase, kind):
    """Turn a phrase into an id. Returns (id or None, explanation).

    Matching order (stops at the first that gives exactly ONE id):
      a) exact match of the normalised phrase            "texas" == "texas"
      b) a known name appears inside the phrase           "huntsville" in "huntsville"
         (longest known name wins)                        "brake kit" in "brake kit order"
      c) the phrase appears inside exactly one known name "camera kit" in "surround view camera kit"
    If several different ids match, the name is AMBIGUOUS and we refuse to guess.
    """
    if not phrase:
        return None, f"no {kind} mentioned"
    p = _norm_name(phrase)
    entries = NAME_INDEX[kind]

    # a) exact
    ids = {eid for n, eid in entries if n == p}
    if len(ids) == 1:
        return ids.pop(), f'"{phrase}" -> exact name match'
    if len(ids) > 1:
        return None, f'"{phrase}" is ambiguous: {sorted(ids)}'

    # b) a known name inside the phrase, longest first
    inside = [(len(n), n, eid) for n, eid in entries if re.search(rf"\b{re.escape(n)}\b", p)]
    if inside:
        longest = max(length for length, _, _ in inside)
        ids = {eid for length, _, eid in inside if length == longest}
        if len(ids) == 1:
            name = next(n for length, n, _ in inside if length == longest)
            return ids.pop(), f'"{phrase}" contains known name "{name}"'
        return None, f'"{phrase}" is ambiguous: {sorted(ids)}'

    # c) the phrase inside a known name (only for phrases long enough to mean something)
    if len(p) >= 4:
        ids = {eid for n, eid in entries if re.search(rf"\b{re.escape(p)}\b", n)}
        if len(ids) == 1:
            return ids.pop(), f'"{phrase}" is part of a single known name'
        if len(ids) > 1:
            return None, f'"{phrase}" is ambiguous: {sorted(ids)}'

    return None, f'"{phrase}" does not match any known {kind}'


# --------------------------------------------------------------------------------------
# STEP 4 — date phrases -> real dates
# --------------------------------------------------------------------------------------
#
# Understood forms (case-insensitive, filler like "from"/"through"/"inclusive" is ignored):
#   2027-03-24              ISO
#   Mar 22 / March 22nd     month + day (year 2027 unless written)
#   Monday Mar 22           weekday + date (we CHECK the weekday matches the date)
#   today / tomorrow
#   next Friday             the Friday of NEXT week          -> 2027-03-19
#   last Monday             the Monday of LAST week          -> 2027-03-01
#   this Friday             the Friday of this week          -> 2027-03-12
#   Friday                  the next Friday from today       -> 2027-03-12
#
# TEAM DECISION: "next <weekday>" = that weekday in the following calendar week.
# With today a Monday, "next Monday" = 03-15 and "next Friday" = 03-19. This matches the
# data (e.g. the Quanta chip shipment due "next Monday" really is due 2027-03-15).

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def resolve_date(phrase):
    """Returns (date or None, explanation)."""
    if not phrase:
        return None, "no date given"
    p = phrase.lower()
    p = re.sub(r"\b(from|through|thru|until|till|to|by|on|starting|inclusive|the|of)\b", " ", p)
    p = re.sub(r"\s+", " ", p).strip(" ,.;()")

    weekday = next((i for i, w in enumerate(WEEKDAYS) if re.search(rf"\b{w[:3]}[a-z]*\b", p)), None)
    found = None
    how = ""

    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", p)                                 # ISO
    if m:
        found, how = date(int(m[1]), int(m[2]), int(m[3])), "ISO date"
    if found is None:
        m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})"
                      r"(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?\b", p)                       # Mar 22
        if m:
            found = date(int(m[3] or TODAY.year), MONTHS[m[1]], int(m[2]))
            how = "month and day"
    if found is not None:
        if weekday is not None and found.weekday() != weekday:
            return None, f'"{phrase}": {found} is a {WEEKDAYS[found.weekday()]}, not a {WEEKDAYS[weekday]}'
    elif p == "today":
        found, how = TODAY, "today"
    elif p == "tomorrow":
        found, how = TODAY + timedelta(days=1), "tomorrow"
    elif weekday is not None:
        this_monday = TODAY - timedelta(days=TODAY.weekday())
        if re.search(r"\bnext\b", p):
            found, how = this_monday + timedelta(days=7 + weekday), "'next' = that day in next week"
        elif re.search(r"\b(last|past|previous)\b", p):
            found, how = this_monday + timedelta(days=weekday - 7), "'last' = that day in last week"
        elif re.search(r"\bthis\b", p):
            found, how = this_monday + timedelta(days=weekday), "'this' = that day in this week"
        else:
            found = this_monday + timedelta(days=weekday)
            if found < TODAY:
                found += timedelta(days=7)
            how = "bare weekday = the next one coming"
    else:
        return None, f'"{phrase}": could not understand this date'

    if found.weekday() >= 5:
        return None, f'"{phrase}" -> {found} is a weekend (plants only build Mon-Fri)'
    return found, f'"{phrase}" -> {found} ({how})'


# --------------------------------------------------------------------------------------
# STEPS 5 + 6 — completeness and data checks, one function per category
# --------------------------------------------------------------------------------------
#
# Each checker receives a "draft" (names and dates already resolved, see triage_incident)
# and returns the finished incident. It appends human-readable lines to draft["trace"]
# so you can always see WHY a verdict was reached. Any failure -> ESCALATE.

def _escalate(draft, reason):
    draft["verdict"] = "ESCALATE"
    draft["verdict_reason"] = reason
    return draft


def check_supplier_delay(d):
    """Needs: which shipment (directly, or via part + plant + original date) and the new date.
    Affected builds (for the next stage): the plant's orders whose model uses the part,
    from the original date up to but NOT including the new date (handbook rule 1)."""
    if d["shipment_id"]:
        sh = SHIPMENTS.get(d["shipment_id"])
        if not sh:
            return _escalate(d, f"shipment {d['shipment_id']} does not exist")
        # The shipment row is the source of truth; anything the text said must agree with it.
        for field, col in (("part_id", "part_id"), ("plant_id", "plant_id"), ("supplier_id", "supplier_id")):
            if d[field] and d[field] != sh[col]:
                return _escalate(d, f"message says {field}={d[field]} but {sh['shipment_id']} has {sh[col]}")
            d[field] = sh[col]
        eta = date.fromisoformat(sh["eta_date"])
        if d["start"] and d["start"] != eta:
            return _escalate(d, f"message says original date {d['start']} but {sh['shipment_id']} is due {eta}")
        d["start"] = eta
        d["trace"].append(f"shipment {sh['shipment_id']} found: {sh['part_id']} to {sh['plant_id']}, due {eta}")
    else:
        missing = [f for f in ("part_id", "plant_id", "start") if not d[f]]
        if missing:
            return _escalate(d, f"cannot identify the shipment, missing: {', '.join(missing)}")
        # Find the shipment of this part, to this plant, due on the original date.
        matches = [s for s in SHIPMENTS.values()
                   if s["part_id"] == d["part_id"] and s["plant_id"] == d["plant_id"]
                   and s["eta_date"] == d["start"].isoformat()
                   and (not d["supplier_id"] or s["supplier_id"] == d["supplier_id"])]
        if len(matches) != 1:
            return _escalate(d, f"expected exactly 1 shipment of {d['part_id']} to {d['plant_id']} "
                                f"due {d['start']}, found {len(matches)}")
        d["shipment_id"] = matches[0]["shipment_id"]
        d["supplier_id"] = matches[0]["supplier_id"]
        d["trace"].append(f"matched shipment {d['shipment_id']} (part + plant + original date)")

    if not d["end"]:
        return _escalate(d, "no new arrival date given")
    if d["end"] <= d["start"]:
        return _escalate(d, f"new date {d['end']} is not after the original date {d['start']}")
    d["trace"].append(f"builds needing {d['part_id']} at {d['plant_id']} blocked {d['start']} up to "
                      f"(not incl.) {d['end']}")
    d["verdict"] = "ACT"
    return d


def check_plant_outage(d):
    """Needs: plant, first and last day down. Every order at that plant in that range
    (inclusive) is affected; the plant restarts the next working day (handbook rule 2)."""
    missing = [f for f in ("plant_id", "start", "end") if not d[f]]
    if missing:
        return _escalate(d, f"outage is missing: {', '.join(missing)}")
    if d["end"] < d["start"]:
        return _escalate(d, f"outage ends ({d['end']}) before it starts ({d['start']})")
    if d["end"] < TODAY or d["start"] > WINDOW_END:
        return _escalate(d, f"outage {d['start']}..{d['end']} is outside the planning window")
    d["trace"].append(f"{d['plant_id']} down {d['start']} through {d['end']} (inclusive)")
    d["verdict"] = "ACT"
    return d


def check_quality_hold(d):
    """Needs: the defective lot — named directly, or found from part + plant (+ supplier)
    + build window. The lot table is the source of truth."""
    if d["lot_id"]:
        lot = LOTS.get(d["lot_id"])
        if not lot:
            return _escalate(d, f"lot {d['lot_id']} does not exist")
        for field in ("part_id", "plant_id", "supplier_id"):
            if d[field] and d[field] != lot[field]:
                return _escalate(d, f"message says {field}={d[field]} but {lot['lot_id']} has {lot[field]}")
            d[field] = lot[field]
        # If the message also gives a build window, it must be the lot's window.
        for field, col in (("start", "first_build_date"), ("end", "last_build_date")):
            if d[field] and d[field].isoformat() != lot[col]:
                return _escalate(d, f"message says build {field} {d[field]} but {lot['lot_id']} has {lot[col]}")
        d["start"] = date.fromisoformat(lot["first_build_date"])
        d["end"] = date.fromisoformat(lot["last_build_date"])
        d["trace"].append(f"lot {lot['lot_id']} found: {lot['part_id']} from {lot['supplier_id']} at "
                          f"{lot['plant_id']}, built {lot['first_build_date']}..{lot['last_build_date']}")
        d["lots"] = [lot["lot_id"]]
    else:
        missing = [f for f in ("part_id", "plant_id", "start", "end") if not d[f]]
        if missing:
            return _escalate(d, f"cannot identify the lot, missing: {', '.join(missing)}")
        candidates = [l for l in LOTS.values()
                      if l["part_id"] == d["part_id"] and l["plant_id"] == d["plant_id"]
                      and (not d["supplier_id"] or l["supplier_id"] == d["supplier_id"])]
        # Best case: a lot whose build window is exactly the window in the message.
        exact = [l for l in candidates if l["first_build_date"] == d["start"].isoformat()
                 and l["last_build_date"] == d["end"].isoformat()]
        # Otherwise: lots whose window overlaps the message's window.
        overlap = [l for l in candidates if l["first_build_date"] <= d["end"].isoformat()
                   and l["last_build_date"] >= d["start"].isoformat()]
        chosen = exact or overlap
        if len(chosen) != 1:
            return _escalate(d, f"expected exactly 1 lot of {d['part_id']} at {d['plant_id']} for "
                                f"{d['start']}..{d['end']}, found {len(chosen)}")
        lot = chosen[0]
        d["supplier_id"] = d["supplier_id"] or lot["supplier_id"]
        d["lots"] = [lot["lot_id"]]
        d["trace"].append(f"matched lot {lot['lot_id']} ({'exact window' if exact else 'overlapping window'}: "
                          f"{lot['first_build_date']}..{lot['last_build_date']})")
    d["verdict"] = "ACT"
    return d


CHECKERS = {
    "supplier_delay": check_supplier_delay,
    "plant_outage": check_plant_outage,
    "quality_hold": check_quality_hold,
}


# --------------------------------------------------------------------------------------
# Putting the steps together for ONE incident
# --------------------------------------------------------------------------------------

def triage_incident(raw, event_text):
    """raw = one incident exactly as the LLM returned it. Returns a resolved incident dict."""
    d = {
        "category": raw["category"],
        "quote": raw.get("quote"),
        "llm_reason": raw.get("reason"),
        "raw": {k: raw.get(k) for k in ("plant", "part", "supplier", "shipment_id", "lot_id",
                                        "start_date", "end_date", "confirmed")},
        "plant_id": None, "part_id": None, "supplier_id": None,
        "shipment_id": None, "lot_id": None, "lots": None,
        "start": None, "end": None,
        "verdict": None, "verdict_reason": None,
        "trace": [],
    }

    # Noise: nothing to resolve. The LLM's reason is the explanation the judges want.
    if raw["category"] == "noise":
        d["verdict"], d["verdict_reason"] = "NO_ACTION", "not about operations"
        return d

    # Rumours / unconfirmed reports: never act, always hand to a human.
    if raw["category"] == "unconfirmed" or not raw.get("confirmed", True):
        return _escalate(d, "report is unconfirmed or vague")

    # STEP 2: every phrase must really be in the message.
    for key in ("plant", "part", "supplier", "shipment_id", "lot_id", "start_date", "end_date"):
        if not appears_in(raw.get(key), event_text):
            return _escalate(d, f'LLM returned {key}="{raw.get(key)}", which is not in the message')
    d["trace"].append("all extracted phrases appear in the message")

    # STEP 3: names -> ids. A name that was MENTIONED but can't be resolved is a reason to
    # stop: acting on the wrong plant or part is worse than asking a human.
    for key, kind in (("plant", "plant"), ("part", "part"), ("supplier", "supplier")):
        if raw.get(key):
            entity_id, why = resolve_name(raw[key], kind)
            d["trace"].append(why)
            if entity_id is None:
                return _escalate(d, why)
            d[f"{kind}_id"] = entity_id

    # Ids written literally in the message (SH-0182, LOT-0191): normalise the format.
    if raw.get("shipment_id"):
        d["shipment_id"] = raw["shipment_id"].strip().upper()
    if raw.get("lot_id"):
        d["lot_id"] = raw["lot_id"].strip().upper()

    # STEP 4: date phrases -> dates.
    for key, field in (("start_date", "start"), ("end_date", "end")):
        if raw.get(key):
            when, why = resolve_date(raw[key])
            d["trace"].append(why)
            if when is None:
                return _escalate(d, why)
            d[field] = when

    # STEPS 5 + 6: category-specific completeness and data checks.
    return CHECKERS[raw["category"]](d)


def triage_event(event, refresh=False):
    """Run the whole pipeline for one event. Never raises: failures become ESCALATE."""
    try:
        extraction = llm_extract(event, refresh=refresh)
    except Exception as e:  # network error, bad key, malformed answer...
        return {"event_id": event["event_id"], "text": event["text"], "model": GEMINI_MODEL,
                "incidents": [_escalate({"category": "unknown", "quote": None, "llm_reason": None,
                                         "trace": [], "lots": None}, f"LLM call failed: {e}")]}

    incidents = [triage_incident(raw, event["text"]) for raw in extraction.get("incidents", [])]
    if not incidents:  # the model returned nothing at all: don't silently drop the event
        incidents = [_escalate({"category": "unknown", "quote": None, "llm_reason": None,
                                "trace": [], "lots": None}, "LLM found no incident in the message")]
    return {"event_id": event["event_id"], "text": event["text"],
            "model": extraction.get("_model", GEMINI_MODEL), "incidents": incidents}


# --------------------------------------------------------------------------------------
# Terminal report
# --------------------------------------------------------------------------------------

VERDICT_LABEL = {"ACT": "ACT       ", "NO_ACTION": "NO ACTION ", "ESCALATE": "ESCALATE  "}


def print_report(results):
    for res in results:
        print("=" * 88)
        print(f"{res['event_id']}  {res['text'].splitlines()[0][:76]}")
        for i, inc in enumerate(res["incidents"], 1):
            label = VERDICT_LABEL.get(inc["verdict"], inc["verdict"])
            print(f"  [{i}] {label} {inc['category']}")
            # The resolved facts, only the ones that are filled in
            facts = []
            for key in ("plant_id", "part_id", "supplier_id", "shipment_id", "lot_id"):
                if inc.get(key):
                    facts.append(f"{key.replace('_id', '')}={inc[key]}")
            if inc.get("lots") and not inc.get("lot_id"):
                facts.append(f"lot={','.join(inc['lots'])}")
            if inc.get("start") or inc.get("end"):
                facts.append(f"dates={inc.get('start')} -> {inc.get('end')}")
            if facts:
                print("      " + "  ".join(facts))
            if inc.get("verdict_reason"):
                print(f"      why: {inc['verdict_reason']}")
            if inc.get("llm_reason"):
                print(f"      LLM: {inc['llm_reason']}")
            for line in inc.get("trace", []):
                print(f"        - {line}")
    # One-line summary per verdict at the end
    counts = {}
    for res in results:
        for inc in res["incidents"]:
            counts[inc["verdict"]] = counts.get(inc["verdict"], 0) + 1
    print("=" * 88)
    print("Totals: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))


def _json_default(o):
    return o.isoformat() if isinstance(o, date) else str(o)


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------

def main(argv):
    refresh = "--refresh" in argv
    wanted = {a.upper() for a in argv if not a.startswith("--")}

    with open(os.path.join(DATA_DIR, "events.json"), encoding="utf-8") as f:
        events = json.load(f)
    if wanted:
        events = [e for e in events if e["event_id"] in wanted]

    results = [triage_event(e, refresh=refresh) for e in events]
    print_report(results)

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, "triage.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=_json_default)
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main(sys.argv[1:])
