import os
from datetime import datetime
from typing import Any, Dict, Optional
from pathlib import Path

import httpx
import streamlit as st
import pandas as pd

# ============================================================
# CONFIGURACIÓN
# ============================================================

DEFAULT_BASE_URL = os.getenv("ORCHESTRATOR_BASE_URL", "http://localhost:8000")
APP_USER_HEADER_NAME = os.getenv("ORCHESTRATOR_USER_HEADER", "X-Orchestrator-User")

SCRIPT_DIR = Path(__file__).parent
LOGO_PATH = SCRIPT_DIR / "logo.png"

# ============================================================
# ESTILOS CSS - DISEÑO CORPORATIVO SOBRIO
# ============================================================

CUSTOM_CSS = """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');
    
    :root {
        --primary: #007589;
        --primary-dark: #005566;
        --bg-dark: #1e2530;
        --bg-card: #2a323e;
        --text-main: #e8eaed;
        --text-muted: #9aa0a6;
        --border: #3c4555;
        --success: #34a853;
        --error: #ea4335;
    }
    
    * { font-family: 'Inter', -apple-system, sans-serif !important; }
    
    .stApp { background-color: var(--bg-dark); }
    
    /* Sidebar */
    [data-testid="stSidebar"] {
        background-color: var(--primary-dark);
        border-right: 1px solid var(--border);
    }
    [data-testid="stSidebar"] * { color: white !important; }
    [data-testid="stSidebar"] .stTextInput input {
        background-color: rgba(255,255,255,0.1);
        border: 1px solid rgba(255,255,255,0.2);
        border-radius: 6px;
    }
    [data-testid="stSidebar"] .stButton button {
        background-color: rgba(255,255,255,0.15);
        border: 1px solid rgba(255,255,255,0.25);
        border-radius: 6px;
    }
    [data-testid="stSidebar"] .stButton button:hover {
        background-color: rgba(255,255,255,0.25);
    }
    
    /* Metrics */
    [data-testid="stMetric"] {
        background-color: var(--bg-card);
        padding: 1rem 1.25rem;
        border-radius: 8px;
        border: 1px solid var(--border);
    }
    [data-testid="stMetricLabel"] {
        color: var(--text-muted) !important;
        font-size: 0.75rem;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    [data-testid="stMetricValue"] {
        color: var(--text-main) !important;
        font-size: 1.75rem !important;
        font-weight: 600;
    }
    [data-testid="stMetricDelta"] { font-size: 0.7rem; }
    
    /* Tabs */
    .stTabs [data-baseweb="tab-list"] {
        background-color: var(--bg-card);
        border-radius: 8px;
        padding: 4px;
        gap: 4px;
        border: 1px solid var(--border);
    }
    .stTabs [data-baseweb="tab"] {
        background-color: transparent;
        border-radius: 6px;
        padding: 0.5rem 1.25rem;
        color: var(--text-muted) !important;
        font-size: 0.875rem;
    }
    .stTabs [aria-selected="true"] {
        background-color: var(--primary) !important;
        color: white !important;
    }
    
    /* Buttons */
    .stButton > button {
        background-color: var(--primary);
        color: white !important;
        border: none;
        border-radius: 6px;
        padding: 0.5rem 1.25rem;
        font-weight: 500;
        font-size: 0.875rem;
    }
    .stButton > button:hover {
        background-color: var(--primary-dark);
    }
    
    /* Forms */
    .stForm {
        background-color: var(--bg-card);
        padding: 1.25rem;
        border-radius: 8px;
        border: 1px solid var(--border);
    }
    
    /* Inputs */
    .stTextInput input, .stNumberInput input, .stSelectbox > div > div {
        background-color: var(--bg-dark) !important;
        border: 1px solid var(--border) !important;
        border-radius: 6px !important;
        color: var(--text-main) !important;
        font-size: 0.875rem;
    }
    .stTextInput input:focus, .stNumberInput input:focus {
        border-color: var(--primary) !important;
    }
    
    /* Headers */
    h1 { color: var(--text-main) !important; font-weight: 600; font-size: 1.5rem !important; }
    h2 { color: var(--text-main) !important; font-weight: 600; font-size: 1.25rem !important; }
    h3 { color: var(--text-main) !important; font-weight: 500; font-size: 1rem !important; }
    p, span, label { color: var(--text-muted) !important; }
    
    /* Cards */
    .info-card {
        background-color: var(--bg-card);
        padding: 1rem;
        border-radius: 8px;
        border: 1px solid var(--border);
        margin-bottom: 0.75rem;
    }
    
    /* Activity row */
    .activity-row {
        background-color: var(--bg-card);
        padding: 0.75rem 1rem;
        border-radius: 6px;
        margin-bottom: 0.5rem;
        border-left: 3px solid var(--primary);
        display: flex;
        justify-content: space-between;
        align-items: center;
    }
    .activity-row:hover { background-color: #353d4a; }
    
    /* Status badge */
    .status-badge {
        display: inline-block;
        padding: 0.25rem 0.75rem;
        border-radius: 4px;
        font-size: 0.7rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    .status-online { background-color: var(--success); color: white; }
    .status-offline { background-color: var(--error); color: white; }
    
    /* Alerts */
    .stSuccess { background-color: rgba(52,168,83,0.15); border: 1px solid rgba(52,168,83,0.3); border-radius: 6px; }
    .stError { background-color: rgba(234,67,53,0.15); border: 1px solid rgba(234,67,53,0.3); border-radius: 6px; }
    .stInfo { background-color: rgba(0,117,137,0.15); border: 1px solid rgba(0,117,137,0.3); border-radius: 6px; }
    
    /* Dividers */
    hr { border-color: var(--border) !important; margin: 1.5rem 0 !important; }
    
    /* DataFrames */
    .stDataFrame { border-radius: 8px; overflow: hidden; }
    
    /* Expanders */
    .streamlit-expanderHeader {
        background-color: var(--bg-card);
        border-radius: 6px;
        color: var(--text-main) !important;
    }
    
    /* Hide Streamlit branding */
    #MainMenu, footer, header { visibility: hidden; }
</style>
"""

# ============================================================
# PAGE CONFIG
# ============================================================

st.set_page_config(
    page_title="Intercity - Sistema Orquestador",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

# ============================================================
# HELPERS
# ============================================================

def init_state():
    if "base_url" not in st.session_state:
        st.session_state["base_url"] = DEFAULT_BASE_URL.rstrip("/")
    if "audit_user" not in st.session_state:
        st.session_state["audit_user"] = "admin"

def call_api(method, path, *, json_body=None, params=None, timeout=15.0):
    url = f"{st.session_state['base_url']}{path}"
    try:
        with httpx.Client(timeout=timeout) as client:
            headers = {APP_USER_HEADER_NAME: st.session_state.get("audit_user", "admin")}
            response = client.request(method, url, json=json_body, params=params, headers=headers)
        try:
            data = response.json()
        except:
            data = response.text
        return response.is_success, response.status_code, data
    except httpx.HTTPError as exc:
        return False, 0, {"error": str(exc)}

def display_response(success, status, data):
    if success:
        st.success("Operación completada exitosamente")
    else:
        st.error(f"Error en la operación (HTTP {status})")
    with st.expander("Ver respuesta", expanded=not success):
        st.json(data)

# ============================================================
# SIDEBAR
# ============================================================

def render_sidebar():
    with st.sidebar:
        if LOGO_PATH.exists():
            st.image(str(LOGO_PATH), use_container_width=True)
        
        st.markdown("---")
        st.markdown("**Configuración**")
        
        st.session_state["base_url"] = st.text_input("URL Orquestador", st.session_state["base_url"])
        st.session_state["audit_user"] = st.text_input("Usuario", st.session_state.get("audit_user", "admin"))
        
        st.markdown("---")
        st.markdown("**Acciones**")
        
        if st.button("Ejecutar Reconciliación", use_container_width=True):
            with st.spinner("Ejecutando..."):
                ok, _, _ = call_api("POST", "/analytics/reconciliation/run")
            st.success("Completado") if ok else st.error("Error")
        
        st.markdown("---")
        
        # System status
        success, _, _ = call_api("GET", "/config", timeout=3)
        status_class = "status-online" if success else "status-offline"
        status_text = "ONLINE" if success else "OFFLINE"
        
        st.markdown(f"""
        <div style="text-align:center; padding:1rem;">
            <div style="color:#9aa0a6; font-size:0.7rem; text-transform:uppercase; margin-bottom:0.5rem;">Estado del Sistema</div>
            <span class="status-badge {status_class}">{status_text}</span>
            <div style="color:#9aa0a6; font-size:0.7rem; margin-top:1rem;">{datetime.now().strftime('%d/%m/%Y %H:%M')}</div>
        </div>
        """, unsafe_allow_html=True)

# ============================================================
# DASHBOARD
# ============================================================

def render_dashboard():
    st.markdown("## Panel de Control")
    
    # Metrics row
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        success, _, _ = call_api("GET", "/config", timeout=3)
        st.metric("Estado", "Operativo" if success else "Error", delta="Sistema activo" if success else "Revisar")
    
    with col2:
        success, _, data = call_api("GET", "/incidents", timeout=5)
        count = len(data) if success and isinstance(data, list) else 0
        st.metric("Incidentes", count, delta="Pendientes")
    
    with col3:
        success, _, data = call_api("GET", "/analytics/customer-events", params={"lookback_days": 1}, timeout=5)
        altas = sum(1 for e in data if e.get("event_type") == "alta") if success and isinstance(data, list) else 0
        st.metric("Altas (24h)", altas, delta="Nuevas conexiones")
    
    with col4:
        bajas = sum(1 for e in data if e.get("event_type") == "baja") if success and isinstance(data, list) else 0
        st.metric("Bajas (24h)", bajas, delta="Desconexiones")
    
    st.markdown("---")
    
    # Two columns
    col_left, col_right = st.columns([1, 1])
    
    with col_left:
        st.markdown("### Actividad Reciente")
        success, _, data = call_api("GET", "/audits", params={"limit": 8})
        if success and isinstance(data, list) and data:
            for item in data:
                action = item.get("action", "-").upper()
                status = item.get("status", "-")
                target = item.get("target_id", "-")
                ts = str(item.get("timestamp", ""))[:16].replace("T", " ")
                
                status_color = "#34a853" if status == "success" else "#fbbc04" if status == "warning" else "#ea4335"
                
                st.markdown(f"""
                <div class="activity-row">
                    <div>
                        <strong style="color:#e8eaed">{action}</strong>
                        <span style="color:#9aa0a6; margin-left:8px;">ID: {target}</span>
                    </div>
                    <div style="text-align:right;">
                        <span style="color:{status_color}; font-size:0.75rem;">{status.upper()}</span>
                        <div style="color:#9aa0a6; font-size:0.7rem;">{ts}</div>
                    </div>
                </div>
                """, unsafe_allow_html=True)
        else:
            st.info("Sin actividad registrada")
    
    with col_right:
        st.markdown("### Tendencia Semanal")
        success, _, data = call_api("GET", "/analytics/customer-events", params={"lookback_days": 7})
        if success and isinstance(data, list) and data:
            try:
                df = pd.DataFrame(data)
                if "timestamp" in df.columns:
                    df["date"] = pd.to_datetime(df["timestamp"], errors="coerce").dt.date
                    df = df.dropna(subset=["date"])
                    if not df.empty:
                        daily = df.groupby(["date", "event_type"]).size().unstack(fill_value=0)
                        st.bar_chart(daily, color=["#007589", "#ea4335"] if len(daily.columns) > 1 else ["#007589"])
                    else:
                        st.info("Sin datos disponibles")
            except:
                st.info("Error procesando datos")
        else:
            st.info("Sin eventos registrados")



# ============================================================
# INCIDENTS
# ============================================================

def render_incidents():
    st.markdown("## Incidentes")
    
    c1, c2 = st.columns([3, 1])
    with c1:
        kind = st.selectbox("Filtrar por tipo", ["Todos", "missing_network_keys", "manual_cleanup_required", "geogrid_conflict"])
    with c2:
        st.markdown("")
        if st.button("Actualizar", use_container_width=True):
            st.rerun()
    
    params = {} if kind == "Todos" else {"kind": kind}
    ok, _, data = call_api("GET", "/incidents", params=params)
    
    if ok and isinstance(data, list):
        if not data:
            st.success("No hay incidentes pendientes")
        else:
            for idx, item in enumerate(data):
                itype = item.get("incident_type", "desconocido")
                cid = item.get("customer_id", "-")
                ts = str(item.get("timestamp", ""))[:19].replace("T", " ")
                
                with st.expander(f"{itype} - Cliente {cid} ({ts})"):
                    st.json(item.get("details", {}))
    else:
        st.error("Error al cargar incidentes")

# ============================================================
# EXPLORER
# ============================================================

def render_explorer():
    st.markdown("## Explorador")
    
    tab1, tab2 = st.tabs(["Eventos", "Auditoría"])
    
    with tab1:
        c1, c2 = st.columns([3, 1])
        days = c1.slider("Período (días)", 1, 30, 7)
        load = c2.button("Cargar", use_container_width=True)
        
        if load:
            with st.spinner("Cargando..."):
                ok, _, data = call_api("GET", "/analytics/customer-events", params={"lookback_days": days})
            if ok and isinstance(data, list) and data:
                df = pd.DataFrame(data)
                if "timestamp" in df.columns:
                    df["timestamp"] = df["timestamp"].astype(str).str[:19]
                cols = ["timestamp", "event_type", "customer_id", "customer_name", "source"]
                st.dataframe(df[[c for c in cols if c in df.columns]], use_container_width=True, height=400)
            else:
                st.info("Sin eventos en el período")
    
    with tab2:
        c1, c2, c3 = st.columns(3)
        action = c1.selectbox("Acción", ["Todas", "sync", "provision", "decommission"])
        user = c2.text_input("Usuario", key="aud_user")
        limit = c3.number_input("Límite", 10, 500, 50)
        
        if st.button("Buscar"):
            params = {"limit": limit}
            if action != "Todas":
                params["action"] = action
            if user.strip():
                params["user"] = user.strip()
            
            with st.spinner("Buscando..."):
                ok, _, data = call_api("GET", "/audits", params=params)
            if ok and isinstance(data, list):
                df = pd.DataFrame(data)
                if "timestamp" in df.columns:
                    df["timestamp"] = df["timestamp"].astype(str).str[:19]
                st.dataframe(df, use_container_width=True, height=400)
            else:
                st.error("Error en la búsqueda")

# ============================================================
# MAIN
# ============================================================

def main():
    init_state()
    render_sidebar()
    
    # Header
    c1, c2 = st.columns([1, 8])
    with c1:
        if LOGO_PATH.exists():
            st.image(str(LOGO_PATH), width=80)
    with c2:
        st.markdown("# Sistema Orquestador")
        st.caption("Gestión de provisión FTTH | Intercity Telecomunicaciones")
    
    st.markdown("---")
    
    # Navigation
    tabs = st.tabs(["Dashboard", "Incidentes", "Explorador"])
    
    with tabs[0]:
        render_dashboard()
    with tabs[1]:
        render_incidents()
    with tabs[2]:
        render_explorer()

if __name__ == "__main__":
    main()
