# 🚀 Manual de Despliegue en Servidor

Esta guía detalla paso a paso cómo instalar y poner en marcha el **Orquestador Intercity** en un servidor limpio (Producción o Staging).
Este documento está diseñado para ser utilizado por el equipo técnico encargado de la infraestructura.

---

## 📋 1. Prerrequisitos del Servidor

El sistema es contenerizado y liviano. No requiere grandes recursos de hardware.

### Hardware Recomendado
*   **CPU:** 2 vCPU
*   **RAM:** 4 GB (mínimo 2 GB)
*   **Disco:** 20 GB de espacio libre (preferiblemente SSD)
*   **SO:** Linux (Ubuntu Server 22.04 LTS recomendado)

### Software Necesario
El servidor debe tener instalado **Docker** y el plugin **Docker Compose** (versión v2).

**Instalación rápida en Ubuntu:**
```bash
# Actualizar repositorios
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg

# Instalar Docker Engine oficial
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Verificar instalación
docker compose version
# Debería mostrar: Docker Compose version v2.x.x
```

### Red y Firewall
Asegúrese de habilitar los siguientes puertos de entrada en el firewall (UFW o Security Groups):
| Puerto | Servicio | Protocolo | Descripción |
| :--- | :--- | :--- | :--- |
| **8000** | API Orquestador | TCP | Recepción de Webhooks y API REST. |
| **3000** | Grafana | TCP | Acceso a los Tableros de Monitoreo. |
| **22**   | SSH | TCP | Gestión remota del servidor. |

> **Nota:** El puerto 9091 (Prometheus) es interno y no necesita estar expuesto a internet.

### 🪟 Alternativa: Servidor Windows (PC)
Si la infraestructura disponible es una PC con Windows 10/11:

1.  **Instalar Docker Desktop:** Descargar desde [docker.com](https://www.docker.com/products/docker-desktop/).
    *   *Importante:* Durante la instalación, asegurarse de que **WSL 2** esté marcado.
2.  **Virtualización:** Verificar que la virtualización (VT-x/AMD-V) esté activada en la BIOS.
3.  **Terminal:** Se recomienda usar **PowerShell** o **Git Bash** para correr los comandos de esta guía.
    *   Ejecute: `git config --global core.autocrlf input` antes de clonar para evitar errores de formato (Linux usa LF, Windows CRLF).

---

## ⚙️ 2. Instalación Paso a Paso

### Paso 1: Obtener el Código
Clone el repositorio en la carpeta `/opt` o en el home del usuario de servicio.

```bash
cd /opt
git clone <URL_DEL_REPOSITORIO> orquestador
cd orquestador
```

### Paso 2: Configuración del Entorno (.env)
Este es el paso más crítico. Debe crear un archivo `.env` con las credenciales reales de producción (pueden pedirlo cualquier cosa).

1. Copie el archivo de ejemplo:
   ```bash
   cp .env.example .env
   ```

2. Edite el archivo `.env` con `nano .env` y complete los valores:

   **Sección ISPCube:**
   *   `ISP_API_KEY`: Token de API de ISPCube (Debe tener permisos de lectura de logs).
   *   `ISP_CLIENT_ID`, `ISP_USERNAME`, `ISP_PASSWORD`: Credenciales para obtener tokens de sesión.
   *   `ISP_BASE_URL`: URL base de la API (ej. `https://api.ispcube.com`).

   **Sección GeoGrid:**
   *   `GEOGRID_API_KEY`: Token Bearer generado en GeoGrid.
   *   `GEOGRID_BASE_URL`: URL base (ej. `https://api.geogrid.com`).

   **Otros:**
   *   `POLL_INTERVAL`: Intervalo de sondeo en segundos (ej. `600` para 10 min).

### Paso 3: Despliegue de Contenedores
Ejecute el siguiente comando para construir las imágenes e iniciar el sistema en segundo plano:

```bash
docker compose up -d --build
```

Esto levantará 4 servicios:
1.  `orchestrator` (Lógica de negocio en Python)
2.  `scheduler` (Sincronización automática)
3.  `prometheus` (Base de datos de métricas)
4.  `grafana` (Tableros visuales)

---

## ✅ 3. Validación de la Instalación

Una vez finalizado el comando anterior, verifique que todo funcione correctamente.

### Verificar Estado de Contenedores
```bash
docker compose ps
```
Todos los contenedores deben mostrar estado `Up` o `Running`.

### Verificar Logs del Orquestador
Revise que no haya errores críticos en el inicio y que se hayan cargado las métricas:
```bash
docker compose logs -f orchestrator
```
Debería ver un mensaje como: `INFO: Application startup complete.`

### Acceder al Monitoreo
Abra un navegador web e ingrese a:
*   **URL:** `http://<IP_SERVIDOR>:3000`
*   **Usuario por defecto:** `admin`
*   **Contraseña por defecto:** `admin` (le pedirá cambiarla al primer inicio).

Debería ver el dashboard "Monitoreo Orquestador Intercity" con los contadores en verde/rojo.

### 🕒 Automatización (¿Falta configurar Cron?)
**No.** El sistema incluye su propio programador de tareas interno.
El contenedor `scheduler` (visible en `docker compose ps`) se encarga automáticamente de:
1.  Buscar novedades cada 10 minutos.
2.  Reintentar fallos cada 30 minutos.
3.  Renovar tokens cada 6 horas.

**No es necesario configurar ninguna Tarea Programada de Windows ni Crontab de Linux.**

---

## 🛠️ 4. Operación y Mantenimiento

### Ver Logs en Tiempo Real
Para ver qué está haciendo el sistema de sincronización automática:
```bash
docker compose logs -f scheduler
```

### Reiniciar el Sistema
Si realiza cambios en el `.env`, debe reiniciar los contenedores:
```bash
docker compose down
docker compose up -d
```

### Persistencia de Datos
*   **Base de Datos (Estado):** Los eventos históricos se guardan en un archivo SQLite (`state.db`) dentro del volumen persistente.
*   **Backups:** Se recomienda copiar periódicamente la carpeta `orchestrator/data/`.

---

## 🧹 5. Clean Start (Inicio Limpio)

Si necesita reiniciar el sistema desde cero (por ejemplo, después de una fase de pruebas para pasar a operación real), siga estos pasos.

⚠️ **ADVERTENCIA:** Esto eliminará todo el historial de eventos, contadores de altas/bajas acumuladas y registros de incidentes de la base de datos interna.

Ejecute los siguientes comandos:

```bash
# 1. Detener todos los servicios
docker compose down

# 2. Eliminar la base de datos de estado (se regenerará automáticamente)
# Nota: La ruta es relativa a donde clonó el repositorio
sudo rm -f orchestrator/data/state.db

# 3. (Opcional) Si desea reiniciar también métricas de Prometheus
docker compose down -v  # Esto borra tamién volúmenes de Prometheus y Grafana (cuidado)

# 4. Volver a iniciar el sistema
docker compose up -d
```

Al volver a iniciar, los contadores en Grafana comenzarán en **0**.

### Copias de Seguridad (Backup)
Se recomienda hacer backup periódico del archivo `.env` y, opcionalmente, del volumen `orchestrator-data` si se desea conservar el histórico de incidentes a largo plazo.


---

## 🌐 6. Acceso Remoto en Red Local

Una vez desplegado, el servidor actúa como un **nodo central** accesible por otros equipos de la empresa.

### Obtener la IP del Servidor
En la terminal del servidor (Linux), ejecute:
```bash
hostname -I
# Ejemplo de salida: 192.168.1.50
```

### Links de Acceso para el Equipo
Comparta los siguientes enlaces con los departamentos correspondientes (reemplace `<IP_SERVIDOR>` por la IP obtenida arriba):

| Perfil | Herramienta | URL | Usuario/Pass |
| :--- | :--- | :--- | :--- |
| **Operaciones/Ventas** | Dashboard Operativo | `http://<IP_SERVIDOR>:8501` | *(Sin contraseña)* |
| **Ingeniería/Técnica** | Métricas de Rendimiento | `http://<IP_SERVIDOR>:3000` | `admin` / `admin` |
| **Desarrolladores** | Documentación API | `http://<IP_SERVIDOR>:8000/docs` | *(Sin contraseña)* |

> **Solución de Problemas de Conexión:**
> Si los equipos no pueden acceder, verifique el Firewall de Linux:
> `sudo ufw allow 8501 && sudo ufw allow 3000 && sudo ufw allow 8000`

---

**Soporte:**
Para dudas sobre la lógica de negocio, referirse al archivo `README.md` incluido en este repositorio.
