"""Comprobantes fiscales transcriptos: Factura A, B y E (#108).

Reglas en docs/dominio.md ("Facturacion" y "Modelo de datos"):
- El sistema no emite: tipo, punto_venta, numero, moneda, importe y
  tipo_cambio se transcriben del comprobante que emitio ARCA.
- punto_venta y numero son dos enteros, nunca el texto impreso
  "0001-00000045": el relleno con ceros se arma al mostrar.
- UNIQUE (tenant_id, tipo, punto_venta, numero): espeja la clave natural de
  ARCA (CUIT emisor, tipo, punto de venta, numero). El CUIT es del tenant.
- tipo_cambio es NULL si y solo si la moneda es ARS. ARS va con A o B; USD o
  EUR, con E (no hay ventas nacionales en moneda extranjera, 11/10).
- El PDF ya esta en S3: archivo_s3_key apunta a un documento registrado del
  mismo criadero (#102, migracion 0011).
- cargado_por es el sub de Cognito de quien lo registro: auditoria, no el
  emisor fiscal.
- Sin UPDATE ni DELETE: lo transcripto es inmutable. Un error se corrige en
  ARCA (nota de credito) y se registra como otro comprobante.

La coherencia de la letra con la condicion del cliente la valida el
servicio (422): un CHECK no puede leer la tabla cliente.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-29

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Rol de aplicacion sin privilegios, creado en la 0002 (#19).
ROL_APP = "malka_app"
PERMISOS = "SELECT, INSERT"

# Mismos valores que app/services/facturacion/comprobantes.py y que la Moneda
# de los pedidos. Se copian a proposito: una migracion no importa la app.
TIPOS = ("A", "B", "E")
TIPOS_SQL = ", ".join(f"'{tipo}'" for tipo in TIPOS)
MONEDAS = ("ARS", "USD", "EUR")
MONEDAS_SQL = ", ".join(f"'{moneda}'" for moneda in MONEDAS)
# Topes de ARCA: punto de venta de hasta 5 digitos y numero de hasta 8.
PUNTO_VENTA_MAXIMO = 99_999
NUMERO_MAXIMO = 99_999_999

# Misma expresion que la 0001: sin app.tenant_id definido, cero filas.
TENANT_ACTUAL = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "comprobante",
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
        sa.Column("tipo", sa.Text(), nullable=False),
        sa.Column("cliente_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("punto_venta", sa.Integer(), nullable=False),
        sa.Column("numero", sa.Integer(), nullable=False),
        sa.Column("fecha_emision", sa.Date(), nullable=False),
        sa.Column("moneda", sa.Text(), nullable=False),
        sa.Column("importe", sa.Numeric(14, 2), nullable=False),
        # La cotizacion de ARCA trae hasta 6 decimales.
        sa.Column("tipo_cambio", sa.Numeric(16, 6), nullable=True),
        sa.Column("archivo_s3_key", sa.Text(), nullable=False),
        sa.Column("cargado_por", sa.Text(), nullable=False),
        sa.Column(
            "creado_en",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(f"tipo IN ({TIPOS_SQL})", name="ck_comprobante_tipo"),
        sa.CheckConstraint(f"moneda IN ({MONEDAS_SQL})", name="ck_comprobante_moneda"),
        sa.CheckConstraint(
            "(tipo = 'E') = (moneda <> 'ARS')", name="ck_comprobante_moneda_segun_tipo"
        ),
        sa.CheckConstraint(
            "(tipo_cambio IS NULL) = (moneda = 'ARS') "
            "AND (tipo_cambio IS NULL OR tipo_cambio > 0)",
            name="ck_comprobante_tipo_cambio",
        ),
        sa.CheckConstraint(
            f"punto_venta BETWEEN 1 AND {PUNTO_VENTA_MAXIMO}",
            name="ck_comprobante_punto_venta",
        ),
        sa.CheckConstraint(
            f"numero BETWEEN 1 AND {NUMERO_MAXIMO}", name="ck_comprobante_numero"
        ),
        sa.CheckConstraint("importe > 0", name="ck_comprobante_importe"),
        sa.UniqueConstraint(
            "tenant_id",
            "tipo",
            "punto_venta",
            "numero",
            name="uq_comprobante_tenant_tipo_punto_venta_numero",
        ),
        # Mismo criadero que el cliente (uq_cliente_tenant_id, 0005).
        sa.ForeignKeyConstraint(
            ["tenant_id", "cliente_id"],
            ["cliente.tenant_id", "cliente.id"],
            name="fk_comprobante_cliente_mismo_tenant",
            ondelete="RESTRICT",
        ),
        # El PDF esta registrado en el mismo criadero (uq_documento_tenant_
        # archivo, 0011). Ese registro ya exige el prefijo documentos/<tenant>/.
        sa.ForeignKeyConstraint(
            ["tenant_id", "archivo_s3_key"],
            ["documento.tenant_id", "documento.archivo_s3_key"],
            name="fk_comprobante_documento_mismo_tenant",
            ondelete="RESTRICT",
        ),
    )
    # El listado va de la emision mas nueva a la mas vieja.
    op.create_index(
        "ix_comprobante_tenant_fecha_emision",
        "comprobante",
        ["tenant_id", "fecha_emision", "id"],
    )
    op.create_index(
        "ix_comprobante_tenant_cliente", "comprobante", ["tenant_id", "cliente_id"]
    )

    op.execute("ALTER TABLE comprobante ENABLE ROW LEVEL SECURITY")
    # FORCE: las politicas se aplican tambien al duenio de la tabla.
    op.execute("ALTER TABLE comprobante FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY aislamiento_por_tenant ON comprobante "
        f"USING (tenant_id = {TENANT_ACTUAL}) "
        f"WITH CHECK (tenant_id = {TENANT_ACTUAL})"
    )
    op.execute(f"GRANT {PERMISOS} ON comprobante TO {ROL_APP}")
    # Sin SET LOCAL el default es NULL y el NOT NULL rechaza el insert.
    op.execute(
        f"ALTER TABLE comprobante ALTER COLUMN tenant_id SET DEFAULT {TENANT_ACTUAL}"
    )


def downgrade() -> None:
    # La politica, los indices y el permiso se van con la tabla.
    op.drop_table("comprobante")
