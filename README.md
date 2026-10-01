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

## De qué va el proyecto


Malka Suite es una plataforma SaaS para criaderos de abejas reinas. El caso piloto es la Cabaña Apícola Malka, en la zona rural de La Plata.

La cría de reinas es una producción encadenada y a contrarreloj: cada tanda pasa por el traslarve, las iniciadoras, las continuadoras y el parque de fecundación o el banco de reinas, con fechas que no se pueden correr. Hoy ese ciclo se registra en pizarras y cuadernos, las reservas se comprometen sin saber cuánto va a poder entregar la producción y la facturación se arma a mano.

Malka Suite unifica en un solo sistema:

- **Producción y trazabilidad**: el ciclo de cada tanda etapa por etapa, con porcentajes de aceptación, material consumido y ocupación de los bancos.
- **Ventas nacionales y de exportación**: pedidos y reservas que se descuentan de la disponibilidad real de la producción.
- **Agente de facturación con IA**: redacta borradores de facturas, remitos y documentación aduanera que una persona revisa y aprueba antes de emitir.

Es multi-tenant: cada criadero es un inquilino con sus datos aislados. Los usuarios son el dueño o administrador, el capataz de criadores y el equipo de administración y ventas.

> En el Checkpoint 1 el backend quedó con la infraestructura base y los endpoints de salud; el Checkpoint 2 agrega CI/CD, migraciones, mensajería y los módulos de negocio (ver el [tablero del CP2](https://github.com/orgs/Reinas-Malka/projects/2)).

---

## Arquitectura

![Arquitectura de Malka Suite en AWS](docs/diagramas/arquitectura.png)

Diagrama regenerable con `python docs/diagramas/generar_arquitectura.py` (lib `diagrams`, iconos oficiales de AWS). El deploy paso a paso está en `docs/diagramas/pipeline.png`. Resumen del flujo:

```mermaid
flowchart TB
    U["Navegador"] -->|"HTTPS"| AGW["API Gateway HTTP · CORS"]
    AGW --> L
    U -->|"binario por URL prefirmada"| S3D["S3 documentos"]
    subgraph VPC["VPC 10.20.0.0/16 — sin NAT · endpoints: S3, Secrets, SQS, bedrock"]
        L["Lambda api · FastAPI"]
        W["Lambda worker"]
        M["Lambda migraciones"]
        L -->|"encola"| SQS["SQS documentos/ingesta"]
        SQS -->|"3 fallos"| DLQ["DLQ + alarma"]
        SQS --> W
        W --> DB[("RDS PostgreSQL 16 · RLS")]
        W -->|"converse"| BR["Bedrock Haiku 4.5"]
    end
    GH["GitHub Actions · OIDC"] --> ECR["ECR"] -.-> L & W & M
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
| Lambda | `malka-suite-dev-worker` | Consume las colas SQS (borrador con IA e ingesta) |
| Lambda | `malka-suite-dev-migraciones` | Aplica las migraciones de Alembic dentro de la VPC |
| ECR | `malka-suite-dev-backend` | Registro de la imagen (única para las tres Lambdas), con lifecycle policy |
| VPC | `vpc-0499544bc014ded68` | Aislamiento de red (10.20.0.0/16) |
| Subredes privadas | `subnet-0f13096…` (1a), `subnet-0018ca9…` (1b) | Dos AZ, requisito del subnet group de RDS |
| Security Groups | lambda `sg-051b948…`, rds `sg-0d2447c…`, endpoints `sg-0865159…` | Acceso a la base solo desde el SG de la Lambda |
| VPC Endpoints | S3 (gateway), Secrets Manager, SQS y bedrock-runtime (interface, 1 AZ) | Salida a AWS sin NAT |
| SQS | colas `documentos` e `ingesta` + sus DLQs | Mensajería asincrónica; 3 fallos → DLQ y alarma |
| S3 | `malka-suite-dev-documentos-…` | Documentos e ingesta por URLs prefirmadas (versionado, SSE) |
| RDS PostgreSQL 16 | `malka-suite-dev-db` (db.t4g.micro) | Base de datos, privada y solo TLS |
| Secrets Manager | `malka-suite-dev/db/owner` | Credenciales de la base |
| IAM + OIDC | proveedor OIDC de GitHub + rol `malka-suite-dev-github-actions` | Deploy por OIDC, sin claves (ADR 0005) |
| CloudWatch | 3 grupos de logs (14 días), 2 alarmas de DLQ, X-Ray | Observabilidad |
| S3 | `malka-suite-tfstate-961868442562` | Estado remoto de Terraform |

### Costos

El proyecto vive dentro del **plan gratuito nuevo de AWS**, con dos consecuencias que condicionaron el diseño: la retención de backups de RDS está limitada a 1 día y no se permite el autoscaling de almacenamiento.

El costo fijo son los **VPC endpoints de interface, que se cobran por AZ: ~0,01 USD/hora por cada endpoint-AZ (~7,30 USD/mes cada uno)**, más 0,01 USD/GB procesado. Por eso cada endpoint vive en **una sola subred** (en dos AZs el costo se duplica) y el de bedrock-runtime queda **apagado hasta que el worker haga llamadas reales (#40)**. Hoy: Secrets Manager + SQS = **~14,60 USD/mes** (al prender Bedrock: ~21,90). Es el precio de no tener un NAT Gateway, que costaría unas cinco veces más. Lambda, API Gateway, RDS `db.t4g.micro` y ECR se mantienen dentro de los límites gratuitos para el volumen de este TP. Hay un presupuesto mensual con alerta por email (`presupuesto_mensual_usd` + `email_alertas`).

---

## Observabilidad

| Qué | Dónde | Detalle |
|---|---|---|
| Logs de la API | CloudWatch `/aws/lambda/malka-suite-dev-api` | Retención de 14 días. El grupo se declara en Terraform; si no, guardaría los logs para siempre |
| Logs del worker | CloudWatch `/aws/lambda/malka-suite-dev-worker` | Retención de 14 días, mismo criterio que la API |
| Trazas | AWS X-Ray | Tracing activo en la Lambda |
| Logs de la base | CloudWatch, exportados desde RDS | Incluye toda consulta que tarde más de 500 ms |
| Prueba de vida | `GET /health` | Liveness; el smoke test del deploy usa `/health/ready` |
| Límite de tráfico | API Gateway | 20 requests por segundo, ráfagas de hasta 50 |
| Log de acceso | CloudWatch, una línea JSON por request | `request_id`, `tenant_id`, `user_id`, `method`, `route`, `status`, `latency_ms` (ver [Errores y request_id](#errores-y-request_id)) |

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

### Errores y request_id

Todos los errores responden con la misma forma, sin importar de dónde vengan:

```json
{
  "error": {
    "code": "tanda_cerrada",
    "message": "La tanda ya está cerrada.",
    "details": {"tanda_id": 7},
    "request_id": "JKJaXmPLvHcESHA="
  }
}
```

| Status | `code` por defecto | Cuándo |
|---|---|---|
| 409 | `conflicto` | `ConflictoError`: el pedido choca con el estado actual |
| 422 | `validacion` | `ValidacionError` (regla de negocio) o datos mal formados. En este caso `details.campos` indica qué campo falló, sin devolver el valor recibido |
| 404 / 405 | `no_encontrado` / `metodo_no_permitido` | Ruta o método inexistente |
| 500 | `error_interno` | Error inesperado. Se loguea con su traza; al cliente no le llega el detalle |

En un endpoint, los errores se lanzan, no se arman a mano:

```python
from app.errores import ConflictoError

raise ConflictoError("La tanda ya está cerrada.", code="tanda_cerrada", details={"tanda_id": tanda_id})
```

Cada respuesta trae el header `x-request-id` (el que mandó el cliente si es válido, si no el de API Gateway o uno nuevo). Es el mismo valor que aparece en el cuerpo del error y en la línea de log, así que alcanza con él para encontrar un request en CloudWatch Logs Insights:

```
fields @timestamp, route, status, latency_ms
| filter request_id = "JKJaXmPLvHcESHA="
```

Los logs nunca incluyen headers, cuerpos ni tokens, y cualquier clave sensible (`authorization`, `token`, `cuit`, `cuil`, `dni`, `cbu`, etc.) se reemplaza por `[REDACTADO]`.

---

## Estructura del proyecto

```
app/
├── main.py              # Aplicación FastAPI y handler que invoca Lambda
├── config.py            # Configuración de conexión a la base de datos
├── migrar.py            # Handler de la Lambda de migraciones
├── errores.py           # Excepciones de dominio y forma única de errores
├── observabilidad.py    # request_id, middleware y logs JSON
├── contexto.py          # request_id del request en curso (contextvars)
└── worker.py            # Handler que consume las colas SQS
infra/
├── versions.tf          # Versiones de Terraform y providers; estado remoto en S3
├── main.tf              # Prefijo común de nombres, zonas y ARNs del modelo de Bedrock
├── migraciones.tf       # Lambda de migraciones, su rol y sus logs
├── variables.tf         # Variables generales del proyecto
├── network.tf           # VPC, subredes, security groups y VPC endpoints
├── rds.tf               # PostgreSQL, credenciales en Secrets Manager y su endpoint
├── ecr.tf               # Repositorio de imágenes y lifecycle policy
├── compute.tf           # Lambda de API, rol IAM y grupo de logs
├── api.tf               # API Gateway: rutas, CORS y límites de tráfico
├── sqs.tf               # Colas con DLQ, Lambda worker y alarmas de DLQ
├── s3_documentos.tf     # Bucket de documentos con URLs prefirmadas
├── oidc.tf              # Proveedor OIDC de GitHub y rol de deploy
├── outputs.tf           # Valores que se consultan después del apply
└── backend.hcl.example  # Plantilla del backend de estado
docs/
├── adr/                 # Decisiones de arquitectura
└── diagramas/           # Arquitectura y pipeline (regenerables con Python)
tests/                   # Pruebas automatizadas
migraciones/             # Migraciones de Alembic (versions/ tiene una por cambio)
alembic.ini              # Configuración de Alembic
.github/workflows/
├── ci.yml               # Lint, tipos, tests, Terraform e imagen en cada PR (#9)
└── deploy.yml           # Deploy a AWS en cada merge a main, con OIDC (#10)
Dockerfile               # Imagen de Lambda con Python 3.12
requirements.txt         # Dependencias de la API
AI-DECISIONS.md          # Registro de decisiones asistidas por IA
```


La API se escribe como una aplicación FastAPI común. Al final de `app/main.py`, Mangum la envuelve en `handler`, que es la función que Lambda invoca en cada request: traduce el evento que manda API Gateway a una request que FastAPI entiende. Por eso el mismo código corre igual en local con Uvicorn y en AWS.

---

## Desarrollo local

Requisitos: Python 3.12, Docker, Terraform 1.10+, AWS CLI con el perfil `malka`.

```bash
python -m venv .venv

# Linux / macOS
source .venv/bin/activate

# Git Bash en Windows
source .venv/Scripts/activate

# Windows (PowerShell)
.venv\Scripts\Activate.ps1

pip install -r requirements-dev.txt 
uvicorn app.main:app --reload
# http://127.0.0.1:8000/health
# http://127.0.0.1:8000/docs  (documentación interactiva de FastAPI, solo en local)
```

Uvicorn está en `requirements-dev.txt` y no en `requirements.txt` porque en AWS no se usa: allá la API la ejecuta Lambda a través de Mangum. 

El frontend solo necesita `VITE_API_BASE_URL` apuntando a la URL de la API. **No necesita credenciales de AWS**: todo lo que empieza con `VITE_` queda expuesto en el bundle del navegador.


### Variables de entorno de la API

| Variable | Descripción | Valor por defecto |
|---|---|---|
| `APP_ENVIRONMENT` | Ambiente en el que corre la API | `dev` |
| `APP_VERSION` | Versión que informa el endpoint `/health` | `0.1.0` |
| `COLA_DOCUMENTOS_URL` | URL de la cola SQS de documentos (la carga Terraform) | — |
| `COLA_INGESTA_URL` | URL de la cola SQS de ingesta (la carga Terraform) | — |

En local no hace falta definirlas. En AWS las carga Terraform en la Lambda: `APP_VERSION` toma el valor de la variable `image_tag`.

---

## Migraciones de base de datos

El esquema se versiona con [Alembic](https://alembic.sqlalchemy.org/). No se usa `create_all()`: cada cambio es una migración en `migraciones/versions/`, con `upgrade` y `downgrade`.

La dirección de la base la arma `app/config.py`: en local, con la variable `DATABASE_URL`; en AWS, con el secreto de Secrets Manager indicado en `DB_SECRET_NAME`.

Las tablas de negocio tienen Row Level Security forzado con la política `aislamiento_por_tenant`: cada consulta solo ve las filas del tenant definido con `SET LOCAL app.tenant_id` en esa transacción. Sin tenant definido, devuelven cero filas.

### Correr las migraciones en local

Con Docker Desktop abierto, levantar PostgreSQL 16 y crear un usuario sin privilegios de superusuario (los superusuarios saltean RLS):

```bash
docker run --name malka-postgres -e POSTGRES_PASSWORD=postgres -p 5432:5432 -d postgres:16
docker exec -it malka-postgres psql -U postgres -c "CREATE ROLE malka_owner LOGIN PASSWORD 'malka';" -c "CREATE DATABASE malka OWNER malka_owner;"
```

Definir la URL y aplicar las migraciones:

```bash
# bash
export DATABASE_URL="postgresql+psycopg://malka_owner:malka@localhost:5432/malka"
# PowerShell
$env:DATABASE_URL = "postgresql+psycopg://malka_owner:malka@localhost:5432/malka"

alembic upgrade head      # aplica todas las migraciones
alembic current           # muestra la versión actual
alembic downgrade -1      # deshace la última
```

### En AWS

La base es privada, así que las migraciones no se aplican desde una máquina local ni desde GitHub Actions: las corre la Lambda de migraciones (#53), que usa la misma imagen que la API. El deploy automático la invoca antes de actualizar la API y se frena si falla (ver [Despliegue](#despliegue)).

### Crear una migración nueva

```bash
alembic revision -m "descripcion del cambio"
```

Genera un archivo en `migraciones/versions/`. Las migraciones se escriben a mano y cada una tiene que tener un `downgrade` que funcione: antes de abrir el PR, probar `alembic downgrade -1` y volver a subir.

### Aplicar las migraciones en AWS

RDS es privada, así que las migraciones las aplica la Lambda `malka-suite-dev-migraciones`, que usa la misma imagen que la API con otro handler (`app/migrar.py`) y corre dentro de la VPC.

Una vez cargado el secret `LAMBDA_MIGRACIONES_NAME`, el deploy automático (`.github/workflows/deploy.yml`) la invoca con `upgrade` en cada merge a `main`, antes de actualizar la API. Si la migración falla, el deploy se frena. Ver `docs/adr/0005-deploy-con-oidc.md`.

Para invocarla a mano, con AWS CLI desde bash:

```bash
# Aplicar todas las migraciones pendientes
aws lambda invoke --profile malka --function-name malka-suite-dev-migraciones \
  --cli-binary-format raw-in-base64-out --cli-read-timeout 320 \
  --payload '{"accion": "upgrade", "revision": "head"}' respuesta.json
cat respuesta.json

# Deshacer la última migración
aws lambda invoke --profile malka --function-name malka-suite-dev-migraciones \
  --cli-binary-format raw-in-base64-out --cli-read-timeout 320 \
  --payload '{"accion": "downgrade", "revision": "-1"}' respuesta.json
cat respuesta.json
```

`respuesta.json` muestra la versión antes y después (`revision_anterior` y `revision_actual`). Los logs quedan en CloudWatch, en `/aws/lambda/malka-suite-dev-migraciones`. Un `downgrade` sin `revision` se rechaza a propósito.

Las migraciones se aplican antes de actualizar la API, así que tienen que ser compatibles con la versión anterior del código: no borrar ni renombrar columnas que el código actual todavía usa.


## Despliegue

### Automático: GitHub Actions con OIDC

Cada merge a `main` publica la versión nueva sin que nadie toque una terminal (`.github/workflows/deploy.yml`). También se puede disparar a mano desde *Actions → Deploy → Run workflow*, siempre sobre `main`.

1. **Credenciales temporales por OIDC.** El job pide un token a GitHub y asume el rol `malka-suite-dev-github-actions` (`infra/oidc.tf`). El rol solo acepta tokens de este repositorio y de la rama `main`; no hay claves de AWS guardadas en ningún lado.
2. **Build y push** de la imagen con los tags `<sha del commit>` y `latest`.
3. **Migraciones:** actualiza e invoca la Lambda de migraciones. Si la respuesta trae `FunctionError`, el deploy se frena y la API queda con la versión anterior. Este paso se saltea mientras `LAMBDA_MIGRACIONES_NAME` no esté configurado (#53).
4. **API y worker:** `update-function-code` apuntando a `latest` y espera a que terminen de actualizarse.
5. **Smoke test:** `curl` contra `/health/ready`; si no responde 200, la corrida queda en rojo.

Se actualiza apuntando a `latest` a propósito: es la misma URI que tiene Terraform en `image_uri`, así que **`terraform plan` sigue sin cambios después de un deploy**. El tag con el SHA queda en ECR para saber qué commit está corriendo y para poder volver atrás.

El pipeline **no** corre `terraform apply`: su rol solo puede subir imágenes a ECR y actualizar las Lambdas del proyecto. Los cambios de infraestructura siguen siendo manuales (ver [Infraestructura](#2-infraestructura)).

#### Configuración (una sola vez)

1. Aplicar `infra/oidc.tf` a mano (`terraform apply`) y copiar la salida `github_actions_role_arn`. Si el proveedor OIDC de GitHub ya existía en la cuenta, importarlo antes (el comando está al principio de `oidc.tf`).
2. Cargar en *Settings → Secrets and variables → Actions → Secrets*:

| Secret | Valor | Sale de |
|---|---|---|
| `AWS_ROLE_ARN` | ARN del rol de deploy | `terraform output github_actions_role_arn` |
| `ECR_REPOSITORY` | `malka-suite-dev-backend` | nombre del repositorio de `ecr_repository_url` |
| `LAMBDA_FUNCTION_NAME` | `malka-suite-dev-api` | `terraform output lambda_api_nombre` |
| `LAMBDA_WORKER_NAME` | `malka-suite-dev-worker` | `terraform output lambda_worker_nombre` |
| `LAMBDA_MIGRACIONES_NAME` | `malka-suite-dev-migraciones` | cuando exista la Lambda (#53); mientras tanto, sin cargar |
| `HEALTH_URL` | `<api_base_url>/health/ready` | `terraform output api_base_url` |

El repositorio es público: el número de cuenta no va escrito en el workflow, y el paso de credenciales lo enmascara en los logs.

#### Revertir un deploy

- **Camino normal:** `git revert <commit>` en una rama, PR y merge. El pipeline despliega la versión anterior y queda registrado quién y por qué.
- **Emergencia:** volver a etiquetar como `latest` una imagen anterior por su SHA y actualizar las Lambdas (ECR conserva las últimas 10 imágenes):

```bash
export AWS_PROFILE=malka
REPO=malka-suite-dev-backend
SHA=<sha-del-commit-bueno>
MANIFIESTO=$(aws ecr batch-get-image --repository-name $REPO --image-ids imageTag=$SHA \
  --query 'images[0].imageManifest' --output text)
aws ecr put-image --repository-name $REPO --image-tag latest --image-manifest "$MANIFIESTO"
URI=$(aws ecr describe-repositories --repository-names $REPO --query 'repositories[0].repositoryUri' --output text)
for fn in malka-suite-dev-api malka-suite-dev-worker; do
  aws lambda update-function-code --function-name $fn --image-uri $URI:latest > /dev/null
done
```

No apuntar la Lambda directamente a `:<sha>`: Terraform lo vería como un cambio. Una migración ya aplicada no se revierte sola: si hace falta, se invoca la Lambda de migraciones con `{"accion": "downgrade", "revision": "-1"}`.

### Manual

> Para aplicar infraestructura o si el pipeline no está disponible. Los comandos de esta sección están escritos para bash (Linux, macOS o Git Bash). En PowerShell, `export AWS_PROFILE=malka` se escribe `$env:AWS_PROFILE = "malka"`, y los comandos cortados con `\` se escriben en una sola línea.

#### 1. Imagen de contenedor

Lambda rechaza las imágenes que traen attestations, por eso los dos flags:

```bash
aws ecr get-login-password --profile malka --region us-east-1 \
  | docker login --username AWS --password-stdin 961868442562.dkr.ecr.us-east-1.amazonaws.com

docker build --provenance=false --sbom=false -t malka-suite-dev-backend:$(git rev-parse --short HEAD) .
docker tag  malka-suite-dev-backend:$(git rev-parse --short HEAD) \
  961868442562.dkr.ecr.us-east-1.amazonaws.com/malka-suite-dev-backend:$(git rev-parse --short HEAD)
docker push 961868442562.dkr.ecr.us-east-1.amazonaws.com/malka-suite-dev-backend:$(git rev-parse --short HEAD)
```

#### 2. Infraestructura

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

### Variables de Terraform

| Variable | Para qué sirve |
|---|---|
| `project` / `environment` | Forman el prefijo de todos los recursos (`malka-suite-dev`) |
| `region` | Región de AWS |
| `vpc_cidr` | Rango de direcciones de la VPC |
| `image_tag` | Etiqueta de la imagen de ECR que ejecuta la Lambda |
| `origenes_permitidos` | Orígenes habilitados para CORS |
| `habilitar_endpoint_sqs` | VPC endpoint de SQS: lo usa la API para enviar mensajes (~7,30 USD/mes; se cobra por AZ) |
| `habilitar_endpoint_bedrock` | VPC endpoint de bedrock-runtime: apagado hasta que el worker llame a Bedrock (#40) |
| `presupuesto_mensual_usd` / `email_alertas` | Tope y email de la alerta de AWS Budgets (con email vacío no se crea) |
| `habilitar_endpoint_secretos` | Crea el VPC endpoint de Secrets Manager |
| `db_clase_instancia` | Clase de la instancia de RDS |

### Salidas útiles

Se consultan desde `infra/` con `terraform output <nombre>`:

| Salida | Para qué sirve |
|---|---|
| `api_base_url` | URL pública de la API; va en `VITE_API_BASE_URL` del frontend |
| `health_url` | URL de la prueba de vida |
| `ecr_repository_url` | Repositorio donde se suben las imágenes |
| `lambda_api_nombre` | Nombre de la función, para buscar sus logs |
| `db_secret_nombre` | Nombre del secreto con las credenciales de la base |
| `github_actions_role_arn` | Rol que asume el deploy; va en el secret `AWS_ROLE_ARN` |

---

## Convenciones

- **Ramas:** `feat/…`, `fix/…`, `docs/…`, `ci/…`, siempre partiendo de `main`.
- **Commits:** Conventional Commits en español (`feat(api): …`, `docs: …`).
- **Pull requests:** uno por issue, con descripción de qué hace y cómo se verificó. Requieren **1 aprobación** y se integran con **Create a merge commit** (nunca squash: rompe las cadenas de ramas y obliga a re-sincronizar los PRs siguientes).
- **`main` protegida** por el ruleset `proteger-main`, que además invalida las aprobaciones cuando se pushean commits nuevos.
- **Tablero:** [Malka Suite — Checkpoint 1](https://github.com/orgs/Reinas-Malka/projects/1), con WIP límite 3 en *In Progress* y en *In Review*.

---

### Pendientes conocidos

- `/health/ready` todavía no consulta la base: responde OK sin verificar dependencias.
- `APP_VERSION` sigue fijo en `"latest"`: el commit desplegado se ve en el tag de la imagen en ECR y en el resumen de cada corrida de *Deploy*, pero todavía no en `/health`.
- `terraform apply` sigue siendo manual, desde la máquina local.

---

## Problemas comunes

**`uvicorn: command not found`**

Uvicorn no está en `requirements.txt`. Instalarlo con `pip install -r requirements-dev.txt`. 

**PowerShell no deja activar el entorno virtual**

Si aparece un error de que la ejecución de scripts está deshabilitada, habilitarla solo para tu usuario y volver a activar el entorno:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
.venv\Scripts\Activate.ps1
```

**El frontend muestra `blocked by CORS policy`**

El origen del frontend no está en `origenes_permitidos`. Agregarlo y ejecutar `terraform apply`. No agregar `CORSMiddleware` en FastAPI.

**Al crear la Lambda, Terraform dice que la imagen no existe**

La Lambda corre una imagen de ECR, así que el repositorio tiene que tener al menos una imagen antes de crear la función. Subir la imagen primero (ver [Despliegue](#despliegue)).

**Lambda rechaza la imagen**

La imagen se construyó con attestations. Construirla con `--provenance=false --sbom=false`.

**El deploy falla con `Not authorized to perform sts:AssumeRoleWithWebIdentity`**

El workflow no corrió sobre `main` (el rol solo confía en esa rama), o falta `permissions: id-token: write`, o el secret `AWS_ROLE_ARN` no coincide con `terraform output github_actions_role_arn`.

**El deploy se frena en "Migraciones"**

La Lambda de migraciones devolvió `FunctionError`. El detalle está en la salida del paso y en CloudWatch (`/aws/lambda/malka-suite-dev-migraciones`). La API no se actualizó: sigue con la versión anterior.

**`terraform init` falla al acceder al estado**

Falta `infra/backend.hcl` o el perfil de AWS no es `malka`. Verificar con `aws sts get-caller-identity --profile malka`.

---

## Repositorios

| Repo | Contenido |
|---|---|
| [backend-malka](https://github.com/Reinas-Malka/backend-malka) | API FastAPI, infraestructura Terraform, documentación |
| [frontend-malka](https://github.com/Reinas-Malka/frontend-malka) | Aplicación React + Vite + TypeScript |

El motivo de tener dos repositorios está documentado en [`docs/adr/0004-dos-repositorios.md`](docs/adr/0004-dos-repositorios.md).

## IA: modelo y costos

- Modelo: `us.anthropic.claude-haiku-4-5-20251001-v1:0` (inference profile, us-east-1).
- Verificado el 29/09/2026: `converse` OK (`end_turn`, 13 tokens in / 4 out, 654 ms).
- Precio: 1,00 USD por millón de tokens de entrada, 5,00 por millón de salida.
- Por 1000 tokens: 0,001 USD de entrada y 0,005 USD de salida.
- Plan B: Sonnet 4.5. Opus descartado por costo.
- `claude-3-5-haiku-20241022` está EOL: devuelve ResourceNotFoundException.
- El plan gratuito de AWS no bloquea Bedrock (riesgo del CP2 descartado).

## Costo fijo de infraestructura

- Cada endpoint de interface se cobra **por AZ**: ~7,30 USD/mes por endpoint-AZ (+0,01 USD/GB).
- En una sola subred: Secrets Manager + SQS = ~14,60 USD/mes. Al prender bedrock-runtime (#40): ~21,90.
- El endpoint de S3 es gateway: sin costo fijo.