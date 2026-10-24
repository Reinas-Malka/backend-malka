"""Contrato de POST /api/v1/pedidos/{id}/estado (#36): que JSON entra.

Aca solo se valida el formato: que el destino sea entregado o cancelado, que
las cantidades sean positivas y que no se repitan lineas ni bancos. Si el
pedido puede pasar a ese estado y si el despacho coincide con sus reservas
necesita la base: lo revisa el repositorio con app/services/ventas/
transiciones.py.

reservado no entra por aca: lo hace POST /api/v1/pedidos/{id}/reservas (#35).
La respuesta es PedidoRespuesta, la misma de app/schemas/pedidos.py.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.fechas import hoy
from app.schemas.pedidos import MAX_CANTIDAD, MAX_ITEMS
from app.services.ventas.pedidos import EstadoPedido

# Una linea sale de pocos bancos; el tope evita cuerpos enormes.
MAX_BANCOS_POR_LINEA = 50


class SalidaDeBanco(BaseModel):
    """De que banco salen reinas para una linea, y cuantas."""

    model_config = ConfigDict(extra="forbid")

    # None: reinas enjauladas que no estan en ningun banco. Sin default: para
    # decir "sin banco" hay que mandar null a proposito; olvidarlo es 422.
    banco_id: UUID | None
    cantidad: int = Field(gt=0, le=MAX_CANTIDAD)


class LineaDespacho(BaseModel):
    """Una linea del pedido y de que bancos salen sus reinas."""

    model_config = ConfigDict(extra="forbid")

    pedido_item_id: UUID
    bancos: list[SalidaDeBanco] = Field(min_length=1, max_length=MAX_BANCOS_POR_LINEA)

    @model_validator(mode="after")
    def sin_bancos_repetidos(self) -> Self:
        # El mismo banco dos veces se pisaria al elegir sus celdas. None
        # tambien cuenta: "sin banco" va una sola vez por linea.
        bancos = [salida.banco_id for salida in self.bancos]
        if len(bancos) != len(set(bancos)):
            raise ValueError("Un banco aparece dos veces en la misma linea.")
        return self


class Entregar(BaseModel):
    """Pasa un pedido reservado a entregado, eligiendo los bancos por linea.

    Que esten todas las lineas y con la cantidad reservada lo revisa el
    repositorio con validar_despacho: aca no se conocen las reservas.
    """

    model_config = ConfigDict(extra="forbid")

    estado: Literal[EstadoPedido.ENTREGADO]
    # Dia del despacho; puede ser anterior a hoy (se carga tarde). Si no
    # viene, el repositorio usa hoy().
    fecha: date | None = None
    lineas: list[LineaDespacho] = Field(min_length=1, max_length=MAX_ITEMS)

    @model_validator(mode="after")
    def validar_lineas_y_fecha(self) -> Self:
        items = [linea.pedido_item_id for linea in self.lineas]
        if len(items) != len(set(items)):
            raise ValueError("Una linea del pedido aparece dos veces.")
        # hoy() con la hora de Argentina: el servidor esta en UTC.
        if self.fecha is not None and self.fecha > hoy():
            raise ValueError("La fecha del despacho no puede ser futura.")
        return self


class Cancelar(BaseModel):
    """Pasa un pedido en borrador o reservado a cancelado. Sin mas datos."""

    model_config = ConfigDict(extra="forbid")

    estado: Literal[EstadoPedido.CANCELADO]


# Pydantic mira "estado" y valida el cuerpo solo contra el modelo que
# corresponde: un error de entregar no se mezcla con los de cancelar. Otro
# estado (reservado incluido) es 422.
CambioDeEstado = Annotated[Entregar | Cancelar, Field(discriminator="estado")]
