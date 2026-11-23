"""Del nombre que dice el usuario al id que espera la API.

El usuario habla de "Buckfast" o de "Apícola del Sur", no de uuids. Las
herramientas de escritura aceptan nombres y los resuelven aca contra la
base del criadero (RLS: un nombre de otro criadero no existe). Si el nombre
no aparece o coincide con mas de uno, la herramienta devuelve el error al
modelo con las opciones, para que pregunte en lugar de adivinar.

Ninguna consulta filtra por tenant_id: RLS se encarga (ADR 0009).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.paginacion import LIMITE_MAXIMO, Pagina, ParametrosDePagina
from app.services.produccion import (
    repositorio_bancos,
    repositorio_items_vendibles,
    repositorio_materiales,
    repositorio_parques,
    repositorio_razas,
)

# Los catalogos de un criadero son chicos (3 razas, un par de parques): una
# pagina del maximo alcanza para resolver cualquier nombre.
PAGINA_CATALOGO = ParametrosDePagina(limite=LIMITE_MAXIMO, cursor=None)

# Cuantas opciones se le muestran al modelo en un error: suficientes para
# preguntar, sin inflar los tokens.
MAX_OPCIONES_EN_ERROR = 10

# Lo mismo que normalizar() pero en SQL, para buscar clientes por nombre sin
# traer la tabla entera: minusculas y sin tildes.
_CON_TILDE = "áéíóúüñàèìòù"
_SIN_TILDE = "aeiouunaeiou"

BUSCAR_CLIENTES = text(
    "SELECT id, nombre, pais, tipo, condicion_iva, activo FROM cliente "
    "WHERE (activo OR :incluir_inactivos) "
    "AND (CAST(:patron AS text) IS NULL "
    "OR translate(lower(nombre), :con_tilde, :sin_tilde) LIKE :patron "
    "ESCAPE '\\') "
    "ORDER BY nombre, id LIMIT :limite"
)

NOMBRES_DE_CLIENTES = text("SELECT id, nombre FROM cliente WHERE id = ANY(:ids)")


class ErrorDeHerramienta(Exception):
    """Un problema que el modelo puede resolver: corregir o preguntar.

    El mensaje va al modelo (como dato) y nunca al usuario directamente.
    """


@dataclass(frozen=True)
class Opcion:
    id: UUID
    nombre: str


def normalizar(texto: str) -> str:
    """Minusculas, sin tildes y con un solo espacio: "Apícola  del Sur" ==
    "apicola del sur"."""
    descompuesto = unicodedata.normalize("NFKD", texto)
    sin_tildes = "".join(c for c in descompuesto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", sin_tildes).strip().lower()


def _listar_nombres(opciones: Sequence[Opcion]) -> str:
    nombres = [f'"{o.nombre}"' for o in opciones[:MAX_OPCIONES_EN_ERROR]]
    return ", ".join(nombres) if nombres else "ninguna"


def resolver(nombre: str, opciones: Sequence[Opcion], que: str) -> Opcion:
    """La opcion que corresponde al nombre, o ErrorDeHerramienta.

    Gana la coincidencia exacta (sin tildes ni mayusculas); si no hay, una
    sola que contenga el texto. Dos o mas es ambiguo: nunca se elige una.
    """
    buscado = normalizar(nombre)
    if not buscado:
        raise ErrorDeHerramienta(f"Falta el nombre de {que}.")
    exactas = [o for o in opciones if normalizar(o.nombre) == buscado]
    candidatas = exactas or [o for o in opciones if buscado in normalizar(o.nombre)]
    if len(candidatas) == 1:
        return candidatas[0]
    if not candidatas:
        disponibles = f" Opciones: {_listar_nombres(opciones)}." if opciones else ""
        raise ErrorDeHerramienta(
            f'No existe {que} "{nombre}" en el criadero (o está dado de baja).'
            f"{disponibles} Preguntale al usuario."
        )
    raise ErrorDeHerramienta(
        f'"{nombre}" es ambiguo: coincide con {_listar_nombres(candidatas)}. '
        f"Preguntale al usuario cuál {que} es."
    )


def _opciones(
    listar: Callable[..., Pagina[Any]],
    sesion: Session,
    campo: str,
    *,
    incluir_inactivos: bool,
) -> list[Opcion]:
    pagina = listar(sesion, PAGINA_CATALOGO, incluir_inactivos=incluir_inactivos)
    return [Opcion(item.id, getattr(item, campo)) for item in pagina.items]


def razas(sesion: Session, *, incluir_inactivas: bool = False) -> list[Opcion]:
    return _opciones(
        repositorio_razas.listar, sesion, "nombre", incluir_inactivos=incluir_inactivas
    )


def productos(sesion: Session) -> list[Opcion]:
    """Items vendibles activos: los inactivos no se venden."""
    return _opciones(
        repositorio_items_vendibles.listar, sesion, "nombre", incluir_inactivos=False
    )


def parques(sesion: Session) -> list[Opcion]:
    return _opciones(
        repositorio_parques.listar, sesion, "nombre", incluir_inactivos=False
    )


def bancos(sesion: Session) -> list[Opcion]:
    return _opciones(
        repositorio_bancos.listar, sesion, "identificador", incluir_inactivos=False
    )


def materiales(sesion: Session) -> list[Opcion]:
    return _opciones(
        repositorio_materiales.listar, sesion, "nombre", incluir_inactivos=False
    )


def _patron(nombre: str | None) -> str | None:
    """%texto% para LIKE, con los comodines del usuario escapados."""
    if nombre is None or not normalizar(nombre):
        return None
    escapado = re.sub(r"([\\%_])", r"\\\1", normalizar(nombre))
    return f"%{escapado}%"


def buscar_clientes(
    sesion: Session,
    nombre: str | None,
    *,
    incluir_inactivos: bool = False,
    limite: int = LIMITE_MAXIMO,
) -> list[Any]:
    """Clientes cuyo nombre contiene el texto (sin tildes ni mayusculas)."""
    return list(
        sesion.execute(
            BUSCAR_CLIENTES,
            {
                "incluir_inactivos": incluir_inactivos,
                "patron": _patron(nombre),
                "con_tilde": _CON_TILDE,
                "sin_tilde": _SIN_TILDE,
                "limite": limite,
            },
        )
    )


def cliente(sesion: Session, nombre: str, *, incluir_inactivos: bool = False) -> Any:
    """La fila del cliente que corresponde al nombre, o ErrorDeHerramienta."""
    filas = buscar_clientes(sesion, nombre, incluir_inactivos=incluir_inactivos)
    elegido = resolver(nombre, [Opcion(f.id, f.nombre) for f in filas], "el cliente")
    return next(f for f in filas if f.id == elegido.id)


def nombres_de_clientes(sesion: Session, ids: Iterable[UUID]) -> dict[UUID, str]:
    """Nombre de cada cliente, para mostrar en lugar del id."""
    filas = sesion.execute(NOMBRES_DE_CLIENTES, {"ids": list(set(ids))})
    return {fila.id: fila.nombre for fila in filas}
