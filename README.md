# Orquestador Intercity – Ingeniería en Telecomunicaciones

**Trabajo Final de Prácticas Profesionales**

Este repositorio alberga el código fuente y la documentación técnica del **Orquestador Intercity**, un middleware desarrollado para la automatización de procesos de aprovisionamiento y sincronización de red. El sistema actúa como puente lógico entre el Business Support System (BSS) **ISP-Cube** y el sistema de gestión geoespacial (GIS/OSS) **GeoGrid**.

---

## 1. Resumen Ejecutivo

El objetivo principal de este proyecto es reducir la carga operativa manual y minimizar errores en la gestión de altas y bajas de clientes FTTH. Para ello, el orquestador implementa una arquitectura orientada a eventos (poll-based) que detecta cambios en el sistema comercial y refleja automáticamente estos cambios en el inventario de red.

**Funcionalidades Clave:**
*   **Sincronización Automática:** Propagación de altas, bajas y modificaciones desde ISP-Cube hacia GeoGrid.
*   **Aprovisionamiento Lógico:** Asignación automática de recursos de red (puertos PON) y documentación de la acometida (Drop) georreferenciada.
*   **Validación de Datos:** Reglas de negocio estrictas para asegurar la integridad de la información (coordenadas, caja, puerto).
*   **Observabilidad:** Sistema integrado de métricas (Prometheus) y visualización (Grafana) para el monitoreo de la salud del servicio y KPIs operativos.

---

## 2. Arquitectura del Sistema

El proyecto ha evolucionado desde un script monolítico hacia una **arquitectura modular y escalable**, diseñada siguiendo principios de ingeniería de software robustos.

### 2.1 Estructura Modular
El código se organiza en capas lógicas para facilitar el mantenimiento y la extensibilidad:

*   **`orchestrator.core`**: Infraestructura base. Manejo de configuración, estado global, logging y clientes HTTP resilientes (con patrones de Retry y Circuit Breaker).
*   **`orchestrator.logic`**: Lógica de negocio pura. Contiene las reglas de validación de clientes, resolución de coordenadas, lógica de reconciliación y reportes.
*   **`orchestrator.services`**: Capa de abstracción para comunicaciones externas. Módulos dedicados para interactuar con las APIs de ISP-Cube y GeoGrid.
*   **`orchestrator.api`**: Controladores REST (FastAPI). Expone endpoints para operaciones síncronas, consultas de métricas y hooks de gestión.
*   **`orchestrator.schemas`**: Definiciones de datos (Pydantic) para garantizar contratos de interfaz estrictos.

### 2.2 Componentes de Despliegue
La solución se despliega mediante **Docker Compose**, orquestando los siguientes contenedores:

| Servicio | Rol | Descripción |
| :--- | :--- | :--- |
| **`orchestrator`** | Núcleo | Aplicación FastAPI (Python) que ejecuta la lógica de negocio. |
| **`prometheus`** | Monitoreo | Recolector de series temporales para métricas de rendimiento y negocio. |
| **`grafana`**  | Visualización | Dashboards para la visualización de incidentes, tasas de sincronización y estado del sistema. |
| **`ui`**      | Interfaz | Herramienta auxiliar (Streamlit) para pruebas manuales y consultas rápidas. |

---

## 3. Flujo de Información

El sistema opera bajo un modelo de **consumidor inteligente**:

1.  **Detección:** Un proceso de sondeo (*polling*) consulta periódicamente los logs de aprovisionamiento de ISP-Cube.
2.  **Procesamiento:** El orquestador recibe la novedad (alta/baja) y aplica validaciones:
    *   ¿El cliente tiene coordenadas válidas?
    *   ¿La caja y puerto asignados existen en el inventario?
3.  **Ejecución:**
    *   **En GeoGrid:** Se crea/actualiza el cliente, se genera la "casita" (punto de acceso) y se documenta el cable de bajada (Drop) conectado al puerto específico.
4.  **Auditoría:** Cada acción genera un registro de auditoría. Si ocurre un error (ej. datos inconsistentes), se registra un **Incidente** para su corrección manual posterior.

### Diagrama de Arquitectura

### Diagrama de Flujo Simplificado

```mermaid
graph LR
    %% Flujo lineal con terminología técnica
    ISP[ISP-Cube\n(CRM Comercial)] -->|Nuevas Altas| SCH(Detector Automático)
    SCH -->|Procesa| ORCH[Orquestador\n(Middleware)]
    
    ORCH -->|1. Valida| DB[(Estado Interno)]
    ORCH -->|2. Provisiona| GEO[GeoGrid\n(GIS Técnico)]
    
    ORCH -.->|3. Métricas| GRAF[Grafana\n(Monitoreo KPIs)]

    %% Estilos visuales (Colores suaves)
    style ISP fill:#e3f2fd,stroke:#1565c0,stroke-width:2px
    style GEO fill:#e3f2fd,stroke:#1565c0,stroke-width:2px
    style ORCH fill:#fff3e0,stroke:#ef6c00,stroke-width:2px
    style GRAF fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px
```

> **Nota Técnica:** El orquestador no inventaria la red ni crea elementos pasivos (Cajas, Splitters) por sí mismo; consume la infraestructura ya documentada en GeoGrid para asignar clientes a recursos existentes.

### 3.1 Integración de APIs Externas
La lógica de orquestación se construyó basándose estrictamente en las especificaciones de interfaz (contratos) de los proveedores:
*   **ISP-Cube API (v1):** Utilizada para la extracción de logs (`/connections/connections_provisioning_logs`) y autenticación basada en Tokens temporales.
*   **GeoGrid API (v3):** Utilizada para la manipulación topológica de la red. Se implementaron algoritmos para determinar la caja más cercana y conectar el "Drop" virtualmente.

---

## 4. Conceptos de Ingeniería Aplicados

Para garantizar la robustez necesaria en un entorno crítico de telecomunicaciones, se aplicaron los siguientes patrones de diseño y estrategias:

### 4.1 Patrones de Arquitectura
*   **Clean Architecture (Capas):** Separación estricta entre la lógica de dominio (`logic/`), los adaptadores de servicios externos (`services/`) y la capa de exposición (`api/`). Esto desacopla el negocio de la tecnología de transporte.
*   **Event-Driven (Pull-Based):** En lugar de acoplamiento fuerte síncrono, el sistema reacciona a eventos asíncronos consultados periódicamente, lo que reduce el impacto en el rendimiento de los sistemas BSS/OSS.

### 4.2 Resiliencia y Fiabilidad
*   **Retry Pattern & Exponential Backoff:** Las comunicaciones HTTP inestables se manejan mediante librerías como `tenacity` y lógica propia en los scripts de *polling*, reintentando operaciones fallidas con tiempos de espera crecientes.
*   **Auto-Healing (Autosanación):** El script `retry_incidents.py` actúa como un agente de recuperación, monitoreando fallos transitorios (ej. "GeoGrid inalcanzable") y reintentándolos automáticamente sin intervención humana.
*   **Idempotencia:** El diseño garantiza que procesar el mismo evento de cliente múltiples veces tenga el mismo resultado y no genere duplicados en el inventario.

### 4.3 Tecnologías Clave (Python Moderno)
*   **Type Hinting & Pydantic:** Uso extensivo de tipado estático y validación de esquemas en tiempo de ejecución para prevenir errores de tipo comunes en lenguajes dinámicos.
*   **Asincronía (AsyncIO):** Preparado para alta concurrencia en I/O bound tasks utilizando FastAPI y servidores ASGI.
*   **Persistencia Ligera:** Uso de SQLite para mantener un estado local robusto de métricas y cursores, evitando la complejidad de mantener una base de datos externa pesada (Postgres/MySQL) para esta escala.

---

## 5. Requisitos Previos

Para ejecutar este proyecto en un entorno local o productivo, se requiere:

*   **Docker & Docker Compose**: Para la contenerización de los servicios.
*   **Acceso a APIs**: Credenciales válidas para ISP-Cube (Token/Usuario) y GeoGrid (API Key).
*   **Python 3.10+** (Opcional, solo para desarrollo/tests locales fuera de Docker).

---

## 6. Instalación y Puesta en Marcha

### 6.1 Configuración de Entorno
1.  Clonar el repositorio.
2.  Crear un archivo `.env` basado en `.env.example`.
3.  Configurar las URLs y credenciales de los servicios externos en `config/environments/dev.json` (o el entorno correspondiente).

**Importante:** La versión actual **no utiliza mocks**. Requiere conexión real a los servicios o configuración adecuada de stubs externos si se desea simular tráfico.

### 6.2 Ejecución
Iniciar todos los servicios:

```bash
docker-compose up --build -d
```

### 6.3 Verificación
*   **API Docs:** [http://localhost:8000/docs](http://localhost:8000/docs)
*   **Estado de Salud:** [http://localhost:8000/health](http://localhost:8000/health)
*   **Dashboards:** [http://localhost:3000](http://localhost:3000) (Credenciales: admin/admin)

---

## 7. Pruebas Automatizadas

Como parte de la ingeniería de calidad del software, se ha incluido una suite de pruebas automatizadas (unitarias e integración) que validan la lógica de dominio y la integridad de los endpoints.

Para ejecutar las pruebas:

```bash
# Instalar dependencias de prueba
pip install -r requirements.txt

# Ejecutar suite de pruebas con pytest
python -m pytest tests
```

---

## 8. Scripts de Mantenimiento

Se incluyen scripts en `scripts/` para tareas operativas recurrentes:

*   `poll_isp_connections.py`: Ejecución manual o programada (Cron) del ciclo de detección de cambios.
*   `replay_provisioning.py`: Herramienta para reprocesar eventos pasados (reconciliación) sin afectar el cursor principal.
*   `retry_incidents.py`: Reintento automático de sincronizaciones fallidas por errores transitorios.

---

## 9. Licencia y Autoría

Este proyecto forma parte de las prácticas profesionales de **Manuel Magallanes** para la carrera de Ingeniería en Telecomunicaciones.

**Licencia:** Ver archivo `LICENSE`.
