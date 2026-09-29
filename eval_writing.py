"""
eval_writing.py — the words a person reads, run on a fixed set so prompt variants can be read side by side.

    python eval_writing.py notches v4 --all          # every notch in the set (a report needs them all)
    python eval_writing.py notches v5                # only the notches marked compare
    python eval_writing.py reports r1 r2 --notches v4
    python eval_writing.py show --notches v4 v5 --reports r1 r2

evals/writing_set.json is one fictional person's September: fifteen spoken notches, the projects
and tag vocabulary the device sends beside each, and two report periods (a week inside the month).

`notches` runs each transcript through analysis.analyze_text exactly as POST /v2/analyze does on
remote config's defaults: the chat classifier, max_tokens_analyze, the configured models and the
zero-retention provider block. `reports` builds each period's request as the device would (every
notch in range, carrying its writing from the --notches run, its day, its milestone flag and its
transcript) and runs it through v2.report_request, report_facts and write_report at the default
temperature, so two report variants read byte-identical notches.

Every answer and what it cost is cached in evals/writing/<kind>-<variant>.json, so `show` and the
comparison page are free; a cached variant is only called again with --refresh.

There is no score and nothing here is a gate: the outputs are for reading. `show` prints a few
shapes worth checking by eye (takeaway lengths and first words, tags that echo a project, each
mood against the set's `expect`, stock phrases, prose length, milestones cited), the way
TAGGING_EVAL.md prints the shape of an error rather than a rate. The set's `expect` is one
person's judgement, not an oracle.
"""

import argparse
import datetime
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from notch_api import analysis, prompts, v2, wire_v2
from notch_api.openrouter import ModelError, OpenRouterClient, Usage
from notch_api.remote_config import DEFAULTS

HERE = os.path.dirname(os.path.abspath(__file__))
SET_PATH = os.path.join(HERE, "evals", "writing_set.json")
RUNS = os.path.join(HERE, "evals", "writing")
DEVICE = wire_v2.Client.parse("ios/1.0.0+1")

# Phrases that read as a template or an HR system rather than a colleague. Diagnostic only.
STOCK = ("going forward", "next version of", "already good at", "what's working", "what is working",
         "usually get counted", "doesn't show up", "won't show up", "invisible", "keep up", "journey",
         "navigat", "testament", "showcas", "demonstrat", "leverag", "impactful", "proactive", "spearhead",
         "deep dive", "a reminder that", "worth noting", "at the end of the day", "moving forward", "momentum",
         "resilien", "growth mindset", "stakeholder", "ownership of", "the power of")


def load_set():
    with open(SET_PATH) as f:
        return json.load(f)


def _path(kind, variant):
    return os.path.join(RUNS, f"{kind}-{variant}.json")


def _read(kind, variant):
    try:
        with open(_path(kind, variant)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write(kind, variant, body):
    os.makedirs(RUNS, exist_ok=True)
    with open(_path(kind, variant), "w") as f:
        json.dump(body, f, indent=1, ensure_ascii=False)
        f.write("\n")


def _bound(client):
    usage = Usage()
    return client.bound(usage=usage, models=DEFAULTS["models"], provider=DEFAULTS["provider"]), usage


def _spend(usage):
    t = usage.totals()
    return {"cost": round(t["cost"], 6), "prompt_tokens": t["prompt_tokens"],
            "completion_tokens": t["completion_tokens"], "calls": len(usage.calls)}


def analyze(client, notch, data, variant):
    """One notch through POST /v2/analyze's call, on remote config's defaults."""
    bound, usage = _bound(client)
    result = analysis.analyze_text(
        bound, notch["transcript"], project_names=data["project_names"], vocabulary=data["vocabulary"],
        labels=prompts.variant("analyze", variant), classifier=DEFAULTS["classifier"],
        max_tokens=DEFAULTS["chat"]["max_tokens_analyze"], thresholds=DEFAULTS["category_thresholds"],
        project_confidence=DEFAULTS["project_confidence"])
    written = {k: result[k] for k in ("summary", "takeaways", "tags", "mood", "impact_note", "acknowledged_by",
                                     "categories", "classified_by")}
    written["project_name"] = v2.canonical_project(result["project_name"], data["project_names"])
    return written | {"spend": _spend(usage)}


def report_body(period, data, notched):
    """The period's POST /v2/reports body, as the device would send it."""
    start, end = period["range_start"], period["range_end"]
    entries = []
    for n in data["notches"]:
        if start <= n["date"] <= end:
            w = notched[n["slug"]]
            entries.append({"id": n["id"], "date": n["date"], "project_name": w["project_name"], "tags": w["tags"],
                            "categories": w["categories"], "is_milestone": n["is_milestone"], "summary": w["summary"],
                            "takeaways": w["takeaways"], "impact_note": w["impact_note"],
                            "acknowledged_by": w["acknowledged_by"], "transcript": n["transcript"]})
    return {"type": period["type"], "range_start": start, "range_end": end, "range_label": period["range_label"],
            "scope": {}, "author": data["author"], "project_names": data["project_names"], "entries": entries}


def write(client, period, data, notched, variant):
    """One period through POST /v2/reports' call, on remote config's defaults."""
    req = v2.report_request(report_body(period, data, notched), DEFAULTS, DEVICE)
    facts = v2.report_facts(req)
    bound, usage = _bound(client)
    prose, themes, highlights = v2.write_report(
        bound, req, facts, prompts.variant("reports", variant),
        transcripts=len(req["entries"]) <= DEFAULTS["limits"]["report_transcripts_up_to"],
        max_tokens=DEFAULTS["chat"]["max_tokens_report"], temperature=DEFAULTS["chat"]["report_temperature"])
    return {"facts": facts, **prose, "themes": themes, "highlights": highlights, "spend": _spend(usage)}


def run_notches(client, data, variant, *, every, refresh, workers):
    cached = None if refresh else _read("notches", variant)
    wanted = [n for n in data["notches"] if every or n["compare"]]
    results = dict(cached["results"]) if cached else {}
    todo = [n for n in wanted if n["slug"] not in results]
    with ThreadPoolExecutor(workers) as pool:
        for n, out in zip(todo, pool.map(lambda n: analyze(client, n, data, variant), todo)):
            results[n["slug"]] = out
    order = [n["slug"] for n in data["notches"] if n["slug"] in results]
    body = {"kind": "notches", "variant": variant, "model": DEFAULTS["models"]["chat"],
            "classifier": DEFAULTS["classifier"], "run_at": _now(), "results": {s: results[s] for s in order}}
    _write("notches", variant, body)
    return body, len(todo)


def run_reports(client, data, variant, *, notches, refresh):
    labelled = _read("notches", notches)
    if not labelled or any(n["slug"] not in labelled["results"] for n in data["notches"]):
        raise SystemExit(f"Run `python eval_writing.py notches {notches} --all` first: a report needs every notch.")
    cached = None if refresh else _read("reports", variant)
    if cached and cached.get("notches") == notches:
        return cached, 0
    results = {p["id"]: write(client, p, data, labelled["results"], variant) for p in data["periods"]}
    body = {"kind": "reports", "variant": variant, "notches": notches, "model": DEFAULTS["models"]["chat"],
            "run_at": _now(), "results": results}
    _write("reports", variant, body)
    return body, len(results)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Reading the runs.
# ---------------------------------------------------------------------------

def _words(text):
    return len((text or "").split())


def _stock(text):
    low = (text or "").lower()
    return [p for p in STOCK if p in low]


def _echoes_project(tag, project_names):
    """A tag that is, or is a piece of, a project's name ('recon' for Ledger Reconciliation)."""
    words = {w for name in project_names for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) >= 4}
    return any(len(part) >= 4 and any(w.startswith(part) or part.startswith(w) for w in words)
               for part in tag.split("-"))


def show_notches(data, runs):
    lines = []
    for n in data["notches"]:
        if not any(n["slug"] in r["results"] for r in runs):
            continue
        e = n["expect"]
        lines += ["", f"== {n['slug']} ({n['date']}{', milestone' if n['is_milestone'] else ''}) — {n['situation']}",
                  f"   expect: mood {e['mood']}{' (or ' + '/'.join(e['also_ok']) + ')' if e['also_ok'] else ''}"
                  f", project {e['project'] or 'none'}"]
        for r in runs:
            w = r["results"].get(n["slug"])
            if not w:
                continue
            mood = "ok" if w["mood"] == e["mood"] else "near" if w["mood"] in e["also_ok"] else "MISS"
            echo = [t for t in w["tags"] if _echoes_project(t, data["project_names"])]
            lines.append(f"  [{r['variant']}] mood {w['mood']} ({mood}) · project {w['project_name'] or 'none'}"
                         f" · tags {', '.join(w['tags'])}{'  ECHOES PROJECT: ' + ', '.join(echo) if echo else ''}")
            for t in w["takeaways"]:
                lines.append(f"      • {t}  [{_words(t)}w]")
            lines.append(f"      summary [{_words(w['summary'])}w]: {w['summary']}")
            if w["impact_note"] or w["acknowledged_by"]:
                lines.append(f"      impact: {w['impact_note']} · recognized by: {w['acknowledged_by']}")
            found = _stock(" ".join([w["summary"], *w["takeaways"]]))
            if found:
                lines.append(f"      stock phrases: {', '.join(found)}")
    lines += ["", "Totals"]
    for r in runs:
        rs = list(r["results"].values())
        slugs = list(r["results"])
        took = [_words(t) for w in rs for t in w["takeaways"]]
        expect = {n["slug"]: n["expect"] for n in data["notches"]}
        exact = sum(w["mood"] == expect[s]["mood"] for s, w in zip(slugs, rs))
        near = sum(w["mood"] in expect[s]["also_ok"] for s, w in zip(slugs, rs))
        you = sum(t.lower().startswith(("you ", "you'", "i ")) for w in rs for t in w["takeaways"])
        cost = sum(w["spend"]["cost"] for w in rs)
        lines.append(f"  [{r['variant']}] {len(rs)} notches · mood {exact} as expected, {near} near"
                     f" · takeaways {len(took)} ({sum(took) / max(len(took), 1):.1f}w avg, max {max(took, default=0)}w,"
                     f" {you} start 'You'/'I') · summary {sum(_words(w['summary']) for w in rs) / max(len(rs), 1):.1f}w avg"
                     f" · tags {sum(len(w['tags']) for w in rs) / max(len(rs), 1):.1f} avg"
                     f" · ${cost:.4f} over {sum(w['spend']['calls'] for w in rs)} calls")
    return lines


def show_reports(data, runs):
    lines = []
    milestones = {n["id"]: n["slug"] for n in data["notches"] if n["is_milestone"]}
    for p in data["periods"]:
        lines += ["", f"== {p['id']}: {p['range_label']}"]
        for r in runs:
            d = r["results"].get(p["id"])
            if not d:
                continue
            paragraphs = [x for x in d["body"].split("\n\n") if x.strip()]
            prose = " ".join([d["headline"], d["lede"], d["body"]])
            cited = {i for h in d["highlights"] if h["kind"] == "milestone" for i in h["source_entry_ids"]}
            lines += [f"  [{r['variant']} on {r['notches']}] ${d['spend']['cost']:.4f}",
                      f"    headline: {d['headline']}",
                      f"    lede [{_words(d['lede'])}w]: {d['lede']}",
                      f"    body [{_words(d['body'])}w, {len(paragraphs)} paragraphs]:"]
            lines += [f"      {x}" for x in paragraphs]
            for h in d["highlights"]:
                names = [next((n["slug"] for n in data["notches"] if n["id"] == i), i) for i in h["source_entry_ids"]]
                lines.append(f"    ◆ {h['kind']:<13} {h['title']} — {h['detail']}  [{_words(h['detail'])}w] ← {', '.join(names)}")
            lines.append(f"    themes: {', '.join(d['themes'])}")
            numbers = sorted(set(re.findall(r"\d[\d,.]*\s?%?", prose)))
            lines.append(f"    numbers in prose: {', '.join(numbers) or 'none'}")
            lines.append(f"    milestone highlights citing a milestone notch: "
                         f"{', '.join(milestones[i] for i in cited if i in milestones) or 'none'}")
            found = _stock(prose + " " + " ".join(h["title"] + " " + h["detail"] for h in d["highlights"]))
            if found:
                lines.append(f"    stock phrases: {', '.join(found)}")
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the writing prompts on the fixed set, or show cached runs.")
    sub = parser.add_subparsers(dest="command", required=True)
    n = sub.add_parser("notches", help="analyze the set's notches with label variants")
    n.add_argument("variants", nargs="+")
    n.add_argument("--all", action="store_true", help="every notch, not only those marked compare")
    n.add_argument("--refresh", action="store_true")
    n.add_argument("--workers", type=int, default=4)
    r = sub.add_parser("reports", help="write the set's reports with report variants")
    r.add_argument("variants", nargs="+")
    r.add_argument("--notches", default="v4", help="the label variant whose notches the reports read")
    r.add_argument("--refresh", action="store_true")
    s = sub.add_parser("show", help="print cached runs")
    s.add_argument("--notches", nargs="*", default=[])
    s.add_argument("--reports", nargs="*", default=[])
    args = parser.parse_args(argv)
    data = load_set()

    if args.command == "show":
        runs = [_read("notches", v) for v in args.notches]
        reps = [_read("reports", v) for v in args.reports]
        missing = [v for v, x in zip(args.notches + args.reports, runs + reps) if x is None]
        if missing:
            print(f"no cached run for: {', '.join(missing)}", file=sys.stderr)
            return 1
        print("\n".join((show_notches(data, runs) if runs else []) + (show_reports(data, reps) if reps else [])))
        return 0

    try:
        client = OpenRouterClient.from_env()
        for variant in args.variants:
            if args.command == "notches":
                body, called = run_notches(client, data, variant, every=args.all, refresh=args.refresh,
                                           workers=args.workers)
                spent = sum(w["spend"]["cost"] for w in body["results"].values())
            else:
                body, called = run_reports(client, data, variant, notches=args.notches, refresh=args.refresh)
                spent = sum(d["spend"]["cost"] for d in body["results"].values())
            print(f"{args.command} {variant}: {called} run now, {len(body['results'])} cached · ${spent:.4f} in total")
    except (RuntimeError, ModelError) as exc:
        print(f"eval failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
