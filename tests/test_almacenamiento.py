"""Armado de keys y firma de URLs de S3 (#102). Sin AWS: firmador falso."""

from __future__ import annotations

import re
import sys
import types
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


def test_en_linea_el_navegador_lo_muestra_en_vez_de_bajarlo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "Abrir" en el front: el PDF se ve en la pestaña, con el mismo nombre."""
    monkeypatch.setenv("BUCKET_DOCUMENTOS", "bucket-de-prueba")
    firmador = FirmadorFalso()

    almacenamiento.firmar_descarga(
        firmador, "documentos/t/x.pdf", "Factura.pdf", en_linea=True
    )

    [llamada] = firmador.llamadas
    assert (
        llamada["params"]["ResponseContentDisposition"]
        == 'inline; filename="Factura.pdf"'
    )


def test_sin_bucket_configurado_es_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BUCKET_DOCUMENTOS", raising=False)

    with pytest.raises(SubidaNoConfiguradaError) as error:
        almacenamiento.firmar_subida(FirmadorFalso(), "documentos/t/x.pdf", "image/png")

    assert error.value.status_code == 503
    assert error.value.code == "subida_no_configurada"


class SubidorFalso:
    def __init__(self, falla: bool = False) -> None:
        self.falla = falla
        self.subidos: list[dict[str, Any]] = []

    def put_object(
        self, *, Bucket: str, Key: str, Body: bytes, ContentType: str
    ) -> dict[str, Any]:
        if self.falla:
            raise RuntimeError("AccessDenied: detalle de AWS")
        self.subidos.append(
            {"Bucket": Bucket, "Key": Key, "Body": Body, "ContentType": ContentType}
        )
        return {}


def test_la_clave_del_pdf_del_borrador_es_fija_y_del_criadero() -> None:
    borrador = uuid.UUID("22222222-2222-4222-8222-222222222222")

    clave = almacenamiento.clave_pdf_borrador(TENANT, borrador)

    assert clave == f"documentos/{TENANT}/borradores/{borrador}.pdf"


def test_subir_pdf_lo_guarda_como_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUCKET_DOCUMENTOS", "bucket-de-prueba")
    subidor = SubidorFalso()

    almacenamiento.subir_pdf(subidor, "documentos/x/borradores/y.pdf", b"%PDF-1.3")

    assert subidor.subidos == [
        {
            "Bucket": "bucket-de-prueba",
            "Key": "documentos/x/borradores/y.pdf",
            "Body": b"%PDF-1.3",
            "ContentType": "application/pdf",
        }
    ]


def test_si_s3_falla_subir_pdf_es_503_sin_el_detalle_de_aws(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BUCKET_DOCUMENTOS", "bucket-de-prueba")

    with pytest.raises(almacenamiento.AlmacenamientoNoDisponibleError) as error:
        almacenamiento.subir_pdf(SubidorFalso(falla=True), "k", b"%PDF")

    assert error.value.status_code == 503
    assert error.value.code == "almacenamiento_no_disponible"
    assert "AccessDenied" not in error.value.message


def test_el_subidor_real_es_el_cliente_s3_de_boto3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """boto3 no esta en local ni en el CI: se reemplaza el modulo."""
    usado: dict[str, Any] = {}

    def client(servicio: str, **opciones: Any) -> str:
        usado.update(servicio=servicio, **opciones)
        return "cliente-s3"

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=client))

    assert almacenamiento.subidor_s3() == "cliente-s3"
    assert usado == {"servicio": "s3", "region_name": "us-east-1"}
