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
        cuerpo = registro.get("body", "")

        logger.info(
            "mensaje recibido cola=%s id_mensaje=%s cuerpo=%s",
            registro.get("eventSourceARN", "?").split(":")[-1],
            registro.get("messageId", "?"),
            cuerpo,
        )

        # Gancho de la prueba del DoD del #37: un cuerpo "veneno" falla
        # siempre, reintenta 3 veces y termina en la DLQ.
        if cuerpo == "veneno":
            raise RuntimeError("mensaje veneno: tiene que terminar en la DLQ")

        procesados += 1

    return {"mensajes_procesados": procesados}
