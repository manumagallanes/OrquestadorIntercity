import logging
import os
import time
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Path, Query, status
from pydantic import BaseModel, Field


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s geogrid :: %(message)s",
)
logger = logging.getLogger("geogrid_mock")


class ClientePayload(BaseModel):
    codigoIntegracao: str = Field(..., min_length=1)
    nome: str = Field(..., min_length=1)
    endereco: str = Field(..., min_length=1)
    cidade: str = Field(..., min_length=1)
    estado: str = Field(..., min_length=1)
    latitude: float
    longitude: float
    observacao: Optional[str] = None


class ClienteResponse(ClientePayload):
    id: int
    ativo: bool = True
    criadoEm: float
    atualizadoEm: float


class PortAssignmentPayload(BaseModel):
    idCliente: int = Field(..., ge=1)
    idPorta: str = Field(..., min_length=3)
    oltId: Optional[int] = None
    board: Optional[int] = None
    pon: Optional[int] = None
    onuSerial: Optional[str] = Field(default=None, alias="onuSerial")
    observacao: Optional[str] = None


class GeoGridStore:
    def __init__(self) -> None:
        self._clientes: Dict[int, ClienteResponse] = {}
        self._by_codigo: Dict[str, int] = {}
        self._assignments: Dict[str, Dict[str, object]] = {}
        self._cliente_seq = 1
        self._bootstrap()

    def _bootstrap(self) -> None:
        self.reset()
        initial_clientes: List[ClientePayload] = [
            ClientePayload(
                codigoIntegracao="003310",
                nome="Juan Carlos Gonzalez",
                endereco="Av. Colón 123",
                cidade="Córdoba Capital",
                estado="Córdoba",
                latitude=-31.416944,
                longitude=-64.183333,
                observacao="Cliente preexistente en GeoGrid",
            )
        ]
        for cliente in initial_clientes:
            self.create_cliente(cliente)

    def _allocate_id(self) -> int:
        next_id = self._cliente_seq
        self._cliente_seq += 1
        return next_id

    def reset(self) -> None:
        logger.info("Reseteando GeoGrid store")
        self._clientes.clear()
        self._by_codigo.clear()
        self._assignments.clear()
        self._cliente_seq = 1

    def create_cliente(self, payload: ClientePayload) -> ClienteResponse:
        codigo = payload.codigoIntegracao.lower()
        if codigo in self._by_codigo:
            existing_id = self._by_codigo[codigo]
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "Cliente ya existe con este codigoIntegracao",
                    "id": existing_id,
                },
            )
        now = time.time()
        cliente = ClienteResponse(
            id=self._allocate_id(),
            criadoEm=now,
            atualizadoEm=now,
            **payload.model_dump(),
        )
        self._clientes[cliente.id] = cliente
        self._by_codigo[codigo] = cliente.id
        return cliente

    def update_cliente(self, cliente_id: int, payload: ClientePayload) -> ClienteResponse:
        cliente = self._clientes.get(cliente_id)
        if not cliente:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"message": f"Cliente {cliente_id} no encontrado"},
            )
        codigo = payload.codigoIntegracao.lower()
        existing_id = self._by_codigo.get(codigo)
        if existing_id and existing_id != cliente_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "codigoIntegracao ya está asociado a otro cliente",
                    "id": existing_id,
                },
            )
        for key, value in payload.model_dump().items():
            setattr(cliente, key, value)
        cliente.atualizadoEm = time.time()
        self._clientes[cliente_id] = cliente
        self._by_codigo[codigo] = cliente_id
        return cliente

    def get_cliente(self, cliente_id: int) -> ClienteResponse:
        cliente = self._clientes.get(cliente_id)
        if not cliente:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"message": f"Cliente {cliente_id} no encontrado"},
            )
        return cliente

    def find_by_codigo(self, codigo_integracao: str) -> Optional[ClienteResponse]:
        cliente_id = self._by_codigo.get(codigo_integracao.lower())
        if cliente_id is None:
            return None
        return self._clientes.get(cliente_id)

    def list_clientes(
        self,
        codigo_integracao: Optional[str] = None,
    ) -> List[ClienteResponse]:
        if codigo_integracao:
            cliente = self.find_by_codigo(codigo_integracao)
            return [cliente] if cliente else []
        return list(self._clientes.values())

    def assign_port(self, payload: PortAssignmentPayload) -> Dict[str, object]:
        cliente = self.get_cliente(payload.idCliente)
        port_key = payload.idPorta.lower()
        existing = self._assignments.get(port_key)
        if existing and existing["idCliente"] != cliente.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "La puerta ya está asignada a otro cliente",
                    "port": payload.idPorta,
                    "cliente_id": existing["idCliente"],
                },
            )
        assignment = {
            "idCliente": cliente.id,
            "idPorta": payload.idPorta,
            "oltId": payload.oltId,
            "board": payload.board,
            "pon": payload.pon,
            "onuSerial": payload.onuSerial,
            "observacao": payload.observacao,
            "creadoEm": time.time(),
        }
        self._assignments[port_key] = assignment
        return assignment

    def remove_assignment(self, id_porta: str, id_cliente: int) -> None:
        port_key = id_porta.lower()
        existing = self._assignments.get(port_key)
        if not existing or existing["idCliente"] != id_cliente:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "message": "Asignación no encontrada",
                    "idPorta": id_porta,
                    "idCliente": id_cliente,
                },
            )
        del self._assignments[port_key]

    def list_assignments(self) -> List[Dict[str, object]]:
        return list(self._assignments.values())


store = GeoGridStore()

app = FastAPI(
    title="GeoGrid Mock",
    description="Mock de la API pública de GeoGrid (v3)",
    version="0.2.0",
)


def get_store() -> GeoGridStore:
    return store


def _serialize_cliente(cliente: ClienteResponse) -> Dict[str, object]:
    data = cliente.model_dump()
    data["assignments"] = [
        assignment
        for assignment in store.list_assignments()
        if assignment["idCliente"] == cliente.id
    ]
    return data


@app.get("/api/v3/clientes")
async def list_clientes(
    codigoIntegracao: Optional[str] = Query(default=None),
    store: GeoGridStore = Depends(get_store),
):
    logger.info("GET /api/v3/clientes?codigoIntegracao=%s", codigoIntegracao)
    clientes = store.list_clientes(codigo_integracao=codigoIntegracao)
    return {
        "dados": [_serialize_cliente(cliente) for cliente in clientes],
        "quantidade": len(clientes),
    }


@app.get("/api/v3/clientes/{cliente_id}")
async def get_cliente(
    cliente_id: int = Path(..., ge=1),
    store: GeoGridStore = Depends(get_store),
):
    logger.info("GET /api/v3/clientes/%s", cliente_id)
    cliente = store.get_cliente(cliente_id)
    return {"dados": _serialize_cliente(cliente)}


@app.post("/api/v3/clientes", status_code=status.HTTP_201_CREATED)
async def create_cliente(
    payload: Dict[str, ClientePayload],
    store: GeoGridStore = Depends(get_store),
):
    logger.info("POST /api/v3/clientes -> %s", payload)
    if "dados" not in payload:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "Payload debe contener la clave 'dados'"},
        )
    cliente = store.create_cliente(payload["dados"])
    return {"dados": _serialize_cliente(cliente)}


@app.put("/api/v3/clientes/{cliente_id}")
async def update_cliente(
    cliente_id: int,
    payload: Dict[str, ClientePayload],
    store: GeoGridStore = Depends(get_store),
):
    logger.info("PUT /api/v3/clientes/%s -> %s", cliente_id, payload)
    if "dados" not in payload:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "Payload debe contener la clave 'dados'"},
        )
    cliente = store.update_cliente(cliente_id, payload["dados"])
    return {"dados": _serialize_cliente(cliente)}


@app.post("/api/v3/integracao/atender")
async def atender(
    payload: PortAssignmentPayload,
    store: GeoGridStore = Depends(get_store),
):
    logger.info("POST /api/v3/integracao/atender -> %s", payload.model_dump())
    assignment = store.assign_port(payload)
    return {"dados": assignment}


@app.delete("/api/v3/integracao/atender/{id_porta}/{id_cliente}")
async def remover_atendimento(
    id_porta: str,
    id_cliente: int,
    store: GeoGridStore = Depends(get_store),
):
    logger.info(
        "DELETE /api/v3/integracao/atender/%s/%s",
        id_porta,
        id_cliente,
    )
    store.remove_assignment(id_porta, id_cliente)
    return {"status": "removed"}


@app.post("/reset")
async def reset(store: GeoGridStore = Depends(get_store)):
    logger.info("POST /reset")
    store._bootstrap()
    return {"status": "reset"}


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "mocks.geogrid.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8002")),
        reload=bool(int(os.getenv("UVICORN_RELOAD", "0"))),
    )
