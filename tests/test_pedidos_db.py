"""Tablas pedido y pedido_item de la migracion 0008 contra un PostgreSQL real.

Los CHECK repiten reglas que Pydantic y validar_pedido ya aplican: no deberian
saltar en el flujo normal. Se prueban insertando directo, porque son la ultima
defensa si un camino nuevo se saltea la API.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from tests.conftest import crear_tenant, fijar_tenant

pytestmark = pytest.mark.integration

INSERTAR_PEDIDO = text(
    "INSERT INTO pedido (cliente_id, moneda, destino_pais, incoterm, "
    "fecha_entrega_solicitada) "
    "VALUES (:cliente_id, :moneda, :destino_pais, :incoterm, '2026-11-30') "
    "RETURNING id"
)

INSERTAR_LINEA = text(
    "INSERT INTO pedido_item (pedido_id, numero_linea, item_vendible_id, raza_id, "
    "cantidad, precio_unitario, alicuota) "
    "VALUES (:pedido_id, :numero_linea, :item, :raza, :cantidad, :precio, :alicuota)"
)


@pytest.fixture(scope="module")
def criadero(motor_owner: Engine) -> Iterator[dict[str, uuid.UUID]]:
    """Un criadero con un cliente, un item y una raza para colgar pedidos."""
    tenant = uuid.uuid4()
    with motor_owner.begin() as c:
        crear_tenant(c, tenant)
        fijar_tenant(c, tenant)
        ids = {
            "tenant": tenant,
            "cliente": c.execute(
                text(
                    "INSERT INTO cliente (nombre, pais, tipo, condicion_iva, "
                    "cuit_o_tax_id) VALUES ('Bienenzucht GmbH', 'DE', "
                    "'exportacion', 'cliente_exterior', 'DE123456789') RETURNING id"
                )
            ).scalar_one(),
            "item": c.execute(
                text(
                    "INSERT INTO item_vendible (nombre) VALUES ('Reina fecundada') "
                    "RETURNING id"
                )
            ).scalar_one(),
            "raza": c.execute(
                text("INSERT INTO raza (nombre) VALUES ('Italiana') RETURNING id")
            ).scalar_one(),
        }
    yield ids
    with motor_owner.begin() as c:
        fijar_tenant(c, tenant)
        for tabla in ("pedido_item", "pedido", "cliente", "item_vendible", "raza"):
            c.execute(text(f"DELETE FROM {tabla} WHERE true"))  # noqa: S608
        c.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": tenant})


def _pedido(criadero: dict[str, uuid.UUID], **cambios: Any) -> dict[str, Any]:
    return {
        "cliente_id": criadero["cliente"],
        "moneda": "EUR",
        "destino_pais": "DE",
        "incoterm": "CPT",
        **cambios,
    }


def _linea(criadero: dict[str, uuid.UUID], pedido: uuid.UUID) -> dict[str, Any]:
    return {
        "pedido_id": pedido,
        "numero_linea": 1,
        "item": criadero["item"],
        "raza": criadero["raza"],
        "cantidad": 10,
        "precio": "35.50",
        "alicuota": "0.21",
    }


@pytest.mark.parametrize(
    ("cambios", "restriccion"),
    [
        pytest.param(
            {"moneda": "USD", "destino_pais": None, "incoterm": None},
            "ck_pedido_nacional_o_exportacion",
            id="nacional_en_dolares",
        ),
        pytest.param(
            {"moneda": "ARS"},
            "ck_pedido_nacional_o_exportacion",
            id="exportacion_en_pesos",
        ),
        pytest.param(
            {"destino_pais": "AR"},
            "ck_pedido_nacional_o_exportacion",
            id="exportacion_a_argentina",
        ),
        pytest.param(
            {"moneda": "ARS", "destino_pais": None},
            "ck_pedido_incoterm_con_destino",
            id="incoterm_sin_destino",
        ),
    ],
)
def test_la_base_rechaza_pedidos_incoherentes(
    motor_owner: Engine,
    criadero: dict[str, uuid.UUID],
    cambios: dict[str, Any],
    restriccion: str,
) -> None:
    with pytest.raises(IntegrityError, match=restriccion):
        with motor_owner.begin() as c:
            fijar_tenant(c, criadero["tenant"])
            c.execute(INSERTAR_PEDIDO, _pedido(criadero, **cambios))


@pytest.mark.parametrize(
    ("cambios", "restriccion"),
    [
        pytest.param({"cantidad": 0}, "ck_pedido_item_cantidad", id="cantidad_cero"),
        pytest.param({"precio": "-1"}, "ck_pedido_item_precio", id="precio_negativo"),
        pytest.param({"alicuota": "0.15"}, "ck_pedido_item_alicuota", id="alicuota"),
        pytest.param(
            {"numero_linea": 0}, "ck_pedido_item_numero_linea", id="linea_cero"
        ),
    ],
)
def test_la_base_rechaza_lineas_invalidas(
    motor_owner: Engine,
    criadero: dict[str, uuid.UUID],
    cambios: dict[str, Any],
    restriccion: str,
) -> None:
    with pytest.raises(IntegrityError, match=restriccion):
        with motor_owner.begin() as c:
            fijar_tenant(c, criadero["tenant"])
            pedido = c.execute(INSERTAR_PEDIDO, _pedido(criadero)).scalar_one()
            c.execute(INSERTAR_LINEA, {**_linea(criadero, pedido), **cambios})


def test_dos_lineas_con_el_mismo_numero_chocan(
    motor_owner: Engine, criadero: dict[str, uuid.UUID]
) -> None:
    with pytest.raises(IntegrityError, match="uq_pedido_item_tenant_pedido_numero"):
        with motor_owner.begin() as c:
            fijar_tenant(c, criadero["tenant"])
            pedido = c.execute(INSERTAR_PEDIDO, _pedido(criadero)).scalar_one()
            c.execute(INSERTAR_LINEA, _linea(criadero, pedido))
            c.execute(INSERTAR_LINEA, _linea(criadero, pedido))


def test_el_listado_tiene_indice_por_fecha_de_alta(motor_owner: Engine) -> None:
    """GET /pedidos ordena por (creado_en DESC, id DESC) dentro del tenant."""
    with motor_owner.begin() as c:
        definicion = c.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'pedido' AND indexname = 'ix_pedido_tenant_creado'"
            )
        ).scalar_one_or_none()

    assert definicion is not None
    assert "(tenant_id, creado_en DESC, id DESC)" in definicion
