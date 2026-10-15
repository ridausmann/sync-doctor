# Sync Doctor

A self-serve diagnostic for 1Now operators whose **Turo bookings aren't blocking dates** in 1Now.

## The problem

1Now's FAQ answer to *"Why aren't my Turo bookings blocking dates on 1Now?"* is "check your Turo
connection, then start a chat". So every case lands on a human. Meanwhile the operator is at risk
of a **double booking**: two renters, one car. That means a refund or relocation cost, a bad
review on Turo, and possible host penalties.

Sync Doctor runs the checks a support agent would run, in the same order:

- It **fixes** what is safe to fix automatically (a stale sync), and **verifies** the fix before claiming success.
- It tells the operator **exactly what to click** when the fix is on their side.
- It **escalates** with a complete, pre-filled ticket when it can't resolve the issue, so support never starts from zero.

## Rule order and outcomes

Rules run top to bottom. The **first** rule that explains the problem decides the outcome. Later
read-only rules still run and are attached to the ticket as *context*. Side effects (the re-sync)
only ever happen before an outcome is decided.

| # | Check | Fires when | Outcome |
|---|---|---|---|
| 1 | **Conflict scan** | A *booked* Turo trip overlaps a 1Now booking on the same car | 🔴 `ESCALATED` · **urgent** · no auto-fix · shows both booking IDs and the overlap |
| 2 | Connection | `disconnected` / `expired` | 🟠 `OPERATOR_ACTION`: connect / reconnect steps |
| 3 | Listing mapping | No `turo_listing_id`, or trips come from a different listing | 🟠 `OPERATOR_ACTION`: fix-the-mapping steps |
| 4 | Sync freshness | `last_sync` > 24h before NOW (or never) | **Auto-fix:** mock re-sync, then **verify** (see below) |
| 5 | Trip status | Reported date is only covered by a `cancelled` / `pending` / `completed` trip | 🟢 `NO_ISSUE_FOUND`: explains why that status doesn't block |
| 6 | Time zone | Account TZ ≠ vehicle TZ **and** the block is shifted ±1 day | 🟠 `OPERATOR_ACTION`: which time-zone setting to fix |
| 7 | Matching trip | Date given but no Turo trip on it | 🟢 `NO_ISSUE_FOUND`: "We can't find a Turo booking on that date" |
| 8 | Final | Dates actually blocked → 🟢 `NO_ISSUE_FOUND`. Otherwise → 🔴 `ESCALATED` (normal) with every check attached | |

**Auto-fix (rule 4) by `resync_behavior`:**

| Behaviour | What happens | Outcome |
|---|---|---|
| `works` | Re-sync rebuilds blocks, verify passes | 🟢 `AUTO_FIXED`, `last_sync` and blocks updated |
| `partial` | Re-sync *says* OK, but verify finds the date still open | 🔴 `ESCALATED`: "re-sync succeeded but date still not blocking" (never claims success) |
| `fails` | Re-sync errors | 🔴 `ESCALATED` with the error recorded under *Already tried* |
| `turo_down` | Turo unavailable | 🔴 `ESCALATED` (normal): "Turo-side outage, not your fault", avoid new 1Now bookings on the at-risk dates |

**Cross-cutting rules** (applied after the rule chain):

- **Repeat failures:** if the car already has ≥ 2 tickets in the last 14 days, an `OPERATOR_ACTION` or
  `AUTO_FIXED` result is still shown (and the fix still applied) but becomes `ESCALATED` with the
  `repeat_failure` flag, so a human looks for the deeper cause.
- **Duplicate request:** if an *open / in-progress* ticket exists for the same car with an overlapping
  date (or both undated), no new ticket is created. The operator sees the existing ticket number and its status.
- **Follow-up:** after `AUTO_FIXED`, `OPERATOR_ACTION` (and `NO_ISSUE_FOUND`) the operator gets *"Did this fix it?"*.
  **No** escalates: it appends to the run's ticket or an open matching ticket, or creates one.
- **Validation:** unknown operator, unknown vehicle, a vehicle from another fleet, malformed dates,
  end-before-start, end-without-start and ranges over 62 days all get a friendly message (HTTP 400). Nothing is changed.

## Run it

```bash
pip install -r requirements.txt && python app.py
```

Open **http://127.0.0.1:5000**. Run the tests with:

```bash
pytest
```

The app uses a fixed simulated clock, `NOW = 2026-10-15T12:00:00Z` (`diagnostics.NOW`). Every
diagnostic function takes `now=` so tests are deterministic.

**Data.** `data/seed/*.json` holds the pristine demo data. On first start the app copies it into
`data/*.json` (the live files, git-ignored), and every fix, ticket and run is written there. Tickets go to
`data/tickets.json` and the diagnosis log to `data/runs.json`. **Reset demo data** (top right)
restores the seed.

## Demo scenarios

The home page has a **Demo scenarios** panel. Click a row to fill the form, then press **Diagnose**.
Reset between runs if you want the original state back, because fixes and tickets persist.

| Vehicle | Operator | Date | Scenario | Expected |
|---|---|---|---|---|
| V-101 Tesla Model 3 | Bayside | any | Turo T-9001 overlaps 1Now B-501 (Oct 20–21) | 🔴 urgent escalation, new ticket |
| V-102 Toyota RAV4 | Bayside | any | Turo disconnected | 🟠 connect steps |
| V-103 Honda Civic | Bayside | any | Connection expired | 🟠 reconnect steps |
| V-104 Jeep Wrangler | Bayside | any | Linked to `turo_lst_5504`, trips on `turo_lst_5540` | 🟠 mapping steps |
| V-105 Mustang | Bayside | 2026-10-20 | Last sync 76h ago, re-sync `works` | 🟢 auto-fixed + verified |
| V-106 BMW X3 | Bayside | 2026-10-22 | Stale, re-sync `partial` | 🔴 "succeeded but still not blocking" |
| V-107 Kia Telluride | Bayside | 2026-10-17 | Stale, re-sync `fails` | 🔴 escalated with error |
| V-108 Hyundai Ioniq 5 | Bayside | 2026-10-24 | Stale, `turo_down` | 🔴 Turo outage, "not your fault" |
| V-109 Toyota Camry | Bayside | 2026-10-22 | Trip T-9082 cancelled | 🟢 explains cancelled |
| V-109 | Bayside | 2026-10-30 | Trip T-9084 pending | 🟢 explains pending |
| V-109 | Bayside | 2026-10-09 | Trip T-9085 completed | 🟢 explains completed |
| V-109 | Bayside | 2026-10-24 | No trip that day | 🟢 "can't find a Turo booking" |
| V-109 | Bayside | 2026-10-18 | Trip T-9081 blocked correctly | 🟢 everything in sync |
| V-109 | Bayside | 2026-10-25 | Healthy car, T-9083 not blocked, no known cause | 🔴 normal escalation |
| V-109 | Bayside | 2026-10-26 | Same gap, but open ticket SD-1004 exists | Shows SD-1004 + status, no new ticket |
| V-110 Chevrolet Tahoe | Bayside | 2026-10-21 | Auto-fix works, but SD-1002/SD-1003 in last 14 days (SD-1001 is older) | 🔴 repeat-failure escalation |
| V-201 Subaru Outback | Harbor Light | 2026-10-20 | Account `America/New_York`, car `America/Los_Angeles`; Oct 20–23 trip blocked as Oct 21–24 | 🟠 time-zone steps |
| All my vehicles | Bayside | none | Every car above | Grouped table, urgent first |
| V-201 while signed in as Bayside | Bayside | any | Not your vehicle | Validation message |

> **Note on fleet size:** the brief suggested 4 + 1 vehicles. Connection state and re-sync behaviour
> are per-vehicle properties, so 4 + 1 can't reach every scenario. Bayside has 10 cars and Harbor
> Light has 1. Date-only scenarios share the healthy Camry (V-109).

## Project layout

```
app.py              Flask routes only (form, diagnose → redirect → results, follow-up, tickets, reset)
diagnostics.py      Pure rule engine: no Flask, no file IO, injectable `now`
storage.py          JSON load/save (atomic writes), seed → live copy, reset
data/seed/*.json    Demo data: operators, vehicles, turo_trips, onenow_bookings (+ blocked_dates), tickets, runs
templates/          Jinja: index, results, _result (one vehicle card), tickets
static/             style.css, app.js (vehicle dropdown, presets, follow-up reveal)
tests/              test_diagnostics.py (engine), test_app.py (Flask smoke tests)
```

## Design decisions

- **Engine works on a plain dict.** `diagnostics.py` takes a `db` dict and returns plain dicts.
  Storage is a separate thin layer, so the engine is trivial to test and could sit behind a real API later.
- **Each rule is a small function** returning `(Check, Verdict | None)`. Order is one list (`RULES`), so
  re-ordering or adding a rule is a one-line change.
- **Record everything, act once.** After the deciding rule, read-only rules still run and are marked
  *context only*. Support sees, for example, that a disconnected car is *also* 15 days stale. The re-sync
  never runs after a cause is found.
- **Never claim a fix without verifying it.** Every re-sync is followed by a re-check of the blocked
  dates. That's what catches the `partial` case.
- **Conflicts are never auto-fixed.** Choosing which renter to move is a business decision with
  penalties attached, so the operator is told not to cancel anything and gets an urgent ticket.
- **Post/Redirect/Get.** Diagnosing redirects to `/results/<batch>`, so a browser refresh can't re-run
  an auto-fix or create tickets twice. Follow-up answers are idempotent.
- **Duplicate detection uses overlapping dates**, not exact matches, because operators rarely type the same date twice.
- **Tone.** Messages say what *we* found and did ("isn't connected", "Turo's side") and never "you
  forgot". Every operator step names the exact setting path.
- **Follow-up on `NO_ISSUE_FOUND` too.** This goes slightly beyond the brief. If the operator disagrees with
  "nothing's wrong", they still have a one-click path to a human.

## Known limitations

- Single-user prototype: JSON files, no locking, no auth (the operator dropdown simulates login).
- 1Now/Turo setting paths in the steps (e.g. *Settings → Integrations → Turo*) are illustrative.
- Trips are matched to vehicles by a `vehicle_id` on the mock trip. A real integration would match by listing/VIN.
- The time-zone rule only looks for ±1 day shifts on trips that have a block. Larger drifts fall through to escalation.
- Dates are calendar days in the vehicle's local time. Buffers between trips (cleaning time) aren't modelled.
- Expected response times are fixed strings, not tied to support hours or SLAs.
- The ticket list page is a read-only view. There's no way to change ticket status in the UI.

## What I would build next

1. **Real Turo / 1Now API integration.** Replace `mock_resync` and the JSON store with the real calendar
   sync job and booking APIs, behind the same `db`-shaped interface.
2. **Webhook-based sync health monitoring.** Track every sync per vehicle (latency, errors, token expiry)
   so rules 2 and 4 read live health instead of a single `last_sync` timestamp.
3. **Proactive alerts before double bookings.** Run the conflict scan and rules 2–4 on a schedule and
   whenever a 1Now booking is about to be accepted. Warn or block before the second renter books, not after.
4. **Support dashboard for tickets.** Queue sorted by priority and SLA, with the full check log, one-click
   re-run, status changes, and pattern views (e.g. "12 expired connections this week").
5. Notify the operator (email/SMS) when a Turo outage ends or a ticket changes status.
