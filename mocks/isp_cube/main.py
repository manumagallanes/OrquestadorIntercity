import copy
import logging
import os
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s isp-cube :: %(message)s",
)
logger = logging.getLogger("isp_cube_mock")


class City(BaseModel):
    id: int
    name: str
    province: str
    postal_code: Optional[int] = None


class CustomerRecord(BaseModel):
    id: int
    code: str = Field(..., min_length=1)
    name: str
    doc_number: Optional[str] = None
    status: str = Field(default="enabled")
    lat: Optional[float] = None
    lng: Optional[float] = None
    address: str = Field(default="")
    zone: Optional[str] = None
    city: City
    integration_enabled: bool = Field(default=True)
    olt_id: Optional[int] = None
    board: Optional[int] = None
    pon: Optional[int] = None
    onu_sn: Optional[str] = None


INITIAL_CUSTOMERS: List[CustomerRecord] = [
    CustomerRecord(
        id=2142,
        code="003310",
        name="Juan Carlos Gonzalez",
        doc_number="99123123",
        status="enabled",
        lat=-31.416944,
        lng=-64.183333,
        address="Av. Colón 123",
        zone="Centro",
        city=City(id=1, name="Córdoba Capital", province="Córdoba"),
        integration_enabled=True,
        olt_id=1,
        board=1,
        pon=4,
        onu_sn="ZTEG12345678",
    ),
    CustomerRecord(
        id=2143,
        code="003311",
        name="María López",
        doc_number="30123123",
        status="enabled",
        lat=-31.42025,
        lng=-64.18875,
        address="Bv. San Juan 999",
        zone="Nueva Córdoba",
        city=City(id=2, name="Córdoba Capital", province="Córdoba"),
        integration_enabled=True,
        olt_id=1,
        board=2,
        pon=8,
        onu_sn="HWTC76543210",
    ),
    CustomerRecord(
        id=2144,
        code="003312",
        name="Empresa Intercity SRL",
        doc_number="30712345123",
        status="disabled",
        lat=-31.377,
        lng=-64.233,
        address="Av. Rafael Núñez 6000",
        zone="Villa Belgrano",
        city=City(id=3, name="Córdoba Capital", province="Córdoba"),
        integration_enabled=False,
        olt_id=2,
        board=1,
        pon=2,
        onu_sn="FHTT90213456",
    ),
]


def _build_indexes(customers: List[CustomerRecord]) -> Dict[str, Dict[str, int]]:
    by_code = {customer.code: customer.id for customer in customers}
    by_doc: Dict[str, int] = {}
    for record in customers:
        if record.doc_number:
            by_doc[record.doc_number] = record.id
    return {"code": by_code, "doc": by_doc}


CUSTOMERS: Dict[int, CustomerRecord] = {
    customer.id: customer.model_copy() for customer in INITIAL_CUSTOMERS
}
INDEXES = _build_indexes(list(CUSTOMERS.values()))


class CustomerUpdatePayload(BaseModel):
    integration_enabled: Optional[bool] = None
    status: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    address: Optional[str] = None
    zone: Optional[str] = None
    olt_id: Optional[int] = None
    board: Optional[int] = None
    pon: Optional[int] = None
    onu_sn: Optional[str] = None


def _match_customer(
    *,
    customer_id: Optional[int],
    code: Optional[str],
    doc_number: Optional[str],
) -> CustomerRecord:
    if customer_id is not None:
        customer = CUSTOMERS.get(customer_id)
        if customer:
            return customer
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": f"Customer {customer_id} not found"},
        )
    if code:
        customer_id = INDEXES["code"].get(code)
        if customer_id:
            return CUSTOMERS[customer_id]
    if doc_number:
        customer_id = INDEXES["doc"].get(doc_number)
        if customer_id:
            return CUSTOMERS[customer_id]
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "message": "Customer not found with provided identifiers",
            "code": code,
            "doc_number": doc_number,
        },
    )


def _serialize_customer_list_entry(customer: CustomerRecord) -> Dict[str, object]:
    return {
        "id": customer.id,
        "code": customer.code,
        "name": customer.name,
        "doc_number": customer.doc_number,
        "status": customer.status,
        "lat": customer.lat,
        "lng": customer.lng,
        "address": customer.address,
        "city": customer.city.model_dump(),
        "integration_enabled": customer.integration_enabled,
        "olt_id": customer.olt_id,
        "board": customer.board,
        "pon": customer.pon,
        "onu_sn": customer.onu_sn,
    }


def _serialize_customer_detail(customer: CustomerRecord) -> Dict[str, object]:
    payload = _serialize_customer_list_entry(customer)
    payload["zone"] = customer.zone
    payload["city"]["postal_code"] = customer.city.postal_code
    return payload


app = FastAPI(
    title="ISP Cube Mock",
    description="Simulación básica de la API pública de ISPCube (clientes)",
    version="0.2.0",
)


@app.get(
    "/api/customers/customers_list",
    summary="Listado de clientes",
)
async def list_customers(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    doc_number: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
):
    logger.info(
        "GET /api/customers/customers_list?limit=%s&offset=%s&doc_number=%s&status=%s",
        limit,
        offset,
        doc_number,
        status_filter,
    )
    customers = list(CUSTOMERS.values())
    if doc_number:
        customers = [
            customer for customer in customers if customer.doc_number == doc_number
        ]
    if status_filter:
        customers = [
            customer for customer in customers if customer.status == status_filter
        ]
    slice_start = offset
    slice_end = offset + limit
    selected = customers[slice_start:slice_end]
    return [_serialize_customer_list_entry(customer) for customer in selected]


@app.get("/api/customer", summary="Detalle de cliente")
async def get_customer(
    customer_id: Optional[int] = Query(default=None, ge=1),
    code: Optional[str] = Query(default=None),
    doc_number: Optional[str] = Query(default=None),
):
    logger.info(
        "GET /api/customer?customer_id=%s&code=%s&doc_number=%s",
        customer_id,
        code,
        doc_number,
    )
    if customer_id is None and not code and not doc_number:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "Provide customer_id, code or doc_number"},
        )
    customer = _match_customer(
        customer_id=customer_id,
        code=code,
        doc_number=doc_number,
    )
    return _serialize_customer_detail(customer)


@app.put("/api/customers/{customer_id}", summary="Actualizar datos de cliente")
async def update_customer(customer_id: int, payload: CustomerUpdatePayload):
    global INDEXES
    logger.info("PUT /api/customers/%s -> %s", customer_id, payload.model_dump())
    customer = CUSTOMERS.get(customer_id)
    if not customer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": f"Customer {customer_id} not found"},
        )
    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(customer, key, value)
    CUSTOMERS[customer_id] = customer
    global INDEXES
    if "code" in update_data or "doc_number" in update_data:
        INDEXES = _build_indexes(list(CUSTOMERS.values()))
    return {"status": "updated", "customer": _serialize_customer_detail(customer)}


@app.post("/reset", summary="Reinicia el estado del mock")
async def reset():
    global INDEXES
    logger.info("POST /reset -> reseteando dataset")
    CUSTOMERS.clear()
    for customer in copy.deepcopy(INITIAL_CUSTOMERS):
        CUSTOMERS[customer.id] = customer
    INDEXES = _build_indexes(list(CUSTOMERS.values()))
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
