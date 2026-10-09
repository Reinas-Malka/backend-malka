"""Idempotencia por Idempotency-Key en los POST que crean recursos (#25).

El frontend manda un header Idempotency-Key (un UUID nuevo por cada operacion,
el mismo en cada reintento). Con la misma clave, dentro de las 24 horas:

- el mismo pedido devuelve la respuesta original guardada, sin crear nada;
- otro pedido (otro cuerpo u otra ruta) responde 409;
- pasadas las 24 horas, la clave se trata como nueva (docs/dominio.md).

La clave se reserva en la MISMA transaccion que crea el recurso (la que abre
obtener_sesion): o se guardan las dos cosas o ninguna. Si el endpoint falla, la
reserva se deshace con el resto y un reintento puede volver a intentar; por eso
solo se guardan las respuestas exitosas.

Dos pedidos simultaneos con la misma clave no crean dos recursos: el INSERT del
segundo espera a que el primero termine (indice unico) y despues repite su
respuesta. Como siempre, las consultas no filtran por tenant: lo hace RLS.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.errores import ConflictoError, ValidacionError

HEADER = "Idempotency-Key"
HEADER_REPETIDA = "Idempotent-Replayed"

# ASCII visible, sin espacios, hasta 255 caracteres (el CHECK de la 0007).
# Alcanza para un UUID y no deja meter saltos de linea ni texto arbitrario.
CLAVE_VALIDA = re.compile(r"[\x21-\x7e]{1,255}")

logger = logging.getLogger("malka.api")

# Si la clave ya existe, el INSERT no hace nada y no devuelve filas. Si otra
# transaccion la esta usando, espera a que termine antes de decidir.
RESERVAR = text(
    "INSERT INTO idempotencia (clave, hash_request) VALUES (:clave, :hash) "
    "ON CONFLICT ON CONSTRAINT uq_idempotencia_tenant_clave DO NOTHING "
    "RETURNING id"
)

# FOR UPDATE: si la clave vencio, solo un pedido la puede renovar.
EXISTENTE = text(
    "SELECT hash_request, status_code, respuesta, expira_en <= now() AS vencida "
    "FROM idempotencia WHERE clave = :clave FOR UPDATE"
)

RENOVAR = text(
    "UPDATE idempotencia SET hash_request = :hash, status_code = NULL, "
    "respuesta = NULL, creado_en = now(), expira_en = now() + interval '24 hours' "
    "WHERE clave = :clave"
)

GUARDAR = text(
    "UPDATE idempotencia SET status_code = :status_code, respuesta = :respuesta "
    "WHERE clave = :clave"
).bindparams(bindparam("respuesta", type_=JSONB))


@dataclass(frozen=True)
class PedidoIdempotente:
    """Lo que hace falta del request para decidir; clave None = sin header."""

    clave: str | None
    hash_request: str


def huella(metodo: str, ruta: str, cuerpo: bytes) -> str:
    """SHA-256 del pedido; el JSON se normaliza (espacios y orden no cuentan)."""
    try:
        normalizado = json.dumps(
            json.loads(cuerpo), sort_keys=True, separators=(",", ":")
        ).encode()
    except ValueError:
        normalizado = cuerpo
    return hashlib.sha256(f"{metodo} {ruta}\n".encode() + normalizado).hexdigest()


async def pedido_idempotente(
    request: Request,
    idempotency_key: str | None = Header(
        default=None,
        alias=HEADER,
        description="UUID nuevo por operacion; el mismo en cada reintento (24 h).",
    ),
) -> PedidoIdempotente:
    """Dependencia de los POST que crean recursos.

    No abre ninguna conexion: una clave mal formada se rechaza antes.
    """
    if idempotency_key is None:
        return PedidoIdempotente(clave=None, hash_request="")

    if not CLAVE_VALIDA.fullmatch(idempotency_key):
        raise ValidacionError(
            "La Idempotency-Key tiene que tener entre 1 y 255 caracteres "
            "visibles, sin espacios.",
            code="idempotency_key_invalida",
        )

    # FastAPI ya leyo el cuerpo para validarlo: esto lo devuelve de cache.
    cuerpo = await request.body()
    return PedidoIdempotente(
        clave=idempotency_key,
        hash_request=huella(request.method, request.url.path, cuerpo),
    )


def _respuesta_previa(
    sesion: Session, pedido: PedidoIdempotente
) -> JSONResponse | None:
    """Reserva la clave, o devuelve lo que corresponde si ya estaba usada.

    None significa "la clave es de este pedido: segui y crea el recurso".
    """
    parametros = {"clave": pedido.clave, "hash": pedido.hash_request}
    if sesion.execute(RESERVAR, parametros).first() is not None:
        return None

    fila = sesion.execute(EXISTENTE, parametros).one()

    if fila.vencida:
        sesion.execute(RENOVAR, parametros)
        return None

    if fila.hash_request != pedido.hash_request:
        raise ConflictoError(
            "La Idempotency-Key ya se uso con otro pedido. Para una operacion "
            "nueva, generar una clave nueva.",
            code="idempotency_key_reutilizada",
        )

    if fila.status_code is None:
        # No deberia pasar: el pedido original guarda su respuesta en la misma
        # transaccion en la que reserva la clave.
        raise ConflictoError(
            "El pedido original con esta Idempotency-Key sigue en curso.",
            code="idempotency_key_en_curso",
        )

    logger.info(
        "pedido idempotente repetido", extra={"campos": {"status": fila.status_code}}
    )
    return JSONResponse(
        status_code=fila.status_code,
        content=fila.respuesta,
        headers={HEADER_REPETIDA: "true"},
    )


def responder_una_vez(
    sesion: Session,
    pedido: PedidoIdempotente,
    crear: Callable[[], Any],
    status_code: int = 201,
) -> JSONResponse:
    """Ejecuta `crear` una sola vez por clave y guarda su respuesta.

    Sin header Idempotency-Key, se comporta como un POST comun.
    """
    if pedido.clave is not None:
        previa = _respuesta_previa(sesion, pedido)
        if previa is not None:
            return previa

    contenido = jsonable_encoder(crear())

    if pedido.clave is not None:
        sesion.execute(
            GUARDAR,
            {
                "clave": pedido.clave,
                "status_code": status_code,
                "respuesta": contenido,
            },
        )

    return JSONResponse(status_code=status_code, content=contenido)
