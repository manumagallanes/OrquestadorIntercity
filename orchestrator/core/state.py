import os
from collections import OrderedDict, deque
from typing import Any, Dict, List, Tuple
from pathlib import Path
from prometheus_client import Counter, Gauge, Histogram

from .config import SETTINGS

# Constants
TRUE_VALUES = {"1", "true", "yes", "on"}
COORDINATE_FIELDS = {"lat", "lng"}
ALLOW_COORDINATE_FALLBACK = (
    os.getenv("ORCHESTRATOR_ALLOW_COORDINATE_FALLBACK", "false").strip().lower()
    in TRUE_VALUES
)
ALLOW_MISSING_NETWORK_KEYS = (
    os.getenv("ORCHESTRATOR_ALLOW_MISSING_NETWORK_KEYS", "false").strip().lower()
    in TRUE_VALUES
)
AUTO_GEOGRID_ATTEND = (
    os.getenv("ORCHESTRATOR_GEOGRID_AUTO_ATTEND", "false").strip().lower()
    in TRUE_VALUES
)

CORE_CUSTOMER_FIELDS: List[str] = [
    "lat",
    "lng",
    "address",
    "name",
]
OPTIONAL_NETWORK_FIELDS: List[str] = [
    "olt_id",
    "board",
    "pon",
    "onu_sn",
    "plan_id",
    "ftthbox_id",
    "ftth_port_id",
]

CORDOBA_LAT_RANGE: Tuple[float, float] = (-35.5, -29.0)
CORDOBA_LON_RANGE: Tuple[float, float] = (-66.5, -62.0)

INCIDENT_BUFFER_SIZE = int(os.getenv("INCIDENT_BUFFER_SIZE", "200"))
RESOLVED_INCIDENT_BUFFER_SIZE = int(os.getenv("INCIDENT_RESOLVED_BUFFER_SIZE", "500"))
CUSTOMER_EVENT_BUFFER_SIZE = int(os.getenv("CUSTOMER_EVENT_BUFFER_SIZE", "2000"))
AUDIT_BUFFER_SIZE = int(os.getenv("AUDIT_BUFFER_SIZE", "500"))

# State Containers
INCIDENT_LOG: deque[Dict[str, Any]] = deque(maxlen=INCIDENT_BUFFER_SIZE)
RESOLVED_INCIDENT_LOG: deque[Dict[str, Any]] = deque(maxlen=RESOLVED_INCIDENT_BUFFER_SIZE)
AUDIT_LOG: deque = deque(maxlen=AUDIT_BUFFER_SIZE) # Type hint AuditEntry handled in main for now or we import schema
CUSTOMER_EVENTS: deque[Dict[str, Any]] = deque(maxlen=CUSTOMER_EVENT_BUFFER_SIZE)
LATEST_CUSTOMER_EVENTS: OrderedDict[Any, Dict[str, Any]] = OrderedDict()

# Constants
INCIDENT_KIND_LABELS: Dict[str, str] = {
    "missing_fields": "Datos incompletos",
    "missing_network_keys": "Identificadores de red faltantes",
    "integration_disabled": "Integración deshabilitada",
    "automation_not_allowed": "Cliente fuera de ventana de automatización",
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

DATA_DIR = Path(os.getenv("ORCHESTRATOR_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data"))).resolve()
DB_PATH = Path(os.getenv("ORCHESTRATOR_STATE_DB", str(DATA_DIR / "state.db")))

# Config constants
CUSTOMER_EVENT_RETENTION_DAYS_CFG = int(os.getenv("CUSTOMER_EVENT_RETENTION_DAYS", "90"))
INCIDENT_RETENTION_DAYS_CFG = int(os.getenv("INCIDENT_RETENTION_DAYS", "90"))
AUDIT_RETENTION_DAYS_CFG = int(os.getenv("AUDIT_RETENTION_DAYS", "90"))
RECONCILIATION_RETENTION_DAYS_CFG = int(os.getenv("RECONCILIATION_RETENTION_DAYS", "30"))


# Metrics
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
    ["event_type", "zone", "source"],
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
