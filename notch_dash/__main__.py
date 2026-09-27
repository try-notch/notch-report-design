"""
python -m notch_dash — serve the dashboard on 127.0.0.1:4130 (http://dash.notch.localhost
through Caddy; NOTCH_DASH_BIND moves it, as the VPS's container does). Every setting is an
environment variable; see DASHBOARD.md › Environment.
"""

import os

import uvicorn

from .app import create_app
from .settings import Settings


def main():
    settings = Settings.from_env(os.environ)
    uvicorn.run(create_app(settings), host=settings.bind, port=settings.port, access_log=False)


if __name__ == "__main__":
    main()
