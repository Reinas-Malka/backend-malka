"""Limite de uso de la IA por criadero (#45).

Bedrock cobra por token: sin topes, un bucle en el frontend o un usuario
insistente genera una factura inesperada (seccion 10 del brief: costo
acotado). Tres topes:

- Llamadas por minuto, por criadero: corta las rafagas (un boton apretado
  muchas veces, un reintento en bucle).
- Llamadas por dia, por criadero: acota el costo. El dia es el de Argentina,
  como el resto del sistema (app/fechas.py).
- Tokens de salida por llamada: MAX_TOKENS_SALIDA va en cada converse
  (app/services/ia/cliente.py).

Quien use la IA reserva la llamada ANTES de encolarla o hacerla
(reservar_llamada, 429 si no hay cupo) y anota lo consumido despues
(registrar_consumo, que tambien emite la metrica por criadero).

Los topes se configuran por entorno (IA_LLAMADAS_POR_MINUTO,
IA_LLAMADAS_POR_DIA). Un valor mal escrito nunca deja la IA sin limite:
cae al valor por defecto.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.errores import LimiteExcedidoError
from app.services.ia.cliente import MAX_TOKENS_SALIDA, RespuestaIA

__all__ = [
    "LLAMADAS_POR_DIA",
    "LLAMADAS_POR_MINUTO",
    "MAX_TOKENS_SALIDA",
    "LimiteExcedidoError",
    "llamadas_por_dia",
    "llamadas_por_minuto",
    "registrar_consumo",
    "reservar_llamada",
]

logger = logging.getLogger(__name__)

# Por defecto: un borrador ronda 3000 tokens de entrada y 1000 de salida, unos
# 0,008 USD con Haiku 4.5 (README, "IA: modelo y costos"). 200 por dia son
# como mucho ~1,60 USD diarios por criadero.
LLAMADAS_POR_MINUTO = 10
LLAMADAS_POR_DIA = 200
SEGUNDOS_POR_MINUTO = 60

# Una llamada a la vez por criadero mientras se cuenta: dos pedidos juntos no
# pueden pasar los dos con el ultimo lugar libre. Mismo patron que la reserva
# por raza (repositorio_reservas.py).
BLOQUEAR_CRIADERO = text(
    "SELECT pg_advisory_xact_lock(hashtextextended("
    "'ia:' || current_setting('app.tenant_id'), 0))"
)

# Las llamadas del ultimo minuto y del dia (de Argentina), y cuantos segundos
# faltan para la medianoche de alla. RLS deja ver solo las del criadero.
CONTAR = text(
    "WITH dia AS (SELECT (date_trunc('day', now() AT TIME ZONE "
    "'America/Argentina/Buenos_Aires') AT TIME ZONE "
    "'America/Argentina/Buenos_Aires') AS inicio) "
    "SELECT "
    "count(l.id) FILTER (WHERE l.creado_en > now() - interval '1 minute') "
    "AS en_el_minuto, "
    "count(l.id) FILTER (WHERE l.creado_en >= dia.inicio) AS en_el_dia, "
    "CAST(ceil(extract(epoch FROM dia.inicio + interval '1 day' - now())) "
    "AS integer) AS hasta_manana "
    "FROM dia LEFT JOIN llamada_ia l "
    "ON l.creado_en >= least(dia.inicio, now() - interval '1 minute') "
    "GROUP BY dia.inicio"
)

# tenant_id lo pone el default de la columna (el de la sesion).
INSERTAR = text("INSERT INTO llamada_ia (motivo) VALUES (:motivo) RETURNING id")

ANOTAR_TOKENS = text(
    "UPDATE llamada_ia SET tokens_entrada = :entrada, tokens_salida = :salida "
    "WHERE id = :id RETURNING tenant_id"
)


def _tope(variable: str, por_defecto: int) -> int:
    """Un entero positivo del entorno; cualquier otra cosa, el valor por defecto."""
    try:
        valor = int(os.getenv(variable, ""))
    except ValueError:
        return por_defecto
    return valor if valor > 0 else por_defecto


def llamadas_por_minuto() -> int:
    return _tope("IA_LLAMADAS_POR_MINUTO", LLAMADAS_POR_MINUTO)


def llamadas_por_dia() -> int:
    return _tope("IA_LLAMADAS_POR_DIA", LLAMADAS_POR_DIA)


def reservar_llamada(sesion: Session, motivo: str) -> UUID:
    """Reserva una llamada a la IA del criadero de la sesion, o 429.

    Va en la misma transaccion que el resto del pedido: si despues algo
    falla, la reserva se deshace con el rollback y no cuenta.
    """
    sesion.execute(BLOQUEAR_CRIADERO)
    uso = sesion.execute(CONTAR).one()
    por_dia = llamadas_por_dia()
    if uso.en_el_dia >= por_dia:
        raise LimiteExcedidoError(
            "El criadero ya uso hoy todas las llamadas a la IA que tiene. "
            "Se renuevan a la medianoche.",
            code="limite_ia_diario",
            reintentar_en=uso.hasta_manana,
            details={"tope": por_dia},
        )
    por_minuto = llamadas_por_minuto()
    if uso.en_el_minuto >= por_minuto:
        raise LimiteExcedidoError(
            "Demasiadas llamadas a la IA seguidas. Esperá un minuto.",
            code="limite_ia_por_minuto",
            reintentar_en=SEGUNDOS_POR_MINUTO,
            details={"tope": por_minuto},
        )
    llamada: UUID = sesion.execute(INSERTAR, {"motivo": motivo}).scalar_one()
    return llamada


def registrar_consumo(
    sesion: Session, llamada_id: UUID, respuesta: RespuestaIA
) -> None:
    """Anota los tokens de una llamada hecha y emite la metrica del criadero."""
    tenant_id = sesion.execute(
        ANOTAR_TOKENS,
        {
            "id": llamada_id,
            "entrada": respuesta.tokens_entrada,
            "salida": respuesta.tokens_salida,
        },
    ).scalar_one_or_none()
    if tenant_id is None:
        # La llamada no es de este criadero o no existe: no se inventa consumo.
        logger.warning("llamada_ia inexistente al registrar consumo id=%s", llamada_id)
        return
    _emitir_metrica(tenant_id, respuesta)


def _emitir_metrica(tenant_id: UUID, respuesta: RespuestaIA) -> None:
    """Embedded Metric Format: una linea JSON en stdout que CloudWatch Logs
    convierte en metricas, sin llamar a la API de CloudWatch (#44).

    Por criadero: el tenant es la unica dimension (pocos valores).
    """
    linea = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": "MalkaSuite/IA",
                    "Dimensions": [["tenant_id"]],
                    "Metrics": [
                        {"Name": "Llamadas", "Unit": "Count"},
                        {"Name": "TokensEntrada", "Unit": "Count"},
                        {"Name": "TokensSalida", "Unit": "Count"},
                    ],
                }
            ],
        },
        "tenant_id": str(tenant_id),
        "Llamadas": 1,
        "TokensEntrada": respuesta.tokens_entrada,
        "TokensSalida": respuesta.tokens_salida,
    }
    sys.stdout.write(json.dumps(linea) + "\n")
    sys.stdout.flush()
