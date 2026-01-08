import math
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Set

from fastapi import HTTPException, status

from ..core.config import EnvConfig
from ..core.integration import fetch_json
from ..core.state import (
    ALLOWED_CUSTOMER_STATUS,
    AUTOMATION_MIN_START_TS,
    CORE_CUSTOMER_FIELDS,
    OPTIONAL_NETWORK_FIELDS,
    COORDINATE_FIELDS,
    ALLOW_COORDINATE_FALLBACK,
    ALLOW_MISSING_NETWORK_KEYS,
    CORDOBA_LAT_RANGE,
    CORDOBA_LON_RANGE,
    NETWORK_KEYS,
    DEFAULT_ZONE_LABEL,
    DEFAULT_COORDINATE_FALLBACK,
    connection_ctx,
)
from ..core.audit import record_incident
from ..schemas.requests import ProvisionRequest
from ..services import isp as isp_service # We need to ensure we can import this.
# Assuming orchestrator/services/isp.py exists.

logger = logging.getLogger("orchestrator.logic.domain")

def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False

def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None

def _resolve_zone_coordinates(zone: str) -> Tuple[Optional[float], Optional[float]]:
    # This was in utils.py. Check if we need to re-implement or import.
    from ..core.utils import resolve_zone_coordinates
    return resolve_zone_coordinates(zone)

def _customer_connection_code(
    customer: Dict[str, Any], fallback: Optional[str] = None
) -> Optional[str]:
    raw_code = customer.get("connection_code") or customer.get("code")
    if isinstance(raw_code, str) and raw_code.strip():
        return raw_code.strip()
    if raw_code is not None and not isinstance(raw_code, str):
        return str(raw_code)
    if fallback:
        return fallback.strip()
    return None


def _customer_connection_identifier(
    customer: Dict[str, Any], fallback: Optional[str] = None
) -> Optional[str]:
    connection_id = customer.get("connection_id")
    if connection_id is not None:
        return str(connection_id)
    return _customer_connection_code(customer, fallback)


def _connection_metadata_snapshot(
    customer: Dict[str, Any],
    fallback_code: Optional[str] = None,
    fallback_id: Optional[int] = None,
) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    override = customer.get("_connection_metadata_override") or {}
    if override:
        metadata.update({k: v for k, v in override.items() if v is not None})
        if override.get("customer_name"):
            metadata.setdefault("customer_name", override.get("customer_name"))

    hinted_id = customer.get("connection_id_hint")
    hinted_code = customer.get("connection_code_hint")
    identifier = _customer_connection_identifier(customer, fallback_code or hinted_code)
    if identifier:
        metadata["connection_id"] = identifier
    code_value = _customer_connection_code(customer, fallback_code or hinted_code)
    if code_value and code_value != identifier:
        metadata["connection_code"] = code_value
    if "connection_id" not in metadata:
        candidate = fallback_id or hinted_id
        if candidate is not None:
            metadata["connection_id"] = str(candidate)
    return metadata


def _inject_connection_context(
    customer: Dict[str, Any],
    *,
    connection_id: Optional[int] = None,
    connection_code: Optional[str] = None,
    customer_name: Optional[str] = None,
) -> None:
    override = customer.setdefault("_connection_metadata_override", {})
    if connection_id is not None and not customer.get("connection_id"):
        customer["connection_id"] = connection_id
    if connection_id is not None:
        customer.setdefault("connection_id_hint", connection_id)
        override.setdefault("connection_id", str(connection_id))
    if connection_code and not customer.get("connection_code"):
        customer["connection_code"] = connection_code
    if connection_code:
        customer.setdefault("connection_code_hint", connection_code)
        override.setdefault("connection_code", connection_code)
    if customer_name:
        override.setdefault("customer_name", customer_name)

# Dependency Injection for record_incident?
# We'll need to pass it or import it.
# To break circular dependency, we should put record_incident in a separate module or this one.
# I'll put record_incident in orchestrator/core/observability.py or logic/audit.py?
# For now, let's assume it's imported from .audit or .observability.
# Wait, I haven't created those.
# I'll create `orchestrator/logic/audit.py` to hold `record_incident` and `record_audit`.

# I will defer the implementation of `ensure_customer_ready` until I have `record_incident` available.
# Or I can define `record_incident` here.

def ensure_customer_ready(
    customer: Dict[str, Any],
    action: str,
    connection_context: Optional[Dict[str, Any]] = None,
) -> None:
    customer_id = customer.get("customer_id")
    status_value = str(customer.get("status") or "").strip().lower()
    customer_name = customer.get("name") or customer.get("customer_name")
    if not customer_name:
        nested_customer = customer.get("customer")
        if isinstance(nested_customer, dict):
            customer_name = nested_customer.get("name")
    if not customer_name:
        connection_detail = customer.get("connection")
        if isinstance(connection_detail, dict):
            nested_customer = connection_detail.get("customer")
            if isinstance(nested_customer, dict):
                customer_name = nested_customer.get("name")
    if status_value and status_value not in ALLOWED_CUSTOMER_STATUS:
        connection_id_hint = customer.get("connection_id_hint") or customer.get("connection_id")
        connection_code_hint = customer.get("connection_code_hint") or customer.get("connection_code")
        metadata = _connection_metadata_snapshot(
            customer,
            fallback_code=connection_code_hint,
            fallback_id=connection_id_hint,
        )
        if connection_context:
            if connection_context.get("connection_id") is not None:
                metadata["connection_id"] = str(connection_context["connection_id"])
            if connection_context.get("connection_code"):
                metadata["connection_code"] = connection_context["connection_code"]
        record_incident_fn(
            "automation_not_allowed",
            {
                "customer_id": customer_id,
                "customer_name": customer_name,
                "action": action,
                "reason": "status",
                "status": status_value,
                **metadata,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail={
                "message": "Customer status not eligible for automation",
                "customer_id": customer_id,
                "status": status_value,
                "reason": "status",
            },
        )

    if AUTOMATION_MIN_START_TS is not None:
        activation_ts = _customer_activation_timestamp(customer)
        if activation_ts is None or activation_ts < AUTOMATION_MIN_START_TS:
            connection_id_hint = customer.get("connection_id_hint") or customer.get("connection_id")
            connection_code_hint = customer.get("connection_code_hint") or customer.get("connection_code")
            metadata = _connection_metadata_snapshot(
                customer,
                fallback_code=connection_code_hint,
                fallback_id=connection_id_hint,
            )
            if connection_context:
                if connection_context.get("connection_id"):
                    metadata.setdefault("connection_id", str(connection_context["connection_id"]))
                if connection_context.get("connection_code"):
                    metadata.setdefault("connection_code", connection_context["connection_code"])
            record_incident(
                "automation_not_allowed",
                {
                "customer_id": customer_id,
                "customer_name": customer_name,
                "action": action,
                "reason": "cutoff",
                "activation_ts": activation_ts.isoformat() if activation_ts else None,
                "cutoff": AUTOMATION_MIN_START_TS.isoformat(),
                **metadata,
            },
            )
            raise HTTPException(
                status_code=status.HTTP_412_PRECONDITION_FAILED,
                detail={
                    "message": "Customer created before automation cutoff",
                    "customer_id": customer_id,
                    "activation_ts": activation_ts.isoformat() if activation_ts else None,
                    "cutoff": AUTOMATION_MIN_START_TS.isoformat(),
                    "reason": "cutoff",
                },
            )

    core_missing = [
        field for field in CORE_CUSTOMER_FIELDS if _is_blank(customer.get(field))
    ]
    if core_missing:
        connection_id_hint = customer.get("connection_id_hint") or customer.get("connection_id")
        connection_code_hint = customer.get("connection_code_hint") or customer.get("connection_code")
        metadata = _connection_metadata_snapshot(
            customer,
            fallback_code=connection_code_hint,
            fallback_id=connection_id_hint,
        )
        if connection_context:
            if connection_context.get("connection_id"):
                metadata.setdefault("connection_id", str(connection_context["connection_id"]))
            if connection_context.get("connection_code"):
                metadata.setdefault("connection_code", connection_context["connection_code"])
        if connection_id_hint is not None:
            metadata.setdefault("connection_id", str(connection_id_hint))
        if connection_code_hint:
            metadata.setdefault("connection_code", connection_code_hint)
        record_incident(
            "missing_fields",
            {
                "customer_id": customer_id,
                "customer_name": customer_name,
                "action": action,
                "missing": core_missing,
                **metadata,
            },
        )
        blocking_missing = [
            field
            for field in core_missing
            if field not in COORDINATE_FIELDS or not ALLOW_COORDINATE_FALLBACK
        ]
        if blocking_missing:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "message": "Customer has incomplete network metadata",
                    "customer_id": customer_id,
                    "missing_fields": blocking_missing,
                },
            )
    network_missing = [
        field
        for field in OPTIONAL_NETWORK_FIELDS
        if _is_blank(customer.get(field))
    ]
    if network_missing:
        connection_id_hint = customer.get("connection_id_hint") or customer.get("connection_id")
        connection_code_hint = customer.get("connection_code_hint") or customer.get("connection_code")
        metadata = _connection_metadata_snapshot(
            customer,
            fallback_code=connection_code_hint,
            fallback_id=connection_id_hint,
        )
        record_incident(
            "missing_network_keys",
            {
                "customer_id": customer_id,
                "customer_name": customer_name,
                "action": action,
                "missing": network_missing,
                **metadata,
            },
        )
        if not ALLOW_MISSING_NETWORK_KEYS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "message": "Customer has incomplete network metadata",
                    "customer_id": customer_id,
                    "missing_fields": network_missing,
                },
            )

    lat = None
    lon = None
    lat_raw = customer.get("lat")
    lon_raw = customer.get("lon")
    if lon_raw is None:
        lon_raw = customer.get("lng")
    coord_missing = lat_raw in (None, "", []) or lon_raw in (None, "", [])

    if not (ALLOW_COORDINATE_FALLBACK and coord_missing):
        try:
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

    if lat is not None and lon is not None:
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
        record_incident_fn(
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

# ... Helper functions for _build_geogrid_cliente_payload, etc.
# Need to copy them too.

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

def _customer_coordinates(customer: Dict[str, Any]) -> Tuple[float, float]:
    lat_value = _safe_float(customer.get("lat"))
    lon_source = customer.get("lon")
    if lon_source is None:
        lon_source = customer.get("lng")
    lon_value = _safe_float(lon_source)

    if lat_value is None or lon_value is None:
        zone_name = _customer_zone(customer) or DEFAULT_ZONE_LABEL
        fallback_lat, fallback_lon = _resolve_zone_coordinates(zone_name)
        if lat_value is None:
            lat_value = fallback_lat if fallback_lat is not None else DEFAULT_COORDINATE_FALLBACK[0]
        if lon_value is None:
            lon_value = fallback_lon if fallback_lon is not None else DEFAULT_COORDINATE_FALLBACK[1]

    if lat_value is None or lon_value is None:
        raise ValueError("Unable to derive customer coordinates")
    return lat_value, lon_value

def _customer_coordinates_strict(customer: Dict[str, Any]) -> Tuple[float, float]:
    lat_value = _safe_float(customer.get("lat"))
    lon_source = customer.get("lon")
    if lon_source is None:
        lon_source = customer.get("lng")
    lon_value = _safe_float(lon_source)
    if lat_value is None or lon_value is None:
        raise ValueError("Missing customer coordinates")
    return lat_value, lon_value

def _resolve_plan_name(customer: Dict[str, Any]) -> Optional[str]:
    raw_plan_name = customer.get("plan_name")
    if isinstance(raw_plan_name, str) and raw_plan_name.strip():
        return raw_plan_name.strip()
    plan_info = customer.get("plan")
    if isinstance(plan_info, dict):
        plan_name = plan_info.get("name")
        if isinstance(plan_name, str) and plan_name.strip():
            return plan_name.strip()
    return None

def build_geogrid_cliente_payload(customer: Dict[str, Any]) -> Dict[str, Any]:
    codigo = _customer_connection_identifier(customer) or str(
        customer.get("code") or customer.get("customer_id") or ""
    ).strip()
    if not codigo:
        codigo = f"customer-{customer.get('customer_id', 'unknown')}"
    customer_name = str(customer.get("name") or codigo)
    display_name = f"{codigo} - {customer_name}"
    observaciones: List[str] = []
    zone = _customer_zone(customer)
    if zone:
        observaciones.append(f"Zona: {zone}")
    plan_name = _resolve_plan_name(customer)
    if plan_name:
        observaciones.append(f"Plan: {plan_name}")
    ftth_box = customer.get("ftthbox_id")
    if ftth_box:
        observaciones.append(f"Caja: {ftth_box}")
    ftth_port = customer.get("ftth_port_id")
    if ftth_port:
        observaciones.append(f"Puerto: {ftth_port}")
    if (
        customer.get("olt_id") is not None
        and customer.get("board") is not None
        and customer.get("pon") is not None
    ):
        observaciones.append(
            f"OLT/B/P: {customer.get('olt_id')}-{customer.get('board')}-{customer.get('pon')}"
        )
    elif customer.get("olt_id") is not None:
        observaciones.append(f"OLT: {customer.get('olt_id')}")
    if customer.get("onu_sn"):
        observaciones.append(f"ONU: {customer.get('onu_sn')}")
    if customer.get("user"):
        observaciones.append(f"PPPoE: {customer.get('user')}")
    observacion = " | ".join(observaciones) if observaciones else None
    doc_raw = str(customer.get("doc_number") or "").strip()
    doc_value = "".join(ch for ch in doc_raw if ch.isdigit())
    if not doc_value:
        doc_value = "00000000000"
    phone_raw = str(customer.get("extra1") or customer.get("phone") or "").strip()
    phone_value = "".join(ch for ch in phone_raw if ch.isdigit())
    if not phone_value:
        phone_value = "0000000000"
    bairro_value = zone or ""
    cep_value = str(customer.get("postal_code") or customer.get("cep") or "").strip()
    if cep_value is None:
        cep_value = ""
    return {
        "codigoIntegracao": codigo,
        "tipo": "F",
        "nome": display_name,
        "cpfCnpj": doc_value,
        "telefone": phone_value,
        "cep": cep_value,
        "endereco": customer.get("address") or "",
        "bairro": bairro_value,
        "cidade": _customer_city(customer) or zone or "",
        "estado": "RS",
        "observacao": observacion or "",
    }


def extract_geogrid_box_sigla(customer: Dict[str, Any]) -> Optional[str]:
    sigla = customer.get("ftthbox_name") or customer.get("ftth_box_name")
    if not sigla:
        connection = customer.get("connection")
        if isinstance(connection, dict):
            ftthbox = connection.get("ftthbox")
            if isinstance(ftthbox, dict):
                sigla = ftthbox.get("name")
    if isinstance(sigla, str):
        sigla = sigla.strip()
    return sigla or None


def extract_geogrid_port_number(customer: Dict[str, Any]) -> Optional[int]:
    porta_num = customer.get("ftth_port_nro") or customer.get("ftth_port_number")
    if porta_num is None:
        connection = customer.get("connection")
        if isinstance(connection, dict):
            ftth_port = connection.get("ftth_port")
            if isinstance(ftth_port, dict):
                porta_num = ftth_port.get("nro")
    try:
        return int(porta_num) if porta_num is not None else None
    except (TypeError, ValueError):
        return None

def _parse_customer_timestamp_value(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    from ..core.utils import parse_iso8601
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    try:
        ts = parse_iso8601(str(value))
        if ts.tzinfo is None:
             return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    except Exception:
        return None


def _customer_activation_timestamp(customer: Dict[str, Any]) -> Optional[datetime]:
    connection = customer.get("connection") if isinstance(customer.get("connection"), dict) else {}
    candidates = [
        connection.get("start_date"),
        customer.get("start_date"),
        connection.get("created_at"),
        customer.get("created_at"),
        connection.get("updated_at"),
        customer.get("updated_at"),
    ]
    for candidate in candidates:
        parsed = _parse_customer_timestamp_value(candidate)
        if parsed is not None:
            return parsed
    return None

def ensure_customer_inactive(customer: Dict[str, Any]) -> None:
    if customer.get("status") == "inactive":
        return
    record_incident_fn(
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

def _sanitize_connection_candidates(raw_connections: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_connections, list):
        return []
    sanitized: List[Dict[str, Any]] = []
    for entry in raw_connections:
        if isinstance(entry, dict):
            sanitized.append(dict(entry))
    return sanitized

def _merge_connection_candidates(
    existing: List[Dict[str, Any]],
    extras: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not existing:
        return [dict(entry) for entry in extras if isinstance(entry, dict)]
    merged = list(existing)
    known_ids = {entry.get("id") for entry in existing if entry.get("id") is not None}
    for entry in extras:
        if not isinstance(entry, dict):
            continue
        conn_id = entry.get("id")
        if conn_id is not None and conn_id in known_ids:
            continue
        merged.append(dict(entry))
    return merged

def _normalize_connection_token(value: Optional[Any]) -> Optional[str]:
    if value is None:
        return None
    token = str(value).strip()
    return token or None

def _select_connection_candidate(
    candidates: List[Dict[str, Any]],
    requested_connection_id: Optional[int],
    requested_connection_code: Optional[str],
) -> Optional[Dict[str, Any]]:
    if not candidates:
        return None
    if requested_connection_id is not None:
        for entry in candidates:
            conn_id = entry.get("id")
            try:
                if conn_id is not None and int(conn_id) == requested_connection_id:
                    return entry
            except (TypeError, ValueError):
                continue
    code_token = _normalize_connection_token(requested_connection_code)
    if code_token:
        for entry in candidates:
            for key in ("connection_code", "oldcode", "code", "id"):
                value = entry.get(key)
                if _normalize_connection_token(value) == code_token:
                    return entry
    if len(candidates) == 1:
        return candidates[0]
    return None

def _propagate_connection_fields(customer: Dict[str, Any], connection: Dict[str, Any]) -> None:
    for field in ("lat", "lng", "address"):
        if _is_blank(customer.get(field)) and not _is_blank(connection.get(field)):
            customer[field] = connection.get(field)
    for field in (
        "plan_id",
        "node_id",
        "ftthbox_id",
        "ftth_port_id",
        "user",
        "password",
        "ip",
        "ipv6",
        "local_ip",
    ):
        if customer.get(field) in (None, "", []):
            value = connection.get(field)
            if value not in (None, "", []):
                customer[field] = value

    plan_info = connection.get("plan")
    if isinstance(plan_info, dict):
        customer.setdefault("plan", dict(plan_info))
        plan_name = plan_info.get("name")
        if plan_name and _is_blank(customer.get("plan_name")):
            customer["plan_name"] = plan_name
    node_info = connection.get("node")
    if isinstance(node_info, dict):
        customer.setdefault("node", dict(node_info))
    customer_summary = connection.get("customer")
    if isinstance(customer_summary, dict):
        customer.setdefault("customer_summary", dict(customer_summary))
    ftthbox = connection.get("ftthbox")
    if isinstance(ftthbox, dict):
        box_name = ftthbox.get("name")
        if box_name and _is_blank(customer.get("ftthbox_name")):
            customer["ftthbox_name"] = box_name
    ftth_port = connection.get("ftth_port")
    if isinstance(ftth_port, dict):
        port_nro = ftth_port.get("nro")
        if port_nro is not None and _is_blank(customer.get("ftth_port_nro")):
            customer["ftth_port_nro"] = port_nro

async def maybe_fetch_connections_for_customer(
    settings: EnvConfig,
    customer: Dict[str, Any],
) -> List[Dict[str, Any]]:
    customer_id = customer.get("customer_id")
    try:
        customer_id_int = int(customer_id) if customer_id is not None else None
    except (TypeError, ValueError):
        customer_id_int = None
    if not customer_id_int:
        return []
    return await isp_service.list_customer_connections(
        settings,
        customer_id=customer_id_int,
        fetch_json=fetch_json,
    )

async def _base_customer_lookup(
    settings: EnvConfig,
    *,
    customer_id: Optional[int],
    connection_code: Optional[str],
) -> Dict[str, Any]:
    if customer_id is not None:
        return await isp_service.get_customer(settings, customer_id, fetch_json)
    if connection_code:
        return await isp_service.get_customer_by_code(
            settings, connection_code, fetch_json
        )
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"message": "Debe especificar customer_id, connection_id o connection_code"},
    )

async def _ensure_connection_metadata(
    settings: EnvConfig,
    customer: Dict[str, Any],
    *,
    connection_payload: Optional[Dict[str, Any]],
    requested_connection_id: Optional[int],
    requested_connection_code: Optional[str],
) -> Dict[str, Any]:
    merged = dict(customer)
    if "customer_id" not in merged and merged.get("id") is not None:
        merged["customer_id"] = merged.get("id")
    connections = _sanitize_connection_candidates(merged.get("connections"))
    if connections:
        merged["connections"] = connections
    resolved_connection = dict(connection_payload) if isinstance(connection_payload, dict) else None

    if resolved_connection is None:
        resolved_connection = _select_connection_candidate(
            connections,
            requested_connection_id,
            requested_connection_code,
        )

    if resolved_connection is None:
        extra_connections = await maybe_fetch_connections_for_customer(
            settings, merged
        )
        if extra_connections:
            connections = _merge_connection_candidates(connections, extra_connections)
            merged["connections"] = connections
            resolved_connection = _select_connection_candidate(
                connections,
                requested_connection_id,
                requested_connection_code,
            )
            if resolved_connection is None and len(connections) == 1:
                resolved_connection = connections[0]

    if resolved_connection is None:
        if requested_connection_id is not None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "message": "La conexión indicada no pertenece al cliente o no existe",
                    "connection_id": requested_connection_id,
                    "customer_id": merged.get("customer_id"),
                },
            )
        if len(connections) > 1:
            raise HTTPException(
                status_code=status.HTTP_412_PRECONDITION_FAILED,
                detail={
                    "message": "El cliente tiene múltiples conexiones; especifique connection_id",
                    "customer_id": merged.get("customer_id"),
                    "available_connections": [
                        conn.get("id") for conn in connections if conn.get("id") is not None
                    ],
                },
            )
        return merged

    merged["connection"] = dict(resolved_connection)
    if resolved_connection.get("id") is not None:
        merged["connection_id"] = resolved_connection["id"]
    connection_code = (
        resolved_connection.get("connection_code")
        or resolved_connection.get("oldcode")
        or resolved_connection.get("code")
    )
    if connection_code and not merged.get("connection_code"):
        merged["connection_code"] = connection_code

    _propagate_connection_fields(merged, resolved_connection)
    return merged

def resolve_customer_id(customer: Dict[str, Any], fallback: Optional[int] = None) -> Optional[int]:
    candidate = customer.get("customer_id") or customer.get("id") or fallback
    try:
        return int(candidate) if candidate is not None else None
    except (TypeError, ValueError):
        return fallback

async def fetch_customer_record(
    settings: EnvConfig,
    *,
    customer_id: Optional[int],
    connection_code: Optional[str],
    connection_id: Optional[int],
) -> Dict[str, Any]:
    resolved_customer_id = customer_id
    resolved_code = connection_code
    connection_payload: Optional[Dict[str, Any]] = None

    if connection_id is not None:
        connection_payload = await isp_service.get_connection_by_id(
            settings,
            connection_id=connection_id,
            fetch_json=fetch_json,
        )
        if connection_payload is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "message": "Conexión no encontrada en ISP-Cube",
                    "connection_id": connection_id,
                },
            )
        conn_customer_id = connection_payload.get("customer_id")
        if conn_customer_id is not None:
            try:
                resolved_customer_id = int(conn_customer_id)
            except (TypeError, ValueError):
                resolved_customer_id = conn_customer_id
        elif resolved_customer_id is None:
            nested_customer = connection_payload.get("customer")
            if isinstance(nested_customer, dict):
                resolved_code = resolved_code or nested_customer.get("code")
        if not resolved_code and connection_payload.get("connection_code"):
            resolved_code = connection_payload.get("connection_code")

    base_customer = await _base_customer_lookup(
        settings,
        customer_id=resolved_customer_id,
        connection_code=resolved_code,
    )
    enriched_customer = await _ensure_connection_metadata(
        settings,
        base_customer,
        connection_payload=connection_payload,
        requested_connection_id=connection_id,
        requested_connection_code=connection_code,
    )
    return enriched_customer
