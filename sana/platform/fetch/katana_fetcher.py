"""Optional Katana adapter used only for bounded site-link discovery."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from sana.modules.content.ports import CapabilityStatus
from sana.modules.shared.errors import ErrorCategory, TypedError
from sana.platform.security.ssrf import SSRFGuard


class _OutputLimitExceeded(Exception):
    pass


class KatanaFetcher:
    """Run Katana without a shell and expose links, never page content."""

    def __init__(
        self,
        guard: SSRFGuard,
        *,
        executable: str = "katana",
        which: Callable[[str], str | None] = shutil.which,
        max_output_bytes: int = 1_000_000,
        max_links: int = 500,
    ) -> None:
        if max_output_bytes < 1 or max_links < 1:
            raise ValueError("Katana output limits must be positive")
        self._guard = guard
        self._executable = executable
        self._which = which
        self._max_output_bytes = max_output_bytes
        self._max_links = max_links

    def _path(self) -> str | None:
        candidate = self._which(self._executable)
        if not candidate:
            return None
        return str(Path(candidate).resolve())

    async def _run_bounded(
        self,
        process: asyncio.subprocess.Process,
        *,
        timeout_seconds: float,
        stdout_limit: int,
        stderr_limit: int = 65_536,
    ) -> tuple[bytes, bytes]:
        async def read_stream(
            stream: asyncio.StreamReader | None,
            limit: int,
        ) -> bytes:
            if stream is None:
                return b""
            chunks: list[bytes] = []
            size = 0
            while chunk := await stream.read(65_536):
                size += len(chunk)
                if size > limit:
                    raise _OutputLimitExceeded
                chunks.append(chunk)
            return b"".join(chunks)

        stdout_task = asyncio.create_task(read_stream(process.stdout, stdout_limit))
        stderr_task = asyncio.create_task(read_stream(process.stderr, stderr_limit))
        wait_task = asyncio.create_task(process.wait())
        tasks = (stdout_task, stderr_task, wait_task)
        try:
            async with asyncio.timeout(timeout_seconds):
                stdout, stderr, _ = await asyncio.gather(*tasks)
                return stdout, stderr
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def probe(self) -> CapabilityStatus:
        path = self._path()
        if path is None:
            return CapabilityStatus("katana", False, "executable_not_found")
        try:
            process = await asyncio.create_subprocess_exec(
                path,
                "-version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await self._run_bounded(
                process,
                timeout_seconds=3,
                stdout_limit=4_096,
                stderr_limit=4_096,
            )
        except (OSError, TimeoutError, _OutputLimitExceeded) as exc:
            return CapabilityStatus("katana", False, type(exc).__name__)
        detail = (stdout or stderr).decode("utf-8", errors="replace").strip()
        return CapabilityStatus(
            "katana",
            process.returncode == 0,
            detail[:200] or f"exit_{process.returncode}",
        )

    async def discover_links(
        self,
        url: str,
        *,
        timeout_seconds: float,
    ) -> tuple[str, ...]:
        if timeout_seconds <= 0:
            raise ValueError("Katana timeout must be positive")
        try:
            async with asyncio.timeout(timeout_seconds):
                return await self._discover_links_within_deadline(
                    url,
                    timeout_seconds=timeout_seconds,
                )
        except TimeoutError as exc:
            raise TypedError(
                ErrorCategory.BUDGET,
                "katana_timeout",
                "Katana navigation exceeded its deadline",
                retryable=False,
                cause=exc,
            ) from exc

    async def _discover_links_within_deadline(
        self,
        url: str,
        *,
        timeout_seconds: float,
    ) -> tuple[str, ...]:
        await self._guard.resolve_and_validate(url)
        path = self._path()
        if path is None:
            raise TypedError(
                ErrorCategory.PERMANENT,
                "katana_unavailable",
                "Katana executable is not available",
                retryable=False,
            )
        try:
            process = await asyncio.create_subprocess_exec(
                path,
                "-u",
                url,
                "-silent",
                "-jsonl",
                "-d",
                "2",
                "-jc",
                "-kf",
                "robotstxt,sitemapxml",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise TypedError(
                ErrorCategory.PERMANENT,
                "katana_unavailable",
                "Katana executable could not be started",
                retryable=False,
                cause=exc,
            ) from exc
        try:
            stdout, stderr = await self._run_bounded(
                process,
                timeout_seconds=timeout_seconds,
                stdout_limit=self._max_output_bytes,
            )
        except TimeoutError as exc:
            raise TypedError(
                ErrorCategory.BUDGET,
                "katana_timeout",
                "Katana navigation exceeded its deadline",
                retryable=False,
                cause=exc,
            ) from exc
        except _OutputLimitExceeded as exc:
            raise TypedError(
                ErrorCategory.CONTENT,
                "katana_output_too_large",
                "Katana navigation output exceeded its size limit",
                retryable=False,
                cause=exc,
            ) from exc
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[:200]
            raise TypedError(
                ErrorCategory.TRANSIENT,
                "katana_failed",
                detail or f"Katana exited with status {process.returncode}",
                retryable=True,
            )

        links: list[str] = []
        seen: set[str] = set()
        for raw_line in stdout.decode("utf-8", errors="replace").splitlines():
            candidate = self._parse_link(raw_line)
            if not candidate or candidate in seen:
                continue
            try:
                self._guard.validate_syntax(candidate)
            except TypedError:
                continue
            seen.add(candidate)
            links.append(candidate)
            if len(links) >= self._max_links:
                break
        return tuple(links)

    @staticmethod
    def _parse_link(line: str) -> str | None:
        stripped = line.strip()
        if not stripped:
            return None
        if stripped.startswith("{"):
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                return None
            candidate = record.get("url") or record.get("endpoint")
        else:
            candidate = stripped
        if not isinstance(candidate, str):
            return None
        parsed = urlsplit(candidate)
        return candidate if parsed.scheme.lower() in {"http", "https"} else None
