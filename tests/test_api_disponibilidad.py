"""GET /api/v1/disponibilidad de punta a punta (#33, ADR 0014).

El stock sale de las celdas: solo cuentan las enjauladas, esten o no en un
banco. Las celdas se cargan directo como duenio porque todavia no existen las
transiciones por lote (#28), y las reservas valen cero hasta #35.
"""

import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, text

from app.main import app
from tests.conftest import crear_tenant, emitir, fijar_tenant

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("cognito", "con_motor_app"),
]

URL = "/api/v1/disponibilidad"

# De hijas a padres, para poder borrar al limpiar.
TABLAS = ("celda", "nucleo", "parque", "banco", "tanda", "raza")

Criaderos = tuple[uuid.UUID, uuid.UUID]


@pytest.fixture(scope="module")
def criaderos(motor_owner: Engine) -> Iterator[Criaderos]:
    """Dos tenants vacios; cada test carga lo que necesita."""
    a, b = uuid.uuid4(), uuid.uuid4()
    with motor_owner.begin() as c:
        for tenant in (a, b):
            crear_tenant(c, tenant)
    yield a, b
    with motor_owner.begin() as c:
        for tenant in (a, b):
            c.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": tenant})


@pytest.fixture(autouse=True)
def sin_datos(motor_owner: Engine, criaderos: Criaderos) -> Iterator[None]:
    """Cada test arranca vacio: los datos de uno no afectan a otro."""
    yield
    for tenant in criaderos:
        with motor_owner.begin() as c:
            fijar_tenant(c, tenant)
            for tabla in TABLAS:
                c.execute(text(f"DELETE FROM {tabla} WHERE true"))  # noqa: S608


@pytest.fixture
def api() -> TestClient:
    return TestClient(app)


def como(tenant: uuid.UUID, rol: str = "ventas") -> dict[str, str]:
    """Headers de un usuario del criadero con ese rol."""
    token = emitir(**{"custom:tenant_id": str(tenant), "cognito:groups": [rol]})
    return {"Authorization": f"Bearer {token}"}


class Criadero:
    """Carga datos de un criadero como duenio, sin pasar por la API."""

    def __init__(self, conexion: Connection) -> None:
        self.c = conexion
        parque = self._id(
            "INSERT INTO parque (nombre, ubicacion, capacidad_max) "
            "VALUES ('P1', 'Predio', 100) RETURNING id"
        )
        self.nucleo = self._id(
            "INSERT INTO nucleo (parque_id, fila, posicion) "
            "VALUES (:parque, 1, 1) RETURNING id",
            parque=parque,
        )

    def _id(self, sql: str, **parametros: Any) -> uuid.UUID:
        return uuid.UUID(str(self.c.execute(text(sql), parametros).scalar_one()))

    def raza(self, nombre: str, *, activo: bool = True) -> uuid.UUID:
        return self._id(
            "INSERT INTO raza (nombre, activo) VALUES (:nombre, :activo) RETURNING id",
            nombre=nombre,
            activo=activo,
        )

    def banco(self, identificador: str) -> uuid.UUID:
        return self._id(
            "INSERT INTO banco (identificador) VALUES (:identificador) RETURNING id",
            identificador=identificador,
        )

    def tanda(self, raza: uuid.UUID) -> uuid.UUID:
        return self._id(
            "INSERT INTO tanda (raza_id, fecha_traslarve) "
            "VALUES (:raza, '2026-09-20') RETURNING id",
            raza=raza,
        )

    def celdas(
        self,
        tanda: uuid.UUID,
        estado: str,
        cantidad: int,
        banco: uuid.UUID | None = None,
    ) -> None:
        # trasladada y descartada todavia no tienen nucleo (CHECK de la 0003).
        sin_nucleo = estado in ("trasladada", "descartada")
        self.c.execute(
            text(
                "INSERT INTO celda (tanda_id, estado, nucleo_id, fecha_introduccion, "
                "fecha_descarte, fecha_extravio, banco_id) "
                "SELECT :tanda, :estado, :nucleo, :introduccion, :descarte, "
                ":extravio, :banco FROM generate_series(1, :cantidad)"
            ),
            {
                "tanda": tanda,
                "estado": estado,
                "nucleo": None if sin_nucleo else self.nucleo,
                "introduccion": None if sin_nucleo else "2026-09-30",
                "descarte": "2026-09-25" if estado == "descartada" else None,
                "extravio": "2026-10-12" if estado == "extraviada" else None,
                "banco": banco,
                "cantidad": cantidad,
            },
        )


Cargar = Callable[[uuid.UUID], AbstractContextManager[Criadero]]


@pytest.fixture
def cargar(motor_owner: Engine) -> Cargar:
    """`with cargar(tenant) as criadero:` carga en una transaccion de ese tenant."""

    @contextmanager
    def _cargar(tenant: uuid.UUID) -> Iterator[Criadero]:
        with motor_owner.begin() as c:
            fijar_tenant(c, tenant)
            yield Criadero(c)

    return _cargar


def leer(api: TestClient, tenant: uuid.UUID, **params: Any) -> list[dict[str, Any]]:
    respuesta = api.get(URL, params=params, headers=como(tenant))
    assert respuesta.status_code == 200, respuesta.json()
    cuerpo: list[dict[str, Any]] = respuesta.json()
    return cuerpo


def test_sin_razas_devuelve_lista_vacia(api: TestClient, criaderos: Criaderos) -> None:
    assert leer(api, criaderos[0]) == []


def test_raza_activa_sin_reinas_aparece_en_cero(
    api: TestClient, criaderos: Criaderos, cargar: Cargar
) -> None:
    with cargar(criaderos[0]) as criadero:
        raza = criadero.raza("Italiana")

    assert leer(api, criaderos[0]) == [
        {
            "raza_id": str(raza),
            "raza": "Italiana",
            "enjauladas": 0,
            "reservadas": 0,
            "disponible": 0,
            "bancos": [],
        }
    ]


def test_solo_cuentan_las_enjauladas(
    api: TestClient, criaderos: Criaderos, cargar: Cargar
) -> None:
    with cargar(criaderos[0]) as criadero:
        tanda = criadero.tanda(criadero.raza("Italiana"))
        banco = criadero.banco("B-norte")
        for estado in (
            "trasladada",
            "descartada",
            "introducida",
            "nacida",
            "madura",
            "fecundada",
            "extraviada",
        ):
            criadero.celdas(tanda, estado, 2)
        criadero.celdas(tanda, "despachada", 5, banco)
        criadero.celdas(tanda, "enjaulada", 3, banco)
        criadero.celdas(tanda, "enjaulada", 4)

    (italiana,) = leer(api, criaderos[0])

    # Ninguna despachada cuenta; las enjauladas sin banco si.
    assert italiana["enjauladas"] == 7
    assert italiana["reservadas"] == 0
    assert italiana["disponible"] == 7


def test_existencia_por_banco_suma_las_enjauladas(
    api: TestClient, criaderos: Criaderos, cargar: Cargar
) -> None:
    with cargar(criaderos[0]) as criadero:
        raza = criadero.raza("Italiana")
        # Reinas de una misma raza en tandas y bancos distintos.
        t1, t2 = criadero.tanda(raza), criadero.tanda(raza)
        sur, norte = criadero.banco("B-sur"), criadero.banco("B-norte")
        criadero.banco("B-vacio")
        criadero.celdas(t1, "enjaulada", 3, norte)
        criadero.celdas(t2, "enjaulada", 2, norte)
        criadero.celdas(t2, "enjaulada", 6, sur)
        criadero.celdas(t1, "enjaulada", 1)
        criadero.celdas(t1, "despachada", 9, sur)

    (italiana,) = leer(api, criaderos[0])

    # Ordenados por identificador; las que no estan en un banco, al final.
    assert italiana["bancos"] == [
        {"banco_id": str(norte), "identificador": "B-norte", "reinas": 5},
        {"banco_id": str(sur), "identificador": "B-sur", "reinas": 6},
        {"banco_id": None, "identificador": None, "reinas": 1},
    ]
    assert italiana["enjauladas"] == sum(b["reinas"] for b in italiana["bancos"])


def test_cada_raza_tiene_su_propio_saldo(
    api: TestClient, criaderos: Criaderos, cargar: Cargar
) -> None:
    with cargar(criaderos[0]) as criadero:
        banco = criadero.banco("B1")
        italiana = criadero.tanda(criadero.raza("Italiana"))
        carniola = criadero.tanda(criadero.raza("Carniola"))
        criadero.celdas(italiana, "enjaulada", 4, banco)
        criadero.celdas(carniola, "enjaulada", 7, banco)

    cuerpo = leer(api, criaderos[0])

    assert [fila["raza"] for fila in cuerpo] == ["Carniola", "Italiana"]
    razas = {fila["raza"]: fila for fila in cuerpo}
    assert razas["Italiana"]["disponible"] == 4
    assert razas["Carniola"]["disponible"] == 7
    # El mismo banco aparece en las dos, cada una con sus reinas.
    assert razas["Italiana"]["bancos"][0]["reinas"] == 4
    assert razas["Carniola"]["bancos"][0]["reinas"] == 7


def test_filtro_por_raza(api: TestClient, criaderos: Criaderos, cargar: Cargar) -> None:
    with cargar(criaderos[0]) as criadero:
        italiana = criadero.raza("Italiana")
        carniola = criadero.raza("Carniola")
        criadero.celdas(criadero.tanda(italiana), "enjaulada", 4)
        criadero.celdas(criadero.tanda(carniola), "enjaulada", 7)

    (fila,) = leer(api, criaderos[0], raza_id=str(italiana))

    assert fila["raza_id"] == str(italiana)
    assert fila["disponible"] == 4
    assert fila["bancos"] == [{"banco_id": None, "identificador": None, "reinas": 4}]


def test_filtro_por_raza_inexistente_devuelve_vacio(
    api: TestClient, criaderos: Criaderos, cargar: Cargar
) -> None:
    with cargar(criaderos[0]) as criadero:
        criadero.raza("Italiana")

    assert leer(api, criaderos[0], raza_id=str(uuid.uuid4())) == []


def test_raza_inactiva_aparece_solo_si_le_quedan_reinas(
    api: TestClient, criaderos: Criaderos, cargar: Cargar
) -> None:
    with cargar(criaderos[0]) as criadero:
        criadero.raza("Sin reinas", activo=False)
        con_reinas = criadero.raza("Con reinas", activo=False)
        criadero.celdas(criadero.tanda(con_reinas), "enjaulada", 2)

    assert [fila["raza"] for fila in leer(api, criaderos[0])] == ["Con reinas"]


def test_no_hay_fuga_entre_criaderos(
    api: TestClient, criaderos: Criaderos, cargar: Cargar
) -> None:
    a, b = criaderos
    with cargar(a) as criadero:
        criadero.celdas(criadero.tanda(criadero.raza("Italiana")), "enjaulada", 5)
    with cargar(b) as criadero:
        italiana_b = criadero.raza("Italiana")
        criadero.celdas(criadero.tanda(italiana_b), "enjaulada", 2)

    (fila_b,) = leer(api, b)

    # Misma raza por nombre, pero B solo ve sus 2 reinas.
    assert fila_b["raza_id"] == str(italiana_b)
    assert fila_b["disponible"] == 2


def test_la_raza_de_otro_criadero_no_se_puede_consultar(
    api: TestClient, criaderos: Criaderos, cargar: Cargar
) -> None:
    a, b = criaderos
    with cargar(a) as criadero:
        italiana_a = criadero.raza("Italiana")
        criadero.celdas(criadero.tanda(italiana_a), "enjaulada", 5)

    assert leer(api, b, raza_id=str(italiana_a)) == []


@pytest.mark.parametrize("rol", ["admin", "produccion", "ventas", "lectura"])
def test_leer_puede_cualquier_rol(
    api: TestClient, criaderos: Criaderos, rol: str
) -> None:
    respuesta = api.get(URL, headers=como(criaderos[0], rol))

    assert respuesta.status_code == 200


def test_sin_token_es_401(api: TestClient) -> None:
    assert api.get(URL).status_code == 401


def test_raza_id_malformado_es_422(api: TestClient, criaderos: Criaderos) -> None:
    respuesta = api.get(
        URL, params={"raza_id": "no-es-uuid"}, headers=como(criaderos[0])
    )

    assert respuesta.status_code == 422


def test_el_schema_no_choca_con_el_de_health_ready(api: TestClient) -> None:
    esquemas = api.get("/openapi.json").json()["components"]["schemas"]

    assert "DisponibilidadReinas" in esquemas
    # El de /health/ready sigue siendo otro, con sus propios campos.
    assert "reservadas" not in esquemas["Disponibilidad"]["properties"]
