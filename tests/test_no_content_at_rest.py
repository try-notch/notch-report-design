"""
No content at rest: a canary phrase goes through every /v2 path, success and failure, and
must come out nowhere but the responses themselves.

The phrase rides in the audio (the transcript the speech-to-text mock returns, and the
recording's own metadata, which the real ffmpeg reads and prints to its stderr), the
transcripts, project names and vocabulary, a report's entries, author and labels, the
provider's error bodies (HTTP errors, in-band errors, refusals, unusable replies), an
exception raised inside a route, one raised outside the route's own handling, every
header, the query string and malformed bodies. The server runs the real OpenRouterClient
over httpx.MockTransport, the real ffmpeg, the real meter and the scrubbed logging.

Afterwards the canary must be absent from:
  - every log line every logger wrote, as formatted, and every record's message and args;
  - the process's stdout and stderr (capfd, which sees subprocesses too);
  - the temp root, which must be empty after every request;
  - the meter's SQLite file, its WAL and its shared-memory file;
  - every error envelope.
"""

import base64
import glob
import json
import logging
import os
import re
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient

from notch_api import analysis, privacy, v2 as v2_module
from notch_api.app import create_app
from notch_api.auth import Verifier
from notch_api.fakes import FakeApple, FakeSupabaseAdmin
from notch_api.openrouter import OpenRouterClient
from notch_api.services import Services
from notch_api.speech import FFmpeg
from notch_api.zdr import ZdrAuditor
from tests.v2kit import IOS, SUPABASE_URL, JWKSStub, SigningKey, WallClock, key, token

CANARY = "purple elephant quarterly review"
PATTERN = re.compile(r"purple\W*elephant|quarterly\W*review", re.I)
USER = "77777777-7777-4777-8777-777777777777"


class FakeOpenRouter:
    """OpenRouter over MockTransport: answers carry the canary, and `failing` picks a failure to answer with."""

    def __init__(self):
        self.failing = None
        self.seen = []

    def __call__(self, request):
        path = request.url.path
        self.seen.append(path)
        if path.endswith("/generation"):
            return httpx.Response(200, json={"data": {"provider_name": "DeepInfra", "model": "openai/whisper-large-v3"}})
        if path.endswith("/endpoints/zdr"):
            return httpx.Response(200, json={"data": [{"provider_name": "DeepInfra",
                                                       "model_id": "openai/whisper-large-v3"}]})
        failure = self.failing
        if failure == "http_400":
            return httpx.Response(400, json={"error": {"message": f"Rejected because {CANARY}"}})
        if failure == "http_500":
            return httpx.Response(500, text=f"upstream said {CANARY}")
        if failure == "in_band":
            return httpx.Response(200, json={"error": {"code": 400, "message": f"provider: {CANARY}"}})
        if failure == "unusable":
            return httpx.Response(200, json={"choices": [{"message": {"content": CANARY}, "finish_reason": "stop"}],
                                             "provider": "DeepInfra"})
        body = json.loads(request.content)
        if path.endswith("/audio/transcriptions"):
            text = "" if failure == "silence" else f"Today I said {CANARY} twice."
            return httpx.Response(200, json={"text": text, "usage": {"seconds": 3.0, "cost": 0.0001}},
                                  headers={"X-Generation-Id": "gen-stt"})
        tool = body["tool_choice"]["function"]["name"]
        if tool == "label_entry":
            arguments = {"tags": ["shipped", "pairing"], "summary": f"You mentioned {CANARY}.",
                         "takeaways": [f"The {CANARY} went well."], "impact_note": CANARY, "acknowledged_by": "",
                         "fixed_tags": ["wins"], "mood": "up",
                         "project_match": {"project_name": "", "confidence": "none"}}
        else:
            ids = re.findall(r"\[id ([^\]]+)\]", body["messages"][1]["content"])
            arguments = {"headline": f"The {CANARY}", "lede": CANARY, "body": CANARY, "themes": ["a", "b", "c"],
                         "highlights": [{"title": CANARY, "detail": CANARY, "kind": "note", "source_entry_ids": ids},
                                        {"title": "Two", "detail": "", "kind": "shipped", "source_entry_ids": []}]}
        return httpx.Response(200, json={
            "id": "gen-chat", "model": "deepseek/deepseek-v4-pro-0813", "provider": "DeepInfra",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0002},
            "choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
                {"function": {"name": tool, "arguments": json.dumps(arguments)}}]}}]})


@pytest.fixture
def logs():
    """Every record at DEBUG, formatted by the server's scrubbed formatter, plus the raw records."""
    root, saved = logging.getLogger(), {}
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpx2", "httpcore"):
        logger = logging.getLogger(name)
        saved[name] = (list(logger.handlers), logger.level, logger.propagate, logger.disabled)
    formatted, raw = [], []

    class Keep(logging.Handler):
        def emit(self, record):
            raw.append(record)
            formatted.append(privacy.ScrubbedFormatter().format(record))

    handler = privacy.install_logging(level=logging.DEBUG, stream=open(os.devnull, "w"))
    keep = Keep()
    for name in ("", "uvicorn", "uvicorn.error"):
        logging.getLogger(name).addHandler(keep)
    yield formatted, raw
    handler.stream.close()
    for name, (handlers, level, propagate, disabled) in saved.items():
        logger = logging.getLogger(name)
        logger.handlers, logger.level, logger.propagate, logger.disabled = handlers, level, propagate, disabled
    root.setLevel(saved[""][1])


@pytest.fixture
def server(tmp_path, logs):
    openrouter = FakeOpenRouter()
    client = OpenRouterClient("test-key", http=httpx.Client(transport=httpx.MockTransport(openrouter)),
                              backoff=0.0, max_attempts=2)
    signing = SigningKey("canary-kid")
    tmp_root = str(tmp_path / "tmpfs")
    services = Services.build(meter_db=str(tmp_path / "meter.db"), tmp_root=tmp_root, client=client, audio=FFmpeg(),
                              verifier=Verifier(SUPABASE_URL, http=JWKSStub(signing).http, cooldown=0),
                              clock=WallClock(), apple=FakeApple(), supabase_admin=FakeSupabaseAdmin())
    services.zdr = ZdrAuditor(client, services.meter, services.remote, inline=True, waits=(0,),
                              sleep=lambda seconds: None)
    with TestClient(create_app(services=services, v1=False)) as http:
        yield http, openrouter, services, tmp_root, signing


def _recording(path, seconds=3):
    """A real m4a whose own metadata carries the canary: ffmpeg prints it on stderr while decoding."""
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={seconds}:sample_rate=16000", "-metadata", f"title={CANARY}",
                    "-metadata", f"comment={CANARY}", "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "32k",
                    "-f", "mp4", "-y", path], check=True, capture_output=True, timeout=60)
    with open(path, "rb") as f:
        return f.read()


def test_the_canary_is_nowhere_but_the_responses(server, logs, capfd, tmp_path, monkeypatch):
    http, openrouter, services, tmp_root, signing = server
    bearer = f"Bearer {token(signing, sub=USER)}"
    audio = _recording(str(tmp_path / "canary.m4a"))
    assert CANARY.encode() in audio   # the metadata really is in the upload
    answers = []

    def call(method, path, *, json_body=None, content=None, headers=None, expect):
        base = {"X-Client": IOS, "Authorization": bearer}
        response = http.request(method, path, json=json_body, content=content, headers={**base, **(headers or {})})
        answers.append((method, path, response))
        assert response.status_code == expect, (path, response.status_code, response.text)
        assert os.listdir(tmp_root) == [], "the temp root is empty after every request"
        if response.status_code >= 400 and response.content:
            assert not PATTERN.search(response.text), "an error envelope never echoes input"
        return response

    def audio_headers(**extra):
        return {"Idempotency-Key": key(), "Content-Type": "audio/mp4", "X-Notch-Mode": "daily",
                "X-Notch-Duration": "3", **extra}

    labelled = {"transcript": f"I said {CANARY}.", "project_names": [CANARY.title()], "vocabulary": [CANARY]}
    report = {"type": "week", "range_start": "2026-09-21", "range_end": "2026-09-27", "range_label": CANARY,
              "scope": {"project_name": CANARY, "tag": "purple-elephant"},
              "author": {"display_name": CANARY, "role": CANARY, "industry": CANARY, "years_experience": CANARY},
              "project_names": [CANARY],
              "entries": [{"id": "e1", "date": "2026-09-22", "project_name": CANARY, "tags": ["purple-elephant"],
                           "categories": ["wins"], "is_milestone": True, "summary": CANARY, "takeaways": [CANARY],
                           "impact_note": CANARY, "acknowledged_by": CANARY, "transcript": CANARY}]}

    # --- success, every route ----------------------------------------------------
    assert CANARY in call("POST", "/v2/transcribe", content=audio, headers=audio_headers(), expect=200).text
    assert CANARY in call("POST", "/v2/analyze", json_body=labelled, headers={"Idempotency-Key": key()},
                          expect=200).text
    call("POST", "/v2/takeaways", json_body=labelled, headers={"Idempotency-Key": key()}, expect=200)
    assert CANARY in call("POST", "/v2/reports", json_body=report, headers={"Idempotency-Key": key()},
                          expect=200).text
    call("GET", "/v2/config", expect=200)
    ciphertext = base64.b64encode(os.urandom(64)).decode()
    call("PUT", "/v2/cloud/records", json_body={"records": [{"id": key(), "deleted": False, "ciphertext": ciphertext,
                                                             "key_id": "0123456789abcdef"}]}, expect=200)
    call("GET", "/v2/cloud/changes", expect=200)

    # --- the provider fails, saying the canary -----------------------------------
    for failing, expect in (("http_400", 422), ("http_500", 502), ("in_band", 422), ("unusable", 422)):
        openrouter.failing = failing
        call("POST", "/v2/analyze", json_body=labelled, headers={"Idempotency-Key": key()}, expect=expect)
        call("POST", "/v2/reports", json_body=report, headers={"Idempotency-Key": key()}, expect=expect)
    openrouter.failing = "http_400"
    call("POST", "/v2/transcribe", content=audio, headers=audio_headers(), expect=422)
    openrouter.failing = "silence"
    call("POST", "/v2/transcribe", content=audio, headers=audio_headers(), expect=422)
    openrouter.failing = None

    # --- an exception inside a route, and one outside its own handling --------------
    def crash(*args, **kwargs):
        raise RuntimeError(f"crashed on {CANARY}")

    monkeypatch.setattr(analysis, "analyze_text", crash)
    call("POST", "/v2/analyze", json_body=labelled, headers={"Idempotency-Key": key()}, expect=500)
    monkeypatch.setattr(v2_module, "report_facts", crash)
    call("POST", "/v2/reports", json_body=report, headers={"Idempotency-Key": key()}, expect=500)
    monkeypatch.undo()

    # --- the canary in every header, the query string and malformed bodies --------
    call("POST", "/v2/analyze", json_body=labelled, headers={"Idempotency-Key": CANARY}, expect=400)
    call("POST", "/v2/analyze", json_body=labelled, headers={"Idempotency-Key": key(), "X-Client": CANARY},
         expect=400)
    call("POST", "/v2/analyze", json_body=labelled, headers={"Idempotency-Key": key(), "X-Notch-Locale": CANARY},
         expect=400)
    call("POST", "/v2/analyze", json_body=labelled, headers={"Idempotency-Key": key(), "Authorization": CANARY},
         expect=401)
    call("POST", "/v2/analyze", json_body=labelled,
         headers={"Idempotency-Key": key(), "Authorization": f"Bearer {CANARY}"}, expect=401)
    for header in ("X-Notch-Mode", "X-Notch-Duration"):
        call("POST", "/v2/transcribe", content=audio, headers=audio_headers(**{header: CANARY}), expect=400)
    call("POST", "/v2/transcribe", content=audio, headers=audio_headers(**{"Content-Type": f"audio/{CANARY}"}),
         expect=415)
    call("POST", "/v2/transcribe", content=CANARY.encode() * 50, headers=audio_headers(), expect=422)
    call("GET", f"/v2/cloud/changes?since={CANARY}", expect=400)
    call("POST", "/v2/analyze", json_body={CANARY: CANARY}, headers={"Idempotency-Key": key()}, expect=400)
    call("POST", "/v2/analyze", json_body={"transcript": [CANARY]}, headers={"Idempotency-Key": key()}, expect=400)
    call("POST", "/v2/analyze", content=f'{{"transcript": "{CANARY}"'.encode(), headers={"Idempotency-Key": key()},
         expect=400)
    call("POST", "/v2/reports", json_body={**report, "type": CANARY}, headers={"Idempotency-Key": key()}, expect=400)
    call("PUT", "/v2/cloud/records", json_body={"records": [{"id": key(), "deleted": False, "ciphertext": ciphertext,
                                                             "key_id": CANARY}]}, expect=400)
    call("PUT", "/v2/cloud/keycheck", json_body={"key_id": "0123456789abcdef", "verifier": CANARY}, expect=400)
    call("DELETE", "/v2/account", json_body={"apple_authorization_code": CANARY}, expect=400)
    call("GET", f"/v2/{CANARY.replace(' ', '-')}", expect=404)

    # --- the account's own deletion, last --------------------------------------------
    call("DELETE", "/v2/account", expect=204)

    # --- where the canary must not be ------------------------------------------------
    formatted, raw = logs
    assert formatted, "the loggers were captured"
    leaks = [line for line in formatted if PATTERN.search(line)]
    assert leaks == [], leaks[:3]
    for record in raw:
        assert not PATTERN.search(record.getMessage()), record.getMessage()
        assert not PATTERN.search(repr(record.args)), record.args
    requests = [line for line in formatted if '"event": "request"' in line]
    assert len(requests) == len(answers), "one line per request"
    assert all('"route": "/v2/' in line or '"route": "unmatched"' in line for line in requests)
    out, err = capfd.readouterr()
    assert not PATTERN.search(out) and not PATTERN.search(err)
    assert os.listdir(tmp_root) == []
    stored = b"".join(open(path, "rb").read() for path in glob.glob(services.meter.path + "*"))
    assert stored and not PATTERN.search(stored.decode("latin-1"))
    assert "/v1/entries" not in openrouter.seen  # no /v1 in this server at all
