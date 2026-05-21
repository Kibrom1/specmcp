"""
specmcp HTTP client — thin httpx wrapper.

Responsibilities:
  - Single async httpx.AsyncClient per server lifetime (connection pooling).
  - Timeout enforcement (per-operation or global default).
  - Response size guard before decoding — ResponseTooLargeError.
  - Text body truncation to text_truncate_bytes.
  - Transient error detection (network/timeout) → TransientError.
  - Upstream 4xx → UpstreamClientError, 5xx → UpstreamServerError.
  - Retry on configured status codes (e.g. 503, 504) up to RetryConfig.attempts.

NOT responsibilities:
  - Auth injection (done by AuthInjector before call).
  - Argument serialisation (done by Dispatcher).
  - MCP framing (done by the serve command).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx

from specmcp.config import DispatchConfig, RetryConfig
from specmcp.errors import (
    ResponseTooLargeError,
    TransientError,
    UpstreamClientError,
    UpstreamServerError,
)


class HttpResponse:
    """Parsed HTTP response, safe to hand to the Dispatcher."""

    __slots__ = ("status_code", "headers", "body", "truncated")

    def __init__(
        self,
        status_code: int,
        headers: dict[str, str],
        body: str,
        truncated: bool,
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self.body = body
        self.truncated = truncated


class HttpClient:
    """Async HTTP client for dispatching tool calls to upstream APIs.

    Create one instance per server lifetime and close it on shutdown::

        async with HttpClient(config) as client:
            response = await client.request(
                method="GET",
                url="https://api.example.com/pets/1",
                headers={"X-Api-Key": "..."},
                params={},
                json_body=None,
                timeout_seconds=30.0,
                retry_config=None,
            )
    """

    def __init__(self, config: DispatchConfig) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Context manager / lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "HttpClient":
        self._client = httpx.AsyncClient(
            verify=self._config.tls_verify,
            follow_redirects=True,
            trust_env=False,  # ignore HTTP_PROXY / SOCKS env vars; we manage our own connections
            limits=httpx.Limits(
                max_connections=self._config.global_concurrency,
                max_keepalive_connections=self._config.per_host_concurrency,
            ),
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Request entry point
    # ------------------------------------------------------------------

    async def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        params: dict[str, str],
        json_body: Any | None,
        form_body: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
        retry_config: RetryConfig | None = None,
        request_id: str | None = None,
    ) -> HttpResponse:
        """Dispatch an HTTP request and return a parsed HttpResponse.

        Args:
            method: HTTP method (GET, POST, …).
            url: Fully-constructed URL (no params here; use *params*).
            headers: Request headers with auth already injected.
            params: URL query string parameters.
            json_body: JSON-serialisable body (mutually exclusive with form_body).
            form_body: Form-encoded body (mutually exclusive with json_body).
            timeout_seconds: Request timeout; defaults to DispatchConfig default.
            retry_config: Retry policy; None means no retry.
            request_id: Caller-supplied correlation ID for error messages.

        Returns:
            HttpResponse with status_code, headers, body, truncated flag.

        Raises:
            TransientError: On network/timeout errors.
            UpstreamClientError: On 4xx responses.
            UpstreamServerError: On 5xx responses not exhausted by retry.
            ResponseTooLargeError: If Content-Length or actual body exceeds limit.
        """
        if self._client is None:
            raise RuntimeError(  # noqa: TRY301
                "HttpClient used outside async context manager. "
                "Use 'async with HttpClient(config) as client: ...'"
            )

        rid = request_id or str(uuid.uuid4())[:8]
        effective_timeout = timeout_seconds or self._config.default_timeout_seconds
        retry = retry_config or RetryConfig(attempts=1, on_status=[])

        return await self._request_with_retry(
            method=method,
            url=url,
            headers=headers,
            params=params,
            json_body=json_body,
            form_body=form_body,
            timeout_seconds=effective_timeout,
            retry=retry,
            request_id=rid,
        )

    # ------------------------------------------------------------------
    # SSE streaming
    # ------------------------------------------------------------------

    async def stream_request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        params: dict[str, str],
        json_body: Any | None,
        timeout_seconds: float,
        streaming_max_bytes: int,
        request_id: str | None = None,
    ) -> tuple[str, bool]:
        """Stream an SSE response and return (concatenated_event_data, truncated).

        Parses ``data:`` lines per the SSE spec (WHATWG). The ``[DONE]`` sentinel
        (OpenAI-style) is recognised as a stream terminator; other APIs close the
        connection instead, which ends ``aiter_lines()`` naturally.

        If the upstream responds with a non-SSE ``Content-Type`` (e.g. a JSON error
        body despite the spec declaring ``text/event-stream``), the full body is
        buffered and returned as plain text so the caller gets a useful error
        message rather than an empty string.

        Returns:
            Tuple of (text, truncated). ``text`` is the joined ``data:`` payloads.
            ``truncated`` is True if ``streaming_max_bytes`` was hit.

        Raises:
            UpstreamClientError: on 4xx responses.
            UpstreamServerError: on 5xx responses.
            TransientError: on network failures or timeout.
        """
        if self._client is None:
            raise RuntimeError(
                "HttpClient used outside async context manager. "
                "Use 'async with HttpClient(config) as client: ...'"
            )

        rid = request_id or str(uuid.uuid4())[:8]
        build_kwargs: dict[str, Any] = {
            "method": method,
            "url": url,
            "headers": headers,
            "params": params,
            "timeout": timeout_seconds,
        }
        if json_body is not None:
            build_kwargs["json"] = json_body

        full_text_parts: list[str] = []
        total_bytes = 0
        truncated = False

        try:
            async with self._client.stream(**build_kwargs) as response:
                # Handle error status codes before reading the body
                if 400 <= response.status_code < 500:
                    body = await response.aread()
                    raise UpstreamClientError(
                        f"Upstream returned HTTP {response.status_code}",
                        status_code=response.status_code,
                        body=body.decode("utf-8", errors="replace"),
                        request_id=rid,
                    )
                if response.status_code >= 500:
                    body = await response.aread()
                    raise UpstreamServerError(
                        f"Upstream returned HTTP {response.status_code}",
                        status_code=response.status_code,
                        request_id=rid,
                    )

                # Runtime content-type check — fall back to buffering if the
                # upstream returns a non-SSE content type (e.g. JSON error on an
                # operation that declares event-stream in its spec).
                actual_ct = response.headers.get("content-type", "")
                if "event-stream" not in actual_ct:
                    body = await response.aread()
                    return body.decode("utf-8", errors="replace"), False

                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        # Skip comment lines, event: fields, id: fields, etc.
                        continue
                    # Strip exactly "data:" prefix; tolerate optional space per spec.
                    data = line[5:].lstrip(" ")
                    if data == "[DONE]":
                        # OpenAI-style terminator. Other APIs close the connection
                        # instead, ending aiter_lines() naturally.
                        break
                    chunk_bytes = len(data.encode("utf-8"))
                    if total_bytes + chunk_bytes > streaming_max_bytes:
                        truncated = True
                        break
                    total_bytes += chunk_bytes
                    full_text_parts.append(data)

        except httpx.TimeoutException as exc:
            raise TransientError(
                f"SSE stream timed out after {timeout_seconds}s: {url}"
            ) from exc
        except httpx.RequestError as exc:
            raise TransientError(
                f"SSE stream network error from {url}: {type(exc).__name__}"
            ) from exc

        return "\n".join(full_text_parts), truncated

    # ------------------------------------------------------------------
    # Retry loop
    # ------------------------------------------------------------------

    async def _request_with_retry(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        params: dict[str, str],
        json_body: Any | None,
        form_body: dict[str, Any] | None,
        timeout_seconds: float,
        retry: RetryConfig,
        request_id: str,
    ) -> HttpResponse:
        last_exc: Exception | None = None
        for attempt in range(retry.attempts):
            try:
                raw = await self._send_once(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    json_body=json_body,
                    form_body=form_body,
                    timeout_seconds=timeout_seconds,
                )
            except TransientError:
                # Network errors: retry immediately (no back-off in v1)
                if attempt < retry.attempts - 1:
                    continue
                raise

            status = raw.status_code

            if status in retry.on_status and attempt < retry.attempts - 1:
                last_exc = UpstreamServerError(
                    f"Upstream returned {status} on attempt {attempt + 1}",
                    status_code=status,
                    request_id=request_id,
                )
                continue

            return self._parse_response(raw, request_id=request_id)

        # Exhausted retries on a retryable status code
        if last_exc is not None:
            raise last_exc
        raise TransientError(  # should not happen
            "Request failed after all retry attempts",
            request_id=request_id,
        )

    # ------------------------------------------------------------------
    # Single HTTP send
    # ------------------------------------------------------------------

    async def _send_once(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        params: dict[str, str],
        json_body: Any | None,
        form_body: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> httpx.Response:
        assert self._client is not None  # guarded by request()

        build_kwargs: dict[str, Any] = {
            "method": method,
            "url": url,
            "headers": headers,
            "params": params,
            "timeout": timeout_seconds,
        }
        if json_body is not None:
            build_kwargs["json"] = json_body
        elif form_body is not None:
            build_kwargs["data"] = form_body

        try:
            response = await self._client.request(**build_kwargs)
        except httpx.TimeoutException as exc:
            raise TransientError(
                f"Request timed out after {timeout_seconds}s: {url}"
            ) from exc
        except httpx.ConnectError as exc:
            raise TransientError(
                f"Connection failed to {url}: {exc}"
            ) from exc
        except httpx.RemoteProtocolError as exc:
            raise TransientError(
                f"Protocol error from {url}: {exc}"
            ) from exc
        except httpx.RequestError as exc:
            raise TransientError(
                f"Network error reaching {url}: {exc}"
            ) from exc

        return response

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(
        self,
        raw: httpx.Response,
        *,
        request_id: str,
    ) -> HttpResponse:
        status = raw.status_code

        # Size guard: check Content-Length before reading body
        content_length = raw.headers.get("content-length")
        if content_length is not None:
            try:
                cl_int = int(content_length)
                if cl_int > self._config.response_size_limit_bytes:
                    raise ResponseTooLargeError(
                        f"Response Content-Length {cl_int} exceeds limit "
                        f"{self._config.response_size_limit_bytes}",
                        response_bytes=cl_int,
                        limit_bytes=self._config.response_size_limit_bytes,
                        request_id=request_id,
                    )
            except ValueError:
                pass  # malformed Content-Length — skip the pre-check

        # Actual body
        body_bytes = raw.content
        if len(body_bytes) > self._config.response_size_limit_bytes:
            raise ResponseTooLargeError(
                f"Response body {len(body_bytes)} bytes exceeds limit "
                f"{self._config.response_size_limit_bytes}",
                response_bytes=len(body_bytes),
                limit_bytes=self._config.response_size_limit_bytes,
                request_id=request_id,
            )

        # Decode + optional truncation
        body_text = body_bytes.decode("utf-8", errors="replace")
        truncated = False
        if len(body_bytes) > self._config.text_truncate_bytes:
            body_text = body_text[: self._config.text_truncate_bytes]
            truncated = True

        # Normalise headers to plain dict
        resp_headers = dict(raw.headers)

        if 400 <= status < 500:
            raise UpstreamClientError(
                f"Upstream returned HTTP {status}",
                status_code=status,
                body=body_text,
                request_id=request_id,
            )
        if 500 <= status < 600:
            raise UpstreamServerError(
                f"Upstream returned HTTP {status}",
                status_code=status,
                request_id=request_id,
            )

        return HttpResponse(
            status_code=status,
            headers=resp_headers,
            body=body_text,
            truncated=truncated,
        )
