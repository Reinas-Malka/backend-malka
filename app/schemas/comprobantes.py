"""Contrato de la API de comprobantes (#108): que JSON entra y cual sale.

Aca se valida el formato y lo que depende de la propia fila (moneda y tipo
de cambio). Lo que necesita la base (que el cliente exista, que la letra sea
coherente con su condicion, que el PDF este subido, que no se repita) lo
aplica el repositorio. tenant_id y cargado_por no se reciben: los completan
PostgreSQL (ADR 0009) y el token, y extra="forbid" los rechaza si llegan.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.fechas import hoy
from app.schemas.embarques import ArchivoS3
from app.services.facturacion.comprobantes import (
    NUMERO_MAXIMO,
    PUNTO_VENTA_MAXIMO,
    TipoComprobante,
)
from app.services.ventas.pedidos import Moneda


class ComprobanteCrear(BaseModel):
    """POST /api/v1/comprobantes: lo transcripto del comprobante de ARCA.

    Ningun campo fiscal tiene default: si falta, no hay comprobante. El
    sistema nunca calcula ni genera la letra, el numero ni el importe.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tipo: TipoComprobante
    cliente_id: UUID
    # strict: dos enteros, nunca el texto impreso "0001" ni "00000045".
    punto_venta: int = Field(strict=True, ge=1, le=PUNTO_VENTA_MAXIMO)
    numero: int = Field(strict=True, ge=1, le=NUMERO_MAXIMO)
    fecha_emision: date
    moneda: Moneda
    # Mismo tamanio que la columna Numeric(14, 2): con un decimal de mas se
    # responde 422 en lugar de dejar que PostgreSQL redondee en silencio.
    importe: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    # Numeric(16, 6): la cotizacion de ARCA trae hasta 6 decimales.
    tipo_cambio: Decimal | None = Field(
        default=None, gt=0, max_digits=16, decimal_places=6
    )
    archivo_s3_key: ArchivoS3

    @model_validator(mode="after")
    def coherente(self) -> Self:
        """Las mismas reglas que los CHECK de la migracion 0014."""
        # hoy() con la hora de Argentina: el servidor esta en UTC.
        if self.fecha_emision > hoy():
            raise ValueError("La fecha de emision no puede ser futura.")
        en_pesos = self.moneda == Moneda.ARS
        if en_pesos and self.tipo_cambio is not None:
            raise ValueError("Un comprobante en pesos no lleva tipo de cambio.")
        if not en_pesos and self.tipo_cambio is None:
            raise ValueError(f"Falta el tipo de cambio de {self.moneda}.")
        if (self.tipo == TipoComprobante.E) == en_pesos:
            raise ValueError("La factura E va en USD o EUR; la A y la B, en pesos.")
        return self


class ComprobanteRespuesta(BaseModel):
    """Lo que la API devuelve de un comprobante, igual a como se transcribio."""

    id: UUID
    tipo: TipoComprobante
    cliente_id: UUID
    punto_venta: int
    numero: int
    fecha_emision: date
    moneda: Moneda
    importe: Decimal
    tipo_cambio: Decimal | None
    archivo_s3_key: str
    # El sub de Cognito de quien lo registro: auditoria, no el emisor fiscal.
    cargado_por: str
    creado_en: datetime
