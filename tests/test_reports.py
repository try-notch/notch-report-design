"""
reports.py: the numbers frozen at acceptance, and the prose the job writes.

The numbers are what a person quotes at review time, so they are tested at their
edges: bucket boundaries, floor shares, the day edges of the range in the user's own
zone. The job is
tested for what it must not let through from the model (invented ids, unnormalised
themes, a missing headline) and for how it fails.
"""

import json
from datetime import date

import pytest

from notch_api import contract, reports, store
from notch_api.config import DEV_USER_ID as DEV
from notch_api.fakes import FakeClient
from notch_api.openrouter import ModelUnavailable
from notch_api.reports import accept_report, momentum, run_report_job
from notch_api.store import ApiError

OTHER = "00000000-0000-4000-8000-000000000002"


def _req(**fields):
    return {"id": "r1", "type": "week", "range_start": "2026-09-21", "range_end": "2026-09-27",
            "range_label": "Sep 21 – Sep 27", "project_id": None, "tag": None} | fields


def _counts(dates, start, end):
    granularity, buckets = momentum([date.fromisoformat(d) for d in dates],
                                    date.fromisoformat(start), date.fromisoformat(end))
    return granularity, [(b["date"], b["count"]) for b in buckets]


def _refused(conn, req, user_id=DEV):
    with pytest.raises(ApiError) as err:
        accept_report(conn, user_id, req)
    return err.value.code, err.value.status


def _job(conn, job_id):
    return conn.execute("SELECT state, failure_code, finished_at FROM report_jobs WHERE id = ?", (job_id,)).fetchone()


# ---------------------------------------------------------------------------
# Momentum
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("end, granularity", [
    ("2026-01-31", "day"),    # 31 days, both ends counted
    ("2026-02-01", "week"),   # 32
    ("2026-04-30", "week"),   # 120
    ("2026-05-01", "month"),  # 121
])
def test_granularity_follows_the_inclusive_range_length(end, granularity):
    assert _counts([], "2026-01-01", end)[0] == granularity


def test_day_buckets_cover_the_whole_range_with_zeros():
    assert _counts(["2026-09-21", "2026-09-21", "2026-09-23"], "2026-09-21", "2026-09-27") == ("day", [
        ("2026-09-21", 2), ("2026-09-22", 0), ("2026-09-23", 1), ("2026-09-24", 0),
        ("2026-09-25", 0), ("2026-09-26", 0), ("2026-09-27", 0)])


def test_week_buckets_start_on_the_monday_before_a_midweek_start_and_cross_the_year():
    dates = ["2026-12-16",   # the Wednesday the range starts on
             "2026-12-20",   # that week's Sunday
             "2026-12-21",   # the next Monday
             "2027-01-01",   # a Friday in the week that began 2026-12-28
             "2027-02-10"]   # the range's last day
    assert _counts(dates, "2026-12-16", "2027-02-10") == ("week", [
        ("2026-12-14", 2), ("2026-12-21", 1), ("2026-12-28", 1), ("2027-01-04", 0), ("2027-01-11", 0),
        ("2027-01-18", 0), ("2027-01-25", 0), ("2027-02-01", 0), ("2027-02-08", 1)])


def test_month_buckets_hold_month_ends_and_roll_over_the_year():
    dates = ["2026-11-30", "2026-12-31", "2027-01-01", "2027-02-28"]
    assert _counts(dates, "2026-11-15", "2027-03-31") == ("month", [
        ("2026-11-01", 1), ("2026-12-01", 1), ("2027-01-01", 1), ("2027-02-01", 1), ("2027-03-01", 0)])


@pytest.mark.parametrize("start, last", [("9999-12-01", "9999-12-31"), ("9999-01-01", "9999-12-01")])  # day, month
def test_momentum_stops_at_the_last_bucket_a_date_can_hold(start, last):
    assert _counts(["9999-12-31"], start, "9999-12-31")[1][-1] == (last, 1)


# ---------------------------------------------------------------------------
# Acceptance
# ---------------------------------------------------------------------------

def test_accept_freezes_the_numbers_for_complete_notches_on_utc_days_in_range(conn, add_user, add_project,
                                                                              add_entry):
    add_user(OTHER)
    add_project("p-a", "Atlas")
    add_project("p-b", "Billing")
    add_entry("first", "2026-09-21T00:00:00Z", project_id="p-a")
    add_entry("mid", "2026-09-23", project_id="p-a", is_milestone=True)
    add_entry("loose", "2026-09-24")                       # unassigned: counted, no project
    add_entry("last", "2026-09-27T23:59:59Z", project_id="p-b")
    add_entry("before", "2026-09-20T23:59:59Z")
    add_entry("after", "2026-09-28T00:00:00Z")
    add_entry("pending", "2026-09-22", analysis_state="pending")
    add_entry("theirs", "2026-09-22", user_id=OTHER)

    report_id, job_id, created = accept_report(conn, DEV, _req())
    wire = store.load_report(conn, DEV, report_id)
    contract.validate("report", wire)

    assert created and wire["source_entry_ids"] == ["first", "mid", "loose", "last"]
    assert wire["counts"] == {"notches": 4, "projects": 2, "milestones": 1}
    assert [b["count"] for b in wire["momentum"]] == [1, 0, 1, 1, 0, 0, 1]
    assert wire["eyebrow"] == "Weekly report · Sep 21 – Sep 27"
    assert wire["headline"] is None and wire["highlights"] == [] and wire["themes"] == []
    assert tuple(_job(conn, job_id)) == ("queued", None, None)


def test_the_range_and_its_momentum_are_days_in_the_users_zone(conn, add_entry):
    with conn:
        conn.execute("UPDATE users SET time_zone = 'America/Los_Angeles' WHERE id = ?", (DEV,))  # UTC-7
    add_entry("sun-night", "2026-09-21T06:59:59Z")   # Sun Sep 20 in LA: before the range, though Sep 21 in UTC
    add_entry("mon-midnight", "2026-09-21T07:00:00Z")  # Mon Sep 21 00:00 in LA: the first second in range
    add_entry("sun-late", "2026-09-28T06:30:00Z")    # Sun Sep 27 23:30 in LA: the last day, though Sep 28 in UTC
    add_entry("next-mon", "2026-09-28T07:00:00Z")    # Mon Sep 28 in LA: after the range
    wire = store.load_report(conn, DEV, accept_report(conn, DEV, _req())[0])
    assert wire["source_entry_ids"] == ["mon-midnight", "sun-late"]
    assert [b["count"] for b in wire["momentum"]] == [1, 0, 0, 0, 0, 0, 1]


def test_breakdown_floors_shares_over_every_notch_and_orders_by_count_then_name(conn, add_project, add_entry):
    for project_id, name in [("z", "Zeta"), ("b", "Beta"), ("a", "Alpha")]:
        add_project(project_id, name)
    for i, project_id in enumerate(["z", "z", "z", "b", "b", "a", "a", None]):
        add_entry(f"e{i}", project_id=project_id)
    wire = store.load_report(conn, DEV, accept_report(conn, DEV, _req())[0])
    assert wire["project_breakdown"] == [
        {"name": "Zeta", "notch_count": 3, "share": 37},  # 37.5: floored, never rounded up
        {"name": "Alpha", "notch_count": 2, "share": 25},
        {"name": "Beta", "notch_count": 2, "share": 25},
    ]
    assert wire["counts"]["projects"] == 3


def test_a_one_day_range_is_a_report(conn, add_entry):
    add_entry("a", "2026-09-21")
    wire = store.load_report(conn, DEV, accept_report(conn, DEV, _req(range_end="2026-09-21"))[0])
    assert wire["momentum"] == [{"date": "2026-09-21", "count": 1}]


def test_tag_scope_is_normalised_and_matches_whole_tags(conn, add_entry):
    add_entry("a", tags=["flaky-tests", "shipped"])
    add_entry("b", tags=["shipped"])
    add_entry("c", tags=["flaky-tests"])
    add_entry("d", tags=["flaky"])
    first = accept_report(conn, DEV, _req(tag="#Flaky Tests"))[0]
    second = accept_report(conn, DEV, _req(id="r2", tag="flaky"))[0]
    assert store.load_report(conn, DEV, first)["source_entry_ids"] == ["a", "c"]
    assert store.load_report(conn, DEV, second)["source_entry_ids"] == ["d"]
    assert conn.execute("SELECT tag FROM reports WHERE id = ?", (first,)).fetchone()[0] == "flaky-tests"


def test_project_scope_counts_that_project_and_refuses_one_the_user_does_not_own(conn, add_user, add_project,
                                                                                 add_entry):
    add_user(OTHER)
    add_project("p-a", "Atlas")
    add_project("p-x", "Theirs", user_id=OTHER)
    add_entry("a1", "2026-09-21", project_id="p-a")
    add_entry("a2", "2026-09-22", project_id="p-a")
    add_entry("loose", "2026-09-22")
    wire = store.load_report(conn, DEV, accept_report(conn, DEV, _req(project_id="p-a"))[0])
    assert wire["source_entry_ids"] == ["a1", "a2"]
    assert wire["project_breakdown"] == [{"name": "Atlas", "notch_count": 2, "share": 100}]
    assert _refused(conn, _req(id="r2", project_id="p-x")) == ("not_found", 404)
    assert _refused(conn, _req(id="r3", project_id="missing")) == ("not_found", 404)


def test_a_range_with_no_complete_notches_is_refused_and_writes_nothing(conn, add_entry):
    add_entry("pending", "2026-09-22", analysis_state="pending")
    assert _refused(conn, _req()) == ("empty_range", 422)
    assert conn.execute("SELECT (SELECT count(*) FROM reports) + (SELECT count(*) FROM report_jobs)").fetchone()[0] == 0


def test_a_repeat_returns_the_same_job_without_recounting_and_another_user_cannot_take_the_id(conn, add_user,
                                                                                              add_entry):
    add_entry("a", "2026-09-21")
    report_id, job_id, _ = accept_report(conn, DEV, _req())
    with conn:  # the only notch goes: a recount would now be an empty range
        conn.execute("DELETE FROM entries WHERE id = 'a'")
    assert accept_report(conn, DEV, _req()) == (report_id, job_id, False)
    wire = store.load_report(conn, DEV, report_id)
    assert wire["counts"]["notches"] == 1 and wire["source_entry_ids"] == ["a"]  # frozen, dangling on purpose
    add_user(OTHER)
    assert _refused(conn, _req(), user_id=OTHER) == ("conflict", 409)


@pytest.mark.parametrize("fields", [
    {"id": "  "},
    {"type": "fortnight"},
    {"range_start": "2026-9-21"},
    {"range_start": "2026-09-28"},   # after range_end
    {"range_label": ""},
    {"tag": "#"},                    # normalises to nothing
])
def test_a_malformed_request_is_invalid(conn, fields):
    assert _refused(conn, _req(**fields)) == ("invalid_request", 400)


# ---------------------------------------------------------------------------
# The writing job
# ---------------------------------------------------------------------------

@pytest.fixture
def accepted(conn, add_project, add_entry):
    add_project("p-a", "Atlas")
    add_entry("e1", "2026-09-21", project_id="p-a", is_milestone=True, tags=["shipped"],
              categories=["wins", "collaboration"], acknowledged_by="Dana")
    add_entry("e2", "2026-09-23", categories=["collaboration"])
    return accept_report(conn, DEV, _req())[:2]


def test_the_job_writes_a_valid_document_and_drops_invented_ids(conn, db_path, fake_client, accepted):
    report_id, job_id = accepted
    run_report_job(db_path, job_id, client=fake_client)

    wire = store.load_report(conn, DEV, report_id)
    contract.validate("report", wire)
    assert all(wire[key] for key in ("headline", "lede", "body"))
    # The fake cites e1 + ghost-id, e2, and ghost-id alone.
    assert [(h["ordinal"], h["kind"], h["source_entry_ids"]) for h in wire["highlights"]] == [
        (0, "shipped", ["e1"]), (1, "collaboration", ["e2"]), (2, "note", [])]
    assert wire["themes"] == ["shipped", "pairing", "momentum"]
    state, code, finished_at = _job(conn, job_id)
    assert (state, code) == ("complete", None) and finished_at

    (_, call), = fake_client.calls
    assert call["tool_name"] == "write_report" and call["temperature"] == 0.3
    # Per-category facts count notches, and every notch travels with its transcript.
    assert "collaboration: 2 of 2 notches; wins: 1 of 2 notches" in call["user"]
    assert call["user"].count("transcript: ") == 2


def test_messy_model_output_is_repaired_before_it_is_stored(conn, db_path, accepted):
    report_id, job_id = accepted
    client = FakeClient(overrides={"write_report": {
        "themes": ["#Shipped", "shipped", "Collaboration", "Big Wins", "Atlas", "q3:okrs", "six", "seven", "eight"],
        # Some providers send a nested array as JSON text.
        "highlights": json.dumps([
            {"title": "Atlas landed", "detail": "demoed", "kind": "milestone", "source_entry_ids": ["e1", "e1", "e9"]},
            {"title": "Quiet win", "detail": "no milestone behind it", "kind": "milestone", "source_entry_ids": ["e2"]},
            {"title": "Odd kind", "detail": "x", "kind": "trophy", "source_entry_ids": []},
            {"title": "  ", "detail": "untitled", "kind": "note", "source_entry_ids": []},
        ]),
    }})
    run_report_job(db_path, job_id, client=client)
    wire = store.load_report(conn, DEV, report_id)
    contract.validate("report", wire)
    assert wire["themes"] == ["shipped", "big-wins", "q3:okrs", "six", "seven"]
    assert [(h["kind"], h["source_entry_ids"]) for h in wire["highlights"]] == [
        ("milestone", ["e1"]), ("note", ["e2"]), ("note", [])]


@pytest.mark.parametrize("client, code", [
    (FakeClient(fail_with=ModelUnavailable("HTTP 503")), "model_unavailable"),
    (FakeClient(overrides={"write_report": {"headline": "  "}}), "model_refused"),  # nothing honest to render
    (FakeClient(fail_with=RuntimeError("a bug")), "model_unavailable"),              # unexpected: still a closed code
])
def test_a_failed_write_fails_the_job_and_leaves_the_frozen_report_unwritten(conn, db_path, accepted, client, code):
    report_id, job_id = accepted
    run_report_job(db_path, job_id, client=client)
    state, failure_code, finished_at = _job(conn, job_id)
    assert (state, failure_code) == ("failed", code) and finished_at
    wire = store.load_report(conn, DEV, report_id)
    assert wire["headline"] is None and wire["highlights"] == [] and wire["counts"]["notches"] == 2


def test_a_finished_job_is_not_written_twice(conn, db_path, fake_client, accepted):
    report_id, job_id = accepted
    run_report_job(db_path, job_id, client=fake_client)
    run_report_job(db_path, job_id, client=fake_client)  # e.g. a resume racing a submit
    assert len(fake_client.calls) == 1
    assert len(store.load_report(conn, DEV, report_id)["highlights"]) == 3


def test_a_report_discarded_while_the_model_writes_is_left_gone(conn, db_path, accepted, caplog):
    report_id, job_id = accepted

    class Discarding(FakeClient):
        def tool_call(self, **kwargs):
            with conn:  # DELETE /v1/reports/{id} lands mid-call; its job row goes with it
                conn.execute("DELETE FROM reports WHERE id = ?", (report_id,))
            return super().tool_call(**kwargs)

    run_report_job(db_path, job_id, client=Discarding())
    assert conn.execute("SELECT (SELECT count(*) FROM reports) + (SELECT count(*) FROM report_highlights)"
                        " + (SELECT count(*) FROM report_jobs)").fetchone()[0] == 0
    assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]  # no crash logged


def test_a_report_discarded_while_it_is_counted_asks_the_model_nothing(conn, db_path, fake_client, accepted, caplog,
                                                                     monkeypatch):
    report_id, job_id = accepted
    count = reports._user_message

    def discard_then_count(*args):
        with conn:  # DELETE /v1/reports/{id} lands while the facts are built; its job row goes with it
            conn.execute("DELETE FROM reports WHERE id = ?", (report_id,))
        return count(*args)

    monkeypatch.setattr(reports, "_user_message", discard_then_count)
    run_report_job(db_path, job_id, client=fake_client)
    assert fake_client.calls == []
    assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]  # no crash logged


def test_the_prompt_dates_each_notch_by_its_day_in_the_users_zone(conn, db_path, fake_client, add_entry):
    with conn:
        conn.execute("UPDATE users SET time_zone = 'America/Los_Angeles' WHERE id = ?", (DEV,))  # UTC-7
    add_entry("sun-eve", "2026-09-28T03:30:00Z")  # Sun Sep 27, 20:30 in LA (the reminder's hour); Sep 28 in UTC
    run_report_job(db_path, accept_report(conn, DEV, _req())[1], client=fake_client)
    assert "\n[id sun-eve] 2026-09-27 — " in fake_client.calls[0][1]["user"]  # the last day of Sep 21 – 27


def test_above_sixty_notches_the_prompt_carries_summaries_only(conn, db_path, fake_client, add_entry):
    for i in range(61):
        add_entry(f"e{i:02d}", "2026-09-22")
    run_report_job(db_path, accept_report(conn, DEV, _req())[1], client=fake_client)
    user = fake_client.calls[0][1]["user"]
    assert user.count("[id ") == 61 and "transcript: " not in user
