"""
The /v2 processing routes over HTTP, on the fakes: header rules, auth and accounts, the
config call, transcribe (the raw body, decoding, splitting, the temp root), analyze,
takeaways and reports, the error envelope with its codes and Retry-After, deadlines,
closed enums per contract version, and every quota boundary as the phone would meet it.
Notch Cloud, account deletion and the ZDR audit have files of their own.
"""

import asyncio
import os

import httpx
import pytest

from notch_api import wire_v2
from notch_api.fakes import GHOST_ID, FakeClient, fake_recording
from notch_api.openrouter import ModelRefused, ModelUnavailable
from tests.v2kit import IOS, OTHER, USER, Harness, key, ok, refused

SPOKEN = "Paired with Dana on the Front-End Refactor and we shipped the checkout form after fixing flaky tests."


@pytest.fixture
def v2(tmp_path):
    with Harness(tmp_path) as harness:
        yield harness


def harness(tmp_path, **kwargs):
    return Harness(tmp_path, **kwargs)


def usage_rows(v2, kind=None):
    rows = v2.rows("SELECT * FROM usage_events ORDER BY id")
    return [r for r in rows if kind is None or r["kind"] == kind]


# ---------------------------------------------------------------------------
# Headers, auth, accounts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("client", [None, "", "ios/1.0+1", "web/1.0.0+1", "ios/1.0.0", "ios/1.0.0+1234567",
                                    "IOS/1.0.0+1", "ios/1.0.0+1 extra"])
def test_x_client_is_required_and_checked_on_every_v2_call(v2, client):
    headers = {} if client is None else {"X-Client": client}
    refused(v2.http.get("/v2/config", headers=headers), 400, "invalid_request")
    refused(v2.http.post("/v2/analyze", json={"transcript": "x"},
                         headers={**v2.headers(request_key=key()), **headers} if client is not None
                         else {k: v for k, v in v2.headers(request_key=key()).items() if k != "X-Client"}),
            400, "invalid_request")


def test_android_is_a_client_too(v2):
    ok(v2.http.get("/v2/config", headers={"X-Client": "android/2.3.4+567"}), "config", "android/2.3.4+567")


def test_healthz_needs_no_headers(v2):
    assert v2.http.get("/healthz").json() == {"ok": True}


@pytest.mark.parametrize("bad_key", [None, "", "not-a-uuid", "12345678-1234-1234-1234-1234567890123", "{uuid}"])
def test_the_idempotency_key_must_be_a_uuid(v2, bad_key):
    headers = v2.headers()
    if bad_key is not None:
        headers["Idempotency-Key"] = bad_key
    refused(v2.http.post("/v2/analyze", json={"transcript": SPOKEN}, headers=headers), 400, "invalid_request")


def test_an_upper_case_key_is_the_same_key_in_lower_case(v2):
    request_key = key()
    ok(v2.post("/v2/analyze", {"transcript": SPOKEN}, request_key=request_key.upper()), "analyze")
    ok(v2.post("/v2/analyze", {"transcript": SPOKEN}, request_key=request_key), "analyze")
    assert {r["request_key"] for r in usage_rows(v2)} == {request_key}
    assert [r["attempt"] for r in usage_rows(v2)] == [1, 2]


@pytest.mark.parametrize("locale", ["en_US", "english", "EN", "en-us", "e"])
def test_a_malformed_locale_is_refused(v2, locale):
    refused(v2.post("/v2/analyze", {"transcript": SPOKEN}, X_Notch_Locale=locale), 400, "invalid_request")


def test_no_token_is_401_with_the_bearer_challenge(v2):
    response = v2.http.post("/v2/analyze", json={"transcript": SPOKEN},
                            headers=v2.headers(request_key=key(), auth=False))
    refused(response, 401, "unauthorized")
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_bad_token_is_401_even_on_the_config_call(v2):
    refused(v2.http.get("/v2/config", headers={"X-Client": IOS, "Authorization": "Bearer nope"}), 401, "unauthorized")


def test_the_first_authenticated_call_creates_the_account(v2):
    assert v2.rows("SELECT * FROM accounts") == []
    ok(v2.http.get("/v2/config", headers=v2.headers()), "config")
    assert [r["user_id"] for r in v2.rows("SELECT * FROM accounts")] == [USER]


def test_a_deleted_accounts_still_valid_token_is_account_gone(v2):
    v2.meter.delete_account(USER)
    refused(v2.http.get("/v2/config", headers=v2.headers()), 403, "account_gone")
    refused(v2.post("/v2/analyze", {"transcript": SPOKEN}), 403, "account_gone")
    refused(v2.transcribe(fake_recording(SPOKEN)), 403, "account_gone")
    assert v2.rows("SELECT * FROM accounts") == [] and usage_rows(v2) == []


def test_a_blocked_account_is_refused_processing_but_still_gets_config(v2):
    v2.meter.set_blocked(USER, "abuse")
    refused(v2.post("/v2/analyze", {"transcript": SPOKEN}), 403, "account_blocked")
    refused(v2.transcribe(fake_recording(SPOKEN)), 403, "account_blocked")
    ok(v2.http.get("/v2/config", headers=v2.headers()), "config")


def test_dev_auth_is_off_unless_asked_for(v2, tmp_path):
    refused(v2.post("/v2/analyze", {"transcript": SPOKEN}, Authorization="Bearer dev"), 401, "unauthorized")
    with harness(tmp_path / "dev", dev_auth=True) as dev:
        ok(dev.post("/v2/analyze", {"transcript": SPOKEN}, Authorization="Bearer dev"), "analyze")


def test_an_app_older_than_min_app_version_must_update(tmp_path):
    with harness(tmp_path, overrides={"min_app_version": "1.2.0"}) as v2:
        refused(v2.post("/v2/analyze", {"transcript": SPOKEN}, client="ios/1.1.9+1"), 426, "app_update_required")
        ok(v2.post("/v2/analyze", {"transcript": SPOKEN}, client="ios/1.2.0+1"), "analyze", "ios/1.2.0+1")
        ok(v2.http.get("/v2/config", headers=v2.headers(client="ios/1.1.9+1")), "config")  # it can still learn why


# ---------------------------------------------------------------------------
# GET /v2/config
# ---------------------------------------------------------------------------

def test_config_without_a_token_is_the_public_config(v2):
    body = ok(v2.http.get("/v2/config", headers={"X-Client": IOS}), "config")
    assert body == {"config_version": 0, "min_app_version": "1.0.0",
                    "features": {"capture": True, "catch_up": True, "reports": True, "takeaways": True,
                                 "notch_cloud": True},
                    "limits": {"notches_per_day": 10, "reports_per_day": 5, "rewrites_per_day": 20,
                               "max_recording_seconds": 1800, "max_audio_bytes": 26214400, "max_json_bytes": 1048576,
                               "vocabulary": 100, "project_names": 500, "report_max_entries": 400,
                               "report_transcripts_up_to": 60}}
    assert v2.rows("SELECT * FROM active_days") == [] and v2.rows("SELECT * FROM accounts") == []


def test_config_with_a_token_records_the_active_day_and_reports_usage(v2):
    ok(v2.transcribe(fake_recording(SPOKEN)), "transcribe")
    ok(v2.post("/v2/takeaways", {"transcript": SPOKEN}), "takeaways")
    body = ok(v2.http.get("/v2/config", headers=v2.headers(client="ios/1.0.3+9")), "config")
    assert body["usage"] == {"day_utc": "2026-09-26", "notches": 1, "reports": 0, "rewrites": 1}
    assert v2.rows("SELECT user_id, day, app_version, platform FROM active_days") == [
        {"user_id": USER, "day": "2026-09-26", "app_version": "1.0.3", "platform": "ios"}]


def test_config_shows_pushed_versions_and_per_account_flags(v2):
    v2.services.remote.push({"features": {"notch_cloud": False}})
    assert ok(v2.http.get("/v2/config", headers={"X-Client": IOS}), "config")["config_version"] == 1
    v2.meter.touch_account(USER)
    v2.rows("UPDATE accounts SET flags = '{\"notch_cloud\": true}'")
    mine = ok(v2.http.get("/v2/config", headers=v2.headers()), "config")
    assert mine["features"]["notch_cloud"] is True
    assert ok(v2.http.get("/v2/config", headers=v2.headers(OTHER)), "config")["features"]["notch_cloud"] is False


# ---------------------------------------------------------------------------
# POST /v2/transcribe
# ---------------------------------------------------------------------------

def test_transcribe_answers_with_the_transcript_and_what_the_provider_billed(v2):
    body = ok(v2.transcribe(fake_recording(SPOKEN, seconds=12.0)), "transcribe")
    assert body == {"transcript": SPOKEN, "word_count": len(SPOKEN.split()), "audio_seconds": 6.8, "chunks": 1,
                    "config_version": 0}
    (row,) = usage_rows(v2)
    assert (row["status"], row["kind"], row["audio_seconds"], row["chunks"], row["attempt"]) == (
        "ok", "transcribe", 6.8, 1, 1)
    assert row["request_bytes"] == len(fake_recording(SPOKEN, seconds=12.0))
    assert os.listdir(v2.tmp_root) == []


def test_transcribe_sends_the_config_model_and_the_locales_language(v2):
    ok(v2.transcribe(fake_recording(SPOKEN), X_Notch_Locale="de-DE"), "transcribe")
    ok(v2.transcribe(fake_recording(SPOKEN), X_Notch_Locale="fil"), "transcribe")
    languages = [kw["language"] for method, kw in v2.fake.calls if method == "transcribe"]
    assert languages == ["de", "en"]  # a three-letter language falls back to config's
    assert {r["model"] for r in v2.fake.routed} == {"openai/whisper-large-v3"}


def test_a_recording_past_the_split_threshold_is_cut_at_its_pauses_and_joined_in_order(v2):
    words = [f"word{n}" for n in range(1500)]
    recording = fake_recording(" ".join(words), seconds=600.0, silences=[(280.0, 284.0)])
    body = ok(v2.transcribe(recording, duration="600"), "transcribe")
    assert body["chunks"] == 2 and body["transcript"].split() == words
    assert sorted(v2.audio.encodes) == [(0.0, 282.0), (282.0, 600.0)]  # encoded in parallel, joined in order
    assert os.listdir(v2.tmp_root) == []


def test_a_recording_in_another_format_is_encoded_before_it_is_sent(v2):
    ok(v2.transcribe(fake_recording(SPOKEN, passthrough=False), content_type="audio/mpeg"), "transcribe")
    assert v2.audio.encodes == [(0.0, pytest.approx(max(1.0, 0.4 * len(SPOKEN.split()))))]
    assert v2.audio.probes == ["mp3"]


@pytest.mark.parametrize("content_type", ["audio/mp4", "audio/mpeg", "audio/ogg", "audio/webm", "audio/wav",
                                          "audio/mp4; codecs=mp4a.40.2"])
def test_every_contract_audio_type_is_accepted(v2, content_type):
    ok(v2.transcribe(fake_recording(SPOKEN), content_type=content_type), "transcribe")


@pytest.mark.parametrize("content_type", ["", "audio/flac", "video/mp4", "application/json", "multipart/form-data"])
def test_any_other_type_is_unsupported_audio(v2, content_type):
    refused(v2.transcribe(fake_recording(SPOKEN), content_type=content_type), 415, "unsupported_audio")
    assert usage_rows(v2) == []


def test_a_body_over_max_audio_bytes_is_payload_too_large(tmp_path):
    with harness(tmp_path, overrides={"limits": {"max_audio_bytes": 64}}) as v2:
        refused(v2.transcribe(fake_recording(SPOKEN)), 413, "payload_too_large")

        def chunks():  # no Content-Length: counted as it streams
            yield fake_recording(SPOKEN)

        headers = v2.headers(request_key=key(), Content_Type="audio/mp4", X_Notch_Mode="daily", X_Notch_Duration="4")
        refused(v2.http.post("/v2/transcribe", content=chunks(), headers=headers), 413, "payload_too_large")
        assert usage_rows(v2) == [] and os.listdir(v2.tmp_root) == []


def test_a_recording_longer_than_allowed_once_decoded_is_audio_too_long(v2):
    refused(v2.transcribe(fake_recording(SPOKEN, seconds=1800.5), duration="60"), 413, "audio_too_long")
    assert usage_rows(v2) == [] and os.listdir(v2.tmp_root) == []


def test_a_client_that_says_its_recording_is_too_long_is_believed(v2):
    refused(v2.transcribe(fake_recording(SPOKEN), duration="1801"), 413, "audio_too_long")
    assert v2.audio.probes == []


@pytest.mark.parametrize("mode, duration", [(None, "4"), ("weekly", "4"), ("daily", None), ("daily", "four"),
                                            ("daily", "-1")])
def test_transcribe_needs_a_mode_and_a_duration_claim(v2, mode, duration):
    headers = v2.headers(request_key=key(), Content_Type="audio/mp4")
    if mode:
        headers["X-Notch-Mode"] = mode
    if duration:
        headers["X-Notch-Duration"] = duration
    refused(v2.http.post("/v2/transcribe", content=fake_recording(SPOKEN), headers=headers), 400, "invalid_request")


@pytest.mark.parametrize("recording", [b"", b"not audio", b"\x00" * 100])
def test_bytes_that_are_not_audio_are_audio_unreadable(v2, recording):
    refused(v2.transcribe(recording), 422, "audio_unreadable")
    assert usage_rows(v2) == [] and os.listdir(v2.tmp_root) == []


def test_a_recording_with_no_speech_is_no_speech_and_its_cost_still_counts(v2):
    refused(v2.transcribe(fake_recording("", seconds=5.0)), 422, "no_speech")
    (row,) = usage_rows(v2)
    assert (row["status"], row["error_code"], row["reached_model"]) == ("failed", "no_speech", 1)
    assert os.listdir(v2.tmp_root) == []


def test_catch_up_needs_its_feature(tmp_path):
    with harness(tmp_path, overrides={"features": {"catch_up": False}}) as v2:
        ok(v2.transcribe(fake_recording(SPOKEN), mode="daily"), "transcribe")
        refused(v2.transcribe(fake_recording(SPOKEN), mode="catch_up"), 503, "feature_disabled")


@pytest.mark.parametrize("feature, route", [("capture", "/v2/transcribe"), ("capture", "/v2/analyze"),
                                            ("takeaways", "/v2/takeaways"), ("reports", "/v2/reports")])
def test_a_feature_switched_off_is_feature_disabled(tmp_path, feature, route):
    with harness(tmp_path, overrides={"features": {feature: False}}) as v2:
        response = (v2.transcribe(fake_recording(SPOKEN)) if route == "/v2/transcribe"
                    else v2.post(route, {"transcript": SPOKEN}))
        refused(response, 503, "feature_disabled")


def test_processing_switched_off_is_processing_paused_with_a_retry_after(tmp_path):
    with harness(tmp_path, overrides={"features": {"processing": False}}) as v2:
        response = v2.post("/v2/analyze", {"transcript": SPOKEN})
        refused(response, 503, "processing_paused")
        assert response.headers["retry-after"] == "300"


# ---------------------------------------------------------------------------
# POST /v2/analyze and /v2/takeaways
# ---------------------------------------------------------------------------

def test_analyze_with_the_chat_classifier(v2):
    body = ok(v2.post("/v2/analyze", {"transcript": SPOKEN, "project_names": ["Front-End Refactor", "Atlas"],
                                      "vocabulary": ["shipped", "pairing"]}), "analyze")
    assert body["classified_by"] == "llm" and body["category_scores"] is None
    assert body["project_name"] == "Front-End Refactor" and body["mood"] == "up"
    assert body["summary"] and body["takeaways"] and body["categories"]
    assert (body["config_version"], body["prompt_version"]) == (0, "v4")
    assert not [t for t in body["tags"] if t in ("front-end-refactor", "wins")]
    assert [r["method"] for r in v2.fake.routed] == ["tool_call", "tool_call"]  # writing + chat classification
    assert all(r["provider"] == {"zdr": True, "data_collection": "deny", "require_parameters": True}
               and r["model"] == "deepseek/deepseek-v4-pro-0813" for r in v2.fake.routed)
    (row,) = usage_rows(v2)
    assert (row["status"], row["input_chars"], row["prompt_version"], row["cost_usd"]) == (
        "ok", len(SPOKEN), "v4", pytest.approx(0.002))


def test_analyze_with_jev(tmp_path):
    with harness(tmp_path, overrides={"classifier": "jev", "models": {"classifier": "typesafe/jev-9"}}) as v2:
        body = ok(v2.post("/v2/analyze", {"transcript": SPOKEN, "project_names": ["Front-End Refactor"]}), "analyze")
        assert body["classified_by"] == "jev" and set(body["category_scores"]) == {
            "wins", "collaboration", "leadership", "growth", "challenges"}
        assert sorted(r["method"] for r in v2.fake.routed) == ["decide", "tool_call"]
        assert [r["model"] for r in v2.fake.routed if r["method"] == "decide"] == ["typesafe/jev-9"]


def test_the_project_name_is_one_the_device_sent_or_none(tmp_path):
    for said, expected in (("front-end REFACTOR", "Front-End Refactor"), ("Something Else", None)):
        fake = FakeClient(overrides={"label_entry": {"project_match": {"project_name": said, "confidence": "high"}}})
        with harness(tmp_path / said, fake=fake) as v2:
            body = ok(v2.post("/v2/analyze", {"transcript": SPOKEN, "project_names": ["Front-End Refactor"]}),
                      "analyze")
            assert body["project_name"] == expected


def test_a_transcript_over_the_cap_is_transcript_too_long(tmp_path):
    with harness(tmp_path, overrides={"max_transcript_chars": 50}) as v2:
        refused(v2.post("/v2/analyze", {"transcript": "x" * 51}), 413, "transcript_too_long")
        refused(v2.post("/v2/takeaways", {"transcript": "x" * 51}), 413, "transcript_too_long")


@pytest.mark.parametrize("body", [
    [], "text", {}, {"transcript": ""}, {"transcript": "   "}, {"transcript": 5},
    {"transcript": "ok", "extra": 1}, {"transcript": "ok", "project_names": "Atlas"},
    {"transcript": "ok", "project_names": [""]}, {"transcript": "ok", "vocabulary": [1]},
    {"transcript": "ok", "vocabulary": ["t"] * 101}, {"transcript": "ok", "project_names": ["p"] * 501},
    {"transcript": "ok", "project_names": ["x" * 201]},
])
def test_a_malformed_body_is_invalid_request(v2, body):
    refused(v2.post("/v2/analyze", body), 400, "invalid_request")
    refused(v2.post("/v2/takeaways", body), 400, "invalid_request")
    assert usage_rows(v2) == []


@pytest.mark.parametrize("raw", [b"{", b"\xff\xfe", b'{"transcript": "\\ud800"}', b"[" * 5000 + b"]" * 5000])
def test_a_body_that_is_not_json_is_invalid_request(v2, raw):
    headers = v2.headers(request_key=key(), Content_Type="application/json")
    refused(v2.http.post("/v2/analyze", content=raw, headers=headers), 400, "invalid_request")


def test_a_json_body_over_its_cap_is_payload_too_large(tmp_path):
    with harness(tmp_path, overrides={"max_request_json_bytes": 100}) as v2:
        refused(v2.post("/v2/analyze", {"transcript": "x" * 200}), 413, "payload_too_large")


@pytest.mark.parametrize("failure, status, code", [
    (ModelRefused("refused"), 422, "model_refused"),
    (ModelRefused("no ZDR endpoint", 404), 502, "model_unavailable"),
    (ModelRefused("out of credit", 402), 502, "model_unavailable"),
    (ModelUnavailable("down"), 502, "model_unavailable"),
])
def test_model_failures_map_to_the_contracts_codes(tmp_path, failure, status, code):
    with harness(tmp_path, fake=FakeClient(fail_with=failure)) as v2:
        refused(v2.post("/v2/analyze", {"transcript": SPOKEN}), status, code)
        (row,) = usage_rows(v2)
        assert (row["status"], row["error_code"]) == ("failed", code)


def test_a_transcription_the_provider_refuses_is_audio_unreadable(tmp_path):
    with harness(tmp_path, fake=FakeClient(fail_with=ModelRefused("bad audio", 400))) as v2:
        refused(v2.transcribe(fake_recording(SPOKEN)), 422, "audio_unreadable")


def test_takeaways_are_written_again_and_nothing_else(v2):
    body = ok(v2.post("/v2/takeaways", {"transcript": SPOKEN, "project_names": ["Front-End Refactor"],
                                        "vocabulary": ["shipped"]}), "takeaways")
    assert body["takeaways"] and (body["config_version"], body["prompt_version"]) == (0, "v4")
    assert [r["method"] for r in v2.fake.routed] == ["tool_call"]


def test_takeaways_with_none_written_is_model_refused(tmp_path):
    with harness(tmp_path, fake=FakeClient(overrides={"label_entry": {"takeaways": []}})) as v2:
        refused(v2.post("/v2/takeaways", {"transcript": SPOKEN}), 422, "model_refused")


def test_a_slow_model_is_deadline_exceeded(tmp_path):
    with harness(tmp_path, fake=FakeClient(delay=3.0), overrides={"deadlines": {"analyze": 1}}) as v2:
        response = v2.post("/v2/analyze", {"transcript": SPOKEN})
        refused(response, 504, "deadline_exceeded")
        (row,) = usage_rows(v2)
        assert (row["status"], row["error_code"]) == ("failed", "deadline_exceeded")


# ---------------------------------------------------------------------------
# Closed enums per contract version
# ---------------------------------------------------------------------------

def test_a_value_the_clients_contract_version_cannot_store_is_never_sent(v2, monkeypatch):
    """Contract 2 adds classified_by 'llm'; a build that speaks contract 1 must not receive it."""
    enums = {1: dict(wire_v2.ENUMS[1], classified_by=["jev"]), 2: dict(wire_v2.ENUMS[1])}
    monkeypatch.setattr(wire_v2, "ENUMS", enums)
    monkeypatch.setattr(wire_v2, "CONTRACTS", {"ios": [((1, 0, 0), 1), ((1, 5, 0), 2)], "android": [((1, 0, 0), 1)]})
    refused(v2.post("/v2/analyze", {"transcript": SPOKEN}, client="ios/1.4.9+1"), 500, "internal_error")
    assert usage_rows(v2)[-1]["error_code"] == "internal_error"
    ok(v2.post("/v2/analyze", {"transcript": SPOKEN}, client="ios/1.5.0+1"), "analyze", "ios/1.5.0+1")


def test_a_report_type_the_client_does_not_know_is_refused(v2, monkeypatch):
    monkeypatch.setattr(wire_v2, "ENUMS", {1: dict(wire_v2.ENUMS[1], report_type=["week", "month"])})
    refused(v2.post("/v2/reports", report_body(type="quarter")), 400, "invalid_request")


# ---------------------------------------------------------------------------
# POST /v2/reports
# ---------------------------------------------------------------------------

def entry(n, day, **fields):
    return {"id": f"e{n}", "date": day, "project_name": "Atlas" if n % 2 else None, "tags": ["shipped"],
            "categories": ["wins"], "is_milestone": n == 1, "summary": f"You did thing {n}.",
            "takeaways": [f"Takeaway {n}."], "impact_note": None, "acknowledged_by": "Dana" if n == 2 else None,
            "transcript": f"Spoken words {n}."} | fields


def report_body(**fields):
    return {"type": "week", "range_start": "2026-09-21", "range_end": "2026-09-27", "range_label": "Sep 21 – 27",
            "scope": {"project_name": None, "tag": None},
            "author": {"display_name": "Sam", "role": "Engineer", "industry": "Software", "years_experience": "6"},
            "project_names": ["Atlas"],
            "entries": [entry(1, "2026-09-21"), entry(2, "2026-09-23"), entry(3, "2026-09-23")]} | fields


def test_a_report_is_written_from_the_entries_the_device_sent(v2):
    body = ok(v2.post("/v2/reports", report_body()), "reports")
    facts = body["facts"]
    assert (facts["notch_count"], facts["project_count"], facts["milestone_count"]) == (3, 1, 1)
    assert facts["project_breakdown"] == [{"name": "Atlas", "notch_count": 2, "share": 66}]
    assert facts["momentum_granularity"] == "day" and len(facts["momentum"]) == 7
    assert [m["count"] for m in facts["momentum"]] == [1, 0, 2, 0, 0, 0, 0]
    assert facts["eyebrow"] == "Weekly report · Sep 21 – 27"
    assert body["source_entry_ids"] == ["e1", "e2", "e3"]
    assert [h["ordinal"] for h in body["highlights"]] == list(range(len(body["highlights"])))
    assert GHOST_ID not in {i for h in body["highlights"] for i in h["source_entry_ids"]}
    assert (body["config_version"], body["prompt_version"]) == (0, "r1")
    (message,) = [kw["user"] for method, kw in v2.fake.calls if method == "tool_call"]
    assert "- notches: 3" in message and "transcript: Spoken words 1." in message
    assert "Sam (Engineer, Software, 6 years' experience)" in message
    (row,) = usage_rows(v2)
    assert (row["entry_count"], row["status"]) == (3, "ok")


def test_past_report_transcripts_up_to_the_prompt_carries_summaries_only(tmp_path):
    with harness(tmp_path, overrides={"limits": {"report_transcripts_up_to": 2}}) as v2:
        ok(v2.post("/v2/reports", report_body()), "reports")
        (message,) = [kw["user"] for method, kw in v2.fake.calls if method == "tool_call"]
        assert "transcript:" not in message and "summaries only" in message


def test_a_transcript_only_notch_can_be_in_a_report(v2):
    body = report_body(entries=[entry(1, "2026-09-22", summary=None, takeaways=[], categories=[])])
    ok(v2.post("/v2/reports", body), "reports")


def test_too_many_entries_or_days_is_range_too_large(tmp_path):
    with harness(tmp_path, overrides={"limits": {"report_max_entries": 2}, "report_max_days": 30}) as v2:
        refused(v2.post("/v2/reports", report_body()), 422, "range_too_large")
        refused(v2.post("/v2/reports", report_body(type="custom", range_start="2026-01-01",
                                                   entries=[entry(1, "2026-09-21")])), 422, "range_too_large")


@pytest.mark.parametrize("change", [
    {"type": "fortnight"}, {"range_start": "2026-09-28"}, {"range_start": "Sep 21"}, {"entries": []},
    {"entries": [entry(1, "2026-09-20")]}, {"entries": [entry(1, "2026-09-21"), entry(1, "2026-09-22")]},
    {"entries": [entry(1, "2026-09-21", categories=["heroics"])]}, {"entries": [entry(1, "2026-09-21", extra=1)]},
    {"entries": [entry(1, "2026-09-21", is_milestone="yes")]}, {"range_label": ""}, {"surprise": True},
    {"scope": {"project": "Atlas"}}, {"author": {"name": "Sam"}},
])
def test_a_malformed_report_is_invalid_request(v2, change):
    refused(v2.post("/v2/reports", report_body(**change)), 400, "invalid_request")
    assert usage_rows(v2) == []


def test_a_report_over_max_json_bytes_is_payload_too_large(tmp_path):
    with harness(tmp_path, overrides={"limits": {"max_json_bytes": 500}}) as v2:
        refused(v2.post("/v2/reports", report_body()), 413, "payload_too_large")


# ---------------------------------------------------------------------------
# Quotas, keys and attempts, as the phone meets them
# ---------------------------------------------------------------------------

def test_the_notch_backstop_allows_n_and_refuses_n_plus_one_with_resets_at(tmp_path):
    with harness(tmp_path, overrides={"limits": {"notches_per_day": 1}}) as v2:  # backstop 2
        first = key()
        for request_key in (first, key()):
            ok(v2.transcribe(fake_recording(SPOKEN), request_key=request_key), "transcribe")
        response = v2.transcribe(fake_recording(SPOKEN))
        error = refused(response, 429, "quota_exceeded")
        assert error["resets_at"] == "2026-09-27T00:00:00Z" and error["retryable"] is True
        assert int(response.headers["retry-after"]) == 12 * 3600
        ok(v2.transcribe(fake_recording(SPOKEN), request_key=first), "transcribe")  # a retry is charged once


def test_two_calls_racing_at_n_minus_one_let_exactly_one_through(tmp_path):
    with harness(tmp_path, overrides={"limits": {"rewrites_per_day": 1}}) as v2:  # backstop 2
        ok(v2.post("/v2/takeaways", {"transcript": SPOKEN}), "takeaways")

        async def race():
            transport = httpx.ASGITransport(app=v2.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await asyncio.gather(*[client.post("/v2/takeaways", json={"transcript": SPOKEN},
                                                          headers=v2.headers(request_key=key())) for _ in range(2)])

        statuses = sorted(r.status_code for r in asyncio.run(race()))
        assert statuses == [200, 429]


def test_the_same_key_with_another_body_is_idempotency_key_reused(v2):
    request_key = key()
    ok(v2.post("/v2/analyze", {"transcript": SPOKEN}, request_key=request_key), "analyze")
    refused(v2.post("/v2/analyze", {"transcript": SPOKEN + " More."}, request_key=request_key), 409,
            "idempotency_key_reused")
    ok(v2.post("/v2/analyze", {"transcript": SPOKEN}, request_key=request_key), "analyze")


def test_the_same_key_while_it_runs_is_request_in_flight(tmp_path):
    with harness(tmp_path, fake=FakeClient(delay=0.6)) as v2:
        request_key = key()

        async def both():
            transport = httpx.ASGITransport(app=v2.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                first = asyncio.create_task(client.post("/v2/takeaways", json={"transcript": SPOKEN},
                                                        headers=v2.headers(request_key=request_key)))
                await asyncio.sleep(0.2)
                second = await client.post("/v2/takeaways", json={"transcript": SPOKEN},
                                           headers=v2.headers(request_key=request_key))
                return await first, second

        first, second = asyncio.run(both())
        assert first.status_code == 200
        refused(second, 409, "request_in_flight")
        assert second.headers["retry-after"].isdigit()


def test_five_attempts_that_reached_a_model_exhaust_the_key_for_an_hour(tmp_path):
    with harness(tmp_path, fake=FakeClient(fail_with=ModelUnavailable("down"))) as v2:
        request_key = key()
        for _ in range(5):
            refused(v2.post("/v2/analyze", {"transcript": SPOKEN}, request_key=request_key), 502, "model_unavailable")
            v2.clock.advance(60)
        refused(v2.post("/v2/analyze", {"transcript": SPOKEN}, request_key=request_key), 429, "attempts_exhausted")
        v2.fake.fail_with = None
        v2.clock.advance(3600)
        ok(v2.post("/v2/analyze", {"transcript": SPOKEN}, request_key=request_key), "analyze")


def test_more_than_three_calls_at_once_is_rate_limited(tmp_path):
    with harness(tmp_path, fake=FakeClient(delay=0.5)) as v2:
        async def four():
            transport = httpx.ASGITransport(app=v2.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await asyncio.gather(*[client.post("/v2/takeaways", json={"transcript": SPOKEN},
                                                          headers=v2.headers(request_key=key())) for _ in range(4)])

        responses = asyncio.run(four())
        assert sorted(r.status_code for r in responses) == [200, 200, 200, 429]
        refused(next(r for r in responses if r.status_code == 429), 429, "rate_limited")


def test_an_accounts_daily_cost_is_capped_counting_every_attempt(tmp_path):
    with harness(tmp_path, fake=FakeClient(cost=0.3)) as v2:  # analyze makes two chat calls: $0.60 a notch
        ok(v2.post("/v2/analyze", {"transcript": SPOKEN}), "analyze")
        ok(v2.post("/v2/analyze", {"transcript": SPOKEN}), "analyze")
        refused(v2.post("/v2/analyze", {"transcript": SPOKEN}), 429, "quota_exceeded")
        ok(v2.post("/v2/analyze", {"transcript": SPOKEN}, user=OTHER), "analyze")


def test_the_global_breaker_pauses_processing(v2):
    v2.rows("INSERT INTO daily_spend (day, kind, calls, cost_usd) VALUES ('2026-09-26', 'reports', 90, 20.0)")
    response = v2.post("/v2/analyze", {"transcript": SPOKEN})
    refused(response, 503, "processing_paused")
    assert int(response.headers["retry-after"]) == 12 * 3600


def test_the_call_is_settled_before_its_response_is_written(v2):
    seen = []

    async def watching(scope, receive, send):
        async def spy(message):
            if message["type"] == "http.response.start":
                seen.append([(r["status"], r["cost_usd"]) for r in usage_rows(v2)])
            await send(message)
        await v2.app(scope, receive, spy)

    from fastapi.testclient import TestClient

    with TestClient(watching) as client:
        response = client.post("/v2/analyze", json={"transcript": SPOKEN}, headers=v2.headers(request_key=key()))
    assert response.status_code == 200
    assert seen == [[("ok", pytest.approx(0.002))]]
