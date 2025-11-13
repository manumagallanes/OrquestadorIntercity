from __future__ import annotations

from typing import Any, Dict, List, Optional, TYPE_CHECKING

import httpx

if TYPE_CHECKING:  # pragma: no cover
    from ..main import EnvConfig


async def list_customers(
    settings: "EnvConfig",
    fetch_json,
) -> List[Dict[str, Any]]:
    """
    Retrieve the catalog of customers from ISP Cube using the public API.
    Normalises `customer_id` to ensure downstream consumers can rely on it.
    """
    client_kwargs, region = settings.http_client_kwargs("isp")
    async with httpx.AsyncClient(**client_kwargs) as client:
        data = await fetch_json(
            client,
            "GET",
            "/customers/customers_list",
            service="isp",
            settings=settings,
            region_name=region,
        )

    if not isinstance(data, list):
        return []

    normalised: List[Dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        payload = dict(entry)
        if "customer_id" not in payload and "id" in payload:
            payload["customer_id"] = payload["id"]
        normalised.append(payload)
    return normalised


async def get_customer(
    settings: "EnvConfig",
    customer_id: int,
    fetch_json,
) -> Dict[str, Any]:
    """
    Fetch a single ISP Cube customer by identifier.
    """
    client_kwargs, region = settings.http_client_kwargs("isp")
    async with httpx.AsyncClient(**client_kwargs) as client:
        return await fetch_json(
            client,
            "GET",
            "/customer",
            params={"customer_id": customer_id},
            service="isp",
            settings=settings,
            region_name=region,
        )


async def get_customer_by_code(
    settings: "EnvConfig",
    connection_code: str,
    fetch_json,
) -> Dict[str, Any]:
    client_kwargs, region = settings.http_client_kwargs("isp")
    async with httpx.AsyncClient(**client_kwargs) as client:
        return await fetch_json(
            client,
            "GET",
            "/customer",
            params={"code": connection_code},
            service="isp",
            settings=settings,
            region_name=region,
        )


def _sanitize_connection_payload(entry: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(entry)
    if "id" in payload:
        try:
            payload["id"] = int(payload["id"])
        except (TypeError, ValueError):
            pass
    if "customer_id" in payload:
        try:
            payload["customer_id"] = int(payload["customer_id"])
        except (TypeError, ValueError):
            pass
    if "plan" in payload and isinstance(payload["plan"], dict):
        payload["plan"] = dict(payload["plan"])
    if "node" in payload and isinstance(payload["node"], dict):
        payload["node"] = dict(payload["node"])
    resolved_code = payload.get("connection_code") or payload.get("oldcode") or payload.get("code")
    if resolved_code is not None:
        payload["connection_code"] = str(resolved_code).strip()
    return payload


async def query_connections(
    settings: "EnvConfig",
    fetch_json,
    *,
    connection_id: Optional[int] = None,
    customer_id: Optional[int] = None,
    code: Optional[str] = None,
    mac_address: Optional[str] = None,
    ip_address: Optional[str] = None,
    doc_number: Optional[str] = None,
) -> List[Dict[str, Any]]:
    params = {
        "connection_id": connection_id,
        "customer_id": customer_id,
        "code": code,
        "mac_address": mac_address,
        "ip_address": ip_address,
        "doc_number": doc_number,
    }
    filtered_params = {
        key: value
        for key, value in params.items()
        if value is not None and (not isinstance(value, str) or value.strip())
    }
    if not filtered_params:
        raise ValueError("Debe indicar al menos un parámetro para consultar conexiones")

    client_kwargs, region = settings.http_client_kwargs("isp")
    async with httpx.AsyncClient(**client_kwargs) as client:
        data = await fetch_json(
            client,
            "GET",
            "/connection",
            params=filtered_params,
            service="isp",
            settings=settings,
            region_name=region,
        )

    if isinstance(data, dict):
        return [_sanitize_connection_payload(data)]
    if isinstance(data, list):
        sanitized: List[Dict[str, Any]] = []
        for entry in data:
            if isinstance(entry, dict):
                sanitized.append(_sanitize_connection_payload(entry))
        return sanitized
    return []


async def get_connection_by_id(
    settings: "EnvConfig",
    *,
    connection_id: int,
    fetch_json,
) -> Optional[Dict[str, Any]]:
    entries = await query_connections(
        settings,
        fetch_json,
        connection_id=connection_id,
    )
    return entries[0] if entries else None


async def list_customer_connections(
    settings: "EnvConfig",
    *,
    customer_id: int,
    fetch_json,
) -> List[Dict[str, Any]]:
    return await query_connections(
        settings,
        fetch_json,
        customer_id=customer_id,
    )
