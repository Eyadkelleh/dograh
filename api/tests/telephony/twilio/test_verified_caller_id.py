from unittest.mock import patch

import aiohttp
import pytest

from api.services.telephony.providers.twilio.provider import TwilioProvider

NUMBER = "+15551230002"
BASE = "https://api.twilio.com/2010-04-01/Accounts/AC123"
INCOMING_LIST = f"{BASE}/IncomingPhoneNumbers.json"
CALLER_IDS = f"{BASE}/OutgoingCallerIds.json"
NOT_OWNED = (
    f"Phone number {NUMBER} is not owned by this Twilio account (AC123). "
    "Add it in the Twilio console first."
)


class _StubResponse:
    def __init__(self, status: int, *, data=None, body: str = ""):
        self.status = status
        self._data = data
        self._body = body

    async def json(self):
        return self._data

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _StubSession:
    """Serves responses keyed by URL and records every request across sessions."""

    def __init__(self, routes: dict, requests: list):
        self.routes = routes
        self.requests = requests

    def _respond(self, method: str, url: str, kwargs: dict):
        self.requests.append((method, url, kwargs))
        response = self.routes[(method, url)]
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url: str, **kwargs):
        return self._respond("GET", url, kwargs)

    def post(self, url: str, **kwargs):
        return self._respond("POST", url, kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _provider() -> TwilioProvider:
    return TwilioProvider(
        {"account_sid": "AC123", "auth_token": "token", "from_numbers": []}
    )


def _patch_twilio(routes: dict, requests: list):
    return patch(
        "api.services.telephony.providers.twilio.provider.aiohttp.ClientSession",
        side_effect=lambda *a, **kw: _StubSession(routes, requests),
    )


def _incoming(numbers: list[dict]) -> _StubResponse:
    return _StubResponse(200, data={"incoming_phone_numbers": numbers})


def _caller_ids(entries: list[dict]) -> _StubResponse:
    return _StubResponse(200, data={"outgoing_caller_ids": entries})


@pytest.mark.asyncio
async def test_owned_number_is_accepted_without_caller_id_lookup():
    requests: list = []
    routes = {
        ("GET", INCOMING_LIST): _incoming([{"sid": "PN1", "phone_number": NUMBER}])
    }

    with _patch_twilio(routes, requests):
        result = await _provider().validate_phone_number(NUMBER)

    assert result.ok
    assert [url for _, url, _ in requests] == [INCOMING_LIST]


@pytest.mark.asyncio
async def test_verified_caller_id_only_number_is_accepted():
    requests: list = []
    routes = {
        ("GET", INCOMING_LIST): _incoming([]),
        ("GET", CALLER_IDS): _caller_ids(
            [{"sid": "PNVERIFIED", "phone_number": NUMBER}]
        ),
    }

    with _patch_twilio(routes, requests):
        result = await _provider().validate_phone_number(NUMBER)

    assert result.ok
    assert requests[1][1] == CALLER_IDS
    assert requests[1][2]["params"] == {"PhoneNumber": NUMBER}
    assert isinstance(requests[1][2]["auth"], aiohttp.BasicAuth)


@pytest.mark.asyncio
async def test_number_neither_owned_nor_verified_keeps_existing_error():
    requests: list = []
    routes = {
        ("GET", INCOMING_LIST): _incoming([]),
        ("GET", CALLER_IDS): _caller_ids([]),
    }

    with _patch_twilio(routes, requests):
        result = await _provider().validate_phone_number(NUMBER)

    assert not result.ok
    assert result.message == NOT_OWNED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "caller_id_response",
    [
        _StubResponse(500, body="internal error"),
        _StubResponse(401, body="unauthorized"),
        aiohttp.ClientConnectionError("connection reset"),
    ],
)
async def test_caller_id_lookup_error_fails_closed_with_existing_error(
    caller_id_response,
):
    requests: list = []
    routes = {
        ("GET", INCOMING_LIST): _incoming([]),
        ("GET", CALLER_IDS): caller_id_response,
    }

    with _patch_twilio(routes, requests):
        result = await _provider().validate_phone_number(NUMBER)

    assert not result.ok
    assert result.message == NOT_OWNED


@pytest.mark.asyncio
async def test_configure_inbound_skips_verified_caller_id_only_number():
    requests: list = []
    routes = {
        ("GET", INCOMING_LIST): _incoming([]),
        ("GET", CALLER_IDS): _caller_ids(
            [{"sid": "PNVERIFIED", "phone_number": NUMBER}]
        ),
    }

    with _patch_twilio(routes, requests):
        result = await _provider().configure_inbound(
            NUMBER, "https://example.com/api/v1/telephony/inbound/run"
        )

    assert result.ok
    assert all(method == "GET" for method, _, _ in requests)
    assert not any(
        url.startswith(f"{BASE}/IncomingPhoneNumbers/") for _, url, _ in requests
    )


@pytest.mark.asyncio
async def test_configure_inbound_unowned_unverified_number_keeps_existing_error():
    requests: list = []
    routes = {
        ("GET", INCOMING_LIST): _incoming([]),
        ("GET", CALLER_IDS): _caller_ids([]),
    }

    with _patch_twilio(routes, requests):
        result = await _provider().configure_inbound(
            NUMBER, "https://example.com/api/v1/telephony/inbound/run"
        )

    assert not result.ok
    assert result.message == NOT_OWNED
