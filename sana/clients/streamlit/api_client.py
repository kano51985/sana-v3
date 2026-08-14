"""Synchronous, redaction-safe HTTP/SSE client for Streamlit reruns."""

from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Any, Iterable, Iterator
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx


class SanaAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ServerSentEvent:
    sequence: int
    event_type: str
    payload: dict[str, Any]

    @property
    def is_terminal(self) -> bool:
        return self.event_type in {
            "RUN_COMPLETED",
            "RUN_SUCCEEDED",
            "RUN_FAILED",
            "RUN_CANCELLED",
        } or bool(self.payload.get("terminal"))


def parse_sse_lines(lines: Iterable[str]) -> Iterator[ServerSentEvent]:
    event_id: str | None = None
    event_type = "message"
    data_lines: list[str] = []

    def dispatch() -> ServerSentEvent | None:
        nonlocal event_id, event_type, data_lines
        if event_id is None and not data_lines:
            return None
        try:
            sequence = int(event_id or "0")
            decoded = json.loads("\n".join(data_lines) or "{}")
        except (ValueError, json.JSONDecodeError) as exc:
            raise SanaAPIError("The API returned a malformed SSE event") from exc
        payload = decoded if isinstance(decoded, dict) else {"data": decoded}
        event = ServerSentEvent(sequence, event_type, payload)
        event_id = None
        event_type = "message"
        data_lines = []
        return event

    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if not line:
            event = dispatch()
            if event is not None:
                yield event
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "id":
            event_id = value
        elif field == "event":
            event_type = value or "message"
        elif field == "data":
            data_lines.append(value)
    event = dispatch()
    if event is not None:
        yield event


class SanaAPIClient:
    def __init__(
        self,
        base_url: str,
        access_token: str,
        *,
        timeout_seconds: float = 15.0,
        sse_read_timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("API URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password:
            raise ValueError("API URL cannot contain credentials")
        if not access_token.strip():
            raise ValueError("Access token cannot be empty")
        self._base_url = base_url.rstrip("/")
        self._token = access_token.strip()
        self._timeout = timeout_seconds
        self._sse_timeout = sse_read_timeout_seconds
        self._transport = transport

    def _client(self, *, sse: bool = False) -> httpx.Client:
        timeout = httpx.Timeout(
            self._sse_timeout if sse else self._timeout,
            connect=self._timeout,
        )
        return httpx.Client(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            },
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
        )

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        detail = "API request failed"
        try:
            body = response.json()
            if isinstance(body, dict) and isinstance(body.get("detail"), str):
                detail = body["detail"]
        except (ValueError, json.JSONDecodeError):
            pass
        raise SanaAPIError(detail, status_code=response.status_code)

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            with self._client() as client:
                response = client.request(method, path, **kwargs)
                self._raise_for_status(response)
                payload = response.json()
        except SanaAPIError:
            raise
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            raise SanaAPIError("Unable to communicate with the Sana API") from exc
        if not isinstance(payload, dict):
            raise SanaAPIError("The API returned an invalid JSON response")
        return payload

    def authenticate(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/me")

    def list_conversations(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/v1/conversations")
        return list(payload.get("conversations") or ())

    def create_conversation(self, title: str = "新会话") -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/conversations",
            json={"title": title.strip() or "新会话"},
        )

    def list_messages(self, conversation_id: str | UUID) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            f"/api/v1/conversations/{conversation_id}/messages",
        )
        return list(payload.get("messages") or ())

    def submit_message(
        self,
        conversation_id: str | UUID,
        content: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not content.strip():
            raise ValueError("Message cannot be empty")
        return self._request(
            "POST",
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": idempotency_key or uuid4().hex},
            json={"content": content.strip()},
        )

    def get_run(self, run_id: str | UUID) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/runs/{run_id}")

    def cancel_run(self, run_id: str | UUID) -> dict[str, Any]:
        return self._request("POST", f"/api/v1/runs/{run_id}/cancel")

    def get_evidence(self, run_id: str | UUID) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/runs/{run_id}/evidence")

    def iter_run_events(
        self,
        run_id: str | UUID,
        *,
        after_sequence: int = 0,
        max_reconnects: int = 3,
        reconnect_delay_seconds: float = 0.05,
    ) -> Iterator[ServerSentEvent]:
        if after_sequence < 0 or max_reconnects < 0:
            raise ValueError("SSE cursors and reconnect counts cannot be negative")
        cursor = after_sequence
        reconnects = 0
        while True:
            try:
                with self._client(sse=True) as client:
                    with client.stream(
                        "GET",
                        f"/api/v1/runs/{run_id}/events",
                        headers={
                            "Accept": "text/event-stream",
                            "Last-Event-ID": str(cursor),
                        },
                    ) as response:
                        self._raise_for_status(response)
                        for event in parse_sse_lines(response.iter_lines()):
                            if event.sequence <= cursor:
                                continue
                            cursor = event.sequence
                            yield event
                            if event.is_terminal:
                                return
            except SanaAPIError:
                raise
            except httpx.HTTPError as exc:
                if reconnects >= max_reconnects:
                    raise SanaAPIError("SSE connection could not be resumed") from exc
            reconnects += 1
            if reconnects > max_reconnects:
                raise SanaAPIError("SSE stream ended before the run completed")
            if reconnect_delay_seconds:
                time.sleep(reconnect_delay_seconds)
