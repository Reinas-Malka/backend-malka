"""Reglas de las reservas de reinas (#35).

No depende de la base, de FastAPI ni de AWS. El repositorio toma el lock,
calcula el disponible y llama a faltantes para decidir si la reserva entra.

Reglas en docs/dominio.md ("Disponibilidad, reservas y despacho") y en el
ADR 0014: la reserva va por raza, sin banco, y no vence.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from uuid import UUID


class EstadoReserva(StrEnum):
    # Activa hasta que el pedido se despacha (consumida) o se cancela
    # (liberada). Las dos las hace repositorio_transiciones (#36).
    ACTIVA = "activa"
    CONSUMIDA = "consumida"
    LIBERADA = "liberada"


def faltantes(
    pedidas: Mapping[UUID, int], disponibles: Mapping[UUID, int]
) -> dict[UUID, int]:
    """Las razas en las que lo pedido supera lo disponible, con cuanto falta.

    Una raza que no esta en disponibles cuenta como cero. Si el resultado
    esta vacio, la reserva entra.
    """
    resultado = {}
    for raza_id, cantidad in pedidas.items():
        falta = cantidad - disponibles.get(raza_id, 0)
        if falta > 0:
            resultado[raza_id] = falta
    return resultado
