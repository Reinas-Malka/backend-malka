"""Borradores de documentos con IA (#40).

Pedir pueden admin y ventas; leer, cualquier rol del criadero. El pedido
responde 202: el borrador se genera en el worker y se consulta despues
(GET por id) hasta que sale de `generando`.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.auth import Identidad, require_role
from app.db import obtener_sesion
from app.idempotencia import PedidoIdempotente, pedido_idempotente, responder_una_vez
from app.paginacion import Pagina, ParametrosDePagina, parametros_de_pagina
from app.services.ia import generacion
from app.services.ia.generacion import BorradorRespuesta, Cola, ColaSqs

router = APIRouter(prefix="/api/v1/documentos/borradores", tags=["borradores"])


class BorradorPedido(BaseModel):
    """POST /api/v1/documentos/borradores: de que pedido redactar."""

    model_config = ConfigDict(extra="forbid")

    pedido_id: UUID


def obtener_cola() -> Cola:
    """La cola SQS de documentos. Los tests la reemplazan."""
    return ColaSqs()


@router.post("", status_code=202, response_model=BorradorRespuesta)
def pedir_borrador(
    datos: BorradorPedido,
    tareas: BackgroundTasks,
    # Antes que la sesion: un rol sin permiso no abre ninguna conexion.
    identidad: Identidad = Depends(require_role("admin", "ventas")),
    pedido: PedidoIdempotente = Depends(pedido_idempotente),
    sesion: Session = Depends(obtener_sesion),
    cola: Cola = Depends(obtener_cola),
) -> JSONResponse:
    def crear() -> BorradorRespuesta:
        borrador = generacion.pedir(sesion, datos.pedido_id, identidad.user_id)
        # Despues del commit: el worker tiene que encontrar el borrador. Solo
        # la primera vez; un reintento con la misma clave no vuelve a encolar.
        tareas.add_task(generacion.encolar, cola, identidad.tenant_id, borrador.id)
        return borrador

    return responder_una_vez(sesion, pedido, crear, status_code=202)


@router.get("", response_model=Pagina[BorradorRespuesta])
def listar_borradores(
    pedido_id: UUID | None = None,
    pagina: ParametrosDePagina = Depends(parametros_de_pagina),
    sesion: Session = Depends(obtener_sesion),
) -> Pagina[BorradorRespuesta]:
    return generacion.listar(sesion, pagina, pedido_id=pedido_id)


@router.get("/{borrador_id}", response_model=BorradorRespuesta)
def obtener_borrador(
    borrador_id: UUID, sesion: Session = Depends(obtener_sesion)
) -> BorradorRespuesta:
    return generacion.obtener(sesion, borrador_id)
