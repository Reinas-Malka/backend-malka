"""Borrador de documento generado por la IA (#40).

La IA redacta, el codigo calcula: los valores fiscales los determina el
sistema, asi que cualquier salida del modelo que los incluya se rechaza
antes de persistir nada. El flujo es una llamada, un unico reintento con
el error en el prompt, y rechazo con motivo salida_invalida.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.services.ia.cliente import MODELO, ClienteIA, pedir_al_modelo
from app.services.ia.prompts import cargar_prompt

logger = logging.getLogger("malka")

# v2 (29/10): el contexto va entre <datos> y no se obedece, y no se escriben
# montos ni numeros que no esten en los datos.
PROMPT_VERSION = "borrador_v2"

# Campos fiscales que el modelo nunca escribe: se transcriben del comprobante
# que emite ARCA. El prompt los nombra; el filtro de abajo busca sus VALORES,
# no la palabra suelta ("la cantidad total de reinas" es prosa valida).
CAMPOS_PROHIBIDOS = ("numero_comprobante", "cae", "total", "iva", "tipo_cambio")

# Lo que delata un valor fiscal en la prosa, sin depender de la palabra
# exacta. Se busca sobre el texto sin tildes y en minusculas, salvo el CAE.
_PATRONES_FISCALES = (
    # Un monto: moneda pegada a un numero ("USD 12.600", "$ 1500", "1500 EUR").
    re.compile(r"(?:\b(?:usd|us\$|eur|ars)|[$€])\s?\d|\d\s?(?:usd|eur|ars|€)\b"),
    re.compile(r"\btipo[\s_]+de[\s_]+cambio\b|\btipo_cambio\b|\bcotizacion\b"),
    re.compile(r"\bnumero[\s_]+(?:de[\s_]+)?comprobante\b"),
    # Punto de venta y numero como se imprimen: 0003-00001234.
    re.compile(r"\b\d{4,5}-\d{8}\b"),
    re.compile(r"\d\s?%"),
    # IVA como palabra o pegado a una alicuota ("iva21").
    re.compile(r"\biva(?:\b|\d)"),
)
# El CAE en mayusculas (en minusculas "cae" es un verbo) o 14 digitos juntos.
_PATRON_CAE = re.compile(r"\bCAE\b|\b\d{14}\b")

# Numeros con separadores de miles o decimales ("35.000,50" = "35000.50").
_PATRON_NUMERO = re.compile(r"\d(?:[\d.,]*\d)?")


class SalidaInvalida(Exception):
    """La salida del modelo no paso la validacion y no se persiste."""


class BorradorValidado(BaseModel):
    """Esquema de la salida del modelo: solo prosa, nada de numeros.

    Estricto: un campo de mas (un "total", un "cbu") se rechaza, y nada viene
    vacio ni desbordado. El contrato definitivo sale del brief del #40.
    """

    model_config = ConfigDict(extra="forbid")

    encabezado: str = Field(min_length=1, max_length=200)
    cuerpo: str = Field(min_length=1, max_length=4000)
    notas: str = Field(default="", max_length=1000)


@dataclass(frozen=True)
class MetricasIA:
    """Lo que hay que registrar de cada generacion (issue #40)."""

    modelo: str
    version_prompt: str
    tokens_entrada: int
    tokens_salida: int
    latencia_ms: int
    reintentos: int
    resultado_validacion: str


@dataclass(frozen=True)
class ResultadoBorrador:
    """Estado final de la generacion: borrador listo o rechazado."""

    estado: str  # "borrador" | "rechazado"
    salida: BorradorValidado | None
    motivo_rechazo: str | None = None
    metricas: MetricasIA | None = None


def _sumar_metricas(
    version_prompt: str, respuestas: list[Any], reintentos: int, resultado: str
) -> MetricasIA:
    return MetricasIA(
        modelo=MODELO,
        version_prompt=version_prompt,
        tokens_entrada=sum(r.tokens_entrada for r in respuestas),
        tokens_salida=sum(r.tokens_salida for r in respuestas),
        latencia_ms=sum(r.latencia_ms for r in respuestas),
        reintentos=reintentos,
        resultado_validacion=resultado,
    )


def _sin_tildes(texto: str) -> str:
    descompuesto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in descompuesto if not unicodedata.combining(c))


def _numeros(texto: str) -> set[str]:
    """Los numeros del texto sin separadores: "35.000,50" y "35000.50" son
    el mismo, y una fecha 2026-10-15 aporta 2026, 10 y 15."""
    return {re.sub(r"[.,]", "", n) for n in _PATRON_NUMERO.findall(texto)}


def _validar_prosa(salida: BorradorValidado, contexto: str) -> None:
    """Ningun valor fiscal y ningun numero que no venga del contexto."""
    prosa = " ".join((salida.encabezado, salida.cuerpo, salida.notas))
    if mencion := _PATRON_CAE.search(prosa):
        raise SalidaInvalida(f"la salida incluye un CAE ({mencion.group(0)})")
    normalizada = _sin_tildes(prosa).lower()
    for patron in _PATRONES_FISCALES:
        if mencion := patron.search(normalizada):
            raise SalidaInvalida(
                f"la salida incluye un valor fiscal ({mencion.group(0)})"
            )
    # Un numero que el modelo invento (una cantidad, un precio, un total) no
    # se persiste: solo los que estan en los datos que le dio el sistema.
    if inventados := _numeros(prosa) - _numeros(contexto):
        raise SalidaInvalida(
            "la salida tiene numeros que no estan en el contexto "
            f"({sorted(inventados)})"
        )


def validar_salida(texto: str, contexto: str = "") -> BorradorValidado:
    """Valida la salida cruda del modelo contra el esquema y el contexto.

    Falla cerrado: cualquier problema (JSON invalido, esquema incompleto o
    con campos de mas, valores fiscales, numeros inventados) levanta
    SalidaInvalida y nada se persiste.
    """
    inicio, fin = texto.find("{"), texto.rfind("}")
    if inicio == -1 or fin <= inicio:
        raise SalidaInvalida("la salida no contiene un objeto JSON")

    try:
        datos = json.loads(texto[inicio : fin + 1])
    except json.JSONDecodeError as error:
        raise SalidaInvalida(f"la salida no es JSON valido ({error.msg})") from error

    try:
        salida = BorradorValidado.model_validate(datos)
    except ValidationError as error:
        raise SalidaInvalida(
            f"la salida no cumple el esquema ({error.error_count()} errores)"
        ) from error
    _validar_prosa(salida, contexto)
    return salida


def _como_datos(contexto: str) -> str:
    """El contexto delimitado: el prompt v2 dice que es dato, no instruccion.

    Se saca cualquier etiqueta <datos> que venga adentro, asi un nombre de
    cliente no puede cerrar el bloque y escribir instrucciones afuera.
    """
    limpio = re.sub(r"</?datos>", "", contexto, flags=re.IGNORECASE)
    return f"<datos>\n{limpio}\n</datos>"


def generar_borrador(cliente: ClienteIA, contexto: str) -> ResultadoBorrador:
    """Genera el borrador: una llamada, un reintento, y rechazo si sigue mal.

    El contexto lo arma el worker leyendo la base cuando exista #19; aca
    entra como texto ya resuelto para poder probar la logica sin AWS.
    """
    prompt_sistema = cargar_prompt(PROMPT_VERSION)
    datos = _como_datos(contexto)
    respuesta = pedir_al_modelo(cliente, prompt_sistema, datos)
    try:
        salida = validar_salida(respuesta.texto, contexto)
    except SalidaInvalida as error:
        logger.warning("salida invalida del modelo, se reintenta: %s", error)
        reintento = pedir_al_modelo(
            cliente,
            prompt_sistema,
            f"{datos}\n\n"
            f"La respuesta anterior fue invalida: {error}. "
            "Respondé solo el JSON pedido, sin los campos fiscales.",
        )
        try:
            salida = validar_salida(reintento.texto, contexto)
        except SalidaInvalida as segundo_error:
            logger.error(
                "segunda salida invalida, documento rechazado: %s", segundo_error
            )
            return ResultadoBorrador(
                estado="rechazado",
                salida=None,
                motivo_rechazo="salida_invalida",
                metricas=_sumar_metricas(
                    PROMPT_VERSION,
                    [respuesta, reintento],
                    reintentos=1,
                    resultado="rechazada",
                ),
            )
        logger.info("borrador generado tras un reintento")
        return ResultadoBorrador(
            estado="borrador",
            salida=salida,
            metricas=_sumar_metricas(
                PROMPT_VERSION,
                [respuesta, reintento],
                reintentos=1,
                resultado="valida_tras_reintento",
            ),
        )

    logger.info("borrador generado")
    return ResultadoBorrador(
        estado="borrador",
        salida=salida,
        metricas=_sumar_metricas(
            PROMPT_VERSION, [respuesta], reintentos=0, resultado="valida"
        ),
    )
