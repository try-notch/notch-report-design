"""
The model-facing edge: OpenRouterClient's retry and parsing rules (over httpx's
MockTransport), the real ffmpeg transcode, and the two things the offline double
must keep doing for an offline run to prove anything.
"""

import base64
import json
import subprocess

import httpx
import jsonschema
import pytest

from notch_api import analysis, audio, openrouter
from notch_api.config import (CHAT_MODEL, DECISIONS_URL, JEV_MODEL, MAX_UPLOAD_BYTES, STT_MODEL, TTS_MODEL,
                               TTS_VOICE)
from notch_api.fakes import GHOST_ID, FakeClient
from notch_api.openrouter import (ModelRefused, ModelUnavailable, OpenRouterClient,
                                  TranscriptionFailed)

TOOL = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def _client(*responses, max_attempts=5, backoff=0.0):
    """A client whose transport answers with `responses` in order. Returns (client, requests)."""
    queue, requests = list(responses), []

    def handler(request):
        requests.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenRouterClient("sk-test", http=http, max_attempts=max_attempts, backoff=backoff), requests


def _completion(arguments):
    return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [
        {"type": "function", "function": {"name": "t", "arguments": arguments}}]}}]})


def _call(client, **kwargs):
    return client.tool_call(system="sys", user="usr", tool_name="t", description="d", parameters=TOOL, **kwargs)


@pytest.fixture
def sleeps(monkeypatch):
    waits = []
    monkeypatch.setattr(openrouter.time, "sleep", waits.append)
    return waits


# ---------------------------------------------------------------------------
# tool_call
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("arguments", ['{"ok": true}', {"ok": True}])
def test_tool_call_forces_the_tool_and_returns_its_arguments(arguments):
    client, requests = _client(_completion(arguments))
    assert _call(client) == {"ok": True}

    (request,) = requests
    body = json.loads(request.content)
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer sk-test"
    assert request.headers["X-Title"] == "notch-report-design"
    assert body["model"] == CHAT_MODEL
    assert body["tool_choice"] == {"type": "function", "function": {"name": "t"}}
    assert body["tools"][0]["function"]["parameters"] == TOOL
    assert body["provider"] == {"require_parameters": True}
    assert body["reasoning"] == {"enabled": False}  # with reasoning on, the forced call is skipped
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


def test_retries_429_and_503_then_succeeds(sleeps):
    client, requests = _client(
        httpx.Response(429, headers={"Retry-After": "2"}, json={"error": {"message": "slow down"}}),
        httpx.Response(503, text="upstream down"),
        _completion('{"ok": true}'),
        backoff=0.5,
    )
    assert _call(client) == {"ok": True}
    assert len(requests) == 3
    assert sleeps == [2.0, 1.0]  # Retry-After honoured, then backoff * 2**1


def test_retries_timeouts_and_in_band_provider_errors(sleeps):
    client, requests = _client(
        httpx.ConnectTimeout("connect"),
        httpx.ReadTimeout("read"),
        httpx.DecodingError("bad gzip"),
        httpx.Response(200, json={"error": {"code": 502, "message": "provider returned error"}}),
        # A provider that fails partway through generating: the error is inside the choice.
        httpx.Response(200, json={"choices": [{"finish_reason": "error", "error": {"code": 502, "message": "x"}}]}),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json=[1, 2]),
        _completion('{"ok": true}'),
        max_attempts=8,
    )
    assert _call(client) == {"ok": True}
    assert len(requests) == 8


def test_gives_up_with_model_unavailable(sleeps):
    client, requests = _client(*[httpx.Response(503) for _ in range(3)], max_attempts=3)
    with pytest.raises(ModelUnavailable) as exc:
        _call(client)
    assert len(requests) == 3 and len(sleeps) == 2  # no pointless sleep after the last try
    assert exc.value.code == "model_unavailable" and exc.value.retryable


@pytest.mark.parametrize("response", [
    httpx.Response(400, json={"error": {"message": "bad tool schema"}}),
    httpx.Response(200, json={"error": {"code": 400, "message": "bad request"}}),
])
def test_a_4xx_is_a_refusal_and_not_retried(response):
    client, requests = _client(response)
    with pytest.raises(ModelRefused) as exc:
        _call(client)
    assert len(requests) == 1
    assert exc.value.code == "model_refused" and not exc.value.retryable


@pytest.mark.parametrize("unusable, why", [
    (httpx.Response(200, json={"choices": [{"message": {"content": "Sure! Here are the labels."}}]}), "did not call"),
    (_completion("{not json"), "not valid JSON"),
    (_completion("[1, 2]"), "not an object"),
    (httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"tool_calls": [
        {"type": "function", "function": {"name": "t", "arguments": '{"ok": tr'}}]}}]}), "cut off"),
], ids=["no-tool-call", "invalid-json", "not-an-object", "cut-off"])
def test_a_reply_without_a_usable_call_is_asked_once_more_then_refused(unusable, why, sleeps):
    client, requests = _client(unusable, _completion('{"ok": true}'))
    assert _call(client) == {"ok": True}

    client, requests = _client(unusable, unusable)
    with pytest.raises(ModelRefused, match=why):
        _call(client)
    assert len(requests) == 2 and sleeps == []


def test_an_answer_the_caller_refuses_is_asked_again_warmer_then_refused_saying_who_answered(sleeps):
    def parse(arguments):
        if not arguments["ok"]:
            raise ModelRefused("not ok")
        return "used"

    no = httpx.Response(200, json={"provider": "Baidu", "choices": [{"finish_reason": "tool_calls", "message": {
        "tool_calls": [{"type": "function", "function": {"name": "t", "arguments": '{"ok": false}'}}]}}]})
    client, requests = _client(no, _completion('{"ok": true}'))
    assert _call(client, parse=parse) == "used"
    # At temperature 0 the same miss would come straight back.
    assert [json.loads(r.content)["temperature"] for r in requests] == [0.0, 0.4]

    client, _ = _client(no, no)
    with pytest.raises(ModelRefused, match=r"^not ok \(finish_reason=tool_calls, provider=Baidu\)$"):
        _call(client, parse=parse)


# ---------------------------------------------------------------------------
# transcribe / speech / from_env
# ---------------------------------------------------------------------------

def test_transcribe_sends_base64_audio_and_returns_the_text():
    client, requests = _client(httpx.Response(200, json={"text": "  Shipped it today.  "}))
    assert client.transcribe(b"RIFFwav", fmt="wav") == "Shipped it today."

    body = json.loads(requests[0].content)
    assert requests[0].url.path == "/api/v1/audio/transcriptions"
    assert body["model"] == STT_MODEL and body["language"] == "en"
    assert body["input_audio"]["format"] == "wav"
    assert base64.b64decode(body["input_audio"]["data"]) == b"RIFFwav"


def test_silence_is_transcription_failed():
    client, _ = _client(httpx.Response(200, json={"text": " \n "}))
    with pytest.raises(TranscriptionFailed, match="No speech detected.") as exc:
        client.transcribe(b"RIFF")
    assert exc.value.code == "transcription_failed"


def test_audio_over_the_request_cap_is_refused_without_being_sent():
    client, requests = _client()
    with pytest.raises(ModelRefused, match="cap"):
        client.transcribe(bytes(MAX_UPLOAD_BYTES * 3 // 4 + 3))  # base64 makes it 4 bytes over
    assert requests == []


def test_speech_returns_the_audio_bytes_and_reads_a_json_answer_as_an_error(sleeps):
    client, requests = _client(
        httpx.Response(200, json={"error": {"code": 502, "message": "provider returned error"}}),
        httpx.Response(200, content=b"ID3mp3bytes", headers={"Content-Type": "audio/mpeg"}),
    )
    assert client.speech("hello") == b"ID3mp3bytes"  # the JSON error was retried, not saved as audio
    body = json.loads(requests[0].content)
    assert requests[0].url.path == "/api/v1/audio/speech"
    assert body == {"model": TTS_MODEL, "input": "hello", "voice": TTS_VOICE, "response_format": "mp3"}

    client, _ = _client(httpx.Response(200, json={"id": "not audio"}))
    with pytest.raises(ModelRefused, match="Expected audio"):
        client.speech("hello")


def test_decide_posts_the_questions_to_the_alpha_endpoint_and_returns_the_answers():
    answers = {"wins": {"type": "noul", "noul": 0.93}}
    client, requests = _client(httpx.Response(200, json={"answers": answers, "usage": {"cost": 0.00001}}))
    questions = {"wins": {"type": "noul", "instructions": "i", "criteria": {"true": "t", "false": "f"}}}
    assert client.decide({"journal_entry": "Shipped it."}, questions) == answers

    assert str(requests[0].url) == DECISIONS_URL == "https://openrouter.ai/api/alpha/decisions"
    assert requests[0].headers["Authorization"] == "Bearer sk-test"
    assert json.loads(requests[0].content) == {"model": JEV_MODEL, "state": {"journal_entry": "Shipped it."},
                                               "questions": questions}
    client, _ = _client(httpx.Response(200, json={"usage": {}}))
    with pytest.raises(ModelRefused):
        client.decide({}, questions)


def test_from_env_needs_a_key(monkeypatch):
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        OpenRouterClient.from_env()
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-from-env")
    assert isinstance(OpenRouterClient.from_env(), OpenRouterClient)


# ---------------------------------------------------------------------------
# audio.to_m4a_16k — real ffmpeg
# ---------------------------------------------------------------------------

def test_to_m4a_16k_turns_the_ios_recording_format_into_16k_mono_aac(tmp_path):
    m4a = tmp_path / "tone.m4a"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1", "-ac", "1", "-ar", "44100",
                    "-c:a", "aac", str(m4a)], check=True)
    out = tmp_path / "out.m4a"
    out.write_bytes(audio.to_m4a_16k(m4a.read_bytes()))

    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,sample_rate,channels:format=duration",
         "-of", "json", str(out)], capture_output=True, check=True).stdout)
    stream, = probe["streams"]
    assert (stream["codec_name"], stream["sample_rate"], stream["channels"]) == ("aac", "16000", 1)
    assert abs(float(probe["format"]["duration"]) - 1) < 0.1


@pytest.mark.parametrize("data", [b"", b"this is not audio at all"])
def test_undecodable_audio_is_audio_unreadable(data):
    with pytest.raises(audio.AudioUnreadable) as exc:
        audio.to_m4a_16k(data)
    assert exc.value.code == "audio_unreadable"


# ---------------------------------------------------------------------------
# The offline double: what it must keep doing for the offline runs to mean anything
# ---------------------------------------------------------------------------

def test_the_fake_refuses_a_tool_schema_it_no_longer_fits():
    drifted = analysis.LABEL_ENTRY | {"required": [*analysis.LABEL_ENTRY["required"], "confidence_score"]}
    with pytest.raises(jsonschema.ValidationError, match="confidence_score"):
        FakeClient().tool_call(system="s", user="Label this entry:\n\nHello.", tool_name="label_entry",
                               description="d", parameters=drifted)


def test_the_fake_report_always_cites_a_ghost_id():
    """So every offline report run exercises reports._clean's id filter."""
    report = FakeClient().tool_call(system="s", user="[id seed-01] ...", tool_name="write_report",
                                    description="d", parameters={"type": "object"})
    assert GHOST_ID in {i for h in report["highlights"] for i in h["source_entry_ids"]}
