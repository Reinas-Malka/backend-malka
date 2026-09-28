# Malka Suite — Backend

API serverless de **Malka Suite**, el Trabajo Práctico Integrador de Cloud Computing (UTN FRLP).
Stack: **FastAPI** sobre **AWS Lambda** (imagen de contenedor), expuesta por **API Gateway HTTP**, con **PostgreSQL en RDS** dentro de una VPC privada. Toda la infraestructura está descrita en **Terraform**.

| | |
|---|---|
| API (dev) | `https://w0kwb9belc.execute-api.us-east-1.amazonaws.com` |
| Cuenta AWS | `961868442562` |
| Región | `us-east-1` |
| Prefijo de recursos | `malka-suite-dev` |
| Frontend | [frontend-malka](https://github.com/Reinas-Malka/frontend-malka) (React + Vite + TS) |

---

## Arquitectura

![Arquitectura cloud de Malka Suite](docs/arquitectura.jpg)

```mermaid
flowchart TB
    U["Usuario / navegador"] --> FE["Frontend React + Vite (Vercel)"]
    FE -->|"HTTPS + CORS"| AGW["API Gateway HTTP<br/>w0kwb9belc"]
    AGW --> L
    subgraph VPC["VPC 10.0.0.0/16 — sin NAT Gateway"]
        L["Lambda malka-suite-dev-api<br/>FastAPI + Mangum (imagen ECR)"]
        VE1["VPC Endpoint (interface)<br/>Secrets Manager"]
        VE2["VPC Endpoint (gateway)<br/>S3"]
        DB["RDS PostgreSQL 16<br/>malka-suite-dev-db (privada)"]
        L --> VE1
        L --> VE2
        L --> DB
    end
```

Decisiones de diseño y sus alternativas descartadas: ver [`docs/adr/`](docs/adr) y [`AI-DECISIONS.md`](AI-DECISIONS.md).

### Principios que sostienen el diseño

1. **Nada de la aplicación es público salvo el API Gateway.** La Lambda corre en subredes privadas y la base no tiene acceso público.
2. **Sin NAT Gateway.** El acceso a servicios de AWS se hace por VPC endpoints, que cuestan una fracción de un NAT.
3. **Sin credenciales en el repositorio.** La contraseña de la base la genera Terraform y vive en Secrets Manager.
4. **Todo por Terraform.** No se crean recursos a mano desde la consola; `terraform plan` sobre `main` debe dar `No changes`.

---

## Servicios utilizados

| Servicio | Recurso | Para qué |
|---|---|---|
| API Gateway HTTP | `w0kwb9belc` | Entrada pública, ruteo y respuesta del preflight CORS |
| Lambda | `malka-suite-dev-api` | Ejecuta FastAPI a partir de una imagen de contenedor |
| ECR | `malka-suite-dev-backend` | Registro de la imagen, con lifecycle policy |
| VPC | `vpc-0499544bc014ded68` | Aislamiento de red (10.0.0.0/16) |
| Subredes privadas | `subnet-0f13096…` (1a), `subnet-0018ca9…` (1b) | Dos AZ, requisito del subnet group de RDS |
| Security Groups | lambda `sg-051b948…`, rds `sg-0d2447c…`, endpoints `sg-0865159…` | Acceso a la base solo desde el SG de la Lambda |
| VPC Endpoints | S3 (gateway) `vpce-0b20ead…`, Secrets Manager (interface) `vpce-0b7d2f4…` | Salida a AWS sin NAT |
| RDS PostgreSQL 16 | `malka-suite-dev-db` (db.t4g.micro) | Base de datos, privada y solo TLS |
| Secrets Manager | `malka-suite-dev/db/owner` | Credenciales de la base |
| CloudWatch Logs | `/aws/lambda/malka-suite-dev-api` | Logs de la API |
| S3 | `malka-suite-tfstate-961868442562` | Estado remoto de Terraform |

### Costos

El proyecto vive dentro del **plan gratuito nuevo de AWS**, con dos consecuencias que condicionaron el diseño: la retención de backups de RDS está limitada a 1 día y no se permite el autoscaling de almacenamiento.

El único costo fijo relevante es el **VPC endpoint de interface de Secrets Manager: ~7,30 USD/mes**. Es el precio de no tener un NAT Gateway, que costaría unas cinco veces más. Lambda, API Gateway, RDS `db.t4g.micro` y ECR se mantienen dentro de los límites gratuitos para el volumen de este TP.

---

## Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/health` | Liveness. Devuelve estado, versión, entorno y timestamp. |
| GET | `/health/ready` | Readiness. Pensado para verificar dependencias (la verificación real contra la base queda pendiente). |

```bash
curl -s https://w0kwb9belc.execute-api.us-east-1.amazonaws.com/health
# {"estado":"ok","version":"latest","entorno":"dev","momento":"..."}
```

### CORS

El preflight lo responde **API Gateway**, no FastAPI. La aplicación no incluye `CORSMiddleware` a propósito: si lo hiciera, los headers vendrían duplicados. Los orígenes habilitados se controlan con la variable `origenes_permitidos` (por defecto `http://localhost:5173` y `http://127.0.0.1:5173`).

```bash
curl -i -X OPTIONS https://w0kwb9belc.execute-api.us-east-1.amazonaws.com/health \
  -H "Origin: http://localhost:5173" -H "Access-Control-Request-Method: GET"
# HTTP/2 204 + access-control-allow-origin + access-control-max-age: 3600
```

**Limitación conocida:** los previews de Vercel generan un subdominio distinto en cada deploy, así que una lista de orígenes exactos no los cubre. Se resuelve en el Checkpoint 2 con un dominio fijo.

---

## Desarrollo local

Requisitos: Python 3.12, Docker, Terraform 1.16+, AWS CLI con el perfil `malka`.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
# http://127.0.0.1:8000/health
```

El frontend solo necesita `VITE_API_BASE_URL` apuntando a la URL de la API. **No necesita credenciales de AWS**: todo lo que empieza con `VITE_` queda expuesto en el bundle del navegador.

---

## Despliegue

### 1. Imagen de contenedor

Lambda rechaza las imágenes que traen attestations, por eso los dos flags:

```bash
aws ecr get-login-password --profile malka --region us-east-1 \
  | docker login --username AWS --password-stdin 961868442562.dkr.ecr.us-east-1.amazonaws.com

docker build --provenance=false --sbom=false -t malka-suite-dev-backend:$(git rev-parse --short HEAD) .
docker tag  malka-suite-dev-backend:$(git rev-parse --short HEAD) \
  961868442562.dkr.ecr.us-east-1.amazonaws.com/malka-suite-dev-backend:$(git rev-parse --short HEAD)
docker push 961868442562.dkr.ecr.us-east-1.amazonaws.com/malka-suite-dev-backend:$(git rev-parse --short HEAD)
```

### 2. Infraestructura

El backend de estado se configura con `backend.hcl`, que **no está versionado** (ver `infra/backend.hcl.example`).

```bash
export AWS_PROFILE=malka
cd infra
terraform init -backend-config=backend.hcl
terraform plan     # sobre main debe decir: No changes
terraform apply
```

**Reglas de oro del equipo:**

- Aplicar **solo desde una rama que contenga todos los `.tf` ya aplicados**. Si se aplica desde una rama incompleta, el plan propone destruir recursos vivos.
- Si un plan muestra algo en `destroy` que no se pidió explícitamente, **frenar** y revisar.
- Nunca imprimir el contenido del secreto de la base en la terminal.

---

## Convenciones

- **Ramas:** `feat/…`, `fix/…`, `docs/…`, `ci/…`, siempre partiendo de `main`.
- **Commits:** Conventional Commits en español (`feat(api): …`, `docs: …`).
- **Pull requests:** uno por issue, con descripción de qué hace y cómo se verificó. Requieren **1 aprobación** y se integran con **Create a merge commit** (nunca squash: rompe las cadenas de ramas y obliga a re-sincronizar los PRs siguientes).
- **`main` protegida** por el ruleset `proteger-main`, que además invalida las aprobaciones cuando se pushean commits nuevos.
- **Tablero:** [Malka Suite — Checkpoint 1](https://github.com/orgs/Reinas-Malka/projects/1), con WIP límite 3 en *In Progress* y en *In Review*.

---

## Estado del Checkpoint 1

| Issue | Tema | Estado |
|---|---|---|
| #2 | Cuenta AWS, usuarios y perfil CLI | Hecho |
| #4 | Red base: VPC, subredes, SGs, VPC endpoints | Hecho (PR #13) |
| #7 | Endpoints de salud | Hecho (PR #14) |
| #8 | ECR, Lambda y API Gateway | Hecho (PR #14) |
| #5 | RDS PostgreSQL + Secrets Manager | Hecho (PR #16) |
| — | CORS para el frontend | Hecho (PR #15) |
| #6 | S3, SQS y Cognito | Pendiente (Checkpoint 2) |
| #9 | CI de lint y tests | Pendiente |
| #10 | CI de deploy con OIDC | Pendiente |

### Pendientes conocidos

- `/health/ready` todavía no consulta la base: responde OK sin verificar dependencias.
- `APP_VERSION` está fijo en `"latest"`; debe pasar a ser el SHA del commit cuando exista el pipeline (#9, #10).
- Sin CI: hoy el build de la imagen y el `terraform apply` se hacen desde la máquina local.

---

## Repositorios

| Repo | Contenido |
|---|---|
| [backend-malka](https://github.com/Reinas-Malka/backend-malka) | API FastAPI, infraestructura Terraform, documentación |
| [frontend-malka](https://github.com/Reinas-Malka/frontend-malka) | Aplicación React + Vite + TypeScript |

El motivo de tener dos repositorios está documentado en [`docs/adr/0004-dos-repositorios.md`](docs/adr/0004-dos-repositorios.md).
