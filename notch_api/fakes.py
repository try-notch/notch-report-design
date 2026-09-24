"""
fakes.py — offline doubles for the model client and the transcoder.

Shared by the test suite and `e2e/run_e2e.py --offline`, so the whole server can
run end to end with no key and no network. They are deterministic, and they are
held to the same shapes as the real thing:

  - FakeClient.tool_call validates its own canned answer against the `parameters`
    schema the caller passed, so if analysis.py or reports.py changes a tool
    schema and the fake no longer fits, the offline run fails instead of quietly
    proving nothing. label_entry answers only the fields that schema asks for, so
    the same fake serves the writing call and the extended fallback. The answer
    goes through the caller's `parse`, as the real client's does; a refusal from
    it is raised at once (the real client would ask once more first).
  - FakeClient.decide answers every Jev question it is asked in Jev's own answer
    shapes: a keyword-derived probability per category (under every threshold
    when no keyword matches, so the at-least-one rule is exercised), the first
    mood option, and the first project named in the entry, else `none`.
  - FakeClient.transcribe raises TranscriptionFailed on blank text, as the real
    client does, and fake_transcode raises AudioUnreadable on empty input.

Knobs: `fail_with` (an exception instance raised by every call while it is set),
`overrides` ({tool_name or "decide": {field: value}} merged over the canned answer,
NOT schema-checked, so a test can feed the caller deliberately messy model output),
and `calls` (every call, in order, as (method, kwargs)).
"""

import re

import jsonschema

from .audio import AudioUnreadable
from .openrouter import ModelRefused, TranscriptionFailed
from .store import normalize_tag

# Audio that starts with this marker transcribes to the UTF-8 text after it, so a
# test chooses its transcript by choosing its upload bytes.
TEXT_MARKER = b"NOTCH-TEXT:"

DEFAULT_TRANSCRIPT = (
    "Paired with Dana on the Front-End Refactor this afternoon and we finally shipped "
    "the new checkout form to staging. The flaky tests turned out to be a race in the "
    "mock server, which took most of the morning to track down."
)

# Always cited by write_report, so every report run exercises the id filter.
GHOST_ID = "ghost-id"

# (substring in the transcript, tag it produces) — a stand-in for the model's judgement.
_TAG_KEYWORDS = [("ship", "shipped"), ("pair", "pairing"), ("review", "code-review"),
                 ("test", "testing"), ("migrat", "migration"), ("bug", "bug-fix"),
                 ("doc", "docs"), ("incident", "incident"), ("interview", "hiring")]
_CATEGORY_KEYWORDS = {"wins": ("shipped", "fixed", "merged", "launched"),
                      "collaboration": ("paired", " with ", "helped", "reviewed"),
                      "challenges": ("flaky", "incident", "stuck", "slog")}
# Jev probabilities: a keyword hit, over every category's threshold, and no hit, under all of them.
_HIT, _MISS = 0.9, 0.1


def fake_transcode(data):
    """Stands in for audio.to_m4a_16k: passes bytes through, so TEXT_MARKER survives."""
    if not data:
        raise AudioUnreadable("The recording is empty.")
    return data


class FakeClient:
    def __init__(self, *, fail_with=None, overrides=None):
        self.fail_with = fail_with
        self.overrides = overrides or {}
        self.calls = []

    def _record(self, method, **kwargs):
        self.calls.append((method, kwargs))
        if self.fail_with is not None:
            raise self.fail_with

    def transcribe(self, audio, *, fmt="m4a", language="en"):
        self._record("transcribe", audio=audio, fmt=fmt, language=language)
        if not audio.startswith(TEXT_MARKER):
            return DEFAULT_TRANSCRIPT
        text = audio[len(TEXT_MARKER):].decode("utf-8").strip()
        if not text:
            raise TranscriptionFailed("No speech detected.")
        return text

    def tool_call(self, *, system, user, tool_name, description, parameters,
                  temperature=0.0, max_tokens=4000, parse=None):
        self._record("tool_call", system=system, user=user, tool_name=tool_name,
                     description=description, parameters=parameters,
                     temperature=temperature, max_tokens=max_tokens)
        if tool_name == "label_entry":
            payload = {k: v for k, v in _label_entry(user).items() if k in parameters.get("properties", {})}
        elif tool_name == "write_report":
            payload = _write_report(user)
        else:
            raise ModelRefused(f"FakeClient has no canned answer for {tool_name!r}.")
        jsonschema.validate(payload, parameters)
        answer = payload | self.overrides.get(tool_name, {})
        return parse(answer) if parse else answer

    def decide(self, state, questions):
        self._record("decide", state=state, questions=questions)
        entry = " ".join(v for v in state.values() if isinstance(v, str)).lower()
        answers = {name: _decision(name, question, entry) for name, question in questions.items()}
        return answers | self.overrides.get("decide", {})


# ---------------------------------------------------------------------------
# Canned answers, derived from the user message the caller built.
# ---------------------------------------------------------------------------

def parse_label_message(user):
    """analysis._user_message's message -> (transcript, the '- name' lines under 'Active projects')."""
    transcript, _, rest = user.removeprefix("Label this entry:\n\n").partition("\n\nActive projects")
    return transcript.strip(), [line[2:] for line in rest.split("\n\n", 1)[0].splitlines() if line.startswith("- ")]


def _decision(name, question, entry):
    """One question, answered in Jev's shape."""
    if question["type"] == "noul":
        hit = any(k in entry for k in _CATEGORY_KEYWORDS.get(name, ()))
        return {"type": "noul", "noul": _HIT if hit else _MISS}
    options = list(question["criteria"])
    if "none" in options:  # a match (the project): the first option the entry names
        pick = next((o for o in options if o != "none" and o.lower() in entry), "none")
    else:  # a forced pick (the mood): the first option, 'up'
        pick = options[0]
    return {"type": "choice", "choice": pick, "probabilities": {o: float(o == pick) for o in options},
            "confidence": 1.0}


def _label_entry(user):
    transcript, projects = parse_label_message(user)
    lower = transcript.lower()
    project = next((p for p in projects if p.lower() in lower), "")
    tags = ([normalize_tag(project)] if project else []) + [t for k, t in _TAG_KEYWORDS if k in lower]
    tags = tags[:5] + ["work-log", "reflection"][:max(0, 2 - len(tags))]
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", transcript) if s.strip()]
    return {
        "fixed_tags": [c for c, keys in _CATEGORY_KEYWORDS.items() if any(k in lower for k in keys)] or ["growth"],
        "tags": tags,
        "summary": sentences[0] if sentences else "You recorded a notch.",
        "takeaways": sentences[:2] or ["You showed up and recorded it."],
        "mood": "up",
        "impact_note": "",
        "acknowledged_by": "",
        "project_match": {"project_name": project, "confidence": "high" if project else "none"},
    }


def _write_report(user):
    ids = [i.strip() for i in re.findall(r"\[id ([^\]]+)\]", user)]
    first, second = (ids + [None, None])[:2]
    return {
        "headline": "Loose ends, tied off",
        "lede": "This stretch was mostly about finishing the things that had been hanging around.",
        "body": ("What is working is steady, visible progress: small pieces landing one after another.\n\n"
                 "Next, build on how well pairing went and bring someone in earlier on the next hard part."),
        "highlights": [
            {"title": "The hard part shipped", "detail": "it landed without drama",
             "kind": "shipped", "source_entry_ids": [i for i in (first, GHOST_ID) if i]},
            {"title": "Worked it out together", "detail": "pairing turned a slog into progress",
             "kind": "collaboration", "source_entry_ids": [second] if second else []},
            {"title": "Quiet groundwork", "detail": "the kind of work that rarely gets counted",
             "kind": "note", "source_entry_ids": [GHOST_ID]},
        ],
        "themes": ["shipped", "pairing", "momentum"],
    }
