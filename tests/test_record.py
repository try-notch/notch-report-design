"""
record_routes.py over HTTP: the entry list's keyset pages, an edit's absent-versus-null
keys and its wait for the analysis, a delete that takes the stored audio with it, a
takeaways rewrite that writes nothing, and a discarded report.
"""

import base64
import json
import os

import pytest

from notch_api import analysis, store
from notch_api.config import DEV_USER_ID as DEV
from notch_api.fakes import TEXT_MARKER
from notch_api.openrouter import ModelRefused, ModelUnavailable
from tests.wire import ok, refused

OTHER = "00000000-0000-4000-8000-000000000002"


def _upload(api, entry_id, text="Shipped the login page today."):
    meta = {"id": entry_id, "recorded_at": "2026-09-21T17:30:00Z", "duration_seconds": 12,
            "mode": "daily", "catch_up_span": None}
    return ok(api.post("/v1/entries", data={"meta": json.dumps(meta)},
                       files={"audio": ("notch.m4a", TEXT_MARKER + text.encode(), "audio/mp4")}), 202, "entry_accepted")


def _page(api, **params):
    return ok(api.get("/v1/entries", params=params), 200, "entry_list")


def _rows(conn, table, entry_id):
    return conn.execute(f"SELECT count(*) FROM {table} WHERE entry_id = ?", (entry_id,)).fetchone()[0]


# ---------------------------------------------------------------------------
# GET /v1/entries
# ---------------------------------------------------------------------------

def test_the_list_pages_newest_first_and_a_capture_between_pages_shifts_nothing(api, add_user, add_entry):
    add_user(OTHER)
    add_entry("theirs", "2026-09-23", user_id=OTHER)
    add_entry("old", "2026-09-20")
    for entry_id in ("a", "b", "c"):  # one second: the id breaks the tie
        add_entry(entry_id, "2026-09-21T17:30:00Z")
    add_entry("pending", "2026-09-22T09:00:00Z", analysis_state="pending")  # every state is listed

    first = _page(api, limit=2)
    assert [e["id"] for e in first["entries"]] == ["pending", "c"]
    assert (first["matched"], first["total"]) == (5, 5)

    add_entry("newest", "2026-09-24")  # an offset page would now serve "c" twice
    second = _page(api, limit=2, cursor=first["next_cursor"])
    third = _page(api, limit=2, cursor=second["next_cursor"])
    assert [e["id"] for e in second["entries"] + third["entries"]] == ["b", "a", "old"]
    assert third["next_cursor"] is None and third["total"] == 6


@pytest.mark.parametrize("count, more", [(100, False), (101, True)])
def test_a_full_last_page_has_no_cursor_to_an_empty_one(api, add_entry, count, more):
    for i in range(count):
        add_entry(f"e{i:03d}", f"2026-09-21T10:{i // 60:02d}:{i % 60:02d}Z")
    page = _page(api)
    assert len(page["entries"]) == 100 and (page["next_cursor"] is not None) == more


@pytest.mark.parametrize("params", [
    {"limit": 0},
    {"limit": 101},
    {"cursor": "not a cursor!"},
    {"cursor": base64.urlsafe_b64encode(b'{"r": "yesterday", "i": "e1"}').decode()},
    {"cursor": ""},
])
def test_a_page_request_this_server_did_not_mint_is_invalid(api, params):
    refused(api.get("/v1/entries", params=params), 400, "invalid_request")


# ---------------------------------------------------------------------------
# PATCH /v1/entries/{id}
# ---------------------------------------------------------------------------

def test_an_edit_changes_only_the_keys_it_sends_and_a_null_project_unassigns(api, add_project, add_entry):
    add_project("p1", "Atlas")
    add_project("p2", "Billing")
    add_entry("e1", project_id="p1", tags=["shipped"])

    tagged = ok(api.patch("/v1/entries/e1", json={"tags": ["#Flaky Tests", "shipped", "Shipped"]}), 200, "entry")
    assert tagged["tags"] == ["flaky-tests", "shipped"]
    assert (tagged["project_id"], tagged["takeaways"]) == ("p1", ["Pairing made the tests go faster."])

    moved = ok(api.patch("/v1/entries/e1", json={"project_id": "p2", "is_milestone": True,
                                                 "takeaways": ["  One.  ", "", "Two."]}), 200, "entry")
    assert (moved["project_id"], moved["project"], moved["is_milestone"]) == ("p2", "Billing", True)
    assert moved["takeaways"] == ["One.", "Two."] and moved["tags"] == ["flaky-tests", "shipped"]

    unassigned = ok(api.patch("/v1/entries/e1", json={"project_id": None}), 200, "entry")
    assert (unassigned["project_id"], unassigned["project"], unassigned["is_milestone"]) == (None, None, True)
    assert ok(api.get("/v1/entries/e1"), 200, "entry") == unassigned


def test_a_corrected_transcript_rederives_the_word_count_and_is_not_reanalysed(api, conn, fake_client, add_entry):
    add_entry("e1")
    raw = conn.execute("SELECT raw_text FROM entries WHERE id = 'e1'").fetchone()[0]
    long_text = "word " * 14_000  # ~70 KB, an 80-minute recording: over the 64 KB cap other bodies get
    entry = ok(api.patch("/v1/entries/e1", json={"transcript": f"  {long_text}  "}), 200, "entry")
    assert (entry["transcript"], entry["word_count"]) == (long_text.strip(), 14_000)
    assert conn.execute("SELECT raw_text FROM entries WHERE id = 'e1'").fetchone()[0] == raw  # the Original stays
    assert fake_client.calls == []


@pytest.mark.parametrize("body, status, code", [
    ({"project_id": "theirs"}, 404, "not_found"),       # another user's project is not there
    ({"tags": ["Atlas"]}, 400, "invalid_request"),      # a project's handle is not a tag
    ({"tags": ["Wins"]}, 400, "invalid_request"),       # nor is a report category
    ({"tags": ["#"]}, 400, "invalid_request"),          # normalises to nothing
    ({"transcript": "   "}, 400, "invalid_request"),    # a complete notch needs a word
    ({"is_milestone": "yes"}, 400, "invalid_request"),
    ({"projectID": "p1"}, 400, "invalid_request"),      # a misspelt key is not silently "unchanged"
])
def test_a_refused_edit_changes_nothing(api, add_user, add_project, add_entry, body, status, code):
    add_user(OTHER)
    add_project("theirs", "Borealis", user_id=OTHER)
    add_project("p1", "Atlas")
    add_entry("e1", project_id="p1", tags=["shipped"])
    before = ok(api.get("/v1/entries/e1"), 200, "entry")
    refused(api.patch("/v1/entries/e1", json=body), status, code)
    assert ok(api.get("/v1/entries/e1"), 200, "entry") == before


@pytest.mark.parametrize("state, code, status", [
    ("analyzing", None, 409),                 # the job's apply_analysis would overwrite the edit
    ("failed", "model_unavailable", 200),     # nothing left to overwrite it
])
def test_an_edit_waits_for_the_analysis_to_finish(api, add_entry, state, code, status):
    add_entry("e1", analysis_state=state, analysis_failure_code=code)
    response = api.patch("/v1/entries/e1", json={"is_milestone": True})
    if status == 409:
        assert refused(response, 409, "entry_processing")["error"]["retryable"] is True
    else:
        assert ok(response, 200, "entry")["is_milestone"] is True


@pytest.mark.parametrize("method, path, kwargs", [
    ("patch", "/v1/entries/theirs", {"json": {"is_milestone": True}}),
    ("delete", "/v1/entries/theirs", {}),
    ("post", "/v1/entries/theirs/takeaways", {"json": {"transcript": "Shipped it."}}),
    ("delete", "/v1/reports/theirs", {}),
])
def test_another_users_entry_or_report_is_not_there(api, conn, add_user, add_entry, method, path, kwargs):
    add_user(OTHER)
    add_entry("theirs", user_id=OTHER)
    with conn:
        conn.execute("INSERT INTO reports (id, user_id, range_start, range_end, range_label) "
                     "VALUES ('theirs', ?, '2026-09-21', '2026-09-27', 'Sep 21 – 27')", (OTHER,))
    refused(api.request(method, path, **kwargs), 404, "not_found")
    assert tuple(conn.execute("SELECT (SELECT is_milestone FROM entries WHERE id = 'theirs'),"
                              " (SELECT count(*) FROM reports WHERE id = 'theirs')").fetchone()) == (0, 1)


# ---------------------------------------------------------------------------
# DELETE /v1/entries/{id}
# ---------------------------------------------------------------------------

def test_a_delete_removes_the_stored_audio_and_every_row_but_reports_keep_the_id(api, conn, audio_dir):
    _upload(api, "e1")
    _upload(api, "e2", "Paired on the review with Dana.")
    ok(api.post("/v1/reports", json={"id": "r1", "type": "week", "range_start": "2026-09-21",
                                     "range_end": "2026-09-27", "range_label": "Sep 21 – Sep 27"}), 202, "report_accepted")

    response = api.delete("/v1/entries/e1")
    assert response.status_code == 204 and response.content == b""
    assert not os.path.exists(os.path.join(audio_dir, DEV, "e1"))
    assert os.path.exists(os.path.join(audio_dir, DEV, "e2", "000"))  # only that entry's audio
    assert [_rows(conn, t, "e1") for t in ("capture_jobs", "audio_objects")] == [0, 0]
    refused(api.get("/v1/entries/e1"), 404, "not_found")
    assert ok(api.get("/v1/reports/r1"), 200, "report")["source_entry_ids"] == ["e1", "e2"]  # frozen
    refused(api.delete("/v1/entries/e1"), 404, "not_found")


# ---------------------------------------------------------------------------
# POST /v1/entries/{id}/takeaways
# ---------------------------------------------------------------------------

def test_takeaways_are_rewritten_by_the_capture_call_and_nothing_is_saved(api, fake_client, add_project, add_entry):
    add_project("p1", "Front-End Refactor")
    add_entry("e1", tags=["shipped"])
    before = ok(api.get("/v1/entries/e1"), 200, "entry")
    text = "Paired on the Front-End Refactor. Shipped the review fixes."

    written = ok(api.post("/v1/entries/e1/takeaways", json={"transcript": text}), 200, "takeaways")
    assert written["takeaways"] == ["Paired on the Front-End Refactor.", "Shipped the review fixes."]
    # The fake tags the project it hears first; like a capture, the project is not kept as a tag.
    assert "front-end-refactor" not in written["tags"] and {"shipped", "pairing"} <= set(written["tags"])
    (method, call), = fake_client.calls
    assert (method, call["tool_name"], call["system"]) == ("tool_call", "label_entry", analysis.SYSTEM_PROMPT)
    assert "- Front-End Refactor" in call["user"] and call["user"].endswith("shipped")  # projects, vocabulary
    assert ok(api.get("/v1/entries/e1"), 200, "entry") == before


@pytest.mark.parametrize("knob, status, code, retryable", [
    ({"fail_with": ModelUnavailable("HTTP 503")}, 503, "model_unavailable", True),
    ({"fail_with": ModelRefused("HTTP 400")}, 502, "model_refused", False),
    ({"overrides": {"label_entry": {"takeaways": ["   "]}}}, 502, "model_refused", False),  # never a blank draft
])
def test_a_rewrite_the_model_cannot_give_is_the_envelope(api, fake_client, add_entry, knob, status, code, retryable):
    add_entry("e1")
    for name, value in knob.items():
        setattr(fake_client, name, value)
    body = refused(api.post("/v1/entries/e1/takeaways", json={"transcript": "Shipped it."}), status, code)
    assert body["error"]["retryable"] is retryable


# ---------------------------------------------------------------------------
# DELETE /v1/reports/{id}
# ---------------------------------------------------------------------------

def test_a_discarded_report_takes_its_highlights_and_job_with_it(api, conn, add_entry):
    add_entry("a")
    job_id = ok(api.post("/v1/reports", json={"id": "r1", "type": "week", "range_start": "2026-09-21",
                                              "range_end": "2026-09-27", "range_label": "Sep 21 – Sep 27"}),
                202, "report_accepted")["job_id"]
    assert conn.execute("SELECT count(*) FROM report_highlights").fetchone()[0] > 0

    response = api.delete("/v1/reports/r1")
    assert response.status_code == 204 and response.content == b""
    assert conn.execute("SELECT (SELECT count(*) FROM report_highlights) + (SELECT count(*) FROM report_jobs)"
                        ).fetchone()[0] == 0
    refused(api.get("/v1/reports/r1"), 404, "not_found")
    refused(api.get(f"/v1/jobs/{job_id}"), 404, "not_found")
    assert ok(api.get("/v1/reports"), 200, "report_list")["reports"] == []
    assert store.load_entry(conn, DEV, "a") is not None  # the notches it read stay
    refused(api.delete("/v1/reports/r1"), 404, "not_found")
