#!/usr/bin/env python3
"""
Reintenta incidentes abiertos llamando /sync/customer.

Uso:
  python3 scripts/retry_incidents.py --lookback-hours 24
"""
from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

import httpx
from dotenv import load_dotenv


logging.basicConfig(
    level=os.getenv("JOB_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s retry-incidents :: %(message)s",
)
logger = logging.getLogger(__name__)

UTC = timezone.utc
DEFAULT_KINDS = [
    "missing_geogrid_assignment",
    "geogrid_unavailable",
]


def _parse_iso(value: str) -> Optional[datetime]:
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    try:
        return datetime.fromisoformat(cleaned).astimezone(UTC)
    except ValueError:
        return None


def _extract_connection_id(incident: Dict[str, Any]) -> Optional[int]:
    for key in ("connection_id", "connectionId"):
        value = incident.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    detail = incident.get("detail")
    if isinstance(detail, dict):
        for key in ("connection_id", "connectionId"):
            value = detail.get(key)
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    return None


def _filter_incidents(
    incidents: Iterable[Dict[str, Any]],
    *,
    kinds: List[str],
    since: datetime,
) -> List[Dict[str, Any]]:
    filtered = []
    for incident in incidents:
        if not isinstance(incident, dict):
            continue
        if incident.get("status") not in (None, "open"):
            continue
        kind = incident.get("kind")
        if kind not in kinds:
            continue
        ts_raw = incident.get("timestamp")
        if not isinstance(ts_raw, str):
            continue
        ts = _parse_iso(ts_raw)
        if ts is None or ts < since:
            continue
        filtered.append(incident)
    return filtered


def _read_env(name: str, default: Optional[str] = None) -> str:
    value = os.getenv(name, default)
    if value is None or value == "":
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Reintenta incidentes abiertos")
    parser.add_argument("--lookback-hours", type=float, default=24.0)
    parser.add_argument(
        "--kinds",
        default=",".join(DEFAULT_KINDS),
        help="Lista separada por comas de incident kinds",
    )
    parser.add_argument("--limit", type=int, default=0, help="Límite de reintentos")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_dotenv()

    base_url = os.getenv("ORCHESTRATOR_BASE_URL", "http://localhost:8000")
    user_header = os.getenv("ORCHESTRATOR_USER_HEADER", "x-app-user")
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    since = datetime.now(UTC) - timedelta(hours=args.lookback_hours)

    logger.info("Buscando incidentes desde %s (kinds=%s)", since.isoformat(), kinds)
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        response = client.get("/incidents")
        response.raise_for_status()
        incidents = response.json()

        filtered = _filter_incidents(incidents, kinds=kinds, since=since)
        if args.limit > 0:
            filtered = filtered[: args.limit]

        if not filtered:
            logger.info("No hay incidentes para reintentar.")
            return 0

        logger.info("Incidentes a reintentar: %s", len(filtered))
        if args.dry_run:
            for incident in filtered:
                logger.info("DRY RUN %s", incident.get("incident_id"))
            return 0

        for incident in filtered:
            connection_id = _extract_connection_id(incident)
            if connection_id is None:
                logger.warning(
                    "Incidente sin connection_id: %s", incident.get("incident_id")
                )
                continue
            payload = {"connection_id": connection_id}
            headers = {user_header: "incident-retry"}
            resp = client.post("/sync/customer", json=payload, headers=headers)
            if resp.status_code >= 400:
                logger.warning(
                    "Sync failed for connection_id=%s -> %s %s",
                    connection_id,
                    resp.status_code,
                    resp.text,
                )
            else:
                logger.info("Sync OK connection_id=%s", connection_id)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
