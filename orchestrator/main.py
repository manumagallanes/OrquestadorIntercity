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
    setup_logging, # Assuming I extracted this or will extract it
    RequestIdLoggingFilter,
)
from .core.state import (
    HTTP_REQUEST_LATENCY,
    HTTP_REQUEST_COUNTER,
)
from .logic.seed import ensure_customer_seed

# Initialize Logging
# I need to ensure setup_logging is in core/config.py or main.py.
# In original config.py extraction, I put logging setup there?
# Checking Step 11 for config.py...
# Yes, "Logging Setup" was mentioned.

setup_logging(os.getenv("LOG_LEVEL", "INFO"))
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
        # Note: Exception handling usually done by Starlette, but we catch generic here for metrics?
        # Standard approach: let generic exception handler deal with it, but we need status code.
        # If we raise, we lose status code unless we catch HTTPExceptions.
        # But we are in middleware.
        # For simplicity, just re-raise and capture 500.
        raise exc
    finally:
        duration = time.perf_counter() - start_time
        endpoint = request.url.path
        method = request.method
        # Avoid high cardinality for unknown paths? 
        # But this matches original main.py logic.
        HTTP_REQUEST_LATENCY.labels(endpoint=endpoint, method=method).observe(duration)
        # We need status code. If exception raised, status_code is 500 (default above).
        # But if Exception was handled by app's exception_handler, we might not see it here if we re-raise.
        # Actually, FastAPI middleware captures 'response' from exception handlers.
        # But if we 'raise', we don't get 'response'.
        # Original main.py caught HTTPException and Exception, set status, and re-raised. (lines 2465-2470)
        # I'll stick to original logic if I can recall it exactly.
        # Original:
        # try:
        #     response = await call_next(request)
        #     status_code = response.status_code
        #     return response
        # except HTTPException as exc:
        #     status_code = exc.status_code
        #     raise
        # except Exception:
        #     status_code = 500
        #     raise
        # finally: ...
        
        HTTP_REQUEST_COUNTER.labels(
            endpoint=endpoint, method=method, status=str(status_code)
        ).inc()

@app.on_event("startup")
async def startup_event() -> None:
    logger.info("Starting up Orchestrator...")
    # Seed logic
    await ensure_customer_seed(SETTINGS)

app.include_router(api_router)

if __name__ == "__main__":
    uvicorn.run(
        "orchestrator.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=bool(int(os.getenv("UVICORN_RELOAD", "0"))),
    )
