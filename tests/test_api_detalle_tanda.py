"""Detalle de la tanda: donde estan sus celdas (front #9).

GET /api/v1/tandas/{id} suma, contados al leer, los nucleos con celdas de la
tanda en el parque y los bancos con sus reinas cosechadas. Requests HTTP
reales: token firmado, sesion con el tenant del token y PostgreSQL con RLS.
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

TANDAS = "/api/v1/tandas"

# Fechas fijas y pasadas. El 2/10/2026 fue viernes.
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


def crear_tanda(api: TestClient, tenant: uuid.UUID, raza: str = "Carniola") -> str:
    """Una tanda de 1 cuadro: 135 celdas en trasladada."""
    raza_id = crear(api, "/api/v1/razas", tenant, {"nombre": raza})
    material = crear(
        api, "/api/v1/materiales", tenant, {"nombre": f"Cupula {raza}", "unidad": "u"}
    )
    datos = {
        "raza_id": raza_id,
        "material_id": material,
        "fecha_traslarve": TRASLARVE.isoformat(),
        "cuadros": 1,
    }
    return crear(api, TANDAS, tenant, datos)


def crear_parque(
    motor_owner: Engine,
    api: TestClient,
    tenant: uuid.UUID,
    nucleos: int,
    nombre: str = "Parque norte",
) -> str:
    """Un parque con esa cantidad de nucleos activos, en la fila 1."""
    datos = {"nombre": nombre, "ubicacion": "Predio", "capacidad_max": 100}
    parque = crear(api, "/api/v1/parques", tenant, datos)
    with motor_owner.begin() as c:
        fijar_tenant(c, tenant)
        c.execute(
            text(
                "INSERT INTO nucleo (parque_id, fila, posicion) "
                "SELECT :parque, 1, g FROM generate_series(1, :n) AS g"
            ),
            {"parque": parque, "n": nucleos},
        )
    return parque


def crear_banco(api: TestClient, tenant: uuid.UUID, identificador: str) -> str:
    datos = {"identificador": identificador, "capacidad_max": 100}
    return crear(api, "/api/v1/bancos", tenant, datos)


def operar(api: TestClient, tenant: uuid.UUID, tanda: str, **cuerpo: Any) -> Any:
    respuesta = api.post(
        f"{TANDAS}/{tanda}/operaciones", json=cuerpo, headers=como(tenant)
    )
    assert respuesta.status_code == 201, respuesta.json()
    return respuesta.json()


def introducir(
    api: TestClient, tenant: uuid.UUID, tanda: str, parque: str, cantidad: int
) -> Any:
    return operar(
        api,
        tenant,
        tanda,
        accion="introducir",
        cantidad=cantidad,
        parque_id=parque,
        fecha=INTRODUCCION.isoformat(),
    )


def cosechar(api: TestClient, tenant: uuid.UUID, tanda: str, cantidad: int) -> Any:
    return operar(
        api,
        tenant,
        tanda,
        accion="cosechar",
        cantidad=cantidad,
        fecha=COSECHA.isoformat(),
    )


def pasar_a_banco(
    api: TestClient, tenant: uuid.UUID, tanda: str, banco: str, cantidad: int
) -> Any:
    return operar(
        api, tenant, tanda, accion="pasar_a_banco", cantidad=cantidad, banco_id=banco
    )


def detalle(
    api: TestClient, tenant: uuid.UUID, tanda: str, rol: str = "lectura"
) -> Any:
    respuesta = api.get(f"{TANDAS}/{tanda}", headers=como(tenant, rol))
    assert respuesta.status_code == 200, respuesta.json()
    return respuesta.json()


def test_tanda_sin_operaciones_no_esta_en_nucleos_ni_bancos(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    tanda = crear_tanda(api, a)

    leida = detalle(api, a, tanda)

    assert leida["celdas_por_estado"] == {"trasladada": 135}
    assert leida["nucleos"] == []
    assert leida["bancos"] == []


def test_despues_de_introducir_los_nucleos_coinciden_con_el_reparto(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    tanda = crear_tanda(api, a)
    parque = crear_parque(motor_owner, api, a, nucleos=4)

    reparto = introducir(api, a, tanda, parque, 10)["reparto"]
    nucleos = detalle(api, a, tanda)["nucleos"]

    assert [(n["fila"], n["posicion"], n["celdas"]) for n in nucleos] == [
        (n["fila"], n["posicion"], n["celdas"]) for n in reparto
    ]
    assert [n["celdas"] for n in nucleos] == [3, 3, 2, 2]
    assert {n["parque_id"] for n in nucleos} == {parque}


def test_dos_introducciones_suman_en_el_mismo_nucleo_y_en_otro_parque(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    tanda = crear_tanda(api, a)
    norte = crear_parque(motor_owner, api, a, nucleos=2)
    sur = crear_parque(motor_owner, api, a, nucleos=1, nombre="Parque sur")

    introducir(api, a, tanda, norte, 4)
    introducir(api, a, tanda, norte, 2)
    introducir(api, a, tanda, sur, 5)
    nucleos = detalle(api, a, tanda)["nucleos"]

    por_parque: dict[str, int] = {}
    for n in nucleos:
        por_parque[n["parque_id"]] = por_parque.get(n["parque_id"], 0) + n["celdas"]
    assert por_parque == {norte: 6, sur: 5}
    assert len(nucleos) == 3
    assert sum(n["celdas"] for n in nucleos) == 11


def test_despues_de_cosechar_los_nucleos_quedan_libres(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    tanda = crear_tanda(api, a)
    parque = crear_parque(motor_owner, api, a, nucleos=5)
    introducir(api, a, tanda, parque, 10)

    cosechar(api, a, tanda, 8)
    leida = detalle(api, a, tanda)

    assert leida["nucleos"] == []
    # Todavia sin banco: las 8 cosechadas no estan en ninguno.
    assert leida["bancos"] == []
    assert leida["celdas_por_estado"]["enjaulada"] == 8


def test_pasar_a_banco_se_ve_por_banco_y_el_resto_queda_sin_banco(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    tanda = crear_tanda(api, a)
    parque = crear_parque(motor_owner, api, a, nucleos=5)
    introducir(api, a, tanda, parque, 20)
    cosechar(api, a, tanda, 15)
    b1 = crear_banco(api, a, "B-01")
    b2 = crear_banco(api, a, "B-02")

    pasar_a_banco(api, a, tanda, b2, 3)
    pasar_a_banco(api, a, tanda, b1, 5)
    pasar_a_banco(api, a, tanda, b1, 2)
    leida = detalle(api, a, tanda)

    # Por identificador, cada banco con lo que tiene de esta tanda.
    assert leida["bancos"] == [
        {"banco_id": b1, "identificador": "B-01", "reinas": 7},
        {"banco_id": b2, "identificador": "B-02", "reinas": 3},
    ]
    en_banco = sum(b["reinas"] for b in leida["bancos"])
    assert leida["celdas_por_estado"]["enjaulada"] - en_banco == 5


def test_solo_cuenta_las_celdas_de_esta_tanda(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    """Otra tanda en el mismo parque y el mismo banco no se mezcla."""
    a, _ = criaderos
    tanda = crear_tanda(api, a)
    otra = crear_tanda(api, a, raza="Buckfast")
    parque = crear_parque(motor_owner, api, a, nucleos=2)
    banco = crear_banco(api, a, "B-01")
    introducir(api, a, otra, parque, 6)
    cosechar(api, a, otra, 6)
    pasar_a_banco(api, a, otra, banco, 6)

    introducir(api, a, tanda, parque, 4)
    leida = detalle(api, a, tanda)

    assert sum(n["celdas"] for n in leida["nucleos"]) == 4
    assert leida["bancos"] == []


def test_el_listado_no_trae_nucleos_ni_bancos(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    crear_tanda(api, a)

    items = api.get(TANDAS, headers=como(a)).json()["items"]

    assert len(items) == 1
    assert "nucleos" not in items[0]
    assert "bancos" not in items[0]


def test_fuga_entre_criaderos(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    """La tanda de otro criadero es 404, y la propia no muestra nada ajeno."""
    a, b = criaderos
    tanda_a = crear_tanda(api, a)
    tanda_b = crear_tanda(api, b)
    parque_b = crear_parque(motor_owner, api, b, nucleos=3)
    introducir(api, b, tanda_b, parque_b, 9)

    ajena = api.get(f"{TANDAS}/{tanda_b}", headers=como(a))
    propia = detalle(api, a, tanda_a)

    assert ajena.status_code == 404
    assert ajena.json()["error"]["code"] == "tanda_no_encontrada"
    assert propia["nucleos"] == []
    assert propia["bancos"] == []


def test_openapi_documenta_el_detalle(api: TestClient) -> None:
    rutas = api.get("/openapi.json").json()["paths"]
    get = rutas[f"{TANDAS}/{{tanda_id}}"]["get"]
    esquema = get["responses"]["200"]["content"]["application/json"]["schema"]
    assert esquema["$ref"].endswith("/TandaDetalle")
    listado = rutas[TANDAS]["get"]["responses"]["200"]["content"]
    assert "TandaRespuesta" in str(listado)