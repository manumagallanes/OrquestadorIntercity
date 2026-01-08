import logging
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List

from ..core.config import EnvConfig
from ..core.integration import fetch_json
from ..persistence import persistence_store
from ..services import isp as isp_service
from ..services import geogrid as geogrid_service

logger = logging.getLogger("orchestrator.logic.reconciliation")

async def run_reconciliation(settings: EnvConfig) -> Dict[str, Any]:
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        isp_customers = await isp_service.list_customers(settings, fetch_json)
    except Exception as exc:
        logger.error("Failed to fetch customers from ISP: %s", exc)
        isp_customers = []

    try:
        # Note: list_clientes usage in main.py call might differ from service definition?
        # main.py line 804: await geogrid_service.list_clientes(settings, fetch_json)
        # Checking logic...
        geogrid_clientes = await geogrid_service.list_clientes(settings, fetch_json)
    except Exception as exc:
        logger.error("Failed to fetch features from GeoGrid: %s", exc)
        geogrid_clientes = []

    isp_by_code: Dict[str, Dict[str, Any]] = {}
    
    for entry in isp_customers:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code") or "").strip().lower()
        if not code:
            continue
        isp_by_code[code] = entry
        # isp_by_id logic existed in main.py but wasn't used in the logic I saw? checks...
        # It populated isp_by_id but didn't use it in lines 835+ loop.

    geogrid_by_code: Dict[str, Dict[str, Any]] = {}
    for cliente in geogrid_clientes:
        if not isinstance(cliente, dict):
            continue
        codigo = str(cliente.get("codigoIntegracao") or "").strip().lower()
        if codigo:
            geogrid_by_code[codigo] = cliente

    issues: List[Dict[str, Any]] = []
    counts: Dict[str, int] = defaultdict(int)

    for code, customer in isp_by_code.items():
        customer_id = customer.get("customer_id")
        status = str(customer.get("status") or "").lower()
        integration_enabled = bool(customer.get("integration_enabled", True))
        geogrid_entry = geogrid_by_code.get(code)
        if integration_enabled and status in {"enabled", "active"} and not geogrid_entry:
            issues.append(
                {
                    "reconciliation_id": str(uuid.uuid4()),
                    "issue_type": "missing_geogrid",
                    "customer_id": customer_id,
                    "detail": {
                        "message": "Cliente activo sin registro asociado en GeoGrid",
                        "customer_code": customer.get("code"),
                        "customer_name": customer.get("name"),
                    },
                }
            )
            counts["missing_geogrid"] += 1
        if geogrid_entry and status not in {"enabled", "active"}:
            issues.append(
                {
                    "reconciliation_id": str(uuid.uuid4()),
                    "issue_type": "inactive_geogrid_resource",
                    "customer_id": customer_id,
                    "detail": {
                        "message": "Cliente inactivo en ISP pero aún presente en GeoGrid",
                        "customer_code": customer.get("code"),
                        "geogrid_id": geogrid_entry.get("id"),
                    },
                }
            )
            counts["inactive_geogrid_resource"] += 1

        if geogrid_entry:
            assignments = geogrid_entry.get("assignments") or []
            customer_onu = str(customer.get("onu_sn") or "").strip().lower()
            if customer_onu and assignments:
                for assignment in assignments:
                    onu_serial = str(assignment.get("onuSerial") or "").strip().lower()
                    if onu_serial and onu_serial != customer_onu:
                        issues.append(
                            {
                                "reconciliation_id": str(uuid.uuid4()),
                                "issue_type": "geogrid_assignment_conflict",
                                "customer_id": customer_id,
                                "detail": {
                                    "message": "Asignación de puerto con ONU distinta a la registrada en ISP",
                                    "customer_code": customer.get("code"),
                                    "expected_onu": customer.get("onu_sn"),
                                    "geogrid_onu": assignment.get("onuSerial"),
                                    "id_porta": assignment.get("idPorta"),
                                },
                            }
                        )
                        counts["geogrid_assignment_conflict"] += 1

            if (
                integration_enabled
                and status in {"enabled", "active"}
                and customer.get("pon") is not None
                and not assignments
            ):
                issues.append(
                    {
                        "reconciliation_id": str(uuid.uuid4()),
                        "issue_type": "missing_geogrid_assignment",
                        "customer_id": customer_id,
                        "detail": {
                            "message": "Cliente con datos de red sin asignación de puerto en GeoGrid",
                            "customer_code": customer.get("code"),
                            "geogrid_id": geogrid_entry.get("id"),
                        },
                    }
                )
                counts["missing_geogrid_assignment"] += 1

    for cliente in geogrid_clientes:
        codigo = str(cliente.get("codigoIntegracao") or "").strip().lower()
        if not codigo:
            continue
        if codigo not in isp_by_code:
            issues.append(
                {
                    "reconciliation_id": str(uuid.uuid4()),
                    "issue_type": "orphan_geogrid_cliente",
                    "customer_id": None,
                    "detail": {
                        "message": "Cliente presente en GeoGrid sin contraparte en ISP",
                        "geogrid_id": cliente.get("id"),
                        "codigo_integracao": cliente.get("codigoIntegracao"),
                    },
                }
            )
            counts["orphan_geogrid_cliente"] += 1

    summary_entry = {
        "reconciliation_id": str(uuid.uuid4()),
        "issue_type": "summary",
        "customer_id": None,
        "detail": {
            "counts": dict(counts),
            "totals": {
                "isp_customers": len(isp_by_code),
                "geogrid_clientes": len(geogrid_clientes),
            },
        },
    }

    try:
        persistence_store.save_reconciliation_results(timestamp, issues + [summary_entry])
        persistence_store.purge_reconciliation_results()
    except Exception as exc:
        logger.error("Failed to persist reconciliation results: %s", exc)

    return {
        "generated_at": timestamp,
        "totals": {
            "isp_customers": len(isp_by_code),
            "geogrid_clientes": len(geogrid_clientes),
        },
        "issues": issues,
        "issue_counts": dict(counts),
    }
