"""
python -m notch_api — serve the API on 127.0.0.1:4131 (api.notch.localhost through Caddy on
a Mac, api.trynotch.xyz through Caddy on the VPS).

Run it from the repo root. The environment chooses everything else:

  NOTCH_ENV          prod on the VPS: /v2 only, and it refuses to start without its
                     settings (services.py lists them); anything else is development,
                     with the /v1 harness mounted beside /v2
  NOTCH_HOST         interface, default 127.0.0.1; 0.0.0.0 lets a phone on the same
                     Wi-Fi reach it (and anyone else on that network)
  NOTCH_PORT         port, default 4131
  NOTCH_DB           /v1's SQLite file, default data/notch_api.db (not used in prod)
  NOTCH_AUDIO_DIR    /v1's stored uploads, default data/audio (not used in prod)
  NOTCH_METER_DB     /v2's metering database, default data/meter.db
  NOTCH_TMP          where /v2's ffmpeg directories go (a tmpfs in prod)
  NOTCH_METRICS      one JSON line per model call, for notch_dash; on by default in
                     development (the DB path with -metrics.jsonl), off in prod unless set
  NOTCH_FAKE_MODELS  1 = the offline doubles from fakes.py instead of OpenRouter and
                     ffmpeg, so the whole server runs with no key and no network
  NOTCH_DEV_AUTH     1 = `Bearer dev` works on /v2 too (refused in prod)
  OPENROUTER_API_KEY read on the first model call, not at boot

Logs are scrubbed JSON lines on stderr (privacy.py): one per request, nothing a person
said in any of them, and no uvicorn access log.
"""

import logging
import os

import uvicorn

from . import audio, config, metrics, privacy
from .app import create_app
from .fakes import FakeAudio, FakeClient, fake_transcode
from .services import Services


def main(environ=os.environ):
    privacy.install_logging()
    prod = environ.get("NOTCH_ENV") == "prod"
    fake = environ.get("NOTCH_FAKE_MODELS") == "1"
    if prod and fake:
        raise SystemExit("NOTCH_FAKE_MODELS=1 is refused when NOTCH_ENV=prod.")
    if not prod or environ.get("NOTCH_METRICS"):
        metrics.path = config.METRICS_PATH  # FakeClient never reaches OpenRouterClient._post, so fakes record nothing
    client = FakeClient() if fake else None
    if fake:
        logging.getLogger(__name__).info("NOTCH_FAKE_MODELS=1: offline fake models, no OpenRouter calls")
    services = Services.from_env(environ, client=client, audio=FakeAudio() if fake else None)
    app = create_app(db_path=config.DB_PATH, audio_dir=config.AUDIO_DIR, client=client,
                     transcode=fake_transcode if fake else audio.to_m4a_16k, services=services, v1=not prod)
    uvicorn.run(app, host=config.HOST, port=config.PORT, access_log=False, log_config=None, server_header=False)


if __name__ == "__main__":
    main()
