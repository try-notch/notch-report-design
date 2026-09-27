"""Assertions on what came back over HTTP, shared by the route tests: status, then the contract kind."""

from notch_api import contract


def ok(response, status, kind):
    assert response.status_code == status, response.text
    body = response.json()
    contract.validate(kind, body)
    return body


def refused(response, status, code):
    assert response.status_code == status, response.text
    body = response.json()
    contract.validate("error", body)
    assert body["error"]["code"] == code
    return body
