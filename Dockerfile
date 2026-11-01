# Build: needs ./index (make index, or pulled from GCS in CI). Run: docker run -p 8000:8000 --env-file .env
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159 AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    FASTEMBED_CACHE_PATH=/opt/fastembed
WORKDIR /app
COPY pyproject.toml uv.lock ./
# Runtime dependencies only: no crawler (Playwright) and no dev tools in the image.
RUN uv sync --frozen --no-default-groups --no-install-project
COPY app/retrieval.py app/constraints.py app/
# Bake the embedding model in, so a cold start never downloads 240 MB from Hugging Face.
RUN .venv/bin/python -c "from fastembed import TextEmbedding; \
from app.retrieval import MODEL; TextEmbedding(MODEL)"

FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
RUN useradd --system --uid 10001 --no-create-home app
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    FASTEMBED_CACHE_PATH=/opt/fastembed HF_HUB_OFFLINE=1 PORT=8000
WORKDIR /app
COPY --from=builder /app/.venv .venv
COPY --from=builder --chown=app /opt/fastembed /opt/fastembed
COPY app app
COPY index index
USER app
EXPOSE 8000
# No access log (it would record client IPs and paths); proxy headers off because the app
# reads X-Forwarded-For itself, trusting only Cloud Run's hop (TRUSTED_PROXY_HOPS).
CMD exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --no-access-log --no-proxy-headers
