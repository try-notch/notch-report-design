"""
The server's per-call model log (notch_api/metrics.py, hooked into OpenRouterClient._post
and the worker's job tagging): one line per HTTP attempt, never a body, never in the way.
"""

import contextlib
import json
import threading

import httpx
import pytest

from notch_api import metrics, openrouter, worker
from notch_api.fakes import FakeClient, fake_transcode
from notch_api.openrouter import ModelRefused, ModelUnavailable, OpenRouterClient

WORDS = "the quarterly launch slipped again"  # stands in for a prompt, a transcript and a reply
KEY = "sk-or-v1-test-key"
TOOL = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def _client(*responses, max_attempts=5):
    queue = list(responses)

    def handler(request):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return OpenRouterClient(KEY, http=httpx.Client(transport=httpx.MockTransport(handler)),
                            max_attempts=max_attempts, backoff=0.0)


def _completion(**extra):
    return httpx.Response(200, json={"choices": [{"message": {"content": WORDS, "tool_calls": [
        {"type": "function", "function": {"name": "t", "arguments": '{"ok": true}'}}]}}]} | extra)


@pytest.fixture
def log(tmp_path, monkeypatch):
    """Turns metrics on for the test; log() -> (the events, the raw file text)."""
    path = tmp_path / "phone-metrics.jsonl"
    monkeypatch.setattr(metrics, "path", str(path))
    monkeypatch.setattr(openrouter.time, "sleep", lambda seconds: None)

    def read():
        text = path.read_text() if path.exists() else ""
        return [json.loads(line) for line in text.splitlines()], text
    return read


def test_each_attempt_is_one_line_with_its_status_and_usage_and_no_text(log):
    client = _client(httpx.Response(503, json={"error": {"message": WORDS}}),
                     _completion(usage={"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150,
                                        "cost": 0.0041, "prompt_tokens_details": {"cached_tokens": 0}}))
    client.tool_call(system=WORDS, user=WORDS, tool_name="t", description=WORDS, parameters=TOOL)

    (failed, answered), text = log()
    assert (failed["status"], failed["ok"], failed["attempt"], failed["usage"]) == (503, False, 1, None)
    assert (answered["status"], answered["ok"], answered["attempt"]) == (200, True, 2)
    assert answered["usage"] == {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150, "cost": 0.0041}
    assert (answered["kind"], answered["tool"], answered["job"], answered["job_id"]) == ("chat", "t", None, None)
    assert answered["model"] == openrouter.CHAT_MODEL
    assert answered["latency_ms"] >= 0 and answered["ts"] > 0
    assert WORDS not in text and KEY not in text


def test_a_timeout_and_a_network_error_are_recorded_and_the_call_still_fails_the_same_way(log):
    client = _client(httpx.ReadTimeout("slow"), httpx.ConnectError("refused"), max_attempts=2)
    with pytest.raises(ModelUnavailable):
        client.decide({"journal_entry": WORDS}, {})

    events, _ = log()
    assert [(e["kind"], e["status"], e["ok"], e["attempt"]) for e in events] == [
        ("classify", "timeout", False, 1), ("classify", "error", False, 2)]


CALLS = {
    "chat": lambda client: client.tool_call(system=WORDS, user=WORDS, tool_name="t", description="d", parameters=TOOL),
    "stt": lambda client: client.transcribe(b"audio"),
    "tts": lambda client: client.speech(WORDS),
}


@pytest.mark.parametrize("kind, response, ok", [
    ("chat", httpx.Response(200, json={"error": {"code": 502, "message": WORDS}}), False),
    ("chat", httpx.Response(200, json={"choices": [{"finish_reason": "error", "error": {"code": 502}}]}), False),
    ("stt", httpx.Response(200, text="<html>not json</html>"), False),
    ("tts", httpx.Response(200, content=b"ID3mp3", headers={"Content-Type": "audio/mpeg"}), True),
], ids=["in-band-error", "error-in-choice", "unparsable", "raw-audio"])
def test_ok_means_the_attempt_gave_a_usable_answer(log, kind, response, ok):
    with contextlib.suppress(ModelUnavailable):
        CALLS[kind](_client(response, max_attempts=1))
    (event,), _ = log()
    assert (event["kind"], event["status"], event["ok"]) == (kind, 200, ok)


def test_a_recorder_that_cannot_write_changes_nothing_about_the_call(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "path", str(tmp_path / "no-such-dir" / "metrics.jsonl"))
    client = _client(httpx.Response(200, json={"text": " hello there "}),
                     httpx.Response(400, json={"error": {"message": "bad audio"}}))
    assert client.transcribe(b"audio") == "hello there"
    with pytest.raises(ModelRefused, match="HTTP 400"):
        client.transcribe(b"audio")
    assert not (tmp_path / "no-such-dir").exists()


def test_a_jobs_calls_are_tagged_even_on_the_thread_analysis_starts_for_jev(db_path, audio_dir, capture):
    seen = []

    class Watching(FakeClient):
        def _record(self, method, **kwargs):
            seen.append((method, metrics.job.get(), threading.current_thread().name))
            super()._record(method, **kwargs)

    job_id = capture("C7E4A1B9-2F3D-4B8E-9A61-3D5E7F9A1B2C", b"audio")
    runner = worker.JobRunner(db_path, client=Watching(), transcode=fake_transcode, audio_dir=audio_dir, inline=True)
    runner.submit_capture(job_id)

    assert {method for method, _, _ in seen} == {"transcribe", "decide", "tool_call"}
    assert all(tags == {"job": "capture", "job_id": job_id} for _, tags, _ in seen)
    decide_thread = next(thread for method, _, thread in seen if method == "decide")
    assert decide_thread != threading.current_thread().name  # Jev runs on analysis.py's own pool
    assert metrics.job.get() is None  # nothing leaks past the job
