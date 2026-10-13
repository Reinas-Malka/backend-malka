"""Piezas comunes de los repositorios (#30).

Los repositorios escriben SQL explicito con parametros (:nombre), nunca con
f-strings, y no filtran por tenant: RLS se encarga (ADR 0009).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError
from sqlalchemy import Row, TextClause
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errores import ErrorDeApi


def escribir(
    sesion: Session,
    sentencia: TextClause,
    parametros: dict[str, Any],
    *,
    conflictos: Mapping[str, Callable[[], ErrorDeApi]],
    no_encontrado: Callable[[], ErrorDeApi],
) -> Row[Any]:
    """Ejecuta un INSERT o UPDATE ... RETURNING.

    `conflictos` traduce el nombre de una restriccion UNIQUE de la base al
    error de la API (409). Cualquier otra violacion (un CHECK, por ejemplo)
    es un bug: no se traduce y termina en 500, que queda en el log con su
    traza. Si el UPDATE no encontro la fila (no existe o es de otro criadero),
    responde `no_encontrado`.
    """
    try:
        fila = sesion.execute(sentencia, parametros).one_or_none()
    except IntegrityError as error:
        diagnostico = getattr(error.orig, "diag", None)
        restriccion = str(getattr(diagnostico, "constraint_name", None))
        if restriccion in conflictos:
            raise conflictos[restriccion]() from None
        raise
    if fila is None:
        raise no_encontrado()
    return fila


def validar_cambios[M: BaseModel](
    modelo: type[M], guardado: BaseModel, cambios: BaseModel
) -> M:
    """Aplica un PATCH sobre lo guardado y valida el resultado completo.

    Las reglas son las del schema de alta, sin repetirlas en el de edicion.
    El error sale con el mismo 422 y la misma forma que en un POST.
    """
    pedidos = cambios.model_dump(exclude_unset=True, exclude={"activo"})
    actuales = guardado.model_dump(include=set(modelo.model_fields))
    try:
        return modelo.model_validate({**actuales, **pedidos})
    except ValidationError as error:
        raise RequestValidationError(error.errors()) from None
