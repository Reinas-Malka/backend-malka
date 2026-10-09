"""Forma unica de los errores de la API (#24).

Todo error, sea de dominio, de validacion, de ruta inexistente o inesperado,
responde con el mismo cuerpo:

    {"error": {"code": ..., "message": ..., "details": ..., "request_id": ...}}

Los endpoints no arman respuestas de error a mano: lanzan una excepcion de
dominio y los manejadores de este modulo la traducen.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.contexto import request_id_actual


class ErrorDeApi(Exception):
    """Base de los errores que la API devuelve a proposito.

    Cada subclase fija su status HTTP y un codigo por defecto. El codigo es
    estable y lo lee el frontend; el mensaje es para personas y puede cambiar.
    """

    status_code: int = 400
    codigo_por_defecto: str = "error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.codigo_por_defecto
        self.details = details or {}


class ConflictoError(ErrorDeApi):
    """El pedido choca con el estado actual (409).

    Ejemplo: reservar mas reinas de las que la tanda puede entregar.
    """

    status_code = 409
    codigo_por_defecto = "conflicto"


class ValidacionError(ErrorDeApi):
    """El pedido esta bien formado pero viola una regla de negocio (422).

    Ejemplo: una fecha de traslarve posterior a la de fecundacion.
    """

    status_code = 422
    codigo_por_defecto = "validacion"


class NoAutenticadoError(ErrorDeApi):
    """Falta el token o no es valido (401).

    Ejemplo: un ID token vencido o firmado con otra clave.
    """

    status_code = 401
    codigo_por_defecto = "no_autenticado"


class SinPermisoError(ErrorDeApi):
    """El usuario esta autenticado pero su rol no alcanza (403).

    Ejemplo: un usuario de solo lectura que intenta dar de alta una raza.
    """

    status_code = 403
    codigo_por_defecto = "sin_permiso"


class NoEncontradoError(ErrorDeApi):
    """El recurso no existe, o es de otro criadero y RLS no lo deja ver (404).

    Ejemplo: pedir un cliente por un id que no esta en el tenant del token.
    """

    status_code = 404
    codigo_por_defecto = "no_encontrado"


CODIGOS_HTTP = {
    400: "pedido_invalido",
    401: "no_autenticado",
    403: "sin_permiso",
    404: "no_encontrado",
    405: "metodo_no_permitido",
}


def respuesta_de_error(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """Unico lugar donde se arma el cuerpo de un error."""
    request_id = request_id_actual()
    cuerpo = {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": request_id,
        }
    }
    return JSONResponse(
        status_code=status_code,
        content=cuerpo,
        headers={"x-request-id": request_id},
    )


def _campos_de_validacion(error: RequestValidationError) -> list[dict[str, Any]]:
    """Resume los errores de Pydantic sin devolver lo que mando el cliente.

    Los errores originales incluyen `input` (el valor recibido, que puede ser
    un dato fiscal) y `ctx` (que puede no ser serializable). Solo se devuelve
    donde esta el problema y que regla fallo.
    """
    return [
        {
            "campo": ".".join(str(parte) for parte in detalle.get("loc", ())),
            "mensaje": detalle.get("msg", ""),
            "tipo": detalle.get("type", ""),
        }
        for detalle in error.errors()
    ]


def registrar_manejadores(app: FastAPI) -> None:
    """Conecta los manejadores de error a la aplicacion."""

    @app.exception_handler(ErrorDeApi)
    async def _error_de_api(request: Request, exc: ErrorDeApi) -> JSONResponse:
        return respuesta_de_error(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _error_de_validacion(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return respuesta_de_error(
            422,
            "validacion",
            "Los datos enviados no son validos.",
            {"campos": _campos_de_validacion(exc)},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _error_http(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        codigo = CODIGOS_HTTP.get(exc.status_code, "error_http")
        mensaje = exc.detail if isinstance(exc.detail, str) else "Error HTTP."
        return respuesta_de_error(exc.status_code, codigo, mensaje)
