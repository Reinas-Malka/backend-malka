"""Bancos: identificador automatico y capacidad fija desde el alta.

Sin identificador, el alta asigna el siguiente de la serie B-01, B-02, ...
del criadero; si viene, se respeta. La capacidad se elige en el alta (276
por defecto) y despues no se edita: un PATCH con capacidad_max es 422.
Los tests comunes de bancos (roles, 404 entre criaderos, baja logica,
paginacion) estan en test_api_produccion.py; la concurrencia del
identificador, en test_concurrencia_banco.py, y la regla pura, en
test_bancos.py.
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

BANCOS = "/api/v1/bancos"

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
            for tabla in ("idempotencia", "banco"):
                c.execute(text(f"DELETE FROM {tabla} WHERE true"))  # noqa: S608


@pytest.fixture
def api() -> TestClient:
    return TestClient(app)


def como(tenant: uuid.UUID, rol: str = "produccion") -> dict[str, str]:
    token = emitir(**{"custom:tenant_id": str(tenant), "cognito:groups": [rol]})
    return {"Authorization": f"Bearer {token}"}


def alta(
    api: TestClient, tenant: uuid.UUID, datos: dict[str, Any] | None = None
) -> dict[str, Any]:
    respuesta = api.post(BANCOS, json=datos or {}, headers=como(tenant))
    assert respuesta.status_code == 201, respuesta.json()
    cuerpo: dict[str, Any] = respuesta.json()
    return cuerpo


def test_sin_identificador_asigna_la_serie(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos

    creados = [alta(api, a)["identificador"] for _ in range(3)]

    assert creados == ["B-01", "B-02", "B-03"]


def test_el_identificador_a_mano_se_respeta(
    api: TestClient, criaderos: Criaderos
) -> None:
    """Uno fuera de la serie no la mueve; uno de la serie sigue desde ahi."""
    a, _ = criaderos

    a_mano = alta(api, a, {"identificador": "Banco galpon"})
    primero = alta(api, a)
    salto = alta(api, a, {"identificador": "B-07"})
    siguiente = alta(api, a)

    assert a_mano["identificador"] == "Banco galpon"
    assert primero["identificador"] == "B-01"
    assert salto["identificador"] == "B-07"
    # No rellena huecos: sigue al mas alto.
    assert siguiente["identificador"] == "B-08"


def test_un_banco_dado_de_baja_no_libera_su_numero(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    primero = alta(api, a)
    api.delete(f"{BANCOS}/{primero['id']}", headers=como(a))

    assert alta(api, a)["identificador"] == "B-02"


def test_a_mano_repetido_es_409(api: TestClient, criaderos: Criaderos) -> None:
    a, _ = criaderos
    alta(api, a)

    respuesta = api.post(BANCOS, json={"identificador": "B-01"}, headers=como(a))

    assert respuesta.status_code == 409
    assert respuesta.json()["error"]["code"] == "banco_duplicado"


def test_cada_criadero_tiene_su_serie(api: TestClient, criaderos: Criaderos) -> None:
    """RLS: los bancos de A no cuentan para numerar los de B."""
    a, b = criaderos
    alta(api, a)
    alta(api, a)

    assert alta(api, b)["identificador"] == "B-01"


def test_reintento_con_la_misma_clave_no_consume_un_numero(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    headers = {**como(a), "Idempotency-Key": str(uuid.uuid4())}

    primera = api.post(BANCOS, json={}, headers=headers)
    segunda = api.post(BANCOS, json={}, headers=headers)

    assert primera.status_code == segunda.status_code == 201
    assert segunda.json() == primera.json()
    assert alta(api, a)["identificador"] == "B-02"


def test_capacidad_por_defecto_u_otra_en_el_alta(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos

    por_defecto = alta(api, a)
    elegida = alta(api, a, {"capacidad_max": 300})

    assert por_defecto["capacidad_max"] == 276  # confirmado por la clienta el 16/10
    assert elegida["capacidad_max"] == 300


@pytest.mark.parametrize(
    "cambio",
    [{"capacidad_max": 300}, {"identificador": "B-09", "capacidad_max": 300}],
    ids=["sola", "con_otro_campo"],
)
def test_patch_de_capacidad_es_422(
    api: TestClient, criaderos: Criaderos, cambio: dict[str, Any]
) -> None:
    a, _ = criaderos
    banco = alta(api, a)
    url = f"{BANCOS}/{banco['id']}"

    respuesta = api.patch(url, json=cambio, headers=como(a))

    assert respuesta.status_code == 422
    assert respuesta.json()["error"]["code"] == "validacion"
    assert api.get(url, headers=como(a)).json() == banco


def test_patch_renombra_y_null_es_422(api: TestClient, criaderos: Criaderos) -> None:
    a, _ = criaderos
    banco = alta(api, a)
    url = f"{BANCOS}/{banco['id']}"

    nulo = api.patch(url, json={"identificador": None}, headers=como(a))
    renombrado = api.patch(url, json={"identificador": "Banco sur"}, headers=como(a))

    assert nulo.status_code == 422
    assert renombrado.status_code == 200
    assert renombrado.json() == {**banco, "identificador": "Banco sur"}
