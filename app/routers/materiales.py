"""Endpoints de materiales y sus movimientos (#30).

El stock no se guarda: GET devuelve SUM(movimientos). Las compras y los
ajustes se cargan en /{material_id}/movimientos; el consumo lo registra el
alta de tanda (#27).

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
    MaterialActualizar,
    MaterialCrear,
    MaterialRespuesta,
    MovimientoCrear,
    MovimientoGuardado,
    MovimientoRespuesta,
)
from app.services.produccion import repositorio_materiales

router = APIRouter(prefix="/api/v1/materiales", tags=["materiales"])

# Corre antes que obtener_sesion: un rol sin permiso no abre ninguna conexion.
SOLO_ESCRITURA = [Depends(require_role("admin", "produccion"))]


@router.post(
    "", status_code=201, response_model=MaterialRespuesta, dependencies=SOLO_ESCRITURA
)
def crear_material(
    datos: MaterialCrear,
    # Antes que la sesion: una clave mal formada no abre ninguna conexion.
    pedido: PedidoIdempotente = Depends(pedido_idempotente),
    sesion: Session = Depends(obtener_sesion),
) -> JSONResponse:
    # Con Idempotency-Key, un reintento devuelve lo ya creado (#25).
    return responder_una_vez(
        sesion, pedido, lambda: repositorio_materiales.crear(sesion, datos)
    )


@router.get("", response_model=Pagina[MaterialRespuesta])
def listar_materiales(
    incluir_inactivos: bool = False,
    pagina: ParametrosDePagina = Depends(parametros_de_pagina),
    sesion: Session = Depends(obtener_sesion),
) -> Pagina[MaterialRespuesta]:
    return repositorio_materiales.listar(
        sesion, pagina, incluir_inactivos=incluir_inactivos
    )


@router.get("/{material_id}", response_model=MaterialRespuesta)
def obtener_material(
    material_id: UUID, sesion: Session = Depends(obtener_sesion)
) -> MaterialRespuesta:
    return repositorio_materiales.obtener(sesion, material_id)


@router.patch(
    "/{material_id}", response_model=MaterialRespuesta, dependencies=SOLO_ESCRITURA
)
def actualizar_material(
    material_id: UUID,
    cambios: MaterialActualizar,
    sesion: Session = Depends(obtener_sesion),
) -> MaterialRespuesta:
    return repositorio_materiales.actualizar(sesion, material_id, cambios)


@router.delete(
    "/{material_id}",
    status_code=204,
    response_class=Response,
    response_model=None,
    dependencies=SOLO_ESCRITURA,
)
def desactivar_material(
    material_id: UUID, sesion: Session = Depends(obtener_sesion)
) -> None:
    repositorio_materiales.desactivar(sesion, material_id)


@router.post(
    "/{material_id}/movimientos",
    status_code=201,
    response_model=MovimientoGuardado,
    dependencies=SOLO_ESCRITURA,
)
def registrar_movimiento(
    material_id: UUID,
    datos: MovimientoCrear,
    pedido: PedidoIdempotente = Depends(pedido_idempotente),
    sesion: Session = Depends(obtener_sesion),
) -> JSONResponse:
    return responder_una_vez(
        sesion,
        pedido,
        lambda: repositorio_materiales.registrar_movimiento(sesion, material_id, datos),
    )


@router.get("/{material_id}/movimientos", response_model=Pagina[MovimientoRespuesta])
def listar_movimientos(
    material_id: UUID,
    pagina: ParametrosDePagina = Depends(parametros_de_pagina),
    sesion: Session = Depends(obtener_sesion),
) -> Pagina[MovimientoRespuesta]:
    return repositorio_materiales.listar_movimientos(sesion, material_id, pagina)
