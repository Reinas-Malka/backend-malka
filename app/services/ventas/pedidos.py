"""Reglas de los pedidos (#34).

No depende de la base, de FastAPI ni de AWS. El servicio carga el cliente,
llama a validar_pedido y convierte el ValueError en un 422.

Reglas en docs/dominio.md, "Pedidos" y "Modalidades de envio" (11/10).
"""

from __future__ import annotations

from enum import StrEnum

from app.services.ventas.clientes import TipoCliente


class EstadoPedido(StrEnum):
    # La lista completa de #36: las transiciones las valida su servicio.
    BORRADOR = "borrador"
    RESERVADO = "reservado"
    ENTREGADO = "entregado"
    CANCELADO = "cancelado"


class Incoterm(StrEnum):
    # Incoterms 2020. Opcional: en los envios consolidados queda vacio.
    EXW = "EXW"
    FCA = "FCA"
    CPT = "CPT"
    CIP = "CIP"
    DAP = "DAP"
    DPU = "DPU"
    DDP = "DDP"
    FAS = "FAS"
    FOB = "FOB"
    CFR = "CFR"
    CIF = "CIF"


class Moneda(StrEnum):
    # ARS en ventas nacionales, USD o EUR en exportacion. No hay otras.
    ARS = "ARS"
    USD = "USD"
    EUR = "EUR"


MONEDAS_EXPORTACION = (Moneda.USD, Moneda.EUR)


def es_pais_valido(pais: str) -> bool:
    """Codigo ISO 3166-1 alfa-2: dos letras mayusculas de la A a la Z."""
    # isalpha() solo no alcanza: acepta letras con tilde y de otros alfabetos.
    return len(pais) == 2 and pais.isascii() and pais.isalpha() and pais.isupper()


def validar_pedido(
    tipo_cliente: TipoCliente,
    moneda: Moneda,
    destino_pais: str | None,
    incoterm: Incoterm | None = None,
) -> None:
    """Revisa que el pedido sea coherente con el tipo de cliente.

    Exportacion si y solo si el cliente es de exportacion. destino_pais es
    el pais donde recibe el cliente final, no el del hub del embarque.
    Lanza ValueError con el primer problema que encuentra.
    """
    if tipo_cliente == TipoCliente.EXPORTACION:
        if destino_pais is None:
            raise ValueError("Un pedido de exportacion necesita destino_pais.")
        if not es_pais_valido(destino_pais):
            raise ValueError("destino_pais tiene que ser un codigo de 2 letras.")
        if destino_pais == "AR":
            raise ValueError("Un pedido de exportacion no puede ir a AR.")
        if moneda not in MONEDAS_EXPORTACION:
            raise ValueError("Un pedido de exportacion va en USD o EUR.")
        return

    if destino_pais is not None:
        raise ValueError("Un pedido nacional no lleva destino_pais.")
    if incoterm is not None:
        raise ValueError("Un pedido nacional no lleva incoterm.")
    if moneda != Moneda.ARS:
        raise ValueError("Un pedido nacional va en ARS.")
