"""workflow-licitaya – LicitaYa
Workflow Agéntico de Nivel 4 (AWS Step Functions) + API para la web.

API (HTTP API, invocadas desde la web / Postman):
  POST /lotes                                   crear_lote
  POST /analisis                                iniciar_analisis
  GET  /analisis?ruc=                           listar_analisis
  GET  /analisis/{analisis_id}                  obtener_analisis
  GET  /analisis/{analisis_id}/oportunidades    listar_oportunidades
  GET  /analisis/{analisis_id}/csv              descargar_csv
  POST /suscripciones                           suscribir

Pasos del workflow (invocados por Step Functions):
  preparar_instrucciones -> listar_convocatorias -> [Map] evaluar_convocatoria -> generar_reporte
Las decisiones POSTULAR / EVALUAR / DESCARTAR las toma el estado Choice (ver serverless.yml).
"""
import base64
import csv
import io
import json
import os
import re
import unicodedata
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

# ---------------------------------------------------------------- configuración
DATA_BUCKET = os.getenv("DATA_BUCKET", "licitaya-datos-dev")
ANALISIS_TABLE = os.getenv("ANALISIS_TABLE", "licitaya-analisis-dev")
OPORTUNIDADES_TABLE = os.getenv("OPORTUNIDADES_TABLE", "licitaya-oportunidades-dev")
TOPIC_ARN = os.getenv("TOPIC_ARN", "")
STATE_MACHINE_ARN = os.getenv("STATE_MACHINE_ARN", "")
EMPRESAS_API_URL = os.getenv("EMPRESAS_API_URL", "").rstrip("/")
AGENTE_IA_API_URL = os.getenv("AGENTE_IA_API_URL", "")
WEB_URL = os.getenv("WEB_URL", "")
MAX_CONVOCATORIAS = int(os.getenv("MAX_CONVOCATORIAS", "20"))
LIMA_TZ = timezone(timedelta(hours=-5))
PLANTILLA_PATH = Path(__file__).with_name("plantilla_instrucciones.md")

RUC_RE = re.compile(r"^\d{11}$")
ID_RE = re.compile(r"^[A-Za-z0-9\-]{6,64}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_clients: Dict[str, Any] = {}


def client(name: str):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def ddb():
    if "ddb_resource" not in _clients:
        _clients["ddb_resource"] = boto3.resource("dynamodb")
    return _clients["ddb_resource"]


# ---------------------------------------------------------------- utilidades HTTP
def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def today_lima() -> date:
    return datetime.now(LIMA_TZ).date()


def to_json(value: Any) -> Any:
    if isinstance(value, list):
        return [to_json(v) for v in value]
    if isinstance(value, dict):
        return {k: to_json(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    return value


def response(status: int, payload: Any) -> Dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": json.dumps(to_json(payload), ensure_ascii=False),
    }


def parse_body(event: Dict[str, Any]) -> Dict[str, Any]:
    body = event.get("body")
    if not body:
        return {}
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    if isinstance(body, dict):
        return body
    try:
        data = json.loads(body)
    except json.JSONDecodeError as error:
        raise ValueError("El cuerpo de la solicitud no es un JSON válido.") from error
    if not isinstance(data, dict):
        raise ValueError("El cuerpo de la solicitud debe ser un objeto JSON.")
    return data


def http_json(method: str, url: str, payload: Any = None, timeout: int = 25) -> Tuple[int, Any]:
    """Llamada HTTP con urllib (sin requests). Devuelve (status, json)."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", "User-Agent": "licitaya-workflow/1.0"})
    try:
        with urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.getcode(), (json.loads(raw) if raw else {})
    except HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        try:
            return error.code, json.loads(raw)
        except json.JSONDecodeError:
            return error.code, {"error": raw}
    except URLError as error:
        raise RuntimeError(f"No se pudo conectar con {url}: {error.reason}") from error


def obtener_empresa(ruc: str) -> Optional[Dict[str, Any]]:
    if not EMPRESAS_API_URL:
        raise RuntimeError("La variable EMPRESAS_API_URL no está configurada.")
    status, data = http_json("GET", f"{EMPRESAS_API_URL}/empresas/{ruc}", timeout=10)
    if status == 404:
        return None
    if status != 200:
        raise RuntimeError(f"api-empresas devolvió HTTP {status}: {data}")
    return data


def nuevo_id(prefijo: str) -> str:
    stamp = datetime.now(LIMA_TZ).strftime("%Y%m%d-%H%M%S")
    return f"{prefijo}-{stamp}-{uuid.uuid4().hex[:4]}"


def prefijo_lote(ruc: str, lote_id: str) -> str:
    return f"convocatorias/{ruc}/{lote_id}/"


def listar_md(prefix: str) -> List[str]:
    keys: List[str] = []
    paginator = client("s3").get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=DATA_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].lower().endswith(".md"):
                keys.append(obj["Key"])
    return sorted(keys)


def ascii_text(text: str, limit: int = 99) -> str:
    """SNS exige asunto ASCII sin saltos de línea y de menos de 100 caracteres."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[\r\n]+", " ", plain)[:limit]


# =================================================================== API
def crear_lote(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """POST /lotes  {ruc, archivos:[nombres]} -> lote_id + URLs prefirmadas PUT (HU02)."""
    try:
        data = parse_body(event)
    except ValueError as error:
        return response(400, {"error": str(error)})
    ruc = str(data.get("ruc", "")).strip()
    archivos = data.get("archivos") or []
    if not RUC_RE.match(ruc):
        return response(400, {"error": "El RUC debe tener 11 dígitos"})
    if not isinstance(archivos, list) or not archivos:
        return response(400, {"error": "Envía al menos una convocatoria"})
    no_md = [a for a in archivos if not str(a).lower().endswith(".md")]
    if no_md:
        return response(400, {"error": "Solo se aceptan archivos .md", "rechazados": no_md})
    if len(archivos) > MAX_CONVOCATORIAS:
        return response(400, {"error": f"Máximo {MAX_CONVOCATORIAS} convocatorias por análisis"})

    lote_id = nuevo_id("L")
    urls = []
    for nombre in archivos:
        limpio = re.sub(r"[^A-Za-z0-9._\-]", "_", Path(str(nombre)).name)
        key = prefijo_lote(ruc, lote_id) + limpio
        url = client("s3").generate_presigned_url(
            "put_object",
            Params={"Bucket": DATA_BUCKET, "Key": key, "ContentType": "text/markdown"},
            ExpiresIn=900,
        )
        urls.append({"archivo": limpio, "key": key, "upload_url": url})
    return response(201, {
        "lote_id": lote_id,
        "mensaje": f"{len(urls)} convocatorias listas para cargar",
        "content_type": "text/markdown",
        "archivos": urls,
    })


def iniciar_analisis(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """POST /analisis {ruc, lote_id, fecha_referencia?} -> 202 (HU03)."""
    try:
        data = parse_body(event)
    except ValueError as error:
        return response(400, {"error": str(error)})
    ruc = str(data.get("ruc", "")).strip()
    lote_id = str(data.get("lote_id", "")).strip()
    if not RUC_RE.match(ruc):
        return response(400, {"error": "El RUC debe tener 11 dígitos"})
    if not ID_RE.match(lote_id):
        return response(400, {"error": "lote_id inválido"})
    fecha_ref = str(data.get("fecha_referencia") or today_lima().isoformat())
    try:
        date.fromisoformat(fecha_ref)
    except ValueError:
        return response(400, {"error": "fecha_referencia debe tener formato AAAA-MM-DD"})

    try:
        empresa = obtener_empresa(ruc)
    except RuntimeError as error:
        return response(502, {"error": "No se pudo consultar api-empresas", "detalle": str(error)})
    if not empresa:
        return response(400, {"error": "Registra primero el perfil de tu empresa"})

    archivos = listar_md(prefijo_lote(ruc, lote_id))
    if not archivos:
        return response(400, {"error": "El lote no tiene convocatorias .md cargadas"})
    if len(archivos) > MAX_CONVOCATORIAS:
        return response(400, {"error": f"Máximo {MAX_CONVOCATORIAS} convocatorias por análisis"})

    analisis_id = nuevo_id("AN")
    item = {
        "ruc": ruc, "analisis_id": analisis_id, "lote_id": lote_id, "estado": "EN_PROCESO",
        "total_convocatorias": len(archivos), "fecha_referencia": fecha_ref,
        "razon_social": empresa.get("razon_social", ""), "creado_en": now_iso(),
    }
    ddb().Table(ANALISIS_TABLE).put_item(Item=item)
    try:
        execution = client("stepfunctions").start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=analisis_id,
            input=json.dumps({"ruc": ruc, "analisis_id": analisis_id, "lote_id": lote_id,
                              "fecha_referencia": fecha_ref}),
        )
    except (ClientError, BotoCoreError) as error:
        ddb().Table(ANALISIS_TABLE).update_item(
            Key={"ruc": ruc, "analisis_id": analisis_id},
            UpdateExpression="SET estado = :e, error_detalle = :d",
            ExpressionAttributeValues={":e": "ERROR", ":d": str(error)})
        return response(502, {"error": "No fue posible iniciar el workflow", "detalle": str(error)})
    ddb().Table(ANALISIS_TABLE).update_item(
        Key={"ruc": ruc, "analisis_id": analisis_id},
        UpdateExpression="SET execution_arn = :a",
        ExpressionAttributeValues={":a": execution["executionArn"]})
    return response(202, {"analisis_id": analisis_id, "estado": "EN_PROCESO",
                          "total_convocatorias": len(archivos),
                          "mensaje": "Análisis iniciado. Te avisaremos por correo cuando termine."})


def _get_analisis(analisis_id: str) -> Optional[Dict[str, Any]]:
    res = ddb().Table(ANALISIS_TABLE).query(
        IndexName="por-id", KeyConditionExpression=Key("analisis_id").eq(analisis_id))
    items = res.get("Items", [])
    return items[0] if items else None


def listar_analisis(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """GET /analisis?ruc= -> historial del más reciente al más antiguo (HU08)."""
    ruc = (event.get("queryStringParameters") or {}).get("ruc", "")
    if not RUC_RE.match(ruc):
        return response(400, {"error": "El RUC debe tener 11 dígitos"})
    res = ddb().Table(ANALISIS_TABLE).query(
        KeyConditionExpression=Key("ruc").eq(ruc), ScanIndexForward=False, Limit=50)
    return response(200, {"analisis": res.get("Items", [])})


def obtener_analisis(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """GET /analisis/{analisis_id} -> estado, totales, top 3 y avance."""
    analisis_id = (event.get("pathParameters") or {}).get("analisis_id", "")
    item = _get_analisis(analisis_id) if ID_RE.match(analisis_id) else None
    if not item:
        return response(404, {"error": "Análisis no encontrado"})
    if item.get("estado") == "EN_PROCESO":
        res = ddb().Table(OPORTUNIDADES_TABLE).query(
            KeyConditionExpression=Key("analisis_id").eq(analisis_id), Select="COUNT")
        item["evaluadas"] = res.get("Count", 0)
    return response(200, item)


def _oportunidades(analisis_id: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {"KeyConditionExpression": Key("analisis_id").eq(analisis_id)}
    while True:
        res = ddb().Table(OPORTUNIDADES_TABLE).query(**kwargs)
        items.extend(res.get("Items", []))
        if "LastEvaluatedKey" not in res:
            break
        kwargs["ExclusiveStartKey"] = res["LastEvaluatedKey"]
    items.sort(key=lambda x: (-(x.get("afinidad") or 0), x.get("convocatoria_key", "")))
    return items


def listar_oportunidades(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """GET /analisis/{analisis_id}/oportunidades?recomendacion= (HU04, HU05)."""
    analisis_id = (event.get("pathParameters") or {}).get("analisis_id", "")
    if not ID_RE.match(analisis_id):
        return response(404, {"error": "Análisis no encontrado"})
    filtro = ((event.get("queryStringParameters") or {}).get("recomendacion") or "").upper()
    items = _oportunidades(analisis_id)
    if filtro:
        items = [i for i in items if i.get("recomendacion") == filtro]
    return response(200, {"analisis_id": analisis_id, "total": len(items), "oportunidades": items})


def descargar_csv(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """GET /analisis/{analisis_id}/csv -> URL prefirmada (HU06)."""
    analisis_id = (event.get("pathParameters") or {}).get("analisis_id", "")
    item = _get_analisis(analisis_id) if ID_RE.match(analisis_id) else None
    if not item:
        return response(404, {"error": "Análisis no encontrado"})
    if item.get("estado") != "COMPLETADO" or not item.get("csv_key"):
        return response(409, {"error": "El análisis aún está en proceso", "estado": item.get("estado")})
    url = client("s3").generate_presigned_url(
        "get_object",
        Params={"Bucket": DATA_BUCKET, "Key": item["csv_key"],
                "ResponseContentDisposition": f'attachment; filename="{analisis_id}.csv"'},
        ExpiresIn=900)
    return response(200, {"url": url, "archivo": f"{analisis_id}.csv"})


def suscribir(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """POST /suscripciones {ruc, email} -> suscripción SNS filtrada por ruc (HU07)."""
    try:
        data = parse_body(event)
    except ValueError as error:
        return response(400, {"error": str(error)})
    ruc, email = str(data.get("ruc", "")).strip(), str(data.get("email", "")).strip()
    if not RUC_RE.match(ruc):
        return response(400, {"error": "El RUC debe tener 11 dígitos"})
    if not EMAIL_RE.match(email):
        return response(400, {"error": "El correo no tiene un formato válido"})
    res = client("sns").subscribe(
        TopicArn=TOPIC_ARN, Protocol="email", Endpoint=email,
        Attributes={"FilterPolicy": json.dumps({"ruc": [ruc]})}, ReturnSubscriptionArn=True)
    return response(201, {"mensaje": "Revisa tu correo y confirma la suscripción de AWS SNS",
                          "subscription_arn": res.get("SubscriptionArn")})


# =================================================================== PASOS DEL WORKFLOW
def formatear_perfil(e: Dict[str, Any]) -> str:
    def lista(v):
        return ", ".join(v) if isinstance(v, list) and v else "(no registrado)"
    exp = e.get("experiencia_facturada", 0)
    try:
        exp_txt = f"S/ {float(exp):,.0f}"
    except (TypeError, ValueError):
        exp_txt = str(exp)
    return "\n".join([
        f"- Razón social: {e.get('razon_social', '')}",
        f"- RUC: {e.get('ruc', '')}",
        f"- Rubros en los que vende: {lista(e.get('rubros'))}",
        f"- Capítulos RNP vigentes: {lista(e.get('capitulos_rnp'))}",
        f"- Experiencia facturada acumulada: {exp_txt}",
        f"- Regiones que atiende: {lista(e.get('regiones'))}",
        f"- Personal clave: {e.get('personal_clave') or '(no registrado)'}",
        f"- Certificaciones: {e.get('certificaciones') or '(ninguna)'}",
    ])


def preparar_instrucciones(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """Paso 1: plantilla + perfil de la empresa -> instrucciones/{analisis_id}.md"""
    empresa = obtener_empresa(event["ruc"])
    if not empresa:
        raise RuntimeError("Empresa no encontrada en api-empresas")
    plantilla = PLANTILLA_PATH.read_text(encoding="utf-8")
    contenido = plantilla.replace("{{perfil}}", formatear_perfil(empresa))
    key = f"instrucciones/{event['analisis_id']}.md"
    client("s3").put_object(Bucket=DATA_BUCKET, Key=key, Body=contenido.encode("utf-8"),
                            ContentType="text/markdown; charset=utf-8")
    return {"instructions_s3_link": f"s3://{DATA_BUCKET}/{key}", "razon_social": empresa.get("razon_social", "")}


def listar_convocatorias(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """Paso 2: lista los .md del lote."""
    keys = listar_md(prefijo_lote(event["ruc"], event["lote_id"]))[:MAX_CONVOCATORIAS]
    files = [{"key": k, "file_name": k.rsplit("/", 1)[-1], "content_s3_link": f"s3://{DATA_BUCKET}/{k}"} for k in keys]
    return {"total": len(files), "files": files}


def _num(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("S/", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _fecha(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def normalizar_evaluacion(raw: Dict[str, Any], fecha_ref: date) -> Dict[str, Any]:
    """Valida el JSON del agente y calcula en código lo determinista."""
    if not isinstance(raw, dict):
        raise ValueError("El agente no devolvió un objeto JSON")
    try:
        afinidad = int(round(float(raw.get("afinidad", 0))))
    except (TypeError, ValueError):
        afinidad = 0
    afinidad = max(0, min(100, afinidad))

    brechas = []
    for b in raw.get("brechas") or []:
        if isinstance(b, str):
            b = {"descripcion": b, "tipo": "subsanable"}
        if not isinstance(b, dict) or not b.get("descripcion"):
            continue
        tipo = unicodedata.normalize("NFKD", str(b.get("tipo", ""))).encode("ascii", "ignore").decode().lower()
        brechas.append({"descripcion": str(b["descripcion"]).strip(),
                        "tipo": "critica" if tipo.startswith("crit") else "subsanable"})
    criticas = [b for b in brechas if b["tipo"] == "critica"]
    subsanables = [b for b in brechas if b["tipo"] == "subsanable"]

    fecha = _fecha(raw.get("fecha_presentacion_ofertas"))
    dias = (fecha - fecha_ref).days if fecha else None

    def textos(v):
        return [str(x).strip() for x in (v or []) if str(x).strip()][:10]

    if subsanables and afinidad >= 40:
        motivo_evaluar = "Brechas subsanables: " + "; ".join(b["descripcion"] for b in subsanables[:2])
    else:
        motivo_evaluar = f"Afinidad media ({afinidad}): revisar antes de decidir"
    return {
        "entidad": str(raw.get("entidad") or "(sin entidad)").strip(),
        "objeto": str(raw.get("objeto") or "(sin objeto)").strip(),
        "monto_estimado": _num(raw.get("monto_estimado")),
        "fecha_presentacion_ofertas": fecha.isoformat() if fecha else None,
        "dias_para_cierre": dias,
        "afinidad": afinidad,
        "requisitos_cumplidos": textos(raw.get("requisitos_cumplidos")),
        "brechas": brechas,
        "checklist": textos(raw.get("checklist"))[:6],
        "justificacion": str(raw.get("justificacion") or "").strip(),
        "tiene_brecha_critica": bool(criticas),
        "tiene_brecha_subsanable": bool(subsanables),
        "motivo_brecha_critica": ("Brecha crítica: " + criticas[0]["descripcion"]) if criticas else "",
        "motivo_evaluar": motivo_evaluar,
    }


_serializer = TypeSerializer()


def marshal(item: Dict[str, Any]) -> Dict[str, Any]:
    """Convierte a formato DynamoDB JSON (para el putItem directo de Step Functions)."""
    clean = {k: v for k, v in item.items() if v is not None and v != "" and v != []}
    return {k: _serializer.serialize(v) for k, v in clean.items()}


def evaluar_convocatoria(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """Paso 4a (dentro del Map): llama al agente IA y normaliza su respuesta."""
    if not AGENTE_IA_API_URL:
        raise RuntimeError("La variable AGENTE_IA_API_URL no está configurada.")
    archivo = event["archivo"]
    status, raw = http_json("POST", AGENTE_IA_API_URL, {
        "instructions_s3_link": event["instructions_s3_link"],
        "content_s3_link": archivo["content_s3_link"],
    }, timeout=29)
    if status != 200:
        # Se lanza la excepción para que Step Functions aplique Retry (p. ej. límite de Groq)
        raise RuntimeError(f"Agente IA devolvió HTTP {status}: {json.dumps(raw, ensure_ascii=False)[:300]}")
    ev = normalizar_evaluacion(raw, date.fromisoformat(event["fecha_referencia"]))
    item = {
        "analisis_id": event["analisis_id"],
        "convocatoria_key": archivo["file_name"],
        "entidad": ev["entidad"], "objeto": ev["objeto"],
        "monto_estimado": ev["monto_estimado"],
        "fecha_presentacion_ofertas": ev["fecha_presentacion_ofertas"],
        "dias_para_cierre": ev["dias_para_cierre"],
        "afinidad": ev["afinidad"],
        "requisitos_cumplidos": ev["requisitos_cumplidos"],
        "brechas": ev["brechas"],
        "checklist": ev["checklist"],
        "justificacion": ev["justificacion"],
        "evaluado_en": now_iso(),
    }
    return {
        "afinidad": ev["afinidad"],
        "dias_para_cierre": ev["dias_para_cierre"],
        "tiene_brecha_critica": ev["tiene_brecha_critica"],
        "tiene_brecha_subsanable": ev["tiene_brecha_subsanable"],
        "motivo_brecha_critica": ev["motivo_brecha_critica"],
        "motivo_evaluar": ev["motivo_evaluar"],
        "ddb_item": marshal(item),
    }


CSV_COLUMNS = ["archivo", "entidad", "objeto", "monto_estimado", "fecha_presentacion_ofertas",
               "dias_para_cierre", "afinidad", "recomendacion", "motivo", "brechas",
               "requisitos_cumplidos", "checklist"]


def _fmt_monto(v: Any) -> str:
    try:
        return f"S/ {float(v):,.0f}"
    except (TypeError, ValueError):
        return "monto no indicado"


def generar_reporte(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """Paso 5: CSV (;) en resultados/, totales, top 3 y mensaje del correo."""
    analisis_id, ruc = event["analisis_id"], event["ruc"]
    items = _oportunidades(analisis_id)

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for i in items:
        writer.writerow([
            i.get("convocatoria_key", ""), i.get("entidad", ""), i.get("objeto", ""),
            to_json(i.get("monto_estimado", "")), i.get("fecha_presentacion_ofertas", ""),
            to_json(i.get("dias_para_cierre", "")), to_json(i.get("afinidad", "")),
            i.get("recomendacion", ""), i.get("motivo", ""),
            " | ".join(f"[{b.get('tipo')}] {b.get('descripcion')}" for b in i.get("brechas", [])),
            " | ".join(i.get("requisitos_cumplidos", [])), " | ".join(i.get("checklist", [])),
        ])
    csv_key = f"resultados/{analisis_id}.csv"
    client("s3").put_object(Bucket=DATA_BUCKET, Key=csv_key, Body=buf.getvalue().encode("utf-8-sig"),
                            ContentType="text/csv; charset=utf-8")

    tot = {r: sum(1 for i in items if i.get("recomendacion") == r) for r in ("POSTULAR", "EVALUAR", "DESCARTAR", "ERROR")}
    top = [i for i in items if i.get("recomendacion") == "POSTULAR"][:3]
    top3 = [{"convocatoria_key": i["convocatoria_key"], "entidad": i.get("entidad"), "objeto": i.get("objeto"),
             "afinidad": i.get("afinidad"), "dias_para_cierre": i.get("dias_para_cierre"),
             "monto_estimado": i.get("monto_estimado")} for i in top]

    ddb().Table(ANALISIS_TABLE).update_item(
        Key={"ruc": ruc, "analisis_id": analisis_id},
        UpdateExpression=("SET estado = :e, csv_key = :c, total_postular = :p, total_evaluar = :v, "
                          "total_descartar = :d, total_error = :x, top3 = :t, finalizado_en = :f"),
        ExpressionAttributeValues={":e": "COMPLETADO", ":c": csv_key, ":p": tot["POSTULAR"],
                                   ":v": tot["EVALUAR"], ":d": tot["DESCARTAR"], ":x": tot["ERROR"],
                                   ":t": top3, ":f": now_iso()})

    razon = event.get("prep", {}).get("razon_social", "")
    lineas = [f"Hola {razon}," if razon else "Hola,", "",
              f"Terminó tu análisis {analisis_id} de LicitaYa ({len(items)} convocatorias):",
              f"  - POSTULAR:  {tot['POSTULAR']}", f"  - EVALUAR:   {tot['EVALUAR']}",
              f"  - DESCARTAR: {tot['DESCARTAR']}"]
    if tot["ERROR"]:
        lineas.append(f"  - No evaluadas: {tot['ERROR']}")
    lineas.append("")
    if top3:
        lineas.append("Tus mejores oportunidades:")
        for n, t in enumerate(top3, 1):
            dias = to_json(t.get("dias_para_cierre"))
            lineas.append(f"  {n}. {t['entidad']} - {t['objeto']}")
            lineas.append(f"     Afinidad {to_json(t['afinidad'])}/100 | {_fmt_monto(t.get('monto_estimado'))} | cierra en {dias} días")
    else:
        lineas.append("Esta vez no encontramos oportunidades para postular.")
        evaluar = [i for i in items if i.get("recomendacion") == "EVALUAR"][:3]
        if evaluar:
            lineas.append("Revisa estas convocatorias marcadas como EVALUAR:")
            lineas += [f"  - {i.get('entidad')} - {i.get('objeto')}" for i in evaluar]
    lineas += ["", f"Ver el análisis: {WEB_URL}" if WEB_URL else "Revisa el detalle en la web de LicitaYa.",
               "", "La recomendación es orientativa y no reemplaza la lectura de las bases oficiales.",
               "-- LicitaYa, Radar de Oportunidades"]
    return {
        "csv_key": csv_key,
        "totales": tot,
        "asunto": ascii_text(f"LicitaYa: {tot['POSTULAR']} oportunidades para postular ({analisis_id})"),
        "mensaje": "\n".join(lineas),
    }
