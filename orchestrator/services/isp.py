from __future__ import annotations

from typing import Any, Dict, List, TYPE_CHECKING

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

