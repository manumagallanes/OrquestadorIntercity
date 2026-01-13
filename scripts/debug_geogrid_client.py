import asyncio
import sys
sys.path.append("/app")
import os
import json
import httpx
from orchestrator.config import get_settings

async def main():
    # Cargar config (asume variables de entorno cargadas en docker)
    try:
        settings = get_settings()
    except Exception:
        # Fallback manual si falla pydantic
        class Settings:
            geogrid_base_url = os.getenv("GEOGRID_BASE_URL")
            geogrid_api_token = os.getenv("GEOGRID_API_TOKEN")
        settings = Settings()

    if not settings.geogrid_base_url or not settings.geogrid_api_token:
        print("Error: Falta config GEOGRID en entorno")
        return

    target = "11140"
    base_url = settings.geogrid_base_url.rstrip('/')
    
    # 1. Buscar por query general (nombre/codigo)
    print(f"--- Buscando '{target}' en /clientes ---")
    async with httpx.AsyncClient(verify=False) as client:
        try:
            resp = await client.get(
                f"{base_url}/clientes",
                params={"q": target},
                headers={"Authorization": f"Bearer {settings.geogrid_api_token}"},
                timeout=10
            )
            data = resp.json()
            print(json.dumps(data, indent=2))
        except Exception as e:
            print(f"Error consulta 1: {e}")

if __name__ == "__main__":
    asyncio.run(main())
