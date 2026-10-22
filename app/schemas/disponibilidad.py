"""Respuesta de la disponibilidad de reinas por raza (#33, ADR 0014).

No se llama Disponibilidad: ese nombre ya lo usa /health/ready y chocaria en
los tipos que el frontend genera desde el OpenAPI.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel


class ExistenciaEnBanco(BaseModel):
    # None: reinas enjauladas que todavia no pasaron a ningun banco.
    banco_id: UUID | None
    identificador: str | None
    reinas: int


class DisponibilidadReinas(BaseModel):
    raza_id: UUID
    raza: str
    enjauladas: int
    # Cantidad de las reservas activas de la raza (#35).
    reservadas: int
    # enjauladas - reservadas: se calcula siempre, nunca se guarda.
    disponible: int
    # Solo ubicacion fisica: no hay "disponible por banco".
    bancos: list[ExistenciaEnBanco]
