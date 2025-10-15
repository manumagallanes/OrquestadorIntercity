# Orquestador Intercity – Entorno de prueba local

Este proyecto crea un entorno local completamente sintético para simular la integración entre ISP-Cube, GeoGrid (Brasil) y SmartOLT utilizando servicios FastAPI y Uvicorn. Sirve para pruebas de orquestación sin usar credenciales ni datos reales.

## Servicios incluidos
- `orchestrator` (`/sync/customer`, `/provision/onu`, `/clients.geojson`, `/health`)
- `isp-mock` (ISP-Cube simulado con clientes sintéticos)
- `geogrid-mock` (API GeoGrid local con almacenamiento en memoria)
- `smartolt-mock` (API SmartOLT simulada con estado en memoria)
- `prometheus` (colecta métricas del orquestador)
- `grafana` (dashboards listos para visualizar métricas)
- `ui` (panel Streamlit para operar los endpoints sin usar `curl`)

Cada servicio expone logs legibles y respuestas de error configurables para escenarios `400`, `404`, `409` y `429`.

## Requisitos previos
- Docker y Docker Compose (`docker compose` plugin o binario `docker-compose`)

## Puesta en marcha
1. Copiá el archivo `.env.dev` como `.env.dev` (ya provisto con valores por defecto). Ajustá si necesitás cambiar puertos/base URLs.
2. Construí y levantá los servicios:
   ```bash
   docker-compose up --build
   ```
3. Cuando los contenedores estén listos, accedé a:
   - Orchestrator: http://localhost:8000/docs
   - ISP mock: http://localhost:8001/docs
   - GeoGrid mock: http://localhost:8002/docs
   - SmartOLT mock: http://localhost:8003/docs
   - Prometheus: http://localhost:9090 (consulta directa de métricas)
   - Grafana: http://localhost:3000 (usuario `admin`, password `admin`)
   - UI Streamlit: http://localhost:8501

## Pruebas rápidas con `curl`

### Sincronizar un cliente hacia GeoGrid
```bash
curl -X POST http://localhost:8000/sync/customer \
  -H "Content-Type: application/json" \
  -d '{"customer_id": 202}'
```
Respuesta esperada (cliente con `integration_enabled: true`):
```json
{"feature_id": "feature_00001", "action": "created"}
```

### Actualizar un cliente existente (manejo de 409 → PUT)
```bash
curl -X POST http://localhost:8000/sync/customer \
  -H "Content-Type: application/json" \
  -d '{"customer_id": 202}'
```
Si la feature ya existe, la respuesta será:
```json
{"feature_id": "feature_00001", "action": "updated"}
```

> Nota: el orquestador **sólo** procesa clientes que traigan `integration_enabled: true` desde ISP-Cube y que tengan completos los campos `lat`, `lon`, `odb`, `olt_id`, `board`, `pon` y `onu_sn`. Si falta alguno, la petición devuelve `422` y se registra un incidente consultable en `/incidents`.

### Provisionar una ONU (con dry-run activado por defecto)
```bash
curl -X POST http://localhost:8000/provision/onu \
  -H "Content-Type: application/json" \
  -d '{"customer_id": 202, "olt_id": 2, "board": 3, "pon_port": 4, "onu_sn": "TESTSN00002"}'
```
Respuesta esperada (no contacta SmartOLT por `DRY_RUN=true`):
```json
{
  "dry_run": true,
  "status": "skipped",
  "message": "SmartOLT provisioning skipped because dry-run is enabled"
}
```

Para forzar la ejecución real, pasá `dry_run: false` en el body o seteá `DRY_RUN=false` en `.env.dev`.

La operación es idempotente: si enviás nuevamente la misma ONU con `dry_run: false`, el orquestador detecta que ya está autorizada y responde con `{"status": "already_authorized", ...}` sin generar error 409. Si el `customer_id` está presente, también valida que OLT/board/puerto/ONU coincidan con el registro en ISP-Cube para prevenir errores humanos.

### Dar de baja un cliente (remover GeoGrid + SmartOLT)
```bash
# Marcar al cliente como inactivo en ISP-Cube (simulación de baja comercial)
curl -X PATCH http://localhost:8001/customers/707/status \
  -H "Content-Type: application/json" \
  -d '{"status": "inactive"}'

# Ejecutar la baja técnica desde el orquestador
curl -X POST http://localhost:8000/decommission/customer \
  -H "Content-Type: application/json" \
  -d '{"customer_id": 707}'
```
El orquestador confirma que el cliente quedó inactivo, elimina la feature correspondiente de GeoGrid (si existe) y retira la ONU en SmartOLT. Con `dry_run: true` (en el body o vía `/config`) sólo informa qué se eliminaría.

### Consultar el GeoJSON combinado
```bash
curl http://localhost:8000/clients.geojson
```

### Cambiar o consultar el modo `dry_run` en caliente
```bash
curl http://localhost:8000/config

curl -X POST http://localhost:8000/config \
  -H "Content-Type: application/json" \
  -d '{"dry_run": false}'
```
El valor configurado aplica como default para las siguientes llamadas a `/provision/onu` (a menos que se envíe el campo `dry_run` en el body).

### Resetear los mocks rápidamente
```bash
curl -X POST http://localhost:8000/reset
```
El orquestador invoca `/reset` en cada mock y deja los estados en memoria limpios. También podés llamar `/reset` directamente en cada mock si querés hacerlo por separado.

## Configuración por entorno
- Las credenciales y endpoints se definen en archivos JSON por entorno dentro de `config/environments/{dev,staging,production}.json`. Cada servicio incluye regiones con sus `base_url`, `timeout_seconds` y si deben verificar TLS.
- El orquestador arranca en modo `dev` por defecto (`ORCHESTRATOR_ENV=dev`). Podés apuntar a staging o producción configurando `ORCHESTRATOR_ENV=staging` o `ORCHESTRATOR_ENV=production` antes de iniciar `uvicorn`.
- Si necesitás seleccionar una región distinta a la predeterminada de cada entorno, seteá variables como `ISP_REGION=mx`, `GEOGRID_REGION=mx` o `SMARTOLT_REGION=mx`. Esto toma la configuración del bloque correspondiente.
- Los overrides finos (por ejemplo para pruebas locales) siguen disponibles mediante variables como `ISP_BASE_URL`, `GEOGRID_BASE_URL`, `SMARTOLT_BASE_URL` o `DRY_RUN`. Estas variables actualizan el endpoint por defecto de su servicio en el entorno elegido.
- Ejemplo rápido para probar staging región México:
  ```bash
  ORCHESTRATOR_ENV=staging \
  ISP_REGION=mx \
  GEOGRID_REGION=mx \
  SMARTOLT_REGION=mx \
  uvicorn orchestrator.main:app --reload
  ```

## Simulación de errores
- ISP mock: `GET /customers/101?simulate=429`
- GeoGrid mock: `POST /features?simulate=429`
- SmartOLT mock: `POST /onu/authorize?simulate=400`
- SmartOLT timeout: `GET /onus?simulate=timeout` (provoca un timeout en el orquestador al exceder los 10s)

El ISP mock expone endpoints auxiliares para preparar datos durante las demos:
- Crear cliente: `POST /customers` (ver `mocks/isp_cube/main.py` para el esquema completo)
- Actualizar flag de automatización: `PATCH /customers/{id}/flag` con `{"integration_enabled": true}`
- Cambiar estado activo/inactivo: `PATCH /customers/{id}/status` con `{"status": "inactive"}`
- Listar pendientes (sin flag): `GET /customers/pending`

Cada mock registra los eventos y devuelve JSONs sintéticos para facilitar el manejo de errores en el orquestador.

Para probar un flujo de ONU duplicada:
1. Asegurate de estar con `dry_run=false` (`POST /config`).
2. Llamá dos veces seguidas a `/provision/onu` con los mismos parámetros.
3. La segunda respuesta vendrá con `status: "already_authorized"` y sin error HTTP.

Ponés un `pon_port` inválido (por ejemplo un número negativo) y FastAPI devolverá `422 Unprocessable Entity`, útil para testear validaciones de entrada.

Los `simulate` permiten comprobar manejo de `400`, `404`, `409`, `429` y timeouts sin tocar sistemas reales.

### Incidentes y monitoreo
- Consultá los incidentes registrados (filtros opcionales por `kind`):
  ```bash
  curl http://localhost:8000/incidents
  curl http://localhost:8000/incidents?kind=missing_fields
  ```
- Cada registro incluye `timestamp`, `customer_id`, `kind` y detalles; puede alimentarse a Grafana/Prometheus parseando los logs o consumiendo este endpoint. El `POST /reset` limpia el buffer interno de incidentes para arrancar otra sesión de pruebas.

### Métricas listas para dashboard
- El orquestador expone métricas Prometheus en `http://localhost:8000/metrics`. Las más útiles:
  - `orchestrator_requests_total{endpoint,method,status}` – recuento de requests por endpoint.
  - `orchestrator_request_latency_seconds_bucket` – histograma de latencias.
  - `orchestrator_customer_sync_total{result}` – altas/actualizaciones en GeoGrid.
  - `orchestrator_provision_total{result}` – autorizaciones/ya autorizados/dry-run en SmartOLT.
  - `orchestrator_decommission_total{result}` – bajas procesadas (dry-run o completadas).
  - `*_total{result="error"}` – errores registrados por cada tipo de acción (alimentan los paneles de Grafana).
  - `orchestrator_incidents_total{kind}` y `orchestrator_incidents_buffer_size` – seguimiento de incidentes pendientes.
- Prometheus (http://localhost:9090) ya viene configurado para scrapear al orquestador.
- Grafana (http://localhost:3000) incluye el data source `Prometheus` precargado; accedé con `admin`/`admin` y armá tus dashboards o alertas sin configuración adicional.
- El dashboard "Orchestrator Overview" se aprovisiona automáticamente en Grafana (carpeta **Orchestrator**) y muestra incidentes, actividad de sync/provision/bajas y paneles de errores por acción.
  1. Ingresá con `admin`/`admin` y definí una contraseña nueva cuando Grafana lo solicite.
  2. Abrí la carpeta **Orchestrator** → **Orchestrator Overview** para ver el panel listo.
  3. Desde ahí podés duplicar o editar los paneles para personalizar alertas y vistas.

### Panel Streamlit (`ui`)
- Accedé a http://localhost:8501 para operar el orquestador con una interfaz gráfica.
- Secciones disponibles:
  - **Sincronizar cliente**: envía `POST /sync/customer`.
  - **Provisionar ONU**: ejecuta `POST /provision/onu` con controles para `dry_run`.
  - **Baja técnica**: dispara `POST /decommission/customer` y muestra el resultado.
  - **Incidentes**: consulta `/incidents` filtrando por tipo.
  - **Features**: lista `GET /features` o muestra el GeoJSON consolidado.
  - **Auditoría**: obtiene `GET /audits` con filtros por acción/usuario.
  - **ISP-Cube (demo)**: listar clientes, alternar `integration_enabled`, cambiar estado activo/inactivo o crear clientes ficticios sin usar `curl`.
- Desde la barra lateral podés modificar la URL base del orquestador, consultar `/config` o ejecutar `POST /reset` sin usar `curl`.
- También podés setear el usuario que se enviará en la cabecera `X-Orchestrator-User` (o la que definas vía `ORCHESTRATOR_USER_HEADER`) para que quede registrado en la bitácora.

### Auditoría
- Cada sincronización, provisión o baja técnica queda registrada en memoria con acción, `customer_id`, usuario (`X-Orchestrator-User`), si fue `dry_run`, timestamp y detalle del resultado.
- Consultá la bitácora con `GET /audits?limit=200&action=provision&user=pepito`.
- El buffer (por defecto 500 entradas) se limpia con `POST /reset`. Podés ajustar el header esperado cambiando la variable `ORCHESTRATOR_USER_HEADER`.

### Suite de pruebas manuales
1. **Reset inicial**
   ```bash
   docker compose up --build
   curl -X POST http://localhost:8000/reset
   ```
   Esperado: cada mock responde `{"status":200}` y los servicios confirman salud en `/health`.

2. **Sincronización básica (cliente 202)**
   ```bash
   curl -X POST http://localhost:8000/sync/customer \
     -H 'Content-Type: application/json' \
     -d '{"customer_id":202}'
   curl -X POST http://localhost:8000/sync/customer \
     -H 'Content-Type: application/json' \
     -d '{"customer_id":202}'
   curl http://localhost:8002/features
   curl http://localhost:8000/audits?action=sync
   ```
   Esperado: primera llamada `action=created`, segunda `action=updated`; la feature aparece en GeoGrid y la auditoría registra ambas entradas.

3. **Flag de integración deshabilitado**
   ```bash
   curl -X PATCH http://localhost:8001/customers/202/flag \
     -H 'Content-Type: application/json' \
     -d '{"integration_enabled":false}'
   curl -X POST http://localhost:8000/sync/customer \
     -H 'Content-Type: application/json' \
     -d '{"customer_id":202}'
   curl http://localhost:8000/incidents
   ```
   Esperado: respuesta `412` con `reason=integration_disabled` y registro del incidente homónimo. Restaurar el flag a `true` antes de continuar.

4. **Datos incompletos (cliente 404)**
   ```bash
   curl -X POST http://localhost:8000/sync/customer \
     -H 'Content-Type: application/json' \
     -d '{"customer_id":404}'
   ```
   Esperado: `422` con `missing_fields` (`lat`, `lon`) e incidente `missing_fields`.

5. **Conflicto en GeoGrid**
   ```bash
   curl -X POST http://localhost:8002/features \
     -H 'Content-Type: application/json' \
     -d '{"name":"Manual conflict","location":{"lat":-23.5505,"lon":-46.6333},"attrs":{"customer_id":202,"address":"Rua dos Testes 456","city":"São Paulo","odb":"CAIXA-22B","olt_id":2,"board":3,"pon":4,"onu_sn":"TESTSN00002"}}'
   curl -X POST http://localhost:8000/sync/customer \
     -H 'Content-Type: application/json' \
     -d '{"customer_id":202}'
   ```
   Esperado: el orquestador recibe 409, toma el `feature_id` y responde `action=updated` tras ejecutar el `PUT`.

6. **Provisionar ONU e idempotencia**
   ```bash
   curl -X POST http://localhost:8000/provision/onu \
     -H 'Content-Type: application/json' \
     -d '{"customer_id":202,"olt_id":2,"board":3,"pon_port":4,"onu_sn":"TESTSN00002","dry_run":false}'
   curl -X POST http://localhost:8000/provision/onu \
     -H 'Content-Type: application/json' \
     -d '{"customer_id":202,"olt_id":2,"board":3,"pon_port":4,"onu_sn":"TESTSN00002","dry_run":false}'
   ```
   Esperado: primera respuesta `"authorized"`, segunda `"already_authorized"`. Revisar auditoría (`action=provision`) y métricas (`orchestrator_provision_total`).

7. **Mismatch de hardware**
   ```bash
   curl -X POST http://localhost:8000/provision/onu \
     -H 'Content-Type: application/json' \
     -d '{"customer_id":202,"olt_id":2,"board":3,"pon_port":9,"onu_sn":"TESTSN00002","dry_run":false}'
   curl http://localhost:8000/incidents
   ```
   Esperado: respuesta `409` con `hardware_mismatch` e incidente del mismo tipo.

8. **Errores SmartOLT simulados**
   - El orquestador no reenvía `simulate`, por lo que los errores HTTP se ejercitan directamente contra el mock:
     ```bash
     curl -X POST 'http://localhost:8003/onu/authorize?simulate=400' \
       -H 'Content-Type: application/json' \
       -d '{"olt_id":2,"board":3,"pon_port":4,"onu_sn":"TESTSN00002"}'
     curl 'http://localhost:8003/onus?simulate=timeout'
     ```
     Esperado: la primera llamada devuelve 400 simulado, la segunda provoca un timeout (>10 s). Para que `/provision/onu` soporte estos flags habría que modificar el orquestador y propagar la query string.

9. **Baja técnica (cliente 707)**
   ```bash
   curl -X POST http://localhost:8000/decommission/customer \
     -H 'Content-Type: application/json' \
     -d '{"customer_id":707,"dry_run":false}'
   curl http://localhost:8000/incidents
   ```
   Esperado: si no existen feature ni ONU, la respuesta indica `status":"not_found"` y se registran incidentes `decommission_missing_feature` y `decommission_missing_onu`. Tras sincronizar/provisionar previamente se valida la rama de eliminación real.

10. **Auditoría e incidentes**
    ```bash
    curl http://localhost:8000/audits?limit=200
    curl http://localhost:8000/incidents
    ```
    Esperado: todas las pruebas quedan trazadas con usuario (`X-Orchestrator-User`) y detalle. `POST /reset` limpia los buffers.

11. **UI Streamlit**
    - Abrí http://localhost:8501 y repetí los flujos de sincronización, provisión y baja técnica. Las secciones **Features** y **Auditoría** permiten validar que los resultados coinciden con los obtenidos por `curl`.

## Estructura de archivos relevante
- `orchestrator/main.py` – FastAPI principal que coordina los servicios
- `mocks/isp_cube/main.py` – Mock de ISP-Cube
- `mocks/geogrid/main.py` – Mock de GeoGrid con almacenamiento en memoria
- `mocks/smartolt/main.py` – Mock de SmartOLT para autorización de ONUs
- `docker-compose.yml` – Orquesta los cuatro servicios
- `.env.dev` – Variables de entorno de ejemplo
- `.env.staging` – Plantilla para apuntar a endpoints reales manteniendo `DRY_RUN=true`
- `requirements.txt` – Dependencias de Python
- `Dockerfile` – Imagen base para todos los servicios

> Todos los datos y credenciales son ficticios y se mantienen en memoria dentro de los contenedores.
