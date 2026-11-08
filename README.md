# Wacky Wheels Control Tower

A multi-agent operations control tower for the **Hack the Highway** challenge. It reads the 15 operational events in `data/events.json`, works out what each one means, finds every affected record in a knowledge graph of the company data, and takes the corrective actions the policy handbook requires through the provided enterprise server.

- **LLM:** Gemini (`gemini-3.5-flash-lite` by default; see [Configuration](#configuration)). It is used only to read each message: one call per event, cached.
- **Everything else is deterministic Python:** name and date resolution, graph traversal, scheduling, loaners and quality holds. Anything that is graded exactly gives the same answer every run.

---

## Quick start

```bash
# 1. Install (Python 3.10+)
pip install -r requirements.txt

# 2. Add your Gemini key in a file named .env in this folder (never committed)
echo "GEMINI_API_KEY=your-key-here" > .env

# 3. Start the control tower
python app.py
```

Your browser opens **http://localhost:8080**. Press **▶ Play**:

1. `runs/live/` is deleted, so every run starts from zero.
2. The organisers' `enterprise_server.py` starts unchanged, with a fresh, empty world.
3. The 15 events go through the agents **one every 5 seconds** (about 70 s in total). Each one appears on the page as it finishes.
4. Every tool call is recorded by the server in `runs/live/action_log.jsonl`. Use **Download action_log.jsonl** on the page, or take the file from that folder.

To run again, press Play (or restart the app and press Play). Everything in `runs/live/` is replaced.
Stop the app with **Ctrl+C**; this also stops the enterprise server.

### App options

| Option | Effect |
|---|---|
| `--refresh` | Gemini re-reads every message instead of using cached answers |
| `--port 9000` | Serve the page on another port (default 8080) |
| `--no-browser` | Don't open a browser tab automatically |

---

## The graded run (command line)

The submission's `action_log.jsonl` must come from the provided server. You can produce it with the app (it runs that server), or step by step:

```bash
# Terminal 1: the organisers' server (fresh world, fresh log)
python enterprise_server.py --data data --events data/events.json --out runs/final --port 8000

# Terminal 2: the agents, then the checks and the page
python run.py --live --out runs/final
python check.py runs/final
python ui/build_report.py runs/final
```

The server refuses to overwrite an existing log. Use a new `--out` folder, or pass `--overwrite` to the server.

**To submit,** take these from `runs/final/`:
- `action_log.jsonl`, the graded log written by the server
- `traces/`, one trace per event
- `agent_graph.mmd`, the agent workflow diagram generated from the code
- `checks.json`
- `control_tower.html`

Also include `runs/graph/ontology.mmd` (from `python graph.py --ontology`), the ontology diagram.

### Rehearsing without the server

```bash
python run.py --dry            # plans and dry-runs every event; nothing is sent or logged
python check.py runs/dry
python ui/build_report.py runs/dry
```

---

## How it works

The workflow is a **LangGraph** state graph (`run.py`). Each node is named `<agent>_<what it does>`, so the drawn graph (`runs/<run>/agent_graph.mmd`) reads at a glance and shows the coordinator and the three specialist agents.

```
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
```

| Node | Agent | What it does | LLM? |
|---|---|---|---|
| `coordinator_read_message` | Coordinator | Gemini splits and labels the message; code checks quotes and resolves names, dates and ids (`incident_triage.py`), giving ACT / ESCALATE / NO ACTION | Yes |
| `coordinator_find_affected` | Coordinator | Walks the knowledge graph for each ACT incident (`graph.py`) and picks the specialists | No |
| `planner_reschedule_builds` | Supply Chain Planner | Reschedules or transfers every affected production order to its earliest legal slot | No |
| `dispatcher_notify_and_loan` | Customer Care Dispatcher | One notice per dealer (each order with its cause and new date); loaners by priority | No |
| `inspector_hold_and_recall` | Quality Inspector | Holds the lot, stops sale of unsold vehicles, contacts owners of sold ones | No |
| `safety_check_plan` | Guardrail | Replays the whole plan on a private copy of `enterprise.py` | No |
| `reject_plan_escalate` | Guardrail | If any call would fail: drops the plan and escalates to a human | No |
| `send_to_server` | Coordinator | Sends the calls to the enterprise server (skipped in `--dry`) | No |
| `write_audit_summary` | Coordinator | Builds each agent's line of the summary from the trace (names, dates, counts); the agents present these on the dashboard | No |

The agents share one typed state, so incidents in a compound event use the same slots and loaner pools.

| Verdict | When | Tools called |
|---|---|---|
| **ACT** | A confirmed incident, fully resolved and checked against the data | Delay or outage: `mes.reschedule` / `mes.transfer`, `dealer.notify`, `crm.loaner_availability`, `crm.assign_loaner`. Quality: `quality.hold_lot`, `dealer.stop_sale`, `crm.notify_customer` |
| **ESCALATE** | A rumour, a vague report, or any name, date or record that doesn't check out | `escalate` only |
| **NO ACTION** | Noise: not about operations | none |

### Policy decisions the handbook leaves open

- **"next Friday"** means the Friday of next week (2027-03-19), so "next Monday" is 2027-03-15. This matches the shipment data.
- **Rescheduling:** orders go in order of original build date, then production order id. Each takes the earliest same-plant working day on or after the restart that has a free slot. If there is none, the order is transferred to the earliest day with a free slot, then the lowest plant id, at another plant that builds the model. Days disrupted in the same event are never used as destinations.
- **Compound events:** each dealer gets **one** `dealer.notify` listing all its affected orders and the cause of each. Loaners are ranked across all incidents together: Platinum, then Gold, then earlier promised date, then lower order id.
- **Escalation:** a report with no specific entity or date, an unconfirmed report, or any extracted value that doesn't match the data is escalated, never acted on.

---

## Files

| File | What it does |
|---|---|
| `app.py` | Local web app with the Play button (starts the enterprise server, runs the events on a 5 s beat) |
| `incident_triage.py` | Step 1: Gemini extraction plus deterministic quote, name, date and data checks, giving ACT / ESCALATE / NO ACTION |
| `graph.py` | Knowledge graph (networkx, about 17,000 nodes) and the traversals for delays, outages and quality holds, with paths |
| `agents.py` | The three specialist agents and the dry run against `enterprise.py` |
| `run.py` | LangGraph workflow, the live and dry runners, and a trace per event |
| `check.py` | Independent policy checker: replays each event's calls and re-derives the correct outcome from the raw CSVs |
| `ui/build_report.py`, `ui/template.html` | Control-tower dashboard (light by default, dark-mode toggle): run highlights with Play and Download, the agent briefing (each agent presents its part), ontology plus reasoning trace, event feed and action feed. Each action opens a pop-up with the actions and audit trail |
| `ui/make_sprites.py`, `ui/sprites/`, `ui/assets/` | Cuts the agent sprite sheet into transparent PNGs, using sheet column 5 for "working" (took part) and column 2 for "idle" (not needed), except the Customer Care Dispatcher, whose idle pose is column 1; the dashboard embeds them. `ui/assets/` holds the source sprite sheet and the colour palette |
| `enterprise.py`, `enterprise_server.py` | Provided by the organisers (unchanged) |
| `data/` | Provided company data, events and policy handbook |

Each script can also run on its own, which is useful for debugging one stage:

```bash
python incident_triage.py [E-01 ...] [--refresh]   # → runs/triage/triage.json
python graph.py [E-01 ...] [--ontology]            # → runs/graph/affected.json (+ ontology.mmd)
python agents.py [E-01 ...]                        # → runs/plan/plans.json (plans + dry runs)
```

---

## Configuration

- **API key:** `GEMINI_API_KEY` in `.env` (read automatically). A variable already set in your shell takes priority.
- **Model:** set `GEMINI_MODEL` in `.env` or your shell to use a different Gemini model.
- **Caching:** Gemini's reading of each message is cached in `runs/triage/llm_cache/`, so re-runs make no Gemini calls and give identical results. Use `--refresh` (or delete that folder) to call Gemini again, for example after changing the prompt. A fresh run makes at most 15 calls, one per event; the per-event summaries are built from the trace and use no LLM.

## Notes

- `runs/` and `.env` are git-ignored. Rebuild the outputs with the commands above.
- The challenge data and handbook belong to the organisers; check the challenge rules before making this repository public.
