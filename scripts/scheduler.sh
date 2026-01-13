#!/bin/bash
# ======================================================================================
# SCRIPT DE PLANIFICACIÓN DE TAREAS (SCHEDULER)
#
# DESCRIPCIÓN:
#   Este script actúa como el "corazón" del contenedor 'scheduler'.
#   Su función es ejecutar tareas periódicas (jobs) sin necesidad de un cron del sistema.
#   Se asegura de que el Orquestador y GeoGrid se mantengan sincronizados con ISP-Cube.
#
# TAREAS QUE EJECUTA:
#   1. Polling (poll_isp_connections.py): Busca nuevas instalaciones/bajas cada 10 min.
#   2. Retry (retry_incidents.py): Reintenta sincronizaciones fallidas cada 30 min.
#   3. Refresh Token (refresh-isp-token): Renueva las credenciales cada 6 horas.
#
# ======================================================================================

set -e

# Configuración de Intervalos (en segundos)
# Se pueden sobreescribir mediante variables de entorno en el docker-compose.yml

POLL_INTERVAL=${POLL_INTERVAL:-600}           # Por defecto: 10 minutos
RETRY_INTERVAL=${RETRY_INTERVAL:-1800}        # Por defecto: 30 minutos
REFRESH_INTERVAL=${REFRESH_INTERVAL:-21600}   # Por defecto: 6 horas (token suele durar mucho)

# Inicializamos contadores con el valor del intervalo
# Esto fuerza a que TODAS las tareas corran inmediatamente al arrancar el contenedor.
poll_counter=$POLL_INTERVAL
retry_counter=$RETRY_INTERVAL
refresh_counter=$REFRESH_INTERVAL

# ======================================================================================
# FASE 1: ESPERA ACTIVA (HEALTHCHECK)
# No empezamos a trabajar hasta que el contenedor principal ('orchestrator') responda.
# ======================================================================================
echo "[scheduler] Iniciando... Esperando a que el servicio Orquestador esté listo..."
until curl -sf http://orchestrator:8000/health > /dev/null 2>&1; do
    echo "[scheduler] Orquestador no disponible todavía, reintentando en 5 segundos..."
    sleep 5
done
echo "[scheduler] ¡Conexión establecida con el Orquestador! Iniciando bucle de tareas."

# ======================================================================================
# FASE 2: BUCLE PRINCIPAL
# ======================================================================================
# Definimos el "latido" del reloj (cada cuántos segundos despierta el scheduler)
TICK=60

while true; do
    # Incrementamos contadores
    poll_counter=$((poll_counter + TICK))
    retry_counter=$((retry_counter + TICK))
    refresh_counter=$((refresh_counter + TICK))

    # --- TAREA 1: Sincronización de Alta/Bajas ---
    if [ $poll_counter -ge $POLL_INTERVAL ]; then
        echo "[scheduler] $(date -Iseconds) Ejecutando Sincronización (poll_isp_connections.py)..."
        # '|| true' evita que el scheduler se detenga si el script de python falla
        python3 /app/scripts/poll_isp_connections.py --lookback-hours 1 || true
        poll_counter=0
    fi

    # --- TAREA 2: Reintento de Fallos (Auto-Healing) ---
    if [ $retry_counter -ge $RETRY_INTERVAL ]; then
        echo "[scheduler] $(date -Iseconds) Ejecutando Reintentos (retry_incidents.py)..."
        python3 /app/scripts/retry_incidents.py --lookback-hours 6 || true
        retry_counter=0
    fi

    # --- TAREA 3: Renovación Preventiva de Credenciales ---
    if [ $refresh_counter -ge $REFRESH_INTERVAL ]; then
        echo "[scheduler] $(date -Iseconds) Solicitando renovación de Token ISP..."
        curl -sf -X POST http://orchestrator:8000/ops/refresh-isp-token || echo "[scheduler] ADVERTENCIA: Falló la renovación del token."
        refresh_counter=0
    fi

    # Dormir hasta el siguiente ciclo
    sleep $TICK
done
