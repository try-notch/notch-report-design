#!/usr/bin/env python3
"""
eval_tags.py — the fixed-tag eval harness. Separate from tagger.py on purpose:
tagger.py is product code, this is the measuring instrument.

    python eval_tags.py --run v2_cardinality      # score one variant
    python eval_tags.py --run v2_cardinality -v   # ...and list every mismatch
    python eval_tags.py --show v2_cardinality     # re-score a cached run, free
    python eval_tags.py --diff v1_definitions v2_cardinality
    python eval_tags.py --list                    # what's been run

GROUND TRUTH
The `tags` column in notch.db, hand-written in seed_db.py. It is one person's
labelling, not an oracle — leadership-vs-collaboration is exactly the boundary
two annotators would disagree on. So a score here measures AGREEMENT WITH THE
SEEDER, and the last few points of disagreement may be the prompt being
defensibly different rather than wrong.

METRIC
Exact set match: the predicted tag set equals the seeded tag set. Deliberately
strict — per-tag precision and recall can both look healthy while most entries
are still labelled wrong, because a near-miss on a two-tag entry scores as one
hit and one miss. Per-tag counts are reported underneath for diagnosis, not as
the headline.

CONTROLS
- temperature=0 in tagger.tag_entry, so a re-run of the same variant gives the
  same answer and a 2-point movement means something.
- No auto tag vocabulary is passed during eval. Vocabulary is a per-run moving
  target; feeding it in would make the fixed-tag score depend on the order
  entries happened to be processed in.
- Every run is cached to evals/<variant>.json, so analysis and re-scoring cost
  nothing and results can be diffed after the fact.
"""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import anthropic

import db
import tagger
from llm import MissingAPIKeyError, load_api_key
from prompt_variants import VARIANTS, build_prompt

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

EVAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evals")

DIM, GREEN, RED, YELLOW, RESET = (
    "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m",
)


# ---------------------------------------------------------------------------
# Running a variant
# ---------------------------------------------------------------------------

def run_variant(name, entries, project_names, tags):
    """Tag every entry with one prompt variant. Returns a cacheable dict."""
    client = anthropic.Anthropic(api_key=load_api_key())
    system_prompt = build_prompt(tags, name)
    tokens_in = tokens_out = 0
    predictions = []

    def work(entry):
        # vocabulary deliberately omitted — see CONTROLS in the module docstring.
        return tagger.tag_entry(client, entry["raw_text"], project_names,
                                system_prompt=system_prompt, tags=tags)

    with ThreadPoolExecutor(max_workers=tagger.WORKERS) as pool:
        for entry, (result, usage) in zip(entries, pool.map(work, entries)):
            tokens_in += usage.input_tokens
            tokens_out += usage.output_tokens
            predictions.append({
                "id": entry["id"],
                "date": entry["date_display"],
                "raw_text": entry["raw_text"],
                "actual": sorted(entry["tags"]),
                "predicted": sorted(result["tags"]),
            })

    return {
        "variant": name,
        "model": tagger.MODEL,
        "n": len(predictions),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "predictions": predictions,
    }


def save(run):
    os.makedirs(EVAL_DIR, exist_ok=True)
    path = os.path.join(EVAL_DIR, f"{run['variant']}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(run, fh, indent=2)
    return path


def load(name):
    path = os.path.join(EVAL_DIR, f"{name}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"No cached run for '{name}'. Run it first:\n"
                                f"    python eval_tags.py --run {name}")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score(run):
    """Exact match plus per-tag tp/fp/fn and the predicted-cardinality spread."""
    names = db.get_tag_names()
    exact = 0
    per_tag = {tag: {"tp": 0, "fp": 0, "fn": 0} for tag in names}
    sizes = {}
    misses = []

    for p in run["predictions"]:
        predicted, actual = set(p["predicted"]), set(p["actual"])
        sizes[len(predicted)] = sizes.get(len(predicted), 0) + 1
        if predicted == actual:
            exact += 1
        else:
            misses.append(p)
        for tag in names:
            if tag in predicted and tag in actual:
                per_tag[tag]["tp"] += 1
            elif tag in predicted:
                per_tag[tag]["fp"] += 1
            elif tag in actual:
                per_tag[tag]["fn"] += 1

    return {
        "exact": exact,
        "n": run["n"],
        "pct": round(100 * exact / run["n"], 1) if run["n"] else 0.0,
        "per_tag": per_tag,
        "sizes": sizes,
        "misses": misses,
    }


def print_score(run, verbose=False):
    s = score(run)
    actual_sizes = {}
    for p in run["predictions"]:
        actual_sizes[len(p["actual"])] = actual_sizes.get(len(p["actual"]), 0) + 1

    print()
    print(f"  {run['variant']}  ·  {run['model']}")
    print("  " + "-" * 58)
    colour = GREEN if s["pct"] >= 70 else YELLOW if s["pct"] >= 55 else RED
    print(f"  Exact set match   {colour}{s['exact']}/{s['n']}  ({s['pct']}%){RESET}")
    print()
    print(f"  {'tag':<14} {'correct':>8} {'extra':>7} {'missed':>7}")
    for tag, t in s["per_tag"].items():
        mark = GREEN if t["fp"] + t["fn"] == 0 else ""
        print(f"  {mark}{tag:<14} {t['tp']:>8} {t['fp']:>7} {t['fn']:>7}{RESET}")
    print()
    print("  tags per entry   " + "  ".join(
        f"{n}: {s['sizes'].get(n, 0)} pred / {actual_sizes.get(n, 0)} actual"
        for n in sorted(set(s["sizes"]) | set(actual_sizes))
    ))
    print(f"  {DIM}{run['tokens_in']} in / {run['tokens_out']} out{RESET}")

    if verbose:
        print()
        print(f"  {len(s['misses'])} mismatches")
        print("  " + "-" * 58)
        for p in s["misses"]:
            over = set(p["predicted"]) - set(p["actual"])
            under = set(p["actual"]) - set(p["predicted"])
            delta = " ".join(
                [f"{GREEN}+{t}{RESET}" for t in sorted(over)]
                + [f"{RED}-{t}{RESET}" for t in sorted(under)]
            )
            print(f"  {DIM}[{p['date']}]{RESET} {delta}")
            print(f"      seeded: {', '.join(p['actual'])}")
            print(f"      pred:   {', '.join(p['predicted'])}")
            print(f"      {DIM}{p['raw_text'][:150]}{RESET}")
            print()
    print()


def print_halves(run):
    """
    Score each half of the entries separately, split by entry id parity.

    This is NOT a held-out test set — every variant here was written after
    reading mismatches from the whole set, so 'the score' is in-sample by
    construction. What this does check is whether the fit is EVEN. Rules that
    generalise should land within a few points across two arbitrary halves; a
    big gap would mean the prompt is carrying memorised special cases for
    particular entries rather than stating a policy. Weak evidence, honestly
    labelled — a real holdout needs entries the prompt author never read.
    """
    halves = {"even ids": [], "odd ids": []}
    for p in run["predictions"]:
        halves["even ids" if p["id"] % 2 == 0 else "odd ids"].append(p)

    print()
    print(f"  {run['variant']} — split consistency {DIM}(not a holdout){RESET}")
    print("  " + "-" * 58)
    for name, group in halves.items():
        hit = sum(1 for p in group if set(p["predicted"]) == set(p["actual"]))
        pct = round(100 * hit / len(group), 1) if group else 0.0
        print(f"  {name:<10} {hit:>3}/{len(group):<3} ({pct}%)")
    print()


def print_diff(a, b):
    """Side by side, plus which individual entries flipped."""
    sa, sb = score(a), score(b)
    delta = sb["pct"] - sa["pct"]
    arrow = GREEN + f"+{delta:.1f}" if delta > 0 else RED + f"{delta:.1f}"

    print()
    print(f"  {a['variant']}  →  {b['variant']}")
    print("  " + "-" * 58)
    print(f"  Exact match   {sa['exact']}/{sa['n']} ({sa['pct']}%)   →   "
          f"{sb['exact']}/{sb['n']} ({sb['pct']}%)   {arrow}{RESET}")
    print()
    print(f"  {'tag':<14} {'correct':>16} {'extra':>14} {'missed':>14}")
    for tag in db.get_tag_names():
        ta, tb = sa["per_tag"][tag], sb["per_tag"][tag]
        print(f"  {tag:<14} "
              f"{ta['tp']:>7} → {tb['tp']:<6} "
              f"{ta['fp']:>6} → {tb['fp']:<6} "
              f"{ta['fn']:>6} → {tb['fn']:<6}")

    before = {p["id"]: set(p["predicted"]) == set(p["actual"]) for p in a["predictions"]}
    after = {p["id"]: set(p["predicted"]) == set(p["actual"]) for p in b["predictions"]}
    fixed = [i for i in before if not before[i] and after.get(i)]
    broke = [i for i in before if before[i] and not after.get(i, True)]
    print()
    print(f"  {GREEN}fixed: {len(fixed)}{RESET}   {RED}newly wrong: {len(broke)}{RESET}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Fixed-tag prompt eval.")
    parser.add_argument("--run", metavar="VARIANT", help="Run a variant and cache it.")
    parser.add_argument("--show", metavar="VARIANT", help="Score a cached run.")
    parser.add_argument("--diff", nargs=2, metavar=("A", "B"), help="Compare two runs.")
    parser.add_argument("--list", action="store_true", help="List variants and cached runs.")
    parser.add_argument("--halves", metavar="VARIANT",
                        help="Split-consistency check on a cached run.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show every mismatch.")
    args = parser.parse_args()

    if args.list:
        cached = {f[:-5] for f in os.listdir(EVAL_DIR)} if os.path.isdir(EVAL_DIR) else set()
        print()
        for name in VARIANTS:
            mark = f"{GREEN}cached{RESET}" if name in cached else f"{DIM}not run{RESET}"
            print(f"  {name:<20} {mark}")
        print()
        return 0

    try:
        if args.run:
            if args.run not in VARIANTS:
                print(f"\n  {RED}✗{RESET}  Unknown variant '{args.run}'. "
                      f"Known: {', '.join(VARIANTS)}\n")
                return 1
            entries = db.get_all_entries()
            project_names = db.list_project_names()
            tags = db.get_tags()
            load_api_key()
            print(f"\n  Running {args.run} over {len(entries)} entries…")
            run = run_variant(args.run, entries, project_names, tags)
            save(run)
            print_score(run, args.verbose)
        elif args.show:
            print_score(load(args.show), args.verbose)
        elif args.halves:
            print_halves(load(args.halves))
        elif args.diff:
            print_diff(load(args.diff[0]), load(args.diff[1]))
        else:
            parser.print_help()
    except (db.NoDatabaseError, MissingAPIKeyError, FileNotFoundError) as exc:
        print(f"\n  {RED}✗{RESET}  {exc}\n")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
