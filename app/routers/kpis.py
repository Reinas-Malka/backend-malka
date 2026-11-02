"""KPIs de produccion: % de cosecha y % de perdida en el parque (#31).

Leer puede cualquier rol del criadero. El calculo vive en el repositorio:
el endpoint solo conecta el request con el.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import obtener_sesion
from app.schemas.kpis import Agrupacion, KpiProduccion
from app.services.produccion import repositorio_kpis

router = APIRouter(prefix="/api/v1/kpis", tags=["kpis"])


@router.get("/produccion", response_model=list[KpiProduccion])
def obtener_kpis_de_produccion(
    agrupar: Agrupacion = "tanda",
    # Periodo por fecha de traslarve, con los dos dias incluidos.
    desde: date | None = None,
    hasta: date | None = None,
    sesion: Session = Depends(obtener_sesion),
) -> list[KpiProduccion]:
    """% de cosecha y % de perdida en el parque, por tanda o por raza."""
    return repositorio_kpis.calcular(sesion, agrupar, desde, hasta)
