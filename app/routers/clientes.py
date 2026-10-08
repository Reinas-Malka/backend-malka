"""Endpoints de clientes (#32).

Leer puede cualquier rol del criadero; escribir, solo admin y ventas. Los
permisos se declaran en cada ruta y la validacion vive en los schemas: los
endpoints solo conectan el request con el repositorio.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db import obtener_sesion
from app.schemas.clientes import ClienteActualizar, ClienteCrear, ClienteRespuesta
from app.services.ventas import repositorio_clientes

router = APIRouter(prefix="/api/v1/clientes", tags=["clientes"])

# Corre antes que obtener_sesion: un rol sin permiso no abre ninguna conexion.
SOLO_ESCRITURA = [Depends(require_role("admin", "ventas"))]


@router.post(
    "", status_code=201, response_model=ClienteRespuesta, dependencies=SOLO_ESCRITURA
)
def crear_cliente(
    datos: ClienteCrear, sesion: Session = Depends(obtener_sesion)
) -> ClienteRespuesta:
    return repositorio_clientes.crear(sesion, datos)


@router.get("", response_model=list[ClienteRespuesta])
def listar_clientes(
    incluir_inactivos: bool = False, sesion: Session = Depends(obtener_sesion)
) -> list[ClienteRespuesta]:
    return repositorio_clientes.listar(sesion, incluir_inactivos=incluir_inactivos)


@router.get("/{cliente_id}", response_model=ClienteRespuesta)
def obtener_cliente(
    cliente_id: UUID, sesion: Session = Depends(obtener_sesion)
) -> ClienteRespuesta:
    return repositorio_clientes.obtener(sesion, cliente_id)


@router.patch(
    "/{cliente_id}", response_model=ClienteRespuesta, dependencies=SOLO_ESCRITURA
)
def actualizar_cliente(
    cliente_id: UUID,
    cambios: ClienteActualizar,
    sesion: Session = Depends(obtener_sesion),
) -> ClienteRespuesta:
    return repositorio_clientes.actualizar(sesion, cliente_id, cambios)


@router.delete(
    "/{cliente_id}",
    status_code=204,
    response_class=Response,
    response_model=None,
    dependencies=SOLO_ESCRITURA,
)
def desactivar_cliente(
    cliente_id: UUID, sesion: Session = Depends(obtener_sesion)
) -> None:
    repositorio_clientes.desactivar(sesion, cliente_id)
