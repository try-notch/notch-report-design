"""
config.py — the server's settings, in one place.

Everything that differs between a laptop run, the test suite and the E2E run is
an environment variable with a default here, so nothing else reads os.environ
for configuration. The one exception is the OpenRouter key: it is read lazily by
OpenRouterClient.from_env(), so the server can boot without one (a job then
fails `model_unavailable`) and tests can remove it per test.
"""

import os

from dotenv import load_dotenv

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# An exported variable wins over the file, same as the demo CLI.
load_dotenv(os.path.join(REPO_ROOT, ".env"))

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
# Chat (the capture-time narrative and the report). It must be sent with reasoning
# off: with its default reasoning it ignores a forced tool_choice (live probe, Sep 24).
CHAT_MODEL = "deepseek/deepseek-v4-pro-0813"
STT_MODEL = "openai/whisper-large-v3"
# E2E fixtures only. OpenRouter has no openai/gpt-4o-mini-tts; Deepgram wants its long voice names.
TTS_MODEL = "deepgram/aura-2"
TTS_VOICE = "aura-2-thalia-en"

# Typed decisions (the five categories, mood, project match). Not under /api/v1.
JEV_MODEL = "typesafe/jev-1.13"
DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
# Tuned on the 52 seed_db hand labels: 58-61% exact match leave-one-out, against 48% at a flat 0.5.
CATEGORY_THRESHOLDS = {"wins": 0.60, "collaboration": 0.50, "leadership": 0.40, "growth": 0.65, "challenges": 0.75}
PROJECT_CONFIDENCE = 0.5   # below this, a project choice leaves the notch unassigned

DB_PATH = os.environ.get("NOTCH_DB") or os.path.join(REPO_ROOT, "data", "notch_api.db")
AUDIO_DIR = os.environ.get("NOTCH_AUDIO_DIR") or os.path.join(REPO_ROOT, "data", "audio")
PORT = int(os.environ.get("NOTCH_PORT") or 4131)  # api.notch.localhost via Caddy
# 127.0.0.1 keeps the development bearer off the network. A phone on the same Wi-Fi
# needs NOTCH_HOST=0.0.0.0, which opens it to the whole LAN while it runs.
HOST = os.environ.get("NOTCH_HOST") or "127.0.0.1"

# Auth is stubbed: one bearer token, one user. JWT verification is out of scope.
DEV_TOKEN = "dev"
DEV_USER_ID = "00000000-0000-4000-8000-000000000001"

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # iOS §5 "Request size", and OpenRouter's own cap
AUDIO_RETENTION_DAYS = 7  # E3; schema.sql's purge_after trigger carries the same number
