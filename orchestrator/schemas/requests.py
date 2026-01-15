from datetime import datetime
from typing import Any, Dict, List, Optional
from typing_extensions import Literal

from pydantic import BaseModel, Field, model_validator

class CustomerSyncRequest(BaseModel):
    customer_id: Optional[int] = Field(
        default=None, 
        ge=1, 
        description="Identificador numérico del cliente en el CRM (ISP-Cube).",
        examples=[10245]
    )
    customer_name: Optional[str] = Field(
        default=None, 
        description="Nombre del cliente para logging (opcional).",
        examples=["Juan Perez"]
    )
    connection_code: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Código de conexión alfanumérico en ISP-Cube (campo 'code').",
        examples=["CON-2024-889"]
    )
    connection_id: Optional[int] = Field(
        default=None,
        ge=1,
        description="ID único interno de la conexión técnica en ISP-Cube.",
        examples=[5501]
    )

    @model_validator(mode="after")
    def validate_identifier(self):
        if (
            self.customer_id is None
            and self.connection_code is None
            and self.connection_id is None
        ):
            raise ValueError("Debe indicar customer_id, connection_id o connection_code")
        return self


class ProvisionRequest(BaseModel):
    customer_id: Optional[int] = Field(default=None, ge=1, examples=[10245])
    olt_id: int = Field(..., ge=1, description="ID de la OLT en GeoGrid.", examples=[1])
    board: int = Field(..., ge=0, description="Número de placa/tarjeta en la OLT.", examples=[1])
    pon_port: int = Field(..., ge=0, description="Número de puerto PON.", examples=[4])
    onu_sn: str = Field(..., min_length=6, max_length=32, description="Número de serie de la ONU (formato Vendor+Hex).", examples=["HWTC1234ABCD"])
    profile: Optional[str] = Field(default="Internet_100M", description="Perfil de velocidad a asignar.", examples=["Plan_300M_Fibra"])
    dry_run: Optional[bool] = Field(
        default=None,
        description="Si es True, simula la operación sin realizar cambios (Modo Prueba).",
    )
    connection_code: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Código de conexión en ISP-Cube.",
        examples=["CON-2024-889"]
    )
    connection_id: Optional[int] = Field(
        default=None,
        ge=1,
        description="ID interno de la conexión en ISP-Cube.",
        examples=[5501]
    )

    @model_validator(mode="after")
    def validate_identifier(self):
        if (
            self.customer_id is None
            and self.connection_code is None
            and self.connection_id is None
        ):
            raise ValueError("Debe indicar customer_id, connection_id o connection_code")
        return self


class GeoGridPoint(BaseModel):
    latitude: float
    longitude: float


class GeoGridAttendRequest(BaseModel):
    codigo_integracion: str = Field(..., min_length=1)
    id_porta: Optional[int] = Field(default=None, ge=1)
    geogrid_caja_sigla: Optional[str] = Field(default=None, min_length=1)
    geogrid_porta_num: Optional[int] = Field(default=None, ge=1)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    id_item_rede_cliente: Optional[int] = Field(default=None, ge=1)
    id_cabo_tipo: Optional[int] = Field(default=None, ge=1)
    pontos: Optional[List[GeoGridPoint]] = None

    @model_validator(mode="after")
    def validate_port_reference(self):
        if self.id_porta is None:
            if not self.geogrid_caja_sigla or self.geogrid_porta_num is None:
                raise ValueError("Debe indicar id_porta o (geogrid_caja_sigla y geogrid_porta_num)")
        return self


class DecommissionRequest(BaseModel):
    customer_id: Optional[int] = Field(default=None, ge=1)
    connection_code: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Código de conexión en ISP-Cube",
    )
    dry_run: Optional[bool] = Field(default=None)
    connection_id: Optional[int] = Field(
        default=None,
        ge=1,
        description="ID interno de la conexión en ISP-Cube",
    )

    @model_validator(mode="after")
    def validate_identifier(self):
        if (
            self.customer_id is None
            and self.connection_code is None
            and self.connection_id is None
        ):
            raise ValueError("Debe indicar customer_id, connection_id o connection_code")
        return self


class ConfigUpdateRequest(BaseModel):
    dry_run: Optional[bool] = Field(
        default=None, description="Override runtime dry-run flag"
    )


class AuditEntry(BaseModel):
    action: Literal["sync", "provision", "decommission"]
    customer_id: Optional[int] = None
    user: Optional[str] = None
    dry_run: Optional[bool] = None
    status: Literal["success", "error"]
    detail: Dict[str, Any] = Field(default_factory=dict)
    timestamp: str


class CustomerEventRequest(BaseModel):
    event_type: Literal["alta", "baja"]
    customer_id: Optional[int] = Field(default=None, ge=1)
    zone: Optional[str] = Field(default=None, min_length=1)
    city: Optional[str] = None
    lat: Optional[float] = Field(default=None)
    lon: Optional[float] = Field(default=None)
    timestamp: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    source: Optional[str] = Field(default="manual")
