"""Worker de Malka Suite.

Consume las colas SQS de documentos e ingesta. Por ahora solo registra el
mensaje para poder verificar el circuito del #37; el borrador asincronico
con Bedrock (#40) reemplaza la logica dejando el mismo handler.
"""

from __future__ import annotations

import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event: dict, context: object) -> dict:
    """Procesa un lote de mensajes de SQS (batch_size 1: uno por ejecucion)."""
    procesados = 0

    for registro in event.get("Records", []):
        logger.info(
            "mensaje recibido cola=%s id_mensaje=%s cuerpo=%s",
            registro.get("eventSourceARN", "?").split(":")[-1],
            registro.get("messageId", "?"),
            registro.get("body", ""),
        )
        procesados += 1

    return {"mensajes_procesados": procesados}
