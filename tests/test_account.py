"""
account_routes.py over HTTP: stats counted on local days in a zone that is not UTC,
the streak's today-or-yesterday rule, /v1/me's partial edits and refusals, and the
reset that returns the dev user to the new-user state.
"""

import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

from notch_api import store
from notch_api.config import DEV_USER_ID as DEV
from notch_api.fakes import TEXT_MARKER
from tests.wire import ok, refused

OTHER = "00000000-0000-4000-8000-000000000002"
LA = "America/Los_Angeles"  # UTC-7 in September, UTC-8 in December and January


def _stats(api, **params):
    return ok(api.get("/v1/stats", params=params), 200, "stats")


def _numbers(stats):
    return {k: v for k, v in stats.items() if k != "days"}


# ---------------------------------------------------------------------------
# GET /v1/stats
# ---------------------------------------------------------------------------

def test_stats_count_local_days_weeks_and_years_in_the_users_zone(api, clock, add_entry):
    clock.now = datetime(2026, 9, 21, 6, 30, tzinfo=timezone.utc)  # Mon 06:30 UTC = Sun Sep 20, 23:30 in LA
    add_entry("sun-late", "2026-09-21T06:00:00Z")    # LA: Sun Sep 20 23:00, today
    add_entry("sat", "2026-09-19T20:00:00Z")         # LA: Sat Sep 19, yesterday
    add_entry("mon-start", "2026-09-14T07:00:00Z")   # LA: Mon Sep 14 00:00, this week's first second
    add_entry("prev-sun", "2026-09-14T06:59:59Z")    # LA: Sun Sep 13 23:59:59, last week
    add_entry("new-year", "2026-01-01T08:00:00Z", is_milestone=True)  # LA: Jan 1 2026 00:00
    add_entry("old-year", "2026-01-01T07:59:59Z", is_milestone=True)  # LA: Dec 31 2025 23:59:59
    add_entry("pending", "2026-09-21T06:10:00Z", analysis_state="pending")  # not counted anywhere

    ok(api.patch("/v1/me", json={"settings": {"time_zone": LA}}), 200, "me")
    local = _stats(api)
    assert _numbers(local) == {"streak": 2, "total": 5, "record_total": 6, "branches": 1, "this_week": 3, "goal": 5}
    lit = [i - 90 for i, day in enumerate(local["days"]) if day]
    assert lit == [-7, -6, -1, 0]  # Sep 13, Sep 14, Sep 19, Sep 20: the last cell is LA's today

    utc = _stats(api, tz="UTC")  # the request's zone wins over the stored one
    assert _numbers(utc) == {"streak": 1, "total": 6, "record_total": 6, "branches": 2, "this_week": 1, "goal": 5}
    assert [i - 90 for i, day in enumerate(utc["days"]) if day] == [-7, -2, 0]  # Sep 14, Sep 19, Sep 21


@pytest.mark.parametrize("latest, streak", [(0, 3), (1, 3), (2, 0)])
def test_a_streak_ends_today_or_yesterday_or_it_is_zero(api, clock, add_entry, latest, streak):
    today = clock.now.date()
    for n in range(3):  # three days in a row, the newest `latest` days ago
        add_entry(f"e{n}", (today - timedelta(days=latest + n)).isoformat())
    assert _stats(api)["streak"] == streak


def test_a_zone_that_is_not_iana_is_refused(api):
    refused(api.get("/v1/stats", params={"tz": "Mars/Olympus"}), 400, "invalid_request")


# ---------------------------------------------------------------------------
# GET and PATCH /v1/me
# ---------------------------------------------------------------------------

def test_a_new_user_has_no_profile_and_the_reminder_off(api):
    me = ok(api.get("/v1/me"), 200, "me")
    assert (me["id"], me["display_name"], me["email"], me["role"]) == (DEV, None, None, None)
    assert me["settings"]["reminder"]["enabled"] is False  # as the app starts it, until onboarding


def test_an_edit_to_me_changes_only_what_it_sends_and_answers_the_whole_object(api):
    first = ok(api.patch("/v1/me", json={
        "display_name": " Sam Lee ", "role": "Designer", "industry": "Health",
        "settings": {"reminder": {"hour": 9, "weekdays": [5, 1, 1]}, "time_zone": LA}}), 200, "me")
    assert (first["display_name"], first["role"], first["industry"], first["years_experience"]) == (
        "Sam Lee", "Designer", "Health", None)
    assert first["settings"]["reminder"] == {"enabled": False, "hour": 9, "minute": 30, "weekdays": [1, 5]}
    assert first["settings"]["time_zone"] == LA

    second = ok(api.patch("/v1/me", json={"role": "", "settings": {"weekly_goal": 0, "reminder": {"enabled": True},
                                                                   "notify_week_recap": False}}), 200, "me")
    assert (second["display_name"], second["role"]) == ("Sam Lee", None)  # blank clears; absent keeps
    assert second["settings"]["reminder"] == {"enabled": True, "hour": 9, "minute": 30, "weekdays": [1, 5]}
    assert (second["settings"]["weekly_goal"], second["settings"]["notify_week_recap"]) == (0, False)
    assert ok(api.get("/v1/me"), 200, "me") == second


@pytest.mark.parametrize("body", [
    {"settings": {"weekly_goal": 1}},                  # 1 is not a goal, by design
    {"settings": {"weekly_goal": True}},               # a JSON true is not the number 1
    {"settings": {"reminder": {"hour": 24}}},
    {"settings": {"reminder": {"weekdays": [7]}}},     # Sunday is 0; there is no 7
    {"settings": {"time_zone": "GMT+2"}},              # not an IANA name
    {"settings": {"theme": "dark"}},                   # a key the server does not keep
    {"display_name": 7},
])
def test_an_invalid_edit_to_me_changes_nothing(api, body):
    before = ok(api.get("/v1/me"), 200, "me")
    refused(api.patch("/v1/me", json=body), 400, "invalid_request")
    assert ok(api.get("/v1/me"), 200, "me") == before


# ---------------------------------------------------------------------------
# DELETE /v1/me
# ---------------------------------------------------------------------------

def test_the_reset_empties_the_record_and_restores_the_new_user_state(api, conn, audio_dir, add_user, add_project,
                                                                      add_entry):
    new_user = ok(api.get("/v1/me"), 200, "me")
    add_user(OTHER)
    add_project("their-p", "Atlas", user_id=OTHER)
    add_entry("their-e", user_id=OTHER, project_id="their-p")

    ok(api.post("/v1/projects", json={"id": "p1", "name": "Atlas"}), 201, "project")
    ok(api.post("/v1/entries", data={"meta": '{"id": "e1", "recorded_at": "2026-09-21T17:30:00Z", '
                                             '"duration_seconds": 5, "mode": "daily", "catch_up_span": null}'},
                files={"audio": ("n.m4a", TEXT_MARKER + b"Shipped Atlas.", "audio/mp4")}), 202, "entry_accepted")
    add_entry("e2", project_id="p1", is_milestone=True)
    ok(api.post("/v1/reports", json={"id": "r1", "type": "week", "range_start": "2026-09-21",
                                     "range_end": "2026-09-27", "range_label": "Sep 21 – Sep 27"}), 202, "report_accepted")
    ok(api.patch("/v1/me", json={"display_name": "Sam", "role": "Designer", "industry": "Health",
                                 "years_experience": "3", "settings": {
                                     "weekly_goal": 3, "notify_week_recap": False, "notify_report_finished": False,
                                     "time_zone": "Europe/London",
                                     "reminder": {"enabled": True, "hour": 7, "minute": 5, "weekdays": [0, 6]}}}),
       200, "me")

    assert ok(api.delete("/v1/me"), 200, "deleted") == {"deleted": True}

    assert ok(api.get("/v1/me"), 200, "me") == new_user  # the schema's defaults, not a copy of them
    assert ok(api.get("/v1/entries"), 200, "entry_list") == {"entries": [], "next_cursor": None,
                                                              "matched": 0, "total": 0}
    assert ok(api.get("/v1/projects"), 200, "project_list")["projects"] == []
    assert ok(api.get("/v1/reports"), 200, "report_list")["reports"] == []
    stats = ok(api.get("/v1/stats"), 200, "stats")
    assert _numbers(stats) == {"streak": 0, "total": 0, "record_total": 0, "branches": 0, "this_week": 0, "goal": 5}
    assert not any(stats["days"])
    left = conn.execute("SELECT (SELECT count(*) FROM entries) + (SELECT count(*) FROM projects) + "
                        "(SELECT count(*) FROM capture_jobs) + (SELECT count(*) FROM audio_objects) + "
                        "(SELECT count(*) FROM reports) + (SELECT count(*) FROM report_highlights) + "
                        "(SELECT count(*) FROM report_jobs)").fetchone()[0]
    assert left == 2  # only the other user's project and notch
    assert not os.path.exists(os.path.join(audio_dir, DEV))
    ok(api.post("/v1/projects", json={"id": "p2", "name": "Atlas"}), 201, "project")  # the user row is still there


def test_a_capture_landing_during_the_reset_keeps_its_audio_and_its_rows_together(api, conn, audio_dir, monkeypatch):
    sweep, answers = store.remove_audio, []
    meta = ('{"id": "e1", "recorded_at": "2026-09-21T17:30:00Z", "duration_seconds": 5, "mode": "daily", '
            '"catch_up_span": null}')
    upload = threading.Thread(target=lambda: answers.append(api.post(
        "/v1/entries", data={"meta": meta}, files={"audio": ("n.m4a", TEXT_MARKER + b"Shipped it.", "audio/mp4")})))

    def sweep_then_upload(*args, **kwargs):
        sweep(*args, **kwargs)  # the reset has read and unlinked every stored file: now a capture arrives
        upload.start()
        upload.join(timeout=0.5)  # time enough to commit, if the reset lets it in before its own delete

    monkeypatch.setattr(store, "remove_audio", sweep_then_upload)
    ok(api.delete("/v1/me"), 200, "deleted")
    upload.join()
    ok(answers[0], 202, "entry_accepted")
    stored = {os.path.relpath(os.path.join(folder, name), audio_dir)
              for folder, _, names in os.walk(audio_dir) for name in names}
    # The capture landed after the reset, so it survives whole: its file and the row that records it.
    assert stored == {r[0] for r in conn.execute("SELECT storage_key FROM audio_objects")} == {f"{DEV}/e1/000"}
