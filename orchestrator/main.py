import asyncio
import json
import logging
import os
import random
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from typing_extensions import Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, ConfigDict
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
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


class CustomerEventRequest(BaseModel):
    event_type: Literal["alta", "baja"]
    customer_id: Optional[int] = Field(default=None, ge=1)
    zone: Optional[str] = Field(default=None, min_length=1)
    city: Optional[str] = None
    lat: Optional[float] = Field(default=None)
    lon: Optional[float] = Field(default=None)
    timestamp: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    source: Optional[str] = Field(default="manual")


CONFIG_ROOT = Path(__file__).resolve().parent.parent / "config" / "environments"
DEFAULT_ENVIRONMENT = "dev"
RETRYABLE_STATUS_CODES = {500, 502, 503, 504}


def _resolve_env_reference(raw_value: Optional[str]) -> Optional[str]:
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        return raw_value
    if raw_value.startswith("env:"):
        env_name = raw_value[4:].strip()
        if not env_name:
            raise RuntimeError("Empty environment variable reference in header config")
        env_value = os.getenv(env_name)
        if env_value is None:
            raise RuntimeError(
                f"Environment variable '{env_name}' required for header is not set"
            )
        return env_value
    return raw_value


class RetrySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=3, ge=1)
    backoff_initial_seconds: float = Field(default=0.2, ge=0.0)
    backoff_max_seconds: float = Field(default=5.0, ge=0.1)


class CircuitBreakerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_threshold: int = Field(default=5, ge=1)
    recovery_timeout_seconds: float = Field(default=30.0, ge=1.0)


class RegionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    timeout_seconds: float = Field(default=10.0, ge=0.1)
    verify_tls: bool = False
    retry: RetrySettings = Field(default_factory=RetrySettings)
    circuit_breaker: Optional[CircuitBreakerSettings] = None
    default_headers: Dict[str, str] = Field(default_factory=dict)

    def resolved_headers(self) -> Dict[str, str]:
        resolved: Dict[str, str] = {}
        for header, raw_value in self.default_headers.items():
            value = _resolve_env_reference(raw_value)
            if value is not None:
                resolved[header] = value
        return resolved


class ServiceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_region: str = "default"
    regions: Dict[str, RegionSettings]

    def update_region(
        self,
        region_name: str,
        *,
        base_url: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        verify_tls: Optional[bool] = None,
    ) -> None:
        if region_name not in self.regions:
            raise KeyError(f"Region '{region_name}' not defined for service")
        region_cfg = self.regions[region_name]
        updates: Dict[str, Any] = {}
        if base_url is not None:
            updates["base_url"] = base_url
        if timeout_seconds is not None:
            updates["timeout_seconds"] = timeout_seconds
        if verify_tls is not None:
            updates["verify_tls"] = verify_tls
        if updates:
            self.regions[region_name] = region_cfg.model_copy(update=updates)


class EnvConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    env_name: str
    dry_run_default: bool
    services: Dict[str, ServiceSettings]

    def _normalize_service(self, service: str) -> str:
        return service.lower()

    def _resolve_region(
        self, service: str, region: Optional[str] = None
    ) -> Tuple[RegionSettings, str]:
        service_key = self._normalize_service(service)
        if service_key not in self.services:
            raise KeyError(f"Unknown service '{service}'")
        service_cfg = self.services[service_key]
        env_region = os.getenv(f"{service_key.upper()}_REGION")
        region_name = (region or env_region or service_cfg.default_region).lower()
        if region_name not in service_cfg.regions:
            logger.warning(
                "Region '%s' not defined for service '%s'; using default '%s'",
                region_name,
                service_key,
                service_cfg.default_region,
            )
            region_name = service_cfg.default_region
        return service_cfg.regions[region_name], region_name

    def resolve_service_region(
        self, service: str, region: Optional[str] = None
    ) -> Tuple[RegionSettings, str]:
        return self._resolve_region(service, region)

    def get_service_region(self, service: str, region: Optional[str] = None) -> RegionSettings:
        region_cfg, _ = self._resolve_region(service, region)
        return region_cfg

    def service_base_url(self, service: str, region: Optional[str] = None) -> str:
        return self.get_service_region(service, region).base_url

    def http_client_kwargs(
        self, service: str, region: Optional[str] = None
    ) -> Tuple[Dict[str, Any], str]:
        region_cfg, region_name = self._resolve_region(service, region)
        base_headers = region_cfg.resolved_headers()
        headers = base_headers if base_headers else None
        return (
            {
                "base_url": region_cfg.base_url,
                "timeout": region_cfg.timeout_seconds,
                "verify": region_cfg.verify_tls,
                "headers": headers,
            },
            region_name,
        )

    def override_default_region(
        self,
        service: str,
        *,
        base_url: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        verify_tls: Optional[bool] = None,
    ) -> None:
        service_key = self._normalize_service(service)
        if service_key not in self.services:
            raise KeyError(f"Unknown service '{service}'")
        service_cfg = self.services[service_key]
        service_cfg.update_region(
            service_cfg.default_region,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            verify_tls=verify_tls,
        )

    @property
    def isp_base_url(self) -> str:
        return self.service_base_url("isp")

    @property
    def geogrid_base_url(self) -> str:
        return self.service_base_url("geogrid")

    @property
    def smartolt_base_url(self) -> str:
        return self.service_base_url("smartolt")


@dataclass
class CircuitBreakerState:
    failure_count: int = 0
    opened_at: Optional[float] = None


_circuit_states: Dict[str, CircuitBreakerState] = {}
_circuit_locks: Dict[str, asyncio.Lock] = {}


def _get_circuit_state(key: str) -> CircuitBreakerState:
    state = _circuit_states.get(key)
    if state is None:
        state = CircuitBreakerState()
        _circuit_states[key] = state
    return state


def _get_circuit_lock(key: str) -> asyncio.Lock:
    lock = _circuit_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _circuit_locks[key] = lock
    return lock


class RetryableHTTPStatus(Exception):
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self.payload = payload
        super().__init__(f"Retryable HTTP status: {status_code}")


def _load_environment_from_file(env_name: str) -> Dict[str, Any]:
    config_path = CONFIG_ROOT / f"{env_name}.json"
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found for env '{env_name}'")
    with config_path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_env_config() -> EnvConfig:
    env_name = os.getenv("ORCHESTRATOR_ENV", DEFAULT_ENVIRONMENT).strip().lower() or DEFAULT_ENVIRONMENT
    try:
        data = _load_environment_from_file(env_name)
    except FileNotFoundError:
        logger.warning(
            "Environment '%s' not found. Falling back to '%s'",
            env_name,
            DEFAULT_ENVIRONMENT,
        )
        env_name = DEFAULT_ENVIRONMENT
        data = _load_environment_from_file(env_name)

    data["env_name"] = env_name
    config = EnvConfig.model_validate(data)

    override_map = {
        "isp": os.getenv("ISP_BASE_URL"),
        "geogrid": os.getenv("GEOGRID_BASE_URL"),
        "smartolt": os.getenv("SMARTOLT_BASE_URL"),
    }
    for service_name, base_override in override_map.items():
        if base_override:
            config.override_default_region(service_name, base_url=base_override)

    dry_run_env = os.getenv("DRY_RUN")
    if dry_run_env is not None:
        config.dry_run_default = dry_run_env.lower() in {"1", "true", "yes", "on"}

    return config


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
CUSTOMER_EVENT_COUNTER = Counter(
    "orchestrator_customer_events_total",
    "Altas y bajas de clientes registradas por zona",
    ["event_type", "zone"],
)
INCIDENT_GAUGE = Gauge(
    "orchestrator_incidents_buffer_size",
    "Current number of incidents retained in buffer",
)

PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


REQUIRED_CUSTOMER_FIELDS: List[str] = [
    "lat",
    "lon",
    "zone",
    "odb",
    "olt_id",
    "board",
    "pon",
    "onu_sn",
]

CORDOBA_LAT_RANGE: Tuple[float, float] = (-35.5, -29.0)
CORDOBA_LON_RANGE: Tuple[float, float] = (-66.5, -62.0)

INCIDENT_LOG: deque[Dict[str, Any]] = deque(
    maxlen=int(os.getenv("INCIDENT_BUFFER_SIZE", "200"))
)
AUDIT_LOG: deque[AuditEntry] = deque(maxlen=int(os.getenv("AUDIT_BUFFER_SIZE", "500")))
ZONE_BASE_COORDINATES: Dict[str, Tuple[float, float]] = {
    "Centro": (-31.417, -64.183),
    "Nueva Córdoba": (-31.4275, -64.1829),
    "General Paz": (-31.4138, -64.1704),
    "Alberdi": (-31.4199, -64.2108),
    "Alta Córdoba": (-31.3836, -64.2033),
    "Villa Belgrano": (-31.3602, -64.2383),
    "Guiñazú": (-31.2861, -64.1736),
    "Villa Test": (-31.1, -64.0),
}
DEFAULT_ZONE_LABEL = "Sin zona"
CUSTOMER_EVENT_BUFFER_SIZE = int(os.getenv("CUSTOMER_EVENT_BUFFER_SIZE", "2000"))
CUSTOMER_EVENTS: deque[Dict[str, Any]] = deque(maxlen=CUSTOMER_EVENT_BUFFER_SIZE)
SYNTHETIC_EVENT_ZONES: List[str] = [
    "Centro",
    "Nueva Córdoba",
    "General Paz",
    "Alberdi",
    "Alta Córdoba",
    "Villa Belgrano",
]


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _resolve_zone_coordinates(zone: str) -> Tuple[Optional[float], Optional[float]]:
    coords = ZONE_BASE_COORDINATES.get(zone)
    if coords:
        return coords
    return (None, None)


def register_customer_event(
    event_type: Literal["alta", "baja"],
    *,
    customer: Optional[Dict[str, Any]] = None,
    zone: Optional[str] = None,
    timestamp: Optional[datetime] = None,
    city: Optional[str] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    source: str = "runtime",
    metadata: Optional[Dict[str, Any]] = None,
    customer_id: Optional[int] = None,
    ) -> Dict[str, Any]:
    safe_zone = zone or (customer.get("zone") if customer else None) or DEFAULT_ZONE_LABEL
    safe_city = city or (customer.get("city") if customer else None)
    event_customer_id = (
        customer_id if customer_id is not None else customer.get("customer_id") if customer else None
    )
    lat_value = _safe_float(lat if lat is not None else customer.get("lat") if customer else None)
    lon_value = _safe_float(lon if lon is not None else customer.get("lon") if customer else None)
    if lat_value is None or lon_value is None:
        fallback_lat, fallback_lon = _resolve_zone_coordinates(safe_zone)
        if lat_value is None:
            lat_value = fallback_lat
        if lon_value is None:
            lon_value = fallback_lon
    event_timestamp = timestamp or datetime.now(timezone.utc)
    if isinstance(event_timestamp, str):
        timestamp_str = event_timestamp
    else:
        timestamp_str = event_timestamp.isoformat()

    event_entry: Dict[str, Any] = {
        "event_id": str(uuid4()),
        "timestamp": timestamp_str,
        "event_type": event_type,
        "zone": safe_zone,
        "city": safe_city,
        "customer_id": event_customer_id,
        "lat": lat_value,
        "lon": lon_value,
        "source": source,
        "metadata": metadata or {},
    }

    CUSTOMER_EVENTS.append(event_entry)
    CUSTOMER_EVENT_COUNTER.labels(event_type=event_type, zone=safe_zone).inc()
    return event_entry


def _seed_synthetic_customer_events() -> None:
    if CUSTOMER_EVENTS:
        return
    seed_flag = os.getenv("ORCHESTRATOR_SEED_CUSTOMER_EVENTS", "false").lower()
    if seed_flag not in {"true", "1", "yes", "on"}:
        return

    lookback_days = int(os.getenv("CUSTOMER_EVENT_SEED_LOOKBACK_DAYS", "30"))
    rng_seed = int(os.getenv("CUSTOMER_EVENT_SEED_RANDOM_SEED", "42"))
    rng = random.Random(rng_seed)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    for day_offset in range(lookback_days):
        day = now - timedelta(days=day_offset)
        for zone in SYNTHETIC_EVENT_ZONES:
            base_lat, base_lon = _resolve_zone_coordinates(zone)
            altas = rng.randint(0, 4)
            bajas = rng.randint(0, 3)
            for _ in range(altas):
                event_time = day.replace(
                    hour=8 + rng.randint(0, 6),
                    minute=rng.randint(0, 59),
                    second=rng.randint(0, 59),
                )
                register_customer_event(
                    "alta",
                    zone=zone,
                    city="Córdoba",
                    lat=base_lat,
                    lon=base_lon,
                    timestamp=event_time,
                    source="synthetic",
                    metadata={"seed": True},
                )
            for _ in range(bajas):
                event_time = day.replace(
                    hour=14 + rng.randint(0, 5),
                    minute=rng.randint(0, 59),
                    second=rng.randint(0, 59),
                )
                register_customer_event(
                    "baja",
                    zone=zone,
                    city="Córdoba",
                    lat=base_lat,
                    lon=base_lon,
                    timestamp=event_time,
                    source="synthetic",
                    metadata={"seed": True},
                )


_seed_synthetic_customer_events()


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


def _parse_iso8601(value: str) -> datetime:
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        logger.debug("Invalid timestamp for customer event: %s", value)
        return datetime.now(timezone.utc)


def _filter_customer_events(
    lookback_days: int,
    *,
    zone: Optional[str] = None,
    event_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    selected: List[Dict[str, Any]] = []
    for event in reversed(CUSTOMER_EVENTS):
        ts = _parse_iso8601(str(event["timestamp"]))
        if ts < since:
            continue
        if zone and event.get("zone") != zone:
            continue
        if event_type and event.get("event_type") != event_type:
            continue
        event_copy = dict(event)
        event_copy["timestamp"] = ts.isoformat()
        selected.append(event_copy)
    return selected


def _summarize_customer_events(events: List[Dict[str, Any]]) -> Tuple[Dict[str, int], List[Dict[str, Any]]]:
    totals = {"altas": 0, "bajas": 0, "neto": 0}
    per_zone: Dict[str, Dict[str, Any]] = {}
    for event in events:
        zone_name = event.get("zone") or DEFAULT_ZONE_LABEL
        zone_entry = per_zone.setdefault(
            zone_name,
            {
                "zone": zone_name,
                "altas": 0,
                "bajas": 0,
                "lat": None,
                "lon": None,
                "city": event.get("city"),
            },
        )
        if event.get("event_type") == "alta":
            zone_entry["altas"] += 1
            totals["altas"] += 1
        else:
            zone_entry["bajas"] += 1
            totals["bajas"] += 1

        if zone_entry["lat"] is None and event.get("lat") is not None:
            zone_entry["lat"] = event.get("lat")
        if zone_entry["lon"] is None and event.get("lon") is not None:
            zone_entry["lon"] = event.get("lon")
        if not zone_entry.get("city") and event.get("city"):
            zone_entry["city"] = event.get("city")

    for zone_name, zone_entry in per_zone.items():
        zone_entry["neto"] = zone_entry["altas"] - zone_entry["bajas"]
        if zone_entry.get("lat") is None or zone_entry.get("lon") is None:
            fallback_lat, fallback_lon = _resolve_zone_coordinates(zone_name)
            if zone_entry.get("lat") is None:
                zone_entry["lat"] = fallback_lat
            if zone_entry.get("lon") is None:
                zone_entry["lon"] = fallback_lon

    totals["neto"] = totals["altas"] - totals["bajas"]
    ordered_zones = sorted(
        per_zone.values(),
        key=lambda item: (item.get("neto", 0), item.get("altas", 0)),
        reverse=True,
    )
    return totals, ordered_zones


def _safe_response_payload(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _record_circuit_success(key: str) -> None:
    if key not in _circuit_states:
        return
    state = _circuit_states[key]
    state.failure_count = 0
    state.opened_at = None


def _record_circuit_failure(
    key: str, breaker_cfg: CircuitBreakerSettings, *, timestamp: float
) -> None:
    state = _get_circuit_state(key)
    state.failure_count += 1
    if state.failure_count >= breaker_cfg.failure_threshold:
        state.opened_at = timestamp


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

    try:
        lat = float(customer.get("lat"))
        lon = float(customer.get("lon"))
    except (TypeError, ValueError):
        record_incident(
            "invalid_coordinates",
            {
                "customer_id": customer_id,
                "lat": customer.get("lat"),
                "lon": customer.get("lon"),
                "reason": "non_numeric",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "Customer coordinates are invalid",
                "customer_id": customer_id,
                "lat": customer.get("lat"),
                "lon": customer.get("lon"),
                "reason": "non_numeric",
            },
        )

    if not (
        CORDOBA_LAT_RANGE[0] <= lat <= CORDOBA_LAT_RANGE[1]
        and CORDOBA_LON_RANGE[0] <= lon <= CORDOBA_LON_RANGE[1]
    ):
        record_incident(
            "invalid_coordinates",
            {
                "customer_id": customer_id,
                "lat": lat,
                "lon": lon,
                "allowed_lat_range": CORDOBA_LAT_RANGE,
                "allowed_lon_range": CORDOBA_LON_RANGE,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "Customer coordinates outside allowed coverage area",
                "customer_id": customer_id,
                "lat": lat,
                "lon": lon,
                "allowed_lat_range": CORDOBA_LAT_RANGE,
                "allowed_lon_range": CORDOBA_LON_RANGE,
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
        breaker_lock = _get_circuit_lock(breaker_key)
        async with breaker_lock:
            state = _get_circuit_state(breaker_key)
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
                break
    except RetryableHTTPStatus as exc:
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
        isp_client_kwargs, isp_region = settings.http_client_kwargs("isp")
        async with httpx.AsyncClient(**isp_client_kwargs) as client:
            customer = await fetch_json(
                client,
                "GET",
                f"/customers/{payload.customer_id}",
                service="isp",
                settings=settings,
                region_name=isp_region,
            )

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

        geogrid_client_kwargs, geogrid_region = settings.http_client_kwargs("geogrid")
        async with httpx.AsyncClient(**geogrid_client_kwargs) as client:
            response = await client.post("/features", json=feature_payload)
            logger.info(
                "HTTP POST %s/features -> %s",
                geogrid_client_kwargs["base_url"],
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
            isp_client_kwargs, isp_region = settings.http_client_kwargs("isp")
            async with httpx.AsyncClient(**isp_client_kwargs) as client:
                customer = await fetch_json(
                    client,
                    "GET",
                    f"/customers/{payload.customer_id}",
                    service="isp",
                    settings=settings,
                    region_name=isp_region,
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

        smartolt_client_kwargs, smartolt_region = settings.http_client_kwargs("smartolt")
        async with httpx.AsyncClient(**smartolt_client_kwargs) as client:
            try:
                current = await fetch_json(
                    client,
                    "GET",
                    "/onus",
                    params=verification_params,
                    service="smartolt",
                    settings=settings,
                    region_name=smartolt_region,
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
                smartolt_client_kwargs["base_url"],
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
                    client,
                    "GET",
                    "/onus",
                    params=verification_params,
                    service="smartolt",
                    settings=settings,
                    region_name=smartolt_region,
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
                client,
                "GET",
                "/onus",
                params=verification_params,
                service="smartolt",
                settings=settings,
                region_name=smartolt_region,
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
        if customer:
            register_customer_event(
                "alta",
                customer=customer,
                source="runtime",
                metadata={
                    "trigger": "provision",
                    "status": result.get("status"),
                },
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
        isp_client_kwargs, isp_region = settings.http_client_kwargs("isp")
        async with httpx.AsyncClient(**isp_client_kwargs) as client:
            customer = await fetch_json(
                client,
                "GET",
                f"/customers/{payload.customer_id}",
                service="isp",
                settings=settings,
                region_name=isp_region,
            )

        ensure_customer_inactive(customer)
        ensure_customer_has_network_keys(customer, action="decommission")

        feature: Optional[Dict[str, Any]] = None
        geogrid_client_kwargs, geogrid_region = settings.http_client_kwargs("geogrid")
        async with httpx.AsyncClient(**geogrid_client_kwargs) as geogrid_client:
            try:
                feature = await fetch_json(
                    geogrid_client,
                    "GET",
                    "/features/search",
                    params={"customer_id": payload.customer_id},
                    service="geogrid",
                    settings=settings,
                    region_name=geogrid_region,
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
                        service="geogrid",
                        settings=settings,
                        region_name=geogrid_region,
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

        smartolt_client_kwargs, smartolt_region = settings.http_client_kwargs("smartolt")
        async with httpx.AsyncClient(**smartolt_client_kwargs) as smart_client:
            params = {
                "olt_id": customer["olt_id"],
                "onu_sn": customer["onu_sn"],
            }
            onus_data = await fetch_json(
                smart_client,
                "GET",
                "/onus",
                params=params,
                service="smartolt",
                settings=settings,
                region_name=smartolt_region,
            )
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
            geogrid_client_kwargs, geogrid_region = settings.http_client_kwargs("geogrid")
            async with httpx.AsyncClient(**geogrid_client_kwargs) as geogrid_client:
                resp = await geogrid_client.delete(f"/features/{feature['id']}")
                logger.info(
                    "HTTP DELETE %s/features/%s -> %s",
                    geogrid_client_kwargs["base_url"],
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
            smartolt_client_kwargs, smartolt_region = settings.http_client_kwargs("smartolt")
            async with httpx.AsyncClient(**smartolt_client_kwargs) as smart_client:
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
                    smartolt_client_kwargs["base_url"],
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
        if customer:
            register_customer_event(
                "baja",
                customer=customer,
                source="runtime",
                metadata={
                    "trigger": "decommission",
                    "feature_status": geogrid_result,
                    "onu_status": smartolt_result,
                },
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
    geogrid_client_kwargs, geogrid_region = settings.http_client_kwargs("geogrid")
    async with httpx.AsyncClient(**geogrid_client_kwargs) as client:
        features = await fetch_json(
            client,
            "GET",
            "/features",
            service="geogrid",
            settings=settings,
            region_name=geogrid_region,
        )

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
        "endpoints": {  # Legacy shape for backward compatibility
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
    results: Dict[str, Any] = {}
    for service_name in ("isp", "geogrid", "smartolt"):
        client_kwargs, region = settings.http_client_kwargs(service_name)
        async with httpx.AsyncClient(**client_kwargs) as client:
            try:
                resp = await client.post("/reset")
                results[service_name] = {
                    "status": resp.status_code,
                    "region": region,
                }
            except httpx.HTTPError as exc:
                results[service_name] = {
                    "error": str(exc),
                    "region": region,
                }
    INCIDENT_LOG.clear()
    AUDIT_LOG.clear()
    INCIDENT_GAUGE.set(0)
    CUSTOMER_EVENTS.clear()
    return results


@app.post(
    "/analytics/customer-events",
    status_code=status.HTTP_201_CREATED,
    summary="Registrar manualmente un evento de alta o baja",
)
async def create_customer_event(
    payload: CustomerEventRequest,
    settings: EnvConfig = Depends(get_settings),
) -> Dict[str, Any]:
    customer: Optional[Dict[str, Any]] = None
    if payload.customer_id is not None:
        isp_client_kwargs, isp_region = settings.http_client_kwargs("isp")
        async with httpx.AsyncClient(**isp_client_kwargs) as client:
            customer = await fetch_json(
                client,
                "GET",
                f"/customers/{payload.customer_id}",
                service="isp",
                settings=settings,
                region_name=isp_region,
            )

    event_metadata = dict(payload.metadata)
    if payload.source and "origin" not in event_metadata:
        event_metadata["origin"] = payload.source

    event_entry = register_customer_event(
        payload.event_type,
        customer=customer,
        zone=payload.zone,
        city=payload.city,
        lat=payload.lat,
        lon=payload.lon,
        timestamp=payload.timestamp,
        source=payload.source or "manual",
        metadata=event_metadata,
        customer_id=payload.customer_id,
    )
    return {"event": event_entry}


@app.get(
    "/analytics/customer-events",
    summary="Eventos de altas y bajas georreferenciados",
)
async def get_customer_events(
    lookback_days: int = Query(
        default=30,
        ge=1,
        le=365,
        description="Cantidad de días hacia atrás a considerar.",
    ),
    limit: int = Query(
        default=500,
        ge=1,
        le=CUSTOMER_EVENT_BUFFER_SIZE,
        description="Cantidad máxima de eventos más recientes a devolver.",
    ),
    zone: Optional[str] = Query(
        default=None,
        description="Filtra por zona/barrio exacto.",
    ),
    event_type: Optional[Literal["alta", "baja"]] = Query(
        default=None,
        description="Filtra por tipo de evento.",
    ),
) -> List[Dict[str, Any]]:
    events = _filter_customer_events(lookback_days, zone=zone, event_type=event_type)
    return events[:limit]


@app.get(
    "/analytics/customer-events/summary",
    summary="Totales de altas y bajas por zona",
)
async def get_customer_events_summary(
    lookback_days: int = Query(
        default=30,
        ge=1,
        le=365,
        description="Cantidad de días hacia atrás a considerar.",
    ),
    zone: Optional[str] = Query(
        default=None,
        description="Filtra por zona/barrio exacto.",
    ),
    event_type: Optional[Literal["alta", "baja"]] = Query(
        default=None,
        description="Filtra por tipo de evento.",
    ),
) -> Dict[str, Any]:
    events = _filter_customer_events(lookback_days, zone=zone, event_type=event_type)
    totals, zones = _summarize_customer_events(events)
    stats = [
        {"metric": "altas", "value": totals["altas"]},
        {"metric": "bajas", "value": totals["bajas"]},
        {"metric": "neto", "value": totals["neto"]},
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_days": lookback_days,
        "filters": {
            "zone": zone,
            "event_type": event_type,
        },
        "stats": stats,
        "totals": totals,
        "zones": zones,
    }


@app.get(
    "/analytics/customer-events/metrics",
    summary="Totales agregados de altas/bajas/neto",
)
async def get_customer_event_metrics(
    lookback_days: int = Query(
        default=30,
        ge=1,
        le=365,
        description="Cantidad de días hacia atrás a considerar.",
    ),
    zone: Optional[str] = Query(
        default=None,
        description="Filtra por zona/barrio exacto.",
    ),
    event_type: Optional[Literal["alta", "baja"]] = Query(
        default=None,
        description="Filtra por tipo de evento.",
    ),
) -> Dict[str, Any]:
    events = _filter_customer_events(lookback_days, zone=zone, event_type=event_type)
    totals, _ = _summarize_customer_events(events)
    return {
        "altas": totals["altas"],
        "bajas": totals["bajas"],
        "neto": totals["neto"],
    }


@app.get(
    "/analytics/customer-events/time-series",
    summary="Serie temporal de altas y bajas por zona",
)
async def get_customer_events_time_series(
    lookback_days: int = Query(
        default=30,
        ge=1,
        le=365,
        description="Cantidad de días hacia atrás a considerar.",
    ),
    zone: Optional[str] = Query(
        default=None,
        description="Filtra por zona/barrio exacto.",
    ),
) -> List[Dict[str, Any]]:
    events = _filter_customer_events(lookback_days, zone=zone)
    buckets: Dict[Tuple[str, date], Dict[str, Any]] = defaultdict(
        lambda: {"altas": 0, "bajas": 0, "lat": None, "lon": None}
    )
    for event in events:
        timestamp = _parse_iso8601(event["timestamp"])
        day_key = timestamp.date()
        zone_name = event.get("zone") or DEFAULT_ZONE_LABEL
        bucket = buckets[(zone_name, day_key)]
        if event.get("event_type") == "alta":
            bucket["altas"] += 1
        else:
            bucket["bajas"] += 1
        if bucket.get("lat") is None and event.get("lat") is not None:
            bucket["lat"] = event.get("lat")
        if bucket.get("lon") is None and event.get("lon") is not None:
            bucket["lon"] = event.get("lon")

    response: List[Dict[str, Any]] = []
    for (zone_name, day_key), counts in sorted(
        buckets.items(), key=lambda item: (item[0][1], item[0][0])
    ):
        day_start = datetime.combine(day_key, datetime.min.time()).replace(
            tzinfo=timezone.utc
        )
        lat = counts.get("lat")
        lon = counts.get("lon")
        if lat is None or lon is None:
            fallback_lat, fallback_lon = _resolve_zone_coordinates(zone_name)
            if lat is None:
                lat = fallback_lat
            if lon is None:
                lon = fallback_lon
        response.append(
            {
                "timestamp": day_start.isoformat(),
                "zone": zone_name,
                "altas": counts["altas"],
                "bajas": counts["bajas"],
                "neto": counts["altas"] - counts["bajas"],
                "lat": lat,
                "lon": lon,
            }
        )
    return response


@app.get(
    "/analytics/customer-events/geo",
    summary="Eventos georreferenciados segmentados por tipo",
)
async def get_customer_events_geo(
    lookback_days: int = Query(
        default=30,
        ge=1,
        le=365,
        description="Cantidad de días hacia atrás a considerar.",
    ),
    zone: Optional[str] = Query(
        default=None,
        description="Filtra por zona/barrio exacto.",
    ),
) -> Dict[str, List[Dict[str, Any]]]:
    events = _filter_customer_events(lookback_days, zone=zone)
    altas: List[Dict[str, Any]] = []
    bajas: List[Dict[str, Any]] = []
    for event in events:
        entry = {
            "event_id": event["event_id"],
            "timestamp": event["timestamp"],
            "zone": event.get("zone"),
            "city": event.get("city"),
            "customer_id": event.get("customer_id"),
            "lat": event.get("lat"),
            "lon": event.get("lon"),
            "source": event.get("source"),
        }
        if entry["lat"] is None or entry["lon"] is None:
            fallback_lat, fallback_lon = _resolve_zone_coordinates(entry.get("zone") or DEFAULT_ZONE_LABEL)
            if entry["lat"] is None:
                entry["lat"] = fallback_lat
            if entry["lon"] is None:
                entry["lon"] = fallback_lon
        if event.get("event_type") == "alta":
            altas.append(entry)
        else:
            bajas.append(entry)
    return {"altas": altas, "bajas": bajas}


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
