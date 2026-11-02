"""KPIs de produccion: las cuentas, sin base (#31).

docs/dominio.md, KPIs redefinidos el 23/10. El denominador es siempre la
cantidad de celdas introducidas al nucleo:

- % de cosecha = cosechadas (enjaulada + despachada) / introducidas. Es el %
  de fecundacion del dominio: una reina cosechada es una reina fecundada.
- % de perdida en el parque = descartadas con nucleo / introducidas.

Las descartadas sin nucleo (antes de introducir) no entran en ningun %: solo
cierran la fila. Sin introducidas no hay base y el % es None, nunca una
division por cero ni un 0 que parezca un dato.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# Un decimal, como se lee en el campo: 95 de 120 = 79,2 %.
UN_DECIMAL = Decimal("0.1")


def porcentaje(parte: int, total: int) -> Decimal | None:
    """parte / total en %, con un decimal. None si total es 0."""
    if total == 0:
        return None
    return (Decimal(parte) * 100 / Decimal(total)).quantize(
        UN_DECIMAL, rounding=ROUND_HALF_UP
    )
