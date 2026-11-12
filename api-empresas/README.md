# api-empresas (LicitaYa)

API REST serverless con **base de datos propia** (DynamoDB `licitaya-empresas-{stage}`) para el perfil de la empresa. Cubre la historia **HU01**.

## Endpoints (HTTP API)

| Método | Ruta | Descripción | Respuestas |
|---|---|---|---|
| POST | `/empresas` | Registra el perfil | 201 · 400 «El RUC debe tener 11 dígitos» · 409 «Ya existe una empresa con ese RUC» |
| GET | `/empresas/{ruc}` | Obtiene el perfil | 200 · 404 |
| PUT | `/empresas/{ruc}` | Actualiza el perfil | 200 · 400 · 404 |

Ejemplo de body: ver `../../datos-prueba/empresa.json`.

## Tabla `licitaya-empresas-{stage}`

- **PK** `ruc` (S).
- Atributos: `razon_social`, `email`, `rubros` (L), `capitulos_rnp` (L), `experiencia_facturada` (N), `regiones` (L), `personal_clave`, `certificaciones`, `creado_en` y `actualizado_en`.
- Los duplicados se evitan con `ConditionExpression: attribute_not_exists(ruc)`.

## Despliegue

```bash
serverless deploy            # stage dev, us-east-1, rol LabRole
```

La salida `EmpresasApiUrl` se usa como `EMPRESAS_API_URL` (en el workflow) y como `VITE_EMPRESAS_API` (en la web).

Creado con asistencia de Claude (Anthropic), modelo Claude Opus 5.5, noviembre de 2026.
