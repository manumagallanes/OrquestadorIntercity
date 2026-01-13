#!/usr/bin/env python3
"""
Herramienta de Reprocesamiento Histórico (Replay).

DESCRIPCIÓN:
    Esta herramienta permite "rebobinar" y volver a procesar logs de ISP-Cube
    sin afectar la operación normal del scheduler.

CASOS DE USO:
    1. Si se detectó un bug en el orquestador que causó que eventos se omitieran.
    2. Si ISP-Cube tuvo problemas y los logs aparecieron con retraso.
    3. Para probar la lógica con datos reales del pasado en un entorno de desarrollo.

FUNCIONAMIENTO:
    Este script funciona como un "wrapper" (envoltorio):
    Calcula las fechas según los argumentos y ejecuta el script principal
    `poll_isp_connections.py` inyectándole la bandera `--no-cursor`.
    Esto asegura que procese los datos pero NO mueva el puntero de "último leído",
    evitando conflictos con la sincronización en tiempo real.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


UTC = timezone.utc
# ISP Cube usa hora local de Argentina (UTC-3) para los timestamps
ARGENTINA_TZ = timezone(timedelta(hours=-3))


def _format_iso(dt: datetime) -> str:
    """Convierte datetime UTC a string ISO en hora Argentina."""
    return dt.astimezone(ARGENTINA_TZ).strftime("%Y-%m-%dT%H:%M:%S")


def _start_of_day(dt: datetime) -> datetime:
    """Retorna el inicio del día (00:00:00) para el datetime dado."""
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ejecuta el reprocesamiento seguro de logs históricos."
    )
    parser.add_argument(
        "--since",
        help="Fecha de inicio exacta (ISO8601). Por defecto: Inicio del día actual.",
    )
    parser.add_argument(
        "--lookback-hours",
        type=float,
        help="Cuántas horas hacia atrás reprocesar (sobreescribe --since).",
    )
    parser.add_argument(
        "--status-file",
        help="Archivo donde guardar el reporte del replay (para no pisar el status del scheduler).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Simulacro: Solo muestra qué procesaría.")
    args = parser.parse_args()

    now = datetime.now(UTC)
    
    # Calcular fecha de inicio
    if args.lookback_hours is not None:
        since_dt = now - timedelta(hours=args.lookback_hours)
        since = since_dt.isoformat() # Usamos ISO nativo, poll_isp lo entiende
    elif args.since:
        since = args.since
    else:
        # Por defecto: desde las 00:00 de hoy
        since = _start_of_day(now).isoformat()

    # Localizar rutas de scripts
    repo_root = Path(__file__).resolve().parents[1]
    poller_path = repo_root / "scripts" / "poll_isp_connections.py"
    
    # Definir archivo de status separado para no confundir al monitoreo
    status_file = (
        Path(args.status_file).expanduser()
        if args.status_file
        else repo_root / ".state" / "connections_provisioning.replay.status.json"
    )

    # Preparar entorno para el subprocess
    env = os.environ.copy()
    if "ORCHESTRATOR_DOTENV_PATH" not in env:
        default_dotenv = repo_root / ".env"
        if default_dotenv.exists():
            env["ORCHESTRATOR_DOTENV_PATH"] = str(default_dotenv)

    print(f"--- Iniciando REPLAY desde {since} ---")
    
    # Construir comando
    cmd = [
        sys.executable,
        str(poller_path),
        "--since",
        since,
        "--no-cursor", # CRÍTICO: No tocar el estado del scheduler
        "--status-file",
        str(status_file),
    ]
    if args.dry_run:
        cmd.append("--dry-run")

    # Ejecutar
    result = subprocess.run(cmd, cwd=repo_root, env=env, check=False)
    
    print(f"--- Fin REPLAY (Código de salida: {result.returncode}) ---")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
