"""
eval_moods.py — how steady, and how right, each classifier's mood is on the writing set.

    python eval_moods.py [--runs 3] [--refresh] [--workers 4]
    python eval_moods.py show

The app's How it felt charts draw each notch's mood, so a mood that flips between runs moves the
chart. This runs evals/writing_set.json's notches through both ways /v2 can decide a notch's mood,
project and categories, `--runs` times each, with the project list the device sends:

  chat  analysis.classify_by_chat: the chat model with v4's fallback prompt (remote config
        classifier "chat")
  jev   classify.classify: Jev's typed decisions at remote config's thresholds (classifier "jev")

and prints, for each: moods that match the set's `expect` (exactly, or one of its also_ok), notches
whose mood came back the same on every run, the same two for the project, and notches whose
categories held. The set's `expect` is one person's judgement, not an oracle. Raw answers are cached
in evals/moods/<classifier>.json, so `show` is free; --refresh asks the models again.
"""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import eval_writing as ew
from notch_api import analysis, classify, v2
from notch_api.openrouter import ModelError, OpenRouterClient
from notch_api.remote_config import DEFAULTS

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "evals", "moods")
CLASSIFIERS = ("chat", "jev")


def decide(client, notch, data, how):
    """One notch's mood, project and categories, the way remote config's `classifier` would decide them."""
    bound, usage = ew._bound(client)
    if how == "chat":
        r = analysis.classify_by_chat(bound, notch["transcript"], project_names=data["project_names"],
                                      vocabulary=data["vocabulary"])
    else:
        r = classify.classify(bound, notch["transcript"], project_names=data["project_names"],
                              thresholds=DEFAULTS["category_thresholds"], project_confidence=DEFAULTS["project_confidence"])
    return {"mood": r["mood"], "project_name": v2.canonical_project(r["project_name"], data["project_names"]),
            "categories": sorted(r["categories"]), "spend": ew._spend(usage)}


def run(client, data, how, *, runs, refresh, workers):
    path = os.path.join(RUNS, f"{how}.json")
    if not refresh and os.path.exists(path):
        with open(path) as f:
            cached = json.load(f)
        if cached.get("runs") == runs and cached.get("model") == DEFAULTS["models"][how if how == "chat" else "classifier"]:
            return cached, 0
    jobs = [(n, i) for n in data["notches"] for i in range(runs)]
    with ThreadPoolExecutor(workers) as pool:
        answers = list(pool.map(lambda job: decide(client, job[0], data, how), jobs))
    results = {}
    for (n, _), answer in zip(jobs, answers):
        results.setdefault(n["slug"], []).append(answer)
    body = {"classifier": how, "model": DEFAULTS["models"][how if how == "chat" else "classifier"], "runs": runs,
            "run_at": ew._now(), "results": results}
    os.makedirs(RUNS, exist_ok=True)
    with open(path, "w") as f:
        json.dump(body, f, indent=1, ensure_ascii=False)
        f.write("\n")
    return body, len(jobs)


def show(data, bodies):
    expect = {n["slug"]: n["expect"] for n in data["notches"]}
    lines = []
    for body in bodies:
        rs = body["results"]
        answers = [(s, a) for s, xs in rs.items() for a in xs]
        steady = sum(len({a["mood"] for a in xs}) == 1 for xs in rs.values())
        exact = sum(a["mood"] == expect[s]["mood"] for s, a in answers)
        near = sum(a["mood"] in expect[s]["also_ok"] for s, a in answers)
        p_steady = sum(len({a["project_name"] for a in xs}) == 1 for xs in rs.values())
        p_right = sum(a["project_name"] == expect[s]["project"] for s, a in answers)
        c_steady = sum(len({tuple(a["categories"]) for a in xs}) == 1 for xs in rs.values())
        cost = sum(a["spend"]["cost"] for _, a in answers)
        lines.append(f"[{body['classifier']}] {body['model']}, {body['runs']} runs of {len(rs)} notches: mood steady on "
                     f"{steady} of {len(rs)}, {exact} of {len(answers)} answers as expected and {near} near · project "
                     f"steady on {p_steady}, right in {p_right} of {len(answers)} · categories steady on {c_steady} · ${cost:.4f}")
    lines += ["", "notch: expected mood (also fine) · each classifier's moods, run by run · expected project"]
    for n in data["notches"]:
        e = n["expect"]
        moods = " · ".join(f"{b['classifier']} {','.join(a['mood'] for a in b['results'][n['slug']])}" for b in bodies)
        projects = " · ".join(f"{b['classifier']} {','.join(a['project_name'] or 'none' for a in b['results'][n['slug']])}"
                              for b in bodies)
        lines.append(f"  {n['slug']}: {e['mood']}{' (' + '/'.join(e['also_ok']) + ')' if e['also_ok'] else ''} · {moods}")
        lines.append(f"      project {e['project'] or 'none'} · {projects}")
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description="Mood steadiness: the chat model against Jev, on the writing set.")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "show"])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    data = ew.load_set()
    if args.command == "show":
        bodies = []
        for how in CLASSIFIERS:
            path = os.path.join(RUNS, f"{how}.json")
            if not os.path.exists(path):
                print(f"no cached run for {how}: run `python eval_moods.py` first", file=sys.stderr)
                return 1
            with open(path) as f:
                bodies.append(json.load(f))
        print("\n".join(show(data, bodies)))
        return 0
    try:
        client = OpenRouterClient.from_env()
        bodies = []
        for how in CLASSIFIERS:
            body, called = run(client, data, how, runs=args.runs, refresh=args.refresh, workers=args.workers)
            print(f"{how}: {called} decided now")
            bodies.append(body)
    except (RuntimeError, ModelError) as exc:
        print(f"eval failed: {exc}", file=sys.stderr)
        return 1
    print("\n".join(show(data, bodies)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
