"""Reservas simultaneas sin sobreventa (#35, DoD), contra PostgreSQL real.

Cada reserva corre en su propia transaccion y su propia conexion, como dos
requests de verdad. Sin el lock por (tenant, raza), las reservas leen el
disponible antes de que las otras confirmen y reservan mas reinas de las que
hay; con el lock, la siguiente espera y ve lo que la anterior reservo.

Se usan motores con NullPool: el motor de la API tiene una sola conexion, y
con el las transacciones esperarian la conexion y no el lock.
"""

import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, create_engine, pool, text
from sqlalchemy.orm import Session

from app import db
from app.errores import ConflictoError
from app.services.ventas import repositorio_disponibilidad, repositorio_reservas
from tests.conftest import BaseDePrueba, crear_tenant, fijar_tenant

pytestmark = pytest.mark.integration

# Lo que se espera para dar por hecho que la segunda transaccion esta bloqueada.
ESPERA = 0.5

# De hijas a padres, para limpiar al final.
TABLAS = (
    "reserva",
    "pedido_item",
    "pedido",
    "celda",
    "tanda",
    "nucleo",
    "parque",
    "cliente",
    "item_vendible",
    "raza",
)


@dataclass(frozen=True)
class Criadero:
    tenant: uuid.UUID
    italiana: uuid.UUID
    carniola: uuid.UUID
    cliente: uuid.UUID
    item: uuid.UUID
    nucleo: uuid.UUID


def _id(c: Connection, sql: str, **parametros: Any) -> uuid.UUID:
    return uuid.UUID(str(c.execute(text(sql), parametros).scalar_one()))


@pytest.fixture
def motor(base_de_prueba: BaseDePrueba) -> Iterator[Engine]:
    """Rol de la API (malka_app), una conexion nueva por transaccion."""
    motor = create_engine(base_de_prueba.url_app, poolclass=pool.NullPool)
    yield motor
    motor.dispose()


@pytest.fixture
def criadero(motor_owner: Engine) -> Iterator[Criadero]:
    """Un criadero con dos razas, sin reinas ni pedidos."""
    tenant = uuid.uuid4()
    with motor_owner.begin() as c:
        crear_tenant(c, tenant)
        fijar_tenant(c, tenant)
        parque = _id(
            c,
            "INSERT INTO parque (nombre, ubicacion, capacidad_max) "
            "VALUES ('P1', 'Predio', 100) RETURNING id",
        )
        criadero = Criadero(
            tenant=tenant,
            italiana=_id(
                c, "INSERT INTO raza (nombre) VALUES ('Italiana') RETURNING id"
            ),
            carniola=_id(
                c, "INSERT INTO raza (nombre) VALUES ('Carniola') RETURNING id"
            ),
            cliente=_id(
                c,
                "INSERT INTO cliente (nombre, pais, tipo, condicion_iva, "
                "cuit_o_tax_id) VALUES ('Apicola del Sur', 'AR', 'nacional', "
                "'monotributo', '20123456786') RETURNING id",
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
        )
    yield criadero
    with motor_owner.begin() as c:
        fijar_tenant(c, tenant)
        for tabla in TABLAS:
            c.execute(text(f"DELETE FROM {tabla} WHERE true"))  # noqa: S608
        c.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": tenant})


def enjaular(
    motor_owner: Engine, criadero: Criadero, raza: uuid.UUID, cantidad: int
) -> None:
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


def pedidos(
    motor_owner: Engine,
    criadero: Criadero,
    raza: uuid.UUID,
    cantidad: int,
    cuantos: int = 1,
) -> list[uuid.UUID]:
    """`cuantos` pedidos en borrador, cada uno con una linea de `cantidad`."""
    ids = []
    with motor_owner.begin() as c:
        fijar_tenant(c, criadero.tenant)
        for _ in range(cuantos):
            pedido = _id(
                c,
                "INSERT INTO pedido (cliente_id, moneda, fecha_entrega_solicitada) "
                "VALUES (:cliente, 'ARS', '2026-11-30') RETURNING id",
                cliente=criadero.cliente,
            )
            c.execute(
                text(
                    "INSERT INTO pedido_item (pedido_id, numero_linea, "
                    "item_vendible_id, raza_id, cantidad, precio_unitario, "
                    "alicuota) VALUES (:pedido, 1, :item, :raza, :cantidad, "
                    "35.00, 0.21)"
                ),
                {
                    "pedido": pedido,
                    "item": criadero.item,
                    "raza": raza,
                    "cantidad": cantidad,
                },
            )
            ids.append(pedido)
    return ids


def reservar(motor: Engine, tenant: uuid.UUID, pedido_id: uuid.UUID) -> str:
    """Una reserva en su propia transaccion, como un request de la API.

    Devuelve "ok" o el codigo del error.
    """
    try:
        with Session(motor) as s, s.begin():
            s.execute(db.FIJAR_TENANT, {"tenant_id": str(tenant)})
            repositorio_reservas.reservar(s, pedido_id)
    except ConflictoError as error:
        return error.code
    return "ok"


def disponibilidad(motor: Engine, tenant: uuid.UUID, raza: uuid.UUID) -> Any:
    with Session(motor) as s, s.begin():
        s.execute(db.FIJAR_TENANT, {"tenant_id": str(tenant)})
        (fila,) = repositorio_disponibilidad.calcular(s, raza)
    return fila


def test_20_pedidos_de_10_con_100_reinas_reservan_exactamente_10(
    motor: Engine, motor_owner: Engine, criadero: Criadero
) -> None:
    enjaular(motor_owner, criadero, criadero.italiana, 100)
    ids = pedidos(motor_owner, criadero, criadero.italiana, 10, cuantos=20)
    # Todos arrancan a la vez: ninguno llega a confirmar antes de que los
    # demas lean el disponible, salvo que el lock los haga esperar.
    largada = threading.Barrier(len(ids))

    def correr(pedido_id: uuid.UUID) -> str:
        largada.wait()
        return reservar(motor, criadero.tenant, pedido_id)

    with ThreadPoolExecutor(max_workers=len(ids)) as hilos:
        resultados = list(hilos.map(correr, ids))

    assert resultados.count("ok") == 10
    assert resultados.count("stock_insuficiente") == 10
    italiana = disponibilidad(motor, criadero.tenant, criadero.italiana)
    assert italiana.reservadas == 100
    assert italiana.disponible == 0


def _en_paralelo(
    motor: Engine,
    tenant: uuid.UUID,
    primera: uuid.UUID,
    segunda: uuid.UUID,
) -> tuple[str, bool]:
    """Reserva `primera` sin confirmar y lanza la reserva de `segunda`.

    Devuelve el resultado de la segunda y si tuvo que esperar a la primera.
    """
    resultado: dict[str, str] = {}

    def correr_segunda() -> None:
        resultado["segunda"] = reservar(motor, tenant, segunda)

    with Session(motor) as s, s.begin():
        s.execute(db.FIJAR_TENANT, {"tenant_id": str(tenant)})
        repositorio_reservas.reservar(s, primera)
        hilo = threading.Thread(target=correr_segunda)
        hilo.start()
        time.sleep(ESPERA)
        espero = hilo.is_alive()
    hilo.join(timeout=10)
    assert not hilo.is_alive()
    return resultado["segunda"], espero


def test_la_segunda_reserva_de_la_raza_espera_y_ve_la_primera(
    motor: Engine, motor_owner: Engine, criadero: Criadero
) -> None:
    enjaular(motor_owner, criadero, criadero.italiana, 10)
    primera, segunda = pedidos(motor_owner, criadero, criadero.italiana, 7, 2)

    resultado, espero = _en_paralelo(motor, criadero.tenant, primera, segunda)

    # Sin el lock no esperaria: leeria 10 disponibles y reservaria 7 mas.
    assert espero
    assert resultado == "stock_insuficiente"


def test_una_reserva_de_otra_raza_no_espera(
    motor: Engine, motor_owner: Engine, criadero: Criadero
) -> None:
    enjaular(motor_owner, criadero, criadero.italiana, 10)
    enjaular(motor_owner, criadero, criadero.carniola, 10)
    (italiana,) = pedidos(motor_owner, criadero, criadero.italiana, 7)
    (carniola,) = pedidos(motor_owner, criadero, criadero.carniola, 7)

    resultado, espero = _en_paralelo(motor, criadero.tenant, italiana, carniola)

    assert not espero
    assert resultado == "ok"
