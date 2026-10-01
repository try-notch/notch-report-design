# Notch — report generation demo

A small, self-contained demo of how a Notch report gets generated end to end:
from raw voice-journal entries in a database, through one LLM call, through
deterministic chart rendering, to a finished PDF with the charts already
embedded.

This is **not production code**. It's a working thing to run in front of people.
No frontend, no app — just a CLI.

The server the iOS app talks to — the same pipeline behind the contract in
`notch-ios-dev` — lives in `notch_api/`; see [SERVER.md](SERVER.md). Read
[CONTRIBUTING.md](CONTRIBUTING.md) before changing either: it says which is which,
which branch to start from, and what to run.

---

## How the Pipeline Actually Works

There are two separate moments where AI is involved here, not one. The first
is **when the user speaks** — that's when the entry gets read and tagged. The
second is **when the user asks for a report**, days or weeks later — that's a
completely different call, doing a completely different job. Keeping these
two moments straight is the main thing to explain to a coworker seeing this
for the first time.

### The moment someone talks to Notch

**User speaks.** They talk naturally about their day — no typing, no forms.

**Speech becomes text.** A transcription tool — not the AI model that does
the "thinking," just a speech-to-text tool — converts what they said into
plain text. That text goes straight into the database exactly as spoken. This
is the `raw_text` column: the rawest, most unprocessed version of what they
said.

**The tagging AI reads that text.** Right after transcription, a small,
cheap AI call reads the raw text and pulls out the structured pieces Notch
needs:

- **Tags** — what kind of moment was this (a win, a collaboration moment, a
  leadership moment, growth, a challenge)
- **Impact note** — if the person mentioned a concrete result (a number, an
  outcome, a "this got used by X"), that gets pulled out as its own short note
- **Acknowledged by** — if the person mentioned that someone else recognized
  or praised the work, that gets pulled out too. This is just what the user
  said out loud — Notch isn't verifying it happened, it's capturing what the
  user remembers being told.
- **Project match** — the AI is also given the list of the user's currently
  active projects at this point, and tries to match the entry to one of them
  based on what was said (mentioning "the refactor" matches a project called
  "Front-End Refactor").

**What the tagging AI actually hands back.** This exact call isn't wired up in
this demo — `seed_db.py` just writes fake versions of these fields straight
into the database by hand, to stand in for it. But this is the shape a real
call would return: one small, plain object per journal entry, nothing else.

```json
{
  "tags": ["collaboration", "wins"],
  "impact_note": "Cut the flaky checkout test failure rate from 15% to zero",
  "acknowledged_by": "Priya",
  "project_match": {
    "project_name": "Front-End Refactor",
    "confidence": "high"
  }
}
```

Four short fields, no writing, no narrative — just facts pulled out of one
piece of raw text. Compare that to the report-generation example further
down: this one is small because its only job is reading and labeling, not
writing anything a person would read.

**The user gets a chance to confirm or correct the project match.** This is
the important part to call out: the AI's project guess is not final. After
tagging, the user sees which project (if any) the entry got matched to, and
can change it — pick a different project, or mark it as not tied to any
project at all. This keeps a person in control of their own record rather
than trusting the AI's guess blindly. If the AI didn't find a confident match
in the first place, the entry just stays unassigned to a project, which is
completely fine — it still shows up in daily and tag-based reports either
way, just not in a project-specific report.

**Everything gets saved.** By the time this is done, the entry in the
database has raw text, tags, an impact note (if there was one), who
acknowledged it (if anyone), and a project link (if there is one, and if the
user confirmed it) — all filled in before the user ever asks for a report.

### The moment someone asks for a report

**User taps "generate my report."** They pick which kind — last 7 days, a
specific project, or a specific tag.

**The app pulls the already-structured entries that match.** This is just a
database lookup, no AI involved yet, because all the hard work of tagging
already happened back when the entry was created.

**If the report needs to compare two time periods, the app pulls both,
separately.** The "Last 7 Days" report is the example of this today — it
shows this week against last week. To do that, the app looks up this week's
entries and last week's entries as two plain, separate database lookups. No
AI is involved in deciding what "last week" means or in fetching it — it's
just calendar math (today, minus seven days) and a second query.

**The app also does the comparison math itself, before AI ever gets
involved.** Once it has both sets of entries, the code counts them up —
what percent of this week was collaboration, what percent of last week was
collaboration — and works out the shift between the two. This is the same
counting the app always does, just run twice, once per period, and then
compared. The AI is never shown last week's entries and never told what the
comparison numbers are. It only ever sees the current period's entries, and
it only writes about the current period. The "this went up, that went down"
comparison exists purely in the code and in the chart — never inside
anything the AI produces. That's deliberate: it means the comparison chart
is always guaranteed to match the real numbers, because a model never
touched them.

**A second, separate AI call does two things:** it writes the actual report
narrative (the highlights, the reflection questions, the summary paragraph),
and — only where real judgment is needed — groups entries into meaningful
sub-patterns (for example, splitting a pile of "collaboration" entries into
"helping teammates," "cross-team work," and "catching issues early"). Simple
math, like what percentage of entries were collaboration this week, is not
done by the AI at all — that's just counting, handled directly by the code,
because it's faster, free, and always accurate.

**What the report-writing AI actually hands back.** Unlike the tagging call
above, this one is real — this is an actual response captured from running
this repo's `tag` report against the `leadership` entries. Every field name
below (`opening_snapshot`, `highlights`, `strengths`, and so on) is fixed in
advance by the code; the AI only fills them in, it never invents new ones.
Some list items are trimmed with `...` to keep this readable, but the shape
— every field, every level of nesting — is complete and unedited:

```json
{
  "opening_snapshot": "Over the past three months, you invested deliberately in the people and systems around you—not as an add-on to your work, but as the core of it. ...",
  "dominant_themes": [
    "teaching through pairing",
    "building shared knowledge",
    "enabling others to ship"
  ],
  "highlights": [
    {
      "theme": "Making room for teammates to own their work",
      "items": [
        {
          "what_happened": "You paired with Nina on her first real feature, letting her drive while you answered questions...",
          "impact": "Nina shipped the table migration on her own the following week",
          "date": "Jun 23, 2026"
        },
        { "...": "...two more items in this group..." }
      ]
    },
    { "...": "...two more theme groups, same shape..." }
  ],
  "uncounted_work": [
    {
      "what": "Updated the on-call runbook, correcting four out-of-date steps before the next rotation.",
      "why_it_matters": "This kind of preventive work doesn't show up in a sprint or a shipped feature, but it directly prevents a 3am crisis. ...",
      "date": "May 19, 2026"
    },
    { "...": "...one more item..." }
  ],
  "strengths": [
    "You're skilled at knowing when to let others do the work, even when it costs you time in the moment. ...",
    "...",
    "..."
  ],
  "building_on": [
    "You're already comfortable questioning designs and proposing simpler solutions—the next version of that is being the person who shapes the RFC and gets team alignment before any code changes.",
    "..."
  ],
  "reflection_questions": [
    "When you paused to write docs for the search service and the event schema instead of moving to the next ticket, what made you decide that was worth the time?",
    "..."
  ],
  "forward_frame": "You've built a pattern of thinking ahead: preventing problems (the runbook fix, the interview feedback), enabling others (the pairing, the docs), and removing toil (the codemod). ...",
  "subpattern_assignments": [
    { "entry_id": 46, "subpattern": "Pushing back on design to simplify" },
    { "entry_id": 49, "subpattern": "Preventive systems work" },
    { "entry_id": 42, "subpattern": "Preventive systems work" },
    { "...": "...one entry_id/subpattern pair per entry, 12 total for this run..." }
  ],
  "subpattern_summary": "Your work breaks into four distinct patterns: pairing sessions where you step back and let others drive (five entries), proactive documentation that prevents future questions (three entries), ..."
}
```

Notice what's *not* in there: no percentages, no counts, no chart data, no
mention of how many entries fell into each sub-pattern. `subpattern_assignments`
is just an entry id paired with a label — the tallying (`5 entries, 41.7%`)
happens afterward, in Python, from this list. That's the same rule from the
top of this document showing up concretely: the AI reads and writes, it
never counts.

**The numbers get turned into charts.** A separate, non-AI step takes those
percentages and produces the actual chart images — bar charts, comparisons,
and so on. This step is deliberately not done by the AI, because a chart
needs to be guaranteed accurate to the numbers, not guessed at.

**Everything gets assembled into one finished PDF** — the written report
plus the charts, already combined, already formatted. The user doesn't build
anything, click into a dashboard, or wait for separate pieces to load — they
tap one button and get one finished document back.

### What's in the database, in plain terms

| Column | What it means |
| --- | --- |
| `raw_text` | Exactly what the user said, transcribed, untouched |
| `tags` | What kind of moment this was (win, collaboration, leadership, growth, challenge) |
| `impact_note` | The concrete result, if the user stated one |
| `acknowledged_by` | Who the user said noticed or praised this, if anyone |
| `project_id` | Which project this belongs to, if any — guessed by AI, confirmed or corrected by the user |

### Why it feels effortless

The whole system only feels effortless to the user because all the hard work
— transcribing, tagging, matching — happens quietly right after they talk,
not later, and not by asking them to fill anything in themselves.

---

## The point of the demo

There's one architectural claim this is built to demonstrate:

> **The LLM reads and writes. Python counts. Matplotlib draws.**

Concretely:

- The LLM is **never** asked to compute a number. Tag counts, percentages,
  entries-per-week, sub-pattern tallies — all of it is plain arithmetic over rows
  we already have, so `db.py` does it in Python. It's free, instant, and exact.
- The LLM is **never** asked to draw a chart. It doesn't emit SVG, HTML, or chart
  markup. It returns structured numbers and labels; `charts.py` renders them with
  matplotlib. That's what makes the charts *guaranteed* accurate rather than
  approximately right.
- Both steps happen inside **one backend pipeline**. By the time
  `generate_report.py` prints "done", the PDF is complete with charts baked in.
  Nothing about charting is deferred to a hypothetical frontend.

The one place the model does real classification work is **semantic
sub-clustering** — splitting the `collaboration` entries into "unblocking
teammates" vs "cross-team work" vs "catching issues before they shipped". That's
a judgment about meaning, not arithmetic, so it's the right job for a model. Even
there the split is clean: **the LLM labels each entry, and Python counts the
labels.** The grouping is the model's call; the percentages on the chart are
exact.

Model is **Claude Haiku 4.5** (`claude-haiku-4-5-20251001`), deliberately — in
the real product this runs on a schedule for every user every week, so it needs
to be the cheap/fast tier. Each report is one call at roughly 500–1,700 input
tokens.

---

## Setup

Needs **Python 3.11+**.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Set your API key — either in a `.env` file (loaded automatically) or as an
environment variable:

```bash
cp .env.example .env      # then edit it
# ...or:
export ANTHROPIC_API_KEY=sk-ant-...
```

Then build the fake database:

```bash
python seed_db.py
```

It prints a summary so you can sanity-check the data before demoing:

```
  Seeded notch.db
  ----------------------------------------------------
  User            Jordan Kim (Software Engineer)
  Entries         52
  Date range      2026-05-06 to 2026-08-03
  Project         Front-End Refactor (2026-06-08 to 2026-07-17, 15 entries)
  Acknowledged    18 entries (34%)

  Tag counts
    wins            22  ######################
    collaboration   21  #####################
    ...
```

`seed_db.py` is safe to re-run — it drops and rebuilds the tables. Dates are
generated relative to the day you run it, so "Last 7 Days" always has content.

---

## Running the three reports

```bash
python generate_report.py --type last7days
python generate_report.py --type project --project "Front-End Refactor"
python generate_report.py --type tag --tag collaboration
```

PDFs land in `output/`, chart PNGs in `output/charts/`.

Each run narrates itself as it goes, which is the part worth watching:

```
  Notch · report generation
  ──────────────────────────────────────────────────────────
  1/5  Fetching entries from SQLite…
     21 entries tagged "collaboration" out of 52 total
  2/5  Computing counts and percentages in Python…
     collaboration is 40.4% of all entries in the period
  3/5  Calling Claude (claude-haiku-4-5-20251001) for narrative + sub-clustering…
     structured response received in 6.2s (1823 in / 1502 out)
     Claude grouped them into 3 sub-patterns; Python counted:
       · Unblocking teammates: 8 entries (38.1%)
       · Cross-team work: 7 entries (33.3%)
       · Catching issues before they shipped: 6 entries (28.6%)
  4/5  Rendering charts with matplotlib (deterministic — no LLM)…
  5/5  Assembling PDF with charts embedded…

  ✓  Report ready — output/tag_collaboration.pdf (60 KB)
```

---

## How this maps to the pipeline

```
  ┌─────────────────────────────────────────────────────────────────┐
  │  generate_report.py  — CLI, orchestrates the five steps         │
  └─────────────────────────────────────────────────────────────────┘
            │
   STEP 1   ▼   db.py                                    [ no LLM ]
   FETCH        Plain SQL. One query per report type:
                last 7 days / by project / by tag.
            │
   STEP 2   ▼   db.py                                    [ no LLM ]
   COUNT        Plain Python arithmetic — tag counts, percentages,
                entries-per-week, recognition filter.
                Free, instant, 100% accurate.
            │
   STEP 3   ▼   llm.py                             [ ONE Claude call ]
   NARRATE      Claude Haiku 4.5, tool-use with a forced JSON schema.
   + CLUSTER    Returns: the narrative sections, which entries are
                "uncounted work", the estimate-vs-actual pulled out of
                prose, and a sub-pattern LABEL for each entry.
                Never returns a count. Never returns chart markup.
            │
            │   ...and the sub-pattern labels go straight back to
            │      db.count_subpatterns() — Python does the tallying.
            │
   STEP 4   ▼   charts.py                                [ no LLM ]
   RENDER       matplotlib takes the numbers from step 2 and draws
                bar charts. Saves PNGs. This step is what guarantees
                the charts match the data.
            │
   STEP 5   ▼   report_builder.py                        [ no LLM ]
   ASSEMBLE     reportlab lays out the 8 sections and embeds the PNGs.
                Output: a finished PDF. Nothing left for a frontend.
```

### Files

| File                 | Its one job                                        |
| -------------------- | -------------------------------------------------- |
| `seed_db.py`         | Build the fake SQLite database                     |
| `db.py`              | SQL queries + all the Python-side arithmetic       |
| `llm.py`             | The single Anthropic call + the forced tool schema |
| `charts.py`          | matplotlib functions: numbers in, PNG path out     |
| `report_builder.py`  | reportlab PDF assembly                             |
| `generate_report.py` | CLI entry point and progress output                |

---

## Report structure

All three types use the same eight sections in the same order:

1. **Opening Snapshot** — a short paragraph plus a stat line
2. **Highlights** — grouped by theme, not chronology
3. **Work That Doesn't Usually Get Counted** — the LLM identifies these
4. **Strengths & Growth** — leads with what's working; growth is framed as
   *building on* a strength, never as a weakness. This is enforced twice: once
   in the system prompt, and once structurally — the tool schema has `strengths`
   and `building_on` fields and no field a "weaknesses" list could go in.
5. **Reflection Questions** — 2, specific to the actual entries
6. **Recognition Received** — every entry with an `acknowledged_by`, pulled
   straight from the database (no LLM — it's a filter, not a judgment)
7. **Data Visualization** — chart images, embedded
8. **Forward Frame** — a closing paragraph pointing at something concrete

Dates always carry the year: `(Jul 29, 2026)` for single dates,
`June 15 – July 30, 2026` for ranges.

### Charts per report type

| Report        | Charts                                                                                  |
| ------------- | --------------------------------------------------------------------------------------- |
| **Last 7 Days** | This week's tag mix (%) vs last week's, grouped bars — the week-over-week comparison    |
| **Project**   | Tag mix across the project · entries per week · estimated vs actual days (if the entries contain it) |
| **Tag**       | The tag as a share of all entries · the within-tag sub-pattern breakdown                |

---

## Notes on the seed data

The fake data is shaped deliberately, because the reports depend on that shape:

- The **last two weeks have opposite tag mixes** — a collaboration-heavy week
  (80% of entries) following a solo-build week (0%). That's what makes the
  week-over-week chart show a real shift rather than noise.
- **"Front-End Refactor"** spans ~6 weeks with 15 entries covering every tag,
  including two that state an estimate and an actual ("thought it'd be a one-day
  job, took three").
- **21 entries are tagged `collaboration`**, falling into three genuinely
  distinguishable sub-patterns — so there's something real for the LLM to find.
- `raw_text` is written the way a person actually talks, not as resume bullets.
  That's the premise of the product: the user rambles, the system does the
  translation.

---

## Failure cases

Handled with a clear message rather than a traceback, so a live demo doesn't die:

- No `ANTHROPIC_API_KEY` → tells you to set it
- No `notch.db` → tells you to run `seed_db.py`
- Unknown project name → lists the projects that do exist
- Unknown tag → lists the tags in use
- No entries in the window → says so
