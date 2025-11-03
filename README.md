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

### 4.3 Dataset inicial de clientes

El archivo `config/seeds/customers.json` define un conjunto base de clientes (IDs 202, 912, 404, 707, 808) que reflejan los casos de uso documentados: clientes listos para automatizar, con flag deshabilitado, con datos incompletos y en estado inactivo. El orquestador los sincroniza automáticamente con el mock de ISP-Cube durante el arranque y cada vez que se ejecuta `POST /reset`, de modo que las pruebas y dashboards partan de una base consistente sin necesidad de cargar datos manualmente.

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

El orquestador también valida que el par `olt_id` + `board` + `pon_port` no esté asignado a otra ONU distinta; de ser así dispara el incidente `hardware_port_conflict` y devuelve `409 Conflict`.

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
| `/analytics/customer-events` | POST / GET | Alta manual de eventos o consulta de feed georreferenciado. | 200, 201 |
| `/health`                | GET    | Comprobación básica de disponibilidad.                     | 200 |

### 7.1 Ejemplos de invocación

> **Importante:** Los mocks arrancan sin clientes precargados. Antes de consumir estos flujos, registrar los que se necesiten en ISP Cube vía `POST /customers`, incluyendo los campos obligatorios (`zone`, `lat`, `lon`, `odb`, etc.). Por ejemplo:

```bash
curl -X POST http://localhost:8001/customers \
  -H 'Content-Type: application/json' \
  -d '{"customer_id":202,"name":"Cliente Demo","address":"Falsa 123","city":"Córdoba","zone":"Centro","lat":-31.41,"lon":-64.18,"odb":"CAJA-10","olt_id":1,"board":0,"pon":1,"onu_sn":"TESTSN00002","integration_enabled":true,"status":"active"}'
```

Luego pueden ejecutarse las operaciones habituales:

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

```bash
curl -X POST http://localhost:8000/analytics/customer-events \
  -H 'Content-Type: application/json' \
  -d '{"event_type":"alta","customer_id":202,"metadata":{"nota":"carga manual"}}'
```

---

## 8. Observabilidad

### 8.1 Despliegue de Prometheus y Grafana

El directorio `monitoring/` contiene todo lo necesario para levantar un stack de observabilidad acoplado al laboratorio:

- `monitoring/prometheus.yml` define un `scrape_config` que apunta al `orchestrator:8000/metrics` cada 15 segundos.
- `monitoring/grafana/provisioning/datasources/prometheus.yml` crea dos datasources: Prometheus (uid `prometheus`) y un datasource JSON (uid `orchestrator-analytics`) que consulta directamente los endpoints de analítica.
- `monitoring/grafana/provisioning/dashboards/dashboards.yml` carga automáticamente los dashboards ubicados en `monitoring/grafana/dashboards/`.

Ejecución recomendada:

```bash
docker compose up -d prometheus grafana orchestrator
```

Grafana queda disponible en `http://localhost:3000` (usuario `admin`, contraseña `admin`). Prometheus está accesible en `http://localhost:9090`.

### 8.2 Métricas expuestas

El middleware HTTP y los flujos de negocio reportan los siguientes indicadores en formato Prometheus:

- `orchestrator_request_latency_seconds_bucket{endpoint,method}`
- `orchestrator_requests_total{endpoint,method,status}`
- `orchestrator_customer_sync_total{result}`
- `orchestrator_provision_total{result}`
- `orchestrator_decommission_total{result}`
- `orchestrator_incidents_total{kind}`
- `orchestrator_incidents_buffer_size`
- `orchestrator_incidents_resolved_total{kind}`
- `orchestrator_customer_events_total{event_type,zone}`

Complementariamente, los endpoints JSON bajo `/analytics/customer-events*` (incluyendo `/analytics/customer-events/map/altas` y `/analytics/customer-events/map/bajas`, que devuelven colecciones GeoJSON) entregan información georreferenciada y agregados listos para consumir desde Grafana (volúmenes por zona, series temporales y feed de eventos). Además, el orquestador implementa `/query` compatible con el datasource JSON de Grafana para generar tablas dinámicas con coordenadas ya validadas (`target: customer_events_map`) y para exponer incidentes corregidos mediante `target: incidents_resolved`.

Otros endpoints útiles para dashboards:

- `GET /incidents/resolved`: historial de incidentes corregidos, filtrable por `customer_id`, `kind` y ventana de tiempo.

Para generar datos de prueba sin tráfico real, exporta `ORCHESTRATOR_SEED_CUSTOMER_EVENTS=true` antes de iniciar el contenedor del orquestador o registra eventos manuales con `POST /analytics/customer-events`.

### 8.3 Dashboard «Monitoreo Orquestador Intercity»

El dashboard principal (`monitoring/grafana/dashboards/orchestrator-overview.json`) incluye:

- Controles de filtros para **zona** (regex sobre `zone`) y **ventana de días** reutilizados por todos los paneles.
- Indicadores acumulados de altas, bajas y crecimiento neto basados en `orchestrator_customer_events_total`, con descripción contextual.
- Series temporales de altas/bajas y de incidentes por tipo usando `increase(...[$__range])` para visualizar tendencias dentro del rango seleccionado.
- Conteo de incidentes activos (`orchestrator_incidents_buffer_size`) y tarjetas de incidentes nuevos / resueltos calculados sobre el rango actual.
- Tabla de incidentes abiertos (`target incidents_open`) que lista cliente, tipo y contexto de cada caso pendiente, y tabla de incidentes resueltos (`target incidents_resolved`) con detalle descargable.
- Mapa georreferenciado que consume el `target customer_events_map` vía `/query`, dibujando marcadores verdes para altas y rojos para bajas con información contextual (zona, cliente, ciudad) y fallback automático de coordenadas. Cada marcador muestra `cliente: <id> – <nombre>` para facilitar la identificación.

Todos los paneles admiten filtros temporales desde Grafana y pueden extenderse para cubrir nuevos indicadores o fuentes de datos corporativas.

---

## 9. Interfaz de usuario (Streamlit)

El servicio `ui` permite ejecutar los flujos sin uso de CLI:

- Formularios para sincronización, provisionamiento y baja técnica.
- Visualización de incidentes, auditorías y features en tiempo real.
- Barra lateral para modificar URLs base, usuario (`X-Orchestrator-User`), lanzar `/reset` y consultar `/config`.
- Herramientas para gestionar clientes en el mock de ISP-Cube (crear, editar, activar/desactivar integración y estado) sin recurrir a llamadas manuales.

Esta interfaz es útil para demostraciones y para usuarios que no desean interactuar directamente con los endpoints REST.

---

## 10. Pruebas recomendadas

Se propone la siguiente secuencia para validar el entorno tras cada despliegue o cambio relevante:

1. Restablecer estado (`docker compose up --build` y `POST /reset`).
2. Registrar en ISP Cube los clientes de prueba necesarios (ej.: 202 activo, 912 con `integration_enabled=false`, 404 con coordenadas ausentes, 707 inactivo) mediante `POST /customers`.
3. Sincronizar el cliente activo (p.e. 202) dos veces y verificar creación / actualización en GeoGrid.
4. Intentar sincronizar el cliente con `integration_enabled=false` y corroborar el `412` esperado.
5. Lanzar `/sync/customer` sobre el cliente incompleto para obtener `422 missing_fields`.
6. Generar un conflicto en GeoGrid (duplicar feature) y confirmar que el flujo aplica `PUT`.
7. Provisionar la ONU del cliente activo; repetir la petición para validar la respuesta `already_authorized`.
8. Cambiar `pon_port` en el payload y comprobar el incidente `hardware_mismatch`.
9. Simular fallos en SmartOLT usando parámetros `simulate` directamente sobre el mock.
10. Procesar la baja del cliente inactivo y revisar incidentes `decommission_*`.
11. Consultar `/audits` y `/incidents` para verificar la trazabilidad y repetir los flujos desde la UI Streamlit.

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
