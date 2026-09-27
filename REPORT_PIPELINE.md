# Notch — report generation pipeline (team overview)

This is the agreed picture of how Notch works end to end: from someone talking
about their day, to a finished PDF report with charts. Written for colleagues —
no code required to read it.

It mixes **what we decided for the product** with **what the demo repo does
today**. Sections marked *Built in demo* are working in this repo. Sections
marked *Planned* are agreed direction but not fully built yet.

---

## The big idea in one sentence

Someone talks for a couple of minutes. Notch saves what they said, labels it
while it's fresh, and later turns those labeled entries into a report they can
actually use — with numbers and charts that code computed, not guessed.

---

## Two separate AI moments (do not merge these)

This is the most important thing to explain to anyone new.

| When | What happens | AI's job |
| --- | --- | --- |
| **When the user speaks** | One journal entry is saved and labeled | Read one entry. Pull out tags, impact, project, etc. **No writing.** |
| **When the user asks for a report** | Many entries are fetched and summarized | Read many entries. Write the narrative. **No counting. No drawing.** |

Same product, two different calls, two different jobs. Tagging is small and
cheap (runs once per entry). Report writing is larger (runs when they export).

---

## What gets stored for each entry

Every journal entry lives in the database with:

| Field | What it is |
| --- | --- |
| **Raw text** | Exactly what they said, as transcribed. Never overwritten. |
| **Tags** | What *kind* of moment this was (see Tag system below). |
| **Impact note** | A concrete result they mentioned ("shipped X", "cut failures to zero"). Empty if none. |
| **Acknowledged by** | Who they said recognized the work. Empty if none. We record what *they* said — we don't verify it. |
| **Project** | Which of their active projects this entry belongs to, if any. |

*Built in demo:* all of the above exist. Tags and impact are seeded by hand in
the demo. The real tagging call is partly wired (`tagger.py`) but not the full
production flow yet.

---

## User profile (onboarding)

Each user has a small profile, collected during onboarding:

| Field | Example |
| --- | --- |
| **Name** | Jordan Kim |
| **Title** | Software Engineer |
| **Industry** | Technology |
| **Years of experience** | 5 |

*Built in demo:* stored in the `users` table. Reports today only use name and
title in the header; industry and years are in the database for the UI team and
for future personalization.

---

## Tag system — the agreed design

There are **only two kinds of tags.** No third layer of "maybe tags" or
invented keywords.

### 1. Core tags (always five)

These are the same for every user:

- `wins`
- `collaboration`
- `leadership`
- `growth`
- `challenges`

**Rules:**

- Every entry gets at least one core tag (applied at capture time).
- Definitions are **owned by Notch** — users cannot edit or delete them.
- Definitions live in the database (name + explanation), and the tagging AI
  reads that list each time. We can improve the wording without hard-coding it
  in the app.
- These always appear on **charts** and in **week-over-week comparisons**.

Core tags answer: *"What kind of work was this?"*

### 2. Personal tags (up to five per user)

These are **optional**. The user chooses names that matter to *their* career —
for example `mentoring`, `oncall`, `stakeholder management`.

**Rules:**

- **Maximum five** personal tags per user. To add a sixth, they delete one first.
- The **user picks the name**. They do **not** write the definition from scratch.
- When they add a tag:
  1. Check if something similar already exists in **their** tags.
  2. If not, check a **tag library** (~200–250 well-written definitions we keep
     backstage). If there's a match, reuse that name and definition.
  3. If there's still no match, the **AI writes a short definition**. The user
     can edit it, then save.
- Users **can edit or delete** personal tag definitions. Core tags stay locked.
- Personal tags are applied at capture time **alongside** core tags — an entry
  can be both `collaboration` and `mentoring`.
- Personal tags appear on **the same charts and reports as core tags**, including
  the week-over-week view. If someone tracked `mentoring` for a year, they
  should see mentoring on the weekly chart — not hidden because it isn't one
  of the five.

Personal tags answer: *"What slice of my work do I want to track and report on?"*

### What we are **not** doing for tags

- **No auto-tagging for tags.** We are not inventing random keywords from speech
  (`flaky tests`, `pairing`) and later asking "want to track this?" That was
  confusing and overlapped with projects. Tags are chosen deliberately — core
  ones by us, personal ones by the user.
- **No browsing a list of 250 tags.** The library is a backstage dictionary for
  matching and definitions only. The user never picks from 250 options.
- **No unlimited custom tags. (yet?)**  The cap of five keeps tagging accurate and charts
  readable.

### Similar tags (e.g. mentoring vs collaboration)

These are **not** the same tag.

- **Collaboration** = another person was involved in the work (broad, one of the
  five core tags).
- **Mentoring** = a specific *kind* of work the user chose to track (personal).

An entry can have **both**. We do not force the user to pick one. When they add
`mentoring`, we may warn that it's close to `collaboration` — but if they want
a mentoring-only report, they need mentoring as its own tag.

### When someone adds a personal tag mid-year

Old entries won't magically have that tag. When they save a new personal tag,
we offer: **"Apply to past entries?"** (e.g. last 90 days). If they skip it,
charts should show something like **"Mentoring tracked since [date]"** so zeros
before that date aren't misleading.

*Built in demo:* five **core** tags in the database with explanations. Personal
tags, the tag library, and the add-tag flow are **planned**.

---

## Projects (separate from tags)

**Projects** are named bodies of work the user is already tracking — e.g.
"Front-End Refactor", "Q3 launch".

At capture time, the AI tries to match the entry to one of their active
projects (same moment as tagging). The user **confirms or corrects** the guess —
same idea as project match today in the README.

Projects are **not** tags. They answer: *"Which piece of work was this part of?"*

A user can run a **project report** (everything on that project) independently
of tag reports.

*Built in demo:* projects table, project report, project match in tagging schema.

---

## Moment 1 — When the user speaks (capture pipeline)

```
Speech → transcription → raw text saved
                              ↓
                    tagging AI (one call per entry)
                              ↓
              tags, impact, acknowledgement, project guess
                              ↓
              user confirms project (if needed)
                              ↓
                         saved to database
```

**What the tagging AI returns** (structured, not prose):

```json
{
  "tags": ["collaboration", "wins"],
  "impact_note": "Cut flaky checkout failures to zero",
  "acknowledged_by": "Priya",
  "project_match": {
    "project_name": "Front-End Refactor",
    "confidence": "high"
  }
}
```

**What goes into the tagging prompt:**

- The user's **core tags** (five definitions from the database).
- The user's **personal tags** (up to five definitions), once that exists.
- Their **active project names** (for matching only).

The tagging AI does **not** see the whole 250-tag library — only this user's
small catalog (at most ten tags total).

*Built in demo:* `tagger.py` and prompt eval work (`TAGGING_EVAL.md`). Full
production capture flow (speech → tag → confirm) is still **planned**.

---

## Moment 2 — When the user asks for a report

### Report types

| Report | What it covers |
| --- | --- |
| **Last 7 days** | This week vs last week — tag mix, shift, narrative |
| **Project** | Everything tied to one project over its date range |
| **Tag** | Everything with one tag (core **or** personal) across a period |

Any tag in the user's catalog — including a personal tag like `mentoring` —
can have its own tag report **and** show up on the weekly mix chart.

### The pipeline (same for all report types)

```
1. FETCH     — SQL: pull the entries that match (by date, project, or tag)
2. COUNT     — Python: totals, percentages, week-over-week math
3. NARRATE   — One AI call: write sections + group entries by meaning (where needed)
4. CHARTS    — matplotlib: draw from the Python numbers only
5. PDF       — reportlab: assemble narrative + charts into one file
```

**Division of labour (non-negotiable):**

| Who | Does what |
| --- | --- |
| **AI** | Reads entries. Writes narrative. Groups by meaning (e.g. sub-patterns inside a tag). |
| **Python** | Every count, percentage, and comparison. |
| **Chart library** | Every bar and line. The AI never draws. |

This is why the charts are trustworthy: the model never computed "collaboration
was 40%" — code did.

### Last 7 days — week-over-week detail

1. Fetch **this week's** entries and **last week's** entries (two separate queries).
2. Python computes each tag as a **% of entries** for both weeks.
3. Python computes the shift (e.g. collaboration 20% → 40%).
4. The AI sees **only this week's entries** and writes about this week.
5. The **comparison chart** is built entirely from Python's numbers — the AI
   never sees last week's entries and never writes the comparison sentence.

The chart includes **all tags in the user's catalog** — core five plus any
personal tags they track (up to ten bars total).

An entry with two tags counts toward **both** bars. Percentages do not have to
add to 100%. That is intentional.

### Tag report — extra AI step

For a tag report (e.g. all `collaboration` entries), the AI also assigns each
entry to a **sub-pattern** ("unblocking teammates", "cross-team work", etc.).
Python then **counts** those labels for a breakdown chart. The AI names the
groups; code counts them.

### What the report contains (narrative sections)

Typical sections in the PDF:

1. Opening snapshot — what the period was mostly about
2. Highlights — grouped by theme, not date order
3. Work that doesn't usually get counted — invisible but meaningful entries
4. Strengths — what's clearly working
5. Building on strengths — growth framed as extending a strength, never a weakness list
6. Recognition received — who acknowledged their work (from the database, no AI)
7. Reflection questions — two specific questions from *their* entries
8. Forward frame — what they might do or say next
9. Charts — embedded, computed in Python

*Built in demo:* all three report types as CLI → PDF (`generate_report.py`).
Weekly chart today uses the **five core tags only**; extending charts to
personal tags is **planned** once personal tags exist.

---

## Tag library (backstage only)

We maintain roughly **200–250** tag names with good definitions — a reference
library, not something users browse.

**Used only when:** someone adds a personal tag.

| Step | What happens |
| --- | --- |
| User types a name | e.g. "mentoring" |
| Match their existing personal tags | Avoid duplicates |
| Match the library | Reuse name + definition if close enough |
| No match | AI generates a short definition → user edits → save |

The library is **never** sent to the daily tagging call. It keeps tagging fast
and accurate.

*Planned* — not in the demo database yet.

---

## What lives in the database (summary)

| Table | Purpose |
| --- | --- |
| **users** | Name, title, industry, years of experience |
| **tags** | Core tag catalog (name + explanation) — *demo has this* |
| **user_tags** (planned) | Personal tags per user (name + explanation, max 5) |
| **tag_library** (planned) | Backstage definitions for matching when adding a tag |
| **projects** | Named work the user is tracking |
| **entries** | Raw text, tags, impact, acknowledgement, project link |

---

## Demo repo vs product

| Piece | Demo today | Product target |
| --- | --- | --- |
| Report PDF pipeline | ✅ Works | Same |
| Five core tags in DB | ✅ Works | Same |
| Tagging eval / prompt quality | ✅ Works | Same approach |
| User onboarding fields | ✅ In DB | UI collects them |
| Capture-time tagging (full) | Partial | Tag every new entry |
| Personal tags (≤5) | Not built | User-named, AI-defined |
| Tag library | Not built | Match on add only |
| Weekly chart with personal tags | Not built | Core + personal |
| Keyword auto-tags for tags | Code exists, **not product** | **Dropped** |

---

## One-page flow (for slides)

```
TALK  →  transcribe  →  label entry (AI)  →  confirm project  →  save
                                                                    ↓
ASK FOR REPORT  →  fetch entries  →  count (code)  →  write (AI)  →  chart (code)  →  PDF
```

**Tags:** 5 core (everyone) + up to 5 personal (optional). All chartable. All
reportable.

**Trust rule:** AI reads and writes words. Code counts. Charts draw numbers code
already computed.

---

## Open questions (not decided yet)

These are fine to leave for later — listed so nobody assumes they're settled:

- Exact UI for add-tag, edit definition, delete tag, and backfill
- Whether industry / years of experience change report *wording* yet or just sit in the profile
- Career level and career goals (mentioned in older docs, not in onboarding yet)
- Quarterly / annual reports (need a summarize-then-summarize design)

If something here doesn't match what you remember deciding, say so — this doc
should be updated to match the team, not the other way around.
