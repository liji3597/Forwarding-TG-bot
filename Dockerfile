FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY pyproject.toml .
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir \
    "telethon>=1.36.0" \
    "pydantic>=2.0" \
    "PyYAML>=6.0" \
    "aiosqlite>=0.20.0"

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY tg_forwarder ./tg_forwarder
COPY scripts ./scripts

RUN useradd --create-home --uid 10001 appuser && \
    mkdir -p /app/data /app/sessions && \
    chown -R appuser:appuser /app

USER appuser
ENTRYPOINT ["python", "-m", "tg_forwarder"]
