from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

import httpx
from fastapi import HTTPException, status

if TYPE_CHECKING:  # pragma: no cover
    from ..main import EnvConfig

logger = logging.getLogger("geogrid_service")


async def list_clientes(
    settings: "EnvConfig",
    fetch_json,
) -> List[Dict[str, Any]]:
    client_kwargs, region = settings.http_client_kwargs("geogrid")
    async with httpx.AsyncClient(**client_kwargs) as client:
        data = await fetch_json(
            client,
            "GET",
            "/clientes",
            service="geogrid",
            settings=settings,
            region_name=region,
        )
    if isinstance(data, dict):
        entries = data.get("dados")
        if isinstance(entries, list):
            return entries
    return []


async def get_cliente_by_codigo(
    settings: "EnvConfig",
    codigo_integracao: str,
    fetch_json,
) -> Optional[Dict[str, Any]]:
    client_kwargs, region = settings.http_client_kwargs("geogrid")
    async with httpx.AsyncClient(**client_kwargs) as client:
        response = await fetch_json(
            client,
            "GET",
            "/clientes",
            params={"codigoIntegracao": codigo_integracao},
            service="geogrid",
            settings=settings,
            region_name=region,
        )
    if isinstance(response, dict):
        dados = response.get("dados")
        if isinstance(dados, list) and dados:
            return dados[0]
    return None


async def upsert_cliente(
    settings: "EnvConfig",
    cliente_payload: Dict[str, Any],
    fetch_json,
) -> Tuple[int, str]:
    client_kwargs, region = settings.http_client_kwargs("geogrid")
    codigo_integracao = cliente_payload["codigoIntegracao"]

    async with httpx.AsyncClient(**client_kwargs) as client:
        existing = await fetch_json(
            client,
            "GET",
            "/clientes",
            params={"codigoIntegracao": codigo_integracao},
            service="geogrid",
            settings=settings,
            region_name=region,
        )

        existing_id: Optional[int] = None
        if isinstance(existing, dict):
            dados = existing.get("dados")
            if isinstance(dados, list) and dados:
                existing_id = dados[0].get("id")

        if existing_id is None:
            response = await client.post("/clientes", json={"dados": cliente_payload})
            logger.info(
                "HTTP POST %s/clientes -> %s",
                client_kwargs["base_url"],
                response.status_code,
            )
            if response.status_code != status.HTTP_201_CREATED:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=_safe_response_payload(response),
                )
            body = response.json()
            geogrid_id = (body.get("dados") or {}).get("id")
            if geogrid_id is None:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail={"message": "GeoGrid response without id", "payload": body},
                )
            return int(geogrid_id), "created"

        response = await client.put(
            f"/clientes/{existing_id}",
            json={"dados": cliente_payload},
        )
        logger.info(
            "HTTP PUT %s/clientes/%s -> %s",
            client_kwargs["base_url"],
            existing_id,
            response.status_code,
        )
        if response.status_code not in {status.HTTP_200_OK, status.HTTP_204_NO_CONTENT}:
            raise HTTPException(
                status_code=response.status_code,
                detail=_safe_response_payload(response),
            )
        return int(existing_id), "updated"


async def assign_port(
    settings: "EnvConfig",
    assignment_payload: Dict[str, Any],
) -> Dict[str, Any]:
    client_kwargs, _ = settings.http_client_kwargs("geogrid")
    async with httpx.AsyncClient(**client_kwargs) as client:
        response = await client.post("/integracao/atender", json=assignment_payload)
        logger.info(
            "HTTP POST %s/integracao/atender -> %s",
            client_kwargs["base_url"],
            response.status_code,
        )
        if response.status_code == status.HTTP_409_CONFLICT:
            detail = _safe_response_payload(response)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "GeoGrid reporta la puerta como ocupada",
                    "detail": detail,
                },
            )
        if response.status_code != status.HTTP_200_OK:
            raise HTTPException(
                status_code=response.status_code,
                detail=_safe_response_payload(response),
            )
        return response.json().get("dados", {})


async def remove_assignment(
    settings: "EnvConfig",
    port_identifier: str,
    geogrid_id: int,
) -> None:
    client_kwargs, _ = settings.http_client_kwargs("geogrid")
    async with httpx.AsyncClient(**client_kwargs) as client:
        response = await client.delete(
            f"/integracao/atender/{port_identifier}/{geogrid_id}"
        )
        logger.info(
            "HTTP DELETE %s/integracao/atender/%s/%s -> %s",
            client_kwargs["base_url"],
            port_identifier,
            geogrid_id,
            response.status_code,
        )
        if response.status_code == status.HTTP_404_NOT_FOUND:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "message": "No se encontró la asignación en GeoGrid",
                    "geogrid_id": geogrid_id,
                    "port": port_identifier,
                },
            )
        if response.status_code != status.HTTP_200_OK:
            raise HTTPException(
                status_code=response.status_code,
                detail=_safe_response_payload(response),
            )


def _safe_response_payload(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text

