"""Contrato del asistente conversacional (POST /api/v1/asistente).

El servidor no guarda conversaciones: el front manda el historial en cada
pedido y recibe la respuesta del asistente, lo que se consulto y las
propuestas de escritura. Una propuesta NO se ejecuto: el front la confirma
llamando al endpoint REST que indica, con el token del usuario, asi pasa por
las mismas validaciones, roles e idempotencia que cualquier otra escritura.
"""

from __future__ import annotations

import unicodedata
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

# Topes del historial: acotan los tokens de cada llamada (y su costo).
MAX_MENSAJES = 20
MAX_CARACTERES_POR_MENSAJE = 2000


def _sin_caracteres_de_control(valor: str) -> str:
    """Saltos de linea y tabulaciones si; un NUL u otro control, no."""
    permitidos = {"\n", "\r", "\t"}
    if any(unicodedata.category(c) == "Cc" and c not in permitidos for c in valor):
        raise ValueError("El texto no puede tener caracteres de control.")
    return valor


TextoDeMensaje = Annotated[
    str,
    Field(min_length=1, max_length=MAX_CARACTERES_POR_MENSAJE),
    AfterValidator(_sin_caracteres_de_control),
]


class RolMensaje(StrEnum):
    USUARIO = "usuario"
    ASISTENTE = "asistente"


class MensajeAsistente(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rol: RolMensaje
    texto: TextoDeMensaje


class PedidoAsistente(BaseModel):
    """Cuerpo de POST /api/v1/asistente: el historial, el ultimo es del usuario."""

    model_config = ConfigDict(extra="forbid")

    mensajes: list[MensajeAsistente] = Field(min_length=1, max_length=MAX_MENSAJES)


class Consulta(BaseModel):
    """Una herramienta de lectura que uso el asistente ("Consulté disponibilidad")."""

    tool: str
    resumen: str


class AccionPropuesta(StrEnum):
    CREAR_CLIENTE = "crear_cliente"
    CREAR_PEDIDO = "crear_pedido"
    RESERVAR_PEDIDO = "reservar_pedido"
    CREAR_TANDA = "crear_tanda"
    OPERAR_TANDA = "operar_tanda"
    PEDIR_BORRADOR = "pedir_borrador"


class ParResumen(BaseModel):
    """Un renglon del resumen que el front muestra antes de confirmar."""

    etiqueta: str
    valor: str


class Propuesta(BaseModel):
    """Una escritura armada y validada por el servidor, todavia sin ejecutar.

    - id: uuid nuevo; el front lo usa como Idempotency-Key al confirmar.
    - metodo y ruta: el endpoint REST existente que la ejecuta.
    - cuerpo: el JSON exacto de ese endpoint, ya validado contra su schema;
      null cuando el endpoint no lleva cuerpo (reservar un pedido).
    - resumen: armado por el servidor desde el cuerpo y los nombres
      resueltos, nunca texto del modelo.
    - roles_permitidos: los del endpoint, para deshabilitar el boton si el
      usuario no puede confirmarla.
    """

    id: UUID
    accion: AccionPropuesta
    metodo: Literal["POST"] = "POST"
    ruta: str
    cuerpo: dict[str, Any] | None
    resumen: list[ParResumen]
    roles_permitidos: list[str]


class RespuestaAsistente(BaseModel):
    texto: str
    consultas: list[Consulta] = []
    propuestas: list[Propuesta] = []
