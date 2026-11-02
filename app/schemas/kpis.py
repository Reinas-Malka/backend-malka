"""Respuesta de los KPIs de produccion por tanda o por raza (#31).

Todo se calcula al leer desde las celdas (docs/dominio.md, 23/10): no hay
columnas de porcentaje. Los conteos van en la respuesta junto a los % para
que se vea de donde sale cada uno.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

Agrupacion = Literal["tanda", "raza"]


class KpiProduccion(BaseModel):
    # Solo al agrupar por tanda; por raza vienen en null.
    tanda_id: UUID | None
    fecha_traslarve: date | None
    raza_id: UUID
    raza: str
    # Tandas del grupo: 1 por tanda; por raza, las del periodo.
    tandas: int
    # Celdas con fecha_introduccion: las que entraron a un nucleo, se hayan
    # cosechado, descartado o sigan en el parque.
    introducidas: int
    # enjaulada + despachada.
    cosechadas: int
    # descartada con nucleo: el resto de la cosecha.
    perdidas: int
    # introducida: todavia sin cosechar. Por eso cosecha + perdida puede no
    # llegar a 100 en una tanda en curso.
    en_parque: int
    # Con un decimal; null si no hay introducidas (sin base).
    porcentaje_cosecha: Decimal | None
    porcentaje_perdida: Decimal | None
