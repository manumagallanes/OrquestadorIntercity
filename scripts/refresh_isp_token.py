#!/usr/bin/env python3
"""
Script de Renovación de Token y Actualización de Entorno.

DESCRIPCIÓN:
    Este script es crítico para el mantenimiento a largo plazo.
    Los tokens de ISP-Cube expiran periódicamente. Este utilitario:
    1.  Lee las credenciales (Usuario/Pass) del archivo `.env`.
    2.  Solicita un nuevo Token a la API de ISP-Cube.
    3.  Modifica el archivo `.env` en disco reemplazando el viejo 'ISP_BEARER'.
    4.  Reinicia el contenedor del Orquestador para que cargue la nueva configuración.

REQUISITOS:
    - Debe ejecutarse en un entorno donde tenga acceso de escritura al archivo `.env`.
    - Debe tener acceso al comando `docker` para reiniciar los servicios.
"""
import os
import sys
import subprocess
import httpx

# Nombre del archivo de configuración que vamos a modificar
ENV_FILE = ".env"

def main():
    if not os.path.exists(ENV_FILE):
        print(f"[ERROR CRÍTICO] No se encuentra el archivo {ENV_FILE}. No se puede continuar.")
        sys.exit(1)

    # Paso 1: Leer variables actuales
    env_vars = {}
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        original_lines = f.readlines()
        
    for line in original_lines:
        line_clean = line.strip()
        if line_clean and not line_clean.startswith("#") and "=" in line_clean:
            key, val = line_clean.split("=", 1)
            # Limpiar comillas si existen
            val = val.strip("'").strip('"')
            env_vars[key] = val

    # Verificar que tenemos usuario y contraseña para pedir nuevo token
    required_vars = ["ISP_BASE_URL", "ISP_API_KEY", "ISP_CLIENT_ID", "ISP_USERNAME", "ISP_PASSWORD"]
    missing = [v for v in required_vars if v not in env_vars]
    if missing:
        print(f"[ERROR] Faltan credenciales en {ENV_FILE} para poder renovar: {', '.join(missing)}")
        sys.exit(1)

    # Paso 2: Solicitar nuevo token
    base_url = env_vars["ISP_BASE_URL"].rstrip("/")
    token_url = f"{base_url}/sanctum/token"
    
    print(f"Contactando a ISP-Cube ({token_url}) para renovar credenciales...")
    
    try:
        response = httpx.post(
            token_url,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "api-key": env_vars["ISP_API_KEY"],
                "client-id": env_vars["ISP_CLIENT_ID"],
                "login-type": "api",
            },
            json={
                "username": env_vars["ISP_USERNAME"],
                "password": env_vars["ISP_PASSWORD"],
            },
            timeout=10.0
        )
        response.raise_for_status()
        data = response.json()
        new_token = data.get("token")
        if not new_token:
            print("[ERROR] La API respondió OK pero no envió ningún token.")
            print("Datos recibidos:", data)
            sys.exit(1)
            
    except httpx.HTTPStatusError as e:
        print(f"[ERROR] La API rechazó la solicitud (Status {e.response.status_code})")
        print(f"Detalle: {e.response.text}")
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] Error de conexión: {e}")
        sys.exit(1)

    print("¡Token nuevo obtenido con éxito!")

    # Paso 3: Guardar en .env
    new_lines = []
    token_updated = False
    bearer_line_prefix = "ISP_BEARER="
    new_bearer_line = f"ISP_BEARER='Bearer {new_token}'\n"
    
    for line in original_lines:
        # Si la línea empieza con ISP_BEARER=, la reemplazamos
        if line.lstrip().startswith(bearer_line_prefix):
            new_lines.append(new_bearer_line)
            token_updated = True
        else:
            new_lines.append(line)
            
    # Si no existía la variable, la agregamos al final
    if not token_updated:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines.append("\n")
        new_lines.append(new_bearer_line)

    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
        
    print(f"[INFO] Archivo {ENV_FILE} actualizado con la nueva clave.")

    # Paso 4: Reiniciar el Orquestador
    print("Aplicando cambios (Reiniciando contenedor 'orchestrator')...")
    try:
        # Intentar con docker compose v2
        cmd = ["docker", "compose", "restart", "orchestrator"]
        subprocess.run(cmd, check=True)
        print("[EXITO] Sistema reiniciado y operando con nuevas credenciales.")
    except FileNotFoundError:
        # Fallback a docker-compose v1
        try:
            cmd = ["docker-compose", "restart", "orchestrator"]
            subprocess.run(cmd, check=True)
            print("[EXITO] Sistema reiniciado y operando con nuevas credenciales.")
        except FileNotFoundError:
             print("[ADVERTENCIA] No se encontró el comando 'docker'. Deberás reiniciar el servicio manualmente.")
        except subprocess.CalledProcessError as e:
            print(f"[ERROR] Falló el reinicio automático: {e}")
    except subprocess.CalledProcessError as e:
         # Fallback si el comando 'docker compose' falló por sintaxis
         try:
            cmd = ["docker-compose", "restart", "orchestrator"]
            subprocess.run(cmd, check=True)
            print("[EXITO] Sistema reiniciado.")
         except Exception:
             print(f"[ADVERTENCIA] No se pudo reiniciar automáticamente. Por favor ejecuta: docker compose restart orchestrator")

if __name__ == "__main__":
    main()
