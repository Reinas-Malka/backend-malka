"""Banco lleno con dos lotes simultaneos (#28), contra PostgreSQL real.

Mismo patron que test_concurrencia_parque.py: el primer lote pasa reinas al
banco y todavia no confirma; el segundo queda esperando el bloqueo del banco
(repositorio_bancos.bloquear). Cuando el primero confirma, el segundo tiene
que ver esas reinas y no pasarse de la capacidad.
"""

import uuid
from collections.abc import Callable, Iterator
from datetime import date
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import db
from app.errores import ConflictoError
from app.schemas.produccion import OperacionPasarABanco
from app.services.produccion import repositorio_bancos, repositorio_operaciones
from tests.conftest import crear_tenant, fijar_tenant
from tests.test_concurrencia_parque import _en_paralelo, motor  # noqa: F401

pytestmark = pytest.mark.integration

FECHA = date(2026, 10, 12)

# tenant, banco y las dos tandas.
Escenario = tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]


@pytest.fixture
def escenario(motor_owner: Engine) -> Iterator[Escenario]:
    """Un banco con lugar para 5 y dos tandas con 3 reinas enjauladas cada una."""
    tenant = uuid.uuid4()
    with motor_owner.begin() as c:
        crear_tenant(c, tenant)
        fijar_tenant(c, tenant)
        raza = c.execute(
            text("INSERT INTO raza (nombre) VALUES ('Carniola') RETURNING id")
        ).scalar_one()
        parque = c.execute(
            text(
                "INSERT INTO parque (nombre, ubicacion, capacidad_max) "
                "VALUES ('P1', 'Predio', 10) RETURNING id"
            )
        ).scalar_one()
        nucleo = c.execute(
            text(
                "INSERT INTO nucleo (parque_id, fila, posicion) "
                "VALUES (:parque, 1, 1) RETURNING id"
            ),
            {"parque": parque},
        ).scalar_one()
        banco = c.execute(
            text(
                "INSERT INTO banco (identificador, capacidad_max) "
                "VALUES ('B1', 5) RETURNING id"
            )
        ).scalar_one()
        tandas = []
        for _ in range(2):
            tanda = c.execute(
                text(
                    "INSERT INTO tanda (raza_id, fecha_traslarve) "
                    "VALUES (:raza, :fecha) RETURNING id"
                ),
                {"raza": raza, "fecha": FECHA},
            ).scalar_one()
            c.execute(
                text(
                    "INSERT INTO celda (tanda_id, estado, nucleo_id, "
                    "fecha_introduccion, fecha_enjaulado) "
                    "SELECT :tanda, 'enjaulada', :nucleo, :fecha, :fecha "
                    "FROM generate_series(1, 3)"
                ),
                {"tanda": tanda, "nucleo": nucleo, "fecha": FECHA},
            )
            tandas.append(tanda)
    yield tenant, banco, tandas[0], tandas[1]
    with motor_owner.begin() as c:
        fijar_tenant(c, tenant)
        for tabla in ("celda", "tanda", "banco", "nucleo", "parque", "raza"):
            c.execute(text(f"DELETE FROM {tabla} WHERE true"))  # noqa: S608
        c.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": tenant})


def test_dos_lotes_simultaneos_no_pasan_la_capacidad_del_banco(
    motor: Engine,  # noqa: F811
    escenario: Escenario,
) -> None:
    tenant, banco, primera, segunda = escenario

    def lote(tanda: uuid.UUID) -> Callable[[Session], Any]:
        datos = OperacionPasarABanco(accion="pasar_a_banco", cantidad=3, banco_id=banco)
        return lambda s: repositorio_operaciones.operar(s, tanda, datos)

    error = _en_paralelo(motor, tenant, lote(primera), lote(segunda))

    # 3 + 3 no entran en 5: el segundo lote ve las 3 del primero.
    assert isinstance(error, ConflictoError)
    assert error.code == "banco_lleno"
    assert error.details == {"caben": 2, "capacidad_max": 5}
    with Session(motor) as s, s.begin():
        s.execute(db.FIJAR_TENANT, {"tenant_id": str(tenant)})
        assert repositorio_bancos.obtener(s, banco).reinas_en_banco == 3
