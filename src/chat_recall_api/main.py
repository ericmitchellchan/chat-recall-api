"""FastAPI application for Chat Recall API."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from chat_recall_api.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — init and cleanup resources."""
    # TODO: Initialize database connection pool
    yield
    # TODO: Close database connection pool


app = FastAPI(
    title="Chat Recall API",
    version="0.1.0",
    lifespan=lifespan,
)

settings = get_settings()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy"}
