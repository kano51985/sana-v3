FROM python:3.12-slim

ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY deployment/requirements-runtime.txt ./deployment/requirements-runtime.txt
RUN python -m pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" \
    "setuptools>=75" wheel -r deployment/requirements-runtime.txt

COPY pyproject.toml alembic.ini ./
COPY alembic ./alembic
COPY sana ./sana
COPY interfaces ./interfaces
COPY .streamlit ./.streamlit

RUN python -m pip install --no-cache-dir --no-deps --no-build-isolation .

EXPOSE 8000 8501
