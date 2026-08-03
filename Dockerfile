FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AIRLINE_MVP_DATA_DIR=/app/data \
    AIRLINE_MVP_RUNTIME_DIR=/app/.runtime \
    HOME=/app/.runtime \
    XDG_CACHE_HOME=/app/.runtime/cache \
    HF_HOME=/app/.runtime/huggingface

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --upgrade pip && python -m pip install .

COPY data ./data
COPY evals ./evals
COPY scripts ./scripts
COPY docs ./docs

RUN addgroup --system airline \
    && adduser --system --ingroup airline airline \
    && mkdir -p /app/.runtime \
    && chown -R airline:airline /app

USER airline

EXPOSE 8000
CMD ["airline-mvp-api"]
