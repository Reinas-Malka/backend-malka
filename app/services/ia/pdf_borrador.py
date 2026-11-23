"""PDF interno del borrador aprobado (#42), renderizado desde el snapshot.

Es un documento interno: el comprobante fiscal lo emite ARCA y su PDF se
sube aparte (#102). Por eso el pie lo dice en cada pagina, junto con quien
aprobo, cuando y la version (hash) aprobada.

Lee solo el snapshot: nada de la base. Las fuentes core de fpdf2 son
latin-1; los caracteres de afuera (raya, puntos suspensivos, comillas
tipograficas) se reemplazan sin tocar tildes ni enies.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from fpdf import FPDF

from app.fechas import ZONA_ARGENTINA

FUENTE = "Helvetica"
ALTO_LINEA = 5.5
LARGO_VERSION = 12
# Un titulo de seccion del prompt v3 ("CONDICIONES DE ENTREGA") es corto.
LARGO_MAXIMO_TITULO = 60

_REEMPLAZOS = {
    "—": "-",  # raya
    "–": "-",  # semirraya
    "‑": "-",
    "−": "-",
    "…": "...",
    "“": '"',
    "”": '"',
    "„": '"',
    "«": '"',
    "»": '"',
    "‘": "'",
    "’": "'",
    "‚": "'",
    "•": "-",
    " ": " ",
    " ": " ",
    "€": "EUR",
    "\t": "    ",
}

# Columnas del detalle: cantidad, item, raza, precio unitario, neto (mm).
ANCHOS = (18.0, 62.0, 40.0, 30.0, 30.0)


def a_latin1(texto: str) -> str:
    """El texto en latin-1: a, e, n con tilde quedan; el resto se aproxima."""
    salida: list[str] = []
    for caracter in texto:
        reemplazo = _REEMPLAZOS.get(caracter, caracter)
        if all(ord(c) < 256 for c in reemplazo):
            salida.append(reemplazo)
            continue
        # "ś" -> "s": la letra base, sin el acento que latin-1 no tiene.
        base = unicodedata.normalize("NFKD", reemplazo)
        latina = base.encode("latin-1", "ignore").decode("latin-1")
        salida.append(latina or "?")
    return "".join(salida)


def es_titulo(linea: str) -> bool:
    """Una linea sola en mayusculas: titulo de seccion del cuerpo."""
    limpia = linea.strip()
    return (
        0 < len(limpia) <= LARGO_MAXIMO_TITULO
        and any(c.isalpha() for c in limpia)
        and limpia == limpia.upper()
    )


def numero_impreso(punto_venta: int, numero: int) -> str:
    """0003-00000045: el relleno se arma al mostrar, nunca se guarda (#108)."""
    return f"{punto_venta:04d}-{numero:08d}"


def momento_argentina(iso: str) -> str:
    return (
        datetime.fromisoformat(iso)
        .astimezone(ZONA_ARGENTINA)
        .strftime("%d/%m/%Y %H:%M")
    )


def pie(aprobacion: Mapping[str, Any]) -> str:
    return (
        f"Documento interno aprobado por {aprobacion['usuario']} el "
        f"{momento_argentina(aprobacion['momento'])} (Argentina) · versión "
        f"{aprobacion['hash_contenido'][:LARGO_VERSION]} · "
        "No es un comprobante fiscal"
    )


class _Documento(FPDF):
    def __init__(self, texto_pie: str) -> None:
        super().__init__(format="A4")
        self.texto_pie = a_latin1(texto_pie)

    def footer(self) -> None:
        self.set_y(-15)
        self.set_font(FUENTE, "I", 7.5)
        self.multi_cell(0, 3.5, self.texto_pie, align="C")


def _escribir(pdf: FPDF, texto: str, estilo: str = "", tamano: float = 10) -> None:
    pdf.set_font(FUENTE, estilo, tamano)
    pdf.multi_cell(0, ALTO_LINEA, a_latin1(texto), new_x="LMARGIN", new_y="NEXT")


def _cuerpo(pdf: FPDF, cuerpo: str) -> None:
    for linea in cuerpo.splitlines():
        if not linea.strip():
            pdf.ln(2)
        elif es_titulo(linea):
            pdf.ln(1)
            _escribir(pdf, linea.strip(), "B", 10.5)
        else:
            _escribir(pdf, linea.strip())


def _celda(pdf: FPDF, ancho: float, texto: str, alinear: str = "L") -> None:
    pdf.cell(ancho, 6, a_latin1(texto), border="B", align=alinear)


def _detalle(pdf: FPDF, pedido: Mapping[str, Any]) -> None:
    moneda = pedido["moneda"]
    pdf.ln(3)
    _escribir(pdf, "Detalle del pedido (calculado por el sistema)", "B", 11)
    pdf.set_font(FUENTE, "B", 9)
    for ancho, titulo, alinear in zip(
        ANCHOS,
        ("Cant.", "Ítem", "Raza", f"P. unit. {moneda}", f"Neto {moneda}"),
        ("R", "L", "L", "R", "R"),
        strict=True,
    ):
        _celda(pdf, ancho, titulo, alinear)
    pdf.ln()
    pdf.set_font(FUENTE, "", 9)
    lineas: Sequence[Mapping[str, Any]] = pedido["lineas"]
    for linea in lineas:
        valores = (
            str(linea["cantidad"]),
            linea["item"],
            linea["raza"],
            linea["precio_unitario"],
            linea["neto"],
        )
        for ancho, valor, alinear in zip(
            ANCHOS, valores, ("R", "L", "L", "R", "R"), strict=True
        ):
            _celda(pdf, ancho, valor, alinear)
        pdf.ln()

    totales = pedido["totales"]
    pdf.ln(1)
    etiqueta_iva = "IVA (exportación: 0%)" if totales["exportacion"] else "IVA"
    for etiqueta, valor, estilo in (
        ("Neto", totales["neto"], ""),
        (etiqueta_iva, totales["iva"], ""),
        ("Total", totales["total"], "B"),
    ):
        pdf.set_font(FUENTE, estilo, 9.5)
        pdf.cell(sum(ANCHOS[:-1]), 6, a_latin1(etiqueta), align="R")
        pdf.cell(ANCHOS[-1], 6, a_latin1(f"{moneda} {valor}"), align="R")
        pdf.ln()


def _comprobante(pdf: FPDF, comprobante: Mapping[str, Any]) -> None:
    pdf.ln(3)
    _escribir(pdf, "Comprobante referenciado (emitido por ARCA)", "B", 11)
    texto = (
        f"Factura {comprobante['tipo']} "
        f"{numero_impreso(comprobante['punto_venta'], comprobante['numero'])}"
        f" del {date.fromisoformat(comprobante['fecha_emision']):%d/%m/%Y}"
    )
    if comprobante.get("cae"):
        texto += f" - CAE {comprobante['cae']}"
    _escribir(pdf, texto)


def renderizar(snapshot: Mapping[str, Any]) -> bytes:
    """Los bytes del PDF. Mismo snapshot, mismo contenido."""
    contenido = snapshot["borrador"]["contenido"]
    aprobacion = snapshot["aprobacion"]
    pdf = _Documento(pie(aprobacion))
    pdf.set_creation_date(datetime.fromisoformat(aprobacion["momento"]))
    pdf.set_title(a_latin1(contenido["encabezado"]))
    pdf.set_creator("Malka Suite")
    pdf.set_auto_page_break(auto=True, margin=22)
    pdf.add_page()

    _escribir(pdf, contenido["encabezado"], "B", 14)
    pdf.ln(2)
    _cuerpo(pdf, contenido["cuerpo"])
    _detalle(pdf, snapshot["pedido"])
    if contenido["notas"]:
        pdf.ln(3)
        _escribir(pdf, "Notas", "B", 11)
        _cuerpo(pdf, contenido["notas"])
    if snapshot["comprobante"] is not None:
        _comprobante(pdf, snapshot["comprobante"])
    return bytes(pdf.output())
