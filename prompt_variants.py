"""
prompt_variants.py — the system prompts under test, one per experiment.

Tag NAMES and EXPLANATIONS do not live here. They live in the `tags` table
(seeded by seed_db.py) and are slipped into `{tag_catalog}` at call time by
build_prompt(). That is what makes the catalog growable: a new row in `tags`
shows up in the prompt and in the tool enum without editing this file.

Only the application rules differ between variants — how to decide, not what
the current tags are called. See TAGGING_EVAL.md.

    from prompt_variants import build_prompt
    prompt = build_prompt(db.get_tags(), "v4_stacking")
"""

_SHARED_PREAMBLE = """You label journal entries for Notch, a voice-first career impact tracker.

People speak for a couple of minutes about their work day. You read one entry — the raw
transcript, exactly as spoken — and pull out its structured pieces. You are not writing
anything a person will read. You are labelling.
"""

_SHARED_TAIL = """
AUTO TAGS
Short open-vocabulary keywords for what this entry is actually about: systems, technologies,
recurring kinds of work, the shape of the day. 'flaky tests', 'oncall', 'design review',
'code review', 'mentoring', 'incident'. Rules:
- Lowercase. Two or three words at most. Noun phrases, not sentences.
- Prefer the term a person would search for later, not a summary of the entry.
- REUSE BEFORE YOU COIN. You may be shown the keywords already in use for this user. If one
  of them fits this entry, use it verbatim, even if you would have phrased it slightly
  differently. Only invent a new keyword when nothing in the existing list covers the idea.
- Prefer the plain common term over an inventive one — 'oncall', not 'being on call'.
- Do not repeat a catalog tag as an auto tag. Catalog tags are the named list in FIXED TAGS.
- Do not include people's names. Do not invent a topic the entry doesn't mention.
- Three to six of them. Fewer is fine if the entry is short.

IMPACT NOTE
Only if the entry states a concrete result — a number, a measured outcome, a thing that
shipped or got adopted. One short clause, in the user's own terms. Empty string if the
entry doesn't state one. Never infer or estimate an impact.

ACKNOWLEDGED BY
Only if the entry says someone recognized or praised the work. Just the name. Empty string
otherwise. You are recording what the user said they were told, not verifying it.

PROJECT MATCH
You are given the user's active projects. Match only on real evidence in the entry — a
name, or an unambiguous reference to the work. 'the refactor' can match 'Front-End
Refactor'. A vague mention of frontend work cannot. When unsure, return an empty
project_name: no match is a good outcome, and a wrong one is worse than none."""


# ---------------------------------------------------------------------------
# v0 — names the catalog, defines none of the application rules.
# ---------------------------------------------------------------------------
V0_FIXED = """
FIXED TAGS
The closed list for this user is below. Choose every tag whose name genuinely applies,
and no others. Use each tag's exact name as written. Most entries take one or two.
Do not reach for a positive tag just to be encouraging.

{tag_catalog}
"""


# ---------------------------------------------------------------------------
# v1 — treat explanations as tests; tags may co-occur.
# ---------------------------------------------------------------------------
V1_FIXED = """
FIXED TAGS
Choose every tag from the closed list that genuinely applies, and no others. Most entries
take one or two. These are not moods — they are categories of work. Each explanation is
a test: apply the tag only if that test is met. Use each tag's exact name as written.

{tag_catalog}

Two tags very often co-occur. Applying only one because you already picked the other
is a mistake.
"""


# ---------------------------------------------------------------------------
# v2 — honour exclusions written into the explanations.
# ---------------------------------------------------------------------------
V2_FIXED = """
FIXED TAGS
These are categories of work, not moods. Apply every one whose explanation fits —
most entries carry two, and a bit under half carry one. Three is almost always wrong.
Use each tag's exact name as written.

{tag_catalog}

Read the exclusions in each explanation as strictly as the inclusions. If an
explanation says a situation is a different tag, use that other tag — do not stretch
this one to cover it.
"""


# ---------------------------------------------------------------------------
# v3 — the aboutness test, so passing remarks are not promoted into tags.
# ---------------------------------------------------------------------------
V3_FIXED = """
FIXED TAGS
These are categories of work, not moods. Apply every tag whose explanation fits.
Use each tag's exact name as written.

{tag_catalog}

THE ABOUTNESS TEST — apply this before adding any tag
A tag must describe what the entry is MAINLY ABOUT, not something it mentions in passing.
Ask: if I removed this aspect, would the entry still be the same story? If yes, do not tag
it. A passing aside is not a tag.

HOW MANY
Most entries take two tags; a bit under half take one. Three is essentially always wrong.
When you are hesitating over a second tag, leave it off — a passing mention is not a tag.
"""


# ---------------------------------------------------------------------------
# v4 — tags stack rather than compete. Aboutness kept, with explanations
# carrying the per-tag exceptions (happy endings, unasked work, etc.).
# ---------------------------------------------------------------------------
V4_FIXED = """
FIXED TAGS
These are categories of work, not moods. The list below is the closed set for this
user — apply every tag whose explanation fits, and no others. Use each tag's exact
name as written.

{tag_catalog}

TAGS DO NOT COMPETE — THEY STACK
Each tag is a separate yes/no question against its own explanation. Answering yes to
one never rules out another. If two explanations both fit, apply both.

THE ABOUTNESS TEST
A tag must describe what the entry is MAINLY ABOUT, not something it mentions in
passing. Ask: if I removed this aspect, would the entry still be the same story?
If yes, do not tag it. Honour any exception written into a tag's own explanation
(for example an explanation that says the tag still applies after a happy ending).

HOW MANY
Most entries take two tags; a bit under half take one. Three is essentially always
wrong unless three explanations clearly fit as the main story.
"""


VARIANTS = {
    "v0_bare": V0_FIXED,
    "v1_definitions": V1_FIXED,
    "v2_cardinality": V2_FIXED,
    "v3_procedure": V3_FIXED,
    "v4_stacking": V4_FIXED,
}


def format_tag_catalog(tags):
    """
    Render the catalog as the block the model reads.

    `tags` is [{"name": ..., "explanation": ...}, ...] from db.get_tags().
    Adding a sixth row in the database is enough — this loop does not care
    how many there are.
    """
    lines = []
    for tag in tags:
        lines.append(f"- {tag['name']} — {tag['explanation']}")
    return "\n".join(lines)


def build_prompt(tags, variant="v4_stacking"):
    """Assemble a full system prompt with this catalog slipped into the variant."""
    if variant not in VARIANTS:
        known = ", ".join(VARIANTS)
        raise KeyError(f"Unknown variant '{variant}'. Known: {known}")
    catalog = format_tag_catalog(tags)
    if not catalog:
        raise ValueError(
            "The tags table is empty. Run  python seed_db.py  first, or insert "
            "at least one tag name and explanation."
        )
    # replace(), not format() — explanations may contain braces.
    fixed = VARIANTS[variant].replace("{tag_catalog}", catalog)
    return _SHARED_PREAMBLE + fixed + _SHARED_TAIL
