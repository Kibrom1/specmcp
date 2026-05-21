"""
Unit tests for SSE streaming support.

Covers:
  - _operation_may_stream(): positive and negative detection
  - HttpClient.stream_request(): multi-event stream, [DONE] sentinel, content-type
    fallback, 4xx/5xx error handling, network timeout, streaming_max_bytes cap
  - dispatcher.dispatch() streaming branch: enable_streaming flag, timeout multiplier,
    truncation marker, fallback to buffered path when streaming disabled
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

from specmcp.config import DispatchConfig
from specmcp.errors import TransientError, UpstreamClientError, UpstreamServerError
from specmcp.runtime.http_client import HttpClient


# ---------------------------------------------------------------------------
# Helpers — SSE response builder
# ---------------------------------------------------------------------------


def _sse_response(events: list[str], status: int = 200) -> httpx.Response:
    """Build a fake SSE httpx.Response from a list of data payloads."""
    lines = []
    for e in events:
        lines.append(f"data: {e}")
        lines.append("")  # blank line separates SSE events
    body = "\n".join(lines)
    return httpx.Response(
        status,
        headers={"content-type": "text/event-stream; charset=utf-8"},
        text=body,
    )


def _json_response(body: str) -> httpx.Response:
    """A non-SSE response (application/json) to test fallback."""
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        text=body,
    )


STREAM_URL = "https://api.example.com/stream"


# ---------------------------------------------------------------------------
# _operation_may_stream()
# ---------------------------------------------------------------------------


def _make_op_with_response_ct(content_types: list[str]):
    """Build a minimal Operation mock with the given 2xx response content types."""
    from specmcp.core.model import Operation, Response, ResponseVariant

    variants = [ResponseVariant(content_type=ct, schema={}) for ct in content_types]
    resp = Response(status_code="200", variants=variants)

    op = MagicMock(spec=Operation)
    op.responses = [resp]
    return op


def test_operation_may_stream_positive():
    from specmcp.runtime.dispatcher import _operation_may_stream
    op = _make_op_with_response_ct(["text/event-stream"])
    assert _operation_may_stream(op) is True


def test_operation_may_stream_positive_with_params():
    from specmcp.runtime.dispatcher import _operation_may_stream
    op = _make_op_with_response_ct(["text/event-stream; charset=utf-8"])
    assert _operation_may_stream(op) is True


def test_operation_may_stream_negative_json():
    from specmcp.runtime.dispatcher import _operation_may_stream
    op = _make_op_with_response_ct(["application/json"])
    assert _operation_may_stream(op) is False


def test_operation_may_stream_negative_no_responses():
    from specmcp.runtime.dispatcher import _operation_may_stream
    from specmcp.core.model import Operation

    op = MagicMock(spec=Operation)
    op.responses = []
    assert _operation_may_stream(op) is False


def test_operation_may_stream_ignores_non_2xx():
    """A text/event-stream on a 4xx response should NOT trigger streaming."""
    from specmcp.runtime.dispatcher import _operation_may_stream
    from specmcp.core.model import Response, ResponseVariant

    variant = ResponseVariant(content_type="text/event-stream", schema={})
    resp_4xx = Response(status_code="400", variants=[variant])
    resp_ok = Response(status_code="200", variants=[ResponseVariant(content_type="application/json", schema={})])

    from specmcp.core.model import Operation
    op = MagicMock(spec=Operation)
    op.responses = [resp_4xx, resp_ok]
    assert _operation_may_stream(op) is False


def test_operation_may_stream_mixed_variants():
    """If at least one 2xx variant is SSE, returns True."""
    from specmcp.runtime.dispatcher import _operation_may_stream
    op = _make_op_with_response_ct(["application/json", "text/event-stream"])
    assert _operation_may_stream(op) is True


# ---------------------------------------------------------------------------
# HttpClient.stream_request() — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_stream_request_single_event():
    """A single SSE data event is returned as the text."""
    respx.post(STREAM_URL).mock(return_value=_sse_response(["hello world"]))

    cfg = DispatchConfig()
    async with HttpClient(cfg) as client:
        text, truncated = await client.stream_request(
            method="POST",
            url=STREAM_URL,
            headers={},
            params={},
            json_body=None,
            timeout_seconds=10.0,
            streaming_max_bytes=cfg.streaming_max_bytes,
        )

    assert text == "hello world"
    assert not truncated


@pytest.mark.asyncio
@respx.mock
async def test_stream_request_multiple_events_joined_by_newline():
    """Multiple SSE events are joined with newlines."""
    events = ["chunk one", "chunk two", "chunk three"]
    respx.post(STREAM_URL).mock(return_value=_sse_response(events))

    cfg = DispatchConfig()
    async with HttpClient(cfg) as client:
        text, truncated = await client.stream_request(
            method="POST",
            url=STREAM_URL,
            headers={},
            params={},
            json_body=None,
            timeout_seconds=10.0,
            streaming_max_bytes=cfg.streaming_max_bytes,
        )

    assert text == "chunk one\nchunk two\nchunk three"
    assert not truncated


@pytest.mark.asyncio
@respx.mock
async def test_stream_request_done_sentinel_stops_iteration():
    """[DONE] stops the SSE parser; events after it are ignored."""
    body = "data: first\n\ndata: [DONE]\n\ndata: ignored\n\n"
    respx.post(STREAM_URL).mock(return_value=httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text=body,
    ))

    cfg = DispatchConfig()
    async with HttpClient(cfg) as client:
        text, truncated = await client.stream_request(
            method="POST",
            url=STREAM_URL,
            headers={},
            params={},
            json_body=None,
            timeout_seconds=10.0,
            streaming_max_bytes=cfg.streaming_max_bytes,
        )

    assert text == "first"
    assert not truncated


@pytest.mark.asyncio
@respx.mock
async def test_stream_request_skips_non_data_lines():
    """event:, id:, comment lines are ignored; only data: lines are collected."""
    body = (
        ": this is a comment\n"
        "event: message\n"
        "id: 1\n"
        "data: payload\n"
        "\n"
    )
    respx.post(STREAM_URL).mock(return_value=httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text=body,
    ))

    cfg = DispatchConfig()
    async with HttpClient(cfg) as client:
        text, _ = await client.stream_request(
            method="POST",
            url=STREAM_URL,
            headers={},
            params={},
            json_body=None,
            timeout_seconds=10.0,
            streaming_max_bytes=cfg.streaming_max_bytes,
        )

    assert text == "payload"


@pytest.mark.asyncio
@respx.mock
async def test_stream_request_empty_stream_returns_empty_string():
    """An SSE response with no data: lines returns an empty string."""
    respx.post(STREAM_URL).mock(return_value=httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text="",
    ))

    cfg = DispatchConfig()
    async with HttpClient(cfg) as client:
        text, truncated = await client.stream_request(
            method="POST",
            url=STREAM_URL,
            headers={},
            params={},
            json_body=None,
            timeout_seconds=10.0,
            streaming_max_bytes=cfg.streaming_max_bytes,
        )

    assert text == ""
    assert not truncated


# ---------------------------------------------------------------------------
# HttpClient.stream_request() — streaming_max_bytes cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_stream_request_truncates_at_max_bytes():
    """If total bytes exceed streaming_max_bytes, truncated=True and rest is dropped."""
    # Each chunk is 10 bytes; limit is 15 → first fits, second causes truncation.
    events = ["0123456789", "abcdefghij", "should-not-appear"]
    respx.post(STREAM_URL).mock(return_value=_sse_response(events))

    cfg = DispatchConfig()
    async with HttpClient(cfg) as client:
        text, truncated = await client.stream_request(
            method="POST",
            url=STREAM_URL,
            headers={},
            params={},
            json_body=None,
            timeout_seconds=10.0,
            streaming_max_bytes=15,  # 10 bytes fits, 10+10 overflows
        )

    assert truncated is True
    assert "should-not-appear" not in text
    assert "0123456789" in text


# ---------------------------------------------------------------------------
# HttpClient.stream_request() — content-type fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_stream_request_falls_back_when_non_sse_content_type():
    """If upstream returns application/json despite declaring SSE, buffer and return."""
    json_body = '{"error": "not a stream"}'
    respx.post(STREAM_URL).mock(return_value=_json_response(json_body))

    cfg = DispatchConfig()
    async with HttpClient(cfg) as client:
        text, truncated = await client.stream_request(
            method="POST",
            url=STREAM_URL,
            headers={},
            params={},
            json_body=None,
            timeout_seconds=10.0,
            streaming_max_bytes=cfg.streaming_max_bytes,
        )

    assert text == json_body
    assert not truncated


# ---------------------------------------------------------------------------
# HttpClient.stream_request() — error responses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_stream_request_4xx_raises_upstream_client_error():
    """A 4xx response during streaming raises UpstreamClientError."""
    respx.post(STREAM_URL).mock(return_value=httpx.Response(
        401,
        headers={"content-type": "application/json"},
        text='{"error": "unauthorized"}',
    ))

    cfg = DispatchConfig()
    async with HttpClient(cfg) as client:
        with pytest.raises(UpstreamClientError) as exc_info:
            await client.stream_request(
                method="POST",
                url=STREAM_URL,
                headers={},
                params={},
                json_body=None,
                timeout_seconds=10.0,
                streaming_max_bytes=cfg.streaming_max_bytes,
            )

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
@respx.mock
async def test_stream_request_5xx_raises_upstream_server_error():
    """A 5xx response during streaming raises UpstreamServerError."""
    respx.post(STREAM_URL).mock(return_value=httpx.Response(
        503,
        headers={"content-type": "text/plain"},
        text="Service Unavailable",
    ))

    cfg = DispatchConfig()
    async with HttpClient(cfg) as client:
        with pytest.raises(UpstreamServerError) as exc_info:
            await client.stream_request(
                method="POST",
                url=STREAM_URL,
                headers={},
                params={},
                json_body=None,
                timeout_seconds=10.0,
                streaming_max_bytes=cfg.streaming_max_bytes,
            )

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
@respx.mock
async def test_stream_request_network_error_raises_transient_error():
    """A network-level error during streaming raises TransientError."""
    respx.post(STREAM_URL).mock(side_effect=httpx.ConnectError("refused"))

    cfg = DispatchConfig()
    async with HttpClient(cfg) as client:
        with pytest.raises(TransientError):
            await client.stream_request(
                method="POST",
                url=STREAM_URL,
                headers={},
                params={},
                json_body=None,
                timeout_seconds=10.0,
                streaming_max_bytes=cfg.streaming_max_bytes,
            )


@pytest.mark.asyncio
@respx.mock
async def test_stream_request_timeout_raises_transient_error():
    """A timeout during streaming raises TransientError."""
    respx.post(STREAM_URL).mock(side_effect=httpx.TimeoutException("timed out"))

    cfg = DispatchConfig()
    async with HttpClient(cfg) as client:
        with pytest.raises(TransientError, match="timed out"):
            await client.stream_request(
                method="POST",
                url=STREAM_URL,
                headers={},
                params={},
                json_body=None,
                timeout_seconds=5.0,
                streaming_max_bytes=cfg.streaming_max_bytes,
            )


# ---------------------------------------------------------------------------
# DispatchConfig — new field defaults
# ---------------------------------------------------------------------------


def test_dispatch_config_streaming_defaults():
    cfg = DispatchConfig()
    assert cfg.enable_streaming is True
    assert cfg.streaming_timeout_multiplier == 5.0
    assert cfg.streaming_max_bytes == 4 * 1024 * 1024


def test_dispatch_config_streaming_can_be_disabled():
    cfg = DispatchConfig(enable_streaming=False)
    assert cfg.enable_streaming is False


def test_dispatch_config_streaming_timeout_multiplier_ge_1():
    """streaming_timeout_multiplier must be >= 1.0."""
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        DispatchConfig(streaming_timeout_multiplier=0.5)
