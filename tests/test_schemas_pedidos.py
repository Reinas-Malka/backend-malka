"""Formato del cuerpo de POST /api/v1/pedidos (#34).

La coherencia con el tipo de cliente no se prueba aca: la aplica el
repositorio con validar_pedido (tests/test_pedidos.py).
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.pedidos import (
    MAX_CANTIDAD,
    MAX_ITEMS,
    PedidoCrear,
    PedidoRespuesta,
)
from app.services.documentos.importes import Alicuota
from app.services.ventas.pedidos import EstadoPedido, Incoterm, Moneda

ITEM: dict[str, Any] = {
    "item_vendible_id": str(uuid.uuid4()),
    "raza_id": str(uuid.uuid4()),
    "cantidad": 10,
    "precio_unitario": "35.50",
}

EXPORTACION: dict[str, Any] = {
    "cliente_id": str(uuid.uuid4()),
    "moneda": "EUR",
    "destino_pais": "DE",
    "incoterm": "CPT",
    "fecha_entrega_solicitada": "2026-11-30",
    "items": [ITEM],
}

NACIONAL: dict[str, Any] = {
    "cliente_id": str(uuid.uuid4()),
    "moneda": "ARS",
    "fecha_entrega_solicitada": "2026-11-30",
    "items": [ITEM],
}


def test_exportacion_completa() -> None:
    pedido = PedidoCrear(**EXPORTACION)

    assert pedido.moneda == Moneda.EUR
    assert pedido.destino_pais == "DE"
    assert pedido.incoterm == Incoterm.CPT
    assert pedido.items[0].precio_unitario == Decimal("35.50")


def test_nacional_sin_destino_ni_incoterm() -> None:
    pedido = PedidoCrear(**NACIONAL)

    assert pedido.destino_pais is None
    assert pedido.incoterm is None


def test_pasa_a_mayusculas_y_saca_espacios() -> None:
    pedido = PedidoCrear(
        **{**EXPORTACION, "moneda": "eur", "destino_pais": " de ", "incoterm": "cpt"}
    )

    assert pedido.moneda == Moneda.EUR
    assert pedido.destino_pais == "DE"
    assert pedido.incoterm == Incoterm.CPT


def test_un_precio_como_numero_json_llega_exacto() -> None:
    """0.1 no se vuelve 0.1000000000000000055511151231257827."""
    # Como numero, no como texto: asi lo manda un frontend despistado.
    cuerpo = json.dumps({**NACIONAL, "items": [{**ITEM, "precio_unitario": 0.1}]})

    pedido = PedidoCrear.model_validate_json(cuerpo)

    assert pedido.items[0].precio_unitario == Decimal("0.1")


def test_precio_cero_es_valido() -> None:
    """Muestra sin cargo o bonificacion, como en importes.py."""
    pedido = PedidoCrear(**{**NACIONAL, "items": [{**ITEM, "precio_unitario": "0"}]})

    assert pedido.items[0].precio_unitario == Decimal("0")


@pytest.mark.parametrize(
    "cambios",
    [
        pytest.param({"estado": "entregado"}, id="no_acepta_estado"),
        pytest.param({"tenant_id": str(uuid.uuid4())}, id="no_acepta_tenant_id"),
        pytest.param({"moneda": "GBP"}, id="moneda_fuera_de_la_lista"),
        pytest.param({"incoterm": "XXX"}, id="incoterm_fuera_de_la_lista"),
        pytest.param({"destino_pais": "DEU"}, id="destino_alfa_3"),
        pytest.param({"destino_pais": "Ñ1"}, id="destino_invalido"),
        pytest.param({"fecha_entrega_solicitada": "30/11/2026"}, id="fecha_mal"),
        pytest.param({"items": []}, id="sin_items"),
        pytest.param({"items": [ITEM] * (MAX_ITEMS + 1)}, id="demasiados_items"),
    ],
)
def test_rechaza_pedido_mal_formado(cambios: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PedidoCrear(**{**EXPORTACION, **cambios})


@pytest.mark.parametrize(
    "cambios",
    [
        pytest.param({"cantidad": 0}, id="cantidad_cero"),
        pytest.param({"cantidad": -1}, id="cantidad_negativa"),
        pytest.param({"cantidad": 1.5}, id="cantidad_con_decimales"),
        pytest.param({"precio_unitario": "-0.01"}, id="precio_negativo"),
        pytest.param({"precio_unitario": "35.555"}, id="precio_con_3_decimales"),
        pytest.param({"precio_unitario": "1" * 13 + ".00"}, id="precio_enorme"),
        pytest.param({"precio_unitario": "NaN"}, id="precio_nan"),
        pytest.param({"alicuota": "0.21"}, id="no_acepta_alicuota"),
        pytest.param({"raza_id": "italiana"}, id="raza_no_es_uuid"),
    ],
)
def test_rechaza_item_mal_formado(cambios: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PedidoCrear(**{**NACIONAL, "items": [{**ITEM, **cambios}]})


def test_respuesta_lee_la_alicuota_como_la_devuelve_la_base() -> None:
    """Numeric(5, 3) devuelve 0.210; la respuesta la muestra como 0.21."""
    respuesta = PedidoRespuesta.model_validate(
        {
            **NACIONAL,
            "id": uuid.uuid4(),
            "estado": "borrador",
            "destino_pais": None,
            "incoterm": None,
            "items": [
                {
                    **ITEM,
                    "id": uuid.uuid4(),
                    "numero_linea": 1,
                    "alicuota": Decimal("0.210"),
                },
            ],
        }
    )

    assert respuesta.estado == EstadoPedido.BORRADOR
    assert respuesta.items[0].alicuota is Alicuota.IVA_21
    assert respuesta.model_dump(mode="json")["items"][0]["alicuota"] == "0.21"


def test_cantidad_que_no_entra_en_la_columna_es_error_de_formato() -> None:
    """La columna es Integer: sin tope, la base revienta y la API da 500."""
    with pytest.raises(ValidationError):
        PedidoCrear(**{**NACIONAL, "items": [{**ITEM, "cantidad": MAX_CANTIDAD + 1}]})


def test_cantidad_maxima_se_acepta() -> None:
    pedido = PedidoCrear(**{**NACIONAL, "items": [{**ITEM, "cantidad": MAX_CANTIDAD}]})

    assert pedido.items[0].cantidad == MAX_CANTIDAD


def test_moneda_y_destino_con_espacios_se_normalizan() -> None:
    """str_strip_whitespace corre despues del validador before: lo hace el."""
    pedido = PedidoCrear(**{**EXPORTACION, "moneda": " eur ", "destino_pais": " de"})

    assert pedido.moneda == Moneda.EUR
    assert pedido.destino_pais == "DE"
