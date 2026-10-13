"""Endpoints de razas (#30).

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
    RazaActualizar,
    RazaCrear,
    RazaRespuesta,
)
from app.services.produccion import repositorio_razas

router = APIRouter(prefix="/api/v1/razas", tags=["razas"])

# Corre antes que obtener_sesion: un rol sin permiso no abre ninguna conexion.
SOLO_ESCRITURA = [Depends(require_role("admin", "produccion"))]


@router.post(
    "", status_code=201, response_model=RazaRespuesta, dependencies=SOLO_ESCRITURA
)
def crear_raza(
    datos: RazaCrear,
    # Antes que la sesion: una clave mal formada no abre ninguna conexion.
    pedido: PedidoIdempotente = Depends(pedido_idempotente),
    sesion: Session = Depends(obtener_sesion),
) -> JSONResponse:
    # Con Idempotency-Key, un reintento devuelve lo ya creado (#25).
    return responder_una_vez(
        sesion, pedido, lambda: repositorio_razas.crear(sesion, datos)
    )


@router.get("", response_model=Pagina[RazaRespuesta])
def listar_razas(
    incluir_inactivos: bool = False,
    pagina: ParametrosDePagina = Depends(parametros_de_pagina),
    sesion: Session = Depends(obtener_sesion),
) -> Pagina[RazaRespuesta]:
    return repositorio_razas.listar(sesion, pagina, incluir_inactivos=incluir_inactivos)


@router.get("/{raza_id}", response_model=RazaRespuesta)
def obtener_raza(
    raza_id: UUID, sesion: Session = Depends(obtener_sesion)
) -> RazaRespuesta:
    return repositorio_razas.obtener(sesion, raza_id)


@router.patch("/{raza_id}", response_model=RazaRespuesta, dependencies=SOLO_ESCRITURA)
def actualizar_raza(
    raza_id: UUID,
    cambios: RazaActualizar,
    sesion: Session = Depends(obtener_sesion),
) -> RazaRespuesta:
    return repositorio_razas.actualizar(sesion, raza_id, cambios)


@router.delete(
    "/{raza_id}",
    status_code=204,
    response_class=Response,
    response_model=None,
    dependencies=SOLO_ESCRITURA,
)
def desactivar_raza(raza_id: UUID, sesion: Session = Depends(obtener_sesion)) -> None:
    repositorio_razas.desactivar(sesion, raza_id)
