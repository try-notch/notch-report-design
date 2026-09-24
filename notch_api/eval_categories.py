"""
eval_categories.py — how well each classifier agrees with the hand-labelled categories.

    python -m notch_api.eval_categories [--refresh] [--workers 4]

Runs seed_db's 52 transcripts through both ways a notch gets its five categories:

  jev   classify.classify: one probability per category, applied at
        config.CATEGORY_THRESHOLD (the likeliest one if none clears it)
  chat  analysis.classify_by_chat: the chat model with the measured v4 category
        prompt, the fallback when Jev fails

and prints, for each, the exact-set match against seed_db's hand labels (the same
strict metric as TAGGING_EVAL.md) and per-category tp / fp / fn. For Jev it also
prints the per-category threshold that would have agreed with the labels most
often. Those thresholds are chosen on the same 52 notches they are scored on, so
they are flagged in-sample: a hint for where to look, not a setting to copy.

The hand labels are one person's judgement, not an oracle, so this is reported and
never a gate. As in eval_tags.py, no project list or tag vocabulary is sent: they
would make a category score depend on the order notches happened to be processed.

Raw model output is cached in data/evals/ (categories-jev.json, categories-chat.json),
so re-scoring is free. A cache made by another model, or for a different number of
notches, is ignored; --refresh calls the models again regardless. There is no
offline mode: a fake model's agreement means nothing (the tests drive run() with one).
"""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import seed_db

from . import analysis, classify, config
from .openrouter import ModelError, OpenRouterClient

CACHE_DIR = os.path.join(config.REPO_ROOT, "data", "evals")
CATEGORIES = classify.CATEGORIES
THRESHOLDS = [round(0.05 * i, 2) for i in range(1, 20)]  # 0.05 .. 0.95


def labels():
    """The hand labels, one set per seed_db.ENTRIES item."""
    return [set(entry[2].split(",")) for entry in seed_db.ENTRIES]


def run(client, *, cache_dir=CACHE_DIR, refresh=False, workers=4):
    """-> {"jev": [scores per notch], "chat": [categories per notch]}, from the cache unless refreshed."""
    texts = [entry[1] for entry in seed_db.ENTRIES]
    calls = {
        "jev": lambda text: classify.classify(client, text, project_names=[])["category_scores"],
        "chat": lambda text: analysis.classify_by_chat(client, text)["categories"],
    }
    models = {"jev": config.JEV_MODEL, "chat": config.CHAT_MODEL}
    raw = {}
    for name, call in calls.items():
        path = os.path.join(cache_dir, f"categories-{name}.json")
        cached = None if refresh else _cached(path)
        if cached and cached["model"] == models[name] and len(cached["results"]) == len(texts):
            raw[name] = cached["results"]
            continue
        with ThreadPoolExecutor(workers) as pool:
            raw[name] = list(pool.map(call, texts))
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "w") as f:
            json.dump({"model": models[name], "results": raw[name]}, f, indent=1)
    return raw


def _cached(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def jev_categories(scores, thresholds=None):
    """classify.parse's rule over cached scores, optionally with a threshold per category."""
    thresholds = thresholds or dict.fromkeys(CATEGORIES, config.CATEGORY_THRESHOLD)
    return {c for c in CATEGORIES if scores[c] >= thresholds[c]} or {max(CATEGORIES, key=scores.get)}


def score(expected, predicted):
    """-> (exact-set matches, {category: (tp, fp, fn)})."""
    counts = {c: (sum(c in e and c in p for e, p in zip(expected, predicted)),
                  sum(c not in e and c in p for e, p in zip(expected, predicted)),
                  sum(c in e and c not in p for e, p in zip(expected, predicted))) for c in CATEGORIES}
    return sum(e == set(p) for e, p in zip(expected, predicted)), counts


def best_thresholds(expected, scores):
    """Per category, the threshold whose yes/no agrees with the labels most often; ties go nearest 0.5."""
    def agreement(c, t):
        return sum((s[c] >= t) == (c in e) for e, s in zip(expected, scores))

    return {c: max(THRESHOLDS, key=lambda t: (agreement(c, t), -abs(t - 0.5))) for c in CATEGORIES}


def report(raw):
    """The printed eval, as lines."""
    expected = labels()
    total = len(expected)
    jev = [jev_categories(s) for s in raw["jev"]]
    best = best_thresholds(expected, raw["jev"])
    tuned = [jev_categories(s, best) for s in raw["jev"]]
    rows = {f"jev {config.JEV_MODEL} at {config.CATEGORY_THRESHOLD}": score(expected, jev),
            f"chat {config.CHAT_MODEL}, v4 prompt": score(expected, raw["chat"])}
    lines = [f"Category eval · {total} seed_db notches · exact set match against the hand labels", ""]
    lines += [f"  {name:<52} {exact:>2}/{total} ({100 * exact // total}%)" for name, (exact, _) in rows.items()]
    lines += ["", f"  {'category':<15} {'jev tp fp fn':>14} {'chat tp fp fn':>15}   jev best threshold (in-sample)"]
    for c in CATEGORIES:
        (jt, jf, jn), (ct, cf, cn) = (counts[c] for _, counts in rows.values())
        lines.append(f"  {c:<15} {jt:>6} {jf:>3} {jn:>3} {ct:>7} {cf:>3} {cn:>3}   {best[c]:.2f}")
    exact, _ = score(expected, tuned)
    lines += ["", f"  jev at the best thresholds: {exact}/{total} ({100 * exact // total}%) — in-sample, so optimistic"]
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description="Score Jev and the chat model against the hand-labelled categories.")
    parser.add_argument("--refresh", action="store_true", help="call the models again instead of using the cache")
    parser.add_argument("--workers", type=int, default=4, help="parallel calls per classifier (default 4)")
    args = parser.parse_args(argv)
    try:
        raw = run(OpenRouterClient.from_env(), refresh=args.refresh, workers=args.workers)
    except (RuntimeError, ModelError) as exc:
        print(f"eval failed: {exc}", file=sys.stderr)
        return 1
    print("\n".join(report(raw)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
