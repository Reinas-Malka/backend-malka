"""Metricas de negocio en Embedded Metric Format (#44).

Una metrica es una linea JSON en stdout con la clave `_aws`: CloudWatch Logs
la convierte en metrica sin llamar a la API de CloudWatch (sin permisos ni
latencia extra en la Lambda). Las alarmas y el dashboard de infra/metricas.tf
leen estas metricas.

Namespace `MalkaSuite`, sin dimensiones: las alarmas miran el total y los
valores con muchas variantes (request_id, uso) van como propiedades, que
CloudWatch Logs Insights puede filtrar pero no cuestan como dimension. La
linea lleva siempre el request_id: desde una metrica se llega al log de la
operacion que la produjo.

Nunca falla: una metrica que no se puede escribir no rompe el request.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

from sqlalchemy.exc import InterfaceError, OperationalError

from app import contexto

NAMESPACE = "MalkaSuite"

DOCUMENTOS_GENERADOS = "documentos_generados"
DOCUMENTOS_RECHAZADOS_POR_VALIDACION = "documentos_rechazados_por_validacion"
RESERVAS_RECHAZADAS_POR_STOCK = "reservas_rechazadas_por_stock"
BEDROCK_LATENCY_MS = "bedrock_latency_ms"
BEDROCK_TOKENS = "bedrock_tokens"
TRANSICIONES_INVALIDAS = "transiciones_invalidas"
ERRORES_CONEXION_RDS = "errores_conexion_rds"

# lock_not_available y deadlock_detected: otra operacion usa los datos, la
# base esta bien (mismo criterio que app/errores.py).
SQLSTATE_OCUPADA = {"55P03", "40P01"}

logger = logging.getLogger("malka")


def emitir(
    valores: dict[str, tuple[float, str]],
    *,
    namespace: str = NAMESPACE,
    dimensiones: dict[str, str] | None = None,
    propiedades: dict[str, Any] | None = None,
) -> None:
    """Escribe una linea EMF. `valores` es {nombre: (valor, unidad)}."""
    try:
        dims = dimensiones or {}
        linea: dict[str, Any] = {
            **(propiedades or {}),
            **dims,
            "request_id": contexto.request_id_actual(),
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": namespace,
                        "Dimensions": [sorted(dims)],
                        "Metrics": [
                            {"Name": nombre, "Unit": unidad}
                            for nombre, (_, unidad) in valores.items()
                        ],
                    }
                ],
            },
        }
        linea.update({nombre: valor for nombre, (valor, _) in valores.items()})
        sys.stdout.write(json.dumps(linea, default=str) + "\n")
        sys.stdout.flush()
    except Exception:
        logger.warning("no se pudo emitir la metrica", exc_info=True)


def contar(nombre: str, **propiedades: Any) -> None:
    """Suma 1 a un contador."""
    emitir({nombre: (1, "Count")}, propiedades=propiedades)


def llamada_bedrock(uso: str, latencia_ms: float, tokens: int) -> None:
    """Latencia y tokens (entrada + salida) de una llamada a Bedrock."""
    emitir(
        {
            BEDROCK_LATENCY_MS: (latencia_ms, "Milliseconds"),
            BEDROCK_TOKENS: (tokens, "Count"),
        },
        propiedades={"uso": uso},
    )


def es_error_de_conexion(error: BaseException) -> bool:
    """True si el error es de la conexion con RDS y no un lock o deadlock."""
    if isinstance(error, InterfaceError):
        return True
    if not isinstance(error, OperationalError):
        return False
    return getattr(error.orig, "sqlstate", None) not in SQLSTATE_OCUPADA


def si_es_de_conexion(error: BaseException, origen: str) -> None:
    """Cuenta el error si es de conexion con RDS."""
    if es_error_de_conexion(error):
        contar(ERRORES_CONEXION_RDS, origen=origen)
