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

/v2 binds the client per request, as it does the real one: `bound(deadline=, usage=,
models=, provider=)` answers the same way and also writes a Usage call per answer
(`cost` dollars, the provider the fake says served it, a generation id), honours the
deadline (`delay` seconds per call, cut short by it), and notes each call's model and
provider block in `routed`. `generation(id)` and `zdr_endpoints()` answer zdr.py from
what the fake served and its `zdr` list, so a test picks a ZDR hit or a miss by naming
the provider: stt_provider / jev_provider / chat_provider.
"""

import itertools
import re
import time

import jsonschema

from .audio import AudioUnreadable
from .config import CHAT_MODEL, JEV_MODEL, STT_MODEL
from .openrouter import DeadlineExceeded, ModelRefused, TranscriptionFailed
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


# What the fake's zdr_endpoints() lists: OpenRouter's shape, reduced to what zdr.py reads.
ZDR_ENDPOINTS = (
    {"provider": "DeepInfra", "model": "openai/whisper-large-v3"},
    {"provider": "Groq", "model": "openai/whisper-large-v3"},
    {"provider": "TypeSafe", "model": "typesafe/jev-1.13-20260917"},
    {"provider": "DeepInfra", "model": "deepseek/deepseek-v4-pro-0813"},
)


class FakeClient:
    def __init__(self, *, fail_with=None, overrides=None, cost=0.001, delay=0.0, stt_provider="DeepInfra",
                 jev_provider="TypeSafe", chat_provider="DeepInfra", zdr=ZDR_ENDPOINTS):
        self.fail_with = fail_with
        self.overrides = overrides or {}
        self.calls = []
        self.cost, self.delay = cost, delay
        self.stt_provider, self.jev_provider, self.chat_provider = stt_provider, jev_provider, chat_provider
        self.zdr = [dict(e) for e in zdr]
        self.routed = []            # bound calls: {"method", "model", "provider"}
        self.generations = {}       # generation id -> {"provider", "model"}
        self.metadata_calls = []    # ("generation", id) / ("zdr",)
        self._ids = itertools.count(1)

    def bound(self, *, deadline=None, usage=None, models=None, provider=None):
        return _BoundFake(self, deadline, usage, models or {}, provider)

    def generation(self, generation_id):
        self.metadata_calls.append(("generation", generation_id))
        return self.generations.get(generation_id)

    def zdr_endpoints(self):
        self.metadata_calls.append(("zdr",))
        return [dict(e) for e in self.zdr]

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


class _BoundFake:
    """FakeClient.bound(): the same answers, metered and deadline-bound like OpenRouterClient.bound()."""

    def __init__(self, fake, deadline, usage, models, provider):
        self._fake, self._deadline, self._usage = fake, deadline, usage
        self._models, self._provider = models, provider

    def _call(self, method, kind, model, provider, answer, measure=lambda result: {}):
        self._fake.routed.append({"method": method, "model": model, "provider": self._provider})
        if self._usage is not None:
            self._usage.reached()
        if self._fake.delay:
            left = self._deadline.remaining() if self._deadline is not None else self._fake.delay
            time.sleep(max(0.0, min(self._fake.delay, left)))
        if self._deadline is not None and self._deadline.expired():
            raise DeadlineExceeded("The request's deadline ran out waiting for the model.")
        result = answer()
        extra = measure(result)
        generation_id = f"gen-fake-{next(self._fake._ids)}"
        self._fake.generations[generation_id] = {"provider": provider, "model": model}
        if self._usage is not None:
            self._usage.add({"kind": kind, "model": model, "provider": provider, "generation_id": generation_id,
                             "cost": self._fake.cost, "prompt_tokens": extra.get("prompt_tokens"),
                             "completion_tokens": extra.get("completion_tokens"), "seconds": extra.get("seconds")})
        return result

    def transcribe(self, audio, *, fmt="m4a", language="en"):
        model = self._models.get("stt", STT_MODEL)
        # The provider bills what it heard: a word takes the fake about 0.4 s.
        return self._call("transcribe", "stt", model, self._fake.stt_provider,
                          lambda: self._fake.transcribe(audio, fmt=fmt, language=language),
                          lambda text: {"seconds": round(0.4 * len(text.split()), 2)})

    def tool_call(self, **kwargs):
        model = self._models.get("chat", CHAT_MODEL)
        return self._call("tool_call", "chat", model, self._fake.chat_provider,
                          lambda: self._fake.tool_call(**kwargs),
                          lambda _: {"prompt_tokens": 100, "completion_tokens": 50})

    def decide(self, state, questions):
        model = self._models.get("classifier", JEV_MODEL)
        return self._call("decide", "classify", model, self._fake.jev_provider,
                          lambda: self._fake.decide(state, questions))
