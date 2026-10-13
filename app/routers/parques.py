"""Endpoints de parques de fecundacion (#30).

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
    ParqueActualizar,
    ParqueCrear,
    ParqueRespuesta,
)
from app.services.produccion import repositorio_parques

router = APIRouter(prefix="/api/v1/parques", tags=["parques"])

# Corre antes que obtener_sesion: un rol sin permiso no abre ninguna conexion.
SOLO_ESCRITURA = [Depends(require_role("admin", "produccion"))]


@router.post(
    "", status_code=201, response_model=ParqueRespuesta, dependencies=SOLO_ESCRITURA
)
def crear_parque(
    datos: ParqueCrear,
    # Antes que la sesion: una clave mal formada no abre ninguna conexion.
    pedido: PedidoIdempotente = Depends(pedido_idempotente),
    sesion: Session = Depends(obtener_sesion),
) -> JSONResponse:
    # Con Idempotency-Key, un reintento devuelve lo ya creado (#25).
    return responder_una_vez(
        sesion, pedido, lambda: repositorio_parques.crear(sesion, datos)
    )


@router.get("", response_model=Pagina[ParqueRespuesta])
def listar_parques(
    incluir_inactivos: bool = False,
    pagina: ParametrosDePagina = Depends(parametros_de_pagina),
    sesion: Session = Depends(obtener_sesion),
) -> Pagina[ParqueRespuesta]:
    return repositorio_parques.listar(
        sesion, pagina, incluir_inactivos=incluir_inactivos
    )


@router.get("/{parque_id}", response_model=ParqueRespuesta)
def obtener_parque(
    parque_id: UUID, sesion: Session = Depends(obtener_sesion)
) -> ParqueRespuesta:
    return repositorio_parques.obtener(sesion, parque_id)


@router.patch(
    "/{parque_id}", response_model=ParqueRespuesta, dependencies=SOLO_ESCRITURA
)
def actualizar_parque(
    parque_id: UUID,
    cambios: ParqueActualizar,
    sesion: Session = Depends(obtener_sesion),
) -> ParqueRespuesta:
    return repositorio_parques.actualizar(sesion, parque_id, cambios)


@router.delete(
    "/{parque_id}",
    status_code=204,
    response_class=Response,
    response_model=None,
    dependencies=SOLO_ESCRITURA,
)
def desactivar_parque(
    parque_id: UUID, sesion: Session = Depends(obtener_sesion)
) -> None:
    repositorio_parques.desactivar(sesion, parque_id)
