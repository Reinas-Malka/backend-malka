# Decisiones asistidas por IA

Registro de los puntos del proyecto donde usamos IA, qué generó y qué corregimos.

---

## 1. Definición del stack y la arquitectura cloud

**Problema abordado**
Elegir lenguaje de backend y servicios de AWS para una solución SaaS multi-tenant
de gestión de cría de reinas, con presupuesto acotado y plazo de un cuatrimestre.

**Prompt y herramienta**
Asistente de IA conversacional. Se describió el dominio, el equipo y la restricción
de costos, y se pidió una recomendación de stack con alternativas justificadas.

**Código o arquitectura generada**
Propuesta serverless: API en FastAPI sobre AWS Lambda detrás de API Gateway,
PostgreSQL en RDS con Row Level Security por tenant, S3 para documentos,
SQS para el trabajo asíncrono y Bedrock para la redacción de borradores.

**Validación y corrección humana**
Se descartó Go, que era la idea inicial del equipo, por curva de aprendizaje frente
al plazo disponible. Se verificó el costo mensual estimado (17 a 20 USD) contra la
calculadora de AWS y se reemplazó NAT Gateway por VPC endpoints, que baja unos
64 USD por mes. Pendiente de validar en la implementación.
