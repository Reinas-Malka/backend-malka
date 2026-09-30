"""Datos del request en curso, accesibles desde cualquier parte del codigo.

Se guardan en variables de contexto (contextvars): cada request tiene su propio
valor aunque se atiendan varios a la vez, y no hace falta pasar el request_id
de funcion en funcion para poder loguearlo o devolverlo en un error.
"""

from __future__ import annotations

from contextvars import ContextVar

SIN_REQUEST_ID = "sin-request-id"

request_id: ContextVar[str] = ContextVar("request_id", default=SIN_REQUEST_ID)


def request_id_actual() -> str:
    """Devuelve el request_id del request que se esta atendiendo."""
    return request_id.get()
