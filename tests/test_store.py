"""
store.py and schema.sql: the helpers every other module leans on, and the schema
rules that are the last line of defence when a handler gets something wrong.
"""

import re
import sqlite3
from datetime import datetime, timezone

import pytest

from notch_api import contract, store
from notch_api.config import DEV_USER_ID as DEV

OTHER = "00000000-0000-4000-8000-000000000002"
TAG_PATTERN = r"[a-z0-9][a-z0-9:-]*"


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("#Flaky Tests", "flaky-tests"),          # hash, case, inner space
    ("  ##project_atlas ", "project-atlas"),  # repeated hashes, underscore, outer space
    ("ci/cd & node.js", "cicd-nodejs"),       # punctuation dropped, hyphen run collapsed
    ("q3:okrs", "q3:okrs"),                   # colon kept
    ("-- wip --", "wip"),                     # hyphens trimmed
    (":scope", "scope"),                      # would otherwise break the wire pattern
    ("#", ""),                                # nothing left
])
def test_normalize_tag(raw, expected):
    tag = store.normalize_tag(raw)
    assert tag == expected
    assert tag == "" or re.fullmatch(TAG_PATTERN, tag)


def test_normalize_tags_drops_empties_and_duplicates_keeping_first_order():
    raw = ["Shipped", "#shipped", "", "   ", "Pairing", None, 7, "shipped "]
    assert store.normalize_tags(raw) == ["shipped", "pairing"]


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

def test_instants_with_z_or_offset_are_the_same_utc_second():
    z = store.parse_instant("2026-05-11T08:42:00Z")
    offset = store.parse_instant("2026-05-11T10:42:00.750+02:00")
    assert z == offset and z.tzinfo is not None
    assert store.iso(offset) == "2026-05-11T08:42:00Z"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", store.now())


@pytest.mark.parametrize("bad", ["2026-05-11T08:42:00", "yesterday"])
def test_parse_instant_rejects_zoneless_and_garbage(bad):
    with pytest.raises(ValueError):
        store.parse_instant(bad)


def test_parse_date_reads_yyyy_mm_dd():
    assert store.parse_date("2026-05-11").isoformat() == "2026-05-11"


@pytest.mark.parametrize("bad", ["20260511", "2026-13-01", None])
def test_parse_date_rejects_every_other_shape(bad):
    with pytest.raises(ValueError):
        store.parse_date(bad)


@pytest.mark.parametrize("start, end, bounds", [
    ("2026-03-08", "2026-03-08", ("2026-03-08T08:00:00Z", "2026-03-09T07:00:00Z")),  # a 23-hour day: DST starts
    ("9999-12-31", "9999-12-31", ("9999-12-31T08:00:00Z", "~")),                    # no midnight after it: open
])
def test_local_day_bounds_are_the_zones_midnights(start, end, bounds):
    tz = store.zone("America/Los_Angeles")
    assert store.local_day_bounds(store.parse_date(start), store.parse_date(end), tz) == bounds


@pytest.mark.parametrize("name", ["", "GMT+2", "../../etc/passwd", "Mars/Olympus", None])
def test_zone_takes_iana_names_only(name):
    with pytest.raises(ValueError):
        store.zone(name)


def test_iso_treats_naive_as_utc():
    assert store.iso(datetime(2026, 5, 11, 8, 42)) == "2026-05-11T08:42:00Z"
    assert store.iso(datetime(2026, 5, 11, 8, 42, tzinfo=timezone.utc)) == "2026-05-11T08:42:00Z"


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def test_init_db_is_idempotent_and_keeps_data(db_path, conn, add_entry):
    add_entry("e1")
    store.init_db(db_path)  # every server start re-applies the schema
    store.ensure_dev_user(conn)
    assert conn.execute("SELECT count(*) FROM entries").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1


def test_ensure_dev_user_sets_profile_and_rejects_unknown_columns(conn):
    store.ensure_dev_user(conn, display_name="Jordan Kim", industry="Technology", years_experience="5")
    row = conn.execute("SELECT display_name, industry, years_experience FROM users WHERE id = ?",
                       (DEV,)).fetchone()
    assert tuple(row) == ("Jordan Kim", "Technology", "5")
    with pytest.raises(ValueError):
        store.ensure_dev_user(conn, id="someone-else")


# ---------------------------------------------------------------------------
# Schema rules
# ---------------------------------------------------------------------------

def test_entry_cannot_point_at_another_users_project(add_user, add_project, add_entry):
    add_user(OTHER)
    add_project("p-theirs", "Atlas", user_id=OTHER)
    add_project("p-mine", "Atlas")  # same folded name, different owner: allowed
    add_entry("mine", project_id="p-mine")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        add_entry("stolen", project_id="p-theirs")


def test_project_names_are_unique_per_owner_ignoring_case(add_project):
    add_project("p1", "Front-End Refactor")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        add_project("p2", "front-end refactor")


@pytest.mark.parametrize("fields", [
    {"capture_mode": "daily", "span_start": "2026-05-11", "span_end": "2026-05-12"},
    {"capture_mode": "catch_up"},
    {"capture_mode": "catch_up", "span_start": "2026-05-12", "span_end": "2026-05-11"},
    {"analysis_state": "failed"},
    {"analysis_state": "complete", "analysis_failure_code": "model_refused"},
    {"tags": ["#Shipped"]},
    {"classified_by": "gpt"},  # which path classified: jev or llm, nothing else
])
def test_entry_checks_reject_inconsistent_rows(add_entry, fields):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        add_entry("bad", **fields)


def test_second_capture_job_for_one_entry_is_refused(conn, add_entry):
    add_entry("e1")
    with conn:
        conn.execute("INSERT INTO capture_jobs (id, user_id, entry_id) VALUES (?, ?, 'e1')",
                     (store.new_id(), DEV))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        conn.execute("INSERT INTO capture_jobs (id, user_id, entry_id) VALUES (?, ?, 'e1')",
                     (store.new_id(), DEV))


def _add_audio(conn, entry_id, **columns):
    job_id = store.new_id()
    row = {"id": store.new_id(), "user_id": DEV, "capture_job_id": job_id, "entry_id": entry_id,
           "storage_key": f"{DEV}/{entry_id}/000", "byte_size": 1024} | columns
    with conn:
        conn.execute("INSERT INTO capture_jobs (id, user_id, entry_id) VALUES (?, ?, ?)",
                     (job_id, DEV, entry_id))
        conn.execute(f"INSERT INTO audio_objects ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                     list(row.values()))


def test_audio_purge_after_is_seven_days_from_upload(conn, add_entry):
    add_entry("e1")
    _add_audio(conn, "e1", uploaded_at="2026-09-01T10:00:00Z")
    purge_after = conn.execute("SELECT purge_after FROM audio_objects").fetchone()[0]
    assert purge_after == "2026-09-08T10:00:00Z"


def test_audio_storage_key_must_derive_from_the_row(conn, add_entry):
    add_entry("e1")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _add_audio(conn, "e1", storage_key=f"{DEV}/e1/0.m4a")


# ---------------------------------------------------------------------------
# Row -> wire
# ---------------------------------------------------------------------------

def test_complete_entry_maps_to_a_valid_wire_entry(conn, add_project, add_entry):
    add_project("p1", "Front-End Refactor")
    add_entry("e1", tags=["shipped", "front-end-refactor"], categories=["wins", "collaboration"],
              project_id="p1", corrected_text="What I actually said.", is_milestone=True)
    _add_audio(conn, "e1", uploaded_at="2026-09-21T17:31:00Z")

    entry = store.load_entry(conn, DEV, "e1")

    contract.validate("entry", entry)
    assert entry["transcript"] == "What I actually said."  # corrected text wins over raw
    assert (entry["project_id"], entry["project"]) == ("p1", "Front-End Refactor")
    assert entry["retryable_until"] == "2026-09-28T17:31:00Z"
    assert entry["is_milestone"] is True


def _add_report(conn, **columns):
    row = {"id": "r1", "user_id": DEV, "type": "week", "range_start": "2026-09-21",
           "range_end": "2026-09-27", "range_label": "Sep 21 – Sep 27"} | columns
    with conn:
        conn.execute(f"INSERT INTO reports ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                     list(row.values()))


def test_unwritten_and_written_reports_map_to_valid_wire_reports(conn):
    _add_report(conn)  # accepted, prose not written yet, breakdown still NULL
    report = store.load_report(conn, DEV, "r1")
    contract.validate("report", report)
    assert report["headline"] is None and report["project_breakdown"] == []

    with conn:
        conn.execute("UPDATE reports SET headline = 'Shipping through the fear', themes = '[\"shipped\"]',"
                     " source_entry_ids = '[\"e1\",\"e2\"]', notch_count = 2 WHERE id = 'r1'")
        for ordinal, ids in ((1, '["e2"]'), (0, '["e1"]')):
            conn.execute("INSERT INTO report_highlights (id, report_id, user_id, ordinal, title, detail,"
                         " kind, source_entry_ids) VALUES (?, 'r1', ?, ?, 't', 'd', 'shipped', ?)",
                         (store.new_id(), DEV, ordinal, ids))
    report = store.load_report(conn, DEV, "r1")
    contract.validate("report", report)
    assert [h["source_entry_ids"] for h in report["highlights"]] == [["e1"], ["e2"]]
    assert report["counts"] == {"notches": 2, "projects": 0, "milestones": 0}


@pytest.mark.parametrize("columns", [
    {"themes": '["#Shipped"]'},
    {"range_start": "2026-09-27", "range_end": "2026-09-21"},
])
def test_report_checks_reject_inconsistent_rows(conn, columns):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _add_report(conn, **columns)


def test_list_projects_counts_complete_entries_and_floors_share(conn, add_project, add_entry):
    add_project("pa", "Atlas")
    add_project("pb", "Billing")
    add_project("pz", "Zeta")
    add_project("pe", "Empty")
    for i in range(3):
        add_entry(f"a{i}", project_id="pa")
    add_entry("b0", project_id="pb")
    add_entry("z0", project_id="pz")
    add_entry("loose")                                                   # unassigned still counts in the total
    add_entry("pending", project_id="pa", analysis_state="pending",
              summary=None, mood=None, raw_text=None, word_count=0)     # not a notch yet

    projects = store.list_projects(conn, DEV)

    contract.validate("project_list", {"projects": projects})
    assert [(p["name"], p["notch_count"], p["share"]) for p in projects] == [
        ("Atlas", 3, 50), ("Billing", 1, 16), ("Zeta", 1, 16), ("Empty", 0, 0)]
