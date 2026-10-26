"""Endpoints de tandas (#27) y de sus operaciones por lote (#28).

Leer puede cualquier rol del criadero; dar de alta y operar, solo admin y
produccion.
Los permisos se declaran en cada ruta y la validacion vive en los schemas y
el repositorio: los endpoints solo conectan el request con el repositorio.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db import obtener_sesion
from app.idempotencia import PedidoIdempotente, pedido_idempotente, responder_una_vez
from app.paginacion import Pagina, ParametrosDePagina, parametros_de_pagina
from app.schemas.produccion import (
    OperacionCrear,
    OperacionRealizada,
    TandaCreada,
    TandaCrear,
    TandaDetalle,
    TandaRespuesta,
)
from app.services.produccion import repositorio_operaciones, repositorio_tandas

router = APIRouter(prefix="/api/v1/tandas", tags=["tandas"])

# Corre antes que obtener_sesion: un rol sin permiso no abre ninguna conexion.
SOLO_ESCRITURA = [Depends(require_role("admin", "produccion"))]


@router.post(
    "", status_code=201, response_model=TandaCreada, dependencies=SOLO_ESCRITURA
)
def crear_tanda(
    datos: TandaCrear,
    # Antes que la sesion: una clave mal formada no abre ninguna conexion.
    pedido: PedidoIdempotente = Depends(pedido_idempotente),
    sesion: Session = Depends(obtener_sesion),
) -> JSONResponse:
    # Con Idempotency-Key, un reintento devuelve la tanda ya creada y no
    # descuenta el material dos veces (#25).
    return responder_una_vez(
        sesion, pedido, lambda: repositorio_tandas.crear(sesion, datos)
    )


@router.get("", response_model=Pagina[TandaRespuesta])
def listar_tandas(
    raza_id: UUID | None = None,
    # Rango de fecha_traslarve, con los dos dias incluidos.
    desde: date | None = None,
    hasta: date | None = None,
    pagina: ParametrosDePagina = Depends(parametros_de_pagina),
    sesion: Session = Depends(obtener_sesion),
) -> Pagina[TandaRespuesta]:
    return repositorio_tandas.listar(
        sesion, pagina, raza_id=raza_id, desde=desde, hasta=hasta
    )


@router.get("/{tanda_id}", response_model=TandaDetalle)
def obtener_tanda(
    tanda_id: UUID, sesion: Session = Depends(obtener_sesion)
) -> TandaDetalle:
    """La tanda con sus celdas por estado, sus nucleos ocupados y sus bancos."""
    return repositorio_tandas.detalle(sesion, tanda_id)


@router.post(
    "/{tanda_id}/operaciones",
    status_code=201,
    response_model=OperacionRealizada,
    dependencies=SOLO_ESCRITURA,
)
def operar_tanda(
    tanda_id: UUID,
    datos: OperacionCrear,
    # Antes que la sesion: una clave mal formada no abre ninguna conexion.
    pedido: PedidoIdempotente = Depends(pedido_idempotente),
    sesion: Session = Depends(obtener_sesion),
) -> JSONResponse:
    """Descartar, introducir, cosechar o pasar a banco, por lote (#28).

    El campo accion elige la operacion. Con Idempotency-Key, un reintento
    devuelve lo ya hecho y no mueve celdas dos veces (#25).
    """
    return responder_una_vez(
        sesion, pedido, lambda: repositorio_operaciones.operar(sesion, tanda_id, datos)
    )
