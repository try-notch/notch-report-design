"""
The zero-data-retention audit (zdr.py) after speech-to-text and Jev calls: a hit records
the provider; a miss records it, switches that path off in remote config (capture, or
the classifier back to chat) and alerts; a provider that cannot be learned, or a list
that cannot be fetched, alerts and switches nothing. Both the hit and the miss run
through the fakes, directly and through the routes.
"""

import logging

import pytest

from notch_api.fakes import FakeClient, fake_recording
from notch_api.meter import Meter
from notch_api.openrouter import ModelUnavailable
from notch_api.remote_config import RemoteConfig
from notch_api.zdr import ZdrAuditor
from tests.v2kit import Harness, WallClock, key, ok, refused

MODELS = {"stt": "openai/whisper-large-v3", "classifier": "typesafe/jev-1.13"}
USER = "99999999-9999-4999-8999-999999999999"


class Directory:
    """zdr.py's view of OpenRouter: generation lookups (a list of answers to give in turn) and the ZDR list."""

    def __init__(self, provider="DeepInfra", model="openai/whisper-large-v3", *, pending=0, listing=None,
                 list_down=False):
        self.answers = [None] * pending + [{"provider": provider, "model": model}]
        self.listing = listing if listing is not None else [
            {"provider": "DeepInfra", "model": "openai/whisper-large-v3"},
            {"provider": "TypeSafe", "model": "typesafe/jev-1.13-20260917"}]
        self.list_down, self.lookups, self.lists = list_down, 0, 0

    def generation(self, generation_id):
        self.lookups += 1
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

    def zdr_endpoints(self):
        self.lists += 1
        if self.list_down:
            raise ModelUnavailable("down")
        return self.listing


@pytest.fixture
def meter(tmp_path):
    return Meter(str(tmp_path / "meter.db"), clock=WallClock())


@pytest.fixture
def remote(meter):
    return RemoteConfig(meter)


def audit(meter, remote, directory, calls, **kwargs):
    started = meter.start(user_id=USER, kind="transcribe", key=key(), body_hmac=b"h", deadline_seconds=60,
                          config=remote.current())
    meter.settle(started, ok=True, totals={"providers": []})
    auditor = ZdrAuditor(directory, meter, remote, inline=True, waits=(0, 0, 0), sleep=lambda s: None, **kwargs)
    auditor.submit(started.row_id, calls, MODELS)
    (row,) = [r for r in _rows(meter) if r["id"] == started.row_id]
    return auditor, row


def _rows(meter):
    from notch_api.meter import connect

    conn = connect(meter.path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM usage_events")]
    finally:
        conn.close()


STT = {"kind": "stt", "generation_id": "gen-1", "model": "openai/whisper-large-v3"}
JEV = {"kind": "classify", "generation_id": "gen-2", "model": "typesafe/jev-1.13"}


def alerts(caplog):
    return [r.notch.get("alert") for r in caplog.records if getattr(r, "notch", {}).get("event") == "alert"]


def test_a_zero_retention_provider_is_a_hit_and_changes_nothing(meter, remote, caplog):
    _, row = audit(meter, remote, Directory("DeepInfra"), [STT])
    assert (row["zdr"], row["providers"]) == ("hit", '["DeepInfra"]')
    assert remote.current().version == 0 and alerts(caplog) == []


def test_a_speech_to_text_miss_switches_capture_off_and_alerts(meter, remote, caplog):
    remote.push({"features": {"reports": False}})
    with caplog.at_level(logging.ERROR):
        _, row = audit(meter, remote, Directory("SketchyAI"), [STT])
    current = remote.current()
    assert (row["zdr"], row["providers"]) == ("miss", '["SketchyAI"]')
    assert current.version == 2 and current["features"]["capture"] is False
    assert current["features"]["reports"] is False     # the row it replaced still holds
    newest_version, newest_body = meter.config_rows_newest_first()[0]
    assert newest_version == 2 and '"capture": false' in newest_body
    assert alerts(caplog) == ["zdr_miss"]


def test_a_jev_miss_puts_the_classifier_back_to_chat(meter, remote):
    remote.push({"classifier": "jev"})
    _, row = audit(meter, remote, Directory("OtherCo", "typesafe/jev-1.13-20260917"), [JEV])
    assert row["zdr"] == "miss" and remote.current()["classifier"] == "chat"
    assert remote.current()["features"]["capture"] is True


def test_a_miss_on_a_path_already_off_adds_no_version_but_still_alerts(meter, remote, caplog):
    remote.push({"features": {"capture": False}})
    with caplog.at_level(logging.ERROR):
        audit(meter, remote, Directory("SketchyAI"), [STT])
    assert remote.current().version == 1 and alerts(caplog) == ["zdr_miss"]


def test_the_same_provider_for_another_model_is_a_miss(meter, remote):
    _, row = audit(meter, remote, Directory("TypeSafe", "openai/whisper-large-v3"), [STT])
    assert row["zdr"] == "miss"


def test_a_dated_model_id_matches_its_undated_name(meter, remote):
    listing = [{"provider": "TypeSafe", "model": "typesafe/jev-1.13"}]
    _, row = audit(meter, remote, Directory("TypeSafe", "typesafe/jev-1.13-20260917", listing=listing), [JEV])
    assert row["zdr"] == "hit"


def test_a_provider_that_cannot_be_learned_is_unknown_and_switches_nothing(meter, remote, caplog):
    directory = Directory(pending=10)
    with caplog.at_level(logging.ERROR):
        _, row = audit(meter, remote, directory, [STT])
    assert row["zdr"] == "unknown" and remote.current().version == 0
    assert directory.lookups == 3 and alerts(caplog) == ["zdr_unverified"]


def test_a_generation_recorded_late_is_found_on_a_later_lookup(meter, remote):
    directory = Directory("DeepInfra", pending=2)
    _, row = audit(meter, remote, directory, [STT])
    assert row["zdr"] == "hit" and directory.lookups == 3


def test_a_list_that_cannot_be_fetched_is_unknown(meter, remote):
    _, row = audit(meter, remote, Directory("SketchyAI", list_down=True), [STT])
    assert row["zdr"] == "unknown" and remote.current().version == 0


def test_the_list_is_fetched_once_per_ttl(meter, remote):
    directory = Directory("DeepInfra")
    auditor, _ = audit(meter, remote, directory, [STT, dict(STT, generation_id="gen-9")])
    assert directory.lists == 1


def test_chat_replies_are_not_audited(meter, remote):
    directory = Directory()
    audit(meter, remote, directory, [{"kind": "chat", "generation_id": "gen-c", "model": "m"}])
    assert directory.lookups == 0


# ---------------------------------------------------------------------------
# Through the routes, on the fakes
# ---------------------------------------------------------------------------

def test_a_transcription_served_by_a_non_zdr_provider_is_returned_and_turns_capture_off(tmp_path, caplog):
    with Harness(tmp_path, fake=FakeClient(stt_provider="SketchyAI")) as v2, caplog.at_level(logging.ERROR):
        body = ok(v2.transcribe(fake_recording("Shipped it today.")), "transcribe")
        assert body["transcript"] == "Shipped it today."          # the content was already sent
        assert v2.rows("SELECT zdr, providers FROM usage_events") == [{"zdr": "miss", "providers": '["SketchyAI"]'}]
        refused(v2.transcribe(fake_recording("Again.")), 503, "feature_disabled")
        refused(v2.post("/v2/analyze", {"transcript": "Hello."}), 503, "feature_disabled")
        assert ok(v2.http.get("/v2/config", headers={"X-Client": "ios/1.0.0+42"}), "config")["features"][
            "capture"] is False
        assert "zdr_miss" in alerts(caplog)


def test_a_transcription_served_by_a_zdr_provider_is_a_hit(tmp_path):
    with Harness(tmp_path) as v2:
        ok(v2.transcribe(fake_recording("Shipped it today.")), "transcribe")
        assert v2.rows("SELECT zdr, providers FROM usage_events") == [{"zdr": "hit", "providers": '["DeepInfra"]'}]
        assert ("generation", "gen-fake-1") in v2.fake.metadata_calls


def test_a_jev_call_served_outside_zdr_sends_analysis_back_to_the_chat_classifier(tmp_path):
    with Harness(tmp_path, fake=FakeClient(jev_provider="OtherCo"), overrides={"classifier": "jev"}) as v2:
        assert ok(v2.post("/v2/analyze", {"transcript": "Shipped it."}), "analyze")["classified_by"] == "jev"
        assert ok(v2.post("/v2/analyze", {"transcript": "Shipped it."}), "analyze")["classified_by"] == "llm"
