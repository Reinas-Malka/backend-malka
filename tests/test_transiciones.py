"""Transiciones de los pedidos y reglas del despacho (#36), sin base."""

from __future__ import annotations

import re
import uuid

import pytest

from app.services.ventas.pedidos import EstadoPedido
from app.services.ventas.transiciones import (
    TRANSICIONES,
    puede_pasar,
    validar_despacho,
)

# Escritas a mano y no sacadas de TRANSICIONES: si no, el test de la matriz
# compararia la constante consigo misma.
VALIDAS = {
    (EstadoPedido.BORRADOR, EstadoPedido.RESERVADO),
    (EstadoPedido.BORRADOR, EstadoPedido.CANCELADO),
    (EstadoPedido.RESERVADO, EstadoPedido.ENTREGADO),
    (EstadoPedido.RESERVADO, EstadoPedido.CANCELADO),
}

LINEA_A = uuid.uuid4()
LINEA_B = uuid.uuid4()
OTRA_LINEA = uuid.uuid4()


@pytest.mark.parametrize("origen", list(EstadoPedido))
@pytest.mark.parametrize("destino", list(EstadoPedido))
def test_matriz_de_transiciones(origen: EstadoPedido, destino: EstadoPedido) -> None:
    # Las 16 combinaciones: 4 validas y 12 que el servicio responde con 409.
    assert puede_pasar(origen, destino) == ((origen, destino) in VALIDAS)


def test_todos_los_estados_tienen_fila() -> None:
    # Un estado nuevo sin fila haria que puede_pasar tire KeyError.
    assert set(TRANSICIONES) == set(EstadoPedido)


@pytest.mark.parametrize("final", [EstadoPedido.ENTREGADO, EstadoPedido.CANCELADO])
def test_los_estados_finales_no_pasan_a_ninguno(final: EstadoPedido) -> None:
    assert TRANSICIONES[final] == frozenset()


def test_acepta_el_estado_como_texto_de_la_base() -> None:
    # El repositorio lee el estado de la base como str. EstadoPedido es un
    # StrEnum: "reservado" encuentra la fila de EstadoPedido.RESERVADO.
    assert puede_pasar("reservado", EstadoPedido.ENTREGADO)  # type: ignore[arg-type]


def test_despacho_igual_a_la_reserva() -> None:
    validar_despacho({LINEA_A: 10, LINEA_B: 5}, {LINEA_A: 10, LINEA_B: 5})


def test_el_orden_de_las_lineas_no_importa() -> None:
    validar_despacho({LINEA_A: 10, LINEA_B: 5}, {LINEA_B: 5, LINEA_A: 10})


def test_rechaza_una_linea_que_no_esta_reservada() -> None:
    with pytest.raises(ValueError, match=str(OTRA_LINEA)):
        validar_despacho({LINEA_A: 10}, {LINEA_A: 10, OTRA_LINEA: 3})


def test_con_varias_lineas_de_mas_nombra_siempre_la_misma() -> None:
    otra = uuid.uuid4()

    with pytest.raises(ValueError, match=str(min(OTRA_LINEA, otra))):
        validar_despacho({LINEA_A: 10}, {LINEA_A: 10, OTRA_LINEA: 3, otra: 2})


def test_rechaza_una_linea_reservada_que_no_se_despacha() -> None:
    with pytest.raises(ValueError, match=str(LINEA_B)):
        validar_despacho({LINEA_A: 10, LINEA_B: 5}, {LINEA_A: 10})


@pytest.mark.parametrize(
    "despachadas",
    [
        pytest.param(8, id="menos_entrega_parcial"),
        pytest.param(12, id="mas_reinas_de_otro_pedido"),
    ],
)
def test_rechaza_una_cantidad_distinta_a_la_reservada(despachadas: int) -> None:
    with pytest.raises(ValueError) as error:
        validar_despacho({LINEA_A: 10, LINEA_B: 5}, {LINEA_A: despachadas, LINEA_B: 5})

    mensaje = str(error.value)
    # El mensaje dice de que linea es y las dos cantidades. \b para no
    # encontrar el numero adentro del UUID.
    assert str(LINEA_A) in mensaje
    assert re.search(rf"\b{despachadas}\b", mensaje)
    assert re.search(r"\b10\b", mensaje)
