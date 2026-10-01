"""Cliente de Bedrock para el borrador de documentos (#40).

boto3 viene incluido en la imagen oficial de Lambda, pero no en local ni
en el CI: se carga solo cuando hace falta (mismo criterio que
app/config.py). Los tests inyectan su propio cliente a traves del
Protocol ClienteIA, asi este modulo no necesita AWS para probarse.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

REGION = "us-east-1"

# Modelo verificado en el #39 (inference profile de us-east-1).
MODELO = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# Temperatura baja: el borrador tiene que ser reproducible, no creativo.
TEMPERATURA = 0.0


@dataclass(frozen=True)
class RespuestaIA:
    """Lo que importa de una llamada al modelo, sin la respuesta cruda."""

    texto: str
    tokens_entrada: int
    tokens_salida: int
    latencia_ms: int


class ClienteIA(Protocol):
    """Lo unico que se usa de bedrock-runtime: converse."""

    def converse(
        self,
        *,
        modelId: str,
        system: Sequence[dict[str, Any]],
        messages: Sequence[dict[str, Any]],
        inferenceConfig: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]: ...


def crear_cliente() -> ClienteIA:
    """Crea el cliente real; solo funciona dentro de la VPC de AWS."""
    boto3 = importlib.import_module("boto3")
    return cast("ClienteIA", boto3.client("bedrock-runtime", region_name=REGION))


def pedir_al_modelo(
    cliente: ClienteIA, prompt_del_sistema: str, mensaje: str
) -> RespuestaIA:
    """Una llamada converse con temperatura baja, midiendo tokens y latencia."""
    comienzo = time.monotonic()
    respuesta = cliente.converse(
        modelId=MODELO,
        system=[{"text": prompt_del_sistema}],
        messages=[{"role": "user", "content": [{"text": mensaje}]}],
        inferenceConfig={"temperature": TEMPERATURA},
    )
    latencia_ms = round((time.monotonic() - comienzo) * 1000)

    uso = respuesta.get("usage", {})
    texto = respuesta["output"]["message"]["content"][0]["text"]
    return RespuestaIA(
        texto=str(texto),
        tokens_entrada=int(uso.get("inputTokens", 0)),
        tokens_salida=int(uso.get("outputTokens", 0)),
        latencia_ms=latencia_ms,
    )
