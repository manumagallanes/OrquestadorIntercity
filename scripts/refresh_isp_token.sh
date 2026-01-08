#!/usr/bin/env bash
set -euo pipefail

# Buscar .env en múltiples ubicaciones
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

if [[ -n "${1:-}" && -f "$1" ]]; then
  ENV_FILE="$1"
elif [[ -f ".env" ]]; then
  ENV_FILE=".env"
elif [[ -f "../.env" ]]; then
  ENV_FILE="../.env"
elif [[ -f "$PROJECT_ROOT/../.env" ]]; then
  ENV_FILE="$PROJECT_ROOT/../.env"
else
  echo "[ERROR] No se encontró archivo .env" >&2
  echo "Buscado en: .env, ../.env, $PROJECT_ROOT/../.env" >&2
  exit 1
fi

echo "[INFO] Usando archivo: $ENV_FILE"

set -a
. "$ENV_FILE"
set +a

if [[ -z "${ISP_BASE_URL:-}" || -z "${ISP_API_KEY:-}" || -z "${ISP_CLIENT_ID:-}" || -z "${ISP_USERNAME:-}" || -z "${ISP_PASSWORD:-}" ]]; then
  echo "[ERROR] Faltan variables ISP_* en $ENV_FILE" >&2
  exit 1
fi

TOKEN_ENDPOINT="${ISP_BASE_URL%/}/sanctum/token"
NEW_TOKEN=$(curl -s -X POST "$TOKEN_ENDPOINT" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json' \
  -H "api-key: ${ISP_API_KEY}" \
  -H "client-id: ${ISP_CLIENT_ID}" \
  -H 'login-type: api' \
  -d "{\"username\":\"${ISP_USERNAME}\",\"password\":\"${ISP_PASSWORD}\"}" | jq -r '.token // empty')

if [[ -z "$NEW_TOKEN" ]]; then
  echo "[ERROR] No se pudo obtener un token nuevo" >&2
  exit 1
fi

TMP_FILE="$(mktemp)"
perl -pe "s/^ISP_BEARER=.*/ISP_BEARER='Bearer ${NEW_TOKEN}'/" "$ENV_FILE" > "$TMP_FILE"
cat "$TMP_FILE" > "$ENV_FILE"
rm "$TMP_FILE"

if command -v docker >/dev/null 2>&1; then
  docker compose restart orchestrator >/dev/null
  echo "[INFO] Token renovado y contenedor reiniciado."
else
  echo "[INFO] Token renovado. Reiniciá el servicio manualmente."
fi
