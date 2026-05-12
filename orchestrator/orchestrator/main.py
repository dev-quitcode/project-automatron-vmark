"""FastAPI application entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

logging.getLogger("orchestrator").setLevel(logging.INFO)

import socketio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from orchestrator.api.routes import router as api_router
from orchestrator.api.webhook_github import router as webhook_router
from orchestrator.api.socket_server import sio
from orchestrator.config import settings
from orchestrator.models.project import init_db

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application startup / shutdown lifecycle."""
    logger.info("Automatron Orchestrator starting up...")
    await init_db(settings.sqlite_db_path)
    logger.info("Database initialized at %s", settings.sqlite_db_path)
    yield
    logger.info("Automatron Orchestrator shutting down...")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Automatron Orchestrator",
        version="0.1.0",
        description="Autonomous software development engine",
        lifespan=lifespan,
    )

    # CORS (private network, permissive for MVP)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # REST API routes
    app.include_router(api_router, prefix="/api")
    app.include_router(webhook_router, prefix="/api")

    # Health endpoint
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


# --- ASGI app with Socket.IO ---
fastapi_app = create_app()
combined_app = socketio.ASGIApp(sio, other_asgi_app=fastapi_app)
app = combined_app

# Register Socket.IO event handlers.
from orchestrator.api import websocket as _websocket  # noqa: F401,E402

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "orchestrator.main:combined_app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
