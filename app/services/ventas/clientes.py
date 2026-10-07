"""Reglas fiscales de los clientes (#32).

No depende de la base, de FastAPI ni de AWS: los schemas de la API y otros
modulos (pedidos, #34) usan estas reglas sin duplicarlas.
"""

from __future__ import annotations

from enum import StrEnum

PESOS_CUIT = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)


class TipoCliente(StrEnum):
    NACIONAL = "nacional"
    EXPORTACION = "exportacion"


class CondicionIva(StrEnum):
    RESPONSABLE_INSCRIPTO = "responsable_inscripto"
    MONOTRIBUTO = "monotributo"
    EXENTO = "exento"
    CONSUMIDOR_FINAL = "consumidor_final"
    CLIENTE_EXTERIOR = "cliente_exterior"


class TipoDocumento(StrEnum):
    FACTURA_A = "factura_a"
    FACTURA_B = "factura_b"
    FACTURA_E = "factura_e"


def normalizar_cuit(cuit: str) -> str:
    """Devuelve los 11 digitos sin guiones, o ValueError si el formato no da.

    Solo revisa el formato; el digito verificador lo revisa es_cuit_valido.
    El mensaje no incluye el CUIT: es un dato fiscal.
    """
    cuit = cuit.replace("-", "")
    if len(cuit) != 11 or not cuit.isascii() or not cuit.isdigit():
        raise ValueError("El CUIT tiene que tener 11 digitos.")
    return cuit


def es_cuit_valido(cuit: str) -> bool:
    try:
        cuit = normalizar_cuit(cuit)
    except ValueError:
        return False

    suma = sum(
        peso * digito
        for peso, digito in zip(PESOS_CUIT, map(int, cuit[:10]), strict=True)
    )
    verificador = 11 - suma % 11

    if verificador == 11:
        verificador = 0

    if verificador == 10:
        return False

    return verificador == int(cuit[10])


def tipo_documento_por_defecto(
    tipo: TipoCliente, condicion_iva: CondicionIva
) -> TipoDocumento:
    """Letra esperada segun docs/dominio.md: exterior E, inscripto A, resto B.

    La letra real la asigna ARCA y se transcribe; esto sirve para validar que
    la letra cargada sea coherente con el cliente.
    """
    if tipo == TipoCliente.EXPORTACION:
        return TipoDocumento.FACTURA_E

    if condicion_iva == CondicionIva.RESPONSABLE_INSCRIPTO:
        return TipoDocumento.FACTURA_A

    if condicion_iva in (
        CondicionIva.MONOTRIBUTO,
        CondicionIva.EXENTO,
        CondicionIva.CONSUMIDOR_FINAL,
    ):
        return TipoDocumento.FACTURA_B

    raise ValueError(f"Un cliente {tipo} no puede tener condicion {condicion_iva}")
