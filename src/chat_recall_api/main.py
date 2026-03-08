"""FastAPI application for Chat Recall API."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from chat_recall_api.config import get_settings
from chat_recall_api.deps import close_db_pool, init_db_pool
from chat_recall_api.routers.billing import router as billing_router
from chat_recall_api.routers.stats import router as stats_router
from chat_recall_api.routers.upload import router as upload_router
from chat_recall_api.routers.users import router as users_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — init and cleanup database pool."""
    settings = get_settings()
    if settings.database_url:
        await init_db_pool(settings.database_url)
    yield
    await close_db_pool()


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

app.include_router(users_router)
app.include_router(billing_router)
app.include_router(stats_router)
app.include_router(upload_router)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy"}
