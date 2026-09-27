# Getting fixed-tag accuracy from 42% to 73%

How the capture-time tagging prompt was improved, what each change was trying to
fix, and what actually happened. Written so the method is repeatable, not just
the result.

**Result: exact set match went 42.3% → 73.1% across five prompt versions. No
data, model, or schema changes — prompt and methodology only.**

| version | what it added | exact match |
| --- | --- | --- |
| `v0_bare` | the original prompt: names the five tags, defines none | 22/52 — **42.3%** |
| `v1_definitions` | a definition and test per tag | 25/52 — **48.1%** |
| `v2_cardinality` | boundaries rewritten from the v1 error list | 31/52 — **59.6%** |
| `v3_procedure` | the "aboutness" test to stop over-tagging | 34/52 — **65.4%** |
| `v4_stacking` | tags stack rather than compete; challenges survives a happy ending | 38/52 — **73.1%** |

Everything is reproducible:

```bash
python eval_tags.py --list                              # variants and cached runs
python eval_tags.py --run v4_stacking                   # re-run one (~52 API calls)
python eval_tags.py --show v4_stacking -v               # re-score from cache, free
python eval_tags.py --diff v3_procedure v4_stacking     # what moved between two
```

---

## The setup

**What's being measured.** The capture-time call in `tagger.py` labels one
journal entry with tags from a closed set of five: `wins`, `collaboration`,
`leadership`, `growth`, `challenges`. This eval only covers those. The
open-vocabulary auto tags are a separate problem, noted at the end.

**Ground truth** is the `tags` column in `notch.db`, hand-written in
`seed_db.py` — 52 entries, one fictional user, three months. It is one person's
labelling, not an oracle.

**The metric is exact set match**: the predicted tag set equals the seeded set,
no partial credit. This was chosen over per-tag precision/recall because
per-tag numbers hide the failure mode that matters. At v0, `collaboration` had
17 correct against 2 extra and 4 missed — respectable — while only 42% of
entries were labelled correctly overall, because a near-miss on a two-tag entry
books one hit and one miss and looks half-right in the aggregate. Per-tag counts
are still reported, but for diagnosis, not as the headline.

### Three methodology decisions that made the numbers readable

**1. `temperature=0`.** The first two measurements were taken at the API default
of 1.0, which meant re-running the same prompt gave a different score and a
two-point movement was unreadable. Setting temperature to 0 is also just correct
for a labelling task — you want the most likely label, not a sample. Measured
reproducibility afterwards: **two full runs of `v4_stacking` agreed on 51 of 52
entries** (scoring 37 and 38). So it is near-deterministic but not exactly so,
and single-entry movements should not be over-read.

**2. Every run is cached to `evals/<variant>.json`.** Predictions are saved with
the entry text, the seeded tags and the predicted tags. Diagnosis, re-scoring and
diffing then cost nothing, which is what makes it practical to actually read the
errors rather than just watch the top-line number.

**3. Only one section of the prompt changes between variants.** The auto tag,
impact-note, acknowledgement and project-match sections are held constant in
`prompt_variants.py` via a shared preamble and tail. Only the FIXED TAGS block
differs. Changing two things at once makes a score movement impossible to
attribute.

### Files

| file | role |
| --- | --- |
| `prompt_variants.py` | every prompt version tried, with a comment on what it was for |
| `eval_tags.py` | the harness: run, score, diff, cache |
| `tagger.py` | product code — imports the winning variant, never a pasted copy |

`tagger.py` sets `WINNING_VARIANT = "v4_stacking"` and does
`SYSTEM_PROMPT = VARIANTS[WINNING_VARIANT]`, so the shipped prompt cannot drift
from the measured one.

---

## v0 → v1: define the categories

**Baseline: 22/52 (42.3%).** The original prompt named the five tags and defined
none of them. Its only guidance was one line about not reaching for `wins` to be
encouraging.

| tag | correct | extra | missed |
| --- | --- | --- | --- |
| wins | 20 | 6 | 2 |
| collaboration | 17 | 2 | 4 |
| leadership | **1** | 0 | **11** |
| growth | 7 | 4 | 7 |
| challenges | 4 | 0 | 7 |

**The diagnostic that mattered wasn't in that table.** Printing the number of
tags per entry alongside ground truth showed:

```
tags per entry   1: 43 pred / 24 actual    2: 9 pred / 28 actual
```

The model was assigning one tag to 43 of 52 entries where the truth has two tags
on 28. It wasn't picking wrong categories so much as stopping after one. That
single line reframed the problem and is the reason the harness prints it on
every run.

**v1** gave each tag a definition and a test — `wins` requires a landed result,
`challenges` describes the shape of the work rather than the outcome, and so on.

**Result: 25/52 (48.1%).** The cardinality problem disappeared almost entirely
(24/26/2 predicted against 24/28/0 actual), which is where most of the gain came
from. But `leadership` swung from 1 correct/11 missed to **12 correct/13 extra** —
from never predicted to wildly over-predicted.

**The lesson, and it recurs below.** v1's leadership definition ended with "it is
the most under-applied tag — if an entry shows someone improving the team's floor
rather than closing their own ticket, it is leadership." That is a thumb on the
scale, not a boundary. Telling a model a tag is under-applied gets it
over-applied. Every later version states what a category *is* and what it
*excludes*, and never how often to reach for it.

## v1 → v2: derive the boundaries from the errors

Rather than guess, this step read all 27 v1 mismatches with their entry text
(`--show v1_definitions -v`) and wrote down the seeder's implicit policy:

- Catching a defect in someone's pull request → `collaboration` + `wins`, **not**
  leadership. This alone accounted for four errors.
- Unblocking or helping a teammate → `collaboration` alone.
- `growth` is far broader than "stated learning" — it covers being wrong, being
  surprised, discomfort, finally doing a thing you'd put off.
- `challenges` doesn't require failure. A flat entry about tedious groundwork
  counts.

**v2** rewrote the definitions around those rules and added an explicit
"what leadership is NOT" list naming code review, unblocking, and attending
meetings.

**Result: 31/52 (59.6%).** Leadership over-prediction collapsed from 13 extra to
2. Growth misses went 7 → 3, challenges 6 → 2.

**A note on process.** An earlier draft of v2 included "exactly one or two,
never three — most entries are one thing." That was written from the *v0*
diagnostic and was already stale: v1 had fixed cardinality, and the claim was
false anyway (28 of 52 entries carry two tags). It was cut before running.
Re-checking whether a problem still exists before writing a rule for it is worth
the thirty seconds.

## v2 → v3: the aboutness test

v2 had traded under-tagging for over-tagging — 37 two-tag predictions against 28
actual, with `challenges` now at 6 extra. Reading the misses showed a single
consistent cause: **passing remarks were being promoted into tags.**

- "Wrapped up the search latency work. p95 down from 1.9s to 640ms. Took longer
  than planned but the number is the number." → tagged `challenges` + `wins`,
  seeded `wins`. The entry is about the result; the delay is an aside.
- "Went through the on-call runbook and updated the four steps that were out of
  date. **Boring.**" → tagged `challenges` + `leadership`, seeded `leadership`.

**v3** added a general rule instead of patching each case:

> A tag must describe what the entry is MAINLY ABOUT, not something it mentions
> in passing. Ask: if I removed this aspect, would the entry still be the same
> story? If yes, do not tag it.

**Result: 34/52 (65.4%).**

## v3 → v4: tags stack, and challenges survives a happy ending

Two distinct leaks in the v3 misses, both over-corrections from earlier steps:

**1. Leadership was displacing collaboration.** v3's emphatic "none of these are
leadership" section was being read as *pick one*. Entries where someone helped a
colleague *and* did something unasked came back as leadership alone:

> "Ravi asked for help reading our event schema again. Instead of just answering,
> I wrote it up in the shared wiki so the next person doesn't have to ask."
> — seeded `collaboration` + `leadership`, predicted `leadership`

`collaboration` + `leadership` is the second most common pair in the data (6 of
52), so this was expensive. v4 states the structural fact directly: **each tag is
a separate yes/no question; answering yes to one never rules out another.**

**2. The aboutness test had over-suppressed `challenges`.** Incidents and on-call
surprises stopped being tagged once they were resolved, because the entry's
headline became the fix:

> "Prod incident — image uploads failing for about 40 minutes. Not our code, a
> dependency, but I was the one who noticed and paged the right people."
> — seeded `challenges` + `wins`, predicted `wins`

v4 carves out an explicit exception list — a production incident, an on-call
surprise, a bug whose cause wasn't obvious, work that overran because it was
tangled — while keeping the aboutness test for passing remarks.

v4 also softened v3's rule that hedged saves aren't wins, which had been killing
entries where a real artifact shipped and only the *benefit* was hedged ("set up
a codemod… probably saves everyone else a day each").

**Result: 38/52 (73.1%).** Target met.

| tag | correct | extra | missed |
| --- | --- | --- | --- |
| wins | 20 | 4 | 2 |
| collaboration | 20 | 2 | 1 |
| leadership | 12 | 1 | 0 |
| growth | 14 | 4 | 0 |
| challenges | 10 | 2 | 1 |

Recall is now near-perfect across every tag — 4 missed labels in total, against
34 at baseline. What remains is mild over-application.

---

## Honest caveats

**This is an in-sample number.** Every variant was written after reading
mismatches from all 52 entries, so 73.1% measures fit to a set that was used for
development. The `--halves` check splits by entry id as a weak sanity test and
shows **61.5% on even ids against 84.6% on odd** — a 23-point spread on 26
entries each. That is around 2.5 standard deviations of binomial noise at this
sample size, so it is not damning, but it is not reassuring either. **A real
holdout needs entries the prompt author never read.** That is the first thing to
do before trusting this number.

**Improvements are not monotone.** `--diff v3_procedure v4_stacking` reports
*fixed: 10, newly wrong: 6* for a net gain of 4. Every version churns entries in
both directions. A prompt change that nets +4 is not "four things got better."

**Ground truth is one annotator.** Leadership-vs-collaboration is exactly the
boundary two reasonable people would draw differently. Some of the remaining 14
disagreements are defensible readings rather than errors, which means the
practical ceiling on this metric is below 100% and unknown.

**52 entries, one persona.** Jordan Kim is a software engineer. Nothing here
tests whether the categories or the prompt survive a designer, a PM, or a
manager — and `leadership` in particular is likely to mean something different
for each.

**Cost went up.** Input tokens per full run rose from 82.5k at v0 to 126.8k at
v4, roughly +54%, because the prompt is much longer. The system prompt is static,
so prompt caching should absorb nearly all of that in production; only the
per-call entry text and vocabulary vary.

---

## What worked, generally

1. **Print the shape of the error, not just the rate.** The tags-per-entry line
   was worth more than the whole per-tag table at v0.
2. **Read the mismatches with the source text.** Every version after v1 was
   written from a specific list of failing entries. None of the gains came from
   guessing at what a model might want to hear.
3. **State boundaries, never frequencies.** "This tag is under-applied" produced
   the single worst regression in the sequence. "This is what the tag excludes"
   fixed it.
4. **Fix causes, not cases.** The aboutness test replaced what would otherwise
   have been five special-case rules, and generalised to entries it wasn't
   written for.
5. **Change one section at a time and cache every run.** Attribution is the whole
   value; without it you are just rewriting prompts and hoping.
6. **Check the problem still exists before writing the rule.** The cardinality
   rule drafted for v2 would have re-broken what v1 had already fixed.

## Next

- Generate held-out entries and re-measure. Nothing else should be trusted until
  this happens.
- The `growth` and `wins` boundaries produce most of the remaining error (4 extra
  each) and are the obvious next target.
- Have a second person label the same 52 entries to estimate the inter-annotator
  ceiling. If two humans agree only 80% of the time on these categories, 73% is
  closer to done than it looks.
- Re-examine whether five tags are the right five. `leadership` needed the most
  prompt text by far to pin down, which is usually a sign the category itself is
  doing too much work.

---

## Appendix: auto tags

Fixed tags were the focus here, but the open-vocabulary auto tags got one change
in the same pass — each call is now shown the keywords already in use for that
user and told to reuse before coining. Measured over the same 52 entries:

| | before | after |
| --- | --- | --- |
| distinct keywords | 122 | **78** |
| appearing exactly once | 104 (85%) | **47 (60%)** |

Near-synonym pairs (`migration docs` / `migration documentation`,
`knowledge sharing` / `knowledge transfer`) largely collapsed. This has no
formal eval yet and needs its own metric — the honest one is probably search
recall, not vocabulary size.
