"""Reglas de los comprobantes transcriptos (#108), sin base.

El sistema no emite: letra, punto de venta, numero, moneda, importe y tipo de
cambio se transcriben del comprobante que emitio ARCA (docs/dominio.md,
"Facturacion"). Aca se prueba el formato y la coherencia con el cliente.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.fechas import hoy
from app.schemas.comprobantes import ComprobanteCrear
from app.services.facturacion.comprobantes import TipoComprobante, letra_esperada
from app.services.ventas.clientes import CondicionIva, TipoCliente

TENANT = uuid.uuid4()

NACIONAL: dict[str, Any] = {
    "tipo": "A",
    "cliente_id": str(uuid.uuid4()),
    "punto_venta": 1,
    "numero": 45,
    "fecha_emision": "2026-10-20",
    "moneda": "ARS",
    "importe": "423506.05",
    "archivo_s3_key": f"documentos/{TENANT}/factura-a-45.pdf",
}

EXPORTACION: dict[str, Any] = {
    **NACIONAL,
    "tipo": "E",
    "moneda": "EUR",
    "importe": "350005.00",
    "tipo_cambio": "1125.500000",
}


@pytest.mark.parametrize(
    ("tipo", "condicion", "letra"),
    [
        (TipoCliente.NACIONAL, CondicionIva.RESPONSABLE_INSCRIPTO, "A"),
        (TipoCliente.NACIONAL, CondicionIva.MONOTRIBUTO, "B"),
        (TipoCliente.NACIONAL, CondicionIva.EXENTO, "B"),
        (TipoCliente.NACIONAL, CondicionIva.CONSUMIDOR_FINAL, "B"),
        (TipoCliente.EXPORTACION, CondicionIva.CLIENTE_EXTERIOR, "E"),
    ],
)
def test_letra_esperada_segun_el_cliente(
    tipo: TipoCliente, condicion: CondicionIva, letra: str
) -> None:
    # exterior -> E, responsable inscripto -> A, resto -> B (dominio).
    assert letra_esperada(tipo, condicion) is TipoComprobante(letra)


def test_un_comprobante_nacional_valido() -> None:
    c = ComprobanteCrear(**NACIONAL)

    assert (c.tipo, c.punto_venta, c.numero) == (TipoComprobante.A, 1, 45)
    assert c.importe == Decimal("423506.05")
    assert c.tipo_cambio is None


def test_un_comprobante_e_valido() -> None:
    c = ComprobanteCrear(**EXPORTACION)

    assert c.tipo is TipoComprobante.E
    assert c.tipo_cambio == Decimal("1125.5")


@pytest.mark.parametrize(
    "cambios",
    [
        # tipo_cambio NULL si y solo si ARS.
        pytest.param({"tipo_cambio": "1000"}, id="ars_con_tipo_de_cambio"),
        pytest.param({**EXPORTACION, "tipo_cambio": None}, id="eur_sin_tipo_cambio"),
        pytest.param({**EXPORTACION, "tipo_cambio": "0"}, id="tipo_cambio_cero"),
        # ARS <=> nacional (A o B); USD o EUR <=> E.
        pytest.param({"tipo": "E"}, id="e_en_pesos"),
        pytest.param({"moneda": "USD", "tipo_cambio": "1000"}, id="a_en_dolares"),
        pytest.param({"moneda": "BRL"}, id="moneda_fuera_de_las_3"),
        pytest.param({"tipo": "C"}, id="letra_fuera_de_a_b_e"),
        # Dos enteros, nunca el texto impreso "0001-00000045".
        pytest.param({"punto_venta": "0001"}, id="punto_venta_como_texto"),
        pytest.param({"numero": "00000045"}, id="numero_como_texto"),
        pytest.param({"punto_venta": 0}, id="punto_venta_cero"),
        pytest.param({"punto_venta": 100000}, id="punto_venta_de_6_digitos"),
        pytest.param({"numero": 0}, id="numero_cero"),
        pytest.param({"numero": 10**8}, id="numero_de_9_digitos"),
        pytest.param({"importe": "0"}, id="importe_cero"),
        pytest.param({"importe": "10.005"}, id="importe_con_3_decimales"),
        pytest.param({"archivo_s3_key": "otra/carpeta/x.pdf"}, id="archivo_fuera"),
        pytest.param({"tenant_id": str(TENANT)}, id="tenant_id_no_se_recibe"),
        pytest.param({"cargado_por": "otro"}, id="cargado_por_no_se_recibe"),
    ],
)
def test_rechaza_un_comprobante_mal_transcripto(cambios: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ComprobanteCrear(**{**NACIONAL, **cambios})


@pytest.mark.parametrize("campo", ["tipo", "punto_venta", "numero", "importe"])
def test_el_sistema_no_completa_ningun_dato_fiscal(campo: str) -> None:
    """Sin el dato transcripto no hay comprobante: nunca se calcula ni genera."""
    cuerpo = {k: v for k, v in NACIONAL.items() if k != campo}

    with pytest.raises(ValidationError):
        ComprobanteCrear(**cuerpo)


def test_la_fecha_de_emision_no_puede_ser_futura() -> None:
    manana = (hoy() + timedelta(days=1)).isoformat()

    with pytest.raises(ValidationError, match="futura"):
        ComprobanteCrear(**{**NACIONAL, "fecha_emision": manana})


def test_la_fecha_de_emision_de_hoy_se_acepta() -> None:
    c = ComprobanteCrear(**{**NACIONAL, "fecha_emision": hoy().isoformat()})

    assert c.fecha_emision == hoy()
