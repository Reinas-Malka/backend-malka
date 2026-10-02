"""Calculo de importes, alicuotas y totales de un documento comercial (#41).

La IA redacta, el codigo calcula: ningun monto sale del modelo. Este modulo
no depende de la base, de FastAPI ni de AWS, asi que se prueba sin nada
levantado.

Reglas de calculo:

- Todo con Decimal, nunca float. Los Decimal se crean desde texto
  (Decimal("0.1")), porque Decimal(0.1) ya arrastra el error del float.
- Se redondea a centavos con ROUND_HALF_UP: 0.525 pasa a 0.53. El default
  de Python es ROUND_HALF_EVEN, que daria 0.52, por eso el modo se fija
  siempre de forma explicita y solo en redondear().
- El neto de cada linea se redondea a centavos.
- El IVA se calcula una vez por alicuota, sobre la suma de los netos de esa
  alicuota, como en el cuadro de IVA de una factura. No se suma el IVA
  redondeado de cada linea.
- En moneda extranjera, el total en pesos es el total por el tipo de cambio,
  redondeado a centavos. De donde sale la cotizacion lo decide quien llama.
- Todo importe devuelto tiene exactamente dos decimales.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

CENTAVOS = Decimal("0.01")
REDONDEO = ROUND_HALF_UP
MONEDA_LOCAL = "ARS"


class ImporteInvalido(ValueError):
    """Los datos no alcanzan para calcular un documento valido.

    Es un error de dominio: el endpoint que use este modulo lo traduce a
    ValidacionError (422).
    """


class Alicuota(Enum):
    """Alicuotas de IVA admitidas. Cualquier otra se rechaza.

    Exento se trata como IVA_0 hasta confirmar si el documento tiene que
    distinguirlos: dos miembros con el mismo valor serian el mismo miembro.
    """

    IVA_0 = Decimal("0")
    IVA_2_5 = Decimal("0.025")
    IVA_5 = Decimal("0.05")
    IVA_10_5 = Decimal("0.105")
    IVA_21 = Decimal("0.21")
    IVA_27 = Decimal("0.27")


@dataclass(frozen=True)
class Linea:
    """Un item del documento."""

    cantidad: int
    precio_unitario: Decimal
    alicuota: Alicuota
    descripcion: str = ""


@dataclass(frozen=True)
class SubtotalAlicuota:
    """Una fila del cuadro de IVA: neto e IVA de una alicuota."""

    alicuota: Alicuota
    neto: Decimal
    iva: Decimal


@dataclass(frozen=True)
class Importes:
    """Resultado del calculo, listo para congelar en el documento."""

    lineas_neto: tuple[Decimal, ...]
    por_alicuota: tuple[SubtotalAlicuota, ...]
    neto: Decimal
    iva: Decimal
    total: Decimal
    moneda: str
    tipo_cambio: Decimal | None
    total_en_pesos: Decimal


def redondear(valor: Decimal) -> Decimal:
    """Redondea un valor a dos decimales con ROUND_HALF_UP."""
    return valor.quantize(CENTAVOS, rounding=REDONDEO)


def calcular_importes(lineas: Sequence[Linea]) -> Importes:
    """Calcula el neto de cada linea, el IVA por alicuota y el total en pesos.

    El neto de cada linea se redondea a centavos y el IVA se calcula una sola
    vez por alicuota, sobre la suma de sus netos (ver el docstring del modulo).
    """
    lineas_neto: list[Decimal] = []
    neto_por_alicuota: dict[Alicuota, Decimal] = {}
    for linea in lineas:
        neto_linea = redondear(linea.precio_unitario * linea.cantidad)
        lineas_neto.append(neto_linea)
        acumulado = neto_por_alicuota.get(linea.alicuota, Decimal("0"))
        neto_por_alicuota[linea.alicuota] = acumulado + neto_linea

    # De menor a mayor alicuota, como el cuadro de IVA de la factura.
    por_alicuota = tuple(
        SubtotalAlicuota(
            alicuota=alicuota,
            neto=neto_alicuota,
            iva=redondear(neto_alicuota * alicuota.value),
        )
        for alicuota, neto_alicuota in sorted(
            neto_por_alicuota.items(), key=lambda par: par[0].value
        )
    )

    neto = redondear(sum((s.neto for s in por_alicuota), Decimal("0")))
    iva = redondear(sum((s.iva for s in por_alicuota), Decimal("0")))
    total = neto + iva
    return Importes(
        lineas_neto=tuple(lineas_neto),
        por_alicuota=por_alicuota,
        neto=neto,
        iva=iva,
        total=total,
        moneda=MONEDA_LOCAL,
        tipo_cambio=None,
        total_en_pesos=total,
    )
