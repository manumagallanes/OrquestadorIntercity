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


async def get_cliente_by_codigo_integrado(
    settings: "EnvConfig",
    codigo_integracao: str,
    fetch_json,
) -> Optional[Dict[str, Any]]:
    """
    Usa la API /clientes/integrado/{codigoIntegracao} que retorna un único cliente.
    """
    client_kwargs, region = settings.http_client_kwargs("geogrid")
    async with httpx.AsyncClient(**client_kwargs) as client:
        response = await fetch_json(
            client,
            "GET",
            f"/clientes/integrado/{codigo_integracao}",
            service="geogrid",
            settings=settings,
            region_name=region,
        )
    if isinstance(response, dict):
        dados = response.get("dados")
        if isinstance(dados, dict):
            return dados
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
            logger.info("GeoGrid create payload: %s", cliente_payload)
            response = await client.post("/clientes", json={"dados": cliente_payload})
            logger.info(
                "HTTP POST %s/clientes -> %s",
                client_kwargs["base_url"],
                response.status_code,
            )
            if response.status_code not in {status.HTTP_201_CREATED, status.HTTP_200_OK}:
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
            "GeoGrid update payload: %s", cliente_payload
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


async def attend_customer(
    settings: "EnvConfig",
    attend_payload: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Llama a /integracao/atender con el payload ya armado.
    Espera que attend_payload tenga al menos idPorta e idCliente, y opcionalmente local, codigoIntegracao, etc.
    """
    client_kwargs, _ = settings.http_client_kwargs("geogrid")
    async with httpx.AsyncClient(**client_kwargs) as client:
        response = await client.post("/integracao/atender", json=attend_payload)
        logger.info(
            "HTTP POST %s/integracao/atender -> %s",
            client_kwargs["base_url"],
            response.status_code,
            )
        if response.status_code != status.HTTP_200_OK:
            raise HTTPException(
                status_code=response.status_code,
                detail=_safe_response_payload(response),
            )
        try:
            body = response.json()
            if isinstance(body, dict):
                return body.get("dados", body)
            return body
        except ValueError:
            return response.text


async def resolve_port_id_by_sigla_and_number(
    settings: "EnvConfig",
    sigla_caja: str,
    porta_num: int,
) -> int:
    """
    Busca una caja/terminal por sigla y devuelve el idPorta asociado al número de porta indicado.
    Prefiere una porta disponible; si está ocupada, devuelve 409; si no existe, 404.
    """
    client_kwargs, _ = settings.http_client_kwargs("geogrid")
    # Buscar terminal por sigla
    async with httpx.AsyncClient(**client_kwargs) as client:
        resp_term = await client.get(
            "/itensRede",
            params={"item": "terminal", "pesquisa": sigla_caja, "pagina": 1, "registrosPorPagina": 50},
        )
        logger.info(
            "HTTP GET %s/itensRede (sigla=%s) -> %s",
            client_kwargs["base_url"],
            sigla_caja,
            resp_term.status_code,
        )
        if resp_term.status_code != status.HTTP_200_OK:
            raise HTTPException(
                status_code=resp_term.status_code,
                detail=_safe_response_payload(resp_term),
            )
        term_data = resp_term.json()
        term_list = term_data.get("registros") if isinstance(term_data, dict) else None
        terminal_id = None
        if isinstance(term_list, list):
            for entry in term_list:
                dados = entry.get("dados") if isinstance(entry, dict) else None
                if dados and str(dados.get("sigla")) == sigla_caja:
                    terminal_id = dados.get("id")
                    break
        if terminal_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"message": "Caja/terminal no encontrada", "sigla": sigla_caja},
            )

        # Listar portas del terminal
        resp_ports = await client.get(f"/viabilidade/{terminal_id}/portas")
        logger.info(
            "HTTP GET %s/viabilidade/%s/portas -> %s",
            client_kwargs["base_url"],
            terminal_id,
            resp_ports.status_code,
        )
        if resp_ports.status_code != status.HTTP_200_OK:
            raise HTTPException(
                status_code=resp_ports.status_code,
                detail=_safe_response_payload(resp_ports),
            )
        ports_data = resp_ports.json()
        registros = ports_data.get("registros") if isinstance(ports_data, dict) else None
        if not isinstance(registros, list):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"message": "Respuesta inesperada de GeoGrid", "payload": ports_data},
            )

        porta_str = str(porta_num)
        candidate_occupied = None
        for entry in registros:
            dados = entry.get("dados") if isinstance(entry, dict) else None
            if not dados:
                continue
            if str(dados.get("porta")) == porta_str:
                if entry.get("disponivel") is True:
                    return int(dados.get("id"))
                if candidate_occupied is None:
                    candidate_occupied = dados.get("id")

        if candidate_occupied is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"message": "Porta no disponible", "sigla": sigla_caja, "porta": porta_num},
            )

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": "No se encontró la porta en la caja", "sigla": sigla_caja, "porta": porta_num},
        )


async def comment_port(
    settings: "EnvConfig",
    id_porta: int,
    comentario: str,
) -> None:
    """
    Añade o actualiza el comentario de una porta en GeoGrid (/diagrama/comentario).
    """
    client_kwargs, _ = settings.http_client_kwargs("geogrid")
    payload = {"idPorta": id_porta, "comentario": comentario}
    async with httpx.AsyncClient(**client_kwargs) as client:
        response = await client.put("/diagrama/comentario", json=payload)
        logger.info(
            "HTTP PUT %s/diagrama/comentario -> %s",
            client_kwargs["base_url"],
            response.status_code,
        )
        if response.status_code != status.HTTP_200_OK:
            raise HTTPException(
                status_code=response.status_code,
                detail=_safe_response_payload(response),
            )


async def create_access_point(
    settings: "EnvConfig",
    *,
    latitude: float,
    longitude: float,
    label: str,
    pasta_id: int,
) -> int:
    """
    Crea un ponto de acesso (casita) en GeoGrid y retorna su id (itensRede).
    """
    client_kwargs, _ = settings.http_client_kwargs("geogrid")
    payload = {
        "dados": {
            "item": "pontoAcesso",
            "latitude": latitude,
            "longitude": longitude,
            "label": label,
        },
        "idPasta": pasta_id,
    }
    async with httpx.AsyncClient(**client_kwargs) as client:
        response = await client.post("/itensRede", json=payload)
        logger.info(
            "HTTP POST %s/itensRede -> %s",
            client_kwargs["base_url"],
            response.status_code,
        )
        if response.status_code not in {status.HTTP_200_OK, status.HTTP_201_CREATED}:
            raise HTTPException(
                status_code=response.status_code,
                detail=_safe_response_payload(response),
            )
        body = response.json()
        dados = body.get("dados") if isinstance(body, dict) else None
        access_point_id = dados.get("id") if isinstance(dados, dict) else None
        if access_point_id is None:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"message": "GeoGrid response without access point id", "payload": body},
            )
        return int(access_point_id)


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
