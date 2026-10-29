"""Llamadas a la IA por criadero: el limite de uso de Bedrock (#45).

Bedrock cobra por token. Cada llamada se reserva antes de hacerla (una fila
por llamada) y despues se le anotan los tokens que consumio:

- El tope por minuto y por dia se cuenta sobre estas filas, por criadero
  (RLS), en app/services/ia/limites.py.
- Los tokens quedan para saber cuanto gasta cada criadero.
- Sin DELETE: es el registro de lo consumido. UPDATE solo para anotar los
  tokens despues de la llamada.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-29

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Rol de aplicacion sin privilegios, creado en la 0002 (#19).
ROL_APP = "malka_app"
PERMISOS = "SELECT, INSERT, UPDATE"

# Para que se usa la IA. Hoy solo el borrador (#40); un uso nuevo suma su
# valor aca y en app/services/ia/limites.py.
MOTIVOS = ("borrador",)
MOTIVOS_SQL = ", ".join(f"'{motivo}'" for motivo in MOTIVOS)

# Misma expresion que la 0001: sin app.tenant_id definido, cero filas.
TENANT_ACTUAL = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "llamada_ia",
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
        ),
        sa.Column("motivo", sa.Text(), nullable=False),
        # NULL hasta que la llamada termina (o si fallo antes de responder).
        sa.Column("tokens_entrada", sa.Integer(), nullable=True),
        sa.Column("tokens_salida", sa.Integer(), nullable=True),
        sa.Column(
            "creado_en",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(f"motivo IN ({MOTIVOS_SQL})", name="ck_llamada_ia_motivo"),
        sa.CheckConstraint(
            "tokens_entrada IS NULL OR tokens_entrada >= 0",
            name="ck_llamada_ia_tokens_entrada",
        ),
        sa.CheckConstraint(
            "tokens_salida IS NULL OR tokens_salida >= 0",
            name="ck_llamada_ia_tokens_salida",
        ),
    )
    # Los topes cuentan las llamadas del criadero en una ventana de tiempo.
    op.create_index(
        "ix_llamada_ia_tenant_creado_en", "llamada_ia", ["tenant_id", "creado_en"]
    )

    op.execute("ALTER TABLE llamada_ia ENABLE ROW LEVEL SECURITY")
    # FORCE: las politicas se aplican tambien al duenio de la tabla.
    op.execute("ALTER TABLE llamada_ia FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY aislamiento_por_tenant ON llamada_ia "
        f"USING (tenant_id = {TENANT_ACTUAL}) "
        f"WITH CHECK (tenant_id = {TENANT_ACTUAL})"
    )
    op.execute(f"GRANT {PERMISOS} ON llamada_ia TO {ROL_APP}")
    # Sin SET LOCAL el default es NULL y el NOT NULL rechaza el insert.
    op.execute(
        f"ALTER TABLE llamada_ia ALTER COLUMN tenant_id SET DEFAULT {TENANT_ACTUAL}"
    )


def downgrade() -> None:
    # La politica, el indice y el permiso se van con la tabla.
    op.drop_table("llamada_ia")
