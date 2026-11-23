"""Acceso a la tabla nucleo (#30) y capacidad del parque (#29).

Reglas fisicas, ademas de las del schema:

- El parque tiene que existir en el criadero (404 parque_no_encontrado) y
estar activo (409 parque_inactivo).
- Un parque no admite mas nucleos activos que su capacidad (409 parque_lleno).
Vale para el alta y para reactivar un nucleo dado de baja.
- Una posicion (fila, posicion) del parque tiene un solo nucleo (409
posicion_ocupada). Cuenta tambien a los nucleos dados de baja: para volver
a usar esa posicion se reactiva el nucleo que la ocupa.
- Si con el alta (o la reactivacion) el parque queda al limite, la operacion
sigue y la respuesta trae la advertencia parque_al_limite (#29).

Alta por cantidad (POST /parques/{id}/nucleos): el usuario no elige
ubicaciones. Los nucleos nuevos van a una fila nueva, la siguiente a la mas
alta del parque (contando los dados de baja, que tambien ocupan su lugar),
en las posiciones 1..cantidad. Asi nunca chocan con un nucleo existente y el
reparto de la introduccion (por fila y posicion) los recorre en el orden en
que se cargaron. Si no entran todos en la capacidad, no se crea ninguno
(409 capacidad_insuficiente, con cuantos entran).

Para que dos altas simultaneas no pasen la capacidad, se bloquea el parque
(FOR UPDATE) y recien despues, en otra consulta, se cuentan sus nucleos
(tests/test_concurrencia_parque.py). El mismo bloqueo hace que dos altas por
cantidad no elijan la misma fila.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Row, text
from sqlalchemy.orm import Session

from app.errores import ConflictoError, ErrorDeApi, NoEncontradoError
from app.paginacion import Pagina, ParametrosDePagina, armar_pagina, valores_del_cursor
from app.schemas.produccion import (
    MAXIMO_UBICACION,
    Advertencia,
    NucleoActualizar,
    NucleoCrear,
    NucleoGuardado,
    NucleoRespuesta,
    NucleosLoteCreados,
    NucleosLoteCrear,
)
from app.services.produccion import repositorio_parques
from app.services.repositorio import escribir, validar_cambios

INSERTAR = text(
    "INSERT INTO nucleo (parque_id, fila, posicion) "
    "VALUES (:parque_id, :fila, :posicion) "
    "RETURNING id, parque_id, fila, posicion, activo"
)

# Orden por parque, fila y posicion; el id desempata. ?parque_id= filtra.
LISTAR = text(
    "SELECT id, parque_id, fila, posicion, activo FROM nucleo "
    "WHERE (activo OR :incluir_inactivos) "
    "AND (CAST(:parque_id AS uuid) IS NULL OR parque_id = CAST(:parque_id AS uuid)) "
    "AND (:sin_cursor OR (parque_id, fila, posicion, id) > "
    "(CAST(:c_parque AS uuid), CAST(:c_fila AS integer), "
    "CAST(:c_posicion AS integer), CAST(:c_id AS uuid))) "
    "ORDER BY parque_id, fila, posicion, id LIMIT :limite"
)

OBTENER = text(
    "SELECT id, parque_id, fila, posicion, activo FROM nucleo WHERE id = :id"
)

# La fila que sigue a la mas alta del parque, contando los dados de baja.
SIGUIENTE_FILA = text(
    "SELECT COALESCE(max(fila), 0) + 1 FROM nucleo WHERE parque_id = :parque"
)

# Todo el lote en un INSERT; el CTE devuelve una sola fila con cuantos entraron.
INSERTAR_LOTE = text(
    "WITH nuevos AS ("
    "INSERT INTO nucleo (parque_id, fila, posicion) "
    "SELECT :parque, :fila, g FROM generate_series(1, :cantidad) AS g "
    "RETURNING id"
    ") SELECT count(*) AS creados FROM nuevos"
)

OBTENER_PARA_EDITAR = text(
    "SELECT id, parque_id, fila, posicion, activo FROM nucleo "
    "WHERE id = :id FOR UPDATE"
)

ACTUALIZAR = text(
    "UPDATE nucleo SET fila = :fila, posicion = :posicion, activo = :activo, "
    "actualizado_en = now() WHERE id = :id "
    "RETURNING id, parque_id, fila, posicion, activo"
)

DESACTIVAR = text(
    "UPDATE nucleo SET activo = false, actualizado_en = now() "
    "WHERE id = :id RETURNING id"
)


def _posicion_ocupada() -> ErrorDeApi:
    return ConflictoError(
        "Esa posicion del parque ya tiene un nucleo.", code="posicion_ocupada"
    )


def _no_encontrado() -> ErrorDeApi:
    return NoEncontradoError("No existe el nucleo.", code="nucleo_no_encontrado")


# Nombre de la restriccion UNIQUE en la migracion 0003.
CONFLICTOS = {"uq_nucleo_ubicacion": _posicion_ocupada}


def _a_respuesta(fila: Row[Any]) -> NucleoRespuesta:
    return NucleoRespuesta.model_validate(dict(fila._mapping))


def _reservar_lugar(sesion: Session, parque_id: UUID) -> list[Advertencia]:
    """Bloquea el parque y verifica que admita un nucleo activo mas.

    Devuelve la advertencia si, con ese nucleo, el parque queda lleno.
    """
    # Bloquea y despues cuenta (ver repositorio_parques.bloquear): asi ve el
    # nucleo que otra alta simultanea acaba de confirmar.
    parque = repositorio_parques.bloquear(sesion, parque_id)
    if not parque.activo:
        raise ConflictoError(
            "El parque esta dado de baja: no admite nucleos.", code="parque_inactivo"
        )
    if parque.nucleos_activos >= parque.capacidad_max:
        raise ConflictoError(
            "El parque ya tiene todos los nucleos que admite.",
            code="parque_lleno",
            details={"capacidad_max": parque.capacidad_max},
        )
    if parque.nucleos_activos + 1 < parque.capacidad_max:
        return []
    return _al_limite(parque.capacidad_max)


def _al_limite(capacidad_max: int) -> list[Advertencia]:
    return [
        Advertencia(
            code="parque_al_limite",
            message="El parque quedo lleno: no admite mas nucleos activos.",
            details={"capacidad_max": capacidad_max},
        )
    ]


def crear_lote(
    sesion: Session, parque_id: UUID, datos: NucleosLoteCrear
) -> NucleosLoteCreados:
    """Crea `cantidad` nucleos activos en una fila nueva del parque."""
    # Bloquea y despues cuenta, como el alta de a uno.
    parque = repositorio_parques.bloquear(sesion, parque_id)
    if not parque.activo:
        raise ConflictoError(
            "El parque esta dado de baja: no admite nucleos.", code="parque_inactivo"
        )
    disponibles = parque.capacidad_max - parque.nucleos_activos
    if datos.cantidad > disponibles:
        raise ConflictoError(
            f"En el parque entran {disponibles} nucleos mas.",
            code="capacidad_insuficiente",
            details={
                "pedidos": datos.cantidad,
                "disponibles": disponibles,
                "capacidad_max": parque.capacidad_max,
                "nucleos_activos": parque.nucleos_activos,
            },
        )
    fila = int(sesion.execute(SIGUIENTE_FILA, {"parque": parque_id}).scalar_one())
    if fila > MAXIMO_UBICACION:
        # Despues no se podria editar ese nucleo (NucleoCrear limita la fila).
        raise ConflictoError(
            "El parque ya usa todas las filas: no hay una libre para el lote.",
            code="parque_sin_filas",
            details={"maximo": MAXIMO_UBICACION},
        )
    creados = escribir(
        sesion,
        INSERTAR_LOTE,
        {"parque": parque_id, "fila": fila, "cantidad": datos.cantidad},
        conflictos=CONFLICTOS,
        no_encontrado=repositorio_parques.no_encontrado,
    ).creados
    activos = parque.nucleos_activos + creados
    return NucleosLoteCreados(
        parque_id=parque_id,
        creados=creados,
        fila=fila,
        posicion_desde=1,
        posicion_hasta=datos.cantidad,
        nucleos_activos=activos,
        capacidad_max=parque.capacidad_max,
        advertencias=(
            _al_limite(parque.capacidad_max) if activos >= parque.capacidad_max else []
        ),
    )


def _guardado(fila: Row[Any], advertencias: list[Advertencia]) -> NucleoGuardado:
    return NucleoGuardado(**_a_respuesta(fila).model_dump(), advertencias=advertencias)


def crear(sesion: Session, datos: NucleoCrear) -> NucleoGuardado:
    advertencias = _reservar_lugar(sesion, datos.parque_id)
    fila = escribir(
        sesion,
        INSERTAR,
        datos.model_dump(),
        conflictos=CONFLICTOS,
        no_encontrado=_no_encontrado,
    )
    return _guardado(fila, advertencias)


def listar(
    sesion: Session,
    pagina: ParametrosDePagina,
    *,
    parque_id: UUID | None = None,
    incluir_inactivos: bool = False,
) -> Pagina[NucleoRespuesta]:
    c_parque, c_fila, c_posicion, c_id = valores_del_cursor(
        pagina, (UUID, int, int, UUID)
    )
    filas = sesion.execute(
        LISTAR,
        {
            "incluir_inactivos": incluir_inactivos,
            "parque_id": parque_id,
            "sin_cursor": pagina.cursor is None,
            "c_parque": c_parque,
            "c_fila": c_fila,
            "c_posicion": c_posicion,
            "c_id": c_id,
            "limite": pagina.limite + 1,
        },
    )
    return armar_pagina(
        [_a_respuesta(fila) for fila in filas],
        pagina.limite,
        lambda nucleo: (nucleo.parque_id, nucleo.fila, nucleo.posicion, nucleo.id),
    )


def obtener(sesion: Session, nucleo_id: UUID) -> NucleoRespuesta:
    fila = sesion.execute(OBTENER, {"id": nucleo_id}).one_or_none()
    if fila is None:
        raise _no_encontrado()
    return _a_respuesta(fila)


def actualizar(
    sesion: Session, nucleo_id: UUID, cambios: NucleoActualizar
) -> NucleoGuardado:
    fila = sesion.execute(OBTENER_PARA_EDITAR, {"id": nucleo_id}).one_or_none()
    if fila is None:
        raise _no_encontrado()
    actual = _a_respuesta(fila)
    validado = validar_cambios(NucleoCrear, actual, cambios)
    activo = actual.activo if cambios.activo is None else cambios.activo

    advertencias: list[Advertencia] = []
    if activo and not actual.activo:
        # Reactivar ocupa un lugar en el parque, igual que un alta.
        advertencias = _reservar_lugar(sesion, actual.parque_id)
    fila = escribir(
        sesion,
        ACTUALIZAR,
        {
            "fila": validado.fila,
            "posicion": validado.posicion,
            "activo": activo,
            "id": nucleo_id,
        },
        conflictos=CONFLICTOS,
        no_encontrado=_no_encontrado,
    )
    return _guardado(fila, advertencias)


def desactivar(sesion: Session, nucleo_id: UUID) -> None:
    """Borrado logico: libera un lugar en el parque."""
    escribir(
        sesion,
        DESACTIVAR,
        {"id": nucleo_id},
        conflictos={},
        no_encontrado=_no_encontrado,
    )
