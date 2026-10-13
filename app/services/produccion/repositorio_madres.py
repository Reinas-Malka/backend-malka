"""Acceso a la tabla madre (#30).

Mismas reglas que razas: sin filtro por tenant (RLS, ADR 0009), 404 para lo
que es de otro criadero y borrado logico.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Row, text
from sqlalchemy.orm import Session

from app.errores import ConflictoError, ErrorDeApi, NoEncontradoError
from app.paginacion import Pagina, ParametrosDePagina, armar_pagina, valores_del_cursor
from app.schemas.produccion import MadreActualizar, MadreCrear, MadreRespuesta
from app.services.repositorio import escribir, validar_cambios

INSERTAR = text(
    "INSERT INTO madre (identificacion) VALUES (:identificacion) "
    "RETURNING id, identificacion, activo"
)

# Orden por identificacion; el id desempata (cursor: identificacion, id).
LISTAR = text(
    "SELECT id, identificacion, activo FROM madre "
    "WHERE (activo OR :incluir_inactivos) "
    "AND (:sin_cursor "
    "OR (identificacion, id) > (:c_identificacion, CAST(:c_id AS uuid))) "
    "ORDER BY identificacion, id LIMIT :limite"
)

OBTENER = text("SELECT id, identificacion, activo FROM madre WHERE id = :id")

OBTENER_PARA_EDITAR = text(
    "SELECT id, identificacion, activo FROM madre WHERE id = :id FOR UPDATE"
)

ACTUALIZAR = text(
    "UPDATE madre SET identificacion = :identificacion, activo = :activo, "
    "actualizado_en = now() WHERE id = :id "
    "RETURNING id, identificacion, activo"
)

DESACTIVAR = text(
    "UPDATE madre SET activo = false, actualizado_en = now() "
    "WHERE id = :id RETURNING id"
)


def _duplicada() -> ErrorDeApi:
    return ConflictoError(
        "Ya hay una madre con esa identificacion.", code="madre_duplicada"
    )


def _no_encontrada() -> ErrorDeApi:
    return NoEncontradoError("No existe la madre.", code="madre_no_encontrada")


# Nombre de la restriccion UNIQUE en la migracion 0003.
CONFLICTOS = {"uq_madre_tenant_identificacion": _duplicada}


def _a_respuesta(fila: Row[Any]) -> MadreRespuesta:
    return MadreRespuesta.model_validate(dict(fila._mapping))


def crear(sesion: Session, datos: MadreCrear) -> MadreRespuesta:
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
) -> Pagina[MadreRespuesta]:
    c_identificacion, c_id = valores_del_cursor(pagina, (str, UUID))
    filas = sesion.execute(
        LISTAR,
        {
            "incluir_inactivos": incluir_inactivos,
            "sin_cursor": pagina.cursor is None,
            "c_identificacion": c_identificacion,
            "c_id": c_id,
            "limite": pagina.limite + 1,
        },
    )
    return armar_pagina(
        [_a_respuesta(fila) for fila in filas],
        pagina.limite,
        lambda madre: (madre.identificacion, madre.id),
    )


def obtener(sesion: Session, madre_id: UUID) -> MadreRespuesta:
    fila = sesion.execute(OBTENER, {"id": madre_id}).one_or_none()
    if fila is None:
        raise _no_encontrada()
    return _a_respuesta(fila)


def actualizar(
    sesion: Session, madre_id: UUID, cambios: MadreActualizar
) -> MadreRespuesta:
    fila = sesion.execute(OBTENER_PARA_EDITAR, {"id": madre_id}).one_or_none()
    if fila is None:
        raise _no_encontrada()
    actual = _a_respuesta(fila)
    validado = validar_cambios(MadreCrear, actual, cambios)
    activo = actual.activo if cambios.activo is None else cambios.activo
    fila = escribir(
        sesion,
        ACTUALIZAR,
        {**validado.model_dump(), "activo": activo, "id": madre_id},
        conflictos=CONFLICTOS,
        no_encontrado=_no_encontrada,
    )
    return _a_respuesta(fila)


def desactivar(sesion: Session, madre_id: UUID) -> None:
    """Borrado logico: la madre sigue existiendo para sus tandas historicas."""
    escribir(
        sesion,
        DESACTIVAR,
        {"id": madre_id},
        conflictos={},
        no_encontrado=_no_encontrada,
    )
