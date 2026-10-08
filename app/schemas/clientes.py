"""Contrato de la API de clientes (#32): que JSON entra y cual sale.

Las reglas fiscales viven en app/services/ventas/clientes.py; aca solo se
aplican. Un ValueError en un validador termina en un 422 con la forma unica
de app/errores.py, y su mensaje lo lee una persona: nunca incluye el CUIT.
"""

from __future__ import annotations

import unicodedata
from typing import Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from app.services.ventas.clientes import (
    CondicionIva,
    TipoCliente,
    TipoDocumento,
    es_cuit_valido,
    normalizar_cuit,
    tipo_documento_por_defecto,
)

PAIS_LOCAL = "AR"


class ClienteCrear(BaseModel):
    """Cuerpo de POST /api/v1/clientes.

    No lleva tenant_id: lo completa PostgreSQL con el tenant de la sesion
    (ADR 0009). extra="forbid" rechaza cualquier campo de mas, incluido ese.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    nombre: str = Field(min_length=1, max_length=200)
    # Codigo ISO 3166-1 alfa-2, en mayusculas.
    pais: str = Field(pattern=r"^[A-Z]{2}$")
    tipo: TipoCliente
    condicion_iva: CondicionIva
    cuit_o_tax_id: str = Field(min_length=1, max_length=50)

    # mode="before": corre antes del pattern, asi "ar" llega como "AR" y se
    # guarda en mayusculas.
    @field_validator("pais", mode="before")
    @classmethod
    def pais_en_mayusculas(cls, valor: object) -> object:
        return valor.upper() if isinstance(valor, str) else valor

    @field_validator("nombre", "cuit_o_tax_id")
    @classmethod
    def sin_caracteres_de_control(cls, valor: str) -> str:
        if any(unicodedata.category(caracter) == "Cc" for caracter in valor):
            raise ValueError("El texto no puede tener caracteres de control.")
        return valor

    @model_validator(mode="after")
    def validar_segun_tipo(self) -> Self:
        if self.tipo == TipoCliente.NACIONAL:
            if self.pais != PAIS_LOCAL:
                raise ValueError("Un cliente nacional tiene que ser de AR.")
            if self.condicion_iva == CondicionIva.CLIENTE_EXTERIOR:
                raise ValueError(
                    "Un cliente nacional no puede tener condicion cliente_exterior."
                )
            if not es_cuit_valido(self.cuit_o_tax_id):
                raise ValueError("El CUIT no es valido.")
            # Se guarda siempre sin guiones.
            self.cuit_o_tax_id = normalizar_cuit(self.cuit_o_tax_id)
        else:
            if self.pais == PAIS_LOCAL:
                raise ValueError("Un cliente de exportacion no puede ser de AR.")
            if self.condicion_iva != CondicionIva.CLIENTE_EXTERIOR:
                raise ValueError(
                    "Un cliente de exportacion tiene que tener condicion "
                    "cliente_exterior."
                )
            # El tax id del exterior es libre: no hay un formato comun.
        return self


class ClienteActualizar(BaseModel):
    """Cuerpo de PATCH /api/v1/clientes/{id}: solo los campos que cambian.

    Las reglas no se repiten aca: el repositorio aplica los cambios sobre el
    cliente guardado y valida el resultado completo con ClienteCrear.
    """

    model_config = ConfigDict(extra="forbid")

    nombre: str | None = None
    pais: str | None = None
    tipo: TipoCliente | None = None
    condicion_iva: CondicionIva | None = None
    cuit_o_tax_id: str | None = None
    # true reactiva un cliente desactivado con DELETE.
    activo: bool | None = None


class ClienteRespuesta(BaseModel):
    """Lo que la API devuelve de un cliente."""

    id: UUID
    nombre: str
    pais: str
    tipo: TipoCliente
    condicion_iva: CondicionIva
    cuit_o_tax_id: str
    activo: bool

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tipo_documento_por_defecto(self) -> TipoDocumento:
        return tipo_documento_por_defecto(self.tipo, self.condicion_iva)
