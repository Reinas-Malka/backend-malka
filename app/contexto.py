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


# Identidad del request, la completa app/auth.py al validar el token.
# Sin token validado quedan en None: nada fuera de una ruta protegida las usa.
tenant_id: ContextVar[str | None] = ContextVar("tenant_id", default=None)
user_id: ContextVar[str | None] = ContextVar("user_id", default=None)
grupos: ContextVar[frozenset[str]] = ContextVar("grupos", default=frozenset())


def tenant_id_actual() -> str | None:
    """Devuelve el tenant del token del request en curso, si ya se valido."""
    return tenant_id.get()


def user_id_actual() -> str | None:
    """Devuelve el usuario (sub de Cognito) del request en curso."""
    return user_id.get()


def grupos_actuales() -> frozenset[str]:
    """Devuelve los grupos de Cognito del usuario del request en curso."""
    return grupos.get()
