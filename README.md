# Orquestador Intercity – Documentación Técnica

Este repositorio contiene el orquestador que integra **ISP‑Cube** (catálogo de clientes y altas) con **GeoGrid** (plataforma geoespacial). El objetivo es **automatizar** la creación/actualización de clientes y el **tendido lógico** (drop) a partir de las altas, con validaciones, auditoría y monitoreo.

El documento está orientado a un **trabajo final de prácticas profesionales**, con foco en claridad, reproducibilidad y criterios técnicos verificables.

---

## 1. Alcance y objetivos

**Alcance principal**
- Detectar movimientos en ISP‑Cube (altas, cambios de FTTH) y sincronizarlos con GeoGrid.
- Validar datos críticos (coordenadas, caja, puerto, etc.).
- Registrar auditorías e incidentes y exponer métricas para observabilidad.

**Objetivo operativo**
- Que una alta en ISP‑Cube se traduzca automáticamente en:
  1) Cliente en GeoGrid.
  2) Casita (ponto de acesso) con coordenadas.
  3) Asignación del drop al puerto correcto.

**No objetivo**
- Inventariar la red en GeoGrid ni corregir datos históricos de ISP‑Cube. El orquestador **consume** esos datos, no los genera.

---

## 2. Arquitectura

**Servicios (Docker Compose)**
| Servicio         | Puerto | Rol |
|------------------|:------:|-----|
| `orchestrator`   | 8000   | API FastAPI, reglas de negocio, métricas y auditoría |
| `isp-mock`       | 8001   | Mock de ISP‑Cube (entorno de laboratorio) |
| `geogrid-mock`   | 8002   | Mock de GeoGrid (entorno de laboratorio) |
| `prometheus`     | 9090   | Métricas del orquestador |
| `grafana`        | 3000   | Paneles de monitoreo |
| `ui`             | 8501   | Interfaz Streamlit para pruebas |

**Diagrama**
- Archivo: `diagrama.mmd` (Mermaid)
- Flujo conceptual: ISP‑Cube → Orquestador → GeoGrid

---

## 3. Flujo funcional (alto nivel)

1) **ISP‑Cube** registra una alta o un cambio en FTTH.
2) El **poller** consulta `connections_provisioning_logs`.
3) El **orquestador** valida datos y sincroniza con GeoGrid.
4) Se crea/actualiza cliente, casita y drop.
5) Se registran **auditorías**, **incidentes** y **métricas**.

**Nota crítica:**
El cron **solo procesa movimientos que ISP‑Cube emite en `connections_provisioning_logs`**. Si no hay movimiento, no hay sincronización automática.

---

## 4. Requisitos de datos (ISP‑Cube)

Para que el flujo sea automático y sin errores, la conexión debe tener:
- **Lat/Lon** válidos (dentro del rango permitido).
- **Caja FTTH** (sigla/nombre coincide con GeoGrid).
- **Puerto FTTH** (número de puerto).

Campos usuales en ISP‑Cube:
- `connection_ftth_box` (ej. `CAJA_MANUEL1`)
- `connection_ftth_port` (número de puerto)
- `connection_lat`, `connection_lng`

Si faltan, el orquestador genera incidentes y **no puede crear el drop correctamente**.

---

## 5. GeoGrid: integración recomendada

La integración sigue el flujo sugerido por GeoGrid:
1) **Crear cliente** (`/clientes`) con `codigoIntegracao`.
2) **Obtener idCliente** (`/clientes/integrado/{codigoIntegracao}`).
3) **Atender** (`/integracao/atender`) con:
   - `idPorta` (puerto en la caja/CTO)
   - `idCliente`
   - `local` con coordenadas y `idItemRedeCliente`
   - `idCaboTipo` opcional (Drop) y `pontos`.

**`codigoIntegracao`**
- Se usa el **connection_id** de ISP‑Cube. Eso evita conflictos y permite trazabilidad.

**Comentario del drop**
- Se escribe un comentario en la porta con formato:
  - `Drop - NOMBRE CLIENTE`
  - Si se repite, se agrega sufijo: `Drop - NOMBRE CLIENTE 2`, etc.

---

## 6. Configuración

### 6.1 Archivos de entorno
`config/environments/*.json` definen:
- URL base por servicio
- Timeouts
- Reintentos
- Circuit breakers

### 6.2 Variables principales
- `ORCHESTRATOR_ENV` (ej. `dev`)
- `ISP_BASE_URL`, `ISP_API_KEY`, `ISP_CLIENT_ID`, `ISP_USERNAME`, `ISP_BEARER`
- `GEOGRID_BASE_URL`, `GEOGRID_BEARER`
- `GEOGRID_PASTA_ID`
- `GEOGRID_CABO_TIPO_NAME` (ej. `Drop Acometidas`) o `GEOGRID_CABO_TIPO_ID`
- `ORCHESTRATOR_GEOGRID_AUTO_ATTEND=true`
- `ORCHESTRATOR_ALLOW_COORDINATE_FALLBACK=true/false`
- `ORCHESTRATOR_ALLOW_MISSING_NETWORK_KEYS=true/false`
- `ORCHESTRATOR_MIN_START_DATE=YYYY-MM-DD`

---

## 7. Puesta en marcha

```bash
docker compose up --build
```

Accesos:
- API: http://localhost:8000/docs
- Grafana: http://localhost:3000 (admin/admin)
- Prometheus: http://localhost:9090
- UI: http://localhost:8501

---

## 8. Poller ISP → Orquestador

Script: `scripts/poll_isp_connections.py`

Ejemplos:
```bash
# Primera corrida o recuperación
rm -f .state/connections_provisioning.cursor
./scripts/poll_isp_connections.py --lookback-hours 24

# Ventana acotada
./scripts/poll_isp_connections.py --since "2026-01-05T16:45:00Z"
```

**Cron actual (15 min):**
```
*/15 * * * * cd /home/manu/Desktop/orquestador_intercity-develop && ./scripts/poll_isp_connections.py --lookback-hours 6 >> logs/poll_job.log 2>&1
```

**Reconcilación (sin cursor):**
Script: `scripts/replay_provisioning.py`  
Reprocesa los movimientos del día (o una ventana) sin tocar el cursor principal.

Ejemplos:
```bash
./scripts/replay_provisioning.py
./scripts/replay_provisioning.py --lookback-hours 24
```

**Cron recomendado (cada 2 h):**
```
7 */2 * * * cd /home/manu/Desktop/orquestador_intercity-develop && ./scripts/replay_provisioning.py --lookback-hours 24 >> logs/replay_provisioning.log 2>&1
```

---

## 9. Reintentos automáticos de incidentes

Para cubrir cambios que **no generan logs**, se agregó un reintento periódico:

```
5 */2 * * * cd /home/manu/Desktop/orquestador_intercity-develop && ./scripts/retry_incidents.py --lookback-hours 24 >> logs/retry_incidents.log 2>&1
```

Esto re‑ejecuta `/sync/customer` para incidentes recientes (últimas 24h).

---

## 10. Monitoreo y alertas

Prometheus consume `/metrics` del orquestador.

**Alertas configuradas** (archivo `monitoring/alerts.yml`):
- Incidentes abiertos
- Errores GeoGrid
- Errores de sync
- Pending en sync
- Faltantes de caja/puerto
- Coordenadas inválidas
- Campos obligatorios faltantes

**Métricas clave**
- `orchestrator_customer_sync_total`
- `orchestrator_incidents_total`
- `orchestrator_integration_errors_total`
- `orchestrator_request_latency_seconds`

---

## 11. Procedimiento de prueba (entorno real)

1) Preparar caja y puertos en GeoGrid.
2) En ISP‑Cube crear la conexión con:
   - Caja FTTH y puerto
   - Lat/Lon correctos
3) Esperar al cron (15 min) o forzar el poller.
4) Verificar en GeoGrid: casita + drop + comentario.
5) Revisar Grafana (syncs, incidentes).

---

## 12. Troubleshooting rápido

**No aparece nada en GeoGrid**
- Verificar si hay movimiento en `connections_provisioning_logs`.
- Confirmar `ftth_port_id` y `ftth_port.nro` en ISP‑Cube.

**401 en ISP‑Cube**
- Renovar bearer con `scripts/refresh_isp_token.sh`.

**GeoGrid responde `erro de conexão`**
- Revisar Token Google en GeoGrid y facturación.

**Conflictos de porta**
- Verificar puerto disponible o `geogrid_assignment_conflict`.

---

## 13. Scripts útiles

- `scripts/poll_isp_connections.py` → poller ISP → orquestador
- `scripts/retry_incidents.py` → reintento de incidentes
- `scripts/replay_provisioning.py` → reconciliación sin mover cursor
- `scripts/recomment_drop.py` → reescribe comentario del drop (opcional)

---

## 14. Estado del proyecto (criterios de éxito)

El sistema está listo para pruebas controladas cuando:
- ISP‑Cube emite correctamente los movimientos.
- GeoGrid tiene infraestructura base (cajas/puertos).
- El orquestador procesa sin incidentes críticos.

En producción, la calidad de datos es el factor más determinante.

---

## 15. Licencia

Ver `LICENSE`.
