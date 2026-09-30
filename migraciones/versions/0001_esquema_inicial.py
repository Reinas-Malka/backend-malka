"""Esquema inicial: tenant, raza, tanda y tanda_evento con RLS.

Revision ID: 0001
Revises:
Create Date: 2026-09-30

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tablas que guardan datos de un criadero y quedan aisladas por RLS.
TABLAS_DE_NEGOCIO = ("raza", "tanda", "tanda_evento")

# Ciclo de una tanda (issue #28).
ETAPAS = ("traslarve", "iniciadora", "continuadora", "parque", "banco", "cerrada")
ETAPAS_SQL = ", ".join(f"'{etapa}'" for etapa in ETAPAS)

# Si app.tenant_id no esta definido, current_setting(..., true) devuelve NULL
# o texto vacio. NULLIF lo convierte en NULL, la comparacion da falso y la
# consulta devuelve cero filas en lugar de fallar.
TENANT_ACTUAL = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _id() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _tenant_id() -> sa.Column:
    return sa.Column(
        "tenant_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("tenant.id", name=None, ondelete="RESTRICT"),
        nullable=False,
    )


def _creado_en() -> sa.Column:
    return sa.Column(
        "creado_en",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )


def _actualizado_en() -> sa.Column:
    return sa.Column(
        "actualizado_en",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )


def upgrade() -> None:
    # Criaderos. No lleva tenant_id porque cada fila ES un tenant.
    op.create_table(
        "tenant",
        _id(),
        sa.Column("nombre", sa.Text(), nullable=False),
        _creado_en(),
        _actualizado_en(),
    )

    op.create_table(
        "raza",
        _id(),
        _tenant_id(),
        sa.Column("nombre", sa.Text(), nullable=False),
        sa.Column("descripcion", sa.Text(), nullable=True),
        # Borrado logico: una raza con tandas historicas no se borra (#30).
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.true()),
        _creado_en(),
        _actualizado_en(),
        sa.UniqueConstraint("tenant_id", "nombre", name="uq_raza_tenant_nombre"),
        # Permite que otras tablas referencien (tenant_id, id) juntos.
        sa.UniqueConstraint("tenant_id", "id", name="uq_raza_tenant_id"),
    )

    op.create_table(
        "tanda",
        _id(),
        _tenant_id(),
        sa.Column("raza_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fecha_traslarve", sa.Date(), nullable=False),
        # Etapa en la que esta hoy. Se actualiza en la misma transaccion que
        # registra el evento; la historia completa vive en tanda_evento.
        sa.Column(
            "etapa_actual",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'traslarve'"),
        ),
        # Alta con fecha pasada (excepcion al rechazo de domingos, #27).
        sa.Column(
            "retroactivo", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        _creado_en(),
        _actualizado_en(),
        sa.CheckConstraint(
            f"etapa_actual IN ({ETAPAS_SQL})", name="ck_tanda_etapa_actual"
        ),
        # La raza tiene que ser del mismo criadero que la tanda.
        sa.ForeignKeyConstraint(
            ["tenant_id", "raza_id"],
            ["raza.tenant_id", "raza.id"],
            name="fk_tanda_raza_mismo_tenant",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_tanda_tenant_id"),
    )
    op.create_index("ix_tanda_tenant_fecha", "tanda", ["tenant_id", "fecha_traslarve"])

    # Historia inmutable de cada tanda (#28): las correcciones son eventos
    # nuevos de tipo 'ajuste', nunca ediciones. Por eso no tiene
    # actualizado_en.
    op.create_table(
        "tanda_evento",
        _id(),
        _tenant_id(),
        sa.Column("tanda_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tipo", sa.Text(), nullable=False),
        sa.Column("etapa", sa.Text(), nullable=False),
        sa.Column("cant_entrada", sa.Integer(), nullable=False),
        sa.Column("cant_aceptada", sa.Integer(), nullable=True),
        # Quien registro el evento: el identificador del usuario (sub de Cognito).
        sa.Column("registrado_por", sa.Text(), nullable=False),
        sa.Column(
            "ocurrido_en",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("observacion", sa.Text(), nullable=True),
        _creado_en(),
        sa.CheckConstraint(
            "tipo IN ('transicion', 'ajuste')", name="ck_tanda_evento_tipo"
        ),
        sa.CheckConstraint(f"etapa IN ({ETAPAS_SQL})", name="ck_tanda_evento_etapa"),
        sa.CheckConstraint("cant_entrada >= 0", name="ck_tanda_evento_entrada"),
        sa.CheckConstraint(
            "cant_aceptada IS NULL OR "
            "(cant_aceptada >= 0 AND cant_aceptada <= cant_entrada)",
            name="ck_tanda_evento_aceptada",
        ),
        # El evento tiene que ser del mismo criadero que la tanda.
        sa.ForeignKeyConstraint(
            ["tenant_id", "tanda_id"],
            ["tanda.tenant_id", "tanda.id"],
            name="fk_tanda_evento_tanda_mismo_tenant",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_tanda_evento_tenant_tanda",
        "tanda_evento",
        ["tenant_id", "tanda_id", "ocurrido_en"],
    )
    # Para las metricas por etapa y periodo (#31).
    op.create_index(
        "ix_tanda_evento_tenant_etapa",
        "tanda_evento",
        ["tenant_id", "etapa", "ocurrido_en"],
    )

    # Row Level Security en las tablas de negocio.
    for tabla in TABLAS_DE_NEGOCIO:
        op.execute(f"ALTER TABLE {tabla} ENABLE ROW LEVEL SECURITY")
        # FORCE: las politicas se aplican tambien al duenio de la tabla.
        op.execute(f"ALTER TABLE {tabla} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY aislamiento_por_tenant ON {tabla} "
            f"USING (tenant_id = {TENANT_ACTUAL}) "
            f"WITH CHECK (tenant_id = {TENANT_ACTUAL})"
        )


def downgrade() -> None:
    # Al borrar cada tabla se borran tambien sus indices y politicas.
    # El orden es el inverso a la creacion por las claves foraneas.
    op.drop_table("tanda_evento")
    op.drop_table("tanda")
    op.drop_table("raza")
    op.drop_table("tenant")
