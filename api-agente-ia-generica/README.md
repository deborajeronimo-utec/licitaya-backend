# api-agente-ia-generica

API generica de Agente de IA 100% serverless en AWS para evaluar contenido markdown usando instrucciones markdown almacenadas en S3 y procesadas con Groq, devolviendo salida JSON.

## Funcionalidades

- Exponer un endpoint `POST /agente-ia-generica` por API Gateway (HTTP API).
- Recibir dos enlaces a archivos markdown en S3:
  - `instructions_s3_link`
  - `content_s3_link`
- Leer markdown desde S3 en formato:
  - `s3://bucket/key.md`
- Construir un prompt unificado con instrucciones + contenido.
- Invocar Groq con modelo `openai/gpt-oss-120b`.
- Retornar la salida de Groq en `application/json`.

## Arquitectura

- AWS API Gateway HTTP API
- AWS Lambda (Python 3.14)
- AWS S3 (fuente de archivos markdown)
- Groq API (`/openai/v1/chat/completions`)
- IaC con Serverless Framework v4
- Rol IAM existente: `LabRole`

## Estructura del proyecto

- `serverless.yml`: definicion IaC (servicio, Lambda, API Gateway, rol IAM)
- `handler.py`: logica de la Lambda
- `postman/agente-ia-generica.postman_collection.json`: coleccion para pruebas
- `prompt.md`: prompt de referencia original

## Contrato del API

### Endpoint

- Metodo: `POST`
- Ruta: `/agente-ia-generica`
- Content-Type: `application/json`

### Body de entrada

```json
{
  "instructions_s3_link": "s3://mi-bucket/instrucciones.md",
  "content_s3_link": "s3://mi-bucket/contenido.md"
}
```

### Respuesta exitosa

- HTTP `200`
- Content-Type: `application/json; charset=utf-8`
- Body: JSON devuelto por Groq

### Respuestas de error

- HTTP `400`: entrada invalida o lectura fallida de archivos
- HTTP `502`: error al invocar Groq

## Variables de entorno

- `GROQ_API_KEY`: API Key de Groq (obligatoria)
- `GROQ_MODEL`: modelo Groq (por defecto `llama-3.3-70b-versatile`)

## Despliegue automatico (Serverless Framework v4)

1. Autentica Serverless Framework (requerido por v4):

```bash
serverless login
```

2. Configura credenciales AWS (.aws/credentials)

3. Define la API Key de Groq en tu shell.

```bash
export GROQ_API_KEY="tu_api_key_de_groq"
```

4. Despliega automaticamente:

```bash
serverless deploy
```

5. (Opcional) Eliminar recursos:

```bash
serverless remove
```

## Prueba rapida con curl

```bash
curl -X POST "https://<api-id>.execute-api.us-east-1.amazonaws.com/agente-ia-generica" \
  -H "Content-Type: application/json" \
  -d '{
    "instructions_s3_link": "s3://mi-bucket/instrucciones.md",
    "content_s3_link": "s3://mi-bucket/contenido.md"
  }'
```

## Coleccion Postman

Importa el archivo `postman/agente-ia-generica.postman_collection.json`.

Configura la variable `baseUrl` con el endpoint base, por ejemplo:

- `https://<api-id>.execute-api.us-east-1.amazonaws.com`

## Metadatos de creacion

- Creado con GitHub Copilot
- Modelo LLM utilizado: GPT-5.3-Codex
- Fecha de creacion: 2026-03-05

## Referencia del prompt utilizado

```md
## Rol / Persona
Actúa como un experto en crear Agentes de IA con servicios de AWS

## Contexto
Se necesita tener un api genérica Agente de IA que sea completamente serverless

## Tarea / Objetivo 
Crea un api genérica Agente de IA con estas funcionalidades:
- Entrada al api:
  - Enlace a archivo en bucket S3 en formato markdown con las instrucciones
  - Enlace a archivo en bucket S3 en formato markdown con el contenido a evaluar con las instrucciones anteriores
- Procesamiento:
  - Generar un prompt con las instrucciones y contenido y enviarlo al Api IA de Groq
- Salida del api:
  - Mostrar como salida lo que responda el Api IA en formato json    

## Requisitos de la respuesta
- Para la IaC (Infraestructura como código) utiliza el framework serverless versión 4 con rol de IAM LabRole existente.
- Para el api genérica Agente de IA (agente-ia-generica) utiliza:
  - Servicios de AWS (Api Gateway y Lambda)
  - Lenguaje de programación python versión 3.14 con librería urllib en vez de requests
  - Api IA de [Groq](https://console.groq.com/docs/overview) con el modelo "llama-3.3-70b-versatile"
- Genera un readme.md con las funcionalidades que contiene y las instrucciones para hacer el despliegue automático. Adicionalmente indica explícitamente que ha sido creado con GitHub Copilot, el modelo LLM usado, la fecha de creación e incluye como referencia todo el texto del prompt utilizado.
- Genera una colección postman para poder probar el api creada.

## Elementos adicionales
-
```


