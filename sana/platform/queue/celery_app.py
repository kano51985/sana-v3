"""Celery configuration; PostgreSQL remains the workflow authority."""

from __future__ import annotations

import os

from celery import Celery
from kombu import Queue


QUEUE_NAMES = ("fast", "research", "crawl", "maintenance")


def create_celery_app(broker_url: str) -> Celery:
    if not broker_url:
        raise ValueError("Celery broker URL cannot be empty")
    app = Celery("sana", broker=broker_url)
    app.conf.update(
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_ignore_result=True,
        result_backend=None,
        task_default_queue="fast",
        task_queues=tuple(Queue(name) for name in QUEUE_NAMES),
        broker_connection_retry_on_startup=True,
        task_serializer="json",
        accept_content=("json",),
        timezone="UTC",
        enable_utc=True,
    )
    return app


celery_app = create_celery_app(
    os.environ.get("SANA_CELERY_BROKER_URL", "redis://localhost:6379/0")
)
