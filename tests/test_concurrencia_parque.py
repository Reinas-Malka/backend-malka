"""Capacidad del parque con altas simultaneas (#29), contra PostgreSQL real.

Dos transacciones de verdad, cada una en su conexion: la primera da de alta un
nucleo y todavia no confirma; la segunda queda esperando el bloqueo del
parque. Cuando la primera confirma, la segunda tiene que ver ese nucleo.

Se usan motores con NullPool: el motor de la API tiene una sola conexion, y
con el la segunda transaccion esperaria la conexion y no el bloqueo.
"""

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, pool, text
from sqlalchemy.orm import Session

from app import db
from app.errores import ConflictoError
from app.schemas.produccion import NucleoCrear
from app.services.produccion import repositorio_nucleos, repositorio_parques
from tests.conftest import BaseDePrueba, crear_tenant, fijar_tenant

pytestmark = pytest.mark.integration

# Lo que se espera para dar por hecho que la segunda transaccion esta bloqueada.
ESPERA = 0.5


@pytest.fixture
def motor(base_de_prueba: BaseDePrueba) -> Iterator[Engine]:
    """Rol de la API (malka_app), una conexion nueva por transaccion."""
    motor = create_engine(base_de_prueba.url_app, poolclass=pool.NullPool)
    yield motor
    motor.dispose()


@pytest.fixture
def parque(motor_owner: Engine) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """Un criadero con un parque de capacidad 1."""
    tenant = uuid.uuid4()
    with motor_owner.begin() as c:
        crear_tenant(c, tenant)
        fijar_tenant(c, tenant)
        parque_id = c.execute(
            text(
                "INSERT INTO parque (nombre, ubicacion, capacidad_max) "
                "VALUES ('P1', 'Predio norte', 1) RETURNING id"
            )
        ).scalar_one()
    yield tenant, parque_id
    with motor_owner.begin() as c:
        fijar_tenant(c, tenant)
        c.execute(text("DELETE FROM nucleo WHERE true"))
        c.execute(text("DELETE FROM parque WHERE true"))
        c.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": tenant})


def _en_paralelo(
    motor: Engine,
    tenant: uuid.UUID,
    primera: Callable[[Session], Any],
    segunda: Callable[[Session], Any],
) -> BaseException | None:
    """Corre `primera` sin confirmar, lanza `segunda` y confirma la primera.

    Devuelve el error que levanto la segunda (None si termino bien).
    """
    resultado: dict[str, BaseException | None] = {"error": None}

    def correr_segunda() -> None:
        try:
            with Session(motor) as s, s.begin():
                s.execute(db.FIJAR_TENANT, {"tenant_id": str(tenant)})
                segunda(s)
        except BaseException as error:  # noqa: BLE001
            resultado["error"] = error

    with Session(motor) as s, s.begin():
        s.execute(db.FIJAR_TENANT, {"tenant_id": str(tenant)})
        primera(s)
        hilo = threading.Thread(target=correr_segunda)
        hilo.start()
        time.sleep(ESPERA)
        # La segunda tiene que estar esperando el bloqueo del parque.
        assert hilo.is_alive()
    hilo.join(timeout=10)
    assert not hilo.is_alive()
    return resultado["error"]


def test_dos_altas_simultaneas_no_pasan_la_capacidad(
    motor: Engine, parque: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant, parque_id = parque

    def alta(posicion: int) -> Callable[[Session], Any]:
        datos = NucleoCrear(parque_id=parque_id, fila=1, posicion=posicion)
        return lambda s: repositorio_nucleos.crear(s, datos)

    error = _en_paralelo(motor, tenant, alta(1), alta(2))

    assert isinstance(error, ConflictoError)
    assert error.code == "parque_lleno"
    with Session(motor) as s, s.begin():
        s.execute(db.FIJAR_TENANT, {"tenant_id": str(tenant)})
        assert repositorio_parques.obtener(s, parque_id).nucleos_activos == 1


def test_no_se_da_de_baja_un_parque_mientras_se_le_suma_un_nucleo(
    motor: Engine, parque: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant, parque_id = parque
    datos = NucleoCrear(parque_id=parque_id, fila=1, posicion=1)

    error = _en_paralelo(
        motor,
        tenant,
        lambda s: repositorio_nucleos.crear(s, datos),
        lambda s: repositorio_parques.desactivar(s, parque_id),
    )

    assert isinstance(error, ConflictoError)
    assert error.code == "parque_con_nucleos"
    with Session(motor) as s, s.begin():
        s.execute(db.FIJAR_TENANT, {"tenant_id": str(tenant)})
        assert repositorio_parques.obtener(s, parque_id).activo is True
