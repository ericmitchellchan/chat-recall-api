# Chat Recall API

REST API for Chat Recall — uploads, user management, billing.

## Setup

```bash
uv sync
cp .env.example .env
# Edit .env with your values
uvicorn chat_recall_api.main:app --reload
```

## Testing

```bash
python -m pytest tests/ -x -q --tb=short
```
