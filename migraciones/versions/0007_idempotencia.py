"""Claves de idempotencia de los POST que crean recursos (#25).

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-09

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROL_APP = "malka_app"

# Misma expresion que la 0001: sin app.tenant_id definido, cero filas.
TENANT_ACTUAL = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"

# docs/dominio.md, Idempotencia: la clave vale 24 horas.
VENTANA = "interval '24 hours'"


def upgrade() -> None:
    op.create_table(
        "idempotencia",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant.id", ondelete="RESTRICT"),
            nullable=False,
            server_default=sa.text(TENANT_ACTUAL),
        ),
        # El valor del header Idempotency-Key, tal como lo mando el frontend.
        sa.Column("clave", sa.Text(), nullable=False),
        # SHA-256 de metodo + ruta + cuerpo: detecta la misma clave con otro pedido.
        sa.Column("hash_request", sa.Text(), nullable=False),
        # NULL mientras el pedido original esta en curso (no deberia verse
        # nunca desde otra transaccion: se completa en la misma).
        sa.Column("status_code", sa.SmallInteger(), nullable=True),
        sa.Column("respuesta", postgresql.JSONB(), nullable=True),
        sa.Column(
            "creado_en",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "expira_en",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text(f"now() + {VENTANA}"),
        ),
        sa.CheckConstraint(
            "char_length(clave) BETWEEN 1 AND 255", name="ck_idempotencia_clave"
        ),
        # La clave es por criadero: dos criaderos pueden usar el mismo valor
        # sin pisarse (y RLS no deja que se vean entre ellos).
        sa.UniqueConstraint("tenant_id", "clave", name="uq_idempotencia_tenant_clave"),
    )
    # Para la limpieza de claves vencidas, cuando exista el job.
    op.create_index("ix_idempotencia_expira_en", "idempotencia", ["expira_en"])

    op.execute("ALTER TABLE idempotencia ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE idempotencia FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY aislamiento_por_tenant ON idempotencia "
        f"USING (tenant_id = {TENANT_ACTUAL}) "
        f"WITH CHECK (tenant_id = {TENANT_ACTUAL})"
    )
    # Sin DELETE: una clave vencida se reutiliza con UPDATE.
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON idempotencia TO {ROL_APP}")


def downgrade() -> None:
    # Con la tabla se van el indice, la politica y los permisos.
    op.drop_table("idempotencia")
