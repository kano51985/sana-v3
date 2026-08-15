FROM python:3.12-slim@sha256:dd29372629eeba2dd003fd9e9d35a5b8236c44727875a0364254b5127af88e65

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
COPY scripts ./scripts
COPY evals ./evals
COPY .streamlit ./.streamlit

RUN python -m pip install --no-cache-dir --no-deps --no-build-isolation .

RUN groupadd --system sana \
    && useradd --system --gid sana --home-dir /app --no-create-home sana \
    && mkdir -p /var/lib/sana/artifacts \
    && chown -R sana:sana /app /var/lib/sana

ARG OCI_REVISION=unknown
LABEL org.opencontainers.image.revision="${OCI_REVISION}"

USER sana

EXPOSE 8000 8501
