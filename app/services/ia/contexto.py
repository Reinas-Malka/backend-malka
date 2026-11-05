"""El contexto del pedido que ve el modelo al redactar el borrador (#40).

Lo arma el sistema con datos de la base: el modelo no descubre nada. Cada
renglon dice lo que el documento necesita (partes, objeto, condiciones de
entrega) y deja explicito que lo que se vende son REINAS: el nombre del item
("Buckfast", "Reina fecundada") es como el criadero llama al producto, y sin
esa aclaracion el modelo llego a escribir "30 nucleos de abejas".

Lo que NO va, a proposito:

- Los precios y los importes. Los calcula el sistema y la pantalla los
  muestra aparte. Sin ellos en el contexto, un precio que el modelo copie o
  invente es un numero que no esta en los datos y el validador lo rechaza
  (borrador.py): la IA no puede escribir montos ni sin la moneda al lado.

El nombre del criadero sale de `nombre_del_criadero()` (migracion 0017):
el rol de la API no lee la tabla tenant, que guarda el CUIT. Si no hay
nombre, el contexto lo dice y el prompt pide no inventarlo.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

DOCUMENTO_NACIONAL = "Nota de venta"
DOCUMENTO_EXPORTACION = "Proforma de exportación"

SIN_NOMBRE_DE_CRIADERO = (
    "el criadero de reinas que emite el documento "
    "(su nombre no figura en los datos: no lo inventes)"
)


@dataclass(frozen=True)
class LineaParaBorrador:
    """Una linea del pedido: cuantas reinas, de que raza, con que item."""

    cantidad: int
    item: str
    raza: str


@dataclass(frozen=True)
class PedidoParaBorrador:
    """Lo que el documento necesita del pedido y del cliente."""

    cliente: str
    pais_cliente: str
    moneda: str
    fecha_entrega: date
    destino_pais: str | None
    incoterm: str | None
    lineas: Sequence[LineaParaBorrador]
    # None mientras el rol de la API no pueda leer tenant (ver arriba).
    criadero: str | None = None

    @property
    def es_exportacion(self) -> bool:
        # Exportacion si y solo si tiene pais de destino (docs/dominio.md).
        return self.destino_pais is not None


MESES = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)


def _fecha(dia: date) -> str:
    """En palabras y en ISO: el modelo escribe "4 de diciembre" y el
    validador solo acepta numeros que esten en los datos ("04" no es "4")."""
    return f"{dia.day} de {MESES[dia.month - 1]} de {dia.year} ({dia.isoformat()})"


def _reinas(cantidad: int) -> str:
    return "reina" if cantidad == 1 else "reinas"


def _tipo_de_venta(pedido: PedidoParaBorrador) -> str:
    if not pedido.es_exportacion:
        return "nacional (dentro de Argentina)"
    incoterm = f"incoterm {pedido.incoterm}" if pedido.incoterm else "sin incoterm"
    return f"exportación a {pedido.destino_pais}, {incoterm}"


def armar_contexto(pedido: PedidoParaBorrador) -> str:
    """El texto que va entre <datos>: un dato por renglon, sin importes."""
    documento = DOCUMENTO_EXPORTACION if pedido.es_exportacion else DOCUMENTO_NACIONAL
    total = sum(linea.cantidad for linea in pedido.lineas)
    renglones = [
        f"Documento: {documento}",
        f"Vendedor: {pedido.criadero or SIN_NOMBRE_DE_CRIADERO}",
        "Lo que se vende: reinas vivas (el criadero vende solo reinas)",
        f"Comprador: {pedido.cliente}, país {pedido.pais_cliente}",
        f"Tipo de venta: {_tipo_de_venta(pedido)}",
        f"Moneda del pedido: {pedido.moneda}",
        f"Fecha de entrega solicitada: {_fecha(pedido.fecha_entrega)}",
        "Líneas del pedido:",
        *(
            f"- {linea.cantidad} {_reinas(linea.cantidad)} {linea.raza}, "
            f"ítem {linea.item}"
            for linea in pedido.lineas
        ),
        f"Cantidad total de reinas: {total}",
        "Importes: los calcula el sistema y se muestran aparte del texto",
    ]
    return "\n".join(renglones)
