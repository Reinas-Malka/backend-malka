"""Contrato de la API de embarques (#109): que JSON entra y cual sale.

Aca solo se valida el formato. Lo que necesita la base (que el embarque
exista, que no este cancelado, que no repita un tipo de documento) lo
aplica el repositorio. Ningun schema lleva tenant_id: lo completa
PostgreSQL (ADR 0009) y extra="forbid" lo rechaza si llega.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.schemas.produccion import Identificacion
from app.services.exportacion.embarques import (
    EstadoEmbarque,
    TipoDocumentoEmbarque,
    normalizar_awb,
)

# Estados en los que el envio ya salio: necesitan guia aerea. Igual que el
# CHECK ck_embarque_awb_al_despachar de la migracion 0010.
ESTADOS_DESPACHADOS = {EstadoEmbarque.EN_TRANSITO, EstadoEmbarque.ENTREGADO}


def _no_argentina(valor: str) -> str:
    if valor == "AR":
        raise ValueError("Un embarque de exportacion no puede tener destino AR.")
    return valor


# ISO 3166-1 alfa-2, como pedido.destino_pais.
DestinoPais = Annotated[
    str, Field(pattern=r"^[A-Z]{2}$"), AfterValidator(_no_argentina)
]
Awb = Annotated[str, AfterValidator(normalizar_awb)]

# La key la arma el endpoint de subida (#102): documentos/<tenant_id>/...
# Que el tenant sea el de la sesion lo garantiza el CHECK
# ck_documento_embarque_archivo_del_tenant; aca solo se valida la forma.
ArchivoS3 = Annotated[
    str,
    Field(
        pattern=r"^documentos/[0-9a-f-]{36}/[A-Za-z0-9._/-]+$",
        max_length=300,
    ),
]


class EmbarqueCrear(BaseModel):
    """Cuerpo de POST /api/v1/embarques. Arranca siempre en preparacion."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # Directo: pais del cliente. Consolidado: pais del hub.
    destino_pais: DestinoPais
    fecha_vuelo: date
    awb: Awb | None = None

    # mode="before": corre antes del pattern, asi "nz" llega como "NZ".
    @field_validator("destino_pais", mode="before")
    @classmethod
    def en_mayusculas(cls, valor: object) -> object:
        return valor.strip().upper() if isinstance(valor, str) else valor


class EmbarqueGuardado(EmbarqueCrear):
    """Un embarque completo con su estado: valida el resultado de un PATCH."""

    estado: EstadoEmbarque

    @model_validator(mode="after")
    def awb_si_ya_salio(self) -> Self:
        if self.awb is None and self.estado in ESTADOS_DESPACHADOS:
            raise ValueError(
                "Un embarque en transito o entregado necesita la guia aerea."
            )
        return self


class EmbarqueActualizar(BaseModel):
    """Cuerpo de PATCH: solo los campos que cambian. awb: null la borra."""

    model_config = ConfigDict(extra="forbid")

    destino_pais: str | None = None
    fecha_vuelo: date | None = None
    awb: str | None = None
    estado: EstadoEmbarque | None = None


class EmbarqueRespuesta(BaseModel):
    id: UUID
    destino_pais: str
    fecha_vuelo: date
    awb: str | None
    estado: EstadoEmbarque
    # Cuantos de los 6 tipos ya estan cargados: alcanza para un "4/6".
    documentos_cargados: int


class DocumentoCrear(BaseModel):
    """Cuerpo de POST /api/v1/embarques/{id}/documentos."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tipo: TipoDocumentoEmbarque
    # Transcripto del documento real: no se calcula ni se genera.
    numero: Identificacion
    archivo_s3_key: ArchivoS3
    # Fecha de emision del documento.
    fecha: date

    @field_validator("archivo_s3_key")
    @classmethod
    def sin_subir_de_carpeta(cls, valor: str) -> str:
        if ".." in valor:
            raise ValueError("La key del archivo no puede tener '..'.")
        return valor


class DocumentoRespuesta(BaseModel):
    id: UUID
    embarque_id: UUID
    tipo: TipoDocumentoEmbarque
    numero: str
    archivo_s3_key: str
    fecha: date


class CompletitudRespuesta(BaseModel):
    """Respuesta de GET /api/v1/embarques/{id}/completitud."""

    embarque_id: UUID
    completo: bool
    presentes: list[TipoDocumentoEmbarque]
    faltantes: list[TipoDocumentoEmbarque]
