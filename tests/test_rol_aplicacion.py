"""Rol de aplicacion y SET LOCAL contra un PostgreSQL real (#19)."""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from app import db
from tests.conftest import crear_tenant, fijar_tenant

pytestmark = pytest.mark.integration


class _Deshacer(Exception):
    """Corta la sesion para que haga rollback."""


@pytest.fixture(scope="module")
def tenants(motor_owner: Engine) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """Dos criaderos con una raza cada uno, cargados como duenio."""
    a, b = uuid.uuid4(), uuid.uuid4()
    with motor_owner.begin() as c:
        for tenant in (a, b):
            crear_tenant(c, tenant)
            # FORCE RLS: ni el duenio escribe sin tenant fijado.
            fijar_tenant(c, tenant)
            c.execute(
                text("INSERT INTO raza (nombre) VALUES (:nombre)"),
                {"nombre": f"Carniola {tenant}"},
            )
    yield a, b
    with motor_owner.begin() as c:
        for tenant in (a, b):
            fijar_tenant(c, tenant)
            c.execute(text("DELETE FROM raza WHERE true"))
        c.execute(text("DELETE FROM tenant WHERE id IN (:a, :b)"), {"a": a, "b": b})


@pytest.mark.usefixtures("tenants")
def test_query_sin_set_local_devuelve_cero_filas(motor_app: Engine) -> None:
    """DoD del #19: hay razas cargadas, pero sin tenant fijado no se ve ninguna."""
    with motor_app.connect() as c:
        assert c.execute(text("SELECT count(*) FROM raza")).scalar() == 0


@pytest.mark.usefixtures("con_motor_app")
def test_con_set_local_ve_solo_su_tenant(tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    a, _ = tenants
    with db.sesion_de_tenant(a) as s:
        nombres = s.execute(text("SELECT nombre FROM raza")).scalars().all()
    assert nombres == [f"Carniola {a}"]


@pytest.mark.usefixtures("con_motor_app")
def test_el_tenant_no_queda_en_la_conexion(
    motor_app: Engine, tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = tenants
    with db.sesion_de_tenant(a) as s:
        s.execute(text("SELECT 1"))
    # pool_size=1: es la misma conexion que acaba de usar la sesion.
    with motor_app.connect() as c:
        valor = c.execute(text("SELECT current_setting('app.tenant_id', true)"))
        assert valor.scalar() in (None, "")
        assert c.execute(text("SELECT count(*) FROM raza")).scalar() == 0


@pytest.mark.usefixtures("con_motor_app")
def test_tenant_id_lo_completa_postgres(tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    a, _ = tenants
    with pytest.raises(_Deshacer), db.sesion_de_tenant(a) as s:
        insertado = s.execute(
            text("INSERT INTO raza (nombre) VALUES ('Buckfast') RETURNING tenant_id")
        ).scalar()
        assert insertado == a
        raise _Deshacer


@pytest.mark.usefixtures("con_motor_app")
def test_no_puede_escribir_en_otro_tenant(tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Fuga entre tenants (AGENTS.md, regla 5): WITH CHECK lo rechaza."""
    a, b = tenants
    with pytest.raises(DBAPIError, match="row-level security"):
        with db.sesion_de_tenant(a) as s:
            s.execute(
                text("INSERT INTO raza (tenant_id, nombre) VALUES (:b, 'intrusa')"),
                {"b": b},
            )


def test_rol_app_sin_privilegios(motor_owner: Engine) -> None:
    with motor_owner.connect() as c:
        rol = c.execute(
            text(
                "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb "
                "FROM pg_roles WHERE rolname = 'malka_app'"
            )
        ).one()
        assert tuple(rol) == (False, False, False, False)

        duenios = c.execute(
            text(
                "SELECT DISTINCT tableowner FROM pg_tables "
                "WHERE schemaname = 'public'"
            )
        ).scalars()
        assert "malka_app" not in set(duenios)

        privilegio = "SELECT has_table_privilege('malka_app', :tabla, :permiso)"
        # tenant no tiene RLS: el rol no puede ni leerla.
        assert not c.execute(
            text(privilegio), {"tabla": "tenant", "permiso": "SELECT"}
        ).scalar()
        # raza usa borrado logico: sin DELETE.
        assert not c.execute(
            text(privilegio), {"tabla": "raza", "permiso": "DELETE"}
        ).scalar()


def test_toda_tabla_con_tenant_id_tiene_rls_y_permisos(motor_owner: Engine) -> None:
    """Guardia para las tablas que vienen (#26, #32...): si una migracion nueva
    olvida FORCE RLS, la politica o el GRANT a malka_app, falla aca."""
    consulta = text(
        """
        SELECT c.relname,
               c.relrowsecurity AND c.relforcerowsecurity AS rls_forzado,
               EXISTS (SELECT 1 FROM pg_policies p
                       WHERE p.schemaname = 'public'
                         AND p.tablename = c.relname) AS tiene_politica,
               has_table_privilege('malka_app', c.oid, 'SELECT') AS app_lee
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid
        WHERE n.nspname = 'public' AND c.relkind = 'r'
          AND a.attname = 'tenant_id' AND NOT a.attisdropped
        """
    )
    with motor_owner.connect() as c:
        filas = c.execute(consulta).all()

    assert "raza" in {f.relname for f in filas}
    incompletas = [
        f.relname
        for f in filas
        if not (f.rls_forzado and f.tiene_politica and f.app_lee)
    ]
    assert incompletas == []
