"""Endpoints de bancos (dominio del 16/10, migracion 0009).

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
    BancoActualizar,
    BancoCrear,
    BancoRespuesta,
)
from app.services.produccion import repositorio_bancos

router = APIRouter(prefix="/api/v1/bancos", tags=["bancos"])

# Corre antes que obtener_sesion: un rol sin permiso no abre ninguna conexion.
SOLO_ESCRITURA = [Depends(require_role("admin", "produccion"))]


@router.post(
    "", status_code=201, response_model=BancoRespuesta, dependencies=SOLO_ESCRITURA
)
def crear_banco(
    datos: BancoCrear,
    # Antes que la sesion: una clave mal formada no abre ninguna conexion.
    pedido: PedidoIdempotente = Depends(pedido_idempotente),
    sesion: Session = Depends(obtener_sesion),
) -> JSONResponse:
    # Con Idempotency-Key, un reintento devuelve lo ya creado (#25).
    return responder_una_vez(
        sesion, pedido, lambda: repositorio_bancos.crear(sesion, datos)
    )


@router.get("", response_model=Pagina[BancoRespuesta])
def listar_bancos(
    incluir_inactivos: bool = False,
    pagina: ParametrosDePagina = Depends(parametros_de_pagina),
    sesion: Session = Depends(obtener_sesion),
) -> Pagina[BancoRespuesta]:
    return repositorio_bancos.listar(
        sesion, pagina, incluir_inactivos=incluir_inactivos
    )


@router.get("/{banco_id}", response_model=BancoRespuesta)
def obtener_banco(
    banco_id: UUID, sesion: Session = Depends(obtener_sesion)
) -> BancoRespuesta:
    return repositorio_bancos.obtener(sesion, banco_id)


@router.patch("/{banco_id}", response_model=BancoRespuesta, dependencies=SOLO_ESCRITURA)
def actualizar_banco(
    banco_id: UUID,
    cambios: BancoActualizar,
    sesion: Session = Depends(obtener_sesion),
) -> BancoRespuesta:
    return repositorio_bancos.actualizar(sesion, banco_id, cambios)


@router.delete(
    "/{banco_id}",
    status_code=204,
    response_class=Response,
    response_model=None,
    dependencies=SOLO_ESCRITURA,
)
def desactivar_banco(banco_id: UUID, sesion: Session = Depends(obtener_sesion)) -> None:
    repositorio_bancos.desactivar(sesion, banco_id)
