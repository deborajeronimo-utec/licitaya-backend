# workflow-licitaya (LicitaYa)

Workflow Agéntico de **Nivel 4** en **AWS Step Functions**, junto con la API que usa la web. Cubre las historias **HU02–HU08**.

## Flujo (`licitaya-evaluador-oportunidades-{stage}`, tipo Standard)

```
PrepararInstrucciones (Lambda) → ListarConvocatorias (Lambda) → ¿HayConvocatorias? (Choice)
  → EvaluarConvocatorias (Map, MaxConcurrency 2)
       EvaluarConvocatoria (Lambda → api-agente-ia-generica → Groq)   [Retry 3, Catch → RegistrarFallo]
       DecidirRecomendacion (Choice)
           ├─ días < 3 ........................ DescartarPorPlazo
           ├─ brecha crítica .................. DescartarPorBrecha
           ├─ afinidad < 40 ................... DescartarPorAfinidad
           ├─ sin fecha de cierre ............. EvaluarSinFecha
           ├─ afinidad ≥ 70 y sin brechas ..... Postular
           └─ (por defecto) ................... Evaluar
       GuardarOportunidad (DynamoDB putItem directo)
  → GenerarReporte (Lambda: CSV ; en S3/resultados) → Notificar (SNS publish directo) → Fin
  Catch global → MarcarError (DynamoDB updateItem) → Fallido
```

**Principio de diseño:** la IA lee y extrae; el workflow decide; lo determinista (días al cierre, banderas de brechas) lo calcula el código.

## Endpoints (HTTP API)

| Método | Ruta | Descripción |
|---|---|---|
| POST | `/lotes` | `{ruc, archivos[]}` → `lote_id` y URLs prefirmadas `PUT` (solo `.md`, máximo 20) |
| POST | `/analisis` | `{ruc, lote_id, fecha_referencia?}` → 202 e inicio asíncrono del workflow |
| GET | `/analisis?ruc=` | Historial por RUC |
| GET | `/analisis/{analisis_id}` | Estado, totales, top 3 y avance (`evaluadas`) |
| GET | `/analisis/{analisis_id}/oportunidades?recomendacion=` | Resultados ordenados por afinidad |
| GET | `/analisis/{analisis_id}/csv` | URL prefirmada del CSV (409 si está en proceso) |
| POST | `/suscripciones` | `{ruc, email}` → suscripción SNS con `FilterPolicy` por RUC |

## Recursos

**S3 `licitaya-datos-{cuenta}-{stage}`** (con CORS para la carga desde la web):

| Prefijo | Contenido |
|---|---|
| `convocatorias/{ruc}/{lote_id}/` | Convocatorias `.md` subidas |
| `instrucciones/{analisis_id}.md` | Instrucciones del agente para ese análisis |
| `resultados/{analisis_id}.csv` | Reporte comparativo |

**DynamoDB:**
- `licitaya-analisis-{stage}`: PK `ruc`, SK `analisis_id`; GSI `por-id`.
- `licitaya-oportunidades-{stage}`: PK `analisis_id`, SK `convocatoria_key`.

**SNS:** `licitaya-notificaciones-{stage}`.

## Variables de entorno para el despliegue

```bash
export EMPRESAS_API_URL="https://<id>.execute-api.us-east-1.amazonaws.com"
export AGENTE_IA_API_URL="https://<id>.execute-api.us-east-1.amazonaws.com/agente-ia-generica"
export WEB_URL="http://..."      # opcional: enlace que va en el correo
serverless deploy
```

## Pruebas locales

```bash
python -m pytest -q tests                 # 8 pruebas con moto + intérprete ASL
python tests/servidor_local.py            # API local en :8787 para probar la web sin AWS
```

Creado con asistencia de Claude (Anthropic), modelo Claude Opus 5.5, noviembre de 2026.
