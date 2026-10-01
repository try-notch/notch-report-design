"""
The usage page end to end, inside the suite: e2e/dash_usage.py's first pass, without the
screenshots or the second pass through the entry points. The API and the dashboard are served
over HTTP on one meter, the first week of real use is played through them, and /api/usage and
the page (in a headless Chrome, when one is installed) are held to what the phones were
answered. Run the script itself for the whole thing and its report.

conftest.py keeps every test offline by making httpx's real transport refuse. This one talks to
its own two servers, so it lets 127.0.0.1 through and still refuses anywhere else.
"""

import importlib.util
import pathlib

import httpx

_SEND = httpx.HTTPTransport.handle_request   # taken at import, before conftest's _offline replaces it


def test_the_usage_page_shows_what_the_phones_were_answered(tmp_path, monkeypatch):
    def loopback_only(self, request):
        if request.url.host != "127.0.0.1":
            raise RuntimeError(f"tests are offline; refused {request.method} {request.url}")
        return _SEND(self, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", loopback_only)
    path = pathlib.Path(__file__).parents[1] / "e2e" / "dash_usage.py"
    spec = importlib.util.spec_from_file_location("dash_usage_e2e", path)
    e2e = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(e2e)
    check = e2e.run(str(tmp_path), quick=True)
    assert check.failed == [], check.failed
