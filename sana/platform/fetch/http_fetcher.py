"""Bounded HTTP content fetcher with redirect-by-redirect SSRF validation."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import urljoin

import httpx

from sana.modules.content.domain import FetchArtifact, FetchRequest, FetchStatus
from sana.modules.shared.clock import Clock
from sana.modules.shared.errors import ErrorCategory, TypedError
from sana.platform.security.ssrf import SSRFBlocked, SSRFGuard


class HostConcurrencyLimiter:
    def __init__(self, per_host_limit: int = 2) -> None:
        if per_host_limit < 1:
            raise ValueError("Host concurrency limit must be positive")
        self._limit = per_host_limit
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._references: dict[str, int] = {}
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, host: str) -> AsyncIterator[None]:
        async with self._lock:
            semaphore = self._semaphores.setdefault(
                host,
                asyncio.Semaphore(self._limit),
            )
            self._references[host] = self._references.get(host, 0) + 1
        try:
            async with semaphore:
                yield
        finally:
            async with self._lock:
                remaining = self._references[host] - 1
                if remaining == 0:
                    del self._references[host]
                    del self._semaphores[host]
                else:
                    self._references[host] = remaining


class HttpContentFetcher:
    _ALLOWED_MEDIA_TYPES = frozenset(
        {
            "text/html",
            "application/xhtml+xml",
            "text/plain",
            "text/csv",
            "application/json",
            "application/pdf",
        }
    )
    _REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

    def __init__(
        self,
        guard: SSRFGuard,
        clock: Clock,
        *,
        client: httpx.AsyncClient | None = None,
        host_limiter: HostConcurrencyLimiter | None = None,
    ) -> None:
        self._guard = guard
        self._clock = clock
        self._client = client or httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_client = client is None
        self._hosts = host_limiter or HostConcurrencyLimiter()

    def _remaining(self, request: FetchRequest) -> float:
        return (request.deadline - self._clock.now()).total_seconds()

    @staticmethod
    def _peer_address(response: httpx.Response) -> str | None:
        stream = response.extensions.get("network_stream")
        if stream is None or not hasattr(stream, "get_extra_info"):
            return None
        server = stream.get_extra_info("server_addr")
        if server is None:
            server = stream.get_extra_info("peername")
        if isinstance(server, tuple) and server:
            return str(server[0])
        return str(server) if server else None

    async def fetch(self, request: FetchRequest) -> FetchArtifact:
        remaining = self._remaining(request)
        if remaining <= 0:
            return self._deadline_failure(request)
        try:
            async with asyncio.timeout(remaining):
                return await self._fetch_within_deadline(request)
        except TimeoutError as exc:
            return self._deadline_failure(request, cause=exc)

    def _deadline_failure(
        self,
        request: FetchRequest,
        *,
        cause: Exception | None = None,
    ) -> FetchArtifact:
        error = TypedError(
            ErrorCategory.BUDGET,
            "fetch_deadline_exceeded",
            "Fetch deadline was exhausted",
            retryable=False,
            cause=cause,
        )
        return FetchArtifact(
            request_url=request.url,
            final_url=request.url,
            status=FetchStatus.FAILED,
            http_status=None,
            media_type=None,
            body=b"",
            content_hash=None,
            fetched_at=self._clock.now(),
            error=error,
        )

    async def _fetch_within_deadline(self, request: FetchRequest) -> FetchArtifact:
        current_url = request.url
        redirects: list[str] = []
        http_status = None
        media_type = None
        try:
            while True:
                remaining = self._remaining(request)
                if remaining <= 0:
                    raise TypedError(
                        ErrorCategory.BUDGET,
                        "fetch_deadline_exceeded",
                        "Fetch deadline was exhausted",
                        retryable=False,
                    )
                target = await self._guard.resolve_and_validate(current_url)
                async with self._hosts.acquire(target.host):
                    try:
                        async with asyncio.timeout(remaining):
                            async with self._client.stream(
                                "GET",
                                current_url,
                                timeout=httpx.Timeout(remaining),
                                follow_redirects=False,
                                headers={
                                    "User-Agent": "SanaResearchBot/0.2",
                                    "Accept": "text/html,text/plain,text/csv,application/json,application/pdf;q=0.8",
                                },
                            ) as response:
                                http_status = response.status_code
                                peer = self._peer_address(response)
                                if peer is None:
                                    raise SSRFBlocked(
                                        "Connected peer address could not be verified"
                                    )
                                self._guard.validate_peer(peer)
                                await self._guard.resolve_and_validate(current_url)
                                if response.status_code in self._REDIRECT_STATUSES:
                                    location = response.headers.get("location")
                                    if not location:
                                        raise TypedError(
                                            ErrorCategory.CONTENT,
                                            "redirect_without_location",
                                            "Redirect response did not include Location",
                                            retryable=False,
                                        )
                                    if len(redirects) >= request.max_redirects:
                                        raise TypedError(
                                            ErrorCategory.CONTENT,
                                            "too_many_redirects",
                                            "Fetch exceeded redirect limit",
                                            retryable=False,
                                        )
                                    next_url = urljoin(current_url, location)
                                    await self._guard.resolve_and_validate(next_url)
                                    redirects.append(next_url)
                                    current_url = next_url
                                    continue
                                if response.status_code == 429 or response.status_code >= 500:
                                    raise TypedError(
                                        ErrorCategory.TRANSIENT,
                                        f"fetch_http_{response.status_code}",
                                        f"Fetch returned HTTP {response.status_code}",
                                        retryable=True,
                                    )
                                if response.status_code >= 400:
                                    raise TypedError(
                                        ErrorCategory.CONTENT,
                                        f"fetch_http_{response.status_code}",
                                        f"Fetch returned HTTP {response.status_code}",
                                        retryable=False,
                                    )
                                media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                                if media_type not in self._ALLOWED_MEDIA_TYPES:
                                    raise TypedError(
                                        ErrorCategory.CONTENT,
                                        "unsupported_content_type",
                                        f"Fetch content type is not allowed: {media_type or 'missing'}",
                                        retryable=False,
                                    )
                                declared = response.headers.get("content-length")
                                if declared:
                                    try:
                                        declared_size = int(declared)
                                    except ValueError as exc:
                                        raise TypedError(
                                            ErrorCategory.CONTENT,
                                            "invalid_content_length",
                                            "Fetch returned invalid Content-Length",
                                            retryable=False,
                                            cause=exc,
                                        ) from exc
                                    if declared_size > request.max_response_bytes:
                                        raise TypedError(
                                            ErrorCategory.CONTENT,
                                            "response_too_large",
                                            "Fetch response exceeded size limit",
                                            retryable=False,
                                        )
                                chunks = []
                                size = 0
                                async for chunk in response.aiter_bytes():
                                    size += len(chunk)
                                    if size > request.max_response_bytes:
                                        raise TypedError(
                                            ErrorCategory.CONTENT,
                                            "response_too_large",
                                            "Fetch response exceeded size limit",
                                            retryable=False,
                                        )
                                    chunks.append(chunk)
                                body = b"".join(chunks)
                                if not body:
                                    raise TypedError(
                                        ErrorCategory.CONTENT,
                                        "empty_response_body",
                                        "Fetch returned an empty response body",
                                        retryable=False,
                                    )
                                return FetchArtifact(
                                    request_url=request.url,
                                    final_url=current_url,
                                    status=FetchStatus.SUCCEEDED,
                                    http_status=response.status_code,
                                    media_type=media_type,
                                    body=body,
                                    content_hash=hashlib.sha256(body).hexdigest(),
                                    fetched_at=self._clock.now(),
                                    redirects=tuple(redirects),
                                    response_headers={
                                        key: value
                                        for key, value in response.headers.items()
                                        if key.lower()
                                        in {"content-type", "content-length", "etag", "last-modified"}
                                    },
                                )
                    except TimeoutError as exc:
                        raise TypedError(
                            ErrorCategory.BUDGET,
                            "fetch_deadline_exceeded",
                            "Fetch deadline was exhausted",
                            retryable=False,
                            cause=exc,
                        ) from exc
                    except (httpx.TimeoutException, httpx.NetworkError) as exc:
                        raise TypedError(
                            ErrorCategory.TRANSIENT,
                            "fetch_network_failure",
                            str(exc) or "Fetch network failure",
                            retryable=True,
                            cause=exc,
                        ) from exc
        except TypedError as error:
            return FetchArtifact(
                request_url=request.url,
                final_url=current_url,
                status=(
                    FetchStatus.BLOCKED
                    if isinstance(error, SSRFBlocked)
                    else FetchStatus.FAILED
                ),
                http_status=http_status,
                media_type=media_type,
                body=b"",
                content_hash=None,
                fetched_at=self._clock.now(),
                redirects=tuple(redirects),
                error=error,
            )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
