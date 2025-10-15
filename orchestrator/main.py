import logging
import time
import os
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from typing_extensions import Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from prometheus_client import Counter, Gauge, Histogram, generate_latest
from starlette.responses import Response


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("orchestrator")


class CustomerSyncRequest(BaseModel):
    customer_id: int = Field(..., ge=1, description="Identifier of the ISP customer")


class ProvisionRequest(BaseModel):
    customer_id: Optional[int] = Field(default=None, ge=1)
    olt_id: int = Field(..., ge=1)
    board: int = Field(..., ge=0)
    pon_port: int = Field(..., ge=0)
    onu_sn: str = Field(..., min_length=6, max_length=32)
    profile: Optional[str] = Field(default="Internet_100M")
    dry_run: Optional[bool] = Field(
        default=None,
        description="Override orchestrator dry-run flag when provided",
    )


class DecommissionRequest(BaseModel):
    customer_id: int = Field(..., ge=1)
    dry_run: Optional[bool] = Field(default=None)


class ConfigUpdateRequest(BaseModel):
    dry_run: Optional[bool] = Field(
        default=None, description="Override runtime dry-run flag"
    )


class AuditEntry(BaseModel):
    action: Literal["sync", "provision", "decommission"]
    customer_id: Optional[int] = None
    user: Optional[str] = None
    dry_run: Optional[bool] = None
    status: Literal["success", "error"]
    detail: Dict[str, Any] = Field(default_factory=dict)
    timestamp: str


class EnvConfig(BaseModel):
    isp_base_url: str
    geogrid_base_url: str
    smartolt_base_url: str
    dry_run_default: bool


def load_env_config() -> EnvConfig:
    dry_run_env = os.getenv("DRY_RUN", "false").lower()
    dry_run_default = dry_run_env in {"1", "true", "yes", "on"}
    return EnvConfig(
        isp_base_url=os.getenv("ISP_BASE_URL", "http://isp-mock:8001"),
        geogrid_base_url=os.getenv("GEOGRID_BASE_URL", "http://localhost:8002"),  # Apunta al mock local
        smartolt_base_url=os.getenv("SMARTOLT_BASE_URL", "http://smartolt-mock:8003"),
        dry_run_default=dry_run_default,
    )


SETTINGS = load_env_config()
APP_USER_HEADER = os.getenv("ORCHESTRATOR_USER_HEADER", "X-Orchestrator-User")


@dataclass
class RuntimeState:
    dry_run: bool = SETTINGS.dry_run_default


runtime_state = RuntimeState()


def get_settings() -> EnvConfig:
    return SETTINGS


def get_runtime_state() -> RuntimeState:
    return runtime_state


HTTP_REQUEST_LATENCY = Histogram(
    "orchestrator_request_latency_seconds",
    "Latency of orchestrator HTTP requests",
    ["endpoint", "method"],
)
HTTP_REQUEST_COUNTER = Counter(
    "orchestrator_requests_total",
    "Total HTTP requests handled by orchestrator",
    ["endpoint", "method", "status"],
)
SYNC_COUNTER = Counter(
    "orchestrator_customer_sync_total",
    "Customer sync results",
    ["result"],
)
PROVISION_COUNTER = Counter(
    "orchestrator_provision_total",
    "Provision ONU results",
    ["result"],
)
DECOMMISSION_COUNTER = Counter(
    "orchestrator_decommission_total",
    "Customer decommission results",
    ["result"],
)
INCIDENT_COUNTER = Counter(
    "orchestrator_incidents_total",
    "Incidents recorded by orchestrator",
    ["kind"],
)
INCIDENT_GAUGE = Gauge(
    "orchestrator_incidents_buffer_size",
    "Current number of incidents retained in buffer",
)

PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


REQUIRED_CUSTOMER_FIELDS: List[str] = [
    "lat",
    "lon",
    "odb",
    "olt_id",
    "board",
    "pon",
    "onu_sn",
]

INCIDENT_LOG: deque[Dict[str, Any]] = deque(
    maxlen=int(os.getenv("INCIDENT_BUFFER_SIZE", "200"))
)
AUDIT_LOG: deque[AuditEntry] = deque(maxlen=int(os.getenv("AUDIT_BUFFER_SIZE", "500")))


def record_incident(kind: str, detail: Dict[str, Any]) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        **detail,
    }
    INCIDENT_LOG.append(entry)
    INCIDENT_COUNTER.labels(kind=kind).inc()
    INCIDENT_GAUGE.set(len(INCIDENT_LOG))
    logger.warning("Incident recorded %s :: %s", kind, detail)


def record_audit(entry: AuditEntry) -> None:
    AUDIT_LOG.append(entry)
    logger.info(
        "Audit action=%s customer_id=%s status=%s user=%s dry_run=%s detail=%s",
        entry.action,
        entry.customer_id,
        entry.status,
        entry.user,
        entry.dry_run,
        entry.detail,
    )


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def ensure_customer_ready(customer: Dict[str, Any], action: str) -> None:
    customer_id = customer.get("customer_id")
    if not customer.get("integration_enabled"):
        record_incident(
            "integration_disabled",
            {"customer_id": customer_id, "action": action},
        )
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail={
                "message": "Customer not eligible for automation",
                "customer_id": customer_id,
                "reason": "integration_disabled",
            },
        )

    missing_fields = [
        field
        for field in REQUIRED_CUSTOMER_FIELDS
        if _is_blank(customer.get(field))
    ]
    if missing_fields:
        record_incident(
            "missing_fields",
            {
                "customer_id": customer_id,
                "action": action,
                "missing": missing_fields,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "Customer has incomplete network metadata",
                "customer_id": customer_id,
                "missing_fields": missing_fields,
            },
        )


NETWORK_KEYS: List[str] = ["olt_id", "board", "pon", "onu_sn"]


def ensure_customer_has_network_keys(customer: Dict[str, Any], action: str) -> None:
    missing = [
        field
        for field in NETWORK_KEYS
        if _is_blank(customer.get(field))
    ]
    if missing:
        record_incident(
            "missing_network_keys",
            {
                "customer_id": customer.get("customer_id"),
                "action": action,
                "missing": missing,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "Customer missing network identifiers",
                "customer_id": customer.get("customer_id"),
                "missing_fields": missing,
            },
        )


def ensure_alignment(customer: Dict[str, Any], payload: ProvisionRequest) -> None:
    mismatches = {}
    if customer.get("olt_id") != payload.olt_id:
        mismatches["olt_id"] = {
            "customer": customer.get("olt_id"),
            "payload": payload.olt_id,
        }
    if customer.get("board") != payload.board:
        mismatches["board"] = {
            "customer": customer.get("board"),
            "payload": payload.board,
        }
    if customer.get("pon") != payload.pon_port:
        mismatches["pon_port"] = {
            "customer": customer.get("pon"),
            "payload": payload.pon_port,
        }
    if customer.get("onu_sn") != payload.onu_sn:
        mismatches["onu_sn"] = {
            "customer": customer.get("onu_sn"),
            "payload": payload.onu_sn,
        }

    if mismatches:
        record_incident(
            "hardware_mismatch",
            {
                "customer_id": customer.get("customer_id"),
                "mismatches": mismatches,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Provisioning payload differs from ISP record",
                "customer_id": customer.get("customer_id"),
                "mismatches": mismatches,
            },
        )


def ensure_customer_inactive(customer: Dict[str, Any]) -> None:
    if customer.get("status") == "inactive":
        return
    record_incident(
        "decommission_status_active",
        {
            "customer_id": customer.get("customer_id"),
            "status": customer.get("status"),
        },
    )
    raise HTTPException(
        status_code=status.HTTP_412_PRECONDITION_FAILED,
        detail={
            "message": "Customer must be inactive before decommission",
            "customer_id": customer.get("customer_id"),
            "status": customer.get("status"),
        },
    )

app = FastAPI(
    title="Intercity Orchestrator",
    description=(
        "Synthetic orchestrator that coordinates ISP-Cube, GeoGrid and SmartOLT "
        "for local integration tests."
    ),
    version="0.1.0",
)


@app.middleware("http")
async def metrics_middleware(request, call_next):
    start_time = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = getattr(response, "status_code", 500)
        return response
    except HTTPException as exc:
        status_code = exc.status_code
        raise
    except Exception:
        status_code = 500
        raise
    finally:
        duration = time.perf_counter() - start_time
        endpoint = request.url.path
        method = request.method
        HTTP_REQUEST_LATENCY.labels(endpoint=endpoint, method=method).observe(duration)
        HTTP_REQUEST_COUNTER.labels(
            endpoint=endpoint, method=method, status=str(status_code)
        ).inc()


async def fetch_json(
    client: httpx.AsyncClient, method: str, url: str, **kwargs: Any
) -> Any:
    response = await client.request(method, url, **kwargs)
    logger.info(
        "HTTP %s %s -> %s",
        method.upper(),
        response.request.url,
        response.status_code,
    )
    if response.status_code >= 400:
        detail: Any
        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        raise HTTPException(
            status_code=response.status_code,
            detail={"upstream_detail": detail},
        )
    if response.status_code == status.HTTP_204_NO_CONTENT:
        return {}
    return response.json()


@app.post(
    "/sync/customer",
    status_code=status.HTTP_200_OK,
    summary="Sync customer data into GeoGrid",
)
async def sync_customer(
    payload: CustomerSyncRequest,
    request: Request,
    settings: EnvConfig = Depends(get_settings),
) -> Dict[str, Any]:
    user = request.headers.get(APP_USER_HEADER, "ui")
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        async with httpx.AsyncClient(
            base_url=settings.isp_base_url, timeout=10.0
        ) as client:
            customer = await fetch_json(client, "GET", f"/customers/{payload.customer_id}")

        ensure_customer_ready(customer, action="sync")

        feature_payload = {
            "name": f"{customer['customer_id']} _ {customer['name']}",
            "location": {"lat": customer["lat"], "lon": customer["lon"]},
            "attrs": {
                "customer_id": customer["customer_id"],
                "address": customer["address"],
                "city": customer["city"],
                "odb": customer["odb"],
                "olt_id": customer["olt_id"],
                "board": customer["board"],
                "pon": customer["pon"],
                "onu_sn": customer["onu_sn"],
            },
        }

        async with httpx.AsyncClient(
            base_url=settings.geogrid_base_url, timeout=10.0
        ) as client:
            response = await client.post("/features", json=feature_payload)
            logger.info(
                "HTTP POST %s/features -> %s",
                settings.geogrid_base_url,
                response.status_code,
            )
            if response.status_code == status.HTTP_201_CREATED:
                feature_id = response.json()["id"]
                SYNC_COUNTER.labels(result="created").inc()
                result = {"feature_id": feature_id, "action": "created"}
                record_audit(
                    AuditEntry(
                        action="sync",
                        customer_id=payload.customer_id,
                        user=user,
                        dry_run=False,
                        status="success",
                        detail=result,
                        timestamp=timestamp,
                    )
                )
                return result
            if response.status_code == status.HTTP_409_CONFLICT:
                data = response.json()
                # FastAPI wraps HTTPException detail inside {"detail": {...}}
                feature_detail = data.get("detail", data)
                feature_id = (
                    feature_detail.get("id")
                    if isinstance(feature_detail, dict)
                    else None
                )
                if not feature_id:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail={"error": "GeoGrid conflict without feature id"},
                    )
                update_resp = await client.put(f"/features/{feature_id}", json=feature_payload)
                logger.info(
                    "HTTP PUT %s/features/%s -> %s",
                    settings.geogrid_base_url,
                    feature_id,
                    update_resp.status_code,
                )
                if update_resp.status_code not in {
                    status.HTTP_200_OK,
                    status.HTTP_204_NO_CONTENT,
                }:
                    try:
                        detail = update_resp.json()
                    except ValueError:
                        detail = update_resp.text
                    raise HTTPException(
                        status_code=update_resp.status_code,
                        detail={"upstream_detail": detail},
                    )
                SYNC_COUNTER.labels(result="updated").inc()
                result = {"feature_id": feature_id, "action": "updated"}
                record_audit(
                    AuditEntry(
                        action="sync",
                        customer_id=payload.customer_id,
                        user=user,
                        dry_run=False,
                        status="success",
                        detail=result,
                        timestamp=timestamp,
                    )
                )
                return result

        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        raise HTTPException(status_code=response.status_code, detail=detail)
    except HTTPException as exc:
        SYNC_COUNTER.labels(result="error").inc()
        record_audit(
            AuditEntry(
                action="sync",
                customer_id=payload.customer_id,
                user=user,
                dry_run=False,
                status="error",
                detail={
                    "status_code": exc.status_code,
                    "detail": exc.detail,
                },
                timestamp=timestamp,
            )
        )
        raise
    except Exception as exc:
        SYNC_COUNTER.labels(result="error").inc()
        detail = {"message": "Unexpected error", "error": str(exc)}
        record_audit(
            AuditEntry(
                action="sync",
                customer_id=payload.customer_id,
                user=user,
                dry_run=False,
                status="error",
                detail=detail,
                timestamp=timestamp,
            )
        )
        logger.exception("Unhandled error during customer sync")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail)


@app.post(
    "/provision/onu",
    status_code=status.HTTP_200_OK,
    summary="Authorize an ONU on SmartOLT",
)
async def provision_onu(
    payload: ProvisionRequest,
    request: Request,
    settings: EnvConfig = Depends(get_settings),
    state: RuntimeState = Depends(get_runtime_state),
) -> Dict[str, Any]:
    user = request.headers.get(APP_USER_HEADER, "ui")
    timestamp = datetime.now(timezone.utc).isoformat()
    dry_run_flag = payload.dry_run if payload.dry_run is not None else state.dry_run
    try:
        customer: Optional[Dict[str, Any]] = None
        if payload.customer_id is not None:
            async with httpx.AsyncClient(
                base_url=settings.isp_base_url, timeout=10.0
            ) as client:
                customer = await fetch_json(
                    client, "GET", f"/customers/{payload.customer_id}"
                )
            ensure_customer_ready(customer, action="provision")
            ensure_alignment(customer, payload)

        if dry_run_flag:
            logger.info(
                "Dry-run enabled, skipping SmartOLT provisioning for ONU %s",
                payload.onu_sn,
            )
            PROVISION_COUNTER.labels(result="dry_run").inc()
            result = {
                "dry_run": True,
                "status": "skipped",
                "message": "SmartOLT provisioning skipped because dry-run is enabled",
            }
            record_audit(
                AuditEntry(
                    action="provision",
                    customer_id=payload.customer_id,
                    user=user,
                    dry_run=True,
                    status="success",
                    detail=result,
                    timestamp=timestamp,
                )
            )
            return result

        request_body = payload.model_dump(exclude={"dry_run", "customer_id"})

        verification_params = {
            "olt_id": payload.olt_id,
            "onu_sn": payload.onu_sn,
        }

        async with httpx.AsyncClient(
            base_url=settings.smartolt_base_url, timeout=10.0
        ) as client:
            try:
                current = await fetch_json(
                    client, "GET", "/onus", params=verification_params
                )
            except HTTPException as exc:
                if exc.status_code not in {status.HTTP_404_NOT_FOUND, status.HTTP_204_NO_CONTENT}:
                    raise
                current = {"onus": []}

            existing_onus = current.get("onus", []) if isinstance(current, dict) else []
            if existing_onus:
                logger.info(
                    "ONU %s already authorized (id=%s)",
                    payload.onu_sn,
                    existing_onus[0].get("onu_id"),
                )
                PROVISION_COUNTER.labels(result="already_authorized").inc()
                result = {
                    "dry_run": False,
                    "status": "already_authorized",
                    "authorization": existing_onus[0],
                    "verification": current,
                }
                record_audit(
                    AuditEntry(
                        action="provision",
                        customer_id=payload.customer_id,
                        user=user,
                        dry_run=False,
                        status="success",
                        detail=result,
                        timestamp=timestamp,
                    )
                )
                return result

            auth_resp = await client.post("/onu/authorize", json=request_body)
            logger.info(
                "HTTP POST %s/onu/authorize -> %s",
                settings.smartolt_base_url,
                auth_resp.status_code,
            )
            if auth_resp.status_code == status.HTTP_409_CONFLICT:
                detail = auth_resp.json()
                logger.info(
                    "SmartOLT returned 409 for ONU %s (id=%s)",
                    payload.onu_sn,
                    detail.get("onu_id"),
                )
                verification = await fetch_json(
                    client, "GET", "/onus", params=verification_params
                )
                PROVISION_COUNTER.labels(result="already_authorized").inc()
                result = {
                    "dry_run": False,
                    "status": "already_authorized",
                    "authorization": detail,
                    "verification": verification,
                }
                record_audit(
                    AuditEntry(
                        action="provision",
                        customer_id=payload.customer_id,
                        user=user,
                        dry_run=False,
                        status="success",
                        detail=result,
                        timestamp=timestamp,
                    )
                )
                return result

            if auth_resp.status_code != status.HTTP_200_OK:
                try:
                    detail = auth_resp.json()
                except ValueError:
                    detail = auth_resp.text
                raise HTTPException(
                    status_code=auth_resp.status_code,
                    detail={"upstream_detail": detail},
                )

            verification = await fetch_json(
                client, "GET", "/onus", params=verification_params
            )
            authorization = auth_resp.json()

        PROVISION_COUNTER.labels(result="authorized").inc()
        result = {
            "dry_run": False,
            "status": "authorized",
            "authorization": authorization,
            "verification": verification,
        }
        record_audit(
            AuditEntry(
                action="provision",
                customer_id=payload.customer_id,
                user=user,
                dry_run=False,
                status="success",
                detail=result,
                timestamp=timestamp,
            )
        )
        return result
    except HTTPException as exc:
        PROVISION_COUNTER.labels(result="error").inc()
        record_audit(
            AuditEntry(
                action="provision",
                customer_id=payload.customer_id,
                user=user,
                dry_run=dry_run_flag,
                status="error",
                detail={
                    "status_code": exc.status_code,
                    "detail": exc.detail,
                },
                timestamp=timestamp,
            )
        )
        raise
    except Exception as exc:
        PROVISION_COUNTER.labels(result="error").inc()
        detail = {"message": "Unexpected error", "error": str(exc)}
        record_audit(
            AuditEntry(
                action="provision",
                customer_id=payload.customer_id,
                user=user,
                dry_run=dry_run_flag,
                status="error",
                detail=detail,
                timestamp=timestamp,
            )
        )
        logger.exception("Unhandled error during ONU provisioning")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail)


@app.post(
    "/decommission/customer",
    status_code=status.HTTP_200_OK,
    summary="Deactivate customer across GeoGrid and SmartOLT",
)
async def decommission_customer(
    payload: DecommissionRequest,
    request: Request,
    settings: EnvConfig = Depends(get_settings),
    state: RuntimeState = Depends(get_runtime_state),
) -> Dict[str, Any]:
    user = request.headers.get(APP_USER_HEADER, "ui")
    timestamp = datetime.now(timezone.utc).isoformat()
    dry_run_flag = payload.dry_run if payload.dry_run is not None else state.dry_run
    try:
        async with httpx.AsyncClient(base_url=settings.isp_base_url, timeout=10.0) as client:
            customer = await fetch_json(client, "GET", f"/customers/{payload.customer_id}")

        ensure_customer_inactive(customer)
        ensure_customer_has_network_keys(customer, action="decommission")

        feature: Optional[Dict[str, Any]] = None
        async with httpx.AsyncClient(
            base_url=settings.geogrid_base_url, timeout=10.0
        ) as geogrid_client:
            try:
                feature = await fetch_json(
                    geogrid_client,
                    "GET",
                    "/features/search",
                    params={"customer_id": payload.customer_id},
                )
            except HTTPException as exc:
                if exc.status_code == status.HTTP_404_NOT_FOUND:
                    feature = None
                else:
                    raise

            if feature is None:
                try:
                    feature = await fetch_json(
                        geogrid_client,
                        "GET",
                        "/features/search",
                        params={"onu_sn": customer["onu_sn"]},
                    )
                except HTTPException as exc:
                    if exc.status_code != status.HTTP_404_NOT_FOUND:
                        raise
                    feature = None

        if feature is None:
            record_incident(
                "decommission_missing_feature",
                {
                    "customer_id": payload.customer_id,
                },
            )

        async with httpx.AsyncClient(
            base_url=settings.smartolt_base_url, timeout=10.0
        ) as smart_client:
            params = {
                "olt_id": customer["olt_id"],
                "onu_sn": customer["onu_sn"],
            }
            onus_data = await fetch_json(smart_client, "GET", "/onus", params=params)
            existing_onus = onus_data.get("onus", []) if isinstance(onus_data, dict) else []

        if not existing_onus:
            record_incident(
                "decommission_missing_onu",
                {"customer_id": payload.customer_id, "onu_sn": customer["onu_sn"]},
            )

        if dry_run_flag:
            DECOMMISSION_COUNTER.labels(result="dry_run").inc()
            result = {
                "dry_run": True,
                "feature": {
                    "found": feature is not None,
                    "feature_id": feature["id"] if feature else None,
                },
                "onu": {
                    "found": bool(existing_onus),
                    "onu_ids": [onu["onu_id"] for onu in existing_onus],
                },
            }
            record_audit(
                AuditEntry(
                    action="decommission",
                    customer_id=payload.customer_id,
                    user=user,
                    dry_run=True,
                    status="success",
                    detail=result,
                    timestamp=timestamp,
                )
            )
            return result

        geogrid_result = "not_found"
        if feature:
            async with httpx.AsyncClient(
                base_url=settings.geogrid_base_url, timeout=10.0
            ) as geogrid_client:
                resp = await geogrid_client.delete(f"/features/{feature['id']}")
                logger.info(
                    "HTTP DELETE %s/features/%s -> %s",
                    settings.geogrid_base_url,
                    feature["id"],
                    resp.status_code,
                )
                if resp.status_code not in {status.HTTP_204_NO_CONTENT, status.HTTP_200_OK}:
                    try:
                        detail = resp.json()
                    except ValueError:
                        detail = resp.text
                    raise HTTPException(
                        status_code=resp.status_code,
                        detail={"upstream_detail": detail},
                    )
                geogrid_result = "deleted"

        smartolt_result = "not_found"
        if existing_onus:
            onu = existing_onus[0]
            async with httpx.AsyncClient(
                base_url=settings.smartolt_base_url, timeout=10.0
            ) as smart_client:
                delete_resp = await smart_client.delete(
                    "/onus",
                    params={
                        "olt_id": onu["olt_id"],
                        "board": onu["board"],
                        "pon_port": onu["pon_port"],
                        "onu_sn": onu["onu_sn"],
                    },
                )
                logger.info(
                    "HTTP DELETE %s/onus -> %s",
                    settings.smartolt_base_url,
                    delete_resp.status_code,
                )
                if delete_resp.status_code != status.HTTP_200_OK:
                    try:
                        detail = delete_resp.json()
                    except ValueError:
                        detail = delete_resp.text
                    raise HTTPException(
                        status_code=delete_resp.status_code,
                        detail={"upstream_detail": detail},
                    )
                smartolt_result = "deleted"

        DECOMMISSION_COUNTER.labels(result="completed").inc()
        result = {
            "dry_run": False,
            "feature": {"status": geogrid_result},
            "onu": {"status": smartolt_result},
        }
        record_audit(
            AuditEntry(
                action="decommission",
                customer_id=payload.customer_id,
                user=user,
                dry_run=False,
                status="success",
                detail=result,
                timestamp=timestamp,
            )
        )
        return result
    except HTTPException as exc:
        DECOMMISSION_COUNTER.labels(result="error").inc()
        record_audit(
            AuditEntry(
                action="decommission",
                customer_id=payload.customer_id,
                user=user,
                dry_run=dry_run_flag,
                status="error",
                detail={
                    "status_code": exc.status_code,
                    "detail": exc.detail,
                },
                timestamp=timestamp,
            )
        )
        raise
    except Exception as exc:
        DECOMMISSION_COUNTER.labels(result="error").inc()
        detail = {"message": "Unexpected error", "error": str(exc)}
        record_audit(
            AuditEntry(
                action="decommission",
                customer_id=payload.customer_id,
                user=user,
                dry_run=dry_run_flag,
                status="error",
                detail=detail,
                timestamp=timestamp,
            )
        )
        logger.exception("Unhandled error during customer decommission")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail)


@app.get(
    "/clients.geojson",
    summary="Return GeoJSON FeatureCollection from GeoGrid",
)
async def clients_geojson(settings: EnvConfig = Depends(get_settings)) -> Dict[str, Any]:
    async with httpx.AsyncClient(
        base_url=settings.geogrid_base_url, timeout=10.0
    ) as client:
        features = await fetch_json(client, "GET", "/features")

    collection = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": feature["id"],
                "properties": {
                    "name": feature["name"],
                    **feature.get("attrs", {}),
                },
                "geometry": {
                    "type": "Point",
                    "coordinates": [
                        feature["location"]["lon"],
                        feature["location"]["lat"],
                    ],
                },
            }
            for feature in features
        ],
    }
    return collection


@app.get("/config", summary="Inspect orchestrator runtime configuration")
async def get_config(
    settings: EnvConfig = Depends(get_settings),
    state: RuntimeState = Depends(get_runtime_state),
) -> Dict[str, Any]:
    return {
        "dry_run": state.dry_run,
        "endpoints": {
            "isp_base_url": settings.isp_base_url,
            "geogrid_base_url": settings.geogrid_base_url,
            "smartolt_base_url": settings.smartolt_base_url,
        },
    }


@app.post(
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


@app.post(
    "/reset",
    summary="Reset all mocks to their default state",
)
async def reset_mocks(settings: EnvConfig = Depends(get_settings)) -> Dict[str, Any]:
    targets = {
        "isp": settings.isp_base_url,
        "geogrid": settings.geogrid_base_url,
        "smartolt": settings.smartolt_base_url,
    }
    results: Dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=5.0) as client:
        for name, base in targets.items():
            try:
                resp = await client.post(f"{base}/reset")
                results[name] = {"status": resp.status_code}
            except httpx.HTTPError as exc:
                results[name] = {"error": str(exc)}
    INCIDENT_LOG.clear()
    AUDIT_LOG.clear()
    INCIDENT_GAUGE.set(0)
    return results


@app.get("/incidents")
async def list_incidents(kind: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
    entries = list(INCIDENT_LOG)
    if kind:
        entries = [entry for entry in entries if entry["kind"] == kind]
    return entries


@app.get("/audits")
async def list_audits(
    action: Optional[Literal["sync", "provision", "decommission"]] = Query(default=None),
    user: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
) -> List[Dict[str, Any]]:
    entries = list(AUDIT_LOG)
    if action:
        entries = [entry for entry in entries if entry.action == action]
    if user:
        entries = [entry for entry in entries if entry.user == user]
    return [entry.model_dump() for entry in entries[-limit:]]


@app.get("/metrics")
async def metrics():
    data = generate_latest()
    return Response(content=data, media_type=PROMETHEUS_CONTENT_TYPE)


@app.get("/health", summary="Health check for orchestrator")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


# Allow running with `python orchestrator/main.py` directly for convenience.
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "orchestrator.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=bool(int(os.getenv("UVICORN_RELOAD", "0"))),
    )
