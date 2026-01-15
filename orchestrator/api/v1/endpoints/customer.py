import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4
import httpx

from fastapi import APIRouter, Depends, HTTPException, Request, status

from orchestrator.core.config import (
    EnvConfig,
    get_settings,
    get_runtime_state,
    RuntimeState,
    APP_USER_HEADER,
    connection_ctx,
)
from orchestrator.core.integration import fetch_json
from orchestrator.core.state import (
    SYNC_COUNTER,
    PROVISION_COUNTER,
    DECOMMISSION_COUNTER,
    AUTO_GEOGRID_ATTEND,
)
from orchestrator.core.audit import record_incident, resolve_incidents, record_audit
from orchestrator.logic.domain import (
    ensure_customer_ready,
    ensure_customer_has_network_keys,
    ensure_alignment,
    ensure_customer_inactive,
    _connection_metadata_snapshot,
    _inject_connection_context,
    _customer_connection_identifier,
    _customer_connection_code,
    extract_geogrid_box_sigla,
    extract_geogrid_port_number,
    _customer_coordinates_strict,
    build_geogrid_cliente_payload,
    fetch_customer_record,
    resolve_customer_id,
    _resolve_geogrid_error_detail,
)
from orchestrator.logic.reporting import register_customer_event
from orchestrator.schemas.requests import (
    CustomerSyncRequest,
    ProvisionRequest,
    GeoGridAttendRequest,
    DecommissionRequest,
    AuditEntry,
)
from orchestrator.services import isp as isp_service
from orchestrator.services import geogrid as geogrid_service

logger = logging.getLogger("orchestrator.api.customer")
router = APIRouter()


@router.post(
    "/sync/customer",
    status_code=status.HTTP_200_OK,
    summary="Sincronizar Cliente (IMS/CRM -> GeoGrid)",
    response_description="Resultado de la sincronización con IDs de GeoGrid.",
)
async def sync_customer(
    payload: CustomerSyncRequest,
    request: Request,
    settings: EnvConfig = Depends(get_settings),
) -> Dict[str, Any]:
    """
    **Sincroniza un cliente desde el sistema comercial (ISP-Cube) hacia el sistema técnico (GeoGrid).**

    Flujo de ejecución:
    1.  **Recupera** los datos del cliente desde ISP-Cube usando el ID provisto.
    2.  **Valida** la integridad de los datos (coordenadas, plan, estado).
    3.  **Sanitiza** coordenadas geográficas si es necesario (Self-Healing).
    4.  **Busca/Crea** el cliente en GeoGrid.
    5.  Si corresponde, **Asigna** el puerto automático (Attend).

    En caso de error (ej. GeoGrid caído), el sistema registra un incidente pero no bloquea futuras operaciones.
    """
    user = request.headers.get(APP_USER_HEADER, "ui")
    timestamp = datetime.now(timezone.utc).isoformat()
    resolved_customer_id: Optional[int] = payload.customer_id
    ctx_token = connection_ctx.set(
        {
            "connection_id": payload.connection_id,
            "connection_code": payload.connection_code,
            "customer_name": payload.customer_name,
        }
    )
    try:
        logger.info(
            "Sync request context connection_id=%s connection_code=%s",
            payload.connection_id,
            payload.connection_code,
        )
        try:
            customer = await fetch_customer_record(
                settings,
                customer_id=payload.customer_id,
                connection_code=payload.connection_code,
                connection_id=payload.connection_id,
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                logger.warning("Conexión %s no encontrada en ISP (404). Buscando en historial local...", payload.connection_id)
                
                # Buscar en memoria y SQLite por connection_id
                from orchestrator.core.state import LATEST_CUSTOMER_EVENTS
                from orchestrator.persistence import persistence_store
                
                target_event = None
                
                # 1. Buscar en memoria
                for evt in LATEST_CUSTOMER_EVENTS.values():
                    meta = evt.get("metadata", {})
                    if str(meta.get("connection_id")) == str(payload.connection_id):
                        # Preferir eventos con customer_name o connection_code
                        if meta.get("customer_name") or meta.get("connection_code"):
                            target_event = evt
                            break
                        elif not target_event:
                            target_event = evt
                
                # 2. Si no está en memoria o no tiene datos completos, buscar en SQLite
                if not target_event or (not target_event.get("metadata", {}).get("customer_name") and not target_event.get("metadata", {}).get("connection_code")):
                    all_events = persistence_store.load_customer_events()
                    for evt in all_events:
                        meta = evt.get("metadata", {})
                        if str(meta.get("connection_id")) == str(payload.connection_id):
                            # Preferir eventos con datos completos
                            if meta.get("customer_name") or meta.get("connection_code"):
                                target_event = evt
                                break
                            elif not target_event:
                                target_event = evt
                
                if target_event:
                    customer_name = target_event.get("metadata", {}).get("customer_name") or target_event.get("customer_name") or "Cliente"
                    connection_code = target_event.get("metadata", {}).get("connection_code")
                    
                    logger.info("Recuperado cliente '%s' (conn=%s) del historial. Ejecutando marcado BAJA...", 
                               customer_name, payload.connection_id)
                    
                    # Buscar el cliente en GeoGrid (intentar por código y por nombre)
                    try:
                        geogrid_cliente = None
                        
                        # 1. Intentar por connection_code
                        if connection_code:
                            geogrid_cliente = await geogrid_service.get_cliente_by_codigo(
                                settings, connection_code, fetch_json
                            )
                        
                        # 2. Intentar por connection_id como código
                        if not geogrid_cliente and payload.connection_id:
                            geogrid_cliente = await geogrid_service.get_cliente_by_codigo(
                                settings, str(payload.connection_id), fetch_json
                            )
                        
                        # 3. Intentar por nombre del cliente (búsqueda parcial)
                        if not geogrid_cliente and customer_name and customer_name != "Cliente":
                            # Buscar por nombre usando query general
                            client_kwargs, region = settings.http_client_kwargs("geogrid")
                            async with httpx.AsyncClient(**client_kwargs) as client:
                                resp = await fetch_json(
                                    client, "GET", "/clientes",
                                    params={"q": customer_name},
                                    service="geogrid", settings=settings, region_name=region
                                )
                                # Manejar estructura {"registros": [{"dados": {...}}]} o {"dados": [...]}
                                if isinstance(resp, dict):
                                    if "registros" in resp:
                                        registros = resp.get("registros", [])
                                        if isinstance(registros, list) and registros:
                                            geogrid_cliente = registros[0].get("dados")
                                            logger.info("Cliente encontrado por nombre (en registros): %s", customer_name)
                                    elif "dados" in resp:
                                        datos = resp.get("dados", [])
                                        if isinstance(datos, list) and datos:
                                            geogrid_cliente = datos[0]
                                            logger.info("Cliente encontrado por nombre (en dados): %s", customer_name)
                        
                        # 4. Intentar búsqueda general por connection_id (si todo lo anterior fallo)
                        if not geogrid_cliente and payload.connection_id:
                            # Buscar por ID usando query general
                            client_kwargs, region = settings.http_client_kwargs("geogrid")
                            async with httpx.AsyncClient(**client_kwargs) as client:
                                resp = await fetch_json(
                                    client, "GET", "/clientes",
                                    params={"q": str(payload.connection_id)},
                                    service="geogrid", settings=settings, region_name=region
                                )
                                # Manejar estructura {"registros": [{"dados": {...}}]} o {"dados": [...]}
                                if isinstance(resp, dict):
                                    if "registros" in resp:
                                        registros = resp.get("registros", [])
                                        if isinstance(registros, list) and registros:
                                            geogrid_cliente = registros[0].get("dados")
                                            logger.info("Cliente encontrado por ID (en registros): %s", payload.connection_id)
                                    elif "dados" in resp:
                                        datos = resp.get("dados", [])
                                        if isinstance(datos, list) and datos:
                                            geogrid_cliente = datos[0]
                                            logger.info("Cliente encontrado por ID (en dados): %s", payload.connection_id)
                        
                        if geogrid_cliente:
                            geogrid_id = geogrid_cliente.get("id")
                            geogrid_name = geogrid_cliente.get("nome") or geogrid_cliente.get("name") or customer_name
                            
                            # Validación de seguridad: verificar coincidencia de nombre o ID
                            normalized_found = str(geogrid_name).upper()
                            normalized_search = str(customer_name).upper()
                            is_match = False
                            
                            if payload.connection_id and str(payload.connection_id) in normalized_found:
                                is_match = True
                            elif connection_code and str(connection_code) in normalized_found:
                                is_match = True
                            else:
                                # Coincidencia parcial de palabras clave (min 4 letras)
                                for word in normalized_search.replace("-", " ").split():
                                    if len(word) >= 4 and word in normalized_found:
                                        is_match = True
                                        break
                            
                            if not is_match:
                                logger.warning("SAFETY CHECK: Cliente encontrado '%s' no coincide suficientemente con buscado '%s' (ID=%s). Abortando renombre.", 
                                              geogrid_name, customer_name, payload.connection_id)
                            else:
                                # Marcar como BAJA
                                await geogrid_service.mark_cliente_as_baja(settings, geogrid_id, geogrid_name)
                            
                            # Registrar evento de baja
                            register_customer_event(
                                "baja",
                                customer=target_event,
                                source="sync",
                                metadata={"action": "decommission_fallback", "connection_id": payload.connection_id},
                            )
                            
                            return {
                                "status": "decommissioned_from_history",
                                "connection_id": payload.connection_id,
                                "customer_name": f"BAJA - {geogrid_name}",
                                "geogrid_id": geogrid_id,
                            }
                        else:
                            logger.warning("Cliente no encontrado en GeoGrid para %s / %s", payload.connection_id, customer_name)
                    except Exception as geogrid_exc:
                        logger.warning("Error al marcar BAJA en GeoGrid: %s", geogrid_exc)
            
            # Si falla recuperación o es otro error
            raise

        # Si llegamos aquí, tenemos customer
        _inject_connection_context(
            customer,
            connection_id=payload.connection_id,
            connection_code=payload.connection_code,
            customer_name=payload.customer_name,
        )
        
        ensure_customer_ready(
            customer,
            action="sync",
            connection_context={
                "connection_id": payload.connection_id,
                "connection_code": payload.connection_code,
                "customer_name": payload.customer_name,
            },
        )

        resolved_customer_id = resolve_customer_id(customer, payload.customer_id)

        cliente_payload = build_geogrid_cliente_payload(customer)
        try:
            geogrid_id, action = await geogrid_service.upsert_cliente(
                settings, cliente_payload, fetch_json
            )
            fallback_mode = False
            
            # Registrar evento de alta exitosa si se creó o actualizó (opcionalmente)
            # Para sync manual, es útil verlo en el mapa.
            if action in ("created", "updated"):
                register_customer_event(
                    "alta",
                    customer=customer,
                    source="sync", 
                    metadata={"geogrid_id": geogrid_id, "action": action},
                )
        except HTTPException as geogrid_exc:
            detail = _resolve_geogrid_error_detail(geogrid_exc)
            if detail is None:
                raise
            record_incident(
                "geogrid_unavailable",
                {
                    "customer_id": resolved_customer_id,
                    "detail": detail,
                    **_connection_metadata_snapshot(customer),
                },
            )
            register_customer_event(
                "alta",
                customer=customer,
                source="sync-fallback",
                metadata={"reason": "geogrid_unavailable"},
            )
            geogrid_id = None
            action = "pending"
            fallback_mode = True
        except Exception as geogrid_exc:
             # handle generic exception
             # ... similar to main.py
            record_incident(
                "geogrid_unavailable",
                {
                    "customer_id": resolved_customer_id,
                    "detail": {"message": str(geogrid_exc)},
                    **_connection_metadata_snapshot(customer),
                },
            )
            register_customer_event(
                "alta",
                customer=customer,
                source="sync-fallback",
                metadata={"reason": "geogrid_exception"},
            )
            geogrid_id = None
            action = "pending"
            fallback_mode = True

        SYNC_COUNTER.labels(result=action).inc()
        resolve_incidents(
            customer_id=resolved_customer_id,
            action="sync",
            resolved_by=user,
            reason=f"sync_{action}",
        )
        geogrid_attend_result: Optional[Dict[str, Any]] = None
        if AUTO_GEOGRID_ATTEND and not fallback_mode:
            codigo_integracion = _customer_connection_identifier(
                customer, payload.connection_code
            )
            sigla_caja = extract_geogrid_box_sigla(customer)
            porta_num = extract_geogrid_port_number(customer)
            missing_fields: List[str] = []
            if not codigo_integracion:
                missing_fields.append("codigo_integracion")
            if not sigla_caja:
                missing_fields.append("geogrid_caja_sigla")
            if porta_num is None:
                missing_fields.append("geogrid_porta_num")
            if missing_fields:
                record_incident(
                    "missing_geogrid_assignment",
                    {
                        "customer_id": resolved_customer_id,
                        "action": "sync",
                        "missing": missing_fields,
                        **_connection_metadata_snapshot(customer),
                    },
                )
            else:
                try:
                    lat, lon = _customer_coordinates_strict(customer)
                    attend_payload = GeoGridAttendRequest(
                        codigo_integracion=codigo_integracion,
                        geogrid_caja_sigla=sigla_caja,
                        geogrid_porta_num=porta_num,
                        latitude=lat,
                        longitude=lon,
                    )
                    # Need to call geogrid_attend_endpointLogic or extract it?
                    # `geogrid_attend` below is an endpoint handler too.
                    # I should call it directly? Or extract the logic.
                    # Since this is "auto attend", it calls the same logic.
                    # I'll call `geogrid_attend_logic` which I will define separately or just call the function if possible.
                    # Calling function directly works if it doesn't rely on Request/Dependency injection machinery differently.
                    geogrid_attend_result = await geogrid_attend_logic(attend_payload, settings)
                except ValueError as attend_exc:
                     record_incident(
                        "invalid_coordinates",
                        {
                            "customer_id": resolved_customer_id,
                            "action": "sync",
                            "detail": {"message": str(attend_exc)},
                            **_connection_metadata_snapshot(customer),
                        },
                    )
                except HTTPException as attend_exc:
                    record_incident(
                        "geogrid_assignment_conflict"
                        if attend_exc.status_code == status.HTTP_409_CONFLICT
                        else "geogrid_unavailable",
                        {
                            "customer_id": resolved_customer_id,
                            "action": "sync",
                            "detail": attend_exc.detail,
                            **_connection_metadata_snapshot(customer),
                        },
                    )
                except Exception as attend_exc:
                    record_incident(
                        "geogrid_unavailable",
                        {
                            "customer_id": resolved_customer_id,
                            "action": "sync",
                            "detail": {"message": str(attend_exc)},
                            **_connection_metadata_snapshot(customer),
                        },
                    )

        result = {
            "geogrid_id": geogrid_id,
            "action": action,
            "connection_id": customer.get("connection_id"),
            "connection_code": _customer_connection_code(
                customer, payload.connection_code
            ),
        }
        if geogrid_attend_result is not None:
            result["geogrid_attend"] = geogrid_attend_result

        record_audit(
            AuditEntry(
                action="sync",
                customer_id=resolved_customer_id,
                user=user,
                dry_run=False,
                status="success",
                detail=result,
                timestamp=timestamp,
            )
        )
        return result
    except HTTPException as exc:
        SYNC_COUNTER.labels(result="error").inc()
        record_audit(
            AuditEntry(
                action="sync",
                customer_id=resolved_customer_id,
                user=user,
                dry_run=False,
                status="error",
                detail={
                    "status_code": exc.status_code,
                    "detail": exc.detail,
                },
                timestamp=timestamp,
            )
        )
        raise
    except Exception as exc:
        SYNC_COUNTER.labels(result="error").inc()
        detail = {"message": "Unexpected error", "error": str(exc)}
        record_audit(
            AuditEntry(
                action="sync",
                customer_id=resolved_customer_id,
                user=user,
                dry_run=False,
                status="error",
                detail=detail,
                timestamp=timestamp,
            )
        )
        logger.exception("Unhandled error during customer sync")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail)
    finally:
        connection_ctx.reset(ctx_token)

async def geogrid_attend_logic(
    payload: GeoGridAttendRequest,
    settings: EnvConfig,
) -> Dict[str, Any]:
    geogrid_cliente = await geogrid_service.get_cliente_by_codigo_integrado(
        settings, payload.codigo_integracion, fetch_json
    )
    if not geogrid_cliente:
        geogrid_cliente = await geogrid_service.get_cliente_by_codigo(
            settings, payload.codigo_integracion, fetch_json
        )
    if not geogrid_cliente:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "message": "Cliente no encontrado en GeoGrid",
                "codigo_integracion": payload.codigo_integracion,
            },
        )

    geogrid_id = geogrid_cliente.get("id")
    resolved_port_id: Optional[int] = payload.id_porta
    if resolved_port_id is None:
        resolved_port_id = await geogrid_service.resolve_port_id_by_sigla_and_number(
            settings,
            sigla_caja=payload.geogrid_caja_sigla,  # type: ignore[arg-type]
            porta_num=payload.geogrid_porta_num,  # type: ignore[arg-type]
        )

    access_point_id = payload.id_item_rede_cliente
    if access_point_id is None:
        if payload.latitude is None or payload.longitude is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "message": "Faltan coordenadas para crear el ponto de acesso",
                    "fields": ["latitude", "longitude"],
                },
            )
        pasta_env = os.getenv("GEOGRID_PASTA_ID")
        if not pasta_env:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"message": "Falta GEOGRID_PASTA_ID en entorno para crear el ponto de acesso"},
            )
        try:
            pasta_id = int(pasta_env)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"message": "GEOGRID_PASTA_ID inválido"},
            )
        customer_name = geogrid_cliente.get("nome") or geogrid_cliente.get("name") or ""
        access_label = payload.codigo_integracion
        if customer_name:
            access_label = f"{payload.codigo_integracion} {customer_name}"
        access_point_id = await geogrid_service.create_access_point(
            settings,
            latitude=payload.latitude,
            longitude=payload.longitude,
            label=access_label,
            pasta_id=pasta_id,
        )
    attend_payload: Dict[str, Any] = {
        "idPorta": resolved_port_id,
        "idCliente": geogrid_id,
        "codigoIntegracao": payload.codigo_integracion,
    }
    local_payload: Dict[str, Any] = {"idItemRedeCliente": access_point_id}
    if payload.latitude is not None and payload.longitude is not None:
        local_payload["latitude"] = payload.latitude
        local_payload["longitude"] = payload.longitude
    attend_payload["local"] = local_payload
    cabo_tipo_id: Optional[int] = payload.id_cabo_tipo
    if cabo_tipo_id is None:
        cabo_env = os.getenv("GEOGRID_CABO_TIPO_ID")
        if cabo_env:
            try:
                cabo_tipo_id = int(cabo_env)
            except ValueError:
                 raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"message": "GEOGRID_CABO_TIPO_ID inválido"},
                )
        else:
            cabo_name = os.getenv("GEOGRID_CABO_TIPO_NAME", "").strip()
            if cabo_name:
                cabo_tipo_id = await geogrid_service.resolve_cabo_tipo_id_by_name(
                    settings, cabo_name
                )
                if cabo_tipo_id is None:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={
                            "message": "No se encontró idCaboTipo en GeoGrid",
                            "cabo_tipo": cabo_name,
                        },
                    )
    if cabo_tipo_id is not None:
        attend_payload["idCaboTipo"] = cabo_tipo_id
        if payload.pontos:
            attend_payload["pontos"] = [p.model_dump() for p in payload.pontos]
        elif payload.latitude is not None and payload.longitude is not None:
            attend_payload["pontos"] = [
                {"latitude": payload.latitude, "longitude": payload.longitude}
            ]
        else:
            logger.warning(
                "idCaboTipo presente pero sin puntos; el drop puede no crearse (codigo=%s)",
                payload.codigo_integracion,
            )

    attend_result = await geogrid_service.attend_customer(settings, attend_payload)
    try:
        raw_name = geogrid_cliente.get("nome") or geogrid_cliente.get("name") or ""
        # Si viene en formato "CODIGO - NOMBRE", extraemos solo el nombre
        if " - " in raw_name:
            parts = raw_name.split(" - ", 1)
            # Verificamos si la primera parte parece un código (alfanumérico corto)
            if len(parts[0]) <= 20: 
                customer_name = parts[1]
            else:
                customer_name = raw_name
        else:
            customer_name = raw_name

        base_label = f"Drop - {customer_name}" if customer_name else f"Drop - {payload.codigo_integracion}"
        drop_label = base_label
        if (
            payload.geogrid_caja_sigla is not None
            and payload.geogrid_porta_num is not None
            and resolved_port_id is not None
        ):
            drop_label = await geogrid_service.resolve_drop_comment_label(
                settings,
                sigla_caja=payload.geogrid_caja_sigla,
                porta_num=payload.geogrid_porta_num,
                base_label=base_label,
                target_port_id=resolved_port_id,
            )
        await geogrid_service.comment_port(settings, resolved_port_id, drop_label)
    except Exception as exc: 
        logger.warning("No se pudo comentar la porta %s :: %s", resolved_port_id, exc)

    return {
        "status": "attended",
        "geogrid_id": geogrid_id,
        "attend_result": attend_result,
    }


@router.post(
    "/geogrid/attend",
    status_code=status.HTTP_200_OK,
    summary="Atender cliente en GeoGrid (usa idPorta e idCliente)",
)
async def geogrid_attend(
    payload: GeoGridAttendRequest,
    settings: EnvConfig = Depends(get_settings),
) -> Dict[str, Any]:
    return await geogrid_attend_logic(payload, settings)

@router.post(
    "/provision/onu",
    status_code=status.HTTP_200_OK,
    summary="Asignar Puerto/ONU (Aprovisionamiento Lógico)",
    response_description="Detalles de la asignación en GeoGrid.",
)
async def provision_onu(
    payload: ProvisionRequest,
    request: Request,
    settings: EnvConfig = Depends(get_settings),
    state: RuntimeState = Depends(get_runtime_state),
) -> Dict[str, Any]:
    """
    **Realiza la asignación lógica de una ONU a un puerto en la red FTTH.**

    Este endpoint conecta "la fibra virtual":
    *   Verifica que la OLT, Tarjeta (Board) y Puerto PON existan.
    *   Asocia el Serial Number (SN) de la ONU al cliente en GeoGrid.
    *   Registra la observación con el usuario responsable y timestamp.
    
    Admite modo `dry_run=True` para simular la asignación sin impactar en inventario.
    """
    user = request.headers.get(APP_USER_HEADER, "ui")
    timestamp = datetime.now(timezone.utc).isoformat()
    dry_run_flag = payload.dry_run if payload.dry_run is not None else state.dry_run
    resolved_customer_id: Optional[int] = payload.customer_id

    ctx_token = connection_ctx.set(
        {"connection_id": payload.connection_id, "connection_code": payload.connection_code}
    )
    try:
        customer = await fetch_customer_record(
            settings,
            customer_id=payload.customer_id,
            connection_code=payload.connection_code,
            connection_id=payload.connection_id,
        )
        ensure_customer_ready(
            customer,
            action="provision",
            connection_context={"connection_id": payload.connection_id, "connection_code": payload.connection_code},
        )
        ensure_alignment(customer, payload)
        resolved_customer_id = resolve_customer_id(customer, payload.customer_id)
        cliente_payload = build_geogrid_cliente_payload(customer)
        geogrid_id, geogrid_action = await geogrid_service.upsert_cliente(
            settings, cliente_payload, fetch_json
        )
        if geogrid_action in {"created", "updated"}:
            resolve_incidents(
                customer_id=resolved_customer_id,
                action="sync",
                resolved_by=user,
                reason=f"sync_{geogrid_action}",
            )

        if dry_run_flag:
            PROVISION_COUNTER.labels(result="dry_run").inc()
            detail = {
                "dry_run": True,
                "status": "skipped",
                "message": "Asignación en GeoGrid omitida por dry-run",
                "geogrid_id": geogrid_id,
            }
            record_audit(
                AuditEntry(
                    action="provision",
                    customer_id=resolved_customer_id,
                    user=user,
                    dry_run=True,
                    status="success",
                    detail=detail,
                    timestamp=timestamp,
                )
            )
            return detail

        port_identifier = f"OLT{payload.olt_id}-B{payload.board}-P{payload.pon_port}"
        assignment_payload = {
            "idCliente": geogrid_id,
            "idPorta": port_identifier,
            "oltId": payload.olt_id,
            "board": payload.board,
            "pon": payload.pon_port,
            "onuSerial": payload.onu_sn,
            "observacao": f"Asignado por {user} en {timestamp}",
        }

        try:
            assignment_result = await geogrid_service.assign_port(
                settings, assignment_payload
            )
        except HTTPException as exc:
            if exc.status_code == status.HTTP_409_CONFLICT:
                record_incident(
                    "geogrid_assignment_conflict",
                    {
                        "customer_id": resolved_customer_id,
                        "request": assignment_payload,
                        "detail": exc.detail,
                    },
                )
            raise

        PROVISION_COUNTER.labels(result="assigned").inc()
        resolve_incidents(
            customer_id=resolved_customer_id,
            action="provision",
            resolved_by=user,
            reason="assigned",
        )
        record_audit(
            AuditEntry(
                action="provision",
                customer_id=resolved_customer_id,
                user=user,
                dry_run=False,
                status="success",
                detail={
                    "geogrid_id": geogrid_id,
                    "assignment": assignment_result,
                    "connection_id": customer.get("connection_id"),
                    "connection_code": _customer_connection_code(
                        customer, payload.connection_code
                    ),
                },
                timestamp=timestamp,
            )
        )
        register_customer_event(
            "alta",
            customer=customer,
            source="runtime",
            metadata={
                "trigger": "provision",
                "geogrid_id": geogrid_id,
                "port": port_identifier,
                **_connection_metadata_snapshot(customer, payload.connection_code), # Need to match signature if possible or check definition
            },
        )
        return {
            "status": "assigned",
            "geogrid_id": geogrid_id,
            "assignment": assignment_result,
            "connection_id": customer.get("connection_id"),
            "connection_code": _customer_connection_code(
                customer, payload.connection_code
            ),
        }
    except HTTPException as exc:
        PROVISION_COUNTER.labels(result="error").inc()
        record_audit(
            AuditEntry(
                action="provision",
                customer_id=resolved_customer_id,
                user=user,
                dry_run=dry_run_flag,
                status="error",
                detail={
                    "status_code": exc.status_code,
                    "detail": exc.detail,
                },
                timestamp=timestamp,
            )
        )
        raise
    except Exception as exc:
        PROVISION_COUNTER.labels(result="error").inc()
        detail = {"message": "Unexpected error", "error": str(exc)}
        record_audit(
            AuditEntry(
                action="provision",
                customer_id=resolved_customer_id,
                user=user,
                dry_run=dry_run_flag,
                status="error",
                detail=detail,
                timestamp=timestamp,
            )
        )
        logger.exception("Unhandled error during provisioning")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail)
    finally:
        connection_ctx.reset(ctx_token)

@router.post(
    "/decommission/customer",
    status_code=status.HTTP_200_OK,
    summary="Desasignar un cliente en GeoGrid",
)
async def decommission_customer(
    payload: DecommissionRequest,
    request: Request,
    settings: EnvConfig = Depends(get_settings),
    state: RuntimeState = Depends(get_runtime_state),
) -> Dict[str, Any]:
    user = request.headers.get(APP_USER_HEADER, "ui")
    timestamp = datetime.now(timezone.utc).isoformat()
    dry_run_flag = payload.dry_run if payload.dry_run is not None else state.dry_run

    ctx_token = connection_ctx.set(
        {"connection_id": payload.connection_id, "connection_code": payload.connection_code}
    )
    try:
        customer = await fetch_customer_record(
            settings,
            customer_id=payload.customer_id,
            connection_code=payload.connection_code,
            connection_id=payload.connection_id,
        )
        ensure_customer_inactive(customer)
        ensure_customer_has_network_keys(customer, action="decommission")
        resolved_customer_id = resolve_customer_id(customer, payload.customer_id)

        codigo_integracion = str(customer.get("code") or customer.get("customer_id"))
        geogrid_cliente = await geogrid_service.get_cliente_by_codigo(
            settings, codigo_integracion, fetch_json
        )
        if not geogrid_cliente:
            record_incident(
                "decommission_missing_feature",
                {
                    "customer_id": resolved_customer_id,
                    "customer_code": codigo_integracion,
                },
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "message": "GeoGrid no tiene registro para el cliente",
                    "customer_code": codigo_integracion,
                },
            )

        geogrid_id = geogrid_cliente.get("id")
        port_identifier = f"OLT{customer['olt_id']}-B{customer['board']}-P{customer['pon']}"

        if dry_run_flag:
            DECOMMISSION_COUNTER.labels(result="dry_run").inc()
            detail = {
                "dry_run": True,
                "status": "skipped",
                "geogrid_id": geogrid_id,
                "port": port_identifier,
            }
            record_audit(
                AuditEntry(
                    action="decommission",
                    customer_id=resolved_customer_id,
                    user=user,
                    dry_run=True,
                    status="success",
                    detail=detail,
                    timestamp=timestamp,
                )
            )
            return detail

        try:
            await geogrid_service.remove_assignment(settings, port_identifier, geogrid_id)
        except HTTPException as exc:
            if exc.status_code == status.HTTP_404_NOT_FOUND:
                record_incident(
                    "decommission_missing_feature",
                    {
                    "customer_id": resolved_customer_id,
                    "geogrid_id": geogrid_id,
                    "port": port_identifier,
                },
            )
            raise
        
        # Marcar cliente como BAJA (renombrar con prefijo)
        try:
            current_name = geogrid_cliente.get("nome") or geogrid_cliente.get("name") or str(resolved_customer_id)
            await geogrid_service.mark_cliente_as_baja(settings, geogrid_id, current_name)
        except Exception as rename_exc:
            logger.warning("No se pudo marcar cliente %s como BAJA: %s", geogrid_id, rename_exc)
        try:
            sigla_caja = extract_geogrid_box_sigla(customer)
            porta_num = extract_geogrid_port_number(customer)
            if sigla_caja and porta_num is not None:
                port_id = await geogrid_service.resolve_port_id_by_sigla_and_number(
                    settings,
                    sigla_caja=sigla_caja,
                    porta_num=porta_num,
                    allow_unavailable=True,
                )
                await geogrid_service.comment_port(settings, port_id, "")
            else:
                logger.warning(
                    "No se pudo limpiar comentario: faltan datos de caja/puerto (sigla=%s, porta=%s)",
                    sigla_caja,
                    porta_num,
                )
        except Exception as exc:
            logger.warning("No se pudo limpiar comentario de porta: %s", exc)

        # NUEVO: Marcar visualmente al cliente como BAJA renombrándolo
        try:
            # Reconstruimos el payload base del cliente
            update_payload = build_geogrid_cliente_payload(customer)
            current_name = update_payload.get("nome", "")
            
            # Agregamos prefijo si no lo tiene ya
            if "[BAJA]" not in current_name.upper():
                update_payload["nome"] = f"[BAJA] {current_name}"
                # Enviamos actualización para reflejar el cambio en el mapa/lista
                await geogrid_service.upsert_cliente(settings, update_payload, fetch_json)
                logger.info("Cliente %s renombrado con marca de [BAJA] en GeoGrid", geogrid_id)
        except Exception as exc:
            logger.warning("No se pudo renombrar cliente a [BAJA]: %s", exc)

        DECOMMISSION_COUNTER.labels(result="removed").inc()
        resolve_incidents(
            customer_id=resolved_customer_id,
            action="decommission",
            resolved_by=user,
            reason="removed",
        )
        record_audit(
            AuditEntry(
                action="decommission",
                customer_id=resolved_customer_id,
                user=user,
                dry_run=False,
                status="success",
                detail={
                    "geogrid_id": geogrid_id,
                    "port": port_identifier,
                    "connection_id": customer.get("connection_id"),
                    "connection_code": _customer_connection_code(
                        customer, payload.connection_code
                    ),
                },
                timestamp=timestamp,
            )
        )
        register_customer_event(
            "baja",
            customer=customer,
            source="runtime",
            metadata={
                "trigger": "decommission",
                "geogrid_id": geogrid_id,
                "port": port_identifier,
                **_connection_metadata_snapshot(customer, payload.connection_code),
            },
        )
        return {
            "status": "removed",
            "geogrid_id": geogrid_id,
            "port": port_identifier,
            "connection_id": customer.get("connection_id"),
            "connection_code": _customer_connection_code(
                customer, payload.connection_code
            ),
        }
    except HTTPException as exc:
        DECOMMISSION_COUNTER.labels(result="error").inc()
        record_audit(
            AuditEntry(
                action="decommission",
                customer_id=resolved_customer_id,
                user=user,
                dry_run=dry_run_flag,
                status="error",
                detail={
                    "status_code": exc.status_code,
                    "detail": exc.detail,
                },
                timestamp=timestamp,
            )
        )
        raise
    except Exception as exc:
        DECOMMISSION_COUNTER.labels(result="error").inc()
        detail = {"message": "Unexpected error", "error": str(exc)}
        record_audit(
            AuditEntry(
                action="decommission",
                customer_id=resolved_customer_id,
                user=user,
                dry_run=dry_run_flag,
                status="error",
                detail=detail,
                timestamp=timestamp,
            )
        )
        logger.exception("Unhandled error during decommission")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail)
    finally:
        connection_ctx.reset(ctx_token)
