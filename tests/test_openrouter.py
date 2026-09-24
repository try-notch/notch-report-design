"""
The model-facing edge: OpenRouterClient's retry and parsing rules (over httpx's
MockTransport), the real ffmpeg transcode, and the offline doubles that stand in
for both everywhere else.
"""

import base64
import io
import json
import subprocess
import wave

import httpx
import jsonschema
import pytest

from notch_api import audio, openrouter
from notch_api.config import CHAT_MODEL, DECISIONS_URL, JEV_MODEL, STT_MODEL, TTS_MODEL, TTS_VOICE
from notch_api.fakes import GHOST_ID, TEXT_MARKER, FakeClient, fake_transcode, parse_label_message
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


def _call(client):
    return client.tool_call(system="sys", user="usr", tool_name="t", description="d", parameters=TOOL)


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
        httpx.Response(200, json={"error": {"code": 502, "message": "provider returned error"}}),
        httpx.Response(200, text="<html>not json</html>"),
        _completion('{"ok": true}'),
    )
    assert _call(client) == {"ok": True}
    assert len(requests) == 5


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


@pytest.mark.parametrize("unusable", [
    httpx.Response(200, json={"choices": [{"message": {"content": "Sure! Here are the labels."}}]}),
    _completion("{not json"),
    _completion("[1, 2]"),
], ids=["no-tool-call", "invalid-json", "not-an-object"])
def test_a_reply_without_a_usable_call_is_asked_once_more_then_refused(unusable, sleeps):
    client, requests = _client(unusable, _completion('{"ok": true}'))
    assert _call(client) == {"ok": True}

    client, requests = _client(unusable, unusable)
    with pytest.raises(ModelRefused):
        _call(client)
    assert len(requests) == 2 and sleeps == []


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
# audio.to_wav_16k — real ffmpeg
# ---------------------------------------------------------------------------

def test_to_wav_16k_turns_the_ios_recording_format_into_16k_mono_wav(tmp_path):
    m4a = tmp_path / "tone.m4a"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1", "-ac", "1", "-ar", "44100",
                    "-c:a", "aac", str(m4a)], check=True)

    wav_bytes = audio.to_wav_16k(m4a.read_bytes())

    assert wav_bytes[:4] == b"RIFF" and wav_bytes[8:12] == b"WAVE"
    with wave.open(io.BytesIO(wav_bytes)) as wav:  # also proves the header sizes are real
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
        assert abs(wav.getnframes() - 16000) < 800


@pytest.mark.parametrize("data", [b"", b"this is not audio at all"])
def test_undecodable_audio_is_audio_unreadable(data):
    with pytest.raises(audio.AudioUnreadable) as exc:
        audio.to_wav_16k(data)
    assert exc.value.code == "audio_unreadable"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

# The label_entry tool as the spec defines it; the fake's canned answer must fit.
LABEL_ENTRY = {
    "type": "object",
    "properties": {
        "fixed_tags": {"type": "array", "items": {"enum": ["wins", "collaboration", "leadership",
                                                           "growth", "challenges"]}},
        "tags": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 5},
        "summary": {"type": "string"},
        "takeaways": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3},
        "mood": {"enum": ["up", "flat", "down"]},
        "impact_note": {"type": "string"},
        "acknowledged_by": {"type": "string"},
        "project_match": {"type": "object", "properties": {
            "project_name": {"type": "string"}, "confidence": {"enum": ["high", "low", "none"]}},
            "required": ["project_name", "confidence"], "additionalProperties": False},
    },
    "required": ["fixed_tags", "tags", "summary", "takeaways", "mood", "impact_note",
                 "acknowledged_by", "project_match"],
    "additionalProperties": False,
}


def _label(client, user, parameters=LABEL_ENTRY):
    return client.tool_call(system="s", user=user, tool_name="label_entry", description="d",
                            parameters=parameters)


@pytest.mark.parametrize("context", [
    "Active projects:\n- Data Platform\n- Front-End Refactor\n\nTags in use (reuse verbatim): shipped",
    "Projects: Data Platform; Front-End Refactor\nTags in use: shipped, pairing",
    "Your projects (copy a name verbatim):\nData Platform\nFront-End Refactor\nExisting tags:\nshipped",
])
def test_parse_label_message_reads_the_project_list_in_any_layout(context):
    user = "Label this entry:\n\nWorked on the front-end refactor.\n\n" + context
    transcript, projects = parse_label_message(user)
    assert transcript == "Worked on the front-end refactor."
    assert projects == ["Data Platform", "Front-End Refactor"]


def test_fake_label_matches_a_named_project_and_fits_the_tool_schema():
    user = ("Label this entry:\n\nShipped the Front-End Refactor login page with Dana.\n\n"
            "Active projects:\n- Front-End Refactor")
    label = _label(FakeClient(), user)
    assert label["project_match"] == {"project_name": "Front-End Refactor", "confidence": "high"}
    assert label["tags"][0] == "front-end-refactor"
    assert {"wins", "collaboration"} <= set(label["fixed_tags"])
    # A narrower label_entry (the writing call's) gets only the fields it asks for.
    narrow = LABEL_ENTRY | {"properties": {k: LABEL_ENTRY["properties"][k] for k in ("tags", "summary")},
                            "required": ["tags", "summary"]}
    assert set(_label(FakeClient(), user, narrow)) == {"tags", "summary"}


def test_fake_decide_answers_in_jevs_shapes():
    questions = {"wins": {"type": "noul"}, "leadership": {"type": "noul"},
                 "mood": {"type": "choice", "criteria": {"up": "", "flat": "", "down": ""}},
                 "project": {"type": "choice", "criteria": {"Atlas": "", "Front-End Refactor": "", "none": ""}}}
    answers = FakeClient().decide({"journal_entry": "Shipped the front-end refactor."}, questions)
    assert answers["wins"]["noul"] > 0.5 > answers["leadership"]["noul"]
    assert (answers["mood"]["choice"], answers["project"]["choice"]) == ("up", "Front-End Refactor")
    unmatched = FakeClient().decide({"journal_entry": "Planning all day."}, questions)
    assert unmatched["project"]["choice"] == "none"


def test_fake_label_refuses_a_schema_it_no_longer_fits():
    drifted = LABEL_ENTRY | {"required": LABEL_ENTRY["required"] + ["confidence_score"]}
    with pytest.raises(jsonschema.ValidationError, match="confidence_score"):
        _label(FakeClient(), "Label this entry:\n\nHello.", drifted)


def test_fake_overrides_let_a_test_feed_messy_model_output():
    client = FakeClient(overrides={"label_entry": {"fixed_tags": ["wins", "made-up"], "impact_note": " "}})
    label = _label(client, "Label this entry:\n\nShipped it.")
    assert label["fixed_tags"] == ["wins", "made-up"] and label["impact_note"] == " "


def test_fake_report_cites_the_first_two_ids_plus_a_ghost():
    user = "FACTS...\n[id seed-01] 2026-09-21 — ...\n[id seed-02] 2026-09-22 — ...\n[id seed-03] ..."
    report = FakeClient().tool_call(system="s", user=user, tool_name="write_report", description="d",
                                    parameters={"type": "object"})
    cited = [h["source_entry_ids"] for h in report["highlights"]]
    assert cited == [["seed-01", GHOST_ID], ["seed-02"], [GHOST_ID]]
    assert 3 <= len(report["themes"]) <= 5 and "\n\n" in report["body"]


def test_fake_transcribe_reads_the_marker_and_fails_on_silence():
    client = FakeClient()
    assert client.transcribe(TEXT_MARKER + "Fixed the flaky test.".encode()) == "Fixed the flaky test."
    assert "Front-End Refactor" in client.transcribe(b"\x00\x01 real audio")
    with pytest.raises(TranscriptionFailed):
        client.transcribe(TEXT_MARKER + b"   ")


def test_fake_fail_with_raises_from_every_call_and_records_it():
    client = FakeClient(fail_with=ModelUnavailable("down"))
    with pytest.raises(ModelUnavailable):
        client.transcribe(b"x")
    with pytest.raises(ModelUnavailable):
        _label(client, "Label this entry:\n\nx")
    with pytest.raises(ModelUnavailable):
        client.decide({}, {})
    assert [method for method, _ in client.calls] == ["transcribe", "tool_call", "decide"]
    client.fail_with = None
    assert client.transcribe(TEXT_MARKER + b"back") == "back"


def test_fake_transcode_passes_bytes_through_and_rejects_empty():
    assert fake_transcode(TEXT_MARKER + b"hi") == TEXT_MARKER + b"hi"
    with pytest.raises(audio.AudioUnreadable):
        fake_transcode(b"")
