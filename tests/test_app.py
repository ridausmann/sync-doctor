"""End-to-end smoke tests through Flask, using a throwaway data directory."""
import json

import pytest

from app import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(data_dir=tmp_path)
    app.testing = True
    with app.test_client() as c:
        c.data_dir = tmp_path
        yield c


def diagnose(client, vehicle_id, start="", operator="op_bayside", **extra):
    data = {"operator_id": operator, "vehicle_id": vehicle_id, "start": start, "end": "", "description": "", **extra}
    return client.post("/diagnose", data=data, follow_redirects=True)


def test_index_renders_form(client):
    r = client.get("/")
    assert r.status_code == 200 and b"Diagnose" in r.data and b"Bayside Car Rentals" in r.data


def test_auto_fix_flow_and_followup_no_opens_ticket(client):
    r = diagnose(client, "V-105", "2026-10-20")
    html = r.get_data(as_text=True)
    assert "Fixed automatically" in html and "Did this fix it?" in html
    run_id = json.loads((client.data_dir / "runs.json").read_text())[-1]["id"]

    r = client.post("/followup", data={"run_id": run_id, "answer": "no", "note": "still open"}, follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "We've opened ticket" in html and "SD-1005" in html
    tickets = json.loads((client.data_dir / "tickets.json").read_text())
    assert tickets[-1]["id"] == "SD-1005" and "still open" in tickets[-1]["operator_description"]


def test_urgent_conflict_shows_ticket_and_response_time(client):
    html = diagnose(client, "V-101").get_data(as_text=True)
    assert "URGENT" in html and "B-501" in html and "T-9001" in html and "within 1 hour" in html


def test_duplicate_shows_existing_ticket(client):
    html = diagnose(client, "V-109", "2026-10-26").get_data(as_text=True)
    assert "already have an open ticket" in html and "SD-1004" in html


def test_fleet_view_groups_results(client):
    html = diagnose(client, "all").get_data(as_text=True)
    assert "10 vehicles checked" in html
    assert html.index("Urgent") < html.index("Action needed") < html.index("Fixed automatically")


def test_validation_error_is_friendly_400(client):
    r = diagnose(client, "V-201")  # belongs to the other operator
    assert r.status_code == 400 and "isn&#39;t part of" in r.get_data(as_text=True)


def test_refreshing_results_does_not_rerun(client):
    r = client.post("/diagnose", data={"operator_id": "op_bayside", "vehicle_id": "V-107", "start": "2026-10-17"})
    assert r.status_code == 302
    client.get(r.headers["Location"])
    client.get(r.headers["Location"])
    assert len(json.loads((client.data_dir / "runs.json").read_text())) == 1


def test_reset_restores_seed(client):
    diagnose(client, "V-107", "2026-10-17")
    client.post("/reset")
    assert len(json.loads((client.data_dir / "tickets.json").read_text())) == 4
