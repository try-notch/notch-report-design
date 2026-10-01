"""
db.py — STEP 1 of the pipeline: fetch the relevant entries. No LLM involved.

ONE JOB: talk to SQLite, and do the plain arithmetic over what comes back.

This file has two halves, and the split between them is the whole point of the demo:

  1. FETCH — plain SQL. One query function per report type.
  2. COUNT — plain Python. Tag counts, percentages, entries-per-week.

Neither half needs an LLM. Counting how many entries were tagged "collaboration"
is arithmetic over data we already have — it's free, it's instant, and it's 100%
accurate. Asking a language model to do it would cost money and introduce a chance
of getting it wrong. So we don't.

The LLM's turn comes in llm.py, where it does the two things Python genuinely
cannot: write the narrative, and group entries by what they MEAN.
"""

import os
import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notch.db")

TAGS = ["wins", "collaboration", "leadership", "growth", "challenges"]

# Where an entry's CURRENT fixed tags came from.
#
#   'seed'  — hand-written in seed_db.py. Ground truth for eval_tags.py.
#   'model' — the capture-time tagger filled them in.
#   'user'  — a person edited them in the app.
#
# This column exists so the three can never be confused for one another. A
# re-tagging pass must not overwrite a person's correction, and an eval must not
# score the model against labels the model itself wrote.
TAG_SOURCES = ("seed", "model", "user")


class NoDatabaseError(Exception):
    """Raised when notch.db doesn't exist yet — the user needs to run seed_db.py."""


class InvalidTagError(ValueError):
    """Raised when a write asks for a tag outside the closed set of five."""


# ---------------------------------------------------------------------------
# Schema migration
#
# seed_db.py holds the canonical schema. This is the catch-up path for a
# notch.db that was created before the tag-editing columns existed: reseeding is
# cheap here, but throwing away a database to add a column is exactly the habit
# that would destroy real user corrections later, so the migration is written
# properly even in a demo.
#
# Idempotent, and runs once per process on the first connection.
# ---------------------------------------------------------------------------

_ADDED_COLUMNS = {
    "entries": [
        ("model_tags", "TEXT"),
        ("tags_source", "TEXT"),
        ("tags_edited_at", "TEXT"),
        ("tagged_variant", "TEXT"),
    ],
}

# Kept byte-identical to the copy in seed_db.py. IF NOT EXISTS makes it a no-op
# on a freshly seeded database.
_TAG_EDITS_DDL = """
CREATE TABLE IF NOT EXISTS tag_edits (
    id INTEGER PRIMARY KEY,
    entry_id INTEGER NOT NULL,
    field TEXT NOT NULL,        -- 'tags' today; project_id and auto_tags later
    old_value TEXT,             -- comma-separated, as stored on the entry
    new_value TEXT,
    model_value TEXT,           -- what the model had predicted, frozen at edit time
    variant TEXT,               -- the prompt variant that produced model_value
    edited_at TEXT NOT NULL     -- ISO timestamp
);
CREATE INDEX IF NOT EXISTS idx_tag_edits_entry ON tag_edits (entry_id);
"""

_migrated = False


def _migrate(conn):
    for table, columns in _ADDED_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.executescript(_TAG_EDITS_DDL)
    # Any row that already had tags before this column existed was hand-written
    # by seed_db.py — nothing else could have written them. Claiming them as
    # 'seed' rather than leaving them NULL keeps the provenance column total,
    # so downstream code can branch on it without a fourth "unknown" case.
    conn.execute(
        "UPDATE entries SET tags_source = 'seed' "
        "WHERE tags_source IS NULL AND tags IS NOT NULL AND tags != ''"
    )
    conn.commit()


def _connect():
    global _migrated
    if not os.path.exists(DB_PATH):
        raise NoDatabaseError(
            f"No database found at {DB_PATH}.\n"
            f"Run  python seed_db.py  first to create it."
        )
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # so rows behave like dicts
    if not _migrated:
        _migrate(conn)
        _migrated = True
    return conn


def _now():
    """Local ISO timestamp, seconds precision — same timezone story as entry_date."""
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Date formatting
#
# The report spec is strict about this: single dates look like "(Jul 29, 2026)"
# and ranges look like "June 15 – July 30, 2026". Always with the year.
# ---------------------------------------------------------------------------

def fmt_date(iso_date):
    """'2026-07-29' -> 'Jul 29, 2026'"""
    d = datetime.strptime(iso_date, "%Y-%m-%d").date()
    return f"{d.strftime('%b')} {d.day}, {d.year}"


def fmt_range(start_iso, end_iso):
    """('2026-06-15', '2026-07-30') -> 'June 15 – July 30, 2026'"""
    start = datetime.strptime(start_iso, "%Y-%m-%d").date()
    end = datetime.strptime(end_iso, "%Y-%m-%d").date()
    if start.year == end.year:
        return f"{start.strftime('%B')} {start.day} – {end.strftime('%B')} {end.day}, {end.year}"
    return (f"{start.strftime('%B')} {start.day}, {start.year} – "
            f"{end.strftime('%B')} {end.day}, {end.year}")


def _split_tags(value):
    """'wins, collaboration' -> ['wins', 'collaboration']. Handles NULL."""
    if not value:
        return []
    return [t.strip() for t in value.split(",") if t.strip()]


def normalize_tags(tags):
    """
    Validate a set of fixed tags and put it in canonical form.

    Ordered by TAGS rather than by however they arrived, and de-duplicated, so
    that two spellings of the same set compare equal as plain strings. That is
    what lets the edit log tell a real correction from a no-op reorder, and it
    keeps exact-set-match scoring from tripping over order.

    Raises InvalidTagError on anything outside the closed five. The set is
    closed on purpose — see Backend.md — so a typo from a UI is a bug to
    surface, not a sixth tag to quietly accept.
    """
    cleaned = {t.strip().lower() for t in tags if t and t.strip()}
    unknown = cleaned - set(TAGS)
    if unknown:
        raise InvalidTagError(
            f"Not fixed tags: {', '.join(sorted(unknown))}. "
            f"The closed set is: {', '.join(TAGS)}."
        )
    return [tag for tag in TAGS if tag in cleaned]


def _canonical(value):
    """
    Canonical form of a stored tags string, dropping anything unrecognised.

    Deliberately lenient where normalize_tags is strict: this reads a value
    that's already in the database, and a stray tag in an old row shouldn't be
    able to raise while someone is trying to fix that very row.
    """
    stored = {t.strip().lower() for t in _split_tags(value)}
    return ",".join(tag for tag in TAGS if tag in stored) or None


def _row_to_entry(row):
    """Turn a sqlite Row into a plain dict, with tags already split into a list."""
    return {
        "id": row["id"],
        "entry_date": row["entry_date"],
        "date_display": fmt_date(row["entry_date"]),
        "raw_text": row["raw_text"],
        # The EFFECTIVE tags — whatever should be believed right now, whoever
        # put them there. Everything downstream (charts, reports, filters) reads
        # this one and never has to care about provenance.
        "tags": _split_tags(row["tags"]),
        # Open-vocabulary keywords from tagger.py. Empty until the entry is tagged.
        "auto_tags": _split_tags(row["auto_tags"]),
        "project_id": row["project_id"],
        "acknowledged_by": row["acknowledged_by"],
        "impact_note": row["impact_note"],
        # Provenance. What the model predicted is kept even after a person
        # overrides it, because the pair (predicted, corrected) is the only
        # labelled data this product generates for free.
        "model_tags": _split_tags(row["model_tags"]),
        "tags_source": row["tags_source"],   # None on rows predating the column
        "tags_edited_at": row["tags_edited_at"],
        "tagged_variant": row["tagged_variant"],
    }


# ---------------------------------------------------------------------------
# PART 1 — FETCH.  Plain SQL, one function per report type.
# ---------------------------------------------------------------------------

def get_user():
    conn = _connect()
    row = conn.execute("SELECT * FROM users WHERE id = 1").fetchone()
    conn.close()
    return {"name": row["name"], "role": row["role"]}


def get_entries_between(start_iso, end_iso):
    """Every entry in an inclusive date window. The building block for everything else."""
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM entries WHERE entry_date >= ? AND entry_date <= ? ORDER BY entry_date",
        (start_iso, end_iso),
    ).fetchall()
    conn.close()
    return [_row_to_entry(r) for r in rows]


def get_last_7_days():
    """
    The 'Last 7 Days' report: today and the six days before it.

    Also returns the SEVEN DAYS BEFORE THAT, because the whole point of this report
    is the week-over-week comparison — you can't show a shift without the thing
    you're shifting from.
    """
    today = date.today()
    this_start, this_end = today - timedelta(days=6), today
    prev_start, prev_end = today - timedelta(days=13), today - timedelta(days=7)

    return {
        "entries": get_entries_between(this_start.isoformat(), this_end.isoformat()),
        "previous_entries": get_entries_between(prev_start.isoformat(), prev_end.isoformat()),
        "period_start": this_start.isoformat(),
        "period_end": this_end.isoformat(),
        "previous_start": prev_start.isoformat(),
        "previous_end": prev_end.isoformat(),
    }


def get_project(project_name):
    """Look up a project by name (case-insensitive). Returns None if there's no match."""
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM projects WHERE LOWER(name) = LOWER(?)", (project_name,)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "start_date": row["start_date"],
        "end_date": row["end_date"],
    }


def list_project_names():
    """Used to give a helpful error message when someone typos a project name."""
    conn = _connect()
    rows = conn.execute("SELECT name FROM projects ORDER BY name").fetchall()
    conn.close()
    return [r["name"] for r in rows]


def get_project_entries(project_id):
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM entries WHERE project_id = ? ORDER BY entry_date", (project_id,)
    ).fetchall()
    conn.close()
    return [_row_to_entry(r) for r in rows]


def get_entries_by_tag(tag):
    """
    Every entry carrying a given tag.

    `tags` is a comma-separated string, so we match with LIKE on a padded copy —
    ',wins,' LIKE '%,wins,%' — which stops "win" from matching "wins".
    """
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM entries WHERE ',' || REPLACE(tags, ' ', '') || ',' LIKE ? "
        "ORDER BY entry_date",
        (f"%,{tag},%",),
    ).fetchall()
    conn.close()
    return [_row_to_entry(r) for r in rows]


def get_all_entries():
    """Every entry in the database, oldest first. Used by tagger.py."""
    conn = _connect()
    rows = conn.execute("SELECT * FROM entries ORDER BY entry_date").fetchall()
    conn.close()
    return [_row_to_entry(r) for r in rows]


def get_entry(entry_id):
    """One entry by id, or None. The read behind an edit screen."""
    conn = _connect()
    row = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    conn.close()
    return _row_to_entry(row) if row else None


def get_entries_by_auto_tag(keyword):
    """
    Every entry carrying a given auto tag. Same padded-LIKE trick as
    get_entries_by_tag, so 'test' doesn't match 'flaky tests'.

    This is the search path for the open vocabulary — the fixed five are what
    reports are built on, but auto tags are how you find "everything I said
    about oncall".
    """
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM entries "
        "WHERE ',' || COALESCE(auto_tags, '') || ',' LIKE ? ORDER BY entry_date",
        (f"%,{keyword.strip().lower()},%",),
    ).fetchall()
    conn.close()
    return [_row_to_entry(r) for r in rows]


def set_auto_tags(entry_id, keywords):
    """Write an entry's auto tags. `keywords` is a list of strings."""
    conn = _connect()
    conn.execute(
        "UPDATE entries SET auto_tags = ? WHERE id = ?",
        (",".join(keywords), entry_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# PART 1b — EDITING FIXED TAGS.  Still no LLM.
#
# The model's guess is not the last word. A person can change it, and when they
# do, two things have to stay true:
#
#   1. The correction sticks. Nothing automated overwrites it — not a re-tagging
#      pass, not a prompt upgrade, not a backfill.
#   2. The model's original prediction survives the edit. An overwritten
#      prediction is a labelled example thrown away, and corrections on entries
#      no prompt author ever read are the closest thing to free held-out eval
#      data this product will ever have.
#
# Hence three columns and a log rather than one mutable `tags` string:
# `tags` is what to believe, `model_tags` is what was predicted, `tags_source`
# says which, and tag_edits records every change a person made.
# ---------------------------------------------------------------------------

def set_tags(entry_id, tags, source="user"):
    """
    Replace an entry's fixed tags. This is the write behind the UI's tag editor.

    `tags` is a list from the closed five, in any order; an empty list is
    allowed and meaningful — "none of these five" is a legitimate answer, and a
    user clearing a tag we guessed wrong is telling us something worth storing.

    A `source='user'` write is appended to tag_edits together with whatever the
    model had predicted at the time. Automated writes are not logged: this is a
    corrections log, and mixing machine writes into it would leave you filtering
    them back out of every analysis.

    This is the unguarded write — passing a non-user source will overwrite a
    correction. Automated callers should use set_model_tags, which won't.

    Returns True if the stored value actually changed.
    """
    if source not in TAG_SOURCES:
        raise ValueError(f"Unknown tag source {source!r}. Expected one of {TAG_SOURCES}.")

    canonical = normalize_tags(tags)
    stored = ",".join(canonical) or None

    conn = _connect()
    row = conn.execute(
        "SELECT tags, model_tags, tagged_variant FROM entries WHERE id = ?", (entry_id,)
    ).fetchone()
    if row is None:
        conn.close()
        raise KeyError(f"No entry with id {entry_id}.")

    # Compare canonically, so re-saving the same tags in a different order is
    # correctly recognised as a no-op rather than logged as a correction.
    previous = _canonical(row["tags"])
    changed = previous != stored

    if changed:
        conn.execute(
            "UPDATE entries SET tags = ?, tags_source = ?, tags_edited_at = ? WHERE id = ?",
            (stored, source, _now() if source == "user" else None, entry_id),
        )
        if source == "user":
            conn.execute(
                "INSERT INTO tag_edits "
                "(entry_id, field, old_value, new_value, model_value, variant, edited_at) "
                "VALUES (?, 'tags', ?, ?, ?, ?, ?)",
                (entry_id, previous, stored, row["model_tags"],
                 row["tagged_variant"], _now()),
            )
        conn.commit()

    conn.close()
    return changed


def set_model_tags(entry_id, tags, variant=None):
    """
    Record what the tagger predicted, and promote it to the live tags only if
    nothing better is already there.

    PROMOTION RULE: the model fills `tags` when the entry has none. It never
    overwrites a value a person put there, and never overwrites the seeded
    labels eval_tags.py scores against. That is what makes a full re-tagging
    pass safe to run over the whole database at any time — it can refresh every
    prediction without destroying either the ground truth or a user's
    corrections.

    `variant` is the prompt version that produced the prediction. Without it a
    correction is unattributable, and there's no way to tell which rows are
    stale after the prompt changes.

    Returns True if the prediction was promoted into the live tags.
    """
    predicted = normalize_tags(tags)
    stored = ",".join(predicted) or None

    conn = _connect()
    row = conn.execute(
        "SELECT tags, tags_source FROM entries WHERE id = ?", (entry_id,)
    ).fetchone()
    if row is None:
        conn.close()
        raise KeyError(f"No entry with id {entry_id}.")

    promote = not _split_tags(row["tags"]) and row["tags_source"] != "user"

    if promote:
        conn.execute(
            "UPDATE entries SET model_tags = ?, tagged_variant = ?, "
            "tags = ?, tags_source = 'model' WHERE id = ?",
            (stored, variant, stored, entry_id),
        )
    else:
        conn.execute(
            "UPDATE entries SET model_tags = ?, tagged_variant = ? WHERE id = ?",
            (stored, variant, entry_id),
        )

    conn.commit()
    conn.close()
    return promote


def get_tag_edits(entry_id=None):
    """
    The corrections log, newest first. Pass an entry_id for one entry's history.

    Each row is a labelled example: what the model said, what a person changed
    it to, and which prompt version produced the mistake. Read as a set, this is
    the eval data TAGGING_EVAL.md says the project doesn't have — entries the
    prompt author never looked at, labelled by someone who wasn't trying to
    make a number go up.
    """
    conn = _connect()
    if entry_id is None:
        rows = conn.execute("SELECT * FROM tag_edits ORDER BY id DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tag_edits WHERE entry_id = ? ORDER BY id DESC", (entry_id,)
        ).fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "entry_id": r["entry_id"],
            "field": r["field"],
            "old_tags": _split_tags(r["old_value"]),
            "new_tags": _split_tags(r["new_value"]),
            "model_tags": _split_tags(r["model_value"]),
            "variant": r["variant"],
            "edited_at": r["edited_at"],
        }
        for r in rows
    ]


def get_full_date_range():
    """The oldest and newest entry dates in the whole database."""
    conn = _connect()
    row = conn.execute("SELECT MIN(entry_date) a, MAX(entry_date) b FROM entries").fetchone()
    conn.close()
    return row["a"], row["b"]


def count_all_entries():
    conn = _connect()
    n = conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"]
    conn.close()
    return n


# ---------------------------------------------------------------------------
# PART 2 — COUNT.  Plain Python arithmetic. Still no LLM.
#
# Everything below is a number the LLM is never asked for. These feed straight
# into charts.py, which means the charts are guaranteed accurate to the data —
# there is no step where a model could round something wrong or invent a figure.
# ---------------------------------------------------------------------------

def tag_counts(entries):
    """{'wins': 7, 'collaboration': 4, ...} — a raw count per tag."""
    counts = Counter()
    for entry in entries:
        counts.update(entry["tags"])
    return {tag: counts.get(tag, 0) for tag in TAGS}


def tag_mix_percent(entries):
    """
    Each tag as a % of entries in the set.

    Note this is a % OF ENTRIES, not a % of tags — an entry with two tags counts
    toward both, so these deliberately don't sum to 100. That's the honest way to
    read "how much of my week involved collaboration?"
    """
    if not entries:
        return {tag: 0.0 for tag in TAGS}
    counts = tag_counts(entries)
    return {tag: round(100 * counts[tag] / len(entries), 1) for tag in TAGS}


def auto_tag_counts(entries, limit=None):
    """
    Raw counts for the open-vocabulary tags: [('flaky tests', 4), ('oncall', 3), ...].

    Counts only — deliberately no percentages, and this never reaches charts.py.
    The fixed five are a closed set, so "collaboration was 40% of the week" is a
    stable claim you can plot and compare across weeks. Auto tags aren't: the
    model may say 'flaky tests' one week and 'test flakiness' the next, which
    would silently split one real theme across two bars and make a
    week-over-week comparison lie. So these are for search and for surfacing
    themes, not for arithmetic anyone reads as exact.
    """
    counts = Counter()
    for entry in entries:
        counts.update(entry["auto_tags"])
    return counts.most_common(limit)


def tag_share_of_period(tag_entries, total_entries):
    """
    For the tag report: what share of ALL entries in the period carried this tag?
    Returns (this_tag_pct, everything_else_pct), which always sums to 100.
    """
    if total_entries == 0:
        return 0.0, 100.0
    share = round(100 * len(tag_entries) / total_entries, 1)
    return share, round(100 - share, 1)


def entries_per_week(entries, start_iso, end_iso):
    """
    Bucket entries into calendar weeks across a period.

    Used by the project report to show where effort actually clustered — the weeks
    with three entries were the heavy weeks, the weeks with one were not.

    Returns ([labels], [counts]) ready to hand to a bar chart.
    """
    start = datetime.strptime(start_iso, "%Y-%m-%d").date()
    end = datetime.strptime(end_iso, "%Y-%m-%d").date()

    # Snap the first bucket back to the Monday of the start week so the buckets
    # line up with how people actually think about "weeks".
    first_monday = start - timedelta(days=start.weekday())

    buckets, labels = [], []
    cursor = first_monday
    while cursor <= end:
        buckets.append(0)
        labels.append(f"{cursor.strftime('%b')} {cursor.day}")
        cursor += timedelta(days=7)

    for entry in entries:
        d = datetime.strptime(entry["entry_date"], "%Y-%m-%d").date()
        index = (d - first_monday).days // 7
        if 0 <= index < len(buckets):
            buckets[index] += 1

    return labels, buckets


def recognition_entries(entries):
    """
    Every entry with someone's name in `acknowledged_by`.

    This is section 6 of the report, and it's a pure filter — 'who recognized this,
    and when' is already recorded in the row. No judgment call, so no LLM call.
    """
    return [e for e in entries if e["acknowledged_by"]]


def count_subpatterns(entries, assignments):
    """
    Turn the LLM's per-entry sub-pattern labels into counts and percentages.

    This is the seam that matters most in this demo. The LLM read each collaboration
    entry and decided which sub-pattern it belongs to — 'unblocking a teammate' vs
    'cross-team work' vs 'catching an issue before it shipped'. That is a judgment
    about MEANING, and it's the one thing here Python genuinely cannot do.

    But once the labels exist, tallying them is arithmetic again — so Python takes
    over and does the counting. The model classifies; we count. That way the
    percentages on the chart are exact, even though the grouping was a model's call.

    `assignments` is [{"entry_id": 12, "subpattern": "..."}].
    """
    valid_ids = {e["id"] for e in entries}
    counts = Counter()
    for item in assignments:
        # Ignore any entry_id the model may have hallucinated — we only count
        # labels that map to a real row.
        if item.get("entry_id") in valid_ids and item.get("subpattern"):
            counts[item["subpattern"].strip()] += 1

    total = sum(counts.values())
    if total == 0:
        return []

    return [
        {"name": name, "count": count, "percent": round(100 * count / total, 1)}
        for name, count in counts.most_common()
    ]
