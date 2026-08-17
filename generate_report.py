#!/usr/bin/env python3
"""
generate_report.py — STEP 5: the CLI. This is the file you run.

ONE JOB: walk the pipeline end to end for one report, printing what it's doing as
it goes, and hand back a finished PDF.

    python generate_report.py --type last7days
    python generate_report.py --type project --project "Front-End Refactor"
    python generate_report.py --type tag --tag collaboration

The pipeline, in order:

    fetch (SQL)  ->  count (Python)  ->  narrate + cluster (Claude)
                 ->  render charts (matplotlib)  ->  assemble PDF (reportlab)

Notice where the LLM sits: one call, in the middle, doing the reading and writing.
Everything numeric happens on either side of it in plain Python.
"""

import argparse
import os
import sys
import time

import charts
import db
import llm
import report_builder

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

# ---------------------------------------------------------------------------
# Terminal output helpers — this is the part being demoed live, so it should be
# readable and pleasant to watch rather than a wall of log lines.
# ---------------------------------------------------------------------------

BOLD, DIM, BLUE, GREEN, RED, RESET = (
    "\033[1m", "\033[2m", "\033[34m", "\033[32m", "\033[31m", "\033[0m"
)


def header(text):
    print()
    print(f"  {BOLD}{text}{RESET}")
    print(f"  {DIM}{'─' * 58}{RESET}")


def step(number, text):
    print(f"  {BLUE}{number}{RESET}  {text}")


def detail(text):
    print(f"     {DIM}{text}{RESET}")


def done(text):
    print(f"  {GREEN}✓{RESET}  {text}")


def fail(text):
    """Print an error. Multi-line messages keep their indent so they stay readable."""
    lines = str(text).splitlines() or [""]
    print()
    print(f"  {RED}✗  {lines[0]}{RESET}")
    for line in lines[1:]:
        print(f"     {RED}{line}{RESET}")
    print()


# ---------------------------------------------------------------------------
# Report type 1 — Last 7 Days
# ---------------------------------------------------------------------------

def report_last7days(user):
    step("1/5", "Fetching entries from SQLite…")
    data = db.get_last_7_days()
    entries = data["entries"]

    if not entries:
        fail("No entries found in the last 7 days. Run `python seed_db.py` to reseed.")
        return None

    period_label = db.fmt_range(data["period_start"], data["period_end"])
    prev_label = db.fmt_range(data["previous_start"], data["previous_end"])
    detail(f"{len(entries)} entries this week · {len(data['previous_entries'])} last week")
    detail(f"period: {period_label}")

    # --- Python does the arithmetic. No LLM call needed for any of this. ---
    step("2/5", "Computing counts and percentages in Python…")
    this_mix = db.tag_mix_percent(entries)
    last_mix = db.tag_mix_percent(data["previous_entries"])
    recognition = db.recognition_entries(entries)
    top = max(this_mix, key=this_mix.get)
    detail(f"tag mix this week: " + ", ".join(f"{t} {p:g}%" for t, p in this_mix.items() if p))
    detail(f"biggest shift: {top} {last_mix[top]:g}% → {this_mix[top]:g}%")
    detail(f"{len(recognition)} entries with recognition (straight from the DB, no LLM)")

    # --- The one LLM call. ---
    step("3/5", f"Calling Claude ({llm.MODEL}) for narrative…")
    content, usage = _call_llm("last7days", {
        "user_name": user["name"], "user_role": user["role"], "period_label": period_label,
    }, entries)

    step("4/5", "Rendering charts with matplotlib (deterministic — no LLM)…")
    chart_path = charts.week_over_week_tag_mix(this_mix, last_mix)
    detail(os.path.relpath(chart_path))
    chart_list = [(
        chart_path,
        f"Tag mix this week ({period_label}) against last week ({prev_label}). "
        f"Percentages are of entries; an entry with two tags counts toward both.",
    )]

    stat_line = (
        f"{len(entries)} entries · dominant themes: "
        f"{', '.join(content.get('dominant_themes', [])) or 'n/a'} · "
        f"most-tagged: {top} ({this_mix[top]:g}% of entries)"
    )

    return {
        "output_name": "last7days.pdf",
        "title": "Last 7 Days",
        "subtitle": "What your week actually looked like.",
        "period_label": period_label,
        "content": content, "usage": usage, "stat_line": stat_line,
        "recognition": recognition, "charts": chart_list,
    }


# ---------------------------------------------------------------------------
# Report type 2 — Project
# ---------------------------------------------------------------------------

def report_project(user, project_name):
    step("1/5", "Fetching entries from SQLite…")
    project = db.get_project(project_name)
    if project is None:
        available = db.list_project_names()
        fail(f"No project named \"{project_name}\".")
        print(f"     Projects in the database: {', '.join(available) or '(none)'}")
        print()
        return None

    entries = db.get_project_entries(project["id"])
    if not entries:
        fail(f"Project \"{project['name']}\" has no entries attached to it.")
        return None

    period_label = db.fmt_range(project["start_date"], project["end_date"])
    detail(f"{len(entries)} entries tagged to \"{project['name']}\"")
    detail(f"period: {period_label}")

    step("2/5", "Computing counts and percentages in Python…")
    mix = db.tag_mix_percent(entries)
    week_labels, week_counts = db.entries_per_week(
        entries, project["start_date"], project["end_date"]
    )
    recognition = db.recognition_entries(entries)
    busiest = week_labels[week_counts.index(max(week_counts))]
    detail("tag mix: " + ", ".join(f"{t} {p:g}%" for t, p in mix.items() if p))
    detail(f"entries per week: {week_counts} (busiest: week of {busiest})")
    detail(f"{len(recognition)} entries with recognition")

    step("3/5", f"Calling Claude ({llm.MODEL}) for narrative + estimate/actual…")
    content, usage = _call_llm("project", {
        "user_name": user["name"], "user_role": user["role"],
        "period_label": period_label, "project_name": project["name"],
    }, entries)

    step("4/5", "Rendering charts with matplotlib (deterministic — no LLM)…")
    chart_list = []

    p1 = charts.tag_mix(mix, f"Tag mix across {project['name']}", "project_tag_mix.png")
    detail(os.path.relpath(p1))
    chart_list.append((p1, "How the work on this project distributed across tags, "
                           "as a share of the project's entries."))

    p2 = charts.entries_per_week(week_labels, week_counts,
                                 "Entries per week across the project",
                                 "project_entries_per_week.png")
    detail(os.path.relpath(p2))
    chart_list.append((p2, f"Where effort clustered week by week. The heaviest week "
                           f"began {busiest}."))

    # The estimate-vs-actual chart only appears if the entries actually contain
    # that data — the LLM tells us whether it found any.
    eva = content.get("estimate_vs_actual") or {}
    if eva.get("found") and eva.get("actual_days"):
        p3 = charts.estimate_vs_actual(eva.get("task", "Estimated vs actual"),
                                       float(eva["estimated_days"]),
                                       float(eva["actual_days"]),
                                       "project_estimate_vs_actual.png")
        detail(os.path.relpath(p3))
        chart_list.append((p3, eva.get("note", "") or
                           "Estimated duration against actual duration."))
    else:
        detail("no estimate-vs-actual found in these entries — skipping that chart")

    stat_line = (
        f"{len(entries)} entries over {len(week_counts)} weeks · dominant themes: "
        f"{', '.join(content.get('dominant_themes', [])) or 'n/a'}"
    )

    return {
        "output_name": f"project_{_slug(project['name'])}.pdf",
        "title": project["name"],
        "subtitle": "A project, start to finish.",
        "period_label": period_label,
        "content": content, "usage": usage, "stat_line": stat_line,
        "recognition": recognition, "charts": chart_list,
    }


# ---------------------------------------------------------------------------
# Report type 3 — Tag
# ---------------------------------------------------------------------------

def report_tag(user, tag):
    step("1/5", "Fetching entries from SQLite…")
    catalog = db.get_tag_names()
    if tag not in catalog:
        fail(f"No tag named \"{tag}\" in the catalog.")
        print(f"     Tags in the catalog: {', '.join(catalog) or '(none)'}")
        print()
        return None

    entries = db.get_entries_by_tag(tag)
    if not entries:
        fail(f"No entries tagged \"{tag}\".")
        print(f"     Tags in the catalog: {', '.join(catalog)}")
        print()
        return None

    start, end = db.get_full_date_range()
    total = db.count_all_entries()
    period_label = db.fmt_range(start, end)
    detail(f"{len(entries)} entries tagged \"{tag}\" out of {total} total")
    detail(f"period: {period_label}")

    step("2/5", "Computing counts and percentages in Python…")
    share, other_share = db.tag_share_of_period(entries, total)
    recognition = db.recognition_entries(entries)
    detail(f"{tag} is {share:g}% of all entries in the period")
    detail(f"{len(recognition)} entries with recognition")

    # This is the interesting call: the sub-clustering is the one classification
    # job that genuinely needs a model. "Which of these entries are about
    # unblocking someone, versus cross-team work?" is a question about meaning —
    # you cannot answer it by counting.
    step("3/5", f"Calling Claude ({llm.MODEL}) for narrative + sub-clustering…")
    content, usage = _call_llm("tag", {
        "user_name": user["name"], "user_role": user["role"],
        "period_label": period_label, "tag": tag,
    }, entries)

    # …and now Python takes the labels back and does the counting.
    subpatterns = db.count_subpatterns(entries, content.get("subpattern_assignments", []))
    detail(f"Claude grouped them into {len(subpatterns)} sub-patterns; Python counted:")
    for sp in subpatterns:
        detail(f"  · {sp['name']}: {sp['count']} entries ({sp['percent']:g}%)")

    step("4/5", "Rendering charts with matplotlib (deterministic — no LLM)…")
    chart_list = []

    p1 = charts.tag_share(tag, share, other_share, f"tag_share_{_slug(tag)}.png")
    detail(os.path.relpath(p1))
    chart_list.append((p1, f"\"{tag.capitalize()}\" as a share of every entry logged "
                           f"in {period_label}."))

    if subpatterns:
        p2 = charts.subpattern_breakdown(
            subpatterns, f"Inside \"{tag}\": what kind of work it actually was",
            f"subpatterns_{_slug(tag)}.png",
        )
        detail(os.path.relpath(p2))
        chart_list.append((p2, "Sub-patterns identified by Claude reading each entry; "
                               "the counts and percentages were computed in Python "
                               "from those labels."))

    stat_line = (
        f"{len(entries)} of {total} entries ({share:g}%) · "
        f"{len(subpatterns)} sub-patterns · dominant themes: "
        f"{', '.join(content.get('dominant_themes', [])) or 'n/a'}"
    )

    return {
        "output_name": f"tag_{_slug(tag)}.pdf",
        "title": f"Tag report: {tag}",
        "subtitle": f"Every entry tagged \"{tag}\", and what it adds up to.",
        "period_label": period_label,
        "content": content, "usage": usage, "stat_line": stat_line,
        "recognition": recognition, "charts": chart_list,
    }


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _slug(text):
    return "".join(c if c.isalnum() else "_" for c in text.lower()).strip("_")


def _call_llm(report_type, context, entries):
    """Wrap the LLM call so we can time it and report token usage in the terminal."""
    started = time.time()
    content, usage = llm.generate_report_content(report_type, context, entries)
    elapsed = time.time() - started
    detail(f"structured response received in {elapsed:.1f}s "
           f"({usage.input_tokens} in / {usage.output_tokens} out)")
    detail(f"sections written: {', '.join(sorted(content.keys()))}")
    return content, usage


def main():
    parser = argparse.ArgumentParser(
        description="Generate a Notch career impact report as a PDF.",
    )
    parser.add_argument("--type", required=True,
                        choices=["last7days", "project", "tag"],
                        help="Which report to generate.")
    parser.add_argument("--project", help='Project name, e.g. "Front-End Refactor". '
                                          "Required for --type project.")
    parser.add_argument("--tag", help="Tag name, e.g. collaboration. Required for --type tag.")
    args = parser.parse_args()

    if args.type == "project" and not args.project:
        parser.error('--type project needs --project "Front-End Refactor"')
    if args.type == "tag" and not args.tag:
        parser.error("--type tag needs --tag collaboration")

    header("Notch · report generation")

    # --- Graceful failure cases, checked up front so the demo never dies mid-run.
    try:
        user = db.get_user()
    except db.NoDatabaseError as exc:
        fail(str(exc))
        return 1

    # Reads .env via llm.load_api_key(). Checked up front so a missing key fails
    # in a second rather than after the fetch-and-count work.
    try:
        llm.load_api_key()
    except llm.MissingAPIKeyError as exc:
        fail(str(exc))
        return 1

    try:
        if args.type == "last7days":
            result = report_last7days(user)
        elif args.type == "project":
            result = report_project(user, args.project)
        else:
            result = report_tag(user, args.tag)
    except llm.MissingAPIKeyError as exc:
        fail(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 — a demo should explain itself, not traceback
        fail(f"{type(exc).__name__}: {exc}")
        return 1

    if result is None:
        return 1

    step("5/5", "Assembling PDF with charts embedded…")
    output_path = os.path.join(OUTPUT_DIR, result["output_name"])
    report_builder.build_pdf(
        output_path=output_path,
        title=result["title"],
        subtitle=result["subtitle"],
        period_label=result["period_label"],
        user=user,
        content=result["content"],
        stat_line=result["stat_line"],
        recognition=result["recognition"],
        charts=result["charts"],
    )
    detail(f"8 sections · {len(result['charts'])} chart"
           f"{'s' if len(result['charts']) != 1 else ''} embedded")

    size_kb = os.path.getsize(output_path) / 1024
    print()
    done(f"Report ready — {os.path.relpath(output_path)} ({size_kb:.0f} KB)")
    print(f"     {DIM}open {os.path.relpath(output_path)}{RESET}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
