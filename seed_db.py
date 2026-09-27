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

import os
import sqlite3
from collections import Counter
from datetime import date, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notch.db")

# The tag catalog. Names and explanations live in the `tags` table so the
# tagger can slip them into the prompt at call time — adding a row here is
# how a new tag becomes available, without editing the prompt.
#
# Explanations are the v4 tagging policy (what each tag includes and excludes).
# The prompt itself only knows how to *apply* a catalog, not what the current
# names are.
TAG_CATALOG = [
    (
        "wins",
        "Something concrete LANDED, or a specific defect was CAUGHT before it "
        "reached users. Shipped, merged, built, fixed, measurably improved. "
        "Helping someone else succeed is not a win — that is collaboration. "
        "Unblocking a teammate, debugging alongside them, saving them hours — "
        "not wins. Two exceptions: catching a real defect in someone's code IS "
        "a win, and BUILDING something that helps everyone (a codemod, a tool) "
        "IS a win, because the thing exists now. Not wins: agreeing on a plan, "
        "attending a productive meeting, or work that overran its estimate "
        "without producing a stated result.",
    ),
    (
        "collaboration",
        "Another person was actually involved in the work: pairing, unblocking "
        "them, reviewing their code, answering their question, a cross-team "
        "back-and-forth, walking someone through something. If another person "
        "was involved at all, collaboration applies even when other tags also "
        "apply — helping someone and then writing it up in the wiki is "
        "collaboration AND leadership, not leadership instead. Onboarding "
        "someone, walking someone through a system, demoing to the team and "
        "taking their feedback: all collaboration, whatever else is also true. "
        "Sitting in a meeting and only listening is not collaboration. It "
        "means you and another person worked something out together.",
    ),
    (
        "leadership",
        "Work NOBODY ASKED FOR that raises the team's floor, or deliberately "
        "stepping back so someone else can own something. A runbook nobody "
        "asked about, docs nobody requested, careful interview feedback, an "
        "RFC setting direction, disagreeing with a design and changing it, "
        "letting a junior drive, a codemod so nobody else has to do the "
        "migration by hand, writing a handover so nobody has to reverse-engineer "
        "your work. Reviewing a pull request, unblocking a teammate, or "
        "agreeing on a shared approach in a meeting are not leadership by "
        "themselves. When leadership applies alongside collaboration, use both.",
    ),
    (
        "growth",
        "The entry shows the PERSON changing, not just the task moving. "
        "Realising something, being wrong, being surprised, sitting with "
        "discomfort, finally doing the thing they had put off. A single wry "
        "or self-aware aside is enough — 'feels obvious now', 'which surprised "
        "me', 'trying to be patient about it', 'that I keep saying I'll write', "
        "'the test was the harder part'.",
    ),
    (
        "challenges",
        "The work was a slog or it went sideways: tedium, a hard investigation, "
        "a bad estimate that hurt, an incident, an ugly merge, unglamorous "
        "groundwork. Tag challenges whenever the work involved real friction, "
        "even if it was resolved and the entry sounds calm about it. Always "
        "challenges: a production incident (whoever caused it), an on-call "
        "surprise or discovering something has been quietly broken for months, "
        "hunting down a confusing cause, work that took materially longer than "
        "estimated because it was tangled. What is NOT challenges: a passing "
        "'boring' or 'took longer than planned' attached to an entry whose "
        "real story is a clean result.",
    ),
]
TAGS = [name for name, _ in TAG_CATALOG]

PROJECT_NAME = "Front-End Refactor"

# The project runs from 56 days ago to 17 days ago — about 5.5 weeks, sitting in
# the middle of the 90-day range so it's clearly bounded on both sides.
PROJECT_START_DAYS_AGO = 56
PROJECT_END_DAYS_AGO = 17

SCHEMA = """
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS projects;
DROP TABLE IF EXISTS tags;
DROP TABLE IF EXISTS entries;

CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    name TEXT,
    role TEXT,              -- job title, e.g. "Software Engineer"
    industry TEXT,          -- e.g. "Technology", collected at onboarding
    years_experience INTEGER  -- whole years in the field, collected at onboarding
);

CREATE TABLE projects (
    id INTEGER PRIMARY KEY,
    user_id INTEGER,
    name TEXT,
    start_date TEXT,   -- ISO date
    end_date TEXT      -- ISO date, nullable if ongoing
);

CREATE TABLE tags (
    name TEXT PRIMARY KEY,   -- the exact string the tagger must return
    explanation TEXT NOT NULL
);

CREATE TABLE entries (
    id INTEGER PRIMARY KEY,
    user_id INTEGER,
    entry_date TEXT,        -- ISO date
    raw_text TEXT,          -- the "voice journal" text, written as if transcribed from speech
    tags TEXT,              -- comma-separated names from the tags table
    auto_tags TEXT,         -- comma-separated open-vocabulary keywords, filled in by tagger.py.
                            -- NULL until tagged. Never feeds a chart — see Backend.md.
    project_id INTEGER,     -- nullable, FK to projects
    acknowledged_by TEXT,   -- nullable — who recognized this, if anyone
    impact_note TEXT        -- nullable — a short, specific stated impact/result
);
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


def seed():
    """Wipe and rebuild notch.db from the ENTRIES table above."""
    today = date.today()

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    conn.execute(
        "INSERT INTO users (id, name, role, industry, years_experience) "
        "VALUES (?, ?, ?, ?, ?)",
        (1, "Jordan Kim", "Software Engineer", "Technology", 5),
    )

    project_start = today - timedelta(days=PROJECT_START_DAYS_AGO)
    project_end = today - timedelta(days=PROJECT_END_DAYS_AGO)
    conn.execute(
        "INSERT INTO projects (id, user_id, name, start_date, end_date) VALUES (?, ?, ?, ?, ?)",
        (1, 1, PROJECT_NAME, project_start.isoformat(), project_end.isoformat()),
    )

    conn.executemany(
        "INSERT INTO tags (name, explanation) VALUES (?, ?)",
        TAG_CATALOG,
    )

    for days_ago, raw_text, tags, is_project, acknowledged_by, impact_note in ENTRIES:
        entry_date = today - timedelta(days=days_ago)
        conn.execute(
            """INSERT INTO entries
               (user_id, entry_date, raw_text, tags, project_id, acknowledged_by, impact_note)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                1,
                entry_date.isoformat(),
                raw_text,
                tags,
                1 if is_project else None,
                acknowledged_by,
                impact_note,
            ),
        )

    conn.commit()

    # ---- Print a summary so you can sanity-check the data before demoing ----
    tag_counts = Counter()
    for _, _, tags, _, _, _ in ENTRIES:
        tag_counts.update(t.strip() for t in tags.split(","))

    acknowledged = sum(1 for e in ENTRIES if e[4])
    project_entries = sum(1 for e in ENTRIES if e[3])
    oldest = today - timedelta(days=max(e[0] for e in ENTRIES))
    newest = today - timedelta(days=min(e[0] for e in ENTRIES))

    print()
    print("  Seeded notch.db")
    print("  " + "-" * 52)
    print(f"  User            Jordan Kim (Software Engineer)")
    print(f"  Industry        Technology · 5 years experience")
    print(f"  Entries         {len(ENTRIES)}")
    print(f"  Date range      {oldest.isoformat()} to {newest.isoformat()}")
    print(f"  Project         {PROJECT_NAME} "
          f"({project_start.isoformat()} to {project_end.isoformat()}, {project_entries} entries)")
    print(f"  Acknowledged    {acknowledged} entries ({acknowledged * 100 // len(ENTRIES)}%)")
    print(f"  Tag catalog     {len(TAG_CATALOG)} tags (name + explanation)")
    print()
    print("  Tag counts")
    for tag in TAGS:
        bar = "#" * tag_counts[tag]
        print(f"    {tag:<14} {tag_counts[tag]:>3}  {bar}")
    print()
    print(f"  Database written to {DB_PATH}")
    print("  Auto tags are empty — run  python tagger.py  to fill them in.")
    print()

    conn.close()


if __name__ == "__main__":
    seed()
