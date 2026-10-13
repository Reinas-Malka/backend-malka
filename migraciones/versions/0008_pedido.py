"""Pedidos e items de pedido (#34).

Reglas en docs/dominio.md, "Pedidos" y "Modalidades de envio" (11/10), y en
el ADR 0008: el pedido es interno y el sistema calcula sus importes, que no
se guardan.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-11

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Rol de aplicacion sin privilegios, creado en la 0002 (#19).
ROL_APP = "malka_app"

# Ninguna lleva DELETE: un pedido se cancela (#36), no se borra, y sus items
# quedan como registro de lo que se pidio.
PERMISOS_TABLAS_NUEVAS = {
    "pedido": "SELECT, INSERT, UPDATE",
    "pedido_item": "SELECT, INSERT",
}

# Mismos valores que los Enum de app/services/ventas/pedidos.py. Se copian a
# proposito: una migracion no importa codigo de la app.
ESTADOS = ("borrador", "reservado", "entregado", "cancelado")
MONEDAS = ("ARS", "USD", "EUR")
INCOTERMS = (
    "EXW",
    "FCA",
    "CPT",
    "CIP",
    "DAP",
    "DPU",
    "DDP",
    "FAS",
    "FOB",
    "CFR",
    "CIF",
)
ESTADOS_SQL = ", ".join(f"'{estado}'" for estado in ESTADOS)
MONEDAS_SQL = ", ".join(f"'{moneda}'" for moneda in MONEDAS)
INCOTERMS_SQL = ", ".join(f"'{incoterm}'" for incoterm in INCOTERMS)

# Valores del enum Alicuota (app/services/documentos/importes.py), igual que
# en la 0004.
ALICUOTAS = ("0", "0.025", "0.05", "0.105", "0.21", "0.27")
ALICUOTAS_SQL = ", ".join(ALICUOTAS)

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
        "pedido",
        _id(),
        _tenant_id(),
        sa.Column("cliente_id", postgresql.UUID(as_uuid=True), nullable=False),
        # El alta crea siempre en borrador; las transiciones son de #36.
        sa.Column(
            "estado",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'borrador'"),
        ),
        # Sin tipo de cambio: lo fija ARCA al facturar, despues del pedido.
        sa.Column("moneda", sa.Text(), nullable=False),
        # Pais donde recibe el cliente final, no el del hub (ese va en el
        # embarque). ISO 3166-1 alfa-2 como cliente.pais; NULL en nacional.
        sa.Column("destino_pais", sa.Text(), nullable=True),
        # Opcional aun en exportacion: en los envios consolidados queda vacio.
        sa.Column("incoterm", sa.Text(), nullable=True),
        sa.Column("fecha_entrega_solicitada", sa.Date(), nullable=False),
        _creado_en(),
        _actualizado_en(),
        sa.CheckConstraint(f"estado IN ({ESTADOS_SQL})", name="ck_pedido_estado"),
        sa.CheckConstraint(f"moneda IN ({MONEDAS_SQL})", name="ck_pedido_moneda"),
        sa.CheckConstraint(
            f"incoterm IS NULL OR incoterm IN ({INCOTERMS_SQL})",
            name="ck_pedido_incoterm",
        ),
        sa.CheckConstraint(
            "destino_pais IS NULL OR destino_pais ~ '^[A-Z]{2}$'",
            name="ck_pedido_destino_pais",
        ),
        # Un incoterm sin destino seria un pedido nacional con incoterm.
        sa.CheckConstraint(
            "incoterm IS NULL OR destino_pais IS NOT NULL",
            name="ck_pedido_incoterm_con_destino",
        ),
        # Las mismas reglas que validar_pedido, menos el tipo de cliente: un
        # CHECK no puede leer otra tabla. destino_pais marca la exportacion:
        # nacional en ARS, o exportacion fuera de AR en USD o EUR. El IS NOT
        # NULL es necesario: con NULL, <> da NULL y un CHECK en NULL pasa.
        sa.CheckConstraint(
            "(destino_pais IS NULL AND moneda = 'ARS') "
            "OR (destino_pais IS NOT NULL AND destino_pais <> 'AR' "
            "AND moneda IN ('USD', 'EUR'))",
            name="ck_pedido_nacional_o_exportacion",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "cliente_id"],
            ["cliente.tenant_id", "cliente.id"],
            name="fk_pedido_cliente_mismo_tenant",
            ondelete="RESTRICT",
        ),
        # Permite que pedido_item y las reservas (#35) referencien (tenant_id, id).
        sa.UniqueConstraint("tenant_id", "id", name="uq_pedido_tenant_id"),
    )
    op.create_index("ix_pedido_tenant_cliente", "pedido", ["tenant_id", "cliente_id"])
    # Para listar pedidos por estado (#36).
    op.create_index("ix_pedido_tenant_estado", "pedido", ["tenant_id", "estado"])

    op.create_table(
        "pedido_item",
        _id(),
        _tenant_id(),
        sa.Column("pedido_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Orden de la linea dentro del pedido (1, 2, 3...). Hace falta porque
        # creado_en es now(), igual para todas las lineas de una transaccion.
        sa.Column("numero_linea", sa.Integer(), nullable=False),
        # El catalogo no conoce la raza: la combinacion item x raza se define
        # en la linea. #35 usa raza_id para el lock de reservas.
        sa.Column("item_vendible_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("raza_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cantidad", sa.Integer(), nullable=False),
        # En la moneda del pedido. Numeric, nunca float (ADR 0006).
        sa.Column("precio_unitario", sa.Numeric(14, 2), nullable=False),
        # Copia de item_vendible.alicuota al crear la linea: si el catalogo
        # cambia despues, el pedido no se recalcula.
        sa.Column("alicuota", sa.Numeric(5, 3), nullable=False),
        # Sin actualizado_en: la linea no se modifica una vez cargada.
        _creado_en(),
        sa.CheckConstraint("numero_linea > 0", name="ck_pedido_item_numero_linea"),
        # Mismas reglas que Linea en importes.py: el precio cero es valido.
        sa.CheckConstraint("cantidad > 0", name="ck_pedido_item_cantidad"),
        sa.CheckConstraint("precio_unitario >= 0", name="ck_pedido_item_precio"),
        sa.CheckConstraint(
            f"alicuota IN ({ALICUOTAS_SQL})", name="ck_pedido_item_alicuota"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "pedido_id"],
            ["pedido.tenant_id", "pedido.id"],
            name="fk_pedido_item_pedido_mismo_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "item_vendible_id"],
            ["item_vendible.tenant_id", "item_vendible.id"],
            name="fk_pedido_item_item_vendible_mismo_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "raza_id"],
            ["raza.tenant_id", "raza.id"],
            name="fk_pedido_item_raza_mismo_tenant",
            ondelete="RESTRICT",
        ),
        # Dos lineas de un mismo pedido no pueden tener el mismo numero. El
        # indice que crea tambien sirve para leer las lineas de un pedido en
        # orden, asi que no hace falta otro indice por (tenant_id, pedido_id).
        sa.UniqueConstraint(
            "tenant_id",
            "pedido_id",
            "numero_linea",
            name="uq_pedido_item_tenant_pedido_numero_linea",
        ),
    )

    for tabla, permisos in PERMISOS_TABLAS_NUEVAS.items():
        _aislar_por_tenant(tabla, permisos)


def downgrade() -> None:
    # Las politicas, los indices y los permisos se van con cada tabla.
    op.drop_table("pedido_item")
    op.drop_table("pedido")
