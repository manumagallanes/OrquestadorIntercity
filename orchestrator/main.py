import logging
import os
import time
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from .api.v1.router import api_router
from .core.config import (
    EnvConfig,
    SETTINGS,
    REQUEST_ID_HEADER,
    request_id_ctx,
    setup_logging,
    RequestIdLoggingFilter,
)
from .core.state import (
    HTTP_REQUEST_LATENCY,
    HTTP_REQUEST_COUNTER,
    initialize_metrics,
)
from .logic.seed import ensure_customer_seed

# Initialize Logging
setup_logging()
logger = logging.getLogger("orchestrator.main")

app = FastAPI(
    title="Intercity Orchestrator",
    description=(
        "Synthetic orchestrator that coordinates ISP-Cube, GeoGrid and SmartOLT "
        "for local integration tests."
    ),
    version="0.1.0",
)

@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid4())
    token = request_id_ctx.set(request_id)
    try:
        response = await call_next(request)
    finally:
        request_id_ctx.reset(token)
    response.headers[REQUEST_ID_HEADER] = request_id
    return response

@app.middleware("http")
async def metrics_middleware(request, call_next):
    start_time = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = getattr(response, "status_code", 500)
        return response
    except Exception as exc:
        raise exc
    finally:
        duration = time.perf_counter() - start_time
        endpoint = request.url.path
        method = request.method
        HTTP_REQUEST_LATENCY.labels(endpoint=endpoint, method=method).observe(duration)
        HTTP_REQUEST_COUNTER.labels(
            endpoint=endpoint, method=method, status=str(status_code)
        ).inc()

@app.on_event("startup")
async def startup_event() -> None:
    logger.info("Starting up Orchestrator...")
    initialize_metrics()
    await ensure_customer_seed(SETTINGS)

app.include_router(api_router)

if __name__ == "__main__":
    uvicorn.run(
        "orchestrator.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=bool(int(os.getenv("UVICORN_RELOAD", "0"))),
    )
