import copy
import logging
import os
from typing import Any, Dict, List, Optional

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


class PlanRecord(BaseModel):
    id: int
    name: str
    price: float
    tags_ids: List[str] = Field(default_factory=lambda: ["[]"])
    tags: List[Dict[str, Any]] = Field(default_factory=list)


class NodeRecord(BaseModel):
    id: int
    code: str
    type: str
    comment: Optional[str] = None
    ip: Optional[str] = None
    tags_ids: List[str] = Field(default_factory=list)
    tags: List[Dict[str, Any]] = Field(default_factory=list)


class ConnectionRecord(BaseModel):
    id: int
    customer_id: int
    oldcode: str
    plan_id: int
    node_id: int
    conntype: str = Field(default="ftth")
    olt_id: Optional[int] = None
    board: Optional[int] = None
    pon: Optional[int] = None
    ftthbox_id: Optional[int] = None
    ftth_port_id: Optional[int] = None
    address: str = Field(default="")
    lat: Optional[float] = None
    lng: Optional[float] = None
    user: Optional[str] = None
    password: Optional[str] = None
    plan: PlanRecord
    node: NodeRecord
    ip: Optional[str] = None
    local_ip: Optional[str] = None


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

PLAN_CATALOG: Dict[int, PlanRecord] = {
    100: PlanRecord(id=100, name="Hogar 50M", price=3500.0),
    200: PlanRecord(id=200, name="Hogar 100M", price=5200.0),
    300: PlanRecord(id=300, name="Pyme 300M", price=9800.0),
}

NODE_CATALOG: Dict[int, NodeRecord] = {
    10: NodeRecord(id=10, code="OLT-1", type="OLT", comment="OLT Central", ip="10.10.0.10"),
    20: NodeRecord(id=20, code="OLT-2", type="OLT", comment="OLT Norte", ip="10.10.0.20"),
}

INITIAL_CONNECTIONS: List[ConnectionRecord] = [
    ConnectionRecord(
        id=7001,
        customer_id=2142,
        oldcode="CNX-003310-A",
        plan_id=100,
        node_id=10,
        plan=PLAN_CATALOG[100].model_copy(),
        node=NODE_CATALOG[10].model_copy(),
        olt_id=1,
        board=1,
        pon=4,
        ftthbox_id=501,
        ftth_port_id=8,
        address="Av. Colón 123",
        lat=-31.416944,
        lng=-64.183333,
        user="pppoe-003310-a",
        password="Secr3t003310A",
    ),
    ConnectionRecord(
        id=7002,
        customer_id=2142,
        oldcode="CNX-003310-B",
        plan_id=200,
        node_id=10,
        plan=PLAN_CATALOG[200].model_copy(),
        node=NODE_CATALOG[10].model_copy(),
        olt_id=1,
        board=1,
        pon=5,
        ftthbox_id=501,
        ftth_port_id=9,
        address="Av. Colón 123",
        lat=-31.416944,
        lng=-64.183333,
        user="pppoe-003310-b",
        password="Secr3t003310B",
    ),
    ConnectionRecord(
        id=7101,
        customer_id=2143,
        oldcode="CNX-003311-A",
        plan_id=200,
        node_id=20,
        plan=PLAN_CATALOG[200].model_copy(),
        node=NODE_CATALOG[20].model_copy(),
        olt_id=1,
        board=2,
        pon=8,
        ftthbox_id=601,
        ftth_port_id=3,
        address="Bv. San Juan 999",
        lat=-31.42025,
        lng=-64.18875,
        user="pppoe-003311",
        password="Secr3t003311",
    ),
    ConnectionRecord(
        id=7201,
        customer_id=2144,
        oldcode="CNX-003312-A",
        plan_id=300,
        node_id=20,
        plan=PLAN_CATALOG[300].model_copy(),
        node=NODE_CATALOG[20].model_copy(),
        olt_id=2,
        board=1,
        pon=2,
        ftthbox_id=701,
        ftth_port_id=5,
        address="Av. Rafael Núñez 6000",
        lat=-31.377,
        lng=-64.233,
        user="pppoe-003312",
        password="Secr3t003312",
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


def _build_customer_connection_index(
    connections: Dict[int, ConnectionRecord]
) -> Dict[int, List[int]]:
    mapping: Dict[int, List[int]] = {}
    for connection in connections.values():
        mapping.setdefault(connection.customer_id, []).append(connection.id)
    return mapping


CONNECTIONS: Dict[int, ConnectionRecord] = {
    connection.id: connection.model_copy() for connection in INITIAL_CONNECTIONS
}
CUSTOMER_CONNECTIONS: Dict[int, List[int]] = _build_customer_connection_index(CONNECTIONS)


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


def _serialize_connection_reference(connection: ConnectionRecord) -> Dict[str, object]:
    return {
        "id": connection.id,
        "connection_code": connection.oldcode,
        "plan_id": connection.plan_id,
        "node_id": connection.node_id,
        "ftthbox_id": connection.ftthbox_id,
        "ftth_port_id": connection.ftth_port_id,
        "olt_id": connection.olt_id,
        "board": connection.board,
        "pon": connection.pon,
        "lat": connection.lat,
        "lng": connection.lng,
        "address": connection.address,
        "user": connection.user,
        "password": connection.password,
    }


def _serialize_connection_detail(connection: ConnectionRecord) -> Dict[str, object]:
    customer = CUSTOMERS.get(connection.customer_id)
    payload = {
        "id": connection.id,
        "ip": connection.ip,
        "local_ip": connection.local_ip,
        "conntype": connection.conntype,
        "customer_id": connection.customer_id,
        "plan_id": connection.plan_id,
        "node_id": connection.node_id,
        "olt_id": connection.olt_id,
        "board": connection.board,
        "pon": connection.pon,
        "ftthbox_id": connection.ftthbox_id,
        "ftth_port_id": connection.ftth_port_id,
        "address": connection.address,
        "lat": f"{connection.lat:.12f}" if connection.lat is not None else None,
        "lng": f"{connection.lng:.12f}" if connection.lng is not None else None,
        "user": connection.user,
        "password": connection.password,
        "oldcode": connection.oldcode,
        "connection_code": connection.oldcode,
        "plan": connection.plan.model_dump(),
        "node": connection.node.model_dump(),
        "customer": {
            "id": customer.id,
            "code": customer.code,
            "name": customer.name,
        }
        if customer
        else None,
    }
    return payload


def _serialize_customer_detail(customer: CustomerRecord) -> Dict[str, object]:
    payload = _serialize_customer_list_entry(customer)
    payload["zone"] = customer.zone
    payload["city"]["postal_code"] = customer.city.postal_code
    payload["connections"] = [
        _serialize_connection_reference(CONNECTIONS[conn_id])
        for conn_id in CUSTOMER_CONNECTIONS.get(customer.id, [])
        if conn_id in CONNECTIONS
    ]
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


@app.get("/api/connections/connections_list", summary="Listado de conexiones")
async def list_connections(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    customer_id: Optional[int] = Query(default=None, ge=1),
    plan_id: Optional[int] = Query(default=None, ge=1),
):
    logger.info(
        "GET /api/connections/connections_list?limit=%s&offset=%s&customer_id=%s&plan_id=%s",
        limit,
        offset,
        customer_id,
        plan_id,
    )
    connections = list(CONNECTIONS.values())
    if customer_id is not None:
        connections = [conn for conn in connections if conn.customer_id == customer_id]
    if plan_id is not None:
        connections = [conn for conn in connections if conn.plan_id == plan_id]
    slice_start = offset
    slice_end = offset + limit
    selected = connections[slice_start:slice_end]
    return [_serialize_connection_detail(conn) for conn in selected]


@app.get("/api/connection", summary="Detalle de conexión")
async def get_connection(
    connection_id: Optional[int] = Query(default=None, ge=1),
    customer_id: Optional[int] = Query(default=None, ge=1),
    code: Optional[str] = Query(default=None),
    mac_address: Optional[str] = Query(default=None),
    ip_address: Optional[str] = Query(default=None),
    doc_number: Optional[str] = Query(default=None),
):
    logger.info(
        "GET /api/connection?connection_id=%s&customer_id=%s&code=%s",
        connection_id,
        customer_id,
        code,
    )
    if connection_id is None and customer_id is None and not code and not doc_number:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "Debe indicar algún parámetro para buscar una conexión"},
        )
    connections = list(CONNECTIONS.values())
    if connection_id is not None:
        connections = [conn for conn in connections if conn.id == connection_id]
    if customer_id is not None:
        connections = [conn for conn in connections if conn.customer_id == customer_id]
    if code:
        token = code.strip()
        connections = [
            conn
            for conn in connections
            if conn.oldcode == token
            or (CUSTOMERS.get(conn.customer_id) and CUSTOMERS[conn.customer_id].code == token)
        ]
    if doc_number:
        connections = [
            conn
            for conn in connections
            if CUSTOMERS.get(conn.customer_id)
            and CUSTOMERS[conn.customer_id].doc_number == doc_number
        ]
    if not connections:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": "Connection not found"},
        )
    return [_serialize_connection_detail(conn) for conn in connections]


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
    global INDEXES, CONNECTIONS, CUSTOMER_CONNECTIONS
    logger.info("POST /reset -> reseteando dataset")
    CUSTOMERS.clear()
    for customer in copy.deepcopy(INITIAL_CUSTOMERS):
        CUSTOMERS[customer.id] = customer
    INDEXES = _build_indexes(list(CUSTOMERS.values()))
    CONNECTIONS.clear()
    for connection in copy.deepcopy(INITIAL_CONNECTIONS):
        CONNECTIONS[connection.id] = connection
    CUSTOMER_CONNECTIONS = _build_customer_connection_index(CONNECTIONS)
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
