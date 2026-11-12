# LicitaYa – Backend (Workflow Agéntico Nivel 4 · AWS Serverless)

Proyecto Final · Cloud Computing · Maestría CDIA V5 – UTEC Posgrado, 2026.
Web del proyecto: repositorio `licitaya-web`.

## Contenido

| Carpeta | Qué es |
|---|---|
| `api-agente-ia-generica/` | API del agente IA (Groq `openai/gpt-oss-120b`, sin memoria). Reutilizada del taller de la Sesión 12 |
| `api-empresas/` | API REST con base de datos propia (DynamoDB `licitaya-empresas`) |
| `workflow-licitaya/` | API + AWS Step Functions + Lambdas + DynamoDB + S3 + SNS. Decide POSTULAR / EVALUAR / DESCARTAR |
| `pruebas/` | `empresa.json`, 10 convocatorias ficticias (`.md`) y la colección Postman |
| `scripts/cargar_convocatorias.py` | Sube las convocatorias de prueba y lanza el análisis |

## Despliegue (MV de desarrollo, región us-east-1, rol LabRole)

```bash
# 1) Agente IA
cd api-agente-ia-generica && export GROQ_API_KEY="..." && serverless deploy
export AGENTE_IA_API_URL="https://<id>.execute-api.us-east-1.amazonaws.com/agente-ia-generica"

# 2) API de empresas
cd ../api-empresas && serverless deploy && serverless info --verbose
export EMPRESAS_API_URL="https://<id>.execute-api.us-east-1.amazonaws.com"

# 3) Workflow
cd ../workflow-licitaya && serverless deploy && serverless info --verbose
export WORKFLOW_API="https://<id>.execute-api.us-east-1.amazonaws.com"
```

## Prueba rápida

```bash
curl -X POST "$EMPRESAS_API_URL/empresas" -H "Content-Type: application/json" -d @pruebas/empresa.json
python3 scripts/cargar_convocatorias.py --ruc 20601234567 --analizar --fecha 2026-11-18
```

Resultado esperado con los datos de prueba: 3 POSTULAR, 3 EVALUAR y 4 DESCARTAR.

## Pruebas locales (sin AWS)

```bash
cd workflow-licitaya && pip install pytest "moto[s3,dynamodb,sns,sqs,stepfunctions]" pyyaml boto3
python -m pytest -q tests
```

Creado con asistencia de Claude (Anthropic), modelo Claude Opus 5.5, noviembre de 2026.
