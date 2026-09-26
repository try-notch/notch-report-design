"""
Remote config: baked defaults, versioned rows where the newest valid one wins, an invalid
row logged and skipped, and the admin CLI that pushes rows and blocks accounts.
"""

import io
import json
import logging

import pytest

from notch_api import admin, meter as meter_module
from notch_api.meter import Meter
from notch_api.remote_config import DEFAULTS, ConfigInvalid, RemoteConfig, build
from tests.v2kit import WallClock, config

USER = "44444444-4444-4444-8444-444444444444"


@pytest.fixture
def meter(tmp_path):
    return Meter(str(tmp_path / "meter.db"), clock=WallClock())


@pytest.fixture
def remote(meter):
    return RemoteConfig(meter)


def _insert_raw(meter, body):
    """A row as if written by hand, bypassing push's validation."""
    conn = meter_module.connect(meter.path)
    try:
        return conn.execute("INSERT INTO config (body, created_at) VALUES (?, 0)", (body,)).lastrowid
    finally:
        conn.close()


def test_the_baked_defaults_are_the_contracts(remote):
    current = remote.current()
    assert current.version == 0
    assert current["limits"] == {"notches_per_day": 10, "reports_per_day": 5, "rewrites_per_day": 20,
                                 "max_recording_seconds": 1800, "max_audio_bytes": 25 * 1024 * 1024,
                                 "max_json_bytes": 1024 * 1024, "vocabulary": 100, "project_names": 500,
                                 "report_max_entries": 400, "report_transcripts_up_to": 60}
    assert current["models"] == {"stt": "openai/whisper-large-v3", "chat": "deepseek/deepseek-v4-pro-0813",
                                 "classifier": "typesafe/jev-1.13"}
    assert current["classifier"] == "chat"
    assert current["provider"] == {"zdr": True, "data_collection": "deny", "require_parameters": True}
    assert current["deadlines"] == {"config": 5, "transcribe": 120, "analyze": 60, "takeaways": 45, "reports": 240,
                                    "account": 30}
    assert current["spend"] == {"account_usd_per_day": 1.0, "global_usd_per_day": 20.0}
    assert (current["max_in_flight"], current["attempts_per_hour"]) == (3, 5)
    assert current["stt"] == {"language": "en", "split_over_seconds": 480, "chunk_seconds": 300, "parallel": 3}


def test_the_newest_valid_row_wins_and_rows_override_the_defaults_not_each_other(remote):
    first = remote.push({"features": {"reports": False}, "classifier": "jev"})
    second = remote.push({"features": {"takeaways": False}})
    current = remote.current()
    assert (first, second, current.version) == (1, 2, 2)
    assert current["features"]["takeaways"] is False
    assert current["features"]["reports"] is True        # row 1's change is not inherited
    assert current["classifier"] == "chat"
    remote.push({"features": {"reports": False}, "classifier": "jev"})  # rolling back is pushing the old body again
    assert remote.current()["classifier"] == "jev" and remote.current().version == 3


def test_an_invalid_row_is_logged_once_and_skipped(meter, remote, caplog):
    remote.push({"max_in_flight": 4})
    bad = _insert_raw(meter, json.dumps({"max_in_flight": "lots"}))
    worse = _insert_raw(meter, "{not json")
    with caplog.at_level(logging.ERROR, logger="notch_api.remote_config"):
        current = remote.current()
        remote.current()
    assert (current.version, current["max_in_flight"]) == (1, 4)
    logged = [r.notch["config_version"] for r in caplog.records if getattr(r, "notch", None)]
    assert sorted(logged) == [bad, worse]


def test_with_no_valid_row_the_server_runs_on_the_defaults(meter, remote):
    _insert_raw(meter, json.dumps({"features": {"capture": "yes"}}))
    assert remote.current().version == 0


def test_a_new_push_is_seen_on_the_next_read(remote):
    assert remote.current().version == 0
    remote.push({"attempts_per_hour": 9})
    assert remote.current()["attempts_per_hour"] == 9


@pytest.mark.parametrize("body, where", [
    ({"featurs": {}}, "<root>"),
    ({"features": {"capture": 1}}, "features/capture"),
    ({"provider": {"zdr": False, "data_collection": "deny", "require_parameters": True}}, "provider/zdr"),
    ({"provider": {"data_collection": "allow"}}, "provider/data_collection"),
    ({"prompts": {"analyze": "v99"}}, "prompts/analyze"),
    ({"classifier": "gpt"}, "classifier"),
    ({"stt": {"chunk_seconds": 600}}, "stt/chunk_seconds"),
    ({"limits": {"notches_per_day": 0}}, "limits/notches_per_day"),
    ({"min_app_version": "1.0"}, "min_app_version"),
    (["not", "an", "object"], "<root>"),
])
def test_a_body_that_cannot_run_is_refused(remote, body, where):
    with pytest.raises(ConfigInvalid, match=where):
        remote.push(body)
    assert remote.current().version == 0


def test_per_account_flags_override_features():
    features = config(features={"notch_cloud": False}).features_for({"notch_cloud": True, "bogus": True,
                                                                     "capture": "no"})
    assert features["notch_cloud"] is True and "bogus" not in features and features["capture"] is True


def test_the_public_view_is_the_contracts_shape():
    public = config(7).public(config(7).features_for({}))
    assert set(public) == {"config_version", "min_app_version", "features", "limits"}
    assert set(public["features"]) == {"capture", "catch_up", "reports", "takeaways", "notch_cloud"}
    assert public["config_version"] == 7


def test_defaults_validate():
    assert build({}) == DEFAULTS


# ---------------------------------------------------------------------------
# The admin CLI
# ---------------------------------------------------------------------------

def _run(meter, *argv):
    out, err = io.StringIO(), io.StringIO()
    code = admin.main(list(argv), meter_db=meter.path, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_admin_pushes_a_valid_file_and_shows_it(meter, tmp_path):
    body = tmp_path / "config.json"
    body.write_text(json.dumps({"features": {"notch_cloud": False}}))
    assert _run(meter, "config", "push", str(body), "--note", "cloud off") == (0, "config version 1 pushed\n", "")
    code, out, _ = _run(meter, "config", "show")
    shown = json.loads(out)
    assert code == 0 and shown["config_version"] == 1 and shown["effective"]["features"]["notch_cloud"] is False
    (row,) = meter.config_rows_newest_first()
    assert json.loads(row[1]) == {"features": {"notch_cloud": False}}


def test_admin_refuses_an_invalid_file_and_writes_nothing(meter, tmp_path):
    body = tmp_path / "config.json"
    body.write_text(json.dumps({"provider": {"zdr": False}}))
    code, out, err = _run(meter, "config", "push", str(body))
    assert code == 2 and err.startswith("refused: provider/zdr")
    assert meter.config_rows_newest_first() == []
    assert _run(meter, "config", "push", str(tmp_path / "missing.json"))[0] == 2


def test_admin_blocks_and_unblocks_an_account(meter):
    assert _run(meter, "account", "block", USER.upper(), "--code", "abuse")[0] == 0
    assert meter.touch_account(USER).blocked_code == "abuse"
    assert _run(meter, "account", "unblock", USER)[0] == 0
    assert not meter.touch_account(USER).blocked


@pytest.mark.parametrize("argv", [("account", "block", "not-a-uuid"),
                                  ("account", "block", USER, "--code", "Spammed the API!")])
def test_admin_refuses_a_bad_account_id_or_a_free_text_code(meter, argv):
    assert _run(meter, *argv)[0] == 2
