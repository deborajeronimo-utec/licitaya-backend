"""Pruebas locales de LicitaYa (sin AWS real): moto simula S3, DynamoDB, SNS y Step Functions;
el agente IA (Groq) se reemplaza por respuestas simuladas; el workflow se ejecuta con un
intérprete local de ASL leído directamente del serverless.yml.

Ejecutar:  cd backend/workflow-licitaya && python -m pytest -q tests
"""
import csv
import importlib.util
import io
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import boto3
import pytest
import yaml
from moto import mock_aws

HERE = Path(__file__).resolve().parent
WF_DIR = HERE.parent
ROOT = WF_DIR.parent  # carpeta backend/
sys.path.insert(0, str(HERE))
from asl_local import Machine  # noqa: E402

RUC = "20601234567"
FECHA_REF = "2026-11-18"
BUCKET = "licitaya-datos-test"
SIMULADAS = json.loads((HERE / "respuestas_agente_simuladas.json").read_text(encoding="utf-8"))
EMPRESA = json.loads((ROOT / "pruebas" / "empresa.json").read_text(encoding="utf-8"))
CONV_DIR = ROOT / "pruebas" / "convocatorias"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def asl_definition() -> dict:
    cfg = yaml.safe_load((WF_DIR / "serverless.yml").read_text(encoding="utf-8"))
    template, _vars = cfg["resources"]["Resources"]["EvaluadorStateMachine"]["Properties"]["DefinitionString"]["Fn::Sub"]
    subs = {"PrepararArn": "arn:local:preparar", "ListarArn": "arn:local:listar", "EvaluarArn": "arn:local:evaluar",
            "ReporteArn": "arn:local:reporte", "OportunidadesTable": "licitaya-oportunidades-test",
            "AnalisisTable": "licitaya-analisis-test", "TopicArn": os.environ["TOPIC_ARN"]}
    for k, v in subs.items():
        template = template.replace("${" + k + "}", v)
    assert "${" not in template, "Quedaron variables sin reemplazar en el ASL"
    return json.loads(template)


def http_event(body=None, path=None, query=None):
    return {"body": json.dumps(body) if body is not None else None,
            "pathParameters": path or {}, "queryStringParameters": query or {}}


def body(resp):
    return json.loads(resp["body"])


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=BUCKET)
        ddb = boto3.client("dynamodb")
        ddb.create_table(TableName="licitaya-empresas-test", BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": "ruc", "AttributeType": "S"}],
                         KeySchema=[{"AttributeName": "ruc", "KeyType": "HASH"}])
        ddb.create_table(TableName="licitaya-analisis-test", BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": "ruc", "AttributeType": "S"},
                                               {"AttributeName": "analisis_id", "AttributeType": "S"}],
                         KeySchema=[{"AttributeName": "ruc", "KeyType": "HASH"},
                                    {"AttributeName": "analisis_id", "KeyType": "RANGE"}],
                         GlobalSecondaryIndexes=[{"IndexName": "por-id",
                                                  "KeySchema": [{"AttributeName": "analisis_id", "KeyType": "HASH"}],
                                                  "Projection": {"ProjectionType": "ALL"}}])
        ddb.create_table(TableName="licitaya-oportunidades-test", BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": "analisis_id", "AttributeType": "S"},
                                               {"AttributeName": "convocatoria_key", "AttributeType": "S"}],
                         KeySchema=[{"AttributeName": "analisis_id", "KeyType": "HASH"},
                                    {"AttributeName": "convocatoria_key", "KeyType": "RANGE"}])
        topic = boto3.client("sns").create_topic(Name="licitaya-notificaciones-test")["TopicArn"]
        # cola SQS suscrita para poder leer el "correo" publicado
        sqs = boto3.client("sqs")
        q = sqs.create_queue(QueueName="correo")["QueueUrl"]
        qarn = sqs.get_queue_attributes(QueueUrl=q, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        boto3.client("sns").subscribe(TopicArn=topic, Protocol="sqs", Endpoint=qarn,
                                      Attributes={"FilterPolicy": json.dumps({"ruc": [RUC]}), "RawMessageDelivery": "true"})
        os.environ["TOPIC_ARN"] = topic
        sm = boto3.client("stepfunctions").create_state_machine(
            name="licitaya-test", definition=json.dumps(asl_definition()),
            roleArn="arn:aws:iam::123456789012:role/LabRole")["stateMachineArn"]

        for k, v in {"EMPRESAS_TABLE": "licitaya-empresas-test", "DATA_BUCKET": BUCKET,
                     "ANALISIS_TABLE": "licitaya-analisis-test", "OPORTUNIDADES_TABLE": "licitaya-oportunidades-test",
                     "STATE_MACHINE_ARN": sm, "EMPRESAS_API_URL": "https://empresas.local",
                     "AGENTE_IA_API_URL": "https://agente.local/agente-ia-generica",
                     "WEB_URL": "http://licitaya-web.local"}.items():
            monkeypatch.setenv(k, v)
        emp = load_module("emp_handler", WF_DIR.parent / "api-empresas" / "handler.py")
        wf = load_module("wf_handler", WF_DIR / "handler.py")

        calls = {"agente": 0, "fallar": set()}

        def fake_http(method, url, payload=None, timeout=25):
            if url.startswith("https://empresas.local/empresas/"):
                r = emp.obtener_empresa({"pathParameters": {"ruc": url.rsplit("/", 1)[-1]}}, None)
                return r["statusCode"], json.loads(r["body"])
            if url == "https://agente.local/agente-ia-generica":
                calls["agente"] += 1
                # el agente real lee ambos .md desde S3: comprobamos que existan
                for link in (payload["instructions_s3_link"], payload["content_s3_link"]):
                    u = urlparse(link)
                    boto3.client("s3").get_object(Bucket=u.netloc, Key=u.path.lstrip("/"))
                name = payload["content_s3_link"].rsplit("/", 1)[-1]
                if name in calls["fallar"]:
                    return 502, {"error": "Fallo al invocar el API de Groq.", "detail": "HTTP 429 rate limit"}
                return 200, SIMULADAS[name]
            raise AssertionError(f"URL inesperada {url}")

        monkeypatch.setattr(wf, "http_json", fake_http)
        lambdas = {"arn:local:preparar": wf.preparar_instrucciones, "arn:local:listar": wf.listar_convocatorias,
                   "arn:local:evaluar": wf.evaluar_convocatoria, "arn:local:reporte": wf.generar_reporte}
        yield {"emp": emp, "wf": wf, "lambdas": lambdas, "queue": q, "calls": calls}


def registrar_empresa(env):
    r = env["emp"].crear_empresa(http_event(EMPRESA), None)
    assert r["statusCode"] == 201, r


def cargar_lote(env, nombres=None):
    nombres = nombres or sorted(p.name for p in CONV_DIR.glob("*.md"))
    r = env["wf"].crear_lote(http_event({"ruc": RUC, "archivos": nombres}), None)
    assert r["statusCode"] == 201, r
    data = body(r)
    for a in data["archivos"]:
        assert "X-Amz-Signature" in a["upload_url"] or "Signature" in a["upload_url"]
        boto3.client("s3").put_object(Bucket=BUCKET, Key=a["key"],
                                      Body=(CONV_DIR / a["archivo"]).read_bytes(), ContentType="text/markdown")
    return data["lote_id"]


def ejecutar(env, entrada):
    trace = []
    out = Machine(asl_definition(), env["lambdas"], trace).run(entrada)
    return out, trace


# ------------------------------------------------------------------ api-empresas
def test_api_empresas_validaciones(env):
    emp = env["emp"]
    assert emp.crear_empresa(http_event({**EMPRESA, "ruc": "2060123"}), None)["statusCode"] == 400
    assert body(emp.crear_empresa(http_event({**EMPRESA, "ruc": "123"}), None))["error"] == "El RUC debe tener 11 dígitos"
    registrar_empresa(env)
    dup = emp.crear_empresa(http_event(EMPRESA), None)
    assert dup["statusCode"] == 409 and body(dup)["error"] == "Ya existe una empresa con ese RUC"
    got = body(emp.obtener_empresa({"pathParameters": {"ruc": RUC}}, None))
    assert got["experiencia_facturada"] == 850000 and "Mobiliario" in got["rubros"]
    upd = emp.actualizar_empresa(http_event({**EMPRESA, "regiones": ["Lima"]}, path={"ruc": RUC}), None)
    assert upd["statusCode"] == 200
    assert emp.obtener_empresa({"pathParameters": {"ruc": "20999999999"}}, None)["statusCode"] == 404


# ------------------------------------------------------------------ lotes
def test_crear_lote_validaciones(env):
    wf = env["wf"]
    r = wf.crear_lote(http_event({"ruc": RUC, "archivos": ["bases.pdf"]}), None)
    assert r["statusCode"] == 400 and body(r)["error"] == "Solo se aceptan archivos .md"
    r = wf.crear_lote(http_event({"ruc": RUC, "archivos": [f"c{i}.md" for i in range(25)]}), None)
    assert r["statusCode"] == 400 and body(r)["error"] == "Máximo 20 convocatorias por análisis"


def test_iniciar_sin_perfil(env):
    lote = cargar_lote(env)
    r = env["wf"].iniciar_analisis(http_event({"ruc": RUC, "lote_id": lote}), None)
    assert r["statusCode"] == 400 and body(r)["error"] == "Registra primero el perfil de tu empresa"


# ------------------------------------------------------------------ flujo completo
def test_flujo_completo(env):
    wf = env["wf"]
    registrar_empresa(env)
    lote = cargar_lote(env)
    r = wf.iniciar_analisis(http_event({"ruc": RUC, "lote_id": lote, "fecha_referencia": FECHA_REF}), None)
    assert r["statusCode"] == 202, r
    analisis_id = body(r)["analisis_id"]

    # mientras está EN_PROCESO el CSV responde 409
    assert wf.descargar_csv({"pathParameters": {"analisis_id": analisis_id}}, None)["statusCode"] == 409

    salida, trace = ejecutar(env, {"ruc": RUC, "analisis_id": analisis_id, "lote_id": lote, "fecha_referencia": FECHA_REF})
    assert trace[-1] == "Fin" and "MarcarError" not in trace
    assert env["calls"]["agente"] == 10

    # instrucciones con el perfil de la empresa
    instr = boto3.client("s3").get_object(Bucket=BUCKET, Key=f"instrucciones/{analisis_id}.md")["Body"].read().decode()
    assert "Suministros Andinos S.A.C." in instr and "{{perfil}}" not in instr

    ops = body(wf.listar_oportunidades({"pathParameters": {"analisis_id": analisis_id}}, None))["oportunidades"]
    reco = {o["convocatoria_key"]: o["recomendacion"] for o in ops}
    assert reco == {
        "conv-01-papel-bond-ugel.md": "POSTULAR", "conv-02-limpieza-hospital.md": "POSTULAR",
        "conv-03-mobiliario-municipal.md": "POSTULAR", "conv-04-computo-universidad.md": "EVALUAR",
        "conv-05-uniformes-arequipa.md": "EVALUAR", "conv-06-mantenimiento-colegios.md": "EVALUAR",
        "conv-07-obra-pista.md": "DESCARTAR", "conv-08-toner-lima-norte.md": "DESCARTAR",
        "conv-09-vigilancia-junin.md": "DESCARTAR", "conv-10-alimentos-callao.md": "DESCARTAR",
    }
    motivos = {o["convocatoria_key"]: o["motivo"] for o in ops}
    assert motivos["conv-08-toner-lima-norte.md"].startswith("Plazo de presentacion insuficiente")
    assert motivos["conv-09-vigilancia-junin.md"] == "Brecha crítica: No cuenta con autorización SUCAMEC"
    assert motivos["conv-07-obra-pista.md"].startswith("Brecha crítica")
    assert motivos["conv-10-alimentos-callao.md"].startswith("Baja afinidad")
    assert motivos["conv-04-computo-universidad.md"].startswith("Brechas subsanables")
    assert [o["afinidad"] for o in ops] == sorted([o["afinidad"] for o in ops], reverse=True)
    by = {o["convocatoria_key"]: o for o in ops}
    assert by["conv-01-papel-bond-ugel.md"]["dias_para_cierre"] == 9
    assert by["conv-02-limpieza-hospital.md"]["monto_estimado"] == 312000

    filtradas = body(wf.listar_oportunidades({"pathParameters": {"analisis_id": analisis_id},
                                              "queryStringParameters": {"recomendacion": "postular"}}, None))
    assert filtradas["total"] == 3

    an = body(wf.obtener_analisis({"pathParameters": {"analisis_id": analisis_id}}, None))
    assert an["estado"] == "COMPLETADO"
    assert (an["total_postular"], an["total_evaluar"], an["total_descartar"]) == (3, 3, 4)
    assert [t["convocatoria_key"] for t in an["top3"]][0] == "conv-01-papel-bond-ugel.md"

    # CSV con ; y BOM UTF-8
    raw = boto3.client("s3").get_object(Bucket=BUCKET, Key=an["csv_key"])["Body"].read()
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"))
    assert rows[0][:3] == ["archivo", "entidad", "objeto"] and len(rows) == 11
    url = body(wf.descargar_csv({"pathParameters": {"analisis_id": analisis_id}}, None))["url"]
    assert an["csv_key"].split("/")[-1] in url

    # historial
    hist = body(wf.listar_analisis({"queryStringParameters": {"ruc": RUC}}, None))["analisis"]
    assert hist[0]["analisis_id"] == analisis_id

    # correo publicado en SNS (filtrado por RUC, asunto ASCII < 100)
    msgs = boto3.client("sqs").receive_message(QueueUrl=env["queue"], MaxNumberOfMessages=10)["Messages"]
    texto = msgs[0]["Body"]
    assert "POSTULAR:  3" in texto and "UGEL N.° 05" in texto and "http://licitaya-web.local" in texto
    assert salida["reporte"]["asunto"].isascii() and len(salida["reporte"]["asunto"]) < 100


def test_fallo_del_agente_no_detiene_el_lote(env):
    registrar_empresa(env)
    lote = cargar_lote(env, ["conv-01-papel-bond-ugel.md", "conv-07-obra-pista.md"])
    env["calls"]["fallar"].add("conv-07-obra-pista.md")
    r = env["wf"].iniciar_analisis(http_event({"ruc": RUC, "lote_id": lote, "fecha_referencia": FECHA_REF}), None)
    analisis_id = body(r)["analisis_id"]
    _, trace = ejecutar(env, {"ruc": RUC, "analisis_id": analisis_id, "lote_id": lote, "fecha_referencia": FECHA_REF})
    assert "RegistrarFallo" in trace and trace[-1] == "Fin"
    ops = {o["convocatoria_key"]: o for o in body(env["wf"].listar_oportunidades(
        {"pathParameters": {"analisis_id": analisis_id}}, None))["oportunidades"]}
    assert ops["conv-07-obra-pista.md"]["recomendacion"] == "ERROR"
    assert ops["conv-01-papel-bond-ugel.md"]["recomendacion"] == "POSTULAR"


def test_lote_vacio_marca_error(env):
    registrar_empresa(env)
    entrada = {"ruc": RUC, "analisis_id": "AN-20261118-000000-test", "lote_id": "L-vacio", "fecha_referencia": FECHA_REF}
    boto3.resource("dynamodb").Table("licitaya-analisis-test").put_item(
        Item={"ruc": RUC, "analisis_id": entrada["analisis_id"], "estado": "EN_PROCESO"})
    with pytest.raises(Exception) as exc:
        ejecutar(env, entrada)
    assert "AnalisisFallido" in str(exc.value)
    an = body(env["wf"].obtener_analisis({"pathParameters": {"analisis_id": entrada["analisis_id"]}}, None))
    assert an["estado"] == "ERROR" and an["error_detalle"].startswith("SinConvocatorias")


def test_suscripcion(env):
    r = env["wf"].suscribir(http_event({"ruc": RUC, "email": "compras@suministrosandinos.pe"}), None)
    assert r["statusCode"] == 201
    assert env["wf"].suscribir(http_event({"ruc": RUC, "email": "no-es-correo"}), None)["statusCode"] == 400


def test_normalizacion_y_asl_consistente(env):
    from datetime import date
    ev = env["wf"].normalizar_evaluacion({"afinidad": "140", "brechas": [{"descripcion": "x", "tipo": "Crítica"}],
                                          "fecha_presentacion_ofertas": "2026-11-19T00:00"}, date(2026, 11, 18))
    assert ev["afinidad"] == 100 and ev["tiene_brecha_critica"] and ev["dias_para_cierre"] == 1
    defn = asl_definition()

    def nexts(states):
        for s in states.values():
            for t in [s.get("Next"), s.get("Default")] + [c.get("Next") for c in s.get("Choices", [])] + \
                     [c.get("Next") for c in s.get("Catch", [])]:
                if t:
                    assert t in states, f"Estado inexistente: {t}"
            if s["Type"] == "Map":
                nexts(s["ItemProcessor"]["States"])
    nexts(defn["States"])
