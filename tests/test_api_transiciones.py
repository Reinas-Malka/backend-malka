"""POST /api/v1/pedidos/{id}/estado de punta a punta (#36, DoD).

Los pedidos se crean y se reservan con la API; las celdas enjauladas se
cargan directo como duenio porque todavia no existen las transiciones por
lote (#28). La concurrencia esta en test_concurrencia_despacho.py.
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, Row, text

from app.fechas import hoy
from app.main import app
from tests.conftest import crear_tenant, emitir, fijar_tenant

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("cognito", "con_motor_app"),
]

PEDIDOS = "/api/v1/pedidos"

# De hijas a padres. celda va antes que pedido_item: la celda despachada
# apunta a su linea. idempotencia la llenan los pedidos con Idempotency-Key.
POR_TEST = ("idempotencia", "reserva", "celda", "pedido_item", "pedido", "tanda")
DEL_CRIADERO = ("banco", "nucleo", "parque", "cliente", "item_vendible", "raza")


@dataclass(frozen=True)
class Criadero:
    tenant: uuid.UUID
    cliente: uuid.UUID
    item: uuid.UUID
    nucleo: uuid.UUID
    italiana: uuid.UUID
    carniola: uuid.UUID
    norte: uuid.UUID
    sur: uuid.UUID


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
        norte=_id(c, "INSERT INTO banco (identificador) VALUES ('N') RETURNING id"),
        sur=_id(c, "INSERT INTO banco (identificador) VALUES ('S') RETURNING id"),
    )


@pytest.fixture(scope="module")
def criaderos(motor_owner: Engine) -> Iterator[tuple[Criadero, Criadero]]:
    """A es el que despacha; B tiene los mismos datos para intentar alcanzarlos."""
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
def b(criaderos: tuple[Criadero, Criadero]) -> Criadero:
    return criaderos[1]


@pytest.fixture
def api() -> TestClient:
    return TestClient(app)


def como(tenant: uuid.UUID, rol: str = "ventas") -> dict[str, str]:
    token = emitir(**{"custom:tenant_id": str(tenant), "cognito:groups": [rol]})
    return {"Authorization": f"Bearer {token}"}


def codigo(respuesta: Any) -> str:
    return str(respuesta.json()["error"]["code"])


def enjaular(
    motor_owner: Engine,
    criadero: Criadero,
    raza: uuid.UUID,
    cantidad: int,
    banco: uuid.UUID | None = None,
    fecha_enjaulado: str = "2026-10-16",
) -> None:
    """Una tanda de la raza con `cantidad` celdas enjauladas en el banco."""
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
                "INSERT INTO celda (tanda_id, estado, nucleo_id, fecha_introduccion, "
                "fecha_enjaulado, banco_id) "
                "SELECT :tanda, 'enjaulada', :nucleo, '2026-09-30', :enjaulado, "
                ":banco FROM generate_series(1, :cantidad)"
            ),
            {
                "tanda": tanda,
                "nucleo": criadero.nucleo,
                "enjaulado": fecha_enjaulado,
                "banco": banco,
                "cantidad": cantidad,
            },
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


def reservar(api: TestClient, criadero: Criadero, pedido: str) -> None:
    respuesta = api.post(f"{PEDIDOS}/{pedido}/reservas", headers=como(criadero.tenant))
    assert respuesta.status_code == 201, respuesta.json()


def lineas(api: TestClient, criadero: Criadero, pedido: str) -> list[str]:
    respuesta = api.get(f"{PEDIDOS}/{pedido}", headers=como(criadero.tenant))
    return [item["id"] for item in respuesta.json()["items"]]


def entregar(*salidas: tuple[str, list[tuple[Any, int]]], **extra: Any) -> Any:
    """Cuerpo de entregar: por linea, una lista de (banco, cantidad)."""
    return {
        "estado": "entregado",
        "lineas": [
            {
                "pedido_item_id": linea,
                "bancos": [
                    {"banco_id": None if banco is None else str(banco), "cantidad": n}
                    for banco, n in bancos
                ],
            }
            for linea, bancos in salidas
        ],
        **extra,
    }


CANCELAR = {"estado": "cancelado"}


def cambiar(
    api: TestClient, criadero: Criadero, pedido: str, cuerpo: Any, **headers: str
) -> Any:
    return api.post(
        f"{PEDIDOS}/{pedido}/estado",
        json=cuerpo,
        headers={**como(criadero.tenant), **headers},
    )


def filas(
    motor_owner: Engine, criadero: Criadero, sql: str, **parametros: Any
) -> list[Row[Any]]:
    """Lo que quedo en la base, leido como duenio dentro del criadero."""
    with motor_owner.begin() as c:
        fijar_tenant(c, criadero.tenant)
        return list(c.execute(text(sql), parametros).all())


def estados_de_reserva(motor_owner: Engine, criadero: Criadero) -> list[str]:
    return [
        f.estado for f in filas(motor_owner, criadero, "SELECT estado FROM reserva")
    ]


def despachadas(motor_owner: Engine, criadero: Criadero) -> int:
    (fila,) = filas(
        motor_owner,
        criadero,
        "SELECT count(*) AS n FROM celda WHERE estado = 'despachada'",
    )
    return int(fila.n)


def disponibilidad(api: TestClient, criadero: Criadero, raza: uuid.UUID) -> Any:
    respuesta = api.get(
        "/api/v1/disponibilidad",
        params={"raza_id": str(raza)},
        headers=como(criadero.tenant),
    )
    assert respuesta.status_code == 200, respuesta.json()
    (fila,) = respuesta.json()
    return fila


# --- Matriz de transiciones ---


def _pedido_en(
    api: TestClient, motor_owner: Engine, a: Criadero, estado: str
) -> tuple[str, str]:
    """Un pedido de 5 italianas en el estado pedido, y su linea."""
    enjaular(motor_owner, a, a.italiana, 5, a.norte)
    pedido = crear_pedido(api, a, (a.italiana, 5))
    (linea,) = lineas(api, a, pedido)
    if estado in ("reservado", "entregado"):
        reservar(api, a, pedido)
    if estado == "entregado":
        assert (
            cambiar(api, a, pedido, entregar((linea, [(a.norte, 5)]))).status_code
            == 200
        )
    if estado == "cancelado":
        assert cambiar(api, a, pedido, CANCELAR).status_code == 200
    return pedido, linea


@pytest.mark.parametrize(
    ("desde", "hacia", "status"),
    [
        ("borrador", "entregado", 409),
        ("borrador", "cancelado", 200),
        ("reservado", "entregado", 200),
        ("reservado", "cancelado", 200),
        # entregado y cancelado son finales.
        ("entregado", "entregado", 409),
        ("entregado", "cancelado", 409),
        ("cancelado", "entregado", 409),
        ("cancelado", "cancelado", 409),
    ],
)
def test_matriz_de_transiciones(
    api: TestClient,
    motor_owner: Engine,
    a: Criadero,
    desde: str,
    hacia: str,
    status: int,
) -> None:
    pedido, linea = _pedido_en(api, motor_owner, a, desde)
    cuerpo = entregar((linea, [(a.norte, 5)])) if hacia == "entregado" else CANCELAR

    respuesta = cambiar(api, a, pedido, cuerpo)

    assert respuesta.status_code == status, respuesta.json()
    if status == 200:
        assert respuesta.json()["estado"] == hacia
    else:
        assert codigo(respuesta) == "transicion_invalida"
        assert respuesta.json()["error"]["details"] == {"desde": desde, "hacia": hacia}
        # Un 409 no cambia nada.
        estado = api.get(f"{PEDIDOS}/{pedido}", headers=como(a.tenant)).json()
        assert estado["estado"] == desde


def test_reservado_no_entra_por_este_endpoint(api: TestClient, a: Criadero) -> None:
    pedido = crear_pedido(api, a, (a.italiana, 1))

    respuesta = cambiar(api, a, pedido, {"estado": "reservado"})

    # Lo hace POST /reservas (#35): para este endpoint es un cuerpo invalido.
    assert respuesta.status_code == 422


# --- Entregar ---


def test_despacho_repartido_entre_dos_bancos(
    api: TestClient, motor_owner: Engine, a: Criadero
) -> None:
    enjaular(motor_owner, a, a.italiana, 8, a.norte)
    enjaular(motor_owner, a, a.italiana, 8, a.sur)
    enjaular(motor_owner, a, a.carniola, 5)
    pedido = crear_pedido(api, a, (a.italiana, 10), (a.carniola, 5))
    reservar(api, a, pedido)
    linea_italiana, linea_carniola = lineas(api, a, pedido)
    antes = disponibilidad(api, a, a.italiana)

    respuesta = cambiar(
        api,
        a,
        pedido,
        entregar(
            (linea_italiana, [(a.norte, 6), (a.sur, 4)]),
            # Enjauladas que no estan en ningun banco.
            (linea_carniola, [(None, 5)]),
            fecha="2026-10-20",
        ),
    )

    assert respuesta.status_code == 200, respuesta.json()
    assert respuesta.json()["estado"] == "entregado"
    salieron = filas(
        motor_owner,
        a,
        "SELECT banco_id, pedido_item_id, fecha_despacho, count(*) AS n FROM celda "
        "WHERE estado = 'despachada' "
        "GROUP BY banco_id, pedido_item_id, fecha_despacho",
    )
    fecha = date(2026, 10, 20)
    assert {
        (f.banco_id, str(f.pedido_item_id), f.fecha_despacho, f.n) for f in salieron
    } == {
        (a.norte, linea_italiana, fecha, 6),
        (a.sur, linea_italiana, fecha, 4),
        (None, linea_carniola, fecha, 5),
    }
    assert estados_de_reserva(motor_owner, a) == ["consumida", "consumida"]
    # Las celdas y la reserva bajan juntas: el disponible no se mueve.
    despues = disponibilidad(api, a, a.italiana)
    assert (antes["enjauladas"], antes["reservadas"], antes["disponible"]) == (
        16,
        10,
        6,
    )
    assert (despues["enjauladas"], despues["reservadas"]) == (6, 0)
    assert despues["disponible"] == antes["disponible"]
    assert {(x["identificador"], x["reinas"]) for x in despues["bancos"]} == {
        ("N", 2),
        ("S", 4),
    }


def test_sin_fecha_despacha_con_la_de_hoy(
    api: TestClient, motor_owner: Engine, a: Criadero
) -> None:
    pedido, linea = _pedido_en(api, motor_owner, a, "reservado")

    respuesta = cambiar(api, a, pedido, entregar((linea, [(a.norte, 5)])))

    assert respuesta.status_code == 200, respuesta.json()
    fechas = filas(
        motor_owner,
        a,
        "SELECT DISTINCT fecha_despacho FROM celda WHERE estado = 'despachada'",
    )
    assert [f.fecha_despacho for f in fechas] == [hoy()]


def test_despacha_primero_las_enjauladas_mas_viejas(
    api: TestClient, motor_owner: Engine, a: Criadero
) -> None:
    enjaular(motor_owner, a, a.italiana, 3, a.norte, fecha_enjaulado="2026-10-20")
    enjaular(motor_owner, a, a.italiana, 3, a.norte, fecha_enjaulado="2026-10-10")
    pedido = crear_pedido(api, a, (a.italiana, 3))
    reservar(api, a, pedido)
    (linea,) = lineas(api, a, pedido)

    assert cambiar(api, a, pedido, entregar((linea, [(a.norte, 3)]))).status_code == 200

    enjaulado = filas(
        motor_owner,
        a,
        "SELECT DISTINCT fecha_enjaulado FROM celda WHERE estado = 'despachada'",
    )
    assert [f.fecha_enjaulado for f in enjaulado] == [date(2026, 10, 10)]


def test_un_banco_sin_stock_no_deja_nada_a_medias(
    api: TestClient, motor_owner: Engine, a: Criadero
) -> None:
    enjaular(motor_owner, a, a.italiana, 6, a.norte)
    enjaular(motor_owner, a, a.italiana, 2, a.sur)
    pedido = crear_pedido(api, a, (a.italiana, 8))
    reservar(api, a, pedido)
    (linea,) = lineas(api, a, pedido)

    # Norte alcanza y se despacha primero; Sur no alcanza.
    respuesta = cambiar(api, a, pedido, entregar((linea, [(a.norte, 5), (a.sur, 3)])))

    assert respuesta.status_code == 409
    assert codigo(respuesta) == "stock_insuficiente_en_banco"
    assert respuesta.json()["error"]["details"] == {
        "pedido_item_id": linea,
        "banco_id": str(a.sur),
        "pedidas": 3,
        "hay": 2,
    }
    # Las 5 de Norte vuelven atras con todo lo demas.
    assert despachadas(motor_owner, a) == 0
    assert estados_de_reserva(motor_owner, a) == ["activa"]
    estado = api.get(f"{PEDIDOS}/{pedido}", headers=como(a.tenant)).json()
    assert estado["estado"] == "reservado"


def test_las_celdas_de_otra_raza_no_cuentan(
    api: TestClient, motor_owner: Engine, a: Criadero
) -> None:
    enjaular(motor_owner, a, a.italiana, 4)
    enjaular(motor_owner, a, a.carniola, 10, a.norte)
    pedido = crear_pedido(api, a, (a.italiana, 4))
    reservar(api, a, pedido)
    (linea,) = lineas(api, a, pedido)

    # Norte tiene 10 reinas, pero ninguna italiana.
    respuesta = cambiar(api, a, pedido, entregar((linea, [(a.norte, 4)])))

    assert respuesta.status_code == 409
    assert codigo(respuesta) == "stock_insuficiente_en_banco"
    assert respuesta.json()["error"]["details"]["hay"] == 0


@pytest.mark.parametrize(
    "caso",
    ["menos_de_lo_reservado", "mas_de_lo_reservado", "falta_una_linea", "linea_ajena"],
)
def test_el_despacho_tiene_que_coincidir_con_la_reserva(
    api: TestClient, motor_owner: Engine, a: Criadero, caso: str
) -> None:
    enjaular(motor_owner, a, a.italiana, 20, a.norte)
    pedido = crear_pedido(api, a, (a.italiana, 5), (a.italiana, 3))
    reservar(api, a, pedido)
    uno, dos = lineas(api, a, pedido)
    otro = crear_pedido(api, a, (a.italiana, 2))
    (ajena,) = lineas(api, a, otro)
    salidas = {
        "menos_de_lo_reservado": [(uno, [(a.norte, 4)]), (dos, [(a.norte, 3)])],
        "mas_de_lo_reservado": [(uno, [(a.norte, 6)]), (dos, [(a.norte, 3)])],
        "falta_una_linea": [(uno, [(a.norte, 5)])],
        "linea_ajena": [
            (uno, [(a.norte, 5)]),
            (dos, [(a.norte, 3)]),
            (ajena, [(a.norte, 2)]),
        ],
    }[caso]

    respuesta = cambiar(api, a, pedido, entregar(*salidas))

    assert respuesta.status_code == 422, respuesta.json()
    assert codigo(respuesta) == "despacho_invalido"
    assert despachadas(motor_owner, a) == 0


def test_un_banco_de_otro_criadero_es_como_uno_inexistente(
    api: TestClient, motor_owner: Engine, a: Criadero, b: Criadero
) -> None:
    pedido, linea = _pedido_en(api, motor_owner, a, "reservado")

    for banco in (b.norte, uuid.uuid4()):
        respuesta = cambiar(api, a, pedido, entregar((linea, [(banco, 5)])))

        assert respuesta.status_code == 422
        assert codigo(respuesta) == "banco_no_encontrado"
        assert respuesta.json()["error"]["details"] == {"banco_id": str(banco)}


# --- Cancelar ---


def test_cancelar_un_reservado_libera_sus_reservas(
    api: TestClient, motor_owner: Engine, a: Criadero
) -> None:
    enjaular(motor_owner, a, a.italiana, 10, a.norte)
    pedido = crear_pedido(api, a, (a.italiana, 7))
    reservar(api, a, pedido)
    assert disponibilidad(api, a, a.italiana)["disponible"] == 3

    respuesta = cambiar(api, a, pedido, CANCELAR)

    assert respuesta.status_code == 200, respuesta.json()
    assert respuesta.json()["estado"] == "cancelado"
    assert estados_de_reserva(motor_owner, a) == ["liberada"]
    # Las reinas no se mueven: vuelven a estar disponibles para otro pedido.
    italiana = disponibilidad(api, a, a.italiana)
    assert (italiana["enjauladas"], italiana["disponible"]) == (10, 10)


def test_cancelar_un_borrador(
    api: TestClient, motor_owner: Engine, a: Criadero
) -> None:
    pedido = crear_pedido(api, a, (a.italiana, 7))

    respuesta = cambiar(api, a, pedido, CANCELAR)

    assert respuesta.status_code == 200, respuesta.json()
    assert respuesta.json()["estado"] == "cancelado"
    assert estados_de_reserva(motor_owner, a) == []


# --- Aislamiento, permisos e idempotencia ---


def test_un_pedido_de_otro_criadero_no_existe(
    api: TestClient, motor_owner: Engine, a: Criadero, b: Criadero
) -> None:
    pedido, linea = _pedido_en(api, motor_owner, a, "reservado")

    for cuerpo in (entregar((linea, [(a.norte, 5)])), CANCELAR):
        respuesta = cambiar(api, b, pedido, cuerpo)

        assert respuesta.status_code == 404
        assert codigo(respuesta) == "pedido_no_encontrado"
    assert estados_de_reserva(motor_owner, a) == ["activa"]
    assert despachadas(motor_owner, a) == 0


@pytest.mark.parametrize("rol", ["produccion", "lectura"])
def test_solo_admin_y_ventas_cambian_el_estado(
    api: TestClient, a: Criadero, rol: str
) -> None:
    pedido = crear_pedido(api, a, (a.italiana, 1))

    respuesta = api.post(
        f"{PEDIDOS}/{pedido}/estado", json=CANCELAR, headers=como(a.tenant, rol)
    )

    assert respuesta.status_code == 403


def test_con_la_misma_clave_un_reintento_no_despacha_dos_veces(
    api: TestClient, motor_owner: Engine, a: Criadero
) -> None:
    enjaular(motor_owner, a, a.italiana, 10, a.norte)
    pedido = crear_pedido(api, a, (a.italiana, 5))
    reservar(api, a, pedido)
    (linea,) = lineas(api, a, pedido)
    cuerpo = entregar((linea, [(a.norte, 5)]))
    clave = str(uuid.uuid4())

    primera = cambiar(api, a, pedido, cuerpo, **{"Idempotency-Key": clave})
    segunda = cambiar(api, a, pedido, cuerpo, **{"Idempotency-Key": clave})

    assert primera.status_code == segunda.status_code == 200
    assert segunda.headers["Idempotent-Replayed"] == "true"
    assert segunda.json() == primera.json()
    assert despachadas(motor_owner, a) == 5


def test_sin_clave_un_reintento_responde_409_sin_despachar_dos_veces(
    api: TestClient, motor_owner: Engine, a: Criadero
) -> None:
    pedido, linea = _pedido_en(api, motor_owner, a, "reservado")
    cuerpo = entregar((linea, [(a.norte, 5)]))

    assert cambiar(api, a, pedido, cuerpo).status_code == 200
    reintento = cambiar(api, a, pedido, cuerpo)

    assert reintento.status_code == 409
    assert codigo(reintento) == "transicion_invalida"
    assert despachadas(motor_owner, a) == 5


def test_la_misma_clave_con_otro_cuerpo_es_409(
    api: TestClient, motor_owner: Engine, a: Criadero
) -> None:
    pedido, linea = _pedido_en(api, motor_owner, a, "reservado")
    clave = {"Idempotency-Key": str(uuid.uuid4())}

    assert cambiar(api, a, pedido, CANCELAR, **clave).status_code == 200
    otra = cambiar(api, a, pedido, entregar((linea, [(a.norte, 5)])), **clave)

    assert otra.status_code == 409
    assert codigo(otra) == "idempotency_key_reutilizada"
