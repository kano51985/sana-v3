"""Bounded HTTP reads shared by fixed search endpoints."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from sana.modules.shared.errors import ErrorCategory, TypedError


_TRACKING_PARAMETERS = frozenset(
    {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid"}
)


def canonicalize_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Search result URL must be HTTP(S)")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Search result URL cannot contain credentials")
    host = parsed.hostname.lower()
    port = parsed.port
    netloc = host
    if port and not (
        (parsed.scheme.lower() == "http" and port == 80)
        or (parsed.scheme.lower() == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.casefold() not in _TRACKING_PARAMETERS
        ],
        doseq=True,
    )
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", query, ""))


async def bounded_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str],
    timeout_seconds: float,
    max_response_bytes: int,
) -> tuple[bytes, int]:
    try:
        async with client.stream(
            "GET",
            url,
            params=params,
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
        ) as response:
            if response.status_code == 429 or response.status_code >= 500:
                raise TypedError(
                    ErrorCategory.TRANSIENT,
                    f"search_http_{response.status_code}",
                    f"Search endpoint returned HTTP {response.status_code}",
                    retryable=True,
                )
            if response.status_code >= 400:
                raise TypedError(
                    ErrorCategory.PERMANENT,
                    f"search_http_{response.status_code}",
                    f"Search endpoint returned HTTP {response.status_code}",
                    retryable=False,
                )
            declared_length = response.headers.get("content-length")
            if declared_length and int(declared_length) > max_response_bytes:
                raise TypedError(
                    ErrorCategory.CONTENT,
                    "search_response_too_large",
                    "Search endpoint response exceeded size limit",
                    retryable=False,
                )
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > max_response_bytes:
                    raise TypedError(
                        ErrorCategory.CONTENT,
                        "search_response_too_large",
                        "Search endpoint response exceeded size limit",
                        retryable=False,
                    )
                chunks.append(chunk)
            return b"".join(chunks), size
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise TypedError(
            ErrorCategory.TRANSIENT,
            "search_network_failure",
            str(exc) or "Search endpoint network failure",
            retryable=True,
            cause=exc,
        ) from exc
