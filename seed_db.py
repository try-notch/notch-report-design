"""
seed_db.py — creates the fake SQLite database this demo reads from.

ONE JOB: build a realistic 90-day set of voice-journal entries for one fake user,
so every report type below has real texture to work with.

Why the data is shaped the way it is (this matters — the reports depend on it):
  * The last two weeks have deliberately DIFFERENT tag mixes (a collaboration-heavy
    week following a solo-build week) so the week-over-week chart shows a real,
    visible shift rather than noise.
  * One clearly-scoped project ("Front-End Refactor") spans ~6 weeks in the middle
    of the range, with entries covering every tag, including two entries that state
    an estimate-vs-actual ("thought it'd take a day, took three").
  * ~21 entries are tagged "collaboration" and fall into three distinguishable
    sub-patterns (unblocking a teammate / cross-team work / catching an issue in
    someone else's work). The tag report asks the LLM to find those clusters — so
    they have to actually exist in the data.
  * Roughly a third of entries have an `acknowledged_by`, because "Recognition
    Received" is its own report section.

Run it with:  python seed_db.py
"""

import argparse
import csv
import os
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notch.db")

# The five tags the product supports. Everything downstream keys off this list.
TAGS = ["wins", "collaboration", "leadership", "growth", "challenges"]

PROJECT_NAME = "Front-End Refactor"

# The project's entries run from ~56 to ~17 days ago — about 5.5 weeks, sitting
# in the middle of the 90-day range so it's clearly bounded on both sides. Its
# start and end dates are DERIVED from those entries rather than declared here,
# which is the same rule a CSV import follows: a project spans the first and
# last entry attached to it.

SCHEMA = """
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS projects;
DROP TABLE IF EXISTS entries;
DROP TABLE IF EXISTS tag_edits;

CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    name TEXT,
    role TEXT
);

CREATE TABLE projects (
    id INTEGER PRIMARY KEY,
    user_id INTEGER,
    name TEXT,
    start_date TEXT,   -- ISO date
    end_date TEXT      -- ISO date, nullable if ongoing
);

CREATE TABLE entries (
    id INTEGER PRIMARY KEY,
    user_id INTEGER,
    entry_date TEXT,        -- ISO date
    raw_text TEXT,          -- the "voice journal" text, written as if transcribed from speech
    tags TEXT,              -- comma-separated: wins, collaboration, leadership, growth, challenges.
                            -- The EFFECTIVE tags — whoever set them. Charts and reports read this.
    auto_tags TEXT,         -- comma-separated open-vocabulary keywords, filled in by tagger.py.
                            -- NULL until tagged. Never feeds a chart — see Backend.md.
    project_id INTEGER,     -- nullable, FK to projects
    acknowledged_by TEXT,   -- nullable — who recognized this, if anyone
    impact_note TEXT,       -- nullable — a short, specific stated impact/result

    -- Tag provenance. `tags` says what to believe; these say where it came from.
    -- Kept apart so a user's correction survives every automated pass, and so
    -- the model's original guess survives the correction.
    model_tags TEXT,        -- what tagger.py predicted. NULL until it has run.
    tags_source TEXT,       -- 'seed' | 'model' | 'user' — who set `tags`
    tags_edited_at TEXT,    -- ISO timestamp of the last user edit, NULL if never
    tagged_variant TEXT     -- prompt variant that produced model_tags
);

-- Append-only log of the corrections people make in the app. Its real job is
-- eval data: entries no prompt author ever read, labelled by someone with no
-- interest in the score. See TAGGING_EVAL.md on why that's the missing piece.
--
-- model_value and variant are copied in rather than joined, so a later
-- re-tagging pass overwriting entries.model_tags can't rewrite the history of
-- what was actually corrected.
CREATE TABLE tag_edits (
    id INTEGER PRIMARY KEY,
    entry_id INTEGER NOT NULL,
    field TEXT NOT NULL,        -- 'tags' today; project_id and auto_tags later
    old_value TEXT,             -- comma-separated, as stored on the entry
    new_value TEXT,
    model_value TEXT,           -- what the model had predicted, frozen at edit time
    variant TEXT,               -- the prompt variant that produced model_value
    edited_at TEXT NOT NULL     -- ISO timestamp
);
CREATE INDEX idx_tag_edits_entry ON tag_edits (entry_id);
"""

# ---------------------------------------------------------------------------
# The entries themselves.
#
# Each row is: (days_ago, raw_text, tags, is_project_entry, acknowledged_by, impact_note)
#
# `days_ago` is counted back from whatever day you run this, so "Last 7 Days"
# always has content no matter when the demo happens. The offsets are chosen to
# land on weekdays.
#
# raw_text is written the way a person actually talks about their day — rambling,
# a little self-deprecating — NOT like a resume bullet. That's the whole premise of
# the product: the user speaks, and the system does the translation work.
# ---------------------------------------------------------------------------
ENTRIES = [
    # ===================================================================
    # THIS WEEK (days 0-6) — deliberately COLLABORATION-HEAVY.
    # 4 of 5 entries are tagged collaboration. Contrast this with last week.
    # ===================================================================
    (6, "Spent most of the morning pairing with Priya on that flaky checkout test — turned out to be a "
        "race condition in how we were seeding the test DB. She shipped the fix herself Friday which "
        "honestly felt better than if I'd just done it.",
     "collaboration,leadership", False, "Priya (teammate)",
     "Flaky checkout test went from ~15% failure rate to zero"),

    (5, "Design sync with Elena's team about the new filter component. Took like 45 minutes but we agreed "
        "on one shared spec instead of both of us building slightly different versions, which is a whole "
        "week we're not going to waste.",
     "collaboration", False, None,
     "Avoided duplicate implementations of the filter component across two teams"),

    (4, "Reviewing Marcus's PR for the notifications batch job and noticed the retry loop had no backoff — "
        "it would've hammered the mail service on any partial outage. Flagged it, he fixed it in like ten "
        "minutes. Would've been ugly in prod.",
     "collaboration,wins", False, "Marcus (teammate)",
     "Caught a missing retry backoff before it reached production"),

    (3, "Ran the onboarding walkthrough for Wes, our new hire. Didn't plan to spend two hours on it but he "
        "had good questions and I ended up writing down a bunch of stuff that was only in my head.",
     "leadership,collaboration", False, "Dana (manager)",
     "New hire ran his first local build and shipped a doc fix on day two"),

    (0, "Wrote up the postmortem doc for the search latency thing from last month. Kind of dreading writing "
        "these but going back through the timeline I actually understood the failure better than when we "
        "fixed it.",
     "growth,wins", False, None, None),

    # ===================================================================
    # LAST WEEK (days 7-13) — deliberately SOLO-BUILD heavy.
    # Zero collaboration entries. Wins + challenges + growth only.
    # This is what makes the week-over-week comparison chart interesting.
    # ===================================================================
    (13, "Head down all day on the CSV export rewrite. Streaming it now instead of buffering the whole thing "
         "in memory. Didn't talk to anyone which was honestly kind of nice.",
     "wins", False, None,
     "CSV export memory usage dropped from ~1.2GB to under 40MB on large accounts"),

    (12, "Export thing is fighting me. The streaming version is correct but it's slower than the old one for "
         "small files and I can't figure out why yet. Spent four hours and got nowhere.",
     "challenges", False, None, None),

    (11, "Found it — I was flushing per row. Batching the writes fixed the small-file regression. Feels "
         "obvious now, always does.",
     "wins,growth", False, None,
     "Small-file export back to baseline speed while keeping the memory win"),

    (10, "Finally sat down and learned how our tracing setup actually works instead of copy-pasting spans "
         "from other services. Read the whole config. Feels slow but I'm tired of not knowing.",
     "growth", False, None, None),

    (7, "Shipped the export rewrite behind a flag. Rolled it out to 10% and watched the dashboards for an "
        "hour. Nothing broke, which after last week I was not taking for granted.",
     "wins,challenges", False, "Dana (manager)",
     "Export rewrite live at 10% with no change in error rate"),

    # ===================================================================
    # WEEK OF -14 to -20 — the tail end of the Front-End Refactor project
    # ===================================================================
    (20, "Last big chunk of the refactor — swapped the old modal system over. Thought it'd be a one-day job, "
         "took three, because half our modals were reaching into internal state I didn't know about.",
     "challenges,wins", True, None,
     "Estimated 1 day, actually took 3 days; 14 modals migrated to the shared component"),

    (19, "Cleaned up the last of the dead CSS. Deleted about 2,000 lines. Nothing feels better than a big "
         "red diff.",
     "wins", True, None, "~2,000 lines of unused CSS removed"),

    (18, "Helped Ravi from the data team figure out why our events weren't showing up in their pipeline. "
         "Turned out to be a schema field we renamed months ago and never told them about. My bad, honestly.",
     "collaboration,challenges", False, "Ravi (data team)",
     "Restored event delivery to the analytics pipeline"),

    (17, "Refactor's done. Wrote up the migration notes and did a walkthrough for the team so nobody has to "
         "reverse-engineer why things moved. Weirdly emotional about it.",
     "wins,leadership", True, "Dana (manager)",
     "Front-End Refactor shipped; component bundle down 31%"),

    (14, "Back on regular work after the refactor. Knocked out three small bug tickets that had been sitting "
         "in the backlog since April. Felt good to just close things.",
     "wins", False, None, "Three long-stale backlog bugs closed"),

    # ===================================================================
    # WEEK OF -21 to -27
    # ===================================================================
    (26, "On-call. Nothing caught fire, but I noticed our alert for queue depth has been misconfigured for "
         "months — it would never have fired. Fixed the threshold.",
     "wins,challenges", False, None,
     "Queue-depth alert corrected after months of silently never firing"),

    (25, "Bundle size finally came in under target — 31% smaller than before we started. Sat and looked at "
         "the graph for longer than I'd like to admit.",
     "wins", True, None,
     "Component bundle 31% smaller than the pre-refactor baseline"),

    (24, "Ravi asked for help reading our event schema again. Instead of just answering, I wrote it up in the "
         "shared wiki so the next person doesn't have to ask.",
     "collaboration,leadership", False, "Ravi (data team)",
     "Event schema documented in the shared wiki"),

    (21, "Worked with Aisha from platform to get the new package into the build pipeline properly. Cross-team "
         "stuff always takes three times as long as you think, but she was great about it.",
     "collaboration", True, "Aisha (platform team)",
     "New component package now publishing automatically on merge"),

    # ===================================================================
    # WEEK OF -28 to -34
    # ===================================================================
    (33, "Set up a codemod so the rest of the team doesn't have to hand-migrate their imports. Took a day to "
         "write, probably saves everyone else a day each.",
     "wins,leadership", True, "Dana (manager)",
     "Codemod migrated 180+ import sites automatically"),

    (31, "Reviewed Priya's billing page PR and spotted that the currency formatting would break for locales "
         "that use commas as decimal separators. Small thing, but it would've been an embarrassing bug.",
     "collaboration,wins", False, "Priya (teammate)",
     "Locale currency-formatting bug caught in review"),

    (28, "Demoed the new component library at the team meeting. Got some pushback on the naming which stung "
         "a bit, but they were fair points and I changed two of them.",
     "collaboration,growth", True, None, None),

    # ===================================================================
    # WEEK OF -35 to -41
    # ===================================================================
    (39, "Caught a bug in Tom's dropdown migration during review — the keyboard nav was silently broken for "
         "anyone using arrow keys. Not something QA would've caught. Glad I actually tabbed through it.",
     "collaboration,wins", True, "Tom (teammate)",
     "Keyboard navigation regression caught before release"),

    (38, "Long meeting about Q3 planning. Mostly listened. Said one thing about scoping the refactor properly "
         "and it ended up in the doc, which surprised me.",
     "growth", False, None, None),

    (35, "Rough one. Merged main and everything exploded — three weeks of other people's changes against "
         "components that no longer exist. Spent the whole day just untangling conflicts.",
     "challenges", True, None, None),

    # ===================================================================
    # WEEK OF -42 to -48
    # ===================================================================
    (47, "Unblocked Marcus on a webpack config thing that had eaten his whole morning. Took me fifteen "
         "minutes because I'd hit the exact same error last year.",
     "collaboration", False, None,
     "Unblocked a teammate after half a day of lost time"),

    (46, "Sam and I went back and forth on whether the design tokens should live in the component package or "
         "a separate one. Ended up separate. He was right, I was being lazy about it.",
     "collaboration,growth", True, None,
     "Design tokens split into their own package so the mobile web app can use them too"),

    (45, "Migrated the settings page to the new primitives. Estimated half a day, took a day and a half — the "
         "form validation was tangled into the old components in ways I didn't see coming.",
     "challenges", True, None,
     "Estimated 0.5 days, actually took 1.5 days; settings page migrated"),

    (42, "Paired with Nina for most of the afternoon on the table component. She's newer to the codebase and "
         "I tried to just ask questions instead of taking the keyboard. Slower but she got it.",
     "leadership,collaboration", True, "Nina (teammate)",
     "Nina shipped the table migration on her own the following week"),

    # ===================================================================
    # WEEK OF -49 to -55
    # ===================================================================
    (54, "Prod incident — image uploads failing for about 40 minutes. Not our code, a dependency, but I was "
         "the one who noticed and paged the right people.",
     "challenges,wins", False, None,
     "Incident detected and escalated within 8 minutes"),

    (53, "Wrote the RFC for how we're going to do this — shared primitives first, then migrate feature by "
         "feature. Sent it to the team for comments instead of just starting, which past me would not "
         "have done.",
     "leadership,growth", True, "Dana (manager)",
     "Refactor RFC approved with a phased migration plan"),

    (52, "Spent the afternoon reading the React 19 migration docs. We're not doing it yet but I want to "
         "actually understand what's changing before someone asks.",
     "growth", False, None, None),

    (49, "Built the base Button and Input primitives. Straightforward day. Nice to have a day where the plan "
         "just works.",
     "wins", True, None, None),

    # ===================================================================
    # WEEK OF -56 to -62 — project kickoff lands at day -56
    # ===================================================================
    (62, "Wrapped up the search latency work. p95 down from 1.9 seconds to 640 milliseconds. Took longer than "
         "planned but the number is the number.",
     "wins", False, "Dana (manager)",
     "Search p95 latency reduced from 1.9s to 640ms"),

    (61, "Pairing with Nina on her first real feature. Mostly just sat there while she drove and answered "
         "questions. Hard to not grab the keyboard.",
     "leadership,collaboration", False, None, None),

    (59, "Sat in on the mobile team's planning to flag that our API change would break their client. Saved us "
         "both a rollback, probably.",
     "collaboration", False, "Kai (mobile team)",
     "Breaking API change caught before the mobile release"),

    (56, "Kicking off the front-end refactor today. Spent the morning just reading the component tree and "
         "writing down everything that's duplicated. It's worse than I thought — four different button "
         "implementations.",
     "challenges", True, None, None),

    # ===================================================================
    # WEEK OF -63 to -69
    # ===================================================================
    (69, "Started digging into the search latency thing. Profiled it and the answer was boring: an N+1 query. "
         "Sometimes it's not exciting.",
     "challenges", False, None, None),

    (67, "Fixed the N+1 and added a test that would've caught it. The test was the harder part.",
     "wins,growth", False, None,
     "Added a regression test covering the N+1 query path"),

    (66, "Caught a data migration in Tom's PR that would've run against prod without a dry-run step. He'd "
         "tested it locally on 200 rows. We have four million.",
     "collaboration,wins", False, None,
     "Unsafe production migration caught before merge"),

    (63, "Wrote docs for the search service. Nobody asked. But I'd just spent two weeks in there and it seemed "
         "dumb to let all that fall out of my head.",
     "leadership,growth", False, None, None),

    # ===================================================================
    # WEEK OF -70 to -76
    # ===================================================================
    (76, "Interviewed a backend candidate. Spent 30 minutes afterward writing careful feedback because I've "
         "been on the other side of a two-line rejection.",
     "leadership", False, None, None),

    (74, "Spent an hour with Priya walking through how our auth flow actually works. She was blocked on a "
         "ticket and the docs were wrong.",
     "collaboration", False, "Priya (teammate)",
     "Unblocked a teammate; auth docs corrected afterwards"),

    (73, "Shipped the rate limiter for the public API. Simple sliding window, nothing fancy. It works.",
     "wins", False, None, "Public API rate limiting live"),

    (70, "Frustrating day. Chased a bug for six hours that turned out to be a stale local cache. Learned to "
         "check the boring things first, again.",
     "challenges,growth", False, None, None),

    # ===================================================================
    # WEEK OF -77 to -83
    # ===================================================================
    (83, "Design review with Elena's team on the new dashboard. I pushed back on a layout that would've "
         "needed three new one-off components. We found a simpler version.",
     "collaboration,leadership", False, None,
     "Avoided three one-off components by simplifying the dashboard layout"),

    (81, "Wrote the integration tests for the webhook system that I keep saying I'll write. Took a day. Nobody "
         "will notice unless they break.",
     "wins,growth", False, None,
     "Webhook system covered by integration tests for the first time"),

    (80, "Marcus was stuck on a deploy that kept rolling back. Sat with him and we found it was an env var "
         "missing in staging only. Two hours of his day, ten minutes together.",
     "collaboration", False, "Marcus (teammate)", None),

    (77, "Went through the on-call runbook and updated the four steps that were out of date. Boring. Would've "
         "mattered a lot at 3am.",
     "leadership", False, None,
     "On-call runbook corrected ahead of the next rotation"),

    # ===================================================================
    # WEEK OF -84 to -90 — start of the 90-day window
    # ===================================================================
    (89, "First real week on the payments integration. A lot of reading, not much writing. Trying to be "
         "patient about it.",
     "growth", False, None, None),

    (87, "Found an edge case in the refund flow where a partial refund could exceed the original charge. "
         "Reported it, turned out nobody had hit it yet.",
     "wins,collaboration", False, "Dana (manager)",
     "Refund over-issue edge case found and fixed before any customer hit it"),

    (84, "Long day pairing with Sam on the payments state machine. Neither of us fully understood it going "
         "in, both of us did coming out.",
     "collaboration,growth", False, None, None),
]


# ---------------------------------------------------------------------------
# RECORDS — the one shape everything is built from.
#
# The built-in fixture above and an imported CSV are different sources for the
# same thing, so both are normalised into a list of these dicts before anything
# touches SQLite. That way validation, project derivation, insertion and the
# summary are written once and behave identically whichever source you used.
#
#   {"entry_date": "2026-07-14",  # ISO
#    "raw_text": "...",
#    "tags": ["wins", "growth"],  # may be empty
#    "auto_tags": [],             # normally empty; tagger.py fills it
#    "project": "Front-End Refactor" or None,
#    "acknowledged_by": "Priya" or None,
#    "impact_note": "..." or None}
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "entry_date", "raw_text", "tags", "project", "acknowledged_by",
    "impact_note", "auto_tags",
]
CSV_REQUIRED = ["entry_date", "raw_text"]


class CSVError(Exception):
    """One or more rows failed validation. Carries every problem, not just the first."""

    def __init__(self, problems):
        self.problems = problems
        super().__init__(f"{len(problems)} problem(s) in the CSV")


def _fixture_records():
    """The built-in 90-day demo data, as records.

    Dates are generated relative to today so 'Last 7 Days' always has content —
    that property is why the fixture stores days-ago rather than real dates.
    """
    today = date.today()
    records = []
    for days_ago, raw_text, tags, is_project, acknowledged_by, impact_note in ENTRIES:
        records.append({
            "entry_date": (today - timedelta(days=days_ago)).isoformat(),
            "raw_text": raw_text,
            "tags": [t.strip() for t in tags.split(",") if t.strip()],
            "auto_tags": [],
            "project": PROJECT_NAME if is_project else None,
            "acknowledged_by": acknowledged_by,
            "impact_note": impact_note,
        })
    return records


def _split(value):
    return [v.strip().lower() for v in (value or "").split(",") if v.strip()]


def read_csv(path):
    """
    Parse and validate a CSV into records. Raises CSVError listing every problem.

    Validation is all-or-nothing on purpose: the database is dropped and rebuilt,
    so a half-valid import that partially succeeded would leave you worse off
    than before. Nothing is written until every row passes.
    """
    # utf-8-sig strips the byte-order mark Excel writes, which otherwise turns
    # the first header into '﻿entry_date' and makes a valid file look like
    # it's missing its required column.
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise CSVError(["the file is empty"])

        headers = [h.strip() for h in reader.fieldnames]
        problems, warnings = [], []

        missing = [c for c in CSV_REQUIRED if c not in headers]
        if missing:
            raise CSVError([
                f"missing required column(s): {', '.join(missing)}. "
                f"Expected some of: {', '.join(CSV_COLUMNS)}"
            ])

        # An extra column is a warning, not an error — people keep their own
        # notes alongside the data, and refusing the file over a column we
        # simply don't read would be obnoxious.
        unknown = [h for h in headers if h not in CSV_COLUMNS]
        if unknown:
            warnings.append(f"ignoring unrecognised column(s): {', '.join(unknown)}")

        records = []
        for line, row in enumerate(reader, start=2):  # start=2: row 1 is the header
            row = {k.strip(): (v or "").strip() for k, v in row.items() if k}

            raw_text = row.get("raw_text", "")
            if not raw_text:
                problems.append(f"row {line}: raw_text is empty")

            entry_date = row.get("entry_date", "")
            try:
                # Strict ISO. Slash formats are rejected rather than guessed at,
                # because 03/04/2026 is March in one country and April in another
                # and silently picking one is worse than refusing.
                parsed = datetime.strptime(entry_date, "%Y-%m-%d").date()
            except ValueError:
                problems.append(
                    f"row {line}: entry_date {entry_date!r} is not YYYY-MM-DD"
                )
                parsed = None

            tags = _split(row.get("tags"))
            for tag in tags:
                if tag not in TAGS:
                    problems.append(
                        f"row {line}: unknown tag {tag!r} — must be one of {', '.join(TAGS)}"
                    )

            records.append({
                "entry_date": parsed.isoformat() if parsed else None,
                "raw_text": raw_text,
                "tags": tags,
                "auto_tags": _split(row.get("auto_tags")),
                "project": row.get("project") or None,
                "acknowledged_by": row.get("acknowledged_by") or None,
                "impact_note": row.get("impact_note") or None,
            })

    if not records:
        problems.append("no data rows — the file has a header and nothing else")
    if problems:
        raise CSVError(problems)
    return records, warnings


def shift_to_today(records):
    """
    Slide every date forward so the newest entry lands on today, preserving gaps.

    A CSV carries real dates, and a report window like 'Last 7 Days' is relative
    to when you run it — so an export from three months ago produces an empty
    weekly report and looks broken. This makes an old file demo-able without
    editing it.
    """
    dates = [datetime.strptime(r["entry_date"], "%Y-%m-%d").date() for r in records]
    offset = date.today() - max(dates)
    for record, original in zip(records, dates):
        record["entry_date"] = (original + offset).isoformat()
    return records


def build(records, user_name, user_role):
    """Drop and rebuild notch.db from records. Projects are derived from names."""
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO users (id, name, role) VALUES (?, ?, ?)",
                 (1, user_name, user_role))

    # A project's span is the first and last entry attached to it. There's no
    # separate projects CSV because the dates are already implied by the entries,
    # and a hand-maintained second file would only drift from them.
    spans = {}
    for record in records:
        if record["project"]:
            dates = spans.setdefault(record["project"], [])
            dates.append(record["entry_date"])

    project_ids = {}
    for index, (name, dates) in enumerate(sorted(spans.items()), start=1):
        project_ids[name] = index
        conn.execute(
            "INSERT INTO projects (id, user_id, name, start_date, end_date) "
            "VALUES (?, ?, ?, ?, ?)",
            (index, 1, name, min(dates), max(dates)),
        )

    for record in records:
        conn.execute(
            """INSERT INTO entries
               (user_id, entry_date, raw_text, tags, auto_tags, project_id,
                acknowledged_by, impact_note, tags_source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'seed')""",
            (
                1,
                record["entry_date"],
                record["raw_text"],
                ",".join(record["tags"]),
                ",".join(record["auto_tags"]) or None,
                project_ids.get(record["project"]),
                record["acknowledged_by"],
                record["impact_note"],
            ),
        )

    conn.commit()
    conn.close()
    return project_ids


def summarise(records, project_ids, user_name, user_role):
    """Print enough to sanity-check the data before demoing on it."""
    tag_counts = Counter()
    for record in records:
        tag_counts.update(record["tags"])

    dates = sorted(r["entry_date"] for r in records)
    acknowledged = sum(1 for r in records if r["acknowledged_by"])
    untagged = sum(1 for r in records if not r["tags"])
    tagged = sum(1 for r in records if r["auto_tags"])
    recent = sum(1 for r in records
                 if r["entry_date"] >= (date.today() - timedelta(days=6)).isoformat())

    print()
    print("  Seeded notch.db")
    print("  " + "-" * 52)
    print(f"  User            {user_name} ({user_role})")
    print(f"  Entries         {len(records)}")
    print(f"  Date range      {dates[0]} to {dates[-1]}")
    for name in sorted(project_ids):
        spans = [r["entry_date"] for r in records if r["project"] == name]
        print(f"  Project         {name} "
              f"({min(spans)} to {max(spans)}, {len(spans)} entries)")
    if not project_ids:
        print("  Project         none")
    print(f"  Acknowledged    {acknowledged} entries "
          f"({acknowledged * 100 // len(records)}%)")

    print()
    print("  Tag counts")
    for tag in TAGS:
        print(f"    {tag:<14} {tag_counts[tag]:>3}  {'#' * tag_counts[tag]}")
    if untagged:
        print(f"    {'(untagged)':<14} {untagged:>3}")

    print()
    print(f"  Database written to {DB_PATH}")
    if not tagged:
        print("  Auto tags are empty — run  python tagger.py  to fill them in.")
    # The weekly report is relative to the day you run it, so an import of older
    # entries produces an empty one. Say so here rather than letting it surprise
    # someone mid-demo.
    if recent == 0:
        print("  ⚠ No entries in the last 7 days — the last7days report will be")
        print("    empty. Re-import with --shift-dates to slide the range forward.")
    print()


def write_template(path):
    """Write a CSV with the expected header and two example rows."""
    today = date.today()
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerow({
            "entry_date": (today - timedelta(days=1)).isoformat(),
            "raw_text": "Paired with Nina on her first real feature. Mostly sat "
                        "there while she drove and answered questions.",
            "tags": "collaboration,leadership",
            "project": "Front-End Refactor",
            "acknowledged_by": "",
            "impact_note": "",
            "auto_tags": "",
        })
        writer.writerow({
            "entry_date": today.isoformat(),
            "raw_text": "Shipped the export rewrite behind a flag. Rolled it out "
                        "to 10% and watched the dashboards for an hour.",
            "tags": "wins",
            "project": "",
            "acknowledged_by": "Priya",
            "impact_note": "Export rewrite live for 10% of traffic",
            "auto_tags": "",
        })

    print()
    print(f"  Wrote {path}")
    print(f"  Required columns: {', '.join(CSV_REQUIRED)}")
    print(f"  Optional columns: {', '.join(c for c in CSV_COLUMNS if c not in CSV_REQUIRED)}")
    print("  Dates must be YYYY-MM-DD. tags is comma-separated and must use:")
    print(f"    {', '.join(TAGS)}")
    print("  Leave tags empty to import entries unlabelled.")
    print()


def seed():
    """Wipe and rebuild notch.db from the built-in fixture."""
    records = _fixture_records()
    project_ids = build(records, "Jordan Kim", "Software Engineer")
    summarise(records, project_ids, "Jordan Kim", "Software Engineer")


def main():
    parser = argparse.ArgumentParser(
        description="Build notch.db, from the built-in demo data or from a CSV.",
        epilog="With no arguments, rebuilds the built-in 90-day demo dataset.",
    )
    parser.add_argument("--csv", metavar="PATH",
                        help="Import entries from a CSV instead of the built-in data.")
    parser.add_argument("--template", metavar="PATH",
                        help="Write an example CSV with the expected columns and exit.")
    parser.add_argument("--shift-dates", action="store_true",
                        help="Slide imported dates so the newest entry is today, "
                             "keeping the gaps between them.")
    parser.add_argument("--user-name", default="Jordan Kim",
                        help="Name for the imported user. Default: Jordan Kim")
    parser.add_argument("--user-role", default="Software Engineer",
                        help="Job title for the imported user. Default: Software Engineer")
    args = parser.parse_args()

    if args.template:
        write_template(args.template)
        return 0

    if not args.csv:
        if args.shift_dates:
            parser.error("--shift-dates only applies to --csv "
                         "(the built-in data is already relative to today)")
        seed()
        return 0

    if not os.path.exists(args.csv):
        print(f"\n  ✗  No such file: {args.csv}")
        print(f"     Generate one to start from:  python seed_db.py --template entries.csv\n")
        return 1

    try:
        records, warnings = read_csv(args.csv)
    except CSVError as exc:
        print(f"\n  ✗  {args.csv} was not imported — {len(exc.problems)} problem(s):")
        for problem in exc.problems[:15]:
            print(f"       {problem}")
        if len(exc.problems) > 15:
            print(f"       ...and {len(exc.problems) - 15} more")
        print("\n     Nothing was written. The existing database is untouched.\n")
        return 1

    for warning in warnings:
        print(f"\n  ⚠ {warning}")

    if args.shift_dates:
        shift_to_today(records)

    records.sort(key=lambda r: r["entry_date"])
    project_ids = build(records, args.user_name, args.user_role)
    summarise(records, project_ids, args.user_name, args.user_role)
    return 0


if __name__ == "__main__":
    sys.exit(main())
