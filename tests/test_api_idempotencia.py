"""Idempotencia de punta a punta (#25), con PostgreSQL real y RLS.

DoD: dos POST con la misma clave generan un solo registro, y la clave vence a
las 24 horas. Cubre ademas el caso de fuga entre tenants (AGENTS.md, regla 5)
y dos pedidos simultaneos con la misma clave.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from app import db
from app.idempotencia import (
    HEADER_REPETIDA,
    PedidoIdempotente,
    huella,
    responder_una_vez,
)
from app.main import app
from tests.conftest import BaseDePrueba, crear_tenant, emitir, fijar_tenant

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("cognito", "con_motor_app"),
]

URL = "/api/v1/clientes"

NACIONAL: dict[str, Any] = {
    "nombre": "Apicola del Sur",
    "pais": "AR",
    "tipo": "nacional",
    "condicion_iva": "monotributo",
    "cuit_o_tax_id": "20123456786",
}

EXPORTACION: dict[str, Any] = {
    "nombre": "Bienenzucht GmbH",
    "pais": "DE",
    "tipo": "exportacion",
    "condicion_iva": "cliente_exterior",
    "cuit_o_tax_id": "DE123456789",
}


@pytest.fixture(scope="module")
def criaderos(motor_owner: Engine) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    a, b = uuid.uuid4(), uuid.uuid4()
    with motor_owner.begin() as c:
        for tenant in (a, b):
            crear_tenant(c, tenant)
    yield a, b
    with motor_owner.begin() as c:
        for tenant in (a, b):
            c.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": tenant})


@pytest.fixture(autouse=True)
def limpio(
    motor_owner: Engine, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> Iterator[None]:
    """Cada test arranca sin clientes ni claves."""
    yield
    for tenant in criaderos:
        with motor_owner.begin() as c:
            fijar_tenant(c, tenant)
            c.execute(text("DELETE FROM idempotencia WHERE true"))
            c.execute(text("DELETE FROM cliente WHERE true"))


@pytest.fixture
def api() -> TestClient:
    return TestClient(app)


def headers(tenant: uuid.UUID, clave: str | None = None) -> dict[str, str]:
    token = emitir(**{"custom:tenant_id": str(tenant), "cognito:groups": ["ventas"]})
    resultado = {"Authorization": f"Bearer {token}"}
    if clave is not None:
        resultado["Idempotency-Key"] = clave
    return resultado


CONTAR = {
    "cliente": text("SELECT count(*) FROM cliente"),
    "idempotencia": text("SELECT count(*) FROM idempotencia"),
}


def contar(motor: Engine, tenant: uuid.UUID, tabla: str) -> int:
    with motor.begin() as c:
        fijar_tenant(c, tenant)
        return int(c.execute(CONTAR[tabla]).scalar_one())


def vencer(motor_owner: Engine, tenant: uuid.UUID, clave: str, en: str) -> None:
    """Corre el vencimiento de la clave: en el pasado o apenas en el futuro."""
    with motor_owner.begin() as c:
        fijar_tenant(c, tenant)
        c.execute(
            text(
                "UPDATE idempotencia SET expira_en = now() + CAST(:en AS interval) "
                "WHERE clave = :clave"
            ),
            {"en": en, "clave": clave},
        )


# --- DoD: dos POST con la misma clave, un solo registro ---


def test_dos_post_con_la_misma_clave_crean_un_solo_cliente(
    api: TestClient, motor_owner: Engine, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = criaderos
    clave = str(uuid.uuid4())

    primera = api.post(URL, json=NACIONAL, headers=headers(a, clave))
    segunda = api.post(URL, json=NACIONAL, headers=headers(a, clave))

    assert primera.status_code == segunda.status_code == 201
    assert segunda.json() == primera.json()
    assert "idempotent-replayed" not in primera.headers
    assert segunda.headers["idempotent-replayed"] == "true"
    assert contar(motor_owner, a, "cliente") == 1


def test_el_mismo_json_con_otro_formato_es_el_mismo_pedido(
    api: TestClient, motor_owner: Engine, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = criaderos
    clave = str(uuid.uuid4())
    api.post(URL, json=NACIONAL, headers=headers(a, clave))

    reordenado = dict(reversed(NACIONAL.items()))
    respuesta = api.post(URL, json=reordenado, headers=headers(a, clave))

    assert respuesta.status_code == 201
    assert respuesta.headers["idempotent-replayed"] == "true"
    assert contar(motor_owner, a, "cliente") == 1


def test_la_misma_clave_con_otro_pedido_responde_409(
    api: TestClient, motor_owner: Engine, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = criaderos
    clave = str(uuid.uuid4())
    api.post(URL, json=NACIONAL, headers=headers(a, clave))

    respuesta = api.post(URL, json=EXPORTACION, headers=headers(a, clave))

    assert respuesta.status_code == 409
    assert respuesta.json()["error"]["code"] == "idempotency_key_reutilizada"
    assert contar(motor_owner, a, "cliente") == 1


def test_sin_header_se_comporta_como_un_post_comun(
    api: TestClient, motor_owner: Engine, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = criaderos

    assert api.post(URL, json=NACIONAL, headers=headers(a)).status_code == 201
    assert api.post(URL, json=EXPORTACION, headers=headers(a)).status_code == 201
    assert contar(motor_owner, a, "cliente") == 2
    assert contar(motor_owner, a, "idempotencia") == 0


# --- DoD: vencimiento a las 24 horas ---


def test_la_clave_vale_24_horas(
    api: TestClient, motor_owner: Engine, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = criaderos
    api.post(URL, json=NACIONAL, headers=headers(a, "clave-24h"))

    with motor_owner.begin() as c:
        fijar_tenant(c, a)
        ventana = c.execute(
            text("SELECT expira_en - creado_en FROM idempotencia")
        ).scalar_one()

    assert ventana.total_seconds() == 24 * 3600


def test_dentro_de_la_ventana_se_repite_la_respuesta(
    api: TestClient, motor_owner: Engine, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = criaderos
    original = api.post(URL, json=NACIONAL, headers=headers(a, "casi-vencida"))
    # Le queda un minuto: sigue valiendo.
    vencer(motor_owner, a, "casi-vencida", "1 minute")

    repetida = api.post(URL, json=NACIONAL, headers=headers(a, "casi-vencida"))

    assert repetida.headers["idempotent-replayed"] == "true"
    assert repetida.json() == original.json()


def test_pasadas_las_24_horas_la_clave_es_nueva(
    api: TestClient, motor_owner: Engine, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = criaderos
    api.post(URL, json=NACIONAL, headers=headers(a, "vencida"))
    vencer(motor_owner, a, "vencida", "-1 second")

    # Otro pedido con la misma clave: ya no es 409, crea un cliente nuevo.
    respuesta = api.post(URL, json=EXPORTACION, headers=headers(a, "vencida"))

    assert respuesta.status_code == 201
    assert "idempotent-replayed" not in respuesta.headers
    assert contar(motor_owner, a, "cliente") == 2
    # La clave se renovo: vuelve a valer 24 horas, ahora para el pedido nuevo.
    assert contar(motor_owner, a, "idempotencia") == 1
    repetida = api.post(URL, json=EXPORTACION, headers=headers(a, "vencida"))
    assert repetida.headers["idempotent-replayed"] == "true"


# --- errores: no se guardan ---


def test_si_el_pedido_falla_la_clave_no_queda_usada(
    api: TestClient, motor_owner: Engine, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = criaderos
    api.post(URL, json=NACIONAL, headers=headers(a))

    # Mismo CUIT: el repositorio responde 409 y la transaccion se deshace.
    fallida = api.post(URL, json=NACIONAL, headers=headers(a, "reintento"))
    assert fallida.status_code == 409
    assert fallida.json()["error"]["code"] != "idempotency_key_reutilizada"
    assert contar(motor_owner, a, "idempotencia") == 0

    # El reintento corregido, con la misma clave, funciona.
    corregida = api.post(URL, json=EXPORTACION, headers=headers(a, "reintento"))
    assert corregida.status_code == 201
    assert contar(motor_owner, a, "cliente") == 2


# --- aislamiento entre tenants (AGENTS.md, regla 5) ---


def test_la_misma_clave_en_otro_criadero_es_independiente(
    api: TestClient, motor_owner: Engine, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, b = criaderos
    clave = "clave-compartida"
    de_a = api.post(URL, json=NACIONAL, headers=headers(a, clave))

    # B usa la misma clave con otro pedido: no es 409 ni le llega el cliente de A.
    de_b = api.post(URL, json=EXPORTACION, headers=headers(b, clave))

    assert de_b.status_code == 201
    assert "idempotent-replayed" not in de_b.headers
    assert de_b.json()["id"] != de_a.json()["id"]
    assert contar(motor_owner, a, "idempotencia") == 1
    assert contar(motor_owner, b, "idempotencia") == 1


def test_sin_tenant_no_se_ve_ninguna_clave(
    api: TestClient, motor_app: Engine, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = criaderos
    api.post(URL, json=NACIONAL, headers=headers(a, "visible-solo-para-a"))

    with motor_app.connect() as c:
        assert c.execute(text("SELECT count(*) FROM idempotencia")).scalar() == 0


# --- concurrencia ---


def test_dos_pedidos_simultaneos_crean_un_solo_recurso(
    base_de_prueba: BaseDePrueba, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """El segundo pedido espera al primero en el indice unico y repite su respuesta."""
    a, _ = criaderos
    motor = create_engine(base_de_prueba.url_app, pool_size=2, max_overflow=0)
    pedido = PedidoIdempotente(
        clave="simultanea", hash_request=huella("POST", URL, b"{}")
    )
    creados: list[int] = []

    def crear() -> dict[str, int]:
        creados.append(1)
        return {"numero": len(creados)}

    resultado: dict[str, JSONResponse] = {}

    def segundo_pedido() -> None:
        with Session(motor) as sesion, sesion.begin():
            sesion.execute(db.FIJAR_TENANT, {"tenant_id": str(a)})
            resultado["segundo"] = responder_una_vez(sesion, pedido, crear)

    try:
        with Session(motor) as primera, primera.begin():
            primera.execute(db.FIJAR_TENANT, {"tenant_id": str(a)})
            original = responder_una_vez(primera, pedido, crear)

            hilo = threading.Thread(target=segundo_pedido)
            hilo.start()
            time.sleep(0.5)
            # Mientras el primero no termina, el segundo queda esperando.
            assert hilo.is_alive()
        # Al salir del with, el primero hace commit y libera al segundo.
        hilo.join(timeout=10)

        assert not hilo.is_alive()
        assert creados == [1]
        segundo = resultado["segundo"]
        assert segundo.headers[HEADER_REPETIDA] == "true"
        assert segundo.body == original.body
    finally:
        motor.dispose()
