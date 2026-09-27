"""
llm.py — STEP 2 of the pipeline: the single Claude call.

ONE JOB: hand Claude the entries and get back a structured object with the
narrative sections written and (where needed) the entries semantically grouped.

WHAT THE MODEL DOES HERE — and, just as importantly, what it does NOT:

  DOES   write the narrative sections (the snapshot, the highlights, the
         reflection questions, the closing)
  DOES   decide which entries are "work that doesn't usually get counted" —
         that's a judgment call about what's meaningful but invisible
  DOES   group the collaboration entries into sub-patterns by meaning
  DOES   read an estimate-vs-actual out of prose like "thought it'd take a day,
         took three"

  DOESN'T count anything. Not tag totals, not percentages, not entries-per-week,
         not the sub-pattern tallies. All of that is arithmetic over rows we
         already have, so db.py does it in plain Python — free, instant, exact.
  DOESN'T draw anything. It never emits SVG, HTML, or chart markup. It returns
         numbers and labels; charts.py renders them with matplotlib. That's what
         makes the charts guaranteed-accurate rather than approximately-right.

We use TOOL USE with a forced schema rather than asking for JSON in prose. The
model has to fill in our fields, so we never parse free text and never have to
handle "the model wrapped it in a code fence again".

MODEL CHOICE: Claude Haiku 4.5. In the real product this runs on a schedule for
every user every week, so it needs to be the cheap, fast tier. This task —
summarize and group ~50 short entries — is well within what Haiku handles, and
paying frontier-model prices for it every week would be a bad trade.
"""

import os

import anthropic
from dotenv import load_dotenv

# Haiku 4.5: the cheap/fast tier. Report generation runs often (weekly digests
# per user), so the per-run cost matters more than squeezing out the last few
# points of quality.
MODEL = "claude-haiku-4-5-20251001"

MAX_TOKENS = 8000


class MissingAPIKeyError(Exception):
    """Raised when ANTHROPIC_API_KEY isn't usable — caught by the CLI entry points."""


# The literal value shipped in .env.example. Someone who copies the file but
# forgets to edit it has a key that is "set" but not real, so we catch it here
# and say so, rather than letting it through to a confusing 401 from the API.
PLACEHOLDER_KEY = "sk-ant-your-key-here"


def load_api_key():
    """
    Load .env and return ANTHROPIC_API_KEY. The one place the key is read.

    load_dotenv() does not overwrite variables already in the environment, so an
    exported key still beats the file — which is what you want when overriding
    for a single run.
    """
    load_dotenv()
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()

    if key == PLACEHOLDER_KEY:
        raise MissingAPIKeyError(
            ".env still has the placeholder key in it.\n"
            "  Open .env and replace sk-ant-your-key-here with your real key."
        )
    if not key:
        raise MissingAPIKeyError(
            "ANTHROPIC_API_KEY is not set.\n"
            "  Copy .env.example to .env and put your key in it:\n"
            "    cp .env.example .env\n"
            "  ...or export it for this session:\n"
            "    export ANTHROPIC_API_KEY=sk-ant-..."
        )
    return key


# ---------------------------------------------------------------------------
# The system prompt.
#
# The "Strengths & Growth" constraint is the important part. Left alone, models
# default to a strengths/weaknesses framing, which is exactly wrong for this
# product — the premise is that people undercount their own work, and handing
# them a list of deficiencies makes that worse. So the instruction is explicit,
# and the tool schema backs it up: there is no "weaknesses" field to fill in.
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are the report writer for Notch, a voice-first career impact tracker.

People speak short journal entries about their work days. You turn a set of those entries
into the written sections of a report they'll actually want to read.

VOICE
- Write like a thoughtful colleague who read every entry closely, not like an HR system.
- Second person ("you"), warm but not saccharine. No corporate filler, no exclamation marks.
- Be specific. Reference actual things that happened and actual stated results. Never invent
  a number, a name, or an outcome that isn't in the entries.

HARD CONSTRAINT ON STRENGTHS & GROWTH
Lead with what is working. Any growth area must be framed as BUILDING ON an existing strength,
never as a standalone weakness, gap, deficiency, or "area for improvement". Do not use those
words. The correct shape is: "You're already good at X — the next version of that is Y."
This is not a stylistic preference. It is a requirement.

WORK THAT DOESN'T USUALLY GET COUNTED
Pick 1-2 entries that are genuinely meaningful but easy to forget at review time: no ticket
attached, nobody asked for it, invisible unless it breaks. Documentation nobody requested,
a runbook fix, unblocking someone at a cost to your own day, careful interview feedback.
Do not just pick the two biggest wins — those already get counted.

DATES
Every date you write must be copied verbatim from the `date_display` field of the entry you're
referring to. Do not reformat, abbreviate, or reconstruct dates yourself.

You do not compute totals, counts, or percentages. Those are calculated separately. Write the
words; leave the arithmetic alone."""


# ---------------------------------------------------------------------------
# The tool schema — the forced shape of the response.
# ---------------------------------------------------------------------------

def _base_schema():
    """The narrative sections every report type shares."""
    return {
        "type": "object",
        "properties": {
            "opening_snapshot": {
                "type": "string",
                "description": (
                    "One short paragraph (2-4 sentences): what this period/project/tag was "
                    "mostly about. Do not include counts or percentages — those are added "
                    "separately as a stat line."
                ),
            },
            "dominant_themes": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "2-3 short theme phrases for the stat line, e.g. 'unblocking teammates', "
                    "'shipping under a flag'. Lowercase, no trailing punctuation."
                ),
            },
            "highlights": {
                "type": "array",
                "description": (
                    "Highlights GROUPED BY THEME, not in date order. 2-4 groups. Each group "
                    "collects entries that belong together conceptually."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "theme": {
                            "type": "string",
                            "description": "Short theme title, e.g. 'Catching problems before they shipped'",
                        },
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "what_happened": {
                                        "type": "string",
                                        "description": "One or two sentences on what happened.",
                                    },
                                    "impact": {
                                        "type": "string",
                                        "description": (
                                            "The stated impact, if the entry has one. Copy it from "
                                            "impact_note. Empty string if there isn't one — never invent."
                                        ),
                                    },
                                    "date": {
                                        "type": "string",
                                        "description": "The entry's date_display value, copied verbatim.",
                                    },
                                },
                                "required": ["what_happened", "impact", "date"],
                            },
                        },
                    },
                    "required": ["theme", "items"],
                },
            },
            "uncounted_work": {
                "type": "array",
                "description": "1-2 items of meaningful-but-invisible work. See the system prompt.",
                "items": {
                    "type": "object",
                    "properties": {
                        "what": {"type": "string", "description": "What they did."},
                        "why_it_matters": {
                            "type": "string",
                            "description": "Why it counts, and why it's easy to forget.",
                        },
                        "date": {"type": "string", "description": "date_display, copied verbatim."},
                    },
                    "required": ["what", "why_it_matters", "date"],
                },
            },
            "strengths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "2-3 things clearly working, each grounded in specific entries. "
                    "Full sentences."
                ),
            },
            "building_on": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "1-2 growth directions, each phrased as an EXTENSION of a strength named "
                    "above. Never a weakness, gap, or area for improvement."
                ),
            },
            "reflection_questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Exactly 2 short, specific questions drawn from these actual entries. "
                    "A generic question that could be asked of anyone is a failure."
                ),
            },
            "forward_frame": {
                "type": "string",
                "description": (
                    "Closing paragraph (2-4 sentences) connecting this period's pattern to "
                    "something concrete the person might do or say next — a conversation to "
                    "have, a claim they can now make. Not 'keep up the great work'."
                ),
            },
        },
        "required": [
            "opening_snapshot",
            "dominant_themes",
            "highlights",
            "uncounted_work",
            "strengths",
            "building_on",
            "reflection_questions",
            "forward_frame",
        ],
    }


def _schema_for(report_type):
    """
    Add the report-type-specific fields.

    Both additions below are genuine reading/judgment tasks — the only extra work
    we ask the model to do beyond writing:
      * tag reports need the collaboration entries clustered by MEANING
      * project reports need an estimate-vs-actual pulled out of conversational prose
    """
    schema = _base_schema()

    if report_type == "tag":
        schema["properties"]["subpattern_assignments"] = {
            "type": "array",
            "description": (
                "Assign EVERY entry to exactly one sub-pattern. Invent 3-4 sub-pattern names "
                "that genuinely describe the different kinds of work in this tag — for "
                "collaboration that might be 'Unblocking teammates', 'Cross-team work', "
                "'Catching issues before they shipped'. Use the exact same wording each time "
                "you reference a sub-pattern. This is the one classification job that needs "
                "your judgment; the counting is handled elsewhere."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "entry_id": {"type": "integer", "description": "The entry's id field."},
                    "subpattern": {"type": "string", "description": "The sub-pattern name."},
                },
                "required": ["entry_id", "subpattern"],
            },
        }
        schema["properties"]["subpattern_summary"] = {
            "type": "string",
            "description": "2-3 sentences on what the sub-patterns reveal. No percentages.",
        }
        schema["required"] += ["subpattern_assignments", "subpattern_summary"]

    if report_type == "project":
        schema["properties"]["estimate_vs_actual"] = {
            "type": "object",
            "description": (
                "If any entry states an estimate AND an actual duration ('thought it'd take a "
                "day, took three'), extract the clearest example. If no entry states both, set "
                "found to false and leave the other fields at zero/empty."
            ),
            "properties": {
                "found": {"type": "boolean"},
                "task": {"type": "string", "description": "Short label, e.g. 'Modal system migration'"},
                "estimated_days": {"type": "number"},
                "actual_days": {"type": "number"},
                "note": {"type": "string", "description": "One sentence on why it ran over or under."},
            },
            "required": ["found", "task", "estimated_days", "actual_days", "note"],
        }
        schema["required"].append("estimate_vs_actual")

    return schema


# ---------------------------------------------------------------------------
# Building the payload
# ---------------------------------------------------------------------------

def _format_entries(entries):
    """
    Render entries as readable text for the prompt.

    We include the id (so the model can reference entries in sub-pattern
    assignments) and the pre-formatted date_display (so it never has to format a
    date itself and get the style wrong).
    """
    lines = []
    for e in entries:
        lines.append(f"[id {e['id']}] {e['date_display']} — tags: {', '.join(e['tags'])}")
        lines.append(f"  said: {e['raw_text']}")
        if e["impact_note"]:
            lines.append(f"  stated impact: {e['impact_note']}")
        if e["acknowledged_by"]:
            lines.append(f"  recognized by: {e['acknowledged_by']}")
        lines.append("")
    return "\n".join(lines)


def _build_user_message(report_type, context, entries):
    """Assemble the human-readable brief that goes alongside the entries."""
    # Built with a branch rather than a dict literal on purpose: a dict would
    # evaluate all three f-strings, and only the project context carries a
    # 'project_name' key (only the tag context carries 'tag').
    who = f"{context['user_name']}, {context['user_role']}"
    if report_type == "project":
        header = (f"Write the PROJECT report for {who}, covering the project "
                  f"\"{context['project_name']}\" ({context['period_label']}).")
    elif report_type == "tag":
        header = (f"Write the TAG report for {who}, covering every entry tagged "
                  f"\"{context['tag']}\" across {context['period_label']}.")
    else:
        header = (f"Write the LAST 7 DAYS report for {who}, covering "
                  f"{context['period_label']}.")

    parts = [header, "", f"There are {len(entries)} entries. Here they are:", "", _format_entries(entries)]

    if report_type == "tag":
        parts.append(
            f"Remember: assign every one of these {len(entries)} entries to exactly one "
            f"sub-pattern in subpattern_assignments, using the entry ids above."
        )

    if report_type == "project":
        parts.append(
            "Remember: check the entries for any that state both an estimate and an actual "
            "duration, and fill in estimate_vs_actual from the clearest one."
        )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# The call itself
# ---------------------------------------------------------------------------

def generate_report_content(report_type, context, entries):
    """
    Make the one Claude call for this report and return the structured result.

    Returns a plain dict matching the schema above. Because we force tool use, the
    response is guaranteed to be a filled-in object — there is no free text to
    parse and no chance of getting prose back where we expected fields.
    """
    client = anthropic.Anthropic(api_key=load_api_key())

    tool = {
        "name": "write_report",
        "description": "Write the narrative sections of a Notch career impact report.",
        "input_schema": _schema_for(report_type),
    }

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        tools=[tool],
        # Forcing this specific tool is what guarantees structured output. The model
        # cannot reply with prose — it has to fill in our fields.
        tool_choice={"type": "tool", "name": "write_report"},
        messages=[{"role": "user", "content": _build_user_message(report_type, context, entries)}],
    )

    for block in response.content:
        if block.type == "tool_use":
            return block.input, response.usage

    # Only reachable if the API contract changes underneath us.
    raise RuntimeError("Claude did not return the expected structured response.")
