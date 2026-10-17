"""Contrato de la subida y descarga de documentos (#102).

Coincide con src/types/documentos.ts del frontend: PedidoDeSubida entra y
SubidaPreparada sale. extra="forbid" rechaza cualquier campo de mas.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.schemas.produccion import _sin_caracteres_de_control
from app.services.documentos.almacenamiento import TAMANO_MAXIMO

TipoDeArchivo = Literal["application/pdf", "image/jpeg", "image/png"]


class PedidoDeSubida(BaseModel):
    """Cuerpo de POST /api/v1/documentos/subidas: que se va a subir."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # El nombre con el que se eligio el archivo; en la key va saneado.
    nombre: Annotated[
        str,
        Field(min_length=1, max_length=200),
        AfterValidator(_sin_caracteres_de_control),
    ]
    tipo: TipoDeArchivo
    # Lo declara el navegador: se valida aca y en el CHECK de la 0011.
    tamano: int = Field(gt=0, le=TAMANO_MAXIMO, strict=True)


class SubidaPreparada(BaseModel):
    """URL de PUT para el navegador y la key que despues se registra."""

    url_subida: str
    clave: str
    expira_en_segundos: int


class DescargaPreparada(BaseModel):
    url_descarga: str
    expira_en_segundos: int
