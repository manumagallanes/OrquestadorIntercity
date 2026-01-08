from datetime import datetime
from typing import Any, Dict, List, Optional
from typing_extensions import Literal

from pydantic import BaseModel, Field, model_validator

class CustomerSyncRequest(BaseModel):
    customer_id: Optional[int] = Field(
        default=None, ge=1, description="Identifier of the ISP customer"
    )
    customer_name: Optional[str] = Field(default=None, description="Nombre del cliente (opcional)")
    connection_code: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Código de conexión en ISP-Cube (campo code)",
    )
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


class ProvisionRequest(BaseModel):
    customer_id: Optional[int] = Field(default=None, ge=1)
    olt_id: int = Field(..., ge=1)
    board: int = Field(..., ge=0)
    pon_port: int = Field(..., ge=0)
    onu_sn: str = Field(..., min_length=6, max_length=32)
    profile: Optional[str] = Field(default="Internet_100M")
    dry_run: Optional[bool] = Field(
        default=None,
        description="Override orchestrator dry-run flag when provided",
    )
    connection_code: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Código de conexión en ISP-Cube",
    )
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
