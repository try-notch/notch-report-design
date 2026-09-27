# Backend

The requirements the Notch backend is built against. This is the source of
truth for scope — where [Notch.md](Notch.md) or [README.md](README.md) disagree
with this file, this file wins.

Status markers reflect the demo in this repo:
**`[built]`** works today · **`[partial]`** partly there · **`[planned]`** not started.

---

## User Info

- `[built]` Job title
- `[planned]` Career level
- `[planned]` Career goals

Career level and goals exist so a report can be aimed at where the user is
trying to get, not just at what they did. Job title alone is currently passed
to the report model as an identity line; it does not yet change how a report is
written.

---

## Capture and Processing

- `[planned]` Process 2–5 minutes of speech into text
- `[built]` Store the transcript verbatim and permanently — everything else is
  derived from it, and the raw text is never overwritten
- `[partial]` Tag each entry (see **Tagging** below)
- `[planned]` Extract a summary and key bullet points for what the user did
  that day
- `[partial]` Match the entry to one of the user's active projects, with the
  user able to confirm or correct the match

### Tagging

Two layers, deliberately. They answer different questions and are stored
separately.

**Fixed tags** — a closed vocabulary of five:
`wins`, `collaboration`, `leadership`, `growth`, `challenges`.

- Every chart, percentage, and week-over-week comparison keys off this list.
  A closed set is what makes "collaboration went from 0% to 80% this week" a
  sentence that means something and can be plotted.
- The set does not grow without a migration. Adding a sixth tag changes every
  historical comparison.

**Auto tags** — an open vocabulary of extracted keywords, unbounded.

- Free-form terms drawn from what the user actually said: technologies,
  systems, people, recurring kinds of work. `flaky tests`, `oncall`,
  `design review`, `mentoring`.
- Feeds search, surfaces themes the fixed five can't express, and lets tagging
  track a user's own goals rather than a taxonomy we picked.
- Never feeds a chart percentage or a comparison. Open vocabularies drift —
  `flaky tests` this week and `test flakiness` next week are the same idea and
  two different strings — so they are not a countable base.

Both layers come from the same model call at capture time. Quality of the
auto tags is expected to need eval work; the fixed tags are the reliable spine
that reports are built on regardless.

---

## Storage, Security and Search

- `[built]` Local SQLite database on the user's device
- `[built]` Entries filterable by tag, project, and date
- `[planned]` Entries searchable by free text and by auto tag
- `[planned]` Encryption at rest

### Where model calls fit

Two calls touch entry text: tagging at capture time, and report generation.
Both go to a hosted model API. That is accepted, with these constraints:

- The database is the user's, on their device. There is no Notch-side copy of
  entries and no server-side retention.
- Entry text is sent transiently, for the duration of a call, and not stored
  by us.
- No employer-facing view exists, and none is planned. Export is
  user-initiated.
- Encryption at rest is a requirement, not yet implemented. Given the content —
  people describing conflicts with managers, or building a case to leave —
  this is the highest-priority security item on the list.

---

## Reports and Output

- `[built]` Weekly snippet — tag breakdown plus week-over-week shift
- `[built]` Project report — tag mix, effort per week, estimate vs. actual
- `[built]` Tag report — the tag's share of the period, plus sub-patterns
  within it
- `[planned]` Gap identification in the weekly snippet — what the user has
  *not* logged, read against their career goals
- `[planned]` Quarterly reports
- `[planned]` Annual reports
- `[built]` PDF export
- `[partial]` Reports personalized to job title, career level, and goals

Project and tag reports are first-class, not demo conveniences: they are how a
user assembles evidence for one specific claim ("here is everything I did on
the refactor") rather than a time-boxed summary.

### Long-horizon reports

Quarterly and annual reports cover far more entries than a single model call
takes. They need a two-stage shape — summarize each period, then summarize the
summaries — rather than an extension of the single-call design used by the
weekly, project, and tag reports.

---

## Division of labour

Not negotiable, and the reason report output can be trusted:

- **The model reads and writes.** Narrative, judgment about which work matters,
  grouping entries by meaning.
- **Python counts.** Every total, percentage, tally, and comparison. Free,
  instant, exact.
- **The chart library draws.** The model never emits chart markup of any kind.

The model never computes a number and never draws. It does select and frame —
which entries become highlights, how a strength is phrased — and that judgment
is the product.
