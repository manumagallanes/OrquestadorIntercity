#!/usr/bin/env python3
"""
Script de Sincronización Automática (Polling de ISP-Cube).

DESCRIPCIÓN:
    Este script es el responsable de mantener los sistemas sincronizados.
    Se conecta a la API de ISP-Cube periódicamente para buscar "novedades" (altas de clientes,
    bajas, cambios de plan, etc.) ocurridas desde la última ejecución.

FUNCIONAMIENTO:
    1.  Carga el último "cursor" (fecha/hora) guardado para saber desde cuándo buscar.
    2.  Consulta a ISP-Cube (`/connections/connections_provisioning_logs`).
    3.  Si encuentra movimientos:
        a. Registra el evento en el sistema de monitoreo (para Grafana).
        b. Si es un alta o modificación, ordena al Orquestador (`/sync/customer`) que actualice GeoGrid.
    4.  Actualiza el cursor con la fecha del último movimiento procesado.

    Además, maneja robustamente la conexión con ISP-Cube, refrescando el token de acceso
    automáticamente si detecta que ha expirado (Error 401).

VARIABLES DE ENTORNO REQUERIDAS:
    - ISP_BASE_URL, ISP_API_KEY, ISP_USERNAME, etc. (Credenciales origen)
    - ORCHESTRATOR_BASE_URL (Destino de la sincronización)
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


# Configuración de Logs: Formato simple para facilitar lectura en Docker
logging.basicConfig(
    level=os.getenv("JOB_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s poll-isp :: %(message)s",
)
logger = logging.getLogger(__name__)


# Configuración de Zonas Horarias y Constantes
UTC = timezone.utc
# ISP Cube trabaja con hora local de Argentina (UTC-3), necesaria para filtrar logs correctamente
ARGENTINA_TZ = timezone(timedelta(hours=-3))

# Si no hay historia previa, mirar 6 horas hacia atrás por defecto
DEFAULT_LOOKBACK_HOURS = 6

# Directorio donde guardamos el archivo "cursor" (estado del job)
DEFAULT_STATE_DIR = Path(os.getenv("ORCHESTRATOR_STATE_DIR", ".state"))
STATE_FILE = DEFAULT_STATE_DIR / "connections_provisioning.cursor"
DEFAULT_STATUS_FILE = DEFAULT_STATE_DIR / "connections_provisioning.status.json"

# Tipos de movimiento que disparan una sincronización hacia GeoGrid
SYNC_MOVEMENTS = {"create_connection", "ftth_change", "delete_connection"}

# Tipos de movimiento que registramos como estadísticas en Grafana
EVENT_MOVEMENTS = {"create_connection", "delete_connection"}

REQUIRED_ISP_VARS = [
    "ISP_BASE_URL",
    "ISP_API_KEY",
    "ISP_CLIENT_ID",
    "ISP_USERNAME",
    "ISP_BEARER",
]


def _ensure_state_dir() -> None:
    """Asegura que exista el directorio para guardar el estado."""
    DEFAULT_STATE_DIR.mkdir(parents=True, exist_ok=True)


def _read_env(name: str) -> str:
    """Lee una variable de entorno obligatoria o lanza error si falta."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"La variable de entorno {name} es obligatoria")
    return value


def _parse_iso(value: str) -> datetime:
    """Convierte un string ISO8601 a objeto datetime UTC."""
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    dt = datetime.fromisoformat(cleaned)
    return dt.astimezone(UTC)


def _format_param_timestamp(dt: datetime) -> str:
    """Convierte datetime a string formato local Argentina para la API de ISP Cube."""
    return dt.astimezone(ARGENTINA_TZ).strftime("%Y-%m-%dT%H:%M:%S")


def load_cursor() -> Optional[datetime]:
    """Lee el archivo de estado para saber cuál fue la última fecha procesada."""
    if not STATE_FILE.exists():
        return None
    try:
        raw = STATE_FILE.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        return _parse_iso(raw)
    except Exception as exc:
        logger.warning("No se pudo leer el archivo cursor %s: %s", STATE_FILE, exc)
        return None


def save_cursor(ts: datetime) -> None:
    """Guarda la fecha del último evento procesado para no repetirlo en la próxima ejecución."""
    _ensure_state_dir()
    STATE_FILE.write_text(ts.astimezone(UTC).isoformat(), encoding="utf-8")


def write_status(payload: Dict[str, Any], status_file: Optional[Path] = None) -> None:
    """Guarda un JSON con el resultado de la ejecución (para monitoreo de salud del job)."""
    _ensure_state_dir()
    try:
        target = status_file or DEFAULT_STATUS_FILE
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("No se pudo guardar archivo de status: %s", exc)


# Variable global para cachear el Bearer Token en memoria durante la ejecución
_CACHED_BEARER: Optional[str] = None


def refresh_isp_token_from_orchestrator(orchestrator_base: str) -> Optional[str]:
    """
    Solicita al Orquestador que refresque el token de ISP (via endpoint /ops/refresh-isp-token).
    Esto centraliza la lógica de renovación.
    """
    global _CACHED_BEARER
    url = f"{orchestrator_base.rstrip('/')}/ops/refresh-isp-token"
    try:
        with httpx.Client() as client:
            response = client.post(url, timeout=30.0)
            response.raise_for_status()
            data = response.json()
            if data.get("status") == "success":
                logger.info("Token de ISP refrescado exitosamente via orchestrator")
                return None  # Señal de éxito (el token nuevo no se retorna aquí, se debe re-autenticar)
    except Exception as exc:
        logger.warning("No se pudo refrescar token via orchestrator: %s", exc)
    return None


def get_fresh_bearer() -> str:
    """Devuelve el token actual (de cache o variable de entorno)."""
    global _CACHED_BEARER
    if _CACHED_BEARER:
        return _CACHED_BEARER
    return _read_env("ISP_BEARER")


def set_cached_bearer(bearer: str) -> None:
    """Actualiza el token en memoria tras una renovación exitosa."""
    global _CACHED_BEARER
    _CACHED_BEARER = bearer


def isp_headers() -> Dict[str, str]:
    """Construye los headers HTTP necesarios para autenticarse con ISP-Cube."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "api-key": _read_env("ISP_API_KEY"),
        "client-id": _read_env("ISP_CLIENT_ID"),
        "login-type": os.getenv("ISP_LOGIN_TYPE", "api"),
        "username": _read_env("ISP_USERNAME"),
        "Authorization": get_fresh_bearer(),
    }
    return headers


def fetch_provisioning_logs(
    client: httpx.Client,
    *,
    base_url: str,
    since: datetime,
    orchestrator_base: str = "http://orchestrator:8000",
    retried: bool = False,
) -> tuple[List[Dict[str, Any]], bool]:
    """
    Realiza la consulta a ISP-Cube buscando logs desde la fecha 'since'.
    
    Características clave:
    - Autocorrección de Auth: Si recibe 401 (Unauthorized), intenta renovar el token y reintentar.
    - Detección de configuración: Si ISP-Cube tiene logs deshabilitados, avisa sin fallar.
    """
    params = {"created_at": _format_param_timestamp(since)}
    url = f"{base_url.rstrip('/')}/connections/connections_provisioning_logs"
    logger.info("Consultando ISP logs desde %s", params["created_at"])
    
    response = client.get(url, params=params, headers=isp_headers(), timeout=15.0)
    provisioning_disabled = False
    
    # Lógica de Recuperación de Token (401)
    if response.status_code == 401 and not retried:
        logger.warning("Token expirado (401), intentando refrescar...")
        # 1. Avisar al orquestador para que actualice su estado global
        refresh_isp_token_from_orchestrator(orchestrator_base)
        
        # 2. Intentar obtener un token válido directamente para usar AHORA en este job
        new_token = _try_authenticate_directly(base_url)
        if new_token:
            set_cached_bearer(new_token)
            # Reintentar la llamada original recursivamente
            return fetch_provisioning_logs(
                client,
                base_url=base_url,
                since=since,
                orchestrator_base=orchestrator_base,
                retried=True,
            )
        else:
            logger.error("No se pudo refrescar token, continuando con el original (probablemente fallará)")
    
    # Manejo de respuesta "funcionalidad deshabilitada" de ISP-Cube
    if response.status_code == 400:
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        if isinstance(payload, dict) and payload.get("message") == "messages.provisioning_logs_disabled_alert":
            logger.warning("ISP reporta logs de aprovisionamiento deshabilitados; finalizando sin errores.")
            provisioning_disabled = True
            return [], provisioning_disabled
            
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError(
            f"Respuesta inesperada en provisioning_logs: {json.dumps(data)[:200]}"
        )
    return data, provisioning_disabled


def _try_authenticate_directly(isp_base_url: str) -> Optional[str]:
    """
    Autenticación de emergencia:
    Intenta obtener un nuevo token golpeando directamente el endpoint /sanctum/token de ISP-Cube,
    usando las credenciales (User/Pass) del entorno.
    """
    username = os.getenv("ISP_USERNAME", "")
    password = os.getenv("ISP_PASSWORD", "")
    api_key = os.getenv("ISP_API_KEY", "")
    client_id = os.getenv("ISP_CLIENT_ID", "")
    
    if not all([username, password, api_key, client_id]):
        logger.warning("Faltan credenciales ISP para autenticación directa (Revisar .env)")
        return None
    
    token_url = f"{isp_base_url.rstrip('/')}/sanctum/token"
    try:
        with httpx.Client() as client:
            response = client.post(
                token_url,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                    "api-key": api_key,
                    "client-id": client_id,
                    "login-type": "api",
                },
                json={
                    "username": username,
                    "password": password,
                },
                timeout=15.0,
            )
            response.raise_for_status()
            data = response.json()
            new_token = data.get("token")
            if new_token:
                logger.info("Autenticación directa exitosa, nuevo token obtenido")
                return f"Bearer {new_token}"
    except Exception as exc:
        logger.warning("Autenticación directa falló: %s", exc)
    return None


def _movement_timestamp(entry: Dict[str, Any]) -> Optional[datetime]:
    """Extrae la fecha del movimiento (created_at o updated_at)."""
    created_raw = entry.get("created_at") or entry.get("updated_at")
    if not created_raw:
        return None
    try:
        return _parse_iso(str(created_raw))
    except Exception:
        return None


def _movement_key(entry: Dict[str, Any]) -> str:
    """Genera una clave única para identificar el movimiento en logs."""
    movement_type = entry.get("movement_type") or "unknown"
    connection_id = entry.get("connection_id") or "na"
    return f"{movement_type}:{connection_id}"


def _safe_float(value: Any) -> Optional[float]:
    """Convierte valor a float de forma segura (para coordenadas)."""
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
    """
    Envía la orden de sincronización al Orquestador.
    Endpoint: POST /sync/customer
    """
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
            "Sync falló para connection_id=%s -> %s %s",
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
    """
    Envía el evento a Analytics para visualización en Grafana.
    Detecta si es Alta o Baja basado en 'movement_type'.
    """
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
            "No se pudo registrar evento ISP en Analytics (id=%s): %s",
            entry.get("id"),
            exc,
        )


def process_logs(
    logs: Iterable[Dict[str, Any]],
    *,
    orchestrator_base: str,
    user_header: str,
) -> tuple[Optional[datetime], int, int]:
    """
    Procesa la lista de logs obtenidos:
    1. Registra eventos en Analytics.
    2. Identifica candidatos para sincronización (Altas/Cambios).
    3. Llama a sync_connection para cada uno.
    4. Determina el nuevo timestamp cursor (max_ts).
    """
    max_ts: Optional[datetime] = None
    processed = 0
    skipped = 0
    min_ts = datetime.min.replace(tzinfo=UTC)
    
    # Agrupamos por connection_id para procesar solo el último movimiento relevante por cliente
    candidates: Dict[int, tuple[datetime, Dict[str, Any]]] = {}
    
    with httpx.Client() as orch_client:
        for entry in logs:
            movement_type = entry.get("movement_type")
            movement_ts = _movement_timestamp(entry)
            
            # Actualizamos la marca de "último visto" global
            if movement_ts and (max_ts is None or movement_ts > max_ts):
                max_ts = movement_ts
                
            # Paso 1: Registrar evento visual
            register_customer_event_from_log(
                orch_client,
                orchestrator_base=orchestrator_base,
                user_header=user_header,
                entry=entry,
            )
            
            # Paso 2: Filtrar si requiere Sincronización
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
            
            # Guardamos solo el movimiento más reciente para esta conexión
            existing = candidates.get(connection_id_int)
            if existing is None or entry_ts > existing[0]:
                candidates[connection_id_int] = (entry_ts, entry)

        # Paso 3: Ejecutar sincronizaciones en orden cronológico
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
                # Si falla una conexión, loguear y seguir con la siguiente.
                logger.exception(
                    "Error sincronizando connection_id=%s (entry=%s)",
                    connection_id,
                    _movement_key(entry),
                )
                continue
                
    logger.info("Resumen del ciclo: Procesados=%s | Omitidos=%s", processed, skipped)
    return max_ts, processed, skipped


def validate_env() -> None:
    """Valida que todas las variables de entorno necesarias estén presentes."""
    missing = [var for var in REQUIRED_ISP_VARS if not os.getenv(var)]
    if missing:
        raise RuntimeError(f"Faltan variables críticas: {', '.join(missing)}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detecta movimientos recientes en ISP-Cube y sincroniza nuevas conexiones."
    )
    parser.add_argument(
        "--since",
        help="Forzar timestamp inicial ISO8601 (ignora el cursor guardado).",
    )
    parser.add_argument(
        "--lookback-hours",
        type=float,
        default=DEFAULT_LOOKBACK_HOURS,
        help=f"Horas a mirar hacia atrás si es la primera ejecución (default: {DEFAULT_LOOKBACK_HOURS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Modo simulación: Lista movimientos pero NO llama al orquestador.",
    )
    parser.add_argument(
        "--no-cursor",
        action="store_true",
        help="Modo reconciliación: Procesa pero NO actualiza el archivo cursor al finalizar.",
    )
    parser.add_argument(
        "--status-file",
        help="Ruta alternativa para guardar el JSON de estado.",
    )
    return parser


def main() -> None:
    # Cargar variables de entorno del archivo .env si existe
    dotenv_path = os.getenv("ORCHESTRATOR_DOTENV_PATH", ".env")
    if Path(dotenv_path).exists():
        load_dotenv(dotenv_path, override=False)
    else:
        load_dotenv(override=False)
        
    args = build_arg_parser().parse_args()
    
    # Preparar estructura de reporte de estado
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

        # Determinar punto de partida (Cursor)
        cursor_ts = None if args.no_cursor else load_cursor()
        
        # Overrides por argumentos CLI
        if args.since:
            cursor_ts = _parse_iso(args.since)
            
        # Default inicial si no hay cursor
        if cursor_ts is None:
            cursor_ts = datetime.now(tz=UTC) - timedelta(hours=args.lookback_hours)
            
        status_payload["cursor_before"] = cursor_ts.isoformat()

        # Obtener logs
        with httpx.Client() as isp_client:
            logs, provisioning_disabled = fetch_provisioning_logs(
                isp_client, base_url=isp_base, since=cursor_ts
            )

        # Caso: Funcionalidad desactivada en origen
        if provisioning_disabled:
            status_payload["result"] = "warning"
            status_payload["message"] = "provisioning_logs_disabled"
            status_payload["run_completed"] = datetime.now(tz=UTC).isoformat()
            write_status(status_payload, status_file=status_file)
            return

        # Caso: Sin novedades
        if not logs:
            logger.info("Sin movimientos recientes desde %s", cursor_ts.isoformat())
            status_payload["result"] = "ok"
            status_payload["message"] = "sin_movimientos"
            status_payload["run_completed"] = datetime.now(tz=UTC).isoformat()
            write_status(status_payload, status_file=status_file)
            return

        # Procesamiento
        if args.dry_run:
            logger.info("--- MODO DRY RUN (Simulación) ---")
            for entry in logs:
                logger.info(
                    "Detectado: %s @ %s :: connection_id=%s customer_id=%s",
                    entry.get("movement_type"),
                    entry.get("created_at"),
                    entry.get("connection_id"),
                    entry.get("customer_id"),
                )
            # Calcular estadísticas simuladas
            processed = sum(
                1 for entry in logs
                if entry.get("movement_type") in SYNC_MOVEMENTS and entry.get("connection_id") is not None
            )
            skipped = len(logs) - processed
            max_ts = max(filter(None, (_movement_timestamp(entry) for entry in logs)), default=None)
        else:
            # Ejecución Real
            max_ts, processed, skipped = process_logs(
                logs,
                orchestrator_base=orchestrator_base,
                user_header=user_header,
            )

        # Finalización
        status_payload["processed"] = processed
        status_payload["skipped"] = skipped
        status_payload["result"] = "ok"
        status_payload["message"] = "movimientos_procesados"

        if max_ts:
            status_payload["cursor_after"] = max_ts.isoformat()
            if args.no_cursor:
                logger.info("Modo sin cursor: No se avanza el puntero (max_ts=%s)", max_ts.isoformat())
            else:
                save_cursor(max_ts)
                logger.info("Cursor actualizado a %s. Guardado exitosamente.", max_ts.isoformat())

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
        logger.exception("El Job finalizó con un error fatal: %s", exc)
        raise
