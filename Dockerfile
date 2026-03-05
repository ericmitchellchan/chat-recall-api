FROM python:3.12-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-editable
COPY src/ src/
RUN uv sync --frozen --no-dev
EXPOSE 8081
CMD ["uv", "run", "uvicorn", "chat_recall_api.main:app", "--host", "0.0.0.0", "--port", "8081"]
