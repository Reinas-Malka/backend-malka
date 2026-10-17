"""Embarques y sus documentos (#109).

Reglas en docs/dominio.md, "Exportacion" y "Modalidades de envio" (11/10):
la entidad central es el embarque y el sistema responde "tengo los 6
documentos de este envio?" (la demo del 30/11).

- destino_pais es el pais al que vuela el embarque: el del cliente en los
  envios directos, el del hub en los consolidados. Nunca AR. Las dos
  modalidades se guardan igual: no hay columna ni logica por modalidad, y
  los sub-envios no se registran.
- Un documento por tipo y por embarque: la completitud es "estan los 6
  tipos". El numero se transcribe del documento real y no se edita.
- archivo_s3_key apunta al PDF subido a S3. La key lleva el tenant en el
  prefijo (documentos/<tenant_id>/...): un CHECK impide registrar un
  archivo de otro criadero aunque se conozca su key.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Rol de aplicacion sin privilegios, creado en la 0002 (#19).
ROL_APP = "malka_app"

# Sin DELETE: un embarque se cancela, no se borra. El documento no lleva
# UPDATE: lo transcripto queda como se cargo, igual que pedido_item.
PERMISOS_TABLAS_NUEVAS = {
    "embarque": "SELECT, INSERT, UPDATE",
    "documento_embarque": "SELECT, INSERT",
}

# Mismos valores que los Enum de app/services/exportacion/embarques.py. Se
# copian a proposito: una migracion no importa codigo de la app.
ESTADOS = ("en_preparacion", "en_transito", "entregado", "cancelado")
TIPOS_DOCUMENTO = (
    "factura_e",
    "permiso_embarque",
    "guia_aerea",
    "lista_empaque",
    "dte",
    "shipper_certificate",
)
ESTADOS_SQL = ", ".join(f"'{estado}'" for estado in ESTADOS)
TIPOS_SQL = ", ".join(f"'{tipo}'" for tipo in TIPOS_DOCUMENTO)

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
        "embarque",
        _id(),
        _tenant_id(),
        # ISO 3166-1 alfa-2 como pedido.destino_pais. Directo: pais del
        # cliente; consolidado: pais del hub.
        sa.Column("destino_pais", sa.Text(), nullable=False),
        sa.Column("fecha_vuelo", sa.Date(), nullable=False),
        # NULL mientras no se reserva el vuelo. Formato IATA NNN-NNNNNNNN;
        # el digito verificador lo valida el schema.
        sa.Column("awb", sa.Text(), nullable=True),
        sa.Column(
            "estado",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'en_preparacion'"),
        ),
        _creado_en(),
        _actualizado_en(),
        sa.CheckConstraint(
            "destino_pais ~ '^[A-Z]{2}$' AND destino_pais <> 'AR'",
            name="ck_embarque_destino_pais",
        ),
        sa.CheckConstraint(
            "awb IS NULL OR awb ~ '^[0-9]{3}-[0-9]{8}$'", name="ck_embarque_awb"
        ),
        sa.CheckConstraint(f"estado IN ({ESTADOS_SQL})", name="ck_embarque_estado"),
        # Sin guia aerea el envio no puede haber salido.
        sa.CheckConstraint(
            "awb IS NOT NULL OR estado IN ('en_preparacion', 'cancelado')",
            name="ck_embarque_awb_al_despachar",
        ),
        # Una guia aerea identifica un solo envio. Los NULL no chocan entre si.
        sa.UniqueConstraint("tenant_id", "awb", name="uq_embarque_tenant_awb"),
        # Permite que documento_embarque referencie (tenant_id, id).
        sa.UniqueConstraint("tenant_id", "id", name="uq_embarque_tenant_id"),
    )
    # GET /embarques ordena por (fecha_vuelo DESC, id DESC) y pagina por
    # cursor sobre ese orden.
    op.create_index(
        "ix_embarque_tenant_fecha_vuelo",
        "embarque",
        ["tenant_id", sa.text("fecha_vuelo DESC"), sa.text("id DESC")],
    )

    op.create_table(
        "documento_embarque",
        _id(),
        _tenant_id(),
        sa.Column("embarque_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tipo", sa.Text(), nullable=False),
        # Transcripto del documento real (la factura E sale de ARCA).
        sa.Column("numero", sa.Text(), nullable=False),
        sa.Column("archivo_s3_key", sa.Text(), nullable=False),
        # Fecha de emision del documento.
        sa.Column("fecha", sa.Date(), nullable=False),
        # Sin actualizado_en: el documento no se modifica una vez cargado.
        _creado_en(),
        sa.CheckConstraint(f"tipo IN ({TIPOS_SQL})", name="ck_documento_embarque_tipo"),
        sa.CheckConstraint("btrim(numero) <> ''", name="ck_documento_embarque_numero"),
        # El archivo tiene que estar bajo el prefijo del propio criadero, y
        # sin ".." que permita salir de el.
        sa.CheckConstraint(
            "archivo_s3_key LIKE 'documentos/' || tenant_id::text || '/%' "
            "AND position('..' IN archivo_s3_key) = 0",
            name="ck_documento_embarque_archivo_del_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "embarque_id"],
            ["embarque.tenant_id", "embarque.id"],
            name="fk_documento_embarque_embarque_mismo_tenant",
            ondelete="RESTRICT",
        ),
        # Un documento de cada tipo por embarque. El indice tambien sirve
        # para leer los documentos de un embarque.
        sa.UniqueConstraint(
            "tenant_id",
            "embarque_id",
            "tipo",
            name="uq_documento_embarque_tenant_embarque_tipo",
        ),
    )

    for tabla, permisos in PERMISOS_TABLAS_NUEVAS.items():
        _aislar_por_tenant(tabla, permisos)


def downgrade() -> None:
    # Las politicas, los indices y los permisos se van con cada tabla.
    op.drop_table("documento_embarque")
    op.drop_table("embarque")
