"""Asistente conversacional del criadero (Bedrock Haiku 4.5 con herramientas).

Lo usa cualquier rol del criadero: los permisos reales los aplican las
herramientas (lectura con RLS) y, al confirmar una propuesta, el endpoint
que la ejecuta con el token del usuario. El servidor no guarda
conversaciones: el front manda el historial en cada pedido.
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter, Depends

from app.auth import ROLES, Identidad, require_role
from app.schemas.asistente import PedidoAsistente, RespuestaAsistente
from app.services.ia.asistente import conversacion
from app.services.ia.cliente import ClienteIA

router = APIRouter(prefix="/api/v1/asistente", tags=["asistente"])


@lru_cache(maxsize=1)
def _cliente_bedrock() -> ClienteIA:
    # Uno por contenedor de Lambda: crear el cliente cuesta.
    return conversacion.crear_cliente_asistente()


def obtener_modelo() -> ClienteIA:
    """El cliente de Bedrock. Los tests lo reemplazan por un modelo falso."""
    return _cliente_bedrock()


@router.post(
    "",
    response_model=RespuestaAsistente,
    responses={
        429: {"description": "Se paso el limite de IA del criadero"},
        503: {"description": "Bedrock no esta disponible"},
    },
)
def conversar_con_asistente(
    datos: PedidoAsistente,
    identidad: Identidad = Depends(require_role(*sorted(ROLES))),
    modelo: ClienteIA = Depends(obtener_modelo),
) -> RespuestaAsistente:
    return conversacion.conversar(datos.mensajes, modelo=modelo, identidad=identidad)
