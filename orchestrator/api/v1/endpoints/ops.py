import logging
import os
import httpx
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST as PROMETHEUS_CONTENT_TYPE

from orchestrator.core.config import EnvConfig, get_settings, get_runtime_state, RuntimeState
from orchestrator.core.state import (
    INCIDENT_LOG, RESOLVED_INCIDENT_LOG, AUDIT_LOG,
    INCIDENT_GAUGE, CUSTOMER_EVENTS, LATEST_CUSTOMER_EVENTS,
)
from orchestrator.persistence import persistence_store
from orchestrator.schemas.requests import ConfigUpdateRequest

logger = logging.getLogger("orchestrator.api.ops")
router = APIRouter()

@router.get("/")
async def root() -> Dict[str, Any]:
    return {"status": "ok", "service": "orchestrator"}

@router.get("/health", summary="Health check for orchestrator")
async def health() -> Dict[str, str]:
    return {"status": "ok"}

@router.get("/metrics")
async def metrics():
    data = generate_latest()
    return Response(content=data, media_type=PROMETHEUS_CONTENT_TYPE)

@router.get("/config", summary="Inspect orchestrator runtime configuration")
async def get_config(
    settings: EnvConfig = Depends(get_settings),
    state: RuntimeState = Depends(get_runtime_state),
) -> Dict[str, Any]:
    services_snapshot: Dict[str, Any] = {}
    for service_name, service_cfg in settings.services.items():
        region_cfg = settings.get_service_region(service_name)
        services_snapshot[service_name] = {
            "default_region": service_cfg.default_region,
            "base_url": region_cfg.base_url,
            "timeout_seconds": region_cfg.timeout_seconds,
            "verify_tls": region_cfg.verify_tls,
            "retry": region_cfg.retry.model_dump(),
            "circuit_breaker": (
                region_cfg.circuit_breaker.model_dump()
                if region_cfg.circuit_breaker
                else None
            ),
        }
    return {
        "environment": settings.env_name,
        "dry_run": state.dry_run,
        "services": services_snapshot,
        "endpoints": {
            "isp_base_url": settings.isp_base_url,
            "geogrid_base_url": settings.geogrid_base_url,
        },
    }

@router.post(
    "/config",
    summary="Update runtime configuration",
)
async def update_config(
    payload: ConfigUpdateRequest,
    state: RuntimeState = Depends(get_runtime_state),
) -> Dict[str, Any]:
    updated: Dict[str, Any] = {}
    if payload.dry_run is not None:
        state.dry_run = payload.dry_run
        updated["dry_run"] = state.dry_run

    return {
        "updated": updated,
        "current": {"dry_run": state.dry_run},
    }


@router.post(
    "/ops/refresh-isp-token",
    summary="Refresh ISP-Cube bearer token",
)
async def refresh_isp_token(
    settings: EnvConfig = Depends(get_settings),
) -> Dict[str, Any]:
    """
    Solicita un nuevo token a ISP-Cube y lo actualiza en memoria.
    No requiere reiniciar el contenedor.
    """
    isp_base_url = os.getenv("ISP_BASE_URL", "").rstrip("/")
    api_key = os.getenv("ISP_API_KEY", "")
    client_id = os.getenv("ISP_CLIENT_ID", "")
    username = os.getenv("ISP_USERNAME", "")
    password = os.getenv("ISP_PASSWORD", "")

    if not all([isp_base_url, api_key, client_id, username, password]):
        return {
            "status": "error",
            "message": "Missing ISP credentials in environment variables",
        }

    token_url = f"{isp_base_url}/sanctum/token"
    
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                token_url,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                    "api-key": api_key,
                    "client-id": client_id,
                    "login-type": "api",
                },
                json={
                    "username": username,
                    "password": password,
                },
            )
            response.raise_for_status()
            data = response.json()
            new_token = data.get("token")
            
            if not new_token:
                return {
                    "status": "error",
                    "message": "Response did not contain a token",
                    "response": data,
                }

        # Update the token in environment (for this process)
        new_bearer = f"Bearer {new_token}"
        os.environ["ISP_BEARER"] = new_bearer

        # Update in settings (reload headers)
        isp_service = settings.services.get("isp")
        if isp_service:
            region = isp_service.regions.get(isp_service.default_region)
            if region:
                region.default_headers["Authorization"] = new_bearer

        logger.info("ISP token refreshed successfully")
        return {
            "status": "success",
            "message": "Token refreshed and updated in memory",
        }

    except httpx.HTTPStatusError as e:
        logger.error("Failed to refresh ISP token: %s", e)
        return {
            "status": "error",
            "message": f"HTTP error: {e.response.status_code}",
            "detail": e.response.text[:500],
        }
    except Exception as e:
        logger.error("Failed to refresh ISP token: %s", e)
        return {
            "status": "error",
            "message": str(e),
        }

