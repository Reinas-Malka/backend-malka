"""Acceso a la tabla raza (#30).

Toda funcion recibe la sesion de obtener_sesion, que ya tiene el tenant fijado:
ninguna consulta filtra por tenant_id, de eso se encarga RLS (ADR 0009). Una
raza de otro criadero no aparece, asi que se responde 404 y no 403: decir
"no tenes permiso" confirmaria que existe.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Row, text
from sqlalchemy.orm import Session

from app.errores import ConflictoError, ErrorDeApi, NoEncontradoError
from app.paginacion import Pagina, ParametrosDePagina, armar_pagina, valores_del_cursor
from app.schemas.produccion import RazaActualizar, RazaCrear, RazaRespuesta
from app.services.repositorio import escribir, validar_cambios

# Las consultas se escriben completas, sin armar SQL con f-strings: los valores
# viajan siempre como parametros (:nombre), nunca pegados al texto.
INSERTAR = text(
    "INSERT INTO raza (nombre, descripcion) VALUES (:nombre, :descripcion) "
    "RETURNING id, nombre, descripcion, activo"
)

# Orden por nombre; el id desempata. El cursor es (nombre, id) de la ultima
# fila devuelta: la pagina siguiente empieza despues de ella.
LISTAR = text(
    "SELECT id, nombre, descripcion, activo FROM raza "
    "WHERE (activo OR :incluir_inactivos) "
    "AND (:sin_cursor OR (nombre, id) > (:c_nombre, CAST(:c_id AS uuid))) "
    "ORDER BY nombre, id LIMIT :limite"
)

OBTENER = text("SELECT id, nombre, descripcion, activo FROM raza WHERE id = :id")

# FOR UPDATE: si dos PATCH llegan juntos, el segundo espera al primero y
# valida sobre lo que el primero guardo.
OBTENER_PARA_EDITAR = text(
    "SELECT id, nombre, descripcion, activo FROM raza WHERE id = :id FOR UPDATE"
)

ACTUALIZAR = text(
    "UPDATE raza SET nombre = :nombre, descripcion = :descripcion, "
    "activo = :activo, actualizado_en = now() WHERE id = :id "
    "RETURNING id, nombre, descripcion, activo"
)

DESACTIVAR = text(
    "UPDATE raza SET activo = false, actualizado_en = now() "
    "WHERE id = :id RETURNING id"
)


def _duplicada() -> ErrorDeApi:
    return ConflictoError("Ya hay una raza con ese nombre.", code="raza_duplicada")


def _no_encontrada() -> ErrorDeApi:
    return NoEncontradoError("No existe la raza.", code="raza_no_encontrada")


# Nombre de la restriccion UNIQUE en la migracion 0001.
CONFLICTOS = {"uq_raza_tenant_nombre": _duplicada}


def _a_respuesta(fila: Row[Any]) -> RazaRespuesta:
    return RazaRespuesta.model_validate(dict(fila._mapping))


def crear(sesion: Session, datos: RazaCrear) -> RazaRespuesta:
    fila = escribir(
        sesion,
        INSERTAR,
        datos.model_dump(),
        conflictos=CONFLICTOS,
        no_encontrado=_no_encontrada,
    )
    return _a_respuesta(fila)


def listar(
    sesion: Session, pagina: ParametrosDePagina, *, incluir_inactivos: bool = False
) -> Pagina[RazaRespuesta]:
    c_nombre, c_id = valores_del_cursor(pagina, (str, UUID))
    filas = sesion.execute(
        LISTAR,
        {
            "incluir_inactivos": incluir_inactivos,
            "sin_cursor": pagina.cursor is None,
            "c_nombre": c_nombre,
            "c_id": c_id,
            # Uno de mas: si viene, hay pagina siguiente.
            "limite": pagina.limite + 1,
        },
    )
    return armar_pagina(
        [_a_respuesta(fila) for fila in filas],
        pagina.limite,
        lambda raza: (raza.nombre, raza.id),
    )


def obtener(sesion: Session, raza_id: UUID) -> RazaRespuesta:
    fila = sesion.execute(OBTENER, {"id": raza_id}).one_or_none()
    if fila is None:
        raise _no_encontrada()
    return _a_respuesta(fila)


def actualizar(
    sesion: Session, raza_id: UUID, cambios: RazaActualizar
) -> RazaRespuesta:
    fila = sesion.execute(OBTENER_PARA_EDITAR, {"id": raza_id}).one_or_none()
    if fila is None:
        raise _no_encontrada()
    actual = _a_respuesta(fila)
    validado = validar_cambios(RazaCrear, actual, cambios)
    activo = actual.activo if cambios.activo is None else cambios.activo
    fila = escribir(
        sesion,
        ACTUALIZAR,
        {**validado.model_dump(), "activo": activo, "id": raza_id},
        conflictos=CONFLICTOS,
        no_encontrado=_no_encontrada,
    )
    return _a_respuesta(fila)


def desactivar(sesion: Session, raza_id: UUID) -> None:
    """Borrado logico: la raza sigue existiendo para sus tandas historicas."""
    escribir(
        sesion,
        DESACTIVAR,
        {"id": raza_id},
        conflictos={},
        no_encontrado=_no_encontrada,
    )
