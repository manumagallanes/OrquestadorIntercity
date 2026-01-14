from __future__ import annotations

import logging
import re
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
        try:
            response = await fetch_json(
                client,
                "GET",
                f"/clientes/integrado/{codigo_integracao}",
                service="geogrid",
                settings=settings,
                region_name=region,
            )
        except HTTPException as exc:
            if exc.status_code in {status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND}:
                return None
            raise
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
            # Fallback por si la búsqueda por /clientes no retorna el registro.
            integrated = await get_cliente_by_codigo_integrado(
                settings, codigo_integracao, fetch_json
            )
            if isinstance(integrated, dict) and integrated.get("id") is not None:
                return int(integrated["id"]), "existing"

        if existing_id is None:
            logger.info("GeoGrid create payload: %s", cliente_payload)
            response = await client.post("/clientes", json={"dados": cliente_payload})
            logger.info(
                "HTTP POST %s/clientes -> %s",
                client_kwargs["base_url"],
                response.status_code,
            )
            if response.status_code not in {status.HTTP_201_CREATED, status.HTTP_200_OK}:
                detail = _safe_response_payload(response)
                # Algunos entornos responden 422 cuando el codigoIntegracao ya existe.
                if isinstance(detail, dict):
                    for key, value in detail.items():
                        if "codigo" in str(key).lower() and "cadastr" in str(value).lower():
                            integrated = await get_cliente_by_codigo_integrado(
                                settings, codigo_integracao, fetch_json
                            )
                            if isinstance(integrated, dict) and integrated.get("id") is not None:
                                return int(integrated["id"]), "existing"
                            break
                elif isinstance(detail, str) and "cadastr" in detail.lower():
                    integrated = await get_cliente_by_codigo_integrado(
                        settings, codigo_integracao, fetch_json
                    )
                    if isinstance(integrated, dict) and integrated.get("id") is not None:
                        return int(integrated["id"]), "existing"
                raise HTTPException(
                    status_code=response.status_code,
                    detail=detail,
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


async def mark_cliente_as_baja(
    settings: "EnvConfig",
    geogrid_id: int,
    current_name: str,
) -> bool:
    """
    Marca un cliente como dado de baja en GeoGrid, 
    renombrándolo con el prefijo 'BAJA - '.
    
    Returns True si se actualizó correctamente, False si ya tenía el prefijo.
    """
    BAJA_PREFIX = "BAJA - "
    
    # Si ya tiene el prefijo, no hacer nada
    if current_name.startswith(BAJA_PREFIX):
        logger.info("Cliente %s ya está marcado como baja: %s", geogrid_id, current_name)
        return False
    
    new_name = f"{BAJA_PREFIX}{current_name}"
    
    client_kwargs, _ = settings.http_client_kwargs("geogrid")
    async with httpx.AsyncClient(**client_kwargs) as client:
        payload = {"dados": {"nome": new_name}}
        response = await client.put(f"/clientes/{geogrid_id}", json=payload)
        logger.info(
            "HTTP PUT %s/clientes/%s (BAJA rename) -> %s",
            client_kwargs["base_url"],
            geogrid_id,
            response.status_code,
        )
        if response.status_code not in {status.HTTP_200_OK, status.HTTP_204_NO_CONTENT}:
            logger.warning(
                "No se pudo renombrar cliente %s a BAJA: %s",
                geogrid_id,
                response.text[:200] if response.text else "empty",
            )
            return False
    
    logger.info("Cliente %s renombrado a: %s", geogrid_id, new_name)
    return True


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
    *,
    allow_unavailable: bool = False,
) -> int:
    """
    Busca una caja/terminal por sigla y devuelve el idPorta asociado al número de porta indicado.
    Prefiere una porta disponible; si está ocupada, devuelve 409; si no existe, 404.
    """
    _, registros = await fetch_terminal_ports(settings, sigla_caja)

    def _norm_flag(value: Any) -> str:
        return str(value).strip().upper() if value is not None else ""

    def _is_available(value: Any) -> bool:
        if value is True:
            return True
        if isinstance(value, str):
            return value.strip().lower() in {"true", "t", "1", "s", "yes"}
        return False

    porta_str = str(porta_num)
    candidates = []
    for entry in registros:
        if not isinstance(entry, dict):
            continue
        dados = entry.get("dados") if isinstance(entry, dict) else None
        if not isinstance(dados, dict):
            continue
        if str(dados.get("porta")) != porta_str:
            continue
        equipamento = entry.get("equipamento") if isinstance(entry, dict) else None
        equip = equipamento if isinstance(equipamento, dict) else {}
        candidates.append(
            {
                "id": dados.get("id"),
                "available": _is_available(entry.get("disponivel")),
                "tipo": _norm_flag(dados.get("tipo")),
                "atendimento": _norm_flag(equip.get("atendimento")),
                "sigla": equip.get("sigla"),
            }
        )

    if not candidates:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": "No se encontró la porta en la caja", "sigla": sigla_caja, "porta": porta_num},
        )

    available = [c for c in candidates if c["available"]]
    selection_pool = available if available else (candidates if allow_unavailable else [])
    if selection_pool:
        preferred = [
            c
            for c in selection_pool
            if c["tipo"] == "S" and c["atendimento"] == "S"
        ]
        if preferred:
            if len(preferred) > 1:
                logger.warning(
                    "Múltiples portas candidatas para sigla=%s porta=%s :: %s",
                    sigla_caja,
                    porta_num,
                    [p.get("sigla") for p in preferred],
                )
            return int(preferred[0]["id"])
        preferred = [c for c in selection_pool if c["tipo"] == "S"]
        if preferred:
            return int(preferred[0]["id"])
        return int(selection_pool[0]["id"])

    if not available:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": "Porta no disponible", "sigla": sigla_caja, "porta": porta_num},
        )


async def fetch_terminal_ports(
    settings: "EnvConfig",
    sigla_caja: str,
) -> Tuple[int, List[Dict[str, Any]]]:
    client_kwargs, _ = settings.http_client_kwargs("geogrid")
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
        return int(terminal_id), registros


async def resolve_drop_comment_label(
    settings: "EnvConfig",
    sigla_caja: str,
    porta_num: int,
    base_label: str,
    target_port_id: int,
) -> str:
    _, registros = await fetch_terminal_ports(settings, sigla_caja)
    target_comment = None
    used_numbers: List[int] = []

    def _matches_label(comment: str) -> Optional[int]:
        pattern = re.compile(rf"^{re.escape(base_label)}(?:\\s+(\\d+))?$", re.IGNORECASE)
        match = pattern.match(comment.strip())
        if not match:
            return None
        if match.group(1):
            try:
                return int(match.group(1))
            except ValueError:
                return None
        return 1

    for entry in registros:
        if not isinstance(entry, dict):
            continue
        dados = entry.get("dados") if isinstance(entry, dict) else None
        if not isinstance(dados, dict):
            continue
        comment = dados.get("comentario")
        if comment:
            number = _matches_label(str(comment))
            if number is not None:
                used_numbers.append(number)
            if str(dados.get("id")) == str(target_port_id):
                target_comment = str(comment)

    if target_comment and _matches_label(target_comment) is not None:
        return target_comment

    if not used_numbers:
        return base_label

    next_number = max(used_numbers) + 1
    return f"{base_label} {next_number}"

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


def _extract_cabo_tipo_entries(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, dict):
        registros = payload.get("registros") or payload.get("dados")
        if isinstance(registros, list):
            return [entry for entry in registros if isinstance(entry, dict)]
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    return []


def _match_cabo_tipo(entry: Dict[str, Any], needle: str) -> bool:
    dados = entry.get("dados") if isinstance(entry, dict) else None
    source = dados if isinstance(dados, dict) else entry
    for key in ("nome", "nomenclatura", "tipo", "descricao", "sigla"):
        value = source.get(key)
        if isinstance(value, str) and needle in value.lower():
            return True
    return False


def _extract_cabo_tipo_id(entry: Dict[str, Any]) -> Optional[int]:
    dados = entry.get("dados") if isinstance(entry, dict) else None
    source = dados if isinstance(dados, dict) else entry
    value = source.get("id")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def resolve_cabo_tipo_id_by_name(
    settings: "EnvConfig",
    name: str,
) -> Optional[int]:
    client_kwargs, _ = settings.http_client_kwargs("geogrid")
    params = {"pesquisa": name} if name else None
    async with httpx.AsyncClient(**client_kwargs) as client:
        response = await client.get("/cabosTipos", params=params)
        logger.info(
            "HTTP GET %s/cabosTipos -> %s",
            client_kwargs["base_url"],
            response.status_code,
        )
        if response.status_code != status.HTTP_200_OK:
            raise HTTPException(
                status_code=response.status_code,
                detail=_safe_response_payload(response),
            )
        payload = response.json()

    entries = _extract_cabo_tipo_entries(payload)
    if not entries:
        return None
    needle = name.lower()
    for entry in entries:
        if _match_cabo_tipo(entry, needle):
            cabo_id = _extract_cabo_tipo_id(entry)
            if cabo_id is not None:
                return cabo_id
    # Fallback: devolver el primero si existe
    return _extract_cabo_tipo_id(entries[0])


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
