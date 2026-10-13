"""Endpoint de pedidos de punta a punta (#34, DoD).

Requests HTTP reales contra la app de produccion: token firmado, sesion con
el tenant del token, schemas y PostgreSQL con RLS (migracion 0008). Cubre
tambien el repositorio, que no tiene tests propios.
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
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

URL = "/api/v1/pedidos"

# De hijas a padres, para limpiar sin chocar con las claves foraneas.
TABLAS = ("pedido_item", "pedido", "idempotencia", "cliente", "item_vendible", "raza")


@dataclass(frozen=True)
class Datos:
    """Lo que cada criadero tiene cargado antes de los tests."""

    nacional: uuid.UUID
    exportacion: uuid.UUID
    inactivo: uuid.UUID
    item: uuid.UUID
    raza: uuid.UUID
    raza_inactiva: uuid.UUID


def _id(c: Connection, sql: str) -> uuid.UUID:
    valor = c.execute(text(sql)).scalar_one()
    assert isinstance(valor, uuid.UUID)
    return valor


def _cargar(c: Connection, tenant: uuid.UUID) -> Datos:
    crear_tenant(c, tenant)
    fijar_tenant(c, tenant)
    return Datos(
        nacional=_id(
            c,
            "INSERT INTO cliente (nombre, pais, tipo, condicion_iva, cuit_o_tax_id) "
            "VALUES ('Apicola del Sur', 'AR', 'nacional', 'monotributo', "
            "'20123456786') RETURNING id",
        ),
        exportacion=_id(
            c,
            "INSERT INTO cliente (nombre, pais, tipo, condicion_iva, cuit_o_tax_id) "
            "VALUES ('Bienenzucht GmbH', 'DE', 'exportacion', 'cliente_exterior', "
            "'DE123456789') RETURNING id",
        ),
        inactivo=_id(
            c,
            "INSERT INTO cliente (nombre, pais, tipo, condicion_iva, cuit_o_tax_id, "
            "activo) VALUES ('Dado de baja', 'AR', 'nacional', 'monotributo', "
            "'30123456781', false) RETURNING id",
        ),
        item=_id(
            c,
            "INSERT INTO item_vendible (nombre) VALUES ('Reina fecundada') "
            "RETURNING id",
        ),
        raza=_id(c, "INSERT INTO raza (nombre) VALUES ('Italiana') RETURNING id"),
        raza_inactiva=_id(
            c, "INSERT INTO raza (nombre, activo) VALUES ('Vieja', false) RETURNING id"
        ),
    )


@pytest.fixture(scope="module")
def criaderos(motor_owner: Engine) -> Iterator[tuple[uuid.UUID, Datos, Datos]]:
    """El criadero A con sus datos, y los datos de B para intentar alcanzarlos."""
    a, b = uuid.uuid4(), uuid.uuid4()
    with motor_owner.begin() as c:
        datos_a = _cargar(c, a)
    with motor_owner.begin() as c:
        datos_b = _cargar(c, b)
    yield a, datos_a, datos_b
    for tenant in (a, b):
        with motor_owner.begin() as c:
            fijar_tenant(c, tenant)
            for tabla in TABLAS:
                c.execute(text(f"DELETE FROM {tabla} WHERE true"))  # noqa: S608
            c.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": tenant})


@pytest.fixture(autouse=True)
def sin_pedidos(
    motor_owner: Engine, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> Iterator[None]:
    """Cada test arranca sin pedidos: los datos de uno no afectan a otro."""
    yield
    with motor_owner.begin() as c:
        fijar_tenant(c, criaderos[0])
        for tabla in ("pedido_item", "pedido", "idempotencia"):
            c.execute(text(f"DELETE FROM {tabla} WHERE true"))  # noqa: S608


@pytest.fixture
def api() -> TestClient:
    return TestClient(app)


def como(tenant: uuid.UUID, rol: str = "ventas") -> dict[str, str]:
    token = emitir(**{"custom:tenant_id": str(tenant), "cognito:groups": [rol]})
    return {"Authorization": f"Bearer {token}"}


def codigo(respuesta: Any) -> str:
    return str(respuesta.json()["error"]["code"])


def nacional(datos: Datos, **cambios: Any) -> dict[str, Any]:
    return {
        "cliente_id": str(datos.nacional),
        "moneda": "ARS",
        "fecha_entrega_solicitada": "2026-11-30",
        "items": [
            {
                "item_vendible_id": str(datos.item),
                "raza_id": str(datos.raza),
                "cantidad": 10,
                "precio_unitario": "35000.50",
            }
        ],
        **cambios,
    }


def exportacion(datos: Datos, **cambios: Any) -> dict[str, Any]:
    completo = {
        "cliente_id": str(datos.exportacion),
        "moneda": "EUR",
        "destino_pais": "DE",
        "incoterm": "CPT",
    }
    return nacional(datos, **{**completo, **cambios})


def cantidad_de_pedidos(motor_owner: Engine, tenant: uuid.UUID) -> int:
    with motor_owner.begin() as c:
        fijar_tenant(c, tenant)
        return int(c.execute(text("SELECT count(*) FROM pedido")).scalar_one())


# --- DoD ---


def test_crea_un_pedido_nacional(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    tenant, datos, _ = criaderos

    respuesta = api.post(URL, json=nacional(datos), headers=como(tenant))

    assert respuesta.status_code == 201, respuesta.json()
    pedido = respuesta.json()
    assert pedido["estado"] == "borrador"
    assert pedido["moneda"] == "ARS"
    assert pedido["destino_pais"] is None
    assert pedido["incoterm"] is None
    [item] = pedido["items"]
    assert item["cantidad"] == 10
    assert item["precio_unitario"] == "35000.50"
    # Copiada del item vendible, que tiene el default de la 0004.
    assert item["alicuota"] == "0.21"


def test_crea_un_pedido_de_exportacion_completo(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    tenant, datos, _ = criaderos

    respuesta = api.post(URL, json=exportacion(datos), headers=como(tenant))

    assert respuesta.status_code == 201, respuesta.json()
    pedido = respuesta.json()
    assert (pedido["moneda"], pedido["destino_pais"], pedido["incoterm"]) == (
        "EUR",
        "DE",
        "CPT",
    )


def test_exportacion_sin_destino_pais_es_422(
    api: TestClient,
    criaderos: tuple[uuid.UUID, Datos, Datos],
    motor_owner: Engine,
) -> None:
    tenant, datos, _ = criaderos
    cuerpo = exportacion(datos)
    del cuerpo["destino_pais"]

    respuesta = api.post(URL, json=cuerpo, headers=como(tenant))

    assert respuesta.status_code == 422
    assert codigo(respuesta) == "pedido_incoherente"
    assert cantidad_de_pedidos(motor_owner, tenant) == 0


# --- Reglas que necesitan la base ---


def test_exportacion_sin_incoterm_se_acepta(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    """Envio consolidado a Europa: el incoterm queda vacio."""
    tenant, datos, _ = criaderos

    respuesta = api.post(
        URL, json=exportacion(datos, incoterm=None), headers=como(tenant)
    )

    assert respuesta.status_code == 201, respuesta.json()
    assert respuesta.json()["incoterm"] is None


def test_nacional_en_euros_es_422(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    tenant, datos, _ = criaderos

    respuesta = api.post(URL, json=nacional(datos, moneda="EUR"), headers=como(tenant))

    assert respuesta.status_code == 422
    assert codigo(respuesta) == "pedido_incoherente"


def test_cliente_de_otro_criadero_responde_igual_que_uno_inexistente(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    """Si las respuestas difirieran, A podria saber que ids existen en B."""
    tenant, datos, datos_b = criaderos

    de_b = api.post(
        URL,
        json=nacional(datos, cliente_id=str(datos_b.nacional)),
        headers=como(tenant),
    )
    inexistente = api.post(
        URL, json=nacional(datos, cliente_id=str(uuid.uuid4())), headers=como(tenant)
    )

    assert de_b.status_code == inexistente.status_code == 422
    assert codigo(de_b) == codigo(inexistente) == "cliente_inexistente"


def test_cliente_dado_de_baja_es_422(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    tenant, datos, _ = criaderos

    respuesta = api.post(
        URL, json=nacional(datos, cliente_id=str(datos.inactivo)), headers=como(tenant)
    )

    assert respuesta.status_code == 422
    assert codigo(respuesta) == "cliente_inactivo"


@pytest.mark.parametrize(
    ("campo", "valor", "esperado"),
    [
        pytest.param("item_vendible_id", "inexistente", "item_vendible_inexistente"),
        pytest.param("item_vendible_id", "de_b", "item_vendible_inexistente"),
        pytest.param("raza_id", "inexistente", "raza_inexistente"),
        pytest.param("raza_id", "de_b", "raza_inexistente"),
        pytest.param("raza_id", "inactiva", "raza_inexistente"),
    ],
)
def test_item_o_raza_que_no_se_puede_usar_es_422(
    api: TestClient,
    criaderos: tuple[uuid.UUID, Datos, Datos],
    motor_owner: Engine,
    campo: str,
    valor: str,
    esperado: str,
) -> None:
    tenant, datos, datos_b = criaderos
    ids = {
        "inexistente": uuid.uuid4(),
        "de_b": datos_b.item if campo == "item_vendible_id" else datos_b.raza,
        "inactiva": datos.raza_inactiva,
    }
    cuerpo = nacional(datos)
    # Una linea valida y una mala: no tiene que quedar ninguna de las dos.
    cuerpo["items"].append({**cuerpo["items"][0], campo: str(ids[valor])})

    respuesta = api.post(URL, json=cuerpo, headers=como(tenant))

    assert respuesta.status_code == 422
    assert codigo(respuesta) == esperado
    assert respuesta.json()["error"]["details"]["ids"] == [str(ids[valor])]
    assert cantidad_de_pedidos(motor_owner, tenant) == 0


def test_la_alicuota_se_copia_y_no_cambia_con_el_catalogo(
    api: TestClient,
    criaderos: tuple[uuid.UUID, Datos, Datos],
    motor_owner: Engine,
) -> None:
    tenant, datos, _ = criaderos
    creado = api.post(URL, json=nacional(datos), headers=como(tenant)).json()

    with motor_owner.begin() as c:
        fijar_tenant(c, tenant)
        c.execute(
            text("UPDATE item_vendible SET alicuota = 0.105 WHERE id = :id"),
            {"id": datos.item},
        )
        guardada = c.execute(
            text("SELECT alicuota FROM pedido_item WHERE id = :id"),
            {"id": creado["items"][0]["id"]},
        ).scalar_one()
        c.execute(
            text("UPDATE item_vendible SET alicuota = 0.21 WHERE id = :id"),
            {"id": datos.item},
        )

    assert str(guardada) == "0.210"


def test_un_pedido_con_varias_lineas(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    tenant, datos, _ = criaderos
    cuerpo = nacional(datos)
    cuerpo["items"].append({**cuerpo["items"][0], "cantidad": 5})

    respuesta = api.post(URL, json=cuerpo, headers=como(tenant))

    assert respuesta.status_code == 201, respuesta.json()
    assert [item["cantidad"] for item in respuesta.json()["items"]] == [10, 5]


# --- Permisos, formato e idempotencia ---


def test_estado_en_el_cuerpo_es_422(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    tenant, datos, _ = criaderos

    respuesta = api.post(
        URL, json=nacional(datos, estado="entregado"), headers=como(tenant)
    )

    assert respuesta.status_code == 422


def test_sin_token_es_401(api: TestClient) -> None:
    assert api.post(URL, json={}).status_code == 401


@pytest.mark.parametrize("rol", ["produccion", "lectura"])
def test_solo_admin_y_ventas_crean(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos], rol: str
) -> None:
    tenant, datos, _ = criaderos

    respuesta = api.post(URL, json=nacional(datos), headers=como(tenant, rol))

    assert respuesta.status_code == 403


def test_la_misma_clave_crea_un_solo_pedido(
    api: TestClient,
    criaderos: tuple[uuid.UUID, Datos, Datos],
    motor_owner: Engine,
) -> None:
    tenant, datos, _ = criaderos
    headers = {**como(tenant), "Idempotency-Key": str(uuid.uuid4())}

    primera = api.post(URL, json=nacional(datos), headers=headers)
    segunda = api.post(URL, json=nacional(datos), headers=headers)

    assert primera.status_code == segunda.status_code == 201
    assert segunda.headers["Idempotent-Replayed"] == "true"
    assert primera.json() == segunda.json()
    assert cantidad_de_pedidos(motor_owner, tenant) == 1
