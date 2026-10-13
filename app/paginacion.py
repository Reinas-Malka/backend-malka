"""Paginacion por cursor de los listados (#30).

Todo listado devuelve {"items": [...], "siguiente": "<cursor>" | null}. Para
pedir la pagina siguiente se manda ese cursor en ?cursor=. Cuando siguiente
es null, no hay mas.

Por que cursor y no ?pagina=3: el cursor es la clave de orden de la ultima
fila devuelta (por ejemplo, nombre e id). La pagina siguiente empieza despues
de esa fila, asi que un alta o una baja entre dos pedidos no hace que se
repitan ni se salteen filas.

El cursor es opaco para el frontend: lo recibe y lo devuelve tal cual. Por
dentro es la clave de orden en JSON, codificada en base64 url-safe. No es un
secreto: RLS ya impide ver filas de otro criadero aunque se arme a mano.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel

from app.errores import ValidacionError

LIMITE_POR_DEFECTO = 50
LIMITE_MAXIMO = 100

T = TypeVar("T")


class Pagina(BaseModel, Generic[T]):
    """Respuesta de todo listado."""

    items: list[T]
    # Cursor para pedir la pagina siguiente; null si esta es la ultima.
    siguiente: str | None = None


@dataclass(frozen=True)
class ParametrosDePagina:
    limite: int
    cursor: str | None


def parametros_de_pagina(
    limite: int = Query(
        LIMITE_POR_DEFECTO,
        ge=1,
        le=LIMITE_MAXIMO,
        description=f"Cantidad maxima de items (1 a {LIMITE_MAXIMO}).",
    ),
    cursor: str | None = Query(
        None,
        max_length=500,
        description="El campo `siguiente` de la pagina anterior.",
    ),
) -> ParametrosDePagina:
    """Dependencia de los listados: ?limite=&cursor=."""
    return ParametrosDePagina(limite=limite, cursor=cursor)


def codificar_cursor(valores: Sequence[object]) -> str:
    texto = json.dumps([str(valor) for valor in valores], separators=(",", ":"))
    return base64.urlsafe_b64encode(texto.encode("utf-8")).decode("ascii").rstrip("=")


def decodificar_cursor(cursor: str, tipos: Sequence[Callable[[str], Any]]) -> list[Any]:
    """Devuelve los valores del cursor convertidos con `tipos` (str, int, UUID).

    Un cursor roto o armado a mano responde 422 y nunca llega a la base: un
    valor que no es un UUID haria fallar la consulta con un 500.
    """
    try:
        relleno = "=" * (-len(cursor) % 4)
        valores = json.loads(base64.urlsafe_b64decode(cursor + relleno))
        if not isinstance(valores, list) or len(valores) != len(tipos):
            raise ValueError("cantidad de valores")
        if not all(isinstance(valor, str) for valor in valores):
            raise ValueError("tipo de valores")
        return [
            convertir(valor) for convertir, valor in zip(tipos, valores, strict=True)
        ]
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise ValidacionError(
            "El cursor no es valido: usa el campo `siguiente` de la pagina anterior.",
            code="cursor_invalido",
        ) from None


def valores_del_cursor(
    pagina: ParametrosDePagina, tipos: Sequence[Callable[[str], Any]]
) -> list[Any]:
    """Valores del cursor para la consulta; None en la primera pagina."""
    if pagina.cursor is None:
        return [None] * len(tipos)
    return decodificar_cursor(pagina.cursor, tipos)


def armar_pagina(
    items: list[T], limite: int, clave: Callable[[T], Sequence[object]]
) -> Pagina[T]:
    """Arma la pagina con `limite + 1` items leidos de la base.

    Si vino uno de mas, hay pagina siguiente, y el cursor es la clave de orden
    del ultimo item que se devuelve.
    """
    if len(items) <= limite:
        return Pagina[T](items=items, siguiente=None)
    visibles = items[:limite]
    return Pagina[T](items=visibles, siguiente=codificar_cursor(clave(visibles[-1])))
