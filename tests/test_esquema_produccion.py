"""Esquema de produccion de la migracion 0003 contra un PostgreSQL real (#26).

Cubre el DoD de la #26: aislamiento por tenant en cada tabla nueva (madre,
parque, nucleo y celda), incluido el caso de fuga entre tenants,
y las reglas de docs/dominio.md que hace cumplir la propia base.
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app import db
from tests.conftest import crear_tenant, fijar_tenant

pytestmark = pytest.mark.integration

TABLAS_NUEVAS = ("madre", "parque", "nucleo", "celda")
CELDAS_POR_TANDA = 3


class _Deshacer(Exception):
    """Corta la sesion para que haga rollback."""


@dataclass(frozen=True)
class Criadero:
    """Un tenant con una tanda de celdas y un parque con un nucleo."""

    tenant: uuid.UUID
    tanda: uuid.UUID
    parque: uuid.UUID
    nucleo: uuid.UUID


def _cargar_criadero(c: Connection, tenant: uuid.UUID) -> Criadero:
    """Carga los datos como duenio: con FORCE RLS necesita el tenant fijado."""
    crear_tenant(c, tenant)
    fijar_tenant(c, tenant)

    def insertar(sql: str, **params: object) -> uuid.UUID:
        valor = c.execute(text(sql), params).scalar_one()
        assert isinstance(valor, uuid.UUID)
        return valor

    raza = insertar("INSERT INTO raza (nombre) VALUES ('Italiana') RETURNING id")
    madre = insertar("INSERT INTO madre (identificacion) VALUES ('M-01') RETURNING id")
    tanda = insertar(
        "INSERT INTO tanda (raza_id, madre_id, fecha_traslarve) "
        "VALUES (:raza, :madre, '2026-10-05') RETURNING id",
        raza=raza,
        madre=madre,
    )
    parque = insertar(
        "INSERT INTO parque (nombre, ubicacion, capacidad_max) "
        "VALUES ('P1', 'Predio norte', 9) RETURNING id"
    )
    nucleo = insertar(
        "INSERT INTO nucleo (parque_id, fila, posicion) "
        "VALUES (:parque, 1, 1) RETURNING id",
        parque=parque,
    )
    c.execute(
        text(
            "INSERT INTO celda (tanda_id) "
            "SELECT :tanda FROM generate_series(1, :cantidad)"
        ),
        {"tanda": tanda, "cantidad": CELDAS_POR_TANDA},
    )
    return Criadero(tenant=tenant, tanda=tanda, parque=parque, nucleo=nucleo)


@pytest.fixture(scope="module")
def criaderos(motor_owner: Engine) -> Iterator[tuple[Criadero, Criadero]]:
    """Dos criaderos con los mismos datos, para probar que no se mezclan."""
    with motor_owner.begin() as c:
        a = _cargar_criadero(c, uuid.uuid4())
    with motor_owner.begin() as c:
        b = _cargar_criadero(c, uuid.uuid4())
    yield a, b
    # El duenio puede borrar (la API no). Se borra de hijas a padres.
    for criadero in (a, b):
        with motor_owner.begin() as c:
            fijar_tenant(c, criadero.tenant)
            for tabla in (
                "celda",
                "nucleo",
                "parque",
                "tanda",
                "madre",
                "raza",
            ):
                c.execute(text(f"DELETE FROM {tabla} WHERE true"))  # noqa: S608
            c.execute(
                text("DELETE FROM tenant WHERE id = :id"), {"id": criadero.tenant}
            )


@pytest.mark.usefixtures("criaderos")
@pytest.mark.parametrize("tabla", TABLAS_NUEVAS)
def test_sin_set_local_cero_filas(motor_app: Engine, tabla: str) -> None:
    with motor_app.connect() as c:
        total = c.execute(text(f"SELECT count(*) FROM {tabla}")).scalar()  # noqa: S608
    assert total == 0


@pytest.mark.usefixtures("con_motor_app")
@pytest.mark.parametrize("tabla", TABLAS_NUEVAS)
def test_cada_tenant_ve_solo_lo_suyo(
    criaderos: tuple[Criadero, Criadero], tabla: str
) -> None:
    """Fuga entre tenants (AGENTS.md, regla 5): B no ve nada de A."""
    for criadero in criaderos:
        with db.sesion_de_tenant(criadero.tenant) as s:
            tenants = s.execute(
                text(f"SELECT DISTINCT tenant_id FROM {tabla}")  # noqa: S608
            ).scalars()
            assert set(tenants) == {criadero.tenant}


@pytest.mark.usefixtures("con_motor_app")
@pytest.mark.parametrize(
    ("tabla", "columnas"),
    [
        ("madre", "(tenant_id, identificacion) VALUES (:otro, 'intrusa')"),
        (
            "parque",
            "(tenant_id, nombre, ubicacion, capacidad_max) "
            "VALUES (:otro, 'intruso', 'Predio sur', 1)",
        ),
    ],
)
def test_no_puede_escribir_en_otro_tenant(
    criaderos: tuple[Criadero, Criadero], tabla: str, columnas: str
) -> None:
    a, b = criaderos
    with pytest.raises(DBAPIError, match="row-level security"):
        with db.sesion_de_tenant(a.tenant) as s:
            s.execute(
                text(f"INSERT INTO {tabla} {columnas}"),  # noqa: S608
                {"otro": b.tenant},
            )


@pytest.mark.usefixtures("con_motor_app")
def test_celda_no_puede_apuntar_a_una_tanda_de_otro_tenant(
    criaderos: tuple[Criadero, Criadero],
) -> None:
    """Segunda capa: la clave foranea compuesta (tenant_id, id)."""
    a, b = criaderos
    with pytest.raises(IntegrityError, match="fk_celda_tanda_mismo_tenant"):
        with db.sesion_de_tenant(a.tenant) as s:
            s.execute(
                text("INSERT INTO celda (tanda_id) VALUES (:tanda)"), {"tanda": b.tanda}
            )


@pytest.mark.usefixtures("con_motor_app")
def test_nucleo_no_puede_apuntar_a_un_parque_de_otro_tenant(
    criaderos: tuple[Criadero, Criadero],
) -> None:
    """Segunda capa: la clave foranea compuesta (tenant_id, id)."""
    a, b = criaderos
    with pytest.raises(IntegrityError, match="fk_nucleo_parque_mismo_tenant"):
        with db.sesion_de_tenant(a.tenant) as s:
            s.execute(
                text(
                    "INSERT INTO nucleo (parque_id, fila, posicion) "
                    "VALUES (:parque, 2, 2)"
                ),
                {"parque": b.parque},
            )


@pytest.mark.usefixtures("con_motor_app")
def test_tenant_id_lo_completa_postgres(criaderos: tuple[Criadero, Criadero]) -> None:
    a, _ = criaderos
    with pytest.raises(_Deshacer), db.sesion_de_tenant(a.tenant) as s:
        insertado = s.execute(
            text(
                "INSERT INTO madre (identificacion) VALUES ('M-02') RETURNING tenant_id"
            )
        ).scalar()
        assert insertado == a.tenant
        raise _Deshacer


@pytest.mark.parametrize("tabla", TABLAS_NUEVAS)
def test_rol_app_sin_delete(motor_owner: Engine, tabla: str) -> None:
    """Borrado logico en madre, parque y nucleo; la celda se cierra con un estado."""
    with motor_owner.connect() as c:
        puede = c.execute(
            text("SELECT has_table_privilege('malka_app', :tabla, :permiso)"),
            {"tabla": tabla, "permiso": "DELETE"},
        ).scalar()
    assert puede is False


@pytest.mark.usefixtures("con_motor_app")
def test_cantidad_de_celdas_es_derivada(criaderos: tuple[Criadero, Criadero]) -> None:
    """Una fila por cupula: la cantidad es COUNT(*), nunca una columna."""
    a, _ = criaderos
    with db.sesion_de_tenant(a.tenant) as s:
        filas = s.execute(
            text("SELECT estado, nucleo_id FROM celda WHERE tanda_id = :tanda"),
            {"tanda": a.tanda},
        ).all()
    assert len(filas) == CELDAS_POR_TANDA
    assert all(f.estado == "trasladada" and f.nucleo_id is None for f in filas)


@pytest.mark.usefixtures("con_motor_app")
def test_celda_introducida_necesita_nucleo(
    criaderos: tuple[Criadero, Criadero],
) -> None:
    a, _ = criaderos
    with pytest.raises(IntegrityError, match="ck_celda_introducida_con_nucleo"):
        with db.sesion_de_tenant(a.tenant) as s:
            s.execute(text("UPDATE celda SET estado = 'introducida'"))


@pytest.mark.usefixtures("con_motor_app")
def test_celda_introducida_con_nucleo_y_fecha(
    criaderos: tuple[Criadero, Criadero],
) -> None:
    a, _ = criaderos
    with pytest.raises(_Deshacer), db.sesion_de_tenant(a.tenant) as s:
        actualizadas = s.execute(
            text(
                "UPDATE celda SET estado = 'introducida', nucleo_id = :nucleo, "
                "fecha_introduccion = '2026-10-15'"
            ),
            {"nucleo": a.nucleo},
        ).rowcount
        assert actualizadas == CELDAS_POR_TANDA
        raise _Deshacer


@pytest.mark.usefixtures("con_motor_app")
def test_estado_invalido(criaderos: tuple[Criadero, Criadero]) -> None:
    a, _ = criaderos
    with pytest.raises(IntegrityError, match="ck_celda_estado"):
        with db.sesion_de_tenant(a.tenant) as s:
            s.execute(text("UPDATE celda SET estado = 'perdida'"))


@pytest.mark.usefixtures("con_motor_app")
def test_posicion_de_nucleo_ocupada(criaderos: tuple[Criadero, Criadero]) -> None:
    a, _ = criaderos
    with pytest.raises(IntegrityError, match="uq_nucleo_ubicacion"):
        with db.sesion_de_tenant(a.tenant) as s:
            s.execute(
                text(
                    "INSERT INTO nucleo (parque_id, fila, posicion) "
                    "VALUES (:parque, 1, 1)"
                ),
                {"parque": a.parque},
            )


def test_sin_modelo_de_etapas_de_tanda(motor_owner: Engine) -> None:
    """tanda_evento y tanda.etapa_actual ya no existen (docs/dominio.md)."""
    with motor_owner.connect() as c:
        tabla = c.execute(text("SELECT to_regclass('public.tanda_evento')")).scalar()
        banco = c.execute(text("SELECT to_regclass('public.banco')")).scalar()
        columna = c.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'tanda' AND column_name = 'etapa_actual'"
            )
        ).scalar()
    assert tabla is None
    assert columna == 0
    # El banco (post cosecha) se modela con la salida de reinas, no aca.
    assert banco is None
