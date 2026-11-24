"""Exercise Skyport auth and rate limiting with an in-memory HTTP session."""

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.daikinone.client.models import DaikinUserCredentials
from custom_components.daikinone.client.transport import DaikinTransport
from custom_components.daikinone.client.wire import (
    DAIKIN_API_URL_DEVICE_DATA,
    DAIKIN_API_URL_LOGIN,
    DAIKIN_API_URL_REFRESH_TOKEN,
)
from custom_components.daikinone.exceptions import DaikinServiceException


class Response:
    def __init__(self, status: int, payload: Any = None, retry_after: str | None = None):
        self.status = status
        self.payload = payload
        self.headers = {"Retry-After": retry_after} if retry_after else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def json(self):
        await asyncio.sleep(0)
        return self.payload


def make_transport(session):
    return DaikinTransport(DaikinUserCredentials("fixture@example.com", "fixture-password"), session)


async def test_concurrent_expired_token_requests_share_one_refresh():
    session = MagicMock()
    refreshes = []

    def request(method, url, *, headers, **kwargs):
        if url == DAIKIN_API_URL_LOGIN:
            return Response(200, {"accessToken": "expired", "refreshToken": "refresh"})
        if url == DAIKIN_API_URL_REFRESH_TOKEN:
            refreshes.append(kwargs["json"]["refreshToken"])
            return Response(200, {"accessToken": "fresh", "refreshToken": "rotated"})
        if headers["Authorization"] == "Bearer expired":
            return Response(401)
        return Response(200, [])

    session.request.side_effect = request
    transport = make_transport(session)

    assert await asyncio.gather(*(transport.request(DAIKIN_API_URL_DEVICE_DATA) for _ in range(3))) == [[], [], []]
    assert refreshes == ["refresh"]


async def test_rejected_refresh_falls_back_to_login_and_replays_once():
    session = MagicMock()
    session.request.side_effect = [
        Response(200, {"accessToken": "old", "refreshToken": "old-refresh"}),
        Response(403),
        Response(401),
        Response(200, {"accessToken": "new", "refreshToken": "new-refresh"}),
        Response(204),
    ]

    assert await make_transport(session).request(DAIKIN_API_URL_DEVICE_DATA, "PUT", {"mode": 1}) is None
    assert [call.args[:2] for call in session.request.call_args_list] == [
        ("POST", DAIKIN_API_URL_LOGIN),
        ("PUT", DAIKIN_API_URL_DEVICE_DATA),
        ("POST", DAIKIN_API_URL_REFRESH_TOKEN),
        ("POST", DAIKIN_API_URL_LOGIN),
        ("PUT", DAIKIN_API_URL_DEVICE_DATA),
    ]
    assert all(call.kwargs["allow_redirects"] is False for call in session.request.call_args_list)


async def test_rate_limit_defers_followup_requests(monkeypatch):
    now = 10
    monkeypatch.setattr("custom_components.daikinone.client.transport.monotonic", lambda: now)
    session = MagicMock()
    session.request.side_effect = [
        Response(200, {"accessToken": "access", "refreshToken": "refresh"}),
        Response(429, retry_after="45"),
        Response(200, []),
    ]
    transport = make_transport(session)

    with pytest.raises(DaikinServiceException) as limited:
        await transport.request(DAIKIN_API_URL_DEVICE_DATA)
    assert limited.value.status == 429
    now = 54
    with pytest.raises(DaikinServiceException, match="deferred"):
        await transport.request(DAIKIN_API_URL_DEVICE_DATA)
    assert session.request.call_count == 2
    now = 55
    assert await transport.request(DAIKIN_API_URL_DEVICE_DATA) == []


async def test_rejected_or_malformed_login_is_classified_without_logging_payload():
    session = MagicMock()
    session.request.side_effect = [Response(400), Response(200, {"accessToken": "sensitive"})]
    transport = make_transport(session)

    assert not await transport.login()
    with pytest.raises(DaikinServiceException, match="did not return a refresh token") as malformed:
        await transport.login()
    assert "sensitive" not in str(malformed.value)
