"""Borradores con IA de punta a punta (#40): pedirlos, generarlos, leerlos.

Pedir (API): valida el pedido, reserva la llamada (limite del #45), deja el
borrador en `generando` y lo encola. El mensaje lleva solo ids.

Generar (worker): lee el contexto de la base (el modelo no descubre datos),
llama al modelo FUERA de una transaccion, valida con generar_borrador y
guarda `borrador` o `rechazado` con sus metricas. Un mensaje repetido (SQS
entrega al menos una vez) encuentra el borrador ya resuelto y no hace nada.

Ninguna consulta filtra por tenant_id: RLS se encarga (ADR 0009).
"""

from __future__ import annotations

import importlib
import json
import logging
import os
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Row, text
from sqlalchemy.orm import Session

from app.db import sesion_de_tenant
from app.errores import NoEncontradoError, ValidacionError
from app.paginacion import Pagina, ParametrosDePagina, armar_pagina, valores_del_cursor
from app.services.ia import limites
from app.services.ia.borrador import BorradorValidado, MetricasIA, generar_borrador
from app.services.ia.cliente import ClienteIA, RespuestaIA

logger = logging.getLogger("malka")


class EstadoBorrador(StrEnum):
    GENERANDO = "generando"
    BORRADOR = "borrador"
    RECHAZADO = "rechazado"


class MotivoRechazo(StrEnum):
    SALIDA_INVALIDA = "salida_invalida"
    ERROR_MODELO = "error_modelo"
    COLA_NO_DISPONIBLE = "cola_no_disponible"


class BorradorRespuesta(BaseModel):
    """Lo que la API devuelve de un borrador. Las metricas quedan adentro."""

    id: UUID
    pedido_id: UUID
    estado: EstadoBorrador
    contenido: BorradorValidado | None
    motivo_rechazo: MotivoRechazo | None
    version_prompt: str | None
    creado_en: datetime
    actualizado_en: datetime


class Cola(Protocol):
    """Lo unico que se usa de SQS: mandar un mensaje."""

    def enviar(self, cuerpo: dict[str, Any]) -> None: ...


class ColaSqs:
    """La cola de documentos real (COLA_DOCUMENTOS_URL, la carga Terraform)."""

    def __init__(self) -> None:
        self.url = os.getenv("COLA_DOCUMENTOS_URL", "")
        boto3 = importlib.import_module("boto3")
        self.cliente = boto3.client("sqs")

    def enviar(self, cuerpo: dict[str, Any]) -> None:
        if not self.url:
            raise RuntimeError("COLA_DOCUMENTOS_URL no esta configurada")
        self.cliente.send_message(QueueUrl=self.url, MessageBody=json.dumps(cuerpo))


EXISTE_PEDIDO = text("SELECT 1 FROM pedido WHERE id = :id")

INSERTAR = text(
    "INSERT INTO borrador (pedido_id, llamada_ia_id, solicitado_por) "
    "VALUES (:pedido_id, :llamada, :solicitado_por) "
    "RETURNING id, pedido_id, estado, contenido, motivo_rechazo, version_prompt, "
    "creado_en, actualizado_en"
)

OBTENER = text(
    "SELECT id, pedido_id, estado, contenido, motivo_rechazo, version_prompt, "
    "creado_en, actualizado_en FROM borrador WHERE id = :id"
)

LISTAR = text(
    "SELECT id, pedido_id, estado, contenido, motivo_rechazo, version_prompt, "
    "creado_en, actualizado_en FROM borrador "
    "WHERE (CAST(:pedido_id AS uuid) IS NULL OR pedido_id = :pedido_id) "
    "AND (:sin_cursor OR (creado_en, id) < "
    "(CAST(:c_creado AS timestamptz), CAST(:c_id AS uuid))) "
    "ORDER BY creado_en DESC, id DESC LIMIT :limite"
)

# Solo un borrador en generando se resuelve: lo bloquea para que dos
# mensajes del mismo borrador no lo procesen a la vez.
BLOQUEAR_GENERANDO = text(
    "SELECT llamada_ia_id FROM borrador "
    "WHERE id = :id AND estado = 'generando' FOR UPDATE"
)

# El contexto que ve el modelo: lo arma el sistema desde la base.
LEER_PEDIDO = text(
    "SELECT p.moneda, p.destino_pais, p.incoterm, p.fecha_entrega_solicitada, "
    "c.nombre AS cliente, c.pais "
    "FROM borrador b JOIN pedido p ON p.id = b.pedido_id "
    "JOIN cliente c ON c.id = p.cliente_id WHERE b.id = :id"
)

LEER_LINEAS = text(
    "SELECT pi.cantidad, pi.precio_unitario, iv.nombre AS item, r.nombre AS raza "
    "FROM borrador b JOIN pedido_item pi ON pi.pedido_id = b.pedido_id "
    "JOIN item_vendible iv ON iv.id = pi.item_vendible_id "
    "JOIN raza r ON r.id = pi.raza_id WHERE b.id = :id ORDER BY pi.numero_linea"
)

RESOLVER = text(
    "UPDATE borrador SET estado = :estado, contenido = CAST(:contenido AS jsonb), "
    "motivo_rechazo = :motivo, modelo = :modelo, version_prompt = :version, "
    "tokens_entrada = :entrada, tokens_salida = :salida, latencia_ms = :latencia, "
    "reintentos = :reintentos, resultado_validacion = :resultado, "
    "actualizado_en = now() WHERE id = :id AND estado = 'generando'"
)


def _no_encontrado() -> NoEncontradoError:
    return NoEncontradoError("No existe el borrador.", code="borrador_no_encontrado")


def _a_respuesta(fila: Row[Any]) -> BorradorRespuesta:
    return BorradorRespuesta.model_validate(dict(fila._mapping))


# --- API ---


def pedir(sesion: Session, pedido_id: UUID, solicitado_por: str) -> BorradorRespuesta:
    """Deja el borrador en generando, con su llamada reservada (o 429)."""
    if sesion.execute(EXISTE_PEDIDO, {"id": pedido_id}).first() is None:
        raise ValidacionError("No existe el pedido.", code="pedido_inexistente")
    llamada = limites.reservar_llamada(sesion, "borrador")
    fila = sesion.execute(
        INSERTAR,
        {"pedido_id": pedido_id, "llamada": llamada, "solicitado_por": solicitado_por},
    ).one()
    return _a_respuesta(fila)


def encolar(cola: Cola, tenant_id: UUID, borrador_id: UUID) -> None:
    """Corre despues del commit (background task). Si la cola falla, el
    borrador queda rechazado en lugar de en generando para siempre."""
    try:
        cola.enviar({"borrador_id": str(borrador_id), "tenant_id": str(tenant_id)})
    except Exception:
        logger.exception("no se pudo encolar el borrador id=%s", borrador_id)
        with sesion_de_tenant(tenant_id) as sesion:
            _resolver(
                sesion,
                borrador_id,
                EstadoBorrador.RECHAZADO,
                motivo=MotivoRechazo.COLA_NO_DISPONIBLE,
            )


def obtener(sesion: Session, borrador_id: UUID) -> BorradorRespuesta:
    fila = sesion.execute(OBTENER, {"id": borrador_id}).one_or_none()
    if fila is None:
        raise _no_encontrado()
    return _a_respuesta(fila)


def listar(
    sesion: Session, pagina: ParametrosDePagina, *, pedido_id: UUID | None = None
) -> Pagina[BorradorRespuesta]:
    c_creado, c_id = valores_del_cursor(pagina, (datetime.fromisoformat, UUID))
    filas = sesion.execute(
        LISTAR,
        {
            "pedido_id": pedido_id,
            "sin_cursor": pagina.cursor is None,
            "c_creado": c_creado,
            "c_id": c_id,
            "limite": pagina.limite + 1,
        },
    )
    return armar_pagina(
        [_a_respuesta(f) for f in filas],
        pagina.limite,
        lambda b: (b.creado_en.isoformat(), b.id),
    )


# --- worker ---


def _numero(valor: Decimal) -> str:
    return format(valor.normalize(), "f")


def _contexto(sesion: Session, borrador_id: UUID) -> str:
    pedido = sesion.execute(LEER_PEDIDO, {"id": borrador_id}).one()
    lineas = sesion.execute(LEER_LINEAS, {"id": borrador_id}).all()
    fecha: date = pedido.fecha_entrega_solicitada
    renglones = [
        f"Cliente: {pedido.cliente} ({pedido.pais})",
        f"Moneda del pedido: {pedido.moneda}",
        f"Entrega solicitada: {fecha.isoformat()}",
    ]
    if pedido.destino_pais:
        renglones.append(
            f"Exportacion a {pedido.destino_pais}"
            + (f", incoterm {pedido.incoterm}" if pedido.incoterm else "")
        )
    renglones.append("Lineas:")
    renglones += [
        f"- {linea.cantidad} x {linea.item}, raza {linea.raza}, a "
        f"{_numero(linea.precio_unitario)} {pedido.moneda} cada una"
        for linea in lineas
    ]
    return "\n".join(renglones)


def _resolver(
    sesion: Session,
    borrador_id: UUID,
    estado: EstadoBorrador,
    *,
    contenido: BorradorValidado | None = None,
    motivo: MotivoRechazo | None = None,
    metricas: MetricasIA | None = None,
) -> None:
    sesion.execute(
        RESOLVER,
        {
            "id": borrador_id,
            "estado": estado.value,
            "contenido": contenido.model_dump_json() if contenido else None,
            "motivo": motivo.value if motivo else None,
            "modelo": metricas.modelo if metricas else None,
            "version": metricas.version_prompt if metricas else None,
            "entrada": metricas.tokens_entrada if metricas else None,
            "salida": metricas.tokens_salida if metricas else None,
            "latencia": metricas.latencia_ms if metricas else None,
            "reintentos": metricas.reintentos if metricas else None,
            "resultado": metricas.resultado_validacion if metricas else None,
        },
    )


def procesar(mensaje: dict[str, Any], modelo: ClienteIA) -> None:
    """Resuelve un borrador encolado. Idempotente: si ya no esta en
    generando (mensaje repetido), no hace nada."""
    tenant_id = UUID(str(mensaje["tenant_id"]))
    borrador_id = UUID(str(mensaje["borrador_id"]))

    # 1. Leer el contexto, en una transaccion corta.
    with sesion_de_tenant(tenant_id) as sesion:
        pendiente = sesion.execute(BLOQUEAR_GENERANDO, {"id": borrador_id}).first()
        if pendiente is None:
            logger.info("borrador ya resuelto o inexistente id=%s", borrador_id)
            return
        contexto = _contexto(sesion, borrador_id)
        llamada_ia_id: UUID | None = pendiente.llamada_ia_id

    # 2. El modelo, fuera de toda transaccion: puede tardar segundos.
    try:
        resultado = generar_borrador(modelo, contexto)
    except Exception:
        logger.exception("el modelo fallo para el borrador id=%s", borrador_id)
        with sesion_de_tenant(tenant_id) as sesion:
            _resolver(
                sesion,
                borrador_id,
                EstadoBorrador.RECHAZADO,
                motivo=MotivoRechazo.ERROR_MODELO,
            )
        return

    # 3. Guardar el resultado y anotar el consumo en el limite de IA.
    with sesion_de_tenant(tenant_id) as sesion:
        if resultado.estado == "borrador":
            _resolver(
                sesion,
                borrador_id,
                EstadoBorrador.BORRADOR,
                contenido=resultado.salida,
                metricas=resultado.metricas,
            )
        else:
            _resolver(
                sesion,
                borrador_id,
                EstadoBorrador.RECHAZADO,
                motivo=MotivoRechazo.SALIDA_INVALIDA,
                metricas=resultado.metricas,
            )
        if llamada_ia_id is not None and resultado.metricas is not None:
            limites.registrar_consumo(
                sesion,
                llamada_ia_id,
                RespuestaIA(
                    texto="",
                    tokens_entrada=resultado.metricas.tokens_entrada,
                    tokens_salida=resultado.metricas.tokens_salida,
                    latencia_ms=resultado.metricas.latencia_ms,
                ),
            )
