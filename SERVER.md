# notch_api — the server the iOS app talks to

A local server that speaks the backend contract `notch-ios-dev` is written against
(`docs/data-and-backend-integration.md` §3 schema, §5 API): upload a recording and get
back an analysed notch; ask for a report over a date range and get back the document the
app renders. It sits beside the report demo in this repo and reuses its prompts and
seed data; the demo CLI (`generate_report.py`, `tagger.py`, …) is unchanged.

[IOS_COMPATIBILITY.md](IOS_COMPATIBILITY.md) measures how far this is from the full
contract.

---

## Run it

```bash
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -r requirements.txt
echo 'OPENROUTER_API_KEY=sk-or-...' > .env      # one key for every model call
.venv/bin/python -m notch_api                    # http://api.notch.localhost (127.0.0.1:4131)
```

Every `/v1` request needs `Authorization: Bearer dev`. There is one development user; the
contract's Supabase JWT verification is not built. Environment knobs: `NOTCH_DB`,
`NOTCH_AUDIO_DIR`, `NOTCH_PORT`, `NOTCH_HOST` (default `127.0.0.1`; `0.0.0.0` lets a phone
on the same Wi-Fi in — and anyone else on that network), and `NOTCH_FAKE_MODELS=1` to run
on deterministic fakes with no key and no network.

**Recording from the iOS app:** a Debug build of `notch-ios-dev` pointed at this server
records real notches through it — `docs/local-backend.md` there has the steps (register L1).

**A brand-new user** is a fresh database with no seed — the app then shows only what you
record:

```bash
NOTCH_DB=data/fresh.db .venv/bin/python -m notch_api
```

`DELETE /v1/me` (the app's "Delete account") returns the dev user to that state on any
database: it removes the stored audio, every entry, job, project, report and highlight,
and resets the profile and settings to their defaults. The user row stays.

```bash
.venv/bin/python -m notch_api.seed               # the 52 demo transcripts, analysed for real
.venv/bin/python -m notch_api.eval_categories    # Jev vs the chat model on the hand labels
.venv/bin/python -m pytest -q                    # offline, ~4 s
.venv/bin/python e2e/run_e2e.py --offline        # the whole flow on fakes, ~2 s
.venv/bin/python e2e/run_e2e.py                  # the whole flow on real models, ~2.5 min, ~$0.06
```

## What it serves

| Route | Behaviour |
| --- | --- |
| `POST /v1/entries` | Multipart `audio` (a file part: it needs `filename=`) + `meta` JSON. `202 {job_id, entry_id}`. Re-posting an id returns its existing job. 25 MB cap. |
| `GET /v1/jobs/{job_id}` | `processing` → `complete` with the entry inline, or `failed` with a closed-set `code` and `retryable_until`. Report jobs poll here too. |
| `GET /v1/entries/{id}` | The entry object of §5. |
| `GET /v1/projects` · `POST /v1/projects` | Counts and shares are computed; a create whose name folds onto an existing project returns that project (`200`) instead of a second one. |
| `POST /v1/reports` | `{id, type, range_start, range_end, range_label, project_id?, tag?}` → `202`. The numbers are counted at acceptance; `422 empty_range` when nothing is in scope. |
| `GET /v1/reports` · `GET /v1/reports/{id}` | The list (no cursor paging yet) and the report document. |
| `DELETE /v1/reports/{id}` | `204`; its highlights and job go with it. `404` for a missing or someone else's report. A job still counting or writing finds it gone at its next step and stops: no model call after the discard, and an answer already in flight is dropped. |
| `GET /v1/entries?limit=&cursor=` | `{entries, next_cursor, matched, total}`: every entry in **any** analysis state, newest first by `(recorded_at, id)`. `limit` 1–100, default 100. `cursor` is opaque (base64 of `{"r": recorded_at, "i": id}`), a keyset, so a capture between two page reads shifts nothing. No filters yet, so `matched` = `total` = all the user's entries. |
| `PATCH /v1/entries/{id}` | Any of `takeaways`, `tags` (bare; normalised; a project's handle or a category name is `400`), `project_id` (`null` unassigns; unknown or someone else's is `404`), `transcript` (becomes the correction, re-derives `word_count`, never re-analyses; blank is `400`), `is_milestone`. An absent key is unchanged; an unknown key is `400`. `409 entry_processing` (retryable) while the entry is pending, transcribing or analysing. Answers the full entry. 1 MB body cap. |
| `DELETE /v1/entries/{id}` | `204`. Hard delete: the stored audio files first, then the entry, its capture job and audio rows. Reports keep their frozen ids. A capture job still running for it stops at its next step: no further model call, and no failure logged. |
| `POST /v1/entries/{id}/takeaways` | `{transcript}` → `{takeaways, tags}`. **Writes nothing.** The capture's own writing call (`label_entry` with `analysis.SYSTEM_PROMPT`, the user's projects and tag vocabulary), cleaned the same way; an answer with no takeaway is refused. `503 model_unavailable` (retryable) or `502 model_refused`. |
| `GET /v1/stats?tz=` | `{streak, total, record_total, branches, this_week, goal, days}` over **complete** notches, on days in `tz`, else `users.time_zone`. `streak`: consecutive days ending today or yesterday, else 0. `this_week`: since Monday 00:00. `total`/`branches`: notches/milestones this calendar year (the tree window, register S3). `record_total`: all time. `days`: 91 booleans, the last today. A `tz` that is not an IANA name is `400`. |
| `GET /v1/me` · `PATCH /v1/me` | `{id, display_name, email: null, role, industry, years_experience, settings: {weekly_goal, reminder: {enabled, hour, minute, weekdays}, notify_week_recap, notify_report_finished, time_zone}}`. PATCH takes any subset, `settings` and `reminder` partial too; validates (goal 0 or 2–7, hour 0–23, minute 0–59, weekdays 0–6 Sunday-first, an IANA zone); a blank text field clears it; `id`/`email` are read-only; answers the whole object. |
| `DELETE /v1/me` | `{deleted: true}`: the development reset above, under one write lock from the audio sweep to the row delete, so a capture landing meanwhile is either swept with the rest or kept whole (never a file with no row). |

Not built: `reanalyse`, filters and delta sync on the entry list (`key`, `sort`, `from`/`to`,
`updated_since`, the `deletions` table), `If-Unmodified-Since`, search, devices. Every
response is validated against a JSON Schema of its §5 object before it is sent
(`notch_api/contract.py`), and every error uses the envelope
`{"error": {code, message, retryable}}`.

**Days are the user's.** Stats and report ranges (their scope and momentum buckets) count a
notch on its `recorded_at` read in the user's IANA zone: `users.time_zone`, which the app
keeps current with `PATCH /v1/me`, unless a stats request passes `tz`. A new user is `UTC`.
The report writer's prompt dates each notch by that same local day, so an evening notch in
Los Angeles is not written about as the next day's.

## Which model does what

All calls go through OpenRouter.

| Work | Model | Why |
| --- | --- | --- |
| Speech → text | `openai/whisper-large-v3` | Sent as 16 kHz mono AAC: the WAV it replaced was ~4× the upload and pushed an 18-minute catch-up past OpenRouter's 25 MB cap. |
| The five categories, mood, project match | `typesafe/jev-1.13` (Jev) | Typed decisions with probabilities: one yes/no question per category, a choice for mood, a choice over the user's projects plus `none`. ~0.2 s and ~$0.00003 per notch. |
| Summary, takeaways, tags, impact, recognition; report prose | `deepseek/deepseek-v4-pro-0813` | Sent with reasoning **off**: with its default reasoning it ignores a forced tool call and answers in prose. |
| Test recordings only | `deepgram/aura-2`, voice `aura-2-thalia-en` | The five E2E fixtures, generated once and committed with a hash of script, model and voice. |

If Jev fails after retries, the chat model classifies instead with the measured v4
category prompt from `prompt_variants.py`; `entries.classified_by` records which path
decided. Transient OpenRouter failures (429, 5xx, in-band errors, timeouts) are retried
with backoff; an answer that parses but breaks its schema is asked for once more.

### The categories, measured

The five categories stay server-side (they feed the report's facts, never the wire).
Exact-set match against the 52 hand labels in `seed_db.py`:

| Classifier | Match |
| --- | ---: |
| Jev, one threshold (0.5) | 25/52 · 48% |
| DeepSeek V4 Pro with the measured v4 prompt | 29/52 · 55% |
| **Jev, per-category thresholds (shipped)** — leave-one-out estimate | **~30–32/52 · 58–61%** |
| Jev, per-category thresholds — scored on the data they were tuned on | 37–38/52 · 71–73% |

The thresholds (`config.CATEGORY_THRESHOLDS`) were tuned on those same 52 entries, so the
in-sample number flatters them; quote the leave-one-out one. For comparison,
`TAGGING_EVAL.md`'s 73% was Claude Haiku on the prompt it was tuned against. A real
holdout — entries nobody tuned on, labelled by a second person — is still the next step.

## How it differs from the iOS document

**Where the document contradicts itself**, the server follows §3 and §5's prose:
`POST /v1/entries` answers `{job_id, entry_id}`; report `momentum` is `[{date, count}]`
with a sibling `momentum_granularity`; report `counts` is `{notches, projects, milestones}`;
each highlight carries `source_entry_ids` (a list); `capture_mode` is `catch_up`; spans and
report ranges are dates.

**Where SQLite forces a difference:** no `search_vector` (search isn't built); the
entries → projects foreign key has no `SET NULL (project_id)` (SQLite can't null one column
of a composite key, and project delete isn't built); the tag normalisation `CHECK` calls a
Python function every connection registers; `users.reminder_weekdays` has no subset check.

**Decisions of this branch:**
- Tags are the app's hashtags: lowercase, hyphenated (`flaky-tests`), never a project name
  (§3.4 stops mirroring the project into tags) or a category name.
- `users` keeps `industry` and `years_experience` (register A1), which §3.1's DDL omits.
- Internal columns never sent: `entries.categories`, `category_scores`, `classified_by`;
  `reports.project_id` and `tag` record a report's scope.
- Entry ids are `[A-Za-z0-9_-]{1,128}`, because an id becomes part of the stored audio's path.
- Report ranges are inclusive, so `range_end >= range_start` (a one-day report has start = end).
- The seven-day audio window is recorded (`audio_objects.purge_after`) but nothing sweeps it yet.
- `users.reminder_enabled` defaults to off, as the app does until onboarding turns it on.
- An edit to a notch still being analysed is refused (`409`), because the analysis would
  overwrite it when it lands; a failed notch can be edited.
- `PATCH /v1/entries` answers an unknown project with `404` (the doc says `409`), the same
  as `POST /v1/reports` does.
- `GET /v1/stats` without `tz` uses `users.time_zone` (§3.1), not UTC (§5 contradicts it).
- The entry list returns every analysis state (§3.3); stats, projects and reports count
  complete notches only.
- A JSON body, or an entry-list cursor, is `400 invalid_request` when it nests past the
  parser's recursion limit or carries an escaped lone surrogate (`"\ud800"`), which JSON
  parsers accept but no UTF-8 text can hold, so neither SQLite nor a response could.

## Verification

| Check | Result |
| --- | --- |
| `pytest` (offline) | 287 passed |
| `e2e/run_e2e.py --offline` | 147/147 (Sep 25, with the record section) |
| `e2e/run_e2e.py` (live, three consecutive runs on Sep 24) | 120/120 each, ~2.5 min, ~$0.06 a run — before the record section; not yet re-run with it |

The live run seeds the 52 demo transcripts through Jev and DeepSeek, uploads five spoken
recordings (one a three-day catch-up) and polls each to completion, writes a week, a
month, a project-scoped and a tag-scoped report, and checks every body against the
contract — plus idempotent re-posts, the refusals (401, 404, 400 `invalid_span`,
413, 422), report arithmetic (counts, contiguous momentum, floored shares, highlight ids
drawn only from the report's notches), and that the right project, recognition and impact
come back for each recording. Its last section, "record", walks the routes behind the
app's live screens: every page of the entry list (and a cursor too deep to parse refused),
`/v1/me` edited, stats recomputed from the entries in Pacific/Auckland and in UTC, an entry
edited (a lone-surrogate transcript refused) and its takeaways rewritten (writing nothing),
a report discarded, the entry deleted with its audio, and finally `DELETE /v1/me` leaving
every list empty and the settings at their defaults. Everything it sent and received is
kept under `e2e/runs/<stamp>/`; one entry and one report from the last saved run are committed in
[`e2e/sample/`](e2e/sample/).

The build was reviewed through four lenses (contract fidelity, correctness and security,
live robustness, simplicity) with each finding checked by a separate skeptic; the
confirmed ones are in the "Harden the server after a four-lens review" commit.

## Known gaps

- Whisper mishears occasionally, and the summary inherits it: "two accounts came out a
  cent off" was transcribed as "sent-off".
- Headline casing varies between runs ("The Last Loose Thread" / "The last loose thread").
- Recordings over ~80 minutes exceed the speech-to-text budget and fail loudly rather than
  being split.
- Custom report ranges have no length cap (§13 leaves custom ranges open).
- Everything the iOS document lists and the route table above does not: auth, Postgres
  with row-level security, Storage and its purge, push, search, reanalyse, delta sync,
  export and a real account deletion (the reset above keeps the one dev user).
- A fixed-offset zone name such as `GMT+0530` is refused as a `tz` or `time_zone`; only
  IANA names are accepted.
