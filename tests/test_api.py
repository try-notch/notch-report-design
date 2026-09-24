"""
app.py and worker.py: the HTTP surface the phone talks to, end to end over the fakes.

Jobs run inline, so a 202 comes back with its job already finished and the next GET
shows the outcome. Every body a test reads is validated against its contract kind,
error bodies included, because "every non-2xx is the envelope" is itself a contract.
"""

import json
import os

import pytest
from fastapi.testclient import TestClient

from notch_api import app as app_module, config, contract, store, worker
from notch_api.app import create_app
from notch_api.config import DEV_USER_ID as DEV
from notch_api.fakes import TEXT_MARKER, FakeClient, fake_transcode
from notch_api.openrouter import ModelUnavailable, TranscriptionFailed
from notch_api.reports import accept_report

OTHER = "00000000-0000-4000-8000-000000000002"
SPOKEN = "Shipped the Front-End Refactor login page with Dana today."
AUDIO = TEXT_MARKER + SPOKEN.encode()


def _meta(**fields):
    return {"id": "e1", "recorded_at": "2026-09-21T17:30:00Z", "duration_seconds": 24,
            "mode": "daily", "catch_up_span": None} | fields


def _upload(api, audio=AUDIO, meta=None, **fields):
    meta = json.dumps(_meta(**fields)) if meta is None else meta
    return api.post("/v1/entries", files={"audio": ("notch.m4a", audio, "audio/mp4")}, data={"meta": meta})


def _report(api, **fields):
    return api.post("/v1/reports", json={"id": "r1", "type": "week", "range_start": "2026-09-21",
                                         "range_end": "2026-09-27", "range_label": "Sep 21 – Sep 27"} | fields)


def _ok(response, status, kind):
    assert response.status_code == status, response.text
    body = response.json()
    contract.validate(kind, body)
    return body


def _refused(response, status, code):
    assert response.status_code == status, response.text
    body = response.json()
    contract.validate("error", body)
    assert body["error"]["code"] == code
    return body


def _count(conn, table):
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _stored_files(audio_dir):
    return [os.path.join(d, f) for d, _, files in os.walk(audio_dir) for f in files]


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

def test_an_upload_is_stored_analysed_and_served_as_a_valid_entry(api, conn, audio_dir, add_project):
    add_project("p1", "Front-End Refactor")
    accepted = _ok(_upload(api, recorded_at="2026-09-21T19:30:00+02:00"), 202, "entry_accepted")
    assert accepted["entry_id"] == "e1" and accepted["job_id"] != "e1"  # D4: two namespaces

    with open(os.path.join(audio_dir, DEV, "e1", "000"), "rb") as f:
        assert f.read() == AUDIO
    purge_after = conn.execute("SELECT purge_after FROM audio_objects WHERE entry_id = 'e1'").fetchone()[0]

    job = _ok(api.get(f"/v1/jobs/{accepted['job_id']}"), 200, "job")
    assert job["status"] == "complete"
    entry = job["entry"]
    assert entry["recorded_at"] == "2026-09-21T17:30:00Z"  # an offset instant is stored as UTC
    assert entry["transcript"] == SPOKEN and entry["word_count"] == len(SPOKEN.split())
    assert (entry["project_id"], entry["project"]) == ("p1", "Front-End Refactor")
    assert entry["retryable_until"] == purge_after
    assert _ok(api.get("/v1/entries/e1"), 200, "entry") == entry
    assert _ok(api.get("/v1/projects"), 200, "project_list")["projects"] == [
        {"id": "p1", "name": "Front-End Refactor", "notch_count": 1, "share": 100}]


def test_a_catch_up_carries_its_span_onto_the_entry(api):
    span = {"start": "2026-09-19", "end": "2026-09-21"}
    _ok(_upload(api, mode="catch_up", catch_up_span=span), 202, "entry_accepted")
    entry = _ok(api.get("/v1/entries/e1"), 200, "entry")
    assert (entry["mode"], entry["catch_up_span"]) == ("catch_up", span)


def test_a_repeated_upload_returns_the_same_job_and_stores_nothing_new(api, conn, audio_dir, fake_client):
    first = _ok(_upload(api), 202, "entry_accepted")
    calls = len(fake_client.calls)
    again = _ok(_upload(api, audio=TEXT_MARKER + b"Different words.", duration_seconds=99), 202, "entry_accepted")
    assert again == first
    assert len(fake_client.calls) == calls
    assert [_count(conn, t) for t in ("entries", "capture_jobs", "audio_objects")] == [1, 1, 1]
    with open(os.path.join(audio_dir, DEV, "e1", "000"), "rb") as f:
        assert f.read() == AUDIO
    assert _ok(api.get("/v1/entries/e1"), 200, "entry")["transcript"] == SPOKEN


def test_an_entry_id_owned_by_another_user_is_a_conflict_not_their_job(api, conn, audio_dir, add_user, add_entry):
    add_user(OTHER)
    add_entry("e1", user_id=OTHER)
    with conn:
        conn.execute("INSERT INTO capture_jobs (id, user_id, entry_id) VALUES ('their-job', ?, 'e1')", (OTHER,))
    body = _refused(_upload(api), 409, "conflict")
    assert "their-job" not in json.dumps(body)
    assert _count(conn, "capture_jobs") == 1 and _stored_files(audio_dir) == []


@pytest.mark.parametrize("mode, span", [
    ("daily", {"start": "2026-09-19", "end": "2026-09-21"}),
    ("catch_up", None),
    ("catch_up", {"start": "2026-09-21", "end": "2026-09-19"}),
])
def test_a_span_that_disagrees_with_the_mode_is_invalid_span(api, conn, mode, span):
    _refused(_upload(api, mode=mode, catch_up_span=span), 400, "invalid_span")
    assert _count(conn, "entries") == 0


@pytest.mark.parametrize("meta", [
    "not json",
    json.dumps(_meta(id="../../escape")),                 # ids become a path segment of the audio
    json.dumps(_meta(recorded_at="2026-09-21T17:30:00")),  # no zone
    json.dumps(_meta(duration_seconds=-1)),
    json.dumps(_meta(mode="catchUp")),                     # the Swift raw value, not the wire token
])
def test_malformed_meta_is_refused_before_anything_is_stored(api, conn, audio_dir, tmp_path, meta):
    _refused(_upload(api, meta=meta), 400, "invalid_request")
    assert _count(conn, "entries") == 0
    assert _stored_files(audio_dir) == [] and not (tmp_path / "escape").exists()


def test_the_audio_part_may_be_25_mb_and_not_a_byte_more(api, conn, audio_dir):
    _refused(_upload(api, audio=b"\0" * (config.MAX_UPLOAD_BYTES + 1)), 413, "payload_too_large")
    assert _count(conn, "entries") == 0 and _stored_files(audio_dir) == []
    _ok(_upload(api, audio=b"\0" * config.MAX_UPLOAD_BYTES), 202, "entry_accepted")


def test_a_body_without_content_length_is_cut_off_as_it_streams(api, conn, monkeypatch):
    # The audio part is within the cap; the body is not, and says nothing of its length up front.
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 1000)
    padding = b"\0" * (1000 + app_module.FORM_SLACK)
    request = api.build_request("POST", "/v1/entries", data={"meta": json.dumps(_meta())},
                                files={"audio": ("notch.m4a", AUDIO, "audio/mp4"), "extra": ("x", padding)})
    del request.headers["Content-Length"]
    _refused(api.send(request), 413, "payload_too_large")
    assert _count(conn, "entries") == 0


def test_a_failed_capture_is_a_failed_job_with_its_retry_window(api, conn, fake_client):
    fake_client.fail_with = TranscriptionFailed("No speech detected.")
    job_id = _ok(_upload(api), 202, "entry_accepted")["job_id"]
    purge_after = conn.execute("SELECT purge_after FROM audio_objects WHERE entry_id = 'e1'").fetchone()[0]

    job = _ok(api.get(f"/v1/jobs/{job_id}"), 200, "job")
    assert (job["status"], job["code"], job["retryable_until"]) == ("failed", "transcription_failed", purge_after)
    entry = _ok(api.get("/v1/entries/e1"), 200, "entry")
    assert (entry["analysis_state"], entry["analysis_failure_code"], entry["retryable_until"]) == (
        "failed", "transcription_failed", purge_after)


def test_the_app_boots_without_a_key_and_a_capture_then_fails_model_unavailable(db_path, audio_dir):
    assert "OPENROUTER_API_KEY" not in os.environ
    app = create_app(db_path=db_path, audio_dir=audio_dir, transcode=fake_transcode, inline_jobs=True)
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as api:
        assert api.get("/healthz").json() == {"ok": True}
        job_id = _ok(_upload(api), 202, "entry_accepted")["job_id"]
        job = _ok(api.get(f"/v1/jobs/{job_id}"), 200, "job")
    assert (job["status"], job["code"]) == ("failed", "model_unavailable")


# ---------------------------------------------------------------------------
# Auth and the error envelope
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("authorization", [None, "Bearer not-the-token", "Basic dev"])
def test_a_missing_or_wrong_bearer_is_401(api, authorization):
    api.headers.pop("Authorization")
    if authorization:
        api.headers["Authorization"] = authorization
    response = api.get("/v1/projects")
    _refused(response, 401, "unauthorized")
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_an_unauthenticated_upload_stores_nothing(api, conn, audio_dir):
    api.headers.pop("Authorization")
    _refused(_upload(api), 401, "unauthorized")
    assert _count(conn, "entries") == 0 and _stored_files(audio_dir) == []


@pytest.mark.parametrize("method, path, kwargs, status, code", [
    ("get", "/v1/jobs/nope", {}, 404, "not_found"),
    ("get", "/v1/entries/theirs", {}, 404, "not_found"),   # another user's entry is not there
    ("get", "/v1/reports/nope", {}, 404, "not_found"),
    ("get", "/v1/nowhere", {}, 404, "not_found"),          # Starlette's own 404
    ("delete", "/v1/projects", {}, 405, "method_not_allowed"),
    ("post", "/v1/entries", {"content": b"x", "headers": {"Content-Type": "multipart/form-data"}},
     400, "invalid_request"),                              # Starlette's multipart error
    ("post", "/v1/reports", {"content": b"{not json"}, 400, "invalid_request"),
])
def test_every_refusal_is_the_error_envelope(api, add_user, add_entry, method, path, kwargs, status, code):
    add_user(OTHER)
    add_entry("theirs", user_id=OTHER)
    _refused(api.request(method, path, **kwargs), status, code)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def test_a_new_project_keeps_the_clients_id_and_a_folded_name_returns_the_existing_one(api):
    created = _ok(api.post("/v1/projects", json={"id": "p1", "name": "Front-End Refactor"}), 201, "project")
    assert created == {"id": "p1", "name": "Front-End Refactor", "notch_count": 0, "share": 0}
    assert _ok(api.post("/v1/projects", json={"id": "p2", "name": "  front-end REFACTOR "}), 200, "project") == created
    assert _ok(api.get("/v1/projects"), 200, "project_list")["projects"] == [created]


def test_a_project_id_already_in_use_is_a_conflict(api, add_user, add_project):
    add_user(OTHER)
    add_project("theirs", "Borealis", user_id=OTHER)
    _ok(api.post("/v1/projects", json={"id": "p1", "name": "Atlas"}), 201, "project")
    _refused(api.post("/v1/projects", json={"id": "p1", "name": "Borealis"}), 409, "conflict")
    _refused(api.post("/v1/projects", json={"id": "theirs", "name": "Borealis"}), 409, "conflict")


@pytest.mark.parametrize("body", [{"id": "p1", "name": "   "}, {"name": "Atlas"}, ["p1", "Atlas"]])
def test_a_project_without_an_id_and_a_name_is_invalid(api, body):
    _refused(api.post("/v1/projects", json=body), 400, "invalid_request")


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def test_a_report_is_accepted_once_written_and_read_back(api, fake_client, add_entry):
    add_entry("a", "2026-09-21")
    add_entry("b", "2026-09-23", is_milestone=True)
    accepted = _ok(_report(api), 202, "report_accepted")
    calls = len(fake_client.calls)
    assert _ok(_report(api), 202, "report_accepted") == accepted
    assert len(fake_client.calls) == calls

    assert _ok(api.get(f"/v1/jobs/{accepted['job_id']}"), 200, "job") == {"status": "complete", "report_id": "r1"}
    report = _ok(api.get("/v1/reports/r1"), 200, "report")
    assert report["headline"] and report["source_entry_ids"] == ["a", "b"]
    assert report["counts"] == {"notches": 2, "projects": 0, "milestones": 1}
    assert {i for h in report["highlights"] for i in h["source_entry_ids"]} <= {"a", "b"}
    assert _ok(api.get("/v1/reports"), 200, "report_list") == {"reports": [
        {"id": "r1", "range_label": "Sep 21 – Sep 27", "headline": report["headline"], "type": "week",
         "generated_at": report["generated_at"]}], "next_cursor": None}


def test_a_range_with_no_notches_is_422(api):
    _refused(_report(api), 422, "empty_range")
    assert _ok(api.get("/v1/reports"), 200, "report_list")["reports"] == []


def test_a_failed_report_is_a_failed_job_with_no_retry_window(api, fake_client, add_entry):
    add_entry("a")
    fake_client.fail_with = ModelUnavailable("down")
    job_id = _ok(_report(api), 202, "report_accepted")["job_id"]
    job = _ok(api.get(f"/v1/jobs/{job_id}"), 200, "job")
    assert (job["status"], job["code"], job["retryable_until"]) == ("failed", "model_unavailable", None)
    assert _ok(api.get("/v1/reports/r1"), 200, "report")["headline"] is None


def test_reports_are_listed_newest_first_even_within_one_second(api, add_entry):
    add_entry("a")
    for report_id in ("r1", "r2", "r3"):
        _ok(_report(api, id=report_id), 202, "report_accepted")
    listed = _ok(api.get("/v1/reports"), 200, "report_list")["reports"]
    assert [r["id"] for r in listed] == ["r3", "r2", "r1"]


def test_categories_never_reach_the_wire(api, conn, add_project):
    add_project("p1", "Front-End Refactor")
    job_id = _ok(_upload(api), 202, "entry_accepted")["job_id"]
    assert store.json_list(conn.execute("SELECT categories FROM entries").fetchone()[0])  # there is something to leak
    _ok(_report(api), 202, "report_accepted")
    bodies = [api.get(path).text for path in
              (f"/v1/jobs/{job_id}", "/v1/entries/e1", "/v1/projects", "/v1/reports", "/v1/reports/r1")]
    assert not [b for b in bodies if '"categories"' in b]


# ---------------------------------------------------------------------------
# The job runner
# ---------------------------------------------------------------------------

def _left_behind(conn, audio_dir, entry_id, state="queued"):
    """What a crash leaves after a 202: a pending entry, its job in `state`, and the stored audio."""
    job_id, key = store.new_id(), f"{DEV}/{entry_id}/000"
    code = "model_unavailable" if state == "failed" else None
    with conn:
        conn.execute("INSERT INTO entries (id, user_id, recorded_at, analysis_state, analysis_failure_code) "
                     "VALUES (?, ?, '2026-09-21T17:30:00Z', ?, ?)",
                     (entry_id, DEV, "failed" if code else "pending", code))
        conn.execute("INSERT INTO capture_jobs (id, user_id, entry_id, state, failure_code, started_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (job_id, DEV, entry_id, state, code, store.now()))
        conn.execute("INSERT INTO audio_objects (id, user_id, capture_job_id, entry_id, storage_key, byte_size) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (store.new_id(), DEV, job_id, entry_id, key, len(AUDIO)))
    os.makedirs(os.path.join(audio_dir, DEV, entry_id))
    with open(os.path.join(audio_dir, key), "wb") as f:
        f.write(AUDIO)
    return job_id


def test_jobs_left_unfinished_are_resumed_on_startup_and_finished_ones_are_not(db_path, audio_dir, conn,
                                                                               add_entry):
    queued = _left_behind(conn, audio_dir, "e1")
    failed = _left_behind(conn, audio_dir, "e2", state="failed")
    add_entry("a")
    _, report_job, _ = accept_report(conn, DEV, {"id": "r1", "type": "week", "range_start": "2026-09-21",
                                                 "range_end": "2026-09-27", "range_label": "Sep 21 – Sep 27"})
    with conn:  # the process died mid-write
        conn.execute("UPDATE report_jobs SET state = 'writing' WHERE id = ?", (report_job,))

    client = FakeClient()
    app = create_app(db_path=db_path, audio_dir=audio_dir, client=client, transcode=fake_transcode, inline_jobs=True)
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as api:
        states = [_ok(api.get(f"/v1/jobs/{j}"), 200, "job")["status"] for j in (queued, report_job, failed)]
    assert states == ["complete", "complete", "failed"]
    # The capture (transcribe, then label and decide side by side) and the report (one call).
    assert sorted(method for method, _ in client.calls) == ["decide", "tool_call", "tool_call", "transcribe"]


def test_the_pooled_runner_finishes_running_jobs_before_shutdown_returns(db_path, audio_dir, conn):
    job_id = _left_behind(conn, audio_dir, "e1")
    runner = worker.JobRunner(db_path, client=FakeClient(), transcode=fake_transcode, audio_dir=audio_dir)
    runner.submit_capture(job_id)
    runner.shutdown()
    assert conn.execute("SELECT state FROM capture_jobs WHERE id = ?", (job_id,)).fetchone()[0] == "complete"
