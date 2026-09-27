"""
privacy.py: the outermost Guard (request ids, one allowlisted line per request, the 500 it
answers itself), the scrubbed formatter, and the logging it installs. The end-to-end proof
is test_no_content_at_rest.py; these pin the pieces.
"""

import io
import json
import logging

import pytest
from fastapi.testclient import TestClient

from notch_api import privacy
from notch_api.fakes import fake_recording
from tests.v2kit import Harness, ok


@pytest.fixture
def lines():
    """Scrubbed lines, as the server writes them, for the duration of a test."""
    stream, root = io.StringIO(), logging.getLogger()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(privacy.ScrubbedFormatter())
    root.addHandler(handler)
    level, root.level = root.level, logging.DEBUG
    yield lambda: [json.loads(line) for line in stream.getvalue().splitlines()]
    root.removeHandler(handler)
    root.setLevel(level)


def test_every_request_gets_an_id_and_one_allowlisted_line(tmp_path, lines):
    with Harness(tmp_path) as v2:
        response = v2.transcribe(fake_recording("Shipped the thing today."))
        ok(response, "transcribe")
    request_id = response.headers["x-request-id"]
    (line,) = [l for l in lines() if l.get("event") == "request"]
    assert line["request_id"] == request_id and line["route"] == "/v2/transcribe" and line["status"] == 200
    assert (line["kind"], line["attempt"], line["chunks"], line["platform"], line["app_version"]) == (
        "transcribe", 1, 1, "ios", "1.0.0")
    assert line["model"] == "openai/whisper-large-v3" and line["provider"] == "DeepInfra"
    assert set(line) <= {"ts", "level", "logger", *privacy.ALLOWED}


def test_the_line_names_the_route_template_never_the_path(tmp_path, lines):
    with Harness(tmp_path) as v2:
        v2.http.get("/v2/cloud/changes?since=12", headers=v2.headers())
        v2.http.get("/v2/no-such-route/8f14e45f", headers=v2.headers())
    routes = [l["route"] for l in lines() if l.get("event") == "request"]
    assert routes == ["/v2/cloud/changes", "unmatched"]
    assert not [l for l in lines() if "8f14e45f" in json.dumps(l) or "since" in json.dumps(l)]


def test_a_refusal_is_logged_with_its_code(tmp_path, lines):
    with Harness(tmp_path) as v2:
        v2.http.get("/v2/config")
    (line,) = [l for l in lines() if l.get("event") == "request"]
    assert (line["status"], line["error_code"]) == (400, "invalid_request")


def _crashing_app(after_start=False):
    """A raw ASGI app that raises, before or after it has started its response."""
    async def app(scope, receive, send):
        if after_start:
            await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("a message that must never be written")

    return privacy.Guard(app)


def test_the_guard_answers_a_crash_with_a_bare_500_and_logs_only_its_type(lines):
    response = TestClient(_crashing_app()).get("/boom")
    assert response.status_code == 500
    assert response.json() == {"error": {"code": "internal_error", "message": "Something went wrong on the server.",
                                         "retryable": False}}
    assert response.headers["x-request-id"]
    crash = [l for l in lines() if l.get("event") == "unhandled_exception"]
    assert crash and crash[0]["exc_type"] == "RuntimeError" and crash[0]["frames"]
    assert "never be written" not in json.dumps(lines())
    (request,) = [l for l in lines() if l.get("event") == "request"]
    assert request["status"] == 500


def test_a_crash_after_the_response_started_is_only_logged(lines):
    assert TestClient(_crashing_app(after_start=True)).get("/anything").status_code == 200
    assert [l["exc_type"] for l in lines() if l.get("event") == "unhandled_exception"] == ["RuntimeError"]


def test_a_crash_inside_a_route_is_the_envelope_and_a_scrubbed_line(tmp_path, lines, monkeypatch):
    from notch_api import v2 as v2_module

    def crash(*args, **kwargs):
        raise RuntimeError("a message that must never be written")

    monkeypatch.setattr(v2_module, "report_facts", crash)
    with Harness(tmp_path) as v2:
        response = v2.post("/v2/reports", {"type": "week", "range_start": "2026-09-21", "range_end": "2026-09-21",
                                           "range_label": "x", "entries": [{
                                               "id": "e", "date": "2026-09-21", "project_name": None, "tags": [],
                                               "categories": [], "is_milestone": False, "summary": "s",
                                               "takeaways": [], "impact_note": None, "acknowledged_by": None}]})
    assert response.status_code == 500 and response.json()["error"]["code"] == "internal_error"
    assert "never be written" not in json.dumps(lines())
    assert [l["exc_type"] for l in lines() if l.get("event") == "unhandled_exception"] == ["RuntimeError"]


def test_the_formatter_writes_templates_never_arguments_and_nothing_from_other_libraries():
    formatter = privacy.ScrubbedFormatter()

    def line(name, msg, *args, **extra):
        record = logging.LogRecord(name, logging.WARNING, "f.py", 1, msg, args, None)
        record.__dict__.update(extra)
        return json.loads(formatter.format(record))

    assert line("notch_api.x", "job %s failed: %s", "id", "said something")["event"] == "job %s failed: %s"
    assert line("somelib", "anything at all")["event"] == "log"
    assert line("notch_api.x", "café %s", "x")["event"] == "log"   # not a plain ASCII template
    fields = line("notch_api.request", "request", notch={"route": "/v2/x", "provider": "Deep Infra",
                                                         "model": "a/b", "bogus": 1, "kind": "text\nwith newline"})
    assert fields["route"] == "/v2/x" and fields["provider"] == "Deep Infra"
    assert "bogus" not in fields and "kind" not in fields


def test_install_logging_scrubs_root_and_uvicorn_and_silences_the_access_log():
    saved = {name: (list(logging.getLogger(name).handlers), logging.getLogger(name).propagate,
                    logging.getLogger(name).disabled, logging.getLogger(name).level)
             for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore", "httpx2")}
    try:
        handler = privacy.install_logging(stream=io.StringIO())
        for name in ("", "uvicorn", "uvicorn.error"):
            assert logging.getLogger(name).handlers == [handler]
        assert isinstance(handler.formatter, privacy.ScrubbedFormatter)
        assert logging.getLogger("uvicorn.access").disabled
        assert all(logging.getLogger(n).level == logging.WARNING for n in ("httpx", "httpcore", "httpx2"))
    finally:
        for name, (handlers, propagate, disabled, level) in saved.items():
            logger = logging.getLogger(name)
            logger.handlers, logger.propagate, logger.disabled, logger.level = handlers, propagate, disabled, level
