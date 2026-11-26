# ADR 0016 — Estructura del repositorio: `app/` e `infra/` en la raíz

- **Estado:** aceptada
- **Fecha:** 2026-11-26
- **Decisores:** equipo Reinas Malka
- **Complementa:** ADR 0004 (dos repositorios)
- **Issue:** #46

## Contexto

El brief original del backend proponía una carpeta `backend/` con el código
de la API adentro. El repo nunca tuvo esa carpeta: desde el primer commit la
API vive en `app/` y la infraestructura en `infra/`, las dos en la raíz.
Nadie lo dejó escrito, así que el brief y el repo se contradicen, y un
integrante o un asistente que arranca por el brief busca rutas que no
existen.

Con el ADR 0004 el repo ya es solo de backend: el frontend está en
`frontend-malka`. Una carpeta `backend/` dentro de `backend-malka` no separa
nada.

## Decisión

La estructura real es la oficial. En la raíz del repo:

| Carpeta o archivo | Qué tiene |
|---|---|
| `app/` | La API FastAPI y los handlers de Lambda (`main.py`, `worker.py`, `migrar.py`) |
| `app/routers/` | Endpoints por módulo: permisos y conexión con el repositorio |
| `app/schemas/` | Contrato de la API (Pydantic) |
| `app/services/` | Reglas de negocio y acceso a la base, por dominio (`produccion`, `ventas`, `documentos`, `facturacion`, `exportacion`, `ia`), sin FastAPI |
| `infra/` | Terraform de toda la cuenta; `infra/scripts/` para los scripts de soporte |
| `migraciones/` | Alembic (`alembic.ini` en la raíz) |
| `tests/` | Pruebas automatizadas |
| `docs/` | `dominio.md` (el negocio), `adr/`, `diagramas/`, `evidencia/`, `cognito.md` |
| `AGENTS.md` | Reglas para agentes y personas; apunta a `docs/dominio.md` |
| `AI-DECISIONS.md` | Registro de decisiones asistidas por IA |
| `Dockerfile`, `pyproject.toml`, `requirements*.txt` | Imagen de Lambda y dependencias |

Las reglas del proyecto se buscan en este orden: `AGENTS.md`, después
`docs/dominio.md` (si contradice código o ADRs, gana el documento) y
después los ADRs.

## Consecuencias

- Las rutas del brief con `backend/` se leen sin ese prefijo:
  `backend/app/` es `app/`, `backend/infra/` es `infra/`.
- El `Dockerfile`, el CI (`.github/workflows/`) y `CODEOWNERS` ya asumen esta
  estructura: no hay que mover nada.
- Una carpeta nueva en la raíz se agrega a la tabla de este ADR y al árbol
  de *Estructura del proyecto* del README.

## Alternativas consideradas

**Mover todo a `backend/`, como decía el brief.** Descartada: rompe el
`Dockerfile`, los workflows, `CODEOWNERS`, `alembic.ini` y cada ruta citada en
issues, ADRs y `AI-DECISIONS.md`, a cambio de un prefijo que no separa nada en
un repo que ya es solo de backend.
