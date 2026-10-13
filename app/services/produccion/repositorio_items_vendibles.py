"""Acceso a la tabla item_vendible (#30).

El catalogo de lo que se factura. La alicuota de IVA vive aca, no en el
cliente (docs/dominio.md), y solo admite los valores del enum Alicuota de
importes.py. El pedido (#34) copia la alicuota a su linea al crearse: cambiar
la del catalogo no recalcula pedidos existentes.

Sin filtro por tenant (RLS, ADR 0009), 404 para lo de otro criadero y borrado
logico, como en el resto de los datos maestros.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Row, text
from sqlalchemy.orm import Session

from app.errores import ConflictoError, ErrorDeApi, NoEncontradoError
from app.paginacion import Pagina, ParametrosDePagina, armar_pagina, valores_del_cursor
from app.schemas.produccion import (
    ItemVendibleActualizar,
    ItemVendibleCrear,
    ItemVendibleRespuesta,
)
from app.services.repositorio import escribir, validar_cambios

INSERTAR = text(
    "INSERT INTO item_vendible (nombre, alicuota) VALUES (:nombre, :alicuota) "
    "RETURNING id, nombre, alicuota, activo"
)

LISTAR = text(
    "SELECT id, nombre, alicuota, activo FROM item_vendible "
    "WHERE (activo OR :incluir_inactivos) "
    "AND (:sin_cursor OR (nombre, id) > (:c_nombre, CAST(:c_id AS uuid))) "
    "ORDER BY nombre, id LIMIT :limite"
)

OBTENER = text("SELECT id, nombre, alicuota, activo FROM item_vendible WHERE id = :id")

OBTENER_PARA_EDITAR = text(
    "SELECT id, nombre, alicuota, activo FROM item_vendible WHERE id = :id FOR UPDATE"
)

ACTUALIZAR = text(
    "UPDATE item_vendible SET nombre = :nombre, alicuota = :alicuota, "
    "activo = :activo, actualizado_en = now() WHERE id = :id "
    "RETURNING id, nombre, alicuota, activo"
)

DESACTIVAR = text(
    "UPDATE item_vendible SET activo = false, actualizado_en = now() "
    "WHERE id = :id RETURNING id"
)


def _duplicado() -> ErrorDeApi:
    return ConflictoError(
        "Ya hay un item vendible con ese nombre.", code="item_vendible_duplicado"
    )


def _no_encontrado() -> ErrorDeApi:
    return NoEncontradoError(
        "No existe el item vendible.", code="item_vendible_no_encontrado"
    )


# Nombre de la restriccion UNIQUE en la migracion 0004.
CONFLICTOS = {"uq_item_vendible_tenant_nombre": _duplicado}


def _a_respuesta(fila: Row[Any]) -> ItemVendibleRespuesta:
    return ItemVendibleRespuesta.model_validate(dict(fila._mapping))


def crear(sesion: Session, datos: ItemVendibleCrear) -> ItemVendibleRespuesta:
    fila = escribir(
        sesion,
        INSERTAR,
        datos.model_dump(),
        conflictos=CONFLICTOS,
        no_encontrado=_no_encontrado,
    )
    return _a_respuesta(fila)


def listar(
    sesion: Session, pagina: ParametrosDePagina, *, incluir_inactivos: bool = False
) -> Pagina[ItemVendibleRespuesta]:
    c_nombre, c_id = valores_del_cursor(pagina, (str, UUID))
    filas = sesion.execute(
        LISTAR,
        {
            "incluir_inactivos": incluir_inactivos,
            "sin_cursor": pagina.cursor is None,
            "c_nombre": c_nombre,
            "c_id": c_id,
            "limite": pagina.limite + 1,
        },
    )
    return armar_pagina(
        [_a_respuesta(fila) for fila in filas],
        pagina.limite,
        lambda item: (item.nombre, item.id),
    )


def obtener(sesion: Session, item_id: UUID) -> ItemVendibleRespuesta:
    fila = sesion.execute(OBTENER, {"id": item_id}).one_or_none()
    if fila is None:
        raise _no_encontrado()
    return _a_respuesta(fila)


def actualizar(
    sesion: Session, item_id: UUID, cambios: ItemVendibleActualizar
) -> ItemVendibleRespuesta:
    fila = sesion.execute(OBTENER_PARA_EDITAR, {"id": item_id}).one_or_none()
    if fila is None:
        raise _no_encontrado()
    actual = _a_respuesta(fila)
    validado = validar_cambios(ItemVendibleCrear, actual, cambios)
    activo = actual.activo if cambios.activo is None else cambios.activo
    fila = escribir(
        sesion,
        ACTUALIZAR,
        {**validado.model_dump(), "activo": activo, "id": item_id},
        conflictos=CONFLICTOS,
        no_encontrado=_no_encontrado,
    )
    return _a_respuesta(fila)


def desactivar(sesion: Session, item_id: UUID) -> None:
    """Borrado logico: el item sigue existiendo para los pedidos que lo usan."""
    escribir(
        sesion,
        DESACTIVAR,
        {"id": item_id},
        conflictos={},
        no_encontrado=_no_encontrado,
    )
