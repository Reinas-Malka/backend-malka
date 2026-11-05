"""PDF interno del borrador aprobado (#42), sin base ni S3."""

from __future__ import annotations

import re
import zlib

import pytest

from app.services.ia import pdf_borrador
from app.services.ia.pdf_borrador import a_latin1, es_titulo, numero_impreso, pie


def _snapshot(**cambios: object) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "borrador": {
            "contenido": {
                "encabezado": "Proforma de exportación de reinas",
                "cuerpo": "PARTES\nEl criadero vende.",
                "notas": "",
            }
        },
        "pedido": {
            "moneda": "EUR",
            "lineas": [
                {
                    "cantidad": 10,
                    "item": "Reina fecundada",
                    "raza": "Carniola",
                    "precio_unitario": "35.00",
                    "neto": "350.00",
                }
            ],
            "totales": {
                "exportacion": True,
                "neto": "350.00",
                "iva": "0.00",
                "total": "350.00",
            },
        },
        "comprobante": None,
        "aprobacion": {
            "usuario": "sub-admin",
            # 02:30 UTC son las 23:30 del dia anterior en Argentina.
            "momento": "2026-11-05T02:30:00+00:00",
            "hash_contenido": "0123456789ab" + "f" * 52,
        },
    }
    snapshot.update(cambios)
    return snapshot


def _texto(pdf: bytes) -> str:
    paginas = [
        zlib.decompress(m.group(1))
        for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", pdf, re.S)
    ]
    cadenas = re.findall(rb"\(((?:\\.|[^\\)])*)\) Tj", b"\n".join(paginas))
    return "\n".join(re.sub(rb"\\(.)", rb"\1", c).decode("latin-1") for c in cadenas)


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        pytest.param("Apícola Ñandú", "Apícola Ñandú", id="tildes_y_enie"),
        pytest.param("a — b – c", "a - b - c", id="rayas"),
        pytest.param("y…", "y...", id="puntos_suspensivos"),
        pytest.param("“hola” ‘chau’", "\"hola\" 'chau'", id="comillas"),
        pytest.param("100 €", "100 EUR", id="euro"),
        pytest.param("Gdańsk", "Gdansk", id="letra_base"),
        pytest.param("ok 🐝", "ok ?", id="sin_equivalente"),
    ],
)
def test_a_latin1_conserva_tildes_y_reemplaza_el_resto(
    texto: str, esperado: str
) -> None:
    assert a_latin1(texto) == esperado
    a_latin1(texto).encode("latin-1")


@pytest.mark.parametrize(
    ("linea", "titulo"),
    [
        ("CONDICIONES DE ENTREGA", True),
        ("  OBJETO  ", True),
        ("El criadero vende reinas.", False),
        ("2026", False),
        ("", False),
        ("A" * 61, False),
    ],
)
def test_es_titulo(linea: str, titulo: bool) -> None:
    assert es_titulo(linea) is titulo


def test_el_numero_se_rellena_al_imprimir() -> None:
    assert numero_impreso(3, 45) == "0003-00000045"
    assert numero_impreso(12345, 1) == "12345-00000001"


def test_el_pie_va_en_hora_de_argentina_y_dice_que_no_es_fiscal() -> None:
    texto = pie(_snapshot()["aprobacion"])  # type: ignore[arg-type]

    assert texto == (
        "Documento interno aprobado por sub-admin el 04/11/2026 23:30 (Argentina)"
        " · versión 0123456789ab · No es un comprobante fiscal"
    )


def test_el_pdf_muestra_el_cae_si_el_comprobante_lo_trae() -> None:
    comprobante = {
        "tipo": "E",
        "punto_venta": 3,
        "numero": 45,
        "fecha_emision": "2026-10-20",
        "cae": "74123456789012",
    }

    texto = _texto(pdf_borrador.renderizar(_snapshot(comprobante=comprobante)))

    assert "Factura E 0003-00000045 del 20/10/2026 - CAE 74123456789012" in texto
    assert "IVA (exportación: 0%)" in texto
    assert "EUR 350.00" in texto
    assert "Notas" not in texto


def test_un_cuerpo_largo_repite_el_pie_en_cada_pagina() -> None:
    contenido = {
        "encabezado": "Nota de venta",
        "cuerpo": "\n\n".join(["Un párrafo formal del documento."] * 120),
        "notas": "Recibirlas apenas lleguen.",
    }

    pdf = pdf_borrador.renderizar(_snapshot(borrador={"contenido": contenido}))

    assert _texto(pdf).count("No es un comprobante fiscal") >= 2
