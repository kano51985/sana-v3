from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from sana.app import worker


def test_worker_refuses_to_start_without_a_concrete_handler_factory(monkeypatch) -> None:
    monkeypatch.delenv("SANA_STEP_HANDLER_FACTORY", raising=False)

    with pytest.raises(RuntimeError, match="refusing to start"):
        worker.create_app()


def test_handler_factory_path_must_be_explicit() -> None:
    with pytest.raises(ValueError, match="module:function"):
        worker.load_handler_factory("ambiguous.path")


def test_celery_entrypoint_evaluates_the_worker_factory() -> None:
    environment = os.environ.copy()
    environment.pop("SANA_STEP_HANDLER_FACTORY", None)

    result = subprocess.run(
        [sys.executable, "-c", "import sana.app.worker_entrypoint"],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "refusing to start an unconfigured worker" in result.stderr
