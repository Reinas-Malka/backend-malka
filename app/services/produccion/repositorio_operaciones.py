"""Operaciones por lote sobre las celdas de una tanda (#28).

Reglas de docs/dominio.md, "Lo que se registra en el campo" (23/10). El
productor nunca opera celda por celda: carga una cantidad y el sistema elige
que celdas mueve. Las unicas transiciones son:

    trasladada  -> descartada               descartar (antes de introducir)
    trasladada  -> introducida              introducir, con reparto automatico
    introducida -> enjaulada | descartada   cosechar: las cosechadas y el resto
    enjaulada   -> enjaulada en un banco    pasar a banco

- Pedir mas celdas de las que hay en el estado de origen: 409
  cantidad_excedida, con cuantas hay.
- Una tanda se cosecha una sola vez (409 tanda_cosechada). Despues de la
  cosecha no se introduce mas (409 transicion_invalida): esas celdas nunca
  se cosecharian.
- Las fechas son dias calendario (el domingo no aplica), no pueden ser
  futuras (schema) ni anteriores al paso previo: el traslarve, o la ultima
  introduccion para la cosecha (422 fecha_anterior).
- Sin retrocesos: como se corrige un error de carga esta pendiente con la
  clienta.

Concurrencia: toda operacion bloquea primero la tanda (FOR UPDATE), asi dos
operaciones sobre la misma tanda van en orden y la segunda cuenta las celdas
que dejo la primera. Introducir bloquea tambien el parque y pasar a banco, el
banco (el mismo patron que el parque en la #29): siempre tanda primero y
despues parque o banco, para que no haya bloqueos cruzados.

Como en el resto de los repositorios, RLS filtra por criadero (ADR 0009): una
tanda, un parque o un banco de otro criadero responden 404, igual que uno que
no existe.
"""

from __future__ import annotations

from datetime import date
from typing import Any, NoReturn
from uuid import UUID

from sqlalchemy import Row, text
from sqlalchemy.orm import Session

from app.errores import ConflictoError, NoEncontradoError, ValidacionError
from app.schemas.produccion import (
    OperacionCosechar,
    OperacionCrear,
    OperacionDescartar,
    OperacionIntroducir,
    OperacionPasarABanco,
    OperacionRealizada,
    RepartoNucleo,
)
from app.services.produccion import (
    repositorio_bancos,
    repositorio_parques,
    repositorio_tandas,
)
from app.services.produccion.operaciones import NucleoOcupado, repartir
from app.services.ventas.repositorio_reservas import BLOQUEAR_RAZA

# Las consultas se escriben completas, sin armar SQL con f-strings.
BLOQUEAR_TANDA = text(
    "SELECT id, fecha_traslarve, raza_id FROM tanda WHERE id = :id FOR UPDATE"
)

CONTAR_EN_ESTADO = text(
    "SELECT count(*) FROM celda WHERE tanda_id = :tanda AND estado = :estado"
)

CONTAR_ENJAULADAS_SIN_BANCO = text(
    "SELECT count(*) FROM celda "
    "WHERE tanda_id = :tanda AND estado = 'enjaulada' AND banco_id IS NULL"
)

# Cosechada: alguna celda ya paso por el parque y salio (enjaulada,
# despachada o descartada con nucleo). Vale tambien para una cosecha de 0.
YA_COSECHADA = text(
    "SELECT EXISTS (SELECT 1 FROM celda WHERE tanda_id = :tanda "
    "AND nucleo_id IS NOT NULL AND estado <> 'introducida')"
)

ULTIMA_INTRODUCCION = text(
    "SELECT max(fecha_introduccion) FROM celda "
    "WHERE tanda_id = :tanda AND estado = 'introducida'"
)

# Cualquier celda en ese estado sirve: dentro de la tanda son todas iguales.
# El ORDER BY solo hace el resultado repetible.
DESCARTAR = text(
    "UPDATE celda SET estado = 'descartada', fecha_descarte = :fecha, "
    "actualizado_en = now() "
    "WHERE id IN (SELECT id FROM celda WHERE tanda_id = :tanda "
    "AND estado = 'trasladada' ORDER BY id LIMIT :cantidad)"
)

# Nucleos activos del parque con sus celdas ocupadas: las introducidas que
# todavia no se cosecharon, de cualquier tanda.
NUCLEOS_DEL_PARQUE = text(
    "SELECT n.id, n.fila, n.posicion, "
    "(SELECT count(*) FROM celda c WHERE c.nucleo_id = n.id "
    "AND c.estado = 'introducida') AS ocupadas "
    "FROM nucleo n WHERE n.parque_id = :parque AND n.activo "
    "ORDER BY n.fila, n.posicion"
)

ELEGIR_TRASLADADAS = text(
    "SELECT id FROM celda WHERE tanda_id = :tanda AND estado = 'trasladada' "
    "ORDER BY id LIMIT :cantidad"
)

# Una sola sentencia para todo el lote: cada celda con su nucleo.
INTRODUCIR = text(
    "UPDATE celda c SET estado = 'introducida', nucleo_id = v.nucleo_id, "
    "fecha_introduccion = :fecha, actualizado_en = now() "
    "FROM unnest(CAST(:celdas AS uuid[]), CAST(:nucleos AS uuid[])) "
    "AS v(celda_id, nucleo_id) "
    "WHERE c.id = v.celda_id"
)

ENJAULAR = text(
    "UPDATE celda SET estado = 'enjaulada', fecha_enjaulado = :fecha, "
    "actualizado_en = now() "
    "WHERE id IN (SELECT id FROM celda WHERE tanda_id = :tanda "
    "AND estado = 'introducida' ORDER BY id LIMIT :cantidad)"
)

# El resto de la cosecha: conservan el nucleo, asi se distinguen de las
# descartadas antes de introducir (docs/dominio.md).
DESCARTAR_RESTO = text(
    "UPDATE celda SET estado = 'descartada', fecha_descarte = :fecha, "
    "actualizado_en = now() "
    "WHERE tanda_id = :tanda AND estado = 'introducida'"
)

# Elige y bloquea las celdas como el despacho (repositorio_transiciones,
# DESPACHAR): las mas viejas primero, FOR UPDATE, y el UPDATE vuelve a exigir
# enjaulada y sin banco. Asi nunca le pone banco a una celda que un despacho
# ya se llevo. RETURNING: si vuelven menos que :cantidad, no alcanzaba.
PASAR_A_BANCO = text(
    "WITH elegidas AS MATERIALIZED ("
    "SELECT id FROM celda WHERE tanda_id = :tanda "
    "AND estado = 'enjaulada' AND banco_id IS NULL "
    "ORDER BY fecha_enjaulado, id LIMIT :cantidad FOR UPDATE"
    ") "
    "UPDATE celda SET banco_id = :banco, actualizado_en = now() "
    "FROM elegidas WHERE celda.id = elegidas.id "
    "AND celda.estado = 'enjaulada' AND celda.banco_id IS NULL "
    "RETURNING celda.id"
)


def _bloquear_tanda(sesion: Session, tanda_id: UUID) -> Row[Any]:
    fila = sesion.execute(BLOQUEAR_TANDA, {"id": tanda_id}).one_or_none()
    if fila is None:
        raise NoEncontradoError("No existe la tanda.", code="tanda_no_encontrada")
    return fila


def _contar(sesion: Session, consulta: Any, tanda_id: UUID, **extra: Any) -> int:
    return int(sesion.execute(consulta, {"tanda": tanda_id, **extra}).scalar_one())


def _excedida(pedidas: int, hay: int, estado: str) -> NoReturn:
    raise ConflictoError(
        f"Se pidieron {pedidas} celdas y hay {hay} en estado {estado}.",
        code="cantidad_excedida",
        details={"pedidas": pedidas, "disponibles": hay, "estado": estado},
    )


def _no_antes_de(fecha: date, minima: date, paso: str) -> None:
    if fecha < minima:
        raise ValidacionError(
            f"La fecha no puede ser anterior a {paso}.",
            code="fecha_anterior",
            details={"fecha": fecha.isoformat(), "minima": minima.isoformat()},
        )


def _ya_cosechada(sesion: Session, tanda_id: UUID) -> bool:
    return bool(sesion.execute(YA_COSECHADA, {"tanda": tanda_id}).scalar_one())


def _resultado(
    sesion: Session, tanda_id: UUID, accion: str, cantidad: int, **extra: Any
) -> OperacionRealizada:
    return OperacionRealizada(
        accion=accion,
        cantidad=cantidad,
        tanda=repositorio_tandas.obtener(sesion, tanda_id),
        **extra,
    )


def _descartar(
    sesion: Session, tanda: Row[Any], datos: OperacionDescartar
) -> OperacionRealizada:
    _no_antes_de(datos.fecha, tanda.fecha_traslarve, "el traslarve")
    hay = _contar(sesion, CONTAR_EN_ESTADO, tanda.id, estado="trasladada")
    if datos.cantidad > hay:
        _excedida(datos.cantidad, hay, "trasladada")
    sesion.execute(
        DESCARTAR,
        {"tanda": tanda.id, "fecha": datos.fecha, "cantidad": datos.cantidad},
    )
    return _resultado(sesion, tanda.id, datos.accion, datos.cantidad)


def _introducir(
    sesion: Session, tanda: Row[Any], datos: OperacionIntroducir
) -> OperacionRealizada:
    if _ya_cosechada(sesion, tanda.id):
        raise ConflictoError(
            "La tanda ya se cosecho: no se le pueden introducir mas celdas.",
            code="transicion_invalida",
        )
    _no_antes_de(datos.fecha, tanda.fecha_traslarve, "el traslarve")
    hay = _contar(sesion, CONTAR_EN_ESTADO, tanda.id, estado="trasladada")
    if datos.cantidad > hay:
        _excedida(datos.cantidad, hay, "trasladada")

    # Bloquea el parque y despues cuenta (como repositorio_nucleos): dos
    # introducciones simultaneas en el mismo parque reparten en orden.
    parque = repositorio_parques.bloquear(sesion, datos.parque_id)
    if not parque.activo:
        raise ConflictoError(
            "El parque esta dado de baja: no admite celdas.", code="parque_inactivo"
        )
    nucleos = [
        NucleoOcupado(id=n.id, fila=n.fila, posicion=n.posicion, ocupadas=n.ocupadas)
        for n in sesion.execute(NUCLEOS_DEL_PARQUE, {"parque": datos.parque_id})
    ]
    if not nucleos:
        raise ConflictoError(
            "El parque no tiene nucleos activos: no hay donde introducir.",
            code="parque_sin_nucleos",
        )

    reparto = repartir(nucleos, datos.cantidad)
    celdas = list(
        sesion.execute(
            ELEGIR_TRASLADADAS, {"tanda": tanda.id, "cantidad": datos.cantidad}
        ).scalars()
    )
    # Una entrada por celda: el nucleo repetido tantas veces como celdas recibe.
    destinos = [nucleo for nucleo, n in reparto.items() for _ in range(n)]
    sesion.execute(
        INTRODUCIR, {"celdas": celdas, "nucleos": destinos, "fecha": datos.fecha}
    )

    ubicacion = {n.id: n for n in nucleos}
    return _resultado(
        sesion,
        tanda.id,
        datos.accion,
        datos.cantidad,
        reparto=[
            RepartoNucleo(
                nucleo_id=nucleo,
                fila=ubicacion[nucleo].fila,
                posicion=ubicacion[nucleo].posicion,
                celdas=n,
            )
            for nucleo, n in reparto.items()
        ],
    )


def _cosechar(
    sesion: Session, tanda: Row[Any], datos: OperacionCosechar
) -> OperacionRealizada:
    if _ya_cosechada(sesion, tanda.id):
        raise ConflictoError(
            "La tanda ya se cosecho: se cosecha una sola vez.",
            code="tanda_cosechada",
        )
    hay = _contar(sesion, CONTAR_EN_ESTADO, tanda.id, estado="introducida")
    if hay == 0:
        raise ConflictoError(
            "La tanda no tiene celdas en el parque: primero hay que introducirlas.",
            code="transicion_invalida",
        )
    if datos.cantidad > hay:
        _excedida(datos.cantidad, hay, "introducida")
    ultima = sesion.execute(ULTIMA_INTRODUCCION, {"tanda": tanda.id}).scalar_one()
    _no_antes_de(datos.fecha, ultima, "la ultima introduccion")

    parametros = {"tanda": tanda.id, "fecha": datos.fecha}
    sesion.execute(ENJAULAR, {**parametros, "cantidad": datos.cantidad})
    sesion.execute(DESCARTAR_RESTO, parametros)
    return _resultado(
        sesion,
        tanda.id,
        datos.accion,
        datos.cantidad,
        descartadas_en_cosecha=hay - datos.cantidad,
    )


def _pasar_a_banco(
    sesion: Session, tanda: Row[Any], datos: OperacionPasarABanco
) -> OperacionRealizada:
    # El mismo lock de raza que la reserva y el despacho: un despacho "sin
    # banco" de esta raza termina antes de contar, y no hay celdas en juego.
    sesion.execute(BLOQUEAR_RAZA, {"raza_id": tanda.raza_id})
    hay = _contar(sesion, CONTAR_ENJAULADAS_SIN_BANCO, tanda.id)
    if datos.cantidad > hay:
        _excedida(datos.cantidad, hay, "enjaulada sin banco")

    # Bloquea el banco y despues cuenta (repositorio_bancos.bloquear): dos
    # lotes simultaneos no pasan la capacidad.
    banco = repositorio_bancos.bloquear(sesion, datos.banco_id)
    if not banco.activo:
        raise ConflictoError(
            "El banco esta dado de baja: no admite reinas.", code="banco_inactivo"
        )
    caben = banco.capacidad_max - banco.reinas_en_banco
    if datos.cantidad > caben:
        raise ConflictoError(
            f"En el banco caben {caben} reinas mas.",
            code="banco_lleno",
            details={"caben": caben, "capacidad_max": banco.capacidad_max},
        )
    movidas = sesion.execute(
        PASAR_A_BANCO,
        {"tanda": tanda.id, "banco": datos.banco_id, "cantidad": datos.cantidad},
    ).all()
    if len(movidas) < datos.cantidad:
        # Con el lock de raza no deberia pasar; si pasa, nada a medias.
        _excedida(datos.cantidad, len(movidas), "enjaulada sin banco")
    return _resultado(sesion, tanda.id, datos.accion, datos.cantidad)


def operar(
    sesion: Session, tanda_id: UUID, datos: OperacionCrear
) -> OperacionRealizada:
    """Aplica una operacion por lote. Todo o nada: corre en una transaccion."""
    tanda = _bloquear_tanda(sesion, tanda_id)
    if isinstance(datos, OperacionDescartar):
        return _descartar(sesion, tanda, datos)
    if isinstance(datos, OperacionIntroducir):
        return _introducir(sesion, tanda, datos)
    if isinstance(datos, OperacionCosechar):
        return _cosechar(sesion, tanda, datos)
    return _pasar_a_banco(sesion, tanda, datos)
