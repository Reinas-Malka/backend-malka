"""El asistente conversacional: Bedrock Converse con tool use.

Un pedido = un historial que manda el front (el servidor no guarda nada).
El bucle llama al modelo; si pide herramientas, las ejecuta en el servidor
(lectura con RLS, o propuestas que no escriben) y le devuelve los
resultados delimitados como datos; si responde texto, termina.

Topes, porque API Gateway corta a los 29 s y Bedrock cobra por token:

- MAX_RONDAS_DE_HERRAMIENTAS rondas de herramientas por pedido.
- PRESUPUESTO_S en total: una llamada nueva solo arranca si su tiempo
  maximo (TIEMPO_MAXIMO_LLAMADA_S, el timeout del cliente) entra en lo que
  queda. Si no, responde con lo que haya y un aviso.
- Cada llamada a Converse reserva su lugar en el limite de IA del criadero
  (limites.py: 429 con Retry-After) y anota sus tokens despues, con la
  metrica EMF. Las reservas van en transacciones cortas: el modelo se llama
  fuera de toda transaccion, como en el worker.

Los logs no llevan el contenido de los mensajes: solo tamanios, herramientas,
tokens y latencia (AGENTS.md, regla 7).
"""

from __future__ import annotations

import importlib
import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from app.auth import Identidad
from app.db import sesion_de_tenant
from app.errores import ErrorDeApi, ValidacionError
from app.fechas import hoy
from app.schemas.asistente import (
    Consulta,
    MensajeAsistente,
    Propuesta,
    RespuestaAsistente,
    RolMensaje,
)
from app.services.ia import limites
from app.services.ia.asistente import herramientas
from app.services.ia.asistente.resolucion import ErrorDeHerramienta
from app.services.ia.borrador import _como_datos
from app.services.ia.cliente import MODELO, REGION, TEMPERATURA, ClienteIA, RespuestaIA
from app.services.ia.prompts import cargar_prompt

logger = logging.getLogger("malka")

# v1 (08/11): primera version del asistente con herramientas.
PROMPT_VERSION = "asistente_v1"

MAX_RONDAS_DE_HERRAMIENTAS = 4
# Respuestas breves: con 1000 tokens sobra y una respuesta desbocada se corta.
MAX_TOKENS_ASISTENTE = 1000

# API Gateway corta a los 29 s; el resto es margen para la base y la red.
PRESUPUESTO_S = 22.0
# Timeouts del cliente de Bedrock del asistente, sin reintentos: asi una
# llamada nunca dura mas que esto y el presupuesto se puede respetar.
TIMEOUT_CONEXION_S = 2
TIMEOUT_LECTURA_S = 9
TIEMPO_MAXIMO_LLAMADA_S = TIMEOUT_CONEXION_S + TIMEOUT_LECTURA_S

# PENDIENTE: el CHECK ck_llamada_ia_motivo (migracion 0015) solo admite
# "borrador". Sumar "asistente" necesita una migracion, que queda para
# despues de la 0018 (#154) para no abrir dos cabezas. Mientras tanto las
# llamadas del asistente cuentan en el mismo limite con ese motivo.
MOTIVO_LIMITE = "borrador"

AVISO_RONDAS = (
    "(Corté la búsqueda: hacía falta consultar más datos de los que puedo en "
    "una sola respuesta. Probá con una pregunta más concreta.)"
)
AVISO_TIEMPO = (
    "(Corté la búsqueda para no pasarme de tiempo. Probá con una pregunta más "
    "concreta.)"
)
SIN_RESPUESTA = "No pude armar una respuesta. Probá reformular la pregunta."


class AsistenteNoDisponibleError(ErrorDeApi):
    """Bedrock no respondio o respondio algo inutilizable (503).

    El detalle va al log, nunca al cliente.
    """

    status_code = 503
    codigo_por_defecto = "asistente_no_disponible"


def _no_disponible() -> AsistenteNoDisponibleError:
    return AsistenteNoDisponibleError(
        "El asistente no está disponible en este momento. Probá de nuevo en "
        "unos minutos."
    )


def crear_cliente_asistente() -> ClienteIA:
    """bedrock-runtime con timeouts cortos y sin reintentos (ver arriba)."""
    try:
        boto3 = importlib.import_module("boto3")
        config = importlib.import_module("botocore.config").Config(
            connect_timeout=TIMEOUT_CONEXION_S,
            read_timeout=TIMEOUT_LECTURA_S,
            retries={"max_attempts": 1, "mode": "standard"},
        )
        cliente = boto3.client("bedrock-runtime", region_name=REGION, config=config)
    except Exception:
        logger.exception("no se pudo crear el cliente de Bedrock del asistente")
        raise _no_disponible() from None
    return cast("ClienteIA", cliente)


@dataclass
class _Estado:
    """Lo que se va juntando durante el bucle."""

    consultas: list[Consulta] = field(default_factory=list)
    propuestas: list[Propuesta] = field(default_factory=list)
    herramientas: list[str] = field(default_factory=list)
    llamadas: int = 0
    tokens_entrada: int = 0
    tokens_salida: int = 0


def _historial(mensajes: Sequence[MensajeAsistente]) -> list[dict[str, Any]]:
    """El historial del front en el formato de Converse.

    Converse exige que empiece el usuario y que los roles se alternen: los
    mensajes seguidos del mismo rol se juntan y un saludo inicial del
    asistente se descarta. El ultimo tiene que ser del usuario.
    """
    turnos: list[tuple[str, list[str]]] = []
    for mensaje in mensajes:
        rol = "user" if mensaje.rol == RolMensaje.USUARIO else "assistant"
        if not turnos and rol == "assistant":
            continue
        if turnos and turnos[-1][0] == rol:
            turnos[-1][1].append(mensaje.texto)
        else:
            turnos.append((rol, [mensaje.texto]))
    if not turnos or turnos[-1][0] != "user":
        raise ValidacionError(
            "El último mensaje tiene que ser del usuario.",
            code="ultimo_mensaje_no_es_del_usuario",
        )
    return [
        {"role": rol, "content": [{"text": "\n\n".join(textos)}]}
        for rol, textos in turnos
    ]


def _sistema() -> list[dict[str, str]]:
    # La fecha va aparte del prompt versionado: "ayer" o "el lunes" se
    # resuelven con el dia de Argentina, no con el de UTC.
    return [
        {"text": cargar_prompt(PROMPT_VERSION)},
        {"text": f"Hoy es {hoy().isoformat()} (fecha de Argentina)."},
    ]


def _llamar(
    modelo: ClienteIA,
    identidad: Identidad,
    historial: list[dict[str, Any]],
    estado: _Estado,
    reloj: Callable[[], float],
) -> tuple[list[dict[str, Any]], str]:
    """Una llamada a Converse con su reserva en el limite del criadero."""
    with sesion_de_tenant(identidad.tenant_id) as sesion:
        llamada = limites.reservar_llamada(sesion, MOTIVO_LIMITE)

    comienzo = reloj()
    try:
        respuesta = modelo.converse(
            modelId=MODELO,
            system=_sistema(),
            messages=historial,
            inferenceConfig={
                "temperature": TEMPERATURA,
                "maxTokens": MAX_TOKENS_ASISTENTE,
            },
            toolConfig={"tools": herramientas.especificaciones()},
        )
        contenido = list(respuesta["output"]["message"]["content"])
        motivo = str(respuesta.get("stopReason", ""))
        uso = respuesta.get("usage", {})
        tokens_entrada = int(uso.get("inputTokens", 0))
        tokens_salida = int(uso.get("outputTokens", 0))
    except Exception as error:
        # Sin el mensaje: puede traer partes del pedido. El tipo alcanza.
        logger.warning(
            "bedrock fallo en el asistente",
            extra={"campos": {"error": type(error).__name__}},
        )
        raise _no_disponible() from None
    latencia_ms = round((reloj() - comienzo) * 1000)

    estado.llamadas += 1
    estado.tokens_entrada += tokens_entrada
    estado.tokens_salida += tokens_salida
    with sesion_de_tenant(identidad.tenant_id) as sesion:
        limites.registrar_consumo(
            sesion,
            llamada,
            RespuestaIA(
                texto="",
                tokens_entrada=tokens_entrada,
                tokens_salida=tokens_salida,
                latencia_ms=latencia_ms,
            ),
        )
    return contenido, motivo


def _resultado(tool_use_id: str, datos: Any, *, error: bool) -> dict[str, Any]:
    """El resultado de una herramienta, delimitado como dato (no instruccion)."""
    cuerpo = json.dumps(datos, default=str, ensure_ascii=False)
    return {
        "toolResult": {
            "toolUseId": tool_use_id,
            "content": [{"text": _como_datos(cuerpo)}],
            "status": "error" if error else "success",
        }
    }


def _usar(uso: dict[str, Any], identidad: Identidad, estado: _Estado) -> dict[str, Any]:
    nombre = str(uso.get("name", ""))
    tool_use_id = str(uso.get("toolUseId", ""))
    estado.herramientas.append(nombre)
    try:
        herramienta, resultado = herramientas.ejecutar(
            nombre,
            uso.get("input", {}),
            tenant_id=identidad.tenant_id,
            grupos=identidad.grupos,
        )
    except ErrorDeHerramienta as error:
        return _resultado(tool_use_id, {"error": str(error)}, error=True)

    if resultado.propuesta is not None:
        estado.propuestas.append(resultado.propuesta)
    else:
        consulta = Consulta(tool=herramienta.nombre, resumen=resultado.resumen)
        if consulta not in estado.consultas:
            estado.consultas.append(consulta)
    return _resultado(tool_use_id, resultado.datos, error=False)


def _texto(contenido: Sequence[dict[str, Any]]) -> str:
    partes = [str(b["text"]).strip() for b in contenido if "text" in b]
    return "\n\n".join(p for p in partes if p)


def conversar(
    mensajes: Sequence[MensajeAsistente],
    *,
    modelo: ClienteIA,
    identidad: Identidad,
    reloj: Callable[[], float] = time.monotonic,
) -> RespuestaAsistente:
    """Responde el ultimo mensaje del usuario, con herramientas si hacen falta."""
    inicio = reloj()
    historial = _historial(mensajes)
    estado = _Estado()
    texto = ""
    aviso: str | None = None
    fin = "respuesta"

    for ronda in range(MAX_RONDAS_DE_HERRAMIENTAS + 1):
        if ronda > 0 and reloj() - inicio + TIEMPO_MAXIMO_LLAMADA_S > PRESUPUESTO_S:
            aviso, fin = AVISO_TIEMPO, "tiempo"
            break
        contenido, motivo = _llamar(modelo, identidad, historial, estado, reloj)
        texto = _texto(contenido)
        usos = [b["toolUse"] for b in contenido if "toolUse" in b]
        if motivo != "tool_use" or not usos:
            break
        if ronda == MAX_RONDAS_DE_HERRAMIENTAS:
            aviso, fin = AVISO_RONDAS, "rondas"
            break
        historial.append({"role": "assistant", "content": contenido})
        historial.append(
            {"role": "user", "content": [_usar(u, identidad, estado) for u in usos]}
        )

    final = "\n\n".join(p for p in (texto, aviso) if p) or SIN_RESPUESTA
    logger.info(
        "asistente respondio",
        extra={
            "campos": {
                "mensajes": len(mensajes),
                "caracteres": sum(len(m.texto) for m in mensajes),
                "llamadas": estado.llamadas,
                "herramientas": estado.herramientas,
                "propuestas": len(estado.propuestas),
                "consumo_entrada": estado.tokens_entrada,
                "consumo_salida": estado.tokens_salida,
                "latencia_ms": round((reloj() - inicio) * 1000),
                "fin": fin,
                "version_prompt": PROMPT_VERSION,
            }
        },
    )
    return RespuestaAsistente(
        texto=final, consultas=estado.consultas, propuestas=estado.propuestas
    )
