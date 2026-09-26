"""
cloud.py — Notch Cloud: opaque, end-to-end encrypted records for people who turn it on.

  PUT    /v2/cloud/records                 {records: [{id, deleted, ciphertext, key_id}]} -> {cursor}
  GET    /v2/cloud/changes?since=&limit=   {records: [{id, deleted, ciphertext, key_id, seq}], cursor, more}
  GET    /v2/cloud/keycheck                {key_id, verifier}, or 404 before Notch Cloud is set up
  PUT    /v2/cloud/keycheck                {key_id, verifier} -> 204
  DELETE /v2/cloud                         -> 204: every record and the keycheck wiped

THE SERVER HOLDS CIPHERTEXT AND NOTHING ELSE. The phone seals each record with AES-GCM
under a data key that never leaves the user's devices (the record id is the associated
data); the kind, fields and timestamps are inside the ciphertext. What the server can see
is the record count, sizes and write times.

SEQUENCE, NOT CLOCKS. Every accepted record gets the next number in its account's
sequence (accounts.cloud_seq) and the newest write of an id wins; a device pulls with
the last `seq` it saw as `since`. A page stops at `limit` records or `cloud.page_bytes`
of ciphertext, whichever comes first, and says `more`. Wiping keeps the sequence, so no
cursor ever goes backwards.

LIMITS (remote config `cloud`): 500 records and a 4 MB body per write, 256 KB per
record once decoded (413 cloud_record_too_large), 100 MB per account (413
cloud_quota_exceeded, for the whole batch). Writes need `features.notch_cloud` and an
unblocked account; reading, the keycheck and turning it off always work.
"""

import base64
import binascii
import re

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from . import wire_v2
from .privacy import request_entry
from .v2 import _fields, parse_json, read_body
from .wire_v2 import UUID, Refusal, bad

KEY_ID = re.compile(r"[0-9a-fA-F]{16}")
MAX_VERIFIER_BYTES = 4096
MAX_SINCE = 2 ** 53


def _decode(value, name, limit, code):
    """Strict base64 -> bytes, at most `limit` of them, else `code` (a 413)."""
    if not isinstance(value, str) or not value:
        raise bad(f"{name} must be base64.")
    if len(value) > (limit + 2) // 3 * 4 + 4:
        raise Refusal(code)
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise bad(f"{name} must be base64.") from None
    if len(data) > limit:
        raise Refusal(code)
    return data


def _key_id(value, name="key_id"):
    if not isinstance(value, str) or not KEY_ID.fullmatch(value):
        raise bad(f"{name} must be 16 hex digits.")
    return value.lower()


def _record(raw, max_bytes):
    record = _fields(raw, "record", ("id", "deleted", "ciphertext", "key_id"))
    if not isinstance(record["id"], str) or not UUID.fullmatch(record["id"]):
        raise bad("record.id must be a UUID.")
    if not isinstance(record["deleted"], bool):
        raise bad("record.deleted must be true or false.")
    ciphertext = record["ciphertext"]
    if ciphertext is None:
        if not record["deleted"]:
            raise bad("record.ciphertext may be null only when deleted is true.")
    else:
        ciphertext = _decode(ciphertext, "record.ciphertext", max_bytes, "cloud_record_too_large")
    return {"id": record["id"].lower(), "deleted": record["deleted"], "ciphertext": ciphertext,
            "key_id": _key_id(record["key_id"], "record.key_id")}


def _query_int(request, name, default, lowest, highest):
    value = request.query_params.get(name)
    if value is None:
        return default
    if not value.isdigit() or not lowest <= int(value) <= highest:
        raise bad(f"{name} must be a whole number from {lowest} to {highest}.")
    return int(value)


class Cloud:
    def __init__(self, v2):
        self.v2, self.s = v2, v2.s

    async def _caller(self, request, *, write):
        caller = await self.v2.caller(request)
        request_entry(request.scope)["kind"] = "cloud"
        if write:
            if caller.account.blocked:
                raise Refusal("account_blocked")
            if not caller.features["notch_cloud"]:
                raise Refusal("feature_disabled", retry_after=3600)
        return caller

    async def put_records(self, request: Request):
        caller = await self._caller(request, write=True)
        limits = caller.cfg["cloud"]
        raw = await read_body(request, limits["max_body_bytes"])
        request_entry(request.scope)["request_bytes"] = len(raw)
        body = _fields(parse_json(raw), "body", ("records",))
        if not isinstance(body["records"], list):
            raise bad("records must be a list.")
        if len(body["records"]) > limits["max_records"]:
            raise Refusal("payload_too_large")
        records = [_record(r, limits["max_record_bytes"]) for r in body["records"]]
        request_entry(request.scope)["count"] = len(records)
        cursor = await run_in_threadpool(self.s.meter.cloud_put, caller.user_id, records,
                                         account_bytes=limits["account_bytes"])
        return self._reply("cloud_put", {"cursor": cursor}, caller)

    async def changes(self, request: Request):
        caller = await self._caller(request, write=False)
        limits = caller.cfg["cloud"]
        since = _query_int(request, "since", 0, 0, MAX_SINCE)
        limit = _query_int(request, "limit", limits["max_records"], 1, limits["max_records"])
        page, cursor, more = await run_in_threadpool(self.s.meter.cloud_changes, caller.user_id, since, limit,
                                                     limits["page_bytes"])
        request_entry(request.scope)["count"] = len(page)
        records = [{"id": r["record_id"], "deleted": bool(r["deleted"]),
                    "ciphertext": base64.b64encode(r["ciphertext"]).decode("ascii") if r["ciphertext"] is not None
                    else None, "key_id": r["key_id"], "seq": r["seq"]} for r in page]
        return self._reply("cloud_changes", {"records": records, "cursor": cursor, "more": more}, caller)

    async def get_keycheck(self, request: Request):
        caller = await self._caller(request, write=False)
        found = await run_in_threadpool(self.s.meter.keycheck, caller.user_id)
        if found is None:
            raise Refusal("not_found", "Notch Cloud has not been set up for this account.")
        body = {"key_id": found["key_id"], "verifier": base64.b64encode(found["verifier"]).decode("ascii")}
        return self._reply("keycheck", body, caller)

    async def put_keycheck(self, request: Request):
        caller = await self._caller(request, write=True)
        raw = await read_body(request, 16 * 1024)
        body = _fields(parse_json(raw), "body", ("key_id", "verifier"))
        verifier = _decode(body["verifier"], "verifier", MAX_VERIFIER_BYTES, "payload_too_large")
        await run_in_threadpool(self.s.meter.set_keycheck, caller.user_id, _key_id(body["key_id"]), verifier)
        return Response(status_code=204)

    async def wipe(self, request: Request):
        caller = await self._caller(request, write=False)
        await run_in_threadpool(self.s.meter.cloud_wipe, caller.user_id)
        return Response(status_code=204)

    @staticmethod
    def _reply(kind, body, caller):
        wire_v2.validate(kind, body, caller.client)
        return JSONResponse(body)


def register(app, v2):
    cloud = Cloud(v2)
    app.add_api_route("/v2/cloud/records", cloud.put_records, methods=["PUT"])
    app.add_api_route("/v2/cloud/changes", cloud.changes, methods=["GET"])
    app.add_api_route("/v2/cloud/keycheck", cloud.get_keycheck, methods=["GET"])
    app.add_api_route("/v2/cloud/keycheck", cloud.put_keycheck, methods=["PUT"])
    app.add_api_route("/v2/cloud", cloud.wipe, methods=["DELETE"])
    return cloud
