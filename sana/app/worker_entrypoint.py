"""Celery CLI entrypoint that resolves the deployment-owned step handler."""

from sana.app.worker import create_app


app = create_app()
