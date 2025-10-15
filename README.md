# Orquestador Intercity – Guía Completa

Este repositorio trae un entorno **100 % local** para ensayar el flujo de integración entre ISP-Cube, GeoGrid y SmartOLT sin tocar sistemas reales. Todo corre con contenedores Docker y mocks livianos que imitan el comportamiento de las APIs productivas. La idea es que puedas levantarlo, probar casos reales de punta a punta y monitorear el resultado desde Grafana.

---

## 1. Qué vas a encontrar

| Servicio            | Puerto | Descripción resumida                                                                 |
|---------------------|:------:|---------------------------------------------------------------------------------------|
| `orchestrator`      |  8000  | FastAPI que coordina los tres sistemas. Exponer `/sync/customer`, `/provision/onu`, etc. |
| `isp-mock`          |  8001  | Catálogo de clientes falso con flags, OLTs, boards, coordenadas…                      |
| `geogrid-mock`      |  8002  | API de features GeoJSON en memoria.                                                   |
| `smartolt-mock`     |  8003  | API de autorización de ONUs.                                                          |
| `prometheus`        |  9090  | Recolecta métricas del orquestador.                                                   |
| `grafana`           |  3000  | Dashboards prearmados (incluye incidentes en vivo).                                   |
| `ui`                |  8501  | Panel Streamlit para operar sin usar `curl`.                                          |

Los contenedores se comunican vía red interna de Docker, así que no necesitás dependencias externas ni credenciales reales.

---

## 2. Requisitos previos

1. Docker y Docker Compose (plugin moderno `docker compose` o el binario `docker-compose`).
2. Python no es obligatorio, salvo que quieras ejecutar scripts fuera del contenedor.
3. Puertos libres 8000–8501, 3000 y 9090.

---

## 3. Arranque rápido

```bash
# Clonar el repo (o descargarlo) y posicionarse en la raíz
docker compose up --build
```

Eso levanta todos los servicios con la configuración por defecto (entorno `dev`). Cuando los contenedores estén listos:

- Orchestrator API: http://localhost:8000/docs  
- UI Streamlit: http://localhost:8501  
- Grafana: http://localhost:3000 (usuario `admin`, contraseña `admin`)  
- Prometheus: http://localhost:9090

Para volver todo al estado inicial:

```bash
curl -X POST http://localhost:8000/reset
```

Ese endpoint llama al `/reset` de cada mock y limpia auditorías/incidentados en memoria.

---

## 4. Configuración por entorno y regiones

El orquestador sabe cargar configuraciones según el entorno elegido. Todos los archivos viven en `config/environments/`:

- `dev.json` – apunta a los mocks locales (sin TLS, timeout corto).
- `staging.json` – ejemplo con URLs HTTPS y distintas regiones (BR, MX).
- `production.json` – placeholders que podés adaptar a tus backends reales.

### Variables clave

| Variable | Significado | Ejemplo |
|----------|-------------|---------|
| `ORCHESTRATOR_ENV` | Entorno base (`dev`, `staging`, `production`). | `ORCHESTRATOR_ENV=staging` |
| `ISP_REGION` / `GEOGRID_REGION` / `SMARTOLT_REGION` | Selecciona la región dentro del entorno. | `ISP_REGION=mx` |
| `ISP_BASE_URL` (`GEOGRID_BASE_URL`, `SMARTOLT_BASE_URL`) | Override puntual del endpoint por defecto. | `ISP_BASE_URL=http://localhost:9000` |
| `DRY_RUN` | Cambia el valor inicial del flag `dry_run`. | `DRY_RUN=false` |

Ejemplo: correr el orquestador usando la configuración de staging para México.

```bash
ORCHESTRATOR_ENV=staging \
ISP_REGION=mx \
GEOGRID_REGION=mx \
SMARTOLT_REGION=mx \
docker compose up --build
```

En cualquier momento podés consultar qué config está activa con:

```bash
curl http://localhost:8000/config
```

Respuesta típica:

```json
{
  "environment": "dev",
  "dry_run": true,
  "services": {
    "isp": {
      "default_region": "default",
      "base_url": "http://isp-mock:8001",
      "timeout_seconds": 10.0,
      "verify_tls": false
    },
    ...
  }
}
```

---

## 5. Flujos principales (vía `curl`)

> Todos los ejemplos asumen que estás en `dev` con los mocks locales.

### 5.1 Sincronizar un cliente hacia GeoGrid

```bash
curl -X POST http://localhost:8000/sync/customer \
  -H 'Content-Type: application/json' \
  -d '{"customer_id":202}'
```

- Primera ejecución: `{"feature_id":"feature_00001","action":"created"}`.
- Segunda ejecución: `{"feature_id":"feature_00001","action":"updated"}` (manejo del 409).

Para inspeccionar el resultado:

```bash
curl http://localhost:8002/features           # features en GeoGrid
curl http://localhost:8000/audits?action=sync # auditoría
```

### 5.2 Provisionar una ONU

```bash
curl -X POST http://localhost:8000/provision/onu \
  -H 'Content-Type: application/json' \
  -d '{"customer_id":202,"olt_id":2,"board":3,"pon_port":4,"onu_sn":"TESTSN00002","dry_run":false}'
```

- Respuesta típica: `{"status":"authorized","authorization":...}`  
- Segunda llamada con el mismo payload: `{"status":"already_authorized", ...}` (idempotente).

### 5.3 Dar de baja un cliente

```bash
curl -X POST http://localhost:8000/decommission/customer \
  -H 'Content-Type: application/json' \
  -d '{"customer_id":707,"dry_run":false}'
```

Si no existe la feature ni la ONU, el orquestador registra los incidentes `decommission_missing_feature` / `decommission_missing_onu`. Verificalo en:

```bash
curl http://localhost:8000/incidents
```

### 5.4 Dry-run

- Flag global: `curl -X POST http://localhost:8000/config -d '{"dry_run": false}'`.
- Flag por petición: incluir `"dry_run": false` en el JSON del `/provision/onu`.


---

## 6. Suite de smoke tests sugerida

Tenés una checklist lista en el README original y replicada acá para que puedas repetirla cuando quieras. No es obligatoria, pero ayuda a validar que todo funciona.

1. **Reset inicial**  
   `docker compose up --build` + `curl -X POST http://localhost:8000/reset`

2. **Sincronización básica (cliente 202)**  
   Ejecutá `/sync/customer` dos veces y revisá features + auditoría.

3. **Flag de integración deshabilitado**  
   `PATCH` (~> false), intentar sync y chequear incidente.

4. **Datos incompletos (cliente 404)**  
   Esperar `422` + incidente `missing_fields`.

5. **Conflicto GeoGrid**  
   Crear feature manualmente en el mock y volver a sincronizar (debe devolver `updated`).

6. **Provisionar ONU e idempotencia**  
   Primera llamada `authorized`, segunda `already_authorized`.

7. **Mismatch de hardware**  
   Cambiar `pon_port` en el payload y verificar `409 hardware_mismatch`.

8. **Errores SmartOLT simulados**  
   El orquestador hoy no reenvía `simulate`; probá directo contra el mock:  
   `curl -X POST 'http://localhost:8003/onu/authorize?simulate=400' ...`

9. **Baja técnica (cliente 707)**  
   Ejecutar `/decommission/customer` y revisar incidentes.

10. **Auditoría + incidentes**  
    Consultar `http://localhost:8000/audits?limit=200` y `http://localhost:8000/incidents`.

11. **UI Streamlit**  
    Repetir los flujos desde http://localhost:8501. Las secciones “Features” y “Auditoría” deberían reflejar los cambios en tiempo real.

Si querés automatizarlo, podés trasladar estos pasos a un script (`scripts/run_smoke.sh`) o a pruebas de `pytest` con `httpx`.

---

## 7. UI Streamlit (http://localhost:8501)

El panel te evita escribir `curl`. Está organizado en secciones:

- **Sincronizar cliente**, **Provisionar ONU**, **Baja técnica**: cada formulario dispara el endpoint correspondiente.
- **Incidentes**, **Features**, **Auditoría**: permiten inspeccionar resultados y filtrar por acción/usuario.
- **ISP-Cube (demo)**: cambiar flags, estado o crear clientes ficticios.
- Barra lateral:
  - Cambiar URLs base (por si querés apuntar a otro orquestador).
  - Leer `/config`.
  - Lanzar `/reset`.
  - Definir el usuario que viaja en `X-Orchestrator-User` para trazabilidad.

---

## 8. Monitoreo y Grafana

- Dashboards listos en http://localhost:3000 (carpeta **Orchestrator**). El principal es **Orchestrator Overview**.
- Paneles destacados:
  - **Incidentes pendientes** y **Incidentes por tipo (últimos 5 min)** con Prometheus.
  - **Latencias p95** de `/sync/customer` y `/provision/onu`.
  - **Actividad últimos 5 min** (sync, provision, baja) por resultado.
  - **Incidentes recientes** – tabla en vivo que consume `/incidents` directamente (gracias al plugin JSON instalado automáticamente). Podés abrir el JSON completo desde cada fila.

> Si necesitás más detalle, agregá enlaces personalizados o integra Loki para ver logs dentro del mismo dashboard.

Prometheus queda disponible en http://localhost:9090; los métricos más útiles:

- `orchestrator_requests_total{endpoint,method,status}`
- `orchestrator_customer_sync_total{result}`
- `orchestrator_incidents_total{kind}`
- `orchestrator_provision_total{result}`
- `orchestrator_decommission_total{result}`
- `orchestrator_request_latency_seconds_bucket`

---

## 9. Tips y buenas prácticas

- **Antes de probar**: asegurate de tener todo limpio (`/reset`) y `dry_run` en el estado que necesites.
- **Cuando estés listo para apuntar a staging/producción**:
  - Ajustá los JSON de `config/environments/`.
  - Cargá secrets o tokens con las variables de entorno que correspondan.
  - Activá `verify_tls=true` y extendé los timeouts según la latencia real.
- **Manejo de datos**: todos los mocks están en memoria, así que un `docker compose down` borra el estado. Si necesitás persistencia, podés montar volúmenes o guardar backups.
- **Auditoría/Incidentes**: se guardan en buffers en memoria (por defecto 500 y 200 entradas). El `/reset` los limpia.
- **Extender la lógica**: si querés propagar parámetros tipo `simulate` desde el orquestador hacia los mocks, el lugar indicado es `orchestrator/main.py` dentro del endpoint correspondiente.

---

## 10. ¿Qué sigue?

- Convertir la suite manual en automatizada.
- Integrar autenticación real y manejo de tokens para cada servicio.
- Agregar circuit breakers/reintentos específicos cuando apuntes a APIs externas.
- Persistir audit trail en una base externa si necesitás histórico más allá de la memoria del proceso.
- Ajustar Grafana/Prometheus para recibir alertas (Slack, mail, etc.) usando las métricas que ya están expuestas.

Mientras tanto, con lo que hay en este repo podés practicar todo el flujo end-to-end sin depender de nadie. ¡Éxitos! 💪
