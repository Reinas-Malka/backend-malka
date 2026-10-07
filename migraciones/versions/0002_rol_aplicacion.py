"""Rol de aplicacion sin privilegios para la API (#19).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-06

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROL_APP = "malka_app"

TENANT_ACTUAL = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"

# Permisos minimos por tabla.
PERMISOS = {
    "raza": "SELECT, INSERT, UPDATE",
    "tanda": "SELECT, INSERT, UPDATE",
    "tanda_evento": "SELECT, INSERT",
}

# Los roles son de todo el servidor, no de una base: si ya existe de una
# ejecucion anterior, no se vuelve a crear. Sin clave: se fija despues.
CREAR_ROL = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'malka_app') THEN
        CREATE ROLE malka_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB
            NOCREATEROLE NOINHERIT NOREPLICATION;
    END IF;
END
$$
"""


def upgrade() -> None:
    op.execute(CREAR_ROL)
    op.execute(f"GRANT USAGE ON SCHEMA public TO {ROL_APP}")
    for tabla, permisos in PERMISOS.items():
        op.execute(f"GRANT {permisos} ON {tabla} TO {ROL_APP}")
        # Los servicios no mandan tenant_id al insertar: lo completa Postgres
        # con el tenant de la transaccion, y WITH CHECK lo valida.
        op.execute(
            f"ALTER TABLE {tabla} ALTER COLUMN tenant_id SET DEFAULT {TENANT_ACTUAL}"
        )


def downgrade() -> None:
    for tabla in PERMISOS:
        op.execute(f"ALTER TABLE {tabla} ALTER COLUMN tenant_id DROP DEFAULT")
    op.execute(f"REVOKE ALL ON {', '.join(PERMISOS)} FROM {ROL_APP}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {ROL_APP}")
    op.execute(f"DROP ROLE IF EXISTS {ROL_APP}")
