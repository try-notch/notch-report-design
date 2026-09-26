#!/usr/bin/env bash
# deploy/setup.sh — prepare an Ubuntu 24.04 VPS for notch_api. Run it on the VPS through
# sudo, from a copy of this deploy/ folder (as the image's `ubuntu` user, say):
#
#   sudo bash notch-deploy/setup.sh
#
# Idempotent: every step checks before it acts, so running it again repairs a drifted
# machine and changes nothing on a correct one. It never overwrites /etc/notch/*.env.
# It installs: ffmpeg, Python's venv, sqlite3, ufw and Caddy (from Caddy's own apt
# repository); a `notch` system user; /opt/notch (code and venv), /var/lib/notch (the
# meter database) and /etc/notch (settings); a RAM-backed tmpfs at /var/lib/notch/tmp for
# ffmpeg; the two systemd units and the notch-admin wrapper; the Caddyfile; and a firewall
# that lets in only SSH (22), HTTP (80) and HTTPS (443). Code arrives with deploy.sh.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "setup.sh: run it through sudo (sudo bash notch-deploy/setup.sh)" >&2; exit 1; }
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
app=/opt/notch/app
venv=/opt/notch/venv
state=/var/lib/notch
tmpfs=/var/lib/notch/tmp
conf=/etc/notch
# A ceiling, not a reservation: a transcription holds about 30 MB here while it runs, and two
# run at once (remote config transcribe_concurrency), so 512 MB is ample on a 4 GB machine.
tmpfs_size=512m

say() { printf '\n== %s\n' "$*"; }

say "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q ffmpeg python3 python3-venv sqlite3 ufw curl gnupg rsync \
  debian-keyring debian-archive-keyring apt-transport-https

say "caddy"
if ! command -v caddy >/dev/null; then
  curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
    | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q
  apt-get install -y -q caddy
fi

say "the notch user and its directories"
id notch >/dev/null 2>&1 || useradd --system --home-dir "$state" --no-create-home --shell /usr/sbin/nologin notch
install -d -o root -g root -m 0755 /opt/notch "$app"
install -d -o notch -g notch -m 0700 "$state"
install -d -o root -g notch -m 0750 "$conf"

say "tmpfs for NOTCH_TMP"
install -d -o notch -g notch -m 0700 "$tmpfs"
line="tmpfs $tmpfs tmpfs rw,nosuid,nodev,noexec,size=$tmpfs_size,mode=0700,uid=$(id -u notch),gid=$(id -g notch) 0 0"
if ! grep -qE "^tmpfs[[:space:]]+$tmpfs[[:space:]]" /etc/fstab; then
  echo "$line" >> /etc/fstab
  systemctl daemon-reload
fi
mountpoint -q "$tmpfs" || mount "$tmpfs"

say "python venv"
[[ -x $venv/bin/python ]] || python3 -m venv "$venv"
"$venv/bin/pip" install -q --upgrade pip
if [[ -f $app/requirements-server.txt ]]; then
  "$venv/bin/pip" install -q -r "$app/requirements-server.txt"
fi

say "settings (kept if present)"
[[ -f $conf/notch.env ]] || install -o root -g notch -m 0640 "$here/notch.env.example" "$conf/notch.env"
[[ -f $conf/dash.env ]] || install -o root -g notch -m 0640 "$here/dash.env.example" "$conf/dash.env"

say "systemd units and notch-admin"
install -m 0644 "$here/notch-api.service" /etc/systemd/system/notch-api.service
install -m 0644 "$here/notch-dash.service" /etc/systemd/system/notch-dash.service
install -m 0755 "$here/notch-admin" /usr/local/bin/notch-admin
systemctl daemon-reload
systemctl enable notch-api.service notch-dash.service

say "caddy site"
install -m 0644 "$here/Caddyfile" /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl enable --now caddy
systemctl reload caddy

say "firewall: 22, 80, 443 only"
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp   # HTTP/3, which Caddy also serves
ufw --force enable

say "done"
if grep -q '<' "$conf/notch.env"; then
  echo "Next: put the real values into $conf/notch.env (it still has <placeholders>),"
  echo "then run deploy/deploy.sh from the Mac. DEPLOY.md has every step."
else
  echo "Settings are filled in. Deploy code with deploy/deploy.sh from the Mac."
fi
