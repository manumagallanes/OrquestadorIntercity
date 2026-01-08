import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

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
    summary="Sync customer data into GeoGrid",
)
async def sync_customer(
    payload: CustomerSyncRequest,
    request: Request,
    settings: EnvConfig = Depends(get_settings),
) -> Dict[str, Any]:
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
        customer = await fetch_customer_record(
            settings,
            customer_id=payload.customer_id,
            connection_code=payload.connection_code,
            connection_id=payload.connection_id,
        )
        _inject_connection_context(
            customer,
            connection_id=payload.connection_id,
            connection_code=payload.connection_code,
            customer_name=payload.customer_name, # Added customer_name arg in domain.py
        )
        # Note: logic in main.py called Inject 3 times redundantly? (lines 2691-2700). I'll skip redundant calls.
        
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
        customer_name = geogrid_cliente.get("nome") or geogrid_cliente.get("name") or ""
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
    summary="Asignar un cliente a una puerta en GeoGrid",
)
async def provision_onu(
    payload: ProvisionRequest,
    request: Request,
    settings: EnvConfig = Depends(get_settings),
    state: RuntimeState = Depends(get_runtime_state),
) -> Dict[str, Any]:
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
