"""Sync Doctor diagnostic engine.

Pure Python: no Flask and no file IO. Everything works on a plain `db` dict (see
storage.py for its shape) and an injectable `now`, so tests are deterministic.

Public entry points:
    run_diagnosis(db, operator_id, vehicle_id, start, end, description, now) -> list[dict]
    handle_followup(db, run_id, fixed, note, now) -> dict
    validate_request(db, operator_id, vehicle_id, start, end) -> Request

Rules run in a fixed order. The first rule that explains the problem decides the
outcome; later read-only rules still run and are recorded as "context only" so a
support ticket shows the full picture. Side effects (the mock re-sync) only ever
happen before an outcome has been decided.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)

STALE_AFTER = timedelta(hours=24)
REPEAT_WINDOW = timedelta(days=14)
REPEAT_THRESHOLD = 2          # this many recent tickets => escalate instead of self-serve
MAX_RANGE_DAYS = 62
OPEN_TICKET_STATUSES = {"open", "in_progress"}
BLOCKING_STATUSES = {"booked"}  # only confirmed trips block dates in 1Now

# Outcomes
AUTO_FIXED = "AUTO_FIXED"
OPERATOR_ACTION = "OPERATOR_ACTION"
NO_ISSUE_FOUND = "NO_ISSUE_FOUND"
ESCALATED = "ESCALATED"

# Display order for multi-vehicle results (urgent escalations are pulled to the very top).
OUTCOME_ORDER = {ESCALATED: 0, OPERATOR_ACTION: 1, AUTO_FIXED: 2, NO_ISSUE_FOUND: 3}
RESPONSE_TIMES = {"urgent": "within 1 hour", "normal": "within 1 business day"}

# Why a non-booked Turo trip status intentionally does not block dates.
STATUS_WHY = {
    "cancelled": "Cancelled trips free the car up again, so 1Now removes their block.",
    "pending": "Pending means the request hasn't been accepted yet. 1Now blocks dates once "
               "the trip is booked, so a request you decline never leaves the car blocked.",
    "completed": "Completed trips are already over, so there's nothing left to block.",
}

SETTINGS_TURO = "Settings → Integrations → Turo"


class ValidationError(ValueError):
    """Raised for bad input. The message is safe to show to the operator as-is."""


# --------------------------------------------------------------------------- helpers

def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _d(value: str) -> date:
    return date.fromisoformat(value)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def fmt_day(d: date) -> str:
    return f"{d:%b} {d.day}"


def fmt_range(start: date, end: date) -> str:
    return fmt_day(start) if start == end else f"{fmt_day(start)} – {fmt_day(end)}"


def fmt_days(days: list[date]) -> str:
    """Collapse a list of days into readable ranges: 'Oct 20 – Oct 22, Oct 25'."""
    days = sorted(set(days))
    if not days:
        return ""
    parts, run_start, prev = [], days[0], days[0]
    for d in days[1:]:
        if d != prev + timedelta(days=1):
            parts.append(fmt_range(run_start, prev))
            run_start = d
        prev = d
    parts.append(fmt_range(run_start, prev))
    return ", ".join(parts)


def trip_days(trip: dict) -> tuple[date, date]:
    """A trip blocks every calendar day it touches, in the vehicle's local time."""
    return _dt(trip["start"]).date(), _dt(trip["end"]).date()


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _by_id(items: list[dict], item_id) -> dict | None:
    return next((i for i in items if i["id"] == item_id), None)


# --------------------------------------------------------------------------- data types

@dataclass
class Request:
    operator: dict
    vehicles: list[dict]
    start: date | None
    end: date | None


@dataclass
class Check:
    rule: int
    key: str
    name: str
    status: str             # "pass" | "fail" | "fixed" | "n/a" | "skipped"
    detail: str
    context_only: bool = False  # ran after the outcome was already decided


@dataclass
class Verdict:
    outcome: str
    title: str
    explanation: str
    steps: list[str]
    priority: str = "normal"
    reason: str = ""
    conflicts: list[dict] = field(default_factory=list)


@dataclass
class Ctx:
    """Everything a rule needs to look at for one vehicle."""
    db: dict
    operator: dict
    vehicle: dict
    start: date | None
    end: date | None
    now: datetime
    attempted: list[str] = field(default_factory=list)  # fixes we tried, for the ticket

    @property
    def name(self) -> str:
        return self.vehicle["name"]

    @property
    def trips(self) -> list[dict]:
        return [t for t in self.db["turo_trips"] if t["vehicle_id"] == self.vehicle["id"]]

    @property
    def booked_trips(self) -> list[dict]:
        return [t for t in self.trips if t["status"] in BLOCKING_STATUSES]

    @property
    def blocks(self) -> list[dict]:
        return self.db["blocked_dates"].setdefault(self.vehicle["id"], [])

    @property
    def bookings(self) -> list[dict]:
        return [b for b in self.db["onenow_bookings"] if b["vehicle_id"] == self.vehicle["id"]]

    @property
    def has_dates(self) -> bool:
        return self.start is not None

    @property
    def dates_label(self) -> str:
        return fmt_range(self.start, self.end) if self.has_dates else "your upcoming dates"

    def reported_days(self) -> list[date]:
        return _days(self.start, self.end) if self.has_dates else []

    def trips_in_range(self) -> list[dict]:
        def overlaps(t):
            s, e = trip_days(t)
            return s <= self.end and self.start <= e
        return [t for t in self.trips if overlaps(t)] if self.has_dates else []

    def is_blocked(self, day: date) -> bool:
        return any(_d(b["start"]) <= day <= _d(b["end"]) for b in self.blocks)

    def unblocked_booked_days(self) -> list[date]:
        """Days that a booked trip needs blocked but which are open in 1Now.
        Limited to the reported range when the operator gave one."""
        wanted = set(self.reported_days()) if self.has_dates else None
        missing = []
        for trip in self.booked_trips:
            for day in _days(*trip_days(trip)):
                if (wanted is None or day in wanted) and not self.is_blocked(day):
                    missing.append(day)
        return sorted(set(missing))


# A rule takes the context and whether it may still decide the outcome / act,
# and returns its check result plus an optional verdict.
Rule = Callable[[Ctx, bool], "tuple[Check, Verdict | None]"]


# --------------------------------------------------------------------------- rules

def rule_conflict_scan(ctx: Ctx, active: bool):
    """1. Is there already a double booking (booked Turo trip overlapping a 1Now booking)?"""
    conflicts = []
    for trip in ctx.booked_trips:
        for booking in ctx.bookings:
            ts, te, bs, be = _dt(trip["start"]), _dt(trip["end"]), _dt(booking["start"]), _dt(booking["end"])
            if ts < be and bs < te:
                conflicts.append({
                    "turo_trip_id": trip["id"],
                    "onenow_booking_id": booking["id"],
                    "turo_dates": fmt_range(*trip_days(trip)),
                    "onenow_dates": fmt_range(bs.date(), be.date()),
                    "overlap": fmt_range(max(ts, bs).date(), min(te, be).date()),
                })
    if not conflicts:
        return Check(1, "conflict_scan", "Existing double bookings", "pass",
                     "No booked Turo trip overlaps a 1Now booking."), None

    pairs = "; ".join(f"Turo trip {c['turo_trip_id']} and 1Now booking {c['onenow_booking_id']} "
                      f"overlap on {c['overlap']}" for c in conflicts)
    check = Check(1, "conflict_scan", "Existing double bookings", "fail", pairs + ".")
    verdict = Verdict(
        outcome=ESCALATED, priority="urgent", reason="existing double booking", conflicts=conflicts,
        title=f"{ctx.name} has two renters on the same dates",
        explanation=f"{pairs}. We haven't changed either booking. A specialist is taking this "
                    f"now to help you sort it out with both renters.",
        steps=[
            f"Please don't accept any more bookings for {ctx.name} on these dates.",
            "Leave both bookings as they are for now. Cancelling one yourself can bring "
            "penalties that support may be able to avoid.",
            f"A specialist will contact you {RESPONSE_TIMES['urgent']} with options, "
            "such as moving one renter to another car.",
        ],
    )
    return check, verdict


def rule_connection(ctx: Ctx, active: bool):
    """2. Is the Turo calendar connection healthy?"""
    status = ctx.vehicle["turo_connection"]["status"]
    name = "Turo connection"
    if status == "connected":
        return Check(2, "connection", name, "pass", "Turo calendar is connected."), None

    if status == "expired":
        check = Check(2, "connection", name, "fail", "Turo connection has expired; Turo needs access renewed.")
        verdict = Verdict(
            outcome=OPERATOR_ACTION, reason="turo connection expired",
            title="Your Turo connection needs to be renewed",
            explanation=f"Turo asks for access to be renewed from time to time. Until it is, new "
                        f"Turo trips for {ctx.name} can't reach 1Now, so they don't block dates.",
            steps=[
                f"In 1Now, open {SETTINGS_TURO}.",
                "Click “Reconnect” and sign in with the Turo account that owns this listing.",
                "Approve calendar access when Turo asks.",
                "Click “Sync now”, then run Sync Doctor again to confirm the dates are blocked.",
            ],
        )
        return check, verdict

    check = Check(2, "connection", name, "fail", "Turo calendar is not connected.")
    verdict = Verdict(
        outcome=OPERATOR_ACTION, reason="turo not connected",
        title="Connect your Turo calendar",
        explanation=f"{ctx.name} isn't connected to a Turo calendar, so 1Now can't see its Turo "
                    f"trips and can't block those dates.",
        steps=[
            f"In 1Now, open {SETTINGS_TURO}.",
            "Click “Connect Turo calendar” and sign in with the Turo account that owns this listing.",
            "Approve calendar access when Turo asks.",
            f"Check that {ctx.name} shows as “Connected”, then run Sync Doctor again. "
            "Trips usually appear within a few minutes.",
        ],
    )
    return check, verdict


def rule_mapping(ctx: Ctx, active: bool):
    """3. Is the vehicle linked to the Turo listing its trips actually come from?"""
    listing = ctx.vehicle.get("turo_listing_id")
    trip_listings = sorted({t["turo_listing_id"] for t in ctx.trips if t["turo_listing_id"] != listing})
    name = "Turo listing mapping"
    fix_steps = [
        f"In 1Now, open {SETTINGS_TURO} → Listing mapping.",
        f"Find {ctx.name} (plate {ctx.vehicle['plate']}).",
        (f"Choose Turo listing {trip_listings[0]}, which is where its Turo trips come from."
         if trip_listings else "Choose the Turo listing for this car."),
        "Save, then run Sync Doctor again.",
    ]
    if not listing:
        check = Check(3, "mapping", name, "fail", "Vehicle isn't linked to any Turo listing.")
        return check, Verdict(
            outcome=OPERATOR_ACTION, reason="vehicle not mapped to turo listing",
            title="Link this car to its Turo listing",
            explanation=f"{ctx.name} isn't linked to a Turo listing yet, so Turo trips can't be "
                        f"matched to it.",
            steps=fix_steps,
        )
    if trip_listings:
        check = Check(3, "mapping", name, "fail",
                      f"Vehicle is linked to {listing}, but its Turo trips use {', '.join(trip_listings)}.")
        return check, Verdict(
            outcome=OPERATOR_ACTION, reason="vehicle mapped to wrong turo listing",
            title="This car is linked to a different Turo listing",
            explanation=f"{ctx.name} is linked to Turo listing {listing}, but its Turo trips come "
                        f"from listing {', '.join(trip_listings)}. 1Now only blocks dates for "
                        f"trips on the linked listing.",
            steps=fix_steps,
        )
    return Check(3, "mapping", name, "pass", f"Linked to {listing}; all trips match."), None


def mock_resync(db: dict, vehicle: dict, now: datetime) -> dict:
    """Pretend to pull the Turo calendar again. `resync_behavior` decides what happens.

    Returns {"ok": bool, "outage": bool, "error": str | None}.
    """
    behavior = vehicle["resync_behavior"]
    if behavior == "turo_down":
        return {"ok": False, "outage": True,
                "error": "Turo calendar service returned 503 Service Unavailable."}
    if behavior == "fails":
        return {"ok": False, "outage": False,
                "error": "Sync job timed out after 30s while reading the listing calendar."}

    vehicle["turo_connection"]["last_sync"] = _iso(now)
    if behavior == "works":
        # Rebuild blocks from every booked trip on the linked listing.
        db["blocked_dates"][vehicle["id"]] = [
            {"start": s.isoformat(), "end": e.isoformat(), "source_trip_id": t["id"]}
            for t in db["turo_trips"]
            if t["vehicle_id"] == vehicle["id"] and t["status"] in BLOCKING_STATUSES
            and t["turo_listing_id"] == vehicle["turo_listing_id"]
            for s, e in [trip_days(t)]
        ]
    # "partial": reports success and updates last_sync, but no blocks change.
    return {"ok": True, "outage": False, "error": None}


def rule_staleness(ctx: Ctx, active: bool):
    """4. Has the calendar synced recently? If not, re-sync and verify (auto-fix)."""
    name = "Sync freshness"
    last = ctx.vehicle["turo_connection"].get("last_sync")
    age = (ctx.now - _dt(last)) if last else None
    if age is not None and age <= STALE_AFTER:
        return Check(4, "staleness", name, "pass", f"Last sync {int(age.total_seconds() // 3600)}h ago."), None

    ago = f"{int(age.total_seconds() // 3600)} hours ago" if age else "never"
    synced = f"last synced {ago}" if age else "has never synced"
    if not active:
        return Check(4, "staleness", name, "fail",
                     f"Last sync {ago}. Auto re-sync not attempted (cause already found)."), None

    # --- Auto-fix: re-sync, then VERIFY. We never claim success without verifying.
    result = mock_resync(ctx.db, ctx.vehicle, ctx.now)
    at_risk = fmt_days(ctx.unblocked_booked_days()) or ctx.dates_label

    if result["outage"]:
        ctx.attempted.append(f"Re-sync with Turo: Turo unavailable ({result['error']})")
        check = Check(4, "staleness", name, "fail", f"Last sync {ago}. Re-sync blocked by a Turo outage.")
        return check, Verdict(
            outcome=ESCALATED, reason="turo-side outage",
            title="Turo is having trouble right now. This isn't anything you did",
            explanation=f"{ctx.name} {synced}. We tried to sync again, but Turo's calendar "
                        f"service isn't responding. This is a problem on Turo's side, and your settings are fine.",
            steps=[
                f"Until this is fixed, please don't accept new 1Now bookings for {ctx.name} on {at_risk}.",
                "Your existing bookings are safe. Nothing was changed.",
                "We've opened a ticket. We'll retry the sync and let you know once Turo is back.",
            ],
        )
    if not result["ok"]:
        ctx.attempted.append(f"Re-sync with Turo: failed ({result['error']})")
        check = Check(4, "staleness", name, "fail", f"Last sync {ago}. Re-sync failed: {result['error']}")
        return check, Verdict(
            outcome=ESCALATED, reason="re-sync failed",
            title="We couldn't refresh your Turo calendar",
            explanation=f"{ctx.name} {synced}, so recent Turo trips may not be blocking "
                        f"dates. We tried to sync again but it didn't go through, so we've passed "
                        f"this to our team with the error details.",
            steps=[
                f"Until this is fixed, please don't accept new 1Now bookings for {ctx.name} on {at_risk}.",
                "No action is needed in your settings.",
            ],
        )

    still_missing = ctx.unblocked_booked_days()
    if still_missing:
        ctx.attempted.append("Re-sync with Turo: reported success")
        ctx.attempted.append(f"Verify: still not blocked on {fmt_days(still_missing)}")
        check = Check(4, "staleness", name, "fail",
                      f"Last sync was {ago}. Re-sync succeeded but date still not blocking "
                      f"({fmt_days(still_missing)}).")
        return check, Verdict(
            outcome=ESCALATED, reason="re-sync succeeded but date still not blocking",
            title="The sync ran, but your dates still aren't blocked",
            explanation=f"We refreshed the Turo calendar for {ctx.name} and then checked again. "
                        f"These dates still aren't blocked: {fmt_days(still_missing)}. So we're "
                        f"handing this to a specialist instead of marking it fixed.",
            steps=[
                f"Please don't accept new 1Now bookings for {ctx.name} on {fmt_days(still_missing)} for now.",
                "No action is needed in your settings.",
            ],
        )

    ctx.attempted.append("Re-sync with Turo: succeeded")
    ctx.attempted.append("Verify: all booked Turo trips are now blocking dates")
    blocked = ", ".join(fmt_range(_d(b["start"]), _d(b["end"])) for b in ctx.blocks) or "none"
    check = Check(4, "staleness", name, "fixed",
                  f"Last sync was {ago}. Re-synced and verified. Blocked now: {blocked}.")
    return check, Verdict(
        outcome=AUTO_FIXED, reason="stale sync",
        title="Fixed: your Turo trips are blocking dates again",
        explanation=f"{ctx.name} {synced}, so recent trips hadn't reached 1Now. "
                    f"We ran a fresh sync and checked the calendar: all booked Turo trips are "
                    f"now blocked.",
        steps=[
            "There's nothing else you need to do.",
            f"If you'd like to check, {ctx.name}'s calendar now shows these blocked dates: {blocked}.",
        ],
    )


def rule_trip_status(ctx: Ctx, active: bool):
    """5. Is the reported date covered only by trips whose status doesn't block dates?"""
    name = "Trip status on reported date"
    if not ctx.has_dates:
        return Check(5, "trip_status", name, "n/a", "No date given."), None
    in_range = ctx.trips_in_range()
    if any(t["status"] in BLOCKING_STATUSES for t in in_range) or not in_range:
        return Check(5, "trip_status", name, "pass",
                     "A booked trip covers the date." if in_range else "No Turo trip on that date."), None

    trip = in_range[0]
    status = trip["status"]
    check = Check(5, "trip_status", name, "fail",
                  f"Trip {trip['id']} on {fmt_range(*trip_days(trip))} is {status}; {status} trips don't block dates.")
    return check, Verdict(
        outcome=NO_ISSUE_FOUND, reason=f"trip is {status}",
        title=f"That Turo trip is {status}, so it doesn't block dates",
        explanation=f"Turo trip {trip['id']} for {ctx.name} ({fmt_range(*trip_days(trip))}) is "
                    f"{status}. {STATUS_WHY.get(status, '')} So 1Now is behaving as designed.",
        steps=[
            f"If Turo shows this trip as booked rather than {status}, click “No” below and we'll look into it.",
            "Otherwise, these dates are open in 1Now and you can rent the car out.",
        ],
    )


def rule_timezone(ctx: Ctx, active: bool):
    """6. Is a booked trip's block shifted by a day because of a time zone mismatch?"""
    name = "Time zone alignment"
    trips = [t for t in ctx.trips_in_range() if t["status"] in BLOCKING_STATUSES] if ctx.has_dates else ctx.booked_trips
    shifted = []
    for trip in trips:
        block = next((b for b in ctx.blocks if b.get("source_trip_id") == trip["id"]), None)
        if not block:
            continue
        (ts, te), (bs, be) = trip_days(trip), (_d(block["start"]), _d(block["end"]))
        ds, de = (bs - ts).days, (be - te).days
        if (ds or de) and abs(ds) <= 1 and abs(de) <= 1:
            shifted.append((trip, ts, te, bs, be))

    op_tz, car_tz = ctx.operator["timezone"], ctx.vehicle["timezone"]
    if not shifted:
        return Check(6, "timezone", name, "pass", "Blocked dates line up with trip dates."), None
    if op_tz == car_tz:
        return Check(6, "timezone", name, "fail",
                     "A block is off by a day, but the time zones match. Not a time zone issue."), None

    trip, ts, te, bs, be = shifted[0]
    open_days = [d for d in _days(ts, te) if not ctx.is_blocked(d)]
    check = Check(6, "timezone", name, "fail",
                  f"Account time zone {op_tz} ≠ vehicle time zone {car_tz}; trip {trip['id']} "
                  f"({fmt_range(ts, te)}) is blocked as {fmt_range(bs, be)}.")
    return check, Verdict(
        outcome=OPERATOR_ACTION, reason="timezone mismatch",
        title="A time zone mismatch is shifting your blocked dates by a day",
        explanation=f"Your 1Now account is set to {op_tz}, but {ctx.name} is set to {car_tz}. "
                    f"Because of that, Turo trip {trip['id']} ({fmt_range(ts, te)}) is blocked as "
                    f"{fmt_range(bs, be)}"
                    + (f", which leaves {fmt_days(open_days)} open." if open_days else "."),
        steps=[
            "Decide which time zone is right. Usually it's where the car is picked up.",
            f"If the car's time zone ({car_tz}) is right, open Settings → Account → Time zone and change it to {car_tz}.",
            f"If your account time zone ({op_tz}) is right, open Vehicles → {ctx.name} → Details and correct the time zone there.",
            "Run Sync Doctor again to check the dates line up."
            + (f" Until then, please don't accept 1Now bookings on {fmt_days(open_days)}." if open_days else ""),
        ],
    )


def rule_matching_trip(ctx: Ctx, active: bool):
    """7. Is there any Turo trip on the reported date at all?"""
    name = "Turo trip on reported date"
    if not ctx.has_dates:
        return Check(7, "matching_trip", name, "n/a", "No date given."), None
    if ctx.trips_in_range():
        return Check(7, "matching_trip", name, "pass", "Found a Turo trip on that date."), None
    check = Check(7, "matching_trip", name, "fail", f"No Turo trip for this car on {ctx.dates_label}.")
    return check, Verdict(
        outcome=NO_ISSUE_FOUND, reason="no turo trip on date",
        title="We can't find a Turo booking on that date",
        explanation=f"We looked at every Turo trip for {ctx.name} (listing "
                    f"{ctx.vehicle.get('turo_listing_id')}) and none of them fall on {ctx.dates_label}.",
        steps=[
            "Double-check the date. Trips that start or end near midnight can land on the next or previous day.",
            f"In Turo, check the trip is on this car's listing ({ctx.vehicle.get('turo_listing_id')}).",
            "If you can see the trip in Turo, click “No” below and add the trip ID. We'll take it from there.",
        ],
    )


def rule_final(ctx: Ctx, active: bool):
    """8. Nothing above explained it: either it's actually fine, or escalate."""
    name = "Dates blocked in 1Now"
    if not active:
        return Check(8, "final", name, "skipped", "Not needed: an earlier check found the cause."), None
    missing = ctx.unblocked_booked_days()
    if not missing:
        detail = (f"{ctx.dates_label} is blocked by a booked Turo trip." if ctx.has_dates
                  else f"All {len(ctx.booked_trips)} booked Turo trip(s) are blocking dates.")
        return Check(8, "final", name, "pass", detail), Verdict(
            outcome=NO_ISSUE_FOUND, reason="dates already blocked",
            title="Everything looks in sync",
            explanation=f"The connection, listing link and sync are all healthy, and {detail[0].lower() + detail[1:]}",
            steps=[
                "Refresh your 1Now calendar. It may have been showing an older view.",
                "If it still looks wrong, click “No” below and we'll take a closer look.",
            ],
        )
    check = Check(8, "final", name, "fail", f"Booked Turo trip not blocking {fmt_days(missing)}; no known cause.")
    return check, Verdict(
        outcome=ESCALATED, reason="unexplained sync gap",
        title="We've passed this to a specialist",
        explanation=f"Your connection, listing link and sync timing all look fine, but these "
                    f"dates for {ctx.name} still aren't blocked: {fmt_days(missing)}. We couldn't "
                    f"pin down why, so a specialist will take a look with everything we checked.",
        steps=[f"Please don't accept new 1Now bookings for {ctx.name} on {fmt_days(missing)} for now."],
    )


RULES: list[Rule] = [
    rule_conflict_scan, rule_connection, rule_mapping, rule_staleness,
    rule_trip_status, rule_timezone, rule_matching_trip, rule_final,
]


# --------------------------------------------------------------------------- validation

def _parse_date(value: str | None, label: str) -> date | None:
    if value is None or not str(value).strip():
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise ValidationError(f"{label} “{value}” isn't a date we recognise. Please use YYYY-MM-DD, e.g. 2026-10-20.")


def validate_request(db: dict, operator_id: str, vehicle_id: str,
                     start: str | None = None, end: str | None = None) -> Request:
    operator = _by_id(db["operators"], operator_id)
    if not operator:
        raise ValidationError("We couldn't find that operator account. Please choose one from the list.")
    if not vehicle_id:
        raise ValidationError("Please choose a vehicle, or “All my vehicles”.")
    if vehicle_id == "all":
        vehicles = [v for v in db["vehicles"] if v["operator_id"] == operator_id]
        if not vehicles:
            raise ValidationError("There aren't any vehicles on this account yet.")
    else:
        vehicle = _by_id(db["vehicles"], vehicle_id)
        if not vehicle:
            raise ValidationError(f"We couldn't find a vehicle with ID “{vehicle_id}”. Please pick one from the list.")
        if vehicle["operator_id"] != operator_id:
            raise ValidationError(f"Vehicle {vehicle_id} isn't part of {operator['name']}'s fleet. "
                                  "Please pick one of your own vehicles.")
        vehicles = [vehicle]

    start_d, end_d = _parse_date(start, "Start date"), _parse_date(end, "End date")
    if end_d and not start_d:
        raise ValidationError("Please add a start date as well as the end date.")
    if start_d and not end_d:
        end_d = start_d
    if start_d and end_d < start_d:
        raise ValidationError("The end date is before the start date. Please check the dates and try again.")
    if start_d and (end_d - start_d).days + 1 > MAX_RANGE_DAYS:
        raise ValidationError(f"Please choose a range of {MAX_RANGE_DAYS} days or fewer.")
    return Request(operator, vehicles, start_d, end_d)


# --------------------------------------------------------------------------- tickets

def _ranges_overlap(t: dict, start: date | None, end: date | None) -> bool:
    """Same 'date' for duplicate detection: both undated, or overlapping ranges."""
    if t.get("reported_start") is None or start is None:
        return t.get("reported_start") is None and start is None
    return _d(t["reported_start"]) <= end and start <= _d(t["reported_end"])


def find_open_ticket(db: dict, vehicle_id: str, start: date | None, end: date | None) -> dict | None:
    return next((t for t in db["tickets"]
                 if t["vehicle_id"] == vehicle_id and t["status"] in OPEN_TICKET_STATUSES
                 and _ranges_overlap(t, start, end)), None)


def recent_ticket_count(db: dict, vehicle_id: str, now: datetime) -> int:
    since = now - REPEAT_WINDOW
    return sum(1 for t in db["tickets"] if t["vehicle_id"] == vehicle_id and _dt(t["created_at"]) >= since)


def _next_ticket_id(db: dict) -> str:
    nums = [int(t["id"].split("-")[1]) for t in db["tickets"]] or [1000]
    return f"SD-{max(nums) + 1}"


def create_ticket(db: dict, *, operator: dict, vehicle: dict, start: date | None, end: date | None,
                  priority: str, reason: str, summary: str, checks: list[dict], attempted: list[str],
                  description: str, flags: list[str], conflicts: list[dict], now: datetime,
                  run_id: str | None) -> dict:
    ticket = {
        "id": _next_ticket_id(db), "status": "open", "priority": priority,
        "operator_id": operator["id"], "operator_name": operator["name"],
        "vehicle_id": vehicle["id"], "vehicle_name": vehicle["name"], "plate": vehicle["plate"],
        "reported_start": start.isoformat() if start else None,
        "reported_end": end.isoformat() if end else None,
        "reason": reason, "summary": summary, "operator_description": description,
        "checks": checks, "attempted": attempted, "flags": flags, "conflicts": conflicts,
        "expected_response": RESPONSE_TIMES[priority],
        "created_at": _iso(now), "updated_at": _iso(now), "run_id": run_id,
        "history": [{"at": _iso(now), "event": "created", "note": "Created by Sync Doctor."}],
    }
    db["tickets"].append(ticket)
    return ticket


def append_to_ticket(ticket: dict, note: str, now: datetime, event: str = "note") -> None:
    ticket["history"].append({"at": _iso(now), "event": event, "note": note})
    ticket["updated_at"] = _iso(now)


# --------------------------------------------------------------------------- runner

def _new_run_id(db: dict) -> str:
    return f"RUN-{len(db['runs']) + 1:04d}"


def diagnose_vehicle(db: dict, operator: dict, vehicle: dict, start: date | None, end: date | None,
                     description: str = "", now: datetime = NOW, batch_id: str | None = None) -> dict:
    """Run every rule for one vehicle, apply cross-cutting rules, record the run."""
    ctx = Ctx(db, operator, vehicle, start, end, now)
    checks: list[Check] = []
    verdict: Verdict | None = None
    for rule in RULES:
        check, v = rule(ctx, verdict is None)
        check.context_only = verdict is not None
        checks.append(check)
        if verdict is None and v is not None:
            verdict = v
    assert verdict is not None, "rule_final always decides when active"

    result = {
        "id": _new_run_id(db), "batch_id": batch_id, "created_at": _iso(now),
        "operator_id": operator["id"], "vehicle_id": vehicle["id"], "vehicle_name": vehicle["name"],
        "plate": vehicle["plate"],
        "reported_start": start.isoformat() if start else None,
        "reported_end": end.isoformat() if end else None,
        "dates_label": ctx.dates_label if start else "",
        "description": description,
        **asdict(verdict),
        "original_outcome": None, "flags": [],
        "checks": [asdict(c) for c in checks],
        "attempted": ctx.attempted,
        "ticket_id": None, "duplicate": False, "followup": None,
    }

    # Cross-cutting: repeat failures. Self-serve outcomes get a human anyway.
    if verdict.outcome in (OPERATOR_ACTION, AUTO_FIXED) and verdict.priority != "urgent":
        recent = recent_ticket_count(db, vehicle["id"], now)
        if recent >= REPEAT_THRESHOLD:
            result["flags"].append("repeat_failure")
            result["original_outcome"] = verdict.outcome
            result["outcome"] = ESCALATED
            result["reason"] = f"repeat failures ({recent} tickets in 14 days); originally {verdict.outcome}"
            result["title"] = ("Fixed for now, but this keeps happening, so a specialist will dig deeper"
                               if verdict.outcome == AUTO_FIXED
                               else f"{verdict.title}. A specialist will also look at why this keeps happening")
            result["explanation"] += (f" This is the {_ordinal(recent + 1)} sync problem with {vehicle['name']} "
                                      f"in two weeks, so we've also asked a specialist to look for a deeper cause.")
            result["steps"] = result["steps"] + [f"A specialist will also follow up {RESPONSE_TIMES['normal']}."]

    if result["outcome"] == ESCALATED:
        existing = find_open_ticket(db, vehicle["id"], start, end)
        if existing:
            # Duplicate request: don't open a second ticket for the same car and dates.
            result["ticket_id"], result["duplicate"] = existing["id"], True
        else:
            ticket = create_ticket(
                db, operator=operator, vehicle=vehicle, start=start, end=end,
                priority=result["priority"], reason=result["reason"],
                summary=f"{vehicle['id']} · {result['title']}",
                checks=result["checks"], attempted=result["attempted"], description=description,
                flags=result["flags"], conflicts=result["conflicts"], now=now, run_id=result["id"],
            )
            result["ticket_id"] = ticket["id"]

    db["runs"].append(result)
    return result


def sort_results(results: list[dict]) -> list[dict]:
    """Urgent first, then by outcome severity, then by vehicle name."""
    return sorted(results, key=lambda r: (r["priority"] != "urgent", OUTCOME_ORDER[r["outcome"]], r["vehicle_name"]))


def run_diagnosis(db: dict, operator_id: str, vehicle_id: str, start: str | None = None,
                  end: str | None = None, description: str = "", now: datetime = NOW) -> list[dict]:
    """Validate the request and diagnose one vehicle or the whole fleet ("all").
    Mutates db (re-syncs, tickets, run log). Raises ValidationError on bad input."""
    req = validate_request(db, operator_id, vehicle_id, start, end)
    batch_id = f"B{len(db['runs']) + 1:04d}"
    results = [diagnose_vehicle(db, req.operator, v, req.start, req.end, (description or "").strip(), now, batch_id)
               for v in req.vehicles]
    return sort_results(results)


def handle_followup(db: dict, run_id: str, fixed: bool, note: str = "", now: datetime = NOW) -> dict:
    """Operator answers “Did this fix it?”. "No" escalates, appending to an existing
    ticket when there is one (the run's own, or an open one for the same car/dates)."""
    run = _by_id(db["runs"], run_id)
    if not run:
        raise ValidationError("We couldn't find that diagnosis. Please run Sync Doctor again.")
    if run["followup"]:
        return run["followup"]  # already answered; idempotent on double-click / refresh

    note = (note or "").strip()
    if fixed:
        run["followup"] = {"answer": "yes", "at": _iso(now), "ticket_id": None, "appended": False}
        return run["followup"]

    start = _d(run["reported_start"]) if run["reported_start"] else None
    end = _d(run["reported_end"]) if run["reported_end"] else None
    text = f"Operator says the suggested fix didn't work (outcome was {run['outcome']})." + (f" Note: {note}" if note else "")
    ticket = (_by_id(db["tickets"], run["ticket_id"]) if run["ticket_id"] else None) \
        or find_open_ticket(db, run["vehicle_id"], start, end)
    if ticket:
        append_to_ticket(ticket, text, now, event="operator_followup")
        appended = True
    else:
        operator = _by_id(db["operators"], run["operator_id"])
        vehicle = _by_id(db["vehicles"], run["vehicle_id"])
        ticket = create_ticket(
            db, operator=operator, vehicle=vehicle, start=start, end=end, priority="normal",
            reason="operator reports fix did not work",
            summary=f"{vehicle['id']} · {run['title']}. Operator says it's still not fixed.",
            checks=run["checks"], attempted=run["attempted"] + [f"Operator followed: {s}" for s in run["steps"]],
            description="\n".join(x for x in [run["description"], note] if x),
            flags=run["flags"] + ["operator_followup_no"], conflicts=run["conflicts"], now=now, run_id=run["id"],
        )
        appended = False
    run["ticket_id"] = ticket["id"]
    run["followup"] = {"answer": "no", "at": _iso(now), "ticket_id": ticket["id"], "appended": appended}
    return run["followup"]
