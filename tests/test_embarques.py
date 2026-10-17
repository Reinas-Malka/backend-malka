"""Reglas de los embarques y contrato de sus schemas (#109)."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.schemas.embarques import DocumentoCrear, EmbarqueCrear, EmbarqueGuardado
from app.services.exportacion.embarques import (
    TipoDocumentoEmbarque,
    completitud,
    normalizar_awb,
)

TENANT = uuid.uuid4()
KEY = f"documentos/{TENANT}/factura-e.pdf"


@pytest.mark.parametrize(
    ("entrada", "esperada"),
    [
        pytest.param("081-12345675", "081-12345675", id="con_guion"),
        pytest.param("08112345675", "081-12345675", id="sin_guion"),
        pytest.param(" 057 11111111 ", "057-11111111", id="con_espacio"),
    ],
)
def test_awb_valida_se_normaliza(entrada: str, esperada: str) -> None:
    assert normalizar_awb(entrada) == esperada


@pytest.mark.parametrize(
    "awb",
    [
        pytest.param("081-12345670", id="digito_verificador_mal"),
        pytest.param("081-1234567", id="corta"),
        pytest.param("81-123456755", id="prefijo_corto"),
        pytest.param("ABC-12345675", id="letras"),
    ],
)
def test_awb_invalida(awb: str) -> None:
    with pytest.raises(ValueError, match="guia aerea"):
        normalizar_awb(awb)


def test_completitud_con_los_6_tipos() -> None:
    resultado = completitud(tipo.value for tipo in TipoDocumentoEmbarque)

    assert resultado.completo
    assert resultado.faltantes == ()
    assert resultado.presentes == tuple(TipoDocumentoEmbarque)


def test_completitud_con_5_lista_el_faltante() -> None:
    cargados = [
        tipo for tipo in TipoDocumentoEmbarque if tipo is not TipoDocumentoEmbarque.DTE
    ]

    resultado = completitud(cargados)

    assert not resultado.completo
    assert resultado.faltantes == (TipoDocumentoEmbarque.DTE,)


def test_completitud_sin_documentos_faltan_todos_en_orden() -> None:
    resultado = completitud([])

    assert resultado.presentes == ()
    assert resultado.faltantes == tuple(TipoDocumentoEmbarque)


def test_embarque_normaliza_destino_y_awb() -> None:
    embarque = EmbarqueCrear.model_validate(
        {"destino_pais": " nz ", "fecha_vuelo": "2026-11-20", "awb": "08112345675"}
    )

    assert embarque.destino_pais == "NZ"
    assert embarque.awb == "081-12345675"


@pytest.mark.parametrize(
    "cuerpo",
    [
        pytest.param({"destino_pais": "AR"}, id="destino_argentina"),
        pytest.param({"destino_pais": "NZL"}, id="destino_no_iso"),
        pytest.param({"awb": "081-12345670"}, id="awb_invalida"),
        pytest.param({"estado": "en_transito"}, id="estado_en_el_alta"),
        pytest.param({"tenant_id": str(TENANT)}, id="tenant_id"),
    ],
)
def test_embarque_invalido(cuerpo: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        EmbarqueCrear.model_validate(
            {"destino_pais": "NZ", "fecha_vuelo": "2026-11-20", **cuerpo}
        )


def test_en_transito_sin_awb_no_valida() -> None:
    with pytest.raises(ValidationError, match="guia aerea"):
        EmbarqueGuardado.model_validate(
            {"destino_pais": "NZ", "fecha_vuelo": "2026-11-20", "estado": "en_transito"}
        )


def test_cancelado_sin_awb_valida() -> None:
    embarque = EmbarqueGuardado.model_validate(
        {"destino_pais": "NZ", "fecha_vuelo": "2026-11-20", "estado": "cancelado"}
    )

    assert embarque.awb is None


def _documento(**cambios: str) -> dict[str, str]:
    return {
        "tipo": "factura_e",
        "numero": "E 0003-00001234",
        "archivo_s3_key": KEY,
        "fecha": "2026-11-18",
        **cambios,
    }


def test_documento_valido() -> None:
    documento = DocumentoCrear.model_validate(_documento(numero="  E 0003-1  "))

    assert documento.tipo is TipoDocumentoEmbarque.FACTURA_E
    assert documento.numero == "E 0003-1"


@pytest.mark.parametrize(
    "cambios",
    [
        pytest.param({"tipo": "remito"}, id="tipo_fuera_de_los_6"),
        pytest.param({"numero": "   "}, id="numero_vacio"),
        pytest.param({"archivo_s3_key": "ingesta/x.pdf"}, id="otro_prefijo"),
        pytest.param(
            {"archivo_s3_key": f"documentos/{TENANT}/../otro/x.pdf"},
            id="sube_de_carpeta",
        ),
        pytest.param({"archivo_s3_key": "documentos/x.pdf"}, id="sin_tenant"),
    ],
)
def test_documento_invalido(cambios: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        DocumentoCrear.model_validate(_documento(**cambios))
