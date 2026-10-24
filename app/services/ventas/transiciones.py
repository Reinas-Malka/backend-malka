"""Transiciones de estado de los pedidos (#36).

No depende de la base, de FastAPI ni de AWS. El repositorio de transiciones
lee el pedido, pregunta aca si el cambio de estado se puede hacer y convierte
la respuesta en un 409 transicion_invalida o un 422 despacho_invalido.

Reglas en docs/dominio.md ("Pedidos" y "Disponibilidad, reservas y
despacho"): borrador -> reservado -> entregado | cancelado. entregado y
cancelado son finales: se cancela solo desde borrador o reservado.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from app.services.ventas.pedidos import EstadoPedido

# Desde cada estado, a cuales se puede pasar. Los finales van con un conjunto
# vacio, no afuera: asi puede_pasar no necesita un caso especial.
# borrador -> reservado esta aunque lo haga /reservas (#35): la matriz es la
# del dominio, no la de un endpoint.
TRANSICIONES: dict[EstadoPedido, frozenset[EstadoPedido]] = {
    EstadoPedido.BORRADOR: frozenset({EstadoPedido.RESERVADO, EstadoPedido.CANCELADO}),
    EstadoPedido.RESERVADO: frozenset({EstadoPedido.CANCELADO, EstadoPedido.ENTREGADO}),
    EstadoPedido.CANCELADO: frozenset(),
    EstadoPedido.ENTREGADO: frozenset(),
}


def puede_pasar(estado_origen: EstadoPedido, estado_destino: EstadoPedido) -> bool:
    """Devuelve True si se puede pasar de estado_origen a estado_destino.

    No valida que el pedido exista ni que el estado_origen sea el actual.
    """
    return estado_destino in TRANSICIONES[estado_origen]


def validar_despacho(
    reservadas: Mapping[UUID, int], despachadas: Mapping[UUID, int]
) -> None:
    """Revisa que el despacho cubra exactamente las reservas activas del pedido.

    Sin entrega parcial: cada linea sale con la cantidad que tiene reservada.
    Lanza ValueError con el primer problema que encuentra.
    """
    sobran = despachadas.keys() - reservadas.keys()
    if sobran:
        raise ValueError(
            f"La linea {min(sobran)} no es de este pedido o no tiene reserva activa."
        )
    faltan = reservadas.keys() - despachadas.keys()
    if faltan:
        raise ValueError(
            f"La linea {min(faltan)} tiene reserva activa pero no esta en el despacho."
        )
    for linea, cantidad in despachadas.items():
        if cantidad != reservadas[linea]:
            raise ValueError(
                f"La linea {linea} tiene {cantidad} en el despacho "
                f"pero {reservadas[linea]} en la reserva."
            )
