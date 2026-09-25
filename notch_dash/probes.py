"""
probes.py — one function per thing notch_dash asks: the local server, cloudflared, the
public tunnel and the gate, OpenRouter's spend, devicectl and the audio dir. Each takes its
HTTP client or `run` injected, returns a plain dict of only the fields the page shows, and
words its own failures (Unreadable): a probe URL can hold the gate secret, so an
exception's text never leaves this module.
"""

import json
import os
import re
import subprocess
import tempfile
import time

import httpx

from .live import Unreadable, describe
from .logs import OWN_UA, redact

UA = {"User-Agent": OWN_UA}
SECRET = re.compile(r"[0-9a-f]{48}")
APPLE_EPOCH = 978307200  # 2001-01-01 in Unix seconds; devicectl counts from there


def _get(http, url, timeout, headers=UA):
    """GET -> (response, latency_ms), or Unreadable worded without the URL."""
    start = time.perf_counter()
    try:
        response = http.get(url, headers=headers, timeout=timeout)
    except httpx.TimeoutException:
        raise Unreadable(f"timed out after {timeout:g} s") from None
    except httpx.HTTPError:
        raise Unreadable("couldn’t connect") from None
    return response, round((time.perf_counter() - start) * 1000, 1)


def _json(response):
    try:
        return response.json()
    except ValueError:
        return None


def _healthy(response):
    """None for 200 {"ok": true}, else why not."""
    if response.status_code != 200:
        return f"HTTP {response.status_code}"
    return None if _json(response) == {"ok": True} else "unexpected body"


def local_health(http, port):
    """GET 127.0.0.1:<port>/healthz -> {http_status, latency_ms, error}; a failure is part of the answer."""
    try:
        response, ms = _get(http, f"http://127.0.0.1:{port}/healthz", 2)
    except Unreadable as exc:
        return {"http_status": None, "latency_ms": None, "error": str(exc)}
    return {"http_status": response.status_code, "latency_ms": ms, "error": _healthy(response)}


_SAMPLE = re.compile(r"^([a-zA-Z_:][\w:]*)(?:\{(.*)\})?\s+(\S+)", re.M)
_LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def cloudflared(http, where):
    """cloudflared's /ready and /metrics -> the numbers the tunnel check shows."""
    ready, _ = _get(http, f"http://{where}/ready", 2)
    metrics, _ = _get(http, f"http://{where}/metrics", 2)
    if metrics.status_code != 200:
        raise Unreadable(f"HTTP {metrics.status_code}")
    out = {"ready_connections": None, "edge": None, "rtt_ms": None, "version": None, "requests_total": None,
           "request_errors": None, "host": None}
    ha = None
    for name, labels, value in _SAMPLE.findall(metrics.text):
        try:
            labels, value = dict(_LABEL.findall(labels)), float(value)
        except ValueError:
            continue
        if name == "cloudflared_tunnel_server_locations" and value > 0:
            out["edge"] = out["edge"] or redact(labels.get("edge_location", ""), 32) or None
        elif name == "quic_client_smoothed_rtt" and out["rtt_ms"] is None:
            out["rtt_ms"] = value
        elif name == "build_info":
            out["version"] = redact(labels.get("version", ""), 32) or None
        elif name == "cloudflared_tunnel_total_requests":
            out["requests_total"] = int(value)
        elif name == "cloudflared_tunnel_request_errors":
            out["request_errors"] = int(value)
        elif name == "cloudflared_tunnel_user_hostnames_counts" and value > 0:
            out["host"] = redact(labels.get("userHostname", "").removeprefix("https://"), 200) or None
        elif name == "cloudflared_tunnel_ha_connections":
            ha = int(value)
    connections = (_json(ready) or {}).get("readyConnections")
    out["ready_connections"] = connections if type(connections) is int else ha
    if out["ready_connections"] is None:
        raise Unreadable("unexpected answer")
    return out


def read_secret(path):
    """The gate secret, read fresh each probe; Unreadable unless it is exactly 48 lowercase hex."""
    try:
        with open(path) as f:
            secret = f.read().strip()
    except OSError as exc:
        raise Unreadable(describe(exc)) from None
    if not SECRET.fullmatch(secret):
        raise Unreadable("it doesn’t hold a 48-character hex secret")
    return secret


def end_to_end(http, host, secret_path):
    """GET https://<host>/<secret>/healthz, the phone's way in -> {ok, http_status, latency_ms, error}, or None."""
    secret = read_secret(secret_path)
    if not host:
        return None
    try:
        response, ms = _get(http, f"https://{host}/{secret}/healthz", 10)
    except Unreadable as exc:
        return {"ok": False, "http_status": None, "latency_ms": None, "error": str(exc)}
    error = _healthy(response)
    return {"ok": error is None, "http_status": response.status_code, "latency_ms": ms, "error": error}


def gate_integrity(http, host):
    """GET https://<host>/healthz and /docs without the secret: both must be 404. None without a host."""
    if not host:
        return None
    out, errors = {}, []
    for name in ("healthz", "docs"):
        try:
            out[name] = _get(http, f"https://{host}/{name}", 10)[0].status_code
        except Unreadable as exc:
            out[name] = None
            errors.append(f"/{name} {exc}")
    return out | {"open": any(code is not None and 200 <= code < 300 for code in out.values()),
                  "error": "; ".join(errors) or None}


def _usd(value):
    return float(value) if type(value) in (int, float) else None


def openrouter_spend(http, key):
    """GET openrouter.ai/api/v1/key -> spend and limit only; never the key's label or ids."""
    response, _ = _get(http, "https://openrouter.ai/api/v1/key", 10, UA | {"Authorization": f"Bearer {key}"})
    if response.status_code != 200:
        raise Unreadable(f"HTTP {response.status_code}")
    data = (_json(response) or {}).get("data")
    if not isinstance(data, dict):
        raise Unreadable("unexpected answer")
    return {"limit_usd": _usd(data.get("limit")), "remaining_usd": _usd(data.get("limit_remaining")),
            "today_usd": _usd(data.get("usage_daily")), "week_usd": _usd(data.get("usage_weekly")),
            "month_usd": _usd(data.get("usage_monthly")), "total_usd": _usd(data.get("usage")),
            "free_tier": data.get("is_free_tier") is True}


def device(run, udid):
    """`xcrun devicectl list devices` -> the phone with this hardware UDID, or None when it isn't listed."""
    with tempfile.TemporaryDirectory(prefix="notch-dash-") as tmp:
        out = os.path.join(tmp, "devices.json")
        try:
            run(["xcrun", "devicectl", "list", "devices", "--json-output", out], capture_output=True, timeout=15)
            with open(out) as f:
                devices = json.load(f)["result"]["devices"]
        except subprocess.TimeoutExpired:
            raise Unreadable("timed out after 15 s") from None
        except (OSError, ValueError, KeyError, TypeError):
            raise Unreadable("devicectl gave no answer") from None
    for found in devices:
        p = found.get("properties") if isinstance(found, dict) else None
        if not isinstance(p, dict) or (p.get("hardware") or {}).get("udid") != udid:
            continue
        connection, last = p.get("connection") or {}, (p.get("connection") or {}).get("lastConnectionDate")
        return {"name": _str((p.get("state") or {}).get("name")), "model": _str(p["hardware"].get("marketingName")),
                "os": _str(((p.get("software") or {}).get("osVersionNumber") or {}).get("stringValue")),
                "connection": _str(connection.get("state")), "pairing": _str(connection.get("pairingState")),
                "transport": _str(connection.get("transportType")),
                "last_connected_at": float(last + APPLE_EPOCH) if type(last) in (int, float) else None}
    return None


def _str(value):
    return redact(value, 100) if isinstance(value, str) else None


def audio_size(path):
    """Total bytes and files under the audio dir."""
    if not os.path.isdir(path):
        raise Unreadable("it isn’t there")
    total = files = 0
    for root, _, names in os.walk(path):
        for name in names:
            try:
                total += os.stat(os.path.join(root, name)).st_size
                files += 1
            except OSError:
                pass
    return {"bytes": total, "files": files}
