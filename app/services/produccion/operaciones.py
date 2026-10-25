"""Reglas de las operaciones por lote que no necesitan la base (#28).

docs/dominio.md, "Lo que se registra en el campo" (23/10): el productor
introduce eligiendo parque, cantidad y fecha, nunca nucleos. El sistema
reparte parejo entre los nucleos activos del parque, primero los que tienen
menos celdas ocupadas y en orden de fila y posicion. Sin tope por nucleo.
"""

from __future__ import annotations

import heapq
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class NucleoOcupado:
    """Un nucleo activo del parque y cuantas celdas tiene sin cosechar."""

    id: UUID
    fila: int
    posicion: int
    ocupadas: int


def repartir(nucleos: Sequence[NucleoOcupado], cantidad: int) -> dict[UUID, int]:
    """Cuantas celdas nuevas va a cada nucleo.

    De a una celda por vez, siempre al nucleo con menos celdas (las que ya
    tenia mas las que va recibiendo); a igual cantidad, el de menor fila y
    posicion. Asi 120 celdas en 60 nucleos vacios son 2 por nucleo, y si hay
    nucleos ya ocupados, los mas libres se llenan primero.

    Devuelve solo los nucleos que reciben alguna celda, en orden de fila y
    posicion.
    """
    if not nucleos:
        raise ValueError("No hay nucleos para repartir.")
    if cantidad < 0:
        raise ValueError("La cantidad no puede ser negativa.")

    # (ocupadas, fila, posicion, indice): el heap devuelve siempre el mas libre.
    pendientes = [(n.ocupadas, n.fila, n.posicion, i) for i, n in enumerate(nucleos)]
    heapq.heapify(pendientes)
    recibe = [0] * len(nucleos)
    for _ in range(cantidad):
        ocupadas, fila, posicion, i = heapq.heappop(pendientes)
        recibe[i] += 1
        heapq.heappush(pendientes, (ocupadas + 1, fila, posicion, i))

    en_orden = sorted(
        range(len(nucleos)), key=lambda i: (nucleos[i].fila, nucleos[i].posicion)
    )
    return {nucleos[i].id: recibe[i] for i in en_orden if recibe[i] > 0}
