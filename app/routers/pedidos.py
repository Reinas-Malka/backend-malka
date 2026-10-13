"""Endpoints de pedidos (#34).

Crear puede solo admin y ventas. La validacion de formato vive en los
schemas y la que necesita la base en el repositorio: el endpoint solo conecta
el request con el repositorio.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db import obtener_sesion
from app.idempotencia import PedidoIdempotente, pedido_idempotente, responder_una_vez
from app.schemas.pedidos import PedidoCrear, PedidoRespuesta
from app.services.ventas import repositorio_pedidos

router = APIRouter(prefix="/api/v1/pedidos", tags=["pedidos"])

# Corre antes que obtener_sesion: un rol sin permiso no abre ninguna conexion.
SOLO_ESCRITURA = [Depends(require_role("admin", "ventas"))]


@router.post(
    "", status_code=201, response_model=PedidoRespuesta, dependencies=SOLO_ESCRITURA
)
def crear_pedido(
    datos: PedidoCrear,
    # "solicitud" y no "pedido" como en clientes: aca pedido es el de reinas.
    # Antes que la sesion: una clave mal formada no abre ninguna conexion.
    solicitud: PedidoIdempotente = Depends(pedido_idempotente),
    sesion: Session = Depends(obtener_sesion),
) -> JSONResponse:
    # Con Idempotency-Key, un reintento devuelve el pedido ya creado (#25).
    return responder_una_vez(
        sesion, solicitud, lambda: repositorio_pedidos.crear(sesion, datos)
    )
