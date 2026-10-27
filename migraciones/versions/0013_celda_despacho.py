"""Trazabilidad del despacho en la celda (#36).

Reglas en docs/dominio.md ("Disponibilidad, reservas y despacho") y en el
ADR 0014: despachar es pasar celdas enjauladas a despachada, eligiendo de uno
o varios bancos. Esta migracion guarda a que linea del pedido fue cada celda
y en que fecha salio:
- celda.pedido_item_id: la linea del pedido. Sin esto, una vez despachadas no
  hay forma de saber a que pedido fue cada reina.
- celda.fecha_despacho: el dia en que salio, como las demas fechas de la
  celda.

Todo es aditivo y nullable. Las celdas despachadas antes de esta migracion
quedan sin linea ni fecha: por eso los CHECK piden los dos datos juntos y
solo en despachada, pero no obligan a toda despachada a tenerlos.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-24

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # La FK compuesta de la celda apunta a (tenant_id, id) de pedido_item, y
    # Postgres exige un UNIQUE sobre esas columnas. El de la 0012 tiene
    # raza_id adentro y la celda no tiene raza.
    op.create_unique_constraint(
        "uq_pedido_item_tenant_id", "pedido_item", ["tenant_id", "id"]
    )

    # Nullable: las celdas que no se despacharon no tienen linea ni fecha.
    op.add_column("celda", sa.Column("pedido_item_id", postgresql.UUID(as_uuid=True)))
    op.add_column("celda", sa.Column("fecha_despacho", sa.Date()))

    # MATCH SIMPLE, como banco_id: con pedido_item_id NULL la FK no se evalua.
    op.create_foreign_key(
        "fk_celda_pedido_item_mismo_tenant",
        "celda",
        "pedido_item",
        ["tenant_id", "pedido_item_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    # Para ver que celdas fueron a una linea, y para que el RESTRICT no
    # recorra toda la tabla al borrar una linea.
    op.create_index(
        "ix_celda_tenant_pedido_item", "celda", ["tenant_id", "pedido_item_id"]
    )

    # Solo una celda despachada sabe a que linea fue.
    op.create_check_constraint(
        "ck_celda_despacho_solo_despachada",
        "celda",
        "pedido_item_id IS NULL OR estado = 'despachada'",
    )
    # Linea y fecha van juntas. No se exige en toda despachada: las de antes
    # de esta migracion quedan sin las dos.
    op.create_check_constraint(
        "ck_celda_despacho_completo",
        "celda",
        "(pedido_item_id IS NULL) = (fecha_despacho IS NULL)",
    )


def downgrade() -> None:
    # Al reves que el upgrade: la FK depende del UNIQUE.
    op.drop_constraint("ck_celda_despacho_completo", "celda", type_="check")
    op.drop_constraint("ck_celda_despacho_solo_despachada", "celda", type_="check")
    op.drop_index("ix_celda_tenant_pedido_item", table_name="celda")
    op.drop_constraint("fk_celda_pedido_item_mismo_tenant", "celda", type_="foreignkey")
    op.drop_column("celda", "fecha_despacho")
    op.drop_column("celda", "pedido_item_id")
    op.drop_constraint("uq_pedido_item_tenant_id", "pedido_item", type_="unique")
