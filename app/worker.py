"""Worker de Malka Suite.

Consume las colas SQS de documentos e ingesta. Registra la recepcion (cola
e id, nunca el cuerpo: puede llevar datos del tenant). Un mensaje de
borrador ({"borrador_id", "tenant_id"}, #40) se genera con Bedrock en
app/services/ia/generacion.py; el resto solo se registra.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from app.services.ia import generacion
from app.services.ia.cliente import ClienteIA, crear_cliente

logger = logging.getLogger()
logger.setLevel(logging.INFO)


@lru_cache(maxsize=1)
def cliente_ia() -> ClienteIA:
    """Un cliente de Bedrock por contenedor, reutilizado entre invocaciones."""
    return crear_cliente()


def _borrador(cuerpo: str) -> dict[str, Any] | None:
    """El mensaje si es de un borrador; None si es otra cosa."""
    try:
        mensaje = json.loads(cuerpo)
    except ValueError:
        return None
    if isinstance(mensaje, dict) and "borrador_id" in mensaje:
        return mensaje
    return None


def handler(event: dict[str, Any], context: object) -> dict[str, int]:
    """Procesa un lote de mensajes de SQS (batch_size 1: uno por ejecucion)."""
    procesados = 0

    for registro in event.get("Records", []):
        cuerpo = registro.get("body", "")

        logger.info(
            "mensaje recibido cola=%s id_mensaje=%s",
            registro.get("eventSourceARN", "?").split(":")[-1],
            registro.get("messageId", "?"),
        )

        if cuerpo == "veneno":
            raise RuntimeError("mensaje veneno: tiene que terminar en la DLQ")

        if (mensaje := _borrador(cuerpo)) is not None:
            generacion.procesar(mensaje, cliente_ia())

        procesados += 1

    return {"mensajes_procesados": procesados}
