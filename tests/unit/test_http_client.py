"""Unit tests for specmcp.runtime.http_client.HttpClient."""

from __future__ import annotations

import pytest
import respx
import httpx

from specmcp.config import DispatchConfig, RetryConfig
from specmcp.errors import (
    ResponseTooLargeError,
    TransientError,
    UpstreamClientError,
    UpstreamServerError,
)
from specmcp.runtime.http_client import HttpClient, HttpResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(**overrides) -> DispatchConfig:
    defaults = dict(
        default_timeout_seconds=5.0,
        per_host_concurrency=5,
        global_concurrency=10,
        response_size_limit_bytes=1_048_576,
        text_truncate_bytes=262_144,
        tls_verify=False,
    )
    defaults.update(overrides)
    return DispatchConfig(**defaults)


URL = "https://api.example.com/pets/1"


async def _get(client: HttpClient, url: str = URL, **kwargs) -> HttpResponse:
    return await client.request(
        method="GET",
        url=url,
        headers={},
        params={},
        json_body=None,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Successful requests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_successful_get_returns_body():
    respx.get(URL).mock(return_value=httpx.Response(200, text='{"id": 1}'))
    async with HttpClient(_cfg()) as client:
        resp = await _get(client)
    assert resp.status_code == 200
    assert resp.body == '{"id": 1}'
    assert resp.truncated is False


@pytest.mark.asyncio
@respx.mock
async def test_response_headers_are_returned():
    respx.get(URL).mock(return_value=httpx.Response(
        200, text="ok", headers={"X-Custom": "value"}
    ))
    async with HttpClient(_cfg()) as client:
        resp = await _get(client)
    assert resp.headers.get("x-custom") == "value"


@pytest.mark.asyncio
@respx.mock
async def test_2xx_non_200_passes_through():
    respx.get(URL).mock(return_value=httpx.Response(201, text="created"))
    async with HttpClient(_cfg()) as client:
        resp = await _get(client)
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# 4xx / 5xx
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_4xx_raises_upstream_client_error():
    respx.get(URL).mock(return_value=httpx.Response(404, text="not found"))
    async with HttpClient(_cfg()) as client:
        with pytest.raises(UpstreamClientError) as exc_info:
            await _get(client)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
@respx.mock
async def test_4xx_body_attached_to_error():
    respx.get(URL).mock(return_value=httpx.Response(422, text='{"error": "bad"}'))
    async with HttpClient(_cfg()) as client:
        with pytest.raises(UpstreamClientError) as exc_info:
            await _get(client)
    assert exc_info.value.body == '{"error": "bad"}'


@pytest.mark.asyncio
@respx.mock
async def test_5xx_raises_upstream_server_error():
    respx.get(URL).mock(return_value=httpx.Response(500, text="oops"))
    async with HttpClient(_cfg()) as client:
        with pytest.raises(UpstreamServerError) as exc_info:
            await _get(client)
    assert exc_info.value.status_code == 500


# ---------------------------------------------------------------------------
# Transient / network errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_timeout_raises_transient_error():
    respx.get(URL).mock(side_effect=httpx.TimeoutException("timed out"))
    async with HttpClient(_cfg()) as client:
        with pytest.raises(TransientError):
            await _get(client)


@pytest.mark.asyncio
@respx.mock
async def test_connect_error_raises_transient_error():
    respx.get(URL).mock(side_effect=httpx.ConnectError("refused"))
    async with HttpClient(_cfg()) as client:
        with pytest.raises(TransientError):
            await _get(client)


# ---------------------------------------------------------------------------
# Response size guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_content_length_over_limit_raises():
    limit = 100
    respx.get(URL).mock(return_value=httpx.Response(
        200, text="x" * 10,
        headers={"Content-Length": str(limit + 1)},
    ))
    async with HttpClient(_cfg(response_size_limit_bytes=limit)) as client:
        with pytest.raises(ResponseTooLargeError) as exc_info:
            await _get(client)
    assert exc_info.value.limit_bytes == limit


@pytest.mark.asyncio
@respx.mock
async def test_actual_body_over_limit_raises():
    limit = 10
    big_body = "x" * (limit + 1)
    respx.get(URL).mock(return_value=httpx.Response(200, text=big_body))
    async with HttpClient(_cfg(response_size_limit_bytes=limit)) as client:
        with pytest.raises(ResponseTooLargeError):
            await _get(client)


# ---------------------------------------------------------------------------
# Body truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_body_truncated_to_text_truncate_bytes():
    truncate_at = 20
    body = "a" * 50
    respx.get(URL).mock(return_value=httpx.Response(200, text=body))
    async with HttpClient(_cfg(
        response_size_limit_bytes=1_000,
        text_truncate_bytes=truncate_at,
    )) as client:
        resp = await _get(client)
    assert len(resp.body) == truncate_at
    assert resp.truncated is True


@pytest.mark.asyncio
@respx.mock
async def test_body_not_truncated_when_small():
    body = "hello"
    respx.get(URL).mock(return_value=httpx.Response(200, text=body))
    async with HttpClient(_cfg(text_truncate_bytes=100)) as client:
        resp = await _get(client)
    assert resp.body == "hello"
    assert resp.truncated is False


# ---------------------------------------------------------------------------
# Retry on configured status codes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_503_succeeds_on_second_attempt():
    route = respx.get(URL)
    route.side_effect = [
        httpx.Response(503, text="unavailable"),
        httpx.Response(200, text="ok"),
    ]
    retry = RetryConfig(attempts=2, on_status=[503])
    async with HttpClient(_cfg()) as client:
        resp = await _get(client, retry_config=retry)
    assert resp.status_code == 200
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_retry_exhausted_raises_upstream_server_error():
    respx.get(URL).mock(return_value=httpx.Response(503, text="still down"))
    retry = RetryConfig(attempts=2, on_status=[503])
    async with HttpClient(_cfg()) as client:
        with pytest.raises(UpstreamServerError):
            await _get(client, retry_config=retry)


@pytest.mark.asyncio
@respx.mock
async def test_no_retry_on_non_configured_status():
    """503 not in retry.on_status — should raise immediately, no retry."""
    route = respx.get(URL)
    route.side_effect = [
        httpx.Response(503, text="down"),
        httpx.Response(200, text="ok"),  # should never be reached
    ]
    retry = RetryConfig(attempts=2, on_status=[504])  # 503 not in list
    async with HttpClient(_cfg()) as client:
        with pytest.raises(UpstreamServerError):
            await _get(client, retry_config=retry)
    assert route.call_count == 1  # only called once


# ---------------------------------------------------------------------------
# Context manager guard
# ---------------------------------------------------------------------------


def test_request_outside_context_raises():
    """Calling request() without entering context manager raises RuntimeError."""
    client = HttpClient(_cfg())
    import asyncio
    with pytest.raises(RuntimeError, match="context manager"):
        asyncio.get_event_loop().run_until_complete(
            client.request(method="GET", url=URL, headers={}, params={}, json_body=None)
        )
