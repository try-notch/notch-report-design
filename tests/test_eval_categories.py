"""
eval_categories.py: the numbers it prints are the evidence for keeping or moving
config.CATEGORY_THRESHOLD, so the scoring is tested on hand-checked cases, and the
cache is tested for the one way it could mislead: answering for a model it was not
made by.
"""

import json

import pytest

import seed_db
from notch_api import config, eval_categories as ev
from notch_api.fakes import FakeClient
from notch_api.openrouter import ModelUnavailable


def _scores(**given):
    return dict.fromkeys(ev.CATEGORIES, 0.1) | given


def test_score_is_exact_set_match_with_per_category_counts():
    expected = [{"wins"}, {"wins", "growth"}, {"challenges"}]
    predicted = [["wins"], ["wins"], ["growth"]]
    exact, counts = ev.score(expected, predicted)
    assert exact == 1
    assert counts["wins"] == (2, 0, 0) and counts["growth"] == (0, 1, 1) and counts["challenges"] == (0, 0, 1)


def test_best_threshold_is_the_one_agreeing_most_often():
    expected = [{"wins"}, {"wins"}, set(), set()]
    scores = [_scores(wins=0.35), _scores(wins=0.6), _scores(wins=0.2), _scores(wins=0.3)]
    best = ev.best_thresholds(expected, scores)
    assert 0.3 < best["wins"] <= 0.35  # 0.5 would miss the 0.35 notch; below 0.3 lets a wrong one in
    # And the Jev rule it is scored with is classify.parse's: never an empty set.
    assert ev.jev_categories(_scores(growth=0.3)) == {"growth"}


def test_the_cache_is_reused_for_the_same_model_and_redone_for_another(tmp_path, monkeypatch):
    first = ev.run(FakeClient(), cache_dir=tmp_path, workers=2)
    assert len(first["jev"]) == len(first["chat"]) == len(seed_db.ENTRIES)

    down = FakeClient(fail_with=ModelUnavailable("no calls expected"))
    assert ev.run(down, cache_dir=tmp_path) == first  # free re-score
    assert down.calls == []

    monkeypatch.setattr(config, "CHAT_MODEL", "some/other-model")
    with pytest.raises(ModelUnavailable):
        ev.run(down, cache_dir=tmp_path)  # a cache made by another chat model is not its answer
    assert json.loads((tmp_path / "categories-jev.json").read_text())["model"] == config.JEV_MODEL


def test_the_report_flags_tuned_thresholds_as_in_sample(tmp_path):
    lines = ev.report(ev.run(FakeClient(), cache_dir=tmp_path))
    assert any("in-sample" in line for line in lines)
    assert all(any(line.strip().startswith(c) for line in lines) for c in ev.CATEGORIES)


def test_the_command_fails_cleanly_without_a_key(capsys):
    assert ev.main([]) == 1
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err
