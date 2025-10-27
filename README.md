# Orquestador Intercity – Documentación Técnica

Este repositorio reúne un entorno autocontenido para reproducir la integración entre ISP-Cube, GeoGrid y SmartOLT mediante un orquestador escrito en FastAPI. El propósito central es disponer de un laboratorio controlado que permita comprender la arquitectura, validar comportamientos y preparar despliegues hacia entornos reales sin depender de sistemas productivos.

---

## 1. Introducción

El orquestador coordina tres dominios: catálogo de clientes (ISP-Cube), plataforma geoespacial (GeoGrid) y autorización de ONUs (SmartOLT). Cada operación se valida de forma previa, se registra en auditoría, expone métricas y produce incidentes cuando un flujo no puede concluirse. El ecosistema completo se empaqueta con Docker Compose e incluye mocks funcionales, panel de monitoreo y una interfaz Streamlit para operar sin requerir herramientas adicionales.

Este documento presenta la fundamentación conceptual, la arquitectura técnica, los flujos principales, las opciones de configuración y las pautas operativas para que una persona ajena al proyecto pueda utilizarlo con criterio.

---

## 2. Marco teórico

### 2.1 Orquestación OSS/BSS en redes FTTx

La orquestación en un escenario FTTx consiste en coordinar procesos entre sistemas de soporte (OSS/BSS) con el fin de preservar consistencia operativa. Un orquestador intermedia entre solicitudes de automatización y sistemas especializados, aplicando reglas de negocio, normalización de datos y control transaccional. El modelo simplifica la integración al centralizar la lógica y desacoplar consumidores de las APIs subyacentes.

### 2.2 Consistencia y calidad de datos

Antes de modificar sistemas externos, el orquestador valida que el cliente tenga datos completos, coordenadas dentro del rango admitido y que la estructura de red solicitada coincida con la registrada. Esta verificación temprana evita divergencias entre plataformas y reduce el costo de remediación. Los incidentes emitidos (`missing_fields`, `invalid_coordinates`, `hardware_mismatch`, entre otros) documentan las causas de rechazo para permitir acciones correctivas.

### 2.3 Resiliencia frente a fallos

Las integraciones utilizan clientes HTTP asíncronos, reintentos con backoff exponencial y circuit breakers configurables. Los códigos 5xx gatillan reintentos, mientras que fallas consecutivas elevadas abren el breaker correspondiente y bloquean la interacción temporalmente. Este patrón protege al orquestador de degradaciones en los servicios externos y facilita la observación de incidentes repetitivos.

---

## 3. Arquitectura de la solución

### 3.1 Componentes principales

| Servicio         | Puerto | Descripción resumida                                                                 |
|------------------|:------:|---------------------------------------------------------------------------------------|
| `orchestrator`   |  8000  | API FastAPI que implementa la lógica de negocio, expone endpoints REST y métricas.   |
| `isp-mock`       |  8001  | Mock de ISP-Cube con catálogo de clientes en memoria y soporte para flags de integración. |
| `geogrid-mock`   |  8002  | Mock de GeoGrid que almacena features GeoJSON y permite altas, actualizaciones y consultas. |
| `smartolt-mock`  |  8003  | Mock de SmartOLT orientado a autorizar ONUs y simular códigos de error.              |
| `prometheus`     |  9090  | Servicio de métricas que consume el endpoint `/metrics` del orquestador.             |
| `grafana`        |  3000  | Dashboards preconfigurados para visualizar flujos, incidentes y latencias.          |
| `ui`             |  8501  | Interfaz Streamlit para operar sobre la API y los mocks sin necesidad de `curl`.    |

El archivo `diagrama.mmd` describe la topología mediante un diagrama Mermaid. Los contenedores comparten una red interna, por lo que no se requieren dependencias externas ni credenciales reales.

### 3.2 Módulos relevantes

- `orchestrator/main.py`: define modelos Pydantic, carga de configuración, middlewares, endpoints y reglas de validación.
- `config/environments/*.json`: describe entornos, regiones, timeouts, reintentos y circuit breakers.
- `monitoring/`: incluye dashboards y reglas de Prometheus/Grafana.
- `mocks/`: implementa la lógica de las APIs simuladas.
- `scripts/`: alberga utilitarios reutilizables (por ejemplo, potenciales smoke tests automatizados).

---

## 4. Configuración de entornos y parámetros

### 4.1 Archivos JSON de entorno

Los archivos `dev.json`, `staging.json`, `production.json` e `intercity.json` especifican, para cada servicio:

- `base_url` y `timeout_seconds`.
- Parámetros de reintento (`max_attempts`, `backoff_initial_seconds`, `backoff_max_seconds`).
- Configuración de circuit breaker (`failure_threshold`, `recovery_timeout_seconds`).
- Headers por defecto, opcionalmente con referencias a variables de entorno (`env:VARIABLE`).

El campo `default_region` determina la región activa cuando no se indica otra explícitamente.

### 4.2 Variables de entorno disponibles

- `ORCHESTRATOR_ENV`: selecciona el archivo de configuración a cargar (`dev` por defecto).
- `ISP_REGION`, `GEOGRID_REGION`, `SMARTOLT_REGION`: sustituyen la región por defecto de cada servicio.
- `ISP_BASE_URL`, `GEOGRID_BASE_URL`, `SMARTOLT_BASE_URL`: sobrescriben el `base_url` de la región principal sin modificar los JSON.
- `DRY_RUN`: fuerza el valor inicial del flag global `dry_run` (interpreta `true/false`, `1/0` o equivalentes).
- Cualquier valor declarado como `env:VARIABLE` en los JSON debe definirse en el entorno del proceso antes de iniciar el orquestador.

El endpoint `GET /config` expone la configuración efectiva con las sustituciones aplicadas, mientras que `POST /config` actualiza el flag `dry_run` en tiempo de ejecución.

---

## 5. Puesta en marcha local

### 5.1 Requisitos previos

1. Docker y Docker Compose (plugin `docker compose`).
2. Puertos libres: 8000–8003, 8501, 3000 y 9090.
3. Python 3.10+ es opcional para ejecutar scripts fuera de los contenedores.

### 5.2 Inicio del entorno

```bash
docker compose up --build
```

El comando compila imágenes, crea la red interna y lanza todos los servicios según la configuración `dev`. Los accesos principales son:

- API y documentación interactiva: http://localhost:8000/docs
- UI Streamlit: http://localhost:8501
- Grafana: http://localhost:3000 (usuario `admin`, contraseña `admin`)
- Prometheus: http://localhost:9090

### 5.3 Reinicialización del estado

```bash
curl -X POST http://localhost:8000/reset
```

El orquestador coordina el reinicio de cada mock y limpia los buffers de auditoría e incidentes en memoria. El endpoint devuelve `202 Accepted` para indicar que la operación se ejecuta de forma asíncrona.

---

## 6. Flujos operativos del orquestador

Los endpoints principales se encuentran en `orchestrator/main.py` y comparten la siguiente estructura: validación preliminar, interacción con servicios externos, registro en auditoría e instrumentación de métricas.

### 6.1 Sincronización de cliente hacia GeoGrid (`POST /sync/customer`)

1. Consulta a ISP-Cube para obtener el cliente maestro.
2. Validaciones ejecutadas por `ensure_customer_ready`: flag de integración activo, campos obligatorios completos, coordenadas numéricas y dentro del bounding box definido (valores de Córdoba por defecto).
3. Construcción del payload GeoJSON y envío a GeoGrid.
4. Manejo de resultados:
   - `201 Created`: el registro se marca como creado (`action: created`).
   - `409 Conflict`: se recupera el identificador existente, se realiza un `PUT` y se marca como actualizado (`action: updated`).
5. Auditoría de la operación y actualización de contadores de métricas.

Los incidentes `integration_disabled`, `missing_fields` e `invalid_coordinates` describen las causas por las que un cliente no puede sincronizarse.

### 6.2 Provisionamiento de ONU (`POST /provision/onu`)

1. Recuperación del cliente en ISP-Cube y reutilización de `ensure_customer_ready`.
2. Comparación de hardware con `ensure_hardware_matches`; divergencias generan el incidente `hardware_mismatch` y devuelven `409 Conflict`.
3. Evaluación del flag `dry_run` (global o provisto en la petición):
   - En modo simulación se devuelve un resultado informativo sin invocar SmartOLT.
   - En modo ejecución se llama a `/onu/authorize`, con reintentos y circuit breaker configurados.
4. Registro en auditoría con el resultado (`authorized`, `already_authorized`, simulaciones) y actualización de métricas.

### 6.3 Baja técnica (`POST /decommission/customer`)

1. Verificación de estado inactivo mediante `ensure_customer_inactive`.
2. Eliminación de la feature en GeoGrid; si no existe, se registra `decommission_missing_feature`.
3. Desautorización de la ONU en SmartOLT; la ausencia de registros genera `decommission_missing_onu`.
4. Consolidación del resultado final y auditoría del proceso.

---

## 7. API expuesta

| Endpoint                 | Método | Descripción                                                | Códigos destacados |
|--------------------------|:------:|------------------------------------------------------------|--------------------|
| `/sync/customer`         | POST   | Replica un cliente en GeoGrid con validaciones previas.    | 200, 409, 412, 422 |
| `/provision/onu`         | POST   | Autoriza una ONU o simula la operación según `dry_run`.    | 200, 204, 409, 412, 422 |
| `/decommission/customer` | POST   | Retira recursos asociados a un cliente inactivo.           | 200, 204, 409, 412 |
| `/config`                | GET    | Informa la configuración efectiva del orquestador.         | 200 |
| `/config`                | POST   | Actualiza el flag global `dry_run`.                        | 200 |
| `/reset`                 | POST   | Restablece mocks, auditorías e incidentes.                 | 202 |
| `/incidents`             | GET    | Lista incidentes recientes (con filtros por tipo y rango). | 200 |
| `/audits`                | GET    | Devuelve auditorías filtradas por acción, usuario o estado.| 200 |
| `/metrics`               | GET    | Exposición Prometheus de contadores e histogramas.         | 200 |
| `/health`                | GET    | Comprobación básica de disponibilidad.                     | 200 |

### 7.1 Ejemplos de invocación

```bash
curl -X POST http://localhost:8000/sync/customer \
  -H 'Content-Type: application/json' \
  -d '{"customer_id": 202}'
```

```bash
curl -X POST http://localhost:8000/provision/onu \
  -H 'Content-Type: application/json' \
  -d '{"customer_id":202,"olt_id":2,"board":3,"pon_port":4,"onu_sn":"TESTSN00002","dry_run":false}'
```

```bash
curl -X POST http://localhost:8000/decommission/customer \
  -H 'Content-Type: application/json' \
  -d '{"customer_id":707,"dry_run":false}'
```

---

## 8. Observabilidad

### 8.1 Métricas Prometheus

El middleware `@app.middleware("http")` mide latencia y contabiliza solicitudes, mientras que contadores específicos registran resultados por flujo. Métricas relevantes:

- `orchestrator_request_latency_seconds_bucket{endpoint,method}`
- `orchestrator_requests_total{endpoint,method,status}`
- `orchestrator_customer_sync_total{result}`
- `orchestrator_provision_total{result}`
- `orchestrator_decommission_total{result}`
- `orchestrator_incidents_total{kind}`
- `orchestrator_incidents_buffer_size`

### 8.2 Dashboards de Grafana

La carpeta **Orchestrator** incluye paneles preparados:

- **Overview**: volumen de operaciones, latencias p95 y distribución de resultados.
- **Incidentes recientes**: tabla conectada al endpoint `/incidents`.
- **Actividad en cinco minutos**: seguimiento de sync, provisionamientos y bajas.

Los dashboards se alimentan de Prometheus y pueden ampliarse según las necesidades del despliegue.

---

## 9. Interfaz de usuario (Streamlit)

El servicio `ui` permite ejecutar los flujos sin uso de CLI:

- Formularios para sincronización, provisionamiento y baja técnica.
- Visualización de incidentes, auditorías y features en tiempo real.
- Barra lateral para modificar URLs base, usuario (`X-Orchestrator-User`), lanzar `/reset` y consultar `/config`.

Esta interfaz es útil para demostraciones y para usuarios que no desean interactuar directamente con los endpoints REST.

---

## 10. Pruebas recomendadas

Se propone la siguiente secuencia para validar el entorno tras cada despliegue o cambio relevante:

1. Restablecer estado (`docker compose up --build` y `POST /reset`).
2. Sincronizar el cliente 202 dos veces y verificar creación / actualización.
3. Intentar sincronizar un cliente con `integration_enabled=false` y corroborar `412`.
4. Ejecutar `/sync/customer` sobre un cliente con datos incompletos para obtener `422 missing_fields`.
5. Generar manualmente un conflicto en GeoGrid y confirmar que el flujo hace `PUT`.
6. Provisionar la ONU del cliente 202 en modo ejecución; repetir el request para validar idempotencia (`already_authorized`).
7. Cambiar `pon_port` en el payload y comprobar el incidente `hardware_mismatch`.
8. Simular fallos en SmartOLT usando parámetros `simulate` directamente sobre el mock.
9. Procesar la baja del cliente 707 y revisar incidentes `decommission_*`.
10. Consultar `/audits` y `/incidents` para verificar la trazabilidad de todos los pasos.
11. Repetir los flujos desde la UI Streamlit y confirmar que refleja los mismos resultados.

La checklist puede automatizarse con scripts en `scripts/` o con pruebas basadas en `pytest` y `httpx`.

---

## 11. Extensión y mantenimiento

- **Entornos reales**: ajustar los JSON en `config/environments`, definir secretos mediante variables de entorno y habilitar `verify_tls`.
- **Persistencia**: si se requiere histórico más allá de la memoria del proceso, adaptar `record_audit` y `record_incident` para almacenar registros en una base de datos externa.
- **Nuevos servicios o regiones**: extender `EnvConfig` agregando entradas y reutilizando las funciones de resolución de región.
- **Observabilidad adicional**: integrar Loki u otro agregador de logs para complementar las métricas existentes.
- **Estrategias de resiliencia**: revisar parámetros de reintento y circuit breaker al apuntar a APIs reales para alinearlos con SLA y capacidad de los upstreams.

---

## 12. Recursos complementarios

- `diagrama.mmd`: descripción visual de la arquitectura.
- `mocks/`: implementación detallada de las APIs simuladas.
- `monitoring/`: dashboards y configuración de métricas.
- `ui/`: código de la interfaz Streamlit.
- `scripts/`: utilitarios reutilizables para operaciones o pruebas.

Con esta información se puede comprender la arquitectura completa, ejecutar los flujos principales y adaptar el Orquestador Intercity a distintos escenarios de integración.
