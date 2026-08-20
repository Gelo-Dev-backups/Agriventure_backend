"""
main.py
Application entrypoint. Wires together middleware (CORS, request-ID
logging, rate limiting), mounts every router under a versioned /api/v1
prefix, serves uploaded crop images statically, and normalizes all error
responses into the same ApiResponse JSON envelope the rest of the API uses
- so a Flutter/React/Vue client only ever has to parse one response shape.

Run locally:
    uvicorn main:app --reload

Run in production (e.g. inside Docker):
    uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
"""

import time
import uuid

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from config import APP_NAME, CORS_ORIGINS, UPLOAD_DIR, logger, close_pool
from routes.auth import router as auth_router, limiter
from routes.farms import router as farms_router
from routes.sensors import router as sensors_router
from routes.crop_analysis import router as crop_analysis_router
from routes.recommendations import router as recommendations_router
from routes.notifications import router as notifications_router
from routes.dashboard import router as dashboard_router
from routes.analytics import router as analytics_router
from routes.realtime import router as realtime_router
from routes.admin import router as admin_router

app = FastAPI(
    title=APP_NAME,
    description="Production backend for the AgriVenture smart-farming platform "
                 "(farm & sensor management, AI crop-disease detection, IoT "
                 "telemetry, recommendations and notifications). "
                 "Frontend-agnostic: consumed by Flutter, web, or any HTTPS client.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ---------------------------------------------------------------------------
# CORS - frontend-agnostic by design (Flutter mobile, web, etc.)
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Static files (uploaded crop images)
# ---------------------------------------------------------------------------
_UPLOADS_ROOT = UPLOAD_DIR.split("/")[0]
app.mount("/uploads", StaticFiles(directory=_UPLOADS_ROOT), name="uploads")


# ---------------------------------------------------------------------------
# Request ID + structured access logging middleware
# ---------------------------------------------------------------------------
@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())
    start = time.time()
    request.state.request_id = request_id

    response = await call_next(request)

    duration_ms = round((time.time() - start) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        f"request_id={request_id} method={request.method} path={request.url.path} "
        f"status={response.status_code} duration_ms={duration_ms}"
    )
    return response


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # Every response here is a live API call, never static content. Without
    # this, intermediaries between the client and us (a Cloudflare Tunnel,
    # a browser, a corporate proxy) are free to cache GET responses per
    # normal HTTP heuristics - so "add a farm, list still shows the old
    # data until you restart the app" is a caching bug, not a app bug.
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


# ---------------------------------------------------------------------------
# Consistent JSON error envelope for every failure mode
# ---------------------------------------------------------------------------
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"success": False, "message": "Validation error", "errors": exc.errors()},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", "unknown")
    logger.error(f"request_id={request_id} unhandled_error={exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "success": False,
            "message": "Internal server error",
            "request_id": request_id,
        },
    )


# FastAPI's HTTPException already returns clean JSON via `detail`; normalize
# it to the same {success, message} envelope used everywhere else.
from fastapi.exceptions import HTTPException as FastAPIHTTPException


@app.exception_handler(FastAPIHTTPException)
async def http_exception_handler(request: Request, exc: FastAPIHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "message": exc.detail},
        headers=exc.headers,
    )


# ---------------------------------------------------------------------------
# Routers - versioned API surface
# ---------------------------------------------------------------------------
API_PREFIX = "/api/v1"

app.include_router(auth_router, prefix=f"{API_PREFIX}/auth", tags=["Auth"])
app.include_router(farms_router, prefix=f"{API_PREFIX}/farms", tags=["Farms"])
app.include_router(sensors_router, prefix=f"{API_PREFIX}/sensors", tags=["Sensors"])
app.include_router(crop_analysis_router, prefix=f"{API_PREFIX}/crop-analysis", tags=["Crop Analysis"])
app.include_router(recommendations_router, prefix=f"{API_PREFIX}/recommendations", tags=["Recommendations"])
app.include_router(notifications_router, prefix=f"{API_PREFIX}/notifications", tags=["Notifications"])
app.include_router(dashboard_router, prefix=f"{API_PREFIX}/dashboard", tags=["Dashboard"])
app.include_router(analytics_router, prefix=f"{API_PREFIX}/analytics", tags=["Analytics"])
app.include_router(realtime_router, prefix=f"{API_PREFIX}/realtime", tags=["Realtime"])
app.include_router(admin_router, prefix=f"{API_PREFIX}/admin", tags=["Admin"])


# ---------------------------------------------------------------------------
# Health check - use for cloud load-balancer / uptime probes
# ---------------------------------------------------------------------------
@app.get("/health", tags=["System"])
def health_check():
    return {"success": True, "message": "ok", "service": APP_NAME}


@app.get("/", tags=["System"])
def root():
    return {"success": True, "message": f"{APP_NAME} is running", "docs": "/docs"}


@app.on_event("shutdown")
def shutdown_event():
    close_pool()
