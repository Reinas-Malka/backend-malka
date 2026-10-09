"""Aislamiento multi-tenant con RLS, tabla por tabla (#22).

Dos criaderos con una fila en cada tabla con tenant_id. Con el rol de la API
(malka_app) y el criadero A fijado, nada de B se lee, se modifica ni se
borra. Una guardia exige que toda tabla con tenant_id este en ORDEN: si una
migracion suma una tabla y no se agrega aca, el CI falla.

El recorrido completo (token -> obtener_sesion -> RLS) se prueba con los
endpoints de clientes, la unica tabla que la API expone hoy.
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from psycopg import errors
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import ProgrammingError

from app.main import app
from tests.conftest import crear_tenant, emitir, fijar_tenant

pytestmark = pytest.mark.integration

# Orden de carga (padres antes que hijas). La limpieza lo recorre al reves.
ORDEN = (
    "raza",
    "madre",
    "tanda",
    "parque",
    "nucleo",
    "celda",
    "material",
    "movimiento_material",
    "item_vendible",
    "cliente",
    "idempotencia",
)

Filas = dict[str, uuid.UUID]


@dataclass(frozen=True)
class Criaderos:
    """A es quien ataca; de B se guardan las filas que intenta alcanzar."""

    a: uuid.UUID
    b: uuid.UUID
    filas_b: Filas


def _cargar(c: Connection, tenant: uuid.UUID) -> Filas:
    """Una fila por tabla, cargada como duenio con el tenant fijado."""
    crear_tenant(c, tenant)
    fijar_tenant(c, tenant)
    filas: Filas = {}

    def insertar(tabla: str, sql: str, **params: object) -> None:
        valor = c.execute(text(sql), params).scalar_one()
        assert isinstance(valor, uuid.UUID)
        filas[tabla] = valor

    insertar("raza", "INSERT INTO raza (nombre) VALUES ('Italiana') RETURNING id")
    insertar("madre", "INSERT INTO madre (identificacion) VALUES ('M-01') RETURNING id")
    insertar(
        "tanda",
        "INSERT INTO tanda (raza_id, madre_id, fecha_traslarve) "
        "VALUES (:raza, :madre, '2026-10-05') RETURNING id",
        raza=filas["raza"],
        madre=filas["madre"],
    )
    insertar(
        "parque",
        "INSERT INTO parque (nombre, ubicacion, capacidad_max) "
        "VALUES ('P1', 'Predio norte', 9) RETURNING id",
    )
    insertar(
        "nucleo",
        "INSERT INTO nucleo (parque_id, fila, posicion) "
        "VALUES (:parque, 1, 1) RETURNING id",
        parque=filas["parque"],
    )
    insertar(
        "celda",
        "INSERT INTO celda (tanda_id) VALUES (:tanda) RETURNING id",
        tanda=filas["tanda"],
    )
    insertar(
        "material",
        "INSERT INTO material (nombre, unidad) "
        "VALUES ('Cupula', 'unidad') RETURNING id",
    )
    insertar(
        "movimiento_material",
        "INSERT INTO movimiento_material (material_id, tipo, cantidad) "
        "VALUES (:material, 'compra', 10) RETURNING id",
        material=filas["material"],
    )
    insertar(
        "item_vendible",
        "INSERT INTO item_vendible (nombre) VALUES ('Reina fecundada') RETURNING id",
    )
    # De exportacion: el tax id no tiene digito verificador que calcular.
    insertar(
        "cliente",
        "INSERT INTO cliente (nombre, pais, tipo, condicion_iva, cuit_o_tax_id) "
        "VALUES ('Bienenzucht GmbH', 'DE', 'exportacion', 'cliente_exterior', "
        "'DE123456789') RETURNING id",
    )
    # La misma clave en los dos criaderos: es unica por tenant, no global (#25).
    insertar(
        "idempotencia",
        "INSERT INTO idempotencia (clave, hash_request) "
        "VALUES ('clave-de-prueba', 'huella') RETURNING id",
    )
    return filas


@pytest.fixture(scope="module")
def criaderos(motor_owner: Engine) -> Iterator[Criaderos]:
    """Dos criaderos con los mismos datos, para probar que no se mezclan."""
    a, b = uuid.uuid4(), uuid.uuid4()
    with motor_owner.begin() as c:
        _cargar(c, a)
    with motor_owner.begin() as c:
        filas_b = _cargar(c, b)
    yield Criaderos(a=a, b=b, filas_b=filas_b)
    # El duenio puede borrar (la API no). De hijas a padres.
    for tenant in (a, b):
        with motor_owner.begin() as c:
            fijar_tenant(c, tenant)
            for tabla in reversed(ORDEN):
                c.execute(text(f"DELETE FROM {tabla} WHERE true"))  # noqa: S608
            c.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": tenant})


def test_toda_tabla_con_tenant_id_esta_cubierta(motor_owner: Engine) -> None:
    """Guardia: una tabla nueva con tenant_id necesita su fila en _cargar."""
    with motor_owner.connect() as c:
        tablas = c.execute(
            text(
                "SELECT table_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND column_name = 'tenant_id'"
            )
        ).scalars()
        assert set(tablas) == set(ORDEN)


def _afectadas(motor_app: Engine, tenant: uuid.UUID, sql: str, fila: uuid.UUID) -> int:
    """Corre una escritura como la API en el criadero indicado y la deshace.

    Devuelve las filas afectadas, o 0 si el rol ni siquiera tiene el permiso
    (por ejemplo DELETE: la API usa borrado logico).
    """
    with motor_app.connect() as c:
        transaccion = c.begin()
        try:
            fijar_tenant(c, tenant)
            return c.execute(text(sql), {"id": fila, "tenant": tenant}).rowcount
        except ProgrammingError as error:
            if isinstance(error.orig, errors.InsufficientPrivilege):
                return 0
            raise
        finally:
            transaccion.rollback()


def _sigue_en_b(motor_owner: Engine, criaderos: Criaderos, tabla: str) -> bool:
    """Visto como duenio desde B: la fila sigue ahi y sigue siendo de B."""
    with motor_owner.begin() as c:
        # FORCE RLS: el duenio tambien necesita el tenant fijado para verla.
        fijar_tenant(c, criaderos.b)
        tenant = c.execute(
            text(f"SELECT tenant_id FROM {tabla} WHERE id = :id"),  # noqa: S608
            {"id": criaderos.filas_b[tabla]},
        ).scalar_one_or_none()
    return tenant == criaderos.b


@pytest.mark.parametrize("tabla", ORDEN)
def test_a_no_lee_las_filas_de_b(
    motor_app: Engine, criaderos: Criaderos, tabla: str
) -> None:
    with motor_app.begin() as c:
        fijar_tenant(c, criaderos.a)
        vistas = c.execute(
            text(f"SELECT count(*) FROM {tabla} WHERE id = :id"),  # noqa: S608
            {"id": criaderos.filas_b[tabla]},
        ).scalar_one()

    assert vistas == 0


@pytest.mark.parametrize("tabla", ORDEN)
def test_a_no_modifica_las_filas_de_b(
    motor_app: Engine,
    motor_owner: Engine,
    criaderos: Criaderos,
    tabla: str,
) -> None:
    """Intenta apropiarse de la fila de B pasandola a su criadero."""
    sql = f"UPDATE {tabla} SET tenant_id = :tenant WHERE id = :id"  # noqa: S608

    assert _afectadas(motor_app, criaderos.a, sql, criaderos.filas_b[tabla]) == 0
    assert _sigue_en_b(motor_owner, criaderos, tabla)


@pytest.mark.parametrize("tabla", ORDEN)
def test_a_no_borra_las_filas_de_b(
    motor_app: Engine,
    motor_owner: Engine,
    criaderos: Criaderos,
    tabla: str,
) -> None:
    sql = f"DELETE FROM {tabla} WHERE id = :id"  # noqa: S608

    assert _afectadas(motor_app, criaderos.a, sql, criaderos.filas_b[tabla]) == 0
    assert _sigue_en_b(motor_owner, criaderos, tabla)


# --- De punta a punta: token del criadero A contra un cliente de B ---


def _como(tenant: uuid.UUID) -> dict[str, str]:
    claims: dict[str, Any] = {
        "custom:tenant_id": str(tenant),
        "cognito:groups": ["admin"],
    }
    return {"Authorization": f"Bearer {emitir(**claims)}"}


def _pedir(api: TestClient, metodo: str, ruta: str, tenant: uuid.UUID) -> Any:
    cuerpo = {"nombre": "Robado"} if metodo == "PATCH" else None
    return api.request(metodo, ruta, headers=_como(tenant), json=cuerpo)


@pytest.mark.usefixtures("cognito", "con_motor_app")
@pytest.mark.parametrize("metodo", ["GET", "PATCH", "DELETE"])
def test_un_cliente_de_b_y_uno_inexistente_responden_igual(
    motor_owner: Engine, criaderos: Criaderos, metodo: str
) -> None:
    """Si las respuestas difirieran, A podria saber que ids existen en B."""
    api = TestClient(app)
    de_b = criaderos.filas_b["cliente"]
    ajeno = _pedir(api, metodo, f"/api/v1/clientes/{de_b}", criaderos.a)
    inexistente = _pedir(api, metodo, f"/api/v1/clientes/{uuid.uuid4()}", criaderos.a)

    assert ajeno.status_code == inexistente.status_code == 404
    # El request_id cambia en cada pedido; el resto tiene que ser identico.
    assert {**ajeno.json()["error"], "request_id": None} == {
        **inexistente.json()["error"],
        "request_id": None,
    }
    with motor_owner.begin() as c:
        fijar_tenant(c, criaderos.b)
        cliente = c.execute(
            text("SELECT nombre, activo FROM cliente WHERE id = :id"), {"id": de_b}
        ).one()
    assert tuple(cliente) == ("Bienenzucht GmbH", True)
