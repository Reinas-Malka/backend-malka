"""request_id y logs JSON (#24).

Cada request:

1. Obtiene un request_id: el header `x-request-id` si viene y es valido; si no,
   el id que asigna API Gateway; si no, uno nuevo.
2. Lo devuelve en el header `x-request-id` de la respuesta (API Gateway ya lo
   expone por CORS, asi que el frontend puede leerlo).
3. Escribe UNA linea JSON en el log al terminar, con request_id, tenant_id,
   user_id, route, status y latency_ms. Lambda manda stdout a CloudWatch, y
   CloudWatch Logs Insights puede filtrar por cualquiera de esos campos.

Nunca se loguean headers, cuerpos ni tokens: la linea de acceso lleva solo
campos elegidos, y el formateador tacha ademas cualquier clave sensible que
aparezca en un log.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import RequestResponseEndpoint

from app import contexto
from app.errores import respuesta_de_error

HEADER_REQUEST_ID = "x-request-id"

REQUEST_ID_VALIDO = re.compile(r"^[A-Za-z0-9._=+/-]{1,128}$")


CLAVES_SENSIBLES = re.compile(
    r"authorization|token|password|contrasena|secret|cookie|api[_-]?key"
    r"|cuit|cuil|dni|cbu|fiscal",
    re.IGNORECASE,
)
REDACTADO = "[REDACTADO]"

logger = logging.getLogger("malka.api")


def sanear(valor: Any) -> Any:
    """Reemplaza por [REDACTADO] el valor de toda clave sensible, a cualquier nivel."""
    if isinstance(valor, dict):
        return {
            clave: REDACTADO if CLAVES_SENSIBLES.search(str(clave)) else sanear(dato)
            for clave, dato in valor.items()
        }
    if isinstance(valor, list | tuple):
        return [sanear(dato) for dato in valor]
    return valor


class FormateadorJson(logging.Formatter):
    """Convierte cada registro de log en una linea JSON."""

    def format(self, record: logging.LogRecord) -> str:
        linea: dict[str, Any] = {
            "momento": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "nivel": record.levelname,
            "logger": record.name,
            "mensaje": record.getMessage(),
            "request_id": contexto.request_id_actual(),
        }
        campos = getattr(record, "campos", None)
        if isinstance(campos, dict):
            linea.update(sanear(campos))
        if record.exc_info and record.exc_info[0] is not None:
            linea["error"] = record.exc_info[0].__name__
            linea["traza"] = self.formatException(record.exc_info)
        return json.dumps(linea, ensure_ascii=False, default=str)


def configurar_logs(nivel: int = logging.INFO) -> None:
    """Deja el logger `malka` escribiendo JSON en stdout. Es idempotente."""
    raiz = logging.getLogger("malka")
    raiz.setLevel(nivel)

    raiz.propagate = False
    if not any(isinstance(h.formatter, FormateadorJson) for h in raiz.handlers):
        salida = logging.StreamHandler(sys.stdout)
        salida.setFormatter(FormateadorJson())
        raiz.addHandler(salida)


def obtener_request_id(request: Request) -> str:
    """Header valido del cliente > id de API Gateway > uno nuevo."""
    del_cliente = request.headers.get(HEADER_REQUEST_ID, "")
    if REQUEST_ID_VALIDO.fullmatch(del_cliente):
        return del_cliente

    evento = request.scope.get("aws.event")
    if isinstance(evento, dict):
        de_api_gateway = evento.get("requestContext", {}).get("requestId")
        if isinstance(de_api_gateway, str) and REQUEST_ID_VALIDO.fullmatch(
            de_api_gateway
        ):
            return de_api_gateway

    return uuid.uuid4().hex


def _ruta(request: Request) -> str:
    """La plantilla de la ruta (`/tandas/{id}`), no la URL con datos reales."""
    ruta = request.scope.get("route")
    plantilla = getattr(ruta, "path", None)
    return plantilla if isinstance(plantilla, str) else request.url.path


def instalar_observabilidad(app: FastAPI) -> None:
    """Agrega el middleware de request_id y log de acceso."""
    configurar_logs()

    @app.middleware("http")
    async def _observar(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        rid = obtener_request_id(request)
        marca = contexto.request_id.set(rid)
        inicio = time.perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception:
                # Error inesperado: se registra con su traza, pero al cliente
                # solo le llega la forma unica, sin detalles internos.
                logger.exception("error no manejado")
                response = respuesta_de_error(
                    500, "error_interno", "Ocurrio un error inesperado."
                )

            response.headers[HEADER_REQUEST_ID] = rid
            logger.info(
                "request",
                extra={
                    "campos": {
                        "tenant_id": getattr(request.state, "tenant_id", None),
                        "user_id": getattr(request.state, "user_id", None),
                        "method": request.method,
                        "route": _ruta(request),
                        "status": response.status_code,
                        "latency_ms": round((time.perf_counter() - inicio) * 1000, 2),
                    }
                },
            )
            return response
        finally:
            contexto.request_id.reset(marca)
