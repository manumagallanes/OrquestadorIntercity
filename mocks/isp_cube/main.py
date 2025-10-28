import copy
import logging
import os
from typing import Dict, List, Literal, Optional

from fastapi import FastAPI, HTTPException, Path, Query, status
from pydantic import BaseModel, Field


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s isp-cube :: %(message)s",
)
logger = logging.getLogger("isp_cube_mock")


INITIAL_CUSTOMERS: Dict[int, Dict[str, object]] = {}

CUSTOMERS: Dict[int, Dict[str, object]] = copy.deepcopy(INITIAL_CUSTOMERS)


class CustomerPayload(BaseModel):
    customer_id: int = Field(..., ge=1)
    name: str
    address: str
    city: str
    zone: str = Field(..., min_length=1)
    lat: Optional[float] = Field(default=None)
    lon: Optional[float] = Field(default=None)
    odb: str
    olt_id: int = Field(..., ge=1)
    board: int = Field(..., ge=0)
    pon: int = Field(..., ge=0)
    onu_sn: str = Field(..., min_length=6)
    integration_enabled: bool = Field(default=False)
    status: Literal["active", "inactive"] = Field(default="active")


class FlagPayload(BaseModel):
    integration_enabled: bool


class StatusPayload(BaseModel):
    status: Literal["active", "inactive"]


app = FastAPI(
    title="ISP Cube Mock",
    description="Minimal ISP Cube simulation with synthetic customers",
    version="0.1.0",
)


@app.get("/customers/{customer_id}")
async def get_customer(
    customer_id: int = Path(..., ge=1),
    simulate: str | None = Query(
        default=None,
        description="Use for testing error handling. Options: 404, 429.",
    ),
):
    logger.info("GET /customers/%s simulate=%s", customer_id, simulate)

    if simulate == "429":
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"message": "Rate limit exceeded in ISP Cube mock"},
        )

    customer = CUSTOMERS.get(customer_id)
    if simulate == "404" or customer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": f"Customer {customer_id} not found"},
        )

    return customer


@app.get("/customers")
async def list_customers():
    logger.info("GET /customers")
    return list(CUSTOMERS.values())


@app.post("/customers", status_code=status.HTTP_201_CREATED)
async def create_customer(payload: CustomerPayload):
    logger.info("POST /customers customer_id=%s", payload.customer_id)
    if payload.customer_id in CUSTOMERS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": "Customer already exists"},
        )
    CUSTOMERS[payload.customer_id] = payload.model_dump()
    return {"status": "created"}


@app.put("/customers/{customer_id}")
async def replace_customer(customer_id: int, payload: CustomerPayload):
    logger.info("PUT /customers/%s", customer_id)
    if customer_id != payload.customer_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "customer_id in body must match URL"},
        )
    if customer_id not in CUSTOMERS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": f"Customer {customer_id} not found"},
        )
    CUSTOMERS[customer_id] = payload.model_dump()
    return {"status": "updated"}


@app.patch("/customers/{customer_id}/flag")
async def toggle_integration(customer_id: int, payload: FlagPayload):
    logger.info("PATCH /customers/%s/flag -> %s", customer_id, payload.integration_enabled)
    customer = CUSTOMERS.get(customer_id)
    if not customer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": f"Customer {customer_id} not found"},
        )
    customer["integration_enabled"] = payload.integration_enabled
    return {"status": "updated", "integration_enabled": customer["integration_enabled"]}


@app.patch("/customers/{customer_id}/status")
async def update_status(customer_id: int, payload: StatusPayload):
    logger.info("PATCH /customers/%s/status -> %s", customer_id, payload.status)
    customer = CUSTOMERS.get(customer_id)
    if not customer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": f"Customer {customer_id} not found"},
        )
    customer["status"] = payload.status
    return {"status": "updated", "customer_status": customer["status"]}


@app.get("/customers/pending")
async def list_pending():
    pending: List[Dict[str, object]] = [
        customer
        for customer in CUSTOMERS.values()
        if customer.get("status") == "active" and not customer.get("integration_enabled")
    ]
    logger.info("GET /customers/pending -> %s", len(pending))
    return pending


def reset_state() -> None:
    global CUSTOMERS
    CUSTOMERS = copy.deepcopy(INITIAL_CUSTOMERS)
    logger.info("State reset to initial dataset")


@app.post("/reset")
async def reset():
    reset_state()
    return {"status": "reset"}


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "mocks.isp_cube.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8001")),
        reload=bool(int(os.getenv("UVICORN_RELOAD", "0"))),
    )
