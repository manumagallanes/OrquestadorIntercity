#!/usr/bin/env python3
"""
Job utilitario para detectar altas nuevas en ISP-Cube y disparar sync en el orquestador.

Requisitos:
    - Exportar las mismas variables ISP_* que usa el orquestador (.env)
    - ORCHESTRATOR_BASE_URL (por defecto http://localhost:8000)
    - Opcional: ORCHESTRATOR_USER_HEADER, ORCHESTRATOR_STATE_DIR
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx
from dotenv import load_dotenv


logging.basicConfig(
    level=os.getenv("JOB_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s poll-isp :: %(message)s",
)
logger = logging.getLogger(__name__)


UTC = timezone.utc
# ISP Cube usa hora local de Argentina (UTC-3) para los timestamps
ARGENTINA_TZ = timezone(timedelta(hours=-3))
DEFAULT_LOOKBACK_HOURS = 6
DEFAULT_STATE_DIR = Path(os.getenv("ORCHESTRATOR_STATE_DIR", ".state"))
STATE_FILE = DEFAULT_STATE_DIR / "connections_provisioning.cursor"
DEFAULT_STATUS_FILE = DEFAULT_STATE_DIR / "connections_provisioning.status.json"
SYNC_MOVEMENTS = {"create_connection", "ftth_change"}
EVENT_MOVEMENTS = {"create_connection", "delete_connection"}

REQUIRED_ISP_VARS = [
    "ISP_BASE_URL",
    "ISP_API_KEY",
    "ISP_CLIENT_ID",
    "ISP_USERNAME",
    "ISP_BEARER",
]


def _ensure_state_dir() -> None:
    DEFAULT_STATE_DIR.mkdir(parents=True, exist_ok=True)


def _read_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def _parse_iso(value: str) -> datetime:
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    dt = datetime.fromisoformat(cleaned)
    return dt.astimezone(UTC)


def _format_param_timestamp(dt: datetime) -> str:
    # ISP Cube espera timestamps en hora Argentina (UTC-3), no UTC
    return dt.astimezone(ARGENTINA_TZ).strftime("%Y-%m-%dT%H:%M:%S")


def load_cursor() -> Optional[datetime]:
    if not STATE_FILE.exists():
        return None
    try:
        raw = STATE_FILE.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        return _parse_iso(raw)
    except Exception as exc:
        logger.warning("Unable to read cursor file %s: %s", STATE_FILE, exc)
        return None


def save_cursor(ts: datetime) -> None:
    _ensure_state_dir()
    STATE_FILE.write_text(ts.astimezone(UTC).isoformat(), encoding="utf-8")


def write_status(payload: Dict[str, Any], status_file: Optional[Path] = None) -> None:
    _ensure_state_dir()
    try:
        target = status_file or DEFAULT_STATUS_FILE
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("Unable to persist status file: %s", exc)


def isp_headers() -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "api-key": _read_env("ISP_API_KEY"),
        "client-id": _read_env("ISP_CLIENT_ID"),
        "login-type": os.getenv("ISP_LOGIN_TYPE", "api"),
        "username": _read_env("ISP_USERNAME"),
        "Authorization": _read_env("ISP_BEARER"),
    }
    return headers


def fetch_provisioning_logs(
    client: httpx.Client,
    *,
    base_url: str,
    since: datetime,
) -> tuple[List[Dict[str, Any]], bool]:
    params = {"created_at": _format_param_timestamp(since)}
    url = f"{base_url.rstrip('/')}/connections/connections_provisioning_logs"
    logger.info("Consultando ISP logs desde %s", params["created_at"])
    response = client.get(url, params=params, headers=isp_headers(), timeout=15.0)
    provisioning_disabled = False
    if response.status_code == 400:
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        if isinstance(payload, dict) and payload.get("message") == "messages.provisioning_logs_disabled_alert":
            logger.warning("ISP reporta provisioning_logs deshabilitado; finalizar job sin errores.")
            provisioning_disabled = True
            return [], provisioning_disabled
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError(
            f"Respuesta inesperada en provisioning_logs: {json.dumps(data)[:200]}"
        )
    return data, provisioning_disabled


def _movement_timestamp(entry: Dict[str, Any]) -> Optional[datetime]:
    created_raw = entry.get("created_at") or entry.get("updated_at")
    if not created_raw:
        return None
    try:
        return _parse_iso(str(created_raw))
    except Exception:
        return None


def _movement_key(entry: Dict[str, Any]) -> str:
    movement_type = entry.get("movement_type") or "unknown"
    connection_id = entry.get("connection_id") or "na"
    return f"{movement_type}:{connection_id}"


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def sync_connection(
    client: httpx.Client,
    *,
    orchestrator_base: str,
    user_header: str,
    connection_id: int,
    customer_name: Optional[str] = None,
) -> None:
    url = f"{orchestrator_base.rstrip('/')}/sync/customer"
    headers = {"Content-Type": "application/json", user_header: "provisioning-job"}
    payload = {"connection_id": connection_id}
    if customer_name:
        payload["customer_name"] = customer_name
    response = client.post(url, json=payload, headers=headers, timeout=60.0)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError:
        logger.error(
            "Sync fallo para connection_id=%s -> %s %s",
            connection_id,
            response.status_code,
            response.text,
        )
        raise
    logger.info(
        "Sync OK connection_id=%s :: %s",
        connection_id,
        response.json(),
    )


def register_customer_event_from_log(
    client: httpx.Client,
    *,
    orchestrator_base: str,
    user_header: str,
    entry: Dict[str, Any],
) -> None:
    movement_type = entry.get("movement_type")
    if movement_type not in EVENT_MOVEMENTS:
        return
    event_type = "alta" if movement_type == "create_connection" else "baja"
    zone = entry.get("connection_city_name") or entry.get("customer_city")
    payload: Dict[str, Any] = {
        "event_type": event_type,
        "zone": zone,
        "city": zone,
        "lat": _safe_float(entry.get("connection_lat")),
        "lon": _safe_float(entry.get("connection_lng")),
        "timestamp": entry.get("created_at") or entry.get("updated_at"),
        "source": "isp-log",
        "metadata": {
            "movement_id": entry.get("id"),
            "movement_type": movement_type,
            "connection_id": entry.get("connection_id"),
            "connection_code": entry.get("customer_code"),
            "customer_name": entry.get("customer_name"),
        },
    }
    customer_id = entry.get("customer_id")
    if customer_id is not None:
        try:
            payload["customer_id"] = int(customer_id)
        except (TypeError, ValueError):
            pass
    url = f"{orchestrator_base.rstrip('/')}/analytics/customer-events"
    headers = {"Content-Type": "application/json", user_header: "provisioning-job"}
    try:
        response = client.post(url, json=payload, headers=headers, timeout=15.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning(
            "No se pudo registrar evento ISP (movement=%s id=%s): %s",
            movement_type,
            entry.get("id"),
            exc,
        )


def process_logs(
    logs: Iterable[Dict[str, Any]],
    *,
    orchestrator_base: str,
    user_header: str,
) -> tuple[Optional[datetime], int, int]:
    max_ts: Optional[datetime] = None
    processed = 0
    skipped = 0
    min_ts = datetime.min.replace(tzinfo=UTC)
    candidates: Dict[int, tuple[datetime, Dict[str, Any]]] = {}
    with httpx.Client() as orch_client:
        for entry in logs:
            movement_type = entry.get("movement_type")
            movement_ts = _movement_timestamp(entry)
            if movement_ts and (max_ts is None or movement_ts > max_ts):
                max_ts = movement_ts
            register_customer_event_from_log(
                orch_client,
                orchestrator_base=orchestrator_base,
                user_header=user_header,
                entry=entry,
            )
            if movement_type not in SYNC_MOVEMENTS:
                skipped += 1
                continue
            connection_id = entry.get("connection_id")
            if connection_id is None:
                skipped += 1
                continue
            try:
                connection_id_int = int(connection_id)
            except (TypeError, ValueError):
                skipped += 1
                continue
            entry_ts = movement_ts or min_ts
            existing = candidates.get(connection_id_int)
            if existing is None or entry_ts > existing[0]:
                candidates[connection_id_int] = (entry_ts, entry)

        for connection_id_int, (_, entry) in sorted(
            candidates.items(), key=lambda item: item[1][0]
        ):
            movement_type = entry.get("movement_type") or "unknown"
            logger.info(
                "Procesando conexion=%s tipo=%s nombre=%s",
                connection_id_int,
                movement_type,
                entry.get("customer_name"),
            )
            try:
                sync_connection(
                    orch_client,
                    orchestrator_base=orchestrator_base,
                    user_header=user_header,
                    connection_id=connection_id_int,
                    customer_name=entry.get("customer_name"),
                )
                processed += 1
            except Exception:
                # dejamos que el job siga con el resto, pero no adelantamos el cursor
                logger.exception(
                    "Error sincronizando connection_id=%s (entry=%s)",
                    connection_id,
                    _movement_key(entry),
                )
                continue
    logger.info("Procesados=%s | omitidos=%s", processed, skipped)
    return max_ts, processed, skipped


def validate_env() -> None:
    missing = [var for var in REQUIRED_ISP_VARS if not os.getenv(var)]
    if missing:
        raise RuntimeError(f"Faltan variables: {', '.join(missing)}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detecta movimientos recientes en ISP-Cube y sincroniza nuevas conexiones."
    )
    parser.add_argument(
        "--since",
        help="Timestamp ISO8601 inicial (default: último cursor o lookback)",
    )
    parser.add_argument(
        "--lookback-hours",
        type=float,
        default=DEFAULT_LOOKBACK_HOURS,
        help=f"Horas hacia atrás si no existe cursor (default: {DEFAULT_LOOKBACK_HOURS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Lista movimientos pero no llama al orquestador.",
    )
    parser.add_argument(
        "--no-cursor",
        action="store_true",
        help="No lee ni actualiza el cursor (modo reconciliación).",
    )
    parser.add_argument(
        "--status-file",
        help="Ruta opcional para guardar el status JSON del job.",
    )
    return parser


def main() -> None:
    dotenv_path = os.getenv("ORCHESTRATOR_DOTENV_PATH", ".env")
    if Path(dotenv_path).exists():
        load_dotenv(dotenv_path, override=False)
    else:
        # intenta cargar desde la raíz sólo si existe
        load_dotenv(override=False)
    args = build_arg_parser().parse_args()
    run_started = datetime.now(tz=UTC)
    status_payload: Dict[str, Any] = {
        "run_started": run_started.isoformat(),
        "dry_run": bool(args.dry_run),
        "lookback_hours": args.lookback_hours,
        "result": "running",
        "processed": 0,
        "skipped": 0,
        "message": None,
        "cursor_before": None,
        "cursor_after": None,
    }
    status_file = Path(args.status_file).expanduser() if args.status_file else DEFAULT_STATUS_FILE
    try:
        validate_env()

        orchestrator_base = os.getenv("ORCHESTRATOR_BASE_URL", "http://localhost:8000")
        user_header = os.getenv("ORCHESTRATOR_USER_HEADER", "X-Orchestrator-User")
        isp_base = _read_env("ISP_BASE_URL").rstrip("/")

        cursor_ts = None if args.no_cursor else load_cursor()
        if args.since:
            cursor_ts = _parse_iso(args.since)
        if cursor_ts is None:
            cursor_ts = datetime.now(tz=UTC) - timedelta(hours=args.lookback_hours)
        status_payload["cursor_before"] = cursor_ts.isoformat()

        with httpx.Client() as isp_client:
            logs, provisioning_disabled = fetch_provisioning_logs(
                isp_client, base_url=isp_base, since=cursor_ts
            )

        if provisioning_disabled:
            status_payload["result"] = "warning"
            status_payload["message"] = "provisioning_logs_disabled"
            status_payload["run_completed"] = datetime.now(tz=UTC).isoformat()
            write_status(status_payload, status_file=status_file)
            return

        if not logs:
            logger.info("Sin movimientos recientes desde %s", cursor_ts.isoformat())
            status_payload["result"] = "ok"
            status_payload["message"] = "sin_movimientos"
            status_payload["run_completed"] = datetime.now(tz=UTC).isoformat()
            write_status(status_payload, status_file=status_file)
            return

        if args.dry_run:
            for entry in logs:
                logger.info(
                    "Movimiento %s @ %s :: connection_id=%s customer_id=%s",
                    entry.get("movement_type"),
                    entry.get("created_at"),
                    entry.get("connection_id"),
                    entry.get("customer_id"),
                )
            max_ts = max(
                filter(None, (_movement_timestamp(entry) for entry in logs)),
                default=None,
            )
            processed = sum(
                1
                for entry in logs
            if entry.get("movement_type") in SYNC_MOVEMENTS
            and entry.get("connection_id") is not None
        )
            skipped = len(logs) - processed
        else:
            max_ts, processed, skipped = process_logs(
                logs,
                orchestrator_base=orchestrator_base,
                user_header=user_header,
            )

        status_payload["processed"] = processed
        status_payload["skipped"] = skipped
        status_payload["result"] = "ok"
        status_payload["message"] = "movimientos_procesados"

        if max_ts:
            status_payload["cursor_after"] = max_ts.isoformat()
            if args.no_cursor:
                logger.info("Modo sin cursor: no se actualiza cursor (max_ts=%s)", max_ts.isoformat())
            else:
                save_cursor(max_ts)
                logger.info("Cursor actualizado a %s", max_ts.isoformat())

        status_payload["run_completed"] = datetime.now(tz=UTC).isoformat()
        write_status(status_payload, status_file=status_file)
    except Exception as exc:
        status_payload["result"] = "error"
        status_payload["message"] = str(exc)
        status_payload["run_completed"] = datetime.now(tz=UTC).isoformat()
        write_status(status_payload, status_file=status_file)
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logger.exception("Job finalizado con error: %s", exc)
        raise
