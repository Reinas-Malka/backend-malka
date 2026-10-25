"""Despachos simultaneos de la misma raza (#36), contra PostgreSQL real.

Cada despacho corre en su propia transaccion y su propia conexion, como dos
requests de verdad. El despacho toma el mismo lock por (tenant, raza) que la
reserva (#35):

- Una reserva de la raza espera a que el despacho confirme y ve el stock
  como quedo. Sin el lock leeria el stock de antes del despacho.
- Dos despachos del mismo banco no se llevan las mismas celdas: el segundo
  espera y elige otras, y salen los dos.

Reusa el criadero, los pedidos y el motor de test_concurrencia_reservas.py.
"""

import threading
import time
import uuid
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import db
from app.errores import ConflictoError
from app.schemas.transiciones import Entregar
from app.services.ventas import repositorio_transiciones
from tests import test_concurrencia_reservas as reservas
from tests.conftest import fijar_tenant
from tests.test_concurrencia_reservas import (
    ESPERA,
    Criadero,
    _id,
    disponibilidad,
    pedidos,
    reservar,
)

# Las fixtures de las reservas: pytest las encuentra por nombre en el modulo.
criadero = reservas.criadero
motor = reservas.motor

pytestmark = pytest.mark.integration


@pytest.fixture
def banco(motor_owner: Engine, criadero: Criadero) -> Iterator[uuid.UUID]:
    """Un banco con 10 italianas enjauladas."""
    with motor_owner.begin() as c:
        fijar_tenant(c, criadero.tenant)
        banco = _id(c, "INSERT INTO banco (identificador) VALUES ('N') RETURNING id")
        tanda = _id(
            c,
            "INSERT INTO tanda (raza_id, fecha_traslarve) "
            "VALUES (:raza, '2026-09-20') RETURNING id",
            raza=criadero.italiana,
        )
        c.execute(
            text(
                "INSERT INTO celda (tanda_id, estado, nucleo_id, fecha_introduccion, "
                "banco_id) SELECT :tanda, 'enjaulada', :nucleo, '2026-09-30', "
                ":banco FROM generate_series(1, 10)"
            ),
            {"tanda": tanda, "nucleo": criadero.nucleo, "banco": banco},
        )
    yield banco
    # Antes que la limpieza del criadero: la celda despachada apunta a su
    # linea, y el criadero borra pedido_item antes que celda.
    with motor_owner.begin() as c:
        fijar_tenant(c, criadero.tenant)
        c.execute(text("DELETE FROM celda WHERE true"))
        c.execute(text("DELETE FROM banco WHERE true"))


def _reservado(motor: Engine, criadero: Criadero, pedido: uuid.UUID) -> Entregar:
    """Reserva el pedido y arma el despacho de toda su linea desde el banco."""
    assert reservar(motor, criadero.tenant, pedido) == "ok"
    with Session(motor) as s, s.begin():
        s.execute(db.FIJAR_TENANT, {"tenant_id": str(criadero.tenant)})
        linea = s.execute(
            text("SELECT id, cantidad FROM pedido_item WHERE pedido_id = :p"),
            {"p": pedido},
        ).one()
        banco = s.execute(text("SELECT id FROM banco")).scalar_one()
    return Entregar.model_validate(
        {
            "estado": "entregado",
            "lineas": [
                {
                    "pedido_item_id": str(linea.id),
                    "bancos": [{"banco_id": str(banco), "cantidad": linea.cantidad}],
                }
            ],
        }
    )


def despachar(
    motor: Engine, tenant: uuid.UUID, pedido: uuid.UUID, datos: Entregar
) -> str:
    """Un despacho en su propia transaccion. Devuelve "ok" o el codigo."""
    try:
        with Session(motor) as s, s.begin():
            s.execute(db.FIJAR_TENANT, {"tenant_id": str(tenant)})
            repositorio_transiciones.cambiar_estado(s, pedido, datos)
    except ConflictoError as error:
        return error.code
    return "ok"


def _mientras_despacha(
    motor: Engine,
    tenant: uuid.UUID,
    pedido: uuid.UUID,
    datos: Entregar,
    segunda: Callable[[], str],
) -> tuple[str, bool]:
    """Despacha `pedido` sin confirmar y lanza `segunda` en otro hilo.

    Devuelve el resultado de la segunda y si tuvo que esperar al despacho.
    """
    resultado: dict[str, str] = {}

    def correr_segunda() -> None:
        resultado["segunda"] = segunda()

    with Session(motor) as s, s.begin():
        s.execute(db.FIJAR_TENANT, {"tenant_id": str(tenant)})
        repositorio_transiciones.cambiar_estado(s, pedido, datos)
        hilo = threading.Thread(target=correr_segunda)
        hilo.start()
        time.sleep(ESPERA)
        espero = hilo.is_alive()
    hilo.join(timeout=10)
    assert not hilo.is_alive()
    return resultado["segunda"], espero


def test_dos_despachos_del_mismo_banco_salen_los_dos(
    motor: Engine, motor_owner: Engine, criadero: Criadero, banco: uuid.UUID
) -> None:
    primero, segundo = pedidos(motor_owner, criadero, criadero.italiana, 5, 2)
    datos_primero = _reservado(motor, criadero, primero)
    datos_segundo = _reservado(motor, criadero, segundo)

    resultado, espero = _mientras_despacha(
        motor,
        criadero.tenant,
        primero,
        datos_primero,
        lambda: despachar(motor, criadero.tenant, segundo, datos_segundo),
    )

    # Espera al primero y despues elige las 5 que quedan: no hay 409 aunque
    # los dos pidan del mismo banco.
    assert espero
    assert resultado == "ok"
    italiana = disponibilidad(motor, criadero.tenant, criadero.italiana)
    assert (italiana.enjauladas, italiana.reservadas) == (0, 0)


def test_una_reserva_de_la_raza_espera_al_despacho(
    motor: Engine, motor_owner: Engine, criadero: Criadero, banco: uuid.UUID
) -> None:
    despacho, otro = pedidos(motor_owner, criadero, criadero.italiana, 6, 2)
    datos = _reservado(motor, criadero, despacho)

    resultado, espero = _mientras_despacha(
        motor,
        criadero.tenant,
        despacho,
        datos,
        lambda: reservar(motor, criadero.tenant, otro),
    )

    # Espera y despues ve 4 enjauladas sin reservar: no alcanzan para 6.
    assert espero
    assert resultado == "stock_insuficiente"
    italiana = disponibilidad(motor, criadero.tenant, criadero.italiana)
    assert (italiana.enjauladas, italiana.reservadas, italiana.disponible) == (4, 0, 4)


def test_una_reserva_de_otra_raza_no_espera_al_despacho(
    motor: Engine, motor_owner: Engine, criadero: Criadero, banco: uuid.UUID
) -> None:
    (despacho,) = pedidos(motor_owner, criadero, criadero.italiana, 5)
    (otro,) = pedidos(motor_owner, criadero, criadero.carniola, 1)
    datos = _reservado(motor, criadero, despacho)

    resultado, espero = _mientras_despacha(
        motor,
        criadero.tenant,
        despacho,
        datos,
        lambda: reservar(motor, criadero.tenant, otro),
    )

    # El lock es por raza: la carniola no espera a la italiana. No hay
    # carniolas enjauladas, asi que no alcanza.
    assert not espero
    assert resultado == "stock_insuficiente"


def test_un_despacho_que_no_alcanza_deja_todo_como_estaba(
    motor: Engine, motor_owner: Engine, criadero: Criadero, banco: uuid.UUID
) -> None:
    (pedido,) = pedidos(motor_owner, criadero, criadero.italiana, 5)
    datos = _reservado(motor, criadero, pedido)
    with motor_owner.begin() as c:
        fijar_tenant(c, criadero.tenant)
        # Quedan 3 en el banco; las otras 7 siguen enjauladas, sin banco.
        c.execute(
            text(
                "UPDATE celda SET banco_id = NULL WHERE id IN "
                "(SELECT id FROM celda LIMIT 7)"
            )
        )

    resultado = despachar(motor, criadero.tenant, pedido, datos)

    assert resultado == "stock_insuficiente_en_banco"
    italiana = disponibilidad(motor, criadero.tenant, criadero.italiana)
    assert (italiana.enjauladas, italiana.reservadas) == (10, 5)
