import base64
import json
import os
from typing import Any, Dict, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

import boto3
from botocore.exceptions import BotoCoreError, ClientError

GROQ_CHAT_COMPLETIONS_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"

s3_client = boto3.client("s3")


def json_response(status_code: int, payload: Any) -> Dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": json.dumps(payload, ensure_ascii=False),
    }


def parse_request_body(event: Dict[str, Any]) -> Dict[str, Any]:
    body = event.get("body")
    if body is None or body == "":
        return {}

    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")

    if isinstance(body, dict):
        return body

    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise ValueError("El cuerpo de la solicitud no es un JSON valido.") from error


def extract_links(payload: Dict[str, Any]) -> Tuple[str, str]:
    instructions_link = (
        payload.get("instructions_s3_link")
        or payload.get("instrucciones_s3_link")
        or payload.get("instructionsLink")
    )
    content_link = (
        payload.get("content_s3_link")
        or payload.get("contenido_s3_link")
        or payload.get("contentLink")
    )

    if not instructions_link or not content_link:
        raise ValueError(
            "Debes enviar instructions_s3_link y content_s3_link en el cuerpo JSON."
        )

    return instructions_link, content_link


def read_s3_object(bucket: str, key: str) -> str:
    response = s3_client.get_object(Bucket=bucket, Key=key)
    return response["Body"].read().decode("utf-8")


def parse_s3_https_url(link: str) -> Tuple[str, str]:
    parsed = urlparse(link)
    host = parsed.netloc.lower()
    path = parsed.path.lstrip("/")

    if not host or not path:
        raise ValueError("La URL de S3 no contiene bucket y key.")

    if host == "s3.amazonaws.com" or (
        host.startswith("s3.") and host.endswith(".amazonaws.com")
    ):
        parts = path.split("/", 1)
        if len(parts) != 2:
            raise ValueError("La URL path-style de S3 es invalida.")
        bucket, key = parts
        return unquote(bucket), unquote(key)

    if host.endswith(".s3.amazonaws.com"):
        bucket = host[: -len(".s3.amazonaws.com")]
        return unquote(bucket), unquote(path)

    if ".s3." in host and host.endswith(".amazonaws.com"):
        bucket = host.split(".s3.", 1)[0]
        return unquote(bucket), unquote(path)

    raise ValueError("La URL HTTP/HTTPS no es una URL de S3 reconocida.")


def read_http_url(link: str) -> str:
    request = Request(link, headers={"User-Agent": "agente-ia-generica/1.0"}, method="GET")
    with urlopen(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def read_markdown_from_link(link: str) -> str:
    parsed = urlparse(link)

    if parsed.scheme == "s3":
        bucket = parsed.netloc
        key = unquote(parsed.path.lstrip("/"))
        if not bucket or not key:
            raise ValueError(f"URI de S3 invalida: {link}")
        return read_s3_object(bucket, key)

    if parsed.scheme in {"http", "https"}:
        try:
            bucket, key = parse_s3_https_url(link)
            return read_s3_object(bucket, key)
        except ValueError:
            return read_http_url(link)
        except (ClientError, BotoCoreError):
            # Fallback for pre-signed/public URLs when IAM read fails.
            return read_http_url(link)

    raise ValueError(
        "El enlace debe ser s3://<bucket>/<key> o una URL http/https accesible."
    )


def build_prompt(instructions_markdown: str, content_markdown: str) -> str:
    return (
        "Evalua el contenido segun las instrucciones dadas."
        " La respuesta final debe ser JSON valido.\n\n"
        "## Instrucciones\n"
        f"{instructions_markdown.strip()}\n\n"
        "## Contenido a evaluar\n"
        f"{content_markdown.strip()}\n"
    )


def parse_model_json_content(content: str) -> Any:
    candidate = content.strip()

    # Accept JSON wrapped in a fenced block to be robust with model outputs.
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 3 and lines[0].startswith("```") and lines[-1].strip() == "```":
            candidate = "\n".join(lines[1:-1]).strip()

    try:
        return json.loads(candidate)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "La respuesta de Groq no es JSON valido. "
            "Verifica que las instrucciones fuercen salida estrictamente en JSON."
        ) from error


def call_groq(prompt_text: str) -> Any:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("La variable de entorno GROQ_API_KEY no esta configurada.")

    model = os.getenv("GROQ_MODEL", DEFAULT_GROQ_MODEL)
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Eres un asistente de evaluacion de contenido. "
                    "Debes responder exclusivamente con JSON valido, "
                    "sin texto adicional ni markdown."
                ),
            },
            {"role": "user", "content": prompt_text},
        ],
        "temperature": 0.2,
    }

    data = json.dumps(payload).encode("utf-8")
    request = Request(
        GROQ_CHAT_COMPLETIONS_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            )
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=60) as response:
            response_json = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        error_body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Groq devolvio HTTP {error.code}: {error_body}") from error
    except URLError as error:
        raise RuntimeError(f"No se pudo conectar con Groq: {error.reason}") from error

    choices = response_json.get("choices", [])
    if not choices:
        raise RuntimeError("La respuesta de Groq no contiene 'choices'.")

    content = choices[0].get("message", {}).get("content")
    if not content:
        raise RuntimeError("La respuesta de Groq no contiene contenido JSON.")

    return parse_model_json_content(content)


def lambda_handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    try:
        payload = parse_request_body(event)
        instructions_link, content_link = extract_links(payload)
    except ValueError as error:
        return json_response(400, {"error": str(error)})

    try:
        instructions_markdown = read_markdown_from_link(instructions_link)
        content_markdown = read_markdown_from_link(content_link)
    except (ValueError, ClientError, BotoCoreError, HTTPError, URLError) as error:
        return json_response(
            400,
            {
                "error": "No fue posible leer los markdown desde los enlaces enviados.",
                "detail": str(error),
            },
        )

    prompt_text = build_prompt(instructions_markdown, content_markdown)

    try:
        groq_json = call_groq(prompt_text)
    except RuntimeError as error:
        return json_response(
            502,
            {"error": "Fallo al invocar el API de Groq.", "detail": str(error)},
        )

    return json_response(200, groq_json)
