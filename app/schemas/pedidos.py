"""Contrato de la API de pedidos (#34): que JSON entra y cual sale.

Aca solo se valida el formato. La coherencia con el tipo de cliente (moneda,
destino_pais, incoterm) necesita leer el cliente de la base: la aplica el
repositorio con validar_pedido de app/services/ventas/pedidos.py.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.documentos.importes import Alicuota
from app.services.ventas.pedidos import EstadoPedido, Incoterm, Moneda

# Un pedido real tiene pocas lineas; el tope evita cuerpos enormes.
MAX_ITEMS = 100
# La columna pedido_item.cantidad es Integer (4 bytes): por encima de esto
# PostgreSQL rechaza el INSERT y la API responderia 500 en lugar de 422.
MAX_CANTIDAD = 2_147_483_647


class PedidoItemCrear(BaseModel):
    """Una linea del pedido: que item, de que raza, cuantas y a que precio.

    La alicuota no se recibe: el repositorio la copia del item vendible.
    """

    model_config = ConfigDict(extra="forbid")

    item_vendible_id: UUID
    raza_id: UUID
    cantidad: int = Field(gt=0, le=MAX_CANTIDAD)
    # Mismo tamanio que la columna Numeric(14, 2): con un decimal de mas se
    # responde 422 en lugar de dejar que PostgreSQL redondee en silencio.
    precio_unitario: Decimal = Field(ge=0, max_digits=14, decimal_places=2)


class PedidoCrear(BaseModel):
    """Cuerpo de POST /api/v1/pedidos.

    Sin estado: el alta crea siempre en borrador y las transiciones son de
    #36. Sin tenant_id: lo completa PostgreSQL (ADR 0009). extra="forbid"
    rechaza cualquiera de los dos si llegan.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    cliente_id: UUID
    moneda: Moneda
    # Pais donde recibe el cliente final. ISO 3166-1 alfa-2, en mayusculas.
    destino_pais: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    incoterm: Incoterm | None = None
    fecha_entrega_solicitada: date
    items: list[PedidoItemCrear] = Field(min_length=1, max_length=MAX_ITEMS)

    # mode="before": corre antes del pattern y del enum, asi "de" llega como
    # "DE" y "cpt" como "CPT". Tambien corre antes de str_strip_whitespace,
    # por eso recorta aca: " eur" tiene que llegar como "EUR".
    @field_validator("destino_pais", "incoterm", "moneda", mode="before")
    @classmethod
    def en_mayusculas(cls, valor: object) -> object:
        return valor.strip().upper() if isinstance(valor, str) else valor


class PedidoItemRespuesta(BaseModel):
    """Lo que la API devuelve de una linea, con la alicuota que se copio."""

    id: UUID
    # Orden de la linea en el pedido, empezando en 1.
    numero_linea: int
    item_vendible_id: UUID
    raza_id: UUID
    cantidad: int
    precio_unitario: Decimal
    alicuota: Alicuota


class PedidoRespuesta(BaseModel):
    """Lo que la API devuelve de un pedido, con sus lineas."""

    id: UUID
    cliente_id: UUID
    estado: EstadoPedido
    moneda: Moneda
    destino_pais: str | None
    incoterm: Incoterm | None
    fecha_entrega_solicitada: date
    items: list[PedidoItemRespuesta]
