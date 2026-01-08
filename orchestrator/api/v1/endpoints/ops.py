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

# Postponing `reset` implementation until I locate `_ensure_customer_seed`. 
# Or I can just omit it for now and add it to `ops.py` later.
# Users heavily rely on `reset` during development.
# I'll search for `_ensure_customer_seed` in main.py.
