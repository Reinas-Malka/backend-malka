"""KPIs de produccion por tanda o por raza, contados desde las celdas (#31).

Reglas de docs/dominio.md (KPIs redefinidos el 23/10). Nada se guarda: cada
lectura cuenta las celdas y app/services/produccion/kpis.py saca los %.

- introducidas: celdas con fecha_introduccion. Incluye las que despues se
  cosecharon o se descartaron, y las que siguen en el parque.
- cosechadas: enjaulada + despachada (una despachada ya se cosecho).
- perdidas: descartada con nucleo, el resto de la cosecha. Las descartadas
  sin nucleo (antes de introducir) no entran en ningun %.
- Por raza se suman las celdas de todas sus tandas y despues se divide: los
  % se calculan a nivel celda, no son el promedio de los % de cada tanda.

Los conteos son los mismos en las dos agrupaciones: se escriben una sola vez
(CONTEOS) y cada consulta solo cambia el GROUP BY. Son constantes, nunca
datos del request.

El periodo filtra por fecha de traslarve, con los dos dias incluidos. RLS
filtra tanda, raza y celda: todo es del criadero de la sesion (ADR 0009).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.orm import Session

from app.errores import ValidacionError
from app.schemas.kpis import Agrupacion, KpiProduccion
from app.services.produccion.kpis import porcentaje

# Las cosechadas y las perdidas exigen haber entrado al nucleo, como el
# denominador: asi ningun % puede pasar de 100.
CONTEOS = (
    "count(c.id) FILTER (WHERE c.fecha_introduccion IS NOT NULL) "
    "AS introducidas, "
    "count(c.id) FILTER (WHERE c.fecha_introduccion IS NOT NULL "
    "AND c.estado IN ('enjaulada', 'despachada')) AS cosechadas, "
    "count(c.id) FILTER (WHERE c.fecha_introduccion IS NOT NULL "
    "AND c.estado = 'descartada' AND c.nucleo_id IS NOT NULL) AS perdidas, "
    "count(c.id) FILTER (WHERE c.estado = 'introducida') AS en_parque "
)

# LEFT JOIN: una tanda sin celdas introducidas tambien sale, sin %.
DESDE_TANDAS_DEL_PERIODO = (
    "FROM tanda t "
    "JOIN raza r ON r.id = t.raza_id "
    "LEFT JOIN celda c ON c.tanda_id = t.id "
    "WHERE (CAST(:desde AS date) IS NULL OR t.fecha_traslarve >= :desde) "
    "AND (CAST(:hasta AS date) IS NULL OR t.fecha_traslarve <= :hasta) "
)

# Como el listado de tandas: de la mas nueva a la mas vieja.
POR_TANDA = text(
    "SELECT t.id AS tanda_id, t.fecha_traslarve, r.id AS raza_id, "
    "r.nombre AS raza, 1 AS tandas, "
    + CONTEOS
    + DESDE_TANDAS_DEL_PERIODO
    + "GROUP BY t.id, r.id "
    "ORDER BY t.fecha_traslarve DESC, t.creado_en DESC, t.id DESC"
)

# Solo las razas con alguna tanda en el periodo: sin tandas no hay que medir.
POR_RAZA = text(
    "SELECT NULL AS tanda_id, NULL AS fecha_traslarve, r.id AS raza_id, "
    "r.nombre AS raza, count(DISTINCT t.id) AS tandas, "
    + CONTEOS
    + DESDE_TANDAS_DEL_PERIODO
    + "GROUP BY r.id "
    "ORDER BY r.nombre, r.id"
)

CONSULTA: dict[str, Any] = {"tanda": POR_TANDA, "raza": POR_RAZA}


def _a_kpi(fila: Row[Any]) -> KpiProduccion:
    return KpiProduccion(
        **fila._mapping,
        porcentaje_cosecha=porcentaje(fila.cosechadas, fila.introducidas),
        porcentaje_perdida=porcentaje(fila.perdidas, fila.introducidas),
    )


def calcular(
    sesion: Session,
    agrupar: Agrupacion,
    desde: date | None = None,
    hasta: date | None = None,
) -> list[KpiProduccion]:
    """KPIs por tanda o por raza de las tandas traslarvadas en el periodo."""
    if desde is not None and hasta is not None and desde > hasta:
        raise ValidacionError(
            "El periodo no puede terminar antes de empezar.",
            code="periodo_invalido",
            details={"desde": desde.isoformat(), "hasta": hasta.isoformat()},
        )
    filas = sesion.execute(CONSULTA[agrupar], {"desde": desde, "hasta": hasta})
    return [_a_kpi(fila) for fila in filas]
