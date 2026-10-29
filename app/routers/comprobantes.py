"""Endpoints de comprobantes transcriptos: Factura A, B y E (#108).

Registrar pueden admin y ventas (las dos personas que facturan); leer,
cualquier rol del criadero. No hay PATCH ni DELETE: lo transcripto es
inmutable (dominio, "Facturacion").
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth import Identidad, require_role
from app.db import obtener_sesion
from app.idempotencia import PedidoIdempotente, pedido_idempotente, responder_una_vez
from app.paginacion import Pagina, ParametrosDePagina, parametros_de_pagina
from app.schemas.comprobantes import ComprobanteCrear, ComprobanteRespuesta
from app.services.facturacion import repositorio_comprobantes

router = APIRouter(prefix="/api/v1/comprobantes", tags=["comprobantes"])


@router.post("", status_code=201, response_model=ComprobanteRespuesta)
def registrar_comprobante(
    datos: ComprobanteCrear,
    # Antes que la sesion: un rol sin permiso no abre ninguna conexion.
    identidad: Identidad = Depends(require_role("admin", "ventas")),
    pedido: PedidoIdempotente = Depends(pedido_idempotente),
    sesion: Session = Depends(obtener_sesion),
) -> JSONResponse:
    # Con Idempotency-Key, un reintento devuelve lo ya registrado (#25).
    return responder_una_vez(
        sesion,
        pedido,
        lambda: repositorio_comprobantes.crear(sesion, datos, identidad.user_id),
    )


@router.get("", response_model=Pagina[ComprobanteRespuesta])
def listar_comprobantes(
    cliente_id: UUID | None = None,
    pagina: ParametrosDePagina = Depends(parametros_de_pagina),
    sesion: Session = Depends(obtener_sesion),
) -> Pagina[ComprobanteRespuesta]:
    return repositorio_comprobantes.listar(sesion, pagina, cliente_id=cliente_id)


@router.get("/{comprobante_id}", response_model=ComprobanteRespuesta)
def obtener_comprobante(
    comprobante_id: UUID, sesion: Session = Depends(obtener_sesion)
) -> ComprobanteRespuesta:
    return repositorio_comprobantes.obtener(sesion, comprobante_id)
