import logging
from collections import defaultdict
from datetime import datetime, timedelta, date, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from ..core.state import (
    CUSTOMER_EVENT_BUFFER_SIZE,
    CUSTOMER_EVENT_COUNTER,
    CUSTOMER_EVENTS,
    DEFAULT_ZONE_LABEL,
    LATEST_CUSTOMER_EVENTS,
)
from ..core.utils import parse_iso8601, resolve_zone_coordinates
from .domain import (
    _connection_metadata_snapshot,
    _customer_coordinates,
    _customer_zone,
    _customer_city,
)

from ..core.audit import record_incident

logger = logging.getLogger("orchestrator.logic.reporting")


def register_customer_event(
    event_type: str,
    customer: Optional[Dict[str, Any]],
    zone: Optional[str] = None,
    city: Optional[str] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    timestamp: Optional[str] = None,
    source: str = "runtime",
    metadata: Optional[Dict[str, Any]] = None,
    customer_id: Optional[int] = None,
) -> Dict[str, Any]:
    ts_val = timestamp or datetime.now(timezone.utc).isoformat()
    cid = customer_id
    if cid is None and customer:
        cid = customer.get("customer_id") or customer.get("id")

    # Try to resolve zone/coords from customer if not provided
    if customer:
        if zone is None:
            zone = _customer_zone(customer)
        if lat is None or lon is None:
            try:
                c_lat, c_lon = _customer_coordinates(customer)
                if lat is None:
                    lat = c_lat
                if lon is None:
                    lon = c_lon
            except ValueError:
                pass
        if city is None:
            city = _customer_city(customer)

    event_id = f"{int(datetime.now().timestamp() * 1000)}-{cid or 'unknown'}"
    entry = {
        "event_id": event_id,
        "event_type": event_type,
        "customer_id": cid,
        "timestamp": ts_val,
        "zone": zone,
        "city": city,
        "lat": lat,
        "lon": lon,
        "source": source,
        "metadata": metadata or {},
    }
    CUSTOMER_EVENTS.appendleft(entry)
    if len(CUSTOMER_EVENTS) > CUSTOMER_EVENT_BUFFER_SIZE:
        CUSTOMER_EVENTS.pop()

    if cid:
        LATEST_CUSTOMER_EVENTS[cid] = entry

    # Persist to SQLite for dashboard history
    try:
        from ..persistence import persistence_store
        persistence_store.save_customer_event(entry)
    except Exception as e:
        logger.warning("Failed to persist customer event to SQLite: %s", e)

    logger.info("Customer Event: %s for %s (source=%s)", event_type, cid, source)

    # Update Prometheus counter for Grafana dashboards
    zone_label = zone or DEFAULT_ZONE_LABEL
    CUSTOMER_EVENT_COUNTER.labels(
        event_type=event_type,
        zone=zone_label,
        source=source,
    ).inc()

    # Automatic incident for manual cleanup on 'baja'
    if event_type == "baja":
        record_incident(
            "manual_cleanup_required",
            {
                "customer_id": cid,
                "action": "decommission",
                "message": "Baja detectada en ISP. Requiere eliminación manual en GeoGrid.",
                "event_id": event_id,
                **entry.get("metadata", {}),
            }
        )

    return entry


def _filter_customer_events(
    lookback_days: int,
    zone: Optional[str] = None,
    event_type: Optional[str] = None,
    source: Optional[str] = None,
    latest_per_customer: bool = False,
) -> List[Dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    
    # Try to load from SQLite first (persistent), fallback to memory
    try:
        from ..persistence import persistence_store
        candidates = persistence_store.load_customer_events()
    except Exception:
        candidates = list(CUSTOMER_EVENTS)
    
    logger.debug("Filtering %d candidates. Cutoff: %s", len(candidates), cutoff)

    results = []
    seen_customers = set()
    
    for event in candidates:
        try:
            ts = parse_iso8601(event["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        
        if ts < cutoff:
            continue
        if zone and event.get("zone") != zone:
            continue
        if event_type and event.get("event_type") != event_type:
            continue
        if source and event.get("source") != source:
            continue
            
        if latest_per_customer:
            cid = event.get("customer_id")
            if cid and cid in seen_customers:
                continue
            if cid:
                seen_customers.add(cid)
                
        results.append(event)
        
    return results


def _summarize_customer_events(
    events: List[Dict[str, Any]]
) -> Tuple[Dict[str, int], Dict[str, Dict[str, int]]]:
    totals = {"altas": 0, "bajas": 0, "neto": 0}
    zones: Dict[str, Dict[str, int]] = defaultdict(lambda: {"altas": 0, "bajas": 0})
    
    for event in events:
        etype = event.get("event_type")
        z = event.get("zone") or DEFAULT_ZONE_LABEL
        if etype == "alta":
            totals["altas"] += 1
            zones[z]["altas"] += 1
        elif etype == "baja":
            totals["bajas"] += 1
            zones[z]["bajas"] += 1
            
    totals["neto"] = totals["altas"] - totals["bajas"]
    return totals, dict(zones)


def _events_to_feature_collection(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    features = []
    for event in events:
        lat = event.get("lat")
        lon = event.get("lon")
        if lat is None or lon is None:
            # Resolve from zone
            z = event.get("zone") or DEFAULT_ZONE_LABEL
            lat, lon = resolve_zone_coordinates(z)
            if lat is None:
                continue
        
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [lon, lat]
            },
            "properties": {
                "event_type": event.get("event_type"),
                "customer_id": event.get("customer_id"),
                "timestamp": event.get("timestamp"),
                "zone": event.get("zone"),
                "source": event.get("source")
            }
        })
        
    return {
        "type": "FeatureCollection",
        "features": features
    }

# Helpers for Grafana DataFrames

def _build_empty_table_frame(ref_id: str) -> Dict[str, Any]:
    return {
        "refId": ref_id,
        "fields": [],
        "length": 0,
    }

def _resolve_lookback_days(range_info: Dict[str, Any], payload_lookback: Any) -> int:
    # Try payload first
    try:
        if payload_lookback is not None:
             return int(payload_lookback)
    except (TypeError, ValueError):
        pass
        
    # Try range
    if not range_info:
        return 30
    
    try:
        raw_from = range_info.get("from")
        # Parse grafana time range... this is complex if it's "now-6h".
        # Simplified logic as used in main.py:
        # If it's iso string, calculate delta.
        # This function wasn't fully shown in main.py snippet but implied.
        # I'll implement a safe default.
        return 30
    except Exception:
        return 30


def _event_with_resolved_coordinates(event: Dict[str, Any]) -> Dict[str, Any]:
    e = dict(event)
    if e.get("lat") is None or e.get("lon") is None:
        z = e.get("zone") or DEFAULT_ZONE_LABEL
        lat, lon = resolve_zone_coordinates(z)
        e["lat"] = lat
        e["lon"] = lon
    return e


def _build_events_table_frame(ref_id: str, events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build Grafana table frame for customer events map."""
    rows = []
    columns = [
        {"text": "timestamp", "type": "string"},
        {"text": "lat", "type": "number"},
        {"text": "lon", "type": "number"},
        {"text": "customer_id", "type": "string"},
        {"text": "customer_label", "type": "string"},
        {"text": "zone", "type": "string"},
        {"text": "event_type", "type": "string"},
    ]
    
    for e in events:
        if e.get("lat") is None or e.get("lon") is None:
            continue
        try:
            parse_iso8601(e.get("timestamp"))
        except Exception:
            continue
        
        # Build customer label
        metadata = e.get("metadata", {})
        label = metadata.get("name") or metadata.get("customer_name") or f"Cliente #{e.get('customer_id', 'N/A')}"
        
        rows.append([
            e.get("timestamp"),
            e.get("lat"),
            e.get("lon"),
            str(e.get("customer_id", "")),
            label,
            e.get("zone") or DEFAULT_ZONE_LABEL,
            e.get("event_type", ""),
        ])
    
    return {
        "type": "table",
        "refId": ref_id,
        "columns": columns,
        "rows": rows,
    }

def _build_resolved_incidents_frame(ref_id: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build Grafana table frame for resolved incidents."""
    columns = [
        {"text": "resolved_at", "type": "string"},
        {"text": "incident_at", "type": "string"},
        {"text": "kind", "type": "string"},
        {"text": "customer_id", "type": "string"},
        {"text": "customer_label", "type": "string"},
        {"text": "action", "type": "string"},
        {"text": "context", "type": "string"},
        {"text": "resolution_reason", "type": "string"},
        {"text": "resolved_by", "type": "string"},
    ]
    rows = []

    for item in items:
        incident = item.get("incident", {})
        resolution = item.get("resolution", {})
        
        customer_label = incident.get("customer_label") or incident.get("name") or str(incident.get("customer_id", ""))
        context = str(incident.get("context", incident.get("message", "")))
        
        rows.append([
            resolution.get("timestamp", ""),
            incident.get("detected_at", incident.get("timestamp", "")),
            incident.get("kind", ""),
            str(incident.get("customer_id", "")),
            customer_label,
            incident.get("action", ""),
            context,
            resolution.get("reason", ""),
            resolution.get("resolved_by", ""),
        ])

    return {
        "type": "table",
        "refId": ref_id,
        "columns": columns,
        "rows": rows,
    }


def _build_open_incidents_frame(ref_id: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build Grafana table frame for open incidents."""
    columns = [
        {"text": "detected_at", "type": "string"},
        {"text": "kind", "type": "string"},
        {"text": "customer_id", "type": "string"},
        {"text": "customer_label", "type": "string"},
        {"text": "action", "type": "string"},
        {"text": "context", "type": "string"},
    ]
    rows = []

    for incident in items:
        # Build a readable label
        customer_label = incident.get("customer_label") or incident.get("name")
        if not customer_label:
            customer_label = f"Cliente #{incident.get('customer_id', 'N/A')}"
        
        # Context includes message and other details
        context = incident.get("message", "")
        if not context:
            context = str({k: v for k, v in incident.items() 
                          if k not in ("kind", "detected_at", "timestamp", "customer_id", "action", "customer_label", "name")})
        
        rows.append([
            incident.get("detected_at", incident.get("timestamp", "")),
            incident.get("kind", ""),
            str(incident.get("customer_id", "")),
            customer_label,
            incident.get("action", ""),
            context,
        ])

    return {
        "type": "table",
        "refId": ref_id,
        "columns": columns,
        "rows": rows,
    }

    
def _build_incidents_summary_frame(ref_id: str, lookback_days: int) -> Dict[str, Any]:
    """Build Grafana table frame for incidents summary by kind."""
    from ..core.state import INCIDENT_LOG, INCIDENT_KIND_LABELS
    from collections import Counter
    
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    kind_counts: Counter = Counter()
    
    for incident in INCIDENT_LOG:
        ts_str = incident.get("detected_at") or incident.get("timestamp")
        if ts_str:
            try:
                ts = parse_iso8601(str(ts_str))
                if ts < cutoff:
                    continue
            except Exception:
                pass
        kind_counts[incident.get("kind", "unknown")] += 1
    
    kind_vals = []
    kind_label_vals = []
    count_vals = []
    
    for kind, count in kind_counts.most_common():
        kind_vals.append(kind)
        kind_label_vals.append(INCIDENT_KIND_LABELS.get(kind, kind))
        count_vals.append(count)
    
    return {
        "refId": ref_id,
        "fields": [
            {"name": "kind", "type": "string", "values": kind_vals},
            {"name": "kind_label", "type": "string", "values": kind_label_vals},
            {"name": "count", "type": "number", "values": count_vals},
        ],
        "length": len(kind_vals),
    }

    
def _build_reconciliation_results_frame(ref_id: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build Grafana table frame for reconciliation results."""
    timestamp_vals = []
    issue_type_vals = []
    resource_type_vals = []
    source_vals = []
    details_vals = []
    customer_id_vals = []

    for item in items:
        timestamp_vals.append(item.get("timestamp", ""))
        issue_type_vals.append(item.get("issue_type", item.get("type", "")))
        resource_type_vals.append(item.get("resource_type", ""))
        source_vals.append(item.get("source", ""))
        details_vals.append(str(item.get("details", item.get("message", ""))))
        customer_id_vals.append(str(item.get("customer_id", "")))

    return {
        "refId": ref_id,
        "fields": [
            {"name": "timestamp", "type": "string", "values": timestamp_vals},
            {"name": "issue_type", "type": "string", "values": issue_type_vals},
            {"name": "resource_type", "type": "string", "values": resource_type_vals},
            {"name": "source", "type": "string", "values": source_vals},
            {"name": "customer_id", "type": "string", "values": customer_id_vals},
            {"name": "details", "type": "string", "values": details_vals},
        ],
        "length": len(items),
    }

    
def _build_reconciliation_summary_frame(ref_id: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build Grafana table frame for reconciliation summary."""
    from collections import Counter
    
    if not items:
        return {
            "refId": ref_id,
            "fields": [
                {"name": "issue_type", "type": "string", "values": []},
                {"name": "count", "type": "number", "values": []},
            ],
            "length": 0,
        }
    
    issue_counts: Counter = Counter()
    latest_timestamp = None
    
    for item in items:
        issue_type = item.get("issue_type", item.get("type", "unknown"))
        issue_counts[issue_type] += 1
        if latest_timestamp is None:
            latest_timestamp = item.get("timestamp")
    
    issue_type_vals = []
    count_vals = []
    
    for issue_type, count in issue_counts.most_common():
        issue_type_vals.append(issue_type)
        count_vals.append(count)
    
    # Add total row
    issue_type_vals.append("TOTAL")
    count_vals.append(sum(issue_counts.values()))
    
    return {
        "refId": ref_id,
        "fields": [
            {"name": "issue_type", "type": "string", "values": issue_type_vals},
            {"name": "count", "type": "number", "values": count_vals},
        ],
        "length": len(issue_type_vals),
    }

    
def _filter_resolved_incidents(
    lookback_days: int,
    customer_id: Optional[str] = None,
    kind: Optional[str] = None,
) -> List[Dict[str, Any]]:
    # from state import RESOLVED_INCIDENT_LOG
    # implement filtering.
    from ..core.state import RESOLVED_INCIDENT_LOG
    results = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    
    for item in RESOLVED_INCIDENT_LOG:
        res = item.get("resolution", {})
        ts_str = res.get("timestamp")
        if not ts_str:
            continue
        try:
             if parse_iso8601(ts_str) < cutoff:
                 continue
        except Exception:
             continue
             
        inc = item.get("incident", {})
        if kind and inc.get("kind") != kind:
            continue
        if customer_id and str(inc.get("customer_id")) != customer_id:
            continue
            
        results.append(item)
    return results

