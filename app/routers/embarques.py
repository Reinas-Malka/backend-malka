"""Endpoints de embarques y sus documentos (#109).

Leer puede cualquier rol del criadero; escribir, solo admin y ventas. La
validacion de formato vive en los schemas y la que necesita la base en el
repositorio: los endpoints solo conectan el request con el repositorio.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db import obtener_sesion
from app.idempotencia import PedidoIdempotente, pedido_idempotente, responder_una_vez
from app.paginacion import Pagina, ParametrosDePagina, parametros_de_pagina
from app.schemas.embarques import (
    CompletitudRespuesta,
    DocumentoCrear,
    DocumentoRespuesta,
    EmbarqueActualizar,
    EmbarqueCrear,
    EmbarqueRespuesta,
)
from app.services.exportacion import repositorio_embarques
from app.services.exportacion.embarques import EstadoEmbarque

router = APIRouter(prefix="/api/v1/embarques", tags=["embarques"])

# Corre antes que obtener_sesion: un rol sin permiso no abre ninguna conexion.
SOLO_ESCRITURA = [Depends(require_role("admin", "ventas"))]


@router.post(
    "", status_code=201, response_model=EmbarqueRespuesta, dependencies=SOLO_ESCRITURA
)
def crear_embarque(
    datos: EmbarqueCrear,
    # Antes que la sesion: una clave mal formada no abre ninguna conexion.
    pedido: PedidoIdempotente = Depends(pedido_idempotente),
    sesion: Session = Depends(obtener_sesion),
) -> JSONResponse:
    # Con Idempotency-Key, un reintento devuelve lo ya creado (#25).
    return responder_una_vez(
        sesion, pedido, lambda: repositorio_embarques.crear(sesion, datos)
    )


@router.get("", response_model=Pagina[EmbarqueRespuesta])
def listar_embarques(
    estado: EstadoEmbarque | None = None,
    pagina: ParametrosDePagina = Depends(parametros_de_pagina),
    sesion: Session = Depends(obtener_sesion),
) -> Pagina[EmbarqueRespuesta]:
    return repositorio_embarques.listar(sesion, pagina, estado=estado)


@router.get("/{embarque_id}", response_model=EmbarqueRespuesta)
def obtener_embarque(
    embarque_id: UUID, sesion: Session = Depends(obtener_sesion)
) -> EmbarqueRespuesta:
    return repositorio_embarques.obtener(sesion, embarque_id)


@router.patch(
    "/{embarque_id}", response_model=EmbarqueRespuesta, dependencies=SOLO_ESCRITURA
)
def actualizar_embarque(
    embarque_id: UUID,
    cambios: EmbarqueActualizar,
    sesion: Session = Depends(obtener_sesion),
) -> EmbarqueRespuesta:
    return repositorio_embarques.actualizar(sesion, embarque_id, cambios)


@router.post(
    "/{embarque_id}/documentos",
    status_code=201,
    response_model=DocumentoRespuesta,
    dependencies=SOLO_ESCRITURA,
)
def agregar_documento(
    embarque_id: UUID,
    datos: DocumentoCrear,
    pedido: PedidoIdempotente = Depends(pedido_idempotente),
    sesion: Session = Depends(obtener_sesion),
) -> JSONResponse:
    return responder_una_vez(
        sesion,
        pedido,
        lambda: repositorio_embarques.agregar_documento(sesion, embarque_id, datos),
    )


@router.get("/{embarque_id}/documentos", response_model=Pagina[DocumentoRespuesta])
def listar_documentos(
    embarque_id: UUID, sesion: Session = Depends(obtener_sesion)
) -> Pagina[DocumentoRespuesta]:
    return repositorio_embarques.listar_documentos(sesion, embarque_id)


# La pregunta de la demo del 30/11: "tengo los 6 documentos de este envio?"
@router.get("/{embarque_id}/completitud", response_model=CompletitudRespuesta)
def completitud_del_embarque(
    embarque_id: UUID, sesion: Session = Depends(obtener_sesion)
) -> CompletitudRespuesta:
    return repositorio_embarques.completitud_de(sesion, embarque_id)
