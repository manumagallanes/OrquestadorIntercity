import asyncio
import json
import logging
import math
import os
import random
import time
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from contextvars import ContextVar
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from uuid import uuid4

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

from typing_extensions import Literal

from .persistence import PersistenceStore
from .services import geogrid as geogrid_service
from .services import isp as isp_service

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s :: %(message)s",
)
logger = logging.getLogger("orchestrator")

REQUEST_ID_HEADER = os.getenv("ORCHESTRATOR_REQUEST_ID_HEADER", "X-Request-ID")
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdLoggingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get("-")
        return True


logging.getLogger().addFilter(RequestIdLoggingFilter())

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
SEED_ROOT = Path(__file__).resolve().parent.parent / "config" / "seeds"
CUSTOMER_SEED_FILE = SEED_ROOT / "customers.json"

DEFAULT_ENVIRONMENT = "dev"
RETRYABLE_STATUS_CODES = {500, 502, 503, 504}


def _load_customer_seed_dataset() -> List[Dict[str, Any]]:
    if not CUSTOMER_SEED_FILE.exists():
        logger.info("Customer seed file not found at %s; skipping seeding", CUSTOMER_SEED_FILE)
        return []
    try:
        raw = CUSTOMER_SEED_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Unable to load customer seed dataset: %s", exc)
        return []
    if not isinstance(data, list):
        logger.warning("Customer seed dataset must be a JSON array; received %s", type(data))
        return []
    sanitized: List[Dict[str, Any]] = []
    for entry in data:
        if isinstance(entry, dict):
            sanitized.append(entry)
        else:
            logger.debug("Skipping non-object entry in customer seed dataset: %s", entry)
    return sanitized


CUSTOMER_SEED_DATA: List[Dict[str, Any]] = _load_customer_seed_dataset()


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
INCIDENT_RESOLVED_COUNTER = Counter(
    "orchestrator_incidents_resolved_total",
    "Incidents marked as resolved",
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
INTEGRATION_ERROR_COUNTER = Counter(
    "orchestrator_integration_errors_total",
    "Errores al invocar servicios externos",
    ["service", "status"],
)

PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


REQUIRED_CUSTOMER_FIELDS: List[str] = [
    "lat",
    "lng",
    "address",
    "olt_id",
    "board",
    "pon",
    "onu_sn",
]

CORDOBA_LAT_RANGE: Tuple[float, float] = (-35.5, -29.0)
CORDOBA_LON_RANGE: Tuple[float, float] = (-66.5, -62.0)

INCIDENT_BUFFER_SIZE = int(os.getenv("INCIDENT_BUFFER_SIZE", "200"))
RESOLVED_INCIDENT_BUFFER_SIZE = int(os.getenv("INCIDENT_RESOLVED_BUFFER_SIZE", "500"))

INCIDENT_LOG: deque[Dict[str, Any]] = deque(maxlen=INCIDENT_BUFFER_SIZE)
RESOLVED_INCIDENT_LOG: deque[Dict[str, Any]] = deque(
    maxlen=RESOLVED_INCIDENT_BUFFER_SIZE
)
AUDIT_LOG: deque[AuditEntry] = deque(maxlen=int(os.getenv("AUDIT_BUFFER_SIZE", "500")))
INCIDENT_KIND_LABELS: Dict[str, str] = {
    "missing_fields": "Datos incompletos",
    "missing_network_keys": "Identificadores de red faltantes",
    "integration_disabled": "Integración deshabilitada",
    "invalid_coordinates": "Coordenadas inválidas",
    "hardware_mismatch": "Desajuste hardware/OLT",
    "decommission_status_active": "Cliente activo al solicitar baja",
    "decommission_missing_feature": "GeoGrid sin registro",
    "geogrid_conflict": "Conflicto GeoGrid",
    "isp_lookup_failure": "Error consulta ISP",
    "geogrid_assignment_conflict": "Puerto en uso en GeoGrid",
    "missing_geogrid_assignment": "Cliente sin asignación de puerto",
    "orphan_geogrid_cliente": "Cliente GeoGrid sin contraparte",
}
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
LATEST_CUSTOMER_EVENTS: OrderedDict[Any, Dict[str, Any]] = OrderedDict()
SYNTHETIC_EVENT_ZONES: List[str] = [
    "Centro",
    "Nueva Córdoba",
    "General Paz",
    "Alberdi",
    "Alta Córdoba",
    "Villa Belgrano",
]
DEFAULT_COORDINATE_FALLBACK: Tuple[float, float] = ZONE_BASE_COORDINATES.get(
    "Centro", (-31.417, -64.183)
)

DATA_DIR = Path(os.getenv("ORCHESTRATOR_DATA_DIR", str(Path(__file__).resolve().parent / "data"))).resolve()
DB_PATH = Path(os.getenv("ORCHESTRATOR_STATE_DB", str(DATA_DIR / "state.db")))
CUSTOMER_EVENT_RETENTION_DAYS_CFG = int(os.getenv("CUSTOMER_EVENT_RETENTION_DAYS", "90"))
INCIDENT_RETENTION_DAYS_CFG = int(os.getenv("INCIDENT_RETENTION_DAYS", "90"))
AUDIT_RETENTION_DAYS_CFG = int(os.getenv("AUDIT_RETENTION_DAYS", "90"))
RECONCILIATION_RETENTION_DAYS_CFG = int(os.getenv("RECONCILIATION_RETENTION_DAYS", "30"))

persistence_store = PersistenceStore(
    db_path=DB_PATH,
    customer_event_retention_days=CUSTOMER_EVENT_RETENTION_DAYS_CFG,
    incident_retention_days=INCIDENT_RETENTION_DAYS_CFG,
    audit_retention_days=AUDIT_RETENTION_DAYS_CFG,
    reconciliation_retention_days=RECONCILIATION_RETENTION_DAYS_CFG,
)


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


def _normalize_customer_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return str(value)


def _lookup_customer_name(customer_id: Any) -> Optional[str]:
    normalized = _normalize_customer_id(customer_id)
    if not normalized:
        return None
    for event in reversed(CUSTOMER_EVENTS):
        if _normalize_customer_id(event.get("customer_id")) == normalized:
            name = event.get("customer_name") or (event.get("metadata") or {}).get("customer_name")
            if name:
                return name
    for entry in reversed(RESOLVED_INCIDENT_LOG):
        if _normalize_customer_id(entry.get("customer_id")) == normalized:
            name = entry.get("customer_name")
            if name:
                return name
    for entry in reversed(INCIDENT_LOG):
        if _normalize_customer_id(entry.get("customer_id")) == normalized:
            name = entry.get("customer_name")
            if name:
                return name
    return None


def _bootstrap_state_from_persistence() -> None:
    try:
        persisted_events = persistence_store.load_customer_events()
    except Exception as exc:
        logger.error("Failed to load customer events from persistence: %s", exc)
        persisted_events = []
    CUSTOMER_EVENTS.clear()
    LATEST_CUSTOMER_EVENTS.clear()
    for event in persisted_events:
        CUSTOMER_EVENTS.append(event)
        cache_key = _normalize_customer_id(event.get("customer_id")) or event.get("event_id")
        if cache_key is None:
            continue
        if cache_key in LATEST_CUSTOMER_EVENTS:
            del LATEST_CUSTOMER_EVENTS[cache_key]
        LATEST_CUSTOMER_EVENTS[cache_key] = dict(event)

    try:
        open_incidents = persistence_store.load_open_incidents()
        resolved_incidents = persistence_store.load_resolved_incidents()
    except Exception as exc:
        logger.error("Failed to load incidents from persistence: %s", exc)
        open_incidents = []
        resolved_incidents = []
    INCIDENT_LOG.clear()
    RESOLVED_INCIDENT_LOG.clear()
    INCIDENT_LOG.extend(open_incidents)
    RESOLVED_INCIDENT_LOG.extend(resolved_incidents)

    try:
        persisted_audits = persistence_store.load_audits()
    except Exception as exc:
        logger.error("Failed to load audits from persistence: %s", exc)
        persisted_audits = []
    AUDIT_LOG.clear()
    for audit_entry in persisted_audits:
        AUDIT_LOG.append(
            AuditEntry(
                action=audit_entry.get("action"),
                customer_id=audit_entry.get("customer_id"),
                user=audit_entry.get("user"),
                dry_run=bool(audit_entry.get("dry_run")),
                status=audit_entry.get("status"),
                detail=audit_entry.get("detail") or {},
                timestamp=audit_entry.get("timestamp"),
            )
        )

    # Apply retention cleanup on startup
    try:
        persistence_store.purge_customer_events()
        persistence_store.purge_incidents()
        persistence_store.purge_audits()
        persistence_store.purge_reconciliation_results()
    except Exception as exc:
        logger.error("Failed to run persistence cleanup tasks: %s", exc)


_bootstrap_state_from_persistence()




async def _run_reconciliation(settings: EnvConfig) -> Dict[str, Any]:
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        isp_customers = await isp_service.list_customers(settings, fetch_json)
    except Exception as exc:
        logger.error("Failed to fetch customers from ISP: %s", exc)
        isp_customers = []

    try:
        geogrid_clientes = await geogrid_service.list_clientes(settings, fetch_json)
    except Exception as exc:
        logger.error("Failed to fetch features from GeoGrid: %s", exc)
        geogrid_clientes = []

    isp_by_code: Dict[str, Dict[str, Any]] = {}
    isp_by_id: Dict[int, Dict[str, Any]] = {}
    for entry in isp_customers:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code") or "").strip().lower()
        if not code:
            continue
        isp_by_code[code] = entry
        try:
            cid = int(entry.get("customer_id"))
            isp_by_id[cid] = entry
        except (TypeError, ValueError):
            continue

    geogrid_by_code: Dict[str, Dict[str, Any]] = {}
    for cliente in geogrid_clientes:
        if not isinstance(cliente, dict):
            continue
        codigo = str(cliente.get("codigoIntegracao") or "").strip().lower()
        if codigo:
            geogrid_by_code[codigo] = cliente

    issues: List[Dict[str, Any]] = []
    counts: Dict[str, int] = defaultdict(int)

    for code, customer in isp_by_code.items():
        customer_id = customer.get("customer_id")
        status = str(customer.get("status") or "").lower()
        integration_enabled = bool(customer.get("integration_enabled", True))
        geogrid_entry = geogrid_by_code.get(code)
        if integration_enabled and status in {"enabled", "active"} and not geogrid_entry:
            issues.append(
                {
                    "reconciliation_id": str(uuid4()),
                    "issue_type": "missing_geogrid",
                    "customer_id": customer_id,
                    "detail": {
                        "message": "Cliente activo sin registro asociado en GeoGrid",
                        "customer_code": customer.get("code"),
                        "customer_name": customer.get("name"),
                    },
                }
            )
            counts["missing_geogrid"] += 1
        if geogrid_entry and status not in {"enabled", "active"}:
            issues.append(
                {
                    "reconciliation_id": str(uuid4()),
                    "issue_type": "inactive_geogrid_resource",
                    "customer_id": customer_id,
                    "detail": {
                        "message": "Cliente inactivo en ISP pero aún presente en GeoGrid",
                        "customer_code": customer.get("code"),
                        "geogrid_id": geogrid_entry.get("id"),
                    },
                }
            )
            counts["inactive_geogrid_resource"] += 1

        if geogrid_entry:
            assignments = geogrid_entry.get("assignments") or []
            customer_onu = str(customer.get("onu_sn") or "").strip().lower()
            if customer_onu and assignments:
                for assignment in assignments:
                    onu_serial = str(assignment.get("onuSerial") or "").strip().lower()
                    if onu_serial and onu_serial != customer_onu:
                        issues.append(
                            {
                                "reconciliation_id": str(uuid4()),
                                "issue_type": "geogrid_assignment_conflict",
                                "customer_id": customer_id,
                                "detail": {
                                    "message": "Asignación de puerto con ONU distinta a la registrada en ISP",
                                    "customer_code": customer.get("code"),
                                    "expected_onu": customer.get("onu_sn"),
                                    "geogrid_onu": assignment.get("onuSerial"),
                                    "id_porta": assignment.get("idPorta"),
                                },
                            }
                        )
                        counts["geogrid_assignment_conflict"] += 1

            if (
                integration_enabled
                and status in {"enabled", "active"}
                and customer.get("pon") is not None
                and not assignments
            ):
                issues.append(
                    {
                        "reconciliation_id": str(uuid4()),
                        "issue_type": "missing_geogrid_assignment",
                        "customer_id": customer_id,
                        "detail": {
                            "message": "Cliente con datos de red sin asignación de puerto en GeoGrid",
                            "customer_code": customer.get("code"),
                            "geogrid_id": geogrid_entry.get("id"),
                        },
                    }
                )
                counts["missing_geogrid_assignment"] += 1

    for cliente in geogrid_clientes:
        codigo = str(cliente.get("codigoIntegracao") or "").strip().lower()
        if not codigo:
            continue
        if codigo not in isp_by_code:
            issues.append(
                {
                    "reconciliation_id": str(uuid4()),
                    "issue_type": "orphan_geogrid_cliente",
                    "customer_id": None,
                    "detail": {
                        "message": "Cliente presente en GeoGrid sin contraparte en ISP",
                        "geogrid_id": cliente.get("id"),
                        "codigo_integracao": cliente.get("codigoIntegracao"),
                    },
                }
            )
            counts["orphan_geogrid_cliente"] += 1

    summary_entry = {
        "reconciliation_id": str(uuid4()),
        "issue_type": "summary",
        "customer_id": None,
        "detail": {
            "counts": dict(counts),
            "totals": {
                "isp_customers": len(isp_by_code),
                "geogrid_clientes": len(geogrid_clientes),
            },
        },
    }

    try:
        persistence_store.save_reconciliation_results(timestamp, issues + [summary_entry])
        persistence_store.purge_reconciliation_results()
    except Exception as exc:
        logger.error("Failed to persist reconciliation results: %s", exc)

    return {
        "generated_at": timestamp,
        "totals": {
            "isp_customers": len(isp_by_code),
            "geogrid_clientes": len(geogrid_clientes),
        },
        "issues": issues,
        "issue_counts": dict(counts),
    }


def _format_customer_label(
    customer_id: Optional[Any], customer_name: Optional[str]
) -> str:
    parts: List[str] = []
    if customer_id is not None:
        customer_id_str = str(customer_id).strip()
        if customer_id_str:
            parts.append(customer_id_str)
    if customer_name:
        name_str = str(customer_name).strip()
        if name_str:
            parts.append(name_str)
    if not parts:
        return "Cliente sin identificar"
    if len(parts) == 1:
        return parts[0]
    return " – ".join(parts)


def _ensure_latest_events_cache() -> None:
    if LATEST_CUSTOMER_EVENTS:
        return
    if not CUSTOMER_EVENTS:
        return
    for event in CUSTOMER_EVENTS:
        key = _normalize_customer_id(event.get("customer_id"))
        if key is None:
            key = event.get("event_id")
        if key is None:
            continue
        # Preserve recency by removing before re-adding
        if key in LATEST_CUSTOMER_EVENTS:
            del LATEST_CUSTOMER_EVENTS[key]
        LATEST_CUSTOMER_EVENTS[key] = dict(event)
        while len(LATEST_CUSTOMER_EVENTS) > CUSTOMER_EVENT_BUFFER_SIZE:
            LATEST_CUSTOMER_EVENTS.popitem(last=False)


def _event_with_resolved_coordinates(event: Dict[str, Any]) -> Dict[str, Any]:
    zone_name = event.get("zone") or DEFAULT_ZONE_LABEL
    lat = event.get("lat")
    lon = event.get("lon")
    if lat is None or lon is None:
        fallback_lat, fallback_lon = _resolve_zone_coordinates(zone_name)
        if lat is None:
            lat = fallback_lat if fallback_lat is not None else DEFAULT_COORDINATE_FALLBACK[0]
        if lon is None:
            lon = fallback_lon if fallback_lon is not None else DEFAULT_COORDINATE_FALLBACK[1]
    metadata = event.get("metadata") or {}
    customer_name = event.get("customer_name") or metadata.get("customer_name")
    customer_label = event.get("customer_label") or _format_customer_label(
        event.get("customer_id"), customer_name
    )
    return {
        "event_id": event.get("event_id"),
        "timestamp": event.get("timestamp"),
        "event_type": event.get("event_type"),
        "zone": zone_name,
        "city": event.get("city"),
        "customer_id": event.get("customer_id"),
        "lat": lat,
        "lon": lon,
        "customer_name": customer_name,
        "customer_label": customer_label,
        "metadata": metadata,
    }


def _events_to_feature_collection(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    features: List[Dict[str, Any]] = []
    for raw_event in events:
        event = _event_with_resolved_coordinates(raw_event)
        lat = event.get("lat")
        lon = event.get("lon")
        if lat is None or lon is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "event_id": event.get("event_id"),
                    "timestamp": event.get("timestamp"),
                    "event_type": event.get("event_type"),
                    "zone": event.get("zone"),
                    "city": event.get("city"),
                    "customer_id": event.get("customer_id"),
                    "customer_name": event.get("customer_name"),
                    "customer_label": event.get("customer_label"),
                },
            }
        )
    return {
        "type": "FeatureCollection",
        "features": features,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(features),
    }


def _build_events_table_frame(
    ref_id: str, events: List[Dict[str, Any]]
) -> Dict[str, Any]:
    columns = [
        {"text": "timestamp", "type": "time"},
        {"text": "timestamp_iso", "type": "string"},
        {"text": "zone", "type": "string"},
        {"text": "lat", "type": "number"},
        {"text": "lon", "type": "number"},
        {"text": "customer_label", "type": "string"},
        {"text": "customer_id", "type": "string"},
        {"text": "customer_name", "type": "string"},
        {"text": "city", "type": "string"},
        {"text": "event_type", "type": "string"},
    ]
    rows: List[List[Any]] = []
    for event in events:
        timestamp_raw = event.get("timestamp")
        timestamp_dt = (
            _parse_iso8601(str(timestamp_raw)) if timestamp_raw is not None else datetime.now(timezone.utc)
        )
        timestamp_ms = int(timestamp_dt.timestamp() * 1000)
        metadata = event.get("metadata") or {}
        customer_name = event.get("customer_name") or metadata.get("customer_name")
        customer_id = "" if event.get("customer_id") is None else str(event.get("customer_id"))
        customer_label = event.get("customer_label") or _format_customer_label(
            customer_id if customer_id else None,
            customer_name,
        )
        rows.append(
            [
                timestamp_ms,
                timestamp_dt.isoformat(),
                event.get("zone") or DEFAULT_ZONE_LABEL,
                event.get("lat"),
                event.get("lon"),
                customer_label,
                customer_id,
                customer_name or "",
                event.get("city") or "",
                event.get("event_type") or "",
            ]
        )
    return {
        "refId": ref_id,
        "type": "table",
        "columns": columns,
        "rows": rows,
    }


def _build_empty_table_frame(ref_id: str) -> Dict[str, Any]:
    return _build_events_table_frame(ref_id, [])


def _build_reconciliation_results_frame(
    ref_id: str, issues: List[Dict[str, Any]]
) -> Dict[str, Any]:
    columns = [
        {"text": "timestamp", "type": "time"},
        {"text": "timestamp_iso", "type": "string"},
        {"text": "issue_type", "type": "string"},
        {"text": "customer_id", "type": "string"},
        {"text": "detail", "type": "string"},
    ]
    rows: List[List[Any]] = []
    for issue in issues:
        if issue.get("issue_type") == "summary":
            continue
        timestamp_raw = issue.get("timestamp")
        timestamp_dt = (
            _parse_iso8601(str(timestamp_raw)) if timestamp_raw else datetime.now(timezone.utc)
        )
        timestamp_ms = int(timestamp_dt.timestamp() * 1000)
        detail = issue.get("detail") or {}
        detail_str = json.dumps(detail, ensure_ascii=False) if isinstance(detail, dict) else str(detail)
        customer_id = issue.get("customer_id")
        rows.append(
            [
                timestamp_ms,
                timestamp_dt.isoformat(),
                issue.get("issue_type") or "",
                "" if customer_id is None else str(customer_id),
                detail_str,
            ]
        )
    return {
        "refId": ref_id,
        "type": "table",
        "columns": columns,
        "rows": rows,
    }


def _build_reconciliation_summary_frame(
    ref_id: str, issues: List[Dict[str, Any]]
) -> Dict[str, Any]:
    columns = [
        {"text": "timestamp", "type": "time"},
        {"text": "timestamp_iso", "type": "string"},
        {"text": "issue_type", "type": "string"},
        {"text": "count", "type": "number"},
    ]
    rows: List[List[Any]] = []
    summary_records = [issue for issue in issues if issue.get("issue_type") == "summary"]
    if summary_records:
        latest_summary = summary_records[0]
        timestamp_raw = latest_summary.get("timestamp")
        timestamp_dt = _parse_iso8601(str(timestamp_raw)) if timestamp_raw else datetime.now(timezone.utc)
        timestamp_ms = int(timestamp_dt.timestamp() * 1000)
        detail = latest_summary.get("detail") or {}
        counts = detail.get("counts") if isinstance(detail, dict) else {}
        if isinstance(counts, dict) and counts:
            for issue_type, count in sorted(counts.items(), key=lambda item: item[0]):
                rows.append(
                    [
                        timestamp_ms,
                        timestamp_dt.isoformat(),
                        issue_type,
                        count,
                    ]
                )
        else:
            rows.append(
                [
                    timestamp_ms,
                    timestamp_dt.isoformat(),
                    "sin_inconsistencias",
                    0,
                ]
            )
    else:
        timestamp_dt = datetime.now(timezone.utc)
        timestamp_ms = int(timestamp_dt.timestamp() * 1000)
        rows.append(
            [
                timestamp_ms,
                timestamp_dt.isoformat(),
                "sin_datos",
                0,
            ]
        )
    return {
        "refId": ref_id,
        "type": "table",
        "columns": columns,
        "rows": rows,
    }


def _resolve_lookback_days(range_info: Dict[str, Any], candidate: Any) -> int:
    if candidate is not None:
        try:
            value = int(candidate)
            if value >= 1:
                return value
        except (TypeError, ValueError):
            pass
    time_from = range_info.get("from")
    time_to = range_info.get("to")
    if time_from and time_to:
        try:
            parsed_from = _parse_iso8601(str(time_from))
            parsed_to = _parse_iso8601(str(time_to))
            delta = parsed_to - parsed_from
            days = delta.total_seconds() / 86400.0
            return max(1, int(math.ceil(days)))
        except Exception:
            pass
    return 30


async def _seed_isp_customers(settings: EnvConfig) -> Dict[str, int]:
    # Los clientes demo ya están embebidos en el mock de ISP-Cube.
    return {"seeded": 0, "skipped": 0}


async def _ensure_customer_seed(settings: EnvConfig) -> None:
    try:
        await _seed_isp_customers(settings)
    except Exception as exc:
        logger.warning("Unable to ensure customer seed: %s", exc)


def _filter_resolved_incidents(
    lookback_days: int,
    *,
    customer_id: Optional[Any] = None,
    kind: Optional[str] = None,
) -> List[Dict[str, Any]]:
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    normalized_customer = _normalize_customer_id(customer_id)
    results: List[Dict[str, Any]] = []
    for incident in reversed(RESOLVED_INCIDENT_LOG):
        resolved_at_raw = incident.get("resolved_at")
        if not resolved_at_raw:
            continue
        resolved_at = _parse_iso8601(str(resolved_at_raw))
        if resolved_at < since:
            continue
        if kind and incident.get("kind") != kind:
            continue
        if normalized_customer and _normalize_customer_id(incident.get("customer_id")) != normalized_customer:
            continue
        enriched = dict(incident)
        enriched["resolved_at"] = resolved_at.isoformat()
        original_ts = incident.get("timestamp")
        if original_ts:
            enriched["timestamp"] = _parse_iso8601(str(original_ts)).isoformat()
        else:
            enriched["timestamp"] = resolved_at.isoformat()
        results.append(enriched)
    return results


def _build_resolved_incidents_frame(
    ref_id: str, incidents: List[Dict[str, Any]]
) -> Dict[str, Any]:
    columns = [
        {"text": "resolved_at", "type": "time"},
        {"text": "kind", "type": "string"},
        {"text": "customer_id", "type": "string"},
        {"text": "action", "type": "string"},
        {"text": "incident_at", "type": "time"},
        {"text": "incident_id", "type": "string"},
        {"text": "resolution_reason", "type": "string"},
        {"text": "resolved_by", "type": "string"},
        {"text": "context", "type": "string"},
    ]
    rows: List[List[Any]] = []
    for incident in incidents:
        resolved_at_str = incident.get("resolved_at")
        incident_at_str = incident.get("timestamp")
        kind_key = incident.get("kind") or ""
        fallback_label = kind_key.replace("_", " ").strip().title() if kind_key else ""
        kind_label = INCIDENT_KIND_LABELS.get(kind_key, fallback_label)
        resolved_dt = _parse_iso8601(str(resolved_at_str)) if resolved_at_str else datetime.now(timezone.utc)
        incident_dt = _parse_iso8601(str(incident_at_str)) if incident_at_str else resolved_dt
        resolved_ms = int(resolved_dt.timestamp() * 1000)
        incident_ms = int(incident_dt.timestamp() * 1000)
        context_payload = {
            key: value
            for key, value in incident.items()
            if key
            not in {
                "resolved_at",
                "resolution_reason",
                "resolved_by",
                "kind",
                "customer_id",
                "action",
                "timestamp",
                "incident_id",
            }
        }
        if kind_key:
            context_payload["kind_key"] = kind_key
        rows.append(
            [
                resolved_ms,
                kind_label,
                _normalize_customer_id(incident.get("customer_id")) or "",
                incident.get("action") or "",
                incident_ms,
                incident.get("incident_id") or "",
                incident.get("resolution_reason") or "",
                incident.get("resolved_by") or "",
                json.dumps(context_payload, separators=(",", ":"), ensure_ascii=True),
            ]
        )
    return {
        "refId": ref_id,
        "type": "table",
        "columns": columns,
        "rows": rows,
    }


def _build_open_incidents_frame(ref_id: str, incidents: List[Dict[str, Any]]) -> Dict[str, Any]:
    columns = [
        {"text": "detected_at", "type": "time"},
        {"text": "kind", "type": "string"},
        {"text": "customer_label", "type": "string"},
        {"text": "customer_id", "type": "string"},
        {"text": "action", "type": "string"},
        {"text": "incident_id", "type": "string"},
        {"text": "context", "type": "string"},
    ]
    rows: List[List[Any]] = []
    for incident in incidents:
        ts_raw = incident.get("timestamp") or incident.get("detected_at")
        ts_dt = _parse_iso8601(str(ts_raw)) if ts_raw else datetime.now(timezone.utc)
        ts_ms = int(ts_dt.timestamp() * 1000)
        customer_label = incident.get("customer_label") or _format_customer_label(
            incident.get("customer_id"), incident.get("customer_name")
        )
        context_payload = {
            key: value
            for key, value in incident.items()
            if key
            not in {
                "timestamp",
                "detected_at",
                "kind",
                "customer_id",
                "customer_name",
                "customer_label",
                "action",
                "incident_id",
            }
        }
        rows.append(
            [
                ts_ms,
                incident.get("kind") or "",
                customer_label,
                _normalize_customer_id(incident.get("customer_id")) or "",
                incident.get("action") or "",
                incident.get("incident_id") or "",
                json.dumps(context_payload, separators=(",", ":"), ensure_ascii=True),
            ]
        )
    return {
        "refId": ref_id,
        "type": "table",
        "columns": columns,
        "rows": rows,
    }


def _build_incidents_summary_frame(
    ref_id: str, *, lookback_days: int
) -> Dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    seen_incident_ids: set[str] = set()
    new_incidents = 0
    for incident in list(INCIDENT_LOG) + list(RESOLVED_INCIDENT_LOG):
        ts_raw = incident.get("timestamp")
        if not ts_raw:
            continue
        try:
            ts = _parse_iso8601(str(ts_raw))
        except Exception:
            continue
        if ts < since:
            continue
        incident_id = str(incident.get("incident_id") or uuid4())
        if incident_id in seen_incident_ids:
            continue
        seen_incident_ids.add(incident_id)
        new_incidents += 1

    resolved_incidents = 0
    for incident in RESOLVED_INCIDENT_LOG:
        resolved_at = incident.get("resolved_at")
        if not resolved_at:
            continue
        try:
            resolved_ts = _parse_iso8601(str(resolved_at))
        except Exception:
            continue
        if resolved_ts >= since:
            resolved_incidents += 1

    open_incidents = len(INCIDENT_LOG)
    return {
        "refId": ref_id,
        "type": "table",
        "columns": [
            {"text": "new_incidents", "type": "number"},
            {"text": "resolved_incidents", "type": "number"},
            {"text": "open_incidents", "type": "number"},
        ],
        "rows": [[new_incidents, resolved_incidents, open_incidents]],
    }


def resolve_incidents(
    *,
    customer_id: Optional[Any],
    action: Optional[str] = None,
    kinds: Optional[Iterable[str]] = None,
    resolved_by: str = "system",
    reason: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if customer_id is None:
        return []
    normalized_customer = _normalize_customer_id(customer_id)
    if not normalized_customer:
        return []
    kind_set = set(kinds) if kinds else None
    now_iso = datetime.now(timezone.utc).isoformat()
    remaining: deque[Dict[str, Any]] = deque(maxlen=INCIDENT_BUFFER_SIZE)
    resolved_entries: List[Dict[str, Any]] = []
    for entry in list(INCIDENT_LOG):
        entry_customer = _normalize_customer_id(entry.get("customer_id"))
        if entry_customer != normalized_customer:
            remaining.append(entry)
            continue
        entry_action = entry.get("action")
        if action and entry_action and entry_action != action:
            remaining.append(entry)
            continue
        if kind_set and entry.get("kind") not in kind_set:
            remaining.append(entry)
            continue
        resolved_entry = dict(entry)
        if action and not resolved_entry.get("action"):
            resolved_entry["action"] = action
        resolved_entry["resolved_at"] = now_iso
        resolved_entry["resolved_by"] = resolved_by
        if reason:
            resolved_entry["resolution_reason"] = reason
        RESOLVED_INCIDENT_LOG.append(resolved_entry)
        INCIDENT_RESOLVED_COUNTER.labels(kind=resolved_entry.get("kind")).inc()
        resolved_entries.append(resolved_entry)
    if resolved_entries:
        INCIDENT_LOG.clear()
        INCIDENT_LOG.extend(remaining)
        INCIDENT_GAUGE.set(len(INCIDENT_LOG))
        try:
            persistence_store.mark_incidents_resolved(resolved_entries)
            persistence_store.purge_incidents()
        except Exception as exc:
            logger.error("Failed to persist resolved incidents: %s", exc)
    return resolved_entries


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
    event_customer_id = (
        customer_id if customer_id is not None else customer.get("customer_id") if customer else None
    )
    previous_event = _latest_customer_event(event_customer_id)

    zone_candidate = zone or (customer.get("zone") if customer else None)
    if _is_blank(zone_candidate) and previous_event and not _is_blank(previous_event.get("zone")):
        zone_candidate = previous_event.get("zone")
    safe_zone = zone_candidate or DEFAULT_ZONE_LABEL

    city_candidate = city or (customer.get("city") if customer else None)
    if _is_blank(city_candidate) and previous_event and not _is_blank(previous_event.get("city")):
        city_candidate = previous_event.get("city")
    if isinstance(city_candidate, dict):
        safe_city = (
            city_candidate.get("name")
            or city_candidate.get("city")
            or ""
        )
    elif city_candidate is not None:
        safe_city = str(city_candidate)
    else:
        safe_city = ""
    safe_city = safe_city.strip()

    customer_name = customer.get("name") if customer else None
    if not customer_name and metadata:
        customer_name = metadata.get("customer_name")
    lat_value = _safe_float(lat if lat is not None else customer.get("lat") if customer else None)
    lon_source = None
    if lon is not None:
        lon_source = lon
    elif customer:
        lon_source = customer.get("lon")
        if lon_source is None:
            lon_source = customer.get("lng")
    lon_value = _safe_float(lon_source)
    if lat_value is None and previous_event is not None:
        lat_value = _safe_float(previous_event.get("lat"))
    if lon_value is None and previous_event is not None:
        lon_value = _safe_float(previous_event.get("lon"))
    if lat_value is None or lon_value is None:
        fallback_lat, fallback_lon = _resolve_zone_coordinates(safe_zone)
        if lat_value is None:
            lat_value = fallback_lat if fallback_lat is not None else DEFAULT_COORDINATE_FALLBACK[0]
        if lon_value is None:
            lon_value = fallback_lon if fallback_lon is not None else DEFAULT_COORDINATE_FALLBACK[1]

    connection_id: Optional[str] = None
    if customer:
        raw_code = customer.get("code")
        raw_customer_id = customer.get("customer_id")
        if raw_code:
            connection_id = str(raw_code).strip()
        elif raw_customer_id is not None:
            connection_id = str(raw_customer_id).strip()
    if not connection_id and metadata:
        raw_connection = metadata.get("connection_id")
        if raw_connection:
            connection_id = str(raw_connection).strip()

    if connection_id and customer_name:
        display_label = f"{connection_id} - {customer_name}"
    elif connection_id:
        display_label = connection_id
    else:
        display_label = _format_customer_label(event_customer_id, customer_name)

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
        "customer_name": customer_name or "",
        "connection_id": connection_id,
    }
    event_entry["customer_label"] = display_label or ""

    CUSTOMER_EVENTS.append(event_entry)
    cache_key: Optional[Any] = _normalize_customer_id(event_customer_id)
    if cache_key is None:
        cache_key = event_entry["event_id"]
    if cache_key is not None:
        if cache_key in LATEST_CUSTOMER_EVENTS:
            del LATEST_CUSTOMER_EVENTS[cache_key]
        LATEST_CUSTOMER_EVENTS[cache_key] = dict(event_entry)
        # Enforce a similar cap as the main buffer to avoid unbounded growth
        while len(LATEST_CUSTOMER_EVENTS) > CUSTOMER_EVENT_BUFFER_SIZE:
            LATEST_CUSTOMER_EVENTS.popitem(last=False)
    CUSTOMER_EVENT_COUNTER.labels(event_type=event_type, zone=safe_zone).inc()
    try:
        persistence_store.save_customer_event(event_entry)
        persistence_store.purge_customer_events()
    except Exception as exc:
        logger.error("Failed to persist customer event %s: %s", event_entry.get("event_id"), exc)
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
    detail_copy = dict(detail) if detail else {}
    customer_id = detail_copy.get("customer_id")
    customer_name = detail_copy.get("customer_name")
    if not customer_name and customer_id is not None:
        customer_name = _lookup_customer_name(customer_id)
        if customer_name:
            detail_copy.setdefault("customer_name", customer_name)
    detail_copy["customer_label"] = _format_customer_label(
        detail_copy.get("customer_id"), detail_copy.get("customer_name")
    )
    entry = {
        "incident_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        **detail_copy,
    }
    INCIDENT_LOG.append(entry)
    INCIDENT_COUNTER.labels(kind=kind).inc()
    INCIDENT_GAUGE.set(len(INCIDENT_LOG))
    try:
        persistence_store.save_incident(entry)
    except Exception as exc:
        logger.error("Failed to persist incident %s: %s", entry.get("incident_id"), exc)
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
    try:
        persistence_store.save_audit(
            {
                "audit_id": str(uuid4()),
                "action": entry.action,
                "customer_id": entry.customer_id,
                "user": entry.user,
                "dry_run": 1 if entry.dry_run else 0,
                "status": entry.status,
                "detail": entry.detail,
                "timestamp": entry.timestamp,
            }
        )
        persistence_store.purge_audits()
    except Exception as exc:
        logger.error("Failed to persist audit entry: %s", exc)


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
    latest_per_customer: bool = False,
) -> List[Dict[str, Any]]:
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    selected: List[Dict[str, Any]] = []
    if latest_per_customer:
        _ensure_latest_events_cache()
        seen_keys: Set[Any] = set()
        cached_events = list(LATEST_CUSTOMER_EVENTS.values())
        for event in reversed(cached_events):
            ts = _parse_iso8601(str(event["timestamp"]))
            if ts < since:
                continue
            event_copy = dict(event)
            event_copy["timestamp"] = ts.isoformat()
            key = _normalize_customer_id(event_copy.get("customer_id"))
            if key is None:
                key = event_copy.get("event_id")
            if key is None:
                continue
            if key in seen_keys:
                continue
            seen_keys.add(key)
            if zone and event_copy.get("zone") != zone:
                continue
            if event_type and event_copy.get("event_type") != event_type:
                continue
            selected.append(event_copy)
    else:
        for event in reversed(CUSTOMER_EVENTS):
            ts = _parse_iso8601(str(event["timestamp"]))
            if ts < since:
                continue
            event_copy = dict(event)
            event_copy["timestamp"] = ts.isoformat()
            if zone and event_copy.get("zone") != zone:
                continue
            if event_type and event_copy.get("event_type") != event_type:
                continue
            selected.append(event_copy)
    return selected


def _latest_customer_event(customer_id: Any) -> Optional[Dict[str, Any]]:
    if customer_id is None:
        return None
    normalized = _normalize_customer_id(customer_id)
    if normalized is not None:
        _ensure_latest_events_cache()
        cached = LATEST_CUSTOMER_EVENTS.get(normalized)
        if cached is not None:
            return cached
    for event in reversed(CUSTOMER_EVENTS):
        if _normalize_customer_id(event.get("customer_id")) == normalized:
            return event
    return None


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
        lat_raw = customer.get("lat")
        lon_raw = customer.get("lon")
        if lon_raw is None:
            lon_raw = customer.get("lng")
        lat = float(lat_raw)
        lon = float(lon_raw)
    except (TypeError, ValueError):
        record_incident(
            "invalid_coordinates",
            {
                "customer_id": customer_id,
                "action": action,
                "lat": customer.get("lat"),
                "lon": customer.get("lon") or customer.get("lng"),
                "reason": "non_numeric",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "Customer coordinates are invalid",
                "customer_id": customer_id,
                "lat": customer.get("lat"),
                "lon": customer.get("lon") or customer.get("lng"),
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
                "action": action,
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


def _customer_zone(customer: Dict[str, Any]) -> str:
    zone = customer.get("zone")
    if isinstance(zone, str) and zone.strip():
        return zone.strip()
    city = customer.get("city")
    if isinstance(city, dict):
        name = city.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return DEFAULT_ZONE_LABEL


def _customer_city(customer: Dict[str, Any]) -> str:
    city = customer.get("city")
    if isinstance(city, dict):
        name = city.get("name")
        if isinstance(name, str):
            return name
    if isinstance(city, str):
        return city
    return ""


def _customer_state(customer: Dict[str, Any]) -> str:
    city = customer.get("city")
    if isinstance(city, dict):
        province = city.get("province") or city.get("state")
        if isinstance(province, str):
            return province
    return ""


def _customer_coordinates(customer: Dict[str, Any]) -> Tuple[float, float]:
    lat = float(customer.get("lat"))
    lon_raw = customer.get("lon")
    if lon_raw is None:
        lon_raw = customer.get("lng")
    lon = float(lon_raw)
    return lat, lon


def _build_geogrid_cliente_payload(customer: Dict[str, Any]) -> Dict[str, Any]:
    lat, lon = _customer_coordinates(customer)
    codigo = str(customer.get("code") or customer.get("customer_id") or "").strip()
    if not codigo:
        codigo = f"customer-{customer.get('customer_id', 'unknown')}"
    customer_name = str(customer.get("name") or codigo)
    display_name = f"{codigo} - {customer_name}"
    observaciones: List[str] = []
    zone = _customer_zone(customer)
    if zone:
        observaciones.append(f"Zona: {zone}")
    if customer.get("onu_sn"):
        observaciones.append(f"ONU: {customer.get('onu_sn')}")
    observacion = " | ".join(observaciones) if observaciones else None
    return {
        "codigoIntegracao": codigo,
        "nome": display_name,
        "endereco": customer.get("address") or "",
        "cidade": _customer_city(customer) or zone,
        "estado": _customer_state(customer) or "NA",
        "latitude": lat,
        "longitude": lon,
        "observacao": observacion,
    }


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
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid4())
    token = request_id_ctx.set(request_id)
    try:
        response = await call_next(request)
    finally:
        request_id_ctx.reset(token)
    response.headers[REQUEST_ID_HEADER] = request_id
    return response


@app.on_event("startup")
async def seed_customers_on_startup() -> None:
    await _ensure_customer_seed(SETTINGS)


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
                break
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
        customer = await isp_service.get_customer(
            settings, payload.customer_id, fetch_json
        )
        ensure_customer_ready(customer, action="sync")

        cliente_payload = _build_geogrid_cliente_payload(customer)
        geogrid_id, action = await geogrid_service.upsert_cliente(
            settings, cliente_payload, fetch_json
        )
        SYNC_COUNTER.labels(result=action).inc()
        resolve_incidents(
            customer_id=payload.customer_id,
            action="sync",
            resolved_by=user,
            reason=f"sync_{action}",
        )
        result = {"geogrid_id": geogrid_id, "action": action}

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
    summary="Asignar un cliente a una puerta en GeoGrid",
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

    if payload.customer_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "customer_id es obligatorio para provisionar"},
        )

    try:
        customer = await isp_service.get_customer(
            settings, payload.customer_id, fetch_json
        )
        ensure_customer_ready(customer, action="provision")
        ensure_alignment(customer, payload)
        cliente_payload = _build_geogrid_cliente_payload(customer)
        geogrid_id, geogrid_action = await geogrid_service.upsert_cliente(
            settings, cliente_payload, fetch_json
        )
        if geogrid_action in {"created", "updated"}:
            resolve_incidents(
                customer_id=payload.customer_id,
                action="sync",
                resolved_by=user,
                reason=f"sync_{geogrid_action}",
            )

        if dry_run_flag:
            PROVISION_COUNTER.labels(result="dry_run").inc()
            detail = {
                "dry_run": True,
                "status": "skipped",
                "message": "Asignación en GeoGrid omitida por dry-run",
                "geogrid_id": geogrid_id,
            }
            record_audit(
                AuditEntry(
                    action="provision",
                    customer_id=payload.customer_id,
                    user=user,
                    dry_run=True,
                    status="success",
                    detail=detail,
                    timestamp=timestamp,
                )
            )
            return detail

        port_identifier = f"OLT{payload.olt_id}-B{payload.board}-P{payload.pon_port}"
        assignment_payload = {
            "idCliente": geogrid_id,
            "idPorta": port_identifier,
            "oltId": payload.olt_id,
            "board": payload.board,
            "pon": payload.pon_port,
            "onuSerial": payload.onu_sn,
            "observacao": f"Asignado por {user} en {timestamp}",
        }

        try:
            assignment_result = await geogrid_service.assign_port(
                settings, assignment_payload
            )
        except HTTPException as exc:
            if exc.status_code == status.HTTP_409_CONFLICT:
                record_incident(
                    "geogrid_assignment_conflict",
                    {
                        "customer_id": payload.customer_id,
                        "request": assignment_payload,
                        "detail": exc.detail,
                    },
                )
            raise

        PROVISION_COUNTER.labels(result="assigned").inc()
        resolve_incidents(
            customer_id=payload.customer_id,
            action="provision",
            resolved_by=user,
            reason="assigned",
        )
        record_audit(
            AuditEntry(
                action="provision",
                customer_id=payload.customer_id,
                user=user,
                dry_run=False,
                status="success",
                detail={
                    "geogrid_id": geogrid_id,
                    "assignment": assignment_result,
                },
                timestamp=timestamp,
            )
        )
        register_customer_event(
            "alta",
            customer=customer,
            source="runtime",
            metadata={
                "trigger": "provision",
                "geogrid_id": geogrid_id,
                "port": port_identifier,
            },
        )
        return {
            "status": "assigned",
            "geogrid_id": geogrid_id,
            "assignment": assignment_result,
        }
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
        logger.exception("Unhandled error during provisioning")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail)


@app.post(
    "/decommission/customer",
    status_code=status.HTTP_200_OK,
    summary="Desasignar un cliente en GeoGrid",
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
        customer = await isp_service.get_customer(
            settings, payload.customer_id, fetch_json
        )
        ensure_customer_inactive(customer)
        ensure_customer_has_network_keys(customer, action="decommission")

        codigo_integracion = str(customer.get("code") or customer.get("customer_id"))
        geogrid_cliente = await geogrid_service.get_cliente_by_codigo(
            settings, codigo_integracion, fetch_json
        )
        if not geogrid_cliente:
            record_incident(
                "decommission_missing_feature",
                {
                    "customer_id": payload.customer_id,
                    "customer_code": codigo_integracion,
                },
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "message": "GeoGrid no tiene registro para el cliente",
                    "customer_code": codigo_integracion,
                },
            )

        geogrid_id = geogrid_cliente.get("id")
        port_identifier = f"OLT{customer['olt_id']}-B{customer['board']}-P{customer['pon']}"

        if dry_run_flag:
            DECOMMISSION_COUNTER.labels(result="dry_run").inc()
            detail = {
                "dry_run": True,
                "status": "skipped",
                "geogrid_id": geogrid_id,
                "port": port_identifier,
            }
            record_audit(
                AuditEntry(
                    action="decommission",
                    customer_id=payload.customer_id,
                    user=user,
                    dry_run=True,
                    status="success",
                    detail=detail,
                    timestamp=timestamp,
                )
            )
            return detail

        try:
            await geogrid_service.remove_assignment(settings, port_identifier, geogrid_id)
        except HTTPException as exc:
            if exc.status_code == status.HTTP_404_NOT_FOUND:
                record_incident(
                    "decommission_missing_feature",
                    {
                        "customer_id": payload.customer_id,
                        "geogrid_id": geogrid_id,
                        "port": port_identifier,
                    },
                )
            raise

        DECOMMISSION_COUNTER.labels(result="removed").inc()
        resolve_incidents(
            customer_id=payload.customer_id,
            action="decommission",
            resolved_by=user,
            reason="removed",
        )
        record_audit(
            AuditEntry(
                action="decommission",
                customer_id=payload.customer_id,
                user=user,
                dry_run=False,
                status="success",
                detail={
                    "geogrid_id": geogrid_id,
                    "port": port_identifier,
                },
                timestamp=timestamp,
            )
        )
        register_customer_event(
            "baja",
            customer=customer,
            source="runtime",
            metadata={
                "trigger": "decommission",
                "geogrid_id": geogrid_id,
                "port": port_identifier,
            },
        )
        return {
            "status": "removed",
            "geogrid_id": geogrid_id,
            "port": port_identifier,
        }
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
        logger.exception("Unhandled error during decommission")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail)


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
        "endpoints": {
            "isp_base_url": settings.isp_base_url,
            "geogrid_base_url": settings.geogrid_base_url,
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
    for service_name in ("isp", "geogrid"):
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
    RESOLVED_INCIDENT_LOG.clear()
    AUDIT_LOG.clear()
    INCIDENT_GAUGE.set(0)
    CUSTOMER_EVENTS.clear()
    LATEST_CUSTOMER_EVENTS.clear()
    try:
        persistence_store.reset()
    except Exception as exc:
        logger.error("Failed to reset persistence store: %s", exc)
    await _ensure_customer_seed(settings)
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


@app.post(
    "/analytics/reconciliation/run",
    summary="Ejecuta conciliación entre ISP-Cube, GeoGrid y SmartOLT",
)
async def run_reconciliation_endpoint(
    settings: EnvConfig = Depends(get_settings),
) -> Dict[str, Any]:
    return await _run_reconciliation(settings)


@app.get(
    "/analytics/reconciliation/results",
    summary="Resultados de conciliaciones recientes",
)
async def list_reconciliation_results(
    limit: int = Query(default=200, ge=1, le=1000),
) -> Dict[str, Any]:
    issues = persistence_store.load_reconciliation_results(limit=limit)
    latest_timestamp: Optional[str] = issues[0].get("timestamp") if issues else None
    return {
        "generated_at": latest_timestamp,
        "count": len(issues),
        "issues": issues,
    }


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


@app.get(
    "/analytics/customer-events/map/altas",
    summary="Eventos de alta con coordenadas resueltas",
)
async def get_customer_events_map_altas(
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
) -> Dict[str, Any]:
    events = _filter_customer_events(
        lookback_days,
        zone=zone,
        event_type="alta",
        latest_per_customer=True,
    )
    feature_collection = _events_to_feature_collection(events)
    feature_collection["event_type"] = "alta"
    feature_collection["zone_filter"] = zone
    return feature_collection


@app.get(
    "/analytics/customer-events/map/bajas",
    summary="Eventos de baja con coordenadas resueltas",
)
async def get_customer_events_map_bajas(
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
) -> Dict[str, Any]:
    events = _filter_customer_events(
        lookback_days,
        zone=zone,
        event_type="baja",
        latest_per_customer=True,
    )
    feature_collection = _events_to_feature_collection(events)
    feature_collection["event_type"] = "baja"
    feature_collection["zone_filter"] = zone
    return feature_collection


@app.get("/")
async def root() -> Dict[str, Any]:
    return {"status": "ok", "service": "orchestrator"}


@app.post("/query")
async def grafana_query(request: Request) -> List[Dict[str, Any]]:
    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        logger.warning("Invalid JSON payload on /query: %s", exc)
        return []

    range_info = payload.get("range") or {}
    targets = payload.get("targets") or []
    if not isinstance(targets, list):
        return []

    responses: List[Dict[str, Any]] = []
    for target in targets:
        if not isinstance(target, dict):
            continue
        target_name = target.get("target")
        if not target_name:
            continue
        ref_id = str(target.get("refId", "A"))
        target_payload = target.get("payload") or {}
        if not isinstance(target_payload, dict):
            target_payload = {}

        lookback_days = _resolve_lookback_days(range_info, target_payload.get("lookback_days"))
        zone_filter = target_payload.get("zone")
        if isinstance(zone_filter, str) and not zone_filter.strip():
            zone_filter = None

        event_type = target_payload.get("event_type")
        if event_type not in {"alta", "baja"}:
            event_type = None

        if target_name == "customer_events_map":
            events = _filter_customer_events(
                lookback_days,
                zone=zone_filter,
                event_type=event_type,
                latest_per_customer=True,
            )
            resolved_events = [_event_with_resolved_coordinates(event) for event in events]
            responses.append(_build_events_table_frame(ref_id, resolved_events))
        elif target_name == "incidents_resolved":
            customer_filter = target_payload.get("customer_id")
            kind_filter = target_payload.get("kind")
            incidents = _filter_resolved_incidents(
                lookback_days,
                customer_id=customer_filter,
                kind=kind_filter,
            )
            responses.append(_build_resolved_incidents_frame(ref_id, incidents))
        elif target_name == "incidents_open":
            kind_filter = target_payload.get("kind")
            since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
            open_incidents: List[Dict[str, Any]] = []
            for incident in list(INCIDENT_LOG):
                ts_raw = incident.get("timestamp") or incident.get("detected_at")
                incident_ts = _parse_iso8601(str(ts_raw)) if ts_raw else datetime.now(timezone.utc)
                if incident_ts < since:
                    continue
                if kind_filter and incident.get("kind") != kind_filter:
                    continue
                open_incidents.append(dict(incident))
            def _incident_ts(value: Dict[str, Any]) -> float:
                raw_ts = value.get("timestamp") or value.get("detected_at")
                try:
                    return _parse_iso8601(str(raw_ts)).timestamp()
                except Exception:
                    return datetime.now(timezone.utc).timestamp()

            open_incidents.sort(key=_incident_ts, reverse=True)
            responses.append(_build_open_incidents_frame(ref_id, open_incidents))
        elif target_name == "incidents_summary":
            responses.append(
                _build_incidents_summary_frame(ref_id, lookback_days=lookback_days)
            )
        elif target_name == "reconciliation_results":
            try:
                limit = int(target_payload.get("limit", 200))
            except (TypeError, ValueError):
                limit = 200
            issues = persistence_store.load_reconciliation_results(limit=limit)
            responses.append(_build_reconciliation_results_frame(ref_id, issues))
        elif target_name == "reconciliation_summary":
            try:
                limit = int(target_payload.get("limit", 200))
            except (TypeError, ValueError):
                limit = 200
            issues = persistence_store.load_reconciliation_results(limit=limit)
            responses.append(_build_reconciliation_summary_frame(ref_id, issues))
        else:
            responses.append(_build_empty_table_frame(ref_id))
    return responses


@app.get("/incidents")
async def list_incidents(kind: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
    entries = list(INCIDENT_LOG)
    if kind:
        entries = [entry for entry in entries if entry["kind"] == kind]
    return entries


@app.get("/incidents/resolved")
async def list_resolved_incidents(
    lookback_days: int = Query(default=30, ge=1, le=365),
    kind: Optional[str] = Query(default=None),
    customer_id: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
) -> List[Dict[str, Any]]:
    entries = _filter_resolved_incidents(
        lookback_days, customer_id=customer_id, kind=kind
    )
    return entries[:limit]


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
