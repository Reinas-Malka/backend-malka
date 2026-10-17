"""Acceso a las tablas embarque y documento_embarque (#109).

Ninguna consulta filtra por tenant_id: RLS se encarga (ADR 0009). Un
embarque de otro criadero responde igual que uno que no existe (404).

Reglas, ademas de las del schema:
- Un embarque cancelado ya no se edita ni recibe documentos (409
  embarque_cancelado).
- Un documento de cada tipo por embarque (409 documento_duplicado).
- La guia aerea no se repite entre embarques del criadero (409
  awb_duplicado).
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import Row, text
from sqlalchemy.orm import Session

from app.errores import ConflictoError, ErrorDeApi, NoEncontradoError, ValidacionError
from app.paginacion import Pagina, ParametrosDePagina, armar_pagina, valores_del_cursor
from app.schemas.embarques import (
    CompletitudRespuesta,
    DocumentoCrear,
    DocumentoRespuesta,
    EmbarqueActualizar,
    EmbarqueCrear,
    EmbarqueGuardado,
    EmbarqueRespuesta,
)
from app.services.exportacion.embarques import (
    EstadoEmbarque,
    TipoDocumento,
    completitud,
)
from app.services.repositorio import escribir, validar_cambios

# estado no se manda: lo pone el default de la columna (en_preparacion).
INSERTAR = text(
    "INSERT INTO embarque (destino_pais, fecha_vuelo, awb) "
    "VALUES (:destino_pais, :fecha_vuelo, :awb) RETURNING id"
)

# documentos_cargados es una subconsulta: RLS tambien filtra
# documento_embarque, asi que solo cuenta los del criadero de la sesion.
COLUMNAS = (
    "SELECT e.id, e.destino_pais, e.fecha_vuelo, e.awb, e.estado, "
    "(SELECT count(*) FROM documento_embarque d WHERE d.embarque_id = e.id) "
    "AS documentos_cargados "
    "FROM embarque e "
)

# El vuelo mas proximo o reciente primero; el id desempata. Las dos columnas
# van DESC: la pagina siguiente sigue con (fecha_vuelo, id) < cursor. El
# CAST del estado es obligatorio: un NULL sin tipo hace fallar a PostgreSQL.
LISTAR = text(
    COLUMNAS + "WHERE (CAST(:estado AS text) IS NULL OR e.estado = :estado) "
    "AND (:sin_cursor OR (e.fecha_vuelo, e.id) < "
    "(CAST(:c_fecha AS date), CAST(:c_id AS uuid))) "
    "ORDER BY e.fecha_vuelo DESC, e.id DESC LIMIT :limite"
)

OBTENER = text(COLUMNAS + "WHERE e.id = :id")

# Toman el mismo bloqueo editar el embarque y agregarle un documento: asi no
# se carga un documento en un embarque que se esta cancelando.
BLOQUEAR = text("SELECT id FROM embarque WHERE id = :id FOR UPDATE")

ACTUALIZAR = text(
    "UPDATE embarque SET destino_pais = :destino_pais, "
    "fecha_vuelo = :fecha_vuelo, awb = :awb, estado = :estado, "
    "actualizado_en = now() WHERE id = :id RETURNING id"
)

INSERTAR_DOCUMENTO = text(
    "INSERT INTO documento_embarque (embarque_id, tipo, numero, archivo_s3_key, "
    "fecha) VALUES (:embarque_id, :tipo, :numero, :archivo_s3_key, :fecha) "
    "RETURNING id, embarque_id, tipo, numero, archivo_s3_key, fecha"
)

LISTAR_DOCUMENTOS = text(
    "SELECT id, embarque_id, tipo, numero, archivo_s3_key, fecha "
    "FROM documento_embarque WHERE embarque_id = :id"
)

LEER_TIPOS = text("SELECT tipo FROM documento_embarque WHERE embarque_id = :id")

# Orden fijo de los 6 tipos (docs/dominio.md) para listar los documentos.
ORDEN_DE_TIPOS = {str(tipo): posicion for posicion, tipo in enumerate(TipoDocumento)}


def no_encontrado() -> ErrorDeApi:
    return NoEncontradoError("No existe el embarque.", code="embarque_no_encontrado")


def _awb_duplicado() -> ErrorDeApi:
    return ConflictoError(
        "Ya hay un embarque con esa guia aerea.", code="awb_duplicado"
    )


def _documento_duplicado() -> ErrorDeApi:
    return ConflictoError(
        "El embarque ya tiene un documento de ese tipo.", code="documento_duplicado"
    )


def _archivo_ajeno() -> ErrorDeApi:
    # 422 y no 404: lo que esta mal es un dato del cuerpo, no la URL.
    return ValidacionError(
        "El archivo no pertenece a este criadero.", code="archivo_de_otro_criadero"
    )


# Nombres de las restricciones en la migracion 0010.
CONFLICTOS_EMBARQUE = {"uq_embarque_tenant_awb": _awb_duplicado}
CONFLICTOS_DOCUMENTO = {
    "uq_documento_embarque_tenant_embarque_tipo": _documento_duplicado,
    "ck_documento_embarque_archivo_del_tenant": _archivo_ajeno,
}


def _a_respuesta(fila: Row[Any]) -> EmbarqueRespuesta:
    return EmbarqueRespuesta.model_validate(dict(fila._mapping))


def crear(sesion: Session, datos: EmbarqueCrear) -> EmbarqueRespuesta:
    fila = escribir(
        sesion,
        INSERTAR,
        datos.model_dump(),
        conflictos=CONFLICTOS_EMBARQUE,
        no_encontrado=no_encontrado,
    )
    return obtener(sesion, fila.id)


def listar(
    sesion: Session,
    pagina: ParametrosDePagina,
    *,
    estado: EstadoEmbarque | None = None,
) -> Pagina[EmbarqueRespuesta]:
    c_fecha, c_id = valores_del_cursor(pagina, (date.fromisoformat, UUID))
    filas = sesion.execute(
        LISTAR,
        {
            "estado": None if estado is None else estado.value,
            "sin_cursor": pagina.cursor is None,
            "c_fecha": c_fecha,
            "c_id": c_id,
            # Uno de mas: si viene, hay pagina siguiente.
            "limite": pagina.limite + 1,
        },
    )
    return armar_pagina(
        [_a_respuesta(fila) for fila in filas],
        pagina.limite,
        lambda embarque: (embarque.fecha_vuelo.isoformat(), embarque.id),
    )


def obtener(sesion: Session, embarque_id: UUID) -> EmbarqueRespuesta:
    """Un embarque. 404 si no existe o es de otro criadero."""
    fila = sesion.execute(OBTENER, {"id": embarque_id}).one_or_none()
    if fila is None:
        raise no_encontrado()
    return _a_respuesta(fila)


def _bloquear_abierto(sesion: Session, embarque_id: UUID) -> EmbarqueRespuesta:
    """Bloquea el embarque y lo devuelve, si todavia no esta cancelado."""
    if sesion.execute(BLOQUEAR, {"id": embarque_id}).one_or_none() is None:
        raise no_encontrado()
    actual = obtener(sesion, embarque_id)
    if actual.estado is EstadoEmbarque.CANCELADO:
        raise ConflictoError(
            "El embarque esta cancelado: ya no se modifica.",
            code="embarque_cancelado",
        )
    return actual


def actualizar(
    sesion: Session, embarque_id: UUID, cambios: EmbarqueActualizar
) -> EmbarqueRespuesta:
    actual = _bloquear_abierto(sesion, embarque_id)
    validado = validar_cambios(EmbarqueGuardado, actual, cambios)
    escribir(
        sesion,
        ACTUALIZAR,
        {**validado.model_dump(mode="json"), "id": embarque_id},
        conflictos=CONFLICTOS_EMBARQUE,
        no_encontrado=no_encontrado,
    )
    return obtener(sesion, embarque_id)


def agregar_documento(
    sesion: Session, embarque_id: UUID, datos: DocumentoCrear
) -> DocumentoRespuesta:
    _bloquear_abierto(sesion, embarque_id)
    fila = escribir(
        sesion,
        INSERTAR_DOCUMENTO,
        {**datos.model_dump(mode="json"), "embarque_id": embarque_id},
        conflictos=CONFLICTOS_DOCUMENTO,
        no_encontrado=no_encontrado,
    )
    return DocumentoRespuesta.model_validate(dict(fila._mapping))


def listar_documentos(sesion: Session, embarque_id: UUID) -> Pagina[DocumentoRespuesta]:
    """Los documentos del embarque en el orden de los 6 tipos. Son 6 a lo
    sumo: no hace falta paginar, pero la respuesta tiene la forma de todo
    listado."""
    obtener(sesion, embarque_id)
    filas = sesion.execute(LISTAR_DOCUMENTOS, {"id": embarque_id}).all()
    documentos = sorted(filas, key=lambda fila: ORDEN_DE_TIPOS[fila.tipo])
    return Pagina[DocumentoRespuesta](
        items=[DocumentoRespuesta.model_validate(dict(f._mapping)) for f in documentos]
    )


def completitud_de(sesion: Session, embarque_id: UUID) -> CompletitudRespuesta:
    """Que documentos tiene el embarque y cuales le faltan."""
    obtener(sesion, embarque_id)
    resultado = completitud(sesion.execute(LEER_TIPOS, {"id": embarque_id}).scalars())
    return CompletitudRespuesta(
        embarque_id=embarque_id,
        completo=resultado.completo,
        presentes=list(resultado.presentes),
        faltantes=list(resultado.faltantes),
    )
