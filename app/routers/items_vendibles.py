"""Endpoints de items vendibles (#30).

Leer puede cualquier rol del criadero; escribir, solo admin y produccion. Los
permisos se declaran en cada ruta y la validacion vive en los schemas: los
endpoints solo conectan el request con el repositorio.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db import obtener_sesion
from app.idempotencia import PedidoIdempotente, pedido_idempotente, responder_una_vez
from app.paginacion import Pagina, ParametrosDePagina, parametros_de_pagina
from app.schemas.produccion import (
    ItemVendibleActualizar,
    ItemVendibleCrear,
    ItemVendibleRespuesta,
)
from app.services.produccion import repositorio_items_vendibles

router = APIRouter(prefix="/api/v1/items-vendibles", tags=["items-vendibles"])

# Corre antes que obtener_sesion: un rol sin permiso no abre ninguna conexion.
SOLO_ESCRITURA = [Depends(require_role("admin", "produccion"))]


@router.post(
    "",
    status_code=201,
    response_model=ItemVendibleRespuesta,
    dependencies=SOLO_ESCRITURA,
)
def crear_item(
    datos: ItemVendibleCrear,
    # Antes que la sesion: una clave mal formada no abre ninguna conexion.
    pedido: PedidoIdempotente = Depends(pedido_idempotente),
    sesion: Session = Depends(obtener_sesion),
) -> JSONResponse:
    # Con Idempotency-Key, un reintento devuelve lo ya creado (#25).
    return responder_una_vez(
        sesion, pedido, lambda: repositorio_items_vendibles.crear(sesion, datos)
    )


@router.get("", response_model=Pagina[ItemVendibleRespuesta])
def listar_items_vendibles(
    incluir_inactivos: bool = False,
    pagina: ParametrosDePagina = Depends(parametros_de_pagina),
    sesion: Session = Depends(obtener_sesion),
) -> Pagina[ItemVendibleRespuesta]:
    return repositorio_items_vendibles.listar(
        sesion, pagina, incluir_inactivos=incluir_inactivos
    )


@router.get("/{item_id}", response_model=ItemVendibleRespuesta)
def obtener_item(
    item_id: UUID, sesion: Session = Depends(obtener_sesion)
) -> ItemVendibleRespuesta:
    return repositorio_items_vendibles.obtener(sesion, item_id)


@router.patch(
    "/{item_id}", response_model=ItemVendibleRespuesta, dependencies=SOLO_ESCRITURA
)
def actualizar_item(
    item_id: UUID,
    cambios: ItemVendibleActualizar,
    sesion: Session = Depends(obtener_sesion),
) -> ItemVendibleRespuesta:
    return repositorio_items_vendibles.actualizar(sesion, item_id, cambios)


@router.delete(
    "/{item_id}",
    status_code=204,
    response_class=Response,
    response_model=None,
    dependencies=SOLO_ESCRITURA,
)
def desactivar_item(item_id: UUID, sesion: Session = Depends(obtener_sesion)) -> None:
    repositorio_items_vendibles.desactivar(sesion, item_id)
