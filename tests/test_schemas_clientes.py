"""Validacion fiscal por tipo de cliente en la entrada de la API (#32, DoD)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.clientes import ClienteCrear, ClienteRespuesta
from app.services.ventas.clientes import TipoCliente, TipoDocumento

NACIONAL: dict[str, Any] = {
    "nombre": "Apicola del Sur",
    "pais": "AR",
    "tipo": "nacional",
    "condicion_iva": "monotributo",
    "cuit_o_tax_id": "20-12345678-6",
}

EXPORTACION: dict[str, Any] = {
    "nombre": "Bienenzucht GmbH",
    "pais": "DE",
    "tipo": "exportacion",
    "condicion_iva": "cliente_exterior",
    "cuit_o_tax_id": "DE123456789",
}


def test_nacional_valido_guarda_el_cuit_sin_guiones() -> None:
    cliente = ClienteCrear(**NACIONAL)

    assert cliente.tipo == TipoCliente.NACIONAL
    assert cliente.cuit_o_tax_id == "20123456786"


def test_exportacion_acepta_un_tax_id_libre() -> None:
    cliente = ClienteCrear(**EXPORTACION)

    assert cliente.cuit_o_tax_id == "DE123456789"


def test_exportacion_no_valida_el_tax_id_como_cuit() -> None:
    """Un tax id que parece un CUIT mal escrito se acepta igual."""
    cliente = ClienteCrear(**{**EXPORTACION, "cuit_o_tax_id": "20-12345678-5"})

    assert cliente.cuit_o_tax_id == "20-12345678-5"


def test_el_pais_se_guarda_en_mayusculas() -> None:
    cliente = ClienteCrear(**{**NACIONAL, "pais": "ar"})

    assert cliente.pais == "AR"


def test_saca_los_espacios_de_los_costados() -> None:
    cliente = ClienteCrear(**{**NACIONAL, "nombre": "  Apicola del Sur  "})

    assert cliente.nombre == "Apicola del Sur"


@pytest.mark.parametrize(
    ("base", "cambios"),
    [
        pytest.param(NACIONAL, {"cuit_o_tax_id": "20-12345678-5"}, id="cuit_invalido"),
        pytest.param(NACIONAL, {"cuit_o_tax_id": "no es un cuit"}, id="cuit_texto"),
        pytest.param(NACIONAL, {"pais": "UY"}, id="nacional_fuera_de_ar"),
        pytest.param(
            NACIONAL,
            {"condicion_iva": "cliente_exterior"},
            id="nacional_con_condicion_exterior",
        ),
        pytest.param(EXPORTACION, {"pais": "AR"}, id="exportacion_desde_ar"),
        pytest.param(
            EXPORTACION,
            {"condicion_iva": "responsable_inscripto"},
            id="exportacion_con_condicion_local",
        ),
        pytest.param(EXPORTACION, {"cuit_o_tax_id": "   "}, id="tax_id_vacio"),
        pytest.param(NACIONAL, {"tipo": "exportasion"}, id="tipo_inexistente"),
        pytest.param(NACIONAL, {"condicion_iva": "otra"}, id="condicion_inexistente"),
        pytest.param(NACIONAL, {"pais": "Argentina"}, id="pais_sin_iso"),
        pytest.param(
            EXPORTACION, {"pais": "ar"}, id="exportacion_desde_ar_en_minuscula"
        ),
        pytest.param(NACIONAL, {"nombre": ""}, id="nombre_vacio"),
        # El tenant lo pone PostgreSQL, nunca el JSON (ADR 0009).
        pytest.param(NACIONAL, {"tenant_id": str(uuid.uuid4())}, id="con_tenant_id"),
    ],
)
def test_rechaza_cliente_invalido(
    base: dict[str, Any], cambios: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        ClienteCrear(**{**base, **cambios})


def test_falta_un_campo() -> None:
    datos = {k: v for k, v in NACIONAL.items() if k != "cuit_o_tax_id"}

    with pytest.raises(ValidationError):
        ClienteCrear(**datos)


def test_el_error_no_expone_el_cuit() -> None:
    with pytest.raises(ValidationError) as error:
        ClienteCrear(**{**NACIONAL, "cuit_o_tax_id": "20-12345678-5"})

    assert "12345678" not in error.value.errors()[0]["msg"]


@pytest.mark.parametrize(
    ("datos", "esperado"),
    [
        pytest.param(EXPORTACION, TipoDocumento.FACTURA_E, id="exportacion_e"),
        pytest.param(
            {**NACIONAL, "condicion_iva": "responsable_inscripto"},
            TipoDocumento.FACTURA_A,
            id="inscripto_a",
        ),
        pytest.param(NACIONAL, TipoDocumento.FACTURA_B, id="monotributo_b"),
    ],
)
def test_la_respuesta_incluye_el_documento_por_defecto(
    datos: dict[str, Any], esperado: TipoDocumento
) -> None:
    creado = ClienteCrear(**datos)
    respuesta = ClienteRespuesta(id=uuid.uuid4(), activo=True, **creado.model_dump())

    cuerpo = respuesta.model_dump(mode="json")

    assert cuerpo["tipo_documento_por_defecto"] == esperado.value
