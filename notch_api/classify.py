"""
classify.py — the closed-set half of the capture-time analysis, asked of Jev.

A notch's five report categories, its mood and its project are choices from a
fixed list, not writing. Jev (OpenRouter's typed-decisions model) answers each as
a probability instead of a label, in a fraction of a second, so the cut-offs are
ours to set (config.CATEGORY_THRESHOLD, config.PROJECT_CONFIDENCE) and to measure
(eval_categories.py). The chat model keeps what is open-ended: tags, summary,
takeaways, impact note, acknowledgement.

  categories  one yes/no (`noul`) per category. `true` is the category's catalog
              explanation from seed_db, the measured v4 policy; `false` states
              what that explanation excludes. Applied at p >= CATEGORY_THRESHOLD;
              if none clears it, the likeliest one, so every notch carries one.
  mood        a `choice` over up / flat / down.
  project     a `choice` over the user's project names plus `none`, taken only
              when it is not `none` and its confidence >= PROJECT_CONFIDENCE.
              Not asked when the user has no projects.

The answers are untrusted: a missing or malformed one raises ModelRefused, which
analysis.py treats like any other Jev failure — it classifies with the chat model.
"""

import seed_db

from . import config
from .openrouter import ModelRefused

CATEGORIES = tuple(seed_db.TAGS)  # wins, collaboration, leadership, growth, challenges
MOODS = ("up", "flat", "down")
NO_PROJECT = "none"

# criteria.false per category: the cases its catalog explanation rules out, as one statement.
_EXCLUDES = {
    "wins": "Nothing concrete landed and no defect was caught: the entry is about helping someone else "
            "succeed, agreeing a plan, a meeting, or work still in progress or overrunning with no stated result.",
    "collaboration": "The person worked alone, or other people were only present (listening in a meeting, "
                     "mentioned in passing) without working anything out together.",
    "leadership": "The work was asked for or routine (reviewing a pull request, unblocking a teammate, "
                  "agreeing an approach in a meeting), and nobody stepped back to let someone else own it.",
    "growth": "The task moved but the person did not: no realisation, surprise, discomfort, put-off thing "
              "finally done, or self-aware aside.",
    "challenges": "The work went smoothly; any 'boring' or 'took longer than planned' is a passing remark on "
                  "an entry whose real story is a clean result.",
}
_EXPLANATIONS = dict(seed_db.TAG_CATALOG)
if set(_EXCLUDES) != set(_EXPLANATIONS):
    raise RuntimeError("seed_db.TAG_CATALOG and classify._EXCLUDES name different categories.")


def questions(project_names):
    """The Jev questions for one notch, keyed by the name each answer comes back under."""
    asked = {name: {
        "type": "noul",
        "instructions": f"Does the category '{name}' describe what this work journal entry is MAINLY about? "
                        "Judge the main story, not a passing mention. Judge this category on its own: "
                        "categories stack, so another one applying never rules this one out.",
        "criteria": {"true": _EXPLANATIONS[name], "false": _EXCLUDES[name]},
    } for name in CATEGORIES}
    asked["mood"] = {
        "type": "choice",
        "instructions": "How did the day feel to the person speaking? Their own feeling, not how good the work was.",
        "criteria": {"up": "Pleased, relieved, energised or proud about the day.",
                     "flat": "An uneventful or mixed day: neither clearly good nor clearly bad to them.",
                     "down": "The day wore them down: frustrated, tired, anxious or discouraged, "
                             "even if the work itself went well."},
    }
    if project_names:
        asked["project"] = {
            "type": "choice",
            "instructions": "Which of the person's projects is this entry about? Match only on real evidence: "
                            "the project's name, or an unambiguous reference to that work. A vague mention of "
                            "similar work is not a match, and no match is better than a wrong one.",
            "criteria": {name: f"The entry is about work on the project '{name}'." for name in project_names}
                        | {NO_PROJECT: "The entry is not clearly about any one of these projects."},
        }
    return asked


def classify(client, transcript, *, project_names):
    """One decisions call -> {categories, category_scores, mood, project_name}."""
    answers = client.decide({"journal_entry": transcript.strip()}, questions(project_names))
    return parse(answers, project_names)


def parse(answers, project_names):
    """Jev's answers -> the classification, or ModelRefused if any answer is missing or malformed."""
    scores = {name: _probability(answers.get(name), name) for name in CATEGORIES}
    categories = [c for c in CATEGORIES if scores[c] >= config.CATEGORY_THRESHOLD]
    project_name = None
    if project_names:
        answer = answers.get("project")
        choice = _choice(answer, "project", (*project_names, NO_PROJECT))
        if choice != NO_PROJECT and _confidence(answer, choice) >= config.PROJECT_CONFIDENCE:
            project_name = choice
    return {"categories": categories or [max(CATEGORIES, key=scores.get)], "category_scores": scores,
            "mood": _choice(answers.get("mood"), "mood", MOODS), "project_name": project_name}


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1 else None


def _probability(answer, name):
    p = _number(answer.get("noul")) if isinstance(answer, dict) else None
    if p is None:
        raise ModelRefused(f"Jev's answer to {name!r} is not a probability.")
    return float(p)


def _choice(answer, name, options):
    choice = answer.get("choice") if isinstance(answer, dict) else None
    if choice not in options:
        raise ModelRefused(f"Jev's answer to {name!r} is not one of the options.")
    return choice


def _confidence(answer, choice):
    """The answer's confidence, else the chosen option's probability, else 0: no evidence, no match."""
    probabilities = answer.get("probabilities")
    fallback = _number(probabilities.get(choice)) if isinstance(probabilities, dict) else None
    return next((p for p in (_number(answer.get("confidence")), fallback) if p is not None), 0.0)
