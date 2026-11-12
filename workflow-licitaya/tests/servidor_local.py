"""Servidor local para probar la web contra el backend real (handlers) sin AWS.

- moto en modo servidor (http://localhost:5000) simula S3, DynamoDB, SNS y Step Functions.
- Este script expone las rutas de api-empresas y workflow-licitaya en http://localhost:8787.
- StartExecution corre el ASL del serverless.yml con el intérprete local (en un hilo).
- El agente IA usa las respuestas simuladas de tests/respuestas_agente_simuladas.json.

Uso:  python tests/servidor_local.py   y luego, en frontend/web-licitaya:
      VITE_EMPRESAS_API=http://localhost:8787 VITE_WORKFLOW_API=http://localhost:8787 \
      VITE_FECHA_REFERENCIA=2026-11-18 npm run dev
"""
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ.update({"AWS_DEFAULT_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
                   "AWS_ENDPOINT_URL": "http://localhost:5000"})

from moto.server import ThreadedMotoServer  # noqa: E402

moto = ThreadedMotoServer(port=5000)
moto.start()

import boto3  # noqa: E402
import test_licitaya as T  # noqa: E402  (reutiliza utilidades y nombres)
from asl_local import Machine  # noqa: E402

s3 = boto3.client("s3")
s3.create_bucket(Bucket=T.BUCKET)
s3.put_bucket_cors(Bucket=T.BUCKET, CORSConfiguration={"CORSRules": [
    {"AllowedOrigins": ["*"], "AllowedMethods": ["PUT", "GET", "HEAD"], "AllowedHeaders": ["*"]}]})
ddb = boto3.client("dynamodb")
for name, keys, gsi in [("licitaya-empresas-test", [("ruc", "HASH")], None),
                        ("licitaya-analisis-test", [("ruc", "HASH"), ("analisis_id", "RANGE")], "analisis_id"),
                        ("licitaya-oportunidades-test", [("analisis_id", "HASH"), ("convocatoria_key", "RANGE")], None)]:
    kw = dict(TableName=name, BillingMode="PAY_PER_REQUEST",
              AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k, _ in keys],
              KeySchema=[{"AttributeName": k, "KeyType": t} for k, t in keys])
    if gsi:
        kw["GlobalSecondaryIndexes"] = [{"IndexName": "por-id", "KeySchema": [{"AttributeName": gsi, "KeyType": "HASH"}],
                                         "Projection": {"ProjectionType": "ALL"}}]
    ddb.create_table(**kw)
os.environ["TOPIC_ARN"] = boto3.client("sns").create_topic(Name="licitaya-notificaciones-test")["TopicArn"]
for k, v in {"EMPRESAS_TABLE": "licitaya-empresas-test", "DATA_BUCKET": T.BUCKET, "ANALISIS_TABLE": "licitaya-analisis-test",
             "OPORTUNIDADES_TABLE": "licitaya-oportunidades-test", "STATE_MACHINE_ARN": "arn:local:sm",
             "EMPRESAS_API_URL": "http://localhost:8787", "AGENTE_IA_API_URL": "http://agente.local",
             "WEB_URL": "http://localhost:4173"}.items():
    os.environ[k] = v

emp = T.load_module("emp_handler", T.WF_DIR.parent / "api-empresas" / "handler.py")
wf = T.load_module("wf_handler", T.WF_DIR / "handler.py")


def fake_http(method, url, payload=None, timeout=25):
    if url.startswith("http://localhost:8787/empresas/"):
        r = emp.obtener_empresa({"pathParameters": {"ruc": url.rsplit("/", 1)[-1]}}, None)
        return r["statusCode"], json.loads(r["body"])
    time.sleep(0.6)  # latencia simulada del LLM
    return 200, T.SIMULADAS.get(payload["content_s3_link"].rsplit("/", 1)[-1], {"afinidad": 50})


wf.http_json = fake_http
LAMBDAS = {"arn:local:preparar": wf.preparar_instrucciones, "arn:local:listar": wf.listar_convocatorias,
           "arn:local:evaluar": wf.evaluar_convocatoria, "arn:local:reporte": wf.generar_reporte}


class FakeSFN:
    def start_execution(self, stateMachineArn, name, input):
        threading.Thread(target=lambda: Machine(T.asl_definition(), LAMBDAS).run(json.loads(input)), daemon=True).start()
        return {"executionArn": f"arn:local:execution:{name}"}


wf._clients["stepfunctions"] = FakeSFN()

ROUTES = [
    ("POST", r"^/empresas$", emp.crear_empresa, []),
    ("GET", r"^/empresas/(?P<ruc>[^/]+)$", emp.obtener_empresa, ["ruc"]),
    ("PUT", r"^/empresas/(?P<ruc>[^/]+)$", emp.actualizar_empresa, ["ruc"]),
    ("POST", r"^/lotes$", wf.crear_lote, []),
    ("POST", r"^/analisis$", wf.iniciar_analisis, []),
    ("GET", r"^/analisis$", wf.listar_analisis, []),
    ("GET", r"^/analisis/(?P<analisis_id>[^/]+)$", wf.obtener_analisis, ["analisis_id"]),
    ("GET", r"^/analisis/(?P<analisis_id>[^/]+)/oportunidades$", wf.listar_oportunidades, ["analisis_id"]),
    ("GET", r"^/analisis/(?P<analisis_id>[^/]+)/csv$", wf.descargar_csv, ["analisis_id"]),
    ("POST", r"^/suscripciones$", wf.suscribir, []),
]


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,OPTIONS")

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def _dispatch(self):
        u = urlparse(self.path)
        for method, pattern, fn, _ in ROUTES:
            m = re.match(pattern, u.path)
            if m and method == self.command:
                length = int(self.headers.get("Content-Length") or 0)
                event = {"body": self.rfile.read(length).decode() if length else None,
                         "pathParameters": m.groupdict(),
                         "queryStringParameters": {k: v[0] for k, v in parse_qs(u.query).items()}}
                r = fn(event, None)
                self.send_response(r["statusCode"]); self._cors()
                self.send_header("Content-Type", "application/json; charset=utf-8"); self.end_headers()
                self.wfile.write(r["body"].encode())
                return
        self.send_response(404); self._cors(); self.end_headers()

    do_GET = do_POST = do_PUT = _dispatch

    def log_message(self, *a):
        pass


print("API local en http://localhost:8787 (moto en :5000)", flush=True)
ThreadingHTTPServer(("0.0.0.0", 8787), Handler).serve_forever()
