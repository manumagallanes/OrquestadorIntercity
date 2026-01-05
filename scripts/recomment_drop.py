#!/usr/bin/env python3
import argparse
import asyncio
from typing import Any, Dict, Optional

import httpx

from orchestrator import main as orchestrator_main
from orchestrator.services import geogrid as geogrid_service


def _extract_customer_name(connection: Dict[str, Any]) -> str:
    customer = connection.get("customer") or {}
    if isinstance(customer, dict) and customer.get("name"):
        return str(customer.get("name"))
    return str(connection.get("customer_name") or "").strip()


def _extract_box_sigla(connection: Dict[str, Any]) -> str:
    ftthbox = connection.get("ftthbox") or {}
    if isinstance(ftthbox, dict) and ftthbox.get("name"):
        return str(ftthbox.get("name"))
    return str(connection.get("ftthbox_name") or "").strip()


def _extract_port_number(connection: Dict[str, Any]) -> Optional[int]:
    ftth_port = connection.get("ftth_port") or {}
    if isinstance(ftth_port, dict) and ftth_port.get("nro") is not None:
        try:
            return int(ftth_port.get("nro"))
        except (TypeError, ValueError):
            return None
    return None


async def _fetch_connection(settings: orchestrator_main.EnvConfig, connection_id: int) -> Dict[str, Any]:
    client_kwargs, _ = settings.http_client_kwargs("isp")
    async with httpx.AsyncClient(**client_kwargs) as client:
        response = await client.get("/connection", params={"connection_id": connection_id})
        response.raise_for_status()
        data = response.json()
    if isinstance(data, list) and data:
        return data[0]
    raise RuntimeError(f"Conexion {connection_id} no encontrada en ISP Cube")


async def _run(args: argparse.Namespace) -> None:
    settings = orchestrator_main.load_env_config()
    connection = await _fetch_connection(settings, args.connection_id)
    customer_name = _extract_customer_name(connection)
    sigla_caja = args.caja_sigla or _extract_box_sigla(connection)
    porta_num = args.porta_num or _extract_port_number(connection)
    if not sigla_caja:
        raise RuntimeError("No se pudo resolver la sigla de caja. Usa --caja-sigla.")
    if porta_num is None:
        raise RuntimeError("No se pudo resolver el numero de porta. Usa --porta-num.")

    base_label = f"Drop - {customer_name}" if customer_name else f"Drop - {args.connection_id}"
    port_id = await geogrid_service.resolve_port_id_by_sigla_and_number(
        settings,
        sigla_caja=sigla_caja,
        porta_num=porta_num,
        allow_unavailable=True,
    )
    label = await geogrid_service.resolve_drop_comment_label(
        settings,
        sigla_caja=sigla_caja,
        porta_num=porta_num,
        base_label=base_label,
        target_port_id=port_id,
    )
    await geogrid_service.comment_port(settings, port_id, label)
    print(f"OK: comentario actualizado en porta {port_id} -> {label}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recomenta la porta del drop con formato 'Drop - Nombre' y sufijos."
    )
    parser.add_argument("--connection-id", type=int, required=True, help="ID de conexion en ISP Cube")
    parser.add_argument("--caja-sigla", help="Sigla de la caja (si no sale de ISP)")
    parser.add_argument("--porta-num", type=int, help="Numero de porta (si no sale de ISP)")
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
