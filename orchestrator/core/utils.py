from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .state import (
    CUSTOMER_EVENTS,
    INCIDENT_LOG,
    RESOLVED_INCIDENT_LOG,
    ZONE_BASE_COORDINATES,
)

def safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def resolve_zone_coordinates(zone: str) -> Tuple[Optional[float], Optional[float]]:
    coords = ZONE_BASE_COORDINATES.get(zone)
    if coords:
        return coords
    return (None, None)


def normalize_customer_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return str(value)


def lookup_customer_name(customer_id: Any) -> Optional[str]:
    normalized = normalize_customer_id(customer_id)
    if not normalized:
        return None
    for event in reversed(CUSTOMER_EVENTS):
        if normalize_customer_id(event.get("customer_id")) == normalized:
            name = event.get("customer_name") or (event.get("metadata") or {}).get("customer_name")
            if name:
                return name
    for entry in reversed(RESOLVED_INCIDENT_LOG):
        if normalize_customer_id(entry.get("customer_id")) == normalized:
            name = entry.get("customer_name")
            if name:
                return name
    for entry in reversed(INCIDENT_LOG):
        if normalize_customer_id(entry.get("customer_id")) == normalized:
            name = entry.get("customer_name")
            if name:
                return name
    return None

def parse_iso8601(value: str) -> datetime:
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return datetime.now(timezone.utc)
