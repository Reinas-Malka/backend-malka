"""Clientes nacionales y de exportacion (#32).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-07

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Rol de aplicacion sin privilegios, creado en la 0002 (#19).
ROL_APP = "malka_app"

# Mismos valores que los Enum de app/services/ventas/clientes.py. Se copian a
# proposito: una migracion no importa codigo de la app, porque la app cambia
# y la migracion tiene que dar siempre el mismo resultado.
TIPOS = ("nacional", "exportacion")
CONDICIONES_IVA = (
    "responsable_inscripto",
    "monotributo",
    "exento",
    "consumidor_final",
    "cliente_exterior",
)
TIPOS_SQL = ", ".join(f"'{tipo}'" for tipo in TIPOS)
CONDICIONES_IVA_SQL = ", ".join(f"'{condicion}'" for condicion in CONDICIONES_IVA)

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
    op.create_table(
        "cliente",
        _id(),
        _tenant_id(),
        sa.Column("nombre", sa.Text(), nullable=False),
        # ISO 3166-1 alfa-2: el schema lo pasa a mayusculas antes de guardar.
        sa.Column("pais", sa.Text(), nullable=False),
        sa.Column("tipo", sa.Text(), nullable=False),
        sa.Column("condicion_iva", sa.Text(), nullable=False),
        # CUIT sin guiones si es nacional; tax id libre si es de exportacion.
        sa.Column("cuit_o_tax_id", sa.Text(), nullable=False),
        # Borrado logico: un cliente con pedidos o comprobantes no se borra.
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.true()),
        _creado_en(),
        _actualizado_en(),
        sa.CheckConstraint("pais ~ '^[A-Z]{2}$'", name="ck_cliente_pais"),
        sa.CheckConstraint(f"tipo IN ({TIPOS_SQL})", name="ck_cliente_tipo"),
        sa.CheckConstraint(
            f"condicion_iva IN ({CONDICIONES_IVA_SQL})",
            name="ck_cliente_condicion_iva",
        ),
        # Las mismas reglas que valida ClienteCrear. En el flujo normal nunca
        # saltan: cubren lo que no pasa por la API. El digito verificador del
        # CUIT queda solo en Python.
        sa.CheckConstraint(
            "tipo <> 'nacional' OR ("
            "pais = 'AR' "
            "AND condicion_iva <> 'cliente_exterior' "
            "AND cuit_o_tax_id ~ '^[0-9]{11}$')",
            name="ck_cliente_nacional",
        ),
        sa.CheckConstraint(
            "tipo <> 'exportacion' OR ("
            "pais <> 'AR' AND condicion_iva = 'cliente_exterior')",
            name="ck_cliente_exportacion",
        ),
        # Un CUIT o tax id por criadero. Otro criadero puede tener al mismo
        # cliente: RLS no deja que se vean entre ellos.
        sa.UniqueConstraint(
            "tenant_id", "cuit_o_tax_id", name="uq_cliente_tenant_cuit_o_tax_id"
        ),
        # Permite que pedido (#34) y comprobante referencien (tenant_id, id).
        sa.UniqueConstraint("tenant_id", "id", name="uq_cliente_tenant_id"),
    )
    # Sin DELETE: el cliente se desactiva con activo = false.
    _aislar_por_tenant("cliente", "SELECT, INSERT, UPDATE")


def downgrade() -> None:
    # Con la tabla se van sus restricciones, su politica y sus permisos.
    op.drop_table("cliente")
