"""Respuesta de las reservas de reinas (#35, ADR 0014)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel

from app.services.ventas.reservas import EstadoReserva


class ReservaRespuesta(BaseModel):
    id: UUID
    # Una reserva por linea del pedido, con la raza y la cantidad de la linea.
    pedido_item_id: UUID
    raza_id: UUID
    cantidad: int
    estado: EstadoReserva
