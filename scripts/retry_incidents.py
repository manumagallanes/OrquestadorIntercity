#!/usr/bin/env python3
"""
Script de Reintento de Incidentes (Auto-Healing).

DESCRIPCIÓN:
    Este script actúa como un mecanismo de "curación" automática.
    Consulta al orquestador por incidentes abiertos recientes y, si son del tipo recuperable
    (por ejemplo, errores de red temporales o servicio no disponible), intenta
    procesar nuevamente la conexión afectada.

FUNCIONAMIENTO:
    1.  Consulta `GET /incidents` para listar problemas pendientes.
    2.  Filtra los incidentes por tipo (ej. "geogrid_unavailable") y por fecha (últimas 24h).
    3.  Para cada incidente válido, extrae el ID de conexión (cliente).
    4.  Llama a `POST /sync/customer` para forzar un reintento de sincronización.

USO:
    Puede ejecutarse manualmente o vía cron/scheduler.
    Ejemplo: python3 retry_incidents.py --lookback-hours 48 --dry-run
"""
from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

import httpx
from dotenv import load_dotenv


# Configuración de Logs
logging.basicConfig(
    level=os.getenv("JOB_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s retry-incidents :: %(message)s",
)
logger = logging.getLogger(__name__)

UTC = timezone.utc

# Tipos de incidentes que consideramos "recuperables" automáticamente
DEFAULT_KINDS = [
    "missing_geogrid_assignment",  # Faltó asignar caja/puerto (quizás ya se corrigió en BSS)
    "geogrid_unavailable",         # GeoGrid estaba caído
]


def _parse_iso(value: str) -> Optional[datetime]:
    """Intenta convertir un string de fecha ISO a datetime."""
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    try:
        return datetime.fromisoformat(cleaned).astimezone(UTC)
    except ValueError:
        return None


def _extract_connection_id(incident: Dict[str, Any]) -> Optional[int]:
    """
    Busca el ID de conexión dentro de la metadata del incidente.
    Puede estar en la raíz o dentro de 'detail'.
    """
    # Intentar buscar en raíz
    for key in ("connection_id", "connectionId"):
        value = incident.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
                
    # Intentar buscar en 'detail' (json anidado)
    detail = incident.get("detail")
    if isinstance(detail, dict):
        for key in ("connection_id", "connectionId"):
            value = detail.get(key)
            if value is not None:
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
    """Selecciona solo los incidentes que cumplen con los criterios de reintento."""
    filtered = []
    for incident in incidents:
        if not isinstance(incident, dict):
            continue
            
        # Solo incidentes abiertos
        if incident.get("status") not in (None, "open"):
            continue
            
        # Solo tipos soportados
        kind = incident.get("kind")
        if kind not in kinds:
            continue
            
        # Solo dentro de la ventana de tiempo
        ts_raw = incident.get("timestamp")
        if not isinstance(ts_raw, str):
            continue
            
        ts = _parse_iso(ts_raw)
        if ts is None or ts < since:
            continue
            
        filtered.append(incident)
    return filtered


def main() -> int:
    parser = argparse.ArgumentParser(description="Reintenta incidentes abiertos automáticamente.")
    parser.add_argument(
        "--lookback-hours",
        type=float,
        default=24.0,
        help="Cuántas horas hacia atrás buscar incidentes (default: 24h)."
    )
    parser.add_argument(
        "--kinds",
        default=",".join(DEFAULT_KINDS),
        help="Lista de tipos de incidente a reintentar (separados por coma).",
    )
    parser.add_argument("--limit", type=int, default=0, help="Límite máximo de incidentes a procesar.")
    parser.add_argument("--dry-run", action="store_true", help="Simulación: No ejecuta la llamada real.")
    args = parser.parse_args()

    load_dotenv()

    base_url = os.getenv("ORCHESTRATOR_BASE_URL", "http://localhost:8000")
    user_header = os.getenv("ORCHESTRATOR_USER_HEADER", "x-app-user")
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    since = datetime.now(UTC) - timedelta(hours=args.lookback_hours)

    logger.info("Buscando incidentes desde %s (Tipos=%s)", since.isoformat(), kinds)
    
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        # 1. Obtener lista completa
        try:
            response = client.get("/incidents")
            response.raise_for_status()
        except Exception as e:
            logger.error("Error consultando API de incidentes: %s", e)
            return 1
            
        incidents = response.json()

        # 2. Filtrar
        filtered = _filter_incidents(incidents, kinds=kinds, since=since)
        if args.limit > 0:
            filtered = filtered[: args.limit]

        if not filtered:
            logger.info("No hay incidentes pendientes para reintentar.")
            return 0

        logger.info("Encontrados %s incidentes recuperables.", len(filtered))
        
        if args.dry_run:
            logger.info("--- DRY RUN MODE ---")
            for incident in filtered:
                logger.info("Se hubiera reintentado incidente ID: %s", incident.get("incident_id"))
            return 0

        # 3. Reintentar uno por uno
        for incident in filtered:
            connection_id = _extract_connection_id(incident)
            if connection_id is None:
                logger.warning(
                    "Incidente ID %s omitido (no se encontró ID de conexión)", incident.get("incident_id")
                )
                continue
                
            payload = {"connection_id": connection_id}
            headers = {user_header: "incident-retry"}
            
            try:
                resp = client.post("/sync/customer", json=payload, headers=headers)
                if resp.status_code >= 400:
                    logger.warning(
                        "Fallo al reintentar conexión ID=%s -> Status %s: %s",
                        connection_id,
                        resp.status_code,
                        resp.text,
                    )
                else:
                    logger.info("Reintento Exitoso para conexión ID=%s", connection_id)
            except Exception as e:
                logger.error("Error de conexión al reintentar ID=%s: %s", connection_id, e)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
