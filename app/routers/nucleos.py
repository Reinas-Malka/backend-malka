"""Endpoints de nucleos (#30) (y capacidad del parque, #29).

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
    NucleoActualizar,
    NucleoCrear,
    NucleoGuardado,
    NucleoRespuesta,
)
from app.services.produccion import repositorio_nucleos

router = APIRouter(prefix="/api/v1/nucleos", tags=["nucleos"])

# Corre antes que obtener_sesion: un rol sin permiso no abre ninguna conexion.
SOLO_ESCRITURA = [Depends(require_role("admin", "produccion"))]


@router.post(
    "", status_code=201, response_model=NucleoGuardado, dependencies=SOLO_ESCRITURA
)
def crear_nucleo(
    datos: NucleoCrear,
    # Antes que la sesion: una clave mal formada no abre ninguna conexion.
    pedido: PedidoIdempotente = Depends(pedido_idempotente),
    sesion: Session = Depends(obtener_sesion),
) -> JSONResponse:
    # Con Idempotency-Key, un reintento devuelve lo ya creado (#25).
    return responder_una_vez(
        sesion, pedido, lambda: repositorio_nucleos.crear(sesion, datos)
    )


@router.get("", response_model=Pagina[NucleoRespuesta])
def listar_nucleos(
    parque_id: UUID | None = None,
    incluir_inactivos: bool = False,
    pagina: ParametrosDePagina = Depends(parametros_de_pagina),
    sesion: Session = Depends(obtener_sesion),
) -> Pagina[NucleoRespuesta]:
    return repositorio_nucleos.listar(
        sesion, pagina, parque_id=parque_id, incluir_inactivos=incluir_inactivos
    )


@router.get("/{nucleo_id}", response_model=NucleoRespuesta)
def obtener_nucleo(
    nucleo_id: UUID, sesion: Session = Depends(obtener_sesion)
) -> NucleoRespuesta:
    return repositorio_nucleos.obtener(sesion, nucleo_id)


@router.patch(
    "/{nucleo_id}", response_model=NucleoGuardado, dependencies=SOLO_ESCRITURA
)
def actualizar_nucleo(
    nucleo_id: UUID,
    cambios: NucleoActualizar,
    sesion: Session = Depends(obtener_sesion),
) -> NucleoGuardado:
    return repositorio_nucleos.actualizar(sesion, nucleo_id, cambios)


@router.delete(
    "/{nucleo_id}",
    status_code=204,
    response_class=Response,
    response_model=None,
    dependencies=SOLO_ESCRITURA,
)
def desactivar_nucleo(
    nucleo_id: UUID, sesion: Session = Depends(obtener_sesion)
) -> None:
    repositorio_nucleos.desactivar(sesion, nucleo_id)
