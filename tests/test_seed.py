"""
seed.py: the demo history the E2E's reports are written from. What matters is that
every seeded notch is one the app could render, that seed_db's project assignment
survives whatever the model says, and that the category score pairs each notch's
hand labels with that same notch's prediction.
"""

from datetime import date

import pytest

import seed_db
from notch_api import contract, seed, store
from notch_api.config import DEV_USER_ID as DEV
from notch_api.fakes import FakeClient
from notch_api.openrouter import ModelRefused, ModelUnavailable

TODAY = date(2026, 9, 24)
PROJECT_ENTRIES = {seed.entry_id(n) for n, e in enumerate(seed_db.ENTRIES) if e[3]}


def _seed(db_path, client=None, **kwargs):
    return seed.seed(db_path, client or FakeClient(), today=TODAY, **kwargs)


def _projects(conn):
    return {r["id"]: r["project"] for r in conn.execute(
        "SELECT e.id, p.name AS project FROM entries e LEFT JOIN projects p ON p.id = e.project_id")}


def test_every_demo_notch_is_seeded_complete_and_valid_on_the_wire(db_path, conn):
    results = _seed(db_path)

    assert [entry_id for entry_id, _, _ in results] == [seed.entry_id(n) for n in range(len(seed_db.ENTRIES))]
    wire = [store.load_entry(conn, DEV, entry_id) for entry_id, _, _ in results]
    for entry in wire:
        contract.validate("entry", entry)
        assert entry["analysis_state"] == "complete"
    # days_ago counts back from the UTC day, at 17:30 UTC: the week and month reports depend on it.
    recorded = sorted(e["recorded_at"] for e in wire)
    assert recorded[0] == "2026-06-27T17:30:00Z" and recorded[-1] == "2026-09-24T17:30:00Z"


@pytest.mark.parametrize("choice", ["Front-End Refactor", "none"])  # Jev claims every notch, or none
def test_the_seeded_project_wins_over_the_models_match(db_path, conn, choice):
    _seed(db_path, FakeClient(overrides={"decide": {"project": {"type": "choice", "choice": choice,
                                                                "confidence": 0.99}}}))

    assigned = {entry_id for entry_id, project in _projects(conn).items() if project}
    assert assigned == PROJECT_ENTRIES
    assert {project for project in _projects(conn).values() if project} == {seed_db.PROJECT_NAME}


def test_each_prediction_is_paired_with_its_own_notch_and_hand_labels(db_path, conn):
    # Notches are analysed oldest first but returned in seed_db order; a mix-up would
    # score one notch's prediction against another's labels.
    results = _seed(db_path)

    stored = {r["id"]: store.json_list(r["categories"]) for r in conn.execute("SELECT id, categories FROM entries")}
    assert len({tuple(predicted) for _, _, predicted in results}) > 1  # the fake varies, so pairing is observable
    for n, (entry_id, expected, predicted) in enumerate(results):
        assert predicted == stored[entry_id]
        assert set(expected) == set(seed_db.ENTRIES[n][2].split(","))


def test_agreement_is_an_exact_set_match_in_any_order():
    assert seed.agreement([("a", ["wins", "growth"], ["growth", "wins"]),
                           ("b", ["wins"], ["wins", "growth"]),
                           ("c", ["challenges"], [])]) == (1, 3)


def test_each_chunk_sees_the_tags_the_earlier_chunks_produced(db_path, fake_client):
    _seed(db_path, fake_client, workers=4)

    messages = [kwargs["user"] for method, kwargs in fake_client.calls if method == "tool_call"]
    assert all(m.endswith("none yet") for m in messages[:4])
    assert not any(m.endswith("none yet") for m in messages[4:])
    assert all("- Billing Migration\n- Front-End Refactor" in m for m in messages)


def test_reseeding_replaces_the_notches_and_keeps_a_project_the_user_already_has(db_path, conn, add_project):
    add_project("mine", "front-end refactor")  # same folded name as the seed's, created through the API
    _seed(db_path)
    seed.seed(db_path, FakeClient(), today=date(2026, 10, 1))

    assert conn.execute("SELECT count(*) FROM entries").fetchone()[0] == len(seed_db.ENTRIES)
    assert conn.execute("SELECT max(recorded_at) FROM entries").fetchone()[0] == "2026-10-01T17:30:00Z"
    on_mine = {r["id"] for r in conn.execute("SELECT id FROM entries WHERE project_id = 'mine'")}
    assert on_mine == PROJECT_ENTRIES


class _RefusesFirstCall(FakeClient):
    refused = False

    def tool_call(self, **kwargs):
        if not self.refused:
            self.refused = True
            raise ModelRefused("The model did not call label_entry.")
        return super().tool_call(**kwargs)


def test_a_refusal_is_retried_once(db_path, conn):
    client = _RefusesFirstCall()
    _seed(db_path, client, workers=1)

    assert client.refused
    states = {r[0] for r in conn.execute("SELECT analysis_state FROM entries")}
    assert states == {"complete"}


def test_a_model_that_keeps_failing_stops_the_seed(db_path):
    with pytest.raises(ModelUnavailable):
        _seed(db_path, FakeClient(fail_with=ModelUnavailable("OpenRouter unavailable after 5 attempts.")))


def test_the_command_fails_cleanly_without_a_key(db_path, monkeypatch, capsys):
    monkeypatch.delenv("NOTCH_FAKE_MODELS", raising=False)

    assert seed.main(["--db", db_path]) == 1
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err
