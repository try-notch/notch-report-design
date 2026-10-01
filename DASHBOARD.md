# notch_dash: watching the Notch stack live

This is one local page that shows the whole path a notch travels, as it happens:

**iPhone** → **Cloudflare quick tunnel** → **Caddy gate** (`notch-gate.localhost`) → **notch_api** (127.0.0.1:4131) → **worker** → **OpenRouter** → **SQLite + audio**

- **What it answers:** is anything broken right now, and where. A notch that fails or gets stuck shows up in the section it affects.
- **What it records:** nothing of its own. It only reads, and every source is optional. A missing source shows as "Not set up" or "Can’t reach it". It is never a crash or a 500.
- **What it shows:** metadata only: states, timings, counts, sizes, costs and errors. It never shows notch content (transcripts, summaries, takeaways, tags, mood, report text) or the profile, because Notch's server is meant to keep no content at all (the local-first decision of 2026-09-25). `test_no_notch_content_or_profile_reaches_the_snapshot` puts a marker phrase in every content column and checks the snapshot never carries it.

The **Snapshot contract** section below is binding for both builders: the backend produces exactly that shape, and the page renders exactly that shape.

---

## Run it

Run it from the repo root. That can be the main checkout or this worktree; the command is the same in both.

```bash
NOTCH_DB=$HOME/Desktop/Notch/notch-report-design/data/phone.db \
NOTCH_AUDIO_DIR=$HOME/Desktop/Notch/notch-report-design/data/phone-audio \
NOTCH_DASH_SERVER_LOG=/path/to/phone-server.log \
NOTCH_DASH_TUNNEL_LOG=/path/to/tunnel.log \
NOTCH_DASH_GATE_SECRET_FILE=/path/to/gate-secret \
~/Desktop/Notch/notch-report-design/.venv/bin/python -m notch_dash
# → http://dash.notch.localhost   (Caddy → 127.0.0.1:4130)
```

- **Use absolute paths for `NOTCH_DB` and `NOTCH_AUDIO_DIR`.** The defaults come from `notch_api.config`, which resolves `data/` against the checkout that holds the code. In a worktree, that data isn't the phone's.
- **Listening:** uvicorn binds 127.0.0.1 only and answers only GET and HEAD. Caddy, in front of it, listens on every interface, so the dashboard also refuses what Caddy forwarded from another machine (see Security).
- **No new dependencies:** it uses fastapi, uvicorn and httpx from the existing venv.

## The usage page (the fleet)

`/usage` (and `/` on the VPS, where `NOTCH_DASH_HOME=usage`) is for the team: how Notch is used
across every account, from the /v2 server's meter database (`notch_dash/usage.py`, `GET /api/usage`).
Counts, timings, costs and codes only, the same rule as the rest of the page: the meter holds no
notch content, Notch Cloud's ciphertext is never read, and an account shows only as the first
eight characters of its Supabase id.

It was first drawn against a synthetic fleet. After the first week of real use (2 accounts, 5
notches) it was rebuilt around what small, real numbers need:

- **Needs a look:** the page's one banner, worded by the server (`attention`). It lists a
  zero-retention verdict of `miss` or `unknown`, a call past its deadline and still in flight, calls
  that failed or were turned away in the last 24 hours, spend at 80% of the daily cap, and the API
  not answering. A quiet page means none of those.
- **Last 24 hours:** accounts that made a call, notches, spend, and how many calls didn't go
  through. It is 24 hours on the clock, not the UTC day, which is empty every evening in America.
  An account here is one that made a call: `active_days` records a config fetch by day, with no time.
- **Last 7 days:** active accounts, notches, spend, and what a notch costs: its transcription and
  write-up, with reports priced apart.
- **Day by day** (UTC days, the meter's own): from the first day in the last 14 with any use to
  today, so a gap shows and two weeks of zeros don't.
- **Problems:** one table from three sources. *Failed*: the call started and didn't finish.
  *Refused*: the meter stopped it (a limit, a reused key). *Turned away*: it was answered before
  check-and-start (no token, an app too old, a switch that is off, unreadable audio), so it has no
  usage row and no account; the server counts those in `refusals` (see below). Requests to paths
  that don't exist are one count under the table, never rows.
- **Models:** one row a model, with the providers that served it and how often.
- **Accounts:** total, new this week, how many have ever made a notch; then each account active
  this week with its last sighting, build, notches, spend and today's notches against the cap.
- **App versions:** each account once, on the newest build it ran on its latest day.
- **Zero data retention:** the audit's verdicts as a state (confirmed, unverified, not zero
  retention), then by kind of call.
- **Server:** whether the API answers `/healthz` (asked every 15 s while the page is open), the
  config in force with its note and prompts, the limits and when the UTC day turns over in the
  viewer's zone, and OpenRouter's own count of the key's spend. The link to `/harness` shows only
  where that page has a source of its own; on the VPS it has none.
- **Speed and cost:** each kind of call by the prompt version that wrote it: calls, failed, the
  typical (median) and slowest time, the mean cost and tokens. A p95 shows from 20 calls.
- **Recent calls:** the last 30, one row each: when, the call, the account's prefix, its size, how
  long it took, cost, tokens, who served it, the prompt version, the build and the outcome. Never the
  Idempotency-Key or the body's HMAC.

**The answers the meter never saw.** `notch_api/refusals.py` counts every non-2xx answer that left
no usage row, under the hour, the route's template (or `unmatched`), the status and the code: no
user, no path, no header. `privacy.Guard` hands it each finished request; it counts in memory and
writes the batch to the meter's `refusals` table every five seconds and at shutdown, so a flood of
bad requests costs one small write, not one each. Rows older than 90 days are dropped.

**Formats on this page.** Money has four decimals under a dollar ("$0.0026": a notch costs a
quarter of a cent) and two from a dollar up; zero is "$0". Sums are rounded once, at the end.
Instants are the viewer's local time; "ago" is measured from the server's clock.

**The document** (`GET /api/usage`) carries `v` (2). The page shows "The dashboard was updated.
Reload the page." when it meets another version. A meter from before `refusals` existed is read as
having none.

A read is cached for ten seconds; the page refreshes every fifteen, and stops while its tab is hidden.

**Check it end to end:** `.venv/bin/python e2e/dash_usage.py` serves the API and the dashboard on
local ports with the fake models, plays the first week of real use and every kind of refusal, and
checks `/api/usage` and the rendered page (in headless Chrome, under the page's own
Content-Security-Policy) against what the phones were answered. It writes `report.html` with
screenshots to `e2e/runs/`; `--serve` leaves the page up, on that week's numbers, to look at.
`tests/test_dash_usage_e2e.py` runs its first pass inside the suite.

## Environment

**Turning a source off:** set any optional source to an empty string. The snapshot then reports it as `off`.

| Variable | Default | What it is |
|---|---|---|
| `NOTCH_DB` | `notch_api.config.DB_PATH` (`<repo>/data/notch_api.db`) | The SQLite file. It is opened read-only. |
| `NOTCH_AUDIO_DIR` | `notch_api.config.AUDIO_DIR` (`<repo>/data/audio`) | Stored uploads. The dashboard only checks their size and whether they exist. |
| `NOTCH_PORT` | `4131` | The notch_api port. The dashboard probes it on 127.0.0.1. |
| `NOTCH_METRICS` | `NOTCH_DB` without its extension, plus `-metrics.jsonl` (for example `data/phone-metrics.jsonl`) | The per-call model log. The server writes it and the dashboard reads it. |
| `NOTCH_DASH_PORT` | `4130` | The dashboard's own port (dash.notch.localhost). |
| `NOTCH_DASH_CADDY_LOG` | `/opt/homebrew/var/log/caddy-notch.access.log` | Caddy's JSON access log. Both notch hosts write to it. |
| `NOTCH_DASH_SERVER_LOG` | off | The notch_api stdout and stderr log, used for errors and as the model-call fallback. |
| `NOTCH_DASH_TUNNEL_LOG` | off | The cloudflared log, used for the public URL and WRN/ERR lines. |
| `NOTCH_DASH_TUNNEL_METRICS` | `127.0.0.1:20241` | The cloudflared metrics server (`/ready`, `/metrics`). |
| `NOTCH_DASH_GATE_SECRET_FILE` | off | A file holding the 48-lowercase-hex gate secret. It turns on the end-to-end probe. |
| `NOTCH_DASH_DEVICE` | unset (the device panel is off) | The phone's hardware UDID for `devicectl`. |
| `OPENROUTER_API_KEY` | read from `.env` (loaded when `notch_api.config` is imported) | Used only for `GET /api/v1/key` (spend). |
| `NOTCH_DASH_HOSTS` | none | Extra `Host` values to answer, comma-separated. On the VPS: the machine's tailnet name (`<machine>.<tailnet>.ts.net`), which `tailscale serve` passes through. |
| `NOTCH_DASH_TAILNET` | off | `1` trusts Tailscale's address ranges (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) in `X-Forwarded-For`, where `tailscale serve` names the tailnet device it was reached from. Only for a machine where nothing but `tailscale serve` can reach the dashboard (DEPLOY.md). |
| `NOTCH_DASH_METER_DB` | off | The /v2 server's meter database, opened read-only, for the usage page (`/usage`, and `/` when `NOTCH_DASH_HOME=usage`). |
| `NOTCH_DASH_HOME` | `harness` | What `/` shows: `harness`, this stack's health (also at `/harness`), or `usage`, the fleet's. |
| `NOTCH_DASH_BIND` | `127.0.0.1` | The interface uvicorn listens on. The VPS's container sets `0.0.0.0`; its port is still published on the host's 127.0.0.1 only. |
| `NOTCH_DASH_AUTH_PROXY` | off | `1` answers any client an authenticating proxy forwarded, skipping the this-machine check. Only where that proxy is the one way in: on the VPS, Caddy's `basic_auth` at `dash.trynotch.xyz` (DEPLOY.md › 6), set in the untracked `compose.override.yaml`. |

## Sources and cadence

The work splits into two paths:

- **Background:** threads keep every slow or remote source cached.
- **Request path:** a snapshot request only reads those caches and runs a few bounded DB queries, so it stays under 50 ms in the typical case.

A **Poller** is one daemon thread that calls one function on a fixed interval and stores `(value, read_at, error)`.

- **Failures keep the last good value.** When a read fails, the poller keeps the last good value and records the error.
- **The snapshot drops stale values.** It uses a value only while it is younger than that source's *max age*. After that, the value counts as missing.
- **"Watched only":** these pollers run only while someone is watching, meaning a snapshot was served in the last 60 s. The public probes use this so an idle dashboard doesn't knock on the tunnel all day. So does the local server probe: uvicorn writes an access line for every `/healthz`, and every 2 s that was about 43 000 lines a day in the server's unrotated log, which the dashboard itself tails. When a viewer returns, those pollers run again within about 1 s, so the first snapshot after an idle minute shows the server as "Not checked yet" (overall "Can’t tell yet") and the next one has it.
- **A source's `state` is about reading, not age:** it is `ok` while its last read worked, even when that value has since grown too old to use. So a probe that paused while nobody watched stays `ok` (with its old `read_at`) and only its value is dropped, instead of showing "Can’t reach it" on the first poll after an idle spell.

| Source | How | Every | Timeout | Max age | Kept |
|---|---|---|---|---|---|
| Caddy access log | tail | 2 s | – | – | 24 h of parsed requests, ≤ 20 000 each of passed, blocked and local |
| Metrics JSONL | tail | 2 s | – | – | 24 h of call events (≤ 20 000), indexed by `job_id` |
| Server log | tail | 2 s | – | – | 24 h of errors and of httpx model lines (≤ 5 000 of each), and the last "resumed" line |
| Tunnel log | tail | 5 s | – | – | the latest `https://*.trycloudflare.com`, and 24 h of WRN/ERR lines (≤ 5 000) |
| Local `GET 127.0.0.1:<NOTCH_PORT>/healthz` | poller, watched only | 2 s | 2 s | 10 s | the latest result, including failures |
| cloudflared `/ready` + `/metrics` | poller | 5 s | 2 s | 30 s | the last good read |
| Public end-to-end probe `GET https://<host>/<secret>/healthz` | poller, watched only | 15 s | 10 s | 60 s | the latest result |
| Gate integrity `GET https://<host>/healthz` and `/docs` (no secret) | poller, watched only | 60 s | 10 s | 5 min | the latest result |
| OpenRouter `GET /api/v1/key` | poller, watched only | 60 s | 10 s | 10 min | the last good read |
| `xcrun devicectl list devices --json-output <tmp>` | poller, watched only | 60 s | 15 s | 10 min | the last good read |
| Audio dir size (walk) | poller | 60 s | – | 10 min | total bytes and file count |
| SQLite | per request | each snapshot | `timeout=1` | – | nothing is cached |

**Tails**
- Each tail keeps its file open between ticks and reads what was appended since the last one.
- **Rolled** (the name now points at a new file): it first reads the old file to its end, then opens the new one from byte 0. So the lines Caddy wrote in the second or two before a roll aren't lost; one of them could be the only sign of a leak.
- **Truncated** (the file got shorter): it starts again from byte 0.
- On start it reads the whole current file and keeps only the last 24 h. Rotated backups are ignored.
- Each kind of item has its own bounded window, so a flood of one kind can't push out another. Caddy's blocked probes come from the internet at whatever rate a prober likes; in one shared window, 50 000 of them pushed the phone's requests out, and with them `phone_host`, the gate traffic and any leak in the last 10 min. A count such as `blocked_24h` stops at the window's size.
- The server and tunnel logs are bounded too. While the origin is unreachable, cloudflared writes one ERR line per incoming request, so a prober sets how fast that log grows; unbounded, 100 000 such lines made a snapshot take 350 ms. Error groups build their sample once per group, so a full window of one error costs about 3 ms.

**Public probes**
- They send `User-Agent: notch-dash/1 (<token>)`, where the token is 16 random hex characters drawn each run (`logs.OWN_UA`). Only the dashboard and Caddy's 0600 log know it.
- Their requests show up in Caddy's log. The dashboard leaves a line out of phone, traffic and gate counts, and counts it nowhere, only when all three hold:
  - the user agent is exactly this run's, or, for a line from before this run started, any `notch-dash/` one (an earlier run's probes, which carry another token or none),
  - the uri is one the probes ask (`/<gate>/healthz`, `/healthz`, `/docs`),
  - and it was blocked or went through the gate. A probe that got through without the secret is kept, so it counts toward `passed_without_secret_10m`.
- **Why a token:** anyone can send `notch-dash/1`. With a plain prefix match, a prober could hide from the Gate panel and a request that got through without the secret could hide from the leak alarm. Now a line sent after this run started can't be hidden at all, and one sent before it only when it is a blocked request to one of those three paths.
- **Why earlier runs count as ours:** without that, every restart turned the last run's probes (two to four a minute while watched) into blocked `/healthz` and `/docs` rows and `GET /healthz` gate traffic for a day. Live, that was 52 of 53 "blocked probes" and the busiest route.

**Tunnel host**
- The public probes send the secret to `host`, and whatever answers on the metrics port names it, so only a whole `<name>.trycloudflare.com` is taken from `userHostname`. Anything else is ignored and the tunnel log's address is used instead, the same rule the tunnel log already had.
- What this can't stop: if cloudflared isn't holding its metrics port, another local account could listen there and name a quick tunnel of its own. Keep cloudflared running while the dashboard is, or set `NOTCH_DASH_TUNNEL_METRICS` to empty and rely on the tunnel log.

**Gate secret file**
- It is read each time a probe runs. It must be exactly 48 lowercase hex characters after stripping whitespace; otherwise the source is `unreachable`.
- The secret lives only inside the probe function.

**SQLite access**
- The URI is built as `pathlib.Path(p).resolve().as_uri() + "?mode=ro"` with `uri=True, timeout=1`.
- The connection is opened, queried and closed within one request.
- Don't use `store.connect`, which runs PRAGMAs.
- Opening with `mode=ro` may create empty `-wal`/`-shm` sidecars when none exist. It never writes the database itself.

---

## Snapshot contract (binding)

`GET api/snapshot` → `200 application/json`. It always returns 200 while the process runs. A failing source changes what's inside the body, never the HTTP status.

### Conventions

- **Instants:** every instant is **epoch seconds, UTC, a JSON number**, named `*_at` (or `at`). DB instants are whole seconds. Log and probe instants have up to 3 decimals.
- **Durations:** every duration is **milliseconds, a number rounded to 0.1**, named `*_ms`. Recording length is `recording_ms` too.
- **Other units:**
  - Money is US dollars, named `*_usd`.
  - Sizes are integer bytes, named `*_bytes`.
  - `*_10m`, `*_1h` and `*_24h` are integer counts over the trailing window that ends at `generated_at`.
- **Check status:** every check carries `status`, `word` and `reason`.
  - `status` ∈ `ok | warn | critical | unknown`.
  - `word` is the short pill label, chosen by the backend.
  - `reason` is one calm sentence.
  - Reasons never contain clock times; they say "3 min ago" instead.
  - The page shows `word` and `reason` as given.
- **Source status:** every source carries `state` ∈ `ok | off | unreachable`.
  - `off` means not configured.
  - `unreachable` means configured but not readable or not answering.
- **Nulls:** a field is `null` exactly when the table under the example says so. Keys are never omitted: every key in the example is present in every snapshot.
- **Lists:** lists are `[]` when empty. They are never null unless marked.
- **Strings from outside:** every string that came from a log, a header, a path or an error is redacted and truncated before it reaches the snapshot (see Security).
- **Percentiles:** p50 and p95 use nearest rank on the window's values.

### Example

This example is illustrative. It shows one notch in flight, one written and one that failed. [`tests/dash_sample_snapshot.json`](tests/dash_sample_snapshot.json) is this exact document with the comments removed. The page renders against that file, and a backend test checks that a live snapshot has the same shape.

```jsonc
{
  "v": 1,  // contract version; the page asks for a reload on any other value
  "generated_at": 1790368700.0,  // when this snapshot was built
  "took_ms": 6.2,  // time spent building it
  "overall": {
    "status": "warn",  // derived from checks (Status rules › Overall)
    "word": "Worth a look",  // All fine | Worth a look | Needs you now | Can’t tell yet
    "reason": "1 notch couldn’t be written up in the last hour.",
    "problems": [  // every warn/critical check: critical first, then strip order; [] when none
      {"check": "worker", "status": "warn", "word": "Failed", "reason": "1 notch couldn’t be written up in the last hour."}
    ]
  },
  "checks": {  // the status strip, in this order
    "phone": {
      "status": "ok",
      "word": "Fine",
      "reason": "Seen 10 s ago through the gate.",
      "last_seen_at": 1790368689.8,  // newest passed gate request with a "Notch/" user agent
      "last_route": "POST /v1/entries",  // that request, normalized
      "requests_1h": 9,  // phone requests in the last hour
      "device": {  // devicectl, matched on properties.hardware.udid
        "name": "Example iPhone",  // properties.state.name
        "model": "iPhone 17 Pro",  // properties.hardware.marketingName
        "os": "27.0",  // properties.software.osVersionNumber.stringValue
        "connection": "disconnected",  // properties.connection.state (devicectl's own link, not the network)
        "pairing": "paired",  // properties.connection.pairingState
        "transport": "localNetwork",  // properties.connection.transportType
        "last_connected_at": 1790368380.0,  // lastConnectionDate (seconds since 2001-01-01) + 978307200
        "read_at": 1790368661.0
      }
    },
    "tunnel": {
      "status": "ok",
      "word": "Fine",
      "reason": "1 connection via ewr14 · answered end to end in 212 ms.",
      "ready_connections": 1,  // /ready readyConnections
      "edge": "ewr14",  // cloudflared_tunnel_server_locations{edge_location}
      "rtt_ms": 18.0,  // quic_client_smoothed_rtt
      "version": "2026.9.3",  // build_info{version}
      "requests_total": 53,  // cloudflared_tunnel_total_requests since cloudflared started
      "request_errors": 0,  // cloudflared_tunnel_request_errors
      "host": "sample-quick-tunnel.trycloudflare.com",  // live public host, no scheme: user_hostnames_counts, else the tunnel log
      "phone_host": "sample-quick-tunnel.trycloudflare.com",  // X-Forwarded-Host of the phone's newest request, however old
      "probe": {  // GET https://<host>/<secret>/healthz
        "at": 1790368690.2,
        "ok": true,  // 200 and body {"ok": true}
        "http_status": 200,  // null when nothing answered
        "latency_ms": 212.4,  // null when nothing answered
        "error": null  // "timed out after 10 s" | "couldn’t connect" | "HTTP 502"; null when ok
      },
      "read_at": 1790368699.1  // last good cloudflared metrics read
    },
    "gate": {
      "status": "ok",
      "word": "Fine",
      "reason": "/healthz and /docs stay closed without the secret.",
      "integrity": {  // GET https://<host>/healthz and /docs, no secret
        "at": 1790368655.0,
        "healthz": 404,  // HTTP status; null when that request got no answer
        "docs": 404,
        "open": false,  // true when either answered 2xx
        "error": null  // why a request got no answer; null otherwise
      },
      "blocked_1h": 1,  // gate-host requests Caddy refused itself
      "blocked_24h": 2,
      "last_blocked_at": 1790368684.99,
      "passed_without_secret_10m": 0  // gate-host requests outside /<gate>/ that reached the server
    },
    "server": {
      "status": "ok",
      "word": "Fine",
      "reason": "Answering in 3.8 ms.",
      "port": 4131,
      "at": 1790368699.4,  // latest local /healthz attempt
      "http_status": 200,
      "latency_ms": 3.8,
      "error": null,  // "couldn’t connect" | "timed out after 2 s" | "HTTP 500" | "unexpected body"
      "started_at": 1790365371.0  // last "resumed N unfinished job(s)" line in the server log
    },
    "worker": {
      "status": "warn",
      "word": "Failed",
      "reason": "1 notch couldn’t be written up in the last hour.",
      "busy": true,  // some capture or report job is unfinished
      "captures": {"queued": 0, "transcribing": 0, "analyzing": 1},  // unfinished capture jobs by state
      "reports": {"queued": 0, "counting": 0, "writing": 0},  // unfinished report jobs by state
      "oldest_pending_ms": 10000.0,  // now − submitted_at of the oldest unfinished job of either kind
      "stuck": 0,  // unfinished jobs older than 120 s
      "done_24h": 2,  // capture + report jobs completed in 24 h
      "failed_24h": 1,  // capture + report jobs failed in 24 h
      "failed_1h": 1
    },
    "openrouter": {
      "status": "ok",
      "word": "Fine",
      "reason": "$0.04 today · $0.24 this month · $49.76 left of $50.",
      "limit_usd": 50.0,  // null when the key has no limit
      "remaining_usd": 49.76,  // null when the key has no limit
      "today_usd": 0.0444,  // usage_daily (OpenRouter's UTC day)
      "week_usd": 0.2435,  // usage_weekly
      "month_usd": 0.2435,  // usage_monthly
      "total_usd": 0.2435,  // usage
      "free_tier": false,  // is_free_tier
      "read_at": 1790368661.3
    }
  },
  "pipeline": {
    "counts_24h": {"notches": 3, "complete": 1, "failed": 1, "in_flight": 1},  // in_flight = unfinished now, any age
    "rows": [  // up to 20 notches, newest submitted first
      {
        "entry_id": "C7E4A1B9-2F3D-4B8E-9A61-3D5E7F9A1B2C",
        "job_id": "3b9d2c1e-5f7a-4e8b-9c0d-1a2b3c4d5e6f",  // null for an entry with no capture job
        "state": "analyzing",  // queued | transcribing | analyzing | complete | failed
        "recorded_at": 1790368682.0,
        "submitted_at": 1790368690.0,
        "finished_at": null,  // null until complete or failed
        "elapsed_ms": 10000.0,  // (finished_at, else now) − submitted_at; grows each poll while in flight
        "attempts": 1,
        "failure_code": null,  // model_unavailable | model_refused | transcription_failed | audio_unreadable
        "note": null,  // one sentence for a failed row
        "recording_ms": 31240.0,
        "words": 58,  // null until transcribed (raw_text is NULL)
        "audio_on_disk": true,  // every audio segment's file exists
        "phase_source": "metrics",  // metrics | db
        "phases": [  // in time order; a phase that never started is left out
          {"name": "wait", "state": "done", "start_ms": 0.0, "ms": 0.0, "calls": null, "failed_calls": null},
          {"name": "stt", "state": "done", "start_ms": 410.0, "ms": 1830.0, "calls": 1, "failed_calls": 0},
          {"name": "classify", "state": "done", "start_ms": 2350.0, "ms": 190.0, "calls": 1, "failed_calls": 0},
          {"name": "chat", "state": "running", "start_ms": 2350.0, "ms": 7650.0, "calls": 0, "failed_calls": 0}
        ]
      },
      {
        "entry_id": "5B2F0285-4E38-4DA8-926A-28B4184D7D79",
        "job_id": "f8af4772-addd-4e68-bffe-f28d946e3dee",
        "state": "complete",
        "recorded_at": 1790368427.0,
        "submitted_at": 1790368440.0,
        "finished_at": 1790368442.0,
        "elapsed_ms": 2000.0,
        "attempts": 1,
        "failure_code": null,
        "note": null,
        "recording_ms": 11569.6,
        "words": 27,
        "audio_on_disk": true,
        "phase_source": "metrics",
        "phases": [
          {"name": "wait", "state": "done", "start_ms": 0.0, "ms": 0.0, "calls": null, "failed_calls": null},
          {"name": "stt", "state": "done", "start_ms": 650.0, "ms": 180.0, "calls": 1, "failed_calls": 0},
          {"name": "classify", "state": "done", "start_ms": 840.0, "ms": 175.0, "calls": 1, "failed_calls": 0},
          {"name": "chat", "state": "done", "start_ms": 840.0, "ms": 706.0, "calls": 1, "failed_calls": 0}
        ]
      },
      {
        "entry_id": "9C1D7E3A-6B2F-4A8C-B5D4-0E9F8A7B6C5D",
        "job_id": "a1c3e5f7-0b2d-4f6a-8c9e-7d5b3a1f2e4c",
        "state": "failed",
        "recorded_at": 1790366552.0,
        "submitted_at": 1790366560.0,
        "finished_at": 1790366578.0,
        "elapsed_ms": 18000.0,
        "attempts": 1,
        "failure_code": "model_unavailable",
        "note": "Couldn’t write this one. stt answered 502 after 5 tries. The audio is still here.",
        "recording_ms": 48210.0,
        "words": null,
        "audio_on_disk": true,
        "phase_source": "metrics",
        "phases": [
          {"name": "wait", "state": "done", "start_ms": 0.0, "ms": 0.0, "calls": null, "failed_calls": null},
          {"name": "stt", "state": "failed", "start_ms": 320.0, "ms": 17030.0, "calls": 5, "failed_calls": 5}
        ]
      }
    ]
  },
  "traffic": {  // phone + local requests; never blocked probes or this run's own probes
    "requests_1h": 11,
    "errors_1h": 0,  // status ≥ 400
    "by_source_1h": {"gate": 10, "local": 1},  // gate = passed notch-gate.localhost; local = api.notch.localhost
    "hour": {  // requests per minute, oldest first; the last bucket is the current minute
      "start_at": 1790365140.0,
      "step_s": 60,
      "by_class": {
        "2xx": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 0, 0, 0, 8],
        "3xx": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        "4xx": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        "5xx": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
      }
    },
    "routes": [  // last 24 h: top 12 by count, then by route
      {"route": "POST /v1/entries", "count": 3, "errors": 0, "p50_ms": 41.0, "p95_ms": 44.2, "last_at": 1790368689.8},
      {"route": "GET /healthz", "count": 2, "errors": 0, "p50_ms": 1.4, "p95_ms": 1.9, "last_at": 1790368685.11},
      {"route": "GET /v1/entries", "count": 1, "errors": 0, "p50_ms": 8.9, "p95_ms": 8.9, "last_at": 1790368685.4},
      {"route": "GET /v1/entries/{id}", "count": 1, "errors": 0, "p50_ms": 7.1, "p95_ms": 7.1, "last_at": 1790368443.0},
      {"route": "GET /v1/me", "count": 1, "errors": 0, "p50_ms": 5.8, "p95_ms": 5.8, "last_at": 1790368685.2},
      {"route": "GET /v1/projects", "count": 1, "errors": 0, "p50_ms": 6.4, "p95_ms": 6.4, "last_at": 1790368685.2},
      {"route": "GET /v1/reports", "count": 1, "errors": 0, "p50_ms": 8.0, "p95_ms": 8.0, "last_at": 1790368685.3},
      {"route": "GET /v1/stats", "count": 1, "errors": 0, "p50_ms": 8.4, "p95_ms": 8.4, "last_at": 1790368685.3}
    ]
  },
  "models": {  // always present
    "source": "metrics",  // metrics | server_log | null
    "note": null,  // why numbers are partial or missing; null when source is metrics
    "kinds": [  // last 24 h; stt, classify and chat always, tts only when it had calls; [] when source is null
      {"kind": "stt", "model": "openai/whisper-large-v3", "calls": 7, "failed": 5, "p50_ms": 405.0, "p95_ms": 1830.0, "prompt_tokens": null, "completion_tokens": null, "total_tokens": null, "cost_usd": 0.0003, "last_at": 1790368690.41},
      {"kind": "classify", "model": "typesafe/jev-1.13", "calls": 2, "failed": 0, "p50_ms": 175.0, "p95_ms": 190.0, "prompt_tokens": null, "completion_tokens": null, "total_tokens": null, "cost_usd": 0.00002, "last_at": 1790368692.35},
      {"kind": "chat", "model": "deepseek/deepseek-v4-pro-0813", "calls": 2, "failed": 0, "p50_ms": 706.0, "p95_ms": 1416.0, "prompt_tokens": 5430, "completion_tokens": 616, "total_tokens": 6046, "cost_usd": 0.0103, "last_at": 1790368663.21}
    ],
    "recent": [  // up to 20 HTTP attempts, newest first
      {"at": 1790368692.35, "kind": "classify", "model": "typesafe/jev-1.13", "tool": null, "status": 200, "ok": true, "latency_ms": 190.0, "attempt": 1, "job": "capture", "entry_id": "C7E4A1B9-2F3D-4B8E-9A61-3D5E7F9A1B2C", "total_tokens": null, "cost_usd": 0.00001},
      {"at": 1790368690.41, "kind": "stt", "model": "openai/whisper-large-v3", "tool": null, "status": 200, "ok": true, "latency_ms": 1830.0, "attempt": 1, "job": "capture", "entry_id": "C7E4A1B9-2F3D-4B8E-9A61-3D5E7F9A1B2C", "total_tokens": null, "cost_usd": 0.0002},
      {"at": 1790368663.21, "kind": "chat", "model": "deepseek/deepseek-v4-pro-0813", "tool": "write_report", "status": 200, "ok": true, "latency_ms": 1416.0, "attempt": 1, "job": "report", "entry_id": null, "total_tokens": 3522, "cost_usd": 0.0062},
      {"at": 1790368440.84, "kind": "chat", "model": "deepseek/deepseek-v4-pro-0813", "tool": "label_entry", "status": 200, "ok": true, "latency_ms": 706.0, "attempt": 1, "job": "capture", "entry_id": "5B2F0285-4E38-4DA8-926A-28B4184D7D79", "total_tokens": 2524, "cost_usd": 0.0041},
      {"at": 1790368440.84, "kind": "classify", "model": "typesafe/jev-1.13", "tool": null, "status": 200, "ok": true, "latency_ms": 175.0, "attempt": 1, "job": "capture", "entry_id": "5B2F0285-4E38-4DA8-926A-28B4184D7D79", "total_tokens": null, "cost_usd": 0.00001},
      {"at": 1790368440.65, "kind": "stt", "model": "openai/whisper-large-v3", "tool": null, "status": 200, "ok": true, "latency_ms": 180.0, "attempt": 1, "job": "capture", "entry_id": "5B2F0285-4E38-4DA8-926A-28B4184D7D79", "total_tokens": null, "cost_usd": 0.0001},
      {"at": 1790366576.935, "kind": "stt", "model": "openai/whisper-large-v3", "tool": null, "status": 502, "ok": false, "latency_ms": 415.0, "attempt": 5, "job": "capture", "entry_id": "9C1D7E3A-6B2F-4A8C-B5D4-0E9F8A7B6C5D", "total_tokens": null, "cost_usd": null},
      {"at": 1790366568.525, "kind": "stt", "model": "openai/whisper-large-v3", "tool": null, "status": 502, "ok": false, "latency_ms": 410.0, "attempt": 4, "job": "capture", "entry_id": "9C1D7E3A-6B2F-4A8C-B5D4-0E9F8A7B6C5D", "total_tokens": null, "cost_usd": null},
      {"at": 1790366564.12, "kind": "stt", "model": "openai/whisper-large-v3", "tool": null, "status": 502, "ok": false, "latency_ms": 405.0, "attempt": 3, "job": "capture", "entry_id": "9C1D7E3A-6B2F-4A8C-B5D4-0E9F8A7B6C5D", "total_tokens": null, "cost_usd": null},
      {"at": 1790366561.718, "kind": "stt", "model": "openai/whisper-large-v3", "tool": null, "status": 502, "ok": false, "latency_ms": 402.0, "attempt": 2, "job": "capture", "entry_id": "9C1D7E3A-6B2F-4A8C-B5D4-0E9F8A7B6C5D", "total_tokens": null, "cost_usd": null},
      {"at": 1790366560.32, "kind": "stt", "model": "openai/whisper-large-v3", "tool": null, "status": 502, "ok": false, "latency_ms": 398.0, "attempt": 1, "job": "capture", "entry_id": "9C1D7E3A-6B2F-4A8C-B5D4-0E9F8A7B6C5D", "total_tokens": null, "cost_usd": null}
    ]
  },
  "blocked": {  // what the gate refused
    "count_1h": 1,
    "count_24h": 2,
    "top_paths": [  // top 5 in 24 h, by count then path
      {"path": "/.env", "count": 1},
      {"path": "/wp-login.php", "count": 1}
    ],
    "recent": [  // up to 50, newest first
      {"at": 1790368684.99, "method": "GET", "path": "/wp-login.php", "status": 404, "country": "US", "user_agent": "curl/8.7.1", "client_ip": "203.0.113.7"},
      {"at": 1790341200.0, "method": "GET", "path": "/.env", "status": 404, "country": "DE", "user_agent": "Mozilla/5.0 zgrab/0.x", "client_ip": "198.51.100.23"}
    ]
  },
  "record": {
    "entries": {
      "total": 3,
      "last_7d": 3,  // recorded in the last 7 days
      "by_state": {"pending": 0, "transcribing": 0, "analyzing": 1, "complete": 1, "failed": 1}  // entries.analysis_state
    },
    "projects": 0,
    "reports": {"total": 1, "last_generated_at": 1790368663.0},
    "report_jobs": {"queued": 0, "counting": 0, "writing": 0, "complete": 1, "failed": 0},
    "audio": {
      "objects": 3,  // audio_objects rows
      "db_bytes": 1163414,  // SUM(byte_size)
      "disk_bytes": 1163414,  // walk of NOTCH_AUDIO_DIR
      "disk_files": 3,
      "past_retention": 0  // purge_after < now and purged_at IS NULL (nothing sweeps yet)
    }
  },
  "errors": {
    "server": [  // groups from the server log, last 24 h, newest last_at first, up to 20
      {"level": "ERROR", "where": "uvicorn.error", "message": "Exception in ASGI application", "exception": "sqlite3.OperationalError: database is locked", "count": 1, "first_at": 1790366101.4, "last_at": 1790366101.4, "sample": "Traceback (most recent call last):\n  File \"notch_api/app.py\", line 152, in db\n    yield conn\nsqlite3.OperationalError: database is locked"}
    ],
    "tunnel": [  // WRN/ERR groups from the tunnel log, same shape; where/exception are null
      {"level": "WRN", "where": null, "message": "Failed to refresh DNS local resolver", "exception": null, "count": 2, "first_at": 1790368300.0, "last_at": 1790368360.0, "sample": "2026-09-25T20:32:40Z WRN Failed to refresh DNS local resolver error=\"lookup region1.v2.argotunnel.com: no such host\""}
    ]
  },
  "sources": {  // one entry per source, in this order
    "db": {"state": "ok", "where": "phone.db", "read_at": 1790368700.0, "reason": null},
    "audio_dir": {"state": "ok", "where": "phone-audio", "read_at": 1790368661.0, "reason": null},
    "metrics": {"state": "ok", "where": "phone-metrics.jsonl", "read_at": 1790368699.0, "reason": null},
    "caddy_log": {"state": "ok", "where": "caddy-notch.access.log", "read_at": 1790368699.0, "reason": null},
    "server_log": {"state": "ok", "where": "phone-server.log", "read_at": 1790368699.0, "reason": null},
    "tunnel_log": {"state": "ok", "where": "tunnel.log", "read_at": 1790368696.0, "reason": null},
    "tunnel_metrics": {"state": "ok", "where": "127.0.0.1:20241", "read_at": 1790368699.1, "reason": null},
    "gate_secret": {"state": "ok", "where": "gate-secret", "read_at": 1790368690.0, "reason": null},
    "openrouter": {"state": "ok", "where": "openrouter.ai/api/v1/key", "read_at": 1790368661.3, "reason": null},
    "device": {"state": "ok", "where": "xcrun devicectl", "read_at": 1790368661.0, "reason": null}
  }
}
```

### Nullability

A source counts as *available* when its `state` is `ok` and it has a value younger than its max age.

| Field | Null when |
|---|---|
| `pipeline`, `record` | the DB is unavailable |
| `traffic`, `blocked` | the Caddy log is unavailable |
| `errors.server` / `errors.tunnel` | the server log / tunnel log is unavailable (`[]` means the source works and nothing went wrong) |
| `checks.phone.last_seen_at`, `last_route` | the Caddy log is unavailable, or the phone wasn't seen in 24 h |
| `checks.phone.requests_1h` | the Caddy log is unavailable |
| `checks.phone.device` | devicectl is off or unavailable, or the UDID isn't listed |
| `checks.tunnel.ready_connections` … `request_errors`, `read_at` | cloudflared metrics are unavailable (`read_at` is null only if it was never read) |
| `checks.tunnel.host` | neither the metrics nor the tunnel log gives a quick-tunnel host |
| `checks.tunnel.phone_host` | no phone request in the Caddy log the dashboard has read (the current file since it started, not the rolled ones), or the Caddy log is unavailable |
| `checks.tunnel.probe` | no secret, no host, not run yet, or older than 60 s |
| `checks.gate.integrity` | no host, not run yet, or older than 5 min |
| `checks.gate.blocked_*`, `passed_without_secret_10m` | the Caddy log is unavailable |
| `checks.gate.last_blocked_at` | same as above, or nothing blocked in 24 h |
| `checks.server.at`, `http_status`, `latency_ms`, `error` | before the first probe; `http_status` and `latency_ms` are also null when nothing answered; `error` is also null when ok |
| `checks.server.started_at` | no "resumed" line in the server log (or the log is off) |
| `checks.worker.captures`, `reports`, `stuck`, `done_24h`, `failed_24h`, `failed_1h` | the DB is unavailable |
| `checks.worker.oldest_pending_ms` | the DB is unavailable, or nothing is unfinished |
| `checks.openrouter.*_usd`, `free_tier`, `read_at` | no good read in 10 min (`limit_usd` and `remaining_usd` are also null for a key with no limit) |
| row `job_id` | the entry has no capture job; `state` then comes from `analysis_state`, with `pending` mapped to `queued` |
| row `finished_at` | unfinished |
| row `failure_code`, `note` | not failed |
| row `words` | not written yet |
| row `audio_on_disk` | the audio dir is unavailable |
| phase `calls`, `failed_calls` | `wait` and `run` phases |
| `models.source` | neither the metrics file nor the server log is available (then `kinds` and `recent` are `[]`) |
| `models.note` | `source` is `metrics` |
| kind `model` | no calls in the window, or the source is `server_log` |
| kind `p50_ms`, `p95_ms`, `cost_usd`, `*_tokens` | the source is `server_log`, or no call in the window reported it |
| kind `last_at` | no calls in the window |
| recent `model`, `tool`, `latency_ms`, `attempt`, `job`, `entry_id`, `total_tokens`, `cost_usd` | as above; `tool` for non-chat; `job`/`entry_id` outside a job or when the job row is gone |
| record `audio.disk_bytes`, `disk_files` | the audio dir is unavailable |
| record `reports.last_generated_at` | there are no reports |
| error group `exception` | the entry has no traceback (so always for `tunnel`) |
| error group `where` | always for `tunnel`: cloudflared's lines name no logger |
| every `sources.*.where` | the source is `off` |
| every `sources.*.read_at` | the source has never been read successfully |
| every `sources.*.reason` | the source's `state` is `ok` |

### Fields that need a rule

- **Phases** (`phase_source: "metrics"`):
  - `wait` runs from `submitted_at` to `started_at` (DB, 1 s resolution).
  - `stt`, `classify` and `chat` each span from their first event's `ts` to the end of their last event (`ts + latency_ms`). The span covers every attempt, including backoff sleeps. It is placed at `start_ms` = `ts − submitted_at`.
  - `classify` and `chat` run **in parallel** (analysis.py puts Jev on its own thread), so their spans overlap.
  - **In flight:** the phase matching the job's DB state is `running` from the end of the previous phase to now. That means `transcribing` → `stt`, and `analyzing` → whichever of `classify`/`chat` has no successful event yet.
  - **Failed:** a phase whose last attempt failed on a failed job is `failed`.
  - **No events** for a job (`phase_source: "db"`): the phases are `wait` plus one `run` phase, from `started_at` to `finished_at` or now.
  - **No capture job** (a seeded entry): `phase_source` is `db` and `phases` is `[]`; `submitted_at` is `entries.created_at`, and `finished_at` is `entries.updated_at` once the entry is complete or failed.
- **`counts_24h`:** `notches` are the entries submitted in 24 h (by the same `submitted_at` as the rows), `complete` and `failed` are those of them in that state, and `in_flight` is every unfinished capture job, any age.
- **Row `audio_on_disk`:** true only when the entry has at least one unpurged segment and every such file exists.
- **Row `note`:**
  - With events: "Couldn’t write this one. <kind> <answered 502 | timed out | couldn’t connect> after <n> tries."
  - Without events, one sentence per `failure_code`:
    - `model_unavailable`: "The model service didn’t answer."
    - `model_refused`: "The model service refused it."
    - `transcription_failed`: "No words came back from the recording."
    - `audio_unreadable`: "The recording couldn’t be read."
  - Then add " The audio is still here." only when `audio_on_disk` is true.
- **Traffic, routes and the gate** (from Caddy):
  - **Which requests passed:** a `notch-gate.localhost` request *passed* the gate when Caddy proxied it. Its `resp_headers` has `Via`, or it is a 502/503/504 under `/<gate>/`.
    - Everything else on that host is *blocked*. That includes wrong-secret requests, which Caddy's filter also logs as `/<gate>/…`.
  - **Gate leak:** a passed request whose uri is not under `/<gate>/` counts toward `passed_without_secret_10m`.
  - **Traffic:** traffic is passed gate requests plus every `api.notch.localhost` request. The dashboard's own probes are dropped everywhere, by the rule under Public probes.
  - **The phone:** a passed request whose user agent starts with `Notch/`.
  - **`phone_host`** is the `X-Forwarded-Host` of the newest phone request the Caddy reader has seen, kept past the 24 h window (`logs.CaddyLog`). After a quick tunnel restarts, the phone keeps calling the old address and none of those calls reach Caddy, so its last good request only gets older. Taken from the 24 h window, `phone_host` went null a day later and the tunnel went back to Fine while the phone build still pointed at a dead address.
  - **Route normalization:**
    1. Drop the `/<gate>` prefix and the query string.
    2. Replace a segment with `{id}` when it is a UUID, all digits, or 16 or more characters of `[0-9A-Fa-f-]`.
    3. Prefix the method: `GET /v1/entries/{id}`.
  - **Sizes and sources:**
    - Durations are Caddy `duration` × 1000.
    - A blocked path is the raw uri (redacted, ≤ 200 chars).
    - `country` is `Cf-Ipcountry` and `client_ip` is `Cf-Connecting-Ip`; each is null when absent. Caddy's own `client_ip` is always 127.0.0.1.
- **Models from the server log** (fallback):
  - Parse `YYYY-MM-DD HH:MM:SS,mmm INFO httpx: HTTP Request: POST <url> "HTTP/x <code> …"` into `{at, kind, status, ok: 2xx}`.
  - The timestamp is the machine's local time.
  - `kind` comes from the path:
    - `/audio/transcriptions` → stt
    - `/audio/speech` → tts
    - `/alpha/decisions` → classify
    - anything else → chat
  - `note`: "Counting from the server log until the metrics file appears."
  - With no source at all: "Not set up. Restart the server so it writes NOTCH_METRICS, or set NOTCH_DASH_SERVER_LOG."
- **Error groups:**
  - **Group key:** level + logger + message, with ids and numbers normalized (after redaction), + the exception type.
  - **Tracebacks:** a traceback belongs to the entry above it. `exception` is its last line.
  - **Lines without a timestamp** (uvicorn's `ERROR:    …`):
    - read while following the file (the tail has reached its end before), they take the time they were read, which is within one tick of when they were written;
    - read while catching up on what the file held when the dashboard opened it, they take the time of the previous timestamped line, or the time they were read when there is none.
    - Why: only model calls, startup and worker warnings write a timestamped line, so the last one can be hours old. A 500 happening now used to show as hours old, or vanish once that line was 24 h old.
  - **`sample`:** the newest occurrence, ≤ 40 lines and ≤ 4 KB.
  - **Tunnel log:** only lines of the form `<ISO>Z WRN|ERR <message> key=value…` are read. `message` drops the key=value fields.

### Shape test

`tests/test_dash_snapshot.py` builds a snapshot with every source faked as available. It asserts:

- The key sets are equal, recursively, to `dash_sample_snapshot.json`.
- For lists, each live item has the same keys as the sample's first item. (`hour.by_class` arrays have 60 numbers.)
- Values may be null only where the table above allows.

The test compares keys, not values or wording.

---

## Status rules

For each check, the backend evaluates the rules top to bottom, and the first match wins. Thresholds are constants at the top of `snapshot.py`. The reason texts below are the copy to use; `{…}` are filled in.

| Check | Rule | status | word | reason |
|---|---|---|---|---|
| server | no probe yet, or the latest is older than 10 s | unknown | Can’t reach it | "Not checked yet." |
| | the latest `/healthz` isn't 200 with `{"ok": true}` (refused, timed out after 2 s, other) | critical | Down | "The server didn’t answer on 127.0.0.1:{port}." |
| | latency > 500 ms | warn | Slow | "Answering, but slowly: {ms}." |
| | otherwise | ok | Fine | "Answering in {ms}." |
| tunnel | metrics available and `ready_connections == 0` | critical | Down | "cloudflared is running but has no connection to Cloudflare." |
| | probe present and not ok, and server isn't critical | critical | Down | "Your phone can’t get through: {error}." |
| | `host` and `phone_host` both known and different | warn | Moved | "The tunnel has a new address. Rebuild the app so your phone can find it." |
| | probe `latency_ms` > 2000 | warn | Slow | "Answering end to end, but slowly: {ms}." |
| | metrics `off` and no probe | unknown | Not set up | "Not set up. Set NOTCH_DASH_TUNNEL_METRICS to watch the tunnel." |
| | metrics unavailable and no probe | unknown | Can’t reach it | "Couldn’t read cloudflared at {where}. Is the tunnel running?" |
| | otherwise | ok | Fine | "{n} connection via {edge} · answered end to end in {ms}." (the probe clause is dropped without a probe, and when the probe failed because the server is down; with neither clause: "Reached the gate; the server behind it didn’t answer.") |
| gate | `integrity.open` | critical | Open | "The gate is open: {/healthz or /docs} answered without the secret." |
| | `passed_without_secret_10m` > 0 | critical | Open | "The gate let {n} request through without the secret in the last 10 min." |
| | no host | unknown | Not set up | "No tunnel address to check from outside." |
| | integrity null, or not both answered 404 | unknown | Can’t reach it | "Couldn’t check the gate from outside: {error, or "/docs answered 502"}." (a half-checked gate is not called closed) |
| | otherwise | ok | Fine | "/healthz and /docs stay closed without the secret." |
| worker | the DB is unavailable | unknown | Can’t reach it | "Couldn’t read {where}." |
| | a queued job is older than 120 s | warn | Stuck | "One notch has waited {age}. It’s saved; the worker hasn’t picked it up." (plural and report forms vary) |
| | a running job is older than 120 s | warn | Stuck | "One notch has been working for {age}. It’s saved." (a queued job's age runs from `submitted_at`, a running one's from `started_at`; report jobs keep no start, so theirs runs from `submitted_at`. `stuck` counts both kinds.) |
| | a job failed in the last hour | warn | Failed | "{n} notch couldn’t be written up in the last hour." / "…report…" |
| | busy | ok | Working | "{n} working now." |
| | otherwise | ok | Fine | "Nothing waiting · {n} done in 24 h." |
| openrouter | no key in the environment | unknown | Not set up | "Not set up. Add OPENROUTER_API_KEY to .env to see spend." |
| | no good read in 10 min | unknown | Can’t reach it | "Couldn’t read spend from OpenRouter: {error}." |
| | a limit is set and `remaining_usd` ≤ 0 | critical | Out | "No credit left. Model calls will be refused until it’s topped up." |
| | a limit is set and `remaining_usd` < 5 | warn | Low | "${x} left of ${limit}." |
| | otherwise | ok | Fine | "${today} today · ${month} this month · ${left} left of ${limit}." (the last clause becomes "no limit" when there is none) |
| phone | the Caddy log is `off` or unavailable | unknown | Not set up / Can’t reach it | the source's reason |
| | not seen through the gate in 24 h | unknown | Not seen | "No sign of it through the gate in the last 24 hours." |
| | otherwise | ok | Fine | "Seen {ago} ago through the gate." |

`busy` is true whenever any job is unfinished, whatever the status. The page shows in-progress styling only when `status` is ok and `busy` is true.

### Overall

1. If any check is **critical**, overall is `critical`, "Needs you now".
2. Otherwise, if any check is **warn**, overall is `warn`, "Worth a look".
3. Otherwise, if `server` or `worker` is **unknown**, overall is `unknown`, "Can’t tell yet". These two are the core; an unknown phone, tunnel, gate or openrouter check never changes the overall status.
4. Otherwise it is `ok`, "All fine".

**Reason:**
- warn or critical: the first problem's reason.
- unknown: the server's reason, or the worker's.
- ok: "Nothing needs you right now."

**`problems`:** every warn and critical check, critical first, then in strip order.

---

## Security

- **Network:**
  - It binds to 127.0.0.1 only.
  - **This Mac only, even through Caddy:** Caddy listens on `*:80`, and its `dash.notch.localhost` site has no `bind`, so any device on the same network can reach it by sending that Host to the Mac's LAN address. A request whose `X-Forwarded-For` holds anything but loopback addresses (or can't be read) gets a 403. Caddy sets that header to the address it was reached from and drops a client's own value, so it can't be forged from the network; a request straight to 127.0.0.1:4130 has none.
  - **Still to do outside this repo:** add `bind 127.0.0.1 [::1]` to the `http://dash.notch.localhost` site in the Caddyfile, and to `http://api.notch.localhost`, which serves the API to the network the same way.
  - **On the VPS** it is reached only through `tailscale serve` (DEPLOY.md), which forwards from the tailnet device's address and keeps the tailnet `Host`; `NOTCH_DASH_TAILNET=1` and `NOTCH_DASH_HOSTS` admit exactly those. Nothing else there listens for it: no Caddy site, and uvicorn binds 127.0.0.1.
  - **Host allow-list:** `dash.notch.localhost`, `127.0.0.1:<port>` and `localhost:<port>`. Any other Host gets a 421, which defends against DNS rebinding. The allowed hosts are a `Settings` field, so tests can add `testserver`, and a future mount under `/<gate>/dash/` would add `notch-gate.localhost`.
  - Only GET and HEAD are served.
  - `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)`.
- **Headers on every response:**
  - `Cache-Control: no-store`
  - `Referrer-Policy: no-referrer`
  - `X-Content-Type-Options: nosniff`
  - `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`
- **Never in any response, page or log:**
  - the gate secret
  - the OpenRouter key
  - bearer tokens or `Authorization` values
  - the key's `label`, `creator_user_id` or workspace ids
  - the device's `serialNumber`, `ecid` or `potentialHostnames`
  - request or response bodies, transcripts or prompts
- **The snapshot is an allow-list:** the snapshot builder writes new dicts field by field and never passes a source's raw JSON through.
- **`redact(s)`** runs on every string that came from a log, a header, a path or an exception:
  - 48 or more hex digits in a row → `<gate>`, where a digit may also be percent-escaped (`%30`–`%39`, `%41`–`%46`, `%61`–`%66`)
  - (the literal secret is never loaded outside the probe; a valid secret is exactly 48 lowercase hex, so the rule above already covers it)
  - **Why escapes and upper case:** Caddy's path matcher unescapes the path and ignores case, so `/%61bc…/healthz` and `/ABC…/healthz` both pass the gate. Its log filter, `^/[0-9a-f]{48}`, matches neither, so it logs them raw. Checked on a throwaway Caddy with a stand-in secret. Only a client that holds the secret sends these forms.
  - `Bearer\s+\S+` → `Bearer <redacted>`
  - `sk-or-[\w-]+` → `<key>`
  - Then it truncates: paths and user agents to 200 characters, messages to 300, samples to 4 KB.
- **Probe URLs contain the secret:**
  - `create_app` sets the `httpx` and `httpcore` loggers to WARNING, so every way of running it does. At INFO, httpx logs full request URLs.
  - The uvicorn access log is off.
  - Probe errors become a type or status ("timed out after 10 s", "HTTP 502"), never `str(exc)`.
- **In the page:** data reaches the DOM only through text nodes, numeric attributes, and `aria-label` strings built from counts and fixed words (the chart summary, glyph labels). There is never `innerHTML` with data.
  - Log text, paths, user agents and countries come from the internet.
  - JS lives only in `static/app.js`.
  - There are no inline `style` attributes. Sizes are set through CSSOM (`el.style.setProperty`), which the CSP allows.
  - Everything the page loads comes from the page itself: no CDN and no web fonts.

---

## Module layout

### `notch_dash/`, kept small and direct

| File | Job |
|---|---|
| `__init__.py` | A docstring pointing here. |
| `__main__.py` | Builds `Settings.from_env()` and runs `uvicorn.run(create_app(settings), host="127.0.0.1", port=settings.port, access_log=False)`. |
| `settings.py` | A frozen `Settings` dataclass and `from_env(environ)`. This is the only place that reads the environment. Defaults come from `notch_api.config`. |
| `live.py` | `Tail`: a file follower that keeps its file open, finishes a rolled file before opening the new one, handles truncation, and holds bounded 24 h deques behind a lock, one per kind of item. `Poller`: a daemon thread with an interval, a watched-only flag and `(value, read_at, error)`. Tests call `.tick()` / `.refresh()` directly, with no threads. |
| `logs.py` | Pure parsers for Caddy lines (classify passed/blocked/local/dash, normalize routes), server-log lines (httpx calls, error groups, the "resumed" line), tunnel-log lines (URL, WRN/ERR) and metrics lines. Also `redact`. |
| `probes.py` | Local `/healthz`; cloudflared `/ready` + `/metrics` (a Prometheus regex for only the lines above); the public end-to-end probe and gate integrity; OpenRouter `/key`; devicectl. Every function takes an injected `httpx.Client` or `run`. |
| `record.py` | Read-only SQLite: `connect_ro(path)` plus the bounded queries (the last 20 rows with audio presence, unfinished jobs by state and the oldest, 1 h/24 h done/failed, record counts, job_id → entry_id for the capture jobs updated in 24 h, the metrics window, ≤ 2000). One connection per snapshot. |
| `snapshot.py` | `build(src, db, now, started=None) -> dict`: the checks, the status rules and thresholds, overall, the panels, phases and notes. Pure: given the sources' cached values and the DB rows, it returns the contract. Each check is a public function (`server_check`, `tunnel_check`, `gate_check`, `worker_check`, `openrouter_check`, `phone_check`, `overall`) so the rules are tested directly. |
| `app.py` | `create_app(settings, *, http=None, run=subprocess.run, clock=time.time, start=True)`. It serves `GET /` (index.html), `GET /static/{app.js,style.css}` (an allow-list), `GET /api/snapshot`, the header middleware and the Host check. `Sources` wires every Tail and Poller, answers each `sources.*` entry, and reads the record once per snapshot (snapshots are built one at a time). The lifespan starts and stops the threads. |
| `static/index.html`, `static/app.js`, `static/style.css` | The page (see below). |
| `usage.py` | `read(path, now) -> dict`: the usage page's document, from the meter database opened read-only, cached for ten seconds. |
| `static/usage.html`, `static/usage.js` | The usage page. It shares `style.css`; its own rules sit under `.usage`, `.mini`, `.list` and `.pair`. |

**Tests:** `tests/test_dash_logs.py` (also `Tail` and `Poller`), `tests/test_dash_probes.py`, `tests/test_dash_snapshot.py`, `tests/test_dash_app.py` and `tests/test_metrics.py`.
- They use the `db_path` / `add_entry` / `capture` / `clock` fixtures from conftest.py.
- HTTP goes through `httpx.MockTransport`.
- devicectl uses a fake `run`.
- conftest.py is not edited.

### notch_api instrumentation

The server writes one JSON line per OpenRouter HTTP attempt to `NOTCH_METRICS`. It is **off unless `notch_api.__main__` turns it on**, so tests, `seed` and `eval` stay silent. It starts working the next time the phone server restarts; until then the dashboard falls back to the server log.

**Files the other workflow is editing:**
- Leave `analysis.py`, `reports.py`, `record_routes.py`, `store.py`, `web.py`, `account_routes.py`, `SERVER.md`, `e2e/run_e2e.py` and the existing tests alone.
- That is why tagging goes through a client proxy in worker.py. A contextvar set in `run()` would miss Jev's call on analysis.py's nested thread, and would leak between jobs on reused pool threads.

**`notch_api/metrics.py`** (new, about 40 lines)
- `path = None` (off).
- `job = ContextVar("notch_job", default=None)`.
- A module `threading.Lock`.
- `kind(url)`: `/audio/transcriptions` → stt, `/audio/speech` → tts, `/alpha/decisions` → classify, anything else → chat.
- `record(url, payload, attempt, wall, seconds, status, response)`:
  - It returns at once when `path is None`.
  - Everything else sits inside `try/except Exception: pass`.
  - It builds the event below.
  - With the lock held, it opens `path` in append mode, writes one line and closes it. Opening per write survives the file being deleted.
  - It never copies text from the payload or the response.

The event (every key is always present):

```json
{"ts": 1790368440.652, "kind": "stt", "model": "openai/whisper-large-v3", "tool": null, "status": 200, "ok": true, "latency_ms": 180.1, "attempt": 1, "job": "capture", "job_id": "f8af4772-addd-4e68-bffe-f28d946e3dee", "usage": {"cost": 0.0001}}
```

| Key | Value |
|---|---|
| `ts` | the attempt's start, `time.time()`, rounded to 3 decimals |
| `model` | `payload.get("model")` |
| `tool` | `payload["tool_choice"]["function"]["name"]` when present (`label_entry`, `write_report`, the takeaways tool), else null |
| `status` | the HTTP status as an int, `"timeout"` (`httpx.TimeoutException`), or `"error"` (any other exception) |
| `ok` | true when the status is 2xx and a JSON body has no `error` at the top level or in `choices[0]`; a raw audio 2xx is ok. A 2xx on a JSON call whose body isn't a JSON object is not ok: `_post` retries it. |
| `latency_ms` | the `perf_counter` span of the one HTTP attempt, rounded to 0.1. It excludes backoff sleeps. |
| `attempt` | 1-based within one `_post`. The tool_call re-ask is a second `_post`, so it starts again at 1. |
| `job`, `job_id` | from `metrics.job.get()`: `"capture"` or `"report"` plus the job id; null outside a job (for example the takeaways rewrite in record_routes). The dashboard joins `capture_jobs.id → entry_id`, so the event carries no entry id. |
| `usage` | 2xx JSON only: the numeric values of `prompt_tokens`, `completion_tokens`, `total_tokens`, `cost`, `input_tokens`, `output_tokens` and `seconds` when present; else null |

**Hook points**, as line numbers at `8b38c2c`:

| Where | Change |
|---|---|
| `notch_api/config.py:37` (after `AUDIO_DIR`) | `METRICS_PATH = os.environ.get("NOTCH_METRICS") or os.path.splitext(DB_PATH)[0] + "-metrics.jsonl"` |
| `notch_api/__main__.py:7-14` | Add `NOTCH_METRICS` to the env list in the docstring. |
| `notch_api/__main__.py:27` (`main()`) | `metrics.path = config.METRICS_PATH` (FakeClient never reaches `_post`, so fake mode records nothing). |
| `notch_api/openrouter.py:32-33` (beside the `.config` import) | `from . import metrics` |
| `notch_api/openrouter.py:182` | `response = self._http.post(url, json=payload, headers=self._headers)` → `response = self._send(url, payload, attempt)` |
| `notch_api/openrouter.py` after `_post` (≈ :206) | `_send(url, payload, attempt)`: take `time.time()` and `perf_counter()`, post, set `status` from the response, `"timeout"` on `httpx.TimeoutException`, else `"error"`, and re-raise unchanged; in `finally`, call `metrics.record(...)`. The retry, raise and return behaviour of `_post` is unchanged. |
| `notch_api/worker.py:22` | `from . import analysis, metrics, reports, store` |
| `notch_api/worker.py` before `JobRunner` (≈ :43) | `_JobClient(client, tags)`: `__getattr__` returns the attribute; a callable is wrapped so that it runs `token = metrics.job.set(tags)` … `finally: metrics.job.reset(token)`. The proxy object itself travels into analysis.py's nested pool, so Jev's call is tagged too. |
| `notch_api/worker.py:48-53` | `submit_capture` passes `client=_JobClient(self.client, {"job": "capture", "job_id": job_id})`; `submit_report` passes `{"job": "report", …}`. |

**Tests for the hook** (`tests/test_metrics.py`):
- One line per attempt, with the right `status` and `ok`, through `MockTransport`, for a retry sequence and a timeout.
- No payload or response text appears in the file.
- A recorder failure (an unwritable path) doesn't change what `_post` returns or raises.
- A job's calls are tagged, including the one made on a nested thread.
- Not written: "nothing is written while `path is None`". It would only restate the default, which the repo's test rules leave out; `record` returns before touching anything when `path` is None.

---

## The page

The page is one file set: `static/index.html`, `app.js` and `style.css`.

- **Relative URLs only:** it uses `api/snapshot` and `static/app.js`, so it works mounted under a prefix. The page must be served at a URL that ends in `/`: a future mount at `/<gate>/dash` redirects that to `/<gate>/dash/` in the proxy.
- **Polling:**
  - It fetches `api/snapshot` with `cache: "no-store"` and a 5 s `AbortController` timeout, through a `setTimeout` chain every 2 s. It never uses `setInterval`, so slow replies don't pile up.
  - It stops while `document.hidden` is true and polls at once when the tab becomes visible again.
- **Version check:** if `v !== 1`, it shows "The dashboard was updated. Reload the page."
- **Times:** they display in the browser's local time.

### Design tokens

- **Source:** use the Notch tokens from the design brief as written: the grounds, ink, honey, leaf, the semantic foreground/background pairs, the spacing, the layout roles, the radii and the elevation, all as CSS custom properties on `:root`.
  - Dark values go under `@media (prefers-color-scheme: dark)`. Grounds are warm espresso there, never grey.
  - There is no glass and no backdrop blur.
- **Type:**
  - System faces: `--font-ui`, `--font-display` (rounded), `--font-serif`, `--font-mono`.
  - Default body is subhead 15/20.
  - `font-variant-numeric: tabular-nums` on every number.
  - Mono only for measurements: times, durations, counts in tables, ms, routes, IPs, model ids and hosts.
- **Readable text:** text that must be read uses `--ink-700` or `--ink-900`. In light mode `--ink-500`, `--label2`, `--tint`, `--warning` and `--calm` fail 4.5:1, so they are only for glyphs, bars and decoration.
- **Motion:**
  - Settle: `cubic-bezier(.32,.72,0,1)` over 400 ms.
  - Press: dim to .55 over 120 ms.
  - In-progress: the leaf breathes 1 → .92 over 1.6 s, alternating.
  - Skeletons pulse `--fill-q` on the same 1.6 s cycle.
  - Under `prefers-reduced-motion`, everything holds still.
  - Never a spinner.

**Status glyphs.** The word is always shown. The label is always `--ink-900`. Hue colours only the glyph and the background.

| status | glyph | glyph colour | background |
|---|---|---|---|
| ok | filled circle | `--success` | `--success-bg` |
| warn | triangle | `--warning` | `--warning-bg` |
| critical | diamond | `--danger` | `--danger-bg` |
| unknown | hollow ring, 1.5 px `--ink-500` stroke | — | `--fill-q` |
| in progress (`status` ok and `busy`, and in-flight rows) | leaf (the `LeafGlyph` path), breathing | `--tint` light / `--accent` dark | `--progress-bg` |

The glyphs are inline SVG `<symbol>`s in index.html, used via `<use href="#g-ok">`.

**Pills:** a capsule with 4 px/10 px padding, an 8 px glyph, a 6 px gap and a 12/600 label.

### Layout

In DOM order, which is also the reading order at every width:

```
header (sticky)       overall glyph + word · reason · freshness line
notice                only when a check is critical
#status  "Status"     six checks
#notches "Notches"    the pipeline
.split
  .main   #traffic "Traffic", #models "Models"
  .side   #gate "The gate", #record "Your record"
#errors  "Errors"
#sources "Where this comes from"
```

**Desktop (≥ 1100 px)**
- A centred column, `max-width: 1180px`, with a 20 px gutter. Sections sit 34 px apart.
- `#status` is a 3 × 2 grid of tiles:
  - `--card`, `--r-inset` (18 px), `--shadow-ring`, 16 px padding.
  - The eyebrow label sits over a pill, the reason and one to three mono fact lines.
- `.split` is a two-column grid, `7fr 5fr`, with a 20 px gap. Each column stacks its two sections, so neither row forces the other's height.

**700–1099 px**
- One column; `.split` stacks.
- The status tiles are 2 × 3.

**Under 700 px** (the 375 px phone):
- One column with 16 px gutters.
- **Status:** `#status` becomes one grouped card of six rows (`GroupedCard`): 44 px minimum height, 11/16 padding, and `--sep` hairlines inset 16 px. Each row is a label with the pill on the right, and the reason under both.
- **Tables:**
  - Rows re-flow into two- or three-line blocks.
  - Long strings (hosts, paths, user agents, ids) use `overflow-wrap: anywhere`.
  - Tracebacks use `<pre>` with `white-space: pre-wrap`.
- **No horizontal scroll:** at 375 px, nothing may be wider than 343 px.

**Cards**
- Main section cards are `--card` on `--paper` with `--r-card` (26 px) corners, a ring shadow and 16 px padding.
- One job per card.
- No cards inside cards.
- No side stripes.
- Card titles are headline 17/600. Section headers are title3 20/25/600, with a 7 px gap.

### Header and notice

- **Header:**
  - A solid `--paper` background with a `--sep` bottom rule.
  - The overall glyph (16 px) and word in display 28/34/600 rounded, then `overall.reason` in `--ink-700`.
- **Freshness line** (footnote 13, mono time), on the right on desktop and under the reason on the phone:
  - "Updated {n} s ago" normally.
  - "Showing what we had at 14:02:31. Trying again." after any failed poll: a `--calm` hollow-ring glyph on a `--calm-bg` chip, the text in `--ink-900` (calm text is only 3.79:1 on paper in light mode). The last data stays at full opacity.
- **Notice:** only when `overall.problems` has a critical entry. It is one card: `--danger-bg`, a diamond in `--danger`, the title "Needs you now" in 17/600, and one 13 px `--ink-700` line per critical problem's reason. This is the only banner. Every other problem shows only in its own tile or section.

### Panels

- **Status:** six checks, in the order phone, tunnel, gate, server, worker, openrouter. The labels are Phone, Tunnel, The gate, Server, Worker and OpenRouter. Each check shows its pill (`word`) and `reason`, plus these mono facts, each dropped when null:
  - **Phone:** `last_route` · "{requests_1h} in the last hour". When `device` is set: "{model} · iOS {os} · {pairing} · last on this Mac {ago} ago".
  - **Tunnel:** `host`, then "{edge} · rtt {rtt_ms} · e2e {probe.latency_ms}", then "cloudflared {version} · {requests_total} requests · {request_errors} errors". When `phone_host` ≠ `host`, it also shows "phone uses {phone_host}".
  - **The gate:** "/healthz {integrity.healthz} · /docs {integrity.docs} · checked {ago} ago", then "{blocked_24h} blocked in 24 h".
  - **Server:** "127.0.0.1:{port} · {latency_ms}". With `started_at`: "up {duration}".
  - **Worker:** "{queued} waiting · {transcribing} hearing · {analyzing} making sense" (plus "· {n} reports working" when report jobs are unfinished), then "oldest {oldest_pending_ms}", then "{done_24h} done · {failed_24h} failed in 24 h".
  - **OpenRouter:** a 6 px capsule track on `--fill-q` whose fill is the share spent (`total_usd` / `limit_usd`). The fill is `--ink-700`, or `--warning` / `--danger` with the status. Then "${week_usd} this week".
- **Notches:**
  - **Header:** "{notches} in 24 h · {in_flight} working · {failed} not written".
  - **Rows:** up to 20. In-flight rows get a `--progress-bg` background.
    - **Desktop columns:** time (mono; `submitted_at`, else `recorded_at`), the state pill, the waterfall (flexible width), elapsed, recording, words.
    - **Line two** (only when there is something to say): "attempt {n}" when n > 1, "audio not on disk" when `audio_on_disk` is false, and the `note` in `--ink-900`.
    - **375 px:** each row stacks: time · pill · elapsed, then the waterfall at full width, then "rec 11.6 s · 27 words".
  - **Row pill** (from `state`):
    - `queued`: "Waiting"
    - `transcribing`: "Hearing it…"
    - `analyzing`: "Making sense of it…"
    - These three are in-progress styled.
    - `complete`: "Written" (ok)
    - `failed`: "Not written" (critical)
  - **Waterfall:**
    - One 6 px capsule lane per phase, stacked with 2 px gaps, on a shared scale of 0 to `elapsed_ms`. Each segment is placed at `start_ms` with width `ms`, so the overlap of `classify` and `chat` shows honestly.
    - Colours by phase: `wait` `--ink-300`; `stt` `--leaf-300`; `classify` `--leaf-500`; `chat` `--leaf-600`; `run` `--leaf-500`. A `running` phase is `--accent` and breathes; a `failed` phase is `--danger`.
    - Under the lanes, a mono caption that doesn't depend on colour: "wait 0 ms · stt 1.8 s · classify 190 ms · chat 7.7 s…" (with an ellipsis while running, and "failed" after a failed phase).
    - Phase words for screen readers (running/done/failed): Waiting/Waited/Couldn’t start, Hearing it…/Heard/Couldn’t hear it, Sorting it…/Sorted/Couldn’t sort it, Writing it up…/Written/Couldn’t write it up, Making sense of it…/Done/Couldn’t finish.
- **Traffic:**
  - **Lead line:** "{requests_1h} requests in the last hour · {errors_1h} with an error".
  - **Chart:** a 60-bar stacked chart from `hour.by_class`.
    - Colours: 2xx `--leaf-500`, 3xx `--ink-500`, 4xx `--warning`, 5xx `--danger`.
    - 1 px `--card` gaps between segments, one `--sep` baseline, and no gridlines.
    - The legend is text beside the bars; the last non-zero bar gets a mono value.
    - `role="img"` with an `aria-label` summary.
  - **Routes:** a table of route (mono), count, errors, p50 and p95, right-aligned and tabular, labelled "Last 24 hours". At 375 px, p50/p95 merge into one cell and errors show only when non-zero.
- **Models:**
  - **One block per kind:**
    - An eyebrow with the kind in mono, then the model id in mono.
    - Metric cells (`MetricCellRow`: a 28 px tabular value over a 12 px caption, split by inset 0.5 px `--sep` rules): calls; "{failed} of {calls} failed" (with a warn glyph when non-zero); p50 and p95 as two cells (at 28 px they don't fit side by side in a ~190 px block); tokens ("—" when null); cost.
    - Three across on desktop; stacked at 375 px.
  - **Recent calls:** time, kind, tool, status, latency, attempt, tokens and cost. A failed call gets a warn glyph beside its status.
  - `note` shows above the blocks in `--ink-700` when set.
- **The gate:**
  - **Header:** `blocked_24h` as a big number with the caption "blocked in 24 h", and `top_paths` as mono lines with counts.
  - **Recent table:** time, method, path (mono), country, user agent (one line, ellipsis) and IP (mono). At 375 px: the path on line 1; time · country · IP on line 2; the user agent on line 3.
  - A blocked probe is healthy, so nothing here is coloured as a problem.
- **Your record:**
  - Metric cells: entries (with `by_state` as a caption line), projects, reports ("last written {ago} ago"), and report jobs by state.
  - Audio: "{disk_bytes} in {disk_files} files", with `past_retention` only when non-zero.
  - Bytes use 1000-based units ("1.2 MB").
- **Errors:**
  - Two groups, Server and Tunnel. Each item shows the level word, `message` (UI font), `exception` (mono), "×{count}" and "first {ago} ago · last {ago} ago".
  - It opens a `<details>` holding `sample` in a `<pre>`.
- **Where this comes from:** a grouped card with one row per `sources` entry, in order.
  - Each row shows the name, `where` (mono), and a glyph for the state (`ok` filled, `off` hollow ring, `unreachable` warn triangle).
  - Then either "read {ago} ago" or the `reason`.

### Formats

| Kind | Format |
|---|---|
| Durations | under 10 ms "3.8 ms"; under 1 s "212 ms"; under 60 s "1.8 s"; under 60 min "3 min"; then "2 h 5 min" |
| Instants | today "14:02:31"; earlier "Sep 24 14:02". "Ago" forms use "10 s", "3 min", "2 h", "1 day". |
| Money | "$0.04"; above 0 and under $0.01, "<$0.01"; per call, 4 decimals ("$0.0041") |
| Tokens | "2,524" |

### Waiting, empty and unreachable

- **First load:** skeletons in the known shapes, pulsing `--fill-q`.
- **A null panel** shows the reason of the source it depends on, in `--ink-700`, inside the card, with the hollow-ring glyph:

| Panel | Null (source reason; the default copy when the backend has none) | Empty |
|---|---|---|
| Notches | "Couldn’t read the record. Your notches are safe. Only this read failed." | "No notches yet. The next one shows up here as it happens." |
| Traffic | `sources.caddy_log.reason` | chart: "Quiet for the last hour."; routes: "No requests in the last 24 hours." |
| Models | `models.note` | "No model calls in the last 24 hours." (or `note` when set) |
| The gate | `sources.caddy_log.reason` | "No one has knocked in the last 24 hours." |
| Your record | same as Notches | — |
| Errors (each group) | e.g. "Not set up. Set NOTCH_DASH_SERVER_LOG to read server errors." | "Nothing’s gone wrong in the last 24 hours." |

### Voice

- Calm, plain and exact. Say what is safe first.
- Sentence case, contractions with curly apostrophes, no exclamation marks, no blame.
- Never "generate", "analyse", "AI", "magic" or "smart".
- Claim only what the data shows. Most copy comes from the backend (`word`, `reason`, `note`), and the page adds only the labels, empty states and formats above.

**Preview against the sample** (states the live stack isn't in)
1. Copy `tests/dash_sample_snapshot.json` to `<scratch>/api/snapshot` in a scratch dir.
2. Put `static/` and `index.html` beside it.
3. Serve that dir on a free 127.0.0.1 port other than 4130, which is the dashboard's own.

To see the other states, edit a copy: null a panel, set a check to critical, or empty the lists. Serving each copy from its own subfolder also exercises the path prefix.

---

## Who edits what

| Owner | Files |
|---|---|
| architect | `DASHBOARD.md`, `tests/dash_sample_snapshot.json` |
| backend | `notch_dash/*.py`; `notch_api/metrics.py` (new); the hook lines in `notch_api/{config,__main__,openrouter,worker}.py`; `tests/test_dash_*.py` and `tests/test_metrics.py` |
| page | `notch_dash/static/{index.html,app.js,style.css}` |

A contract change updates this section and the sample JSON in the same commit, and says why.

## Verification

### Metadata only (Sep 25)

- `python -m pytest -q tests/test_dash_snapshot.py -k no_notch_content`: a marker phrase in every entry text column, the tags, the takeaways, the profile and a report headline. It failed on the summary before the change and passes after. `python -m pytest -q`: 381 passed.
- Live against the phone's record: `api/snapshot` has no `summary`, `mood`, `tags` or `profile` key, and the gate secret appears 0 times. In the browser, the notch row reads "Written · wait 0 ms · run 2.0 s · rec 11.6 s · 27 words", and the console is empty.

### Review fixes (Sep 25)

Each finding was reproduced before it was fixed, and each new or changed test failed first.

| Finding | Reproduced | Test that failed first |
|---|---|---|
| A prober sending `notch-dash/1` hid from the Gate panel and the leak alarm | the reviewer's synthetic lines: forged user agent → dropped, leak → gate `unknown` | `test_dash_logs.py::test_caddy_lines_are_sorted_into_passed_blocked_and_local[forged-own-probe, own-probe-leaked, earlier-run-probe]` |
| Snapshot readable from the network through Caddy's `*:80` | `curl -H 'Host: dash.notch.localhost' http://<LAN ip>/api/snapshot` → 200 with the full snapshot | `test_dash_app.py::test_only_this_mac_is_answered_even_through_caddy` |
| A percent-escaped secret wasn't redacted | a throwaway Caddy on 127.0.0.1:4159 (`admin off`, a stand-in secret): escaped and upper-case forms both passed `handle_path` and were logged raw | `test_dash_logs.py::test_redact_hides_a_secret_with_escaped_characters_which_caddy_unescapes_and_lets_through` |
| The secret went to any host the metrics port named | the reviewer's MockTransport script: the probe went to `collector.example.net` | `test_dash_probes.py::test_cloudflared_names_no_host_that_is_not_a_quick_tunnel_since_the_secret_goes_there` |
| 50 000 blocked probes evicted the phone | the reviewer's script: at 50 000 the phone read "Not seen" and `phone_host` null; after the fix, 60 000 left it Fine with its host | `test_dash_snapshot.py::test_a_flood_of_blocked_probes_never_pushes_the_phone_out_of_the_window` |
| Server and tunnel log windows unbounded | 20 000 cloudflared ERR lines: 71 ms a snapshot; after the fix 100 000 lines keep 5 000, 6 MB, 3 ms | none: the bound is a setting, and the per-kind windows are held by the flood test above |
| A line written just before a roll was lost | write, tick, append, rename, new file, tick → the appended line missing | the roll step of `test_dash_logs.py::test_tail_follows_appends_rotation_and_truncation_and_keeps_a_day` |
| A 500 happening now was dated hours back | the reviewer's script: last stamp 5 h ago → "5 h ago"; 25 h ago → no error shown; after the fix both "0 s ago" | `test_dash_logs.py::test_a_timeless_error_takes_the_last_stamp_in_the_backlog_but_the_time_it_was_read_once_followed` |
| Moved cleared itself a day after the phone's last request | a phone request 25 h old through an old address → tunnel Fine | `test_dash_snapshot.py::test_the_tunnel_stays_moved_after_the_phone_last_got_through_over_a_day_ago` |
| `/healthz` every 2 s with nobody watching | an unwatched instance wrote 10 `GET /healthz` lines to `phone-server.log` in 20 s, from its own connection (lsof) | none: a scheduling flag; checked live below |

**Live**, with the command under Run it, read-only (the phone server, Caddy and cloudflared untouched; `phone.db` kept its size and mtime):
- Unwatched for 20 s: 0 new `/healthz` lines. The first snapshot after that said "Can’t tell yet" (server "Not checked yet"); the next, 2 s later, "All fine", with the server answering in 4 ms.
- All six checks Fine; every source `ok` but `metrics`, still waiting for the phone server's restart. A snapshot took 2 ms.
- Gate: `blocked_24h` 1 (`/wp-login.php`), where before the earlier-run rule it was 53, 52 of them earlier dashboard runs' probes. The 33 lines this run's probes wrote to Caddy's log counted nowhere. The 4 `GET /healthz` left in traffic are curl checks (2 local, 2 through the gate).
- From the Mac's LAN address the snapshot and page get 403, also with a forged `X-Forwarded-For: 127.0.0.1`. Through `dash.notch.localhost` and straight to 127.0.0.1:4130 they get 200. POST 405, a foreign Host 421, and the four headers on the snapshot.
- `grep -c` found the gate secret and the OpenRouter key 0 times each in `api/snapshot`, `/`, `static/app.js`, `static/style.css` and the dashboard's log; no run of 48 hex in the snapshot.
- The page at http://dash.notch.localhost in the in-app browser: All fine, the Gate panel listing only `/wp-login.php`, and no console messages.

### Integration: backend and page together (Sep 25)

Run live and read-only with the command under Run it (the three optional files pointing at this session's `phone-server.log`, `tunnel.log` and `gate-secret`). The phone server on 4131, Caddy and cloudflared were not touched; `phone.db` kept its size and mtime.

**The snapshot against its sources** (`curl -s http://dash.notch.localhost/api/snapshot`, field by field):
- Record, via `sqlite3 -readonly phone.db` on `capture_jobs`, `entries`, `audio_objects`, `reports`, `report_jobs` and `users`: one notch submitted 20:34:00Z (1790368440) and finished 2 s later, 27 words, flat, tagged testing, 11.57 s recorded. Also one report generated 20:37:43Z with its job complete, the profile "Cc · America/New_York · 5", and one audio object of 148,421 bytes, which is also the one file on disk. Every figure matches.
- Caddy log, parsed separately with Via meaning passed: 11 passed (10 from the phone, 1 curl `/<gate>/healthz`), 1 blocked (`/wp-login.php`, 404), 1 on api.notch.localhost, and 9 notch-dash/1 lines. Routes, the phone's last route and `requests_1h` (5), `blocked_24h` (1) and the IPv6 client IP all match, and the notch-dash/1 lines are counted nowhere.
- Server log: its 4 httpx lines give models stt 1, classify 1 and chat 2, from `server_log`. The "resumed" line at 15:42:51 local is `started_at` 1790365371.268. It has no ERROR or WARNING lines, and `errors.server` is `[]`.
- cloudflared: `/ready` shows 1 connection and `/metrics` shows edge ewr14, version 2026.9.3 and 0 request errors. The tunnel log's URL is the snapshot's `host` and `phone_host`. It has no WRN or ERR lines, and `errors.tunnel` is `[]`.
- `metrics` is `unreachable` ("it isn’t there"). That is expected until the phone server restarts with the hook.

**The page** in the in-app browser at http://dash.notch.localhost:
- Every panel showed the figures above, read from the accessibility tree and screenshots. Times are local, for example 16:34:00 for 20:34:00Z.
- The console had no errors and no CSP reports.
- The only requests were `/`, `static/style.css`, `static/app.js` and `api/snapshot`. No `href` or `src` holds an absolute URL.
- With the tab visible it made 5 snapshot requests in 7 s. A hidden tab stops polling.
- Layout at each width:
  - 1280 px: 3 × 2 tiles and the split.
  - 1024 px: 2 × 3 tiles, stacked.
  - 375 px (preset mobile): the grouped status card and reflowed tables. Nothing reaches past the 16 px gutter, and `scrollWidth` equals the viewport at every width.
- Light and dark both render, using prefers-color-scheme emulation.
- Fixed while checking:
  - At 700–1099 px, the blocked-requests table printed "COUNTRYUSER AGENT": the Method and Country headers were 60 and 67 px in 57 px columns. Both columns are 6em now and were measured to fit.
  - At 375 px, a model call with no latency (the server-log fallback) showed a bare "—". That empty cell is now hidden like the others.

**Path prefix:**
- `curl -s -H 'Host: 127.0.0.1:4130' http://127.0.0.1:4130/`: the HTML references only `static/style.css`, `static/app.js`, `data:,` and the in-page `#g-*` symbols, and `app.js` fetches only `api/snapshot`.
- A throwaway stdlib proxy on 127.0.0.1:4158 served the dashboard under `/pfx/dash/`, with the prefix stripped. The page rendered, and its requests were `/pfx/dash/static/style.css`, `/pfx/dash/static/app.js` and `/pfx/dash/api/snapshot`, none outside the prefix.

**Secrets:**
- The gate secret and the OpenRouter key were read into shell variables only.
- `grep -c` found each 0 times in `api/snapshot`, `/`, `static/app.js`, `static/style.css` and the dashboard's own log.
- The snapshot has no run of 48 hex characters, and none of `label`, `creator_user_id`, `workspace_id`, `serialNumber`, `ecid` or `potentialHostnames`.

**Headers:**
- `curl -sI` on `/`, `static/app.js`, `api/snapshot` and a 404 returned Cache-Control no-store, Referrer-Policy no-referrer, X-Content-Type-Options nosniff and the CSP above.
- A POST gets 405, a foreign Host gets 421, and `/docs` and `/openapi.json` get 404.
- The dashboard listens on 127.0.0.1:4130 only.
- A snapshot takes about 1 ms to build and 2–3 ms through Caddy.

**Idle, then back:**
- Before the fix: after 2+ min with no viewer, the first snapshot showed the gate secret as `unreachable` ("not read yet"), although the file had been read fine. The probe had only paused.
- Now a source's state follows its last read, and a stale value is dropped by the checks alone (see Sources and cadence).
- Re-run live after the fix: the first snapshot after 140 s idle had the gate secret `ok`, read 94 s earlier, and the tunnel without its probe clause. The probe was back 2 s later, at 230 ms.
- The OpenRouter reason for a paused read now says "not read in the last 10 min", not "not read yet".

**Tests:**
- `tests/test_dash_snapshot.py::test_a_probe_paused_while_nobody_watched_leaves_its_source_readable_and_drops_only_its_value` failed first (`'unreachable' != 'ok'`) and passes with the fix.
- The five dashboard test files pass, and so does the rest of the suite.
- `node --check notch_dash/static/app.js` passes.
- The two page fixes are layout fixes, recorded above. The repo has no page test harness to hold them.

### Backend and instrumentation (Sep 25)

**Tests**, run from the worktree with `~/Desktop/Notch/notch-report-design/.venv/bin/python -m pytest -q`:
- `tests/test_metrics.py`, `tests/test_dash_logs.py`, `tests/test_dash_probes.py`, `tests/test_dash_snapshot.py` and `tests/test_dash_app.py` pass, and the existing suite still passes around them.
- They were written first. Each guarded behaviour was then broken on purpose, one at a time, and its test went red: redaction of the secret, traceback grouping and a timeless line's time, rotation and truncation in `Tail`, the 120 s stuck boundary, `mode=ro`, a failed record read still answering 200, both waterfalls, probe errors worded without the exception text, OpenRouter's label kept out, notch-dash/1 traffic dropped, the Host allow-list, the headers, the overall order, the half-checked gate, a failed probe while the server is down, the recorder's `try`, `ok` for in-band errors, the timeout status, and the job tag's reset.

**Live, read-only**, against the running stack (phone server on 4131 untouched, not restarted):
- `python -m notch_dash` with `NOTCH_DB`, `NOTCH_AUDIO_DIR`, `NOTCH_DASH_SERVER_LOG`, `NOTCH_DASH_TUNNEL_LOG` and `NOTCH_DASH_GATE_SECRET_FILE` set as in Run it; `curl -s http://dash.notch.localhost/api/snapshot | jq`.
- Every source `ok` except `metrics`, which is `unreachable` ("it isn’t there") until the phone server restarts with the hook; models fall back to the server log (stt 1, classify 1, chat 2).
- Overall "All fine". Phone seen through the gate, iPhone 17 Pro paired over localNetwork. Tunnel: 1 connection via ewr14, rtt 13 ms, end-to-end probe 728 ms, the phone's host the same as cloudflared's. Gate: /healthz and /docs 404 from outside, 1 blocked probe in 24 h. Server answering in about 1 ms. Worker idle, 2 done in 24 h. OpenRouter $0.05 today, $49.75 left of $50.
- Pipeline: the one real notch, `phase_source: "db"` (it predates the metrics file), 2.0 s end to end, audio on disk.
- The dashboard's own probes appear in Caddy's log as notch-dash/1 and are counted nowhere.
- `grep -c "$S"` for the gate secret: 0 in the snapshot, the page, `app.js`, `style.css` and the dashboard's own log. No OpenRouter key either.
- A snapshot takes 1–2 ms to build and 2–6 ms end to end through Caddy. POST answers 405, a foreign Host 421.
- `phone.db` itself is untouched (same size and mtime). The read-only connection keeps SQLite's `-shm`/`-wal` sidecars, as noted under SQLite access.
