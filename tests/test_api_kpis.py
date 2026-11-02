"""KPIs de produccion por tanda y por raza, de punta a punta (#31).

Requests HTTP reales contra la app: token firmado, sesion con el tenant del
token y PostgreSQL con RLS. Cada historial se arma con las operaciones por
lote de la #28 (descartar, introducir, cosechar, pasar a banco), igual que en
el campo. Cubre el DoD: 120 introducidas, 95 cosechadas y 25 descartadas dan
79,2 % y 20,8 %; las descartadas antes de introducir no cuentan; tanda sin
introducidas sin division por cero; agregado por raza y fuga entre criaderos.
"""

import uuid
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.main import app
from tests.conftest import crear_tenant, emitir, fijar_tenant

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("cognito", "con_motor_app"),
]

KPIS = "/api/v1/kpis/produccion"
TANDAS = "/api/v1/tandas"

# Fechas fijas y pasadas, de lunes a viernes (el traslarve no va en domingo).
TRASLARVE = date(2026, 10, 2)
INTRODUCCION = date(2026, 10, 12)
COSECHA = date(2026, 10, 20)

# De hijas a padres, para poder borrar al limpiar.
TABLAS = (
    "idempotencia",
    "movimiento_material",
    "celda",
    "tanda",
    "nucleo",
    "parque",
    "banco",
    "material",
    "raza",
)

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


def como(tenant: uuid.UUID, rol: str = "produccion") -> dict[str, str]:
    token = emitir(**{"custom:tenant_id": str(tenant), "cognito:groups": [rol]})
    return {"Authorization": f"Bearer {token}"}


def crear(api: TestClient, url: str, tenant: uuid.UUID, datos: dict[str, Any]) -> str:
    respuesta = api.post(url, json=datos, headers=como(tenant))
    assert respuesta.status_code == 201, respuesta.json()
    return str(respuesta.json()["id"])


class Criadero:
    """Lo que hace falta para cargar tandas en un criadero: razas, cupulas y
    un parque con nucleos."""

    def __init__(self, api: TestClient, motor_owner: Engine, tenant: uuid.UUID):
        self.api = api
        self.tenant = tenant
        self.razas: dict[str, str] = {}
        self.material = crear(
            api, "/api/v1/materiales", tenant, {"nombre": "Cupula", "unidad": "u"}
        )
        datos = {"nombre": "Parque", "ubicacion": "Predio", "capacidad_max": 100}
        self.parque = crear(api, "/api/v1/parques", tenant, datos)
        with motor_owner.begin() as c:
            fijar_tenant(c, tenant)
            c.execute(
                text(
                    "INSERT INTO nucleo (parque_id, fila, posicion) "
                    "SELECT :parque, 1, g FROM generate_series(1, 10) AS g"
                ),
                {"parque": self.parque},
            )

    def raza(self, nombre: str) -> str:
        if nombre not in self.razas:
            self.razas[nombre] = crear(
                self.api, "/api/v1/razas", self.tenant, {"nombre": nombre}
            )
        return self.razas[nombre]

    def operar(self, tanda: str, **cuerpo: Any) -> None:
        respuesta = self.api.post(
            f"{TANDAS}/{tanda}/operaciones", json=cuerpo, headers=como(self.tenant)
        )
        assert respuesta.status_code == 201, respuesta.json()

    def tanda(
        self,
        raza: str = "Carniola",
        *,
        cuadros: int = 1,
        descartadas_antes: int = 0,
        introducidas: int = 0,
        cosechadas: int | None = None,
        traslarve: date = TRASLARVE,
    ) -> str:
        """Una tanda con su historial, cargado como en el campo."""
        datos = {
            "raza_id": self.raza(raza),
            "material_id": self.material,
            "fecha_traslarve": traslarve.isoformat(),
            "cuadros": cuadros,
        }
        tanda = crear(self.api, TANDAS, self.tenant, datos)
        if descartadas_antes:
            self.operar(
                tanda,
                accion="descartar",
                cantidad=descartadas_antes,
                fecha=INTRODUCCION.isoformat(),
            )
        if introducidas:
            self.operar(
                tanda,
                accion="introducir",
                cantidad=introducidas,
                parque_id=self.parque,
                fecha=INTRODUCCION.isoformat(),
            )
        if cosechadas is not None:
            self.operar(
                tanda,
                accion="cosechar",
                cantidad=cosechadas,
                fecha=COSECHA.isoformat(),
            )
        return tanda


def kpis(
    api: TestClient, tenant: uuid.UUID, rol: str = "lectura", **filtros: str
) -> list[dict[str, Any]]:
    respuesta = api.get(KPIS, params=filtros, headers=como(tenant, rol))
    assert respuesta.status_code == 200, respuesta.json()
    return list(respuesta.json())


@pytest.fixture
def criadero(api: TestClient, motor_owner: Engine, criaderos: Criaderos) -> Criadero:
    return Criadero(api, motor_owner, criaderos[0])


# --- por tanda ---


def test_el_ejemplo_del_dominio(api: TestClient, criadero: Criadero) -> None:
    """DoD: 120 introducidas, 95 cosechadas y 25 descartadas = 79,2 % y 20,8 %."""
    # 135 cupulas: 15 no se usan, 120 al parque y se cosechan 95.
    tanda = criadero.tanda(descartadas_antes=15, introducidas=120, cosechadas=95)

    [kpi] = kpis(api, criadero.tenant)

    assert kpi == {
        "tanda_id": tanda,
        "fecha_traslarve": TRASLARVE.isoformat(),
        "raza_id": criadero.razas["Carniola"],
        "raza": "Carniola",
        "tandas": 1,
        "introducidas": 120,
        "cosechadas": 95,
        "perdidas": 25,
        "en_parque": 0,
        "porcentaje_cosecha": "79.2",
        "porcentaje_perdida": "20.8",
    }


def test_las_descartadas_antes_de_introducir_no_entran_en_ningun_porcentaje(
    api: TestClient, criadero: Criadero
) -> None:
    """DoD: las sin nucleo solo cierran la fila."""
    sin_descartes = criadero.tanda(introducidas=40, cosechadas=30)
    con_descartes = criadero.tanda(
        "Buckfast", descartadas_antes=95, introducidas=40, cosechadas=30
    )

    por_tanda = {k["tanda_id"]: k for k in kpis(api, criadero.tenant)}

    for tanda in (sin_descartes, con_descartes):
        assert por_tanda[tanda]["introducidas"] == 40
        assert por_tanda[tanda]["perdidas"] == 10
        assert por_tanda[tanda]["porcentaje_cosecha"] == "75.0"
        assert por_tanda[tanda]["porcentaje_perdida"] == "25.0"


def test_tanda_sin_introducidas_no_divide_por_cero(
    api: TestClient, criadero: Criadero
) -> None:
    """DoD: sin introducidas no hay base; el % viene en null."""
    recien_hecha = criadero.tanda()
    solo_descartes = criadero.tanda("Buckfast", descartadas_antes=135)

    por_tanda = {k["tanda_id"]: k for k in kpis(api, criadero.tenant)}

    for tanda in (recien_hecha, solo_descartes):
        assert por_tanda[tanda]["introducidas"] == 0
        assert por_tanda[tanda]["porcentaje_cosecha"] is None
        assert por_tanda[tanda]["porcentaje_perdida"] is None


def test_una_tanda_en_el_parque_todavia_no_suma_cosecha_ni_perdida(
    api: TestClient, criadero: Criadero
) -> None:
    criadero.tanda(introducidas=50)

    [kpi] = kpis(api, criadero.tenant)

    assert (kpi["introducidas"], kpi["cosechadas"], kpi["perdidas"]) == (50, 0, 0)
    assert kpi["en_parque"] == 50
    assert kpi["porcentaje_cosecha"] == "0.0"
    assert kpi["porcentaje_perdida"] == "0.0"


def test_las_despachadas_siguen_contando_como_cosechadas(
    api: TestClient, motor_owner: Engine, criadero: Criadero
) -> None:
    """Una reina despachada ya se cosecho: el % no baja al vender."""
    tanda = criadero.tanda(descartadas_antes=15, introducidas=120, cosechadas=95)
    antes = kpis(api, criadero.tenant)
    with motor_owner.begin() as c:
        fijar_tenant(c, criadero.tenant)
        c.execute(
            text(
                "UPDATE celda SET estado = 'despachada' WHERE id IN ("
                "SELECT id FROM celda WHERE tanda_id = :tanda "
                "AND estado = 'enjaulada' LIMIT 40)"
            ),
            {"tanda": tanda},
        )

    despues = kpis(api, criadero.tenant)

    assert despues == antes
    assert despues[0]["cosechadas"] == 95


def test_por_tanda_de_la_mas_nueva_a_la_mas_vieja(
    api: TestClient, criadero: Criadero
) -> None:
    vieja = criadero.tanda(traslarve=date(2026, 10, 1))
    nueva = criadero.tanda(traslarve=date(2026, 10, 5))

    assert [k["tanda_id"] for k in kpis(api, criadero.tenant)] == [nueva, vieja]


# --- por raza ---


def test_por_raza_suma_las_celdas_de_sus_tandas(
    api: TestClient, criadero: Criadero
) -> None:
    """DoD: agregado por raza. Se suman celdas, no se promedian los %."""
    # Carniola: 120/95 y 30/30 -> 150 introducidas, 125 cosechadas.
    criadero.tanda(descartadas_antes=15, introducidas=120, cosechadas=95)
    criadero.tanda(introducidas=30, cosechadas=30)
    # Buckfast: una sola tanda, todavia en el parque.
    criadero.tanda("Buckfast", introducidas=20)

    por_raza = kpis(api, criadero.tenant, agrupar="raza")

    assert [k["raza"] for k in por_raza] == ["Buckfast", "Carniola"]
    buckfast, carniola = por_raza
    assert carniola["tanda_id"] is None
    assert carniola["fecha_traslarve"] is None
    assert carniola["tandas"] == 2
    assert (carniola["introducidas"], carniola["cosechadas"]) == (150, 125)
    assert carniola["perdidas"] == 25
    # 125/150 = 83,3 %; el promedio de 79,2 y 100 daria 89,6.
    assert carniola["porcentaje_cosecha"] == "83.3"
    assert carniola["porcentaje_perdida"] == "16.7"
    assert buckfast["tandas"] == 1
    assert buckfast["en_parque"] == 20


def test_raza_sin_introducidas_tampoco_divide_por_cero(
    api: TestClient, criadero: Criadero
) -> None:
    criadero.tanda()

    [kpi] = kpis(api, criadero.tenant, agrupar="raza")

    assert kpi["introducidas"] == 0
    assert kpi["porcentaje_cosecha"] is None


# --- periodo ---


def test_el_periodo_filtra_por_fecha_de_traslarve(
    api: TestClient, criadero: Criadero
) -> None:
    criadero.tanda(traslarve=date(2026, 9, 30), introducidas=10)
    del_medio = criadero.tanda(traslarve=date(2026, 10, 1), introducidas=10)
    criadero.tanda(traslarve=date(2026, 10, 5), introducidas=10)

    por_tanda = kpis(api, criadero.tenant, desde="2026-10-01", hasta="2026-10-02")
    por_raza = kpis(
        api, criadero.tenant, agrupar="raza", desde="2026-10-01", hasta="2026-10-02"
    )

    assert [k["tanda_id"] for k in por_tanda] == [del_medio]
    assert por_raza[0]["tandas"] == 1
    assert por_raza[0]["introducidas"] == 10


def test_periodo_al_reves_es_422(api: TestClient, criadero: Criadero) -> None:
    respuesta = api.get(
        KPIS,
        params={"desde": "2026-10-05", "hasta": "2026-10-01"},
        headers=como(criadero.tenant),
    )

    assert respuesta.status_code == 422
    assert respuesta.json()["error"]["code"] == "periodo_invalido"


def test_agrupacion_desconocida_es_422(api: TestClient, criadero: Criadero) -> None:
    respuesta = api.get(
        KPIS, params={"agrupar": "madre"}, headers=como(criadero.tenant)
    )

    assert respuesta.status_code == 422


def test_sin_tandas_la_lista_viene_vacia(api: TestClient, criaderos: Criaderos) -> None:
    a, _ = criaderos
    assert kpis(api, a) == []
    assert kpis(api, a, agrupar="raza") == []


# --- seguridad ---


def test_fuga_entre_criaderos(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    """DoD: cada criadero ve solo sus tandas y sus razas."""
    a, b = criaderos
    propio = Criadero(api, motor_owner, a)
    ajeno = Criadero(api, motor_owner, b)
    tanda_a = propio.tanda(introducidas=10, cosechadas=8)
    ajeno.tanda(introducidas=100, cosechadas=10)
    ajeno.tanda("Italiana", introducidas=50)

    por_tanda = kpis(api, a)
    por_raza = kpis(api, a, agrupar="raza")

    assert [k["tanda_id"] for k in por_tanda] == [tanda_a]
    assert [(k["raza"], k["introducidas"]) for k in por_raza] == [("Carniola", 10)]
    assert por_raza[0]["porcentaje_cosecha"] == "80.0"


def test_sin_token_es_401(api: TestClient) -> None:
    assert api.get(KPIS).status_code == 401


def test_openapi_documenta_los_kpis(api: TestClient) -> None:
    get = api.get("/openapi.json").json()["paths"][KPIS]["get"]
    parametros = {p["name"] for p in get["parameters"]}
    assert {"agrupar", "desde", "hasta"} <= parametros
