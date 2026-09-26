#!/usr/bin/env bash
# deploy/deploy.sh — ship this checkout's server to the VPS and restart it. Run on the Mac,
# from the repo root:
#
#   NOTCH_VPS=ubuntu@<vps address> deploy/deploy.sh
#
# It refuses to ship uncommitted server code, runs the offline test suite, copies
# notch_api/, notch_dash/ and requirements-server.txt (nothing else: no .env, no data/, no
# demo modules) to a staging directory on the VPS, moves them into /opt/notch/app, installs
# the requirements, restarts both services and checks /healthz. The previous release is kept
# at /opt/notch/app.previous; DEPLOY.md says how to roll back to it.
#
# NOTCH_VPS must be the VPS, as a user with passwordless sudo (the image's `ubuntu` user).
# This script has no default host, on purpose.
set -euo pipefail

: "${NOTCH_VPS:?set NOTCH_VPS=ubuntu@<vps address>}"
cd "$(dirname "${BASH_SOURCE[0]}")/.."
shipped=(notch_api notch_dash requirements-server.txt)

if ! git diff --quiet HEAD -- "${shipped[@]}" || [[ -n $(git ls-files --others --exclude-standard -- "${shipped[@]}") ]]; then
  echo "deploy.sh: commit the server changes first; only committed code ships." >&2
  exit 1
fi
revision=$(git rev-parse --short HEAD)

if [[ ${SKIP_TESTS:-0} != 1 ]]; then
  .venv/bin/python -m pytest -q
fi

echo "== shipping $revision to $NOTCH_VPS"
ssh "$NOTCH_VPS" 'mkdir -p ~/notch-release'
rsync -az --delete --exclude '__pycache__' --exclude '*.pyc' "${shipped[@]}" "$NOTCH_VPS:notch-release/"

ssh "$NOTCH_VPS" "REVISION=$revision bash -s" <<'REMOTE'
set -euo pipefail
app=/opt/notch/app
sudo rm -rf "$app.previous"
if [[ -d $app/notch_api ]]; then sudo cp -a "$app" "$app.previous"; fi
sudo rsync -a --delete --chown=root:root --chmod=D755,F644 ~/notch-release/ "$app/"
echo "$REVISION" | sudo tee "$app/REVISION" >/dev/null
sudo /opt/notch/venv/bin/pip install -q -r "$app/requirements-server.txt"
sudo /opt/notch/venv/bin/python -m compileall -q "$app"
sudo systemctl restart notch-api notch-dash
for attempt in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:4131/healthz >/dev/null 2>&1; then
    echo "== notch-api answers /healthz ($REVISION)"
    exit 0
  fi
  sleep 1
done
echo "== notch-api did not come up; recent log lines:" >&2
sudo journalctl -u notch-api -n 30 --no-pager >&2
exit 1
REMOTE
