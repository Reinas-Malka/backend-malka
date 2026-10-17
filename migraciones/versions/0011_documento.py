"""Documentos subidos a S3: los datos de cada archivo (#102).

Reglas en docs/dominio.md ("Facturacion": flujo real) y la retencion por
prefijo: los comprobantes y documentos se emiten por fuera del sistema, el
PDF se sube a S3 bajo documentos/ y el sistema registra sus datos.

- Una fila por archivo pedido para subir. El archivo lo sube el navegador
  directo a S3 con una URL prefirmada: la API nunca recibe los bytes.
- La key lleva el tenant en el prefijo (documentos/<tenant_id>/...), igual
  que documento_embarque (0010): un CHECK lo exige.
- Sin UPDATE ni DELETE: el registro de lo que se subio no se edita. La
  retencion (5 anios) la hace el lifecycle del bucket, no la base.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Rol de aplicacion sin privilegios, creado en la 0002 (#19).
ROL_APP = "malka_app"
PERMISOS = "SELECT, INSERT"

# Mismos valores que app/services/documentos/almacenamiento.py. Se copian a
# proposito: una migracion no importa codigo de la app.
TIPOS = ("application/pdf", "image/jpeg", "image/png")
TIPOS_SQL = ", ".join(f"'{tipo}'" for tipo in TIPOS)
TAMANO_MAXIMO = 10 * 1024 * 1024

# Misma expresion que la 0001: sin app.tenant_id definido, cero filas.
TENANT_ACTUAL = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "documento",
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
        sa.Column("archivo_s3_key", sa.Text(), nullable=False),
        # El nombre con el que lo eligio la persona, para mostrarlo.
        sa.Column("nombre", sa.Text(), nullable=False),
        sa.Column("tipo_contenido", sa.Text(), nullable=False),
        sa.Column("tamano_bytes", sa.Integer(), nullable=False),
        # El sub de Cognito de quien lo subio (cargado_por del dominio). No es
        # el emisor fiscal ni un email.
        sa.Column("subido_por", sa.Text(), nullable=False),
        sa.Column(
            "creado_en",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            f"tipo_contenido IN ({TIPOS_SQL})", name="ck_documento_tipo_contenido"
        ),
        sa.CheckConstraint(
            f"tamano_bytes > 0 AND tamano_bytes <= {TAMANO_MAXIMO}",
            name="ck_documento_tamano",
        ),
        sa.CheckConstraint(
            "archivo_s3_key LIKE 'documentos/' || tenant_id::text || '/%' "
            "AND position('..' IN archivo_s3_key) = 0",
            name="ck_documento_archivo_del_tenant",
        ),
        sa.UniqueConstraint(
            "tenant_id", "archivo_s3_key", name="uq_documento_tenant_archivo"
        ),
    )

    op.execute("ALTER TABLE documento ENABLE ROW LEVEL SECURITY")
    # FORCE: las politicas se aplican tambien al duenio de la tabla.
    op.execute("ALTER TABLE documento FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY aislamiento_por_tenant ON documento "
        f"USING (tenant_id = {TENANT_ACTUAL}) "
        f"WITH CHECK (tenant_id = {TENANT_ACTUAL})"
    )
    op.execute(f"GRANT {PERMISOS} ON documento TO {ROL_APP}")
    # Sin SET LOCAL el default es NULL y el NOT NULL rechaza el insert.
    op.execute(
        f"ALTER TABLE documento ALTER COLUMN tenant_id SET DEFAULT {TENANT_ACTUAL}"
    )


def downgrade() -> None:
    # La politica, el permiso y los indices se van con la tabla.
    op.drop_table("documento")
