"""Acceso a material y movimiento_material (#30).

Reglas de docs/dominio.md, "Materiales y consumo":

- El stock no se guarda: es SUM(cantidad) de los movimientos, calculado al
  leer. Puede quedar negativo (los movimientos se cargan tarde): no se
  bloquea, la respuesta trae la advertencia stock_negativo.
- Los movimientos no se editan ni se borran (malka_app solo tiene SELECT e
  INSERT): un error se corrige con un ajuste.
- Aca se cargan compras y ajustes. El consumo lo registra el alta de tanda
  (#27), con la tanda que lo genero.

Como en el resto de los repositorios, ninguna consulta filtra por tenant: RLS
se encarga (ADR 0009), y lo de otro criadero responde 404.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Row, text
from sqlalchemy.orm import Session

from app.errores import ConflictoError, ErrorDeApi, NoEncontradoError
from app.fechas import hoy
from app.paginacion import Pagina, ParametrosDePagina, armar_pagina, valores_del_cursor
from app.schemas.produccion import (
    Advertencia,
    MaterialActualizar,
    MaterialCrear,
    MaterialRespuesta,
    MovimientoCrear,
    MovimientoGuardado,
    MovimientoRespuesta,
)
from app.services.repositorio import escribir, validar_cambios

INSERTAR = text(
    "INSERT INTO material (nombre, unidad) VALUES (:nombre, :unidad) RETURNING id"
)

# stock es una subconsulta: RLS tambien filtra movimiento_material.
LISTAR = text(
    "SELECT m.id, m.nombre, m.unidad, m.activo, "
    "(SELECT COALESCE(SUM(mm.cantidad), 0) FROM movimiento_material mm "
    "WHERE mm.material_id = m.id) AS stock "
    "FROM material m "
    "WHERE (m.activo OR :incluir_inactivos) "
    "AND (:sin_cursor OR (m.nombre, m.id) > (:c_nombre, CAST(:c_id AS uuid))) "
    "ORDER BY m.nombre, m.id LIMIT :limite"
)

OBTENER = text(
    "SELECT m.id, m.nombre, m.unidad, m.activo, "
    "(SELECT COALESCE(SUM(mm.cantidad), 0) FROM movimiento_material mm "
    "WHERE mm.material_id = m.id) AS stock "
    "FROM material m WHERE m.id = :id"
)

# FOR UPDATE OF m: bloquea solo el material, no sus movimientos.
OBTENER_PARA_EDITAR = text(
    "SELECT m.id, m.nombre, m.unidad, m.activo, "
    "(SELECT COALESCE(SUM(mm.cantidad), 0) FROM movimiento_material mm "
    "WHERE mm.material_id = m.id) AS stock "
    "FROM material m WHERE m.id = :id FOR UPDATE OF m"
)

ACTUALIZAR = text(
    "UPDATE material SET nombre = :nombre, unidad = :unidad, activo = :activo, "
    "actualizado_en = now() WHERE id = :id RETURNING id"
)

DESACTIVAR = text(
    "UPDATE material SET activo = false, actualizado_en = now() "
    "WHERE id = :id RETURNING id"
)

# La fecha llega siempre completa desde Python (hoy() si no vino): CURRENT_DATE
# daria la fecha UTC, que de 21 a 24 h de Argentina ya es manana.
INSERTAR_MOVIMIENTO = text(
    "INSERT INTO movimiento_material (material_id, tipo, cantidad, motivo, fecha) "
    "VALUES (:material_id, :tipo, :cantidad, :motivo, :fecha) "
    "RETURNING id, material_id, tipo, cantidad, tanda_id, motivo, fecha"
)

# Del mas nuevo al mas viejo: por fecha del movimiento, despues por fecha de
# carga; el id desempata. El cursor es esa terna de la ultima fila.
LISTAR_MOVIMIENTOS = text(
    "SELECT id, material_id, tipo, cantidad, tanda_id, motivo, fecha, creado_en "
    "FROM movimiento_material WHERE material_id = :material_id "
    "AND (:sin_cursor OR (fecha, creado_en, id) < "
    "(CAST(:c_fecha AS date), CAST(:c_creado AS timestamptz), CAST(:c_id AS uuid))) "
    "ORDER BY fecha DESC, creado_en DESC, id DESC LIMIT :limite"
)


def _duplicado() -> ErrorDeApi:
    return ConflictoError(
        "Ya hay un material con ese nombre.", code="material_duplicado"
    )


def _no_encontrado() -> ErrorDeApi:
    return NoEncontradoError("No existe el material.", code="material_no_encontrado")


# Nombre de la restriccion UNIQUE en la migracion 0004.
CONFLICTOS = {"uq_material_tenant_nombre": _duplicado}


def _a_respuesta(fila: Row[Any]) -> MaterialRespuesta:
    return MaterialRespuesta.model_validate(dict(fila._mapping))


# --- material ---


def crear(sesion: Session, datos: MaterialCrear) -> MaterialRespuesta:
    fila = escribir(
        sesion,
        INSERTAR,
        datos.model_dump(),
        conflictos=CONFLICTOS,
        no_encontrado=_no_encontrado,
    )
    return obtener(sesion, fila.id)


def listar(
    sesion: Session, pagina: ParametrosDePagina, *, incluir_inactivos: bool = False
) -> Pagina[MaterialRespuesta]:
    c_nombre, c_id = valores_del_cursor(pagina, (str, UUID))
    filas = sesion.execute(
        LISTAR,
        {
            "incluir_inactivos": incluir_inactivos,
            "sin_cursor": pagina.cursor is None,
            "c_nombre": c_nombre,
            "c_id": c_id,
            "limite": pagina.limite + 1,
        },
    )
    return armar_pagina(
        [_a_respuesta(fila) for fila in filas],
        pagina.limite,
        lambda material: (material.nombre, material.id),
    )


def obtener(sesion: Session, material_id: UUID) -> MaterialRespuesta:
    fila = sesion.execute(OBTENER, {"id": material_id}).one_or_none()
    if fila is None:
        raise _no_encontrado()
    return _a_respuesta(fila)


def actualizar(
    sesion: Session, material_id: UUID, cambios: MaterialActualizar
) -> MaterialRespuesta:
    fila = sesion.execute(OBTENER_PARA_EDITAR, {"id": material_id}).one_or_none()
    if fila is None:
        raise _no_encontrado()
    actual = _a_respuesta(fila)
    validado = validar_cambios(MaterialCrear, actual, cambios)
    activo = actual.activo if cambios.activo is None else cambios.activo
    escribir(
        sesion,
        ACTUALIZAR,
        {**validado.model_dump(), "activo": activo, "id": material_id},
        conflictos=CONFLICTOS,
        no_encontrado=_no_encontrado,
    )
    return obtener(sesion, material_id)


def desactivar(sesion: Session, material_id: UUID) -> None:
    """Borrado logico: el material sigue existiendo para sus movimientos."""
    escribir(
        sesion,
        DESACTIVAR,
        {"id": material_id},
        conflictos={},
        no_encontrado=_no_encontrado,
    )


# --- movimientos ---


def registrar_movimiento(
    sesion: Session, material_id: UUID, datos: MovimientoCrear
) -> MovimientoGuardado:
    """Carga una compra o un ajuste y devuelve el stock que queda."""
    # FOR UPDATE del material: dos cargas simultaneas calculan el stock en orden.
    fila = sesion.execute(OBTENER_PARA_EDITAR, {"id": material_id}).one_or_none()
    if fila is None:
        raise _no_encontrado()
    if not fila.activo:
        raise ConflictoError(
            "El material esta dado de baja: no admite movimientos.",
            code="material_inactivo",
        )
    movimiento = sesion.execute(
        INSERTAR_MOVIMIENTO,
        {
            "material_id": material_id,
            **datos.model_dump(),
            # Sin fecha, es el dia de hoy en Argentina.
            "fecha": datos.fecha or hoy(),
        },
    ).one()
    stock = obtener(sesion, material_id).stock

    advertencias = []
    if stock < 0:
        advertencias.append(
            Advertencia(
                code="stock_negativo",
                message="El stock del material quedo negativo.",
                details={"stock": str(stock)},
            )
        )
    return MovimientoGuardado(
        **MovimientoRespuesta.model_validate(dict(movimiento._mapping)).model_dump(),
        stock=stock,
        advertencias=advertencias,
    )


def listar_movimientos(
    sesion: Session, material_id: UUID, pagina: ParametrosDePagina
) -> Pagina[MovimientoRespuesta]:
    # Un material de otro criadero (o inexistente) responde 404, no una
    # lista vacia: asi no se distingue de uno sin movimientos.
    obtener(sesion, material_id)
    c_fecha, c_creado, c_id = valores_del_cursor(
        pagina, (date.fromisoformat, datetime.fromisoformat, UUID)
    )
    filas = sesion.execute(
        LISTAR_MOVIMIENTOS,
        {
            "material_id": material_id,
            "sin_cursor": pagina.cursor is None,
            "c_fecha": c_fecha,
            "c_creado": c_creado,
            "c_id": c_id,
            "limite": pagina.limite + 1,
        },
    ).all()
    # La fecha de carga solo sirve para el cursor: no sale en la respuesta.
    claves = {fila.id: fila.creado_en for fila in filas}
    return armar_pagina(
        [MovimientoRespuesta.model_validate(dict(fila._mapping)) for fila in filas],
        pagina.limite,
        lambda movimiento: (
            movimiento.fecha.isoformat(),
            claves[movimiento.id].isoformat(),
            movimiento.id,
        ),
    )
