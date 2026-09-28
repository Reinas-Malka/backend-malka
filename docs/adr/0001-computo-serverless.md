# ADR 0001 — Cómputo serverless con Lambda e imagen de contenedor

- **Estado:** aceptada
- **Fecha:** 2026-09-20
- **Decisores:** equipo Reinas Malka
- **Issues:** #8

## Contexto

Malka Suite es un TP académico con tráfico casi nulo entre demostraciones y un presupuesto que debe mantenerse dentro del plan gratuito de AWS. El equipo tiene experiencia previa en Python y ninguna en administración de servidores. Además, el cursado impone tres hitos con fechas fijas, así que el tiempo de puesta en marcha pesa tanto como la calidad técnica del resultado.

Las opciones evaluadas fueron Lambda, ECS Fargate y una EC2 con Docker Compose.

## Decisión

Usamos **AWS Lambda con imagen de contenedor** (no ZIP), con **FastAPI** adaptado por **Mangum**, y **API Gateway HTTP** como entrada pública.

La imagen se publica en ECR con `--provenance=false --sbom=false`: sin esos flags Docker agrega attestations que generan un manifiesto multi-arquitectura, y Lambda lo rechaza.

## Consecuencias

**A favor**

- Costo cero cuando no hay tráfico: no se paga capacidad en reposo.
- No hay sistema operativo que parchear ni escalado que administrar.
- La imagen de contenedor permite dependencias nativas sin pelear con los límites del paquete ZIP, y el mismo `Dockerfile` sirve para correr local.

**En contra**

- **Cold start**, agravado por estar en VPC. Aceptable para este caso de uso.
- Estado por invocación: obliga a que la API sea stateless y a manejar el pool de conexiones a PostgreSQL con cuidado (una Lambda por conexión puede agotar el límite de la instancia).
- Depender de API Gateway para el CORS nos ataja de un problema pero acopla la configuración de la API a Terraform en vez del código.

## Alternativas consideradas

**ECS Fargate.** Más parecido a un despliegue productivo real y sin cold start, pero requiere ALB, que cuesta ~16 USD/mes solo por existir. Descartada por costo.

**EC2 con Docker Compose.** La más barata en t4g.micro y la más simple de entender, pero nos volvía responsables del SO, los backups y el TLS. Descartada porque el objetivo de la materia es usar servicios administrados, no administrar servidores.

**Go en lugar de Python.** Se evaluó por el menor cold start y por ser el lenguaje que mejor muestra el ahorro en Lambda, pero ninguno del equipo lo conocía. El costo de aprendizaje no se justificaba frente a un problema que Python resuelve igual de bien.
