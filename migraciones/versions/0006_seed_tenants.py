"""Semilla de los criaderos de demo (docs/dominio.md, Multitenant), solo en dev.

Los usuarios de prueba de Cognito (infra/scripts/seed_cognito_users.sh)
traen custom:tenant_id con estos UUIDs; sin las filas correspondientes en
la tabla tenant, el primer INSERT de negocio revienta por la FK
(descubierto en vivo el 09/10). Idempotente: ON CONFLICT DO NOTHING.

Solo siembra si APP_ENVIRONMENT vale exactamente "dev" (#104). La auditoria
de fugas del 15/10 encontro que, sin ese guard, cualquier base que corriera
la cadena (produccion incluida) quedaba con tres criaderos con UUID y CUIT
fijos. Decisiones:

- Guard en esta migracion y no una migracion nueva: es solo DML, no cambia
  el esquema, y en produccion nunca corrio. Una 0017 que borrara las filas
  llegaria tarde: la 0006 ya las habria creado antes en una base nueva.
- Se usa APP_ENVIRONMENT, la misma variable que la Lambda de migraciones ya
  recibe de Terraform (infra/migraciones.tf, var.environment). No hace falta
  tocar infra/.
- Falla cerrado: si la variable falta o tiene otro valor, no siembra. Ojo:
  la API asume "dev" cuando falta (app/main.py); aca es al reves a proposito,
  porque una semilla de mas en produccion es peor que una de menos en local.
  En local, para tener los criaderos: APP_ENVIRONMENT=dev alembic upgrade head.
- El downgrade usa el mismo guard: fuera de dev no borra filas que esta
  migracion nunca creo.

Dev ya tenia aplicada la 0006 con las filas: no se vuelve a correr ni se
pierden.
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Unico entorno donde existen los usuarios de prueba de Cognito.
ENTORNO_CON_SEMILLA = "dev"

TENANTS = [
    (
        "11111111-1111-4111-8111-111111111111",
        "Malka (criadero propio)",
        "30123456781",
    ),
    (
        "22222222-2222-4222-8222-222222222222",
        "Criadero socio B (demo)",
        "30234567892",
    ),
    (
        "33333333-3333-4333-8333-333333333333",
        "Criadero socio C (demo)",
        "30345678901",
    ),
]


def corresponde_sembrar() -> bool:
    """True solo si el entorno es dev; sin entorno conocido, no se siembra."""
    return os.getenv("APP_ENVIRONMENT") == ENTORNO_CON_SEMILLA


def upgrade() -> None:
    if not corresponde_sembrar():
        return
    for tenant_id, nombre, cuit in TENANTS:
        op.execute(
            sa.text(
                "INSERT INTO tenant (id, nombre, cuit) "
                "VALUES (CAST(:id AS uuid), :nombre, :cuit) "
                "ON CONFLICT (id) DO NOTHING"
            ).bindparams(id=tenant_id, nombre=nombre, cuit=cuit)
        )


def downgrade() -> None:
    if not corresponde_sembrar():
        return
    for tenant_id, _, _ in TENANTS:
        op.execute(
            sa.text("DELETE FROM tenant WHERE id = CAST(:id AS uuid)").bindparams(
                id=tenant_id
            )
        )
