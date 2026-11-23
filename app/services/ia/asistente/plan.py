"""Planes de varios pasos: propuestas del mismo turno que dependen entre si.

"Crea el parque Casa Santi con 10 nucleos" son dos escrituras, y la
segunda necesita el id que va a devolver la primera. Las dos se proponen en
el mismo turno: la de los nucleos lleva un marcador {{ref:<id>.id}} donde va
el id del parque, y el front lo reemplaza con el id de la respuesta del paso
del que depende al confirmar (en orden). Nada se ejecuta aca.

Los nombres se resuelven primero contra la base y despues contra las
propuestas pendientes del turno (lo que se va a crear). Lo ambiguo vuelve
al modelo, igual que en resolucion.py.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from app.schemas.asistente import AccionPropuesta, Propuesta
from app.services.ia.asistente.resolucion import (
    MAX_OPCIONES_EN_ERROR,
    ErrorDeHerramienta,
    Opcion,
    normalizar,
)

# Propuestas por respuesta: un plan razonable entra; mas, se pide de a partes.
MAX_PROPUESTAS = 10

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
MARCADOR = re.compile(r"\{\{ref:(" + _UUID + r")\.id\}\}")


def marcador(propuesta_id: UUID) -> str:
    """Donde va el id del recurso que crea esa propuesta."""
    return f"{{{{ref:{propuesta_id}.id}}}}"


def dependencias(ruta: str, cuerpo: Any) -> list[UUID]:
    """Las propuestas que nombran los marcadores de la ruta y el cuerpo, en
    el orden en que aparecen y sin repetir."""
    vistos: list[UUID] = []
    for texto in [ruta, *_textos(cuerpo)]:
        for coincidencia in MARCADOR.finditer(texto):
            propuesta_id = UUID(coincidencia.group(1))
            if propuesta_id not in vistos:
                vistos.append(propuesta_id)
    return vistos


def _textos(valor: Any) -> Iterable[str]:
    if isinstance(valor, str):
        yield valor
    elif isinstance(valor, dict):
        for item in valor.values():
            yield from _textos(item)
    elif isinstance(valor, list):
        for item in valor:
            yield from _textos(item)


def sustituir(valor: Any, reemplazos: dict[str, str]) -> Any:
    """Copia de valor con cada string que es exactamente una clave del mapa
    reemplazado (los marcadores son campos enteros: un id)."""
    if isinstance(valor, str):
        return reemplazos.get(valor, valor)
    if isinstance(valor, dict):
        return {clave: sustituir(item, reemplazos) for clave, item in valor.items()}
    if isinstance(valor, list):
        return [sustituir(item, reemplazos) for item in valor]
    return valor


def con_uuids_de_prueba(cuerpo: Any) -> tuple[Any, dict[str, str]]:
    """El cuerpo con un uuid de prueba distinto por marcador (para validarlo
    contra el schema real) y el mapa para volver a poner los marcadores."""
    reemplazos = {texto: str(uuid4()) for texto in set(_textos(cuerpo))}
    reemplazos = {k: v for k, v in reemplazos.items() if MARCADOR.fullmatch(k)}
    inverso = {prueba: original for original, prueba in reemplazos.items()}
    return sustituir(cuerpo, reemplazos), inverso


@dataclass(frozen=True)
class Elegido:
    """Lo que resolvio un nombre: algo de la base (id real) o algo que
    crea una propuesta pendiente del turno (id = marcador)."""

    id: str
    nombre: str
    pendiente: Propuesta | None = None


def pendientes_de(
    pendientes: Sequence[Propuesta],
    accion: AccionPropuesta,
    nombre: Callable[[Propuesta], str | None] | None = None,
) -> list[tuple[Propuesta, str]]:
    """Las propuestas pendientes de esa accion con el nombre de lo que crean
    (por defecto, el campo nombre del cuerpo)."""
    elegir = nombre or (lambda p: (p.cuerpo or {}).get("nombre"))
    return [
        (p, str(texto))
        for p in pendientes
        if p.accion == accion and (texto := elegir(p))
    ]


def resolver(
    nombre: str,
    base: Sequence[Opcion],
    pendientes: Sequence[tuple[Propuesta, str]],
    que: str,
) -> Elegido:
    """Primero la base, despues las propuestas del turno.

    Gana la coincidencia exacta (sin tildes ni mayusculas) en la base, luego
    la exacta en el turno; si no hay, una sola que contenga el texto entre
    las dos. Dos o mas es ambiguo: nunca se elige una.
    """
    buscado = normalizar(nombre)
    if not buscado:
        raise ErrorDeHerramienta(f"Falta el nombre de {que}.")
    candidatos = [Elegido(str(o.id), o.nombre) for o in base] + [
        Elegido(marcador(p.id), texto, p) for p, texto in pendientes
    ]
    for grupo in (
        [c for c in candidatos if c.pendiente is None],
        [c for c in candidatos if c.pendiente is not None],
    ):
        exactos = [c for c in grupo if normalizar(c.nombre) == buscado]
        if len(exactos) == 1:
            return exactos[0]
        if len(exactos) > 1:
            raise _ambiguo(nombre, exactos, que)
    parciales = [c for c in candidatos if buscado in normalizar(c.nombre)]
    if len(parciales) == 1:
        return parciales[0]
    if parciales:
        raise _ambiguo(nombre, parciales, que)
    nombres = ", ".join(f'"{c.nombre}"' for c in candidatos[:MAX_OPCIONES_EN_ERROR])
    opciones = f" Opciones: {nombres}." if nombres else ""
    raise ErrorDeHerramienta(
        f'No existe {que} "{nombre}" en el criadero ni en las propuestas de '
        f"este turno.{opciones} Preguntale al usuario."
    )


def _ambiguo(nombre: str, candidatos: Sequence[Elegido], que: str) -> Exception:
    nombres = ", ".join(
        f'"{c.nombre}"' + (" (propuesto en este turno)" if c.pendiente else "")
        for c in candidatos[:MAX_OPCIONES_EN_ERROR]
    )
    return ErrorDeHerramienta(
        f'"{nombre}" es ambiguo: coincide con {nombres}. Preguntale al usuario '
        f"cuál {que} es."
    )


def pendiente(
    pendientes: Sequence[Propuesta], propuesta_id: UUID, accion: AccionPropuesta
) -> Propuesta | None:
    """La propuesta pendiente con ese id y esa accion, si la hay."""
    return next(
        (p for p in pendientes if p.id == propuesta_id and p.accion == accion), None
    )


def valor_del_resumen(propuesta: Propuesta, etiqueta: str) -> str:
    return next(
        (par.valor for par in propuesta.resumen if par.etiqueta == etiqueta), "?"
    )
