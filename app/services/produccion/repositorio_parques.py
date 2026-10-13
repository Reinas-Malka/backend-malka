"""Acceso a la tabla parque (#30).

El parque de fecundacion es donde las celdas entran a un nucleo. Reglas
fisicas, ademas de las del schema:

- No se puede bajar la capacidad por debajo de los nucleos activos que ya
  tiene (409 capacidad_insuficiente).
- No se puede dar de baja un parque con nucleos activos (409
  parque_con_nucleos): primero se dan de baja o se reubican los nucleos.

La cantidad de nucleos activos se cuenta al leer, nunca se guarda.
"""

from __future__ import annotations

from typing import Any, NoReturn
from uuid import UUID

from sqlalchemy import Row, text
from sqlalchemy.orm import Session

from app.errores import ConflictoError, ErrorDeApi, NoEncontradoError
from app.paginacion import Pagina, ParametrosDePagina, armar_pagina, valores_del_cursor
from app.schemas.produccion import ParqueActualizar, ParqueCrear, ParqueRespuesta
from app.services.repositorio import escribir, validar_cambios

INSERTAR = text(
    "INSERT INTO parque (nombre, ubicacion, capacidad_max) "
    "VALUES (:nombre, :ubicacion, :capacidad_max) RETURNING id"
)

# nucleos_activos es una subconsulta: RLS tambien filtra nucleo, asi que
# solo cuenta los del criadero de la sesion.
LISTAR = text(
    "SELECT p.id, p.nombre, p.ubicacion, p.capacidad_max, p.activo, "
    "(SELECT count(*) FROM nucleo n WHERE n.parque_id = p.id AND n.activo) "
    "AS nucleos_activos "
    "FROM parque p "
    "WHERE (p.activo OR :incluir_inactivos) "
    "AND (:sin_cursor OR (p.nombre, p.id) > (:c_nombre, CAST(:c_id AS uuid))) "
    "ORDER BY p.nombre, p.id LIMIT :limite"
)

OBTENER = text(
    "SELECT p.id, p.nombre, p.ubicacion, p.capacidad_max, p.activo, "
    "(SELECT count(*) FROM nucleo n WHERE n.parque_id = p.id AND n.activo) "
    "AS nucleos_activos "
    "FROM parque p WHERE p.id = :id"
)

# FOR UPDATE bloquea el parque: el alta de un nucleo (repositorio_nucleos)
# toma el mismo bloqueo, asi el conteo de nucleos no cambia mientras se edita.
OBTENER_PARA_EDITAR = text(
    "SELECT p.id, p.nombre, p.ubicacion, p.capacidad_max, p.activo, "
    "(SELECT count(*) FROM nucleo n WHERE n.parque_id = p.id AND n.activo) "
    "AS nucleos_activos "
    "FROM parque p WHERE p.id = :id FOR UPDATE OF p"
)

ACTUALIZAR = text(
    "UPDATE parque SET nombre = :nombre, ubicacion = :ubicacion, "
    "capacidad_max = :capacidad_max, activo = :activo, actualizado_en = now() "
    "WHERE id = :id RETURNING id"
)


def _duplicado() -> ErrorDeApi:
    return ConflictoError("Ya hay un parque con ese nombre.", code="parque_duplicado")


def no_encontrado() -> ErrorDeApi:
    """Tambien lo usa repositorio_nucleos cuando el parque del body no existe."""
    return NoEncontradoError("No existe el parque.", code="parque_no_encontrado")


# Nombre de la restriccion UNIQUE en la migracion 0003.
CONFLICTOS = {"uq_parque_tenant_nombre": _duplicado}


def _a_respuesta(fila: Row[Any]) -> ParqueRespuesta:
    return ParqueRespuesta.model_validate(dict(fila._mapping))


def crear(sesion: Session, datos: ParqueCrear) -> ParqueRespuesta:
    fila = escribir(
        sesion,
        INSERTAR,
        datos.model_dump(),
        conflictos=CONFLICTOS,
        no_encontrado=no_encontrado,
    )
    return obtener(sesion, fila.id)


def listar(
    sesion: Session, pagina: ParametrosDePagina, *, incluir_inactivos: bool = False
) -> Pagina[ParqueRespuesta]:
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
        lambda parque: (parque.nombre, parque.id),
    )


def obtener(sesion: Session, parque_id: UUID) -> ParqueRespuesta:
    fila = sesion.execute(OBTENER, {"id": parque_id}).one_or_none()
    if fila is None:
        raise no_encontrado()
    return _a_respuesta(fila)


def _guardar(
    sesion: Session, parque_id: UUID, datos: ParqueCrear, *, activo: bool
) -> ParqueRespuesta:
    escribir(
        sesion,
        ACTUALIZAR,
        {**datos.model_dump(), "activo": activo, "id": parque_id},
        conflictos=CONFLICTOS,
        no_encontrado=no_encontrado,
    )
    return obtener(sesion, parque_id)


def actualizar(
    sesion: Session, parque_id: UUID, cambios: ParqueActualizar
) -> ParqueRespuesta:
    fila = sesion.execute(OBTENER_PARA_EDITAR, {"id": parque_id}).one_or_none()
    if fila is None:
        raise no_encontrado()
    actual = _a_respuesta(fila)
    validado = validar_cambios(ParqueCrear, actual, cambios)
    activo = actual.activo if cambios.activo is None else cambios.activo

    if validado.capacidad_max < actual.nucleos_activos:
        raise ConflictoError(
            "La capacidad no puede ser menor que los nucleos activos del parque.",
            code="capacidad_insuficiente",
            details={"nucleos_activos": actual.nucleos_activos},
        )
    if not activo and actual.nucleos_activos > 0:
        _con_nucleos(actual.nucleos_activos)
    return _guardar(sesion, parque_id, validado, activo=activo)


def desactivar(sesion: Session, parque_id: UUID) -> None:
    """Borrado logico, solo si el parque no tiene nucleos activos."""
    fila = sesion.execute(OBTENER_PARA_EDITAR, {"id": parque_id}).one_or_none()
    if fila is None:
        raise no_encontrado()
    actual = _a_respuesta(fila)
    if actual.nucleos_activos > 0:
        _con_nucleos(actual.nucleos_activos)
    datos = ParqueCrear.model_validate(
        actual.model_dump(include=set(ParqueCrear.model_fields))
    )
    _guardar(sesion, parque_id, datos, activo=False)


def _con_nucleos(cantidad: int) -> NoReturn:
    raise ConflictoError(
        "El parque tiene nucleos activos: hay que darlos de baja primero.",
        code="parque_con_nucleos",
        details={"nucleos_activos": cantidad},
    )
