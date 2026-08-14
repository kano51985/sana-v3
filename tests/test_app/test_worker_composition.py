from __future__ import annotations

import os

import pytest

from sana.app import worker


def test_worker_refuses_to_start_without_a_concrete_handler_factory(monkeypatch) -> None:
    monkeypatch.delenv("SANA_STEP_HANDLER_FACTORY", raising=False)

    with pytest.raises(RuntimeError, match="refusing to start"):
        worker.create_app()


def test_handler_factory_path_must_be_explicit() -> None:
    with pytest.raises(ValueError, match="module:function"):
        worker.load_handler_factory("ambiguous.path")
