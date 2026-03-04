import logging
import json
from collections import defaultdict
from datetime import datetime, date, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple

from fastapi import APIRouter, Depends, Query, Request, status, HTTPException
import httpx

from orchestrator.core.config import EnvConfig, get_settings
from orchestrator.core.state import (
    CUSTOMER_EVENT_BUFFER_SIZE,
    INCIDENT_LOG,
    AUDIT_LOG,
    DEFAULT_ZONE_LABEL,
)
from orchestrator.core.audit import record_incident, resolve_incidents, record_audit
from orchestrator.core.integration import fetch_json
from orchestrator.core.utils import parse_iso8601, resolve_zone_coordinates
from orchestrator.persistence import persistence_store
from orchestrator.logic.reporting import (
    register_customer_event,
    _filter_customer_events,
    _summarize_customer_events,
    _events_to_feature_collection,
    _resolve_lookback_days,
    _event_with_resolved_coordinates,
    _build_events_table_frame,
    _build_resolved_incidents_frame,
    _build_open_incidents_frame,
    _build_incidents_summary_frame,
    _build_reconciliation_results_frame,
    _build_reconciliation_summary_frame,
    _build_empty_table_frame,
    _filter_resolved_incidents,
)
from orchestrator.logic.domain import _connection_metadata_snapshot
from orchestrator.schemas.requests import CustomerEventRequest, AuditEntry

logger = logging.getLogger("orchestrator.api.analytics")
router = APIRouter()

from orchestrator.logic import reconciliation

@router.post(
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
        # Intenta obtener datos adicionales del cliente desde ISP-Cube (opcional)
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
        except Exception as e:
            # Si falla la consulta a ISP, continuamos sin los datos adicionales
            logger.debug("No se pudo obtener datos del cliente %s desde ISP: %s", payload.customer_id, e)

    event_metadata = dict(payload.metadata)
    if payload.source and "origin" not in event_metadata:
        event_metadata["origin"] = payload.source
    if customer:
        connection_meta = _connection_metadata_snapshot(customer)
        for key, value in connection_meta.items():
            event_metadata.setdefault(key, value)

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


@router.get(
    "/analytics/customer-events",
    summary="Eventos de altas y bajas georreferenciados",
)
async def get_customer_events(
    lookback_days: int = Query(
        default=365,
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
    source: Optional[str] = Query(
        default=None,
        description="Filtra por origen del evento.",
    ),
) -> List[Dict[str, Any]]:
    events = _filter_customer_events(
        lookback_days,
        zone=zone,
        event_type=event_type,
        source=source,
    )
    return events[:limit]

@router.get(
    "/analytics/customer-events/summary",
    summary="Totales de altas y bajas por zona",
)
async def get_customer_events_summary(
    lookback_days: int = Query(default=365, ge=1, le=365),
    zone: Optional[str] = Query(default=None),
    event_type: Optional[Literal["alta", "baja"]] = Query(default=None),
    source: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    events = _filter_customer_events(
        lookback_days,
        zone=zone,
        event_type=event_type,
        source=source,
    )
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
            "source": source,
        },
        "stats": stats,
        "totals": totals,
        "zones": zones,
    }

@router.get(
    "/analytics/customer-events/metrics",
    summary="Totales agregados de altas/bajas/neto",
)
async def get_customer_event_metrics(
    lookback_days: int = Query(default=365, ge=1, le=365),
    zone: Optional[str] = Query(default=None),
    event_type: Optional[Literal["alta", "baja"]] = Query(default=None),
    source: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    events = _filter_customer_events(
        lookback_days,
        zone=zone,
        event_type=event_type,
        source=source,
    )
    totals, _ = _summarize_customer_events(events)
    return {
        "altas": totals["altas"],
        "bajas": totals["bajas"],
        "neto": totals["neto"],
    }

@router.get(
    "/analytics/customer-events/time-series",
    summary="Serie temporal de altas y bajas por zona",
)
async def get_customer_events_time_series(
    lookback_days: int = Query(default=365, ge=1, le=365),
    zone: Optional[str] = Query(default=None),
    source: Optional[str] = Query(default=None),
) -> List[Dict[str, Any]]:
    events = _filter_customer_events(
        lookback_days,
        zone=zone,
        source=source,
    )
    buckets: Dict[Tuple[str, date], Dict[str, Any]] = defaultdict(
        lambda: {"altas": 0, "bajas": 0, "lat": None, "lon": None}
    )
    for event in events:
        try:
            timestamp = parse_iso8601(event["timestamp"])
        except Exception:
            continue
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
            fallback_lat, fallback_lon = resolve_zone_coordinates(zone_name)
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

@router.get(
    "/analytics/customer-events/geo",
    summary="Eventos georreferenciados segmentados por tipo",
)
async def get_customer_events_geo(
    lookback_days: int = Query(default=365),
    zone: Optional[str] = Query(default=None),
    source: Optional[str] = Query(default=None),
) -> Dict[str, List[Dict[str, Any]]]:
    events = _filter_customer_events(
        lookback_days,
        zone=zone,
        source=source,
    )
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
            fallback_lat, fallback_lon = resolve_zone_coordinates(entry.get("zone") or DEFAULT_ZONE_LABEL)
            if entry["lat"] is None:
                entry["lat"] = fallback_lat
            if entry["lon"] is None:
                entry["lon"] = fallback_lon
        if event.get("event_type") == "alta":
            altas.append(entry)
        else:
            bajas.append(entry)
    return {"altas": altas, "bajas": bajas}

@router.get(
    "/analytics/customer-events/map/altas",
    summary="Eventos de alta con coordenadas resueltas",
)
async def get_customer_events_map_altas(
    lookback_days: int = Query(default=365),
    zone: Optional[str] = Query(default=None),
    source: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    events = _filter_customer_events(
        lookback_days,
        zone=zone,
        event_type="alta",
        source=source,
        latest_per_customer=True,
    )
    feature_collection = _events_to_feature_collection(events)
    feature_collection["event_type"] = "alta"
    feature_collection["zone_filter"] = zone
    return feature_collection

@router.get(
    "/analytics/customer-events/map/bajas",
    summary="Eventos de baja con coordenadas resueltas",
)
async def get_customer_events_map_bajas(
    lookback_days: int = Query(default=365),
    zone: Optional[str] = Query(default=None),
    source: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    events = _filter_customer_events(
        lookback_days,
        zone=zone,
        event_type="baja",
        source=source,
        latest_per_customer=True,
    )
    feature_collection = _events_to_feature_collection(events)
    feature_collection["event_type"] = "baja"
    feature_collection["zone_filter"] = zone
    return feature_collection


@router.post(
    "/analytics/reconciliation/run",
    summary="Ejecuta conciliación entre ISP-Cube, GeoGrid y SmartOLT",
)
async def run_reconciliation_endpoint(
    settings: EnvConfig = Depends(get_settings),
) -> Dict[str, Any]:
    return await reconciliation.run_reconciliation(settings)


@router.get(
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


@router.post("/query")
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
    
    logger.debug("Grafana /query targets: %s", [t.get('target') for t in targets])

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
        source_filter = target_payload.get("source")
        if isinstance(source_filter, str) and not source_filter.strip():
            source_filter = None

        if target_name == "customer_events_map":
            events = _filter_customer_events(
                lookback_days,
                zone=zone_filter,
                event_type=event_type,
                source=source_filter,
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
                incident_ts = parse_iso8601(str(ts_raw)) if ts_raw else datetime.now(timezone.utc)
                if incident_ts < since:
                    continue
                if kind_filter and incident.get("kind") != kind_filter:
                    continue
                open_incidents.append(dict(incident))
            def _incident_ts(value: Dict[str, Any]) -> float:
                raw_ts = value.get("timestamp") or value.get("detected_at")
                try:
                    return parse_iso8601(str(raw_ts)).timestamp()
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


@router.get("/incidents")
async def list_incidents(kind: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
    entries = list(INCIDENT_LOG)
    if kind:
        entries = [entry for entry in entries if entry["kind"] == kind]
    return entries


@router.get("/incidents/resolved")
async def list_resolved_incidents_endpoint(
    lookback_days: int = Query(default=30, ge=1, le=365),
    kind: Optional[str] = Query(default=None),
    customer_id: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
) -> List[Dict[str, Any]]:
    entries = _filter_resolved_incidents(
        lookback_days, customer_id=customer_id, kind=kind
    )
    return entries[:limit]

@router.get("/audits")
async def list_audits(
    action: Optional[Literal["sync", "provision", "decommission"]] = Query(default=None),
    user: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
) -> List[Dict[str, Any]]:
    # from state import AUDIT_LOG # imported at top
    entries = list(AUDIT_LOG)
    if action:
        entries = [entry for entry in entries if entry.action == action]
    if user:
        entries = [entry for entry in entries if entry.user == user]
    return [entry.model_dump() for entry in entries[-limit:]]
