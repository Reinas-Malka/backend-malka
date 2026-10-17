"""Disponibilidad de reinas por raza (#33).

Leer puede cualquier rol del criadero. El calculo vive en el repositorio:
el endpoint solo conecta el request con el.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import obtener_sesion
from app.schemas.disponibilidad import DisponibilidadReinas
from app.services.ventas import repositorio_disponibilidad

router = APIRouter(prefix="/api/v1/disponibilidad", tags=["disponibilidad"])


@router.get("", response_model=list[DisponibilidadReinas])
def obtener_disponibilidad(
    raza_id: UUID | None = None,
    sesion: Session = Depends(obtener_sesion),
) -> list[DisponibilidadReinas]:
    return repositorio_disponibilidad.calcular(sesion, raza_id)
