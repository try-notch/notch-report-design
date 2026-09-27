"""
settings.py — the dashboard's configuration, and the only place that reads the environment
(DASHBOARD.md › Environment). A source set to None is off; from_env turns an empty variable
into None, so `NOTCH_DASH_CADDY_LOG=` switches that source off.
"""

import os
from dataclasses import dataclass, field

from notch_api import config  # importing it loads .env, where the OpenRouter key lives

CADDY_LOG = "/opt/homebrew/var/log/caddy-notch.access.log"
TUNNEL_METRICS = "127.0.0.1:20241"
DEVICE = None  # set NOTCH_DASH_DEVICE to the phone's hardware UDID to turn the device panel on
PORT = 4130


# Where `tailscale serve` forwards from: Tailscale's IPv4 (CGNAT) and IPv6 ranges. Trusted only
# with NOTCH_DASH_TAILNET=1, on a machine where the dashboard is reachable by nothing else.
TAILNET = ("100.64.0.0/10", "fd7a:115c:a1e0::/48")


def _hosts(port, extra=()):
    return ("dash.notch.localhost", f"127.0.0.1:{port}", f"localhost:{port}", *extra)


@dataclass(frozen=True)
class Settings:
    db: str | None = None
    audio_dir: str | None = None
    metrics: str | None = None
    caddy_log: str | None = None
    server_log: str | None = None
    tunnel_log: str | None = None
    tunnel_metrics: str | None = None  # host:port
    gate_secret_file: str | None = None
    device: str | None = None
    openrouter_key: str | None = field(default=None, repr=False)
    notch_port: int = config.PORT
    port: int = PORT
    allowed_hosts: tuple = _hosts(PORT)  # the Host header a request must carry (DNS rebinding)
    trusted_forwarders: tuple = ()        # networks, besides loopback, X-Forwarded-For may name
    bind: str = "127.0.0.1"               # the interface uvicorn listens on
    behind_auth_proxy: bool = False       # a proxy that authenticates every request stands in front
    meter_db: str | None = None           # the /v2 meter, for the fleet's usage (usage.py); off if unset
    home: str = "harness"                 # what `/` shows: "harness" (this stack) or "usage" (the fleet)

    @classmethod
    def from_env(cls, environ=os.environ):
        def opt(name, default=None):
            return environ.get(name, default) or None

        db, port = opt("NOTCH_DB", config.DB_PATH), int(environ.get("NOTCH_DASH_PORT") or PORT)
        return cls(db=db, audio_dir=opt("NOTCH_AUDIO_DIR", config.AUDIO_DIR),
                   metrics=opt("NOTCH_METRICS", db and os.path.splitext(db)[0] + "-metrics.jsonl"),
                   caddy_log=opt("NOTCH_DASH_CADDY_LOG", CADDY_LOG), server_log=opt("NOTCH_DASH_SERVER_LOG"),
                   tunnel_log=opt("NOTCH_DASH_TUNNEL_LOG"),
                   tunnel_metrics=opt("NOTCH_DASH_TUNNEL_METRICS", TUNNEL_METRICS),
                   gate_secret_file=opt("NOTCH_DASH_GATE_SECRET_FILE"), device=opt("NOTCH_DASH_DEVICE", DEVICE),
                   openrouter_key=(environ.get("OPENROUTER_API_KEY") or "").strip() or None,
                   notch_port=int(environ.get("NOTCH_PORT") or config.PORT), port=port,
                   allowed_hosts=_hosts(port, [h.strip() for h in (environ.get("NOTCH_DASH_HOSTS") or "").split(",")
                                               if h.strip()]),
                   trusted_forwarders=TAILNET if environ.get("NOTCH_DASH_TAILNET") == "1" else (),
                   bind=environ.get("NOTCH_DASH_BIND") or "127.0.0.1",
                   behind_auth_proxy=environ.get("NOTCH_DASH_AUTH_PROXY") == "1",
                   meter_db=opt("NOTCH_DASH_METER_DB"),
                   home="usage" if environ.get("NOTCH_DASH_HOME") == "usage" else "harness")
