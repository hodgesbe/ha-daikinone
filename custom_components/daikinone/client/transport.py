"""Authenticated HTTP requests to Daikin Skyport."""

import asyncio
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import monotonic
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from custom_components.daikinone.client.models import DaikinUserCredentials
from custom_components.daikinone.client.wire import DAIKIN_API_URL_LOGIN, DAIKIN_API_URL_REFRESH_TOKEN
from custom_components.daikinone.exceptions import DaikinAuthError, DaikinServiceException

_AUTH_STATUSES = (401, 403)
_TIMEOUT = ClientTimeout(total=8, connect=4)


class DaikinTransport:
    """Handle Skyport authentication and HTTP requests for one account."""

    def __init__(self, creds: DaikinUserCredentials, session: ClientSession | None = None) -> None:
        self._creds = creds
        self._session = session
        self._auth_lock = asyncio.Lock()
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._retry_at = 0.0

    async def login(self) -> bool:
        """Check the provided credentials, distinguishing connectivity failures."""
        async with self._auth_lock:
            try:
                await self._login()
            except DaikinAuthError:
                return False
            return True

    async def _login(self) -> None:
        self._access_token = self._refresh_token = None
        status, payload = await self._send(
            "POST", DAIKIN_API_URL_LOGIN, {"email": self._creds.email, "password": self._creds.password}
        )
        if status in (400, *_AUTH_STATUSES):
            raise DaikinAuthError("Daikin rejected the login", status)
        self._access_token, self._refresh_token = self._read_tokens(payload, require_refresh=True)

    async def _token(self, rejected: str | None = None) -> str:
        async with self._auth_lock:
            if self._access_token is not None and self._access_token != rejected:
                return self._access_token
            if self._refresh_token is not None:
                status, payload = await self._send(
                    "POST",
                    DAIKIN_API_URL_REFRESH_TOKEN,
                    {"email": self._creds.email, "refreshToken": self._refresh_token},
                )
                if status not in (400, *_AUTH_STATUSES):
                    access, refresh = self._read_tokens(payload, require_refresh=False)
                    self._access_token = access
                    if refresh is not None:
                        self._refresh_token = refresh
                    return access
            await self._login()
            assert self._access_token is not None
            return self._access_token

    @staticmethod
    def _read_tokens(payload: Any, *, require_refresh: bool) -> tuple[str, str | None]:
        if not isinstance(payload, dict):
            raise DaikinServiceException("Unexpected authentication response from Daikin")
        access, refresh = payload.get("accessToken"), payload.get("refreshToken")
        if not isinstance(access, str) or not access:
            raise DaikinServiceException("Daikin did not return an access token")
        if not isinstance(refresh, str) or not refresh:
            if require_refresh:
                raise DaikinServiceException("Daikin did not return a refresh token")
            refresh = None
        return access, refresh

    async def request(self, url: str, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
        token = await self._token()
        status, payload = await self._send(method, url, body, token)
        if status in _AUTH_STATUSES:
            token = await self._token(rejected=token)
            status, payload = await self._send(method, url, body, token)
        if status in _AUTH_STATUSES:
            raise DaikinAuthError("Daikin denied access", status)
        if status >= 300:
            raise DaikinServiceException("Daikin rejected the device request", status)
        return payload

    async def _send(
        self, method: str, url: str, body: dict[str, Any] | None, token: str | None = None
    ) -> tuple[int, Any]:
        if monotonic() < self._retry_at:
            raise DaikinServiceException("Daikin request deferred due to rate limiting", 429)
        if self._session is not None:
            return await self._send_with_session(self._session, method, url, body, token)
        async with ClientSession() as session:
            return await self._send_with_session(session, method, url, body, token)

    async def _send_with_session(
        self, session: ClientSession, method: str, url: str, body: dict[str, Any] | None, token: str | None
    ) -> tuple[int, Any]:
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with session.request(
                method, url, json=body, headers=headers, timeout=_TIMEOUT, allow_redirects=False
            ) as response:
                status = response.status
                if status == 429:
                    self._retry_at = max(
                        self._retry_at, monotonic() + self._retry_delay(response.headers.get("Retry-After"))
                    )
                    raise DaikinServiceException("Daikin rate limited requests", status)
                if status == 204 or (method == "PUT" and 200 <= status < 300):
                    return status, None
                if 200 <= status < 300:
                    return status, await response.json()
                if status in (400, *_AUTH_STATUSES):
                    return status, None
                raise DaikinServiceException("Unexpected HTTP status from Daikin", status)
        except ClientError, TimeoutError, ValueError:
            raise DaikinServiceException("Could not complete the Daikin request") from None

    @staticmethod
    def _retry_delay(value: str | None) -> float:
        if value:
            try:
                return max(1, int(value))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(value)
                    return max(1, (retry_at - datetime.now(UTC)).total_seconds())
                except TypeError, ValueError, OverflowError:
                    pass
        return 30
