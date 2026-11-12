"""Carga las convocatorias de prueba y (opcionalmente) lanza el análisis, usando las APIs desplegadas.

Uso (en la MV de desarrollo):
  export WORKFLOW_API=https://xxxx.execute-api.us-east-1.amazonaws.com
  python3 scripts/cargar_convocatorias.py --ruc 20601234567 --analizar --fecha 2026-11-18

Solo usa la librería estándar (urllib), igual que las Lambdas.
"""
import argparse
import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent  # carpeta backend/


def call(method, url, body=None, headers=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = Request(url, data=data, method=method, headers=headers or {"Content-Type": "application/json"})
    with urlopen(req, timeout=30) as resp:
        txt = resp.read().decode()
        return resp.getcode(), (json.loads(txt) if txt else {})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ruc", default="20601234567")
    ap.add_argument("--carpeta", default=str(ROOT / "pruebas" / "convocatorias"))
    ap.add_argument("--analizar", action="store_true", help="Inicia el análisis después de cargar")
    ap.add_argument("--fecha", default="", help="fecha_referencia AAAA-MM-DD (opcional)")
    args = ap.parse_args()
    api = os.getenv("WORKFLOW_API", "").rstrip("/")
    if not api:
        sys.exit("Defina la variable WORKFLOW_API con la URL del API workflow-licitaya")

    files = sorted(Path(args.carpeta).glob("*.md"))
    _, lote = call("POST", f"{api}/lotes", {"ruc": args.ruc, "archivos": [f.name for f in files]})
    print(f"Lote creado: {lote['lote_id']}")
    for a in lote["archivos"]:
        status, _ = call("PUT", a["upload_url"], raw=(Path(args.carpeta) / a["archivo"]).read_bytes(),
                         headers={"Content-Type": lote["content_type"]})
        print(f"  [{status}] {a['archivo']}")
    if args.analizar:
        body = {"ruc": args.ruc, "lote_id": lote["lote_id"]}
        if args.fecha:
            body["fecha_referencia"] = args.fecha
        status, res = call("POST", f"{api}/analisis", body)
        print(f"Análisis iniciado [{status}]: {res['analisis_id']}")
        print(f"Consultar: GET {api}/analisis/{res['analisis_id']}")


if __name__ == "__main__":
    main()
