#!/usr/bin/env python3
"""
Smoke test de validaciones estrictas vs relajadas sin depender de ISP/GeoGrid.

Ejecuta `ensure_customer_ready` y `_customer_coordinates` en dos procesos separados
para evitar colisiones de métricas Prometheus al recargar el módulo.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from textwrap import dedent
from typing import Any, Dict


TEST_CUSTOMERS = {
    "missing_coords": {
        "customer_id": 123,
        "name": "Sin Coordenadas",
        "address": "Calle Falsa 123",
        "lat": None,
        "lng": None,
        "zone": "Centro",
    },
    "missing_network": {
        "customer_id": 456,
        "name": "Sin OLT",
        "address": "Avenida Siempre Viva 742",
        "lat": -31.42,
        "lng": -64.18,
    },
}


def run_subprocess(label: str, coord_fallback: bool, network_relaxed: bool) -> str:
    payload = json.dumps(TEST_CUSTOMERS)
    code = dedent(
        f"""
        import json, os, sys
        os.environ['ORCHESTRATOR_ALLOW_COORDINATE_FALLBACK'] = "{'true' if coord_fallback else 'false'}"
        os.environ['ORCHESTRATOR_ALLOW_MISSING_NETWORK_KEYS'] = "{'true' if network_relaxed else 'false'}"
        sys.path.insert(0, '.')
        import orchestrator.main as main
        customers = json.loads({payload!r})
        def check(label, data):
            try:
                main.ensure_customer_ready(data, action="sync")
                lat, lon = main._customer_coordinates(data)  # type: ignore[attr-defined]
                print(f"{label}: OK (lat={{{{lat}}}}, lon={{{{lon}}}})".format(lat=lat, lon=lon))
            except Exception as exc:  # noqa: BLE001
                print(f"{label}: ERROR -> {{exc}}")
        check("missing_coords", customers["missing_coords"])
        check("missing_network", customers["missing_network"])
        """
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.getcwd()
    env["ORCHESTRATOR_MIN_START_DATE"] = ""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
    )
    output = result.stdout.strip() + ("\n" + result.stderr.strip() if result.stderr.strip() else "")
    return f"[{label}] {output}"


def main() -> None:
    print("== MODO ESTRICTO ==")
    print(run_subprocess("estricto", coord_fallback=False, network_relaxed=False))
    print("\n== MODO RELAJADO ==")
    print(run_subprocess("relajado", coord_fallback=True, network_relaxed=True))


if __name__ == "__main__":
    main()
