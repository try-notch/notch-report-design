# Notch

**An AI-powered workplace performance tracker built for the employee, not the employer.**

---

## Related docs

| Doc | What it covers |
| --- | --- |
| **Notch.md** (this file) | What the product is, who it's for, how it behaves |
| [Backend.md](Backend.md) | The system requirements — the spec this demo and the real backend are both built against |
| [README.md](README.md) | The report-generation demo in this repo: what runs today, and how |

`Backend.md` is the source of truth for scope. If this doc and `Backend.md`
disagree about what the system does, `Backend.md` wins.

---

## Summary

Most tools in this space are sold to managers. Notch isn't. It exists so that
when review time rolls around, the employee is the one holding the record —
whether they're arguing for a raise, a promotion, or a better job somewhere
else.

The ask is small: talk to Notch for two minutes at the end of your workday. In
a week you have a clearer picture of what you actually did. In a month you're
further along than you'd be from memory. In a year you have every piece of
impact and improvement already written down, organized, and ready to hand to
someone.

The premise is that people are bad at remembering their own work and worse at
writing it up. Notch moves that cost from *later, all at once, under pressure*
to *two minutes a day, when it's fresh*.

## Who it's for

Someone with a job they're trying to grow in, who:

- can't recall in November what they shipped in March
- does real work that never lands in a ticket — unblocking a teammate, fixing
  the runbook, catching a bad design before it shipped
- freezes at the self-review form
- wants leverage in a conversation about money or title

Notch stores job title, career level, and career goals (see
[Backend.md](Backend.md#user-info)) because a report for someone targeting a
staff role should not read like a report for someone targeting their first
promotion.

---

## How it works

There are two separate moments where AI is involved, not one. The first is
**when you speak** — the entry gets read and tagged. The second is **when you
ask for a report**, days or weeks later — a completely different call, doing a
completely different job. Keeping these two straight is the main thing to
explain to someone seeing Notch for the first time.

### The moment you talk to Notch

**You speak.** Two to five minutes about your day, naturally. No typing, no
forms.

**Speech becomes text.** A transcription tool — not the model that does the
"thinking," just speech-to-text — converts it into plain text, which goes
straight into storage exactly as spoken. That's the rawest, most unprocessed
version of what you said, and Notch keeps it permanently. Everything else is
derived from it.

**A tagging pass reads that text.** A small, cheap model call pulls out the
structured pieces:

- **Tags** — two layers. A fixed set of five (a win, collaboration, leadership,
  growth, a challenge), which is what every chart and percentage is built on;
  and open-ended auto tags pulled from what you actually said — `flaky tests`,
  `oncall`, `mentoring`. The fixed five make comparison possible; the auto tags
  catch what five buckets can't hold
- **Impact note** — if you mentioned a concrete result (a number, an outcome,
  a "this got used by X"), it becomes its own short note
- **Acknowledged by** — if you mentioned someone recognizing or praising the
  work. This is what *you* said out loud; Notch isn't verifying it happened,
  it's capturing what you remember being told
- **Project match** — given your currently active projects, the model tries to
  match the entry to one ("the refactor" → "Front-End Refactor")

**You confirm or correct the project match.** The model's guess is not final.
You see what the entry got matched to and can change it, or mark it as tied to
no project at all. A person stays in control of their own record rather than
trusting a guess blindly. If there was no confident match, the entry just stays
unassigned — that's fine, it still appears in daily and tag-based reports, just
not in a project-specific one.

**Everything gets saved.** Raw text, tags, impact note, acknowledgement,
project link — all filled in before you ever ask for a report.

### The moment you ask for a report

**You pick a report.** A period, a project, or a tag.

**Notch pulls the matching entries.** Just a lookup. No AI yet, because the
tagging already happened when the entry was created.

**Notch does the math itself.** Counts, percentages, week-over-week shifts —
plain arithmetic over rows that already exist. No model involved.

**One call writes the narrative.** A second, separate model call does two
things: writes the report itself (highlights, strengths, reflection questions,
the closing frame), and — only where real judgment is needed — groups entries
into meaningful sub-patterns, like splitting a pile of "collaboration" entries
into "unblocking teammates," "cross-team work," and "catching issues early."

**Charts are drawn from the numbers, not by the model.** Deterministic
rendering, so a chart is guaranteed to match the data rather than approximately
right.

**You get one finished PDF.** Narrative and charts already combined, already
formatted. Nothing to assemble, no dashboard to click into.

### Why it feels effortless

The whole thing only feels effortless because the hard work — transcribing,
tagging, matching — happens quietly right after you talk, not later, and not by
asking you to fill anything in yourself.

---

## Design principles

These are commitments, not implementation details. They're the reasons to trust
the output.

**The model reads and writes. Code counts. A chart library draws.** No
percentage, tally, or chart in a Notch report was produced by a language model.
Arithmetic over rows we already have is free, instant, and exact — there's no
reason to ask a model for it, and every reason not to.

**The model does exercise judgment, and we're specific about where.** It
decides which entries become highlights, which count as work that usually goes
unnoticed, and how a strength gets phrased. That editorial judgment is the
product. What it never does is compute or draw. Both halves of that boundary
matter.

**Growth is framed as building on a strength, never as a weakness.** This is a
document you take into a room to argue for yourself. It is not a performance
improvement plan. Enforced in the prompt and structurally — there is no field a
weaknesses list could go in.

**Your record is yours.** The database lives on your device. There is no
Notch-side copy of your entries, no employer-facing view, and export is
something you initiate. Entry text does go to a model API twice — once to tag
it, once to write a report — transiently, and it isn't retained there. See
[Backend.md](Backend.md#storage-security-and-search) for the full position,
including encryption at rest, which is the top open security item.

**Notch captures, it doesn't verify.** Recognition and impact are what you
said. Notch is a memory aid with structure, not a system of record for your
company.

---

## Features

**Capture**
- Speak to Notch any time — two to five minutes, transcribed to text
- Raw transcript kept permanently and untouched
- Automatic tagging — fixed tags plus open-ended keywords — with impact
  extraction and project matching, the project match confirmable by you

**Organize**
- Entries searchable and filterable by tag, keyword, project, and date
- Projects tracked with their own date ranges

**Report**
- Weekly snippets — tag breakdown, week-over-week shift, and gap identification
- Quarterly and annual reports
- Project and tag reports
- Reports tailored to job title and career goals
- PDF export

See [Backend.md](Backend.md#reports-and-output) for the authoritative list.

---

## Open questions

Not yet decided, and worth deciding before more gets built:

- **Auto tag quality.** The open vocabulary is only useful if it's consistent —
  `flaky tests` one week and `test flakiness` the next are the same idea and
  two different strings. Needs an eval before anything depends on it.
- **Encryption at rest.** The database is local but unencrypted. Given what's
  in it, this is the top security item.
- **Long-horizon reports.** A quarterly or annual report covers far more
  entries than a single model call can take. It probably needs a two-stage
  shape: summarize each period, then summarize the summaries.
- **Gap identification.** Telling someone what they *haven't* logged —
  "no leadership moment in five weeks, and you're targeting a senior role" — is
  a meaningfully different feature from summarizing what they did, and the one
  that makes Notch prescriptive rather than retrospective. Currently one
  parenthetical in Backend.md.
- **Signalling evidence strength.** Everything in a report is self-reported. A
  skeptical manager will push on exactly that. Should reports distinguish
  claims with a concrete number attached from ones that are pure recall?
