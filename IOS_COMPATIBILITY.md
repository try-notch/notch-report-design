# How far this repo is from the backend the iOS app expects

A measurement, not a plan. It compares what this repo builds today against the
backend `notch-ios-dev` is written to call, field by field, so the gap has a
number and a list instead of a feeling.

**Measured against**

| Side | Commit | Source of truth read |
| --- | --- | --- |
| This repo | `jeet-dev` @ `838749b` (Aug 25) | the code, not the docs |
| `notch-ios-dev` | `main` @ `5687f90` (Sep 7) | `docs/data-and-backend-integration.md` §3 (schema, which that doc calls canonical) and §5 (API), `docs/discrepancies.md`, `Packages/NotchKit/Sources/NotchAPI/NotchBackend.swift` |

`main` of this repo is three weeks behind `jeet-dev` and has no tagger at all, so every
number below is the better of the two.

---

## Verdict

**Conceptually close, structurally far.** The iOS design adopts this repo's core
rule word for word — the model reads and writes, code counts
(`data-and-backend-integration.md` §2.1, "Report narrative") — and keeps every
AI-side idea that matters: a cheap capture-time call, project matching the user
confirms, `acknowledged_by` and `impact_note` as report inputs, reports rendered
from structured JSON. What it expects around those ideas does not exist here: no
HTTP layer, no users beyond a hard-coded id 1, no transcription, no job model, and
different shapes for both objects the app reads — the entry and the report.

### Scorecard

✓ same name, type and meaning · ◐ the data exists or is computed here, but under
another name, type or shape, or it is never persisted or returned · ✗ nothing here
produces it.

| Surface | Expected | ✓ | ◐ | ✗ |
| --- | ---: | ---: | ---: | ---: |
| HTTP endpoints (§5) | 21 | 0 | 0 | **21** |
| Tables (§3) | 10 | 0 | 3 | 7 |
| `entries` columns (§3.3) | 23 | 3 | 5 | 15 |
| Server-written entry fields — the capture analysis (§3.10, **S**) | 9 | 0 | 4 | 5 |
| Report object fields (`GET /v1/reports/{id}`) | 16 | 0 | 10 | 6 |
| Cross-cutting mechanics (below) | 10 | 0 | 1 | 9 |

No surface has a single exact match on the wire. The ◐ column is where the work
is cheap: the value is already computed or extracted and only needs renaming,
reshaping or saving.

---

## Where the two already agree

These survive any port unchanged and are the reason this repo is worth porting
rather than rewriting.

- **Division of labour.** iOS §2.1: "The backend's original rule … is restored
  intact." The server counts at report acceptance and the model writes the prose.
- **Project match is a guess.** The tagger returns a name and a confidence
  (`tagger.py`, `project_match`); iOS §3.2 has the worker resolve that name with a
  folded `SELECT` and never create a project. Same design.
- **`acknowledged_by` and `impact_note`** are extracted at capture and read only by
  the report (iOS register E5). Same design.
- **`role` reaches the report prompt** (iOS A1; `llm.py` puts it in the brief).
- **Forced tool-use schemas and a cheap model** for both calls. Nothing in the iOS
  docs contradicts Haiku.
- **Structured output, not a PDF.** iOS D1 renders reports natively and builds the
  PDF on device, so `charts.py` and `report_builder.py` become unnecessary rather
  than wrong.

## Where they disagree — decisions, not code

Each of these has to be decided before code can match, because the two repos
have each already decided, differently.

**1. Where the record lives.** `Notch.md` and `Backend.md`: "The database lives on
your device. There is no Notch-side copy of your entries." iOS §2 (Sep 7): "The
server is the system of record" — Supabase Postgres, audio uploaded and kept seven
days, transcripts server-side, driven by Android needing to read the same record.
This repo is public and still makes the first promise.

**2. Tags are inverted.** Both repos have two kinds of tag, and each keeps the
kind the other drops:

| | This repo | iOS |
| --- | --- | --- |
| Five fixed categories (`wins`, `collaboration`, …) | The spine: every chart, percentage, tag report and the whole tagging eval | Absent. §3.13: "`ai_categories` … until one names a consumer"; §7.2 T1: "Drop the categories from the wire" |
| Open-vocabulary keywords | `auto_tags` — and `REPORT_PIPELINE.md` says these are **dropped** from the product | `tags text[]` — "suggested by analysis, edited freely by the user" (§3.10) |

So this repo's `auto_tags` is iOS's `tags`, and this repo's `tags` has no iOS
column. If iOS drops the categories, the report has nothing closed to count; if
this repo drops keywords, iOS cards have no tags.

**3. The report document.** This repo writes eight sections, schema-enforced. iOS
§3.7 stores four prose fields — `headline`, `eyebrow`, `lede`, `body` — plus
`highlights` and `themes`, and §7.2 R2 recommends dropping "the backend's other
five sections". The sections with no iOS home are the ones that carry this repo's
product thinking: uncounted work, strengths, building on a strength, reflection
questions, forward frame, recognition received, tag sub-patterns and
estimate-vs-actual.

**4. Report scope.** This repo: `last7days` (rolling seven days), `project`, `tag`
(all time — no date window). iOS: `week` (Monday-anchored) / `month` / `quarter` /
`year` / `custom`, each a date range, with optional `project_id` and `tag` scopes.
The iOS shape can express all three of this repo's reports; the reverse is not true.

**5. Charts.** This repo renders PNGs from tag mix, entries per week, sub-patterns
and estimate-vs-actual. iOS wants numbers only — `momentum` (per-day counts) and
`project_breakdown` — and draws them itself. The two chart sets do not overlap.

**6. Profile.** This repo's `users` has `industry` and `years_experience`
(Saiyyam, Aug 25). iOS register A1 decided on Sep 1 to keep both, but the iOS §3.1
DDL omits them. Here this repo is ahead of the iOS schema.

---

## Field by field

### `entries` — 23 expected columns

| iOS column | Here | |
| --- | --- | :-: |
| `raw_text` | `raw_text` | ✓ |
| `acknowledged_by` | `acknowledged_by` (seed data writes `"Priya (teammate)"`; the tagger is told to return a bare name) | ✓ |
| `impact_note` | `impact_note` | ✓ |
| `id text` (client-minted) | `id INTEGER` autoincrement | ◐ |
| `user_id uuid` | `user_id INTEGER`, always 1, never filtered on | ◐ |
| `recorded_at timestamptz` | `entry_date` — a date, no time | ◐ |
| `tags text[]` (freeform) | `auto_tags`, comma-joined string | ◐ |
| `project_id text` | `project_id INTEGER` | ◐ |
| `duration_seconds`, `capture_mode`, `span_start`, `span_end` | — | ✗ ×4 |
| `corrected_text`, `word_count` | — | ✗ ×2 |
| `summary`, `takeaways`, `mood` (register E2) | — | ✗ ×3 |
| `is_milestone` | — | ✗ |
| `analysis_state`, `analysis_failure_code` | — | ✗ ×2 |
| `search_vector` | — | ✗ |
| `created_at`, `updated_at` | — | ✗ ×2 |

This repo's `tags` column (the five categories) has no counterpart; see
disagreement 2.

### The capture analysis — 9 server-written fields

What the analysis worker must write into the row it analysed (iOS §3.10, writer
**S**), against what `tagger.py` does today.

| Field | `tagger.py` | |
| --- | --- | :-: |
| `tags` | returns `auto_tags` and **saves them** — the only field it persists | ◐ |
| `project_id` + `project` | returns `project_match.project_name` + `confidence`; not saved, not resolved to an id | ◐ |
| `acknowledged_by` | returned, not saved | ◐ |
| `impact_note` | returned, not saved | ◐ |
| `raw_text` | an input here — there is no speech-to-text | ✗ |
| `word_count` | — (one line once a transcript exists) | ✗ |
| `summary` | — | ✗ |
| `takeaways` | — | ✗ |
| `mood` | — | ✗ |

The five categories it also returns are discarded, in the product path as well as
on the wire.

### The report object — 16 fields

| iOS field | Nearest thing here | |
| --- | --- | :-: |
| `type` | `--type last7days \| project \| tag` | ◐ |
| `range_start`, `range_end` | computed per report type, never returned | ◐ ×2 |
| `range_label` | `fmt_range()` → "June 15 – July 30, 2026" (iOS wants "May 2026") | ◐ |
| `lede` | `opening_snapshot` | ◐ |
| `counts` | `notches` only (`len(entries)`); no `projects`, no `milestones` | ◐ |
| `momentum` | `entries_per_week()` — weekly, project report only; iOS wants per-day | ◐ |
| `highlights` | theme groups of `{what_happened, impact, date}`; iOS wants a flat list of `{title, detail, kind, source ids}`, and no entry ids come back from the model today | ◐ |
| `source_entry_ids` | the fetched ids exist in memory, never returned | ◐ |
| `themes` | `dominant_themes` (phrases rather than single tags) | ◐ |
| `id`, `generated_at` | — no report is stored | ✗ ×2 |
| `headline` | — | ✗ |
| `eyebrow` | — (the PDF's title and subtitle are hard-coded per type) | ✗ |
| `body` | — | ✗ |
| `project_breakdown` | — | ✗ |

`highlights[].kind` is one of `milestone`, `shipped`, `collaboration`, `note` —
two of which are near-copies of this repo's categories (`wins`, `collaboration`),
which is a natural bridge for disagreement 2.

### `users` and `projects`

- **`users`** — 13 expected columns. `role` ✓; `display_name` ◐ (`name`); `id` ◐
  (integer, not the Supabase UUID). The ten settings columns — time zone, weekly
  goal, reminder on/off, hour, minute and weekdays, two notification switches, two
  timestamps — are ✗.
- **`projects`** — 5 expected. `name` ✓; `id` and `user_id` ◐ (integers). This repo's
  `start_date`/`end_date` have no iOS column (register T3: no reader) — though they
  are what this repo's project report is scoped by.

### Endpoints — 21 expected, 0 exist

There is no HTTP layer. The closest existing function for each:

| Endpoint | Closest code here |
| --- | --- |
| `POST /v1/entries` (multipart audio → `202`) | `tagger.tag_entry()`, minus transcription |
| `GET /v1/jobs/{job_id}` | — |
| `POST /v1/entries/{id}/reanalyse` | — |
| `GET /v1/entries/{id}` | `db._row_to_entry()` |
| `GET /v1/entries` (cursor, filters, `updated_since`) | `db.get_entries_between()` |
| `PATCH /v1/entries/{id}` | `db.set_auto_tags()` |
| `DELETE /v1/entries/{id}` | — |
| `POST /v1/entries/{id}/takeaways` | — |
| `GET /v1/projects` (with counts and share) | `db.list_project_names()` |
| `POST /v1/projects` | — |
| `GET /v1/search` (Postgres FTS) | `db.get_entries_by_auto_tag()` |
| `GET /v1/stats` (streak, week, 91 days) | — |
| `POST /v1/reports` · `GET /v1/reports` · `GET /v1/reports/{id}` · `DELETE /v1/reports/{id}` | `generate_report.py` for the first; nothing stores a report |
| `GET /v1/me` · `PATCH /v1/me` · `DELETE /v1/me` | `db.get_user()` for the first |
| `POST /v1/devices` · `DELETE /v1/devices/{id}` | — |

### Cross-cutting — 10 expected mechanics

| Mechanic | Here | |
| --- | --- | :-: |
| Supabase JWT verification | none | ✗ |
| Per-user scoping (RLS, composite FKs) | one user, id 1, no query filters on it | ✗ |
| Client-minted text ids, echoed on create | integer autoincrement | ✗ |
| UTC instants on the wire | ISO dates only | ◐ |
| Async jobs: `202` then poll | synchronous CLI | ✗ |
| Closed error envelope | printed messages | ✗ |
| Server-side speech-to-text | none | ✗ |
| Audio storage, seven-day purge | none | ✗ |
| Report persistence | none — generate and forget | ✗ |
| Push notifications | none | ✗ |

---

## Loose ends in the target itself

The iOS document is the target, but it is not fully consistent. A matching branch
has to pick one reading of each; §3's DDL wins where the doc says so.

- **`momentum`** is an ordered `[{date, count}]` in §3.7 and §5's prose, but
  `{granularity, start, counts}` in §5's `GET /v1/reports/{id}` example.
- **Highlight provenance** is `source_entry_ids text[]` in §3.7 and §5's prose, but
  a scalar `entry_id` in the same example.
- **`POST /v1/entries`'s response** is described as returning `entry_id` and
  `job_id`; the example returns `job_id` and `source_entry_ids`.
- **`users`** omits `industry` and years of experience that register A1 decided to
  keep.
- **§5 cites §3 as storing `catchUp` and `timestamptz` spans**; §3's DDL already
  stores `catch_up` and `date`. §5 is stale on both.
- **`backend-contract.md`** still links `saiyyamkochar-29/notch-report-design`
  (moved to `try-notch`) and says the tagging call "is not implemented yet" — true
  of `main`, false of `jeet-dev`.

---

## What closing the gap takes, in dependency order

1. **Decide the five disagreements above.** Everything below changes shape with
   the tag and report decisions.
2. **Port the schema.** §3's tables in SQLite for local runs — same columns, text
   ids, UTC instants — with Postgres, RLS and Storage as a later step rather than a
   prerequisite.
3. **Grow and persist the capture call.** Add `summary`, `takeaways` and `mood` to
   the tagger's schema (iOS E2's own proposal), save every field it returns, and
   resolve `project_match` to an id. Re-run the eval afterwards: the prompt grows.
4. **Add speech-to-text** in front of it.
5. **Rebuild report inputs around a date range**: per-day momentum, project
   breakdown, notch/project/milestone counts, and a report schema that fills
   whatever document shape disagreement 3 settles on. Store the result.
6. **Put an HTTP layer over 3–5**: upload, job poll, entry read, report create and
   read — the capture and report paths end to end.
7. **Then the rest**: auth, list and delta sync, search, stats, account, devices.

Steps 2–6 are enough for an end-to-end run: an audio file in, an entry and a
report out, each in the shape the iOS docs specify.

## Re-measuring

The numbers are counts of the rows in the tables above; each ✓/◐/✗ was assigned by
reading the named code, not the docs. To re-measure after changes, re-read the iOS
§3 DDL and §5 payloads at their current commit and re-classify each row.
