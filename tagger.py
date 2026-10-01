#!/usr/bin/env python3
"""
tagger.py — the CAPTURE-TIME call. This is the other half of the pipeline.

There are two moments where a model is involved in Notch, and they are not the
same job:

  1. WHEN THE USER SPEAKS  (this file)   — read one entry, label it.
  2. WHEN THEY ASK FOR A REPORT (llm.py) — read many entries, write about them.

This one is small on purpose. It reads a single raw transcript and hands back
labels: nothing it returns is prose a person will read. That's why it can run
per-entry on the cheap tier without anyone caring how long the output is.

    python tagger.py                # fill in auto tags for untagged entries
    python tagger.py --limit 5      # cheap smoke test
    python tagger.py --overwrite    # re-tag entries that already have auto tags

TWO LAYERS OF TAG, AND WHY
--------------------------
FIXED tags come from the `tags` table — a closed catalog of name + explanation.
Every chart, percentage and week-over-week comparison in a report keys off them.
A closed catalog is what makes "this tag went from 0% to 80%" a claim that holds
up across weeks. The prompt never hardcodes the names; tagger.py loads the table
and slips it in, so a new row is enough to add a tag.

AUTO tags are open vocabulary — whatever the entry is actually about. They feed
search and surface themes the catalog can't hold, and they are never counted
into a chart, because an open vocabulary drifts: 'flaky tests' this week and
'test flakiness' next week are one idea stored as two strings.

Both come from this one call. See Backend.md for the full rule.

WHAT GETS WRITTEN
-----------------
`auto_tags`, and the predicted fixed tags into `model_tags` — stamped with the
prompt variant that produced them, because an unversioned prediction can't be
attributed later.

The live `tags` column is left alone on every entry that already has one. In
this demo those were written by hand in seed_db.py, which makes them a usable
ground truth — so eval_tags.py scores the model's predictions against them
without touching a row. In production the same rule protects a user's own
corrections. db.set_model_tags owns that decision; see the promotion rule
there. Scoring lives in eval_tags.py, not here: this file is product code, that
one is the measuring instrument. See TAGGING_EVAL.md.
"""

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor

import anthropic

import db
from llm import MissingAPIKeyError, load_api_key
from prompt_variants import build_prompt

# Which prompt variant ships. Changing this is a product change — re-run the
# eval before you do, and record the result in TAGGING_EVAL.md.
WINNING_VARIANT = "v4_stacking"

# Windows terminals default to cp1252, which can't encode the box-drawing and
# check marks used below — printing one raises UnicodeEncodeError and kills the
# run. Ask for UTF-8, and degrade to '?' rather than dying if that isn't
# available. Cosmetic output should never be able to fail a tagging pass.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Same cheap/fast tier as report generation, and the argument is even stronger
# here: this runs once per entry per user per day, not once per week.
MODEL = "claude-haiku-4-5-20251001"

MAX_TOKENS = 1000

# How many auto tags we keep per entry. An open vocabulary with no ceiling turns
# into a restatement of the entry rather than a set of handles for finding it.
MAX_AUTO_TAGS = 6

# Parallelism for the backfill. Small enough to stay well clear of rate limits.
WORKERS = 4


# The system prompt is assembled at call time: application rules from the
# winning variant, tag names and explanations from the `tags` table. Re-run
# python eval_tags.py --run v4_stacking  to reproduce its score.
# SYSTEM_PROMPT is not a constant — the catalog can grow without editing this file.


def _tool_schema(project_names, tag_names):
    """The forced shape of the response. One flat object per entry."""
    if not tag_names:
        raise ValueError(
            "The tags table is empty. Run  python seed_db.py  first, or insert "
            "at least one tag name and explanation."
        )
    if project_names:
        project_hint = (
            "Exact name of the matching project, copied verbatim from this list: "
            + "; ".join(project_names)
            + ". Empty string if none of them clearly match."
        )
    else:
        project_hint = "The user has no active projects. Always return an empty string."

    return {
        "type": "object",
        "properties": {
            "tags": {
                "type": "array",
                "items": {"type": "string", "enum": tag_names},
                "description": (
                    "Every tag from the closed catalog that applies. At least one. "
                    "Usually one or two. Use the exact names from the catalog."
                ),
            },
            "auto_tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "3-6 lowercase open-vocabulary keywords for what this entry is "
                    "about. See the system prompt for the rules."
                ),
            },
            "impact_note": {
                "type": "string",
                "description": (
                    "The concrete stated result, if the entry states one. Empty string "
                    "if not. Never invent or estimate."
                ),
            },
            "acknowledged_by": {
                "type": "string",
                "description": (
                    "Name of whoever recognized the work, if the entry says so. Empty "
                    "string otherwise."
                ),
            },
            "project_match": {
                "type": "object",
                "properties": {
                    "project_name": {"type": "string", "description": project_hint},
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "low", "none"],
                        "description": (
                            "'none' when project_name is empty. 'low' means the user "
                            "should be asked to confirm."
                        ),
                    },
                },
                "required": ["project_name", "confidence"],
            },
        },
        "required": ["tags", "auto_tags", "impact_note", "acknowledged_by", "project_match"],
    }


def _clean_auto_tags(raw, tag_names):
    """
    Normalize the open vocabulary as far as we can without a real eval.

    Lowercase, de-duplicate, drop anything that's just a catalog tag under another
    name, and cap the count. This does NOT solve drift — 'flaky tests' vs 'test
    flakiness' still slip through as two tags — which is exactly why auto tags
    stay out of charts until there's an eval saying they're consistent enough.
    """
    catalog = set(tag_names)
    seen, cleaned = set(), []
    for tag in raw:
        if not isinstance(tag, str):
            continue
        tag = tag.strip().strip(".,").lower()
        if not tag or tag in catalog or tag in seen:
            continue
        seen.add(tag)
        cleaned.append(tag)
    return cleaned[:MAX_AUTO_TAGS]


def tag_entry(client, raw_text, project_names, vocabulary=(), system_prompt=None,
              temperature=0):
    """
    The call itself: one raw transcript in, one labelled object out.

    Forced tool use, same as llm.py — the model has to fill in our fields, so
    there is no free text to parse.

    `tags` is the catalog from db.get_tags(). Names go into the tool enum;
    names and explanations are slipped into the system prompt. If omitted,
    the catalog is loaded from the database.

    `vocabulary` is the auto tags this user already has. Tagging one entry in
    isolation is what produces drift — the model has no way to know it said
    'migration documentation' last week, so it coins 'migration docs' today.
    Showing it the existing list is the fix. It goes in the user message rather
    than the system prompt because it changes per call, which keeps the system
    prompt static and cacheable.

    `system_prompt` defaults to the winning variant with this catalog injected,
    and exists so eval_tags.py can run a variant against the same entries
    without editing this file.
    `system_prompt` defaults to SYSTEM_PROMPT and exists so eval_tags.py can run
    a variant against the same entries without editing this file.

    `temperature` defaults to 0 — product behavior, one most-likely answer.
    eval_confidence.py raises it to sample the answer DISTRIBUTION instead;
    nothing in the product path should ever pass this.
    """
    if tags is None:
        tags = db.get_tags()
    tag_names = [t["name"] for t in tags]
    if system_prompt is None:
        system_prompt = build_prompt(tags, WINNING_VARIANT)

    tool = {
        "name": "label_entry",
        "description": "Extract the structured labels for one Notch journal entry.",
        "input_schema": _tool_schema(project_names, tag_names),
    }

    message = f"Label this entry:\n\n{raw_text}"
    if vocabulary:
        message += (
            "\n\nKeywords already in use for this user. Reuse one verbatim wherever it "
            "fits rather than coining a near-synonym:\n"
            + ", ".join(vocabulary)
        )

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        # Labelling wants the single most likely answer, not a sample from the
        # distribution. It also makes the eval reproducible — at the default
        # temperature the same prompt scores differently run to run, which makes
        # a 2-point movement impossible to read.
        temperature=temperature,
        tools=[tool],
        tool_choice={"type": "tool", "name": "label_entry"},
        messages=[{"role": "user", "content": message}],
    )

    for block in response.content:
        if block.type == "tool_use":
            result = dict(block.input)
            result["auto_tags"] = _clean_auto_tags(result.get("auto_tags", []), tag_names)
            result["tags"] = [t for t in result.get("tags", []) if t in tag_names]
            return result, response.usage

    # Only reachable if the API contract changes underneath us.
    raise RuntimeError("Claude did not return the expected structured response.")


# ---------------------------------------------------------------------------
# Terminal output — same visual language as generate_report.py.
# ---------------------------------------------------------------------------

DIM, GREEN, RED, RESET = "\033[2m", "\033[32m", "\033[31m", "\033[0m"


def _client():
    """One client for the whole backfill. Key comes from .env via llm.py."""
    return anthropic.Anthropic(api_key=load_api_key())


def _select(entries, overwrite, limit):
    """Which entries this run will touch."""
    if not overwrite:
        entries = [e for e in entries if not e["auto_tags"]]
    return entries[:limit] if limit else entries


def backfill(entries, project_names, existing_vocabulary=(), dry_run=False, tags=None):
    """
    Tag every entry, printing results in order as they land.

    Runs in chunks rather than one flat parallel map, because each call is shown
    the vocabulary built so far and a flat map would show every call the same
    empty list. Chunk-sized parallelism keeps it fast while letting the
    vocabulary grow as the run proceeds — which is also how this behaves in
    production, where entries arrive one a day and the vocabulary is simply
    whatever the user had accumulated by then.

    Returns the list of (entry, result) pairs.
    """
    if tags is None:
        tags = db.get_tags()
    client = _client()
    results = []
    vocabulary = set(existing_vocabulary)
    tokens_in = tokens_out = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for start in range(0, len(entries), WORKERS):
            chunk = entries[start:start + WORKERS]
            # Snapshot before the chunk so every call in it sees the same list.
            seen = sorted(vocabulary)

            def work(entry, seen=seen):
                return tag_entry(client, entry["raw_text"], project_names, seen,
                                 tags=tags)

            # executor.map preserves input order, so output reads chronologically
            # even though the calls finish out of order.
            for entry, (result, usage) in zip(chunk, pool.map(work, chunk)):
                tokens_in += usage.input_tokens
                tokens_out += usage.output_tokens
                results.append((entry, result))
                vocabulary.update(result["auto_tags"])

                if not dry_run:
                    db.set_auto_tags(entry["id"], result["auto_tags"])
                    # Records the fixed-tag prediction and the prompt that made
                    # it. Only promotes it to the live `tags` when the entry has
                    # none — it will not overwrite seeded ground truth or a
                    # correction someone made in the app.
                    db.set_model_tags(entry["id"], result["tags"], WINNING_VARIANT)

                print(f"  {DIM}[{entry['date_display']}]{RESET} "
                      f"{', '.join(result['auto_tags']) or DIM + '(none)' + RESET}")

    print()
    print(f"  {len(results)} entries · {tokens_in} in / {tokens_out} out")
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Run the capture-time tagging call over entries in notch.db.",
    )
    parser.add_argument("--limit", type=int,
                        help="Only process the first N entries. Useful as a smoke test.")
    parser.add_argument("--overwrite", action="store_true",
                        help="Re-tag entries that already have auto tags.")
    args = parser.parse_args()

    # Both checked up front, so a missing database or key fails immediately
    # instead of part-way through a backfill.
    try:
        all_entries = db.get_all_entries()
        project_names = db.list_project_names()
        tags = db.get_tags()
        load_api_key()
    except (db.NoDatabaseError, MissingAPIKeyError) as exc:
        print(f"\n  {RED}✗{RESET}  {exc}\n")
        return 1

    entries = _select(all_entries, args.overwrite, args.limit)

    if not entries:
        print(f"\n  Every entry already has auto tags. "
              f"Use {DIM}--overwrite{RESET} to re-tag them.\n")
        return 0

    catalog = ", ".join(t["name"] for t in tags)
    print()
    print("  Notch · capture-time tagging")
    print("  " + "─" * 56)
    print(f"  {len(entries)} entries · {MODEL} · prompt {WINNING_VARIANT}")
    print(f"  catalog: {catalog}")
    print()

    # Seed the vocabulary from entries that are already tagged, so a partial
    # backfill continues the vocabulary rather than starting a competing one.
    existing_vocabulary = {t for e in all_entries for t in e["auto_tags"]}

    try:
        results = backfill(entries, project_names, existing_vocabulary, tags=tags)
    except MissingAPIKeyError as exc:
        print(f"\n  {RED}✗{RESET}  {exc}\n")
        return 1
    except Exception as exc:  # noqa: BLE001 — explain itself rather than traceback
        print(f"\n  {RED}✗{RESET}  {type(exc).__name__}: {exc}\n")
        return 1

    vocabulary = {t for _, r in results for t in r["auto_tags"]}
    print(f"  {GREEN}✓{RESET}  Auto tags written · "
          f"{len(vocabulary)} distinct keywords across {len(results)} entries")
    print(f"     {DIM}python eval_tags.py --run {WINNING_VARIANT}  scores the fixed tags{RESET}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
