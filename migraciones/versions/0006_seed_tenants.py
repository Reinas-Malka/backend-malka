"""Semilla de los criaderos de demo (docs/dominio.md, Multitenant).

Los usuarios de prueba de Cognito (infra/scripts/seed_cognito_users.sh)
traen custom:tenant_id con estos UUIDs; sin las filas correspondientes en
la tabla tenant, el primer INSERT de negocio revienta por la FK
(descubierto en vivo el 09/10). Idempotente: ON CONFLICT DO NOTHING.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANTS = [
    ("11111111-1111-4111-8111-111111111111", "Malka (criadero propio)"),
    ("22222222-2222-4222-8222-222222222222", "Criadero socio B (demo)"),
    ("33333333-3333-4333-8333-333333333333", "Criadero socio C (demo)"),
]


def upgrade() -> None:
    for tenant_id, nombre in TENANTS:
        op.execute(
            sa.text(
                "INSERT INTO tenant (id, nombre) VALUES (:id, :nombre) "
                "ON CONFLICT (id) DO NOTHING"
            ).bindparams(id=tenant_id, nombre=nombre)
        )


def downgrade() -> None:
    for tenant_id, _ in TENANTS:
        op.execute(
            sa.text("DELETE FROM tenant WHERE id = :id").bindparams(id=tenant_id)
        )
