# notch_api — the server the iOS app talks to

Two APIs in one process:

- **`/v2`, the production API** (`docs/backend-contract.md` in notch-ios-dev, accepted
  2026-09-26). Stateless: every call is synchronous, its response carries the whole result,
  and the server keeps no readable content. The device is the system of record. It runs on
  the VPS (OVHcloud, US East) at `https://api.trynotch.xyz`, sized for 2 vCPU and 4 GB;
  [DEPLOY.md](DEPLOY.md) has every step.
- **`/v1`, the development harness** (§3/§5 of `docs/data-and-backend-integration.md`): the
  server of record the Debug build still talks to until cutover. It stores transcripts and
  audio, so it is never mounted when `NOTCH_ENV=prod`.

Both reuse the report demo's measured prompts, now copied into `notch_api/prompts.py` (held
equal to their demo sources by `tests/test_prompts.py`); the demo CLI (`generate_report.py`,
`tagger.py`, …) is unchanged. [IOS_COMPATIBILITY.md](IOS_COMPATIBILITY.md) measures `/v1`
against the old §5 contract.

---

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
echo 'OPENROUTER_API_KEY=<your key>' > .env       # one key for every model call
NOTCH_DEV_AUTH=1 .venv/bin/python -m notch_api    # http://api.notch.localhost (127.0.0.1:4131)
```

`requirements-server.txt` is what the server needs (and all the VPS installs);
`requirements.txt` adds the demo CLI, the `/v1` harness's multipart parser and pytest.

- **`/v1`** needs `Authorization: Bearer dev`, the one development user.
- **`/v2`** verifies Supabase access tokens when `SUPABASE_URL` is set, and accepts
  `Bearer dev` too when `NOTCH_DEV_AUTH=1` (refused in prod). Every `/v2` call needs an
  `X-Client` header (`ios/1.0.0+1`).
- **Environment:** `NOTCH_DB` and `NOTCH_AUDIO_DIR` (`/v1`'s files), `NOTCH_METER_DB` (`/v2`'s
  meter, default `meter.db` beside `NOTCH_DB`), `NOTCH_TMP` (where ffmpeg's per-request
  directories go), `NOTCH_PORT`, `NOTCH_HOST` (default `127.0.0.1`; `0.0.0.0` lets a phone on
  the same Wi-Fi in, and anyone else on that network), `NOTCH_FAKE_MODELS=1` (deterministic
  fakes, no key, no network), and the production settings in `notch_api/services.py`.
- **Logs** are scrubbed JSON lines on stderr in every mode (`privacy.py`).

**Recording from the iOS app:** a Debug build of `notch-ios-dev` pointed at this server
records real notches through `/v1`; `docs/local-backend.md` there has the steps (register L1).

**A brand-new user** is a fresh database with no seed; the app then shows only what you
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
.venv/bin/python -m notch_api.admin config show  # /v2's remote config (NOTCH_METER_DB)
.venv/bin/python -m pytest -q                    # offline, ~25 s
.venv/bin/python e2e/run_e2e.py --offline        # the /v1 flow on fakes, ~2 s
.venv/bin/python e2e/run_e2e.py                  # the /v1 flow on real models, ~2.5 min, ~$0.06
```

## /v2: what it serves

| Route | Behaviour |
| --- | --- |
| `GET /healthz` | `{ok: true}`. No headers needed (monitors call it). |
| `GET /v2/config` | Remote config for the app: `config_version`, `min_app_version`, `features`, `limits`. Without a token, just that; with one, also `usage` (what the backstops counted today, UTC) and an `active_days` row for the account. |
| `POST /v2/transcribe` | The raw audio as the body (`audio/mp4`, `mpeg`, `ogg`, `webm`, `wav`), `X-Notch-Mode`, `X-Notch-Duration` (a claim). Decoded by ffmpeg to measure it; past 480 s cut near pauses into ~300 s pieces transcribed 3 at a time. `{transcript, word_count, audio_seconds, chunks, config_version}`; `audio_seconds` is what the provider billed. |
| `POST /v2/analyze` | `{transcript, project_names, vocabulary}` → summary, takeaways, tags, mood, impact note, recognition, `project_name` (one the device sent, or null), categories, `category_scores`, `classified_by`, versions. |
| `POST /v2/takeaways` | The same input → `{takeaways, tags}` written again, versions. |
| `POST /v2/reports` | The report's entries, author and scope, sent by the device → `facts` counted by the server (`momentum`, `project_breakdown`), prose, themes, highlights (ids not sent are dropped), versions. |
| `DELETE /v2/account` | Apple revoked when `apple_authorization_code` is sent (required for an Apple-linked account), the Supabase user deleted, every metering row and Notch Cloud record of the account deleted, the id kept as deleted. `204`, and `204` again on a retry. |
| `PUT /v2/cloud/records` · `GET /v2/cloud/changes` | Notch Cloud: opaque ciphertext under a per-account sequence; the newest write of an id wins; pages by `since`/`limit` and a byte budget. |
| `GET`/`PUT /v2/cloud/keycheck` · `DELETE /v2/cloud` | The recovery-key verifier (404 until set up), and turning Notch Cloud off (records and keycheck wiped, the sequence kept). |

**Every processing call** goes: headers (400) → token (401) → account (403 `account_gone` /
`account_blocked`) → app version (426) → switches (503 `processing_paused` /
`feature_disabled`) → the account's calls in this process (429 `rate_limited`) → the body,
read with a cap (413, 415, 400) → for audio, one of `transcribe_concurrency` (2) slots in the
process (503 `unavailable` after waiting up to 10 s), then the decode (422, 413 `audio_too_long`) →
check-and-start (409, 429, 503) → the models, under the deadline (502, 422, 504) → settle →
the response, validated against the enums of the client's contract version (from `X-Client`).
The error envelope is `{"error": {code, message, retryable}}` (+ `resets_at` on
`quota_exceeded`), `Retry-After` on every 429 and 503 and on 409 `request_in_flight`, and a
message that is fixed per code and never echoes input.

**Metering** (`meter.py`, one SQLite file, no content columns but Notch Cloud ciphertext):
check-and-start is one `BEGIN IMMEDIATE` transaction that refuses (in order) a deleted or
blocked account, the $20/day global breaker, a key reused with another body (HMAC under
`NOTCH_BODY_HMAC_KEY`), a key still in flight, 5 model-reaching attempts on a key in the last
hour, 3 calls in flight, the day's backstop of distinct keys (twice the phone's limits: 20
notches, 10 reports, 40 rewrites), and $1/day per account; otherwise it inserts the
`in_flight` row. Settle records the outcome, tokens, cost, models and providers, and the
user-less `daily_spend`, before the response is written. Quotas count distinct keys among
`ok` and live `in_flight` rows; cost counts every attempt. An `in_flight` row past its
deadline has ended.

**Remote config** (`remote_config.py`): append-only versioned rows in the meter; the newest
valid row, deep-merged over the baked defaults, wins, and an invalid one is logged and
skipped. `python -m notch_api.admin config push <file>` (validated first), `config show`,
`account block|unblock <uuid>`.

**Zero retention** (`zdr.py`): chat calls send `provider: {zdr: true, data_collection: "deny",
require_parameters: true}`. After every transcription and Jev call the server looks up the
generation's provider and checks it against OpenRouter's ZDR endpoint list (cached an hour);
on a miss it pushes a config version turning that path off (`features.capture`, or
`classifier` back to `chat`) and logs an alert. The result was still returned: the content
had already gone.

**No content at rest** (`privacy.py`): one allowlisted JSON line per request; every other
record reduced to its message template; exceptions as type and frames only; the uvicorn
access log off; an outermost ASGI layer that answers any crash with a bare 500 so uvicorn
never prints one; ffmpeg's files only in a `TemporaryDirectory` under `NOTCH_TMP`, removed
before the response. `tests/test_no_content_at_rest.py` proves it with a canary.

**Modules:** `v2.py` (the routes and the report request), `cloud.py`, `auth.py` (JWKS through
PyJWT's `PyJWKClient` over httpx, cached ≤ 10 min, refetched on an unknown kid),
`meter.py`, `remote_config.py`, `speech.py` (ffmpeg), `zdr.py`, `identity.py` (Apple,
Supabase admin), `privacy.py`, `wire_v2.py` (headers, errors, schemas, enums per contract
version), `services.py` (building it all from the environment), `prompts.py`, `admin.py`.

### /v2 decisions where the contract left room

- `X-Client` is required on every `/v2` call but not on `/healthz`, which monitors call bare.
- "Later" in the contract's retryable column is `retryable: true` with a `Retry-After`.
- `resets_at` rides inside the error object: `{"error": {..., "resets_at": "…Z"}}`.
- A token-less `GET /v2/config` is fine; a bad token there is still 401, never silently
  anonymous. A blocked account still gets config.
- `analyze` shares the notches backstop (20 distinct keys per UTC day), counted on its own.
- Idempotency keys are compared lowercase (iOS sends `UUID().uuidString`, upper case), and a
  key is bound to its body per kind for all time (transcribe and analyze share the notch id).
- Duration is measured by decoding a file in the tmpfs directory, not `ffmpeg -i -`: an MP4's
  index is often at its end, and a pipe cannot seek. ffmpeg may read only through the
  demuxer the `Content-Type` names, and only local files.
- A recording the client itself says is over `max_recording_seconds` is refused before
  decoding.
- `X-Notch-Locale`'s two-letter language picks the speech-to-text language; anything else
  falls back to config's `stt.language`.
- With `classifier: "chat"` (the default) analyze makes two chat calls side by side, the
  writing prompt and the measured v4 classification prompt, rather than merging them into one
  unmeasured prompt.
- A provider 401, 402 or 404 (OpenRouter's "no endpoint for your data policy") is
  `model_unavailable`, not a refusal; a speech-to-text refusal is `audio_unreadable`.
- Reports: entries sort by date then id; entries dated outside the range, or repeated, are
  400; transcripts past `report_transcripts_up_to` are left out of the prompt, not refused; a
  notch kept as a transcript only (no summary) may be in a report. More than
  `report_max_entries` (400) entries, or a range over `report_max_days` (400), is
  `range_too_large`.
- Notch Cloud: a deleted record may still carry ciphertext (its deletion time lives inside
  it); a batch naming an id twice keeps the last write; a page also stops at 4 MB of
  ciphertext; `GET /v2/cloud/keycheck` before setup is 404 `not_found`. Blocked accounts and
  `features.notch_cloud: false` refuse writes only.
- Account deletion: a code Apple refuses is 400 (nothing deleted) unless the account is
  already deleted; an Apple-linked token (from its `app_metadata`) without a code is 400.
- A generation whose provider cannot be learned, or a ZDR list that cannot be fetched, is an
  alert (`zdr_unverified`) and switches nothing off.

## /v1, the development harness: what it serves


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
| `pytest` (offline) | 726 passed (Sep 26, with `/v2`) |
| `e2e/run_e2e.py --offline` | 147/147 (Sep 26, `/v1` unchanged with `/v2` beside it) |
| `python -m notch_api` on fakes, `/v2` over curl | config, transcribe, analyze and a refused header answered; scrubbed JSON lines only; the temp root empty (Sep 26) |
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
- `/v1`: everything the old iOS document lists and its route table does not (Postgres with
  row-level security, Storage and its purge, push, search, reanalyse, delta sync, export).
  `/v2` replaces it; `/v1` stays only as the harness until cutover.
- `/v2` has not yet run against real OpenRouter, Supabase or Apple: everything above is
  proven on fakes and mocks (DEPLOY.md lists what the owner must set up first).
- A fixed-offset zone name such as `GMT+0530` is refused as a `tz` or `time_zone`; only
  IANA names are accepted.
