"""Alta de nucleos por cantidad: POST /api/v1/parques/{id}/nucleos.

El usuario carga solo cuantos nucleos suma; el sistema los pone en una fila
nueva (la siguiente a la mas alta del parque) en las posiciones 1..cantidad,
y respeta la capacidad del parque. Requests HTTP reales: token firmado,
sesion con el tenant del token y PostgreSQL con RLS.
"""

import uuid
from collections.abc import Iterator
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

PARQUES = "/api/v1/parques"
NUCLEOS = "/api/v1/nucleos"

# De hijas a padres, para poder borrar al limpiar.
TABLAS = ("idempotencia", "nucleo", "parque")

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


def codigo(respuesta: Any) -> str:
    return str(respuesta.json()["error"]["code"])


def crear_parque(api: TestClient, tenant: uuid.UUID, capacidad: int = 20) -> str:
    datos = {"nombre": "Parque", "ubicacion": "Predio", "capacidad_max": capacidad}
    respuesta = api.post(PARQUES, json=datos, headers=como(tenant))
    assert respuesta.status_code == 201, respuesta.json()
    return str(respuesta.json()["id"])


def nucleo(api: TestClient, tenant: uuid.UUID, parque: str, fila: int) -> str:
    """Un nucleo cargado de a uno, en la posicion 1 de esa fila."""
    datos = {"parque_id": parque, "fila": fila, "posicion": 1}
    respuesta = api.post(NUCLEOS, json=datos, headers=como(tenant))
    assert respuesta.status_code == 201, respuesta.json()
    return str(respuesta.json()["id"])


def lote(
    api: TestClient,
    tenant: uuid.UUID,
    parque: str,
    cantidad: Any,
    rol: str = "produccion",
    **headers: str,
) -> Any:
    return api.post(
        f"{PARQUES}/{parque}/nucleos",
        json={"cantidad": cantidad},
        headers={**como(tenant, rol), **headers},
    )


def ubicaciones(api: TestClient, tenant: uuid.UUID, parque: str) -> list[tuple]:
    pagina = api.get(
        NUCLEOS,
        params={"parque_id": parque, "incluir_inactivos": True, "limite": 100},
        headers=como(tenant),
    ).json()
    return [(n["fila"], n["posicion"], n["activo"]) for n in pagina["items"]]


@pytest.mark.parametrize("rol", ["admin", "produccion"])
def test_crea_la_cantidad_en_una_fila_nueva(
    api: TestClient, criaderos: Criaderos, rol: str
) -> None:
    a, _ = criaderos
    parque = crear_parque(api, a)

    respuesta = lote(api, a, parque, 4, rol=rol)

    assert respuesta.status_code == 201, respuesta.json()
    assert respuesta.json() == {
        "parque_id": parque,
        "creados": 4,
        "fila": 1,
        "posicion_desde": 1,
        "posicion_hasta": 4,
        "nucleos_activos": 4,
        "capacidad_max": 20,
        "advertencias": [],
    }
    assert ubicaciones(api, a, parque) == [(1, p, True) for p in (1, 2, 3, 4)]
    leido = api.get(f"{PARQUES}/{parque}", headers=como(a)).json()
    assert leido["nucleos_activos"] == 4


def test_cada_lote_va_a_la_fila_siguiente_de_la_mas_alta(
    api: TestClient, criaderos: Criaderos
) -> None:
    """Los cargados a mano cuentan; tambien uno dado de baja (ocupa su lugar)."""
    a, _ = criaderos
    parque = crear_parque(api, a)
    nucleo(api, a, parque, fila=1)
    baja = nucleo(api, a, parque, fila=3)
    api.delete(f"{NUCLEOS}/{baja}", headers=como(a))

    primero = lote(api, a, parque, 2).json()
    segundo = lote(api, a, parque, 3).json()

    assert (primero["fila"], segundo["fila"]) == (4, 5)
    assert segundo["nucleos_activos"] == 1 + 2 + 3
    assert ubicaciones(api, a, parque) == [
        (1, 1, True),
        (3, 1, False),
        (4, 1, True),
        (4, 2, True),
        (5, 1, True),
        (5, 2, True),
        (5, 3, True),
    ]


def test_si_no_entran_todos_no_crea_ninguno(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    parque = crear_parque(api, a, capacidad=5)
    lote(api, a, parque, 3)

    respuesta = lote(api, a, parque, 3)

    assert respuesta.status_code == 409
    assert codigo(respuesta) == "capacidad_insuficiente"
    assert respuesta.json()["error"]["details"] == {
        "pedidos": 3,
        "disponibles": 2,
        "capacidad_max": 5,
        "nucleos_activos": 3,
    }
    assert len(ubicaciones(api, a, parque)) == 3


def test_avisa_cuando_el_lote_llena_el_parque(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    parque = crear_parque(api, a, capacidad=5)
    lote(api, a, parque, 3)

    respuesta = lote(api, a, parque, 2)

    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert cuerpo["nucleos_activos"] == 5
    assert [aviso["code"] for aviso in cuerpo["advertencias"]] == ["parque_al_limite"]


def test_parque_de_otro_criadero_o_inexistente_es_404(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, b = criaderos
    parque_a = crear_parque(api, a)

    ajeno = lote(api, b, parque_a, 2)
    inexistente = lote(api, b, str(uuid.uuid4()), 2)

    for respuesta in (ajeno, inexistente):
        assert respuesta.status_code == 404
        assert codigo(respuesta) == "parque_no_encontrado"
    assert ubicaciones(api, a, parque_a) == []


def test_parque_dado_de_baja_es_409(api: TestClient, criaderos: Criaderos) -> None:
    """Mismo codigo que el alta de a uno y la introduccion: parque_inactivo."""
    a, _ = criaderos
    parque = crear_parque(api, a)
    api.delete(f"{PARQUES}/{parque}", headers=como(a))

    respuesta = lote(api, a, parque, 2)

    assert respuesta.status_code == 409
    assert codigo(respuesta) == "parque_inactivo"


@pytest.mark.parametrize("rol", ["ventas", "lectura"])
def test_sin_rol_de_escritura_es_403(
    api: TestClient, criaderos: Criaderos, rol: str
) -> None:
    a, _ = criaderos
    parque = crear_parque(api, a)

    respuesta = lote(api, a, parque, 2, rol=rol)

    assert respuesta.status_code == 403
    assert codigo(respuesta) == "sin_permiso"
    assert ubicaciones(api, a, parque) == []


@pytest.mark.parametrize("cantidad", [0, -1, 1001, "3", 2.5, None])
def test_cantidad_invalida_es_422(
    api: TestClient, criaderos: Criaderos, cantidad: Any
) -> None:
    a, _ = criaderos
    parque = crear_parque(api, a)

    respuesta = lote(api, a, parque, cantidad)

    assert respuesta.status_code == 422
    assert codigo(respuesta) == "validacion"


def test_campos_de_mas_son_422(api: TestClient, criaderos: Criaderos) -> None:
    """La ubicacion la elige el sistema: mandar fila es un error, no se ignora."""
    a, _ = criaderos
    parque = crear_parque(api, a)

    respuesta = api.post(
        f"{PARQUES}/{parque}/nucleos",
        json={"cantidad": 2, "fila": 7},
        headers=como(a),
    )

    assert respuesta.status_code == 422


def test_con_la_misma_clave_crea_un_solo_lote(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    parque = crear_parque(api, a)
    clave = {"Idempotency-Key": str(uuid.uuid4())}

    primera = lote(api, a, parque, 3, **clave)
    segunda = lote(api, a, parque, 3, **clave)
    otra_cantidad = lote(api, a, parque, 4, **clave)

    assert primera.status_code == segunda.status_code == 201
    assert segunda.json() == primera.json()
    assert otra_cantidad.status_code == 409
    assert len(ubicaciones(api, a, parque)) == 3


def test_openapi_documenta_el_alta_por_cantidad(api: TestClient) -> None:
    rutas = api.get("/openapi.json").json()["paths"]
    post = rutas[f"{PARQUES}/{{parque_id}}/nucleos"]["post"]
    esquema = post["responses"]["201"]["content"]["application/json"]["schema"]
    assert esquema["$ref"].endswith("/NucleosLoteCreados")
    assert "Idempotency-Key" in [p["name"] for p in post["parameters"]]
