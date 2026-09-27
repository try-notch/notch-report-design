"""
Shared fixtures. Every test runs offline: the OpenRouter key is removed from the
environment and httpx's real network transport refuses to send, so a test that
forgets to inject a fake fails loudly instead of spending money.

Factories (`add_user`, `add_project`, `add_entry`, `capture`) insert rows straight
into the schema through the `conn` fixture and commit, so the rows are visible to any
other connection on the same file (the app's, a job's).
"""

import os
from datetime import datetime, timezone

import httpx
import pytest

from notch_api import config, store
from notch_api.fakes import FakeClient, fake_transcode as _fake_transcode

DEV = config.DEV_USER_ID


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def refuse(self, request):
        raise RuntimeError(f"tests are offline; refused {request.method} {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)


@pytest.fixture
def db_path(tmp_path):
    """A fresh database file with the schema applied and the dev user present."""
    path = str(tmp_path / "notch_api.db")
    store.init_db(path)
    conn = store.connect(path)
    store.ensure_dev_user(conn)
    conn.close()
    return path


@pytest.fixture
def audio_dir(tmp_path):
    path = tmp_path / "audio"
    path.mkdir()
    return str(path)


@pytest.fixture
def conn(db_path):
    conn = store.connect(db_path)
    yield conn
    conn.close()


@pytest.fixture
def fake_client():
    return FakeClient()


@pytest.fixture
def fake_transcode():
    return _fake_transcode


@pytest.fixture
def add_user(conn):
    """add_user(user_id) — a second tenant, for cross-user tests."""
    def add(user_id):
        with conn:
            conn.execute("INSERT INTO users (id) VALUES (?)", (user_id,))
        return user_id
    return add


@pytest.fixture
def add_project(conn):
    """add_project(project_id, name, user_id=DEV) -> project_id."""
    def add(project_id, name, user_id=DEV):
        with conn:
            conn.execute("INSERT INTO projects (id, user_id, name) VALUES (?, ?, ?)",
                         (project_id, user_id, name))
        return project_id
    return add


@pytest.fixture
def add_entry(conn):
    """
    add_entry(entry_id, recorded_at="2026-09-21", *, tags=(), categories=(), project_id=None,
              is_milestone=False, user_id=DEV, **columns) -> entry_id

    Inserts a COMPLETE entry (transcript, summary, takeaways, mood all set) — the kind
    reports count. `recorded_at` may be a date, meaning 17:30 UTC that day, or a full
    instant. Tags go in verbatim, so the schema's normalisation CHECK still applies.
    Any other entries column can be set through **columns (e.g. analysis_state='pending').
    """
    def add(entry_id, recorded_at="2026-09-21", *, tags=(), categories=(), project_id=None,
            is_milestone=False, user_id=DEV, **columns):
        if len(recorded_at) == 10:
            recorded_at += "T17:30:00Z"
        transcript = f"Shipped part of the refactor today and paired with Dana on the tests ({entry_id})."
        row = {
            "id": entry_id, "user_id": user_id, "recorded_at": recorded_at,
            "duration_seconds": 42.0, "raw_text": transcript, "word_count": len(transcript.split()),
            "summary": "You shipped part of the refactor.",
            "takeaways": store.json_dump(["Pairing made the tests go faster."]),
            "mood": "up", "tags": store.json_dump(list(tags)),
            "categories": store.json_dump(list(categories)), "project_id": project_id,
            "is_milestone": int(is_milestone), "analysis_state": "complete",
        } | columns
        with conn:
            conn.execute(f"INSERT INTO entries ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                         list(row.values()))
        return entry_id
    return add


@pytest.fixture
def capture(conn, audio_dir):
    """
    capture(entry_id, audio, state="queued") -> job_id. What app.py leaves behind after a
    202: a pending entry, its capture job, an audio_objects row and the file at
    <audio_dir>/<storage_key>. state="failed" leaves the job and the entry failed
    model_unavailable instead.
    """
    def add(entry_id, audio, state="queued"):
        job_id, key = store.new_id(), f"{DEV}/{entry_id}/000"
        code = "model_unavailable" if state == "failed" else None
        with conn:
            conn.execute("INSERT INTO entries (id, user_id, recorded_at, duration_seconds, analysis_state, "
                         "analysis_failure_code) VALUES (?, ?, '2026-09-21T17:30:00Z', 24, ?, ?)",
                         (entry_id, DEV, "failed" if code else "pending", code))
            conn.execute("INSERT INTO capture_jobs (id, user_id, entry_id, state, failure_code) VALUES (?, ?, ?, ?, ?)",
                         (job_id, DEV, entry_id, state, code))
            conn.execute("INSERT INTO audio_objects (id, user_id, capture_job_id, entry_id, storage_key, byte_size)"
                         " VALUES (?, ?, ?, ?, ?, ?)", (store.new_id(), DEV, job_id, entry_id, key, len(audio)))
        path = os.path.join(audio_dir, key)
        os.makedirs(os.path.dirname(path))
        with open(path, "wb") as f:
            f.write(audio)
        return job_id
    return add


class Clock:
    """The app's clock (create_app's `clock`). A test moves it by setting `.now`."""

    def __init__(self):
        self.now = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)  # a Friday

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def api(db_path, audio_dir, fake_client, fake_transcode, clock):
    """
    A TestClient over create_app with the fakes, inline jobs and the test's `clock`,
    authenticated as the dev user. Imported lazily: app.py is built after this file.
    """
    from fastapi.testclient import TestClient

    from notch_api.app import create_app

    app = create_app(db_path=db_path, audio_dir=audio_dir, client=fake_client,
                     transcode=fake_transcode, inline_jobs=True, clock=clock)
    with TestClient(app, headers={"Authorization": f"Bearer {config.DEV_TOKEN}"}) as client:
        yield client
