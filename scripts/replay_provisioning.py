#!/usr/bin/env python3
"""
Reprocesa logs de provisioning sin tocar el cursor principal.

Uso:
  python3 scripts/replay_provisioning.py
  python3 scripts/replay_provisioning.py --since "2026-01-05T00:00:00Z"
  python3 scripts/replay_provisioning.py --lookback-hours 24
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
    # ISP Cube espera timestamps en hora Argentina (UTC-3), no UTC
    return dt.astimezone(ARGENTINA_TZ).strftime("%Y-%m-%dT%H:%M:%S")


def _start_of_day(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reintenta provisioning logs sin mover cursor principal."
    )
    parser.add_argument(
        "--since",
        help="Timestamp ISO8601 inicial (default: inicio del dia UTC).",
    )
    parser.add_argument(
        "--lookback-hours",
        type=float,
        help="Horas hacia atras (ignora --since).",
    )
    parser.add_argument(
        "--status-file",
        help="Ruta para el status JSON (default: .state/connections_provisioning.replay.status.json).",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    now = datetime.now(UTC)
    if args.lookback_hours is not None:
        since_dt = now - timedelta(hours=args.lookback_hours)
        since = _format_iso(since_dt)
    elif args.since:
        since = args.since
    else:
        since = _format_iso(_start_of_day(now))

    repo_root = Path(__file__).resolve().parents[1]
    poller_path = repo_root / "scripts" / "poll_isp_connections.py"
    status_file = (
        Path(args.status_file).expanduser()
        if args.status_file
        else repo_root / ".state" / "connections_provisioning.replay.status.json"
    )

    env = os.environ.copy()
    if "ORCHESTRATOR_DOTENV_PATH" not in env:
        default_dotenv = repo_root / ".env"
        if default_dotenv.exists():
            env["ORCHESTRATOR_DOTENV_PATH"] = str(default_dotenv)

    cmd = [
        sys.executable,
        str(poller_path),
        "--since",
        since,
        "--no-cursor",
        "--status-file",
        str(status_file),
    ]
    if args.dry_run:
        cmd.append("--dry-run")

    result = subprocess.run(cmd, cwd=repo_root, env=env, check=False)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
