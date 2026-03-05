# Chat Recall API

## Overview
FastAPI REST service for Chat Recall SaaS. Handles file uploads, user management, Stripe billing, and webhooks.

## Testing
```
python -m pytest tests/ -x -q --tb=short
```

## Conventions
- FastAPI with async endpoints
- Pydantic models for request/response validation
- JWT auth via NextAuth shared secret
- Read existing code before modifying
- Run tests before declaring work complete
