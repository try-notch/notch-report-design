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

Not built: `reanalyse`, the entry list and delta sync, `PATCH`/`DELETE` on entries and
reports, `takeaways`, search, stats, `/v1/me`, devices. Every response is validated
against a JSON Schema of its §5 object before it is sent (`notch_api/contract.py`), and
every error uses the envelope `{"error": {code, message, retryable}}`.

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

## Verification

| Check | Result |
| --- | --- |
| `pytest` (offline) | 217 passed |
| `e2e/run_e2e.py --offline` | 111/111 |
| `e2e/run_e2e.py` (live, three consecutive runs on Sep 24) | 120/120 each, ~2.5 min, ~$0.06 a run |

The live run seeds the 52 demo transcripts through Jev and DeepSeek, uploads five spoken
recordings (one a three-day catch-up) and polls each to completion, writes a week, a
month, a project-scoped and a tag-scoped report, and checks every body against the
contract — plus idempotent re-posts, the refusals (401, 404, 400 `invalid_span`,
413, 422), report arithmetic (counts, contiguous momentum, floored shares, highlight ids
drawn only from the report's notches), and that the right project, recognition and impact
come back for each recording. Everything it sent and received is kept under
`e2e/runs/<stamp>/`; one entry and one report from the last saved run are committed in
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
  with row-level security, Storage and its purge, push, search, stats, account endpoints.
