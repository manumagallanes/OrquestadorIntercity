import time
import httpx
from typing import Any, Dict, Optional, Tuple
from fastapi import HTTPException, status
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import EnvConfig, CircuitBreakerSettings, RETRYABLE_STATUS_CODES, REQUEST_ID_HEADER, request_id_ctx, get_circuit_lock, get_circuit_state
from .state import INTEGRATION_ERROR_COUNTER, logger

class RetryableHTTPStatus(Exception):
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self.payload = payload
        super().__init__(f"Retryable HTTP status: {status_code}")

def _safe_response_payload(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text

def _record_circuit_success(key: str) -> None:
    state = get_circuit_state(key)
    state.failure_count = 0
    state.opened_at = None

def _record_circuit_failure(
    key: str, breaker_cfg: CircuitBreakerSettings, *, timestamp: float
) -> None:
    state = get_circuit_state(key)
    state.failure_count += 1
    if state.failure_count >= breaker_cfg.failure_threshold:
        state.opened_at = timestamp

async def fetch_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    service: str,
    settings: EnvConfig,
    region_name: Optional[str] = None,
    **kwargs: Any,
) -> Any:
    region_cfg, resolved_region = settings.resolve_service_region(
        service, region=region_name
    )
    breaker_cfg = region_cfg.circuit_breaker
    breaker_key = f"{service.lower()}:{resolved_region}"

    if breaker_cfg:
        breaker_lock = get_circuit_lock(breaker_key)
        async with breaker_lock:
            state = get_circuit_state(breaker_key)
            if state.opened_at is not None:
                elapsed = time.monotonic() - state.opened_at
                if elapsed < breaker_cfg.recovery_timeout_seconds:
                    retry_after = round(
                        breaker_cfg.recovery_timeout_seconds - elapsed, 2
                    )
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail={
                            "message": "Circuit breaker open for upstream service",
                            "service": service,
                            "region": resolved_region,
                            "retry_after_seconds": retry_after,
                        },
                    )
                state.opened_at = None
                state.failure_count = 0
        breaker_lock_for_updates = breaker_lock
    else:
        breaker_lock_for_updates = None

    retry_cfg = region_cfg.retry
    multiplier = max(retry_cfg.backoff_initial_seconds, 0.1)

    base_headers = region_cfg.resolved_headers()
    extra_headers = kwargs.pop("headers", None)
    merged_headers: Optional[Dict[str, str]]
    if extra_headers is None:
        merged_headers = base_headers if base_headers else None
    else:
        merged_headers = dict(base_headers) if base_headers else {}
        merged_headers.update(extra_headers)
    request_id = request_id_ctx.get("-")
    if request_id and request_id != "-":
        if merged_headers is None:
            merged_headers = {}
        merged_headers.setdefault(REQUEST_ID_HEADER, request_id)

    response: Optional[httpx.Response] = None
    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(retry_cfg.max_attempts),
            wait=wait_exponential(multiplier=multiplier, max=retry_cfg.backoff_max_seconds),
            retry=retry_if_exception_type((httpx.RequestError, RetryableHTTPStatus)),
            reraise=True,
        ):
            with attempt:
                response = await client.request(
                    method,
                    url,
                    headers=merged_headers.copy() if merged_headers else None,
                    **kwargs,
                )
                logger.info(
                    "HTTP %s %s -> %s",
                    method.upper(),
                    response.request.url,
                    response.status_code,
                )
                if response.status_code in RETRYABLE_STATUS_CODES:
                    payload = _safe_response_payload(response)
                    raise RetryableHTTPStatus(response.status_code, payload)
    except RetryableHTTPStatus as exc:
        INTEGRATION_ERROR_COUNTER.labels(service=service, status=str(exc.status_code)).inc()
        if breaker_cfg and breaker_lock_for_updates:
            async with breaker_lock_for_updates:
                _record_circuit_failure(
                    breaker_key, breaker_cfg, timestamp=time.monotonic()
                )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "message": "Upstream service returned error",
                "service": service,
                "region": resolved_region,
                "status_code": exc.status_code,
                "upstream_detail": exc.payload,
            },
        ) from exc
    except httpx.RequestError as exc:
        INTEGRATION_ERROR_COUNTER.labels(service=service, status="request_error").inc()
        if breaker_cfg and breaker_lock_for_updates:
            async with breaker_lock_for_updates:
                _record_circuit_failure(
                    breaker_key, breaker_cfg, timestamp=time.monotonic()
                )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": "Error communicating with upstream service",
                "service": service,
                "region": resolved_region,
                "error": str(exc),
            },
        ) from exc

    if response is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "message": "No response returned from upstream service",
                "service": service,
                "region": resolved_region,
            },
        )

    if breaker_cfg and breaker_lock_for_updates:
        async with breaker_lock_for_updates:
            _record_circuit_success(breaker_key)

    if response.status_code >= 400:
        INTEGRATION_ERROR_COUNTER.labels(service=service, status=str(response.status_code)).inc()
        detail = _safe_response_payload(response)
        raise HTTPException(
            status_code=response.status_code,
            detail={
                "service": service,
                "region": resolved_region,
                "upstream_detail": detail,
            },
        )
    if response.status_code == status.HTTP_204_NO_CONTENT:
        return {}
    if response.status_code >= 300:
        detail = _safe_response_payload(response)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "service": service,
                "region": resolved_region,
                "upstream_detail": detail,
                "status_code": response.status_code,
            },
        )
    try:
        return response.json()
    except ValueError:
        detail = _safe_response_payload(response)
        INTEGRATION_ERROR_COUNTER.labels(service=service, status="invalid_json").inc()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "service": service,
                "region": resolved_region,
                "message": "Invalid JSON payload from upstream",
                "upstream_detail": detail,
                "status_code": response.status_code,
            },
        )
