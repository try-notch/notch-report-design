"""
seed.py — give the dev user a history: the 52 demo notches, through the real analysis.

    python -m notch_api.seed [--db PATH] [--workers 4]

Reports need texture, so a fresh server is seeded with seed_db.ENTRIES, the demo's
90 days of spoken notches, set relative to today (17:30 UTC on each day). They go
in the way a capture's text does after transcription: pending with raw_text set,
then analysis.analyze_text + apply_analysis write the tags, summary, takeaways,
mood and categories. So the seeded notches are what the capture path would have
made of those words, not hand-written rows that could drift from it.

TWO THINGS ARE KEPT FROM THE SEED, NOT THE MODEL:
  - The project. seed_db says which notches belong to the Front-End Refactor; that
    is ground truth, so it is assigned even when the model's match disagrees.
  - The hand-labelled categories, returned beside the model's so the E2E can report
    exact-set agreement. Reported, never a gate.

The vocabulary grows chunk by chunk, as in tagger.backfill: every call in a chunk
sees the tags the earlier chunks produced, which is how a real user's history
builds up. Re-running replaces the seeded notches (fresh dates, fresh analysis).
"""

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone

import seed_db

from . import analysis, config, store
from .fakes import FakeClient
from .openrouter import ModelError, OpenRouterClient

DEV = config.DEV_USER_ID
PROFILE = {"display_name": "Jordan Kim", "role": "Software Engineer", "industry": "Technology",
           "years_experience": "5", "time_zone": "UTC"}
# Fixed ids, so the E2E and a person with curl can name them. Billing Migration has no
# seeded notches; the E2E's spoken fixtures give the matcher a second project to choose.
PROJECTS = {"project-front-end-refactor": seed_db.PROJECT_NAME,
            "project-billing-migration": "Billing Migration"}
WORDS_PER_SECOND = 2.5


def entry_id(n):
    """The id of seed_db.ENTRIES[n]."""
    return f"seed-{n:02d}"


def seed(db_path, client, *, workers=4, today=None):
    """
    Seed db_path and analyse every seeded notch with `client`.

    Returns [(entry_id, expected_categories, predicted_categories)], one per
    seed_db.ENTRIES item in the same order. A ModelError is retried once per notch;
    a second one propagates and the seed stops there.
    """
    today = today or datetime.now(timezone.utc).date()
    store.init_db(db_path)
    conn = store.connect(db_path)
    try:
        store.ensure_dev_user(conn, **PROFILE)
        with conn:
            # OR IGNORE: a project created through the API under the same folded name wins.
            conn.executemany("INSERT OR IGNORE INTO projects (id, user_id, name) VALUES (?, ?, ?)",
                             [(pid, DEV, name) for pid, name in PROJECTS.items()])
            conn.executemany("DELETE FROM entries WHERE id = ? AND user_id = ?",
                             [(entry_id(n), DEV) for n in range(len(seed_db.ENTRIES))])
            conn.executemany(
                "INSERT INTO entries (id, user_id, recorded_at, duration_seconds, raw_text) VALUES (?, ?, ?, ?, ?)",
                [(entry_id(n), DEV,
                  store.iso(datetime.combine(today - timedelta(days=days_ago), time(17, 30), timezone.utc)),
                  round(len(text.split()) / WORDS_PER_SECOND, 1), text)
                 for n, (days_ago, text, *_) in enumerate(seed_db.ENTRIES)])

        # Oldest first, so the vocabulary grows in the order the days happened.
        order = sorted(range(len(seed_db.ENTRIES)), key=lambda n: -seed_db.ENTRIES[n][0])
        predicted = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for start in range(0, len(order), workers):
                chunk = order[start:start + workers]
                projects, vocabulary = analysis.user_context(conn, DEV)
                results = pool.map(lambda n: _analyse(client, seed_db.ENTRIES[n][1], projects, vocabulary), chunk)
                for n, result in zip(chunk, results):
                    _, text, _, is_project, *_ = seed_db.ENTRIES[n]
                    result["project_name"] = seed_db.PROJECT_NAME if is_project else None
                    with conn:
                        analysis.apply_analysis(conn, DEV, entry_id(n), text, result)
                    predicted[n] = result["categories"]
    finally:
        conn.close()
    return [(entry_id(n), [c for c in analysis.CATEGORIES if c in labels.split(",")], predicted[n])
            for n, (_, _, labels, *_) in enumerate(seed_db.ENTRIES)]


def _analyse(client, text, projects, vocabulary):
    """analyze_text, retried once: a refusal (no tool call, a blank summary) is often a one-off."""
    try:
        return analysis.analyze_text(client, text, project_names=projects, vocabulary=vocabulary)
    except ModelError:
        return analysis.analyze_text(client, text, project_names=projects, vocabulary=vocabulary)


def agreement(results):
    """(notches whose predicted category set equals the hand label, notches)."""
    return sum(set(expected) == set(got) for _, expected, got in results), len(results)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Seed the dev user with the 52 demo notches, analysed.")
    parser.add_argument("--db", default=config.DB_PATH, help=f"database file (default {config.DB_PATH})")
    parser.add_argument("--workers", type=int, default=4, help="parallel analysis calls (default 4)")
    args = parser.parse_args(argv)
    try:
        # Same switch as the server, so the seed can run offline too.
        client = FakeClient() if os.environ.get("NOTCH_FAKE_MODELS") == "1" else OpenRouterClient.from_env()
        results = seed(args.db, client, workers=args.workers)
    except (RuntimeError, ModelError) as exc:
        print(f"seed failed: {exc}", file=sys.stderr)
        return 1
    agree, total = agreement(results)
    print(f"Seeded {total} notches into {args.db}. "
          f"Categories match the hand labels exactly on {agree}/{total} ({100 * agree // total}%).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
