#!/bin/bash
# Scheduler para ejecutar los jobs de polling del orquestador
# Este script corre en un loop infinito ejecutando cada job según su intervalo

set -e

# Intervalos en segundos
POLL_INTERVAL=${POLL_INTERVAL:-600}           # 10 minutos
RETRY_INTERVAL=${RETRY_INTERVAL:-1800}        # 30 minutos
REFRESH_INTERVAL=${REFRESH_INTERVAL:-21600}   # 6 horas

# Contadores (Iniciados en el intervalo para ejecutar inmediatamente al arranque)
poll_counter=$POLL_INTERVAL
retry_counter=$RETRY_INTERVAL
refresh_counter=$REFRESH_INTERVAL

# Esperar a que el orquestador esté listo
echo "[scheduler] Esperando a que el orquestador esté listo..."
until curl -sf http://orchestrator:8000/health > /dev/null 2>&1; do
    echo "[scheduler] Orquestador no disponible, reintentando en 5s..."
    sleep 5
done
echo "[scheduler] Orquestador listo!"

# Loop principal - cada 60 segundos
TICK=60

while true; do
    poll_counter=$((poll_counter + TICK))
    retry_counter=$((retry_counter + TICK))
    refresh_counter=$((refresh_counter + TICK))

    # Poll ISP connections (cada 10 minutos)
    if [ $poll_counter -ge $POLL_INTERVAL ]; then
        echo "[scheduler] $(date -Iseconds) Ejecutando poll_isp_connections.py..."
        python3 /app/scripts/poll_isp_connections.py --lookback-hours 1 || true
        poll_counter=0
    fi

    # Retry incidents (cada 30 minutos)
    if [ $retry_counter -ge $RETRY_INTERVAL ]; then
        echo "[scheduler] $(date -Iseconds) Ejecutando retry_incidents.py..."
        python3 /app/scripts/retry_incidents.py --lookback-hours 6 || true
        retry_counter=0
    fi

    # Refresh ISP token (cada 6 horas)
    if [ $refresh_counter -ge $REFRESH_INTERVAL ]; then
        echo "[scheduler] $(date -Iseconds) Ejecutando refresh_isp_token.py..."
        python3 /app/scripts/refresh_isp_token.py || true
        refresh_counter=0
    fi

    sleep $TICK
done
