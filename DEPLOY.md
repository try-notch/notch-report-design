# Deploying notch_api to the VPS

The production server is one VPS (OVHcloud, US East) running Ubuntu 24.04. It serves the
stateless `/v2` API at `https://api.trynotch.xyz`
(`docs/backend-contract.md` in notch-ios-dev is the wire). `/v1`, the development harness,
is never mounted there: it stores transcripts and audio, and the server keeps no readable
content.

```
iPhone ── HTTPS ──▶ Caddy :443 (api.trynotch.xyz, TLS, 30 MB body cap, no access log)
                      └─▶ notch_api 127.0.0.1:4131  (systemd: notch-api, user notch)
                            ├─ /var/lib/notch/meter.db   metering, accounts, config, Notch Cloud ciphertext
                            ├─ /var/lib/notch/tmp        tmpfs (512 MB ceiling): ffmpeg's per-request directories
                            └─▶ OpenRouter, Supabase JWKS / admin API, Apple

Your devices ── tailnet ──▶ tailscale serve (https://<machine>.<tailnet>.ts.net)
                              └─▶ notch_dash 127.0.0.1:4130  (systemd: notch-dash)
```

Everything in `deploy/`:

| File | Goes to | What it is |
|---|---|---|
| `setup.sh` | run on the VPS | Idempotent machine setup: packages, Caddy, the `notch` user, directories, the tmpfs, the units, the firewall |
| `deploy.sh` | run on the Mac | Tests, ships `notch_api/`, `notch_dash/` and `requirements-server.txt`, restarts, checks `/healthz` |
| `Caddyfile` | `/etc/caddy/Caddyfile` | The one public site |
| `notch-api.service` | `/etc/systemd/system/` | The API, hardened, with `NOTCH_TMP` on the tmpfs |
| `notch-dash.service` | `/etc/systemd/system/` | The dashboard, 127.0.0.1 only |
| `notch.env.example` | `/etc/notch/notch.env` | The API's settings (placeholders; fill in on the VPS) |
| `dash.env.example` | `/etc/notch/dash.env` | The dashboard's settings |
| `notch-admin` | `/usr/local/bin/notch-admin` | `python -m notch_api.admin` as the service's user |

Nothing in this repo holds a real key, token or password. The real values live only in
`/etc/notch/` on the VPS.

---

## 1. Before the machine: accounts and keys (owner only)

**Supabase** (Auth only; there is no Supabase database for Notch data):
1. In the production project, switch JWT signing to asymmetric keys: *Project Settings → JWT
   Keys*, "Migrate JWT secret", then "Rotate keys" to an **ES256** key. Until then the JWKS
   endpoint is empty and every token is refused. Wait at least 1 h 15 min before revoking the
   legacy secret, so sessions signed with it expire first.
2. Check `https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json` lists the key.
3. Enable the providers (Apple, Google, email with 6-digit codes through Resend).
4. Copy the **secret API key** (`sb_secret_…`, *Project Settings → API Keys*) for
   `SUPABASE_SECRET_KEY`. A legacy `service_role` key also works; the server sends a new-style
   key in the `apikey` header only, and a legacy one in both headers.

**OpenRouter:**
1. *Settings → Privacy*: turn on **"Enable ZDR only"**, and leave prompt logging and "OpenRouter
   Use of Inputs/Outputs" **off**.
2. Create a key for this server only, with a monthly credit limit, for `OPENROUTER_API_KEY`.
3. Use only models whose providers are on OpenRouter's ZDR list. That is the owner's decision
   (2026-09-26), in place of waiting for written confirmation. Transcription and Jev can't carry
   the per-request ZDR flag, so the server audits each call's provider afterwards and turns that
   path off on a miss (see "Zero retention" below).

**Apple** (Sign in with Apple token revocation on account deletion):
1. *Certificates, Identifiers & Profiles → Keys*: a key with Sign in with Apple enabled for the
   app's App ID. Download the `.p8` once.
2. Note the team id, the key id and the app's bundle id.

**The body HMAC key:** `openssl rand -hex 32`. Keep it for the life of the server: changing it
makes every pending retry look like a reused key (409 `idempotency_key_reused`).

## 2. The machine (owner only)

1. Order the VPS from OVHcloud in a **US East** location: **VPS-1** (2 vCores, 4 GB RAM, 40 GB
   NVMe) is enough, with image **Ubuntu 24.04** and your SSH key. OVH's Ubuntu image logs in as
   `ubuntu`, with your key installed and passwordless `sudo`; every step below uses that user.
2. DNS: `A` (and `AAAA`, if the VPS has IPv6) records for `api.trynotch.xyz` to the VPS's
   addresses. Caddy cannot get a certificate until they resolve.
3. Check you can get in and use sudo without a password (`deploy.sh` runs `sudo` over SSH
   without a terminal):
   ```bash
   ssh ubuntu@<vps address> 'sudo -n true && echo ok'
   ```
   If it asks for a password, give `ubuntu` a sudoers drop-in:
   `echo "ubuntu ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/90-ubuntu && sudo chmod 440 /etc/sudoers.d/90-ubuntu`.

**Sized for 2 vCPU and 4 GB.** One uvicorn process, by design: the per-account in-flight
count and the config cache live in it. ffmpeg, the only CPU-heavy step, runs single-threaded,
and at most `transcribe_concurrency` (2, remote config) transcriptions decode and transcribe
at once; a third waits up to 10 s for a slot, then is 503 `unavailable` with `Retry-After`.
Model calls are network-bound and share a 16-thread pool. A transcription holds about 100 MB
of memory at its peak; `notch-api` may use at most 75% of RAM (`MemoryMax`), and the tmpfs is
capped at 512 MB. On a bigger machine, raise `transcribe_concurrency` with a config push.

## 3. Set the machine up

From the Mac, copy `deploy/` over and run `setup.sh`:

```bash
rsync -az deploy/ ubuntu@<vps address>:notch-deploy/
ssh ubuntu@<vps address> 'sudo bash notch-deploy/setup.sh'
```

It installs ffmpeg, Python's venv, sqlite3, ufw and Caddy; creates the `notch` user,
`/opt/notch/{app,venv}`, `/var/lib/notch` (0700) and `/etc/notch` (0750, root:notch); mounts a
512 MB tmpfs at `/var/lib/notch/tmp` from `/etc/fstab` (0700, owned by `notch`, `noexec`); installs
the units, the Caddyfile and `notch-admin`; and turns on ufw with only 22/tcp, 80/tcp, 443/tcp and
443/udp open. Run it again whenever `deploy/` changes; it keeps `/etc/notch/*.env`.

Then fill in the settings, on the VPS:

```bash
sudo -e /etc/notch/notch.env        # replace every <placeholder>
sudo install -o root -g notch -m 0640 /path/to/AuthKey_XXXX.p8 /etc/notch/apple-signin.p8
```

## 3b. Run it as a container (the owner's chosen path)

Test on the Mac first, then ship the same image to the VPS. Only two files hold secrets, and
both are gitignored and never committed:
- `.env` in the repo root: every key from `deploy/notch.env.example`, except the paths and
  ports that `compose.yaml` sets itself;
- `secrets/apple-signin.p8`.

**On the Mac:**
```bash
NOTCH_ENV=dev NOTCH_HOST_PORT=4175 docker compose up --build
```
- The Mac's Caddy serves it at `http://api.notch.localhost`, which points at 4175.
- With no secrets yet, add `NOTCH_FAKE_MODELS=1 NOTCH_DEV_AUTH=1`. The fakes accept only
  `fakes.fake_recording()` audio; real audio comes back as `audio_unreadable`.
- Don't run it while another local server holds 4175.

**On the VPS**, once the Mac run looks right:
1. Install Docker from Ubuntu's own packages, then stop the systemd service:
   `sudo apt-get install -y docker.io docker-compose-v2`, then
   `sudo systemctl disable --now notch-api`.
   Caddy, the firewall and the dashboard from `setup.sh` stay as they are. Caddy keeps
   proxying `api.trynotch.xyz` to `127.0.0.1:4131`, which is where the container listens.
2. Copy the code, then the two secret files:
   `rsync -az Dockerfile compose.yaml .dockerignore requirements-server.txt notch_api notch_dash ubuntu@<vps>:notch/`
   `ssh ubuntu@<vps> 'mkdir -p notch/secrets'`
   `scp .env ubuntu@<vps>:notch/.env`
   `scp secrets/apple-signin.p8 ubuntu@<vps>:notch/secrets/`
3. Lock the secrets down so only the container's user (uid 10001) can read the key, then start:
   `ssh ubuntu@<vps> 'cd notch && chmod 600 .env && sudo chown 10001:10001 secrets/apple-signin.p8 && sudo chmod 400 secrets/apple-signin.p8 && sudo docker compose up -d --build'`
4. Check it: `ssh ubuntu@<vps> 'curl -s 127.0.0.1:4131/healthz && sudo docker compose -f notch/compose.yaml ps'`.

**What the container enforces:**
- it runs as a non-root user, on a read-only root filesystem, with no capabilities;
- ffmpeg works in a 512 MB tmpfs at `/tmp/notch`;
- the meter database and the metadata-only metrics sit in the `meter` volume;
- it's capped at 3 GB of memory.

Roll back by checking out the previous commit and running `docker compose up -d --build`
again. The volume keeps the database.

## 4. Deploy the code

From the repo root on the Mac, with the server changes committed:

```bash
NOTCH_VPS=ubuntu@<vps address> deploy/deploy.sh
```

It runs the offline suite, ships `notch_api/`, `notch_dash/` and `requirements-server.txt`
(nothing else), installs the requirements into the venv, restarts `notch-api` and
`notch-dash`, and waits for `/healthz`. The shipped commit is in `/opt/notch/app/REVISION`.
`SKIP_TESTS=1` skips the suite when you have just run it.

On first start `notch_api` creates `/var/lib/notch/meter.db`. With `NOTCH_ENV=prod` it refuses
to start without `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `NOTCH_TMP` and a 32+ character
`NOTCH_BODY_HMAC_KEY`, and refuses `NOTCH_DEV_AUTH=1` or `NOTCH_FAKE_MODELS=1` outright.

**Roll back** to the release before:

```bash
ssh ubuntu@<vps address> 'sudo rsync -a --delete /opt/notch/app.previous/ /opt/notch/app/ && sudo systemctl restart notch-api notch-dash'
```

or check out an older commit on the Mac and run `deploy.sh` again.

## 5. Check it

```bash
curl -s https://api.trynotch.xyz/healthz                                   # {"ok":true}
curl -s -H 'X-Client: ios/1.0.0+1' https://api.trynotch.xyz/v2/config      # the public config
ssh ubuntu@<vps address> 'sudo journalctl -u notch-api -n 20 --no-pager'   # one JSON line per request
```

A signed-in check needs a real Supabase session token: sign in from a TestFlight build and
watch `journalctl -u notch-api -f` while it calls `GET /v2/config` (the line has
`"kind": "config"` and `"status": 200`).

## 6. The dashboard at dash.trynotch.xyz (password)

The dashboard runs as the `dash` service in `compose.yaml`, from the API's own image: it shares
the `api` container's network (so its health probe on 127.0.0.1:4131 reaches the server), reads
the `meter` volume read-only, and publishes 4130 on the host's 127.0.0.1 only. Caddy serves it at
`dash.trynotch.xyz` behind `basic_auth`, and the dashboard trusts that gate and nothing else.

1. **Retire the systemd dashboard first.** `setup.sh` installed `notch-dash.service` on
   127.0.0.1:4130; while it runs, the containers cannot publish 4130 and **the API does not
   start either** (both ports belong to the `api` container):
   ```bash
   sudo systemctl disable --now notch-dash
   ```
2. DNS, DNS only: `A dash → <VPS IPv4>` and `AAAA dash → <VPS IPv6>`.
3. On the VPS, `~/notch/compose.override.yaml` (untracked; the Mac never has it):
   ```yaml
   services:
     dash:
       environment:
         NOTCH_DASH_AUTH_PROXY: "1"
   ```
4. The login: make a random password on the Mac, keep it somewhere private, and put only its
   hash on the VPS (`caddy hash-password --plaintext <password>`). Append a site to
   `/etc/caddy/Caddyfile`:
   ```
   dash.trynotch.xyz {
   	basic_auth {
   		notch <bcrypt hash>
   	}
   	reverse_proxy 127.0.0.1:4130
   }
   ```
   then `sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && sudo systemctl reload caddy`.
5. `cd ~/notch && sudo docker compose up -d --build`, and open `https://dash.trynotch.xyz`.

On the VPS the dashboard's Mac sources (the /v1 database, Caddy's log, the tunnel, the phone)
are switched off in `dash.env`; it shows the API's health and OpenRouter key spend. Panels over
the meter database are the next step for it.

## 7. Operating it

**Remote config** (no deploy, no App Store release). Bodies are overrides of the defaults in
`notch_api/remote_config.py`; the newest valid version wins, and a push that would not
validate is refused.

```bash
sudo notch-admin config show
echo '{"features": {"notch_cloud": false}}' > /tmp/cloud-off.json
sudo notch-admin config push /tmp/cloud-off.json --note "cloud off while we look at an incident"
```

Rolling back is pushing the older body again (`config show` prints the current one).
Switching prompt variants is a config push (`{"prompts": {"analyze": "v4"}}`); a new prompt is
a deploy that adds a variant to `notch_api/prompts.py` first.

**Accounts:** `sudo notch-admin account block <user uuid> --code abuse`, and `unblock`. A
blocked account gets 403 `account_blocked` on processing and Notch Cloud writes.

**Spend:** the server stops everyone at $20 of model spend per UTC day (503
`processing_paused`) and each account at $1 (429 `quota_exceeded`). Today's total:
```bash
sudo -u notch sqlite3 /var/lib/notch/meter.db "select kind, calls, round(cost_usd, 4) from daily_spend where day = date('now')"
```

**Zero retention:** chat calls carry `provider: {zdr: true, data_collection: "deny"}`. After
every transcription and Jev call the server looks up who served it and checks the ZDR endpoint
list; on a miss it pushes a config version that turns capture off (or the classifier back to
chat) and logs an `alert` line. Watch for them:
```bash
sudo journalctl -u notch-api | grep '"event": "alert"'
```
After a `zdr_miss`, capture stays off until you push a config that turns it on again.

**Logs** are journald's (`journalctl -u notch-api`): one allowlisted JSON line per request,
message templates for everything else, exception types and frames with no messages. Caddy
writes no access log. Set journald's retention in `/etc/systemd/journald.conf` if you want it
shorter than the default.

**Backups:** `meter.db` holds metering and Notch Cloud ciphertext. Back it up with
`sqlite3 /var/lib/notch/meter.db ".backup /somewhere/meter-$(date +%F).db"` into encrypted
storage, and remember a deleted account's rows live on in old backups until they expire.

**Updating the OS:** `sudo apt-get update && sudo apt-get upgrade`, then
`sudo systemctl restart notch-api notch-dash caddy`.
