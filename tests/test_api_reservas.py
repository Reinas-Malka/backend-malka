"""POST /api/v1/pedidos/{id}/reservas de punta a punta (#35, ADR 0014).

Los pedidos se crean con la API; las celdas enjauladas se cargan directo como
duenio porque todavia no existen las transiciones por lote (#28). La
concurrencia (el DoD) esta en test_concurrencia_reservas.py.
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

PEDIDOS = "/api/v1/pedidos"

# De hijas a padres. Las primeras se vacian despues de cada test; el resto
# es de los criaderos y se borra al final del modulo. idempotencia la llenan
# las reservas con Idempotency-Key.
POR_TEST = ("idempotencia", "reserva", "pedido_item", "pedido", "celda", "tanda")
DEL_CRIADERO = ("nucleo", "parque", "cliente", "item_vendible", "raza")


@dataclass(frozen=True)
class Criadero:
    tenant: uuid.UUID
    cliente: uuid.UUID
    item: uuid.UUID
    nucleo: uuid.UUID
    italiana: uuid.UUID
    carniola: uuid.UUID


def _id(c: Connection, sql: str, **parametros: Any) -> uuid.UUID:
    return uuid.UUID(str(c.execute(text(sql), parametros).scalar_one()))


def _cargar(c: Connection, tenant: uuid.UUID) -> Criadero:
    crear_tenant(c, tenant)
    fijar_tenant(c, tenant)
    parque = _id(
        c,
        "INSERT INTO parque (nombre, ubicacion, capacidad_max) "
        "VALUES ('P1', 'Predio', 100) RETURNING id",
    )
    return Criadero(
        tenant=tenant,
        cliente=_id(
            c,
            "INSERT INTO cliente (nombre, pais, tipo, condicion_iva, cuit_o_tax_id) "
            "VALUES ('Apicola del Sur', 'AR', 'nacional', 'monotributo', "
            "'20123456786') RETURNING id",
        ),
        item=_id(
            c,
            "INSERT INTO item_vendible (nombre) VALUES ('Reina fecundada') "
            "RETURNING id",
        ),
        nucleo=_id(
            c,
            "INSERT INTO nucleo (parque_id, fila, posicion) "
            "VALUES (:parque, 1, 1) RETURNING id",
            parque=parque,
        ),
        italiana=_id(c, "INSERT INTO raza (nombre) VALUES ('Italiana') RETURNING id"),
        carniola=_id(c, "INSERT INTO raza (nombre) VALUES ('Carniola') RETURNING id"),
    )


@pytest.fixture(scope="module")
def criaderos(motor_owner: Engine) -> Iterator[tuple[Criadero, Criadero]]:
    """A es el que reserva; B tiene los mismos datos para intentar alcanzarlos."""
    with motor_owner.begin() as c:
        a = _cargar(c, uuid.uuid4())
    with motor_owner.begin() as c:
        b = _cargar(c, uuid.uuid4())
    yield a, b
    for criadero in (a, b):
        with motor_owner.begin() as c:
            fijar_tenant(c, criadero.tenant)
            for tabla in DEL_CRIADERO:
                c.execute(text(f"DELETE FROM {tabla} WHERE true"))  # noqa: S608
            c.execute(
                text("DELETE FROM tenant WHERE id = :id"), {"id": criadero.tenant}
            )


@pytest.fixture(autouse=True)
def sin_datos(
    motor_owner: Engine, criaderos: tuple[Criadero, Criadero]
) -> Iterator[None]:
    """Cada test arranca sin pedidos ni reinas."""
    yield
    for criadero in criaderos:
        with motor_owner.begin() as c:
            fijar_tenant(c, criadero.tenant)
            for tabla in POR_TEST:
                c.execute(text(f"DELETE FROM {tabla} WHERE true"))  # noqa: S608


@pytest.fixture
def a(criaderos: tuple[Criadero, Criadero]) -> Criadero:
    return criaderos[0]


@pytest.fixture
def api() -> TestClient:
    return TestClient(app)


def como(tenant: uuid.UUID, rol: str = "ventas") -> dict[str, str]:
    token = emitir(**{"custom:tenant_id": str(tenant), "cognito:groups": [rol]})
    return {"Authorization": f"Bearer {token}"}


def codigo(respuesta: Any) -> str:
    return str(respuesta.json()["error"]["code"])


def enjaular(
    motor_owner: Engine, criadero: Criadero, raza: uuid.UUID, cantidad: int
) -> None:
    """Una tanda de la raza con `cantidad` celdas enjauladas."""
    with motor_owner.begin() as c:
        fijar_tenant(c, criadero.tenant)
        tanda = _id(
            c,
            "INSERT INTO tanda (raza_id, fecha_traslarve) "
            "VALUES (:raza, '2026-09-20') RETURNING id",
            raza=raza,
        )
        c.execute(
            text(
                "INSERT INTO celda (tanda_id, estado, nucleo_id, fecha_introduccion) "
                "SELECT :tanda, 'enjaulada', :nucleo, '2026-09-30' "
                "FROM generate_series(1, :cantidad)"
            ),
            {"tanda": tanda, "nucleo": criadero.nucleo, "cantidad": cantidad},
        )


def crear_pedido(
    api: TestClient, criadero: Criadero, *lineas: tuple[uuid.UUID, int]
) -> str:
    """Un pedido nacional en borrador con una linea por (raza, cantidad)."""
    respuesta = api.post(
        PEDIDOS,
        json={
            "cliente_id": str(criadero.cliente),
            "moneda": "ARS",
            "fecha_entrega_solicitada": "2026-11-30",
            "items": [
                {
                    "item_vendible_id": str(criadero.item),
                    "raza_id": str(raza),
                    "cantidad": cantidad,
                    "precio_unitario": "35.00",
                }
                for raza, cantidad in lineas
            ],
        },
        headers=como(criadero.tenant),
    )
    assert respuesta.status_code == 201, respuesta.json()
    return str(respuesta.json()["id"])


def reservar(
    api: TestClient,
    criadero: Criadero,
    pedido_id: str,
    rol: str = "ventas",
    **headers: str,
) -> Any:
    return api.post(
        f"{PEDIDOS}/{pedido_id}/reservas",
        headers={**como(criadero.tenant, rol), **headers},
    )


def estado_del_pedido(api: TestClient, criadero: Criadero, pedido_id: str) -> str:
    respuesta = api.get(f"{PEDIDOS}/{pedido_id}", headers=como(criadero.tenant))
    return str(respuesta.json()["estado"])


def reservadas(api: TestClient, criadero: Criadero, raza: uuid.UUID) -> int:
    respuesta = api.get(
        "/api/v1/disponibilidad",
        params={"raza_id": str(raza)},
        headers=como(criadero.tenant),
    )
    (fila,) = respuesta.json()
    return int(fila["reservadas"])


def test_reserva_todas_las_lineas_y_el_pedido_pasa_a_reservado(
    api: TestClient, motor_owner: Engine, a: Criadero
) -> None:
    enjaular(motor_owner, a, a.italiana, 10)
    pedido = crear_pedido(api, a, (a.italiana, 3), (a.italiana, 4))

    respuesta = reservar(api, a, pedido)

    assert respuesta.status_code == 201, respuesta.json()
    reservas = respuesta.json()
    assert [r["cantidad"] for r in reservas] == [3, 4]
    assert {r["raza_id"] for r in reservas} == {str(a.italiana)}
    assert {r["estado"] for r in reservas} == {"activa"}
    assert estado_del_pedido(api, a, pedido) == "reservado"
    assert reservadas(api, a, a.italiana) == 7


def test_si_no_alcanza_es_409_con_el_disponible_por_raza(
    api: TestClient, motor_owner: Engine, a: Criadero
) -> None:
    enjaular(motor_owner, a, a.italiana, 5)
    pedido = crear_pedido(api, a, (a.italiana, 8))

    respuesta = reservar(api, a, pedido)

    assert respuesta.status_code == 409
    assert codigo(respuesta) == "stock_insuficiente"
    assert respuesta.json()["error"]["details"] == {
        "razas": [
            {"raza_id": str(a.italiana), "pedidas": 8, "disponible": 5, "faltan": 3}
        ]
    }
    # No se guardo nada.
    assert estado_del_pedido(api, a, pedido) == "borrador"
    assert reservadas(api, a, a.italiana) == 0


def test_si_una_raza_no_alcanza_no_se_reserva_ninguna(
    api: TestClient, motor_owner: Engine, a: Criadero
) -> None:
    enjaular(motor_owner, a, a.italiana, 10)
    enjaular(motor_owner, a, a.carniola, 2)
    pedido = crear_pedido(api, a, (a.italiana, 5), (a.carniola, 3))

    respuesta = reservar(api, a, pedido)

    assert respuesta.status_code == 409
    # Solo aparece la que no alcanza.
    (carniola,) = respuesta.json()["error"]["details"]["razas"]
    assert carniola["raza_id"] == str(a.carniola)
    assert reservadas(api, a, a.italiana) == 0


def test_las_lineas_de_una_misma_raza_se_suman(
    api: TestClient, motor_owner: Engine, a: Criadero
) -> None:
    # Cada linea entra sola (4 y 4 con 6), pero juntas no.
    enjaular(motor_owner, a, a.italiana, 6)
    pedido = crear_pedido(api, a, (a.italiana, 4), (a.italiana, 4))

    respuesta = reservar(api, a, pedido)

    assert respuesta.status_code == 409
    (italiana,) = respuesta.json()["error"]["details"]["razas"]
    assert italiana["pedidas"] == 8


def test_cuenta_lo_reservado_por_otros_pedidos(
    api: TestClient, motor_owner: Engine, a: Criadero
) -> None:
    enjaular(motor_owner, a, a.italiana, 10)
    primero = crear_pedido(api, a, (a.italiana, 7))
    segundo = crear_pedido(api, a, (a.italiana, 4))

    assert reservar(api, a, primero).status_code == 201
    respuesta = reservar(api, a, segundo)

    assert respuesta.status_code == 409
    (italiana,) = respuesta.json()["error"]["details"]["razas"]
    assert italiana["disponible"] == 3


def test_justo_lo_que_hay_entra(
    api: TestClient, motor_owner: Engine, a: Criadero
) -> None:
    enjaular(motor_owner, a, a.italiana, 10)
    pedido = crear_pedido(api, a, (a.italiana, 10))

    assert reservar(api, a, pedido).status_code == 201


def test_reservar_dos_veces_es_409_y_no_cuenta_doble(
    api: TestClient, motor_owner: Engine, a: Criadero
) -> None:
    enjaular(motor_owner, a, a.italiana, 10)
    pedido = crear_pedido(api, a, (a.italiana, 3))
    assert reservar(api, a, pedido).status_code == 201

    respuesta = reservar(api, a, pedido)

    assert respuesta.status_code == 409
    assert codigo(respuesta) == "pedido_no_reservable"
    assert respuesta.json()["error"]["details"] == {"estado": "reservado"}
    assert reservadas(api, a, a.italiana) == 3


# --- Idempotency-Key (frontend #14) ---


def test_con_la_misma_clave_un_reintento_devuelve_las_mismas_reservas(
    api: TestClient, motor_owner: Engine, a: Criadero
) -> None:
    enjaular(motor_owner, a, a.italiana, 10)
    pedido = crear_pedido(api, a, (a.italiana, 3))
    clave = str(uuid.uuid4())

    primera = reservar(api, a, pedido, **{"Idempotency-Key": clave})
    segunda = reservar(api, a, pedido, **{"Idempotency-Key": clave})

    assert primera.status_code == segunda.status_code == 201
    assert segunda.headers["Idempotent-Replayed"] == "true"
    assert segunda.json() == primera.json()
    assert reservadas(api, a, a.italiana) == 3


def test_tras_stock_insuficiente_la_misma_clave_vuelve_a_intentar(
    api: TestClient, motor_owner: Engine, a: Criadero
) -> None:
    """El 409 no se guarda: el reintento ve la disponibilidad nueva."""
    enjaular(motor_owner, a, a.italiana, 3)
    pedido = crear_pedido(api, a, (a.italiana, 5))
    clave = {"Idempotency-Key": str(uuid.uuid4())}
    assert codigo(reservar(api, a, pedido, **clave)) == "stock_insuficiente"

    enjaular(motor_owner, a, a.italiana, 2)
    reintento = reservar(api, a, pedido, **clave)

    assert reintento.status_code == 201, reintento.json()
    assert "Idempotent-Replayed" not in reintento.headers
    assert estado_del_pedido(api, a, pedido) == "reservado"
    assert reservadas(api, a, a.italiana) == 5


def test_la_misma_clave_en_otro_pedido_es_409(
    api: TestClient, motor_owner: Engine, a: Criadero
) -> None:
    enjaular(motor_owner, a, a.italiana, 10)
    uno = crear_pedido(api, a, (a.italiana, 3))
    otro = crear_pedido(api, a, (a.italiana, 4))
    clave = {"Idempotency-Key": str(uuid.uuid4())}
    assert reservar(api, a, uno, **clave).status_code == 201

    respuesta = reservar(api, a, otro, **clave)

    assert respuesta.status_code == 409
    assert codigo(respuesta) == "idempotency_key_reutilizada"
    assert estado_del_pedido(api, a, otro) == "borrador"


def test_las_reinas_de_otro_criadero_no_cuentan(
    api: TestClient, motor_owner: Engine, criaderos: tuple[Criadero, Criadero]
) -> None:
    a, b = criaderos
    enjaular(motor_owner, b, b.italiana, 10)
    pedido = crear_pedido(api, a, (a.italiana, 1))

    respuesta = reservar(api, a, pedido)

    assert respuesta.status_code == 409
    assert codigo(respuesta) == "stock_insuficiente"


def test_pedido_de_otro_criadero_responde_igual_que_uno_inexistente(
    api: TestClient, motor_owner: Engine, criaderos: tuple[Criadero, Criadero]
) -> None:
    a, b = criaderos
    enjaular(motor_owner, b, b.italiana, 10)
    pedido_b = crear_pedido(api, b, (b.italiana, 1))

    ajeno = reservar(api, a, pedido_b)
    inexistente = reservar(api, a, str(uuid.uuid4()))

    assert ajeno.status_code == inexistente.status_code == 404
    assert codigo(ajeno) == codigo(inexistente) == "pedido_no_encontrado"
    # Y el pedido de B sigue sin reservar.
    assert estado_del_pedido(api, b, pedido_b) == "borrador"


@pytest.mark.parametrize("rol", ["produccion", "lectura"])
def test_solo_admin_y_ventas_reservan(
    api: TestClient, motor_owner: Engine, a: Criadero, rol: str
) -> None:
    enjaular(motor_owner, a, a.italiana, 10)
    pedido = crear_pedido(api, a, (a.italiana, 1))

    respuesta = reservar(api, a, pedido, rol)

    assert respuesta.status_code == 403
    assert estado_del_pedido(api, a, pedido) == "borrador"


def test_admin_reserva(api: TestClient, motor_owner: Engine, a: Criadero) -> None:
    enjaular(motor_owner, a, a.italiana, 10)
    pedido = crear_pedido(api, a, (a.italiana, 1))

    assert reservar(api, a, pedido, "admin").status_code == 201


def test_sin_token_es_401(api: TestClient) -> None:
    assert api.post(f"{PEDIDOS}/{uuid.uuid4()}/reservas").status_code == 401


def test_id_que_no_es_uuid_es_422(api: TestClient, a: Criadero) -> None:
    assert reservar(api, a, "no-es-uuid").status_code == 422
