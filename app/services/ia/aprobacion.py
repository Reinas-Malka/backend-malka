"""Revision humana de un borrador: aprobarlo o descartarlo (#42).

docs/dominio.md, "Aprobacion de borradores": aprueba siempre una persona
(admin), nunca la IA, y la bitacora registra quien, cuando y sobre que
version. Lo aprobado se congela en un snapshot y se renderiza como PDF
interno: no es un comprobante fiscal (lo emite ARCA, "Regla general"). Si
se referencia un comprobante, se toma tal como se transcribio (#108).

Aprobar, en este orden: bloquear la fila (FOR UPDATE), validar estado,
version y comprobante, armar el snapshot, renderizar, subir a S3, y recien
ahi actualizar el borrador y escribir la bitacora en la misma transaccion.
Si S3 falla, 503 y nada cambia. Un aprobado o descartado no se toca mas: lo
garantiza ademas un trigger (migracion 0018).

Ninguna consulta filtra por tenant_id: RLS se encarga (ADR 0009).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Row, text
from sqlalchemy.orm import Session

from app.errores import ConflictoError, ValidacionError
from app.schemas.borradores import Aprobacion, Descarte, RevisionRespuesta
from app.services.documentos import almacenamiento
from app.services.documentos.almacenamiento import Subidor
from app.services.ia import generacion, pdf_borrador
from app.services.ia.borrador import BorradorValidado
from app.services.ia.generacion import BorradorRespuesta, EstadoBorrador
from app.services.ia.snapshot import armar_snapshot, hash_contenido, sha256_canonico

BLOQUEAR = text(
    "SELECT id, tenant_id, pedido_id, estado, contenido, modelo, version_prompt, "
    "corrige_a FROM borrador WHERE id = :id FOR UPDATE"
)

LEER_PEDIDO = text(
    "SELECT p.id, p.cliente_id, p.moneda, p.destino_pais, p.incoterm, "
    "p.fecha_entrega_solicitada, c.nombre AS cliente_nombre, "
    "c.pais AS cliente_pais, c.tipo AS cliente_tipo, "
    "c.condicion_iva AS cliente_condicion_iva, "
    "c.cuit_o_tax_id AS cliente_cuit_o_tax_id "
    "FROM pedido p JOIN cliente c ON c.id = p.cliente_id WHERE p.id = :id"
)

LEER_LINEAS = text(
    "SELECT pi.numero_linea, pi.cantidad, iv.nombre AS item, r.nombre AS raza, "
    "pi.precio_unitario, pi.alicuota "
    "FROM pedido_item pi JOIN item_vendible iv ON iv.id = pi.item_vendible_id "
    "JOIN raza r ON r.id = pi.raza_id WHERE pi.pedido_id = :id "
    "ORDER BY pi.numero_linea"
)

# Todas las columnas, tal como se transcribieron.
LEER_COMPROBANTE = text(
    "SELECT id, tipo, cliente_id, punto_venta, numero, fecha_emision, moneda, "
    "importe, tipo_cambio, archivo_s3_key, cargado_por, creado_en "
    "FROM comprobante WHERE id = :id"
)

APROBAR = text(
    "UPDATE borrador SET estado = 'aprobado', snapshot = CAST(:snapshot AS jsonb), "
    "hash_contenido = :hash_contenido, pdf_s3_key = :pdf_s3_key, "
    "comprobante_id = :comprobante_id, actualizado_en = now() "
    "WHERE id = :id AND estado = 'borrador'"
)

DESCARTAR = text(
    "UPDATE borrador SET estado = 'descartado', motivo_descarte = :motivo, "
    "actualizado_en = now() WHERE id = :id AND estado = 'borrador'"
)

REGISTRAR = text(
    "INSERT INTO revision_borrador (borrador_id, accion, usuario, momento, "
    "hash_contenido, hash_snapshot, version_prompt, motivo, comprobante_id) "
    "VALUES (:borrador_id, :accion, :usuario, :momento, :hash_contenido, "
    ":hash_snapshot, :version_prompt, :motivo, :comprobante_id)"
)

LISTAR_REVISIONES = text(
    "SELECT id, borrador_id, accion, usuario, momento, hash_contenido, "
    "hash_snapshot, version_prompt, motivo, comprobante_id "
    "FROM revision_borrador WHERE borrador_id = :id ORDER BY momento, id"
)

EXISTE = text("SELECT 1 FROM borrador WHERE id = :id")

LEER_PDF = text("SELECT estado, pdf_s3_key FROM borrador WHERE id = :id")


def _bloquear_en_borrador(
    sesion: Session, borrador_id: UUID, hacia: EstadoBorrador
) -> Row[Any]:
    """La fila bloqueada, si todavia esta en revision. Si no, 404 o 409."""
    fila = sesion.execute(BLOQUEAR, {"id": borrador_id}).one_or_none()
    if fila is None:
        raise generacion.no_encontrado()
    if fila.estado != EstadoBorrador.BORRADOR:
        raise ConflictoError(
            f"Un borrador en estado {fila.estado} no se puede pasar a {hacia}.",
            code="transicion_invalida",
            details={"desde": fila.estado, "hacia": hacia.value},
        )
    return fila


def _comprobante(
    sesion: Session, comprobante_id: UUID, cliente_id: UUID
) -> dict[str, Any]:
    """El comprobante del criadero y del cliente del pedido, sin tocarlo."""
    fila = sesion.execute(LEER_COMPROBANTE, {"id": comprobante_id}).one_or_none()
    if fila is None:
        # 422 y no 404: el borrador de la URL existe, el dato del cuerpo no.
        raise ValidacionError(
            "No existe el comprobante.", code="comprobante_inexistente"
        )
    if fila.cliente_id != cliente_id:
        raise ValidacionError(
            "El comprobante es de otro cliente que el del pedido.",
            code="comprobante_de_otro_cliente",
        )
    return dict(fila._mapping)


def aprobar(
    sesion: Session,
    borrador_id: UUID,
    datos: Aprobacion,
    usuario: str,
    subidor: Subidor,
) -> BorradorRespuesta:
    """Congela lo aprobado, sube el PDF y deja la bitacora. Idempotencia y
    rol los resuelve el router."""
    fila = _bloquear_en_borrador(sesion, borrador_id, EstadoBorrador.APROBADO)
    contenido = BorradorValidado.model_validate(fila.contenido)
    version = hash_contenido(contenido)
    if datos.hash_contenido != version:
        raise ConflictoError(
            "El borrador cambio desde que lo abriste. Volvé a cargarlo y revisalo.",
            code="version_desactualizada",
        )

    pedido = sesion.execute(LEER_PEDIDO, {"id": fila.pedido_id}).one()
    lineas = sesion.execute(LEER_LINEAS, {"id": fila.pedido_id}).all()
    comprobante = (
        None
        if datos.comprobante_id is None
        else _comprobante(sesion, datos.comprobante_id, pedido.cliente_id)
    )

    momento = datetime.now(UTC)
    snapshot = armar_snapshot(
        borrador=dict(fila._mapping),
        contenido=contenido,
        pedido=dict(pedido._mapping),
        lineas=[dict(linea._mapping) for linea in lineas],
        comprobante=comprobante,
        usuario=usuario,
        momento=momento,
    )
    # Primero S3: si falla, 503 y el borrador sigue en revision, sin bitacora.
    clave = almacenamiento.clave_pdf_borrador(fila.tenant_id, borrador_id)
    almacenamiento.subir_pdf(subidor, clave, pdf_borrador.renderizar(snapshot))

    sesion.execute(
        APROBAR,
        {
            "id": borrador_id,
            "snapshot": json.dumps(snapshot, ensure_ascii=False),
            "hash_contenido": version,
            "pdf_s3_key": clave,
            "comprobante_id": datos.comprobante_id,
        },
    )
    sesion.execute(
        REGISTRAR,
        {
            "borrador_id": borrador_id,
            "accion": EstadoBorrador.APROBADO.value,
            "usuario": usuario,
            "momento": momento,
            "hash_contenido": version,
            "hash_snapshot": sha256_canonico(snapshot),
            "version_prompt": fila.version_prompt,
            "motivo": None,
            "comprobante_id": datos.comprobante_id,
        },
    )
    return generacion.obtener(sesion, borrador_id)


def descartar(
    sesion: Session, borrador_id: UUID, datos: Descarte, usuario: str
) -> BorradorRespuesta:
    """Una persona lo rechaza al revisarlo: queda descartado, con su motivo."""
    fila = _bloquear_en_borrador(sesion, borrador_id, EstadoBorrador.DESCARTADO)
    contenido = BorradorValidado.model_validate(fila.contenido)
    sesion.execute(DESCARTAR, {"id": borrador_id, "motivo": datos.motivo})
    sesion.execute(
        REGISTRAR,
        {
            "borrador_id": borrador_id,
            "accion": EstadoBorrador.DESCARTADO.value,
            "usuario": usuario,
            "momento": datetime.now(UTC),
            "hash_contenido": hash_contenido(contenido),
            "hash_snapshot": None,
            "version_prompt": fila.version_prompt,
            "motivo": datos.motivo,
            "comprobante_id": None,
        },
    )
    return generacion.obtener(sesion, borrador_id)


def revisiones(sesion: Session, borrador_id: UUID) -> list[RevisionRespuesta]:
    """La bitacora del borrador. 404 si no es del criadero."""
    if sesion.execute(EXISTE, {"id": borrador_id}).first() is None:
        raise generacion.no_encontrado()
    filas = sesion.execute(LISTAR_REVISIONES, {"id": borrador_id})
    return [RevisionRespuesta.model_validate(dict(f._mapping)) for f in filas]


def clave_del_pdf(sesion: Session, borrador_id: UUID) -> str:
    """La key del PDF aprobado. 404 si no existe, 409 si no esta aprobado."""
    fila = sesion.execute(LEER_PDF, {"id": borrador_id}).one_or_none()
    if fila is None:
        raise generacion.no_encontrado()
    if fila.pdf_s3_key is None:
        raise ConflictoError(
            "El borrador no esta aprobado: todavia no tiene PDF.",
            code="pdf_no_disponible",
            details={"estado": fila.estado},
        )
    return str(fila.pdf_s3_key)
