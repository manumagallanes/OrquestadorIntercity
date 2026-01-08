import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .state import (
    AUDIT_BUFFER_SIZE,
    AUDIT_LOG,
    INCIDENT_BUFFER_SIZE,
    INCIDENT_GAUGE,
    INCIDENT_LOG,
    RESOLVED_INCIDENT_BUFFER_SIZE,
    RESOLVED_INCIDENT_LOG,
    RuntimeState,
)
from ..schemas.requests import AuditEntry

logger = logging.getLogger("orchestrator.core.audit")

def record_incident(
    kind: str, details: Dict[str, Any], timestamp: Optional[float] = None
) -> None:
    ts = timestamp or time.time()
    iso_ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    incident = {
        "kind": kind,
        "detected_at": iso_ts,
        "timestamp": iso_ts,
        **details,
    }
    # Deduplicate via key (kind + customer_id) if present
    customer_id = details.get("customer_id")
    if customer_id:
        existing = next(
            (
                i
                for i in INCIDENT_LOG
                if i.get("kind") == kind and i.get("customer_id") == customer_id
            ),
            None,
        )
        if existing:
            existing.update(incident)  # refresh timestamp/details
            return

    INCIDENT_LOG.appendleft(incident)
    if len(INCIDENT_LOG) > INCIDENT_BUFFER_SIZE:
        INCIDENT_LOG.pop()
    INCIDENT_GAUGE.set(len(INCIDENT_LOG))
    logger.warning("Incident recorded: %s - %s", kind, details)


def resolve_incidents(
    customer_id: Optional[int], action: str, resolved_by: str, reason: str
) -> None:
    if not customer_id:
        return
    to_resolve = [
        i for i in INCIDENT_LOG if i.get("customer_id") == customer_id
    ]
    if not to_resolve:
        return
    
    timestamp = datetime.now(timezone.utc).isoformat()
    for incident in to_resolve:
        try:
            INCIDENT_LOG.remove(incident)
        except ValueError:
            continue  # race condition handling
        
        resolved_record = {
            "incident": incident,
            "resolution": {
                "action": action,
                "resolved_by": resolved_by,
                "reason": reason,
                "timestamp": timestamp,
            },
        }
        RESOLVED_INCIDENT_LOG.appendleft(resolved_record)
    
    # Trim resolved log
    while len(RESOLVED_INCIDENT_LOG) > RESOLVED_INCIDENT_BUFFER_SIZE:
        RESOLVED_INCIDENT_LOG.pop()
    
    INCIDENT_GAUGE.set(len(INCIDENT_LOG))
    logger.info("Resolved %d incidents for customer %s", len(to_resolve), customer_id)


def record_audit(entry: AuditEntry) -> None:
    AUDIT_LOG.appendleft(entry)
    while len(AUDIT_LOG) > AUDIT_BUFFER_SIZE:
        AUDIT_LOG.pop()
    logger.info("Audit: %s %s - %s", entry.action, entry.customer_id, entry.status)
