"""
store.py — SQLite access, time and id helpers, the row → wire mappers, and ApiError.

Everything that turns a database row into what the iOS client reads lives here,
so there is one place where `categories` could leak onto the wire (it does not:
no mapper reads it), one place that decides `transcript = coalesce(corrected,
raw)`, and one place that knows the time format.

TIME. Every instant, in the database and on the wire, is UTC at second precision:
'YYYY-MM-DDTHH:MM:SSZ'. Strings in that one shape sort and compare correctly as
text, so SQL can range-filter them without parsing. Dates are 'YYYY-MM-DD'. A
notch's DAY is local: its instant read in the user's IANA zone (see "Local days").

IDS. Record ids (entries, projects, reports) are minted by the client and echoed.
Server handles (jobs, audio objects, highlights) are uuid4 strings from new_id().
"""

import json
import os
import re
import sqlite3
import uuid
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, available_timezones

from . import config

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


class ApiError(Exception):
    """A request the API refuses: `code` for the error envelope, `status` for the HTTP answer."""

    def __init__(self, code, status, message, retryable=False):
        super().__init__(message)
        self.code, self.status, self.message, self.retryable = code, status, message, retryable


# ---------------------------------------------------------------------------
# Time and ids
# ---------------------------------------------------------------------------

_INSTANT = "%Y-%m-%dT%H:%M:%SZ"
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def iso(dt):
    """Aware datetime -> '2026-05-11T08:42:00Z'. A naive datetime is taken as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(_INSTANT)


def now():
    """The current instant as a wire/DB string."""
    return iso(datetime.now(timezone.utc))


def parse_instant(s):
    """
    '2026-05-11T08:42:00Z' or '...+02:00' -> aware UTC datetime, truncated to the second.

    A string with no zone is rejected: the contract sends instants, and guessing a
    zone for one is how a notch lands on the wrong day.
    """
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError(f"instant has no zone: {s!r}")
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def parse_date(s):
    """'2026-05-11' -> date. Strict: fromisoformat alone also takes '20260511' and week dates."""
    if not isinstance(s, str) or not _DATE.fullmatch(s):
        raise ValueError(f"not a YYYY-MM-DD date: {s!r}")
    return date.fromisoformat(s)


def new_id():
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Local days. Stats and report ranges count days in the user's IANA zone: the
# request's `tz` where a route takes one, else users.time_zone (PATCH /v1/me).
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _zone_names():
    return frozenset(available_timezones())


def zone(name):
    """An IANA zone name -> ZoneInfo. Anything else (a path, an offset, a typo) is ValueError."""
    if not isinstance(name, str) or name not in _zone_names():
        raise ValueError(f"not an IANA time zone: {name!r}")
    return ZoneInfo(name)


def user_zone(conn, user_id):
    """users.time_zone as a ZoneInfo; UTC if the row is missing or holds a name this machine lacks."""
    row = conn.execute("SELECT time_zone FROM users WHERE id = ?", (user_id,)).fetchone()
    try:
        return zone(row["time_zone"] if row else "UTC")
    except ValueError:
        return ZoneInfo("UTC")


def local_date(instant, tz):
    """A stored instant -> its calendar date in `tz` (its UTC date at the far ends of the calendar)."""
    dt = parse_instant(instant)
    try:
        return dt.astimezone(tz).date()
    except OverflowError:
        return dt.date()


def local_day_bounds(start, end, tz):
    """
    Local days start..end inclusive -> [lo, hi) as instant strings, so SQL can range-filter
    the stored text. Midnight is taken as the zone reads it on each day, DST included; a
    bound past either end of the calendar is left open.
    """
    try:
        lo = iso(datetime.combine(start, time.min, tz))
    except (OverflowError, ValueError):
        lo = ""
    try:
        hi = iso(datetime.combine(end + timedelta(days=1), time.min, tz))
    except (OverflowError, ValueError):
        hi = "~"  # sorts after every instant
    return lo, hi


# ---------------------------------------------------------------------------
# Tags
#
# iOS normalize_tags folds case and strips '#'. We also hyphenate, so a tag the
# model phrases as words still renders as one hashtag: 'flaky tests' -> 'flaky-tests'.
# ---------------------------------------------------------------------------

def normalize_tag(s):
    s = s.strip().lstrip("#").lower()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"[^a-z0-9:-]", "", s)
    return re.sub(r"-{2,}", "-", s).strip("-:")


def normalize_tags(tags):
    """Normalise each, drop empties and non-strings, de-duplicate keeping first-seen order."""
    out = []
    for tag in tags:
        tag = normalize_tag(tag) if isinstance(tag, str) else ""
        if tag and tag not in out:
            out.append(tag)
    return out


def project_echo(project_names):
    """
    A test for a normalised tag or theme that repeats a project, which has its own chip (§3.4):
    the project's whole name, or a piece of the tag (split at '-') that is a word of a project's
    name, extends one ('dashboards'), or at five letters or more is a short form of one ('recon').
    Words under four letters never count. So 'recon', 'reconciliation' and 'ledger-recon' echo
    Ledger Reconciliation and 'hiring' echoes Q4 Hiring, while 'code-review' beside Checkout
    Revamp and 'data-quality' beside Database Migration do not. Only the names given are used:
    nothing about the user is kept.
    """
    whole = {normalize_tag(name) for name in project_names if isinstance(name, str)}
    words = {w for name in project_names if isinstance(name, str)
             for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) >= 4}

    def echoes(tag):
        return tag in whole or any(
            part.startswith(word) or (len(part) >= 5 and word.startswith(part))
            for part in tag.split("-") if len(part) >= 4 for word in words)
    return echoes


def _tags_normalized(text):
    """schema.sql's CHECK for tags/themes: a JSON array that normalize_tags leaves unchanged."""
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return 0
    return int(isinstance(value, list) and value == normalize_tags(value))


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

def connect(db_path):
    """
    One connection, configured the same way every time.

    foreign_keys is per-connection in SQLite and OFF by default, and the composite
    foreign keys are the cross-user guard, so it is never left to the caller.
    check_same_thread is off because FastAPI may run a request's code on a worker
    thread; each request and each job still gets a connection of its own.
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.create_function("tags_normalized", 1, _tags_normalized, deterministic=True)
    conn.execute("PRAGMA trusted_schema = ON")  # the CHECKs call tags_normalized()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(db_path):
    """Create the database file and apply schema.sql. Safe to run on every start."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    with open(SCHEMA_PATH) as f:
        schema = f.read()
    conn = connect(db_path)
    try:
        conn.executescript(schema)
    finally:
        conn.close()


_PROFILE_COLUMNS = {"display_name", "role", "industry", "years_experience", "time_zone", "weekly_goal"}


def ensure_dev_user(conn, **profile):
    """Make sure the stub-auth user exists, and set any profile columns given."""
    unknown = set(profile) - _PROFILE_COLUMNS
    if unknown:
        raise ValueError(f"not a profile column: {', '.join(sorted(unknown))}")
    with conn:
        conn.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (config.DEV_USER_ID,))
        if profile:
            sets = ", ".join(f"{column} = ?" for column in profile)
            conn.execute(f"UPDATE users SET {sets}, updated_at = ? WHERE id = ?",
                         (*profile.values(), now(), config.DEV_USER_ID))


# ---------------------------------------------------------------------------
# JSON array columns (text[] and jsonb in Postgres)
# ---------------------------------------------------------------------------

def json_dump(value):
    return json.dumps(value, ensure_ascii=False)


def json_list(text):
    """A JSON array column -> list. NULL (e.g. a report's unset project_breakdown) -> []."""
    return json.loads(text) if text else []


def loose_json(value):
    """A model's field, with a nested array or object it sent as JSON text read back."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


# ---------------------------------------------------------------------------
# Row -> wire. Each output is exactly one contract.py kind; categories never appear.
# ---------------------------------------------------------------------------

def entry_to_wire(row, project_name, retryable_until):
    span = {"start": row["span_start"], "end": row["span_end"]} if row["span_start"] else None
    transcript = row["corrected_text"] if row["corrected_text"] is not None else row["raw_text"]
    return {
        "id": row["id"],
        "recorded_at": row["recorded_at"],
        "duration_seconds": row["duration_seconds"],
        "word_count": row["word_count"],
        "summary": row["summary"],
        "transcript": transcript,
        "takeaways": json_list(row["takeaways"]),
        "tags": json_list(row["tags"]),
        "project_id": row["project_id"],
        "project": project_name,
        "mood": row["mood"],
        "is_milestone": bool(row["is_milestone"]),
        "acknowledged_by": row["acknowledged_by"],
        "impact_note": row["impact_note"],
        "mode": row["capture_mode"],
        "catch_up_span": span,
        "analysis_state": row["analysis_state"],
        "analysis_failure_code": row["analysis_failure_code"],
        "retryable_until": retryable_until,
        "updated_at": row["updated_at"],
    }


def report_to_wire(row, highlights):
    return {
        "id": row["id"],
        "type": row["type"],
        "range_start": row["range_start"],
        "range_end": row["range_end"],
        "range_label": row["range_label"],
        "headline": row["headline"],
        "eyebrow": row["eyebrow"],
        "lede": row["lede"],
        "body": row["body"],
        "generated_at": row["generated_at"],
        "counts": {
            "notches": row["notch_count"],
            "projects": row["project_count"],
            "milestones": row["milestone_count"],
        },
        "momentum": json_list(row["momentum"]),
        "momentum_granularity": row["momentum_granularity"],
        "project_breakdown": json_list(row["project_breakdown"]),
        "highlights": [
            {
                "ordinal": h["ordinal"],
                "title": h["title"],
                "detail": h["detail"],
                "kind": h["kind"],
                "source_entry_ids": json_list(h["source_entry_ids"]),
            }
            for h in sorted(highlights, key=lambda h: h["ordinal"])
        ],
        "source_entry_ids": json_list(row["source_entry_ids"]),
        "themes": json_list(row["themes"]),
    }


def project_to_wire(row, total):
    """`row` has id, name, notch_count. share = floor(100 * n / total), the Models.swift doc comment."""
    n = row["notch_count"]
    return {"id": row["id"], "name": row["name"], "notch_count": n,
            "share": 100 * n // total if total else 0}


# ---------------------------------------------------------------------------
# Reads every route needs, scoped to the caller. None means "not yours or not there" —
# the API answers both with the same 404.
# ---------------------------------------------------------------------------

def find_project_id(conn, user_id, name):
    """Folded-name lookup (§3.2). The analysis worker matches with this; it never creates."""
    row = conn.execute("SELECT id FROM projects WHERE user_id = ? AND lower(name) = lower(?)",
                       (user_id, name.strip())).fetchone()
    return row["id"] if row else None


# One entry's wire row: the entry, its project's name and its audio's retry window.
ENTRY_SELECT = """
    SELECT e.*, p.name AS project_name,
           (SELECT min(a.purge_after) FROM audio_objects a
             WHERE a.entry_id = e.id AND a.user_id = e.user_id AND a.purged_at IS NULL)
             AS retryable_until
    FROM entries e LEFT JOIN projects p ON p.id = e.project_id AND p.user_id = e.user_id
"""


def entry_row_to_wire(row):
    """A row from ENTRY_SELECT -> the entry object."""
    return entry_to_wire(row, row["project_name"], row["retryable_until"])


def load_entry(conn, user_id, entry_id):
    row = conn.execute(ENTRY_SELECT + " WHERE e.id = ? AND e.user_id = ?", (entry_id, user_id)).fetchone()
    return entry_row_to_wire(row) if row else None


def load_report(conn, user_id, report_id):
    row = conn.execute("SELECT * FROM reports WHERE id = ? AND user_id = ?",
                       (report_id, user_id)).fetchone()
    if row is None:
        return None
    highlights = conn.execute("SELECT * FROM report_highlights WHERE report_id = ? AND user_id = ?",
                              (report_id, user_id)).fetchall()
    return report_to_wire(row, highlights)


def remove_audio(conn, audio_dir, user_id, entry_id=None):
    """
    Unlink the stored files of one entry (or all of the user's), and their now-empty
    directories. Rows are the caller's to delete AFTERWARDS: audio_objects is the only
    record of a storage_key, so deleting rows first would orphan the files (§2.4). A
    caller whose sweep a concurrent capture could add a key to (the reset) holds the
    write lock (BEGIN IMMEDIATE) from before this read until that delete commits.
    """
    rows = conn.execute("SELECT storage_key FROM audio_objects WHERE user_id = ? AND (? IS NULL OR entry_id = ?)",
                        (user_id, entry_id, entry_id)).fetchall()
    root = os.path.realpath(audio_dir)
    for row in rows:
        path = os.path.realpath(os.path.join(root, row["storage_key"]))
        if not path.startswith(root + os.sep):
            continue  # a key that names somewhere else is never followed
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        folder = os.path.dirname(path)
        while folder.startswith(root + os.sep):  # <user>/<entry>, then <user>, each only once empty
            try:
                os.rmdir(folder)
            except OSError:
                break
            folder = os.path.dirname(folder)


def list_projects(conn, user_id):
    """All-time counts over complete entries; share is of all complete entries, assigned or not."""
    rows = conn.execute(
        """
        SELECT p.id, p.name, count(e.id) AS notch_count
        FROM projects p
        LEFT JOIN entries e ON e.project_id = p.id AND e.user_id = p.user_id
                           AND e.analysis_state = 'complete'
        WHERE p.user_id = ?
        GROUP BY p.id, p.name
        ORDER BY notch_count DESC, p.name ASC
        """,
        (user_id,),
    ).fetchall()
    total = conn.execute("SELECT count(*) FROM entries WHERE user_id = ? AND analysis_state = 'complete'",
                         (user_id,)).fetchone()[0]
    return [project_to_wire(row, total) for row in rows]
