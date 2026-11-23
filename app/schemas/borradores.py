"""Contrato de la revision de borradores (#40, #42): que JSON entra y cual sale.

BorradorRespuesta vive en app/services/ia/generacion.py junto a los estados.
Aca van los cuerpos de los POST y la fila de la bitacora. extra="forbid":
un campo de mas (un tenant_id, un "estado") se rechaza con 422.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from app.schemas.produccion import _sin_caracteres_de_control

# Los mismos topes que el CHECK ck_borrador_descartado de la 0018.
MOTIVO_MINIMO = 3
MOTIVO_MAXIMO = 500

HashSha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Motivo = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=MOTIVO_MINIMO, max_length=MOTIVO_MAXIMO
    ),
    AfterValidator(_sin_caracteres_de_control),
]


class _Entrada(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BorradorPedido(_Entrada):
    """POST /api/v1/documentos/borradores: de que pedido redactar.

    corrige_a: el borrador aprobado del mismo pedido que este corrige. El
    aprobado no se toca (es inmutable); el nuevo lo referencia.
    """

    pedido_id: UUID
    corrige_a: UUID | None = None


class Aprobacion(_Entrada):
    """POST .../aprobar: la version (hash_contenido) que vio quien aprueba y,
    si ya existe, el comprobante que ARCA emitio para el pedido (#108)."""

    hash_contenido: HashSha256
    comprobante_id: UUID | None = None


class Descarte(_Entrada):
    """POST .../rechazar: por que el borrador no sirve."""

    motivo: Motivo


class RevisionRespuesta(BaseModel):
    """Una fila de la bitacora: quien, cuando y sobre que version."""

    id: UUID
    borrador_id: UUID
    accion: Literal["aprobado", "descartado"]
    # El sub de Cognito.
    usuario: str
    momento: datetime
    hash_contenido: str
    # Solo al aprobar: sha256 del snapshot congelado.
    hash_snapshot: str | None
    version_prompt: str | None
    motivo: str | None
    comprobante_id: UUID | None
