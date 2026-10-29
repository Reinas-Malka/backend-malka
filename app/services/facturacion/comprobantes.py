"""Reglas de los comprobantes transcriptos (#108), sin base ni FastAPI.

docs/dominio.md ("Facturacion"): el sistema no emite. La letra la asigna
ARCA y se transcribe; la condicion del cliente solo sirve para validar que
la letra cargada sea coherente.
"""

from __future__ import annotations

from enum import StrEnum

from app.services.ventas.clientes import (
    CondicionIva,
    TipoCliente,
    TipoDocumento,
    tipo_documento_por_defecto,
)


class TipoComprobante(StrEnum):
    """La letra del comprobante, tal como la asigno ARCA."""

    A = "A"
    B = "B"
    E = "E"


# Topes de ARCA: punto de venta de hasta 5 digitos y numero de hasta 8.
# Iguales a los CHECK de la migracion 0014.
PUNTO_VENTA_MAXIMO = 99_999
NUMERO_MAXIMO = 99_999_999

_LETRA_DEL_DOCUMENTO = {
    TipoDocumento.FACTURA_A: TipoComprobante.A,
    TipoDocumento.FACTURA_B: TipoComprobante.B,
    TipoDocumento.FACTURA_E: TipoComprobante.E,
}


def letra_esperada(tipo: TipoCliente, condicion_iva: CondicionIva) -> TipoComprobante:
    """Exterior E, responsable inscripto A, resto B (la misma regla del cliente)."""
    return _LETRA_DEL_DOCUMENTO[tipo_documento_por_defecto(tipo, condicion_iva)]
