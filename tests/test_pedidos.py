"""Reglas de los pedidos (#34)."""

from __future__ import annotations

import pytest

from app.services.ventas.clientes import TipoCliente
from app.services.ventas.pedidos import (
    Incoterm,
    Moneda,
    es_pais_valido,
    validar_pedido,
)


@pytest.mark.parametrize(
    ("tipo", "moneda", "destino_pais", "incoterm"),
    [
        pytest.param(TipoCliente.NACIONAL, Moneda.ARS, None, None, id="nacional"),
        pytest.param(
            TipoCliente.EXPORTACION,
            Moneda.USD,
            "CL",
            Incoterm.CPT,
            id="exportacion_directa_con_incoterm",
        ),
        # Envio consolidado a Europa: el incoterm queda vacio.
        pytest.param(
            TipoCliente.EXPORTACION,
            Moneda.EUR,
            "DE",
            None,
            id="exportacion_sin_incoterm",
        ),
    ],
)
def test_acepta_pedido_coherente(
    tipo: TipoCliente,
    moneda: Moneda,
    destino_pais: str | None,
    incoterm: Incoterm | None,
) -> None:
    validar_pedido(tipo, moneda, destino_pais, incoterm)


@pytest.mark.parametrize(
    ("tipo", "moneda", "destino_pais", "incoterm", "mensaje"),
    [
        pytest.param(
            TipoCliente.EXPORTACION,
            Moneda.USD,
            None,
            Incoterm.CPT,
            "destino_pais",
            id="exportacion_sin_destino_pais",
        ),
        pytest.param(
            TipoCliente.EXPORTACION,
            Moneda.USD,
            "AR",
            None,
            "AR",
            id="exportacion_a_argentina",
        ),
        pytest.param(
            TipoCliente.EXPORTACION,
            Moneda.ARS,
            "CL",
            None,
            "USD o EUR",
            id="exportacion_en_pesos",
        ),
        pytest.param(
            TipoCliente.EXPORTACION,
            Moneda.USD,
            "cl",
            None,
            "2 letras",
            id="destino_pais_en_minusculas",
        ),
        pytest.param(
            TipoCliente.NACIONAL,
            Moneda.USD,
            None,
            None,
            "ARS",
            id="nacional_en_dolares",
        ),
        pytest.param(
            TipoCliente.NACIONAL,
            Moneda.ARS,
            "CL",
            None,
            "destino_pais",
            id="nacional_con_destino_pais",
        ),
        pytest.param(
            TipoCliente.NACIONAL,
            Moneda.ARS,
            None,
            Incoterm.EXW,
            "incoterm",
            id="nacional_con_incoterm",
        ),
    ],
)
def test_rechaza_pedido_incoherente(
    tipo: TipoCliente,
    moneda: Moneda,
    destino_pais: str | None,
    incoterm: Incoterm | None,
    mensaje: str,
) -> None:
    with pytest.raises(ValueError, match=mensaje):
        validar_pedido(tipo, moneda, destino_pais, incoterm)


@pytest.mark.parametrize("pais", ["AR", "CL", "DE"])
def test_acepta_pais_valido(pais: str) -> None:
    assert es_pais_valido(pais)


@pytest.mark.parametrize(
    "pais",
    [
        pytest.param("", id="vacio"),
        pytest.param("D", id="una_letra"),
        pytest.param("DEU", id="alfa_3"),
        pytest.param("de", id="minusculas"),
        pytest.param("D1", id="con_numero"),
        pytest.param("ÑA", id="letra_no_ascii"),
    ],
)
def test_rechaza_pais_invalido(pais: str) -> None:
    assert not es_pais_valido(pais)
