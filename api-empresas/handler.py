"""api-empresas – LicitaYa
CRUD del perfil de la empresa (HU01). Base de datos propia: DynamoDB `licitaya-empresas`.

Endpoints (HTTP API):
  POST /empresas          -> crear perfil
  GET  /empresas/{ruc}    -> obtener perfil
  PUT  /empresas/{ruc}    -> actualizar perfil
"""
import base64
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Tuple

import boto3
from botocore.exceptions import ClientError

TABLE_NAME = os.getenv("EMPRESAS_TABLE", "licitaya-empresas-dev")
RUC_RE = re.compile(r"^\d{11}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_dynamodb = None


def table():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb.Table(TABLE_NAME)


# ---------------------------------------------------------------- utilidades
def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def to_json(value: Any) -> Any:
    """Convierte Decimal (DynamoDB) a int/float para serializar."""
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


def str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [v for v in value.split(",")]
    if not isinstance(value, list):
        raise ValueError("Se esperaba una lista de textos.")
    return [str(v).strip() for v in value if str(v).strip()]


def validate_profile(data: Dict[str, Any], ruc: str) -> Tuple[Dict[str, Any], List[str]]:
    """Valida y normaliza el perfil. Devuelve (item, errores)."""
    errors: List[str] = []
    if not RUC_RE.match(ruc or ""):
        errors.append("El RUC debe tener 11 dígitos")
    razon = str(data.get("razon_social", "")).strip()
    if not razon:
        errors.append("La razón social es obligatoria")
    try:
        rubros = str_list(data.get("rubros"))
        capitulos = str_list(data.get("capitulos_rnp"))
        regiones = str_list(data.get("regiones"))
    except ValueError as error:
        errors.append(str(error))
        rubros, capitulos, regiones = [], [], []
    if not rubros:
        errors.append("Registra al menos un rubro")
    try:
        experiencia = Decimal(str(data.get("experiencia_facturada", "")).replace(",", ""))
        if experiencia < 0:
            raise ValueError
    except Exception:  # noqa: BLE001 - cualquier valor no numérico
        errors.append("La experiencia facturada debe ser un número mayor o igual a 0")
        experiencia = Decimal(0)
    email = str(data.get("email", "")).strip()
    if email and not EMAIL_RE.match(email):
        errors.append("El correo no tiene un formato válido")

    item = {
        "ruc": ruc,
        "razon_social": razon,
        "email": email,
        "rubros": rubros,
        "capitulos_rnp": capitulos,
        "experiencia_facturada": experiencia,
        "regiones": regiones,
        "personal_clave": str(data.get("personal_clave", "")).strip(),
        "certificaciones": str(data.get("certificaciones", "")).strip(),
    }
    return item, errors


# ---------------------------------------------------------------- handlers
def crear_empresa(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    try:
        data = parse_body(event)
    except ValueError as error:
        return response(400, {"error": str(error)})
    ruc = str(data.get("ruc", "")).strip()
    item, errors = validate_profile(data, ruc)
    if errors:
        return response(400, {"error": errors[0], "errores": errors})
    item["creado_en"] = item["actualizado_en"] = now_iso()
    try:
        table().put_item(Item=item, ConditionExpression="attribute_not_exists(ruc)")
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return response(409, {"error": "Ya existe una empresa con ese RUC"})
        raise
    return response(201, {"mensaje": "Perfil guardado correctamente", "empresa": item})


def obtener_empresa(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    ruc = (event.get("pathParameters") or {}).get("ruc", "")
    if not RUC_RE.match(ruc):
        return response(400, {"error": "El RUC debe tener 11 dígitos"})
    found = table().get_item(Key={"ruc": ruc}).get("Item")
    if not found:
        return response(404, {"error": "Empresa no encontrada"})
    return response(200, found)


def actualizar_empresa(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    ruc = (event.get("pathParameters") or {}).get("ruc", "")
    try:
        data = parse_body(event)
    except ValueError as error:
        return response(400, {"error": str(error)})
    item, errors = validate_profile(data, ruc)
    if errors:
        return response(400, {"error": errors[0], "errores": errors})
    current = table().get_item(Key={"ruc": ruc}).get("Item")
    if not current:
        return response(404, {"error": "Empresa no encontrada"})
    item["creado_en"] = current.get("creado_en", now_iso())
    item["actualizado_en"] = now_iso()
    table().put_item(Item=item)
    return response(200, {"mensaje": "Perfil guardado correctamente", "empresa": item})
