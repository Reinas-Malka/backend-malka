"""Tipos comunes de las herramientas del asistente."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.paginacion import ParametrosDePagina
from app.schemas.asistente import Propuesta

# Filas por resultado de una herramienta: suficientes para responder, sin
# inflar los tokens de la llamada siguiente.
MAX_FILAS = 20
PAGINA_CORTA = ParametrosDePagina(limite=MAX_FILAS, cursor=None)


@dataclass(frozen=True)
class Contexto:
    """Con que corre una herramienta: la sesion del tenant y los roles."""

    sesion: Session
    grupos: frozenset[str]


@dataclass(frozen=True)
class Resultado:
    """Lo que devuelve una herramienta.

    - datos: lo que ve el modelo (JSON compacto, delimitado como dato).
    - resumen: lo que ve el usuario ("Consulté la disponibilidad").
    - propuesta: solo las de escritura; nada se ejecuto.
    """

    datos: Any
    resumen: str
    propuesta: Propuesta | None = None
