"""Validacion fiscal de clientes (#32)."""

from __future__ import annotations

import pytest

from app.services.ventas.clientes import (
    CondicionIva,
    TipoCliente,
    TipoDocumento,
    es_cuit_valido,
    tipo_documento_por_defecto,
)


@pytest.mark.parametrize(
    "cuit",
    [
        pytest.param("20123456786", id="sin_guiones"),
        pytest.param("20-12345678-6", id="con_guiones"),
        pytest.param("30-12345678-1", id="persona_juridica"),
        # La cuenta da 11, que se convierte en 0.
        pytest.param("27-12345678-0", id="verificador_11_pasa_a_0"),
    ],
)
def test_acepta_cuit_valido(cuit: str) -> None:
    assert es_cuit_valido(cuit)


@pytest.mark.parametrize(
    "cuit",
    [
        pytest.param("20123456785", id="verificador_incorrecto"),
        # Con 2010000005 la cuenta da 10: ningun digito final lo hace valido.
        pytest.param("20100000050", id="verificador_10"),
        pytest.param("", id="vacio"),
        pytest.param("123", id="muy_corto"),
        pytest.param("201234567861", id="muy_largo"),
        pytest.param("2012345678X", id="con_letra"),
        pytest.param("20 12345678 6", id="con_espacios"),
        # isdigit() los acepta, pero no son digitos del 0 al 9.
        pytest.param("2012345678²", id="superindice"),
        pytest.param("٢٠١٢٣٤٥٦٧٨٦", id="digitos_arabigos"),
    ],
)
def test_rechaza_cuit_invalido(cuit: str) -> None:
    assert not es_cuit_valido(cuit)


@pytest.mark.parametrize(
    ("tipo", "condicion_iva", "esperado"),
    [
        # Si una exportacion admite otra condicion de IVA se decide en el
        # schema (paso 4); aca solo se prueba el caso tipico.
        pytest.param(
            TipoCliente.EXPORTACION,
            CondicionIva.CLIENTE_EXTERIOR,
            TipoDocumento.FACTURA_E,
            id="exportacion_es_e",
        ),
        pytest.param(
            TipoCliente.NACIONAL,
            CondicionIva.RESPONSABLE_INSCRIPTO,
            TipoDocumento.FACTURA_A,
            id="responsable_inscripto_es_a",
        ),
        # Confirmado por la clienta en docs/dominio.md: el monotributo va con B.
        pytest.param(
            TipoCliente.NACIONAL,
            CondicionIva.MONOTRIBUTO,
            TipoDocumento.FACTURA_B,
            id="monotributo_es_b",
        ),
        pytest.param(
            TipoCliente.NACIONAL,
            CondicionIva.CONSUMIDOR_FINAL,
            TipoDocumento.FACTURA_B,
            id="consumidor_final_es_b",
        ),
        pytest.param(
            TipoCliente.NACIONAL,
            CondicionIva.EXENTO,
            TipoDocumento.FACTURA_B,
            id="exento_es_b",
        ),
    ],
)
def test_tipo_documento_por_defecto(
    tipo: TipoCliente, condicion_iva: CondicionIva, esperado: TipoDocumento
) -> None:
    assert tipo_documento_por_defecto(tipo, condicion_iva) == esperado


def test_nacional_con_condicion_de_exterior_no_tiene_documento() -> None:
    with pytest.raises(ValueError):
        tipo_documento_por_defecto(TipoCliente.NACIONAL, CondicionIva.CLIENTE_EXTERIOR)
