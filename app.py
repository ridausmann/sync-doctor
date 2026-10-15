"""Sync Doctor: Flask front end. All diagnostic logic lives in diagnostics.py."""
from __future__ import annotations

import os
from itertools import groupby

from flask import Flask, abort, redirect, render_template, request, url_for

import diagnostics as dx
from storage import DEFAULT_DATA_DIR, Storage

OUTCOME_LABELS = {
    dx.AUTO_FIXED: "Fixed automatically",
    dx.NO_ISSUE_FOUND: "No issue found",
    dx.OPERATOR_ACTION: "Action needed",
    dx.ESCALATED: "Passed to support",
}

# Click-to-fill presets for reviewers (see README "Demo scenarios").
DEMO_SCENARIOS = [
    ("op_bayside", "V-101", "", "Existing double booking → urgent ticket"),
    ("op_bayside", "V-102", "2026-10-23", "Turo disconnected → connect steps"),
    ("op_bayside", "V-103", "2026-10-18", "Connection expired → reconnect steps"),
    ("op_bayside", "V-104", "2026-10-25", "Wrong Turo listing → mapping steps"),
    ("op_bayside", "V-105", "2026-10-20", "Stale sync → auto-fixed and verified"),
    ("op_bayside", "V-106", "2026-10-22", "Re-sync 'succeeds' but date still open → escalate"),
    ("op_bayside", "V-107", "2026-10-17", "Re-sync errors → escalate"),
    ("op_bayside", "V-108", "2026-10-24", "Turo outage → not your fault + ticket"),
    ("op_bayside", "V-109", "2026-10-22", "Cancelled trip → no issue"),
    ("op_bayside", "V-109", "2026-10-30", "Pending trip → no issue"),
    ("op_bayside", "V-109", "2026-10-09", "Completed trip → no issue"),
    ("op_bayside", "V-109", "2026-10-24", "No Turo trip that day → no issue"),
    ("op_bayside", "V-109", "2026-10-18", "Blocking correctly → no issue"),
    ("op_bayside", "V-109", "2026-10-25", "Unexplained gap → escalate"),
    ("op_bayside", "V-109", "2026-10-26", "Open ticket exists → duplicate"),
    ("op_bayside", "V-110", "2026-10-21", "Fixed, but 3rd problem in 14 days → escalate"),
    ("op_harbor", "V-201", "2026-10-20", "Time zone mismatch → fix time zone"),
    ("op_bayside", "all", "", "Whole fleet → grouped table"),
]


def create_app(data_dir: str | os.PathLike | None = None, now=dx.NOW) -> Flask:
    app = Flask(__name__)
    store = Storage(data_dir or os.environ.get("SYNC_DOCTOR_DATA_DIR", DEFAULT_DATA_DIR))
    store.ensure()

    @app.context_processor
    def helpers():
        return {"OUTCOME_LABELS": OUTCOME_LABELS, "RESPONSE_TIMES": dx.RESPONSE_TIMES, "now": now}

    def render_form(db, form=None, error=None, status=200, notice=None):
        fleet = {o["id"]: [{"id": v["id"], "label": f"{v['name']} · {v['plate']} ({v['id']})"}
                           for v in db["vehicles"] if v["operator_id"] == o["id"]]
                 for o in db["operators"]}
        return render_template("index.html", operators=db["operators"], fleet=fleet,
                               form=form or {}, error=error, notice=notice,
                               scenarios=DEMO_SCENARIOS), status

    @app.get("/")
    def index():
        notice = "Demo data has been reset." if request.args.get("reset") else None
        return render_form(store.load(), notice=notice)

    @app.post("/diagnose")
    def diagnose():
        form = request.form.to_dict()
        db = store.load()
        try:
            results = dx.run_diagnosis(db, form.get("operator_id", ""), form.get("vehicle_id", ""),
                                       form.get("start"), form.get("end"), form.get("description", ""), now=now)
        except dx.ValidationError as e:
            return render_form(db, form, str(e), status=400)
        store.save(db)
        # Post/Redirect/Get: refreshing the results page must never re-run an auto-fix.
        return redirect(url_for("results", batch_id=results[0]["batch_id"]))

    @app.get("/results/<batch_id>")
    def results(batch_id):
        db = store.load()
        runs = dx.sort_results([r for r in db["runs"] if r["batch_id"] == batch_id])
        if not runs:
            abort(404)
        tickets = {t["id"]: t for t in db["tickets"]}
        groups = [(k, list(g)) for k, g in groupby(
            runs, key=lambda r: "URGENT" if r["priority"] == "urgent" else r["outcome"])]
        operator = next(o for o in db["operators"] if o["id"] == runs[0]["operator_id"])
        return render_template("results.html", runs=runs, groups=groups, tickets=tickets, operator=operator)

    @app.post("/followup")
    def followup():
        db = store.load()
        run_id = request.form.get("run_id", "")
        try:
            dx.handle_followup(db, run_id, fixed=request.form.get("answer") == "yes",
                               note=request.form.get("note", ""), now=now)
        except dx.ValidationError:
            abort(404)
        store.save(db)
        run = next(r for r in db["runs"] if r["id"] == run_id)
        return redirect(url_for("results", batch_id=run["batch_id"]) + f"#{run_id}")

    @app.get("/tickets")
    def tickets():
        db = store.load()
        return render_template("tickets.html", tickets=sorted(db["tickets"], key=lambda t: t["id"], reverse=True))

    @app.post("/reset")
    def reset():
        store.reset()
        return redirect(url_for("index", reset=1))

    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Sync Doctor running at http://127.0.0.1:{port}  (simulated time: {dx.NOW:%Y-%m-%d %H:%M} UTC)")
    create_app().run(host="127.0.0.1", port=port, debug=False)
