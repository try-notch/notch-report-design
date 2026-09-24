"""
classify.py: what Jev's answers become. The thresholds are where a notch gains or
loses a category or a project, so they are tested at their edges; a malformed answer
must be a refusal (analysis.py then falls back to the chat model), never a guess.
"""

import pytest

import seed_db
from notch_api import classify
from notch_api.openrouter import ModelRefused

PROJECTS = ["Billing Migration", "Front-End Refactor"]


def _answers(scores=None, mood="flat", project=None):
    """Jev's answer shapes. `scores` defaults to 0.1 for every category; `project` is an answer dict."""
    scores = {c: 0.1 for c in classify.CATEGORIES} | (scores or {})
    answers = {c: {"type": "noul", "noul": p} for c, p in scores.items()}
    answers["mood"] = {"type": "choice", "choice": mood, "probabilities": {mood: 0.8}, "confidence": 0.8}
    return answers | ({"project": project} if project else {})


def _project(choice, **fields):
    return {"type": "choice", "choice": choice} | fields


def test_the_questions_carry_the_measured_catalog_and_ask_about_projects_only_when_there_are_some():
    asked = classify.questions(PROJECTS)
    assert {name: q["criteria"]["true"] for name, q in asked.items() if q["type"] == "noul"} == dict(
        seed_db.TAG_CATALOG)
    assert list(asked["project"]["criteria"]) == [*PROJECTS, classify.NO_PROJECT]
    assert list(asked["mood"]["criteria"]) == list(classify.MOODS)
    assert "project" not in classify.questions([])


@pytest.mark.parametrize("scores, expected", [
    ({"wins": 0.5, "growth": 0.49}, ["wins"]),                      # the threshold itself applies
    ({"wins": 0.9, "challenges": 0.7}, ["wins", "challenges"]),     # categories stack, catalog order
    ({"growth": 0.3, "challenges": 0.2}, ["growth"]),               # none clears it: the likeliest one
])
def test_categories_are_thresholded_and_never_empty(scores, expected):
    result = classify.parse(_answers(scores), [])
    assert result["categories"] == expected
    assert result["category_scores"]["wins"] == scores.get("wins", 0.1)


@pytest.mark.parametrize("project, expected", [
    (_project("Front-End Refactor", confidence=0.5), "Front-End Refactor"),         # at the confidence bar
    (_project("Front-End Refactor", confidence=0.49), None),                        # just under it
    (_project("Front-End Refactor", probabilities={"Front-End Refactor": 0.7}), "Front-End Refactor"),
    (_project("Front-End Refactor"), None),                                         # no confidence at all
    (_project("none", confidence=0.99), None),                                      # a confident "none"
])
def test_a_project_is_taken_only_when_jev_is_confident(project, expected):
    assert classify.parse(_answers(project=project), PROJECTS)["project_name"] == expected


@pytest.mark.parametrize("answers, projects", [
    ({k: v for k, v in _answers().items() if k != "growth"}, []),              # a category unanswered
    (_answers({"wins": 1.5}), []),                                             # not a probability
    (_answers({"wins": True}), []),                                            # a bool is not a number here
    (_answers(mood="ecstatic"), []),                                           # not one of the options
    (_answers(project=_project("Atlas", confidence=0.9)), PROJECTS),           # not one of the user's projects
    (_answers(), PROJECTS),                                                    # the project question unanswered
], ids=["missing", "out-of-range", "bool", "bad-mood", "unknown-project", "no-project-answer"])
def test_a_malformed_answer_is_a_refusal(answers, projects):
    with pytest.raises(ModelRefused):
        classify.parse(answers, projects)
