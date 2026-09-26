# ADR 0004 — Dos repositorios separados para backend y frontend

- **Estado:** aceptada
- **Fecha:** 2026-09-19
- **Decisores:** equipo Reinas Malka

## Contexto

El equipo son cinco personas con dos frentes de trabajo bien distintos: una API en Python con infraestructura en Terraform, y una SPA en React con TypeScript. Los ciclos de despliegue también son distintos: el frontend va a Vercel en cada push, el backend implica construir una imagen, subirla a ECR y aplicar Terraform.

Había que decidir entre un monorepo y dos repositorios antes de empezar a repartir tareas.

## Decisión

Dos repositorios públicos en la organización `Reinas-Malka`:

- **`backend-malka`**: API FastAPI, infraestructura Terraform y documentación de arquitectura.
- **`frontend-malka`**: aplicación React + Vite + TypeScript.

El contrato entre ambos es la **URL pública de la API**, que el frontend consume por `VITE_API_BASE_URL`. El frontend **no recibe credenciales de AWS**: todo lo que empieza con `VITE_` termina embebido en el bundle y es público.

La planificación se unifica en un único **GitHub Project a nivel de organización**, que toma issues de los dos repos, con límite WIP de 3 en *In Progress* y en *In Review*.

## Consecuencias

**A favor**

- Historial y issues limpios: quien trabaja en el frontend no navega commits de Terraform.
- Permisos y protecciones de rama configurables por repo según el riesgo (el backend toca infraestructura real y con costo).
- Despliegues independientes, sin builds innecesarios.
- Repos chicos y fáciles de clonar y explicar en la defensa.

**En contra**

- **Un cambio que toca las dos partes necesita dos PRs** y coordinación para el orden de merge.
- El tablero unificado es indispensable; sin él la planificación se fragmenta. Ya nos pasó: el issue de `AI-DECISIONS` se creó en el repo equivocado y hay que transferirlo.
- No hay forma automática de versionar juntos el contrato de la API y su consumidor.

## Alternativas consideradas

**Monorepo.** Un solo lugar, un solo tablero, cambios atomicos entre API y UI. Descartada porque requiere herramientas de build con filtros por carpeta para no desplegar todo en cada push, y esa complejidad no se justifica en un TP de cinco personas.

**Tres repositorios (backend, frontend, infraestructura).** Es lo más parecido a la práctica de muchas empresas, y permitiría darle a la infraestructura permisos más estrictos. Descartada porque separar el `.tf` del código que describe obliga a coordinar tres PRs para un cambio como agregar una variable de entorno a la Lambda.
