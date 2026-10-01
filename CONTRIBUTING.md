# Contributing

Two things share this repository, and they are easy to confuse.

- **`notch_api/` is production.** It is the server the iOS app talks to: the stateless `/v2` API
  at `https://api.trynotch.xyz`. [SERVER.md](SERVER.md) describes it and [DEPLOY.md](DEPLOY.md)
  is how it runs. `notch_dash/` is its dashboard.
- **The scripts at the repository's root are the report demo:** `generate_report.py`,
  `tagger.py`, `db.py`, `seed_db.py`, `llm.py`, `charts.py`, `report_builder.py`,
  `prompt_variants.py` and the `eval_*.py` scripts. [README.md](README.md) describes it. It is
  where prompts are written and measured. None of it is deployed, and the app never reads its
  `notch.db`.

## Where the product keeps things

Since 2026-09-26 **the phone holds the record and the server keeps no readable content.** The
decision is the record at the top of `docs/data-and-backend-integration.md` in notch-ios-dev, and
`docs/backend-contract.md` there is the wire. Read both before a change that stores, sends or
returns anything a person said.

| You want to | It goes in |
|---|---|
| Change how a notch is written, tagged or classified | A prompt variant in `notch_api/prompts.py`, or `classify.py` and `analysis.py`. Measure it with the eval scripts (SERVER.md › "Run it"). A variant is switched on by remote config, not by a deploy (DEPLOY.md › 7). |
| Change a measured prompt in the demo (`prompt_variants.py`, `seed_db.py`'s catalog, `llm.py`) | Both places. `notch_api/prompts.py` holds copies of that text, and `tests/test_prompts.py` fails when a copy drifts from its source. |
| Store something a person said, or an edit they made | The app. The record is SQLite on the phone (`NotchStore` in notch-ios-dev). The server has nowhere to put it: `tests/test_no_content_at_rest.py` sends a canary phrase through every `/v2` path and fails if it turns up on disk or in a log. |
| Change what the phone and the server send each other | The contract first (`docs/backend-contract.md` in notch-ios-dev), then `notch_api/wire_v2.py`, then the app. |
| Count, limit or price a call | `notch_api/meter.py` and `notch_api/remote_config.py`. Metadata only. |
| Try an idea about storage or reports quickly | The demo. Say in the pull request that it is demo-only, and where its home in the product would be. |

The demo and the product use different names for the same two things. The demo's **fixed tags**
are the contract's `categories` (`wins`, `collaboration`, `leadership`, `growth`, `challenges`).
Its **auto tags** are the contract's `tags`, which the app shows as hashtags.

## Branches and pull requests

- **`main` is the one branch.** Start from it and open the pull request against it:

  ```bash
  git fetch origin
  git switch -c <your-branch> origin/main
  ```

  `ios-contract` was the integration branch until 2026-10-01. It is kept so old links work, and
  nothing lands there. A branch cut from it, or from an older `main`, catches up with
  `git merge origin/main`.
- Branches so far are one per change (`claude/dash-usage`, `claude/eval-moods`). Each was merged
  with a merge commit whose subject starts "Merge the".
- **There is no CI here.** The pull request says which of the checks below you ran and what they
  printed. If you changed a function no test calls (most of the demo's scripts), run it once and
  say that too.
- A repository owner merges and deploys. Say in the pull request if the change needs a remote
  config push or a new environment variable.
- **This repository is public.** No key, token, password or server address goes in a commit, and
  neither does a real person's recording or transcript. `.env`, `secrets/` and `*.p8` are ignored.

## Set up

Linux, macOS, or Windows through WSL2. It needs Python 3.11 or later, and `ffmpeg` with `ffprobe`
on the `PATH` (the speech tests and the end-to-end runs call the real ones).

```bash
sudo apt-get update && sudo apt-get install -y git python3-venv ffmpeg   # Ubuntu, WSL2 included
git clone https://github.com/try-notch/notch-report-design.git
cd notch-report-design
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

On macOS the first line is `brew install ffmpeg`. On Windows, use WSL2 with Ubuntu and follow the
same lines inside it; nothing here has been run on Windows itself.

**Checked** on macOS with Python 3.11 and 3.14 (2026-10-01). The production container is Debian
with Python 3.14 (`Dockerfile`). If a step fails on Ubuntu or WSL2, fix this page in the same
pull request.

## Run the server with no key

```bash
NOTCH_FAKE_MODELS=1 NOTCH_DEV_AUTH=1 .venv/bin/python -m notch_api
```

It listens on `127.0.0.1:4131` and keeps its files in `data/`, which is ignored.
`NOTCH_FAKE_MODELS=1` swaps OpenRouter and ffmpeg for offline doubles, and `NOTCH_DEV_AUTH=1`
lets `Bearer dev` in as the one development account. Both are refused when `NOTCH_ENV=prod`.
(`api.notch.localhost` in SERVER.md is one Mac's reverse proxy in front of the same port.)

From a second terminal:

```bash
curl -s http://127.0.0.1:4131/healthz
curl -s -H 'Authorization: Bearer dev' -H 'X-Client: ios/1.0.7+7' http://127.0.0.1:4131/v2/config
curl -s -X POST http://127.0.0.1:4131/v2/analyze \
  -H 'Authorization: Bearer dev' -H 'X-Client: ios/1.0.7+7' \
  -H "Idempotency-Key: $(python3 -c 'import uuid; print(uuid.uuid4())')" \
  -H 'Content-Type: application/json' \
  -d '{"transcript": "Paired with Nina on the checkout tests and got the flaky one to pass ten runs in a row.", "project_names": ["Front-End Refactor"], "vocabulary": ["flaky-tests"]}'
```

- The first answers `{"ok":true}`.
- The second answers the app's config with a `usage` block, because it carried a token.
- The third answers a whole notch: `summary`, `takeaways`, `tags`, `mood`, `categories`,
  `classified_by`, `prompt_version`. On the fakes the writing is the transcript handed back, which
  is enough to walk every route and no way to judge the writing.
- Leave `X-Client` off and the answer is 400 in the error envelope every `/v2` error uses:
  `{"error": {"code": "invalid_request", "message": "…", "retryable": false}}`.

The server's log is one JSON line per request on stderr, with nothing a person said in it. The
fakes take only `fakes.fake_recording()` audio, so a real recording sent to `/v2/transcribe`
comes back `audio_unreadable`. DEPLOY.md › 3b runs the same server as the container that ships.

## Check a change

| Command | Needs | Takes | What it proves |
|---|---|---|---|
| `.venv/bin/python -m pytest -q` | ffmpeg | under a minute | The offline suite: 730 passed on 2026-10-01. |
| `.venv/bin/python e2e/run_e2e.py --offline` | ffmpeg | seconds | The `/v1` harness end to end on fakes, 147 checks. The run is kept under `e2e/runs/<stamp>/`. |
| `.venv/bin/python e2e/dash_usage.py` | a Chrome or Chromium binary (`CHROME=/path/to/it`) | ~20 s | `/v2`, the meter and the usage page on fakes, 49 checks. It writes `e2e/runs/dash-usage-<stamp>/report.html`. The suite runs the same pass without the browser. |
| `.venv/bin/python e2e/check_route.py` | `OPENROUTER_API_KEY` | ~30 s, ~$0.01 | The writing check through `/v2` on real models. |
| `.venv/bin/python e2e/run_e2e.py` | `OPENROUTER_API_KEY` | ~2.5 min, ~$0.06 | The `/v1` harness on real models. |

Run the first two before every pull request. The last two cost money, so run them when the change
touches a prompt, a model call or audio.

## With a key

Put keys in `.env` at the repository's root: `cp .env.example .env` starts one with both names
in it. It is ignored, and the server and the demo both load it.

- `OPENROUTER_API_KEY` is for the server, `e2e/`, `notch_api.seed`, `notch_api.eval_categories`,
  `eval_writing.py` and `eval_moods.py`.
- `ANTHROPIC_API_KEY` is for the demo: `generate_report.py`, `tagger.py` and `eval_tags.py`.

Use a key of your own with a credit limit, never the production server's. DEPLOY.md › 1 has the
OpenRouter privacy settings the product relies on; turn them on for your key too if you send it
anything but the fixtures.

## Seeing it on a phone

A TestFlight build of the app always calls the deployed server, so a branch cannot be checked
from a phone until it is deployed. Until then the checks above are the evidence. A teammate with
a Mac can also run the app's simulator against your branch. `docs/onboarding-no-mac.md` in
notch-ios-dev covers the app's side: what a contributor without a Mac can check, and how.

## For coding agents

[CLAUDE.md](CLAUDE.md) repeats the rules above that are easiest to break. Keep the two in step.
