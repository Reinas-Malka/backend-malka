"""El nombre del criadero actual, para el borrador con IA (#40).

El rol de la API (malka_app) no lee la tabla tenant: no tiene RLS y guarda
el CUIT. En vez de abrirla, una funcion SECURITY DEFINER devuelve solo el
nombre del criadero fijado en la sesion (app.tenant_id, el mismo de las
politicas RLS). Sin tenant fijado devuelve NULL; nunca el de otro criadero.

- search_path fijo: nadie puede colar una tabla tenant propia.
- EXECUTE solo para malka_app (se revoca de PUBLIC).

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-02

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROL_APP = "malka_app"


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION nombre_del_criadero() RETURNS text
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $$
            SELECT nombre FROM public.tenant
            WHERE id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION nombre_del_criadero() FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION nombre_del_criadero() TO {ROL_APP}")


def downgrade() -> None:
    op.execute("DROP FUNCTION nombre_del_criadero()")
