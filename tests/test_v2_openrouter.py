"""
OpenRouterClient.bound(), the /v2 view of the model client: the request's deadline, what
each reply cost and who served it, remote config's models and provider block. Plus the
two metadata reads zdr.py makes. Everything over httpx.MockTransport, as test_openrouter.
"""

import json

import httpx
import pytest

from notch_api import openrouter
from notch_api.openrouter import (Deadline, DeadlineExceeded, ModelRefused, ModelUnavailable, OpenRouterClient,
                                  Usage)

TOOL = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
PROVIDER = {"zdr": True, "data_collection": "deny", "require_parameters": True}
MODELS = {"chat": "vendor/chat-x", "stt": "vendor/stt-x", "classifier": "vendor/jev-x"}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _client(handler):
    requests = []

    def record(request):
        requests.append(request)
        return handler(request)

    return OpenRouterClient("test-key", http=httpx.Client(transport=httpx.MockTransport(record)), backoff=0.0), requests


def _completion(**extra):
    return httpx.Response(200, json={"id": "gen-chat-1", "model": "vendor/chat-x", "provider": "DeepInfra",
                                     "usage": {"prompt_tokens": 120, "completion_tokens": 30, "cost": 0.0021},
                                     "choices": [{"message": {"tool_calls": [
                                         {"function": {"name": "t", "arguments": '{"ok": true}'}}]}}]} | extra)


def _call(client):
    return client.tool_call(system="s", user="u", tool_name="t", description="d", parameters=TOOL)


@pytest.fixture
def sleeps(monkeypatch):
    waits = []
    monkeypatch.setattr(openrouter.time, "sleep", waits.append)
    return waits


def test_a_bound_chat_call_carries_the_config_model_and_provider_block_and_meters_the_reply():
    client, requests = _client(lambda request: _completion())
    usage = Usage()
    assert _call(client.bound(usage=usage, models=MODELS, provider=PROVIDER)) == {"ok": True}
    sent = json.loads(requests[0].content)
    assert sent["model"] == "vendor/chat-x"
    assert sent["provider"] == PROVIDER
    assert usage.reached_model
    assert usage.calls == [{"kind": "chat", "model": "vendor/chat-x", "provider": "DeepInfra",
                            "generation_id": "gen-chat-1", "cost": 0.0021, "prompt_tokens": 120,
                            "completion_tokens": 30, "seconds": None}]
    assert usage.totals() == {"cost": 0.0021, "prompt_tokens": 120, "completion_tokens": 30, "seconds": None,
                              "models": ["vendor/chat-x"], "providers": ["DeepInfra"]}


def test_the_unbound_client_is_v1s():
    client, requests = _client(lambda request: _completion())
    _call(client)
    sent = json.loads(requests[0].content)
    assert sent["provider"] == {"require_parameters": True}
    assert sent["model"] == openrouter.CHAT_MODEL


def test_speech_to_text_reports_billed_seconds_and_the_generation_header():
    reply = httpx.Response(200, json={"text": "hello there", "usage": {"seconds": 9.2, "total_tokens": 113,
                                                                       "cost": 0.000508}},
                           headers={"X-Generation-Id": "gen-stt-7"})
    client, requests = _client(lambda request: reply)
    usage = Usage()
    assert client.bound(usage=usage, models=MODELS).transcribe(b"audio") == "hello there"
    assert json.loads(requests[0].content)["model"] == "vendor/stt-x"
    (call,) = usage.calls
    assert (call["kind"], call["generation_id"], call["seconds"], call["cost"]) == ("stt", "gen-stt-7", 9.2, 0.000508)
    assert call["model"] == "vendor/stt-x"  # the reply named none, so what was asked for


def test_jev_uses_the_config_classifier_model():
    client, requests = _client(lambda request: httpx.Response(200, json={"id": "gen-j", "answers": {"x": 1}}))
    usage = Usage()
    client.bound(usage=usage, models=MODELS).decide({"s": "t"}, {"x": {}})
    assert json.loads(requests[0].content)["model"] == "vendor/jev-x"
    assert usage.calls[0]["kind"] == "classify" and usage.calls[0]["generation_id"] == "gen-j"


def test_an_attempt_that_failed_still_reached_the_model(sleeps):
    client, _ = _client(lambda request: httpx.Response(503))
    usage = Usage()
    with pytest.raises(ModelUnavailable):
        _call(client.bound(usage=usage))
    assert usage.reached_model and usage.calls == []


def test_a_refusal_carries_its_status():
    client, _ = _client(lambda request: httpx.Response(404, json={"error": {"message": "No endpoints found"}}))
    with pytest.raises(ModelRefused) as refused:
        _call(client.bound())
    assert refused.value.http_status == 404


def test_each_attempt_waits_at_most_the_time_left():
    clock = Clock()
    client, requests = _client(lambda request: _completion())
    _call(client.bound(deadline=Deadline(42, clock)))
    timeout = requests[0].extensions["timeout"]
    assert timeout["read"] == pytest.approx(42) and timeout["connect"] == 15.0


def test_a_retry_is_only_made_while_it_fits_in_the_deadline(sleeps):
    clock = Clock()
    replies = iter([httpx.Response(503, headers={"Retry-After": "30"}), _completion()])
    client, requests = _client(lambda request: next(replies))
    with pytest.raises(ModelUnavailable):
        _call(client.bound(deadline=Deadline(20, clock)))  # 30 s to wait, 20 s left
    assert len(requests) == 1 and sleeps == []


def test_a_retry_that_fits_is_made(sleeps):
    clock = Clock()
    replies = iter([httpx.Response(503, headers={"Retry-After": "2"}), _completion()])
    client, requests = _client(lambda request: next(replies))
    assert _call(client.bound(deadline=Deadline(20, clock))) == {"ok": True}
    assert len(requests) == 2 and sleeps == [2.0]


def test_no_attempt_starts_once_the_deadline_is_gone():
    clock = Clock()
    deadline = Deadline(5, clock)
    clock.now += 4.5
    client, requests = _client(lambda request: _completion())
    with pytest.raises(DeadlineExceeded):
        _call(client.bound(deadline=deadline))
    assert requests == []


def test_a_timeout_as_the_deadline_runs_out_is_deadline_exceeded():
    clock = Clock()
    deadline = Deadline(10, clock)

    def slow(request):
        clock.now += 10
        raise httpx.ReadTimeout("slow", request=request)

    client, _ = _client(slow)
    with pytest.raises(DeadlineExceeded) as late:
        _call(client.bound(deadline=deadline))
    assert late.value.code == "deadline_exceeded" and late.value.retryable


def test_usage_after_settling_goes_to_the_late_hook():
    usage, late = Usage(), []
    usage.add({"kind": "chat", "cost": 0.01})
    usage.late(late.append)
    usage.add({"kind": "chat", "cost": 0.02})
    assert late == [{"kind": "chat", "cost": 0.02}]
    assert usage.totals()["cost"] == pytest.approx(0.03)


def test_generation_names_the_provider_and_is_none_until_recorded():
    answers = iter([httpx.Response(404, json={"error": {"message": "not found"}}),
                    httpx.Response(200, json={"data": {"id": "gen-1", "model": "openai/whisper-large-v3",
                                                       "provider_name": "Groq", "total_cost": 0.001}})])
    client, requests = _client(lambda request: next(answers))
    assert client.generation("gen-1") is None
    assert client.generation("gen-1") == {"provider": "Groq", "model": "openai/whisper-large-v3"}
    assert requests[0].url.path == "/api/v1/generation" and requests[0].url.params["id"] == "gen-1"
    assert requests[0].method == "GET"


def test_generation_lookups_that_fail_are_model_unavailable():
    client, _ = _client(lambda request: httpx.Response(500))
    with pytest.raises(ModelUnavailable):
        client.generation("gen-1")


def test_the_zdr_list_reads_provider_and_model_from_either_field():
    body = {"data": [
        {"name": "Groq | openai/whisper-large-v3", "model_id": "openai/whisper-large-v3", "provider_name": "Groq"},
        {"name": "TypeSafe | typesafe/jev-1.13-20260917"},
        {"provider_name": "DeepInfra", "model_name": "DeepSeek V4 Pro"},
        "not an endpoint",
    ]}
    client, requests = _client(lambda request: httpx.Response(200, json=body))
    assert client.zdr_endpoints() == [{"provider": "Groq", "model": "openai/whisper-large-v3"},
                                      {"provider": "TypeSafe", "model": "typesafe/jev-1.13-20260917"},
                                      {"provider": "DeepInfra", "model": "DeepSeek V4 Pro"}]
    assert requests[0].url.path == "/api/v1/endpoints/zdr"


def test_a_zdr_list_without_data_is_model_unavailable():
    client, _ = _client(lambda request: httpx.Response(200, json={"nope": []}))
    with pytest.raises(ModelUnavailable):
        client.zdr_endpoints()
