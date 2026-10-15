"""Tests for the pure diagnostic engine. All use the fixed NOW (2026-10-15T12:00Z)."""
from datetime import datetime, timezone

import pytest

import diagnostics as dx
from diagnostics import AUTO_FIXED, ESCALATED, NO_ISSUE_FOUND, OPERATOR_ACTION, ValidationError

BAY = "op_bayside"


def run(db, vehicle_id, start=None, end=None, operator=BAY, description=""):
    """Diagnose a single vehicle and return its result."""
    [result] = dx.run_diagnosis(db, operator, vehicle_id, start, end, description, now=dx.NOW)
    return result


def check(result, key):
    return next(c for c in result["checks"] if c["key"] == key)


def vehicle(db, vid):
    return next(v for v in db["vehicles"] if v["id"] == vid)


# --------------------------------------------------------------------------- rule 1: conflict scan

def test_conflict_scan_escalates_urgent_with_both_booking_ids(db):
    r = run(db, "V-101")
    assert r["outcome"] == ESCALATED and r["priority"] == "urgent"
    assert r["reason"] == "existing double booking"
    [c] = r["conflicts"]
    assert (c["turo_trip_id"], c["onenow_booking_id"]) == ("T-9001", "B-501")
    assert c["overlap"] == "Oct 20 – Oct 21"
    ticket = next(t for t in db["tickets"] if t["id"] == r["ticket_id"])
    assert ticket["priority"] == "urgent" and ticket["conflicts"] == r["conflicts"]


def test_conflict_scan_beats_every_other_rule(db):
    # V-107 is stale with a failing re-sync; add an overlapping 1Now booking.
    db["onenow_bookings"].append({"id": "B-999", "vehicle_id": "V-107",
                                  "start": "2026-10-17T09:00:00-07:00", "end": "2026-10-19T09:00:00-07:00"})
    before = vehicle(db, "V-107")["turo_connection"]["last_sync"]
    r = run(db, "V-107", "2026-10-17")
    assert r["outcome"] == ESCALATED and r["priority"] == "urgent"
    assert r["attempted"] == []  # no auto-fix attempted
    assert vehicle(db, "V-107")["turo_connection"]["last_sync"] == before
    assert check(r, "staleness")["context_only"] is True


def test_cancelled_turo_trip_overlapping_1now_booking_is_not_a_conflict(db):
    # V-109 has 1Now booking B-504 on top of cancelled trip T-9082: that's fine.
    r = run(db, "V-109", "2026-10-22")
    assert check(r, "conflict_scan")["status"] == "pass"


# --------------------------------------------------------------------------- rule 2: connection

def test_disconnected_needs_operator_to_connect(db):
    r = run(db, "V-102")
    assert r["outcome"] == OPERATOR_ACTION and r["reason"] == "turo not connected"
    assert any("Connect Turo calendar" in s for s in r["steps"])
    assert r["ticket_id"] is None


def test_expired_needs_operator_to_reconnect(db):
    r = run(db, "V-103", "2026-10-18")
    assert r["outcome"] == OPERATOR_ACTION and r["reason"] == "turo connection expired"
    assert any("Reconnect" in s for s in r["steps"])


# --------------------------------------------------------------------------- rule 3: mapping

def test_wrong_listing_mapping(db):
    r = run(db, "V-104", "2026-10-25")
    assert r["outcome"] == OPERATOR_ACTION and r["reason"] == "vehicle mapped to wrong turo listing"
    assert "turo_lst_5540" in r["explanation"]


def test_missing_listing_mapping(db):
    vehicle(db, "V-109")["turo_listing_id"] = None
    r = run(db, "V-109", "2026-10-18")
    assert r["outcome"] == OPERATOR_ACTION and r["reason"] == "vehicle not mapped to turo listing"


# --------------------------------------------------------------------------- rule 4: staleness + auto-fix

def test_stale_sync_resync_works_is_auto_fixed_and_verified(db):
    r = run(db, "V-105", "2026-10-20")
    assert r["outcome"] == AUTO_FIXED
    v = vehicle(db, "V-105")
    assert v["turo_connection"]["last_sync"] == "2026-10-15T12:00:00Z"
    assert {"start": "2026-10-19", "end": "2026-10-22", "source_trip_id": "T-9041"} in db["blocked_dates"]["V-105"]
    assert any(a.startswith("Verify:") for a in r["attempted"])
    assert check(r, "staleness")["status"] == "fixed"
    assert r["ticket_id"] is None


def test_stale_sync_partial_does_not_claim_success(db):
    r = run(db, "V-106", "2026-10-22")
    assert r["outcome"] == ESCALATED and r["priority"] == "normal"
    assert r["reason"] == "re-sync succeeded but date still not blocking"
    assert r["ticket_id"]


def test_stale_sync_resync_fails_records_error(db):
    r = run(db, "V-107", "2026-10-17")
    assert r["outcome"] == ESCALATED and r["reason"] == "re-sync failed"
    ticket = next(t for t in db["tickets"] if t["id"] == r["ticket_id"])
    assert any("timed out" in a for a in ticket["attempted"])


def test_turo_outage_is_not_operators_fault_and_opens_normal_ticket(db):
    r = run(db, "V-108", "2026-10-24")
    assert r["outcome"] == ESCALATED and r["priority"] == "normal"
    assert r["reason"] == "turo-side outage"
    assert "isn't anything you did" in r["title"]
    assert any("don't accept new 1Now bookings" in s and "Oct 24" in s for s in r["steps"])
    assert r["ticket_id"]


def test_never_synced_counts_as_stale(db):
    vehicle(db, "V-109")["turo_connection"]["last_sync"] = None
    r = run(db, "V-109", "2026-10-25")
    assert r["outcome"] == AUTO_FIXED
    assert "never synced" in r["explanation"]


# --------------------------------------------------------------------------- rule 5: trip status

@pytest.mark.parametrize("day,status", [("2026-10-22", "cancelled"), ("2026-10-30", "pending"), ("2026-10-09", "completed")])
def test_non_blocking_trip_status_is_explained(db, day, status):
    r = run(db, "V-109", day)
    assert r["outcome"] == NO_ISSUE_FOUND and r["reason"] == f"trip is {status}"
    assert status in r["title"]
    assert r["ticket_id"] is None


# --------------------------------------------------------------------------- rule 6: timezone

def test_timezone_mismatch_shifting_block(db):
    r = run(db, "V-201", "2026-10-20", operator="op_harbor")
    assert r["outcome"] == OPERATOR_ACTION and r["reason"] == "timezone mismatch"
    assert "America/New_York" in r["explanation"] and "America/Los_Angeles" in r["explanation"]
    assert "Oct 20" in r["explanation"]


def test_shifted_block_with_matching_timezones_escalates_instead(db):
    next(o for o in db["operators"] if o["id"] == "op_harbor")["timezone"] = "America/Los_Angeles"
    r = run(db, "V-201", "2026-10-20", operator="op_harbor")
    assert check(r, "timezone")["status"] == "fail"
    assert r["outcome"] == ESCALATED and r["reason"] == "unexplained sync gap"


# --------------------------------------------------------------------------- rules 7 and 8

def test_no_turo_trip_on_reported_date(db):
    r = run(db, "V-109", "2026-10-24")
    assert r["outcome"] == NO_ISSUE_FOUND and r["reason"] == "no turo trip on date"
    assert r["title"] == "We can't find a Turo booking on that date"


def test_date_already_blocked_is_no_issue(db):
    r = run(db, "V-109", "2026-10-18")
    assert r["outcome"] == NO_ISSUE_FOUND and r["reason"] == "dates already blocked"


def test_unexplained_gap_escalates_normal_with_all_checks(db):
    r = run(db, "V-109", "2026-10-25", description="Turo shows a trip, 1Now shows available")
    assert r["outcome"] == ESCALATED and r["priority"] == "normal"
    assert r["reason"] == "unexplained sync gap"
    ticket = next(t for t in db["tickets"] if t["id"] == r["ticket_id"])
    assert [c["rule"] for c in ticket["checks"]] == list(range(1, 9))
    assert ticket["operator_description"] == "Turo shows a trip, 1Now shows available"


def test_every_rule_is_recorded_even_when_stopping_early(db):
    r = run(db, "V-102", "2026-10-23")
    assert [c["rule"] for c in r["checks"]] == list(range(1, 9))
    assert all(c["context_only"] for c in r["checks"][2:])
    assert check(r, "final")["status"] == "skipped"
    # staleness detected but not acted on (V-102 last synced Sep 30)
    assert "not attempted" in check(r, "staleness")["detail"]


# --------------------------------------------------------------------------- cross-cutting

def test_repeat_failures_escalate_even_after_successful_auto_fix(db):
    r = run(db, "V-110", "2026-10-21")
    assert r["outcome"] == ESCALATED and r["original_outcome"] == AUTO_FIXED
    assert "repeat_failure" in r["flags"]
    assert "3rd" in r["explanation"]
    # the fix itself was still applied and logged
    assert db["blocked_dates"]["V-110"] and r["ticket_id"]


def test_repeat_failure_ignores_tickets_older_than_14_days(db):
    # V-105 gets one recent ticket and one old one: below the threshold.
    for tid, created in [("SD-2001", "2026-10-10T00:00:00Z"), ("SD-2002", "2026-09-01T00:00:00Z")]:
        db["tickets"].append({"id": tid, "vehicle_id": "V-105", "status": "resolved", "created_at": created,
                              "reported_start": None, "reported_end": None})
    r = run(db, "V-105", "2026-10-20")
    assert r["outcome"] == AUTO_FIXED and r["flags"] == []


def test_repeat_failure_never_changes_no_issue_outcome(db):
    for i in range(3):
        db["tickets"].append({"id": f"SD-30{i}", "vehicle_id": "V-109", "status": "resolved",
                              "created_at": "2026-10-14T00:00:00Z", "reported_start": None, "reported_end": None})
    r = run(db, "V-109", "2026-10-24")
    assert r["outcome"] == NO_ISSUE_FOUND and r["flags"] == []


def test_duplicate_request_reuses_open_ticket(db):
    count = len(db["tickets"])
    r = run(db, "V-109", "2026-10-26")
    assert r["outcome"] == ESCALATED
    assert r["duplicate"] is True and r["ticket_id"] == "SD-1004"
    assert len(db["tickets"]) == count


def test_running_twice_creates_only_one_ticket(db):
    first = run(db, "V-107", "2026-10-17")
    second = run(db, "V-107", "2026-10-17")
    assert first["duplicate"] is False and second["duplicate"] is True
    assert second["ticket_id"] == first["ticket_id"]


def test_resolved_ticket_does_not_count_as_duplicate(db):
    next(t for t in db["tickets"] if t["id"] == "SD-1004")["status"] = "resolved"
    r = run(db, "V-109", "2026-10-26")
    assert r["duplicate"] is False and r["ticket_id"] != "SD-1004"


def test_all_vehicles_runs_each_and_sorts_urgent_first(db):
    results = dx.run_diagnosis(db, BAY, "all", now=dx.NOW)
    assert len(results) == 10
    assert {r["vehicle_id"] for r in results} == {f"V-1{n:02d}" for n in range(1, 11)}
    assert results[0]["vehicle_id"] == "V-101" and results[0]["priority"] == "urgent"
    order = [dx.OUTCOME_ORDER[r["outcome"]] for r in results[1:]]
    assert order == sorted(order)
    assert len({r["batch_id"] for r in results}) == 1


def test_all_vehicles_for_single_car_operator(db):
    results = dx.run_diagnosis(db, "op_harbor", "all", now=dx.NOW)
    assert [r["vehicle_id"] for r in results] == ["V-201"]


# --------------------------------------------------------------------------- follow-up

def test_followup_no_creates_ticket_when_none_exists(db):
    r = run(db, "V-103", "2026-10-18")
    fu = dx.handle_followup(db, r["id"], fixed=False, note="Reconnected, still open", now=dx.NOW)
    assert fu["answer"] == "no" and fu["appended"] is False
    ticket = next(t for t in db["tickets"] if t["id"] == fu["ticket_id"])
    assert "operator_followup_no" in ticket["flags"] and "still open" in ticket["operator_description"]


def test_followup_no_appends_to_existing_open_ticket(db):
    r = run(db, "V-105", "2026-10-20")  # AUTO_FIXED, no ticket of its own
    db["tickets"].append({"id": "SD-1500", "vehicle_id": "V-105", "status": "open", "created_at": "2026-10-15T00:00:00Z",
                          "reported_start": "2026-10-19", "reported_end": "2026-10-21", "history": []})
    count = len(db["tickets"])
    fu = dx.handle_followup(db, r["id"], fixed=False, note="Still open on the 20th", now=dx.NOW)
    assert fu["ticket_id"] == "SD-1500" and fu["appended"] is True and len(db["tickets"]) == count
    assert db["tickets"][-1]["history"][-1]["event"] == "operator_followup"


def test_followup_yes_and_idempotency(db):
    r = run(db, "V-105", "2026-10-20")
    count = len(db["tickets"])
    assert dx.handle_followup(db, r["id"], fixed=True)["answer"] == "yes"
    assert dx.handle_followup(db, r["id"], fixed=False)["answer"] == "yes"  # already answered
    assert len(db["tickets"]) == count


def test_followup_unknown_run(db):
    with pytest.raises(ValidationError):
        dx.handle_followup(db, "RUN-9999", fixed=False)


# --------------------------------------------------------------------------- validation

@pytest.mark.parametrize("operator,vehicle_id,start,end,message", [
    (BAY, "V-999", None, None, "couldn't find a vehicle"),
    (BAY, "V-201", None, None, "isn't part of"),
    ("op_nobody", "V-101", None, None, "operator account"),
    (BAY, "", None, None, "choose a vehicle"),
    (BAY, "V-101", "2026-13-40", None, "isn't a date"),
    (BAY, "V-101", "next tuesday", None, "isn't a date"),
    (BAY, "V-101", "2026-10-20", "2026-10-18", "end date is before"),
    (BAY, "V-101", None, "2026-10-18", "add a start date"),
    (BAY, "V-101", "2026-01-01", "2026-12-31", "62 days or fewer"),
])
def test_validation_errors(db, operator, vehicle_id, start, end, message):
    with pytest.raises(ValidationError, match=message):
        dx.run_diagnosis(db, operator, vehicle_id, start, end, now=dx.NOW)


def test_validation_failure_has_no_side_effects(db):
    with pytest.raises(ValidationError):
        dx.run_diagnosis(db, BAY, "V-105", "2026-10-20", "2026-10-01")
    assert db["runs"] == [] and vehicle(db, "V-105")["turo_connection"]["last_sync"] == "2026-10-12T08:00:00Z"


def test_now_is_injectable(db):
    # At the default NOW V-109's sync is fresh; three days later it's stale, so it gets re-synced.
    later = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
    [r] = dx.run_diagnosis(db, BAY, "V-109", "2026-10-24", now=later)
    assert r["outcome"] == AUTO_FIXED
    assert run(db, "V-109", "2026-10-24")["outcome"] == NO_ISSUE_FOUND
