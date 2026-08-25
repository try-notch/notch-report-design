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


class NoDatabaseError(Exception):
    """Raised when notch.db doesn't exist yet — the user needs to run seed_db.py."""


def _connect():
    if not os.path.exists(DB_PATH):
        raise NoDatabaseError(
            f"No database found at {DB_PATH}.\n"
            f"Run  python seed_db.py  first to create it."
        )
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # so rows behave like dicts
    return conn


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


def _row_to_entry(row):
    """Turn a sqlite Row into a plain dict, with tags already split into a list."""
    return {
        "id": row["id"],
        "entry_date": row["entry_date"],
        "date_display": fmt_date(row["entry_date"]),
        "raw_text": row["raw_text"],
        "tags": _split_tags(row["tags"]),
        # Open-vocabulary keywords from tagger.py. Empty until the entry is tagged.
        "auto_tags": _split_tags(row["auto_tags"]),
        "project_id": row["project_id"],
        "acknowledged_by": row["acknowledged_by"],
        "impact_note": row["impact_note"],
    }


# ---------------------------------------------------------------------------
# PART 1 — FETCH.  Plain SQL, one function per report type.
# ---------------------------------------------------------------------------

def get_user():
    conn = _connect()
    row = conn.execute("SELECT * FROM users WHERE id = 1").fetchone()
    conn.close()
    return {
        "name": row["name"],
        "role": row["role"],
        "industry": row["industry"],
        "years_experience": row["years_experience"],
    }


def get_tags():
    """
    The tag catalog: [{"name", "explanation"}, ...] in seed order.

    This is what the tagger slips into the prompt. Adding a row to the `tags`
    table (and re-seeding) is how a new tag becomes available — the prompt has
    no hardcoded names.
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT name, explanation FROM tags ORDER BY rowid"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        conn.close()
        raise NoDatabaseError(
            "This database has no tags table.\n"
            "Run  python seed_db.py  to rebuild it."
        ) from exc
    conn.close()
    return [{"name": r["name"], "explanation": r["explanation"]} for r in rows]


def get_tag_names():
    """Just the names, in catalog order. Used for counts, charts, and the enum."""
    return [t["name"] for t in get_tags()]


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
    """{'wins': 7, 'collaboration': 4, ...} — a raw count per catalog tag."""
    names = get_tag_names()
    counts = Counter()
    for entry in entries:
        counts.update(entry["tags"])
    return {tag: counts.get(tag, 0) for tag in names}


def tag_mix_percent(entries):
    """
    Each tag as a % of entries in the set.

    Note this is a % OF ENTRIES, not a % of tags — an entry with two tags counts
    toward both, so these deliberately don't sum to 100. That's the honest way to
    read "how much of my week involved collaboration?"
    """
    names = get_tag_names()
    if not entries:
        return {tag: 0.0 for tag in names}
    counts = tag_counts(entries)
    return {tag: round(100 * counts[tag] / len(entries), 1) for tag in names}


def auto_tag_counts(entries, limit=None):
    """
    Raw counts for the open-vocabulary tags: [('flaky tests', 4), ('oncall', 3), ...].

    Counts only — deliberately no percentages, and this never reaches charts.py.
    The catalog tags are a closed set, so "this tag was 40% of the week" is a
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
