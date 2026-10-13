"""Endpoints de pedidos (#34).

Leer puede cualquier rol del criadero; crear, solo admin y ventas. La
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
from app.schemas.pedidos import PedidoCrear, PedidoRespuesta
from app.services.ventas import repositorio_pedidos
from app.services.ventas.pedidos import EstadoPedido

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


# Los parametros que no estan en la ruta llegan por query string
# (?estado=borrador&limite=20&cursor=...). ?limite= y ?cursor= los valida
# parametros_de_pagina (app/paginacion.py), igual que en los demas listados.
@router.get("", response_model=Pagina[PedidoRespuesta])
def listar_pedidos(
    estado: EstadoPedido | None = None,
    cliente_id: UUID | None = None,
    pagina: ParametrosDePagina = Depends(parametros_de_pagina),
    sesion: Session = Depends(obtener_sesion),
) -> Pagina[PedidoRespuesta]:
    return repositorio_pedidos.listar(
        sesion, pagina, estado=estado, cliente_id=cliente_id
    )


# pedido_id es UUID: /api/v1/pedidos/abc responde 422 sin tocar la base.
@router.get("/{pedido_id}", response_model=PedidoRespuesta)
def obtener_pedido(
    pedido_id: UUID, sesion: Session = Depends(obtener_sesion)
) -> PedidoRespuesta:
    return repositorio_pedidos.obtener(sesion, pedido_id)
