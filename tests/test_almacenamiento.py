"""Armado de keys y firma de URLs de S3 (#102). Sin AWS: firmador falso."""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest

from app.services.documentos import almacenamiento
from app.services.documentos.almacenamiento import SubidaNoConfiguradaError

TENANT = uuid.UUID("11111111-1111-4111-8111-111111111111")


class FirmadorFalso:
    def __init__(self) -> None:
        self.llamadas: list[dict[str, Any]] = []

    def generate_presigned_url(
        self, ClientMethod: str, Params: dict[str, Any], ExpiresIn: int
    ) -> str:
        self.llamadas.append(
            {"metodo": ClientMethod, "params": Params, "expira": ExpiresIn}
        )
        return f"https://s3.test/{Params['Key']}?firma=falsa"


@pytest.mark.parametrize(
    ("nombre", "esperado"),
    [
        pytest.param("factura.pdf", "factura.pdf", id="simple"),
        pytest.param(
            "Factura E Nº 41.pdf", "Factura-E-No-41.pdf", id="espacios_y_acentos"
        ),
        pytest.param("../../otro/guia.pdf", "guia.pdf", id="sin_carpetas"),
        pytest.param("C:\\docs\\dte.pdf", "dte.pdf", id="ruta_windows"),
        pytest.param("ñññ", "nnn", id="solo_acentos"),
        pytest.param("???", "documento", id="nada_valido"),
    ],
)
def test_nombre_para_clave_queda_seguro(nombre: str, esperado: str) -> None:
    assert almacenamiento.nombre_para_clave(nombre) == esperado


def test_la_clave_va_bajo_el_prefijo_del_criadero_y_no_se_repite() -> None:
    una = almacenamiento.armar_clave(TENANT, "factura.pdf")
    otra = almacenamiento.armar_clave(TENANT, "factura.pdf")

    assert una != otra
    # El mismo patrón que exigen los CHECK de las migraciones 0010 y 0011.
    assert re.fullmatch(rf"documentos/{TENANT}/[0-9a-f-]{{36}}-factura\.pdf", una)


def test_la_subida_firma_el_content_type_y_vence_rapido(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BUCKET_DOCUMENTOS", "bucket-de-prueba")
    firmador = FirmadorFalso()

    almacenamiento.firmar_subida(firmador, "documentos/t/x.pdf", "application/pdf")

    [llamada] = firmador.llamadas
    assert llamada["metodo"] == "put_object"
    assert llamada["params"] == {
        "Bucket": "bucket-de-prueba",
        "Key": "documentos/t/x.pdf",
        "ContentType": "application/pdf",
    }
    assert llamada["expira"] == almacenamiento.SEGUNDOS_SUBIDA


def test_la_descarga_baja_con_el_nombre_saneado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BUCKET_DOCUMENTOS", "bucket-de-prueba")
    firmador = FirmadorFalso()

    almacenamiento.firmar_descarga(firmador, "documentos/t/x.pdf", 'Fac"tura.pdf')

    [llamada] = firmador.llamadas
    assert llamada["metodo"] == "get_object"
    assert (
        llamada["params"]["ResponseContentDisposition"]
        == 'attachment; filename="Fac-tura.pdf"'
    )


def test_sin_bucket_configurado_es_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BUCKET_DOCUMENTOS", raising=False)

    with pytest.raises(SubidaNoConfiguradaError) as error:
        almacenamiento.firmar_subida(FirmadorFalso(), "documentos/t/x.pdf", "image/png")

    assert error.value.status_code == 503
    assert error.value.code == "subida_no_configurada"
