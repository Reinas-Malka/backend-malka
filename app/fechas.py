"""El dia de "hoy" con la hora de Argentina.

La Lambda y la base corren en UTC: entre las 21 y las 24 h de Argentina,
date.today() y CURRENT_DATE ya devuelven el dia siguiente, y una fecha que en
Argentina todavia es futura pasaria como valida. Toda regla de negocio que
compare con "hoy" usa esta funcion, nunca date.today() ni CURRENT_DATE.

Los instantes (creado_en, actualizado_en, vencimientos) no pasan por aca: un
timestamptz es el mismo instante en cualquier zona.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

# La cabania opera en Argentina. La base de zonas horarias viene del paquete
# tzdata (requirements.txt): ni Windows ni la imagen de Lambda garantizan
# tenerla en el sistema.
ZONA_ARGENTINA = ZoneInfo("America/Argentina/Buenos_Aires")


def _ahora_utc() -> datetime:
    """El instante actual. Funcion aparte para que los tests fijen la hora."""
    return datetime.now(UTC)


def hoy() -> date:
    """La fecha de hoy en Argentina, aunque el servidor este en UTC."""
    return _ahora_utc().astimezone(ZONA_ARGENTINA).date()
