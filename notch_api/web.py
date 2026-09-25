"""
web.py — what every route module shares: the bearer check, capped JSON bodies, and
the only two ways out (a validated contract kind, or the error envelope).

app.py and the route modules it registers (record_routes, account_routes) all read
requests and write responses through here, so "every 2xx is a contract kind" and
"every refusal is the envelope" hold for a route wherever it lives.
"""

from fastapi import Depends, Request
from fastapi.responses import JSONResponse

from . import config, contract
from .store import ApiError

JSON_BODY_LIMIT = 64 * 1024        # POST /v1/projects, /v1/reports, PATCH /v1/me: a few hundred bytes
TEXT_BODY_LIMIT = 1024 * 1024      # a transcript: an 80-minute recording is ~70 KB of text


def bad(message):
    return ApiError("invalid_request", 400, message)


def too_large(limit):
    return ApiError("payload_too_large", 413, f"Over the {limit}-byte limit.")


def envelope(status, code, message, retryable=False, headers=None):
    body = {"error": {"code": code, "message": message, "retryable": retryable}}
    contract.validate("error", body)
    return JSONResponse(body, status_code=status, headers=headers)


def reply(kind, body, status=200):
    """Every 2xx body goes through here: it leaves as `kind` or not at all."""
    contract.validate(kind, body)
    return JSONResponse(body, status_code=status)


async def user(request: Request):
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or token.strip() != config.DEV_TOKEN:
        raise ApiError("unauthorized", 401, "A valid bearer token is required.")
    return config.DEV_USER_ID


def capped(request, limit):
    """The same request, except that reading more than `limit` body bytes raises 413."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise too_large(limit)
    seen = 0

    async def receive():
        nonlocal seen
        message = await request.receive()
        seen += len(message.get("body", b""))
        if seen > limit:
            raise too_large(limit)
        return message

    return Request(request.scope, receive)


def json_body(limit=JSON_BODY_LIMIT):
    """A dependency: the JSON body, read only after auth passes and capped at `limit`. Its shape is the route's to check."""
    async def read(request: Request, _=Depends(user)):
        try:
            return await capped(request, limit).json()
        except (ValueError, RecursionError):
            raise bad("The body must be JSON.") from None
    return read


def json_object(value, keys, name="body"):
    """`value` as a dict whose keys are all in `keys`, else 400 naming `name` and the stray key."""
    if not isinstance(value, dict):
        raise bad(f"{name} must be a JSON object.")
    stray = sorted(set(value) - set(keys))
    if stray:
        raise bad(f"{name} has an unknown field: {stray[0]}.")
    return value
