"""Worker de Malka Suite.

Consume las colas SQS de documentos e ingesta. Por ahora solo registra la
recepcion (cola e id, nunca el cuerpo) para poder verificar el circuito del
#37; el borrador asincronico con Bedrock (#40) reemplaza la logica dejando
el mismo handler.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)


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

        procesados += 1

    return {"mensajes_procesados": procesados}
