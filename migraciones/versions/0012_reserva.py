"""Reservas de reinas por raza, ligadas a la linea del pedido (#35).

Reglas en docs/dominio.md ("Disponibilidad, reservas y despacho") y en el
ADR 0014:
- La reserva va por raza, sin banco: los bancos se eligen al despachar (#36).
- disponible = celdas enjauladas de la raza - reservas activas de la raza.
- Una reserva esta activa hasta que se despacha (consumida) o se cancela el
  pedido (liberada). No vence.
- Sin DELETE: una reserva cambia de estado, no se borra.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-19

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Rol de aplicacion sin privilegios, creado en la 0002 (#19).
ROL_APP = "malka_app"
PERMISOS = "SELECT, INSERT, UPDATE"

# Mismos valores que el Enum de app/services/ventas/reservas.py. Se copian a
# proposito: una migracion no importa codigo de la app.
ESTADOS = ("activa", "consumida", "liberada")
ESTADOS_SQL = ", ".join(f"'{estado}'" for estado in ESTADOS)

# Misma expresion que la 0001: sin app.tenant_id definido, cero filas.
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
        sa.ForeignKey("tenant.id", ondelete="RESTRICT"),
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


def _aislar_por_tenant(tabla: str, permisos: str) -> None:
    """Aplica a una tabla de negocio la convencion de la #19."""
    op.execute(f"ALTER TABLE {tabla} ENABLE ROW LEVEL SECURITY")
    # FORCE: las politicas se aplican tambien al duenio de la tabla.
    op.execute(f"ALTER TABLE {tabla} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY aislamiento_por_tenant ON {tabla} "
        f"USING (tenant_id = {TENANT_ACTUAL}) "
        f"WITH CHECK (tenant_id = {TENANT_ACTUAL})"
    )
    op.execute(f"GRANT {permisos} ON {tabla} TO {ROL_APP}")
    # Sin SET LOCAL el default es NULL y el NOT NULL rechaza el insert.
    op.execute(
        f"ALTER TABLE {tabla} ALTER COLUMN tenant_id SET DEFAULT {TENANT_ACTUAL}"
    )


def upgrade() -> None:
    # La FK de abajo apunta a (tenant_id, id, raza_id) de pedido_item, y
    # Postgres exige un UNIQUE sobre esas columnas. Con raza_id adentro, la
    # base garantiza que la reserva es de la misma raza que su linea.
    op.create_unique_constraint(
        "uq_pedido_item_tenant_id_raza",
        "pedido_item",
        ["tenant_id", "id", "raza_id"],
    )

    op.create_table(
        "reserva",
        _id(),
        _tenant_id(),
        sa.Column("pedido_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Copia de pedido_item.raza_id: el lock y la suma de reservadas van
        # por raza (ADR 0014) y asi no necesitan un JOIN con pedido_item.
        sa.Column("raza_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cantidad", sa.Integer(), nullable=False),
        sa.Column(
            "estado",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'activa'"),
        ),
        _creado_en(),
        _actualizado_en(),
        sa.CheckConstraint("cantidad > 0", name="ck_reserva_cantidad"),
        sa.CheckConstraint(f"estado IN ({ESTADOS_SQL})", name="ck_reserva_estado"),
        # Mismo criadero y misma raza que la linea. No hace falta otra FK a
        # raza: pedido_item ya la tiene.
        sa.ForeignKeyConstraint(
            ["tenant_id", "pedido_item_id", "raza_id"],
            ["pedido_item.tenant_id", "pedido_item.id", "pedido_item.raza_id"],
            name="fk_reserva_pedido_item_mismo_tenant",
            ondelete="RESTRICT",
        ),
    )

    # Una sola reserva activa por linea: reservar dos veces el mismo pedido
    # no puede contar doble. Las consumidas y liberadas no cuentan.
    op.create_index(
        "uq_reserva_item_activa",
        "reserva",
        ["tenant_id", "pedido_item_id"],
        unique=True,
        postgresql_where=sa.text("estado = 'activa'"),
    )
    # Para sumar las reservas activas de una raza (disponibilidad y lock).
    op.create_index(
        "ix_reserva_tenant_raza_activa",
        "reserva",
        ["tenant_id", "raza_id"],
        postgresql_where=sa.text("estado = 'activa'"),
    )

    _aislar_por_tenant("reserva", PERMISOS)


def downgrade() -> None:
    # Las politicas, los indices y los permisos se van con la tabla.
    op.drop_table("reserva")
    op.drop_constraint("uq_pedido_item_tenant_id_raza", "pedido_item", type_="unique")
