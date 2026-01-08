import os
import sys
import subprocess
import httpx

ENV_FILE = ".env"

def main():
    if not os.path.exists(ENV_FILE):
        print(f"[ERROR] No existe el archivo {ENV_FILE}")
        sys.exit(1)

    # Read env vars manually to support update later
    env_vars = {}
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        original_lines = f.readlines()
        
    for line in original_lines:
        line_clean = line.strip()
        if line_clean and not line_clean.startswith("#") and "=" in line_clean:
            key, val = line_clean.split("=", 1)
            # Remove quotes if present
            val = val.strip("'").strip('"')
            env_vars[key] = val

    required_vars = ["ISP_BASE_URL", "ISP_API_KEY", "ISP_CLIENT_ID", "ISP_USERNAME", "ISP_PASSWORD"]
    missing = [v for v in required_vars if v not in env_vars]
    if missing:
        print(f"[ERROR] Faltan variables en {ENV_FILE}: {', '.join(missing)}")
        sys.exit(1)

    base_url = env_vars["ISP_BASE_URL"].rstrip("/")
    token_url = f"{base_url}/sanctum/token"
    
    print(f"Solicitando token a {token_url}...")
    
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
            print("[ERROR] La respuesta no contiene un token.")
            print("Respuesta:", data)
            sys.exit(1)
            
    except httpx.HTTPStatusError as e:
        print(f"[ERROR] La API retornó error: {e.response.status_code}")
        print(f"Respuesta: {e.response.text}")
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] Falló la solicitud del token: {e}")
        sys.exit(1)

    print("Token obtenido exitosamente.")

    # Update .env file contents
    new_lines = []
    token_updated = False
    bearer_line_prefix = "ISP_BEARER="
    new_bearer_line = f"ISP_BEARER='Bearer {new_token}'\n"
    
    for line in original_lines:
        if line.lstrip().startswith(bearer_line_prefix):
            new_lines.append(new_bearer_line)
            token_updated = True
        else:
            new_lines.append(line)
            
    if not token_updated:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines.append("\n")
        new_lines.append(new_bearer_line)

    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
        
    print(f"[INFO] Archivo {ENV_FILE} actualizado.")

    # Restart Docker service
    print("Intentando reiniciar el servicio orchestrator...")
    try:
        # Try 'docker compose' (v2)
        cmd = ["docker", "compose", "restart", "orchestrator"]
        subprocess.run(cmd, check=True)
        print("[INFO] Contenedor reiniciado exitosamente.")
    except FileNotFoundError:
        # Try 'docker-compose' (v1)
        try:
            cmd = ["docker-compose", "restart", "orchestrator"]
            subprocess.run(cmd, check=True)
            print("[INFO] Contenedor reiniciado exitosamente.")
        except FileNotFoundError:
             print("[WARN] No se encontró el comando 'docker'. ¿Está instalado/en el PATH?")
        except subprocess.CalledProcessError as e:
            print(f"[ERROR] Error al reiniciar contenedor: {e}")
    except subprocess.CalledProcessError as e:
        # Fallback to docker-compose if docker compose failed (sometimes happens purely on naming)
         try:
            cmd = ["docker-compose", "restart", "orchestrator"]
            subprocess.run(cmd, check=True)
            print("[INFO] Contenedor reiniciado exitosamente.")
         except Exception:
             print(f"[WARN] No se pudo reiniciar el contenedor automáticamente. Error: {e}")
             print("Ejecuta manualmente: docker compose restart orchestrator")

if __name__ == "__main__":
    main()
