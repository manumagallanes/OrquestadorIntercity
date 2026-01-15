# Orquestador de Aprovisionamiento FTTH - Intercity Telecomunicaciones

**Informe Técnico y Documentación de Proyecto**
*Prácticas Profesionales Supervisadas - Ingeniería en Telecomunicaciones*

---

## 1. Introducción y Valor de Negocio

Este proyecto surge de la necesidad crítica de **integrar los dos pilares operativos** de un proveedor de servicios de internet (ISP): el área **Administrativa/Comercial** y el área **Técnica/Ingeniería**.

### 1.1 Problemática Original
Tradicionalmente, la gestión de altas de clientes implicaba procesos manuales desconectados:
1.  **Ventas/Admin** cargaba el cliente en el CRM (ISP-Cube) para facturación.
2.  **Técnica** debía recibir una orden manual, ir al sitio, y luego cargar manualmente la documentación de red en el GIS (GeoGrid).

Esta desconexión generaba:
*   **Inconsistencia de Datos:** Diferencias entre lo que se factura y lo que está instalado realmente.
*   **Errores Humanos:** Asignación incorrecta de puertos o cajas, coordenadas erróneas.
*   **Retrasos Operativos:** Dependencia de la intervención humana para "mover los papeles".

### 1.2 Solución Implementada
El **Orquestador Intercity** actúa como un middleware inteligente que automatiza el ciclo de vida del aprovisionamiento.

**Impacto Transversal en la Organización:**
*   **Para Administración/Facturación:** Garantiza que cada cliente dado de alta tenga inmediatamente su contraparte técnica asignada, asegurando la trazabilidad del servicio facturable.
*   **Para Ingeniería/Técnica:** Elimina la carga de documentación manual. El sistema asigna automáticamente puertos PON libres, valida factibilidad técnica (cobertura) y documenta la acometida (Drop) con precisión geográfica.
*   **Para la Gerencia:** Proporciona un "tablero de control" unificado, visibilizando en tiempo real la eficiencia operativa y detectando anomalías (ej. clientes activos sin documentación técnica).

---

## 2. Conceptos de Ingeniería Aplicados

El desarrollo se fundamenta en principios sólidos de ingeniería de software y sistemas distribuidos, priorizando la **robustez** sobre la velocidad de implementación.

### 2.1 Resiliencia y Fallos (Fault Tolerance)
En telecomunicaciones, los sistemas no pueden detenerse por fallos externos. Se implementaron patrones específicos:
*   **Circuit Breakers:** Si un servicio externo (GeoGrid) no responde, el sistema "abre el circuito" para evitar saturarlo y acumular errores, reintentando paulatinamente.
*   **Exponential Backoff:** Ante fallos transitorios (Network Glitch), los reintentos se espacian exponencialmente en el tiempo (1s, 2s, 4s, 8s...), optimizando el uso de recursos.
*   **Auto-Healing (Autosanación):** El sistema es capaz de detectar tokens de sesión expirados y renovarlos automáticamente sin detener el flujo de trabajo.

### 2.2 Integridad de Datos (Data Quality)
El sistema no es un simple "pasa-amanos" de datos; actúa como un **filtro de calidad**.
*   **Sanitización Heurística:** Implementación de algoritmos matemáticos para corregir errores de entrada comunes (ej. transformar coordenadas enteras `-3310555` a decimales `-33.10555` automáticamente).
*   **Validación de Tipos Estricta:** Uso de **Pydantic** para garantizar que los datos cumplan contratos estrictos antes de procesarlos.
*   **Idempotencia:** Garantía de que procesar el mismo evento (alta de cliente) múltiples veces produce el mismo resultado consistente, evitando duplicados en la base de datos de red.

### 2.3 Concurrencia y Performance (AsyncIO)
Dado que la naturaleza del orquestador es **I/O Bound** (esperar respuestas de APIs externas), se utilizó **Python AsyncIO** (con FastAPI y HTTPX).
*   Esto permite manejar múltiples solicitudes de sincronización en paralelo sin bloquear el hilo principal de ejecución, optimizando drásticamente el uso de CPU y memoria en comparación con modelos sincrónicos tradicionales.

---

## 3. Arquitectura del Sistema

Se adoptó una arquitectura de **Microservicios** basada en **Clean Architecture**, desacoplando la lógica de negocio de la infraestructura.

### 3.1 Estructura de Capas
1.  **Capa de Dominio (`/logic`):** Contiene las reglas puras del negocio (ej. "¿Cómo sé si un puerto es válido?", "¿Cuándo se considera un cliente 'en zona'?"). No depende de ninguna librería externa.
2.  **Capa de Servicios (`/services`):** Adaptadores que saben "hablar" con el mundo exterior (APIs de ISP-Cube y GeoGrid). Traducen los datos externos al lenguaje del dominio interno.
3.  **Capa de Infraestructura (`/core`):** Configuración, manejo de base de datos (SQLite), logging y utilidades de bajo nivel.

### 3.2 Stack Tecnológico
*   **Lenguaje:** Python 3.11 (Tipado, Async).
*   **Contenerización:** Docker & Docker Compose (Portabilidad total).
*   **Frontend:** Streamlit (Panel de operación sobrio y eficiente).
*   **Observabilidad:** Prometheus (Métricas) + Grafana (Visualización).

---

## 4. Flujo de Trabajo Automatizado

1.  **Detección (Polling Inteligente):**
    Un proceso cronometrado consulta los logs de auditoría del CRM. A diferencia de un Webhook pasivo, este método activo nos da control sobre la tasa de procesamiento y permite "rebobinar" la historia si es necesario.

2.  **Reconciliación de Datos:**
    Al detectar un alta/baja, el orquestador:
    *   Recupera el estado actual del cliente técnico en GeoGrid.
    *   Compara con la "verdad" comercial de ISP-Cube.
    *   Calcula el "delta" (diferencia) necesario para alinear ambos sistemas.

3.  **Ejecución y Feedback:**
    *   Si los datos son válidos → Se ejecuta la orden en el GIS.
    *   Si los datos son inválidos (ej. fuera de zona) → Se genera un **Incidente** registrado en base de datos.
    *   Los operadores pueden ver estos incidentes en el Dashboard Web y corregirlos en el CRM, tras lo cual el orquestador los reprocesará automáticamente.

---

## 5. Conclusión para el Informe de PPS

Este proyecto demuestra la aplicación práctica de competencias clave de la ingeniería:
*   **Integración de Sistemas Heterogéneos** (CRM vs GIS).
*   **Automatización de Procesos Críticos**.
*   **Diseño de Software Robusto y Escalable**.

El resultado es una herramienta que transforma la operación de Intercity, pasando de una gestión reactiva y manual a una **gestión proactiva, automatizada y basada en datos fiables**.

---
*Autor: Manuel Magallanes*
