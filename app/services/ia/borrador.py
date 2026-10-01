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
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from app.services.ia.cliente import MODELO, ClienteIA, pedir_al_modelo
from app.services.ia.prompts import cargar_prompt

logger = logging.getLogger("malka")

PROMPT_VERSION = "borrador_v1"

# Campos fiscales que el modelo nunca escribe: los calcula el sistema.
# Se buscan como palabra completa para no rechazar palabras que los
# contienen (totalidad) pero fallando hacia el lado seguro: si el modelo
# los menciona, no se persiste la salida.
CAMPOS_PROHIBIDOS = ("numero_comprobante", "cae", "total", "iva", "tipo_cambio")

_PATRON_PROHIBIDOS = re.compile(
    r"\b(?:" + "|".join(re.escape(campo) for campo in CAMPOS_PROHIBIDOS) + r")\b",
    re.IGNORECASE,
)


class SalidaInvalida(Exception):
    """La salida del modelo no paso la validacion y no se persiste."""


class BorradorValidado(BaseModel):
    """Esquema de la salida del modelo: solo prosa, nada de numeros.

    El contrato definitivo sale del brief del #40; estos campos son el
    minimo para arrancar el circuito.
    """

    encabezado: str
    cuerpo: str
    notas: str = ""


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


def validar_salida(texto: str) -> BorradorValidado:
    """Valida la salida cruda del modelo contra el esquema.

    Falla cerrado: cualquier problema (campos fiscales, JSON invalido,
    esquema incompleto) levanta SalidaInvalida y nada se persiste.
    """
    if mencion := _PATRON_PROHIBIDOS.search(texto):
        raise SalidaInvalida(
            f"la salida menciona un campo fiscal prohibido ({mencion.group(0)})"
        )

    inicio, fin = texto.find("{"), texto.rfind("}")
    if inicio == -1 or fin <= inicio:
        raise SalidaInvalida("la salida no contiene un objeto JSON")

    try:
        datos = json.loads(texto[inicio : fin + 1])
    except json.JSONDecodeError as error:
        raise SalidaInvalida(f"la salida no es JSON valido ({error.msg})") from error

    try:
        return BorradorValidado.model_validate(datos)
    except ValidationError as error:
        raise SalidaInvalida(
            f"la salida no cumple el esquema ({error.error_count()} errores)"
        ) from error


def generar_borrador(cliente: ClienteIA, contexto: str) -> ResultadoBorrador:
    """Genera el borrador: una llamada, un reintento, y rechazo si sigue mal.

    El contexto lo arma el worker leyendo la base cuando exista #19; aca
    entra como texto ya resuelto para poder probar la logica sin AWS.
    """
    prompt_sistema = cargar_prompt(PROMPT_VERSION)
    respuesta = pedir_al_modelo(cliente, prompt_sistema, contexto)
    try:
        salida = validar_salida(respuesta.texto)
    except SalidaInvalida as error:
        logger.warning("salida invalida del modelo, se reintenta: %s", error)
        reintento = pedir_al_modelo(
            cliente,
            prompt_sistema,
            f"{contexto}\n\n"
            f"La respuesta anterior fue invalida: {error}. "
            "Respondé solo el JSON pedido, sin los campos fiscales.",
        )
        try:
            salida = validar_salida(reintento.texto)
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
