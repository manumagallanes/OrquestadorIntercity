import os
import httpx
import json

ENV_FILE = ".env"

def main():
    env_vars = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.strip().split("=", 1)
                    env_vars[k] = v.strip("'").strip('"')

    base_url = env_vars.get("GEOGRID_BASE_URL", "").rstrip("/")
    api_key = env_vars.get("GEOGRID_API_KEY", "")

    if not base_url or not api_key:
        print("[ERROR] Faltan GEOGRID_BASE_URL o GEOGRID_API_KEY en .env")
        return

    headers = {
        "Content-Type": "application/json",
        "api-key": api_key
    }
    
    print(f"Probando conexión a GeoGrid: {base_url}")

    # Endpoints to probe
    candidates = [
        ("/pastas", {}),
        ("/projetos", {}),
        ("/itensRede", {"item": "pasta"}),
        ("/itensRede", {"item": "pasta", "pesquisa": ""}),
        ("/itensRede", {"item": "projeto"}),
    ]

    for path, params in candidates:
        url = base_url + path
        print(f"\n--- GET {url} params={params} ---")
        try:
            resp = httpx.get(url, headers=headers, params=params, timeout=10.0)
            print(f"Status: {resp.status_code}")
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    # Print simplified structure
                    if isinstance(data, dict) and "registros" in data:
                         print(f"Registros encontrados: {len(data['registros'])}")
                         for r in data['registros'][:5]: # Show first 5
                             d = r.get("dados", r)
                             print(f" - ID: {d.get('id')} | Nome: {d.get('nome') or d.get('label') or d.get('descricao')}")
                    elif isinstance(data, list):
                        print(f"Lista encontrada: {len(data)} items")
                        for d in data[:5]:
                             print(f" - ID: {d.get('id')} | Nome: {d.get('nome') or d.get('label')}")
                    else:
                        print(str(data)[:200])
                except Exception:
                    print(resp.text[:200])
            else:
                print(f"Error: {resp.text[:200]}")
        except Exception as e:
            print(f"Excepción: {e}")

if __name__ == "__main__":
    main()
