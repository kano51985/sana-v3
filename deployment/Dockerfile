FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml alembic.ini requirements.txt ./
COPY alembic ./alembic
COPY sana ./sana
COPY interfaces ./interfaces
COPY .streamlit ./.streamlit

RUN python -m pip install --no-cache-dir .

EXPOSE 8000 8501
