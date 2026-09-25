"""
reports.py — "Write my report": the numbers at acceptance, the words in a job.

Split the way the demo pipeline splits it (db.py counts, llm.py writes), and for the
same reason: arithmetic over rows we already hold is exact and free, so the model
never does any.

  accept_report   runs inside POST /v1/reports. It validates the request, picks the
                  notches in scope and freezes every number the document shows
                  (counts, momentum, project breakdown, provenance) into the reports
                  row, beside a queued report_jobs row, in one transaction. A report
                  is a frozen artifact (§3.7): a notch deleted later moves nothing.
  run_report_job  the worker. One forced tool call writes the prose against a FACTS
                  block built from those frozen numbers, and may state a number only
                  as FACTS or a notch states it, never one it worked out.

The model's answer is repaired, not trusted: a highlight may cite only notches this
report counted, so an invented id is dropped here rather than rendered as a dead link.
"""

import logging
import sqlite3
from collections import Counter
from datetime import date, timedelta

import llm

from . import store
from .classify import CATEGORIES  # the five: context for the writer, never labels
from .openrouter import ModelError, ModelRefused

log = logging.getLogger(__name__)

REPORT_TYPES = ("week", "month", "quarter", "year", "custom")
HIGHLIGHT_KINDS = ("milestone", "shipped", "collaboration", "note")
TRANSCRIPTS_UP_TO = 60            # above this many notches the prompt carries summaries only
_ADJECTIVE = {"week": "Weekly", "month": "Monthly", "quarter": "Quarterly", "year": "Yearly", "custom": "Custom"}


# ---------------------------------------------------------------------------
# Acceptance: validate, count, freeze.
# ---------------------------------------------------------------------------

def accept_report(conn, user_id, req):
    """
    POST /v1/reports -> (report_id, job_id, created).

    Idempotent on the client-minted id, as entries are: a repeat returns the existing
    job and counts nothing again, which is what makes a double-tap on "Write my
    report" free. The same id under another user is a 409.
    """
    req = _parse(req)
    job_id = _existing_job(conn, user_id, req["id"])
    if job_id:
        return req["id"], job_id, False
    if req["project_id"] is not None and not conn.execute(
            "SELECT 1 FROM projects WHERE id = ? AND user_id = ?", (req["project_id"], user_id)).fetchone():
        raise store.ApiError("not_found", 404, "No such project.")

    tz = store.user_zone(conn, user_id)
    rows = _scope(conn, user_id, req, tz)
    if not rows:
        raise store.ApiError("empty_range", 422, "There are no notches in this range to write about.")
    granularity, buckets = momentum([store.local_date(r["recorded_at"], tz) for r in rows],
                                    req["range_start"], req["range_end"])
    job_id = store.new_id()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO reports (id, user_id, type, range_start, range_end, project_id, tag,
                                     generated_at, range_label, eyebrow, source_entry_ids, notch_count,
                                     project_count, milestone_count, project_breakdown, momentum,
                                     momentum_granularity)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (req["id"], user_id, req["type"], req["range_start"].isoformat(), req["range_end"].isoformat(),
                 req["project_id"], req["tag"], store.now(), req["range_label"],
                 f"{_ADJECTIVE[req['type']]} report · {req['range_label']}",
                 store.json_dump([r["id"] for r in rows]), len(rows),
                 len({r["project_id"] for r in rows} - {None}), sum(r["is_milestone"] for r in rows),
                 store.json_dump(project_breakdown([r["project_name"] for r in rows])),
                 store.json_dump(buckets), granularity),
            )
            conn.execute("INSERT INTO report_jobs (id, user_id, report_id) VALUES (?, ?, ?)",
                         (job_id, user_id, req["id"]))
    except sqlite3.IntegrityError:
        # A concurrent repeat of this request won the insert: answer as a repeat (§3.5).
        existing = _existing_job(conn, user_id, req["id"])
        if existing is None:
            raise
        return req["id"], existing, False
    return req["id"], job_id, True


def momentum(dates, range_start, range_end):
    """
    -> (granularity, [{date, count}]): every bucket from the one holding range_start to
    the one holding range_end, zeros included, `date` = the bucket's first day.

    Granularity by inclusive length: up to 31 days by day, up to 120 by Monday-started
    week, else by calendar month. So the first bucket may start before the range.
    """
    days = (range_end - range_start).days + 1
    granularity = "day" if days <= 31 else "week" if days <= 120 else "month"
    counts = Counter(_bucket(d, granularity) for d in dates)
    series, cursor = [], _bucket(range_start, granularity)
    while cursor <= range_end:
        series.append({"date": cursor.isoformat(), "count": counts[cursor]})
        try:
            cursor = (date(cursor.year + cursor.month // 12, cursor.month % 12 + 1, 1) if granularity == "month"
                      else cursor + timedelta(days=1 if granularity == "day" else 7))
        except (OverflowError, ValueError):  # no bucket after 9999-12: range_end was in this one
            break
    return granularity, series


def project_breakdown(project_names):
    """
    One project name per notch in scope (None when unassigned) -> [{name, notch_count, share}].

    share = floor(100 * n / all notches), unassigned ones included in the total, the same
    rule as GET /v1/projects; sorted by count desc, then name.
    """
    counts = Counter(name for name in project_names if name)
    return [{"name": name, "notch_count": n, "share": 100 * n // len(project_names)}
            for name, n in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


def _bucket(d, granularity):
    if granularity == "week":
        return d - timedelta(days=d.weekday())
    return d.replace(day=1) if granularity == "month" else d


def _parse(req):
    """The request's fields, checked and normalised, else ApiError('invalid_request')."""
    def bad(message):
        return store.ApiError("invalid_request", 400, message)

    if not isinstance(req, dict):
        raise bad("The body must be a JSON object.")
    report_id, label, project_id, tag = (req.get(k) for k in ("id", "range_label", "project_id", "tag"))
    if not isinstance(report_id, str) or not report_id.strip():
        raise bad("id must be a non-empty string.")
    if req.get("type") not in REPORT_TYPES:
        raise bad(f"type must be one of: {', '.join(REPORT_TYPES)}.")
    try:
        start, end = store.parse_date(req.get("range_start")), store.parse_date(req.get("range_end"))
    except ValueError:
        raise bad("range_start and range_end must be YYYY-MM-DD dates.") from None
    if start > end:
        raise bad("range_start is after range_end.")
    if not isinstance(label, str) or not label.strip():
        raise bad("range_label must be a non-empty string.")
    if project_id is not None and (not isinstance(project_id, str) or not project_id):
        raise bad("project_id must be a non-empty string or null.")
    if tag is not None:
        tag = store.normalize_tag(tag) if isinstance(tag, str) else ""
        if not tag:
            raise bad("tag must be a non-empty tag or null.")
    return {"id": report_id, "type": req["type"], "range_start": start, "range_end": end,
            "range_label": label.strip(), "project_id": project_id, "tag": tag}


def _existing_job(conn, user_id, report_id):
    """The job of the report this id already names, or None."""
    row = conn.execute("SELECT r.user_id, j.id AS job_id FROM reports r "
                       "LEFT JOIN report_jobs j ON j.report_id = r.id WHERE r.id = ?", (report_id,)).fetchone()
    if row and row["user_id"] != user_id:
        raise store.ApiError("conflict", 409, "This report id is already in use.")
    return row["job_id"] if row else None


def _scope(conn, user_id, req, tz):
    """
    Complete notches whose day in the user's zone `tz` falls in the inclusive range,
    oldest first, narrowed by the optional project and tag. Instants are stored as
    'YYYY-MM-DDTHH:MM:SSZ', so the local day bounds, turned into instants, compare as
    text and use the (user_id, recorded_at) index.
    """
    rows = conn.execute(
        """
        SELECT e.id, e.recorded_at, e.project_id, e.is_milestone, e.tags, p.name AS project_name
        FROM entries e LEFT JOIN projects p ON p.id = e.project_id AND p.user_id = e.user_id
        WHERE e.user_id = ? AND e.analysis_state = 'complete'
          AND e.recorded_at >= ? AND e.recorded_at < ?
          AND (? IS NULL OR e.project_id = ?)
        ORDER BY e.recorded_at, e.id
        """,
        (user_id, *store.local_day_bounds(req["range_start"], req["range_end"], tz),
         req["project_id"], req["project_id"]),
    ).fetchall()
    # Whole-tag match: a LIKE over the JSON text would let 'flaky' match 'flaky-tests'.
    return [r for r in rows if req["tag"] is None or req["tag"] in store.json_list(r["tags"])]


# ---------------------------------------------------------------------------
# The writing job.
# ---------------------------------------------------------------------------

def _borrowed_rules():
    """The demo writer's VOICE, strengths constraint and uncounted-work paragraphs, sliced so they cannot drift."""
    prompt = llm.SYSTEM_PROMPT
    block = prompt[prompt.index("VOICE\n"):prompt.index("\n\nDATES\n")]
    for header in ("VOICE", "HARD CONSTRAINT ON STRENGTHS & GROWTH", "WORK THAT DOESN'T USUALLY GET COUNTED"):
        assert f"{header}\n" in block, f"llm.SYSTEM_PROMPT no longer has its {header} paragraph"
    return block


SYSTEM_PROMPT = f"""You are the report writer for Notch, a voice-first career impact tracker.

People speak short notches about their work days. You turn the notches in one date range into
a short report document in the Notch app: a page they will want to read, keep and bring to a
review.

{_borrowed_rules()}

THE DOCUMENT
- headline: a short, evocative title for the period, at most 7 words, no colon, built from a
  specific event or phrase in these notches. It is not the date range; the app shows that
  separately.
- lede: 1-2 sentences on what the period was mostly about.
- body: 1-3 short paragraphs separated by a blank line. Carry, in this order: what is working;
  one direction that builds on a strength (the shape above); at most one piece of work that
  doesn't usually get counted, if there is one; and a forward frame, something concrete the
  person might do or say next. Not "keep up the great work". Parts may share a paragraph, and
  are never labelled (no "What's working:").
- highlights: 2-4 moments worth a card. title is 2-5 words naming the moment; detail is one
  clause on how it went; kind is shipped, collaboration or note, or milestone only for a notch
  marked milestone; source_entry_ids lists the [id ...] values of the notches it rests on,
  copied exactly.
- themes: 3-5 hashtag-style handles for the period: lowercase, 1-3 words joined by hyphens, no
  '#'. Prefer handles the notches already carry in their tags. Never a project's name: projects
  have their own breakdown.
Word the headline, titles and details from these notches, never from these instructions. Where
the sections before THE DOCUMENT say otherwise (they pick 1-2 uncounted entries), THE DOCUMENT
wins.

NUMBERS RULE
State a number only as the FACTS block or a notch states it, copied exactly. Never compute,
round, estimate or total anything. Write no dates: say "early in the week" or "mid-month".

The CATEGORY COUNTS block (wins, collaboration, leadership, growth, challenges) is for your
understanding only. Never name a category or state its count in the prose, and never use one as
a label, heading or theme."""

WRITE_REPORT = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "A short evocative title: at most 7 words, no colon."},
        "lede": {"type": "string", "description": "1-2 sentences on what the period was mostly about."},
        "body": {"type": "string", "description": "1-3 short paragraphs separated by a blank line."},
        "highlights": {
            "type": "array", "minItems": 2, "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "2-5 words."},
                    "detail": {"type": "string", "description": "One clause."},
                    "kind": {"type": "string", "enum": list(HIGHLIGHT_KINDS)},
                    "source_entry_ids": {"type": "array", "items": {"type": "string"},
                                         "description": "The [id ...] values it rests on, copied exactly."},
                },
                "required": ["title", "detail", "kind", "source_entry_ids"],
                "additionalProperties": False,
            },
        },
        "themes": {"type": "array", "minItems": 3, "maxItems": 5, "items": {"type": "string"},
                   "description": "Hashtag-style handles: lowercase, words joined by '-', no '#'."},
    },
    "required": ["headline", "lede", "body", "highlights", "themes"],
    "additionalProperties": False,
}


def run_report_job(db_path, job_id, *, client):
    """
    queued -> counting (build the facts) -> writing (the model call) -> complete, each
    state its own transaction, the prose, highlights and `complete` in one. A ModelError
    fails the job with its code; anything unexpected is logged and fails it
    `model_unavailable`. It never raises for a model failure: GET /v1/jobs reports it.
    A job that is already finished, or gone, is left alone; one whose report is discarded
    mid-way (its job row goes with it) stops at its next step, before any model call.
    """
    conn = store.connect(db_path)
    try:
        job = conn.execute("SELECT user_id, report_id, state FROM report_jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None or job["state"] in ("complete", "failed"):
            return
        try:
            with conn:  # read under the transition's write lock, so no discard lands in between
                if not _set_job(conn, job_id, "counting"):
                    return
                report = conn.execute("SELECT * FROM reports WHERE id = ? AND user_id = ?",
                                      (job["report_id"], job["user_id"])).fetchone()
            entries = conn.execute(
                """
                SELECT e.*, p.name AS project_name
                FROM entries e LEFT JOIN projects p ON p.id = e.project_id AND p.user_id = e.user_id
                WHERE e.user_id = ? AND e.id IN (SELECT value FROM json_each(?))
                ORDER BY e.recorded_at, e.id
                """,
                (report["user_id"], report["source_entry_ids"]),
            ).fetchall()
            user = _user_message(conn, report, entries)
            with conn:
                if not _set_job(conn, job_id, "writing"):
                    return  # discarded while it was counted: nothing to ask the model for
            report_ids, milestone_ids = (set(store.json_list(report["source_entry_ids"])),
                                         {e["id"] for e in entries if e["is_milestone"]})
            projects = {store.normalize_tag(r["name"]) for r in
                        conn.execute("SELECT name FROM projects WHERE user_id = ?", (report["user_id"],))}
            prose, themes, highlights = client.tool_call(
                system=SYSTEM_PROMPT, user=user, tool_name="write_report",
                description="Write the Notch report document for this range.", parameters=WRITE_REPORT,
                temperature=0.3, max_tokens=4000, parse=lambda doc: _clean(doc, report_ids, milestone_ids, projects))
            with conn:
                if not conn.execute("UPDATE reports SET headline = ?, lede = ?, body = ?, themes = ?, updated_at = ? "
                                    "WHERE id = ? AND user_id = ?",
                                    (prose["headline"], prose["lede"], prose["body"], store.json_dump(themes),
                                     store.now(), report["id"], report["user_id"])).rowcount:
                    return  # discarded while the model was writing: nothing left to write into
                conn.executemany(
                    "INSERT INTO report_highlights (id, report_id, user_id, ordinal, title, detail, kind, "
                    "source_entry_ids) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(store.new_id(), report["id"], report["user_id"], n, h["title"], h["detail"], h["kind"],
                      store.json_dump(h["source_entry_ids"])) for n, h in enumerate(highlights)])
                _set_job(conn, job_id, "complete")
        except ModelError as exc:
            log.warning("report job %s failed: %s (%s)", job_id, exc.code, exc.message)
            with conn:
                _set_job(conn, job_id, "failed", exc.code)
        except Exception:
            log.exception("report job %s failed unexpectedly", job_id)
            with conn:
                _set_job(conn, job_id, "failed", "model_unavailable")
    finally:
        conn.close()


def _set_job(conn, job_id, state, code=None):
    """One report_jobs state write -> 1, or 0 once the report is discarded. finished_at marks the terminal states."""
    now = store.now()
    finished = now if state in ("complete", "failed") else None
    return conn.execute("UPDATE report_jobs SET state = ?, failure_code = ?, finished_at = ?, updated_at = ? "
                        "WHERE id = ?", (state, code, finished, now, job_id)).rowcount


def _user_message(conn, report, entries):
    """Who, what range, the FACTS (the frozen numbers), the per-category counts, then every notch."""
    user = conn.execute("SELECT display_name, role, industry, years_experience FROM users WHERE id = ?",
                        (report["user_id"],)).fetchone()
    who = ", ".join(filter(None, [user["role"], user["industry"], user["years_experience"]
                                  and f"{user['years_experience']} years' experience"]))
    scope = []
    if report["project_id"]:
        project = conn.execute("SELECT name FROM projects WHERE id = ? AND user_id = ?",
                               (report["project_id"], report["user_id"])).fetchone()
        scope.append(f'the project "{project["name"] if project else report["project_id"]}"')
    if report["tag"]:
        scope.append(f"notches tagged #{report['tag']}")

    total = report["notch_count"]
    categories = Counter(c for e in entries for c in set(store.json_list(e["categories"])) if c in CATEGORIES)
    recognitions = dict.fromkeys(e["acknowledged_by"] for e in entries if e["acknowledged_by"])
    transcripts = len(entries) <= TRANSCRIPTS_UP_TO
    if not transcripts:
        log.info("report %s: %d notches, sending summaries without transcripts", report["id"], len(entries))

    lines = [
        f"Write the {_ADJECTIVE[report['type']].lower()} report for {user['display_name'] or 'this person'}"
        f"{f' ({who})' if who else ''}, covering {report['range_label']}.",
        f"Scope: {' and '.join(scope) or 'every notch in the range'}.",
        "",
        "FACTS (numbers you may state, copied verbatim)",
        f"- notches: {total}",
        f"- projects: {report['project_count']}",
        f"- milestones: {report['milestone_count']}",
        "- project breakdown: " + ("; ".join(f"{p['name']}: {_notches(p['notch_count'])}, {p['share']}%"
                                            for p in store.json_list(report["project_breakdown"])) or "none"),
        "- recognized by: " + (", ".join(recognitions) or "nobody named"),
        "",
        "CATEGORY COUNTS (context only: never name a category or state these counts)",
        "- " + ("; ".join(f"{c}: {categories[c]} of {_notches(total)}"
                          for c in sorted(categories, key=lambda c: (-categories[c], CATEGORIES.index(c))))
                or "none"),
        "",
        f"NOTCHES ({'with transcripts' if transcripts else 'summaries only'})",
    ]
    for row in entries:
        e = store.entry_to_wire(row, row["project_name"], None)
        lines.append(f"[id {e['id']}] {e['recorded_at'][:10]} — project: {e['project'] or 'none'}"
                     f" — tags: {', '.join(e['tags']) or 'none'}"
                     f" — categories: {', '.join(store.json_list(row['categories'])) or 'none'}"
                     + (" — milestone" if e["is_milestone"] else ""))
        lines.append(f"  summary: {e['summary']}")
        lines += [f"  takeaway: {t}" for t in e["takeaways"]]
        if e["impact_note"]:
            lines.append(f"  impact: {e['impact_note']}")
        if e["acknowledged_by"]:
            lines.append(f"  recognized by: {e['acknowledged_by']}")
        if transcripts:
            lines.append(f"  transcript: {e['transcript']}")
        lines.append("")
    return "\n".join(lines)


def _notches(n):
    return f"{n} notch" if n == 1 else f"{n} notches"


def _list(value):
    """A list from the model, which sometimes sends a nested array as JSON text instead."""
    value = store.loose_json(value)
    return value if isinstance(value, list) else []


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _clean(doc, report_ids, milestone_ids, projects):
    """
    The model's document, made safe to store -> (prose, themes, highlights).

    Missing prose is a refusal: there is nothing honest to render. Everything else is
    repaired: themes normalised, category and project names dropped, at most five; a highlight's ids
    cut to the notches this report counted; an unknown kind, or `milestone` resting on
    no milestone notch, becomes `note` (the app draws a milestone with the Tree's badge).
    """
    prose = {key: _text(doc.get(key)) for key in ("headline", "lede", "body")}
    if not all(prose.values()):
        raise ModelRefused(f"write_report left {', '.join(k for k, v in prose.items() if not v)} empty.")
    themes = [t for t in store.normalize_tags(_list(doc.get("themes"))) if t not in CATEGORIES and t not in projects][:5]
    highlights = []
    for h in _list(doc.get("highlights")):
        if not isinstance(h, dict) or not _text(h.get("title")):
            continue
        ids = list(dict.fromkeys(i for i in _list(h.get("source_entry_ids")) if isinstance(i, str) and i in report_ids))
        kind = h.get("kind") if h.get("kind") in HIGHLIGHT_KINDS else "note"
        if kind == "milestone" and not milestone_ids.intersection(ids):
            kind = "note"
        highlights.append({"title": _text(h["title"]), "detail": _text(h.get("detail")), "kind": kind,
                           "source_entry_ids": ids})
    return prose, themes, highlights
