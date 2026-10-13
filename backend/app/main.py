# Path: app/main.py
# Description: This file contains the main FastAPI application.

import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import API_PREFIX, get_settings
from app.logger import configure_logging, get_logger
from app.routes import main_router
from app.utils import archive, request_context

# Get the settings
settings = get_settings()

# Get the logger
logger = get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    await archive.ensure_archive_ready()
    logger.info("Claude Code Proxy backend started")
    try:
        yield
    finally:
        await archive.close_archive_store()


app = FastAPI(
    title="Claude Code Proxy",
    description="Rotates pooled Claude subscriptions and tracks per-user usage.",
    version="0.1.0",
    lifespan=lifespan,
    openapi_url=f"{API_PREFIX}/openapi.json",
    docs_url=f"{API_PREFIX}/docs",
    redoc_url=f"{API_PREFIX}/redoc",
)


# Applied to every API response unless a route set its own value. The API returns session tokens, keys and
# usage data, so nothing may be cached, sniffed into another content type, framed, or leak via Referer.
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


# Advanced Middleware - request correlation + timing
class CustomMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        archive_event_id = getattr(request.state, "archive_event_id", None)
        request_id = request_context.set_request_context(
            request_id=f"req_{archive_event_id.replace('-', '')}" if isinstance(archive_event_id, str) else None,
            endpoint=request.url.path,
            method=request.method,
        )
        try:
            start_time = time.perf_counter()
            response = await call_next(request)
            process_time = time.perf_counter() - start_time

            response.headers["X-Process-Time"] = str(process_time)
            response.headers["X-Request-Id"] = request_id
            for header, value in _SECURITY_HEADERS.items():
                response.headers.setdefault(header, value)

            logger.info(f"Request completed: {request.method} {request.url.path} - Status: {response.status_code} - Time: {process_time:.3f}s")
            return response
        finally:
            request_context.clear_request_context()


# CORS Middleware (added before CustomMiddleware so it runs outermost). The dashboard shares the proxy's origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(CustomMiddleware)
app.add_middleware(archive.RawArchiveMiddleware, settings=settings)

# Every router is mounted under /api: the proxy at /api/v1/messages, the admin API at /api/v1/{auth,accounts,users,stats}.
app.include_router(main_router, prefix=API_PREFIX)


@app.get("/health", tags=["Internal Services"], include_in_schema=False)
def health_check():
    return {"status": "healthy"}


@app.get(f"{API_PREFIX}/health", tags=["Internal Services"], include_in_schema=False)
def public_health_check():
    """Health endpoint exposed through the same-origin reverse proxy."""
    response = JSONResponse({"status": "healthy"})
    slot = os.getenv("DEPLOYMENT_SLOT")
    if slot and slot != "stable":
        response.headers["X-Deployment-Slot"] = slot
    return response
