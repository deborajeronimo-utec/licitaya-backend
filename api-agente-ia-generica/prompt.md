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
  - Api IA de [Groq](https://console.groq.com/docs/overview) con el modelo "openai/gpt-oss-120b"
- Genera un readme.md con las funcionalidades que contiene y las instrucciones para hacer el despliegue automático. Adicionalmente indica explícitamente que ha sido creado con GitHub Copilot, el modelo LLM usado, la fecha de creación e incluye como referencia todo el texto del prompt utilizado.
- Genera una colección postman para poder probar el api creada.

## Elementos adicionales
-
