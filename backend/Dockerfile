FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONPATH=/srv/backend FASTEMBED_CACHE_PATH=/models PATH=/srv/backend/.venv/bin:$PATH
WORKDIR /srv/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
# Bake embedding + reranker models into the image (no download at runtime, works offline).
RUN python -c "from fastembed import TextEmbedding; from fastembed.rerank.cross_encoder import TextCrossEncoder; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/models'); TextCrossEncoder('Xenova/ms-marco-MiniLM-L-6-v2', cache_dir='/models')"
COPY backend/ ./
COPY knowledge/ /srv/knowledge/
RUN useradd -r -u 10001 app && mkdir -p /srv/infra/dev-jwt && chown -R app /srv/infra /models
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
