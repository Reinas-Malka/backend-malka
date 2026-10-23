"""Dos altas de tanda simultaneas sobre el mismo material (#27), con PostgreSQL.

Mismo patron que test_concurrencia_parque.py: la primera alta descuenta las
cupulas y todavia no confirma; la segunda queda esperando el bloqueo del
material (FOR UPDATE). Cuando la primera confirma, la segunda tiene que ver
ese consumo al calcular su stock: si no, las dos informarian el mismo stock y
la advertencia de stock negativo podria no salir.
"""

import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import db
from app.schemas.produccion import TandaCreada, TandaCrear
from app.services.produccion import repositorio_materiales, repositorio_tandas
from tests.conftest import crear_tenant, fijar_tenant
from tests.test_concurrencia_parque import _en_paralelo, motor  # noqa: F401

pytestmark = pytest.mark.integration

# Miercoles pasado: ni domingo ni futuro.
FECHA = date(2026, 10, 21)

Criadero = tuple[uuid.UUID, uuid.UUID, uuid.UUID]


@pytest.fixture
def criadero(motor_owner: Engine) -> Iterator[Criadero]:
    """Un criadero con una raza y 200 cupulas compradas."""
    tenant = uuid.uuid4()
    with motor_owner.begin() as c:
        crear_tenant(c, tenant)
        fijar_tenant(c, tenant)
        raza_id = c.execute(
            text("INSERT INTO raza (nombre) VALUES ('Carniola') RETURNING id")
        ).scalar_one()
        material_id = c.execute(
            text(
                "INSERT INTO material (nombre, unidad) "
                "VALUES ('Cupula', 'unidad') RETURNING id"
            )
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO movimiento_material (material_id, tipo, cantidad, fecha) "
                "VALUES (:material, 'compra', 200, :fecha)"
            ),
            {"material": material_id, "fecha": FECHA},
        )
    yield tenant, raza_id, material_id
    with motor_owner.begin() as c:
        fijar_tenant(c, tenant)
        for tabla in ("movimiento_material", "celda", "tanda", "material", "raza"):
            c.execute(text(f"DELETE FROM {tabla} WHERE true"))  # noqa: S608
        c.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": tenant})


def test_dos_altas_simultaneas_ven_el_consumo_de_la_otra(
    motor: Engine,  # noqa: F811
    criadero: Criadero,
) -> None:
    tenant, raza_id, material_id = criadero
    datos = TandaCrear(
        raza_id=raza_id, material_id=material_id, fecha_traslarve=FECHA, cuadros=1
    )
    resultados: list[TandaCreada] = []

    def alta(sesion: Session) -> Any:
        resultados.append(repositorio_tandas.crear(sesion, datos))

    error = _en_paralelo(motor, tenant, alta, alta)

    assert error is None
    # 200 - 135 = 65 para la primera; la segunda ya ve ese consumo: 65 - 135.
    assert [r.stock for r in resultados] == [Decimal(65), Decimal(-70)]
    assert resultados[0].advertencias == []
    assert [a.code for a in resultados[1].advertencias] == ["stock_negativo"]
    with Session(motor) as s, s.begin():
        s.execute(db.FIJAR_TENANT, {"tenant_id": str(tenant)})
        assert repositorio_materiales.obtener(s, material_id).stock == Decimal(-70)
