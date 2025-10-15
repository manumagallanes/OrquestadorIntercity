import os
from typing import Any, Dict, Optional, Tuple

import httpx
import streamlit as st


DEFAULT_BASE_URL = os.getenv("ORCHESTRATOR_BASE_URL", "http://localhost:8000")
DEFAULT_ISP_BASE = os.getenv("ISP_BASE_URL", "http://localhost:8001")
DEFAULT_GEOGRID_BASE = os.getenv("GEOGRID_BASE_URL", "http://localhost:8002")
DEFAULT_SMARTOLT_BASE = os.getenv("SMARTOLT_BASE_URL", "http://localhost:8003")
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
    if "smartolt_base_url" not in st.session_state:
        st.session_state["smartolt_base_url"] = DEFAULT_SMARTOLT_BASE.rstrip("/")
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
        "smartolt": st.session_state["smartolt_base_url"],
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
        submitted = st.form_submit_button("Sincronizar")
        if submitted:
            payload = {"customer_id": int(customer_id)}
            success, status, data = call_api("POST", "/sync/customer", json=payload)
            display_response(success, status, data)


def provision_onu_section() -> None:
    st.subheader("2. Provisionar ONU en SmartOLT")
    with st.form("provision_form"):
        col1, col2, col3 = st.columns(3)
        customer_id = col1.number_input(
            "ID de cliente (opcional)",
            min_value=1,
            step=1,
            value=202,
        )
        use_customer_id = col1.checkbox("Incluir customer_id", value=True)
        olt_id = col2.number_input("OLT ID", min_value=1, step=1, value=2)
        board = col2.number_input("Board", min_value=0, step=1, value=3)
        pon_port = col3.number_input("Puerto PON", min_value=0, step=1, value=4)
        onu_sn = col3.text_input("Número de serie ONU", value="TESTSN00002")
        profile = st.text_input("Perfil (opcional)", value="Internet_100M")
        dry_run_choice = st.selectbox(
            "Dry-run",
            ("Usar configuración actual", "Forzar true", "Forzar false"),
        )
        submitted = st.form_submit_button("Provisionar")
        if submitted:
            payload: Dict[str, Any] = {
                "olt_id": int(olt_id),
                "board": int(board),
                "pon_port": int(pon_port),
                "onu_sn": onu_sn.strip(),
            }
            if profile.strip():
                payload["profile"] = profile.strip()
            if use_customer_id:
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
        dry_run = st.checkbox("Dry-run (solo reporte)", value=True)
        submitted = st.form_submit_button("Ejecutar baja")
        if submitted:
            payload: Dict[str, Any] = {"customer_id": int(customer_id)}
            if dry_run:
                payload["dry_run"] = True
            success, status, data = call_api(
                "POST", "/decommission/customer", json=payload
            )
            display_response(success, status, data)


def incidents_section() -> None:
    st.subheader("4. Incidentes registrados")
    col1, col2 = st.columns([1, 4])
    with col1:
        kind_filter = st.selectbox(
            "Filtrar por tipo",
            options=[
                "Todos",
                "missing_fields",
                "integration_disabled",
                "hardware_mismatch",
                "decommission_status_active",
                "decommission_missing_feature",
                "decommission_missing_onu",
            ],
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


def features_section() -> None:
    st.subheader("5. Features en GeoGrid")
    col1, col2 = st.columns([1, 1])
    with col1:
        if st.button("Listar features"):
            success, status, data = call_api("GET", "/features", service="geogrid")
            if success and isinstance(data, list):
                st.dataframe(data)
            else:
                display_response(success, status, data)
    with col2:
        if st.button("Ver GeoJSON"):
            success, status, data = call_api("GET", "/clients.geojson")
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


def isp_controls_section() -> None:
    st.subheader("7. Herramientas demo para ISP-Cube")
    col1, col2 = st.columns(2)
    with col1:
        with st.form("isp_flag_form"):
            st.markdown("**Activar/Desactivar integración**")
            customer_id = st.number_input(
                "ID de cliente", min_value=1, step=1, value=202, key="flag_customer"
            )
            enabled = st.checkbox("integration_enabled", value=True)
            submitted = st.form_submit_button("Actualizar flag")
            if submitted:
                payload = {"integration_enabled": enabled}
                success, status, data = call_api(
                    "PATCH",
                    f"/customers/{int(customer_id)}/flag",
                    service="isp",
                    json=payload,
                )
                display_response(success, status, data)
    with col2:
        with st.form("isp_status_form"):
            st.markdown("**Cambiar estado (activo/inactivo)**")
            customer_id = st.number_input(
                "ID de cliente ",
                min_value=1,
                step=1,
                value=707,
                key="status_customer",
            )
            status_value = st.selectbox(
                "Estado nuevo", options=["active", "inactive"], index=1
            )
            submitted = st.form_submit_button("Actualizar estado")
            if submitted:
                payload = {"status": status_value}
                success, status_code, data = call_api(
                    "PATCH",
                    f"/customers/{int(customer_id)}/status",
                    service="isp",
                    json=payload,
                )
                display_response(success, status_code, data)

    col3, col4 = st.columns([1, 2])
    with col3:
        if st.button("Listar clientes ISP-Cube"):
            success, status, data = call_api("GET", "/customers", service="isp")
            if success and isinstance(data, list):
                st.dataframe(data)
            else:
                display_response(success, status, data)
    with col4:
        with st.expander("Crear cliente de ejemplo"):
            with st.form("create_customer_form"):
                cid = st.number_input("customer_id", min_value=1, step=1, value=808)
                name = st.text_input("Nombre", "Cliente Demo")
                address = st.text_input("Dirección", "Calle Ejemplo 123")
                city = st.text_input("Ciudad", "Río Cuarto")
                col_lat, col_lon = st.columns(2)
                lat = col_lat.number_input("Latitud", value=-33.1245)
                lon = col_lon.number_input("Longitud", value=-64.3456)
                odb = st.text_input("ODB", "CAJA-XX")
                col_a, col_b, col_c = st.columns(3)
                olt_id = col_a.number_input("OLT ID", min_value=1, step=1, value=1)
                board = col_b.number_input("Board", min_value=0, step=1, value=1)
                pon = col_c.number_input("Puerto PON", min_value=0, step=1, value=1)
                onu_sn = st.text_input("ONU SN", "TESTSNDEMO")
                integration_enabled = st.checkbox("integration_enabled", value=True)
                status_val = st.selectbox(
                    "Estado", options=["active", "inactive"], index=0
                )
                submitted = st.form_submit_button("Crear cliente")
                if submitted:
                    payload = {
                        "customer_id": int(cid),
                        "name": name,
                        "address": address,
                        "city": city,
                        "lat": lat,
                        "lon": lon,
                        "odb": odb,
                        "olt_id": int(olt_id),
                        "board": int(board),
                        "pon": int(pon),
                        "onu_sn": onu_sn,
                        "integration_enabled": integration_enabled,
                        "status": status_val,
                    }
                    success, status_code, data = call_api(
                        "POST", "/customers", service="isp", json=payload
                    )
                    display_response(success, status_code, data)


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
    smartolt_url = st.sidebar.text_input(
        "URL SmartOLT", value=st.session_state["smartolt_base_url"]
    )
    if smartolt_url:
        st.session_state["smartolt_base_url"] = smartolt_url.rstrip("/")
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
    features_section()
    audits_section()
    isp_controls_section()


if __name__ == "__main__":
    main()
