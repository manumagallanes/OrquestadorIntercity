import json
import os
from typing import Any, Dict, Optional, Tuple, List

import httpx
import streamlit as st


DEFAULT_BASE_URL = os.getenv("ORCHESTRATOR_BASE_URL", "http://localhost:8000")
DEFAULT_ISP_BASE = os.getenv("ISP_BASE_URL", "http://localhost:8001/api")
DEFAULT_GEOGRID_BASE = os.getenv("GEOGRID_BASE_URL", "http://localhost:8002/api/v3")
APP_USER_HEADER_NAME = os.getenv("ORCHESTRATOR_USER_HEADER", "X-Orchestrator-User")


st.set_page_config(page_title="Intercity Orchestrator UI", layout="wide")
st.title("Intercity Orchestrator – Panel de pruebas")


def init_state() -> None:
    if "base_url" not in st.session_state:
        st.session_state["base_url"] = DEFAULT_BASE_URL.rstrip("/")
    if "isp_base_url" not in st.session_state:
        st.session_state["isp_base_url"] = DEFAULT_ISP_BASE.rstrip("/")
    if "geogrid_base_url" not in st.session_state:
        st.session_state["geogrid_base_url"] = DEFAULT_GEOGRID_BASE.rstrip("/")
    if "audit_user" not in st.session_state:
        st.session_state["audit_user"] = "demo-ui"


def call_api(
    method: str,
    path: str,
    *,
    service: str = "orchestrator",
    json: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, int, Any]:
    service_map = {
        "orchestrator": st.session_state["base_url"],
        "isp": st.session_state["isp_base_url"],
        "geogrid": st.session_state["geogrid_base_url"],
    }
    base_url = service_map.get(service, st.session_state["base_url"])
    url = f"{base_url}{path}"
    try:
        with httpx.Client(timeout=30.0) as client:
            headers = None
            if service == "orchestrator" and st.session_state.get("audit_user"):
                headers = {APP_USER_HEADER_NAME: st.session_state["audit_user"]}
            response = client.request(method, url, json=json, params=params, headers=headers)
        status = response.status_code
        if response.headers.get("content-type", "").startswith("application/json"):
            data = response.json()
        else:
            data = response.text
        success = response.is_success
        return success, status, data
    except httpx.HTTPError as exc:
        return False, 0, {"error": str(exc)}


def display_response(success: bool, status: int, data: Any) -> None:
    if success:
        st.success(f"Respuesta OK (status {status})")
    else:
        st.error(f"Ocurrió un error (status {status or 'N/D'})")
    st.json(data)


def sync_customer_section() -> None:
    st.subheader("1. Sincronizar cliente hacia GeoGrid")
    with st.form("sync_form"):
        customer_id = st.number_input(
            "ID de cliente (ISP-Cube)", min_value=1, step=1, value=202
        )
        connection_code = st.text_input(
            "Código de conexión (opcional)", value=""
        )
        submitted = st.form_submit_button("Sincronizar")
        if submitted:
            payload: Dict[str, Any] = {}
            if connection_code.strip():
                payload["connection_code"] = connection_code.strip()
            else:
                payload["customer_id"] = int(customer_id)
            success, status, data = call_api("POST", "/sync/customer", json=payload)
            display_response(success, status, data)


def provision_onu_section() -> None:
    st.subheader("2. Asignar puerto en GeoGrid")
    with st.form("provision_form"):
        col1, col2, col3 = st.columns(3)
        customer_id = col1.number_input(
            "ID de cliente (ISP-Cube)",
            min_value=1,
            step=1,
            value=2142,
        )
        connection_code = col1.text_input(
            "Código de conexión (opcional)", value=""
        )
        olt_id = col2.number_input("OLT ID", min_value=1, step=1, value=1)
        board = col2.number_input("Board", min_value=0, step=1, value=1)
        pon_port = col3.number_input("Puerto PON", min_value=0, step=1, value=4)
        onu_sn = col3.text_input("Número de serie ONU", value="TESTSN00002")
        dry_run_choice = st.selectbox(
            "Dry-run",
            ("Usar configuración actual", "Forzar true", "Forzar false"),
        )
        submitted = st.form_submit_button("Asignar puerto")
        if submitted:
            payload: Dict[str, Any] = {
                "olt_id": int(olt_id),
                "board": int(board),
                "pon_port": int(pon_port),
                "onu_sn": onu_sn.strip(),
            }
            if connection_code.strip():
                payload["connection_code"] = connection_code.strip()
            else:
                payload["customer_id"] = int(customer_id)
            if dry_run_choice == "Forzar true":
                payload["dry_run"] = True
            elif dry_run_choice == "Forzar false":
                payload["dry_run"] = False
            success, status, data = call_api("POST", "/provision/onu", json=payload)
            display_response(success, status, data)


def decommission_section() -> None:
    st.subheader("3. Baja técnica de conexión")
    with st.form("decommission_form"):
        customer_id = st.number_input(
            "ID de cliente (inactivo en ISP-Cube)",
            min_value=1,
            step=1,
            value=707,
        )
        connection_code = st.text_input(
            "Código de conexión (opcional)", value=""
        )
        dry_run = st.checkbox(
            "Dry-run (solo reporte)",
            value=False,
            help="Si está activo no se ejecuta la baja real ni se generan eventos de monitoreo.",
        )
        submitted = st.form_submit_button("Ejecutar baja")
        if submitted:
            payload: Dict[str, Any] = {}
            if connection_code.strip():
                payload["connection_code"] = connection_code.strip()
            else:
                payload["customer_id"] = int(customer_id)
            payload["dry_run"] = bool(dry_run)
            success, status, data = call_api(
                "POST", "/decommission/customer", json=payload
            )
            display_response(success, status, data)


def incidents_section() -> None:
    st.subheader("4. Incidentes registrados")
    col1, col2 = st.columns([1, 4])
    with col1:
        known_kinds = [
            "missing_fields",
            "integration_disabled",
            "invalid_coordinates",
            "hardware_mismatch",
            "decommission_status_active",
            "decommission_missing_feature",
            "missing_network_keys",
            "geogrid_conflict",
            "geogrid_assignment_conflict",
            "missing_geogrid_assignment",
            "orphan_geogrid_cliente",
            "manual_cleanup_required",
        ]
        kind_filter = st.selectbox(
            "Filtrar por tipo",
            options=["Todos"] + sorted(known_kinds),
        )
        refresh = st.button("Actualizar incidentes")
    if refresh:
        params = None
        if kind_filter != "Todos":
            params = {"kind": kind_filter}
        success, status, data = call_api("GET", "/incidents", params=params)
        if success and isinstance(data, list) and data:
            st.dataframe(data)
        else:
            display_response(success, status, data)


def geogrid_clients_section() -> None:
    st.subheader("5. Clientes en GeoGrid")
    col1, col2 = st.columns([1, 1])
    with col1:
        if st.button("Listar clientes"):
            success, status, data = call_api("GET", "/clientes", service="geogrid")
            if success and isinstance(data, dict):
                st.json(data)
            else:
                display_response(success, status, data)
    with col2:
        with st.form("buscar_cliente_geogrid"):
            codigo = st.text_input("codigoIntegracao", value="003310")
            submitted = st.form_submit_button("Buscar")
            if submitted:
                params = {"codigoIntegracao": codigo.strip()}
                success, status, data = call_api(
                    "GET", "/clientes", service="geogrid", params=params
                )
                display_response(success, status, data)


def audits_section() -> None:
    st.subheader("6. Bitácora de acciones (audit trail)")
    col1, col2, col3 = st.columns([1, 1, 1])
    with col1:
        action = st.selectbox(
            "Acción",
            options=["Todas", "sync", "provision", "decommission"],
        )
    with col2:
        user = st.text_input("Usuario", value="")
    with col3:
        limit = st.number_input("Cantidad", min_value=10, max_value=500, value=200, step=10)
    if st.button("Ver auditoría"):
        params: Dict[str, Any] = {"limit": int(limit)}
        if action != "Todas":
            params["action"] = action
        if user.strip():
            params["user"] = user.strip()
        success, status, data = call_api("GET", "/audits", params=params)
        if success and isinstance(data, list) and data:
            st.dataframe(data)
        else:
            display_response(success, status, data)

def reconciliation_section() -> None:
    st.subheader("7. Conciliación de integraciones")
    col1, col2 = st.columns([1, 1])
    with col1:
        if st.button("Ejecutar conciliación ahora"):
            success, status, data = call_api("POST", "/analytics/reconciliation/run")
            display_response(success, status, data)
    with col2:
        with st.form("reconciliation_form"):
            limit = st.number_input(
                "Cantidad de registros",
                min_value=10,
                max_value=500,
                value=200,
                step=10,
            )
            submitted = st.form_submit_button("Ver resultados")
            if submitted:
                params = {"limit": int(limit)}
                success, status, data = call_api(
                    "GET", "/analytics/reconciliation/results", params=params
                )
                if success and isinstance(data, dict):
                    issues = data.get("issues") or []
                    if issues:
                        formatted: List[Dict[str, Any]] = []
                        for issue in issues:
                            detail = issue.get("detail")
                            if isinstance(detail, dict):
                                detail_value = json.dumps(detail, ensure_ascii=False)
                            else:
                                detail_value = str(detail)
                            formatted.append(
                                {
                                    "timestamp": issue.get("timestamp"),
                                    "issue_type": issue.get("issue_type"),
                                    "customer_id": issue.get("customer_id"),
                                    "detail": detail_value,
                                }
                            )
                        st.dataframe(formatted)
                    else:
                        st.info("Sin inconsistencias registradas.")
                else:
                    display_response(success, status, data)


def isp_controls_section() -> None:
    st.subheader("8. Herramientas demo para ISP-Cube")
    col1, col2 = st.columns(2)
    with col1:
        with st.form("isp_fetch_form"):
            st.markdown("**Consultar cliente**")
            customer_id = st.number_input(
                "ID de cliente", min_value=1, step=1, value=2142, key="fetch_customer"
            )
            submitted = st.form_submit_button("Ver detalle")
            if submitted:
                params = {"customer_id": int(customer_id)}
                success, status, data = call_api(
                    "GET",
                    "/customer",
                    service="isp",
                    params=params,
                )
                display_response(success, status, data)
    with col2:
        with st.form("isp_update_form"):
            st.markdown("**Actualizar flags básicos**")
            customer_id = st.number_input(
                "ID de cliente",
                min_value=1,
                step=1,
                value=2142,
                key="update_customer",
            )
            integration_enabled = st.checkbox("integration_enabled", value=True)
            status_value = st.selectbox(
                "Estado",
                options=["enabled", "disabled", "inactive"],
                index=0,
            )
            submitted = st.form_submit_button("Aplicar cambios")
            if submitted:
                payload = {
                    "integration_enabled": integration_enabled,
                    "status": status_value,
                }
                success, status_code, data = call_api(
                    "PUT",
                    f"/customers/{int(customer_id)}",
                    service="isp",
                    json=payload,
                )
                display_response(success, status_code, data)

    if st.button("Listar clientes ISP-Cube"):
        success, status, data = call_api(
            "GET",
            "/customers/customers_list",
            service="isp",
            params={"limit": 50},
        )
        display_response(success, status, data)
        with st.expander("Editar cliente existente"):
            def _reset_edit_customer_form_state() -> None:
                keys_to_clear = [
                    key
                    for key in list(st.session_state.keys())
                    if key.startswith("edit_toggle_") or key.startswith("edit_input_")
                ]
                for key in keys_to_clear:
                    st.session_state.pop(key, None)

            edit_container = st.container()
            if "edit_customer_cache" not in st.session_state:
                st.session_state["edit_customer_cache"] = None
            with edit_container:
                load_col, toggle_col, input_col = st.columns([1, 1, 2])
                with load_col:
                    edit_id = st.number_input(
                        "customer_id a editar",
                        min_value=1,
                        step=1,
                        value=202,
                        key="edit_customer_id",
                    )
                    if st.button("Cargar datos", key="load_customer_button"):
                        success, status_code, data = call_api(
                            "GET",
                            f"/customers/{int(edit_id)}",
                            service="isp",
                        )
                        if success:
                            st.session_state["edit_customer_cache"] = data
                            _reset_edit_customer_form_state()
                            st.success("Cliente cargado correctamente.")
                        else:
                            display_response(success, status_code, data)
                cached = st.session_state.get("edit_customer_cache") or {}
                if not cached:
                    with toggle_col:
                        st.info("Carga un cliente para habilitar la edición.")
                    with input_col:
                        if st.button("Guardar cambios", key="disabled_save_button"):
                            st.warning("No hay cliente cargado.")
                else:
                    original = dict(cached)
                    editable_fields = [
                        {
                            "key": "name",
                            "toggle_label": "Modificar nombre",
                            "input_label": "Nuevo nombre",
                            "type": "text",
                        },
                        {
                            "key": "address",
                            "toggle_label": "Modificar dirección",
                            "input_label": "Nueva dirección",
                            "type": "text",
                        },
                        {
                            "key": "city",
                            "toggle_label": "Modificar ciudad",
                            "input_label": "Nueva ciudad",
                            "type": "text",
                        },
                        {
                            "key": "zone",
                            "toggle_label": "Modificar zona/barrio",
                            "input_label": "Nueva zona/barrio",
                            "type": "text",
                        },
                        {
                            "key": "lat",
                            "toggle_label": "Modificar latitud",
                            "input_label": "Nueva latitud",
                            "type": "float_optional",
                        },
                        {
                            "key": "lon",
                            "toggle_label": "Modificar longitud",
                            "input_label": "Nueva longitud",
                            "type": "float_optional",
                        },
                        {
                            "key": "odb",
                            "toggle_label": "Modificar ODB",
                            "input_label": "Nueva ODB",
                            "type": "text_free",
                        },
                        {
                            "key": "olt_id",
                            "toggle_label": "Modificar OLT ID",
                            "input_label": "OLT ID",
                            "type": "int",
                        },
                        {
                            "key": "board",
                            "toggle_label": "Modificar board",
                            "input_label": "Board",
                            "type": "int",
                        },
                        {
                            "key": "pon",
                            "toggle_label": "Modificar puerto PON",
                            "input_label": "Puerto PON",
                            "type": "int",
                        },
                        {
                            "key": "onu_sn",
                            "toggle_label": "Modificar ONU SN",
                            "input_label": "Nuevo ONU SN",
                            "type": "text_free",
                        },
                        {
                            "key": "integration_enabled",
                            "toggle_label": "Modificar integration_enabled",
                            "input_label": "integration_enabled",
                            "type": "bool",
                        },
                        {
                            "key": "status",
                            "toggle_label": "Modificar estado",
                            "input_label": "Estado",
                            "type": "select",
                            "options": ["active", "inactive"],
                        },
                    ]

                    with toggle_col:
                        st.markdown(
                            "Seleccioná los datos a actualizar y completá los nuevos valores:"
                        )
                        toggle_states: Dict[str, bool] = {}
                        for field in editable_fields:
                            toggle_key = f"edit_toggle_{field['key']}"
                            toggled = st.checkbox(
                                field["toggle_label"],
                                key=toggle_key,
                            )
                            toggle_states[field["key"]] = toggled
                            if not toggled:
                                st.session_state.pop(
                                    f"edit_input_{field['key']}", None
                                )

                    active_fields = [
                        field
                        for field in editable_fields
                        if toggle_states.get(field["key"])
                    ]

                    with input_col:
                        st.caption("Valores actuales")
                        st.json(cached)

                        if active_fields:
                            for field in active_fields:
                                input_key = f"edit_input_{field['key']}"
                                if input_key not in st.session_state:
                                    if field["type"] in {"text", "text_free"}:
                                        st.session_state[input_key] = str(
                                            original.get(field["key"], "") or ""
                                        )
                                    elif field["type"] == "float_optional":
                                        current_val = original.get(field["key"])
                                        st.session_state[input_key] = (
                                            ""
                                            if current_val is None
                                            else str(current_val)
                                        )
                                    elif field["type"] == "int":
                                        current_val = original.get(field["key"])
                                        st.session_state[input_key] = (
                                            ""
                                            if current_val is None
                                            else str(current_val)
                                        )
                                    elif field["type"] == "bool":
                                        st.session_state[input_key] = bool(
                                            original.get(field["key"], False)
                                        )
                                    elif field["type"] == "select":
                                        options = field.get("options", [])
                                        current_val = original.get(field["key"])
                                        if current_val in options:
                                            st.session_state[input_key] = current_val
                                        elif options:
                                            st.session_state[input_key] = options[0]

                                if field["type"] in {"text", "text_free"}:
                                    st.text_input(
                                        field["input_label"],
                                        key=f"edit_input_{field['key']}",
                                    )
                                elif field["type"] == "float_optional":
                                    st.text_input(
                                        field["input_label"],
                                        key=f"edit_input_{field['key']}",
                                    )
                                elif field["type"] == "int":
                                    st.text_input(
                                        field["input_label"],
                                        key=f"edit_input_{field['key']}",
                                    )
                                elif field["type"] == "bool":
                                    st.checkbox(
                                        field["input_label"],
                                        key=f"edit_input_{field['key']}",
                                    )
                                elif field["type"] == "select":
                                    options = field.get("options", [])
                                    if options:
                                        st.selectbox(
                                            field["input_label"],
                                            options=options,
                                            key=f"edit_input_{field['key']}",
                                        )
                        else:
                            st.info("Seleccioná al menos un campo para editar.")

                        if st.button("Guardar cambios", key="edit_save_button"):
                            if not active_fields:
                                st.warning(
                                    "Marcá al menos una casilla antes de guardar."
                                )
                            else:
                                payload = dict(original)
                                has_error = False

                                for field in active_fields:
                                    field_type = field["type"]
                                    input_key = f"edit_input_{field['key']}"
                                    raw_value = st.session_state.get(input_key)

                                    if field_type == "text":
                                        cleaned = (raw_value or "").strip()
                                        if field["key"] == "zone" and not cleaned:
                                            st.error(
                                                "La zona/barrio es obligatoria para actualizar el cliente."
                                            )
                                            has_error = True
                                            break
                                        if field["key"] in {
                                            "name",
                                            "address",
                                            "city",
                                        }:
                                            payload[field["key"]] = (
                                                cleaned
                                                or original.get(field["key"], "")
                                            )
                                        else:
                                            payload[field["key"]] = cleaned
                                    elif field_type == "text_free":
                                        payload[field["key"]] = (raw_value or "").strip()
                                    elif field_type == "float_optional":
                                        text_value = (raw_value or "").strip()
                                        if not text_value:
                                            payload[field["key"]] = None
                                        else:
                                            try:
                                                payload[field["key"]] = float(text_value)
                                            except ValueError:
                                                st.error(
                                                    f"Valor inválido para {field['input_label']}."
                                                )
                                                has_error = True
                                                break
                                    elif field_type == "int":
                                        text_value = (raw_value or "").strip()
                                        try:
                                            payload[field["key"]] = int(text_value)
                                        except (ValueError, TypeError):
                                            st.error(
                                                f"Valor inválido para {field['input_label']}."
                                            )
                                            has_error = True
                                            break
                                    elif field_type == "bool":
                                        payload[field["key"]] = bool(raw_value)
                                    elif field_type == "select":
                                        payload[field["key"]] = raw_value

                                if has_error:
                                    st.stop()

                                payload["customer_id"] = int(
                                    st.session_state.get(
                                        "edit_customer_id",
                                        original.get("customer_id", edit_id),
                                    )
                                )

                                success, status_code, data = call_api(
                                    "PUT",
                                    f"/customers/{payload['customer_id']}",
                                    service="isp",
                                    json=payload,
                                )
                                display_response(success, status_code, data)
                                if success:
                                    st.session_state["edit_customer_cache"] = payload


def config_section() -> None:
    st.sidebar.header("Configuración")
    base_url = st.sidebar.text_input(
        "URL del orquestador", value=st.session_state["base_url"]
    )
    if base_url:
        st.session_state["base_url"] = base_url.rstrip("/")
    isp_url = st.sidebar.text_input(
        "URL ISP-Cube", value=st.session_state["isp_base_url"]
    )
    if isp_url:
        st.session_state["isp_base_url"] = isp_url.rstrip("/")
    geogrid_url = st.sidebar.text_input(
        "URL GeoGrid", value=st.session_state["geogrid_base_url"]
    )
    if geogrid_url:
        st.session_state["geogrid_base_url"] = geogrid_url.rstrip("/")
    audit_user = st.sidebar.text_input(
        f"Usuario ({APP_USER_HEADER_NAME})",
        value=st.session_state["audit_user"],
    )
    st.session_state["audit_user"] = audit_user.strip() or st.session_state["audit_user"]
    if st.sidebar.button("Leer /config"):
        success, status, data = call_api("GET", "/config")
        with st.sidebar:
            display_response(success, status, data)
    if st.sidebar.button("Resetear mocks"):
        success, status, data = call_api("POST", "/reset")
        with st.sidebar:
            display_response(success, status, data)
    st.sidebar.markdown(
        """
        **Atajos útiles**

        - [Docs Orchestrator](http://localhost:8000/docs)
        - [Prometheus](http://localhost:9090)
        - [Grafana](http://localhost:3000)
        """
    )


def main() -> None:
    init_state()
    config_section()
    sync_customer_section()
    provision_onu_section()
    decommission_section()
    incidents_section()
    geogrid_clients_section()
    audits_section()
    reconciliation_section()
    isp_controls_section()


if __name__ == "__main__":
    main()
