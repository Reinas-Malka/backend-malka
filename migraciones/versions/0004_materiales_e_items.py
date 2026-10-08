"""Materiales, movimientos de material y catalogo de items vendibles (#30).

Reglas en docs/dominio.md:
- "Materiales y consumo": el stock nunca es una columna, el stock negativo se
  permite y la unidad vive en el material como texto corto.
- Notas al modelo: la alicuota de IVA vive en el catalogo de items vendibles,
  con los valores del enum Alicuota de app/services/documentos/importes.py.

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

# Permisos minimos del rol de aplicacion. Ninguna lleva DELETE:
# - material usa borrado logico (tiene movimientos historicos).
# - movimiento_material es un registro: no se edita ni se borra. Un error se
#   corrige con un movimiento de ajuste, asi el stock siempre tiene historia.
PERMISOS_TABLAS_NUEVAS = {
    "material": "SELECT, INSERT, UPDATE",
    "movimiento_material": "SELECT, INSERT",
    "item_vendible": "SELECT, INSERT, UPDATE",
}

# Valores del enum Alicuota (app/services/documentos/importes.py). Una
# migracion no importa codigo de la app: si el enum cambia, va otra migracion.
# tests/test_materiales.py verifica que coincidan.
ALICUOTAS = ("0", "0.025", "0.05", "0.105", "0.21", "0.27")
ALICUOTAS_SQL = ", ".join(ALICUOTAS)
# Default para reinas: 21%, NO VERIFICADO (la clienta dijo "creo"; falta
# confirmarlo con una factura vieja). Se puede cambiar por item.
ALICUOTA_POR_DEFECTO = "0.21"

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
    # --- material: la cupula suelta, como se compra ---
    # La multiplicacion por cupulas por cuadro vive en el servicio de alta de
    # tanda (#27), nunca en esta tabla.
    op.create_table(
        "material",
        _id(),
        _tenant_id(),
        sa.Column("nombre", sa.Text(), nullable=False),
        # Texto corto, sin enum cerrado (docs/dominio.md).
        sa.Column("unidad", sa.Text(), nullable=False),
        # Borrado logico: un material con movimientos no se borra.
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.true()),
        _creado_en(),
        _actualizado_en(),
        sa.CheckConstraint(
            "length(btrim(unidad)) BETWEEN 1 AND 20", name="ck_material_unidad"
        ),
        sa.UniqueConstraint("tenant_id", "nombre", name="uq_material_tenant_nombre"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_material_tenant_id"),
    )

    # --- movimiento_material: el stock es SUM(cantidad), nunca una columna ---
    # Sin constraint de stock negativo: es una suma calculada y validarla
    # obligaria a lockear la tabla; ademas los movimientos se cargan tarde y
    # bloquear el consumo impediria registrar la tanda. El alta devuelve una
    # advertencia (#27).
    op.create_table(
        "movimiento_material",
        _id(),
        _tenant_id(),
        sa.Column("material_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tipo", sa.Text(), nullable=False),
        # Con signo: compra positiva, consumo negativa, ajuste cualquiera.
        # Numeric porque la unidad es libre (cupulas, kg, litros).
        sa.Column("cantidad", sa.Numeric(12, 3), nullable=False),
        # Solo en consumos: la tanda que lo genero (alta de tanda, #27).
        sa.Column("tanda_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("motivo", sa.Text(), nullable=True),
        # Dia del movimiento, que puede ser anterior a la carga (se cargan tarde).
        sa.Column(
            "fecha",
            sa.Date(),
            nullable=False,
            server_default=sa.text("CURRENT_DATE"),
        ),
        # Sin actualizado_en: el movimiento no se modifica una vez cargado.
        _creado_en(),
        sa.CheckConstraint(
            "tipo IN ('compra', 'consumo', 'ajuste')", name="ck_movimiento_tipo"
        ),
        # Coherencia del signo con el tipo.
        sa.CheckConstraint(
            "(tipo = 'compra' AND cantidad > 0) "
            "OR (tipo = 'consumo' AND cantidad < 0) "
            "OR (tipo = 'ajuste' AND cantidad <> 0)",
            name="ck_movimiento_signo",
        ),
        sa.CheckConstraint(
            "tanda_id IS NULL OR tipo = 'consumo'",
            name="ck_movimiento_tanda_solo_en_consumo",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "material_id"],
            ["material.tenant_id", "material.id"],
            name="fk_movimiento_material_mismo_tenant",
            ondelete="RESTRICT",
        ),
        # Con tanda_id en NULL la clave foranea no se verifica (MATCH SIMPLE).
        sa.ForeignKeyConstraint(
            ["tenant_id", "tanda_id"],
            ["tanda.tenant_id", "tanda.id"],
            name="fk_movimiento_tanda_mismo_tenant",
            ondelete="RESTRICT",
        ),
    )
    # Para calcular el stock de un material (SUM por material_id).
    op.create_index(
        "ix_movimiento_tenant_material",
        "movimiento_material",
        ["tenant_id", "material_id"],
    )
    op.create_index(
        "ix_movimiento_tenant_tanda",
        "movimiento_material",
        ["tenant_id", "tanda_id"],
    )

    # --- item_vendible: lo que se factura, con su alicuota de IVA ---
    # La alicuota vive aca y no en el cliente (docs/dominio.md).
    op.create_table(
        "item_vendible",
        _id(),
        _tenant_id(),
        sa.Column("nombre", sa.Text(), nullable=False),
        sa.Column(
            "alicuota",
            sa.Numeric(5, 3),
            nullable=False,
            server_default=sa.text(ALICUOTA_POR_DEFECTO),
        ),
        # Borrado logico: un item ya facturado no se borra.
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.true()),
        _creado_en(),
        _actualizado_en(),
        sa.CheckConstraint(
            f"alicuota IN ({ALICUOTAS_SQL})", name="ck_item_vendible_alicuota"
        ),
        sa.UniqueConstraint(
            "tenant_id", "nombre", name="uq_item_vendible_tenant_nombre"
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_item_vendible_tenant_id"),
    )

    for tabla, permisos in PERMISOS_TABLAS_NUEVAS.items():
        _aislar_por_tenant(tabla, permisos)


def downgrade() -> None:
    # Las politicas, los indices y los permisos se van con cada tabla.
    op.drop_table("item_vendible")
    op.drop_table("movimiento_material")
    op.drop_table("material")
