"""Chequeo de las dependencias para /health/ready (#23).

Prueba lo que de verdad usa un request de negocio: leer el secreto de la base
(cacheado), conectarse a RDS por la red privada y ejecutar una consulta. Cada
paso tiene un tiempo maximo, para que una base caida se responda con 503 en
segundos y no con un timeout de API Gateway (29 s).

La respuesta solo dice "ok" o "error" por dependencia: el motivo queda en el
log (el tipo de error, sin mensaje), nunca el host, el usuario ni la clave.
"""

from __future__ import annotations

import logging
from enum import StrEnum

from sqlalchemy import text

from app import db

# Tope de la consulta. La conexion tiene el suyo (CONNECT_TIMEOUT_S en db.py).
TIMEOUT_CONSULTA_MS = 2000

# set_config(..., true) es SET LOCAL: el limite vale solo para esta transaccion
# y la conexion vuelve al pool sin el.
FIJAR_TIMEOUT = text("SELECT set_config('statement_timeout', :ms, true)")
CONSULTA = text("SELECT 1")

logger = logging.getLogger("malka.api")


class Estado(StrEnum):
    OK = "ok"
    ERROR = "error"


def verificar_base_de_datos() -> Estado:
    """Lee el secreto, se conecta y ejecuta SELECT 1 con un timeout corto."""
    try:
        # db.obtener_motor (y no un import directo) para que los tests puedan
        # apuntarlo a la base de prueba.
        with db.obtener_motor().connect() as conexion, conexion.begin():
            conexion.execute(FIJAR_TIMEOUT, {"ms": str(TIMEOUT_CONSULTA_MS)})
            conexion.execute(CONSULTA).scalar_one()
    except Exception as error:
        # Cualquier falla cuenta: secreto ausente, red, credencial o timeout.
        # Solo el tipo: el mensaje de psycopg incluye el host de RDS.
        logger.warning(
            "dependencia no disponible",
            extra={
                "campos": {
                    "dependencia": "base_de_datos",
                    "error": type(error).__name__,
                }
            },
        )
        return Estado.ERROR
    return Estado.OK


def verificar_dependencias() -> dict[str, Estado]:
    """Estado de cada dependencia. Hoy solo la base; se suman aca."""
    return {"base_de_datos": verificar_base_de_datos()}
