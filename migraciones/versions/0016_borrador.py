"""Borradores de documentos generados por la IA (#40).

Reglas en docs/dominio.md ("Aprobacion de borradores") y en el issue:
- La IA redacta, el codigo calcula: el contenido es solo prosa validada
  (app/services/ia/borrador.py); nada fiscal se persiste.
- Estados: generando -> borrador | rechazado. aprobado lo agrega el #42.
- Un borrador listo tiene contenido; uno rechazado tiene motivo y ningun
  contenido (una salida invalida no se guarda).
- Se registra modelo, version del prompt, tokens, latencia, reintentos y
  resultado de la validacion de cada generacion.
- Sin DELETE: el worker solo actualiza el estado y el resultado.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROL_APP = "malka_app"
PERMISOS = "SELECT, INSERT, UPDATE"

# Mismos valores que app/services/ia/generacion.py.
ESTADOS = ("generando", "borrador", "rechazado")
ESTADOS_SQL = ", ".join(f"'{e}'" for e in ESTADOS)
MOTIVOS = ("salida_invalida", "error_modelo", "cola_no_disponible")
MOTIVOS_SQL = ", ".join(f"'{m}'" for m in MOTIVOS)

TENANT_ACTUAL = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "borrador",
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
        sa.Column("pedido_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "estado",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'generando'"),
        ),
        # encabezado, cuerpo y notas, ya validados. NULL hasta que hay borrador.
        sa.Column("contenido", postgresql.JSONB(), nullable=True),
        sa.Column("motivo_rechazo", sa.Text(), nullable=True),
        # La llamada reservada en el limite de IA (#45), para anotarle tokens.
        sa.Column("llamada_ia_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("modelo", sa.Text(), nullable=True),
        sa.Column("version_prompt", sa.Text(), nullable=True),
        sa.Column("tokens_entrada", sa.Integer(), nullable=True),
        sa.Column("tokens_salida", sa.Integer(), nullable=True),
        sa.Column("latencia_ms", sa.Integer(), nullable=True),
        sa.Column("reintentos", sa.Integer(), nullable=True),
        sa.Column("resultado_validacion", sa.Text(), nullable=True),
        # El sub de Cognito de quien lo pidio.
        sa.Column("solicitado_por", sa.Text(), nullable=False),
        sa.Column(
            "creado_en",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "actualizado_en",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(f"estado IN ({ESTADOS_SQL})", name="ck_borrador_estado"),
        sa.CheckConstraint(
            f"motivo_rechazo IS NULL OR motivo_rechazo IN ({MOTIVOS_SQL})",
            name="ck_borrador_motivo",
        ),
        sa.CheckConstraint(
            "(estado = 'borrador') = (contenido IS NOT NULL) "
            "AND (estado = 'rechazado') = (motivo_rechazo IS NOT NULL)",
            name="ck_borrador_contenido_segun_estado",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "pedido_id"],
            ["pedido.tenant_id", "pedido.id"],
            name="fk_borrador_pedido_mismo_tenant",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_borrador_tenant_pedido", "borrador", ["tenant_id", "pedido_id", "creado_en"]
    )

    op.execute("ALTER TABLE borrador ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE borrador FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY aislamiento_por_tenant ON borrador "
        f"USING (tenant_id = {TENANT_ACTUAL}) "
        f"WITH CHECK (tenant_id = {TENANT_ACTUAL})"
    )
    op.execute(f"GRANT {PERMISOS} ON borrador TO {ROL_APP}")
    op.execute(
        f"ALTER TABLE borrador ALTER COLUMN tenant_id SET DEFAULT {TENANT_ACTUAL}"
    )


def downgrade() -> None:
    op.drop_table("borrador")
