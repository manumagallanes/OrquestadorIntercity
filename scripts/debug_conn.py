import os
import httpx

ENV_FILE = ".env"

def main():
    env_vars = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.strip().split("=", 1)
                    env_vars[k] = v.strip("'").strip('"')

    host = "https://online20.ispcube.com"
    paths = [
        "/sanctum/token",
        "/api/sanctum/token",
        "/public/api/sanctum/token"
    ]
    
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "api-key": env_vars.get("ISP_API_KEY", ""),
        "client-id": env_vars.get("ISP_CLIENT_ID", ""),
        "login-type": "api",
    }
    
    body = {
        "username": env_vars.get("ISP_USERNAME", ""),
        "password": env_vars.get("ISP_PASSWORD", "")
    }

    print(f"Testing connectivity to {host}...")
    
    for p in paths:
        url = host + p
        print(f"\n--- Testing POST {url} ---")
        try:
            resp = httpx.post(url, headers=headers, json=body, timeout=10.0, follow_redirects=False)
            print(f"Status: {resp.status_code}")
            if resp.is_redirect:
                print(f"Redirect Location: {resp.headers.get('location')}")
            else:
                print(f"Response prefix: {resp.text[:200]}")
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    main()
