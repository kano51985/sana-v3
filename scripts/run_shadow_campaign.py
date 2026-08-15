"""Seven-command live Shadow Campaign entrypoint with secret-safe failures."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
import getpass
import json
import os
from pathlib import Path
import signal
import sys
from typing import Any, Protocol
from uuid import UUID

from sana.app.shadow_api_client import ShadowAPIError, ShadowCandidateAPI
from sana.app.shadow_runtime import shadow_runtime
from sana.modules.identity.domain import Principal
from sana.modules.shadow_campaign.manifest import parse_manifest_bytes
from sana.modules.shared.errors import InvariantViolation


class RunnerBindings(Protocol):
    runner: Any

    def create_command(self, principal: Principal, args, manifest): ...

    async def review(self, principal: Principal, campaign_id: UUID) -> Any: ...


class RunnerConfigurationError(RuntimeError):
    code = "runner_runtime_not_configured"


@asynccontextmanager
async def _unconfigured_runtime(principal, api, args):
    del principal, api, args
    raise RunnerConfigurationError()
    yield  # pragma: no cover


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_shadow_campaign")
    commands = parser.add_subparsers(dest="command", required=True)

    def add_api_url(command: argparse.ArgumentParser) -> None:
        command.add_argument("--api-url", required=True)

    create = commands.add_parser("create")
    add_api_url(create)
    confirmation = create.add_mutually_exclusive_group()
    confirmation.add_argument("--confirm-live", action="store_true")
    confirmation.add_argument("--confirm-offline-fixture", action="store_true")
    create.add_argument("--campaign-key", required=True)
    create.add_argument("--manifest", required=True, type=Path)
    create.add_argument(
        "--profile",
        required=True,
        choices=("docker-smoke-v1", "shadow-full-v1"),
    )
    create.add_argument("--parent-smoke-campaign-id", type=UUID)
    create.add_argument("--name", default="Sana Shadow Campaign")

    listing = commands.add_parser("list")
    add_api_url(listing)

    resume = commands.add_parser("resume")
    add_api_url(resume)
    resume.add_argument("--campaign-id", required=True, type=UUID)
    resume.add_argument("--manifest", required=True, type=Path)

    pause = commands.add_parser("pause")
    add_api_url(pause)
    pause.add_argument("--campaign-id", required=True, type=UUID)
    pause.add_argument("--manifest", required=True, type=Path)

    abort = commands.add_parser("abort")
    add_api_url(abort)
    abort.add_argument("--campaign-id", required=True, type=UUID)
    abort.add_argument("--manifest", required=True, type=Path)

    review = commands.add_parser("review")
    add_api_url(review)
    review.add_argument("--campaign-id", required=True, type=UUID)

    report = commands.add_parser("report")
    add_api_url(report)
    report.add_argument("--campaign-id", required=True, type=UUID)
    return parser


def _read_token(
    environ: Mapping[str, str],
    prompt: Callable[[str], str],
) -> str:
    value = environ.get("SANA_ACCESS_TOKEN", "").strip()
    if value:
        return value
    if not sys.stdin.isatty():
        raise RunnerConfigurationError()
    value = prompt("Sana access token: ").strip()
    if not value:
        raise RunnerConfigurationError()
    return value


def _load_manifest(path: Path):
    if not path.is_file():
        raise InvariantViolation(
            "Manifest file was not found",
            code="manifest_not_found",
        )
    return parse_manifest_bytes(path.read_bytes(), now=datetime.now(UTC))


def _safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_safe(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return _safe(asdict(value))
    raise TypeError(f"Unsupported CLI result type: {type(value).__name__}")


def _emit(value: Any) -> None:
    print(json.dumps(_safe(value), ensure_ascii=False, sort_keys=True))


async def _dispatch(args, token: str, client_factory, runtime_factory) -> int:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(signum, frame) -> None:
        del signum, frame
        if task is not None:
            loop.call_soon_threadsafe(task.cancel)

    signal.signal(signal.SIGTERM, terminate)
    try:
        return await _dispatch_inner(args, token, client_factory, runtime_factory)
    finally:
        signal.signal(signal.SIGTERM, previous)


async def _dispatch_inner(args, token: str, client_factory, runtime_factory) -> int:
    async with client_factory(args.api_url, token) as api:
        identity = await api.authenticate()
        principal = Principal(
            identity.tenant_id,
            identity.user_id,
            identity.issuer,
            identity.subject,
        )
        async with runtime_factory(principal, api, args) as bindings:
            if args.command == "create":
                if (
                    args.profile == "shadow-full-v1"
                    and args.parent_smoke_campaign_id is None
                ):
                    raise InvariantViolation(
                        "Full Campaign requires a parent smoke Campaign",
                        code="parent_smoke_required",
                    )
                manifest = _load_manifest(args.manifest)
                command = bindings.create_command(principal, args, manifest)
                receipt, report = await bindings.runner.create(principal, command)
                _emit({"campaign": receipt, "report": report})
            elif args.command == "list":
                _emit(await bindings.runner.list(principal))
            elif args.command == "resume":
                _emit(
                    await bindings.runner.resume(
                        principal,
                        args.campaign_id,
                        _load_manifest(args.manifest),
                    )
                )
            elif args.command == "pause":
                _emit(
                    await bindings.runner.pause(
                        principal,
                        args.campaign_id,
                        _load_manifest(args.manifest),
                    )
                )
            elif args.command == "abort":
                _emit(
                    await bindings.runner.abort(
                        principal,
                        args.campaign_id,
                        _load_manifest(args.manifest),
                    )
                )
            elif args.command == "review":
                _emit(await bindings.review(principal, args.campaign_id))
            elif args.command == "report":
                _emit(await bindings.runner.report(principal, args.campaign_id))
            else:  # pragma: no cover
                raise AssertionError("unknown command")
    return 0


def _error_code(error: BaseException) -> str:
    if isinstance(error, ShadowAPIError):
        return error.code
    if isinstance(error, InvariantViolation):
        return error.code
    if isinstance(error, RunnerConfigurationError):
        return error.code
    return "runner_internal_error"


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory=ShadowCandidateAPI,
    runtime_factory=shadow_runtime,
    environ: Mapping[str, str] | None = None,
    token_prompt: Callable[[str], str] = getpass.getpass,
) -> int:
    args = _parser().parse_args(argv)
    selected_environ = os.environ if environ is None else environ
    offline_fixture = selected_environ.get(
        "SANA_SHADOW_OFFLINE_FIXTURE",
        "",
    ).strip().casefold() in {"1", "true", "yes", "on"}
    if args.command == "create":
        if not args.confirm_live and not args.confirm_offline_fixture:
            code = (
                "offline_fixture_confirmation_required"
                if offline_fixture
                else "live_confirmation_required"
            )
            print(f"shadow_campaign_error:{code}", file=sys.stderr)
            return 2
        if args.confirm_offline_fixture and not offline_fixture:
            print(
                "shadow_campaign_error:offline_fixture_environment_required",
                file=sys.stderr,
            )
            return 2
        if args.confirm_live and offline_fixture:
            print(
                "shadow_campaign_error:live_confirmation_forbidden_in_fixture",
                file=sys.stderr,
            )
            return 2
    try:
        token = _read_token(selected_environ, token_prompt)
        return asyncio.run(_dispatch(args, token, client_factory, runtime_factory))
    except KeyboardInterrupt:
        print("shadow_campaign_error:operator_interrupt", file=sys.stderr)
        return 130
    except asyncio.CancelledError:
        print("shadow_campaign_error:operator_interrupt", file=sys.stderr)
        return 130
    except BaseException as error:
        print(f"shadow_campaign_error:{_error_code(error)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
