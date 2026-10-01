# notch-report-design

Read [CONTRIBUTING.md](CONTRIBUTING.md) before changing anything. It says which half of this
repository is production (`notch_api/`, [SERVER.md](SERVER.md)) and which is the report demo (the
scripts at the root, [README.md](README.md)), which branch to start from, and what to run before
a pull request.

The rules that are easiest to break:

- **Start from `main`, and open pull requests against it.** `ios-contract` is an older branch
  kept so links work; nothing lands there.
- **The phone holds the record; the server keeps no readable content.** Do not add a table,
  column, file or log line to `notch_api/` that holds what a person said or an edit they made.
  `tests/test_no_content_at_rest.py` is the check.
- **`notch.db`, `db.py` and `seed_db.py` are the demo's.** A storage change there changes nothing
  the app does. If the task is about the product's record, it belongs in `NotchStore` in
  notch-ios-dev; say so instead of building it here.
- **Prompts live twice.** `notch_api/prompts.py` copies the text measured in `prompt_variants.py`,
  `seed_db.py` and `llm.py`, and `tests/test_prompts.py` fails when a copy drifts.
- **The wire is `docs/backend-contract.md` in notch-ios-dev.** Change it there first.
- **This repository is public.** No key, token, password or server address in a commit, and never
  read `.env` or `secrets/`.
- **Before a pull request:** `.venv/bin/python -m pytest -q` and
  `.venv/bin/python e2e/run_e2e.py --offline`. The suite does not call most of the demo's scripts,
  so run any function you changed there once and report what it printed.
