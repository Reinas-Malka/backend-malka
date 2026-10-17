"""Acceso a la tabla banco (dominio del 16/10, migracion 0009).

El banco guarda reinas ya enjauladas, listas para despacho. No es el parque
(que tiene nucleos para fecundar). Reglas fisicas, ademas de las del schema:
- No se puede bajar la capacidad por debajo de las reinas que ya tiene (409
  capacidad_insuficiente).
- No se puede dar de baja un banco con reinas (409 banco_con_reinas): primero
  se despachan o se pasan a otro banco.

Las reinas del banco se cuentan al leer (celdas enjauladas con ese banco_id),
nunca se guardan (ADR 0014). El paso de celdas al banco es por lote (#28) y
toma el mismo bloqueo que este repositorio (bloquear) para no pasarse de la
capacidad, como el parque con sus nucleos (#29).
"""

from __future__ import annotations

from typing import Any, NoReturn
from uuid import UUID

from sqlalchemy import Row, text
from sqlalchemy.orm import Session

from app.errores import ConflictoError, ErrorDeApi, NoEncontradoError
from app.paginacion import Pagina, ParametrosDePagina, armar_pagina, valores_del_cursor
from app.schemas.produccion import BancoActualizar, BancoCrear, BancoRespuesta
from app.services.repositorio import escribir, validar_cambios

INSERTAR = text(
    "INSERT INTO banco (identificador, capacidad_max) "
    "VALUES (:identificador, :capacidad_max) RETURNING id"
)

# reinas_en_banco es una subconsulta: RLS tambien filtra celda, asi que solo
# cuenta las del criadero de la sesion.
_COLUMNAS = (
    "b.id, b.identificador, b.capacidad_max, b.activo, "
    "(SELECT count(*) FROM celda c WHERE c.banco_id = b.id "
    "AND c.estado = 'enjaulada') AS reinas_en_banco "
)

LISTAR = text(
    f"SELECT {_COLUMNAS}FROM banco b "  # noqa: S608 - texto fijo, sin datos
    "WHERE (b.activo OR :incluir_inactivos) "
    "AND (:sin_cursor OR (b.identificador, b.id) > "
    "(:c_identificador, CAST(:c_id AS uuid))) "
    "ORDER BY b.identificador, b.id LIMIT :limite"
)

OBTENER = text(f"SELECT {_COLUMNAS}FROM banco b WHERE b.id = :id")  # noqa: S608

# Bloqueo y conteo en consultas separadas, como en repositorio_parques: en
# READ COMMITTED la consulta siguiente ya ve lo que otra transaccion confirmo
# mientras esta esperaba el bloqueo.
BLOQUEAR = text("SELECT id FROM banco WHERE id = :id FOR UPDATE")

ACTUALIZAR = text(
    "UPDATE banco SET identificador = :identificador, "
    "capacidad_max = :capacidad_max, activo = :activo, actualizado_en = now() "
    "WHERE id = :id RETURNING id"
)


def _duplicado() -> ErrorDeApi:
    return ConflictoError(
        "Ya hay un banco con ese identificador.", code="banco_duplicado"
    )


def no_encontrado() -> ErrorDeApi:
    """Tambien lo usa el paso de celdas a banco (#28) si el banco no existe."""
    return NoEncontradoError("No existe el banco.", code="banco_no_encontrado")


# Nombre de la restriccion UNIQUE en la migracion 0009.
CONFLICTOS = {"uq_banco_tenant_identificador": _duplicado}


def _a_respuesta(fila: Row[Any]) -> BancoRespuesta:
    return BancoRespuesta.model_validate(dict(fila._mapping))


def crear(sesion: Session, datos: BancoCrear) -> BancoRespuesta:
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
) -> Pagina[BancoRespuesta]:
    c_identificador, c_id = valores_del_cursor(pagina, (str, UUID))
    filas = sesion.execute(
        LISTAR,
        {
            "incluir_inactivos": incluir_inactivos,
            "sin_cursor": pagina.cursor is None,
            "c_identificador": c_identificador,
            "c_id": c_id,
            "limite": pagina.limite + 1,
        },
    )
    return armar_pagina(
        [_a_respuesta(fila) for fila in filas],
        pagina.limite,
        lambda banco: (banco.identificador, banco.id),
    )


def obtener(sesion: Session, banco_id: UUID) -> BancoRespuesta:
    fila = sesion.execute(OBTENER, {"id": banco_id}).one_or_none()
    if fila is None:
        raise no_encontrado()
    return _a_respuesta(fila)


def bloquear(sesion: Session, banco_id: UUID) -> BancoRespuesta:
    """Bloquea el banco y lo devuelve con sus reinas ya contadas."""
    if sesion.execute(BLOQUEAR, {"id": banco_id}).one_or_none() is None:
        raise no_encontrado()
    return obtener(sesion, banco_id)


def _guardar(
    sesion: Session, banco_id: UUID, datos: BancoCrear, *, activo: bool
) -> BancoRespuesta:
    escribir(
        sesion,
        ACTUALIZAR,
        {**datos.model_dump(), "activo": activo, "id": banco_id},
        conflictos=CONFLICTOS,
        no_encontrado=no_encontrado,
    )
    return obtener(sesion, banco_id)


def actualizar(
    sesion: Session, banco_id: UUID, cambios: BancoActualizar
) -> BancoRespuesta:
    actual = bloquear(sesion, banco_id)
    validado = validar_cambios(BancoCrear, actual, cambios)
    activo = actual.activo if cambios.activo is None else cambios.activo
    if validado.capacidad_max < actual.reinas_en_banco:
        raise ConflictoError(
            "La capacidad no puede ser menor que las reinas que hay en el banco.",
            code="capacidad_insuficiente",
            details={"reinas_en_banco": actual.reinas_en_banco},
        )
    if not activo and actual.reinas_en_banco > 0:
        _con_reinas(actual.reinas_en_banco)
    return _guardar(sesion, banco_id, validado, activo=activo)


def desactivar(sesion: Session, banco_id: UUID) -> None:
    """Borrado logico, solo si el banco no tiene reinas."""
    actual = bloquear(sesion, banco_id)
    if actual.reinas_en_banco > 0:
        _con_reinas(actual.reinas_en_banco)
    datos = BancoCrear.model_validate(
        actual.model_dump(include=set(BancoCrear.model_fields))
    )
    _guardar(sesion, banco_id, datos, activo=False)


def _con_reinas(cantidad: int) -> NoReturn:
    raise ConflictoError(
        "El banco tiene reinas: hay que despacharlas o pasarlas a otro banco.",
        code="banco_con_reinas",
        details={"reinas_en_banco": cantidad},
    )
