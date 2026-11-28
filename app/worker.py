"""Worker de Malka Suite.

Consume las colas SQS de documentos e ingesta. Registra la recepcion (cola
e id, nunca el cuerpo: puede llevar datos del tenant). Un mensaje de
borrador ({"borrador_id", "tenant_id", "request_id"}, #40) se genera con
Bedrock en app/services/ia/generacion.py; el resto solo se registra.

Cada mensaje corre con el request_id del pedido que lo encolo (#44): los
logs JSON y las metricas del worker llevan el mismo id que la linea de
acceso de la API. Un mensaje sin request_id valido usa su messageId.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from app import contexto, metricas
from app.observabilidad import REQUEST_ID_VALIDO, configurar_logs
from app.services.ia import generacion
from app.services.ia.cliente import ClienteIA, crear_cliente

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Los logs de app/ (logger "malka") salen en JSON con el request_id.
configurar_logs()


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


def _request_id(mensaje: dict[str, Any] | None, registro: dict[str, Any]) -> str:
    """El request_id que trae el mensaje; si no es valido, el messageId."""
    del_mensaje = (mensaje or {}).get("request_id")
    if isinstance(del_mensaje, str) and REQUEST_ID_VALIDO.fullmatch(del_mensaje):
        return del_mensaje
    return str(registro.get("messageId", "sin-request-id"))


def handler(event: dict[str, Any], context: object) -> dict[str, int]:
    """Procesa un lote de mensajes de SQS (batch_size 1: uno por ejecucion)."""
    procesados = 0

    for registro in event.get("Records", []):
        cuerpo = registro.get("body", "")
        mensaje = _borrador(cuerpo)
        rid = _request_id(mensaje, registro)

        marca = contexto.request_id.set(rid)
        try:
            logger.info(
                "mensaje recibido cola=%s id_mensaje=%s request_id=%s",
                registro.get("eventSourceARN", "?").split(":")[-1],
                registro.get("messageId", "?"),
                rid,
            )

            if cuerpo == "veneno":
                raise RuntimeError("mensaje veneno: tiene que terminar en la DLQ")

            if mensaje is not None:
                try:
                    generacion.procesar(mensaje, cliente_ia())
                except Exception as error:
                    metricas.si_es_de_conexion(error, "worker")
                    raise
        finally:
            contexto.request_id.reset(marca)

        procesados += 1

    return {"mensajes_procesados": procesados}
