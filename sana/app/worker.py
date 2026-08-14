"""Celery worker composition boundary.

Concrete search operations are deployment-owned.  The factory is mandatory so a
worker cannot report healthy while silently lacking a step handler.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable

from celery import Celery

from sana.platform.queue.celery_app import celery_app
from sana.platform.queue.tasks import StepHandler, configure_step_handler


HandlerFactory = Callable[[], StepHandler]


def load_handler_factory(import_path: str) -> HandlerFactory:
    module_name, separator, attribute_name = import_path.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError(
            "SANA_STEP_HANDLER_FACTORY must use the 'module:function' format"
        )
    module = importlib.import_module(module_name)
    factory = getattr(module, attribute_name, None)
    if not callable(factory):
        raise TypeError(f"Step handler factory is not callable: {import_path}")
    return factory


def create_app() -> Celery:
    import_path = os.environ.get("SANA_STEP_HANDLER_FACTORY", "").strip()
    if not import_path:
        raise RuntimeError(
            "SANA_STEP_HANDLER_FACTORY is required; refusing to start an "
            "unconfigured worker"
        )
    handler = load_handler_factory(import_path)()
    if not callable(handler):
        raise TypeError("Step handler factory must return a callable")
    configure_step_handler(handler)
    return celery_app
